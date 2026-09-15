# Здоровье rts-tender и watchdog

## Симптом

nginx **502 Bad Gateway**, при этом `systemctl status rts-tender` часто показывает **active (running)** — процесс жив, но **не отвечает** на `127.0.0.1:8765`.

## Вероятные причины (VPS)

1. **Блокировка event loop** — большинство страниц и API объявлены как синхронные `def` и ходят в SQLite (~3 ГБ `eis_analytics.sqlite3`). Долгий запрос блокирует все HTTP-ответы, включая `/login`.
2. **Фоновые supervisor-циклы** (ЕИС HTML auto-fetch каждые ~5 с, RSS, PP monitor) создают нагрузку на SQLite и потоки.
3. **Периодические ошибки** `sqlite3.OperationalError: unable to open database file` в логах — проверьте права `chown -R mitrist12:mitrist12 /var/lib/rts_tender`.
4. **Память** — процесс может занимать 2–3 ГБ RAM; при нехватке памяти ответы «замирают».

## Как узнать до 502

### 1. Endpoint `/health/live` (без авторизации)

```bash
curl -s http://127.0.0.1:8765/health/live | jq
```

Поля:

| Поле | Значение |
|------|----------|
| `ok` | процесс отвечает |
| `degraded` | `true`, если event loop «задержался» ≥ `RTS_HEALTH_MAX_LOOP_LAG_SEC` (по умолчанию 15 с) |
| `event_loop_lag_sec` | насколько asyncio-цикл отстаёт от тика 1 с |
| `uptime_sec` | время с запуска процесса |

HTTP **503** + `degraded: true` — зависание **ещё до полного отказа** curl.

### 2. `/health/ready`

Проверка SQLite (`SELECT 1`) с таймаутом 2 с.

### 3. Watchdog (systemd timer)

```bash
sudo cp deploy/rts-tender-watchdog.{service,timer}.example /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now rts-tender-watchdog.timer
```

Каждую минуту: 3 подряд неудачных probe → `systemctl restart rts-tender`.

Логи: `journalctl -t rts_tender_watchdog` или `grep watchdog /var/log/syslog`.

### 4. Ручной мониторинг

```bash
watch -n 10 'curl -sf -o /dev/null -w "%{http_code} lag=$(curl -s http://127.0.0.1:8765/health/live | jq -r .event_loop_lag_sec)" http://127.0.0.1:8765/health/live'
```

## Снижение нагрузки (VPS заказчика)

В `/etc/rts-tender/service.env`:

```bash
# Реже сканировать очередь HTML (по умолчанию 5 с — агрессивно)
EIS_HTML_AUTO_FETCH_SCAN_SEC=30
EIS_HTML_AUTO_FETCH_MAX_PER_POLL=5
```

## nginx

Убедитесь, что proxy timeout не маскирует проблему слишком долго; для диагностики достаточно `curl` напрямую на 8765.
