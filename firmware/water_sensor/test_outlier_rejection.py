"""Self-check for the outlier-rejection logic in water_sensor.ino's loop().

Mirrors the accept/reject decision in plain Python so it's runnable without
an Arduino toolchain. Run directly: python3 test_outlier_rejection.py
"""

MAX_JUMP_CM = 15.0
OUTLIER_PERSIST_COUNT = 3


def should_accept(readings):
    """Replays the firmware's decision for a sequence of raw distances.
    Returns the list of accepted (published) distances."""
    last_good = None
    streak = 0
    accepted = []
    for dist in readings:
        if last_good is not None and abs(dist - last_good) > MAX_JUMP_CM:
            streak += 1
            if streak < OUTLIER_PERSIST_COUNT:
                continue
        streak = 0
        last_good = dist
        accepted.append(dist)
    return accepted


def test_stable_readings_all_accepted():
    readings = [50.0, 50.2, 49.8, 50.1, 49.9]
    assert should_accept(readings) == readings


def test_single_spike_rejected():
    readings = [50.0, 51.0, 150.0, 49.5, 50.2]
    assert should_accept(readings) == [50.0, 51.0, 49.5, 50.2]


def test_persistent_jump_eventually_accepted():
    # Sensor physically moved: the new distance repeats for 3+ cycles in a row.
    # The first two post-jump readings are rejected (streak building to 3);
    # the third is the one that crosses OUTLIER_PERSIST_COUNT and gets accepted.
    readings = [50.0, 80.0, 81.0, 79.5, 80.2]
    accepted = should_accept(readings)
    assert accepted == [50.0, 79.5, 80.2], accepted


def test_first_reading_always_accepted():
    readings = [300.0]
    assert should_accept(readings) == [300.0]


def test_alternating_noise_stays_rejected():
    # Cable interference bouncing between two bad values, never persisting.
    readings = [50.0, 90.0, 50.1, 88.0, 49.9]
    assert should_accept(readings) == [50.0, 50.1, 49.9]


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok: {t.__name__}")
    print(f"\n{len(tests)} tests passed")
