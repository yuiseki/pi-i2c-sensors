"""Tests for publishing a Meshtastic node's fix as this host's position.

The deck usually has no GPS mouse and usually does have a node plugged into it.
The node is on the deck, so its fix is the deck's fix. What has to be right is
not the reading -- the node does that -- but the three rules that decide when
NOT to publish, because a confidently wrong position is worse than none.
"""
import os
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(HERE, "..", "bin", "pi-gps-mesh")

sys.path.insert(0, os.path.join(HERE, "..", "bin"))
import importlib.machinery
import importlib.util


def load():
    loader = importlib.machinery.SourceFileLoader("pi_gps_mesh", TOOL)
    spec = importlib.util.spec_from_loader("pi_gps_mesh", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


M = load()
NOW = 1787633826.0


def pos(**kw):
    base = {"locationSource": "LOC_INTERNAL", "latitude": 35.7257705,
            "longitude": 139.7909393, "altitude": 18, "time": NOW - 5}
    base.update(kw)
    return base


class UsableTest(unittest.TestCase):
    def test_a_live_fix_from_the_nodes_own_receiver(self):
        got = M.usable(pos(), 120, now=NOW)
        self.assertIsNotNone(got)
        lat, lon, sats = got
        self.assertAlmostEqual(lat, 35.7257705)
        self.assertAlmostEqual(lon, 139.7909393)

    def test_a_manual_position_is_not_a_fix(self):
        # c6l-a has a hand-set position. Publishing that as this host's GPS
        # would put the deck confidently somewhere it is not.
        self.assertIsNone(M.usable(pos(locationSource="LOC_MANUAL"), 120, now=NOW))

    def test_a_position_heard_over_the_mesh_is_not_ours(self):
        self.assertIsNone(M.usable(pos(locationSource="LOC_EXTERNAL"), 120, now=NOW))

    def test_a_stale_fix_is_silence(self):
        # Not published, so the file goes stale and consumers read "no GPS" --
        # the contract pi-gps already has.
        self.assertIsNone(M.usable(pos(time=NOW - 600), 120, now=NOW))
        self.assertIsNotNone(M.usable(pos(time=NOW - 119), 120, now=NOW))

    def test_a_position_with_no_timestamp_is_not_trusted(self):
        # Age cannot be judged, so it cannot be shown to be current.
        self.assertIsNone(M.usable(pos(time=0), 120, now=NOW))
        self.assertIsNone(M.usable(pos(time=None), 120, now=NOW))

    def test_null_island_is_not_a_fix(self):
        # 0,0 is what a node reports before it has one.
        self.assertIsNone(M.usable(pos(latitude=0, longitude=0), 120, now=NOW))

    def test_a_node_clock_running_ahead_is_not_stale(self):
        # The node sets its own clock from GPS and can be a second ahead of us.
        # A negative age must not wrap into a rejection.
        self.assertIsNotNone(M.usable(pos(time=NOW + 3), 120, now=NOW))

    def test_missing_position_is_handled(self):
        self.assertIsNone(M.usable(None, 120, now=NOW))
        self.assertIsNone(M.usable({}, 120, now=NOW))

    def test_sats_default_to_zero_when_the_node_does_not_send_them(self):
        self.assertEqual(M.usable(pos(), 120, now=NOW)[2], 0)
        self.assertEqual(M.usable(pos(satsInView=9), 120, now=NOW)[2], 9)


class PublishTest(unittest.TestCase):
    def test_the_line_matches_what_pi_gps_writes(self):
        # Same five fields, so no consumer has to know which receiver spoke.
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "pi-gps")
            M.publish(out, 35.7257705, 139.7909393, 7)
            line = open(out).read().strip()
        parts = line.split()
        self.assertEqual(len(parts), 5)
        self.assertEqual(parts[0], "35.725771")
        self.assertEqual(parts[2], "1", "a live internal fix is fix=1")
        self.assertEqual(parts[3], "7")

    def test_the_write_is_atomic(self):
        # A consumer must never read half a line.
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "pi-gps")
            M.publish(out, 1.0, 2.0, 3)
            self.assertFalse(os.path.exists(out + ".tmp"))


def run_once(env_extra, byid_files=()):
    """Run the tool once against a fake /dev/serial/by-id."""
    tmp = tempfile.mkdtemp(prefix="pi-gps-mesh.")
    byid = os.path.join(tmp, "by-id")
    os.makedirs(byid)
    for name in byid_files:
        open(os.path.join(byid, name), "w").close()
    env = dict(os.environ)
    env.update({"PI_GPS_MESH_BYID": byid,
                "PI_GPS_MESH_OUT": os.path.join(tmp, "pi-gps")})
    env.update(env_extra)
    r = subprocess.run([sys.executable, TOOL, "--once"],
                       capture_output=True, text=True, env=env, timeout=60)
    return r, os.path.join(tmp, "pi-gps")


class LivenessTest(unittest.TestCase):
    """The file's mtime is the liveness signal, not the fix's age.

    The map calls /dev/shm/pi-gps stale after two seconds and pi-gps satisfies
    that by rewriting on every GGA at 1 Hz, holding the last position. The first
    version of this tool published only when it polled, once every eleven
    seconds, so the map's GPS indicator sat grey for nine seconds out of every
    eleven and flapped. The rates have to be separate.
    """

    def test_the_publish_rate_defaults_faster_than_the_map_calls_it_stale(self):
        ap = [a for a in M.build_parser()._actions if a.dest == "publish_every"]
        self.assertEqual(len(ap), 1)
        self.assertLessEqual(ap[0].default, 2.0,
                             "the map's freshness window is 2s")

    def test_the_poll_rate_is_the_slow_one(self):
        acts = {a.dest: a for a in M.build_parser()._actions}
        poll, pub = acts["interval"], acts["publish_every"]
        self.assertGreater(poll.default, pub.default,
                           "reading the node is the expensive half")

    def test_publishing_refreshes_the_mtime_even_when_nothing_moved(self):
        # A stationary deck must still look alive.
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "pi-gps")
            M.publish(out, 35.0, 139.0, 0)
            first = os.stat(out).st_mtime_ns
            time.sleep(0.01)
            M.publish(out, 35.0, 139.0, 0)
            self.assertGreater(os.stat(out).st_mtime_ns, first)


class StandDownTest(unittest.TestCase):
    USB_GPS = "usb-u-blox_AG_-_www.u-blox.com_u-blox_7_-_GPS_GNSS_Receiver-if00"
    NODE = "usb-Espressif_USB_JTAG_serial_debug_unit_20:6E:F1:15:B8:F4-if00"

    def test_a_usb_receiver_wins(self):
        # One writer at a time. A dedicated receiver outranks a side effect of
        # the radio, and pi-gps is already the owner of the file.
        r, out = run_once({}, [self.USB_GPS, self.NODE])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("standing down", r.stdout)
        self.assertFalse(os.path.exists(out))

    def test_nothing_attached_publishes_nothing(self):
        r, out = run_once({}, [])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("no Meshtastic node", r.stdout)
        self.assertFalse(os.path.exists(out))

    def test_a_node_alone_is_the_case_this_exists_for(self):
        # No meshtastic package reachable in the test env, so it gets as far as
        # trying to read and then declines to publish. What matters here is
        # that it chose the node rather than standing down.
        r, out = run_once({}, [self.NODE])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("standing down", r.stdout)
        self.assertNotIn("no Meshtastic node", r.stdout)
        self.assertFalse(os.path.exists(out))


if __name__ == "__main__":
    unittest.main()
