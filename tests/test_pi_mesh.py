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


if __name__ == "__main__":
    unittest.main()
