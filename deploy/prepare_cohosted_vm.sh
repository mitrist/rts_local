#!/usr/bin/env bash
# Подготовка rts_local на VM рядом с prod rts-tender.
# Не трогает /etc/rts-tender, порт 8765, nginx rts_tender.
set -euo pipefail

APP_USER="${APP_USER:-mitrist12}"
APP_ROOT="${APP_ROOT:-/opt/rts_local}"
META_DIR="${META_DIR:-/var/lib/rts_local}"
ETC_DIR="${ETC_DIR:-/etc/rts-local}"
SERVICE_NAME="${SERVICE_NAME:-rts-local}"
WEB_PORT="${WEB_PORT:-8766}"
DOMAIN="${DOMAIN:-rts-local.raytec-log.pro}"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run as root (sudo)" >&2
  exit 1
fi

if [[ ! -d "$APP_ROOT/app" ]]; then
  echo "Missing $APP_ROOT/app — upload code first" >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3-venv nginx certbot python3-certbot-nginx curl ca-certificates

# Swap 2G (на VM нет swap, RAM ~4G, рядом prod)
if ! swapon --show | grep -q .; then
  if [[ ! -f /swapfile ]]; then
    fallocate -l 2G /swapfile 2>/dev/null || dd if=/dev/zero of=/swapfile bs=1M count=2048
    chmod 600 /swapfile
    mkswap /swapfile
  fi
  swapon /swapfile || true
  grep -q '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

mkdir -p "$APP_ROOT/data" "$META_DIR/cookies" "$ETC_DIR"
chown -R "$APP_USER:$APP_USER" "$APP_ROOT" "$META_DIR"

if [[ ! -d "$APP_ROOT/venv" ]]; then
  sudo -u "$APP_USER" python3 -m venv "$APP_ROOT/venv"
fi
"$APP_ROOT/venv/bin/pip" install -q --upgrade pip
"$APP_ROOT/venv/bin/pip" install -q -r "$APP_ROOT/app/requirements.txt"

gen_secret() {
  python3 -c 'import secrets; print(secrets.token_urlsafe(48))'
}

if [[ ! -f "$ETC_DIR/service.env" ]]; then
  SESSION_SECRET="$(gen_secret)"
  COOKIE_KEY="$(gen_secret)"
  cat >"$ETC_DIR/service.env" <<EOF
RTS_PROJECT_ROOT=${APP_ROOT}/app
RTS_META_DIR=${META_DIR}
RTS_STATE_FILE=${META_DIR}/rts_invites_state.json
EIS_ANALYTICS_DB=${META_DIR}/eis_analytics.sqlite3
EIS_HISTORY_DB=${APP_ROOT}/data/eis_history.sqlite3
RTS_PREOFFERS_FILE=${META_DIR}/rts_preoffers.json
EIS_ENV_FILE=${ETC_DIR}/eis.env
RTS_WEB_USERS_FILE=${ETC_DIR}/web_users.txt
RTS_WEB_HOST=127.0.0.1
RTS_WEB_PORT=${WEB_PORT}
RTS_WEB_SESSION_SECRET=${SESSION_SECRET}
RTS_WEB_HTTPS_ONLY=1
RTS_WEB_PUBLIC_BASE_URL=https://${DOMAIN}
RTS_SETTINGS_ADMIN_USERS=admin,IPSokolova
RTS_COOKIE_INGEST_KEY=${COOKIE_KEY}
RTS_PP_MONITOR=1
RTS_PP_MONITOR_CRON=1
RTS_PP_MONITOR_SUPERVISOR=0
RTS_PP_MONITOR_INTERVAL_SEC=420
RTS_PP_MONITOR_FULL_PAGES=0
RTS_AUTO_PARTICIPATE=0
RTS_AUTO_MONITOR_DRAFT=0
EIS_HTML_AUTO_FETCH=1
EIS_HTML_AUTO_FETCH_SCAN_SEC=60
EIS_HTML_AUTO_FETCH_MAX_PER_POLL=3
RTS_HEALTH_MAX_LOOP_LAG_SEC=15
RTS_WATCHDOG_URL=http://127.0.0.1:${WEB_PORT}/health/live
RTS_WATCHDOG_SERVICE=${SERVICE_NAME}
EOF
fi

if [[ ! -f "$ETC_DIR/eis.env" ]]; then
  cat >"$ETC_DIR/eis.env" <<'EOF'
