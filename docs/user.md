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
thermostat-reonboard --list              # which rooms are in manual override?
thermostat-reonboard                     # clear every manual room
thermostat-reonboard "Bad OG"            # clear specific rooms (quote names with spaces)
```

That is the command installed into the PATH; it wraps
`thermostat_scheduler.py --list-manual` / `--reset-manual` and runs it as the
service user with the right venv and config, so you never have to name an
account, an interpreter or a path. (The `python3 thermostat_scheduler.py …`
spellings from inside the install directory do the same.)

Re-onboarding pushes the **active season's** state, not always the weekly
schedule: cooling → valves open, heating → schedule, standby → off. With no room
named it touches only the rooms actually in manual override — never rooms that
are already under control — and needs the daemon running to tell the two apart.
Rooms that are genuinely *off* (e.g. an open window) are always left off.

It then **verifies** rather than trusting "sent": the device is read back and any
key it did not take is re-sent on its own. z2m turns one payload into a burst of
Zigbee writes and a freshly re-joined or weak-link TRV drops some of them — a
valve once came back with all seven schedule days written but `preset` still
`manual`, looking configured while sitting shut. Expect one of:

```
  ✓ verified applied
  ! <room> did not take preset — resending individually
  ✗ still not applying: preset — the device may be refusing writes
```

The last line means the head itself is rejecting writes while still reporting
normally. Battery, remount and a z2m *reconfigure* do **not** fix that; a
delete / re-join / rename in z2m does, and it also clears a stuck `fault_alarm`.
After a re-join the device comes back unconfigured, so re-onboard it by name.

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
manual valves, and the open issues. The `bat` column carries the percentage a
device reports; where a device reports no percentage at all — the AVATTO ME168
sends only the binary `battery_low` flag — it shows the verdict instead: `ok`,
`low` (highlighted like a low percentage), or `?` when it reports neither. The daemon attaches this same overview to
every alert and to the daily digest, so each mail is self-contained — there's no
separate periodic report mail. Issues are collected over a short window
(`batch_window_minutes`, default 10) and sent as one combined mail, so a burst of
problems — or a sensor that briefly drops off the mesh and comes back — produces a
single mail (or none), not a stream. A daily backstop cap (`max_mails_per_day`,
default 6; the digest is exempt) bounds the worst case. Mail arrives from the
sender name `thermostat_monitor`.

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
  not applied, room not following setpoint) — re-sent only after
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

## Radiator fans (heating and cooling support)

Radiators are weak emitters, especially for cooling, so the manager switches **fan
plugs** that blow air across them (`fan_control`). A fan only helps while heated or
cooled water actually flows through the radiator, so the fans run **only while the
heating-circuit pump PC1 pumps** (`pc1flow` above `min_flow`, default 50 l/h) and
no hot-water charge is running. PC1 (buffer → radiators) keeps running through the
compressor's pauses — measured ~1400 l/h through a 35-minute cooling pause — so
the buffer's cold or heat still reaches the radiators and the fans keep going; it
stops during a hot-water charge, and so do the fans. `heatingpump` is *not* this
pump: it is the primary pump PC0 (heat pump ↔ buffer), which stops with the
compressor. After PC1 stops the fans run on for `off_delay` seconds (default 0,
here 120): the radiators still hold some heat or cold for a while.

- Plugs are **zigbee2mqtt** (`{type: zigbee, name: ...}` → `zigbee2mqtt/<name>/set`)
  or **Tasmota** (`{type: tasmota, topic: ..., power: POWER}` → `cmnd/<topic>/<power>`).
- `on_debounce` (default 30 s) avoids reacting to a momentary blip; `off_delay`
  (default 0) can hold the fans on after circulation stops.
- **Heating too** (`heating: true`, the default); a single fan opts out with
  `heating: false`, e.g. a loud standing fan in a room that heats fine alone.
- **Per room:** give a fan a `room:` and it only supports: it switches off once the
  room is within `room_margin` (default 1 °C) of its target — below the scheduled
  setpoint when heating, above the cool target (`season.cool_target` or the room's
  own `cool_target`) when cooling — and back on `room_hysteresis` (0.5 °C) further
  out. It is also off while a window of the room is open. No room, or no
  temperature for it, and the fan simply follows the circulation.
- The status report shows a **Fans** line (how many are on, whether water
  circulates and which way, and which rooms' fans are off because the room is
  close to its target).
- Kill switch: `fan_control.act: false` logs "would publish …" without switching;
  `enabled: false` turns it off.

Rooms without a contact sensor (e.g. Julians, Wohnzimmer) are not window-controlled.

## Cooling

When `season.mode: auto` with `season.source: heatpump`, the season is what the
pump does: its main switch `hpmode` decides what is possible (`heating` = never
cooling, `off` = standby), and `hpoperatingstate` whether it heats or cools right
now. While the pump idles (summer mode, or the ~1 h changeover between heating and
cooling) the previous season holds; after `season.standby_after_hours` (24) of
idling the house goes to standby. In cooling the manager forces every
controllable thermostat fully open (valves let cold water through) and restores
the weekly schedule when heating resumes. Rooms in
manual override are left alone. Set `season.mode: cooling`/`heating`/`standby`
to force a mode regardless of the automatic decision.

## Standby (shoulder season — warm water only)

For mild weather where you want **neither heating nor cooling**, just domestic
hot water, the manager has a third season, **standby**: every controllable
thermostat is switched off, and warm-water production keeps running (the heat
pump makes it independently of the heating circuit, so it is unaffected).

Two ways to use it:

- **Manual:** set `season.mode: standby`. The house stays in standby until you
  change it back.
- **Automatic by outdoor temperature:** set `season.mode: auto` and
  `season.source: outdoor_temp`, then pick the boundaries:
  - below `season.standby_below` °C → **heating**
  - above `season.standby_above` °C → **cooling**
  - in between → **standby**

  `season.standby_hysteresis` (°C) widens the standby band slightly once the
  house is already in standby, so an outdoor temperature hovering right at a
  boundary doesn't flip the season back and forth.

On every season change the manager at once mails a reminder for the **manual
(non-controllable) valves** listed in `manual_thermostats` — OPEN for cooling,
CLOSE for standby, back to normal for heating — since a valve left open for
cooling would otherwise heat its room. (`season.manual_reminder_after_hours`
can delay it until the new season has held that long; the ~1 h idle gaps at a
pump changeover never trigger it, as the season holds through them.) a controllable valve that somehow isn't off is
flagged `standby_not_off` in the daily report.

> The heat pump itself has no "off" mode — it always allows heating, cooling, or
> both. Standby is therefore decided entirely by this software from the outdoor
> temperature; it switches the room valves off rather than changing anything on
> the pump. Warm water is never interrupted.
