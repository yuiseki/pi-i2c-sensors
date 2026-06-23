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
