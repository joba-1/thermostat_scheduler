"""Heat-pump fault surfacing: warning line while a fault is open, mail when it clears."""
import tempfile

import thermostat_monitor as tm

CFG = {
    'mqtt': {'base_topic': 'zigbee2mqtt'},
    'alerts': {'enabled': True},
    'season': {'mode': 'cooling', 'cool_target': 21},
    'thermostat_types': {'VNTH-T2_v2': {}},
    'thermostats': {
        'Bad OG': {'type': 'VNTH-T2_v2', 'sensors': {'temperature': 'Bad OG Luft'}},
    },
}


def make_mgr():
    cfg = {k: (dict(v) if isinstance(v, dict) else v) for k, v in CFG.items()}
    cfg['device_state_file'] = tempfile.mkdtemp() + '/devices.json'
    mgr = tm.Manager(cfg)
    sent = []
    mgr.alerter.enabled = True
    mgr.alerter._sender = lambda s, b, html=None: sent.append((s, b)) or True
    return mgr, sent


def hp(lastcode):
    return {'mode': 'cooling', 'active': False,
            'telemetry': {'outdoor': 30.0}, 'raw': {'lastcode': lastcode}}


def test_open_fault_shows_line_no_mail_then_clears_with_mail():
    mgr, sent = make_mgr()
    # fault opens -> tracked, surfaced as a warning line, but no mail yet
    mgr._apply_hp_alarm(hp(' --(5140) 22.06.2026 08:01 - now'), now_ts=1000)
    assert mgr._hp_alarm and mgr._hp_alarm['code'] == '5140'
    assert sent == []
    line = mgr._report_data(mode='cooling', hp=hp(' --(5140) 22.06.2026 08:01 - now'),
                            issues=[])['hp_alarm_line']
    assert line and '5140' in line and '08:01' in line

    # still open next pass -> no duplicate mail
    mgr._apply_hp_alarm(hp(' --(5140) 22.06.2026 08:01 - now'), now_ts=1100)
    assert sent == []

    # clears -> one mail announcing recovery, line gone
    mgr._apply_hp_alarm(hp(' --(5140) 22.06.2026 08:01 - 22.06.2026 08:07'), now_ts=2000)
    assert mgr._hp_alarm is None
    assert len(sent) == 1
    subj, body = sent[0]
    assert '5140' in subj and 'alarm' in subj.lower()
    assert '08:07' in body
    assert 'already ended' not in body            # we saw it open -> live clear

    # stays cleared -> no further mail (signature already notified)
    mgr._apply_hp_alarm(hp(' --(5140) 22.06.2026 08:01 - 22.06.2026 08:07'), now_ts=2100)
    assert len(sent) == 1


def test_old_already_ended_fault_is_mailed_once_with_note():
    mgr, sent = make_mgr()
    # first sight is an already-cleared fault -> still mailed, noting it had ended
    code = ' --(5140) 22.06.2026 08:01 - 22.06.2026 08:07'
    mgr._apply_hp_alarm(hp(code), now_ts=1000)
    assert mgr._hp_alarm is None            # nothing open -> no warning line
    assert len(sent) == 1
    assert 'already ended' in sent[0][1]
    assert mgr._report_data(mode='cooling', hp=hp('0H'), issues=[])['hp_alarm_line'] is None
    # same fault on later passes / after a restart -> never re-mailed
    mgr._apply_hp_alarm(hp(code), now_ts=2000)
    assert len(sent) == 1


def test_persisted_signature_survives_restart():
    mgr, sent = make_mgr()
    code = ' --(5140) 22.06.2026 08:01 - 22.06.2026 08:07'
    mgr._apply_hp_alarm(hp(code), now_ts=1000)
    assert len(sent) == 1
    mgr._save_device_state()
    # a fresh Manager on the same state file must not re-mail the same fault
    cfg = dict(mgr.cfg)
    mgr2 = tm.Manager(cfg)
    sent2 = []
    mgr2.alerter._sender = lambda s, b, html=None: sent2.append((s, b)) or True
    assert mgr2._hp_alarm_notified == '5140@22.06.2026 08:01'
    mgr2._apply_hp_alarm(hp(code), now_ts=2000)
    assert sent2 == []
