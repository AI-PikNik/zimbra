# Установка и защита Zimbra 10.1 FOSS за Proxmox Mail Gateway

Пошаговый мануал развёртывания почтового сервера Zimbra за двумя Proxmox Mail Gateway. Разделы 0–2 проверены на рабочем сервере; разделы 3–4 — черновик. Миграция со старого сервера — в [02-migration.md](02-migration.md).

Все адреса, имена серверов и домены в документе — примеры (`example.com`, `10.0.x.x`). Подставьте свои значения из таблицы ниже.

**Как пользоваться.** Разделы выполняются по порядку. В конце каждого — блок «Проверка» с ожидаемым результатом: не переходите дальше, пока он не совпал. Команды выполняются от root (или через sudo); блоки «от zimbra» — после `sudo su - zimbra`. Для другого сервера замените значения из таблицы ниже.

| Параметр | Значение |
| --- | --- |
| Сервер | mail-new.example.com, 10.0.10.10/27 |
| ОС | Ubuntu 24.04 LTS, минимальная установка |
| Zimbra | FOSS 10.1.20.p1, сборка Maldua, single-server |
| PMG | 10.0.10.11, 10.0.10.12 |
| Админская сеть (SSH) | 10.0.0.0/24 |
| Сети, отправляющие почту на порт 25 | 10.0.20.0/24, 10.0.30.0/24, 10.0.10.0/24 |
| Внутренние сети (fail2ban не банит) | 10.0.0.0/16 |
| Периметр | файрвол на границе сети: проброс портов без SNAT, порт 7071 открыт только для своей страны |
| Почтовые домены | example.com, example.net, example.org, shop.example |

## 0. Подготовка ОС и установка Zimbra

Минимальный образ Ubuntu 24.04 не содержит части пакетов, которые Zimbra ожидает, — прежде всего **cron**. Инсталлятор Zimbra об этом не предупреждает: установка проходит, но периодические задания не регистрируются, и в админке все службы показываются красными при фактически работающем сервере.

Перед началом сделайте снапшот ВМ: откатить неудачную установку проще, чем чистить её.

### 0.1. Имя сервера и время

```bash
sudo hostnamectl set-hostname mail-new.example.com
sudo sed -i '/127.0.1.1/d' /etc/hosts      # строка 127.0.1.1 ломает LDAP Zimbra
echo "10.0.10.10 mail-new.example.com mail-new" | sudo tee -a /etc/hosts
hostname -f                                  # должно вывести mail-new.example.com

sudo timedatectl set-timezone Europe/Berlin
sudo timedatectl set-ntp true
```

### 0.2. Обновление и пакеты

```bash
sudo apt update && sudo apt -y full-upgrade
[ -f /var/run/reboot-required ] && sudo reboot

sudo apt -y install cron rsyslog logrotate \
  wget curl netcat-openbsd sudo libidn12 libpcre3 libgmp10 libexpat1 libstdc++6 \
  libperl5.38 libaio1t64 unzip pax sysstat sqlite3 dnsutils \
  less nano htop traceroute telnet
sudo systemctl enable --now cron rsyslog

# конфликтующие MTA и веб-серверы
sudo apt -y purge postfix exim4* sendmail* apache2* nginx* 2>/dev/null || true
```

| Пакет | Зачем |
| --- | --- |
| cron | периодические задания Zimbra (статус служб, статистика, обучение антиспама). В минимальном образе отсутствует |
| rsyslog, logrotate | журналы `/var/log/zimbra.log`, `/var/log/zimbra-stats.log` и их ротация |
| wget, curl, dnsutils | загрузка дистрибутива, проверки DNS и HTTP — утилиты администратора |
| netcat-openbsd | проверка портов служб скриптами Zimbra |
| sudo | Zimbra работает от пользователя zimbra и через sudoers вызывает привилегированные команды |
| libidn12 | интернационализированные домены (в 24.04 заменила libidn11) |
| libpcre3 | регулярные выражения для Postfix и nginx |
| libgmp10, libexpat1, libstdc++6 | криптография, разбор XML (SOAP) в Perl-скриптах, рантайм C++ |
| libperl5.38 | Perl-обвязка Zimbra: zmlocalconfig, zmconfigd, Amavis, SpamAssassin |
| libaio1t64 | асинхронный ввод-вывод для MariaDB (`t64` — новое имя пакета в 24.04) |
| unzip, pax | развёртывание зимлетов, служебные скрипты |
| sysstat | `iostat` для графиков нагрузки в админке |
| sqlite3 | входит в список обязательных пакетов инсталлятора |
| less, nano, htop, traceroute, telnet | утилиты администратора: просмотр журналов (less), правка конфигов (nano), нагрузка по процессам (htop), трассировка маршрута до PMG и внешних узлов (traceroute), ручная проверка портов и диалога SMTP, POP3, IMAP (telnet). В минимальном образе отсутствуют; самой Zimbra не нужны |

В минимальном образе также отсутствуют документация и man-страницы. Поэтому `dpkg -V <пакет>` показывает строки `missing /usr/share/doc/...` — это нормально и на работу не влияет.

### 0.3. Загрузка и установка Zimbra

```bash
cd /tmp
BASE=https://github.com/maldua/zimbra-foss/releases/download/zimbra-foss-build-ubuntu-24.04/10.1.20.p1
F=zcs-10.1.20_GA_4200001.UBUNTU24_64.20260820122254.tgz
wget $BASE/$F $BASE/$F.sha256
sha256sum -c $F.sha256                    # должно быть: OK
tar xzf $F && cd zcs-10.1.20_GA_4200001.UBUNTU24_64.20260820122254
sudo ./install.sh
```

| Вопрос инсталлятора | Ответ |
| --- | --- |
| License agreement | Y |
| Use Zimbra's package repository | Y (нужен исходящий доступ к repo.zimbra.com) |
| zimbra-ldap, logger, mta, snmp, store, apache, spell, memcached, proxy | Y |
| zimbra-dnscache | N — порт 53 занят systemd-resolved, свой DNS-кэш не нужен |
| zimbra-imapd | N — для single-server не нужен |
| The system will be modified. Continue? | Y |
| DNS ERROR resolving MX… Change domain name? | Y, ввести домен. Предупреждение ожидаемо: MX указывает на PMG, а не на этот сервер |

В меню конфигурации: zimbra-store → Admin Password (пункт отмечен `******`), затем `a` — применить и сохранить.

### 0.4. Пост-настройка

Если cron был установлен после Zimbra, инсталлятор не смог зарегистрировать задания. Проверить и при необходимости собрать crontab (от zimbra):

```bash
crontab -l | grep -cE 'zmstatuslog|zmlogprocess|zmtrainsa'   # должно быть 3
# если 0:
cd /opt/zimbra/conf/crontabs && cat crontab crontab.* > /tmp/crontab.zimbra && crontab /tmp/crontab.zimbra
/opt/zimbra/libexec/zmstatuslog && /opt/zimbra/libexec/zmlogprocess
```

Если `/var/log/zimbra.log` пустой или отсутствует — не применилась настройка rsyslog (от root):

```bash
sudo /opt/zimbra/libexec/zmsyslogsetup && sudo systemctl restart rsyslog
```

**Реальный IP клиентов в логах (обязательно).** nginx (proxy) передаёт адрес клиента в mailboxd, но mailboxd принимает его только от доверенных адресов. Без этой настройки во всех логах вместо клиента будет адрес самого сервера, и fail2ban в разделе 2 не сможет никого забанить (от zimbra):

```bash
zmprov mcf +zimbraMailTrustedIP 127.0.0.1 +zimbraMailTrustedIP 10.0.10.10
zmmailboxdctl restart        # 1–2 минуты недоступности webmail
```

### 0.5. Прокси: режим redirect и уровни настроек

Webmail работает только по HTTPS: запрос на порт 80 получает перенаправление 301 на 443. Режим `redirect` совместим с выпуском сертификатов Let's Encrypt (раздел 4) — не работает только режим `https`, при котором порт 80 не слушается.

**Уровни настроек.** Параметры Zimbra задаются на двух уровнях: глобально (`zmprov mcf`, админка «Глобальные настройки») и на сервере (`zmprov ms`, админка «Серверы → mail-new»). Значение сервера перекрывает глобальное; глобальное действует только на серверы без собственного значения. Инсталлятор записывает режим и порты прокси **на уровень сервера**, поэтому менять их нужно там — правка только в глобальных настройках на работающий сервер не влияет.

| Параметр | Глобально | Сервер mail-new |
| --- | --- | --- |
| zimbraReverseProxyMailMode (режим веб-прокси) | `redirect` — стандарт для будущих серверов | `redirect` |
| zimbraMailProxyPort / zimbraMailSSLProxyPort (порты прокси) | `0` — не менять | `80` / `443` |
| zimbraMailPort / zimbraMailSSLPort (порты mailboxd) | `80` / `443` — не менять | `8080` / `8443` |

