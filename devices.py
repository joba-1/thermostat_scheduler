"""Device identity: anchor on the zigbee ieee, display the friendly name.

Friendly names are mutable and differ between zigbee2mqtt and Home Assistant; the
**ieee address** (`0x…`) is the one stable id shared by both. z2m's retained
`zigbee2mqtt/bridge/devices` topic is the authoritative `ieee ↔ friendly_name`
registry. This module turns config device references into a stable `(ieee, friendly,
label)` triple and derives HA InfluxDB entity-id candidates from it.

Safety contract: every resolution step falls back to the configured `name`, so if the
registry is missing or late, or an ieee is unknown, callers behave exactly as they did
when everything was friendly-name keyed. Identity-by-ieee is an *additional* anchor,
never a hard dependency — important because the daemon drives real valves.
"""
import unicodedata

_TRANSLIT = {'ä': 'a', 'ö': 'o', 'ü': 'u', 'ß': 'ss',
             'á': 'a', 'à': 'a', 'é': 'e', 'è': 'e', 'í': 'i', 'ó': 'o', 'ú': 'u'}


def slugify(name):
    """Home Assistant entity-id slug for a friendly name: lowercase, transliterate
    umlauts/accents to ASCII, collapse non-alphanumerics to single underscores.
    Verified live: 'Waschküche' -> 'waschkuche', 'Arbeitszimmer Fenster' ->
    'arbeitszimmer_fenster'."""
    s = (name or '').strip().lower()
    s = ''.join(_TRANSLIT.get(ch, ch) for ch in s)
    s = unicodedata.normalize('NFKD', s).encode('ascii', 'ignore').decode('ascii')
    out, prev_us = [], False
    for ch in s:
        if ch.isalnum():
            out.append(ch)
            prev_us = False
        elif not prev_us:
            out.append('_')
            prev_us = True
    return ''.join(out).strip('_')


def is_ieee(s):
    """True for a zigbee ieee address like 0xa4c138251f74d9ed."""
    return isinstance(s, str) and s.lower().startswith('0x') and len(s) >= 6


def parse_ref(ref):
    """A device reference is either a plain string (legacy: the friendly name) or a
    map {ieee, name}. Returns (ieee|None, name|None)."""
    if isinstance(ref, dict):
        ieee = ref.get('ieee')
        return (ieee.lower() if isinstance(ieee, str) else None), ref.get('name')
    if isinstance(ref, str):
        # a bare 0x… string is an ieee; anything else is a friendly name
        return (ref.lower(), None) if is_ieee(ref) else (None, ref)
    return None, None


class Registry:
    """ieee <-> friendly_name map built from a z2m bridge/devices payload."""

    def __init__(self, payload=None):
        self._name = {}       # ieee -> friendly_name
        self._ieee = {}       # friendly_name -> ieee
        if payload:
            self.update(payload)

    def update(self, payload):
        """Merge a bridge/devices payload (list of device dicts). Returns the set of
        ieees whose friendly_name changed (so the caller can re-subscribe topics)."""
        changed = set()
        for dev in payload or []:
            ieee = dev.get('ieee_address')
            fn = dev.get('friendly_name')
            if not ieee or not fn or dev.get('type') == 'Coordinator':
                continue
            ieee = ieee.lower()
            if self._name.get(ieee) != fn:
                old = self._name.get(ieee)
                if old is not None:
                    self._ieee.pop(old, None)
                self._name[ieee] = fn
                self._ieee[fn] = ieee
                changed.add(ieee)
        return changed

    def name_of(self, ieee):
        return self._name.get((ieee or '').lower()) if ieee else None

    def ieee_of(self, friendly):
        return self._ieee.get(friendly)

    def __len__(self):
        return len(self._name)


def resolve(ref, registry=None):
    """Resolve a config device reference to (ieee, friendly, label).

      ieee     stable identity     ref.ieee, else registry lookup by name, else None
      friendly MQTT topic key      registry.name_of(ieee), else ref.name  (fallback)
      label    display string      ref.name, else friendly, else ieee
    """
    ieee, name = parse_ref(ref)
    if not ieee and name and registry is not None:
        ieee = registry.ieee_of(name)
    friendly = (registry.name_of(ieee) if (ieee and registry is not None) else None) \
        or name or ieee
    label = name or friendly or ieee
    return ieee, friendly, label


def ha_entity_candidates(ieee, friendly, prop):
    """Ordered HA InfluxDB entity_id candidates for a device property.

    Tries the **named slug** first (`arbeitszimmer_fenster_contact`) then HA's
    **ieee-fallback** id (`0xa4c138251f74d9ed_contact`, used when HA never got a
    friendly name). Caller queries each in order and keeps the one with data.
    """
    cands = []
    if friendly:
        cands.append(f"{slugify(friendly)}_{prop}")
    if ieee:
        cands.append(f"{ieee.lower()}_{prop}")
    # de-dup, preserve order
    seen, out = set(), []
    for c in cands:
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out
