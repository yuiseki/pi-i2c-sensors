#!/usr/bin/env python3
"""Tests for bin/pi-power.

Both backends are faked so this runs anywhere: the x120x backend reads a
throwaway sysfs tree, the PiSugar backend talks to a loopback TCP server.
"""
import os
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PI_POWER = REPO / "bin" / "pi-power"


def load_module():
    """Import bin/pi-power, which has no .py suffix."""
    import importlib.util
    spec = importlib.util.spec_from_loader(
        "pi_power", importlib.machinery.SourceFileLoader("pi_power", str(PI_POWER)))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


import importlib.machinery  # noqa: E402


def run(env_extra, args=()):
    env = dict(os.environ)
    # Never let a real PiSugar (or a real /sys) leak into a test.
    env["PI_POWER_SYSFS"] = "/nonexistent"
    env["PI_POWER_PISUGAR"] = "127.0.0.1:1"
    env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(PI_POWER), *args],
        capture_output=True, text=True, env=env, timeout=20,
    )


def make_sysfs(battery=None, ac=None):
    """Build a fake sysfs root containing class/power_supply/x120x-*."""
    root = Path(tempfile.mkdtemp())
    ps = root / "class" / "power_supply"
    for name, attrs in (("x120x-battery", battery), ("x120x-ac", ac)):
        if attrs is None:
            continue
        d = ps / name
        d.mkdir(parents=True)
        for k, v in attrs.items():
            (d / k).write_text(str(v) + "\n")
    return root


class FakePiSugar:
    """Minimal stand-in for pisugar-server's line protocol on TCP 8423."""

    def __init__(self, replies):
        self.replies = replies
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.addr = "127.0.0.1:%d" % self.sock.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                data = conn.recv(256).decode().strip()
                key = data.replace("get ", "", 1)
                if key in self.replies:
                    conn.sendall(("%s: %s\n" % (key, self.replies[key])).encode())

    def close(self):
        self.sock.close()


class X120xBackend(unittest.TestCase):
    def test_reports_capacity_voltage_and_plugged(self):
        root = make_sysfs(
            battery={"capacity": 94, "voltage_now": 4172500, "status": "Charging"},
            ac={"online": 1},
        )
        r = run({"PI_POWER_SYSFS": str(root)})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(
            r.stdout.splitlines(),
            ["battery: 94", "battery_v: 4.1725", "battery_power_plugged: true"],
        )

    def test_unplugged_reports_false(self):
        root = make_sysfs(
            battery={"capacity": 41, "voltage_now": 3701000},
            ac={"online": 0},
        )
        r = run({"PI_POWER_SYSFS": str(root)})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("battery_power_plugged: false", r.stdout)
        self.assertIn("battery: 41", r.stdout)

    def test_capacity_falls_back_to_charge_ratio(self):
        # Some x120x builds expose only the charge counters.
        root = make_sysfs(
            battery={"charge_now": 2600000, "charge_full": 5200000,
                     "voltage_now": 3800000},
            ac={"online": 1},
        )
        r = run({"PI_POWER_SYSFS": str(root)})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("battery: 50", r.stdout)

    def test_missing_ac_node_is_not_fatal(self):
        # No AC sysfs node: fall back to the battery's own charging status.
        root = make_sysfs(battery={"capacity": 80, "voltage_now": 4000000,
                                   "status": "Discharging"})
        r = run({"PI_POWER_SYSFS": str(root)})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("battery_power_plugged: false", r.stdout)


class PiSugarBackend(unittest.TestCase):
    def setUp(self):
        self.server = FakePiSugar({
            "battery": "100",
            "battery_v": "4.1270666",
            "battery_power_plugged": "true",
        })
        self.addCleanup(self.server.close)

    def test_reports_pisugar_values_verbatim(self):
        r = run({"PI_POWER_PISUGAR": self.server.addr})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(
            r.stdout.splitlines(),
            ["battery: 100", "battery_v: 4.1270666",
             "battery_power_plugged: true"],
        )