Глобальные порты прокси задать `80`/`443` нельзя: глобальные порты mailboxd по умолчанию уже `80`/`443` (значения для установки без прокси), и Zimbra отклоняет команду с ошибкой `port 80 conflict between zimbraMailProxyPort and zimbraMailPort on global config`. Команда отклоняется целиком — остальные параметры в ней тоже не записываются.

```bash
# от zimbra
zmprov mcf zimbraReverseProxyMailMode redirect                 # глобально — только режим
zmprov ms $(zmhostname) zimbraReverseProxyMailMode redirect     # сервер
/opt/zimbra/libexec/zmproxyconfgen
zmproxyctl restart
```

**Осторожно с кнопкой «сбросить к глобальному значению» в настройках сервера.** Админка загружает глобальные настройки при входе; изменения через `zmprov mcf` она видит только после повторного входа. Сброс портов прокси на сервере подставит глобальный `0` — после сохранения и перезапуска прокси nginx перестанет слушать 80/443. Если это произошло: `zmprov ms $(zmhostname) zimbraMailProxyPort 80 zimbraMailSSLProxyPort 443`.

Остальные параметры прокси (админка «Серверы → mail-new → Прокси-сервер»):

| Параметр | Значение | Почему |
| --- | --- | --- |
| Прокси к вышестоящим серверам через SSL | включено | nginx передаёт запросы в mailboxd по 8443 |
| Прокси-сервер консоли администрирования (9071) | выключено | админка открывается напрямую на 7071 |
| Проверка подлинности PLAIN для IMAP / POP3 | включено | обычный вход по логину и паролю |
| Проверка подлинности GSSAPI для IMAP / POP3 | выключено | вход через Kerberos; требует Kerberos-сферы, SPN и keytab. Без них клиенты получают лишние отказы |
| Режим входа без шифрования IMAP / POP3 | только starttls | на 143 и 110 пароль принимается только после включения шифрования |

**Проверка:**

| Проверка | Команда | Ожидаемый результат |
| --- | --- | --- |
| Значения сервера | `zmprov gs $(zmhostname) zimbraReverseProxyMailMode zimbraMailProxyPort zimbraMailSSLProxyPort zimbraMailPort zimbraMailSSLPort` | `redirect`, `80`, `443`, `8080`, `8443` |
| Глобальные значения | `zmprov gacf \| grep -E 'zimbraReverseProxyMailMode\|zimbraMailProxyPort\|zimbraMailPort'` | `redirect`, порт прокси `0`, порт mailboxd `80` |
| Перенаправление | `curl -sI http://mail-new.example.com/ \| grep -iE '^HTTP\|^Location'` | `301`, `Location: https://mail-new.example.com/` |

### 0.6. Обновления антивируса и антиспама

