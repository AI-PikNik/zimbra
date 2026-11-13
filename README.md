# Zimbra 10.1 FOSS: установка, защита и миграция

Материалы по развёртыванию почтового сервера Zimbra 10.1 FOSS (сборка [Maldua](https://github.com/maldua/zimbra-foss)) на Ubuntu 24.04 за двумя Proxmox Mail Gateway и переносу на него данных со старого сервера Zimbra.

| Файл | Содержание |
| --- | --- |
| [docs/01-server-setup.md](docs/01-server-setup.md) | подготовка ОС, установка Zimbra, прокси, ufw, fail2ban с AbuseIPDB, связка с PMG, Let's Encrypt |
| [docs/02-migration.md](docs/02-migration.md) | миграция со старого сервера скриптом `zm-ldap-migrate.py` |
| [scripts/zm-ldap-migrate.py](scripts/zm-ldap-migrate.py) | перенос LDAP (учётки с хэшами паролей, домены, COS, списки, алиасы, права) и содержимого ящиков (почта, контакты, календари, задачи, портфель), сверка, досинхронизация, переиндексация |
| [scripts/migrate.conf.example](scripts/migrate.conf.example) | образец файла настроек |
| [configs/fail2ban/](configs/fail2ban/) | фильтры, jail-ы и действие AbuseIPDB из раздела 2 |

## Быстрый старт миграции

```bash
# на новом сервере, от zimbra
cd /opt/zimbra/migration
python3 zm-ldap-migrate.py                       # что нужно подготовить
cp migrate.conf.example migrate.conf && chmod 600 migrate.conf && nano migrate.conf
python3 zm-ldap-migrate.py -c migrate.conf       # справка по ключам
python3 zm-ldap-migrate.py -c migrate.conf --dry-run
python3 zm-ldap-migrate.py -c migrate.conf --ldap
python3 zm-ldap-migrate.py -c migrate.conf --data
python3 zm-ldap-migrate.py -c migrate.conf --verify
```

Подробно — в [docs/02-migration.md](docs/02-migration.md).

## Примечания

- Адреса, имена серверов и домены в материалах — примеры (`example.com`, `10.0.x.x`); подставьте свои.
- Пароли, ключи API и заполненный `migrate.conf` в репозиторий не добавлять (см. `.gitignore`).
- Проверено на Zimbra FOSS 10.0.8 → 10.1.20.p1. На других версиях сначала проверьте на одной учётке (`--only`).
