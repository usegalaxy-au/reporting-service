from datetime import datetime, timedelta

from analysis.backtest import (
    is_ignored, recidive_bans, repeat_offenders, simulate)

START = datetime(2026, 8, 1, 12, 0, 0)


def _events(ip, offsets):
    return [(ip, START + timedelta(seconds=s)) for s in offsets]


def test_bans_at_maxretry_within_findtime():
    bans = simulate(_events('a', [0, 10, 20]), 3, 60, 3600)
    assert len(bans) == 1
    ip, start, end, failures = bans[0]
    assert start == START + timedelta(seconds=20)
    assert end == start + timedelta(seconds=3600)
    assert failures == 3


def test_failures_outside_findtime_do_not_accumulate():
    assert simulate(_events('a', [0, 100, 200]), 3, 60, 3600) == []


def test_failures_during_ban_are_ignored_then_counting_restarts():
    # Banned at t=20 until t=80; t=30..70 are ignored; after expiry
    # three more failures are needed.
    offsets = [0, 10, 20, 30, 40, 70, 90, 91]
    bans = simulate(_events('a', offsets), 3, 60, 60)
    assert [b[1] for b in bans] == [START + timedelta(seconds=20)]
    bans = simulate(_events('a', offsets + [92]), 3, 60, 60)
    assert len(bans) == 2


def test_ips_are_counted_independently():
    events = sorted(_events('a', [0, 1]) + _events('b', [0, 1, 2]))
    bans = simulate(events, 3, 60, 60)
    assert [b[0] for b in bans] == ['b']


def test_ignoreip_networks():
    import ipaddress
    nets = [ipaddress.ip_network('127.0.0.0/8')]
    assert is_ignored('127.0.0.1', nets)
    assert not is_ignored('203.0.113.1', nets)
    assert not is_ignored('not-an-ip', nets)


def _ban(ip, day, jail='galaxy-scan'):
    start = START + timedelta(days=day)
    return (jail, ip, start, start + timedelta(hours=1), 3)


def test_repeat_offenders_need_bans_in_two_distinct_weeks():
    # 2026-08-01 is a Saturday. ISO weeks end on Sunday, so days 1 (Sun)
    # and 2 (Mon) fall in different weeks; days 2 and 3 in the same one.
    bans = [
        _ban('198.51.100.1', 1), _ban('198.51.100.1', 2),
        _ban('198.51.100.2', 2), _ban('198.51.100.2', 3),
        _ban('198.51.100.3', 5),
    ]
    rows = repeat_offenders(bans)
    assert [r['ip'] for r in rows] == ['198.51.100.1']
    assert rows[0]['weeks_banned'] == 2
    assert rows[0]['subnet24'] == '198.51.100.0/24'


def test_repeat_offenders_sorted_by_weeks_then_bans():
    bans = [_ban('a.b.c.1', d) for d in (0, 7, 14)]
    bans += [_ban('a.b.c.2', d, jail) for d in (0, 7)
             for jail in ('galaxy-scan', 'galaxy-malformed')]
    rows = repeat_offenders(bans)
    assert [r['ip'] for r in rows] == ['a.b.c.1', 'a.b.c.2']
    assert rows[1]['bans'] == 4
    assert rows[1]['jails'] == 'galaxy-malformed galaxy-scan'


def test_recidive_bans_after_repeated_bans_within_findtime():
    hour = 3600
    bans = [_ban('198.51.100.1', 0), _ban('198.51.100.1', 0.25),
            _ban('198.51.100.1', 0.5),          # 3 bans within a day
            _ban('198.51.100.2', 0), _ban('198.51.100.2', 2),
            _ban('198.51.100.2', 4)]            # 3 bans, days apart
    rec = recidive_bans(bans, maxretry=3, findtime=24 * hour,
                        bantime=7 * 24 * hour)
    assert [r[0] for r in rec] == ['198.51.100.1']
    ip, start, end, failures = rec[0]
    assert start == START + timedelta(days=0.5)
    assert end - start == timedelta(days=7)