Базы ClamAV Zimbra обновляет сама (freshclam в составе службы antivirus), с периодичностью `zimbraVirusDefinitionsUpdateFrequency`. Обновление правил SpamAssassin (sa-update) включается параметрами localconfig `antispam_enable_rule_updates` и `antispam_enable_restarts`; по [статье Zextras](https://community.zextras.com/spamassassin/) их умолчание — `false`, поэтому значения проверяются на каждом сервере. Обоим нужен исходящий доступ в интернет: ClamAV — `database.clamav.net` (HTTPS), SpamAssassin — DNS-запросы к `updates.spamassassin.org` и HTTPS к его зеркалам.

```bash
# от zimbra — текущие значения
zmprov gs $(zmhostname) zimbraVirusDefinitionsUpdateFrequency
zmlocalconfig antispam_enable_rule_updates antispam_enable_restarts antispam_enable_rule_compilation
crontab -l | grep -E 'zmsaupdate|zmtrainsa'

# включить обновление правил SpamAssassin, если false
zmlocalconfig -e antispam_enable_rule_updates=true antispam_enable_restarts=true
zmamavisdctl restart && zmmtactl restart
```

| Проверка | Команда | Ожидаемый результат |
| --- | --- | --- |
| freshclam работает | `ps -o pid,args -u zimbra \| grep [f]reshclam` | процесс freshclam |
| Базы ClamAV обновляются | `tail -n 20 /opt/zimbra/log/freshclam.log` | строки `daily.cld … updated` или `is up to date` с сегодняшней датой, без `Can't connect` |
| Правила SpamAssassin обновляются | `grep -i sa-update /var/log/zimbra.log \| tail -5` (после ночного запуска) | записи об обновлении без ошибок загрузки |

### 0.7. imapsync (резервный инструмент)

Основной перенос выполняет скрипт `zm-ldap-migrate.py`; imapsync держим на сервере для разовых задач: забрать ящик с чужого IMAP-сервера, сравнить папки двух ящиков. В репозиториях Ubuntu 24.04 его нет, ставится по официальной инструкции [INSTALL.Ubuntu](https://imapsync.lamiral.info/INSTALL.d/INSTALL.Ubuntu.txt) (v1.25, 12.09.2025):

```bash
sudo apt -y install libauthen-ntlm-perl libclass-load-perl libcrypt-openssl-rsa-perl libcrypt-ssleay-perl \
  libdata-uniqid-perl libdigest-hmac-perl libdist-checkconflicts-perl libencode-imaputf7-perl \
  libfile-copy-recursive-perl libfile-tail-perl libio-compress-perl libio-socket-inet6-perl \
  libio-socket-ssl-perl libio-tee-perl libjson-webtoken-perl libmail-imapclient-perl \
  libmodule-scandeps-perl libnet-dbus-perl libnet-dns-perl libnet-ssleay-perl libpar-packer-perl \
  libproc-processtable-perl libreadonly-perl libregexp-common-perl libsys-meminfo-perl \
  libterm-readkey-perl libtest-fatal-perl libtest-mock-guard-perl libtest-mockobject-perl \
  libtest-pod-perl libtest-requires-perl libtest-simple-perl libunicode-string-perl liburi-perl \
  libtest-nowarnings-perl libtest-deep-perl libtest-warn-perl make time cpanminus
cd /tmp && wget -N https://raw.githubusercontent.com/imapsync/imapsync/master/imapsync
sudo install -m 755 imapsync /usr/local/bin/imapsync
imapsync --version
```

| Проверка | Команда | Ожидаемый результат |
| --- | --- | --- |
| Зависимости | `imapsync` без параметров | справка и пример запуска, без строк `Can't locate … .pm` |

Если imapsync запускается с машины вне 10.0.0.0/16, её адрес добавить в `ignoreip` fail2ban на обоих серверах: ошибки входа при отладке иначе приведут к бану.

### 0.8. Размер писем и вложений

Лимит письма целиком — 100 МБ на всём пути: PMG (раздел 3) и Zimbra одинаково. Лимит считается по письму в виде, в котором оно идёт по SMTP: вложения и текст закодированы base64, это около +37 % к исходному размеру (×4/3 за кодирование и ×78/76 за переносы строк ≈ ×1,368). Отсюда лимиты частей:

| Параметр | Значение | Байт | Что ограничивает |
| --- | --- | --- | --- |
| `zimbraMtaMaxMessageSize` | 100 МБ | 104857600 | письмо целиком при отправке и приёме по SMTP, в том числе из Outlook и с телефона |
| `zimbraMailContentMaxSize` | 12 МБ | 12582912 | текст письма в веб-клиенте, включая картинки, вставленные прямо в HTML |
| `zimbraFileUploadMaxSize` | 60 МБ | 62914560 | вложения в веб-клиенте, все вместе |

Проверка худшего случая: (12 + 60) × 1,368 ≈ 98,5 МБ — письмо проходит, запас около 1,5 МБ на заголовки. Для сравнения, 15 + 60 МБ дают ≈ 102,6 МБ и не проходят.

```bash
# от zimbra
zmprov mcf zimbraMtaMaxMessageSize 104857600
zmprov mcf zimbraMailContentMaxSize 12582912
zmprov mcf zimbraFileUploadMaxSize 62914560
zmmtactl reload
```

Почтовые программы (Outlook, Thunderbird) лимит вложений веб-клиента не видят: для них действует только лимит письма целиком, около 73 МБ файлов. Внешние серверы сравнивают только общий размер письма со своим лимитом: Gmail — 25 МБ на отправку, Microsoft 365 — 36 МБ по умолчанию ([SMTPedia, 28.08.2026](https://smtpedia.com/email-attachment-size-limits/)). Наружу без риска отказа проходят вложения примерно до 18–20 МБ; при отказе отправитель получает уведомление о недоставке. Большие файлы — ссылкой из Nextcloud.

| Проверка | Команда (от zimbra) | Ожидаемый результат |
| --- | --- | --- |
| Лимиты Zimbra | `zmprov gcf zimbraMtaMaxMessageSize`, затем так же `zimbraMailContentMaxSize` и `zimbraFileUploadMaxSize` (по одному атрибуту за вызов) | 104857600, 12582912, 62914560 |
| Лимит Postfix | `postconf -h message_size_limit` | 104857600 |
| Веб-клиент | приложить файлы общим размером больше 60 МБ | сообщение о превышении размера |

### Проверка раздела 0

| Проверка | Команда | Ожидаемый результат |
| --- | --- | --- |
| Имя сервера | `hostname -f` | `mail-new.example.com` |
| Версия Zimbra | `zmcontrol -v` (от zimbra) | `Release 10.1.20_GA_4200001.UBUNTU24_64 UBUNTU24_64 FOSS edition` |
| Службы | `zmcontrol status` (от zimbra) | все строки `Running` |
| Задания cron | `crontab -l \| grep -cE 'zmstatuslog\|zmlogprocess\|zmtrainsa'` (от zimbra) | `3` |
| Журнал Zimbra | `ls -l /var/log/zimbra.log` | файл есть, размер растёт |
| Админка | https://mail-new.example.com:7071 → Монитор → Состояние сервера | все службы зелёные, время обновляется каждые 2–4 минуты |
| Доверенные адреса proxy | `zmprov gcf zimbraMailTrustedIP` (от zimbra) | `127.0.0.1` и `10.0.10.10` |
| Реальный IP в логе | неверный вход в webmail с другого ПК, затем `grep 'handler exception: authentication failed' /opt/zimbra/log/mailbox.log \| tail -1` | `oip=` содержит адрес этого ПК, а не `10.0.10.10` |

## 1. Файрвол (ufw)

Входящие соединения по умолчанию запрещены; порт 25 и LDAP открыты только для PMG и внутренних сетей, клиентские порты — для всех. Порт 7071 открыт для всех, ограничение по геолокации делает периметровый файрвол.

**Перед `ufw enable`** держите открытой вторую SSH-сессию или консоль Proxmox/vSphere. Правила по умолчанию начинают действовать только после `enable`; если вы подключены не из 10.0.0.0/24, сначала добавьте свой адрес (`echo $SSH_CLIENT` покажет его).

```bash
sudo apt -y install ufw
sudo ufw default deny incoming
sudo ufw default allow outgoing

# SSH — только из админской сети
sudo ufw allow from 10.0.0.0/24 to any port 22 proto tcp

# PMG: входящая почта (25) и LDAP-профили PMG (389: вход в карантин, алиасы, правила)
sudo ufw allow from 10.0.10.11 to any port 25,389 proto tcp
sudo ufw allow from 10.0.10.12 to any port 25,389 proto tcp

# SMTP 25 из внутренних сетей (МФУ, ERP, скрипты)
sudo ufw allow from 10.0.20.0/24 to any port 25 proto tcp
sudo ufw allow from 10.0.30.0/24 to any port 25 proto tcp
sudo ufw allow from 10.0.10.0/24 to any port 25 proto tcp

# Клиенты
sudo ufw allow 80,443/tcp            # webmail + проверка Let's Encrypt
sudo ufw allow 465,587/tcp           # отправка писем с авторизацией
sudo ufw allow 110,143,993,995/tcp   # POP3 / IMAP

# Админка — для всех; ограничение по геолокации на периметровом файрволе
sudo ufw allow 7071/tcp

sudo ufw logging low                 # нужно для jail ufw-portscan в разделе 2
sudo ufw enable
```

| Порт | Назначение | Кому открыт |
| --- | --- | --- |
| 22 | SSH | 10.0.0.0/24 |
| 25 | приём почты (MTA→MTA) | PMG и внутренние сети. Клиенты на 25 не отправляют |
| 389 | LDAP | PMG (LDAP-профили: вход в карантин, объединение алиасов, правила) |
| 80, 443 | webmail, Let's Encrypt | все |
| 465, 587 | отправка из почтовых клиентов | все |
| 110, 995 / 143, 993 | POP3 / IMAP (без шифрования + STARTTLS / SSL) | все |
| 7071 | админка | все (на периметровом файрволе — только своя страна) |

**Заметки:**

- Периметровый файрвол должен пробрасывать порты **без SNAT**. Иначе во всех логах будет адрес шлюза, fail2ban забанит его или не забанит никого.
- Сканирование закрытых портов из интернета отсекает периметровый файрвол (защита от сканирования); jail ufw-portscan в разделе 2 — страховка.
- Если IPv6 на сервере не используется: `IPV6=no` в `/etc/default/ufw`, затем `sudo ufw reload` — исчезнут дубли правил `(v6)`.
- Изменить правило нельзя, только заменить: `sudo ufw insert N <новое правило>`, проверить `sudo ufw status numbered`, удалить старое `sudo ufw delete M`. Номера пересчитываются после каждой операции.

### Проверка раздела 1

| Проверка | Команда | Ожидаемый результат |
| --- | --- | --- |
| Состояние | `sudo ufw status verbose` | `Status: active`, `Logging: on (low)`, `Default: deny (incoming), allow (outgoing)` |
| Правила | `sudo ufw status numbered` | 10 правил IPv4 (22, 2 × PMG, 3 × сети на 25, 80/443, 465/587, POP/IMAP, 7071) + дубли `(v6)` для правил «Anywhere», если IPv6 включён |
| SSH из админской сети | новая SSH-сессия с ПК из 10.0.0.0/24 | подключается |
| SSH из другой сети | `nc -vz -w3 10.0.10.10 22` с ПК вне 10.0.0.0/24 | timeout |
| Порт 25 снаружи закрыт | `nc -vz -w3 10.0.10.10 25` с ПК вне разрешённых сетей | timeout |
| Клиентские порты открыты | `nc -vz -w3 10.0.10.10 993` с любого ПК | `succeeded` / `open` |
| Журнал ufw | `ls -l /var/log/ufw.log` | файл есть; строки `[UFW BLOCK]` появляются после попыток на закрытые порты |

## 2. fail2ban

fail2ban читает журналы, находит неудачные входы и временно банит IP через nftables, а сведения об атакующем отправляет в AbuseIPDB. Первичный бан — сутки, повторные растут до 30 дней; три бана за сутки в любых jail-ах дают бан на неделю на всех портах (recidive). Внутренние сети 10.0.0.0/16 не банятся никогда.

**Главное правило конфигурации:** файлы пакета `*.conf` не редактируются. Все изменения — только в `*.local` и `jail.d/`. Иначе при `apt upgrade` пакет спросит, чей вариант оставить, и при неверном ответе затрёт ключ API и настройки, а автоматические обновления безопасности для fail2ban перестанут ставиться. Собственные фильтры `filter.d/zimbra-*.conf` и `ufw-portscan.conf` пакету не принадлежат — их обновления не затрагивают.

| Jail | Журнал | Что ловит | Действие | Порты бана | Категории AbuseIPDB |
| --- | --- | --- | --- | --- | --- |
| zimbra-smtp | /var/log/zimbra.log | неверный пароль SMTP, мусор вместо SMTP, попытки relay | multiport | 25, 465, 587 | 18 |
| zimbra-web | /opt/zimbra/log/mailbox.log | неверный вход в webmail, админку 7071, IMAP | multiport | 80, 443, 110, 143, 993, 995, 7071 | 18, 21 |
| zimbra-mailproxy | /opt/zimbra/log/nginx.log | несуществующий ящик через IMAP/POP, неверный пароль POP | multiport | 110, 143, 993, 995 | 18 |
| zimbra-nginx | /opt/zimbra/log/nginx.log | сканеры: HTTP на HTTPS-порт, мусорные запросы | multiport | 80, 443 | 14, 19 |
| ufw-portscan | /var/log/ufw.log | TCP SYN на закрытые порты | allports | все | 14 |
| recidive | /var/log/fail2ban.log | 3 бана за сутки в других jail-ах | allports | все | не отправляется |
| sshd | journal | подбор SSH (штатный jail Ubuntu) | multiport | 22 | не отправляется |

**multiport** — бан на портах своего jail-а: если сотрудник ошибся паролем в Outlook (SMTP), webmail у него продолжит работать. **allports** — бан на всех портах; у сканера и повторного нарушителя «своего» порта нет.

### 2.1. Установка

```bash
sudo apt -y install fail2ban
```

Предупреждения `SyntaxWarning: invalid escape sequence` при установке относятся к тестовым файлам пакета (fail2ban 1.0.2 + Python 3.12) и на работу не влияют.

### 2.2. /etc/fail2ban/fail2ban.local

```ini
[Definition]
allowipv6 = auto
```

Убирает предупреждение `'allowipv6' not defined` — в fail2ban.conf этой версии параметра нет.

### 2.3. /etc/fail2ban/filter.d/zimbra-smtp.conf

```ini
[Definition]
_smtpd = postfix/(?:submission/|smtps/)?smtpd\[\d+\]:
_sub   = postfix/(?:submission|smtps)/smtpd\[\d+\]:

failregex = %(_smtpd)s warning: [-._\w]+\[<ADDR>\]: SASL [A-Z\d-]+ authentication failed(?::.*)?$
            %(_smtpd)s warning: non-SMTP command from [-._\w]+\[<ADDR>\]:
            %(_smtpd)s too many errors after AUTH from [-._\w]+\[<ADDR>\]
            %(_smtpd)s NOQUEUE: reject: RCPT from [-._\w]+\[<ADDR>\]:.*Relay access denied
            %(_sub)s NOQUEUE: reject: RCPT from [-._\w]+\[<ADDR>\]:.*Client host rejected: Access denied
ignoreregex =
```

Отказы `Recipient address rejected`, `Sender address rejected` и `Helo command rejected` сюда намеренно не включены: на порт 25 приходит только PMG, и такие правила забанили бы его. Эти проверки — на стороне PMG.

### 2.4. /etc/fail2ban/filter.d/zimbra-web.conf

```ini
[Definition]
_msg = (?:SoapEngine - handler exception: |imap - )authentication failed for \[
failregex = \[[^\]]*\boip=<ADDR>;[^\]]*\] %(_msg)s
            \[(?:[^\]]*;)?ip=<ADDR>;(?![^\]]*\boip=)[^\]]*\] %(_msg)s
ignoreregex = \[qtp[^\]]*:smtp://
datepattern = ^%%Y-%%m-%%d %%H:%%M:%%S
```

- Журнал — **mailbox.log**, а не audit.log: в audit.log попадают только неудачные входы в существующие учётки (`invalid password`), а попытки с несуществующими именами (`account not found`) — нет. Подбор чаще всего идёт перебором имён, поэтому фильтр по audit.log пропустил бы большую часть атак.
- Правило 1 — подключения через proxy: реальный клиент в поле `oip`. Правило 2 — прямые подключения к админке 7071: клиент в поле `ip`, поля `oip` нет.
- `ignoreregex` исключает проверку пароля SMTP через внутренний порт 7073: её уже считает zimbra-smtp. Без исключения одна ошибка в Outlook засчитывалась бы дважды и закрывала бы пользователю webmail.
- Работает только при настроенном `zimbraMailTrustedIP` (раздел 0.4).

### 2.5. /etc/fail2ban/filter.d/zimbra-mailproxy.conf

```ini
[Definition]
_client = client: <ADDR>(?::\d+)?(?: using starttls)?, server: 
failregex = An error occurred in mail zmauth: .*, %(_client)s
            upstream sent invalid response: "-ERR authentication failed[^"]*" while reading response from upstream, %(_client)s
ignoreregex =
```

Попытка входа в несуществующий ящик по IMAP/POP не доходит до mailboxd — её отклоняет nginx, поэтому она есть только в nginx.log. Неверный пароль POP в mailbox.log пишется дважды, поэтому он тоже считается здесь, по одной строке nginx на попытку.

### 2.6. /etc/fail2ban/filter.d/zimbra-nginx.conf

```ini
[Definition]
failregex = client sent plain HTTP request to HTTPS port while reading client request headers, client: <ADDR>,
            client sent invalid method while reading client request line, client: <ADDR>,
            client sent invalid request while reading client request line, client: <ADDR>,
ignoreregex =
```

### 2.7. /etc/fail2ban/filter.d/ufw-portscan.conf

```ini
[Definition]
failregex = \[UFW BLOCK\] .* SRC=<ADDR> .* PROTO=TCP .* SYN 
ignoreregex =
```

Только TCP SYN. Без этого ограничения фильтр каждые \~125 секунд срабатывает на служебные IGMP-запросы коммутатора с адресом `0.0.0.0`.

### 2.8. /etc/fail2ban/action.d/abuseipdb.local

```ini
[Definition]
norestored = 1
actionban = ts_iso=$(date -d @<time> '+%%Y-%%m-%%dT%%H:%%M:%%S%%:z'); \
            ts_h=$(date -d @<time> '+%%Y-%%m-%%d %%H:%%M:%%S %%Z'); \
            curl -sS --fail --max-time 10 'https://api.abuseipdb.com/api/v2/report' \
              -H 'Accept: application/json' -H 'Key: <abuseipdb_apikey>' \
              --data-urlencode 'ip=<ip>' \
              --data-urlencode "timestamp=$ts_iso" \
              --data-urlencode "comment=<abuseipdb_comment>, last attempt $ts_h" \
              --data 'categories=<abuseipdb_category>' >/dev/null \
            || logger -t f2b-abuseipdb "report failed for <ip> (<name>)"

[Init]
abuseipdb_apikey   = ВАШ_КЛЮЧ
abuseipdb_reporter = mail-new
abuseipdb_comment  = Fail2Ban [<abuseipdb_reporter>] jail <name>: <failures> failed attempts
```

```bash
sudo chmod 600 /etc/fail2ban/action.d/abuseipdb.local
```

- Ключ API создаётся в кабинете abuseipdb.com → Account → API. Не храните его в чатах и общих файлах.
- `abuseipdb_reporter` — короткое имя этого сервера, уникальное для каждого сервера. Отчёты на AbuseIPDB публичные, поэтому без домена.
- В отчёт уходят только имя сервера, jail, число попыток и время. Строки журнала (`<matches>`) **не отправляются**: в них логины сотрудников и внутренние имена.
- `timestamp` — время последней попытки: AbuseIPDB показывает время атаки, а не отправки отчёта. `norestored = 1` — после перезапуска fail2ban старые баны повторно не отправляются.
- Знак `%` в конфигах fail2ban всегда удваивается.

Проверка ключа (ничего не отправляет в базу):

```bash
read -rs KEY; curl -sG https://api.abuseipdb.com/api/v2/check --data-urlencode "ipAddress=8.8.8.8" \
  -H "Key: $KEY" -H "Accept: application/json" | head -c 200; echo; unset KEY
```

Ответ с `"data":{...}` — ключ рабочий, с `"errors"` — нет.

### 2.9. /etc/fail2ban/jail.d/zimbra.local

```ini
[DEFAULT]
usedns   = no
ignoreip = 127.0.0.1/8 ::1 10.0.0.0/16
banaction          = nftables-multiport
banaction_allports = nftables-allports
findtime = 1d
bantime  = 1d
bantime.increment = true
bantime.maxtime   = 30d
maxretry = 3

[zimbra-smtp]
enabled  = true
filter   = zimbra-smtp
logpath  = /var/log/zimbra.log
port     = 25,465,587
action   = %(action_)s
           abuseipdb[abuseipdb_category="18"]

[zimbra-web]
enabled  = true
filter   = zimbra-web
logpath  = /opt/zimbra/log/mailbox.log
port     = 80,443,110,143,993,995,7071
action   = %(action_)s
           abuseipdb[abuseipdb_category="18,21"]

[zimbra-mailproxy]
enabled  = true
filter   = zimbra-mailproxy
logpath  = /opt/zimbra/log/nginx.log
port     = 110,143,993,995
action   = %(action_)s
           abuseipdb[abuseipdb_category="18"]

[zimbra-nginx]
enabled  = true
filter   = zimbra-nginx
logpath  = /opt/zimbra/log/nginx.log
port     = 80,443
action   = %(action_)s
           abuseipdb[abuseipdb_category="14,19"]

[ufw-portscan]
enabled   = true
filter    = ufw-portscan
logpath   = /var/log/ufw.log
banaction = %(banaction_allports)s
maxretry  = 20
findtime  = 2m
bantime   = 1d
action    = %(action_)s
            abuseipdb[abuseipdb_category="14"]

[recidive]
enabled   = true
logpath   = /var/log/fail2ban.log
banaction = %(banaction_allports)s
findtime  = 1d
maxretry  = 3
bantime   = 1w
```

- `[DEFAULT]` действует на все jail-ы, включая штатный sshd.
- `action = %(action_)s` — обычный бан через nftables; вторая строка — отчёт в AbuseIPDB. Строка продолжения обязана начинаться с пробелов. Без `%(action_)s` jail будет только отправлять отчёты и перестанет банить.

### 2.10. Применение и исключения Zimbra

```bash
sudo fail2ban-client -t                      # проверка синтаксиса всех файлов
sudo systemctl restart fail2ban && sleep 3 && sudo fail2ban-client status

# адреса PMG и n8n — в исключения встроенного DoS-фильтра Zimbra (от zimbra)
sudo su - zimbra -c "zmprov mcf +zimbraHttpThrottleSafeIPs 10.0.10.11 +zimbraHttpThrottleSafeIPs 10.0.10.12"
```

`sleep 3` нужен: после `restart` fail2ban несколько секунд поднимает jail-ы, и `status` без паузы выдаёт `Failed to access socket path` — это не ошибка.

**Встроенный фильтр Zimbra** оставлен со значениями по умолчанию: блокировка IP для webmail на 15 минут после 10 неудачных входов (`zmprov gacf | grep -i InvalidLoginFilter`). Для внешних адресов первым срабатывает fail2ban (3 попытки); фильтр Zimbra — вторая линия, в том числе для внутренних сетей, которые fail2ban не банит. Счётчик по IP копится, а не обнуляется со временем, поэтому за общим NAT офиса порог набирается суммарно.

### 2.11. Встроенный фильтр Zimbra: как он работает вместе с fail2ban

Zimbra сама блокирует IP для webmail на 15 минут после 10 неудачных входов (Invalid Login Filter, значения по умолчанию). Фильтр считает только входы через webmail и SOAP (HTTP); IMAP, POP и SMTP он не защищает. Счётчик хранится в памяти mailboxd, копится без сброса по времени и обнуляется только при блокировке или перезапуске mailboxd.

| Источник попыток | Кто срабатывает |
| --- | --- |
| Внешний IP, обычный подбор | fail2ban: 3 попытки за сутки → бан в nftables. До порога Zimbra запросы не доходят |
| Внутренний IP (10.0.0.0/16) | только фильтр Zimbra: fail2ban внутренние сети не банит |
| Внешний IP, медленный подбор (1–2 попытки в сутки) | fail2ban не набирает 3 попытки в окне 1 сутки; счётчик Zimbra не сбрасывается и через несколько дней блокирует IP |
| fail2ban остановлен или периметровый файрвол подменяет адреса | только фильтр Zimbra |

Текущие значения (от zimbra):

```bash
zmprov gacf | grep -i InvalidLoginFilter
```

| Атрибут | По умолчанию | Смысл |
| --- | --- | --- |
| zimbraInvalidLoginFilterMaxFailedLogin | 10 | неудачных входов с одного IP до блокировки |
| zimbraInvalidLoginFilterDelayInMinBetwnReqBeforeReinstating | 15 | минут блокировки |
| zimbraInvalidLoginFilterReinstateIpTaskIntervalInMin | 5 | как часто проверяется, кого разблокировать |
| zimbraInvalidLoginFilterMaxSizeOfFailedIpDb | 7000 | сколько IP отслеживается одновременно |

Команды для просмотра текущих блокировок у Zimbra нет — состояние только в памяти. Блокировки видны по журналу.

**Кто блокировался и когда последний раз:**

```bash
zgrep -h 'suspended, for repeated failed login' /opt/zimbra/log/mailbox.log* \
  | sed -E 's/^(\S+ [0-9:]+),.*Access from IP (\S+) suspended.*/\1 \2/' \
  | awk '{last[$3]=$1" "$2; n[$3]++} END{for(ip in last) print last[ip], ip, n[ip]}' \
  | sort
```

Вывод: дата и время последней записи, IP, число записей. IP заблокирован сейчас, если последняя запись моложе 15–20 минут; старше — уже разблокирован.

**Кто близок к блокировке** (неудачные входы в webmail и админку по IP за текущий журнал):

```bash
grep 'SoapEngine - handler exception: authentication failed' /opt/zimbra/log/mailbox.log \
  | grep -v ':smtp://' \
  | grep -oP 'oip=\K[0-9a-f.:]+(?=;)|\[ip=\K[0-9a-f.:]+(?=;port)' \
  | sort | uniq -c | sort -rn | head -20
```

Точного значения счётчика Zimbra по журналу не восстановить: он обнуляется при блокировке и при перезапуске mailboxd.

**Снятие блокировки:**

| Способ | Эффект |
| --- | --- |
| подождать | 15–20 минут, снимается автоматически |
| `zmmailboxdctl restart` (от zimbra) | снимает все блокировки и обнуляет счётчики; 1–2 минуты недоступности webmail для всех |

Разблокировать один IP без перезапуска нельзя. Типичная причина блокировки внутреннего адреса — устройство в офисе (телефон, Outlook) с сохранённым старым паролем: искать по второй команде, какой IP набирает ошибки, и по логину в строке журнала.

### Проверка раздела 2

**Шаг 1. Конфигурация** — сразу после 2.10.

| Проверка | Команда | Ожидаемый результат |
| --- | --- | --- |
| Синтаксис | `sudo fail2ban-client -t` | `OK: configuration test is successful`, без строк `ERROR` |
| Jail-ы | `sudo fail2ban-client status` | 7 jail-ов: `recidive, sshd, ufw-portscan, zimbra-mailproxy, zimbra-nginx, zimbra-smtp, zimbra-web` |
| Действия jail-ов Zimbra | `sudo fail2ban-client get zimbra-web actions` | `nftables-multiport, abuseipdb` — ровно два. Так же у zimbra-smtp, zimbra-mailproxy, zimbra-nginx |
| Действия ufw-portscan | `sudo fail2ban-client get ufw-portscan actions` | `nftables-allports, abuseipdb` |
| Действия recidive | `sudo fail2ban-client get recidive actions` | `nftables-allports` |
| Используется abuseipdb.local | `sudo fail2ban-client -d 2>/dev/null \| grep -c 'last attempt'` | число больше 0. Если 0 — работает штатный файл, проверьте имя и права abuseipdb.local |
| Файлы пакета не изменены | `dpkg -V fail2ban \| grep ' c '` | пустой вывод (строки `missing /usr/share/doc...` — норма для минимального образа) |
| Права на ключ | `ls -l /etc/fail2ban/action.d/abuseipdb.local` | `-rw-------` |

**Шаг 2. Тестовые неверные входы.** Выполняются с **другого** ПК, не с сервера; каждый тест — 1–2 попытки. Адреса из 10.0.0.0/16 в `ignoreip`, поэтому бана не будет — проверяется, что фильтр находит попытку и правильный IP.

Тестовый ящик создаётся в существующем домене (от zimbra):

```bash
zmprov ca test-f2b@mail-new.example.com "Xx-$(openssl rand -hex 6)"
```

| # | Тест | Как выполнить | Строка в журнале | Jail |
| --- | --- | --- | --- | --- |
| 1 | Webmail, неверный пароль / несуществующий ящик | браузер → https://mail-new.example.com | mailbox.log: `[oip=<IP ПК>;…] SoapEngine - handler exception: authentication failed for [...]` | zimbra-web |
| 2 | Админка, неверный пароль | браузер → https://mail-new.example.com:7071 | mailbox.log: `[ip=<IP ПК>;port=…] SoapEngine - handler exception: authentication failed for [admin]` | zimbra-web |
| 3 | IMAP 993, неверный пароль | `curl -k --url imaps://mail-new.example.com/ --user 'test-f2b@mail-new.example.com:wrong'` | mailbox.log: `[ip=10.0.10.10;oip=<IP ПК>;…] imap - authentication failed for [...]` | zimbra-web |
| 4 | IMAP 143 STARTTLS, неверный пароль | `curl -k --url imap://mail-new.example.com/ --ssl-reqd --user 'test-f2b@mail-new.example.com:wrong'` | то же, что тест 3 | zimbra-web |
| 5 | IMAP 993 / 143, несуществующий ящик | тот же `curl` с `--user 'nouser@mail-new.example.com:wrong'` | nginx.log: `An error occurred in mail zmauth: user not found… client: <IP ПК>:<порт>[ using starttls], server:` | zimbra-mailproxy |
| 6 | POP3 995, неверный пароль | `curl -k --url pop3s://mail-new.example.com/ --user 'test-f2b@mail-new.example.com:wrong'` | nginx.log: `upstream sent invalid response: "-ERR authentication failed: LOGIN failed"… client: <IP ПК>` | zimbra-mailproxy |
| 7 | POP3 995, несуществующий ящик | тот же `curl` с `nouser@…` | nginx.log: `An error occurred in mail zmauth…` | zimbra-mailproxy |
| 8 | SMTP 587, неверный пароль | `curl -k --url smtp://mail-new.example.com:587 --ssl-reqd --user 'test-f2b@mail-new.example.com:wrong' --mail-from test-f2b@mail-new.example.com --mail-rcpt test-f2b@mail-new.example.com -T /dev/null` | zimbra.log: `postfix/submission/smtpd[…]: warning: …[<IP ПК>]: SASL LOGIN authentication failed` | zimbra-smtp |
| 9 | SMTPS 465, неверный пароль | тот же `curl` с `--url smtps://mail-new.example.com:465` без `--ssl-reqd` | zimbra.log: `postfix/smtps/smtpd[…]: warning: … SASL … authentication failed` | zimbra-smtp |
| 10 | Сканер: HTTP на HTTPS-порт | `curl -s http://mail-new.example.com:443/ >/dev/null` | nginx.log: `client sent plain HTTP request to HTTPS port… client: <IP ПК>` | zimbra-nginx |

**Встроенный фильтр Zimbra.** После серии неудачных входов с одного IP Zimbra сама временно блокирует его для webmail (Invalid Login Filter; порог и срок — атрибуты `zimbraInvalidLoginFilter*`, смотреть `zmprov gacf | grep -i InvalidLoginFilter`). Фильтр действует и на внутренние сети. При частых тестах с одного ПК вход в webmail начнёт отвечать ошибкой, а в mailbox.log вместо `authentication failed` появятся `Access from IP … suspended, for repeated failed login` и Java stacktrace `Service Unavailable`. Это не ошибка конфигурации: подождать указанное время или продолжить тесты с другого ПК.

Тесты 8–9 дополнительно пишут строку в mailbox.log (`smtp://…:7073`, `oproto=smtp`) — она **должна** попасть в `Ignoreregex` фильтра zimbra-web, а не в совпадения.

**Шаг 3. Подсчёт совпадений** после тестов:

```bash
for p in zimbra-web:/opt/zimbra/log/mailbox.log zimbra-mailproxy:/opt/zimbra/log/nginx.log \
         zimbra-nginx:/opt/zimbra/log/nginx.log zimbra-smtp:/var/log/zimbra.log \
         ufw-portscan:/var/log/ufw.log; do
  j=${p%%:*}; f=${p#*:}
  echo "-- $j"; sudo fail2ban-regex "$f" /etc/fail2ban/filter.d/$j.conf | grep -E '^\|\s+[0-9]+\)|^Lines:|^Ignoreregex'
done
```

| Jail | Ожидаемо |
| --- | --- |
| zimbra-web | совпадения по правилу 1 (тесты 1, 3, 4) и правилу 2 (тест 2); `Ignoreregex` — по числу SMTP-тестов 8–9 |
| zimbra-mailproxy | совпадения по тестам 5–7, включая строки `using starttls` |
| zimbra-nginx | не меньше 1 (тест 10) |
| zimbra-smtp | по числу попыток тестов 8–9 |
| ufw-portscan | только TCP SYN; адрес `0.0.0.0` не встречается |

Во всех совпадениях IP должен быть адресом тестового ПК. Адрес `10.0.10.10` (сам сервер) в совпадениях означает, что не настроен `zimbraMailTrustedIP` (раздел 0.4).

После тестов удалить ящик (от zimbra): `zmprov da test-f2b@mail-new.example.com`.

**Шаг 4. Действие бана.** Только через ручной бан и **не на зарезервированных адресах**: AbuseIPDB принимает отчёт и по ним, это ложный отчёт в публичной базе.

```bash
# бан без отчёта: временно на jail recidive (у него нет AbuseIPDB)
sudo fail2ban-client set recidive banip 192.0.2.10
sudo nft list ruleset | grep -A2 addr-set-recidive      # ожидаемо: elements = { 192.0.2.10 }
sudo fail2ban-client set recidive unbanip 192.0.2.10
```

Отчёты в AbuseIPDB проверяются на первом реальном бане: в кабинете → My Reports комментарий вида `Fail2Ban [mail-new] jail zimbra-web: 6 failed attempts, last attempt 2026-11-11 13:14:27 EET`, без строк журнала. Сбои отправки: `journalctl -t f2b-abuseipdb --since today`.

### Известные сообщения и их значение

| Сообщение | Где | Значение |
| --- | --- | --- |
| `SyntaxWarning: invalid escape sequence` | установка fail2ban | тестовые файлы пакета, не влияет |
| `WARNING 'allowipv6' not defined` | `fail2ban-client -t` | нет fail2ban.local (2.2) |
| `Failed to access socket path` | `fail2ban-client status` сразу после restart | fail2ban ещё стартует; повторить через 3 секунды |
| `Found no accessible config files for 'filter.d/…'` | `fail2ban-client -t` | файл фильтра не создан или опечатка в имени; fail2ban с такой ошибкой не стартует — не перезапускайте его до исправления |
| `missing /usr/share/doc/…`, `/usr/share/man/…` | `dpkg -V` | минимальный образ, норма |
| `??5?????? c /etc/fail2ban/…conf` | `dpkg -V fail2ban` | изменён файл пакета — перенести правки в `.local`, файл восстановить: удалить его и выполнить `sudo apt-get install --reinstall -o Dpkg::Options::="--force-confmiss" fail2ban` |
| `SSL_do_handshake() failed … certificate unknown` | nginx.log | браузер не доверяет самоподписанному сертификату; не атака, не банится; исчезнет после Let's Encrypt (раздел 4) |
| `Found 0.0.0.0` в jail ufw-portscan | fail2ban.log | старая версия фильтра без ограничения TCP SYN (2.7) |
| `nginxlookup - user not found` без IP | mailbox.log | служебная строка поиска маршрута; попытку считает zimbra-mailproxy по nginx.log |
| `Ignore <IP> by ip` | fail2ban.log | IP из `ignoreip` — бан намеренно не выполняется |
| `# Warning: table ip filter is managed by iptables-nft` | `nft list ruleset` | таблицы ufw; fail2ban работает в своей таблице `inet f2b-table`, конфликта нет |

`misc - Access from IP <IP> suspended, for repeated failed login` и следом `webclient - system failure: error while proxying request to target server: Service Unavailable` с Java stacktrace в mailbox.log — сработал встроенный фильтр Zimbra против подбора паролей. Проверка учётки не выполнялась, поэтому строки `authentication failed` нет. Блокировка снимается автоматически. Для внутренних адресов (NAT офиса) это может выглядеть как жалоба «не могу войти в webmail» — причина обычно в устройстве со старым паролем.

## 3. Связка с Proxmox Mail Gateway

### На Zimbra

```bash
sudo su - zimbra
# исходящая почта через PMG (порт 26), второй — резерв
zmprov mcf zimbraMtaRelayHost pmg1.example.com:26
zmprov mcf zimbraMtaFallbackRelayHost pmg2.example.com:26
# PMG — доверенные хосты (важно и для SpamAssassin на Zimbra)
zmprov ms $(zmhostname) zimbraMtaMyNetworks "127.0.0.0/8 [::1]/128 <ZIMBRA_IP>/32 <PMG1_IP>/32 <PMG2_IP>/32"
zmcontrol restart
```

Антиспам и антивирус на Zimbra остаются включёнными — второй уровень защиты.

### На PMG (master, синхронизируется на node)

| Раздел | Значение |
| --- | --- |
| Mail Proxy → Relaying → Default Relay | mail-new.example.com, порт 25, Disable MX lookup = Yes (задать при переключении) |
| Mail Proxy → Relay Domains | example.com, example.net, example.org, shop.example, news.example, intl.example |
| Mail Proxy → Networks | IP Zimbra (для приёма исходящей на порт 26) |
| Mail Proxy → Options → Verify Receivers | пробный SMTP-запрос к Zimbra (не LDAP); 450 на время переезда, затем 550 |
| Mail Proxy → DKIM | один селектор `pmg2026` (2048) на все домены, TXT в DNS каждого домена — порядок ниже |
| Certificates → ACME | Let's Encrypt, HTTP-01; на каждом узле свой домен для SMTP и API — подробнее ниже |

Обучение Bayes на PMG из пометок Junk/Not Junk в Zimbra — отдельным workflow в n8n.

**Маршрутизация входящей почты.** Relay Domains только разрешают приём для доменов; куда передавать почту, задают Default Relay и вкладка Transports. Для домена из Transports действует его запись, Default Relay — для остальных доменов из Relay Domains. При пустых обоих PMG доставляет по MX домена из своего DNS — схема зависит от внутреннего DNS, при переезде о ней легко забыть. Поэтому адрес Zimbra задаётся явно в одном месте: Default Relay = mail-new.example.com, порт 25, Disable MX lookup = Yes; в Transports — только исключения.

```bash
# на PMG: куда реально уходит почта и что в таблице транспортов
grep 'status=sent' /var/log/mail.log | grep -v '127.0.0.1' | tail -5 | grep -o 'relay=[^ ]*'
postconf -h transport_maps relayhost
cat /etc/pmg/transport
```

**Mail Proxy → Options** — принятые значения:

| Параметр | Значение | Почему |
| --- | --- | --- |
| Message Size (bytes) | 104857600 | 100 МБ, как `zimbraMtaMaxMessageSize` (раздел 0.8). Если у Zimbra лимит больше, исходящее письмо примет Zimbra, а отвергнет PMG |
| Reject Unknown Clients | Yes | отправители без корректной обратной DNS-записи; отказ временный (450), при сбое DNS отправитель повторит. Отвергнутых проверять командой ниже, нужных добавлять в Welcomelist |
| Reject Unknown Senders, SMTP HELO checks, Use SPF | Yes | — |
| Verify Receivers | Yes (450) на время переезда, после — 550 | Postfix PMG (`reject_unverified_recipient`) проверяет адрес пробным `RCPT TO` к Default Relay, то есть к Zimbra; LDAP-профиль для этого не нужен. 450 — временный отказ: если на проверяемом сервере учётки ещё нет, письмо дождётся. 550 — окончательный: отправитель с опечаткой узнаёт сразу. При недоступности проверки отказ временный при любом значении |
| Greylisting IPv4 /24, IPv6 /64 | Yes | первое письмо от нового отправителя задерживается на несколько минут |
| Client Connection Count / Rate / Message Rate | 10 / 10 / 5 | лимиты для внешних отправителей. Адреса Zimbra (старого и нового) обязательно во вкладке Networks: доверенные сети этими лимитами не ограничиваются |
| Accept Broken MIME Structure | No | письма с повреждённой MIME-структурой — способ спрятать вложение от проверки |
| Before Queue Filtering | Yes | отказ в ходе SMTP-сессии, без отражённого спама |
| DNSBL Threshold / Sites | 2 / zen.spamhaus.org, b.barracudacentral.org, bl.spamcop.net, Abusix | отказ при попадании минимум в два списка. Ключ Abusix входит в имя зоны — снимки экрана с ним не публиковать |

**Mail Proxy → TLS:**

| Параметр | Значение | Почему |
| --- | --- | --- |
| Enable TLS | Yes | STARTTLS при приёме и отправке, если другая сторона его поддерживает; без TLS почта тоже принимается |
| Enable TLS Logging | Yes | в журнале видно, кто подключался с шифрованием, — для диагностики |
| Add TLS received header | Yes | в заголовке `Received` письма указываются протокол и шифр, с которыми оно пришло из интернета: по заголовкам видно, было ли соединение зашифровано. Отправителю и получателю ничего не меняет |
| TLS Destination Policy | пусто | обязательный TLS при отправке задаётся отдельно для партнёров, которые его требуют (банки, госорганы): домен + политика `encrypt` (только шифрование) или `verify` (шифрование и проверка сертификата). Для остальных — по возможности |
| TLS Inbound Domains | пусто | обязательный TLS при приёме для своих доменов отверг бы почту от старых серверов без TLS |

**Spam Detector → Options:**

| Параметр | Значение | Почему |
| --- | --- | --- |
| Use auto-welcomelists | No | автоматический белый список по истории отправителя легко «отравить» |
| Use Bayesian filter | Yes | работает после обучения: минимум 200 спам- и 200 обычных писем; обучение из пометок Junk/Not Junk в Zimbra — через n8n |
| Use RBL checks | Yes | проверки по спискам внутри антиспама, тот же DNS, что у DNSBL |
| Use Razor2 checks | Yes | нужен исходящий TCP 2703; если закрыт — выключить, иначе каждая проверка ждёт тайм-аута |
| Extract Text from Attachments | Yes | текст из PDF и документов Office тоже проверяется |
| Max Spam Size (bytes) | 10485760 | письма крупнее антиспамом не проверяются (антивирусом — да); при высокой нагрузке на CPU снизить до 2–5 МБ |
| Languages | all | письма на румынском, русском и английском |
| Backscatter Score / Heuristic Score | 1 / 3 | добавки к оценке за «отражённые» уведомления о недоставке и срабатывания эвристики ClamAV |

```bash
# на PMG
nc -vz -w5 discovery.razor.cloudmark.com 2703            # Razor2: succeeded/open
sa-learn --dump magic | grep -E 'nspam|nham'              # Bayes: ≥ 200 каждого — фильтр участвует в оценке
```

**Spam Detector → Quarantine:**

| Параметр | Значение | Почему |
| --- | --- | --- |
| Lifetime (days) | 30 | пользователь успевает заметить и вернуть нужное письмо |
| Authentication mode | Ticket (решение: Ticket или LDAP) | Ticket — вход по ссылке из отчёта без пароля: пересланный отчёт даёт доступ к карантину. LDAP — ссылка или вход логином и паролем Zimbra через уже настроенный LDAP-профиль |
| User Spamreport Style | Verbose | список писем с отправителем, темой и кнопками «доставить» и «удалить» |
| Quarantine Host / port | mxs.example.com / 8006 | 8006 — это и интерфейс администратора PMG: наружу не открывать; доступ из офиса или VPN, либо обратный прокси только для `/quarantine` |
| EMail 'From:' | antispam@example.com | домен подписывается DKIM PMG; для ответов — алиас или список рассылки на администраторов в Zimbra |
| View images | Load external images on demand | вложенные картинки показываются, внешние — только по кнопке: иначе они подтверждают спамеру, что адрес живой |
| Allow HREFs | No | в карантине спам и фишинг — ссылки в предпросмотре не должны быть кликабельными |

```bash
# на PMG: отчёт себе вручную, не дожидаясь ежедневной рассылки
pmgqm send --receiver <ваш адрес> --timespan week
# с компьютера вне сети: 8006 снаружи закрыт (ожидаемо timeout)
nc -vz -w5 <внешний IP PMG> 8006
```

**Virus Detector → Options:**

| Параметр | Значение | Почему |
| --- | --- | --- |
| Block encrypted archives and documents | Yes | зашифрованный архив или документ не проверить — частый способ доставки вредоносов. Доверенных отправителей (банки, госорганы) — в исключения: Who Object + правило Accept с приоритетом выше |
| Max recursion / Max files | 9 / 1000 | глубина вложенности и число файлов в архиве |
| Max file size | 100000000 | файл крупнее не проверяется; должно быть не меньше разрешённых вложений (60 МБ, раздел 0.8) |
| Max scan size | 100000000 | объём проверки на одно письмо, покрывает письмо в 100 МБ |
| Max credit card numbers | 0 | проверка номеров карт выключена |

**Virus Detector → ClamAV:** Database Mirror `database.clamav.net`, Incremental Download = Yes — загружаются только изменения базы, а не вся база daily при каждом обновлении: меньше трафика и нет риска упереться в ограничение частых полных загрузок у сети доставки ClamAV. В Status база `daily` должна быть сегодняшней; `main` и `bytecode` обновляются раз в несколько месяцев — старые даты у них нормальны.

```bash
# на PMG: обновление баз без ошибок (норма — updated / up-to-date; плохо — Can't download, 429, cool-down)
journalctl -u clamav-freshclam --since today --no-pager | tail -20
```

**Virus Detector → Quarantine** (видят только администраторы): Lifetime 14 дней (30 — если блокируются зашифрованные вложения и сюда попадают документы банков), View images = Load external images on demand, Allow HREFs = No.

**Mail Proxy → DKIM.** Один ключ и один селектор на все домены; на второй узел кластера синхронизируется с master. Порядок важен: сначала DNS, потом включение подписи — иначе получатели не найдут ключ, а при строгом DMARC почту начнут отклонять.

| Параметр | Значение | Почему |
| --- | --- | --- |
| Selector | `pmg2026` | год в имени — для замены ключа: следующий `pmg2027`, старый и новый временно публикуются вместе |
| Key Size | 2048 | 1024 слабый; 4096 и 8192 дают TXT в несколько КБ, часть DNS-панелей их не примет |
| Signing Domain Source | Header | подпись по домену из `From:` — его проверяет DMARC |
| Sign all Outgoing Mail | No | подписываются только домены из Sign Domains; иначе PMG подпишет и чужие домены (пересылку), для которых ключа в DNS нет |
| Sign Domains | example.com, example.net, example.org, shop.example, news.example, intl.example | все домены, от имени которых отправляет Zimbra |
| Enable DKIM Signing | Yes — последним шагом | после публикации и проверки DNS |

1. Проверить текущее состояние: `dig +short TXT _dmarc.<домен>`, `dig +short TXT <домен> | grep spf`; в письме, отправленном наружу, — заголовок `DKIM-Signature:` (`d=` домен, `s=` селектор). Если старый Zimbra подписывает сам, после переезда подписывает только PMG, DKIM на новом Zimbra не включается.
2. Создать селектор (`pmg2026`, 2048), Overwrite existing file = No.
3. View DNS Record → опубликовать TXT `pmg2026._domainkey.<домен>` для каждого домена (ключ у всех один). Разбиение длинной записи на части по 255 символов — норма.
4. Проверить снаружи для каждого домена: `dig +short TXT pmg2026._domainkey.<домен>` → `v=DKIM1; k=rsa; p=…` с ключом из PMG.
5. Добавить Sign Domains, затем Enable DKIM Signing = Yes, Signing Domain Source = Header, Sign all Outgoing Mail = No.
6. Письмо из Zimbra на ящик Gmail → «Показать оригинал»: `DKIM: PASS` с вашим доменом; повторить для одного-двух второстепенных доменов.

SPF всех доменов должен включать внешние адреса PMG: после переезда почта уходит в интернет с них.

| Проверка | Команда | Ожидаемый результат |
| --- | --- | --- |
| Ключ в DNS | `dig +short TXT pmg2026._domainkey.example.com` | `v=DKIM1; k=rsa; p=…`, совпадает с View DNS Record |
| Подпись | письмо на Gmail → «Показать оригинал» | `DKIM: PASS`, `d=<ваш домен>` |
| SPF и DMARC | там же | `SPF: PASS`, `DMARC: PASS` |

```bash
# на PMG: DNSBL отвечают (Spamhaus не работает через публичные резолверы, Barracuda — без регистрации DNS-сервера)
dig +short 2.0.0.127.zen.spamhaus.org          # 127.0.0.2, .4, .10; 127.255.255.25x — запросы блокируются
dig +short 2.0.0.127.b.barracudacentral.org    # 127.0.0.2
dig +short 2.0.0.127.bl.spamcop.net            # 127.0.0.2
# кого отвергла проверка обратной DNS-записи
grep 'cannot find your hostname\|cannot find your reverse hostname' /var/log/mail.log \
  | grep -o 'from [^ ]*\[[0-9.]*\]' | sort | uniq -c | sort -rn | head
```

**Certificates (ACME).** Let's Encrypt, проверка HTTP-01 встроенным плагином standalone: PMG на время проверки сам поднимает веб-сервер на порту 80. Аккаунт ACME общий для кластера, домены и сертификаты — у каждого узла свои: `mx1.example.com` на mx1, `mx2.example.com` на mx2. У каждого узла два сертификата, домен добавляется для обоих назначений:

| Назначение | Файл | Для чего |
| --- | --- | --- |
| SMTP | `/etc/pmg/pmg-tls.pem` | STARTTLS на порту 25; после выпуска PMG сам перезагружает Postfix |
| API | `/etc/pmg/pmg-api.pem` | веб-интерфейс и API (8006), в том числе ссылки из отчётов карантина |

Выпущены 14.11.2026: SMTP и API на обоих узлах. Продление — `pmg-daily.service`, когда до окончания срока меньше 30 дней. Поэтому проброс TCP 80 на каждый узел и записи `mx1`/`mx2` в публичном DNS должны оставаться постоянно: без них продление через 60 дней не пройдёт. Первый выпуск лучше делать на аккаунте Let's Encrypt Staging — у боевого лимиты на число выпусков.

**Сертификат API и кластер.** Узлы кластера доверяют друг другу по отпечатку сертификата API, записанному в `/etc/pmg/cluster.conf`. Встроенный ACME при выпуске и продлении обновляет отпечатки сам; ручная замена `pmg-api.pem` без `pmgcm update-fingerprints` останавливает синхронизацию. Выпускать по одному узлу (сначала node, потом master), после каждого — проверка:

```bash
pmgcm status                                                        # оба узла active
openssl x509 -in /etc/pmg/pmg-api.pem -noout -fingerprint -sha256   # отпечаток локального сертификата
grep -E 'name|fingerprint' /etc/pmg/cluster.conf                    # совпадает с записью узла на обоих узлах
journalctl -u pmgmirror --since '-15 min' | tail -20                # синхронизация без ошибок
# не совпадает — на узле с новым сертификатом: pmgcm update-fingerprints
```

**MX и общее имя `mxs`.** Сейчас MX: `mxs` (0, два A-адреса — оба узла), `mx1` (5), `mx2` (10). Сертификат на `mxs` через HTTP-01 надёжно не выпустить (Let's Encrypt попадает то на один узел, то на другой), DNS-01 невозможен — у nic.md нет API. Предложено при переключении DNS заменить на `MX 10 mx1` и `MX 10 mx2`: то же распределение нагрузки, и имя в MX совпадает с сертификатом узла (решение не принято).

```bash
# на каждом узле: издатель и срок обоих сертификатов
for p in 25 8006; do
  s=$([ $p = 25 ] && echo '-starttls smtp')
  echo "== $p"; openssl s_client $s -connect $(hostname -f):$p -servername $(hostname -f) </dev/null 2>/dev/null \
    | openssl x509 -noout -subject -issuer -enddate
done
systemctl list-timers pmg-daily.timer
```

| Проверка | Ожидаемый результат |
| --- | --- |
| порт 25 и 8006 | `issuer` — Let's Encrypt (`R10`–`R14` или `E5`–`E9`), `subject` — имя узла, срок около 90 дней |
| `pmg-daily.timer` | следующий запуск в ближайшие сутки |
| снаружи: `swaks --server mx1.example.com --tls --quit-after EHLO` | `TLS started` без ошибок сертификата |

**Обновление PMG.** Настройки из веб-интерфейса хранятся в `/etc/pmg/` и базе PMG; конфиги Postfix, ClamAV, SpamAssassin PMG генерирует из шаблонов, поэтому dpkg при обновлении считает их изменёнными. На вопрос dpkg о таких файлах — **N** (оставить текущий; проверить клавишей D: в разнице видны ваши домены и адреса), после обновления — `pmgconfig sync --restart 1`. Для файлов, к которым PMG отношения не имеет (`sshd_config`, `grub`): не правили — **Y**, правили — **N**. Свои изменения конфигов — только копией шаблона в `/etc/pmg/templates/` ([документация PMG](https://pmg.proxmox.com/pmg-docs/pmgconfig.1.html)); обновление не сообщает об изменениях базовых шаблонов, поэтому:

```bash
cp -a /var/lib/pmg/templates /root/pmg-templates-$(date +%F)     # перед обновлением
apt update && apt dist-upgrade
for f in /etc/pmg/templates/*; do                                 # после: что поменялось в базовых шаблонах
  diff -u /root/pmg-templates-$(date +%F)/$(basename "$f") /var/lib/pmg/templates/$(basename "$f")
done
pmgconfig sync --restart 1
```

Узлы обновляются по одному, с проверкой очереди между ними. Переход между старшими версиями — по отдельной инструкции Proxmox для этого перехода.

## 4. SSL-сертификаты (Let's Encrypt, автопродление)

Один SAN-сертификат на все имена. Zimbra отдаёт его на всех портах (443, 465, 587, 993, 995), а клиент находит в списке имён то, к которому подключался, поэтому предупреждений нет. Отдельные сертификаты на каждый домен покрыли бы только HTTPS: SMTP и IMAP всё равно отдают один сертификат.

| Имя в сертификате | Зачем |
| --- | --- |
| mail-new.example.com | имя сервера, внутренние соединения Zimbra |
| mail.example.com | клиенты example.com |
| mail.example.net | клиенты example.net |
| mail.example.org | клиенты example.org |
| mail.shop.example | клиенты shop.example |

Для проверки HTTP-01 каждое имя должно иметь публичную A-запись на внешний IP Zimbra, порт 80 открыт снаружи. Если у доменов есть CAA-записи, в них нужно разрешение letsencrypt.org.

### Шаг 1. Виртуальные имена доменов

```bash
sudo su - zimbra
for d in example.com example.net example.org shop.example; do
  zmprov cd $d 2>/dev/null || true
  zmprov md $d zimbraVirtualHostname mail.$d \
               zimbraPublicServiceHostname mail.$d \
               zimbraPublicServiceProtocol https \
               zimbraPublicServicePort 443
done
```

Бонус: на https://mail.example.org пользователь входит просто по логину, без @example.org.

### Шаг 2. Certbot и certbot-zimbra

```bash
sudo apt -y install certbot
cd /tmp
# последний релиз: https://github.com/YetOpen/certbot-zimbra/releases
tar xzf certbot-zimbra-*.tar.gz && cd certbot-zimbra-*/
sudo ./install all

sudo certbot_zimbra.sh --new --prompt-confirm \
  -H mail-new.example.com \
  -e mail.example.com -e mail.example.net -e mail.example.org -e mail.shop.example
```

Скрипт патчит nginx Zimbra под проверку ACME, получает сертификат, разворачивает его через zmcertmgr и прописывает себя хуками продления. Новый домен: добавить ещё один -e и перевыпустить с --force-renewal.

### Шаг 3. Автопродление ночью

Хук продления перезапускает Zimbra, поэтому таймер certbot переносим на ночь:

```bash
sudo systemctl edit certbot.timer
```

```ini
[Timer]
OnCalendar=
OnCalendar=*-*-* 03:30:00
RandomizedDelaySec=15m
```

```bash
sudo systemctl daemon-reload && sudo systemctl restart certbot.timer
sudo certbot renew --dry-run
```

### Шаг 4. Проверка

```bash
sudo su - zimbra -c "zmcertmgr viewdeployedcrt" | grep -E 'notAfter|SubjectAltName' -A1
openssl s_client -connect mail.example.org:587 -starttls smtp </dev/null 2>/dev/null | openssl x509 -noout -ext subjectAltName -enddate
openssl s_client -connect mail.example.net:993 </dev/null 2>/dev/null | openssl x509 -noout -ext subjectAltName -enddate
```

PMG: сертификаты для SMTP и веб-интерфейса выпускает встроенный ACME на каждом узле (раздел 3, Certificates). Срок действия всех сертификатов стоит проверять из n8n с алертом за 14 дней.

### Wildcard

Let's Encrypt выдаёт wildcard (\*.example.org), но только через проверку DNS-01, то есть нужен API у DNS-провайдера. Для почты он не нужен: явный список имён mail.<домен> проще и работает через HTTP-01.

