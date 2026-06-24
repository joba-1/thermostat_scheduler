#!/usr/bin/env bash
set -euo pipefail

# idempotent installer for thermostat_monitor
# Usage: sudo ./install.sh [USERNAME] [REPO_URL]

USERNAME=${1:-thermostat}
REPO_URL=${2:-}
INSTALL_DIR="/home/${USERNAME}/thermostat_scheduler"
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

if [ "$(id -u)" -ne 0 ]; then
  echo "This installer must be run as root. Use sudo." >&2
  exit 1
fi

echo "Installer started. Username=${USERNAME}, install_dir=${INSTALL_DIR}"

# 1) create system user if missing
if id -u "${USERNAME}" >/dev/null 2>&1; then
  echo "User ${USERNAME} already exists"
else
  echo "Creating system user ${USERNAME} with home /home/${USERNAME}"
  useradd -r -m -d "/home/${USERNAME}" -s /usr/sbin/nologin "${USERNAME}"
fi

# Ensure home exists
mkdir -p "/home/${USERNAME}"
chown "${USERNAME}:${USERNAME}" "/home/${USERNAME}"

# 2) clone or copy/update repository
if [ -n "${REPO_URL}" ] && [ ! -d "${INSTALL_DIR}/.git" ]; then
  echo "Cloning ${REPO_URL} into ${INSTALL_DIR}"
  git clone "${REPO_URL}" "${INSTALL_DIR}"
  chown -R "${USERNAME}:${USERNAME}" "${INSTALL_DIR}"
elif [ "${SCRIPT_DIR}" != "${INSTALL_DIR}" ]; then
  # Update code from this checkout; preserve the deployed venv and config.yaml.
  echo "Syncing code from ${SCRIPT_DIR} into ${INSTALL_DIR} (preserving venv + config.yaml)"
  mkdir -p "${INSTALL_DIR}"
  rsync -a --delete --exclude .git --exclude venv --exclude config.yaml \
    --exclude __pycache__ --exclude .pytest_cache \
    "${SCRIPT_DIR}/" "${INSTALL_DIR}/"
  chown -R "${USERNAME}:${USERNAME}" "${INSTALL_DIR}"
else
  echo "Running from the install dir; not copying."
fi

# 3) create virtualenv
VENV_PY="${INSTALL_DIR}/venv/bin/python"
if [ -x "${VENV_PY}" ]; then
  echo "Virtualenv already present"
else
  echo "Creating virtualenv in ${INSTALL_DIR}/venv"
  python3 -m venv "${INSTALL_DIR}/venv"
  chown -R "${USERNAME}:${USERNAME}" "${INSTALL_DIR}/venv"
fi

# 4) install requirements if present
REQ_FILE="${INSTALL_DIR}/requirements.txt"
if [ -f "${REQ_FILE}" ]; then
  echo "Installing Python requirements"
  "${INSTALL_DIR}/venv/bin/pip" install -U pip
  "${INSTALL_DIR}/venv/bin/pip" install -r "${REQ_FILE}"
else
  echo "No requirements.txt found; skipping pip install"
fi

# 4b) seed config from the annotated example if none exists yet
CONFIG_FILE="${INSTALL_DIR}/config.yaml"
if [ ! -f "${CONFIG_FILE}" ] && [ -f "${INSTALL_DIR}/config.example.yaml" ]; then
  echo "No config.yaml; seeding from config.example.yaml (edit it before relying on the service)"
  cp "${INSTALL_DIR}/config.example.yaml" "${CONFIG_FILE}"
  chown "${USERNAME}:${USERNAME}" "${CONFIG_FILE}"
fi

# 4c) optional MQTT secrets file (env vars override config; never commit secrets)
SECRETS_DIR="/etc/thermostat"
SECRETS_FILE="${SECRETS_DIR}/secrets.env"
if [ ! -f "${SECRETS_FILE}" ]; then
  echo "Creating ${SECRETS_FILE} template (mode 600). Fill in if your broker needs auth."
  mkdir -p "${SECRETS_DIR}"
  cat > "${SECRETS_FILE}" <<'SECRETS'
# MQTT credentials for the thermostat manager (override config.yaml).
# Leave commented if the broker is open. Keep this file mode 600.
#THERMOSTAT_MQTT_USER=mqtt_user
#THERMOSTAT_MQTT_PASS=mqtt_pass
SECRETS
  chmod 600 "${SECRETS_FILE}"
fi

# Mail uses the send-mail helper (~/bin/send-mail.py of the service User). Warn
# if it is missing for that user, since alerts would then fail to send.
USER_HOME=$(getent passwd "${USERNAME}" | cut -d: -f6)
if [ ! -x "${USER_HOME}/bin/send-mail.py" ]; then
  echo "WARNING: ${USER_HOME}/bin/send-mail.py not found for user ${USERNAME}."
  echo "         Email alerts will fail until send-mail is deployed for this user,"
  echo "         or set alerts.send_mail_cmd in config.yaml to an absolute path."
fi

# 4d) install the re-onboard convenience wrapper into PATH so day-to-day use is
# just `thermostat-reonboard "Room"` (no venv/user/config to remember).
WRAPPER_SRC="${INSTALL_DIR}/thermostat-reonboard"
WRAPPER_DST="/usr/local/bin/thermostat-reonboard"
if [ -f "${WRAPPER_SRC}" ]; then
  echo "Installing ${WRAPPER_DST}"
  # Point the wrapper at the chosen service user (repo default is 'thermostat').
  sed "s/^USER_NAME=thermostat$/USER_NAME=${USERNAME}/" \
    "${WRAPPER_SRC}" > "${WRAPPER_DST}"
  chmod 755 "${WRAPPER_DST}"
fi

# 5) install systemd unit
SERVICE_PATH="/etc/systemd/system/thermostat_monitor.service"
echo "Writing systemd unit to ${SERVICE_PATH} (backing up existing if present)"
if [ -f "${SERVICE_PATH}" ]; then
  cp "${SERVICE_PATH}" "${SERVICE_PATH}.bak.$(date +%s)"
fi
cat > "${SERVICE_PATH}" <<EOF
[Unit]
Description=Thermostat Manager (health, cooling, alerts)
After=network.target

[Service]
Type=simple
User=${USERNAME}
WorkingDirectory=${INSTALL_DIR}
# Optional mode-600 secrets file (THERMOSTAT_MQTT_USER / THERMOSTAT_MQTT_PASS).
# The leading '-' makes it optional.
EnvironmentFile=-${SECRETS_FILE}
ExecStart=/bin/bash -lc 'source ${INSTALL_DIR}/venv/bin/activate && exec python ${INSTALL_DIR}/thermostat_monitor.py --config ${INSTALL_DIR}/config.yaml'
Restart=on-failure
RestartSec=5
Environment=PYTHONUNBUFFERED=1
StandardOutput=journal
StandardError=journal
SyslogIdentifier=thermostat_monitor

[Install]
WantedBy=multi-user.target
EOF

chmod 644 "${SERVICE_PATH}"

echo "Reloading systemd daemon, enabling and (re)starting service"
systemctl daemon-reload
systemctl enable thermostat_monitor.service
# Always restart so freshly-synced code is actually loaded: `enable --now` only
# *starts* a stopped unit and would leave an already-running daemon on the old
# code. `restart` starts it if stopped and reloads new code if already running.
systemctl restart thermostat_monitor.service

echo "Installation complete. Check status with: sudo systemctl status thermostat_monitor.service"
echo "Logs: sudo journalctl -u thermostat_monitor.service -f"
