# User documentation

## Everyday tasks

Reinstate the intended state after editing `config.yaml`:

```bash
python3 thermostat_scheduler.py            # reinstate intended state on all thermostats
python3 thermostat_scheduler.py --dry-run  # preview payloads, send nothing
python3 thermostat_scheduler.py --check    # compare live state vs intended (needs the daemon)
```

A plain run is **season-aware**: in heating it pushes the weekly schedule, in
cooling it forces the valves open (the heat pump decides which — see below). It
also reads each device's live state from the running manager daemon, so two
kinds of room are left in place: those in **manual override** and those switched
**off** (`system_mode: off`, e.g. a window is open). For those it refreshes only
the stored schedule + calibration, never the mode/preset/setpoint — so the room
stays manual/off. (Without the daemon it can't see per-device state and falls
back to the season-intended payload for every room.)

### Manual overrides

If someone turns a thermostat to manual at the device, comfort control for that
room is suspended (and you get a low-priority note).

```bash
python3 thermostat_scheduler.py --list-manual           # which rooms are in manual?
python3 thermostat_scheduler.py --reset-manual           # clear all (re-push schedule)
python3 thermostat_scheduler.py --reset-manual "Bad OG"  # clear specific rooms
```

### Status overview

```bash
python3 thermostat_scheduler.py --status        # print a full overview
python3 thermostat_scheduler.py --status-mail   # ...and email it
```

These ask the **running daemon** for the report, so it is built from the
daemon's full accumulated state (real data, not the many `—` you'd get from a
cold one-shot). The equivalent one-shot still exists for when the daemon isn't
running: `python3 thermostat_monitor.py --report [--mail]`.

The overview shows the desired mode, heat-pump telemetry (with units), every
thermostat (state, setpoint, room temp, battery, last seen), every sensor, any
manual valves, and the open issues. The daemon also mails this automatically
every `report_interval_hours`. Mail arrives from the sender name
`thermostat_monitor`.

### Status web page

If `web.enabled` is set (see config), the daemon serves the same overview as a
self-refreshing web page at `http://<host>:<port>/` (default port `8099`). It is
**read-only** and shows the daemon's latest cached snapshot — opening it never
changes anything. The page re-fetches itself every `web.refresh` seconds and
adapts to light/dark mode. There is **no authentication**, so bind it to a
trusted LAN only (set `web.host: 127.0.0.1` for local-only access).

**Per-room history charts.** Click a room's temperature on the status page to open
a chart of the last **6 h / 24 h / 3 d / 1 w / 1 mo** (toggle links, "← all rooms" to
go back). Each chart overlays the room temperature and the heat pump's outdoor
reference temperature (dashed) with stripe rows below: **HP cooling / heating**,
**window open**, and **conditioned** (heat pump producing *and* the window closed —
i.e. the room was actually being cooled/heated). Window-open spans longer than 12 h
are hidden as a dropped-close artifact (cheap contacts often miss the close edge and
get stuck "open"). The history is read on demand from Home
Assistant's InfluxDB (`web.history.influx_url`, default `http://job4:8086`); if it's
unreachable the chart degrades gracefully ("no temperature history"). Each device's HA
entity is resolved from its **ieee + friendly name**: the named slug
(`schlafzimmer_fenster_contact`) and HA's `0x<ieee>_<property>` fallback are both
queried and their history is **merged**, so a sensor HA logged under its raw ieee
still resolves — and history that is split across two entity ids by an HA **rename**
stays one continuous band (no gap). No per-room overrides.

## Email alerts

The manager mails you (low-noise):

- **immediately** for each new alert (battery low, lost device/sensor, settings
  not applied, room not following setpoint, water leak) — re-sent only after
  `cooldown_hours` while it stays open;
- a **daily digest** at `digest_hour` of everything still open;
- a **periodic status report** (full overview);
- a **mode-change reminder** to open/reset your manual valves when the house
  switches between heating and cooling.

Window open while a room is cold/hot is reported as info, not an alert — it
explains the deviation.

## Window/door control

When a room's contact sensor opens, the manager switches that room's TRV **off**;
when it closes, it restores the room's intended state (season-aware: in cooling it
re-opens the valve, in heating it restores the weekly schedule). This replaces the
old Home Assistant "lüften/heizen" automations — control now lives here.

- Only rooms the manager itself switched off are restored, so a TRV you turned
  **off by hand** stays off, and a room in **manual override** is left untouched.
- A short debounce (`window_control.open_debounce`/`close_debounce`, default 5 s;
  2 s for the shower) avoids reacting to a brief open/close.
- The laundry room only ventilates when it is dry (`humidity_guard`): if it's
  humid (or the humidity sensor is silent) the heating stays on.
- Each TRV's own built-in window detection is disabled on connect so the two
  don't fight (`window_control.disable_builtin`).
- Every decision is logged (`journalctl -u thermostat_monitor`), and the status
  report lists rooms currently **Off (window open)** and shows `off (window)` in
  the thermostat's state column.
- Kill switch: `window_control.act: false` keeps detecting/logging/status without
  touching any valve; `enabled: false` turns the feature off.

Rooms without a contact sensor (e.g. Julians, Wohnzimmer) are not window-controlled.

## Cooling

When `season.mode: auto` and the heat pump reports cooling (`coolingon: on`),
the manager forces every controllable thermostat fully open (valves let cold
water through) and restores the weekly schedule when heating resumes. Rooms in
manual override are left alone. Set `season.mode: cooling`/`heating` to force a
mode regardless of the heat pump.
