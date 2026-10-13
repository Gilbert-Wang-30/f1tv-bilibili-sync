"""Sync calibrations: which one a new session starts from."""

from fone.store import Store

CHANNEL = "胡闹P房"


def production(tmp_path) -> Store:
    """The calibrations on 2026-10-09. The epoch one had been the channel default for days."""
    store = Store(tmp_path / "fone.sqlite3")
    for row in (("1000011024", CHANNEL, "multiviewer", 40.8, 1791071846.44),
                ("1000011022", CHANNEL, "epoch", -0.2, 1791096748.93),
                ("1000010389", CHANNEL, "multiviewer", 44.94, 1791565514.34)):
        store.query("INSERT INTO content_delay VALUES (?, ?, ?, ?, ?)", row)
    store.query("INSERT INTO channel_delay VALUES (?, ?, ?)", (CHANNEL, 44.94, 1791565514.34))
    return store


def test_new_sessions_start_from_the_latest_calibration_on_a_comparable_clock(tmp_path):
    store = production(tmp_path)
    assert store.family_delay(CHANNEL, "multiviewer") == 44.94
    assert store.family_delay(CHANNEL, "preroll") == 44.94
    assert store.family_delay(CHANNEL, "epoch") == -0.2
    assert store.family_delay(CHANNEL, "edge") is None and store.family_delay(CHANNEL, "manual") is None
    assert store.family_delay("someone else", "multiviewer") is None


def test_an_epoch_calibration_never_reaches_a_replay(tmp_path):
    store = Store(tmp_path / "fone.sqlite3")
    store.set_delay("1000011022", CHANNEL, "epoch", -0.2)
    assert store.family_delay(CHANNEL, "multiviewer") is None and store.family_delay(CHANNEL, "preroll") is None
    store.set_delay("1000011024", CHANNEL, "multiviewer", 40.8)
    store.set_delay("1000011023", CHANNEL, "epoch", 0.1)  # newer, on the other footing
    assert store.family_delay(CHANNEL, "multiviewer") == 40.8 and store.family_delay(CHANNEL, "epoch") == 0.1
    store.set_delay("1000011025", CHANNEL, "preroll", 43.0)
    assert store.family_delay(CHANNEL, "multiviewer") == 43.0 and store.family_delay(CHANNEL, "epoch") == 0.1


def test_channel_delay_is_kept_for_older_servers_from_exact_replays_only(tmp_path):
    store = production(tmp_path)

    def channel_delay():
        return store.query("SELECT delay FROM channel_delay WHERE channel = ?", (CHANNEL,))[0][0]

    store.set_delay("1000011200", CHANNEL, "epoch", 0.3)
    store.set_delay("1000011201", CHANNEL, "edge", 12.0)
    store.set_delay("1000011202", CHANNEL, "preroll", 43.0)  # an estimated anchor
    assert channel_delay() == 44.94
    store.set_delay("1000011203", CHANNEL, "multiviewer", 45.5)
    assert channel_delay() == 45.5
    # ...and nothing here reads it.
    store.query("UPDATE channel_delay SET delay = -0.2")
    assert store.family_delay(CHANNEL, "multiviewer") == 45.5
