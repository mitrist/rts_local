# rts_local

Сокращённая / отдельная копия консоли **RTS Tender** для локального и VM-деплоя.

Прод-референс: [https://rts.raytec-log.pro/](https://rts.raytec-log.pro/)  
Репозиторий: [https://github.com/mitrist/rts_local](https://github.com/mitrist/rts_local)

## Что делает сервис

1. **ЕИС** — мониторинг извещений (RSS + HTML карточки zakupki), сравнение характеристик с каталогом поставщика.
2. **РТС** — опрос приглашений / сессии ЛК, кэш КТРУ, справочник предварительных предложений.
3. **Монитор ПП** — слежение за опубликованными предложениями конкурентов на площадке.
4. **Черновики ПП** — создание своих предварительных предложений копированием (из извещения или образца монитора).
5. **Уведомления** — Telegram / MAX.

Веб-консоль: FastAPI + Jinja (`python -m web_ui`, порт `8765`).

## Две базы SQLite

| БД | Назначение |
|----|------------|
| `eis_history.sqlite3` | Аналитика КТРУ (XML-backfill) |
| `eis_analytics.sqlite3` | Операции: RSS, HTML, монитор ПП, черновики, уведомления |

Переменные: `EIS_HISTORY_DB`, `EIS_ANALYTICS_DB`.

## Быстрый старт

### 1) Дополнить недостающий код из `rts_tender`

Сейчас в дереве не хватает части модулей/шаблонов (без них импорт `web_ui.main` падает). Синхронизация:

```powershell
powershell -ExecutionPolicy Bypass -File tools\sync_missing_from_rts_tender.ps1
```

### 2) Локально

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp web_users.example.txt web_users.txt
python -m web_ui
```

Откройте http://127.0.0.1:8765

### 3) Yandex VM через GitHub

Полная инструкция: **[deploy/DEPLOY.md](deploy/DEPLOY.md)**

Кратко:

1. Push в `git@github.com:mitrist/rts_local.git`
2. На VM: `bash deploy/bootstrap_yandex_vm.sh`
3. Secrets GitHub Actions: `VM_HOST`, `VM_USER`, `VM_SSH_KEY` → автодеплой на push в `main`

## Веб-доступ

Файл `web_users.txt` (`login:password`), страница `/login`.  
См. [deploy/WEB_AUTH.md](deploy/WEB_AUTH.md), [web_users.example.txt](web_users.example.txt).

## Cookie РТС

Расширение `tools/rts-cookie-bridge` → `POST /api/rts/cookie/save` с `RTS_COOKIE_INGEST_KEY`.

## Исторический backfill ЕИС

```bash
python -m eis_history_backfill
```

Требуется `env.txt` / `EIS_ENV_FILE` с токеном ЕРУЗ. Подробнее — в [deploy/DEPLOY.md](deploy/DEPLOY.md).
