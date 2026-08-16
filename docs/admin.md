# Admin / operations

## Installation

See `install.sh` (creates the service user, venv, installs requirements, and the
systemd unit). The daemon runs from `thermostat_monitor.service`.

```bash
sudo ./install.sh [username] [repo_url]
sudo systemctl enable --now thermostat_monitor.service
journalctl -u thermostat_monitor.service -f
```

## Deployment topology (important)

The live daemon does **not** run from the working copy you edit in. On job6 it runs
as the **`thermostat`** service user from a **separate checkout at
`/home/thermostat/thermostat_scheduler`** (its own venv), per the *installed* unit
at `/etc/systemd/system/thermostat_monitor.service` (`User=thermostat`). The
`thermostat_monitor.service` file in the repo is only a template and may show a
different user/path — the installed unit wins.

So **deploy = sync the code into `/home/thermostat/thermostat_scheduler`, then
restart** — a restart alone reloads whatever is already there. `install.sh`
performs the sync; after it (or any manual copy) run:

```bash
sudo systemctl restart thermostat_monitor.service
```

To confirm a change is actually live, diff the deployed file against your commit
(`sudo diff /home/thermostat/thermostat_scheduler/thermostat_monitor.py <repo>/thermostat_monitor.py`).

### `config.yaml` is NOT synced — edit it on the target

`install.sh` rsyncs with `--exclude config.yaml` (and `--exclude venv`), so it
**never overwrites the deployed config**. Any *new* config block (e.g. the `web:`
section) must be added by hand to the deployed file
`/home/thermostat/thermostat_scheduler/config.yaml` — syncing code alone will not
enable it. The repo's `config.yaml` / `config.example.yaml` are only references.
After editing the deployed config, keep its owner and restart:

```bash
sudoedit /home/thermostat/thermostat_scheduler/config.yaml   # or edit + chown thermostat:thermostat
sudo systemctl restart thermostat_monitor.service
```

### State files are per-user

Persisted device state lives under the service user's home:
`/home/thermostat/.local/state/thermostat_manager/devices.json`
(`last_state` = thermostats, `sensor_state` = sensors). A one-shot report run as a
*different* user reads an empty/non-existent state file and shows all sensors as
"never". Always run reports **as the service user, from its checkout**:

```bash
sudo -u thermostat bash -lc \
  'source /home/thermostat/thermostat_scheduler/venv/bin/activate && \
   exec python /home/thermostat/thermostat_scheduler/thermostat_monitor.py \
     --config /home/thermostat/thermostat_scheduler/config.yaml --report --mail'
```

(`--report` prints a snapshot; add `--mail` to also send it.)

## Configuration

All tunables live in `config.yaml`; `config.example.yaml` is the annotated
reference. Key sections: `mqtt`, `alerts`, `heatpump`, `season`, `sensors`,
`manual_thermostats`, `thermostat_types`, `thermostats`.

## Secrets (do NOT commit)

- **MQTT credentials**: prefer environment variables over `config.yaml`:
  `THERMOSTAT_MQTT_USER` / `THERMOSTAT_MQTT_PASS` (read by `common.mqtt_credentials`).
  For the service, put them in a mode-600 EnvironmentFile, e.g.
  `/etc/thermostat/secrets.env`, referenced from the unit (`EnvironmentFile=`).
- **Mail**: handled by the `send-mail` helper (`~/bin/send-mail.py` +
  `~/.config/sendmail-gmail.env`, mode 600). The repo never stores these. The
  manager invokes it with `--from-name` (sender shows as `thermostat_monitor`)
  and `--pre` (adds a monospace HTML part so the report's aligned tables stay
  readable in clients that render plain text proportionally). The helper must
  support those flags; deploy it for the service user too
  (`/home/thermostat/bin/send-mail.py`).
- Where to find the values: password manager / Nextcloud (personal). The MQTT
  password is also in the broker/zigbee2mqtt config on the broker host.

## Logging

Both tools log via the `logging` module to stdout/stderr; the systemd unit
captures these into the journal with `SyslogIdentifier=thermostat_monitor`. Set
`THERMOSTAT_LOG_TS=1` for timestamps when running interactively.

## Backup & restore

- **Source**: private GitHub repo `joba-1/thermostat_scheduler` (push regularly).
- **Config**: committed (sanitised — no secrets).
- **Alert state** (`alerts.state_file`): transient; safe to lose (worst case a
  one-time re-alert). Not backed up.
- No database.

### Restore procedure

```
1. Install OS + Python3 + venv (see Installation)
2. git clone git@github.com:joba-1/thermostat_scheduler.git
3. Restore secrets: /etc/thermostat/secrets.env (MQTT) and
   ~/.config/sendmail-gmail.env (mail) from password manager / Nextcloud
4. python3 -m venv venv && venv/bin/pip install -r requirements.txt
5. sudo ./install.sh   (or just enable the systemd unit)
6. Verify: python3 thermostat_monitor.py --report
```

Test the restore on a spare host at least once after major changes.

## Troubleshooting

- **No device states in `--check`/`--report`**: the daemon must be running and
  subscribed; many TRVs only publish on change, so wait or nudge a setpoint.
- **False "battery unknown"**: mains/unknown-battery devices report no battery;
  these are not alerted (only `battery_low` / level < limit are).
- **Cooling not engaging**: confirm `coolingon` is `on` in
  `ems-esp/thermostat_data` (`mosquitto_sub -t 'ems-esp/#' -v`) and that
  `season.control` is true.
- **Per-type cooling payload wrong**: verify against one device of each type
  with `--dry-run` and zigbee2mqtt before trusting auto control (see test.md).
- **A TRV reports fine but ignores every command** (`device_fault` alert, room
  drifting, `--check` shows it stuck): report path and write path fail
  independently — a head can sit on the mesh with a strong link, a full battery
  and normal state reports while discarding every `/set`. z2m logs nothing about
  it here (`bridge/logging` publishes nothing unless MQTT log output is enabled).
  Confirm with one *inert* write and read it back — `comfort_temperature` is
  ideal, it does not disturb a running valve:
  `mosquitto_pub -t 'zigbee2mqtt/<Name>/set' -m '{"comfort_temperature":33}'`.
  If it never takes: battery swap, remount and z2m **reconfigure do not fix it**;
  a **delete / re-join / rename** in z2m does, and clears a stuck `fault_alarm`
  too. The ieee is unchanged by a re-join, so `config.yaml` needs no edit — but
  the device returns unconfigured, so finish with
  `thermostat-reonboard "<Room>"`. Re-joining tends to pick a worse mesh parent
  (seen: LQI 80→18), so re-join near a router when you can.
  Note the fault flag alone proves nothing: one valve reported `fault_alarm: 2`
  and refused everything, another reported `fault_alarm: {'error': 2}` and obeyed
  perfectly. Always test a write.
- **Heat-pump remote feed stale (alert)**: the `heatpump.remote_feed` source
  sensor (e.g. `Wohnzimmer Luft`) stopped updating; the pump's dew-point
  protection is running on a stale value. Usually a Zigbee mesh dropout —
  confirm the sensor is publishing (`mosquitto_sub -t 'zigbee2mqtt/Wohnzimmer Luft' -v`).
  Watch the feed itself with `mosquitto_sub -t 'ems-esp/thermostat/hc1/remote#' -v`.
