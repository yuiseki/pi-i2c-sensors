# pi-i2c-sensors

Small, dependency-light tools for reading I2C / [Qwiic / STEMMA QT](https://www.sparkfun.com/qwiic)
sensors on a Raspberry Pi, and turning them into something useful (right now: a
tilt + compass orientation feed for a map/UI).

Built and proven on a Pi 4 appliance (HDMI map display) with a Qwiic SHIM
breaking out I2C1, but the tools are generic.

## Hardware

| Device | I2C addr | ID register | Notes |
|---|---|---|---|
| [SparkFun Qwiic SHIM](https://www.sparkfun.com/products/15794) | - | - | Breaks Pi I2C1 (GPIO2/3) out to Qwiic; daisy-chain sensors |
| MSA311 3-axis accelerometer | `0x62` | `0x01` -> `0x13` | Bosch/MEMSIC; **powers up SUSPENDED** |
| MMC5603 3-axis magnetometer | `0x30` | `0x39` -> `0x10` | MEMSIC; needs SET/RESET to null its offset |

Multiple Qwiic sensors share the one I2C1 bus, so they coexist with anything
else already on it (e.g. a PiSugar at `0x57`/`0x68`). `i2cdetect -y 1` lists them.

## Install

```bash
./install.sh          # copies bin/* to /usr/local/bin and adds you to the i2c group
# log out / back in once so the i2c group membership takes effect
```

`/dev/i2c-1` is `root:i2c`, so the tools need either i2c-group membership (what
install.sh sets up) or sudo. They auto-elevate with `sudo` if they hit a
permission error, so they work either way.

## Tools

All are self-contained Python 3 (only `smbus2` or `smbus`), live-updating in the
terminal, Ctrl-C to quit.

### `pi-calib-mag` - magnetometer field-strength meter

Live `|B|` readout. Use it to **find a mounting spot clear of magnets**: a good
spot reads near Earth's field (~0.5 G); a much larger value means a magnet
(speaker, motor, a PiSugar mount magnet) is too close and will swamp the compass.

### `pi-calib` - magnetometer hard-iron calibration

Tumble the device through all orientations for 30 s; saves the hard-iron offset
and per-axis soft-iron scale to `~/tmp/pi-calib.json`. Watch the per-axis range:
if one axis stays small, something ferrous near that axis is compressing the
field (soft-iron) and the compass will be poor in that plane.

### `pi-orient` - orientation publisher

Reads accel + mag, applies the calibration, and publishes
`<pitch_deg> <bearing_deg> <have_sensor>` to **`/dev/shm/pi-orientation`**
(tmpfs = RAM, so no microSD wear) ~15x/s. A consumer (e.g. the map app) reads
that file.

- `pitch` is the unsigned tilt away from the resting pose (0 at rest, grows with
  any tilt), suited to a 0..60 map camera pitch.
- `bearing` is the magnetic heading from the horizontal axes, minus the offset
  set by `pi-set-north`.
- `have_sensor` is `0` when the chips do not respond, so a consumer can tell
  "no sensor" from "sensor reads zero". Runs harmlessly on a Pi with no sensors.

Smoothing favours **big moves over fine jitter**: a deadband holds the output
steady at rest, then EMA smooths the catch-up. Tune via env:
`PI_ORIENT_BEAR_DB` (deg, default 6), `PI_ORIENT_PITCH_DB` (deg, default 3),
`PI_ORIENT_ALPHA` (0..1, default 0.2, smaller = smoother).

### `pi-set-north` / `pi-set-south` - bearing reference

Point the device the way you want to read as North (or South), then run the
matching command. It folds the current bearing into an offset in `~/tmp/pi-north`
so that facing reads 0 (`pi-set-north`) or 180 (`pi-set-south`); the rest of the
circle follows. `pi-orient` applies the offset within ~1 s. Touches no I2C, so it
never fights a running `pi-orient`. Use whichever cardinal direction is the
convenient one to point at.

### `pi-cal-point <north|south|east|west>` - heading calibration points

The accurate way to align the compass. Point the device at each TRUE cardinal
direction (flat) and run it; it captures the live calibrated mag + accel from
`/dev/shm/pi-orientation` (no I2C), auto-detects horizontal vs vertical from
gravity, and saves `~/tmp/pi-h-<dir>.json` (or `pi-v-<dir>.json`). With all four
horizontal points, `pi-orient` fits the residual hard-iron centre + a rotation
offset, which corrects errors a single `pi-set-north` cannot (e.g. an
off-centre, mildly elliptical horizontal circle left by an imperfect tumble).

```bash
# face true North (flat):  pi-cal-point north
# face true East:          pi-cal-point east
# face true South:         pi-cal-point south
# face true West:          pi-cal-point west
```

`pi-orient` reloads the points every few seconds, so the fix applies without a
restart. Prefer this 4-point fit; `pi-set-north`/`-south` remain a quick
single-point fallback when the points are not present.

## Orientation pipeline

```
pi-calib  (once)            ->  ~/tmp/pi-calib.json   (persistent, on disk)
pi-set-north (when aligning) ->  ~/tmp/pi-north        (persistent, on disk)
pi-orient (always running)  ->  /dev/shm/pi-orientation (RAM, ~15 Hz)
                                        |
                                        v
                            your app reads pitch/bearing
```

Keep one-shot calibration on disk; keep the high-rate stream in `/dev/shm` so the
microSD is never written in a hot loop.

## Run `pi-orient` at boot (systemd)

So the orientation feed is always available:

```bash
sudo cp systemd/pi-orient.service /etc/systemd/system/
# the unit runs as User=yuiseki; edit it if your user differs
sudo systemctl daemon-reload
sudo systemctl enable --now pi-orient.service
systemctl status pi-orient.service
```

`pi-orient` suppresses its live ANSI display when not on a terminal, so the
journal stays clean. It re-probes for the sensors, so it survives the chips
being absent or hot-plugged.

## Gotchas worth knowing

These cost real debugging time; see [docs/chip-notes.md](docs/chip-notes.md) for
the register-level detail.

- **MSA311 boots SUSPENDED.** Out of reset it returns stale values. Write
  `0x11=0x00` (NORMAL) + `0x0F=0x00` (+/-2g, 14-bit) before reading. Data is
  left-justified: `g = int16 / 16384` at +/-2g.
- **MMC5603 needs SET/RESET... except near a strong magnet.** A single TM_M read
  carries a large constant per-part bias, so use SET/RESET (measure after a SET
  pulse and after a RESET pulse, take half the difference) to null it. BUT near a
  strong DC field (a nearby magnet) SET/RESET goes unstable and bimodal; there,
  the raw single-shot is steadier (and the compass is unusable anyway). Rule:
  remove the magnet, then use SET/RESET.
- **Permanent magnets destroy the field, not just offset it.** A PiSugar mount
  magnet right next to the magnetometer collapsed the field response to ~1
  dimension. It is held on by double-sided tape; removing it dropped `|B|` from
  ~5 G to ~0.4 G (Earth). Use `pi-calib-mag` to hunt for a clean spot.
- **Soft-iron compresses one axis.** Ferrous material (brackets, screws, a
  display's steel frame) near one axis shrinks that axis's range; the min/max
  scale then amplifies its noise. If the mounted Z axis is compressed, compute
  heading from the horizontal X-Y axes only and skip tilt compensation (works
  while the device is roughly flat).
- **Stream to tmpfs, not the SD card.** Writing an orientation file at 15 Hz to
  the microSD wears it out; `/dev/shm` is RAM.

## Adding a new I2C / Qwiic sensor

1. Plug it into the Qwiic chain, confirm it with `i2cdetect -y 1`, and verify its
   ID register (the datasheet's WHO_AM_I / product-id) with `i2cget`.
2. Copy the read pattern from an existing tool: `open_bus()` (auto-sudo),
   `read_byte_data` / `read_i2c_block_data`, and a small decode (`s16`, `u20`).
3. Add register init + a decode to physical units, following the per-chip notes.
4. If it feeds the orientation/state stream, publish to `/dev/shm` (never the SD
   in a hot loop) and expose a `have_sensor` flag so consumers degrade cleanly.

PRs for more sensors welcome.
