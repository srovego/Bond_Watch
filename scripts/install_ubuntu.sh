#!/usr/bin/env bash
set -euo pipefail

if [[ $(id -u) -ne 0 ]]; then
  echo "Run as root on the target Ubuntu VPS after review" >&2
  exit 1
fi
APP=/opt/bond-watch
if [[ ! -f "$APP/pyproject.toml" ]]; then
  echo "Copy the repository to $APP first" >&2
  exit 1
fi
id bondwatch >/dev/null 2>&1 || useradd --system --home "$APP" --shell /usr/sbin/nologin bondwatch
python3 -m venv "$APP/.venv"
"$APP/.venv/bin/pip" install -e "$APP"
install -d -o bondwatch -g bondwatch -m 0750 "$APP/data" "$APP/data/backups"
chown -R bondwatch:bondwatch "$APP/config"
if [[ ! -f "$APP/.env" ]]; then
  cp "$APP/.env.example" "$APP/.env"
  echo "Edit $APP/.env with Telegram secrets before enabling services" >&2
fi
chown bondwatch:bondwatch "$APP/.env"
chmod 0600 "$APP/.env"
install -m 0644 "$APP"/deploy/*.service "$APP"/deploy/*.timer /etc/systemd/system/
systemctl daemon-reload
echo "Installed. Configure .env, run init-db and check-telegram, then enable timers/services."

