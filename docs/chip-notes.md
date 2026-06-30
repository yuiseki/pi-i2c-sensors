# Chip notes

Register-level detail for the sensors here, plus the lessons learned wiring them
on a real appliance. Useful when adding the next chip.

## MSA311 (3-axis accelerometer, I2C 0x62)

- Part-ID: register `0x01` reads `0x13`.
- **Power mode (the big trap): boots SUSPENDED.** Until you set NORMAL mode the
  data registers return a stale/garbage value (we first read ~1.8 g on one axis,
  which is physically impossible at rest). Init:
  - `0x11 = 0x00`  power mode NORMAL (bits 7:6 = 00) + bandwidth
  - `0x0F = 0x00`  range +/-2 g (bits 1:0 = 00) + 14-bit resolution (bits 3:2 = 00)
  - `0x10 = 0x08`  ODR ~250 Hz (optional)
- Data: registers `0x02..0x07`, X/Y/Z little-endian, **left-justified**. Read as
  signed 16-bit and divide:
  - `g = int16 / 16384`  at +/-2 g (full-scale +/-2 g maps to +/-32768).
- At rest `|a|` should be ~1.0 g. We saw ~1.05-1.10 (a few % per-axis offset/scale
  error); fine for tilt, calibrate with a 6-point tumble if you need better.
- Tilt: `pitch = atan2(ay, az)`, `roll = atan2(ax, ...)` in the device frame; or
  the unsigned tilt from a resting reference via `acos(dot(a_now, a_rest))`.

## MMC5603 (3-axis magnetometer, I2C 0x30)

- Product-ID: register `0x39` reads `0x10`.
- Single measurement: `0x1B = 0x01` (Take_meas_M), wait ~12 ms, read 9 bytes from
  `0x00`. 20-bit unsigned per axis, centered at 2^19:
  - `Xout = (reg00 << 12) | (reg01 << 4) | (reg06 >> 4)`  (Y at 02/03/07, Z at 04/05/08)
  - `G = (Xout - 524288) / 16384`
- **Intrinsic bias + SET/RESET.** A bare single-shot carries a large constant
  per-part bridge offset that is fixed in the *sensor* frame, so it does not
  change as you rotate -> a raw reading looks "stuck". Null it with SET/RESET:
  - SET:   `0x1B = 0x08`, wait ~4 ms, measure -> `s`
  - RESET: `0x1B = 0x10`, wait ~4 ms, measure -> `r`
  - field = `(s - r) / 2`   (offset = `(s + r) / 2`)
- **SET/RESET breaks near a strong magnet.** With a strong DC field present, the
  SET/RESET pair becomes unstable and the result jumps bimodally (we saw X swing
  0.7..3.2 G). There, the raw single-shot is steadier. But you cannot get a
  compass near a magnet anyway, so the real fix is to remove the magnet and then
  use SET/RESET.
- BW: `0x1C` bits 1:0 (0 = most accurate / slowest). SW reset: `0x1C = 0x80`.
- Continuous + automatic SET/RESET exists (`0x1A` ODR, `0x1B` bit5 Auto_SR + bit7
  Cmm_freq_en, `0x1D` bit4 Cmm_en) but we do not use it: per-sample manual
  SET/RESET was simpler and more predictable here.

### Magnetic environment (the part that actually bit us)

Earth's field is ~0.5 G total (in Tokyo: ~0.3 G horizontal, ~0.37 G vertical,
inclination ~49 deg). Anything stronger at the sensor is local interference.

- **A permanent magnet collapses the field to ~1 dimension.** The PiSugar battery
  mount magnet, sitting right by the magnetometer, gave `|B|` ~5 G and a field
  that barely changed with rotation (you could not recover a 2D heading). The
  magnet is only double-sided-taped on; removing it dropped `|B|` to ~0.4 G and
  the heading became a clean circle. Always scout the mount with `pi-calib-mag`.
- **Soft-iron (ferrous, no field of its own) compresses an axis.** In one mount
  the Y axis range was 0.16 G vs 0.52 G for X/Z; in another the Z axis collapsed
  to 0.10 G. The min/max scale "fixes" this by multiplying that axis ~4x, which
  just amplifies its noise; a tilt-compensated heading that uses the bad axis
  goes to garbage (we saw the heading jump 2..324 deg while the plain horizontal
  heading sat steady). Mitigations, in order of preference:
  1. Move the sensor away from the ferrous part (re-mount).
  2. If the bad axis is vertical (Z) and the device is used roughly flat, compute
     heading from the horizontal X-Y axes only (no tilt compensation).
  3. A full ellipsoid (3x3 soft-iron) fit beats per-axis min/max, but cannot
     rescue an axis whose signal is buried in noise.

## Tilt-compensated heading

