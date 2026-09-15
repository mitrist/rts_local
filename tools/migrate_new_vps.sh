#!/bin/bash
# Bootstrap rts_tender on a fresh Ubuntu VPS.
# Run as root on the NEW server after copying /tmp/rts_migrate_bundle.tgz here.
set -euo pipefail

BUNDLE="${1:-/tmp/rts_migrate_bundle.tgz}"
APP_USER="${APP_USER:-mitrist12}"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run as root" >&2
  exit 1
fi
if [[ ! -f "$BUNDLE" ]]; then
  echo "Bundle not found: $BUNDLE" >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq git nginx certbot python3-certbot-nginx sqlite3 rsync curl

if ! id "$APP_USER" &>/dev/null; then
  useradd -m -s /bin/bash "$APP_USER"
fi

if ! grep -q '/swapfile' /etc/fstab 2>/dev/null; then
  fallocate -l 2G /swapfile || dd if=/dev/zero of=/swapfile bs=1M count=2048
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

mkdir -p /opt/rts_tender /var/lib/rts_tender /etc/rts-tender
tar -xzf "$BUNDLE" -C /

PY=/usr/bin/python3
"$PY" -m venv /opt/rts_tender/venv
/opt/rts_tender/venv/bin/pip install -q --upgrade pip
/opt/rts_tender/venv/bin/pip install -q -r /opt/rts_tender/app/requirements.txt

chown -R "$APP_USER:$APP_USER" /opt/rts_tender /var/lib/rts_tender
chmod 750 /etc/rts-tender
chmod 640 /etc/rts-tender/*
chown root:"$APP_USER" /etc/rts-tender/service.env /etc/rts-tender/eis.env /etc/rts-tender/web_users.txt
chown "$APP_USER:$APP_USER" /etc/rts-tender/cookies_rts.txt
chmod 600 /etc/rts-tender/cookies_rts.txt

install -d -m 755 /etc/systemd/system/rts-tender.service.d
cat >/etc/systemd/system/rts-tender.service <<'UNIT'
[Unit]
Description=RTS Tender web console
After=network.target

[Service]
Type=simple
User=mitrist12
Group=mitrist12
WorkingDirectory=/opt/rts_tender/app
EnvironmentFile=/etc/rts-tender/service.env
ExecStart=/opt/rts_tender/venv/bin/python -m web_ui
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT

cat >/etc/systemd/system/rts-tender.service.d/eis-env.conf <<'DROPIN'
[Service]
EnvironmentFile=-/etc/rts-tender/eis.env
DROPIN

cat >/etc/nginx/sites-available/rts-tender <<'NGINX'
server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name 161.104.18.194 _;

    location / {
        proxy_pass http://127.0.0.1:8765;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 300;
    }
}
NGINX

ln -sf /etc/nginx/sites-available/rts-tender /etc/nginx/sites-enabled/rts-tender
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl enable --now rts-tender
systemctl restart nginx

echo "Bootstrap done. Python: $($PY --version)"
systemctl --no-pager status rts-tender
curl -s -o /dev/null -w "HTTP %{http_code}\n" http://127.0.0.1:8765/ || true
echo "Service URL: http://161.104.18.194 (set RTS_WEB_PUBLIC_BASE_URL and RTS_WEB_HTTPS_ONLY=0 in service.env)"
echo "RTS: add HTTPS_PROXY via old VPS tinyproxy; reload rts-cookie-bridge extension (http://IP in manifest)"
echo "Cookie API: http://161.104.18.194/api/rts/cookie/save"
