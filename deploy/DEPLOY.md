# Деплой rts_local (Yandex VM + GitHub)

Локальная/отдельная копия консоли мониторинга ЕИС и ПП РТС (prod-референс: https://rts.raytec-log.pro/).

## Архитектура на VM (совместно с prod)

Хост `93.77.182.91` уже крутит **prod** `rts-tender` → https://rts.raytec-log.pro/ (порт **8765**, `/etc/rts-tender`).
`rts_local` ставится **рядом**, не перезаписывая prod:

```
GitHub / rsync
        ▼
 /opt/rts_local/app          ← код
 /opt/rts_local/venv
 /var/lib/rts_local          ← своя SQLite / cookies / preoffers
 /etc/rts-local/             ← service.env, eis.env, web_users.txt
 nginx → 127.0.0.1:8766
 systemd: rts-local
 HTTPS: https://rts-local.raytec-log.pro/
```

Одноразовая подготовка: `sudo bash deploy/prepare_cohosted_vm.sh`

## Справочник шаблонов ПП (обязательно для «Участвовать» / «Создать черновик»)

Черновик ПП создаётся только если КТРУ есть в `$RTS_META_DIR/ktru_rts_cache`
(на rts-local: `/var/lib/rts_local/ktru_rts_cache`). Иначе UI пишет
«КТРУ нет в справочнике шаблонов ПП (draft_ktru_dict)».

На co-hosted VM скопируйте кэш с prod:

```bash
sudo bash /opt/rts_local/app/deploy/sync_ktru_cache_from_prod.sh
```

Или импортируйте dump `KTRU/draft_ktru_dict.txt` (файл ~30 MB, в git не хранится):

```bash
# положите draft_ktru_dict.txt в /opt/rts_local/app/KTRU/
cd /opt/rts_local/app
/opt/rts_local/venv/bin/python tools/import_draft_ktru_dict.py \
  --cache-dir /var/lib/rts_local/ktru_rts_cache
```

Перезапуск сервиса не нужен — шаблоны читаются с диска.

## 0. Перед первым деплоем — целостность кода

В `rts_local` сейчас **не хватает модулей и шаблонов**, без которых `python -m web_ui` падает на import.
Рядом лежит полный проект `rts_tender` — синхронизируйте недостающее:

```powershell
cd E:\D\01_Projects\rts_local
powershell -ExecutionPolicy Bypass -File tools\sync_missing_from_rts_tender.ps1
```

Скрипт копирует только отсутствующие `.py` и HTML-шаблоны. После этого проверьте импорт (на машине с Python):

```bash
python -c "import web_ui.main"
```

## 1. Локальный запуск (Windows / Linux, без VM)

```bash
cd /path/to/rts_local
python -m venv .venv
# Windows: .venv\Scripts\activate
source .venv/bin/activate
pip install -r requirements.txt
cp web_users.example.txt web_users.txt
# опционально: env.txt с токеном ЕРУЗ, cookies через Cookie Bridge
python -m web_ui
# → http://127.0.0.1:8765
```

Без `web_users.txt` и без `RTS_WEB_USER` сайт открыт без логина (удобно для dev).

## 2. Yandex Cloud VM — первый bootstrap

На VM (Ubuntu 22.04/24.04), с доступом к GitHub по SSH deploy key или HTTPS:

```bash
# 1) Deploy key: ssh-keygen -t ed25519 -f ~/.ssh/rts_local_deploy -N ""
#    Public key → GitHub → Settings → Deploy keys (read-only)
#    Private key остаётся на VM

sudo apt-get update && sudo apt-get install -y git
sudo mkdir -p /opt/rts_local
sudo chown "$USER:$USER" /opt/rts_local

# Если репозиторий ещё пустой — сначала push с рабочей станции (см. §4)
git clone git@github.com:mitrist/rts_local.git /opt/rts_local/app

cd /opt/rts_local/app
sudo APP_USER="$USER" REPO_URL=git@github.com:mitrist/rts_local.git \
  bash deploy/bootstrap_yandex_vm.sh
```

Скрипт создаёт venv, `/etc/rts-tender/*`, systemd `rts-local`, nginx proxy.

Затем:

```bash
sudo nano /etc/rts-tender/service.env   # SESSION_SECRET, PUBLIC_BASE_URL, COOKIE_INGEST_KEY
sudo nano /etc/rts-tender/eis.env       # токен ЕРУЗ, Telegram
sudo nano /etc/rts-tender/web_users.txt # смените 123456
sudo systemctl restart rts-local
curl -s http://127.0.0.1:8765/health/live
```

Security group Yandex: открыть TCP 22, 80 (и 443 после certbot).

## 3. Timers (рекомендуется)

```bash
# Опрос реестра ПП
sudo cp deploy/rts-pp-monitor-poll.service.example /etc/systemd/system/rts-pp-monitor-poll.service
sudo cp deploy/rts-pp-monitor-poll.timer.example   /etc/systemd/system/rts-pp-monitor-poll.timer
# правьте User=/WorkingDirectory=/ExecStart= под /opt/rts_local и пользователя VM

# Watchdog зависаний
sudo mkdir -p /run/rts-tender && sudo chown "$USER:$USER" /run/rts-tender
sudo cp deploy/rts-tender-watchdog.service.example /etc/systemd/system/rts-local-watchdog.service
sudo cp deploy/rts-tender-watchdog.timer.example   /etc/systemd/system/rts-local-watchdog.timer
# в unit: ExecStart=.../tools/rts_tender_watchdog.py , RTS_WATCHDOG_SERVICE=rts-local

sudo systemctl daemon-reload
sudo systemctl enable --now rts-pp-monitor-poll.timer
sudo systemctl enable --now rts-local-watchdog.timer
```

Подробности: [HEALTH.md](HEALTH.md), [WEB_AUTH.md](WEB_AUTH.md).

## 4. Первый push в GitHub

Репозиторий https://github.com/mitrist/rts_local сейчас пустой. С рабочей станции:

```bash
cd E:/D/01_Projects/rts_local
git init
git remote add origin git@github.com:mitrist/rts_local.git
git add -A
git status   # убедитесь: нет cookies_rts.txt, env.txt, *.sqlite3, .cursor/
git commit -m "Initial rts_local: console + deploy for Yandex VM"
git branch -M main
git push -u origin main
```

## 5. GitHub Actions → автодеплой

Workflow: [`.github/workflows/deploy.yml`](../.github/workflows/deploy.yml).

В GitHub → Settings → Secrets and variables → Actions:

| Secret     | Значение                          |
|------------|-----------------------------------|
| `VM_HOST`  | публичный IP VM                   |
| `VM_USER`  | SSH-пользователь                  |
| `VM_SSH_KEY` | private key с правом `systemctl restart` (sudo NOPASSWD для сервиса) |

На VM для Actions-пользователя:

```bash
# /etc/sudoers.d/rts-local-deploy
ubuntu ALL=(root) NOPASSWD: /bin/systemctl restart rts-local, /bin/systemctl is-active rts-local
```

На VM deploy key / обычный ключ должен уметь `git fetch` в `/opt/rts_local/app`.

После push в `main` Actions сделает `git reset --hard origin/main` + restart.

## 6. Cookie Bridge

Расширение: `tools/rts-cookie-bridge/`. В `manifest.json` / popup укажите URL консоли (`http://VM_IP` или домен). Ключ = `RTS_COOKIE_INGEST_KEY` из `service.env`.

## 7. Переменные (кратко)

| Переменная | Назначение |
|------------|------------|
| `EIS_ANALYTICS_DB` | операционная SQLite (RSS, HTML, монитор ПП) |
| `EIS_HISTORY_DB` | аналитика КТРУ (XML backfill) |
| `RTS_META_DIR` | cookies, JSON-настройки, lock-файлы |
| `RTS_PP_MONITOR*` | опрос реестра ПП конкурентов |
| `RTS_COOKIE_INGEST_KEY` | API ключ Cookie Bridge |
| `RTS_WEB_USERS_FILE` | логин:пароль (логин = профиль РТС) |

Примеры: [service.env.example](service.env.example), [eis.env.example](eis.env.example).