# Заполните токен ЕРУЗ и Telegram при необходимости.
# eruz_token=
# telegram_bot=
# telegram_chat_id=
EOF
fi

if [[ ! -f "$ETC_DIR/web_users.txt" ]]; then
  if [[ -f "$APP_ROOT/app/web_users.example.txt" ]]; then
    cp "$APP_ROOT/app/web_users.example.txt" "$ETC_DIR/web_users.txt"
  else
    echo "admin:changeme" >"$ETC_DIR/web_users.txt"
  fi
fi

if [[ ! -f "$META_DIR/rts_preoffers.json" ]] && [[ -f "$APP_ROOT/app/rts_preoffers.json" ]]; then
  cp "$APP_ROOT/app/rts_preoffers.json" "$META_DIR/rts_preoffers.json"
  chown "$APP_USER:$APP_USER" "$META_DIR/rts_preoffers.json"
fi

chown root:"$APP_USER" "$ETC_DIR"
chmod 750 "$ETC_DIR"
chown root:"$APP_USER" "$ETC_DIR/service.env" "$ETC_DIR/eis.env" "$ETC_DIR/web_users.txt"
chmod 640 "$ETC_DIR/service.env" "$ETC_DIR/eis.env" "$ETC_DIR/web_users.txt"

cat >"/etc/systemd/system/${SERVICE_NAME}.service" <<EOF
[Unit]
Description=RTS Local web console (${DOMAIN})
After=network.target

[Service]
Type=simple
User=${APP_USER}
Group=${APP_USER}
WorkingDirectory=${APP_ROOT}/app
EnvironmentFile=${ETC_DIR}/service.env
EnvironmentFile=-${ETC_DIR}/eis.env
ExecStart=${APP_ROOT}/venv/bin/python -m web_ui
Restart=on-failure
RestartSec=5
TimeoutStopSec=15
KillSignal=SIGKILL

[Install]
WantedBy=multi-user.target
EOF

# PP monitor oneshot (лёгкий, не supervisor)
cat >"/etc/systemd/system/rts-local-pp-monitor-poll.service" <<EOF
[Unit]
Description=RTS Local PP monitor registry poll
After=network.target

[Service]
Type=oneshot
User=${APP_USER}
Group=${APP_USER}
WorkingDirectory=${APP_ROOT}/app
EnvironmentFile=${ETC_DIR}/service.env
EnvironmentFile=-${ETC_DIR}/eis.env
ExecStart=${APP_ROOT}/venv/bin/python -m rts_pp_monitor_cron
Nice=10
EOF

cat >"/etc/systemd/system/rts-local-pp-monitor-poll.timer" <<'EOF'
[Unit]
Description=Timer for RTS Local PP monitor poll

[Timer]
OnBootSec=3min
OnUnitActiveSec=7min
AccuracySec=30s
Persistent=true
Unit=rts-local-pp-monitor-poll.service

[Install]
WantedBy=timers.target
EOF

# nginx HTTP (certbot допишет SSL)
SITE="/etc/nginx/sites-available/${SERVICE_NAME}"
cat >"$SITE" <<EOF
server {
    listen 80;
    listen [::]:80;
    server_name ${DOMAIN};

    location / {
        proxy_pass http://127.0.0.1:${WEB_PORT};
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 300;
    }
}
EOF
ln -sf "$SITE" "/etc/nginx/sites-enabled/${SERVICE_NAME}"
nginx -t
systemctl reload nginx

systemctl daemon-reload
systemctl enable --now "$SERVICE_NAME"
systemctl enable --now rts-local-pp-monitor-poll.timer

# TLS если DNS уже на эту VM
if ! [[ -d "/etc/letsencrypt/live/${DOMAIN}" ]]; then
  certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos --register-unsafely-without-email --redirect || {
    echo "WARN: certbot failed — HTTP на :80 доступен, повторите: sudo certbot --nginx -d ${DOMAIN}"
  }
fi

echo "=== done ==="
systemctl is-active "$SERVICE_NAME"
curl -sf "http://127.0.0.1:${WEB_PORT}/health/live" || curl -s "http://127.0.0.1:${WEB_PORT}/health/live" || true
echo
echo "Public: https://${DOMAIN}/"
echo "Configs: ${ETC_DIR}/ (не трогает /etc/rts-tender)"
echo "Смените пароли в ${ETC_DIR}/web_users.txt"
