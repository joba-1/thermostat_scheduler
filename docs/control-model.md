# Control model — what each setting does and how they correlate

This explains the layers that decide a radiator valve's (TRV's) state, in priority
order, and the per-type quirks. Use it when a room looks "wrong" (not open in
cooling, not off with a window open, stuck in an odd mode).

## The inputs

| Setting / signal | Where | What it means |
|---|---|---|
| **Season** (`season.mode`, `season.source`) | config + heat pump / outdoor temp | `heating`, `cooling`, or `standby`. `auto` derives it: `source: heatpump` → binary heat/cool from the EMS-ESP `coolingon` signal; `source: outdoor_temp` → 3-season heat/standby/cool from the outdoor temperature (below). The season decides the *intended active state*. |
| **Standby thresholds** (`season.standby_below`, `season.standby_above`, `season.standby_hysteresis`) | config | With `source: outdoor_temp`: heat below `standby_below`, cool above `standby_above`, **standby** (valves off, warm water only) in between. `standby_hysteresis` widens the standby band by that many °C on both sides *while already in standby*, so a reading hovering at a threshold doesn't flap the season. |
| **Schedule** (`day/night_hour`, `day/night_temperature`) | per room | The weekly heating program pushed to the device as `schedule_*` strings + the type's `schedule_mode`. Used in **heating**. |
| **cooling_open / cooling_restore** (`thermostat_types`) | per type | How to force a TRV fully open for cooling, and how to restore it. Used in **cooling**. |
| **Manual override** (`manual_marker`) | per type | The reported field/value that means *the user took control at the device*. Such rooms are left alone (comfort control suspended) and flagged as info. |
| **Window control** (`window_control`, `sensors.windows`) | per room | A contact opening → TRV `system_mode: off`; closing → restore the season-intended state. |
| **Built-in window detection** (`builtin_window_off`) | per type | The TRV's *own* (temperature-drop) window heuristic. Disabled on connect so only the monitor drives off/on. |

## Priority (what wins)

For each room, on every relevant event, the monitor resolves the state like this:

1. **Manual override** → do nothing (only refresh stored schedule/calibration).
   The user wins. Surfaced as an info note.
2. **Window open** (and not manual) → `system_mode: off`. Latched as
   "we turned it off" so only *we* restore it later (a hand-set off stays off).
   The laundry room only ventilates when dry (`humidity_guard`).
3. **Window closed** (and we had turned it off) → restore the **season-intended**
   state: cooling → `cooling_open`; heating → `schedule_mode` + schedule;
   standby → off (`off_signature`).
4. Otherwise the season-intended state is what the daemon (and
   `thermostat_scheduler.py` no-args) reinstates: cooling → open, heating →
   schedule, standby → off.

So: *manual* beats *window* beats *season*. The heat pump only chooses the season
(when `source: heatpump`); it never directly drives a valve.

**Standby** is the shoulder-season state: outdoor temperature sits between
`season.standby_below` and `season.standby_above`, so neither heating nor cooling
is wanted. Every controllable valve is switched off (the same `off_signature` a
window-open uses), and **domestic hot water keeps running** — it is produced by
the heat pump independently of the heating circuit (`dhw.*`, not `hc1`), so
switching the circuit off doesn't touch it. A non-manual valve that isn't off in
standby is flagged `standby_not_off` (the mirror of `cooling_not_open`).

## Per-type mechanics

| Type | Rooms | Schedule (`schedule_mode`) | Cooling-open | Manual marker |
|---|---|---|---|---|
| VNTH-T2_v2 (TECH) | Bad OG, Esszimmer, Julians, WC OG, Wohnzimmer | `system_mode: heat`, `preset: schedule` | `preset: comfort`, `comfort_temperature: 34` | `preset == manual` |
| TR-M3Z (Tuya) | Waschküche | `system_mode: heat`, `preset: schedule` | `preset: comfort`, `comfort_temperature: 34` | `preset == manual` |
| TRVZB (SONOFF) | Caros, Schlafzimmer | `system_mode: auto` | `system_mode: heat`, `occupied_heating_setpoint: 34` | `system_mode == heat` |
| ME168_1 (AVATTO) | Arbeitszimmer, Dusche | `system_mode: auto` | `system_mode: heat`, `current_heating_setpoint: 34` | `system_mode == heat` |

