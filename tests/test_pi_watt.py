#!/usr/bin/env python3
"""Tests for bin/pi-watt.

No hardware and no Pi: the pack is a throwaway sysfs tree and the vcgencmd
calls are replaced by `echo`, so the same cases run on a laptop.

What is worth pinning down here is the sign logic. The X120x driver reports
status=Charging whenever input is present, so a deck can report Charging while
it runs itself flat - that is the bug this tool exists to make visible, and it
is invisible to any test that only checks the happy path.
"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PI_WATT = REPO / "bin" / "pi-watt"

# One real reading, trimmed to the rails that carry any power. EXT5V is what
# pi-watt pulls out of it; the rest only has to sum.
PMIC = """\
   3V3_SYS_A current(1)=0.14638950A
  VDD_CORE_A current(7)=1.95567000A
   3V3_SYS_V volt(9)=3.30910500V
  VDD_CORE_V volt(15)=0.87247780V
     EXT5V_V volt(24)=4.98480000V
      BATT_V volt(25)=0.00000000V
"""


def sysfs(tmp, *, power_now, capacity=42, voltage_now=3683750,
          online=1, status="Charging", energy_now=4692004):
    """Build the corner of /sys that pi-watt reads."""
    root = Path(tmp)
    batt = root / "class/power_supply/x120x-battery"
    batt.mkdir(parents=True)
    for name, value in (("capacity", capacity), ("voltage_now", voltage_now),
                        ("power_now", power_now), ("energy_now", energy_now)):
        (batt / name).write_text("%s\n" % value)
    for node, fields in (("x120x-ac", {"online": online}),
                         ("x120x-charger", {"status": status})):
        d = root / "class/power_supply" / node
        d.mkdir(parents=True)
        for name, value in fields.items():
            (d / name).write_text("%s\n" % value)
    return root


def run(root, args=()):
    env = dict(os.environ)
    env["PI_WATT_SYSFS"] = str(root)
    # vcgencmd is not on a laptop, and on a Pi it would read the real machine.
    (Path(root) / "pmic.txt").write_text(PMIC)
    env["PI_WATT_PMIC"] = "cat " + str(Path(root) / "pmic.txt")
    env["PI_WATT_TEMP"] = "echo temp=54.3'C"
    env["PI_WATT_THROTTLED"] = "echo throttled=0x50000"
    return subprocess.run(["bash", str(PI_WATT), *args],
                          capture_output=True, text=True, env=env, timeout=30)


class SignTest(unittest.TestCase):
    """The reported status is not the state. The sign of power_now is."""

    def test_draining_while_the_driver_says_charging(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = run(sysfs(tmp, power_now=-5930000, status="Charging", online=1))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("DRAINING", r.stdout)
        self.assertNotIn("charging", r.stdout.split("\n")[0])

    def test_a_present_input_that_is_losing_ground_is_called_out(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = run(sysfs(tmp, power_now=-5930000, online=1))
        self.assertIn("the supply is too weak", r.stdout)

    def test_no_such_complaint_when_the_input_is_simply_unplugged(self):
        # Running on the pack with nothing plugged in is the normal case, not
        # a fault: warning about it would train the reader to ignore the line.
        with tempfile.TemporaryDirectory() as tmp:
            r = run(sysfs(tmp, power_now=-5930000, online=0))
        self.assertIn("DRAINING", r.stdout)
        self.assertNotIn("too weak", r.stdout)

    def test_charging(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = run(sysfs(tmp, power_now=+6230000))
        self.assertIn("charging", r.stdout)
        self.assertNotIn("too weak", r.stdout)

    def test_a_gauge_dithering_around_zero_is_not_a_drain(self):
        # Supply exactly matching load. Calling this DRAINING would cry wolf
        # every time the deck sat idle on a marginal adapter.
        with tempfile.TemporaryDirectory() as tmp:
            r = run(sysfs(tmp, power_now=-40000, online=1))
        self.assertIn("float", r.stdout)
        self.assertNotIn("too weak", r.stdout)


class ReadingTest(unittest.TestCase):
    def test_rails_are_summed_and_ext5v_is_pulled_out(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = run(sysfs(tmp, power_now=+6230000))
        # 0.1463895*3.309105 + 1.95567*0.8724778 = 0.4844 + 1.7062 = 2.19
        self.assertIn("rails=2.19W", r.stdout)
        self.assertIn("ext5v=4.98480000V", r.stdout)

    def test_time_left_only_appears_while_draining(self):
        with tempfile.TemporaryDirectory() as tmp:
            drain = run(sysfs(tmp, power_now=-5930000, energy_now=4692004))
        # 4.692 Wh at 5.93 W is a little under 48 minutes.
        self.assertIn("47min left", drain.stdout)
        with tempfile.TemporaryDirectory() as tmp:
            charge = run(sysfs(tmp, power_now=+6230000))
        self.assertNotIn("min left", charge.stdout)

    def test_verbose_breaks_out_the_rails(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = run(sysfs(tmp, power_now=+6230000), ["-v"])
        self.assertIn("VDD_CORE", r.stdout)
        self.assertIn("3V3_SYS", r.stdout)
        # Biggest consumer first, so the eye lands on it.
        rails = [ln for ln in r.stdout.splitlines() if " A x " in ln]
        self.assertTrue(rails[0].strip().startswith("VDD_CORE"), rails)

    def test_a_missing_pack_is_an_error_not_a_zero(self):
        # Reporting 0% because the driver failed to load would be worse than
        # saying nothing: pi-power's policy would read it as a flat battery.
        with tempfile.TemporaryDirectory() as tmp:
            r = run(tmp)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("x120x", r.stderr)

    def test_an_unreadable_field_says_unknown_rather_than_vanishing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = sysfs(tmp, power_now=+6230000)
            (root / "class/power_supply/x120x-battery/capacity").unlink()
            r = run(root)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("unknown", r.stdout)


if __name__ == "__main__":
    unittest.main()
