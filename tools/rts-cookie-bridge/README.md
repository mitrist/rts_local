# RTS Cookie Bridge

Минимальное расширение Chrome для отправки cookie `*.rts-tender.ru` на сервис `rts_tender`.

## Установка (режим разработчика)

1. Chrome → `chrome://extensions` → «Режим разработчика».
2. «Загрузить распакованное расширение» → каталог `tools/rts-cookie-bridge`.
3. На сервере задайте `RTS_COOKIE_INGEST_KEY` (длинная случайная строка) в окружении сервиса `rts-tender`.
4. В popup выберите **профиль**:
   - `IPSokolova` / `Nacpro` — ЛК поставщика (тот же логин, что в веб-консоли);
   - **Заказчик** (`customer_monitor`) — ЛК заказчика для монитора ПП конкурентов (`/rts-pp-monitor`).

## Использование

1. Войти на [offers.rts-tender.ru](https://offers.rts-tender.ru) по ЭП (поставщик или заказчик).
2. В popup: URL API (можно два — VM и VPS), профиль (`IPSokolova` / `Nacpro` / **Заказчик**), API key.
3. Оставить включённым **«Авто-обновление каждые 180 сек»** (по умолчанию включено).
4. Нажать **«Сохранить cookie»** — cookie соберутся из браузера автоматически, сервер проверит сессию (probe).
5. После успешного probe расширение **каждые 180 секунд** отправляет свежие cookie на сервер(ы) в фоне (popup можно закрыть). Работает, пока Chrome открыт и сессия РТС в браузере жива.

Авто-обновление можно выключить чекбоксом в popup. При ручной вставке cookie из DevTools авто-режим **не включается** (только авто-сбор из браузера).

Фоновые сохранения идут с `probe: false` (без лишней нагрузки на РТС); probe выполняется только при ручном «Сохранить cookie».

URL **1** (VM): `https://rts.raytec-log.pro/api/rts/cookie/save`  
URL **2** (VPS): `http://161.104.18.194/api/rts/cookie/save` (prod по IP, только http).

Можно указать один URL (только VM или только VPS) или оба — тогда cookie отправляются на оба сервиса с одним профилем и API key.
На VM **обязательно https** — иначе nginx редиректит http→https и POST превращается в GET (`Method Not Allowed`).

Ручная вставка из DevTools → Network — только если авто-сбор не нашёл `.Auth.Sso` (раскройте «Ручная вставка» в popup).

После обновления расширения перезагрузите его в `chrome://extensions` (кнопка «Обновить»).

## Новый VPS без домена

1. На VM должны быть заданы `RTS_COOKIE_INGEST_KEY` и `RTS_COOKIE_FILE` (см. `/etc/rts-tender/service.env`).
2. Запросы к `*.rts-tender.ru` с VM идут через прокси (на prod: `HTTPS_PROXY=http://93.77.182.91:8888`) — без него cookie probe и черновики ПП не работают.
3. В popup расширения укажите **HTTP**-URL API (не https), иначе Chrome заблокирует запрос с IP.
7. «Отправить cookie» — в ответе должно быть `probe OK (offers)`.

Авто-сбор из браузера обычно даёт мало cookie (9–10) и недостаточен для API справочника КТРУ.

Альтернатива: ручная вставка на странице веб-консоли «Cookie / сессия».

## Кэш шаблонов КТРУ (Ktru/search)

С сервера VM поиск `Ktru/search` часто возвращает пустой `page`. Для кнопки «Участвовать» нужен шаблон характеристик из ответа DevTools.

1. В браузере с живой сессией РТС: DevTools → Network → `Ktru/search` для нужного кода КТРУ.
2. Сохраните тело ответа JSON (как `ktrusearch.json`).
3. На VM импортируйте в кэш:

```bash
python -m tools.compare_ktru_notice_rts --import-rts-json /path/ktrusearch.json \
  --ktru-code 32.50.13.110-00005820 \
  --cache-dir /var/lib/rts_tender/ktru_rts_cache
```

Каталог кэша по умолчанию: `RTS_META_DIR/ktru_rts_cache` (на проде обычно `/var/lib/rts_tender/ktru_rts_cache`).
