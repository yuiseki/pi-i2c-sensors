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


if __name__ == "__main__":
    unittest.main()