Standard form (requires all three mag axes to be good):

```
pitch = atan2(-ax, hypot(ay, az))
roll  = atan2(ay, az)
Xh = mx*cos(pitch) + mz*sin(pitch)
Yh = mx*sin(roll)*sin(pitch) + my*cos(roll) - mz*sin(roll)*cos(pitch)
heading = atan2(Yh, Xh)
```

If the Z axis is unreliable (soft-iron), drop to the flat heading
`atan2(my, mx)`; it is exact only when level but good enough at small tilt and
far better than a garbage tilt-comp.

## VEML7700 (ambient light, I2C 0x10)

- **Breakouts are interchangeable.** The Adafruit STEMMA QT VEML7700
  (Switch Science 8182) and the SparkFun Qwiic VEML7700 (SS 10600) carry the
  same VEML7700 at the same address `0x10`, so swapping one for the other is a
  drop-in: no code, no address change. Verified on pi4-s-1 (2026-06-30) — the
  new board read 20/20 and tracked dark (~8 lux covered) to bright (~100+ lux).
- **No chip-id / who-am-I register.** Detect by whether `0x10` answers a read
  of config reg `0x00`. Read by `pi-env`.
- Registers are 16-bit **LSB-first words** (use SMBus word ops directly):
  - `0x00` ALS_CONF_0 (config): bits `[12:11]`=gain, `[9:6]`=integration time
    (IT), `[5:4]`=persistence, `[1]`=INT enable, `[0]`=**ALS_SD (shutdown)**.
  - `0x04` ALS (the light channel used for lux), `0x05` WHITE.
- **Boots shut down** (`0x00` reads `0x0001` = ALS_SD set). Clear bit0 to enable;
  wait one integration time before the first valid read.
- Gain bits: x1=`0b00`, x2=`0b01`, x1/8=`0b10`, x1/4=`0b11`.
  IT bits: 100ms=`0b0000`, 200=`0b0001`, 400=`0b0010`, 800=`0b0011`, 50=`0b1000`,
  25=`0b1100`.
- **Lux = raw x resolution**, where `resolution = 0.0036 * (2/gain) * (800/IT_ms)`
  lux/count (0.0036 is the max-resolution figure at gain x2 / IT 800 ms; verified
  it gives the datasheet 0.0576 at gain x1 / IT 100 ms).
- **Auto-range** (what `pi-env` does): start wide (gain x1/8, IT 100 ms) and step
  up sensitivity only while `raw <= 100`, so it never saturates in sun yet keeps
  resolution in a dim room. The high-lux nonlinearity polynomial is skipped
  (overkill for an environment reading).

## Qwiic daisy-chain: swapping a board (and the reseat trap)

Replacing one sensor on a Qwiic chain (e.g. swap the VEML7700 board) routinely
jostles the connector of the *next* sensor on the chain. On pi4-s-1 a VEML7700
swap left the chained BME280 (0x77) reading errors while the VEML7700 itself was
perfect — the fault was purely the `VEML -> BME280` cable segment, not the new
board.

- **Symptom of a marginal Qwiic contact: ACK-but-can't-read.** A barely-seated
  connector still ACKs the 1-byte address probe but fails longer transfers with
  `OSError: [Errno 5] Input/output error`. Reads can also flap (a few good ones,
  then a stretch of failures) as the contact shifts. `i2cdetect` is unreliable
  here both ways: it may *show* the address (probe ACKs) when reads fail, and for
  the BME280 at 0x77 it often does **not** show it (i2cdetect's default
  quick-write probe gets NAKed) even when register reads work fine. **Trust a
  real register read, not i2cdetect**, especially for 0x77.
- **Quantify with a trial loop, don't eyeball one read.** Loop each device ~20x
  and count successes; a healthy device is 20/20, a bad contact is 0/20 (or
  flapping). This also isolates the bad chain segment: if the swapped sensor is
  20/20 while its neighbour is 0/20, the `Pi -> swapped` part is fine and only the
  `swapped -> neighbour` segment is loose.
  ```python
  def trials(fn, n=20):
      ok = 0
      for _ in range(n):
          try: fn(); ok += 1
          except Exception: pass
      return ok
  trials(lambda: b.read_byte_data(0x77, 0xD0))      # BME280 chip-id (->0x60)
  trials(lambda: b.read_i2c_block_data(0x77, 0xE1, 7))  # BME280 calib block
  trials(lambda: b.read_word_data(0x10, 0x04))      # VEML7700 ALS
  ```
- **Fix:** firmly reseat the neighbour's Qwiic cable at *both* ends (click home);
  if still 0/20, swap that cable (intermittent break) or move to the spare
  connector on the new board (these boards have two). Re-run the trial loop;
  expect 20/20 on every device before declaring done.
