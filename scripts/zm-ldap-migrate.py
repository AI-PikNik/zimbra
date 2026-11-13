#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
zm-ldap-migrate.py — перенос Zimbra со старого сервера на новый: LDAP-объекты и содержимое ящиков.

РЕЖИМЫ (можно вместе; выполняются по порядку LDAP -> данные -> переиндексация)
  --ldap      COS, домены, учётки (прежний zimbraId и хэш пароля), подписи, персоны, внешние ящики,
              алиасы, списки рассылки, глобальные делегированные права
  --data      содержимое ящиков (REST, tgz): почта, контакты, календари, задачи, портфель
  --reindex   переиндексация ящиков на новом сервере (поиск по почте)
УТОЧНЕНИЯ РЕЖИМА
  --verify    сверка старого и нового, ничего не меняет. Без режима — сверка LDAP и данных
  --dry-run   только план, ничего не меняет. Без режима — план LDAP и данных
ВЫБОР ОБЪЕКТОВ (для всех режимов)
  --domain D  только домен D (можно несколько раз или через запятую): его учётки, списки,
              алиасы и их данные. Без --domain — все домены, кроме exclude_domains из настроек
  --only A    только учётка A (можно несколько раз)

ПРИМЕРЫ (на НОВОМ сервере от пользователя zimbra)
  ... -c migrate.conf --dry-run                      план: LDAP и данные
  ... -c migrate.conf --verify                       сверка: LDAP и данные
  ... -c migrate.conf --ldap                         перенос LDAP
  ... -c migrate.conf --ldap --domain example.org         LDAP только домена example.org
  ... -c migrate.conf --data                         перенос всех данных ящиков
  ... -c migrate.conf --data --domain example.org         данные только домена example.org
  ... -c migrate.conf --data --types message         только почта
  ... -c migrate.conf --data --verify                сверка количества по ящикам
  ... -c migrate.conf --data --sync                  добрать новое с прошлого переноса
  ... -c migrate.conf --data --force                 всё заново
  ... -c migrate.conf --data --force --diff-only     заново только там, где не сошлось
  ... -c migrate.conf --data --sync --reindex        финальная синхронизация перед переездом
  ... -c migrate.conf --reindex                      переиндексация
  (... = python3 zm-ldap-migrate.py)

ПОВТОРНЫЙ ЗАПУСК --data
                 почта                               контакты, календари, задачи, портфель
  --data         только ящики, где ещё не переносилась   так же
  --sync         новое с прошлого переноса           очистить на новом и загрузить заново
  --force        полностью (повторы Zimbra отсеет)   очистить на новом и загрузить заново
  Дублей нет ни в одном режиме. Очистка стирает изменения, сделанные на НОВОМ сервере, поэтому
  --sync и --force — только пока пользователи работают на старом; перед очисткой скрипт
  спрашивает подтверждение (без вопроса: --yes).

