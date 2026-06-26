# pi-i2c-sensors

Small, dependency-light tools for reading sensors on a Raspberry Pi and turning
them into something useful (a tilt + compass orientation feed and a GPS position
feed for a map/UI). Mostly I2C / [Qwiic / STEMMA QT](https://www.sparkfun.com/qwiic),
plus a USB NMEA GPS (the repo name predates that one; it lives here anyway).

Built and proven on a Pi 4 appliance (HDMI map display) with a Qwiic SHIM
breaking out I2C1, but the tools are generic.

## Hardware

| Device | I2C addr | ID register | Notes |
|---|---|---|---|
| [SparkFun Qwiic SHIM](https://www.sparkfun.com/products/15794) | - | - | Breaks Pi I2C1 (GPIO2/3) out to Qwiic; daisy-chain sensors |
| MSA311 3-axis accelerometer | `0x62` | `0x01` -> `0x13` | Bosch/MEMSIC; **powers up SUSPENDED** |
| MMC5603 3-axis magnetometer | `0x30` | `0x39` -> `0x10` | MEMSIC; needs SET/RESET to null its offset |
| u-blox 7 GPS/GNSS receiver | USB | - | USB CDC-ACM serial (`/dev/ttyACM0`), NMEA; not I2C |
| BME280 temp/humidity/pressure | `0x77` (`0x76`) | `0xD0` -> `0x60` | Bosch; pressure is fine close to the Pi, but T/RH read hot (place it away for ambient) |
| VEML7700 ambient light | `0x10` | (no chip id) | Vishay; lux via auto-ranged gain/IT. Read by `pi-env` alongside the BME280 |

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
points of a plane, `pi-orient` least-squares fits an **affine map** (centre +
per-axis scale + rotation + shear) of the two horizontal magnetometer axes to
the true bearings. That corrects errors a single `pi-set-north` cannot, and
handles the upright (vertical) case where one horizontal axis is the soft-iron
compressed one (so a plain centre/offset is not enough). Capture both planes
(`pi-h-*` flat, `pi-v-*` upright) and `pi-orient` picks the right fit from
gravity automatically.

```bash
# face true North (flat):  pi-cal-point north
# face true East:          pi-cal-point east
# face true South:         pi-cal-point south
# face true West:          pi-cal-point west
```

`pi-orient` reloads the points every few seconds, so the fix applies without a
restart. Prefer this 4-point fit; `pi-set-north`/`-south` remain a quick
single-point fallback when the points are not present.

### `pi-gps` - USB GPS position feed

Reads a USB NMEA GPS (e.g. u-blox 7) at `/dev/serial/by-id/*GPS*` (falls back to
`/dev/ttyACM0`) and publishes `<lat> <lon> <fix> <sats>` to `/dev/shm/pi-gps`
(RAM). `fix` is the NMEA quality (0 = no fix, 1 = GPS, 2 = DGPS); lat/lon hold
the last known position. Needs the **dialout** group (no sudo); parses NMEA
directly (no gpsd/pyserial). Runs harmlessly with no GPS attached (keeps
publishing `fix=0`). The map consumer recentres on the position when `fix >= 1`.

### `pi-env` - environment (BME280 temp / humidity / pressure + VEML7700 lux)

One-shot by default: `pi-env` prints `27.5 C  45 %RH  1010.5 hPa  230.1 lux`,
also writes `<temp_c> <humidity_pct> <pressure_hpa> <bme_have> <lux> <lux_have>`
to `/dev/shm/pi-env`, and exits (a plain non-interactive CLI). `pi-env --daemon`
keeps measuring ~every 2 s for a consumer (systemd unit). Bosch float
compensation is inlined and VEML7700 lux is computed directly (no driver libs).

Both sensors are optional and independent: each is reported with its own
presence flag, so the BME280 and the VEML7700 (light) can be used separately or
together. The first four fields are unchanged from the BME280-only version, so
existing consumers keep working; lux is appended. Light is auto-ranged (gain /
integration time stepped so it stays accurate from bright sun to a dim room).
Rationale for folding lux into `pi-env` rather than a separate `pi-lux`: avoids
one-command-per-chip sprawl (YAGNI); `pi-env` is the single "ambient" reader.

Placement matters: the **pressure** reading is accurate even right next to the
Pi, but **temperature/humidity read hot** there (the Pi's heat). For ambient
T/RH put the sensor on a Qwiic cable away from the Pi (the dew point of the hot
reading matches the room, confirming it is the same air measured warm).

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

## Run the feeds at boot (systemd)

So the orientation + GPS feeds are always available:

```bash
sudo cp systemd/pi-orient.service systemd/pi-gps.service systemd/pi-env.service /etc/systemd/system/
# the units run as User=yuiseki; edit if your user differs
sudo systemctl daemon-reload
sudo systemctl enable --now pi-orient.service pi-gps.service pi-env.service
systemctl status pi-orient.service pi-gps.service pi-env.service
```

(`pi-env` needs `--daemon`, which its unit already passes; the bare command is a
one-shot CLI.)

Both suppress their live ANSI display when not on a terminal, so the journal
stays clean, and both survive their device being absent or hot-plugged
(re-probing / reopening), publishing a "no data" state meanwhile.

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
