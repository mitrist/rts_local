# Деплой rts_local (отдельная Ubuntu VM + GitHub)

Консоль мониторинга ЕИС и ПП РТС на своей машине. Старый хост `93.77.182.91` и сценарий «рядом с prod» не используются. `deploy/prepare_cohosted_vm.sh` и `deploy/sync_ktru_cache_from_prod.sh` не запускать.

## Раскладка

```
GitHub main
        ▼
 /opt/rts_local/app                 ← код (git reset --hard только здесь)
 /opt/rts_local/venv
 /opt/rts_local/data                ← eis_history.sqlite3 (вне git)
 /var/lib/rts_local                 ← eis_analytics.sqlite3, ktru_rts_cache, cookies
 /etc/rts-local/                    ← service.env, eis.env, web_users.txt
 nginx :80 → 127.0.0.1:8765
 systemd: rts-local
```

Обновление кода: push в `main` → GitHub Actions ([`.github/workflows/deploy.yml`](../.github/workflows/deploy.yml)). Базы, кэш и `/etc/rts-local` в `git reset` не входят.

## Файлы, которые git не везёт

Их копируют с рабочей станции после bootstrap:

| Откуда | Куда |
|--------|------|
| `SQL/eis_history.sqlite3` | `/opt/rts_local/data/eis_history.sqlite3` |
| `data/local/eis_analytics.sqlite3` | `/var/lib/rts_local/eis_analytics.sqlite3` |
| `data/local/meta/ktru_rts_cache/` | `/var/lib/rts_local/ktru_rts_cache/` |
| `KTRU/draft_ktru_dict.txt` | импорт в тот же кэш (см. ниже) |

Вручную, их нет в репозитории и на диске разработчика может не быть:

- токен ЕРУЗ в `/etc/rts-local/eis.env`
- пароли в `/etc/rts-local/web_users.txt` (в примере `123456`, сменить)
- cookie РТС через Cookie Bridge на URL новой консоли

## Справочник шаблонов ПП

Черновик ПП создаётся только если КТРУ есть в `/var/lib/rts_local/ktru_rts_cache`. Иначе UI пишет «КТРУ нет в справочнике шаблонов ПП (draft_ktru_dict)».

```bash
cd /opt/rts_local/app
sudo -u ubuntu /opt/rts_local/venv/bin/python tools/import_draft_ktru_dict.py \
  --source /opt/rts_local/data/draft_ktru_dict.txt \
  --cache-dir /var/lib/rts_local/ktru_rts_cache
```

Перезапуск сервиса не нужен — шаблоны читаются с диска. Импорт перезаписывает файл кэша, если тот же код уже есть. Сначала копируют пять локальных шаблонов, затем импортируют справочник: официальный шаблон заменяет локальный.

## Первый bootstrap

На Ubuntu открыты TCP 22 и 80. Deploy key GitHub — только чтение.

```bash
sudo APP_USER=ubuntu REPO_URL=git@github.com:mitrist/rts_local.git \
  bash deploy/bootstrap_yandex_vm.sh
```

Скрипт ставит git, nginx, python3-venv, создаёт `/opt/rts_local/app`, venv, systemd `rts-local` и nginx на 80 → `127.0.0.1:8765`. Конфиги копируются из примеров в `/etc/rts-local`. Повторный запуск существующие `service.env`, `eis.env` и `web_users.txt` не затирает. `RTS_WEB_PORT` при первом копировании принудительно равен `WEB_PORT` (8765).

Затем:

```bash
sudo nano /etc/rts-local/service.env   # SESSION_SECRET, PUBLIC_BASE_URL, COOKIE_INGEST_KEY
sudo nano /etc/rts-local/eis.env       # токен ЕРУЗ
sudo nano /etc/rts-local/web_users.txt # смените 123456
sudo systemctl restart rts-local
curl -s http://127.0.0.1:8765/health/live
```

Пока нет сертификата, в `service.env` стоит `RTS_WEB_HTTPS_ONLY=0`. После `certbot --nginx` поставьте `1` и https-URL.

Проверка ядра: страница «Ядро КТРУ» видит историю, кнопка «Обновить ядро из ЕИС» активна.

## GitHub Actions

Secrets: `VM_HOST` (публичный IP), `VM_USER`, `VM_SSH_KEY` (ключ, которым Actions ходит на VM). Это не deploy key репозитория.

На VM:

```bash
# /etc/sudoers.d/rts-local-deploy
ubuntu ALL=(root) NOPASSWD: /bin/systemctl restart rts-local, /bin/systemctl is-active rts-local
```

Пользователь сервиса должен уметь `git fetch` в `/opt/rts_local/app` (read-only deploy key).

## Cookie Bridge

Расширение: `tools/rts-cookie-bridge/`. URL консоли — `http://IP` или домен. Ключ = `RTS_COOKIE_INGEST_KEY` из `service.env`.

## Timers

Их можно включить после появления cookie и токена. В unit-файлах замените пользователя и `/etc/rts-local`.

```bash
sudo cp deploy/rts-pp-monitor-poll.service.example /etc/systemd/system/rts-pp-monitor-poll.service
sudo cp deploy/rts-pp-monitor-poll.timer.example   /etc/systemd/system/rts-pp-monitor-poll.timer
sudo cp deploy/rts-tender-watchdog.service.example /etc/systemd/system/rts-local-watchdog.service
sudo cp deploy/rts-tender-watchdog.timer.example   /etc/systemd/system/rts-local-watchdog.timer
sudo systemctl daemon-reload
```

Подробности: [HEALTH.md](HEALTH.md), [WEB_AUTH.md](WEB_AUTH.md).