Старый сервер только читается. Журнал, отчёты и выгрузки — в workdir из настроек.
"""
import argparse
import base64
import configparser
from json import dumps as json_dumps, loads as json_loads
import datetime
import os
import re
import secrets
import shlex
import shutil
import ssl
import stat
import subprocess
import tarfile
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

DATA_TYPES_DEFAULT = "contact,task,document,appointment"
DATA_TYPES_ALL = {"message", "contact", "appointment", "task", "document"}

# ---------------------------------------------------------------- атрибуты, которые НЕ переносим
SKIP_COMMON = {
    "objectClass", "zimbraId", "zimbraCreateTimestamp",
    "entryUUID", "entryCSN", "createTimestamp", "modifyTimestamp",
    "creatorsName", "modifiersName", "structuralObjectClass",
}
SKIP_COS = SKIP_COMMON | {"cn", "zimbraMailHostPool"}           # MailHostPool — id серверов старой установки
SKIP_DOMAIN = SKIP_COMMON | {
    "dc", "o", "zimbraDomainName", "zimbraDomainType",
    "zimbraSSLCertificate", "zimbraSSLPrivateKey", "zimbraVirtualIPAddress",   # сертификаты доменов ломают nginx
    "zimbraGalAccountId", "zimbraGalDefinitionLastModifiedTime",                # galsync создаётся заново
    "zimbraAggregateQuotaLastUsage",
    "zimbraDomainDefaultCOSId", "zimbraDomainDefaultExternalUserCOSId",         # переназначаются по имени COS
}
SKIP_ACCOUNT = SKIP_COMMON | {
    "uid", "mail", "zimbraMailAlias",                       # алиасы создаются через zmprov aaa
    "zimbraMailHost", "zimbraMailTransport",                # указывают на старый сервер
    "zimbraLastLogonTimestamp", "zimbraAuthTokens", "zimbraCsrfTokenData",
    "zimbraAuthTokenValidityValue",
    "zimbraPasswordLockoutFailureTime", "zimbraPasswordLockoutLockedTime",
    "zimbraCOSId",                                          # переназначается по имени COS
}
SKIP_DL = SKIP_COMMON | {"uid", "mail", "zimbraMailAlias", "zimbraMailHost"}
SKIP_CHILD = {"zimbraCreateTimestamp", "entryUUID", "entryCSN", "createTimestamp",
              "modifyTimestamp", "creatorsName", "modifiersName", "structuralObjectClass"}

ACCOUNT_FILTER = ("(&(objectClass=zimbraAccount)(!(objectClass=zimbraCalendarResource))"
                  "(!(zimbraIsSystemAccount=TRUE))(!(zimbraIsSystemResource=TRUE)))")
CHILD_CLASSES = ("zimbraSignature", "zimbraIdentity", "zimbraDataSource")

LOG = None
SCOPED = False


# ---------------------------------------------------------------- вывод: цвет и форматирование
# Цвет только в терминале; в журнал и при перенаправлении вывода — обычный текст. Отключить: NO_COLOR=1
COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR") and os.environ.get("TERM") != "dumb"
ANSI = re.compile(r"\033\[[0-9;]*m")
_STYLE = {"head": "1;36", "total": "1;35", "ok": "32", "err": "1;31", "bad": "31", "warn": "33",
          "dim": "2", "bold": "1", "th": "1;4"}


def c(kind, text):
    return f"\033[{_STYLE[kind]}m{text}\033[0m" if COLOR and text else str(text)


def _width():
    return max(60, min(shutil.get_terminal_size((120, 20)).columns, 160))


def _wrap(text, first, rest):
    import textwrap
    return textwrap.fill(text, width=_width(), initial_indent=first, subsequent_indent=rest,
                         break_on_hyphens=False, break_long_words=False)


def _style(line):
    """Оформление строки по её началу. Строки, уже содержащие цвет, не трогаются."""
    if not line or ANSI.search(line):
        return line
    if line.startswith("====="):
        return c("total", line)
    if line.startswith("==="):
        return c("head", "━━ " + line[3:].strip() + " " + "━" * max(3, _width() - len(line) - 4))
    if line.startswith("ОШИБКА"):
        return c("err", _wrap(line, "", "  "))
    if line.startswith(("  ! ", "    ! ")):
        ind = line[:line.index("!")]
        return c("warn", _wrap(line.strip()[2:], ind + "⚠ ", ind + "  "))
    if line.startswith("  ✗ "):
        return c("bad", _wrap(line.strip(), "  ", "    "))
    if line.startswith(("  … ", "  Обозначения", "Журнал:", "  Подробно", "  Даты", "    на новом")):
        return c("dim", _wrap(line.strip(), "  " if line.startswith(" ") else "", "    "))
    if line.startswith(("Режим:", "Домены:", "Учётки:")):
        k, v = line.split(":", 1)
        return c("bold", k + ":") + v
    if line.startswith(("  Тип ", "  Объекты")):
        return c("th", line)
    return line


def log(msg=""):
    msg = str(msg)
    print("\n".join(_style(x) for x in msg.split("\n")), flush=True)
    if LOG:
        LOG.write(ANSI.sub("", msg) + "\n")
        LOG.flush()


def die(msg):
    log("ОШИБКА: " + msg)
    sys.exit(1)


# ---------------------------------------------------------------- LDIF
def parse_ldif(text):
    """Разбор вывода ldapsearch -LLL. Возвращает список (dn, {attr: [bytes, ...]})."""
    entries, lines = [], []
    for raw in text.splitlines():
        if raw.startswith(" ") and lines:          # строка-продолжение
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    dn, attrs = None, {}

    def flush():
        if dn is not None:
            entries.append((dn, attrs))

    for line in lines + [""]:
        if line == "":
            flush()
            dn, attrs = None, {}
            continue
        if line.startswith("#"):
            continue
        m = re.match(r"^([A-Za-z0-9;-]+)(::?)\s?(.*)$", line)
        if not m:
            continue
        name, sep, val = m.groups()
        data = base64.b64decode(val) if sep == "::" else val.encode("utf-8")
        if name.lower() == "dn":
            dn = data.decode("utf-8")
        else:
            attrs.setdefault(name, []).append(data)
    return entries


def b64(v):
    return base64.b64encode(v if isinstance(v, bytes) else v.encode("utf-8")).decode("ascii")


def first(attrs, name, default=None):
    v = attrs.get(name)
    return v[0].decode("utf-8") if v else default


def ldif_modify(dn, add_oc=(), replace=None):
    out = ["dn:: " + b64(dn), "changetype: modify"]
    for oc in add_oc:
        out += ["add: objectClass", "objectClass: " + oc, "-"]
    for name, values in (replace or {}).items():
        out.append("replace: " + name)
        out += [name + ":: " + b64(v) for v in values]
        out.append("-")
    return "\n".join(out) + "\n\n"


def ldif_add(dn, attrs):
    out = ["dn:: " + b64(dn), "changetype: add"]
    for name, values in attrs.items():
        out += [name + ":: " + b64(v) for v in values]
    return "\n".join(out) + "\n\n"


def dn_to_name(dn):
    """uid=user,ou=people,dc=example,dc=com -> user@example.com"""
    parts = [p.split("=", 1) for p in dn.split(",")]
    uid = parts[0][1]
    domain = ".".join(v for k, v in parts if k.strip().lower() == "dc")
    return f"{uid}@{domain}"


# ---------------------------------------------------------------- вызовы утилит
class Ldap:
    def __init__(self, url, bind_dn, bind_pw, starttls, workdir, tag):
        self.url, self.bind_dn, self.starttls = url, bind_dn, starttls
        self.pwfile = os.path.join(workdir, f".{tag}.pw")
        with open(os.open(self.pwfile, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
            f.write(bind_pw)
        self.env = dict(os.environ, LDAPTLS_REQCERT="allow")

    def _base(self, tool):
        cmd = [tool, "-x", "-H", self.url, "-D", self.bind_dn, "-y", self.pwfile]
        if self.starttls:
            cmd.append("-ZZ")
        return cmd

    def search(self, flt, base="", scope="sub", attrs=()):
        cmd = self._base("ldapsearch") + ["-LLL", "-o", "ldif-wrap=no", "-E", "pr=500/noprompt",
                                           "-s", scope, "-b", base, flt] + list(attrs)
        r = subprocess.run(cmd, capture_output=True, env=self.env)
        if r.returncode not in (0, 32):           # 32 = No such object
            die(f"ldapsearch {self.url} {flt}: {r.stderr.decode(errors='replace').strip()}")
        return parse_ldif(r.stdout.decode("utf-8", errors="replace"))

    def modify(self, ldif, add=False):
        cmd = self._base("ldapmodify") + (["-a"] if add else [])
        r = subprocess.run(cmd, input=ldif.encode("utf-8"), capture_output=True, env=self.env)
        return r.returncode, r.stderr.decode(errors="replace").strip()

    def cleanup(self):
        try:
            os.remove(self.pwfile)
        except OSError:
            pass


def zmprov_batch(lines, workdir, name, dry):
    path = os.path.join(workdir, name)
    with open(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        f.write("\n".join(lines) + ("\n" if lines else ""))
    if not lines:
        return
    log(f"  команд zmprov: {len(lines)} -> {path}")
    if dry:
        return
    r = subprocess.run(["zmprov", "-l", "-f", path], capture_output=True, text=True)
    errs = [re.sub(r"(prov> )+", "", l) for l in (r.stdout + r.stderr).splitlines() if "ERROR" in l]
    for e in errs:
        log("  ! " + e)


def temp_password():
    """Временный пароль для zmprov ca: проходит любую разумную парольную политику COS
    (буквы обоих регистров, цифры, знаки). Сразу после создания заменяется хэшем со старого сервера."""
    sets = ("ABCDEFGHJKLMNPQRSTUVWXYZ", "abcdefghijkmnopqrstuvwxyz", "23456789", "-_.")
    chars = [secrets.choice(s) for s in sets for _ in range(5)]
    chars += [secrets.choice("".join(sets)) for _ in range(12)]
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)


def zmlocal(key):
    r = subprocess.run(["zmlocalconfig", "-s", "-m", "nokey", key], capture_output=True, text=True)
    if r.returncode != 0:
        die(f"zmlocalconfig {key}: {r.stderr.strip()}")
    return r.stdout.rstrip("\n")


# ---------------------------------------------------------------- применение атрибутов
def apply_attrs(new, dn, add_oc, replace, dry, report, label):
    if not replace and not add_oc:
        return
    if dry:
        return
    rc, err = new.modify(ldif_modify(dn, add_oc, replace))
    if rc == 0:
        return
    # пакет отклонён (например, атрибута нет в схеме 10.1) — по одному
    for oc in add_oc:
        rc, err = new.modify(ldif_modify(dn, [oc], None))
        if rc not in (0, 20):                       # 20 = значение уже есть
            report.append(f"{label}: objectClass {oc}: {err}")
    for name, values in replace.items():
        rc, err = new.modify(ldif_modify(dn, (), {name: values}))
        if rc != 0:
            report.append(f"{label}: {name}: {err.splitlines()[-1] if err else rc}")


# ---------------------------------------------------------------- перенос содержимого ящиков (REST)
def human(n):
    for u in ("Б", "КБ", "МБ", "ГБ"):
        if n < 1024:
            return f"{n:.0f} {u}"
        n /= 1024
    return f"{n:.1f} ТБ"


TYPE_NAMES = {"message": "почта", "contact": "контакты", "task": "задачи", "document": "портфель",
              "appointment": "календари"}
# тип для --types -> вид папки (view) в ответе GetFolderRequest; папки без view — почтовые
TYPE_VIEW = {"message": "message", "contact": "contact", "task": "task", "document": "document",
             "appointment": "appointment"}
TRASH_ID = "3"


def log_file(msg):
    """Только в журнал, без вывода на экран."""
    if LOG:
        LOG.write(msg + "\n")
        LOG.flush()


def hms(sec):
    sec = int(sec)
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


class ZServer:
    """SOAP и REST сервера Zimbra через порт администрирования (7071)."""

    def __init__(self, label, base, user, password, ctx, mail_soap=None):
        self.label, self.base, self.user, self.password, self.ctx = label, base.rstrip("/"), user, password, ctx
        self.mail_soap = mail_soap or [self.base + "/service/soap"]
        self._tok = None
        self._lock = threading.Lock()
        self.method = None            # способ почтовых SOAP-запросов, который сработал (запоминается)

    @staticmethod
    def _fault(text):
        """Текст ошибки из ответа Zimbra: JSON- или XML-SOAP Fault, либо страница ошибки."""
        try:
            f = json_loads(text)["Body"]["Fault"]
            code = f.get("Detail", {}).get("Error", {}).get("Code", "")
            return (f"{code}: " if code else "") + f["Reason"]["Text"]
        except Exception:
            pass
        code = re.search(r"<Code>([^<]+)</Code>", text)
        msg = re.search(r"<(?:soap:)?Text[^>]*>([^<]+)</(?:soap:)?Text>", text) or \
            re.search(r"<faultstring>([^<]+)</faultstring>", text)
        if code or msg:
            return ((code.group(1) + ": ") if code else "") + (msg.group(1).strip() if msg else "")
        m = re.search(r"(?:Error|ERROR|error) \d+[^<\n]{0,200}", text)
        return m.group(0).strip() if m else re.sub(r"\s+", " ", text)[:200]

    def _call(self, url, body, token=None, cookie="ZM_AUTH_TOKEN", timeout=120, account=None):
        ctxh = {"_jsns": "urn:zimbra"}
        hdr = {"Content-Type": "application/json"}
        if token:
            ctxh["authToken"] = {"_content": token}
            hdr["Cookie"] = f"{cookie}={token}"
        if account:
            ctxh["account"] = {"by": "name", "_content": account}
        data = json_dumps({"Header": {"context": ctxh}, "Body": body}).encode()
        for pause in (2, 5, 10, 20, None):          # 429 — защита Zimbra от перегрузки: ждём и повторяем
            req = urllib.request.Request(url, data=data, headers=hdr)
            try:
                with urllib.request.urlopen(req, context=self.ctx, timeout=timeout) as resp:
                    return json_loads(resp.read().decode())["Body"]
            except urllib.error.HTTPError as e:
                if e.code == 429 and pause:
                    time.sleep(pause)
                    continue
                raise RuntimeError(f"HTTP {e.code}: {self._fault(e.read().decode(errors='replace'))}") from None

    def admin_token(self, force=False):
        with self._lock:
            if self._tok and not force:
                return self._tok
            b = self._call(self.base + "/service/admin/soap",
                           {"AuthRequest": {"_jsns": "urn:zimbraAdmin", "name": self.user, "password": self.password}})
            self._tok = b["AuthResponse"]["authToken"][0]["_content"]
            return self._tok

    def admin(self, body):
        for force in (False, True):
            try:
                return self._call(self.base + "/service/admin/soap", body, self.admin_token(force),
                                  cookie="ZM_ADMIN_AUTH_TOKEN")
            except RuntimeError as e:
                if force or "auth" not in str(e).lower():
                    raise

    def mbox_size(self, zid):
        """Размер ящика в байтах (административный GetMailboxRequest) или None."""
        try:
            b = self.admin({"GetMailboxRequest": {"_jsns": "urn:zimbraAdmin", "mbox": {"id": zid}}})
            return int(b["GetMailboxResponse"]["mbox"][0]["s"])
        except Exception:
            return None

    # --- почтовые SOAP-запросы от имени ящика: два способа, сработавший запоминается
    def _m_delegated(self, name, body, url):
        b = self.admin({"DelegateAuthRequest": {"_jsns": "urn:zimbraAdmin",
                                                "account": {"by": "name", "_content": name}}})
        tok = b["DelegateAuthResponse"]["authToken"][0]["_content"]
        return self._call(url, body, tok)

    def _m_admin_ctx(self, name, body):
        for force in (False, True):
            try:
                return self._call(self.base + "/service/admin/soap", body, self.admin_token(force),
                                  cookie="ZM_ADMIN_AUTH_TOKEN", account=name)
            except RuntimeError as e:
                if force or "auth" not in str(e).lower():
                    raise

    def mail(self, name, body):
        methods = [("делегированный токен, " + u, lambda n, b, u=u: self._m_delegated(n, b, u)) for u in self.mail_soap]
        methods.append(("токен администратора + ящик в заголовке", self._m_admin_ctx))
        if self.method is not None:
            methods = [methods[self.method]]
        errors = []
        for i, (label, fn) in enumerate(methods):
            try:
                res = fn(name, body)
                if self.method is None:
                    self.method = i
                return res
            except Exception as e:
                errors.append(f"[{label}] {e}")
        raise RuntimeError(" | ".join(errors))

    def folders(self, name):
        """Плоский список папок ящика: (id, путь, вид, число элементов). Ссылки на чужие папки не входят."""
        b = self.mail(name, {"GetFolderRequest": {"_jsns": "urn:zimbraMail"}})
        out = []

        def walk(f, path):
            p = f"{path}/{f.get('name', '')}" if f.get("id") != "1" else ""
            out.append((f.get("id"), p or "/", f.get("view", "message"), int(f.get("n", 0))))
            for sub in f.get("folder", []):
                walk(sub, p)

        for root in b["GetFolderResponse"].get("folder", []):
            walk(root, "")
        return out

    def counts(self, name):
        acc = {}
        for _, _, view, n in self.folders(name):
            acc[view] = acc.get(view, 0) + n
        return acc

    def empty_views(self, name, views):
        """Очистить на этом сервере папки указанных видов (кроме корзины). Возвращает число удалённых элементов."""
        removed = 0
        for fid, path, view, n in self.folders(name):
            if view in views and fid not in ("1", TRASH_ID) and n:
                self.mail(name, {"FolderActionRequest": {"_jsns": "urn:zimbraMail",
                                                         "action": {"op": "empty", "id": fid, "recursive": False}}})
                removed += n
        return removed

    def reindex(self, zid, action):
        b = self.admin({"ReIndexRequest": {"_jsns": "urn:zimbraAdmin", "action": action, "mbox": {"id": zid}}})
        r = b.get("ReIndexResponse", {})
        prog = r.get("progress", {})
        if isinstance(prog, list):
            prog = prog[0] if prog else {}
        return r.get("status", ""), {k: int(prog.get(k, 0)) for k in ("numSucceeded", "numFailed", "numRemaining")}


def data_migrate(cfg, old, new, exclude, only, dry, workdir, types, since, workers, keep,
                 verify_only=False, sync=False, chunk_mb=2048, force=False, assume_yes=False,
                 reindex=False, reindex_only=False, diff_only=False):
    if reindex_only:
        cfg = cfg if cfg.has_section("old_rest") else None
    if cfg is None:
        r = {"url": "http://-", "admin": "", "password": "", "verify_tls": "no"}
    elif not cfg.has_section("old_rest"):
        die("для --data нужна секция [old_rest] в файле настроек (см. migrate.conf.example)")
    else:
        r = cfg["old_rest"]
    if not reindex_only and not r.get("password", "").strip() or "ВСТАВИТЬ" in r.get("password", ""):
        die("в секции [old_rest] не заполнен password")
    ctx = ssl.create_default_context()
    if str(r.get("verify_tls", "no")).lower() not in ("yes", "true", "1", "on"):
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    local_ctx = ssl.create_default_context()
    local_ctx.check_hostname = False
    local_ctx.verify_mode = ssl.CERT_NONE
    auth = "Basic " + b64(f"{r['admin']}:{r['password']}")

    host = zmlocal("zimbra_server_hostname")
    S_old = ZServer("старый", r["url"], r["admin"], r["password"], ctx)
    # Загрузка — напрямую на имя этого сервера, не зависит от DNS публичных имён доменов
    S_new = ZServer("новый", f"https://{host}:7071", zmlocal("zimbra_user"), zmlocal("zimbra_ldap_password"),
                    local_ctx, mail_soap=[f"https://{host}:7071/service/soap", f"https://{host}:8443/service/soap"])
    try:
        S_new.admin_token()
    except Exception as e:
        die(f"не удалось получить токен администратора на {S_new.base}: {e}")
    old_soap = False
    if not reindex_only:
        try:
            S_old.admin_token()
            old_soap = True
        except Exception as e:
            log(f"  ! SOAP старого сервера недоступен ({e}) — сверка количества со старым сервером не выполняется")

    o = read_old(old, exclude, only)
    acc_id = {dn_to_name(dn).lower(): first(a, "zimbraId") for dn, a in o["acc"]}
    accounts = sorted(dn_to_name(dn) for dn, _ in o["acc"])
    n_names = {dn_to_name(dn).lower() for dn, _ in new.search("(objectClass=zimbraAccount)", attrs=["uid"])}
    missing = [a for a in accounts if a.lower() not in n_names]
    accounts = [a for a in accounts if a.lower() in n_names]
    if missing and not accounts:
        die(f"на новом сервере не найдено ни одной из {len(missing)} учёток (LDAP нового сервера вернул "
            f"{len(n_names)} учёток). Если LDAP уже переносился — проверьте LDAP нового сервера: "
            f"zmprov -l gaa | wc -l. Если ещё нет — сначала: --ldap")

    tlist = list(dict.fromkeys(types.split(",")))
    views = [TYPE_VIEW[t] for t in tlist]
    vname = {TYPE_VIEW[t]: TYPE_NAMES[t] for t in tlist}
    chunk_bytes = chunk_mb * 1024 * 1024
    run_date = datetime.date.today()
    t_start = time.time()

    # ---- состояние: дата последнего успешного переноса по ящику и типу
    state_path = os.path.join(os.path.dirname(workdir), "data-state.json")
    try:
        with open(state_path) as f:
            state = json_loads(f.read())
    except (OSError, ValueError):
        state = {}
    state_lock = threading.Lock()

    def save_state():
        tmp = state_path + ".tmp"
        with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
            f.write(json_dumps(state, indent=1, ensure_ascii=False))
        os.replace(tmp, state_path)

    def type_plan(name):
        """Что делать с ящиком: {тип: дата 'после' или None (полностью)} и список пропущенных типов."""
        st = state.get(name.lower(), {})
        todo, skipped = {}, []
        for t in tlist:
            again = t in st
            if t == "message":
                # почта: повторы Zimbra отсеивает сама, поэтому можно добирать по дате
                if since:
                    todo[t] = since.date()
                elif sync:
                    todo[t] = (datetime.date.fromisoformat(st[t]) - datetime.timedelta(days=1)) if again else None
                elif again and not force:
                    skipped.append(t)
                else:
                    todo[t] = None
            else:
                # остальное: всегда очистка на новом и полная загрузка — дублей не бывает
                if again and not (force or sync or since):
                    skipped.append(t)
                else:
                    todo[t] = None
        return todo, skipped

    def reloads(name):
        """Типы, которые у ящика уже переносились и будут очищены на новом и загружены заново."""
        st = state.get(name.lower(), {})
        return [t for t in type_plan(name)[0] if t != "message" and t in st]

    # ---- заголовок
    out_lock = threading.Lock()
    if reindex_only:
        title = "Переиндексация ящиков на новом сервере"
    elif verify_only:
        title = "Сверка содержимого ящиков"
    else:
        title = "Перенос содержимого ящиков"
    mode = ""
    if not verify_only and not reindex_only:
        mode = (" — досинхронизация" if sync else f" — изменённое после {since:%d.%m.%Y}" if since else
                " — всё заново" if force else "")
    log(f"\n=== {title}" + ("" if reindex_only else f": {', '.join(vname[v] for v in views)}"
                                                  + ("" if "message" in views else " (БЕЗ ПОЧТЫ)")) + mode)
    log(f"  ящиков: {len(accounts)}; потоков: {workers}"
        + ("" if reindex_only else f"; источник: {S_old.base}; приёмник: {S_new.base}"))
    if missing:
        log(f"  ! нет на новом сервере, пропущены ({len(missing)}): {', '.join(missing[:5])}"
            + (f" и ещё {len(missing) - 5} — список в журнале" if len(missing) > 5 else ""))
        log_file("    все пропущенные: " + ", ".join(missing))
    if not verify_only and not reindex_only:
        if diff_only and not (force or sync or since):
            log("  ! --diff-only без --force: в ящиках, куда данные уже переносились, они пропускаются. "
                "Перенести заново там, где не сошлось: --force --diff-only")
        sk = sum(1 for a in accounts if type_plan(a)[1])
        if sk:
            log(f"  ! у {sk} ящиков часть типов уже переносилась — пропускаются. "
                f"Добрать новое: --sync; перенести всё заново: --force")
        rl = [a for a in accounts if reloads(a)]
        if rl and not dry and not assume_yes:
            log(f"\n  Контакты, календари, задачи и портфель в {len(rl)} ящиках будут ОЧИЩЕНЫ на новом сервере")
            log("  и загружены со старого заново. Всё, что пользователи изменили в них на НОВОМ сервере, пропадёт.")
            if not sys.stdin.isatty():
                die("нужно подтверждение: пользователи ещё работают на старом сервере? Если да — добавьте --yes")
            try:
                ans = input("  Пользователи ещё работают на СТАРОМ сервере? Введите да, чтобы продолжить: ")
            except EOFError:
                ans = ""
            log_file(f"  ответ: {ans}")
            if ans.strip().lower() not in ("да", "yes", "y", "д"):
                die("отменено")
    if not reindex_only:
        log("  Обозначения: «почта 120 ✓» — на новом столько же, сколько на старом; "
            "«почта 118≠120 ✗» — на новом 118, на старом 120; «(+5)» — добавлено этим запуском")

    def export_url(name, types_, after=None, before=None):
        q = {"fmt": "tgz", "types": types_}
        cond = []
        if after:
            cond.append(f'after:"{after:%m/%d/%Y}"')        # after: — строго позже этого дня
        if before:
            cond.append(f'before:"{before:%m/%d/%Y}"')      # before: — строго раньше этого дня
        if cond:
            q["query"] = " ".join(cond)
        return f"{S_old.base}/home/{urllib.parse.quote(name)}/?{urllib.parse.urlencode(q)}"

    def jobs_for(todo, mbox_size):
        """Части выгрузки: (метка, типы, after, before). Типы с одной датой — вместе;
        почта большого ящика — по годам."""
        groups = {}
        for t, sd in todo.items():
            groups.setdefault((sd, t != "message"), []).append(t)
        big = mbox_size is not None and mbox_size > chunk_bytes
        jobs = []
        for (sd, pre), ts in groups.items():
            # каждый тип — отдельным архивом: в общем архиве нескольких типов часть контактов терялась
            for t in ts:
                if t != "message":
                    jobs.append((TYPE_NAMES[t], t, sd, None, pre))
            if "message" not in ts:
                continue
            if not big:
                jobs.append(("почта", "message", sd, None, False))
                continue
            y_now = run_date.year
            ranges = [("до 2000", None, datetime.date(2000, 1, 1))]
            ranges += [(str(y), datetime.date(y - 1, 12, 31), datetime.date(y + 1, 1, 1))
                       for y in range(2000, y_now + 1)]
            ranges += [(f"после {y_now}", datetime.date(y_now, 12, 31), None)]
            for label, a, b in ranges:
                if sd and b and b <= sd + datetime.timedelta(days=1):
                    continue                                 # период целиком до даты досинхронизации
                jobs.append((f"почта {label}", "message", max(a, sd) if (a and sd) else (a or sd), b, False))
        return sorted(jobs, key=lambda j: not j[4])          # сначала заменяемые

    if accounts and not verify_only and not reindex_only:
        probe = urllib.request.Request(f"{S_old.base}/home/{urllib.parse.quote(accounts[0])}/Tasks?fmt=json",
                                       headers={"Authorization": auth})
        try:
            urllib.request.urlopen(probe, context=ctx, timeout=60).read(1)
            log("  доступ к REST старого сервера: OK")
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                die(f"REST старого сервера: отказ в доступе (HTTP {e.code}) — проверьте admin/password в [old_rest]")
            log(f"  доступ к REST старого сервера: HTTP {e.code} (аутентификация прошла)")
        except Exception as e:
            die(f"REST старого сервера недоступен: {e}. Порт 7071 старого сервера открыт для этого сервера?")
    if dry and reindex_only:
        log(f"  будут переиндексированы: {', '.join(accounts[:10])}" + (" …" if len(accounts) > 10 else ""))
        return []
    if dry:
        groups = {}
        for a in accounts:
            todo, skipped = type_plan(a)
            rl_ = reloads(a)
            desc = ", ".join(f"{TYPE_NAMES[t]} " + (f"после {d:%d.%m.%Y}" if d else
                                                     "заново (очистка на новом)" if t in rl_ else "полностью")
                             for t, d in todo.items()) or "нечего переносить"
            if skipped:
                desc += f"; пропуск (уже перенесено): {', '.join(TYPE_NAMES[t] for t in skipped)}"
            groups.setdefault(desc, []).append(a)
            log_file(f"  план {a}: {desc}")
        log(c("bold", f"  План по {len(accounts)} ящикам") + c("dim", " (по каждому ящику — в журнале)"))
        for desc, names in sorted(groups.items(), key=lambda x: -len(x[1])):
            print(c("bold", f"    {len(names):>4} ящ.") + "\n" + _wrap(desc, "      • ", "        ").replace(", ", "\n      • ")
                  + ("\n" + c("dim", _wrap(", ".join(names), "        ", "        ")) if len(names) <= 15 else ""))
            log_file(f"    {len(names):>4} ящ.: {desc}" + (f"\n           {', '.join(names)}" if len(names) <= 15 else ""))
        return []

    ddir = os.path.join(workdir, "data")
    os.makedirs(ddir, mode=0o700, exist_ok=True)

    # ---- размеры ящиков на старом — для прогресса и оценки времени
    sizes = {}
    if not verify_only and not reindex_only and old_soap:
        ex8 = ThreadPoolExecutor(max_workers=8)
        try:
            for a, s_ in zip(accounts, ex8.map(lambda a: S_old.mbox_size(acc_id.get(a.lower(), "")), accounts)):
                sizes[a] = s_
        finally:
            ex8.shutdown(wait=False, cancel_futures=True)
        total_bytes = sum(v for v in sizes.values() if v)
        log(f"  общий объём ящиков на старом сервере: {human(total_bytes)}")
    else:
        total_bytes = 0

    old_state = {"on": old_soap, "warned": False}

    def safe_counts(srv, name):
        if srv is S_old and not old_state["on"]:
            return None, "нет доступа"
        try:
            return srv.counts(name), None
        except Exception as e:
            if srv is S_old and srv.method is None and "HTTP 429" not in str(e):
                with out_lock:
                    old_state["on"] = False
                    if not old_state["warned"]:
                        old_state["warned"] = True
                        log(f"  ! подсчёт на старом сервере не удался, сверка со старым отключена: {e}")
                return None, "нет доступа"
            return None, str(e)

    # ---- прогресс
    progress, prog_lock = {}, threading.Lock()
    stats = {"done": 0, "done_bytes": 0}

    def set_prog(name, text):
        with prog_lock:
            if text is None:
                progress.pop(name, None)
            else:
                progress[name] = text

    class ProgFile:
        def __init__(self, f, cb):
            self.f, self.cb, self.n, self.last = f, cb, 0, 0

        def read(self, size=-1):
            d = self.f.read(size)
            self.n += len(d)
            if self.n - self.last >= (1 << 20) or not d:
                self.last = self.n
                self.cb(self.n)
            return d

    def check_tgz(path):
        """Архив целиком читается и корректно завершён? Возвращает (число элементов, ошибка)."""
        if os.path.getsize(path) == 0:
            return 0, None
        try:
            n = 0
            with tarfile.open(path, "r:gz") as t:
                for m in t:
                    if m.name.endswith(".meta"):
                        n += 1
            return n, None
        except (tarfile.TarError, EOFError, OSError) as e:
            return None, f"архив оборван или повреждён ({e})"

    def export_part(name, url, path, tag):
        err = None
        for pause in (5, 15, 30, 60, None):
            req = urllib.request.Request(url, headers={"Authorization": auth})
            try:
                got = 0
                with urllib.request.urlopen(req, context=ctx, timeout=900) as resp, open(path, "wb") as f:
                    while True:
                        chunk = resp.read(1 << 20)
                        if not chunk:
                            break
                        f.write(chunk)
                        got += len(chunk)
                        set_prog(name, f"{tag}⇣{human(got)}")
                set_prog(name, f"{tag}проверка архива")
                n, err = check_tgz(path)
                if err is None:
                    log_file(f"    {name}: выгружено {tag or 'всё '}— элементов в архиве: {n}, {human(got)}")
                    return None
                log_file(f"    {name}: {tag}{err}, повтор выгрузки")
                err = f"выгрузка {tag}{err}"
            except urllib.error.HTTPError as e:
                if e.code != 429:
                    return f"выгрузка {tag}HTTP {e.code} — {ZServer._fault(e.read().decode(errors='replace'))}"
                err = f"выгрузка {tag}HTTP 429"
            except Exception as e:
                err = f"выгрузка {tag}{e}"
                log_file(f"    {name}: {err}, повтор выгрузки")
            if pause is None or cancel.is_set():
                break
            time.sleep(pause)
        return err

    def upload_part(name, path, size, tag):
        url = f"{S_new.base}/home/{urllib.parse.quote(name)}/?fmt=tgz&resolve=skip"
        err = None
        for force_tok in (False, True):
            with open(path, "rb") as f:
                body = ProgFile(f, lambda n: set_prog(name, f"{tag}⇡{human(n)}/{human(size)}"))
                req = urllib.request.Request(url, data=body, method="POST", headers={
                    "Cookie": f"ZM_ADMIN_AUTH_TOKEN={S_new.admin_token(force_tok)}",
                    "Content-Type": "application/x-compressed-tar", "Content-Length": str(size)})
                try:
                    with urllib.request.urlopen(req, context=local_ctx, timeout=3600) as resp:
                        resp.read()
                    return None
                except urllib.error.HTTPError as e:
                    err = f"загрузка {tag}HTTP {e.code} — {ZServer._fault(e.read().decode(errors='replace'))}"
                    if e.code != 401:
                        return err
                except Exception as e:
                    return f"загрузка {tag}{e}"
        return err

    def transfer(name, jobs, res):
        """Заменяемые типы: сначала выгрузка со старого, и только если она удалась — очистка на новом
        и загрузка. Если старый сервер недоступен, на новом ничего не удаляется."""
        total, parts_done = 0, 0
        for label, ttypes, after, before, pre in jobs:
            if cancel.is_set():
                return "ПРЕРВАН", "прерван пользователем", total
            tag = f"{label} " if label else ""
            path = os.path.join(ddir, f"{name}{'.' + label.replace(' ', '_') if label else ''}.{ttypes.replace(',', '-')}.tgz")
            err = export_part(name, export_url(name, ttypes, after, before), path, tag)
            if err:
                if os.path.exists(path):
                    os.remove(path)
                return "ОШИБКА", err + ("; на новом ничего не очищалось" if pre else ""), total
            if cancel.is_set():
                os.remove(path)
                return "ПРЕРВАН", "прерван пользователем" + ("; на новом ничего не очищалось" if pre else ""), total
            if pre:
                rv = [TYPE_VIEW[t] for t in ttypes.split(",")]
                set_prog(name, "очистка " + ", ".join(vname[v] for v in rv))
                try:
                    res["removed"] = res.get("removed", 0) + S_new.empty_views(name, rv)
                    if res["removed"]:
                        res["info"] = f"очищено на новом: {res['removed']}"
                except Exception as e:
                    os.remove(path)
                    return "ОШИБКА", f"очистка на новом: {e}", total
            size = os.path.getsize(path)
            if size == 0:
                os.remove(path)
                continue
            err = upload_part(name, path, size, tag)
            if not keep:
                os.remove(path)
            if err:
                return "ОШИБКА", err, total + size
            total += size
            parts_done += 1
        return ("OK" if parts_done else "НЕТ НОВЫХ"), "", total

    def do_reindex(name):
        zid = acc_id.get(name.lower())
        try:
            st_, pr = S_new.reindex(zid, "start")
        except Exception as e:
            if "already" not in str(e).lower() and "maintenance" not in str(e).lower():
                return f"переиндексация: {e}"
        t0, last = time.time(), {"numSucceeded": 0, "numFailed": 0, "numRemaining": 0}
        while time.time() - t0 < 4 * 3600:
            time.sleep(5)
            try:
                st_, pr = S_new.reindex(zid, "status")
            except Exception as e:
                return f"переиндексация, статус: {e}"
            if any(pr.values()):
                last = pr
            if st_ not in ("running", "started"):
                return last
            set_prog(name, f"индекс {pr['numSucceeded']}/{pr['numSucceeded'] + pr['numRemaining']}")
        return "переиндексация: не завершилась за 4 часа"

    def one(name):
        if cancel.is_set():
            return None
        todo, skipped = ({}, []) if (verify_only or reindex_only) else type_plan(name)
        res = dict(name=name, status="", msg="", size=0, rows=[], diff=False, info="", ridx=None)
        if reindex_only:
            set_prog(name, "индексация")
            try:
                r_ = do_reindex(name)
            finally:
                set_prog(name, None)
            if isinstance(r_, dict):
                res["status"], res["ridx"] = "OK", r_
            else:
                res["status"], res["msg"] = "ОШИБКА", r_
            return res
        oc, oerr = safe_counts(S_old, name)
        nb, nerr = safe_counts(S_new, name)
        na = nb
        same = (oc is not None and nb is not None and all(oc.get(v, 0) == nb.get(v, 0) for v in views))
        if diff_only and not verify_only and (oc is None or nb is None):
            res["status"], res["msg"] = "ОШИБКА", "--diff-only: не удалось посчитать количество на обоих серверах"
            return res
        if verify_only:
            res["status"] = "СВЕРКА"
        elif diff_only and same:
            res["status"] = "СОВПАДАЕТ"
        elif not todo:
            res["status"] = "ПРОПУЩЕН"
            res["info"] = "уже перенесено: " + ", ".join(TYPE_NAMES[t] for t in skipped)
        else:
            if diff_only:
                todo = {t: d for t, d in todo.items() if oc.get(TYPE_VIEW[t], 0) != nb.get(TYPE_VIEW[t], 0)}
            jobs = jobs_for(todo, sizes.get(name))
            log_file(f"  → начат: {name} ({human(sizes[name]) if sizes.get(name) else '?'}), частей: {len(jobs)}")
            set_prog(name, "старт")
            try:
                res["status"], res["msg"], res["size"] = transfer(name, jobs, res)
                if reindex and res["status"] == "OK" and "message" in todo:
                    r_ = do_reindex(name)
                    if isinstance(r_, dict):
                        res["ridx"] = r_
                    else:
                        res["msg"] = (res["msg"] + "; " if res["msg"] else "") + r_
            finally:
                set_prog(name, None)
            if skipped:
                res["info"] = "; ".join(x for x in (res["info"], "пропущено (уже перенесено): "
                                                    + ", ".join(TYPE_NAMES[t] for t in skipped)) if x)
            if len(jobs) > 1 and (sizes.get(name) or 0) > chunk_bytes:
                res["info"] = "; ".join(x for x in (res["info"], f"частей: {len(jobs)}") if x)
            na, nerr = safe_counts(S_new, name)
            if res["status"] in ("OK", "НЕТ НОВЫХ"):
                with state_lock:
                    st = state.setdefault(name.lower(), {})
                    for t in todo:
                        st[t] = run_date.isoformat()
                    save_state()
        for v in views:
            o_ = oc.get(v, 0) if oc is not None else None
            b_ = nb.get(v, 0) if nb is not None else None
            a_ = na.get(v, 0) if na is not None else None
            res["rows"].append((v, o_, b_, a_))
            if o_ is not None and a_ is not None and o_ != a_:
                res["diff"] = True
        if oerr and oerr != "нет доступа":
            res["msg"] = (res["msg"] + "; " if res["msg"] else "") + f"сверка со старым: {oerr}"
        if nerr:
            res["msg"] = (res["msg"] + "; " if res["msg"] else "") + f"подсчёт на новом: {nerr}"
        return res

    ST_KIND = {"OK": "ok", "СВЕРКА": "ok", "ОШИБКА": "err", "ПРЕРВАН": "err", "ПРОПУЩЕН": "dim",
               "СОВПАДАЕТ": "dim", "НЕТ НОВЫХ": "dim"}

    def line(res, n_total):
        bad = res["diff"] and res["status"] != "ОШИБКА"
        st_plain = res["status"] + (" ✗" if bad else "")
        st = c("bad" if bad else ST_KIND.get(res["status"], "bold"), st_plain) + " " * (11 - len(st_plain))
        pct = stats["done"] * 100 // max(1, n_total)
        head = c("dim", f"  [{stats['done']:>3}/{n_total} {pct:>3}%]") + f" {st} {res['name']:<34}"
        err = ("\n" + c("err", _wrap(res["msg"], "        ! ", "          "))) if res["msg"] else ""
        if reindex_only:
            p = res["ridx"]
            items = (f"проиндексировано {p['numSucceeded']}"
                     + (c("bad", f", ошибок {p['numFailed']}") if p["numFailed"] else "")) if p else ""
            return head + " " + items + err
        parts = []
        for v, o_, b_, a_ in res["rows"]:
            add = (a_ - b_) if (a_ is not None and b_ is not None) else 0
            if not (o_ or a_ or add):
                continue
            s_ = f"{vname[v]} {a_ if a_ is not None else '?'}"
            if o_ is not None and a_ is not None:
                s_ = c("ok", s_ + " ✓") if o_ == a_ else c("bad", f"{s_}≠{o_} ✗")
            if add:
                s_ += c("dim", f" ({add:+d})")
            parts.append(s_)
        body = c("dim", " · ").join(parts) or c("dim", "пусто")
        size_txt = "" if verify_only else (" " * 10 if res["status"] in ("ПРОПУЩЕН", "СОВПАДАЕТ")
                                           else f"{human(res['size']):>8}  ")
        extra = []
        if res.get("ridx"):
            extra.append(f"индекс: {res['ridx']['numSucceeded']}" + (f", ошибок {res['ridx']['numFailed']}"
                                                                     if res['ridx']['numFailed'] else ""))
        if res["info"]:
            extra.append(res["info"])
        return (head + " " + size_txt + body + (c("dim", f"  [{'; '.join(extra)}]") if extra else "") + err)

    stop = threading.Event()
    cancel = threading.Event()

    def heartbeat():
        while not stop.wait(60):
            with prog_lock:
                items = sorted(progress.items())
            if not items:
                continue
            el = time.time() - t_start
            head = f"  … {hms(el)} | готово {stats['done']}/{len(accounts)}"
            if total_bytes and stats["done_bytes"]:
                frac = stats["done_bytes"] / total_bytes
                head += f" ({frac * 100:.0f}% объёма), осталось ~{hms(el / frac - el)}"
            with out_lock:
                log(head + " | в работе: " + " · ".join(f"{n.split('@')[0]} {t}" for n, t in items))

    hb = threading.Thread(target=heartbeat, daemon=True)
    if not verify_only:
        hb.start()

    results = []
    ex = ThreadPoolExecutor(max_workers=workers)
    try:
        futs = [ex.submit(one, a) for a in accounts]
        for fu in as_completed(futs):
            res = fu.result()
            if res is None:
                continue
            results.append(res)
            with out_lock:
                stats["done"] += 1
                stats["done_bytes"] += sizes.get(res["name"]) or 0
                log(line(res, len(accounts)))
    except KeyboardInterrupt:
        cancel.set()
        ex.shutdown(wait=False, cancel_futures=True)
        with prog_lock:
            busy = sorted(progress)
        log(f"\n===== ПРЕРВАНО (Ctrl+C) через {hms(time.time() - t_start)}: готово {stats['done']} из {len(accounts)}")
        if busy:
            log(f"  Не завершены ({len(busy)}): {', '.join(busy)}")
            if reindex_only:
                log("  Переиндексация на сервере продолжится сама; повторный --reindex для них безопасен.")
            else:
                log("  Почта: повтор безопасен (Zimbra отсеет уже загруженные письма) — "
                    "--data --types message --force --only <ящик>")
                log("  Контакты/календари/задачи/портфель могли загрузиться частично — "
                    "--data --types contact,appointment,task,document --force --only <ящик>")
        if not reindex_only and not verify_only:
            log("  Готовые ящики записаны в data-state.json; следующий запуск их пропустит.")
        raise
    finally:
        stop.set()
    ex.shutdown(wait=True)

    el = hms(time.time() - t_start)
    errs = [x for x in results if x["status"] == "ОШИБКА"]
    if reindex_only:
        log(f"\n===== ИТОГ ПЕРЕИНДЕКСАЦИИ за {el}: успешно {len(results) - len(errs)}, ошибок {len(errs)}")
        return [f"{x['name']}: {x['msg']}" for x in sorted(errs, key=lambda x: x["name"])]

    # ---- отчёт CSV
    csv_path = os.path.join(workdir, "data-report.csv")
    with open(csv_path, "w") as f:
        f.write("ящик;статус;тип;на_старом;на_новом_до;на_новом_после;добавлено;расхождение;объём_байт;примечание;ошибка\n")
        for res in sorted(results, key=lambda x: x["name"]):
            for v, o_, b_, a_ in res["rows"]:
                add = a_ - b_ if a_ is not None and b_ is not None else ""
                d = "да" if o_ is not None and a_ is not None and o_ != a_ else ""
                f.write(f"{res['name']};{res['status']};{vname[v]};{'' if o_ is None else o_};"
                        f"{'' if b_ is None else b_};{'' if a_ is None else a_};{add};{d};{res['size']};"
                        f"{res['info'].replace(';', ',')};{res['msg'].replace(';', ',')}\n")

    # ---- итог
    log(f"\n===== ИТОГ {'СВЕРКИ' if verify_only else 'ПЕРЕНОСА'} за {el}")
    if not verify_only:
        cnt = {}
        for x in results:
            cnt[x["status"]] = cnt.get(x["status"], 0) + 1
        log("  ящиков: " + ", ".join(f"{k} {v}" for k, v in sorted(cnt.items()))
            + f"; передано {human(sum(x['size'] for x in results))}")
    log(f"  {'Тип':<12}{'на старом':>12}{'на новом':>12}" + ("" if verify_only else f"{'добавлено':>12}")
        + f"{'ящиков ✗':>12}")
    for i, v in enumerate(views):
        olds = [x["rows"][i][1] for x in results if x["rows"] and x["rows"][i][1] is not None]
        so = sum(olds) if olds else "—"
        if olds and len(olds) < len(results):
            so = f"{so}*"
        sn = sum(x["rows"][i][3] or 0 for x in results if x["rows"])
        sa = sum((x["rows"][i][3] or 0) - (x["rows"][i][2] or 0) for x in results if x["rows"])
        nd = sum(1 for x in results if x["rows"] and x["rows"][i][1] is not None and x["rows"][i][3] is not None
                 and x["rows"][i][1] != x["rows"][i][3])
        log(f"  {vname[v]:<12}{so:>12}{sn:>12}" + ("" if verify_only else f"{sa:>12}") + f"{nd:>12}")
    diffs = sorted(x["name"] for x in results if x["diff"] and x["status"] != "ОШИБКА")
    if diffs:
        log(f"\n  ✗ Расхождения ({len(diffs)}): {', '.join(diffs[:30])}" + (" …" if len(diffs) > 30 else ""))
        log("    на новом меньше — новое на старом: --sync; не всё перенесено: "
            "--data --types message --force --diff-only;")
        log("    на новом больше у контактов/календарей/задач/портфеля — перенести заново: --force --diff-only")
    log(f"  Подробно по каждому ящику: {csv_path}")
    if not verify_only:
        log(f"  Даты переносов (для --sync и защиты от повторов): {state_path}")
    out = [f"{x['name']}: {x['msg']}" for x in sorted(errs, key=lambda x: x["name"])]
    if verify_only:
        out += [f"{n}: расхождение по количеству" for n in diffs]
    return out


SETUP_TEXT = """zm-ldap-migrate.py — перенос Zimbra со старого сервера на новый (LDAP и содержимое ящиков).

