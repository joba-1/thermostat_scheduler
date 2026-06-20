# Control model — what each setting does and how they correlate

This explains the layers that decide a radiator valve's (TRV's) state, in priority
order, and the per-type quirks. Use it when a room looks "wrong" (not open in
cooling, not off with a window open, stuck in an odd mode).

## The inputs

| Setting / signal | Where | What it means |
|---|---|---|
| **Season** (`season.mode`, `season.source`) | config + heat pump | `heating` or `cooling`. `auto` + `source: heatpump` derives it from the EMS-ESP `coolingon` signal. The season decides the *intended active state*. |
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
   state: cooling → `cooling_open`; heating → `schedule_mode` + schedule.
4. Otherwise the season-intended state is what `thermostat_scheduler.py` (no args)
   reinstates: cooling → open, heating → schedule.

So: *manual* beats *window* beats *season*. The heat pump only chooses the season;
it never directly drives a valve.

## Per-type mechanics

| Type | Rooms | Schedule (`schedule_mode`) | Cooling-open | Manual marker |
|---|---|---|---|---|
| VNTH-T2_v2 (TECH) | Bad OG, Esszimmer, Julians, WC OG, Wohnzimmer | `system_mode: heat`, `preset: schedule` | `preset: comfort`, `comfort_temperature: 34` | `preset == manual` |
| TR-M3Z (Tuya) | Waschküche | `system_mode: heat`, `preset: schedule` | `preset: comfort`, `comfort_temperature: 34` | `preset == manual` |
| TRVZB (SONOFF) | Caros, Schlafzimmer | `system_mode: auto` | `system_mode: heat`, `occupied_heating_setpoint: 34` | `system_mode == heat` |
| ME168_1 (AVATTO) | Arbeitszimmer, Dusche | `system_mode: auto` | `system_mode: heat`, `current_heating_setpoint: 34` | `system_mode == heat` |

A heating TRV opens its valve when the room is **below** setpoint; giving it a high
"open" setpoint (34 °C) while cold water is in the loop makes it open and cool.

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
