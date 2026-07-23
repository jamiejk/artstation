# Experiment: soft pen entry / exit (mid-stroke Z)

**Status:** direct-EBB proof retained; production soft-out now has an opt-in AxiDraw 3.9.6 fork
**Goal:** Prove we can gradually lower into a stroke and raise out of it by driving the pen servo while XY is still moving, using direct EBB (not `axicli`).
**Why:** Reduce start/end ink dots now; later foundation for **variable line width via Z** (humanized strokes).

## Background

Stock AxiDraw plotting does:

```text
lower pen → wait → draw full stroke → raise pen → wait → transit
```

Stock AxiDraw has no “lift during the last millimetres of the line.”
Negative **pen-up delay** overlaps the next transit with the lift, but does not
start the lift before the plotted stroke stops.

The local AxiDraw fork now adds that missing symmetric control as
`--soft_out_mm`. The controller exposes it as **Experimental end soft lift
(mm)**. Zero uses the installed stock AxiDraw; a positive value selects the
vendored fork for that job snapshot.

The pen servo and XY steppers are separate. Direct EBB can:

1. Command a new pen height with little/no queue delay (`SP` duration ≈ 0)
2. Immediately queue an XY move (`SM`)
3. Servo keeps moving while XY travels

That is enough for soft-in, soft-out, and later multi-step Z along a path.

## Safety (read before running)

1. **Stop the plotter server** so nothing else holds `/dev/ttyACM0`:
   ```bash
   # e.g. stop your systemd unit, or Ctrl+C the start script
   ```
2. Use **scrap paper**, pen you care less about first.
3. Park the head (pen **up**) at the top-left of a clear scrap region.
   The script uses **relative** bed moves from wherever you are.
4. Keep a hand near the power switch. First run with short lines.
5. After all rows (or if a row fails), the script **always commands a final pen-up** to the configured top (`--pen-up`, default 100) and checks `QP`.
6. This is **not** part of automated `unittest` discovery.

## What you will draw

Default layout (each stroke is a horizontal line to the **right**, spaced down in Y):

| Row | Label | Behaviour |
|-----|--------|-----------|
| 1 | `A_baseline` | Hard down → full stroke → hard up (AxiDraw-like) |
| 2 | `B_soft_in` | Ramp down over first *ramp_mm*, then full down, hard up |
| 3 | `C_soft_out` | Hard down, full stroke, ramp up over last *ramp_mm* |
| 4 | `D_soft_both` | Soft-in + soft-out |
| 5 | `E_variable_steps` | Stepped Z mid-stroke (proof for later variable width) |

Defaults (match your usual ballpark; override on CLI):

- Pen up / down: `100` / `85` (from your saved pen settings)
- Stroke length: `40 mm`
- Ramp length: `5 mm` at each end (where used)
- XY speed: `40 mm/s` (slower than production so ramps are visible)
- Row spacing: `8 mm`

## Run

From the repo root, **preview** (no serial motion except optional connect check is skipped until `--go`):

```bash
cd ~/plotter
venv/bin/python manual_tests/z_ramp_stroke_experiment.py --help
venv/bin/python manual_tests/z_ramp_stroke_experiment.py --plan
```

**Live run** (moves hardware):

```bash
venv/bin/python manual_tests/z_ramp_stroke_experiment.py --go
```

Useful knobs:

```bash
# Match your saved pen heights / try lighter contact
venv/bin/python manual_tests/z_ramp_stroke_experiment.py --go \
  --pen-up 100 --pen-down 85 --ramp-mm 5 --length-mm 40 --speed 40

# Longer soft ends (if 5 mm is too short to see)
venv/bin/python manual_tests/z_ramp_stroke_experiment.py --go --ramp-mm 10

# Only baseline + soft-both (faster strip)
venv/bin/python manual_tests/z_ramp_stroke_experiment.py --go --rows A,D

# Reposition: start 50 mm further right after a strip
# (park manually, or use a second scrap area)
```

Log is printed to the terminal; optional file:

```bash
venv/bin/python manual_tests/z_ramp_stroke_experiment.py --go \
  --log manual_tests/z_ramp_$(date +%Y%m%d_%H%M%S).log
```

## What to look at on the paper

For each row, note:

1. **Start dot** — blob or thickening at the left end
2. **End dot** — blob at the right end
3. **Soft-in taper** — does the line “fade in” cleanly, or skip/scratch?
4. **Soft-out taper** — fade-out vs residual blob vs thin tail
5. **Mid stroke** — is full-width consistent on B/C/D?
6. **Row E** — do stepped Z changes show as visible width steps? (yes/no is the result)