ЧТО НУЖНО ДО ЗАПУСКА
  1. Запуск на НОВОМ сервере от пользователя zimbra (su - zimbra), Zimbra установлена и работает.
  2. Каталог для журналов и выгрузок (от root):
       install -d -o zimbra -g zimbra -m 700 /opt/zimbra/migration
  3. Файл настроек из migrate.conf.example (от zimbra):
       cp migrate.conf.example migrate.conf && chmod 600 migrate.conf && nano migrate.conf
     [old]       LDAP старого сервера: ldap_url, bind_dn, bind_pw
                 (на старом: zmlocalconfig -s ldap_url zimbra_ldap_userdn zimbra_ldap_password)
     [old_rest]  для --data: url https://<старый>:7071, admin, password глобального администратора
     [migrate]   exclude_domains, workdir, chunk_mb
  4. Сеть с нового на старый: TCP 389 (LDAP, STARTTLS) и TCP 7071 (REST/SOAP администратора).
  5. Снять ограничение частоты запросов (иначе HTTP 429), на обоих серверах:
       zmprov mcf +zimbraHttpThrottleSafeIPs <IP нового сервера>; zmmailboxdctl restart
     на новом дополнительно: zmprov mcf +zimbraHttpThrottleSafeIPs 127.0.0.1

