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

# 2) clone or copy repository
if [ -d "${INSTALL_DIR}" ] && [ -f "${INSTALL_DIR}/thermostat_monitor.py" ]; then
  echo "Repository already present at ${INSTALL_DIR}"
else
  if [ -n "${REPO_URL}" ]; then
    echo "Cloning ${REPO_URL} into ${INSTALL_DIR}"
    git clone "${REPO_URL}" "${INSTALL_DIR}"
  else
    echo "Copying current repository into ${INSTALL_DIR}"
    mkdir -p "${INSTALL_DIR}"
    rsync -a --exclude .git "${SCRIPT_DIR}/" "${INSTALL_DIR}/"
  fi
  chown -R "${USERNAME}:${USERNAME}" "${INSTALL_DIR}"
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

# 5) install systemd unit
SERVICE_PATH="/etc/systemd/system/thermostat_monitor.service"
echo "Writing systemd unit to ${SERVICE_PATH} (backing up existing if present)"
if [ -f "${SERVICE_PATH}" ]; then
  cp "${SERVICE_PATH}" "${SERVICE_PATH}.bak.$(date +%s)"
fi
cat > "${SERVICE_PATH}" <<EOF
[Unit]
Description=Thermostat Monitor Service
After=network.target

[Service]
Type=simple
User=${USERNAME}
WorkingDirectory=${INSTALL_DIR}
ExecStart=${INSTALL_DIR}/venv/bin/python ${INSTALL_DIR}/thermostat_monitor.py --config ${INSTALL_DIR}/config.yaml
Restart=on-failure
RestartSec=5
Environment=PYTHONUNBUFFERED=1
StandardOutput=syslog
StandardError=syslog
SyslogIdentifier=thermostat_monitor

[Install]
WantedBy=multi-user.target
EOF

chmod 644 "${SERVICE_PATH}"

echo "Reloading systemd daemon and enabling service"
systemctl daemon-reload
systemctl enable --now thermostat_monitor.service

echo "Installation complete. Check status with: sudo systemctl status thermostat_monitor.service"
echo "Logs: sudo journalctl -u thermostat_monitor.service -f"