class Selection(unittest.TestCase):
    def test_x120x_wins_when_both_are_present(self):
        server = FakePiSugar({"battery": "100", "battery_v": "4.0",
                              "battery_power_plugged": "true"})
        self.addCleanup(server.close)
        root = make_sysfs(battery={"capacity": 12, "voltage_now": 3300000},
                          ac={"online": 0})
        r = run({"PI_POWER_SYSFS": str(root), "PI_POWER_PISUGAR": server.addr})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("battery: 12", r.stdout)

    def test_forced_backend_does_not_fall_back(self):
        r = run({}, ["--backend", "x120x"])
        self.assertEqual(r.returncode, 1)
        self.assertIn("x120x", r.stderr)

    def test_no_backend_exits_nonzero_with_a_message(self):
        r = run({})
        self.assertEqual(r.returncode, 1)
        self.assertTrue(r.stderr.strip(), "expected an explanation on stderr")

    def test_backend_flag_reports_which_one_is_in_use(self):
        root = make_sysfs(battery={"capacity": 7, "voltage_now": 3300000},
                          ac={"online": 0})
        r = run({"PI_POWER_SYSFS": str(root)}, ["--which"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "x120x")
    def test_dry_run_reports_without_halting(self):
        root = make_sysfs(battery={"capacity": 1, "voltage_now": 3300000},
                          ac={"online": 0})
        r = run({"PI_POWER_SYSFS": str(root)},
                ["--daemon", "--dry-run", "--dwell", "0", "--interval", "0.05",
                 "--exit-after", "0.4"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("would shut down", r.stderr.lower())

    def test_daemon_publishes_to_shm_path(self):
        root = make_sysfs(battery={"capacity": 55, "voltage_now": 3900000},
                          ac={"online": 1})
        out = Path(tempfile.mkdtemp()) / "pi-power"
        r = run({"PI_POWER_SYSFS": str(root), "PI_POWER_PUBLISH": str(out)},
                ["--daemon", "--interval", "0.05", "--exit-after", "0.2"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(out.read_text().split(), ["55", "3.9", "1", "1"])


class GuardPolicy(unittest.TestCase):
    """The low-battery policy: warn at 10%, shut down at 3%, only on battery,
    and only once the level has stayed there continuously."""

    WARN, CRIT, DWELL, REPEAT = 10, 3, 60, 300

    def setUp(self):
        self.mod = load_module()
        self.warned = []
        self.halted = []
        self.guard = self.mod.Guard(
            warn_pct=self.WARN, crit_pct=self.CRIT,
            dwell_s=self.DWELL, repeat_s=self.REPEAT,
            on_warn=self.warned.append, on_shutdown=self.halted.append,
        )

    def feed(self, samples):
        """samples: (t, percent, plugged) tuples."""
        for t, pct, plugged in samples:
            self.guard.step(t, pct, plugged)

    def test_on_ac_nothing_happens_however_low(self):
        self.feed([(t, 1, True) for t in range(0, 1000, 10)])
        self.assertEqual(self.warned, [])
        self.assertEqual(self.halted, [])

    def test_warn_waits_for_the_dwell_to_elapse(self):
        self.feed([(t, 9, False) for t in range(0, self.DWELL, 10)])
        self.assertEqual(self.warned, [], "warned before the dwell elapsed")
        self.feed([(self.DWELL, 9, False)])
        self.assertEqual(self.warned, [9])

    def test_warn_repeats_only_after_the_repeat_interval(self):
        self.feed([(t, 9, False) for t in range(0, self.DWELL + 1, 10)])
        self.assertEqual(len(self.warned), 1)
        self.feed([(t, 9, False) for t in range(self.DWELL, self.DWELL + self.REPEAT, 10)])
        self.assertEqual(len(self.warned), 1, "warned again too soon")
        self.feed([(self.DWELL + self.REPEAT, 9, False)])
        self.assertEqual(len(self.warned), 2)

    def test_recovering_above_the_threshold_rearms_the_dwell(self):
        self.feed([(t, 9, False) for t in range(0, self.DWELL, 10)])
        self.feed([(self.DWELL, 20, False)])            # back up
        self.feed([(self.DWELL + 10, 9, False)])        # down again
        self.assertEqual(self.warned, [], "the old dwell was reused")
        self.feed([(self.DWELL + 10 + self.DWELL, 9, False)])
        self.assertEqual(self.warned, [9])

    def test_reconnecting_ac_clears_a_pending_warning(self):
        self.feed([(t, 9, False) for t in range(0, self.DWELL, 10)])
        self.feed([(self.DWELL, 9, True)])              # plugged back in
        self.feed([(self.DWELL + 10, 9, False)])        # unplugged again
        self.assertEqual(self.warned, [])

    def test_shutdown_after_a_sustained_critical_level(self):
        self.feed([(t, 2, False) for t in range(0, self.DWELL, 10)])
        self.assertEqual(self.halted, [], "shut down before the dwell elapsed")
        self.feed([(self.DWELL, 2, False)])
        self.assertEqual(self.halted, [2])

    def test_critical_also_warns(self):
        # 2% is below both thresholds; the user should hear about it as well.
        self.feed([(t, 2, False) for t in range(0, self.DWELL + 1, 10)])
        self.assertEqual(self.warned, [2])

    def test_shutdown_fires_once(self):
        self.feed([(t, 2, False) for t in range(0, 1000, 10)])
        self.assertEqual(len(self.halted), 1)

    def test_a_brief_dip_to_critical_does_not_shut_down(self):
        self.feed([(0, 2, False), (10, 2, False), (20, 30, False)])
        self.feed([(t, 30, False) for t in range(30, 400, 10)])
        self.assertEqual(self.halted, [])

    def test_unknown_percent_is_ignored_not_treated_as_zero(self):
        self.feed([(t, None, False) for t in range(0, 400, 10)])
        self.assertEqual(self.warned, [])
        self.assertEqual(self.halted, [])


if __name__ == "__main__":
    unittest.main()