СПРАВКА ПО КЛЮЧАМ И ПРИМЕРЫ
  python3 zm-ldap-migrate.py -c migrate.conf
"""


def domain_scope(old, exclude, domains):
    """--domain: всё, кроме указанных доменов, попадает в исключения. Проверяет, что домены есть."""
    all_dom = {first(a, "zimbraDomainName", "").lower() for _, a in old.search("(objectClass=zimbraDomain)",
                                                                                 attrs=["zimbraDomainName"])}
    unknown = sorted(set(domains) - all_dom)
    if unknown:
        die(f"--domain: на старом сервере нет домена {', '.join(unknown)}. Есть: {', '.join(sorted(all_dom))}")
    skipped = sorted(set(domains) & exclude)
    if skipped:
        log(f"  ! {', '.join(skipped)} указан в exclude_domains, но выбран через --domain — переносится")
    return all_dom - set(domains)


def scoped_aces(aces, grantee_ids):
    """Глобальные права только для переносимых учёток/списков (при --domain/--only)."""
    out = []
    for ace in aces:
        gid = ace.decode().split(" ", 1)[0]
        if gid in grantee_ids:
            out.append(ace)
    return out


# ---------------------------------------------------------------- основной сценарий
def main():
    global LOG
    if len(sys.argv) == 1:
        print(SETUP_TEXT)
        sys.exit(0)
    ap = argparse.ArgumentParser(prog="python3 zm-ldap-migrate.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_argument_group("основное")
    g.add_argument("-c", "--config", required=True, metavar="ФАЙЛ", help="файл настроек (см. migrate.conf.example)")
    g.add_argument("--ldap", action="store_true", help="перенос LDAP-объектов")
    g.add_argument("--data", action="store_true", help="перенос содержимого ящиков")
    g.add_argument("--reindex", action="store_true",
                   help="переиндексация на новом сервере; с --data — после успешного переноса почты")
    g.add_argument("--verify", action="store_true", help="сверка, ничего не меняет (без режима — LDAP и данные)")
    g.add_argument("--dry-run", action="store_true", help="только план, ничего не меняет (без режима — LDAP и данные)")
    g = ap.add_argument_group("выбор объектов")
    g.add_argument("--domain", metavar="ДОМЕН", action="append", default=[],
                   help="только этот домен (можно несколько раз или через запятую)")
    g.add_argument("--only", metavar="АДРЕС", action="append", default=[],
                   help="только эта учётка (можно несколько раз)")
    g = ap.add_argument_group("для --data")
    g.add_argument("--types", default="all",
                   help="all (по умолчанию) или через запятую: message (почта), contact, appointment (календарь), "
                        "task, document (портфель)")
    g.add_argument("--sync", action="store_true",
                   help="почта — только с даты прошлого успешного переноса каждого ящика")
    g.add_argument("--since", metavar="ДД.ММ.ГГГГ", help="почта — только после этой даты")
    g.add_argument("--force", action="store_true",
                   help="всё заново: почта полностью (повторы Zimbra отсеивает), остальное очищается на новом "
                        "и загружается заново")
    g.add_argument("--replace", action="store_true", help=argparse.SUPPRESS)   # прежнее имя --force
    g.add_argument("--yes", action="store_true",
                   help="не спрашивать подтверждение перед очисткой контактов/календарей/задач/портфеля на новом")
    g.add_argument("--diff-only", action="store_true",
                   help="только ящики, где количество на старом и новом не совпадает")
    g.add_argument("--workers", type=int, default=4, help="параллельных ящиков (по умолчанию 4)")
    g.add_argument("--chunk-mb", type=int, default=None,
                   help="ящики больше этого размера (МБ) — почта по годам (по умолчанию из настроек, иначе 2048)")
    g.add_argument("--keep-files", action="store_true", help="не удалять выгруженные архивы tgz")
    if len(sys.argv) == 3 and sys.argv[1] in ("-c", "--config"):
        ap.print_help()
        sys.exit(0)
    args = ap.parse_args()
    if args.types.strip().lower() == "all":
        args.types = "message," + DATA_TYPES_DEFAULT
    bad = set(args.types.split(",")) - DATA_TYPES_ALL
    if bad:
        die(f"неизвестные типы в --types: {', '.join(bad)}; допустимы: all, {', '.join(sorted(DATA_TYPES_ALL))}")
    if args.verify and args.dry_run:
        die("--verify и --dry-run вместе не используются")
    if not (args.ldap or args.data or args.reindex or args.verify or args.dry_run):
        die("не указан режим: --ldap, --data, --reindex, --verify или --dry-run. Справка: "
            f"python3 zm-ldap-migrate.py -c {args.config}")
    if args.sync and args.since:
        die("--sync и --since вместе не используются: --sync сам берёт дату прошлого переноса по каждому ящику")
    args.force = args.force or args.replace
    if (args.sync or args.since or args.force or args.diff_only) and not args.data:
        die("--sync, --since, --force и --diff-only работают только вместе с --data")
    if args.reindex and args.verify:
        die("--reindex и --verify вместе не используются")
    since = None
    if args.since:
        try:
            since = datetime.datetime.strptime(args.since, "%d.%m.%Y")
        except ValueError:
            die("--since: формат ДД.ММ.ГГГГ, например 20.11.2026")
    # без режима --verify / --dry-run относятся и к LDAP, и к данным
    bare = not (args.ldap or args.data or args.reindex)
    do_ldap = args.ldap or bare
    do_data = args.data or bare

    if os.geteuid() == 0:
        die("запускайте от пользователя zimbra: su - zimbra")
    if not os.path.exists(args.config):
        die(f"нет файла {args.config}. Создать из образца: cp migrate.conf.example {args.config}; "
            f"chmod 600 {args.config}. Что нужно для запуска: python3 zm-ldap-migrate.py")
    if not os.access(args.config, os.R_OK):
        die(f"{args.config} не читается пользователем zimbra. От root: chown zimbra:zimbra {args.config}")
    st = os.stat(args.config)
    if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        die(f"{args.config} доступен другим пользователям — выполните: chmod 600 {args.config}")

    cfg = configparser.ConfigParser(interpolation=None)
    try:
        cfg.read(args.config, encoding="utf-8-sig")
    except (configparser.Error, UnicodeDecodeError) as e:
        die(f"не удалось разобрать {args.config}: {e}")
    if not cfg.has_section("old"):
        die(f"в {args.config} нет секции [old]; найдены секции: {cfg.sections() or 'нет'}. "
            f"Проверьте файл: grep -n '^\\[' {args.config}")
    o = cfg["old"]
    missing = [k for k in ("ldap_url", "bind_dn", "bind_pw") if not o.get(k, "").strip()]
    if missing or "ВСТАВИТЬ" in o.get("bind_pw", ""):
        die(f"в секции [old] не заполнены: {', '.join(missing) or 'bind_pw'}")
    m = cfg["migrate"] if cfg.has_section("migrate") else {}
    exclude = {d.strip().lower() for d in m.get("exclude_domains", "").split(",") if d.strip()}
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    workdir = os.path.join(os.path.expanduser(m.get("workdir", "~/migration")), stamp)
    try:
        os.makedirs(workdir, mode=0o700, exist_ok=True)
    except PermissionError:
        die(f"нет прав на создание {workdir}. От root: "
            f"install -d -o zimbra -g zimbra -m 700 {os.path.dirname(workdir)}")
    LOG = open(os.path.join(workdir, "migrate.log"), "w")
    dry = args.dry_run
    only = {a.lower() for a in args.only}
    domains = sorted({d.strip().lower() for x in args.domain for d in x.split(",") if d.strip()})

    parts = []
    if do_ldap:
        parts.append("LDAP")
    if do_data:
        parts.append("данные ящиков")
    if args.reindex:
        parts.append("переиндексация")
    how = "СВЕРКА (без изменений)" if args.verify else "ПЛАН (без изменений)" if dry else "ПЕРЕНОС"
    log(f"Режим: {how}: {', '.join(parts)}; рабочий каталог: {workdir}")
    old = Ldap(o["ldap_url"], o["bind_dn"], o["bind_pw"], o.getboolean("starttls", True), workdir, "old")
    new = Ldap(zmlocal("ldap_master_url").split()[0], zmlocal("zimbra_ldap_userdn"),
               zmlocal("zimbra_ldap_password"), zmlocal("ldap_starttls_supported") == "1", workdir, "new")
    global SCOPED
    rc = 0
    try:
        if domains:
            exclude = domain_scope(old, exclude, domains)
            log(f"Домены: {', '.join(domains)}")
        if only:
            log(f"Учётки: {', '.join(sorted(only))}")
        SCOPED = bool(domains or only)
        if do_ldap:
            if args.verify:
                if not verify(old, new, exclude, only, workdir):
                    rc = 2
            else:
                report = []
                run(old, new, exclude, only, dry, workdir, report)
                log("")
                if report:
                    log(f"===== НЕ ПРИМЕНЕНО ({len(report)}) — проверить вручную")
                    for r in report:
                        log("  " + r)
                    rc = 2
                elif not dry:
                    log("===== Ошибок применения атрибутов нет")
        if do_data or args.reindex:
            errors = data_migrate(cfg, old, new, exclude, only, dry, workdir, args.types, since,
                                  max(1, args.workers), args.keep_files,
                                  verify_only=args.verify, sync=args.sync,
                                  chunk_mb=args.chunk_mb or int(m.get("chunk_mb", 2048)),
                                  force=args.force, assume_yes=args.yes,
                                  reindex=args.reindex, reindex_only=args.reindex and not do_data,
                                  diff_only=args.diff_only)
            if errors:
                rc = 2
                log(f"\n===== {'РАСХОЖДЕНИЯ' if args.verify else 'ОШИБКИ'} ({len(errors)})"
                    + ("" if args.verify else " — повторить: --data --only <адрес>"))
                for e in errors:
                    log("  " + e)
    finally:
        old.cleanup()
        new.cleanup()
    log(f"Журнал: {os.path.join(workdir, 'migrate.log')}")
    sys.exit(rc)


def read_old(old, exclude, only):
    """Чтение объектов старого сервера в границах переноса."""
    o_cos = old.search("(objectClass=zimbraCOS)")
    o_dom = [e for e in old.search("(objectClass=zimbraDomain)")
             if first(e[1], "zimbraDomainName", "").lower() not in exclude]
    dom_names = {first(a, "zimbraDomainName").lower() for _, a in o_dom}

    def in_scope(dn):
        return dn_to_name(dn).split("@", 1)[1].lower() in dom_names

    o_acc = [e for e in old.search(ACCOUNT_FILTER) if in_scope(e[0])]
    o_dl = [e for e in old.search("(|(objectClass=zimbraDistributionList)(objectClass=zimbraGroup))")
            if in_scope(e[0])]
    if only:
        o_acc = [e for e in o_acc if dn_to_name(e[0]).lower() in only]
        o_dl = []
    o_alias = [e for e in old.search("(objectClass=zimbraAlias)") if in_scope(e[0])]
    acc_dns = {dn.lower() for dn, _ in o_acc}
    o_child = []
    for oc in CHILD_CLASSES:
        for dn, a in old.search(f"(objectClass={oc})"):
            if dn.split(",", 1)[1].lower() in acc_dns:
                o_child.append((dn, a))
    gg = old.search("(objectClass=*)", base="cn=globalgrant,cn=zimbra", scope="base", attrs=["zimbraACE"])
    o_gace = gg[0][1].get("zimbraACE", []) if gg else []
    if SCOPED:
        o_gace = scoped_aces(o_gace, {first(a, "zimbraId") for _, a in o_acc + o_dl})
    return dict(cos=o_cos, dom=o_dom, dom_names=dom_names, acc=o_acc, dl=o_dl,
                alias=o_alias, child=o_child, gace=o_gace)


def diff_attrs(old_a, new_a, skip):
    """Сравнение атрибутов как множеств значений. Возвращает список строк с расхождениями."""
    out = []
    for k, ov in old_a.items():
        if k in skip:
            continue
        nv = new_a.get(k)
        if nv is None:
            out.append(f"{k}: нет на новом")
        elif set(ov) != set(nv):
            if len(ov) == len(nv) == 1 and len(ov[0]) < 80 and len(nv[0]) < 80:
                out.append(f"{k}: было «{ov[0].decode(errors='replace')}», стало «{nv[0].decode(errors='replace')}»")
            else:
                out.append(f"{k}: значения отличаются (старых {len(ov)}, новых {len(nv)})")
    return out


def verify(old, new, exclude, only, workdir):
    log("\n=== Сверка: чтение старого и нового серверов")
    o = read_old(old, exclude, only)
    n_cos = {first(a, "cn"): (dn, a) for dn, a in new.search("(objectClass=zimbraCOS)")}
    n_dom = {first(a, "zimbraDomainName").lower(): (dn, a) for dn, a in new.search("(objectClass=zimbraDomain)")}
    n_obj = {first(a, "zimbraId"): (dn, a) for dn, a in
             new.search("(&(|(objectClass=zimbraAccount)(objectClass=zimbraDistributionList)"
                        "(objectClass=zimbraGroup))(zimbraId=*))")}
    n_byname = {dn_to_name(dn).lower(): first(a, "zimbraId") for dn, a in n_obj.values()}
    n_alias = {dn_to_name(dn).lower() for dn, _ in new.search("(objectClass=zimbraAlias)", attrs=["uid"])}
    n_child = {dn.lower(): a for oc in CHILD_CLASSES for dn, a in new.search(f"(objectClass={oc})")}
    gg = new.search("(objectClass=*)", base="cn=globalgrant,cn=zimbra", scope="base", attrs=["zimbraACE"])
    n_gace = set(gg[0][1].get("zimbraACE", [])) if gg else set()

    old_cos_name = {first(a, "zimbraId"): first(a, "cn") for _, a in o["cos"]}
    new_cos_name = {first(a, "zimbraId"): name for name, (_, a) in n_cos.items()}

    def cos_names(values, names):
        return sorted(names.get(v.decode(), v.decode()) for v in values)

    stats, problems = {}, []

    def check(kind, label, issues):
        s = stats.setdefault(kind, [0, 0])
        s[0] += 1
        if issues:
            s[1] += 1
            problems.append(f"[{kind}] {label}")
            problems.extend("      " + i for i in issues)

    for dn, a in o["cos"]:
        name = first(a, "cn")
        if name not in n_cos:
            check("COS", name, ["нет на новом сервере"])
        else:
            check("COS", name, diff_attrs(a, n_cos[name][1], SKIP_COS))

    for dn, a in o["dom"]:
        name = first(a, "zimbraDomainName").lower()
        if name not in n_dom:
            check("Домены", name, ["нет на новом сервере"])
            continue
        na = n_dom[name][1]
        iss = diff_attrs(a, na, SKIP_DOMAIN)
        for k in ("zimbraDomainDefaultCOSId", "zimbraDomainDefaultExternalUserCOSId"):
            if k in a and cos_names(a[k], old_cos_name) != cos_names(na.get(k, []), new_cos_name):
                iss.append(f"{k}: COS {cos_names(a[k], old_cos_name)} -> {cos_names(na.get(k, []), new_cos_name)}")
        check("Домены", name, iss)

    for kind, objs, skip in (("Учётки", o["acc"], SKIP_ACCOUNT), ("Списки", o["dl"], SKIP_DL)):
        for dn, a in objs:
            name, zid = dn_to_name(dn), first(a, "zimbraId")
            if zid not in n_obj:
                other = n_byname.get(name.lower())
                check(kind, name, [f"создана с другим zimbraId ({other})" if other else "нет на новом сервере"])
                continue
            ndn, na = n_obj[zid]
            iss = [] if dn_to_name(ndn).lower() == name.lower() else [f"имя на новом: {dn_to_name(ndn)}"]
            iss += diff_attrs(a, na, skip)
            if "zimbraCOSId" in a and cos_names(a["zimbraCOSId"], old_cos_name) != cos_names(na.get("zimbraCOSId", []), new_cos_name):
                iss.append("zimbraCOSId: COS отличается")
            o_al = {v.decode().lower() for v in a.get("zimbraMailAlias", [])
                    if v.decode().split("@")[-1].lower() in o["dom_names"]}
            n_al = {v.decode().lower() for v in na.get("zimbraMailAlias", [])}
            if o_al - n_al:
                iss.append("нет алиасов: " + ", ".join(sorted(o_al - n_al)))
            check(kind, name, iss)

    for dn, a in o["alias"]:
        tid = first(a, "zimbraAliasTargetId")
        if only and tid not in {first(x, "zimbraId") for _, x in o["acc"]}:
            continue
        name = dn_to_name(dn)
        check("Алиасы", name, [] if name.lower() in n_alias else ["нет на новом сервере"])

    for dn, a in o["child"]:
        na = n_child.get(dn.lower())
        check("Подписи/персоны/внешние ящики", dn.split(",", 1)[0],
              ["нет на новом сервере"] if na is None else diff_attrs(a, na, SKIP_CHILD | {"objectClass"}))

    for ace in o["gace"]:
        check("Глобальные права", ace.decode(), [] if ace in n_gace else ["нет на новом сервере"])

    old_names = {dn_to_name(dn).lower() for dn, _ in o["acc"] + o["dl"]}
    extra = sorted(nm for nm in n_byname if nm.split("@")[-1] in o["dom_names"] and nm not in old_names
                   and not nm.split("@")[0].startswith(("galsync", "spam.", "ham.", "virus-quarantine.")))

    log("\n===== ИТОГ СВЕРКИ")
    log(f"  {'Объекты':<32}{'всего':>7}{'OK':>7}{'расхождений':>13}")
    for kind, (total, bad) in stats.items():
        log(f"  {kind:<32}{total:>7}{total - bad:>7}{bad:>13}")
    if extra and not SCOPED:
        log(f"\n  На новом сервере есть, на старом нет ({len(extra)}): " + ", ".join(extra[:20])
            + (" …" if len(extra) > 20 else ""))
    path = os.path.join(workdir, "verify-report.txt")
    with open(path, "w") as f:
        f.write("\n".join(problems) + "\n")
    if problems:
        log(f"\n===== РАСХОЖДЕНИЯ")
        for p in problems[:60]:
            log("  " + p)
        if len(problems) > 60:
            log(f"  … полный список: {path}")
    else:
        log("\n  Расхождений нет")
    return not problems


def run(old, new, exclude, only, dry, workdir, report):
    # ------------------------------------------------ чтение старого сервера
    log("\n=== Чтение старого сервера")
    o_cos = old.search("(objectClass=zimbraCOS)")
    o_dom = [e for e in old.search("(objectClass=zimbraDomain)")
             if first(e[1], "zimbraDomainName", "").lower() not in exclude]
    dom_names = {first(a, "zimbraDomainName").lower() for _, a in o_dom}

    def in_scope(dn):
        return dn_to_name(dn).split("@", 1)[1].lower() in dom_names

    o_acc = [e for e in old.search(ACCOUNT_FILTER) if in_scope(e[0])]
    if only:
        o_acc = [e for e in o_acc if dn_to_name(e[0]).lower() in only]
    o_res = [e for e in old.search("(objectClass=zimbraCalendarResource)") if in_scope(e[0])]
    o_dl = [e for e in old.search("(|(objectClass=zimbraDistributionList)(objectClass=zimbraGroup))")
            if in_scope(e[0])]
    if only:
        o_dl = []
    o_alias = [e for e in old.search("(objectClass=zimbraAlias)") if in_scope(e[0])]
    acc_dns = {dn.lower() for dn, _ in o_acc}
    o_child = []
    for oc in CHILD_CLASSES:
        for dn, a in old.search(f"(objectClass={oc})"):
            parent = dn.split(",", 1)[1].lower()
            if parent in acc_dns:
                o_child.append((dn, a))
    gg = old.search("(objectClass=*)", base="cn=globalgrant,cn=zimbra", scope="base", attrs=["zimbraACE"])
    o_global_ace = gg[0][1].get("zimbraACE", []) if gg else []

    log(f"  COS: {len(o_cos)}  доменов: {len(o_dom)} (исключены: {', '.join(sorted(exclude)) or '—'})")
    log(f"  учёток: {len(o_acc)}  ресурсов календаря: {len(o_res)}  списков: {len(o_dl)}  алиасов: {len(o_alias)}")
    log(f"  подписей/персон/внешних ящиков: {len(o_child)}  глобальных прав: {len(o_global_ace)}")
    if o_res:
        log("  ! ресурсы календаря этим скриптом не переносятся — перенести вручную")

    # ------------------------------------------------ состояние нового сервера
    n_cos = {first(a, "cn"): first(a, "zimbraId") for _, a in new.search("(objectClass=zimbraCOS)", attrs=["cn", "zimbraId"])}
    n_dom = {first(a, "zimbraDomainName").lower(): dn for dn, a in
             new.search("(objectClass=zimbraDomain)", attrs=["zimbraDomainName"])}

    def new_by_id(flt="(objectClass=*)"):
        return {first(a, "zimbraId"): (dn, a) for dn, a in new.search(f"(&{flt}(zimbraId=*))",
                                                                         attrs=["zimbraId", "objectClass", "uid"])}
    n_ids = new_by_id("(|(objectClass=zimbraAccount)(objectClass=zimbraDistributionList)(objectClass=zimbraGroup))")
    n_names = {dn_to_name(dn).lower() for dn, _ in n_ids.values()}

    # ------------------------------------------------ 1. COS
    log("\n=== 1. Классы обслуживания (COS)")
    cmds = [f"cc {shlex.quote(first(a, 'cn'))}" for _, a in o_cos if first(a, "cn") not in n_cos]
    zmprov_batch(cmds, workdir, "10-cos.zmp", dry)
    if not dry:
        n_cos = {first(a, "cn"): first(a, "zimbraId") for _, a in new.search("(objectClass=zimbraCOS)", attrs=["cn", "zimbraId"])}
    cos_map = {first(a, "zimbraId"): n_cos.get(first(a, "cn"), f"<новый id {first(a, 'cn')}>") for _, a in o_cos}
    for dn, a in o_cos:
        name = first(a, "cn")
        repl = {k: v for k, v in a.items() if k not in SKIP_COS}
        log(f"  {name}: атрибутов {len(repl)}")
        if not dry:
            ndn = new.search(f"(&(objectClass=zimbraCOS)(cn={name}))", attrs=["cn"])[0][0]
            apply_attrs(new, ndn, (), repl, dry, report, f"COS {name}")

    def remap(values):
        return [cos_map.get(v.decode(), v.decode()).encode() for v in values]

    # ------------------------------------------------ 2. Домены
    log("\n=== 2. Домены")
    cmds = [f"cd {first(a, 'zimbraDomainName')}" for _, a in o_dom if first(a, "zimbraDomainName").lower() not in n_dom]
    zmprov_batch(cmds, workdir, "20-domains.zmp", dry)
    if not dry:
        n_dom = {first(a, "zimbraDomainName").lower(): dn for dn, a in
                 new.search("(objectClass=zimbraDomain)", attrs=["zimbraDomainName"])}
    for dn, a in o_dom:
        name = first(a, "zimbraDomainName")
        repl = {k: v for k, v in a.items() if k not in SKIP_DOMAIN}
        for k in ("zimbraDomainDefaultCOSId", "zimbraDomainDefaultExternalUserCOSId"):
            if k in a:
                repl[k] = remap(a[k])
        vh = ", ".join(v.decode() for v in a.get("zimbraVirtualHostname", [])) or "—"
        log(f"  {name}: атрибутов {len(repl)}; zimbraVirtualHostname: {vh}")
        if not dry:
            apply_attrs(new, n_dom[name.lower()], (), repl, dry, report, f"домен {name}")

    # ------------------------------------------------ 3. Учётки
    log("\n=== 3. Учётки")
    cmds = []
    for dn, a in o_acc:
        name, zid = dn_to_name(dn), first(a, "zimbraId")
        if zid in n_ids:
            continue
        if name.lower() in n_names:
            report.append(f"{name}: на новом сервере уже есть учётка с другим zimbraId — пропущена")
            continue
        cmds.append(f"ca {name} {temp_password()} zimbraId {zid}")
    zmprov_batch(cmds, workdir, "30-accounts.zmp", dry)
    if not dry:
        n_ids = new_by_id("(|(objectClass=zimbraAccount)(objectClass=zimbraDistributionList)(objectClass=zimbraGroup))")
    for dn, a in o_acc:
        name, zid = dn_to_name(dn), first(a, "zimbraId")
        repl = {k: v for k, v in a.items() if k not in SKIP_ACCOUNT}
        if "zimbraCOSId" in a:
            repl["zimbraCOSId"] = remap(a["zimbraCOSId"])
        if dry:
            continue
        if zid not in n_ids:
            report.append(f"{name}: не создана или создана с другим zimbraId — атрибуты не применены")
            continue
        ndn, na = n_ids[zid]
        new_oc = {v.decode().lower() for v in na.get("objectClass", [])}
        add_oc = [v.decode() for v in a.get("objectClass", []) if v.decode().lower() not in new_oc]
        apply_attrs(new, ndn, add_oc, repl, dry, report, name)
    log(f"  учёток к созданию: {len(cmds)}, к обновлению атрибутов: {len(o_acc)}")

    # ------------------------------------------------ 4. Подписи, персоны, внешние ящики
    log("\n=== 4. Подписи, персоны, внешние ящики")
    added = 0
    for dn, a in o_child:
        attrs = {k: v for k, v in a.items() if k not in SKIP_CHILD}
        if dry:
            continue
        rc, err = new.modify(ldif_add(dn, attrs), add=True)
        if rc == 68:                                   # уже есть — обновить
            repl = {k: v for k, v in attrs.items() if k != "objectClass"}
            apply_attrs(new, dn, (), repl, dry, report, dn)
        elif rc != 0:
            report.append(f"{dn}: {err.splitlines()[-1] if err else rc}")
        else:
            added += 1
    log(f"  записей: {len(o_child)}" + ("" if dry else f", добавлено новых: {added}"))
    ds = [dn_to_name(dn.split(",", 1)[1]) for dn, a in o_child if b"zimbraDataSource" in a.get("objectClass", [])]
    if ds:
        log("  ! внешние ящики (data sources) проверить после переноса данных ящиков: " + ", ".join(sorted(set(ds))))

    # ------------------------------------------------ 5. Списки рассылки
    log("\n=== 5. Списки рассылки")
    cmds = []
    n_names = {dn_to_name(ndn).lower() for ndn, _ in n_ids.values()}
    for dn, a in o_dl:
        name, zid = dn_to_name(dn), first(a, "zimbraId")
        if zid in n_ids or name.lower() in n_names:
            continue
        dyn = b"zimbraGroup" in a.get("objectClass", [])
        cmds.append(f"{'cddl' if dyn else 'cdl'} {name}")
    zmprov_batch(cmds, workdir, "50-lists.zmp", dry)
    if not dry:
        n_ids = new_by_id("(|(objectClass=zimbraAccount)(objectClass=zimbraDistributionList)(objectClass=zimbraGroup))")
        # zmprov cdl не принимает zimbraId — у списка нет ящика, поэтому id меняется прямо в LDAP,
        # вместе со ссылками алиасов списка на него
        by_name = {dn_to_name(ndn).lower(): (nid, ndn, na) for nid, (ndn, na) in n_ids.items()}
        fixed = 0
        for dn, a in o_dl:
            name, zid = dn_to_name(dn), first(a, "zimbraId")
            if zid in n_ids or name.lower() not in by_name:
                continue
            nid, ndn, na = by_name[name.lower()]
            ocs = {v.decode().lower() for v in na.get("objectClass", [])}
            if not ocs & {"zimbradistributionlist", "zimbragroup"}:
                report.append(f"список {name}: на новом сервере это не список рассылки — пропущен")
                continue
            rc, err = new.modify(ldif_modify(ndn, (), {"zimbraId": [zid.encode()]}))
            if rc != 0:
                report.append(f"список {name}: не удалось сменить zimbraId: {err.splitlines()[-1] if err else rc}")
                continue
            for adn, _ in new.search(f"(&(objectClass=zimbraAlias)(zimbraAliasTargetId={nid}))", attrs=["uid"]):
                new.modify(ldif_modify(adn, (), {"zimbraAliasTargetId": [zid.encode()]}))
            fixed += 1
        if fixed:
            log(f"  zimbraId восстановлен у списков: {fixed}")
            n_ids = new_by_id("(|(objectClass=zimbraAccount)(objectClass=zimbraDistributionList)(objectClass=zimbraGroup))")
    for dn, a in o_dl:
        name, zid = dn_to_name(dn), first(a, "zimbraId")
        repl = {k: v for k, v in a.items() if k not in SKIP_DL}
        if dry:
            continue
        if zid not in n_ids:
            report.append(f"список {name}: не создан или создан с другим zimbraId")
            continue
        apply_attrs(new, n_ids[zid][0], (), repl, dry, report, f"список {name}")
    log(f"  списков к созданию: {len(cmds)}, всего: {len(o_dl)}")

    # ------------------------------------------------ 6. Алиасы учёток и списков
    log("\n=== 6. Алиасы")
    id2 = {first(a, "zimbraId"): (dn_to_name(dn), "dl" if b"zimbraAccount" not in a.get("objectClass", []) else "acc")
           for dn, a in o_acc + o_dl}
    n_alias = {dn_to_name(dn).lower() for dn, _ in new.search("(objectClass=zimbraAlias)", attrs=["uid"])}
    cmds, orphan = [], 0
    for dn, a in o_alias:
        alias, tid = dn_to_name(dn), first(a, "zimbraAliasTargetId")
        if alias.lower() in n_alias:
            continue
        if tid not in id2:
            orphan += 1
            continue
        target, kind = id2[tid]
        cmds.append(f"{'adla' if kind == 'dl' else 'aaa'} {target} {alias}")
    zmprov_batch(cmds, workdir, "60-aliases.zmp", dry)
    log(f"  алиасов к созданию: {len(cmds)}; пропущено (цель вне переноса): {orphan}")

    # ------------------------------------------------ 7. Глобальные права
    log("\n=== 7. Глобальные делегированные права (cn=globalgrant)")
    if SCOPED:
        o_global_ace = scoped_aces(o_global_ace, {first(a, "zimbraId") for _, a in o_acc + o_dl})
        log(f"  только права переносимых учёток и списков: {len(o_global_ace)}")
    for ace in o_global_ace:
        log("  " + ace.decode())
        if dry:
            continue
        ldif = ("dn: cn=globalgrant,cn=zimbra\nchangetype: modify\nadd: zimbraACE\n"
                f"zimbraACE:: {b64(ace)}\n-\n\n")
        rc, err = new.modify(ldif)
        if rc not in (0, 20):
            report.append(f"globalgrant {ace.decode()}: {err.splitlines()[-1] if err else rc}")

    # ------------------------------------------------ итог
    if not dry:
        subprocess.run(["zmprov", "fc", "all"], capture_output=True)
        log("\nКэш Zimbra сброшен (zmprov fc all). Рекомендуется: zmmailboxdctl restart")
        log("Учётки синхронизации адресной книги создать заново, по каждому домену:")
        for _, a in o_dom:
            if "zimbraGalAccountId" in a:
                d = first(a, "zimbraDomainName")
                log(f"  zmgsautil createAccount -a galsync@{d} -n InternalGAL --domain {d} -s $(zmhostname) -t zimbra -f _InternalGAL")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        # потоки с незавершёнными HTTP-запросами не ждём: одно нажатие Ctrl+C — сразу выход
        try:
            log("Прервано пользователем (Ctrl+C)." + (f" Журнал: {LOG.name}" if LOG else ""))
            sys.stdout.flush()
        finally:
            os._exit(130)
