"""Device identity: ieee anchor, friendly-name display, HA entity resolution."""
import devices


SAMPLE = [
    {'ieee_address': '0xa4c138251f74d9ed', 'friendly_name': 'Schlafzimmer Fenster'},
    {'ieee_address': '0xc09b9efffeaf4972', 'friendly_name': 'Schlafzimmer Thermostat'},
    {'ieee_address': '0x0000000000000000', 'friendly_name': 'Coordinator',
     'type': 'Coordinator'},
]


def test_parse_ref_string_map_and_ieee():
    assert devices.parse_ref('Schlafzimmer Fenster') == (None, 'Schlafzimmer Fenster')
    assert devices.parse_ref('0xABC123') == ('0xabc123', None)
    assert devices.parse_ref({'ieee': '0xA4C1', 'name': 'X'}) == ('0xa4c1', 'X')
    assert devices.parse_ref({'name': 'Y'}) == (None, 'Y')


def test_registry_maps_and_skips_coordinator():
    reg = devices.Registry(SAMPLE)
    assert len(reg) == 2                       # coordinator skipped
    assert reg.name_of('0xa4c138251f74d9ed') == 'Schlafzimmer Fenster'
    assert reg.ieee_of('Schlafzimmer Thermostat') == '0xc09b9efffeaf4972'
    assert reg.name_of('0xdead') is None


def test_registry_update_reports_renames():
    reg = devices.Registry(SAMPLE)
    changed = reg.update([{'ieee_address': '0xa4c138251f74d9ed',
                           'friendly_name': 'Schlafzimmer Fenster neu'}])
    assert changed == {'0xa4c138251f74d9ed'}
    assert reg.name_of('0xa4c138251f74d9ed') == 'Schlafzimmer Fenster neu'
    assert reg.ieee_of('Schlafzimmer Fenster neu') == '0xa4c138251f74d9ed'
    assert reg.ieee_of('Schlafzimmer Fenster') is None        # old name dropped
    # re-publishing the current state reports nothing
    assert reg.update([{'ieee_address': '0xa4c138251f74d9ed',
                        'friendly_name': 'Schlafzimmer Fenster neu'}]) == set()


def test_resolve_prefers_ieee_then_registry_then_name_fallback():
    reg = devices.Registry(SAMPLE)
    # ieee given -> friendly resolved from registry, label is the config name
    ieee, friendly, label = devices.resolve(
        {'ieee': '0xa4c138251f74d9ed', 'name': 'Schlafzimmer Fenster'}, reg)
    assert (ieee, friendly, label) == (
        '0xa4c138251f74d9ed', 'Schlafzimmer Fenster', 'Schlafzimmer Fenster')
    # name only -> ieee looked up from registry
    assert devices.resolve('Schlafzimmer Thermostat', reg)[0] == '0xc09b9efffeaf4972'
    # no registry at all -> falls back to the name for topic + label (today's behaviour)
    assert devices.resolve('Bad OG Luft', None) == (None, 'Bad OG Luft', 'Bad OG Luft')


def test_ha_entity_candidates_slug_then_ieee():
    cands = devices.ha_entity_candidates(
        '0xa4c138251f74d9ed', 'Schlafzimmer Fenster', 'contact')
    assert cands == ['schlafzimmer_fenster_contact', '0xa4c138251f74d9ed_contact']
    # no friendly -> ieee form only; no ieee -> slug only
    assert devices.ha_entity_candidates('0xabc', None, 'temperature') == \
        ['0xabc_temperature']
    assert devices.ha_entity_candidates(None, 'Wohnzimmer Luft', 'temperature') == \
        ['wohnzimmer_luft_temperature']


def test_slugify_umlauts():
    assert devices.slugify('Waschküche') == 'waschkuche'
    assert devices.slugify('Arbeitszimmer Fenster') == 'arbeitszimmer_fenster'
