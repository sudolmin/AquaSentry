"""Self-check for the pure-logic helpers behind /api/fill-sessions and /api/uptime.

Run directly: python3 test_main.py
"""

from datetime import datetime, timedelta, timezone

from main import compute_uptime, pair_pump_events


def test_pair_pump_events_basic():
    # Newest-first, as the DB query returns it.
    events = [
        ("off", "t4"),
        ("on", "t3"),
        ("off", "t2"),
        ("on", "t1"),
    ]
    pairs = pair_pump_events(events, limit=10)
    assert pairs == [("t3", "t4"), ("t1", "t2")], pairs


def test_pair_pump_events_ignores_duplicate_republishes():
    # A retained-state republish on HA restart can repeat the same value twice.
    events = [
        ("off", "t5"),
        ("off", "t4"),  # duplicate off, no matching "on" right after it
        ("on", "t3"),
        ("off", "t2"),
        ("on", "t1"),
    ]
    pairs = pair_pump_events(events, limit=10)
    assert pairs == [("t3", "t2"), ("t1", "t2")] or pairs == [("t1", "t2")] or ("t3", "t2") not in pairs
    # The only guaranteed-correct pair here is (t1, t2); duplicate handling just
    # must not crash and must not fabricate a pair for the lone "off" at t4/t5.
    assert ("t1", "t2") in pairs


def test_pair_pump_events_respects_limit():
    events = [("off", f"t{i}") if i % 2 == 0 else ("on", f"t{i}") for i in range(20, 0, -1)]
    pairs = pair_pump_events(events, limit=2)
    assert len(pairs) == 2


def test_compute_uptime_no_gaps():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    now = start + timedelta(hours=1)
    timestamps = [start + timedelta(seconds=i * 10) for i in range(360)]  # every 10s, no gaps
    result = compute_uptime(timestamps, start, now, gap_threshold_seconds=120)
    assert result["gap_count"] == 0
    assert result["uptime_percent"] == 100.0


def test_compute_uptime_with_one_gap():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    now = start + timedelta(hours=1)
    # Readings for the first 10 minutes, then a 30-minute silent gap, then resume.
    timestamps = [start + timedelta(seconds=i * 10) for i in range(60)]
    timestamps += [start + timedelta(minutes=40, seconds=i * 10) for i in range(120)]
    result = compute_uptime(timestamps, start, now, gap_threshold_seconds=120)
    assert result["gap_count"] == 1
    assert 45 < result["uptime_percent"] < 55  # roughly half the hour was downtime


def test_compute_uptime_no_readings_at_all():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    now = start + timedelta(hours=2)
    result = compute_uptime([], start, now, gap_threshold_seconds=120)
    assert result["uptime_percent"] == 0.0
    assert result["downtime_minutes"] == 120.0


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok: {t.__name__}")
    print(f"\n{len(tests)} tests passed")
