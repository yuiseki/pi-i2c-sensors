"""Tests for the position history, and for the last-fix file the map draws.

This tool had lived only on pi5-deck's card, which is the arrangement that lost
pi4-d-hdmi's work when its card died. Bringing it into the repo without tests
would keep the part that actually matters unexamined: /dev/shm/pi-gps cannot
answer "where was I last, and when" -- it keeps republishing the last known
position with fix=0 and carries no timestamp -- so the map's own blue marker
comes from here, not from there.
"""
import datetime
import importlib.machinery
import importlib.util
import os
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(HERE, "..", "bin", "pi-gps-track")


def load(**env):
    """A fresh module with the paths pointed at a scratch directory.

    The paths are module-level constants read from the environment at import,
    so a test that only patches os.environ afterwards would exercise nothing.
    """
    old = {k: os.environ.get(k) for k in env}
    os.environ.update({k: str(v) for k, v in env.items()})
    try:
        loader = importlib.machinery.SourceFileLoader("pi_gps_track", TOOL)
        spec = importlib.util.spec_from_loader("pi_gps_track", loader)
        mod = importlib.util.module_from_spec(spec)
        loader.exec_module(mod)
        return mod
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


M = load()
NOW = 1787634000.0
TOKYO = (35.725783, 139.790774)


def sample(lat=TOKYO[0], lon=TOKYO[1], fix=1, used=6, view=14):
    return {"lat": lat, "lon": lon, "fix": fix,
            "sats_used": used, "sats_in_view": view}


class ParseTest(unittest.TestCase):
    def test_the_five_fields_pi_gps_publishes(self):
        s = M.parse_shm("35.725783 139.790774 1 6 14\n")
        self.assertEqual((s["lat"], s["lon"], s["fix"]), (35.725783, 139.790774, 1))

    def test_anything_else_is_not_a_sample(self):
        for bad in ("", "35.7 139.7", "35.7 139.7 1 6 14 extra", "a b c d e"):
            self.assertIsNone(M.parse_shm(bad), repr(bad))


class RecordTest(unittest.TestCase):
    def test_a_position_without_a_fix_is_never_recorded(self):
        """pi-gps republishes the last known position with fix=0. Writing that
        would invent a track the receiver never saw."""
        self.assertIsNone(
            M.should_record(sample(fix=0), None, 0, NOW, 10, 300))

    def test_the_first_fix_is_always_a_point(self):
        self.assertEqual(
            M.should_record(sample(), None, 0, NOW, 10, 300), "first")

    def test_standing_still_writes_nothing(self):
        """Otherwise a deck on a desk writes an identical line every second."""
        last = sample()
        self.assertIsNone(
            M.should_record(sample(), last, NOW - 5, NOW, 10, 300))

    def test_moving_far_enough_writes(self):
        last = sample()
        moved = sample(lat=TOKYO[0] + 0.0002)          # ~22 m
        self.assertEqual(
            M.should_record(moved, last, NOW - 5, NOW, 10, 300), "move")

    def test_a_stationary_receiver_still_marks_the_time(self):
        last = sample()
        self.assertEqual(
            M.should_record(sample(), last, NOW - 301, NOW, 10, 300),
            "heartbeat")


class FreshnessTest(unittest.TestCase):
    def test_a_source_older_than_the_window_is_dead(self):
        self.assertTrue(M.is_fresh(NOW - 15, NOW, 15))
        self.assertFalse(M.is_fresh(NOW - 16, NOW, 15))

    def test_a_stale_file_yields_no_sample(self):
        tmp = tempfile.mkdtemp(prefix="gps-track.")
        shm = os.path.join(tmp, "pi-gps")
        with open(shm, "w") as fh:
            fh.write("35.725783 139.790774 1 6 14\n")
        os.utime(shm, (NOW - 60, NOW - 60))
        m = load(PI_GPS_SHM=shm, PI_GPS_TRACK_STALE=15)
        self.assertIsNone(m.read_source(NOW))
        os.utime(shm, (NOW - 1, NOW - 1))
        self.assertIsNotNone(m.read_source(NOW))

    def test_a_missing_file_is_not_a_crash(self):
        m = load(PI_GPS_SHM="/nonexistent/pi-gps")
        self.assertIsNone(m.read_source(NOW))


class LastFixTest(unittest.TestCase):
    """The file the map draws this host's own marker from."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gps-lastfix.")
        self.lastfix = os.path.join(self.tmp, "pi-gps-lastfix")
        self.log = os.path.join(self.tmp, "track.log")

    def test_it_carries_a_timestamp_which_pi_gps_does_not(self):
        m = load(PI_GPS_LASTFIX=self.lastfix)
        m.publish_lastfix(TOKYO[0], TOKYO[1], NOW)
        lat, lon, when = open(self.lastfix).read().split()
        self.assertAlmostEqual(float(lat), TOKYO[0], places=6)
        self.assertEqual(int(when), int(NOW))

    def test_a_reboot_recovers_the_last_fix_from_the_track(self):
        """tmpfs is empty after a reboot; the track log is not. Without this the
        map has no own-marker until the receiver fixes again."""
        iso = datetime.datetime.fromtimestamp(NOW).astimezone().isoformat(
            timespec="seconds")
        with open(self.log, "w") as fh:
            fh.write("%s\t35.700000\t139.700000\t1\t6\t14\tfirst\n" % iso)
            fh.write("%s\t%.6f\t%.6f\t1\t6\t14\tmove\n" % (iso, *TOKYO))
        m = load(PI_GPS_LASTFIX=self.lastfix, PI_GPS_TRACK_LOG=self.log)
        m.seed_lastfix_from_track()
        lat, lon, _ = open(self.lastfix).read().split()
        self.assertAlmostEqual(float(lat), TOKYO[0], places=6,
                               msg="the tail of the log, not its head")

    def test_seeding_never_overwrites_a_live_fix(self):
        with open(self.lastfix, "w") as fh:
            fh.write("1.000000 2.000000 %d\n" % int(NOW))
        with open(self.log, "w") as fh:
            fh.write("2026-08-27T00:00:00+09:00\t9.9\t9.9\t1\t6\t14\tfirst\n")
        m = load(PI_GPS_LASTFIX=self.lastfix, PI_GPS_TRACK_LOG=self.log)
        m.seed_lastfix_from_track()
        self.assertTrue(open(self.lastfix).read().startswith("1.000000"))

    def test_a_damaged_track_is_not_a_crash(self):
        with open(self.log, "w") as fh:
            fh.write("this is not a track line\n")
        m = load(PI_GPS_LASTFIX=self.lastfix, PI_GPS_TRACK_LOG=self.log)
        m.seed_lastfix_from_track()
        self.assertFalse(os.path.exists(self.lastfix))

    def test_no_track_at_all_is_not_a_crash(self):
        m = load(PI_GPS_LASTFIX=self.lastfix,
                 PI_GPS_TRACK_LOG=os.path.join(self.tmp, "nope.log"))
        m.seed_lastfix_from_track()
        self.assertFalse(os.path.exists(self.lastfix))


class FormatTest(unittest.TestCase):
    def test_the_line_is_tab_separated_with_seven_fields(self):
        line = M.format_line("2026-08-27T08:00:00+09:00", sample(), "move")
        parts = line.split("\t")
        self.assertEqual(len(parts), 7)
        self.assertEqual(parts[-1], "move")
        self.assertEqual(parts[1], "35.725783")


if __name__ == "__main__":
    unittest.main()
