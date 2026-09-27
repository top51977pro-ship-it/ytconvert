#!/usr/bin/env bash
# Install YTConvert Online on a fresh Ubuntu 22.04/24.04 server (Oracle Cloud
# Always Free Ampere A1 is the target, but any Ubuntu box works).
#
#   sudo bash setup-server.sh <hostname>
#
# <hostname> must resolve to this server - with no domain of your own, use
# <public-ip-with-dashes>.sslip.io (e.g. 158-101-12-34.sslip.io).
# Expects the app files (app.py, server.py, web/) next to this script's parent.
set -euo pipefail

HOST="${1:?usage: setup-server.sh <hostname>}"
SRC="$(cd "$(dirname "$0")/.." && pwd)"
APP=/opt/ytconvert
DATA=/var/lib/ytconvert

echo "==> packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q python3-venv ffmpeg caddy unzip curl iptables-persistent

echo "==> Deno (JavaScript runtime yt-dlp needs for YouTube)"
if ! command -v deno >/dev/null; then
  arch=$(uname -m); [ "$arch" = "aarch64" ] && t=aarch64-unknown-linux-gnu || t=x86_64-unknown-linux-gnu
  curl -fsSL "https://github.com/denoland/deno/releases/latest/download/deno-$t.zip" -o /tmp/deno.zip
  unzip -o -q /tmp/deno.zip -d /usr/local/bin && chmod +x /usr/local/bin/deno
fi
deno --version | head -1

echo "==> app"
id ytconvert >/dev/null 2>&1 || useradd --system --home "$DATA" --shell /usr/sbin/nologin ytconvert
mkdir -p "$APP" "$DATA"
cp "$SRC/app.py" "$SRC/server.py" "$APP/"
rm -rf "$APP/web" && cp -r "$SRC/web" "$APP/web"
[ -d "$APP/venv" ] || python3 -m venv "$APP/venv"
"$APP/venv/bin/pip" install -q -U pip "yt-dlp[default]"
chown -R ytconvert:ytconvert "$DATA"

echo "==> service"
cat > /etc/systemd/system/ytconvert.service <<EOF
[Unit]
Description=YTConvert Online
After=network-online.target
Wants=network-online.target

[Service]
User=ytconvert
WorkingDirectory=$APP
Environment=PATH=/usr/local/bin:/usr/bin:/bin
ExecStart=$APP/venv/bin/python server.py --port 8080 --data $DATA
Restart=always
RestartSec=3
# hardening
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=yes
ReadWritePaths=$DATA

[Install]
WantedBy=multi-user.target
EOF

# YouTube changes often; keep yt-dlp current.
cat > /etc/systemd/system/ytconvert-update.service <<EOF
[Unit]
Description=Update yt-dlp for YTConvert

[Service]
Type=oneshot
ExecStart=$APP/venv/bin/pip install -q -U "yt-dlp[default]"
ExecStartPost=/bin/systemctl restart ytconvert
EOF
cat > /etc/systemd/system/ytconvert-update.timer <<EOF
[Unit]
Description=Daily yt-dlp update for YTConvert

[Timer]
OnCalendar=*-*-* 04:30:00
RandomizedDelaySec=30m
Persistent=true

[Install]
WantedBy=timers.target
EOF

echo "==> Caddy (HTTPS)"
cat > /etc/caddy/Caddyfile <<EOF
$HOST {
    encode gzip
    reverse_proxy 127.0.0.1:8080 {
        flush_interval -1
        transport http {
            read_timeout 30m
            write_timeout 30m
        }
    }
}
EOF

echo "==> firewall (Oracle's Ubuntu images reject everything but SSH)"
for p in 80 443; do
  iptables -C INPUT -p tcp --dport $p -m state --state NEW -j ACCEPT 2>/dev/null ||
    iptables -I INPUT 5 -p tcp --dport $p -m state --state NEW -j ACCEPT
done
netfilter-persistent save >/dev/null

systemctl daemon-reload
systemctl enable --now ytconvert ytconvert-update.timer
systemctl restart caddy
sleep 3
systemctl --no-pager --lines=5 status ytconvert | head -12
echo "==> done: https://$HOST"