A heating TRV opens its valve when the room is **below** setpoint; giving it a high
"open" setpoint (34 °C) while cold water is in the loop makes it open and cool.

## Manual is detected by exclusion (signature model)

We **cannot** read who set a state — it can change from the device buttons, the
z2m web UI, a raw MQTT `/set`, this software, or (formerly) HA, and none of those
leave a provenance tag. So rather than detect "manual" positively, we **define the
exact state-combos we produce** (`cooling.classify_state`) and treat anything else
as manual:

| Classified as | Matches |
|---|---|
| `open` | the type's `cooling_open` (e.g. heat / preset comfort **+ setpoint 34**) |
| `off` | the type's `off_signature` — **our** window-off (see below) |
| `schedule` | the weekly-schedule mode (preset schedule / system_mode auto) |
| `manual` | none of the above → leave it alone, surface as info |

So `is_manual_override` = "classified manual", `is_open` = "classified open".
A stuck state like `heat/21.5` is manual (not one of ours); the defined recovery
is to push one of our signatures (e.g. the no-arg scheduler pushes open/schedule).

## "Our off" vs a user's off (`off_signature`)

Window-control switches a TRV off, and must later restore **only** the rooms *it*
turned off — not a radiator the user switched off by hand. Since a plain
`system_mode: off` is indistinguishable, our off is a **signature combo**:

| Type | `off_signature` | `off_clear` (undo on restore) |
|---|---|---|
| VNTH-T2_v2, TR-M3Z, ME168/ME167 | `system_mode: off` + `frost_protection: ON` | `frost_protection: OFF` |
| TRVZB | *none* — latch-based (see below) | — |

`is_our_off` matches this signature. On window close we restore if the device
still shows our off **or** we still have it latched and it is off; a user's plain
off (`frost_protection` not ON) is left alone. The signature is **self-describing
on the device**, so it survives a restart / lost latch.

**TRVZB has no usable off-signature**: writing `occupied_heating_setpoint` flips
`system_mode` back to `heat` (the setpoint write implies heat mode), so a setpoint
sentinel won't hold, and there's no frost_protection boolean. So TRVZB window-off
is a plain `system_mode: off` and "ours" is tracked **only by the `window_off`
latch** (persisted). The status report shows `off (window)` for any latched room
regardless of how its plain-off state would otherwise classify.

Window control also **reconciles every eval pass** (not just on contact events),
so a room left in a stale state after a restart, or one whose contact rarely
reports, self-heals within an eval interval.

## The AVATTO/SONOFF ambiguity (important)

On **ME168/ME167 (AVATTO)** and **TRVZB (SONOFF)** the cooling-open mode and the
manual-override marker are *the same field value*: `system_mode == heat`. The only
way to tell them apart is the **setpoint**:

- `system_mode: heat` **+ setpoint 34** → this is **our cooling-open** (recognised
  as "open", not manual).
- `system_mode: heat` **+ any other setpoint** → treated as **manual override**, so
  the monitor (and window control) leave the room alone.

Consequence: if such a TRV ends up at `system_mode: heat` with a *normal* setpoint
(e.g. 21.5, left over from a previous `climate.turn_on`), the monitor reads it as
manual and will neither open it for cooling nor switch it off for a window. In
cooling that means the valve stays closed (`idle`) and the room isn't cooled.

**Fix:** push the exact cooling-open payload (`system_mode: heat`,
`current_heating_setpoint: 34`) — e.g. `thermostat_scheduler.py` (no args) in
cooling, or a one-off `mosquitto_pub … /set -m '{"system_mode":"heat","current_heating_setpoint":34}'`.
Once it matches `cooling_open` exactly it is recognised as open, and window control
works for it again. (Worked example: Arbeitszimmer on 2026-06-20 was stuck at
`heat/21.5`, read as manual, so a window-close didn't reopen it; pushing `heat/34`
unstuck it.)

VNTH/TR-M3Z don't have this problem: their manual marker is `preset == manual`,
which is distinct from the cooling-open `preset == comfort`.
