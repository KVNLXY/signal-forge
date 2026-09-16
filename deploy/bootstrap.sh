#!/usr/bin/env bash
# One-shot server setup for SignalForge (Ubuntu 22.04 / 24.04, x86 or ARM).
#
# Upload the project first, then:
#
#     sudo bash deploy/bootstrap.sh
#
# It is safe to re-run: every step checks its own state before acting.
# Nothing is started until `python -m app.main check` passes.

set -euo pipefail

APP_DIR=/opt/signalforge
APP_USER=signalforge
SERVICE=signalforge

say() { printf "\n\033[1m==> %s\033[0m\n" "$*"; }
fail() { printf "\n\033[31mERROR: %s\033[0m\n" "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || fail "run this with sudo"

SOURCE_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
[[ -f "$SOURCE_DIR/app/main.py" ]] || fail "run this from inside the project directory"

say "Installing system packages"
apt-get update -qq
# libgl1 / libglib2.0-0 are what OpenCV needs for the chart image reader.
apt-get install -y -qq python3-venv python3-pip libgl1 libglib2.0-0 \
    || apt-get install -y -qq python3-venv python3-pip libgl1-mesa-glx libglib2.0-0

say "Creating the $APP_USER user"
if ! id -u "$APP_USER" >/dev/null 2>&1; then
    useradd --system --create-home --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"
fi
mkdir -p "$APP_DIR"

say "Copying the application to $APP_DIR"
if [[ "$SOURCE_DIR" != "$APP_DIR" ]]; then
    cp -r "$SOURCE_DIR"/app "$SOURCE_DIR"/scripts "$SOURCE_DIR"/deploy \
          "$SOURCE_DIR"/requirements.txt "$APP_DIR"/
    [[ -f "$SOURCE_DIR/.env" ]] && cp "$SOURCE_DIR/.env" "$APP_DIR"/
fi
[[ -f "$APP_DIR/.env" ]] || fail "$APP_DIR/.env is missing - upload your .env first"

chown -R "$APP_USER:$APP_USER" "$APP_DIR"
chmod 600 "$APP_DIR/.env"          # it holds the Telegram session

say "Building the virtualenv"
if [[ ! -x "$APP_DIR/.venv/bin/python" ]]; then
    sudo -u "$APP_USER" python3 -m venv "$APP_DIR/.venv"
fi
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"

say "Pointing the database at a local file"
if grep -q '^DATABASE_URL=.*postgresql' "$APP_DIR/.env"; then
    sed -i 's|^DATABASE_URL=.*|DATABASE_URL=sqlite+aiosqlite:///./signalforge.db|' "$APP_DIR/.env"
    echo "DATABASE_URL switched to SQLite"
fi

say "Pre-flight check"
cd "$APP_DIR"
if ! sudo -u "$APP_USER" "$APP_DIR/.venv/bin/python" -m app.main check; then
    fail "the check failed - fix the FAILED lines above, then re-run this script"
fi

say "Installing the systemd service"
cp "$APP_DIR/deploy/$SERVICE.service" "/etc/systemd/system/$SERVICE.service"
systemctl daemon-reload
systemctl enable --now "$SERVICE"

sleep 3
systemctl --no-pager --lines=15 status "$SERVICE" || true

cat <<EOF

==> Done. SignalForge runs as a service now.

    journalctl -u $SERVICE -f          live log
    systemctl restart $SERVICE         after editing $APP_DIR/.env
    systemctl stop $SERVICE            stop it

Remember: the same Telegram session must not run anywhere else at the
same time - stop the bot on your PC before leaving this one running.
EOF
