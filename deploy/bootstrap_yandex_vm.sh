#!/usr/bin/env bash
# Bootstrap rts_local на чистой Ubuntu VM (Yandex Cloud).
# Запуск от root после первого clone или через:
#   curl -fsSL ... | bash   (не используйте без ревью)
#
# Пример:
#   sudo APP_USER=ubuntu REPO_URL=git@github.com:mitrist/rts_local.git bash deploy/bootstrap_yandex_vm.sh
#
set -euo pipefail

APP_USER="${APP_USER:-ubuntu}"
APP_ROOT="${APP_ROOT:-/opt/rts_local}"
META_DIR="${META_DIR:-/var/lib/rts_local}"
ETC_DIR="${ETC_DIR:-/etc/rts-local}"
REPO_URL="${REPO_URL:-git@github.com:mitrist/rts_local.git}"
BRANCH="${BRANCH:-main}"
SERVICE_NAME="${SERVICE_NAME:-rts-local}"
WEB_PORT="${WEB_PORT:-8765}"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run as root (sudo)" >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq git nginx python3 python3-venv python3-pip sqlite3 curl ca-certificates

if ! id "$APP_USER" &>/dev/null; then
  useradd -m -s /bin/bash "$APP_USER"
fi

# Swap 2G если нет (SQLite + web часто едят RAM)
if ! grep -q '/swapfile' /etc/fstab 2>/dev/null; then
  if [[ ! -f /swapfile ]]; then
    fallocate -l 2G /swapfile 2>/dev/null || dd if=/dev/zero of=/swapfile bs=1M count=2048
    chmod 600 /swapfile
    mkswap /swapfile
  fi
  swapon /swapfile || true
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

mkdir -p "$APP_ROOT" "$META_DIR" "$ETC_DIR" "$APP_ROOT/data" "$META_DIR/cookies"

if [[ ! -d "$APP_ROOT/app/.git" ]]; then
  if [[ -d "$APP_ROOT/app" ]] && [[ -n "$(ls -A "$APP_ROOT/app" 2>/dev/null || true)" ]]; then
    echo "Directory $APP_ROOT/app exists and is not empty — skip clone" >&2
  else
    sudo -u "$APP_USER" git clone --branch "$BRANCH" "$REPO_URL" "$APP_ROOT/app"
  fi
else
  echo "Repo already present at $APP_ROOT/app"
fi

python3 -m venv "$APP_ROOT/venv"
"$APP_ROOT/venv/bin/pip" install -q --upgrade pip
"$APP_ROOT/venv/bin/pip" install -q -r "$APP_ROOT/app/requirements.txt"

# Конфиги (не перезаписываем существующие)
if [[ ! -f "$ETC_DIR/service.env" ]]; then
  cp "$APP_ROOT/app/deploy/service.env.example" "$ETC_DIR/service.env"
  sed -i "s|/opt/rts_local|$APP_ROOT|g; s|/var/lib/rts_local|$META_DIR|g" "$ETC_DIR/service.env"
  # Пример может содержать 8766 (старый соседний хост). Nginx ниже проксирует на WEB_PORT.
  if grep -q '^RTS_WEB_PORT=' "$ETC_DIR/service.env"; then
    sed -i "s/^RTS_WEB_PORT=.*/RTS_WEB_PORT=${WEB_PORT}/" "$ETC_DIR/service.env"
  else
    echo "RTS_WEB_PORT=${WEB_PORT}" >> "$ETC_DIR/service.env"
  fi
fi
if [[ ! -f "$ETC_DIR/eis.env" ]]; then
  cp "$APP_ROOT/app/deploy/eis.env.example" "$ETC_DIR/eis.env"
fi
if [[ ! -f "$ETC_DIR/web_users.txt" ]]; then
  cp "$APP_ROOT/app/web_users.example.txt" "$ETC_DIR/web_users.txt"
fi
if [[ ! -f "$META_DIR/rts_preoffers.json" ]] && [[ -f "$APP_ROOT/app/rts_preoffers.json" ]]; then
  cp "$APP_ROOT/app/rts_preoffers.json" "$META_DIR/rts_preoffers.json"
fi

chown -R "$APP_USER:$APP_USER" "$APP_ROOT" "$META_DIR"
chmod 750 "$ETC_DIR"
chown root:"$APP_USER" "$ETC_DIR/service.env" "$ETC_DIR/eis.env" "$ETC_DIR/web_users.txt"
chmod 640 "$ETC_DIR/service.env" "$ETC_DIR/eis.env" "$ETC_DIR/web_users.txt"

UNIT="/etc/systemd/system/${SERVICE_NAME}.service"
cat >"$UNIT" <<EOF
[Unit]
Description=RTS Local web console
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

# nginx
SITE="/etc/nginx/sites-available/${SERVICE_NAME}"
cat >"$SITE" <<EOF
server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name _;

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
rm -f /etc/nginx/sites-enabled/default
nginx -t

systemctl daemon-reload
systemctl enable --now "$SERVICE_NAME"
systemctl reload nginx

echo "=== Bootstrap done ==="
echo "App:     $APP_ROOT/app"
echo "Meta:    $META_DIR"
echo "Config:  $ETC_DIR/service.env , $ETC_DIR/eis.env , $ETC_DIR/web_users.txt"
echo "Service: systemctl status $SERVICE_NAME"
echo "Health:  curl -s http://127.0.0.1:${WEB_PORT}/health/live"
echo ""
echo "Next:"
echo "  1) Заполните секреты в $ETC_DIR/eis.env и session secret в service.env"
echo "  2) Смените пароли в $ETC_DIR/web_users.txt"
echo "  3) Опционально: certbot --nginx -d your.domain"
echo "  4) Включите timer PP monitor: см. deploy/DEPLOY.md"
