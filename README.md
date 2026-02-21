# Thermostat Monitor

This repository contains two scripts:

- `thermostat_scheduler.py` — publishes schedules to thermostats (used manually or by CI)
- `thermostat_monitor.py` — subscribes to thermostat state topics and responds to requests

This README explains how to install `thermostat_monitor.py` as a systemd service.

Prerequisites
- A Linux system with systemd
- Python 3 (with `venv`) available as `python3`
- `git` if you want the installer to clone from a remote repository

Quick install (automated)

1. Copy this repository to the machine that will run the monitor (or run the installer from this repo).
2. Run the install script as root (it will perform checks and be idempotent):

```bash
sudo ./install.sh [USERNAME] [REPO_URL]
```

- `USERNAME` (optional): system user to run the service; default: `thermostat`
- `REPO_URL` (optional): if provided and the target directory does not exist, the script will `git clone` this URL into the new user's home under `thermostat_scheduler`.

What the installer does

- Creates a system user with a home directory (if it doesn't already exist)
- Clones the repository into `/home/<user>/thermostat_scheduler` (or copies the current repo if you run the script from within the repo)
- Creates a Python virtual environment inside the repository (`venv`) and installs `requirements.txt` if present
- Creates a systemd unit file `/etc/systemd/system/thermostat_monitor.service` and enables+starts the service

Manual install (step-by-step)

1. Create system user:

```bash
sudo useradd -r -m -d /home/thermostat -s /usr/sbin/nologin thermostat
```

2. Clone repository (or copy files):

```bash
sudo -u thermostat git clone <repo-url> /home/thermostat/thermostat_scheduler
```

3. Create venv and install requirements:

```bash
python3 -m venv /home/thermostat/thermostat_scheduler/venv
/home/thermostat/thermostat_scheduler/venv/bin/pip install -U pip
/home/thermostat/thermostat_scheduler/venv/bin/pip install -r /home/thermostat/thermostat_scheduler/requirements.txt
```

4. Create systemd unit (example)

The installer will create `/etc/systemd/system/thermostat_monitor.service`. It runs the monitor with the virtualenv python and the `config.yaml` from the installation directory.

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now thermostat_monitor.service
sudo journalctl -u thermostat_monitor.service -f
```

Uninstall

To stop and remove the service:

```bash
sudo systemctl stop thermostat_monitor.service
sudo systemctl disable thermostat_monitor.service
sudo rm /etc/systemd/system/thermostat_monitor.service
sudo systemctl daemon-reload
```

Notes

- If you use a different username than the systemd service file expects, the installer will write the correct user and paths into the unit file.
- The monitor listens for `get` requests on topic `thermostat_monitor` and publishes per-device responses to `thermostat_monitor/<Name>`.
