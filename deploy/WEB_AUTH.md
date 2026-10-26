# Веб-доступ к консоли RTS Tender

## Файл пользователей

```bash
sudo nano /etc/rts-tender/web_users.txt
```

Формат (одна учётная запись на строку). **Логин = профиль РТС** (отдельный cookie и черновики ПП):

```text
IPSokolova:123456
Nacpro:123456
```

На production **смените пароль** `123456` после первого входа.

```bash
sudo chown root:mitrist12 /etc/rts-tender/web_users.txt
sudo chmod 640 /etc/rts-tender/web_users.txt
```

## Секреты ЕИС / Telegram / MAX (`eis.env`)

Файл должен читаться пользователем сервиса (`mitrist12`):

```bash
sudo chown root:mitrist12 /etc/rts-tender/eis.env
sudo chmod 640 /etc/rts-tender/eis.env
```

Либо продублируйте `telegram_bot`, `MAX_BOT_TOKEN` и т.д. в `service.env` и добавьте в unit:

```ini
EnvironmentFile=-/etc/rts-tender/eis.env
```

## Переменные в `service.env` (или unit systemd)

```bash
RTS_WEB_USERS_FILE=/etc/rts-tender/web_users.txt
RTS_WEB_SESSION_SECRET=<случайная_длинная_строка_32+_символов>
RTS_WEB_HTTPS_ONLY=1
RTS_WEB_PUBLIC_BASE_URL=https://rts.raytec-log.pro
EIS_HISTORY_DB=/opt/rts_tender/data/eis_history.sqlite3
EIS_ANALYTICS_DB=/opt/rts_tender/data/eis_analytics.sqlite3
```

`EIS_HISTORY_DB` — SQLite с XML-backfill для раздела «Аналитика КТРУ». Операционная БД (RSS, HTML, «Итоги дня») — `EIS_ANALYTICS_DB`.

`RTS_WEB_PUBLIC_BASE_URL` — базовый URL для ссылок в Telegram/MAX на модалку «Сравнение характеристик» в «Итоги дня» (`/day-summary?open_compare=1&...`). На VM: `https://rts.raytec-log.pro`.

Пока файл отсутствует или пуст **и** не заданы `RTS_WEB_USER` / `RTS_WEB_PASSWORD` — сайт открыт без входа (удобно для локальной разработки).

Запасной вариант до создания файла: одна учётка через `RTS_WEB_USER` и `RTS_WEB_PASSWORD`.

## Перезапуск

```bash
cd /opt/rts_tender/app && git pull
# Справочник предложений на проде: RTS_PREOFFERS_FILE=/var/lib/rts_tender/rts_preoffers.json
# (не файл из git). После обновления ктру.csv в репозитории:
/opt/rts_tender/venv/bin/python tools/import_ktru_csv.py --catalog /var/lib/rts_tender/rts_preoffers.json
sudo systemctl restart rts-tender
sudo systemctl is-active rts-tender
```

Для кнопки «Участвовать» (создание черновика ПП в РТС) задайте в `service.env` или `eis.env`:

```bash
RTS_COOKIE_INGEST_KEY=<длинная_случайная_строка>
```

Оператор обновляет cookie через расширение `tools/rts-cookie-bridge` → `POST /api/rts/cookie/save` с полем `"profile": "IPSokolova"` или `"Nacpro"`.

Cookie хранятся в `$RTS_META_DIR/cookies/<логин>.txt` (на проде: `/var/lib/rts_tender/cookies/`).

## Авто-участие (ПП по извещению ЕИС через инструмент РТС)

Глобальный выключатель в `service.env`:

```bash
RTS_AUTO_PARTICIPATE=1
```

По умолчанию выключено — включайте только после настройки правил на VM.

Создание идёт через API РТС: `creation-data` → `temporary/create` (характеристики из извещения на площадке, не из HTML ЕИС). Страна РФ + «нет в реестре» проставляются автоматически. MVP: одна позиция извещения (`starNotificationNumber=l{номер}-1`).

Кто может редактировать правила (страница **«Авто-участие»** в sidebar):

```bash
RTS_SETTINGS_ADMIN_USERS=admin,IPSokolova
```

Список логинов через запятую. `IPSokolova` входит в список админов (настройки + cookie поставщика). `Nacpro` по умолчанию только оператор.

Настройки хранятся в `$RTS_META_DIR/auto_participate.json` (отдельные правила на профиль: сумма, КТРУ catalog/whitelist, лимит в час, **автопубликация**).

После RSS-опроса и загрузки HTML карточки система может автоматически создать Temporary ПП. При включённой автопубликации профиля + `RTS_PP_PUBLISH_ENABLED=1` ставится задание `awaiting_signature`. Оператор держит открытой вкладку сервиса (живая сессия РТС + КриптоПро): при первом подписании выбирается сертификат (thumbprint в `localStorage`), дальше `watchPending` подписывает без `confirm` — TSA и publish.

## Авточерновик из монитора ПП

Отдельный источник (ПП конкурента), не смешивать с авто-участием по ЕИС. Публикация и ЭП — тот же контур, что у notice.

```bash
RTS_AUTO_MONITOR_DRAFT=1
RTS_PP_PUBLISH_ENABLED=1
```

Чеклист:

1. Env: `RTS_AUTO_MONITOR_DRAFT=1`, `RTS_PP_PUBLISH_ENABLED=1`
2. Админка → «Авточерновик из монитора ПП»: вкл, whitelist КТРУ, мин. сумма, **автопубликация**, профиль cookie поставщика
3. Монитор ПП: backfill done, customer cookie
4. Открытая вкладка сервиса на ПК с ЭЦП (не hidden); первый раз — выбор сертификата, дальше тихая автоподпись

Файл настроек: `$RTS_META_DIR/auto_monitor_draft.json`.

При опросе монитора подходящие ПП ставятся в очередь; после poll worker создаёт черновики FIFO. При автопубликации — prepare → `awaiting_signature` → CryptoPro (silent при сохранённом thumbprint).

Вход: `https://rts.raytec-log.pro/login` (логины `IPSokolova`, `Nacpro`).
