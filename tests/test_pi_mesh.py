"""Tests for putting the mesh on the map.

The map already knew how to draw Meshtastic nodes; what was missing was the
process that tells it where they are. That producer died with pi4-d-hdmi's card
and was rebuilt here, together with the position half, because there is one
serial port and two processes polling it would fight.
"""
import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(HERE, "..", "bin", "pi-mesh")


def load():
    loader = importlib.machinery.SourceFileLoader("pi_mesh", TOOL)
    spec = importlib.util.spec_from_loader("pi_mesh", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


M = load()
NOW = 1787634000.0
MINE = "!f115b8f4"


def node(nid, lat=None, lon=None, t=None, short="x", src="LOC_INTERNAL",
         last_heard=None):
    n = {"user": {"id": nid, "shortName": short, "longName": short + "-long"}}
    if lat is not None:
        n["position"] = {"latitude": lat, "longitude": lon,
                         "locationSource": src, "time": t}
    if last_heard:
        n["lastHeard"] = last_heard
    return n


NODES = {
    MINE: node(MINE, 35.7257, 139.7909, NOW - 5, "c6lb"),
    "!f115b530": node("!f115b530", 35.7258, 139.7910, NOW - 60, "c6la",
                      src="LOC_MANUAL"),
    "!8390dd3b": node("!8390dd3b", 35.7250, 139.7900, NOW - 4000, "44f4"),
    "!ef6b27f3": node("!ef6b27f3", short="27f3"),          # heard, no position
}


class MarkerTest(unittest.TestCase):
    def lines(self, nodes=None, mine=MINE):
        return M.marker_lines(nodes if nodes is not None else NODES, mine)

    def test_our_own_node_is_not_a_marker(self):
        # The map draws this host separately, in its own colour, from
        # pi-gps-lastfix. Two markers on one spot is just a smudge.
        self.assertNotIn("f115b8f4", self.lines())

    def test_every_other_positioned_node_is(self):
        ids = [l.split()[0] for l in self.lines().splitlines()]
        self.assertEqual(sorted(ids), ["8390dd3b", "f115b530"])

    def test_a_node_with_no_position_is_skipped(self):
        self.assertNotIn("ef6b27f3", self.lines())

    def test_a_manual_position_is_still_drawn(self):
        # Unlike this host's own fix, where a hand-set coordinate would be a
        # lie about where the deck is, a hand-set node position is simply where
        # that node says it is. Drawing it is the honest thing.
        self.assertIn("f115b530", self.lines())

    def test_an_old_position_is_published_with_its_real_age(self):
        # The map fades rather than drops, so a node last heard hours ago is
        # still worth a faded marker. Publishing "now" would make it look live.
        line = [l for l in self.lines().splitlines() if l.startswith("8390dd3b")][0]
        self.assertEqual(int(line.split()[3]), int(NOW - 4000))

    def test_the_line_has_exactly_five_fields(self):
        # The map parses with a stream extractor; a sixth field or a space in
        # the name silently loses everything after it.
        for line in self.lines().splitlines():
            self.assertEqual(len(line.split()), 5, line)

    def test_a_name_with_spaces_becomes_one_field(self):
        n = {"!aa": node("!aa", 1.0, 2.0, NOW, "Meshtastic b8f4")}
        self.assertTrue(M.marker_lines(n, MINE).endswith(" Meshtastic_b8f4"))

    def test_a_nameless_node_falls_back_to_its_id(self):
        n = {"!aa": {"position": {"latitude": 1.0, "longitude": 2.0,
                                  "time": NOW}}}
        self.assertTrue(M.marker_lines(n, MINE).endswith(" aa"))

    def test_null_island_is_not_a_position(self):
        n = {"!aa": node("!aa", 0, 0, NOW)}
        self.assertEqual(M.marker_lines(n, MINE), "")

    def test_the_id_never_looks_like_a_poi(self):
        # The map colours by id prefix: "poi<digit>-" is a search result. A
        # node id colliding with that would draw a contact as a cafe.
        for line in self.lines().splitlines():
            self.assertFalse(line.split()[0].startswith("poi"), line)


class OwnPositionTest(unittest.TestCase):
    def test_a_live_internal_fix(self):
        got = M.own_position(NODES, MINE, 120, now=NOW)
        self.assertIsNotNone(got)
        self.assertAlmostEqual(got[0], 35.7257)

    def test_a_manual_position_is_not_this_hosts_fix(self):
        got = M.own_position(NODES, "!f115b530", 120, now=NOW)
        self.assertIsNone(got)

    def test_a_stale_fix_is_silence(self):
        self.assertIsNone(M.own_position(NODES, "!8390dd3b", 120, now=NOW))

    def test_an_unknown_node_id(self):
        self.assertIsNone(M.own_position(NODES, "!nope", 120, now=NOW))
        self.assertIsNone(M.own_position(NODES, None, 120, now=NOW))


class RatesTest(unittest.TestCase):
    """The file's mtime means "a publisher is alive", not "this fix is new"."""

    def test_publishing_is_faster_than_the_map_calls_it_stale(self):
        acts = {a.dest: a for a in M.build_parser()._actions}
        self.assertLessEqual(acts["publish_every"].default, 2.0)
        self.assertGreater(acts["interval"].default,
                           acts["publish_every"].default)


def run_once(byid_files=(), extra=None, args=()):
    tmp = tempfile.mkdtemp(prefix="pi-mesh.")
    byid = os.path.join(tmp, "by-id")
    os.makedirs(byid)
    for name in byid_files:
        open(os.path.join(byid, name), "w").close()
    env = dict(os.environ)
    env.update({"PI_MESH_BYID": byid,
                "PI_MESH_OUT_GPS": os.path.join(tmp, "pi-gps"),
                "PI_MESH_MARKERS": os.path.join(tmp, "markers"),
                "PI_MESH_LAYERS": os.path.join(tmp, "layers.json")})
    if extra:
        env.update(extra)
    r = subprocess.run([sys.executable, TOOL, "--once", *args],
                       capture_output=True, text=True, env=env, timeout=60)
    return r, tmp


class StandDownTest(unittest.TestCase):
    USB_GPS = "usb-u-blox_AG_-_www.u-blox.com_u-blox_7_-_GPS_GNSS_Receiver-if00"
    NODE = "usb-Espressif_USB_JTAG_serial_debug_unit_20:6E:F1:15:B8:F4-if00"

    def test_no_node_publishes_nothing(self):
        r, tmp = run_once([])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("no Meshtastic node", r.stdout)
        self.assertFalse(os.path.exists(os.path.join(tmp, "pi-gps")))

    def test_a_node_is_found_even_beside_a_usb_gps(self):
        # The markers are still wanted; only the position half stands down.
        r, tmp = run_once([self.USB_GPS, self.NODE])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("no Meshtastic node", r.stdout)

    def test_no_gps_is_a_flag_and_the_parser_has_it(self):
        # An earlier version of this test set an environment variable the tool
        # does not read, so it passed without exercising anything.
        acts = {a.dest for a in M.build_parser()._actions}
        self.assertIn("no_gps", acts)

    def test_no_gps_never_writes_the_position_file(self):
        r, tmp = run_once([self.NODE], args=("--no-gps",))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(os.path.exists(os.path.join(tmp, "pi-gps")))



class SharePositionTest(unittest.TestCase):
    """Sending this deck's own fix back into the node it is plugged into.

    pi4-deck has a USB GPS mouse and a C6L whose own Grove GPS rarely fixes, so
    the deck knows where it is and the mesh does not. A separate bridge used to
    carry one to the other by shelling out to the meshtastic CLI, which cannot
    work beside this: pi-mesh holds the only connection, and while it does every
    CLI command fails. So the job moves in here, where the port is already open.

    Two rules do the real work. Writing costs a flash erase, so a stationary deck
    must not write on every poll; and the fix must come from a receiver rather
    than from us, or the node's own position would travel out to the file and
    back into the node it came from.
    """

    def test_a_first_fix_is_always_worth_sending(self):
        due, why = M.share_due((35.7, 139.7), None, 50.0, 300.0, now=NOW)
        self.assertTrue(due)
        self.assertIn("first", why)

    def test_a_deck_sitting_still_does_not_write(self):
        """The cost here is flash, not time: --setlat erases a sector, and a
        30s poll would be 2880 erases a day for a deck on a desk."""
        last = (35.7, 139.7, NOW - 10)
        due, _ = M.share_due((35.700001, 139.700001), last, 50.0, 300.0, now=NOW)
        self.assertFalse(due)

    def test_moving_far_enough_writes(self):
        last = (35.7, 139.7, NOW - 10)
        # ~111m north.
        due, why = M.share_due((35.701, 139.7), last, 50.0, 300.0, now=NOW)
        self.assertTrue(due)
        self.assertIn("moved", why)

    def test_a_stationary_deck_still_refreshes_eventually(self):
        """Without this a parked node looks like it stopped reporting."""
        last = (35.7, 139.7, NOW - 301)
        due, why = M.share_due((35.7, 139.7), last, 50.0, 300.0, now=NOW)
        self.assertTrue(due)
        self.assertIn("heartbeat", why)

    def test_we_never_send_a_position_we_published_ourselves(self):
        """The guard against a loop. Without a receiver attached, the position
        file is written by *this* process from the node's own fix; sending that
        back would launder a node position into a hand-set one, and the
        LOC_INTERNAL test that keeps a stale fix off the map would stop
        applying to it."""
        self.assertFalse(M.may_share(True, None))     # no receiver: never
        self.assertTrue(M.may_share(True, "/dev/serial/by-id/usb-u-blox"))
        self.assertFalse(M.may_share(False, "/dev/serial/by-id/usb-u-blox"))

    def test_the_write_goes_through_and_is_remembered(self):
        wrote = []
        fix = (35.72, 139.79)
        why = M.share_position(lambda *a: wrote.append(a), fix, None,
                               50.0, 300.0, now=NOW)
        self.assertIsNotNone(why)
        self.assertEqual(wrote, [(35.72, 139.79, 0)])

    def test_nothing_is_written_when_it_is_not_due(self):
        wrote = []
        last = (35.72, 139.79, NOW - 5)
        why = M.share_position(lambda *a: wrote.append(a), (35.72, 139.79),
                               last, 50.0, 300.0, now=NOW)
        self.assertIsNone(why)
        self.assertEqual(wrote, [])

    def test_a_failed_write_is_not_remembered_as_a_success(self):
        """Otherwise one failure suppresses retries for the whole interval."""
        def boom(*a):
            raise OSError("port went away")
        why = M.share_position(boom, (35.72, 139.79), None, 50.0, 300.0, now=NOW)
        self.assertIsNone(why)

    def test_the_receiver_fix_must_be_fresh_and_real(self):
        tmp = tempfile.mkdtemp(prefix="pi-mesh-fix.")
        path = os.path.join(tmp, "pi-gps")
        with open(path, "w") as fh:
            fh.write("35.725783 139.790774 1 6 14\n")
        self.assertEqual(M.receiver_fix(path, 10.0), (35.725783, 139.790774))
        with open(path, "w") as fh:
            fh.write("35.725783 139.790774 0 0 0\n")
        self.assertIsNone(M.receiver_fix(path, 10.0), "fix=0 is not a fix")
        os.utime(path, (time.time() - 60, time.time() - 60))
        with open(path, "w") as fh:
            fh.write("35.725783 139.790774 1 6 14\n")
        os.utime(path, (time.time() - 60, time.time() - 60))
        self.assertIsNone(M.receiver_fix(path, 10.0), "a stale file is not a fix")
        self.assertIsNone(M.receiver_fix(os.path.join(tmp, "nope"), 10.0))

    def test_sharing_is_opt_in(self):
        """pi5-deck runs pi-mesh with no receiver attached and must not start
        writing to its node's flash because this landed."""
        acts = {a.dest: a for a in M.build_parser()._actions}
        self.assertIn("share_position", acts)
        self.assertFalse(acts["share_position"].default)


class ShareStateTest(unittest.TestCase):
    """What was last told to the node has to outlive this process.

    The bridge this replaced kept it in a file; the first version of this did
    not, and every restart logged "first fix" and wrote to flash again. With
    Restart=always and RestartSec=10 a crash loop would have been 8640 erases a
    day -- precisely the thing the distance rule exists to prevent.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pi-mesh-state.")
        self.path = os.path.join(self.tmp, "shared")

    def test_a_write_survives_a_restart(self):
        M.save_shared(self.path, 35.72, 139.79, NOW)
        self.assertEqual(M.load_shared(self.path), (35.72, 139.79, NOW))

    def test_no_file_yet_is_not_an_error(self):
        self.assertIsNone(M.load_shared(os.path.join(self.tmp, "nope")))

    def test_a_damaged_file_is_treated_as_no_history(self):
        """Losing the history costs one extra write. Crashing costs the map."""
        with open(self.path, "w") as fh:
            fh.write("not a position\n")
        self.assertIsNone(M.load_shared(self.path))

    def test_a_restart_right_after_a_write_does_not_write_again(self):
        M.save_shared(self.path, 35.72, 139.79, NOW - 5)
        last = M.load_shared(self.path)
        due, _ = M.share_due((35.72, 139.79), last, 50.0, 300.0, now=NOW)
        self.assertFalse(due, "a restart must not look like a first fix")

    def test_the_state_path_is_overridable(self):
        """So the tests, and a second deck, do not share one file."""
        self.assertIn("PI_MESH_SHARE_STATE", open(TOOL, encoding="utf-8").read())


if __name__ == "__main__":
    unittest.main()