Suggested scoring (quick):

| Row | Start (1–5) | End (1–5) | Notes |
|-----|-------------|-----------|-------|
| A baseline | | | |
| B soft-in | | | |
| C soft-out | | | |
| D both | | | |
| E steps | | | |

(1 = bad blob/skip, 5 = clean)

## Pass / fail for “is this worth building?”

**Worth continuing** if any of:

- D or C clearly reduces the **end** dot vs A
- B or D clearly reduces the **start** dot vs A (beyond what −75 ms delay already does)
- E shows controllable width change with Z steps (even crude)

**Not worth yet** if:

- Soft-out still blobs as bad as baseline (servo too slow vs XY, or pen still flooding)
- Soft-in skips (need slower XY, longer ramp, or lighter full-down)
- E shows no width change (pen/pressure range too narrow — try wider pen-up/down span or different pen)

## Tuning if first strip is messy

| Symptom | Try |
|---------|-----|
| Soft-in skips / dry start | Slower `--speed`, longer `--ramp-mm`, slightly lower (more pressure) `--pen-down` |
| Soft-out still blobs | Longer `--ramp-mm`, faster raise (`--servo-rate 100` default), slightly higher `--pen-down` (lighter), less ink |
| Thin tail after soft-out | Ramp ends too high while still moving; shorten ramp or finish raise after last 1 mm |
| Whole strip too aggressive | Smaller `--length-mm`, fewer `--rows` |
| No width change on E | Increase span: e.g. `--pen-down 70 --pen-up 100` on a pen that responds to pressure |

## How this maps to later work

| Now (this experiment) | Later (variable / humanized width) |
|----------------------|-------------------------------------|
| Z ramps only at stroke ends | Z profile along entire polyline |
| Fixed linear ramp | Envelope from path curvature, noise, pressure map, etc. |
| Manual script, relative moves | Plot pipeline path (digest → timed SM + Z targets) |
| Prove EBB queue timing | Productize with safety, resume, auto-dip boundaries |

**Architectural note:** full plots continue to use AxiDraw. The opt-in fork
splits AxiDraw `SM` motion at the start and end of a stroke and interleaves
absolute servo targets with those segments. It preserves the planned XY steps,
timing, endpoint, clipping, resume, and progress SVG behavior. Full arbitrary
variable-Z profiles would still require a larger hybrid or direct-EBB executor.

## AxiDraw fork A/B test

On 2026-07-23, two three-line strips ran from calibrated home:

- Upper strip, job `8f1aee43fa0f`: stock AxiDraw
- Lower strip, job `9686be3d50c7`: `soft_out_mm=2`

Both completed, returned to controller steps `(0, 0)`, and reconciled to the
same calibrated home. In preview, the same geometry retained 180 mm pen-down
and 309 mm total XY distance; estimated time changed from 6.232 s to 5.842 s
because the lift wait overlaps the final motion.

## Selected Marsmatic profile

The later direct-EBB strips narrowed the first clean end to the third test
between 4.0625 and 4.375 mm:

- linear lower distance: **4.375 mm**
- linear raise distance: **4.6875 mm** (increased slightly after the first
  production plots still showed small end dots)
- fully raised exit tail: **0.4375 mm**
- servo target spacing: approximately **0.5 mm**

These values are now the built-in **Staedtler Marsmatic** pen profile in the
control page. The Standard profile leaves gradual entry/exit disabled. Profile
values are copied into each job so changing the selected pen later cannot
silently change an already-prepared job.

## After the run

1. Keep the scrap strip or photo it with labels A–E.
2. Note winning params (`pen-down`, `ramp-mm`, `speed`).
3. Resume the plotter server when finished.
4. Park the “variable width humanize” design until this experiment says Z-during-XY is usable with your pen.

## Related code

- Script: [`manual_tests/z_ramp_stroke_experiment.py`](../manual_tests/z_ramp_stroke_experiment.py)
- Fork wrapper: [`scripts/axicli-softout`](../scripts/axicli-softout)
- Fork provenance: [`vendor/axidraw-softout/README.md`](../vendor/axidraw-softout/README.md)
- Servo / step helpers used by the server: [`server/hardware.py`](../server/hardware.py)
- Dual-control policy: [`docs/axicli-vs-direct-ebb.md`](axicli-vs-direct-ebb.md)
