# AxiDraw 3.9.6 — local gradual-pen fork

This directory vendors the `axidrawinternal` and matching `axicli` Python
packages from the official Evil Mad Scientist AxiDraw API 3.9.6 release:

- Release archive: `https://cdn.evilmadscientist.com/dl/ad/public/AxiDraw_API.zip`
- Source wheel/archive SHA-256 recorded when vendored:
  `744be32fa64d85adb9dee4d03098af272be4bbf04a0c3dee40b685c3b0d97c4e`
- Upstream package version: `3.9.6`

The upstream source files retain their original copyright and license notices.
`axidrawinternal` is GPL-2.0-or-later; the upstream `axicli` files carry their
MIT notice. The repository as a whole is distributed under GPLv3.

## Why this is vendored

Stock AxiDraw completes a pen-down path, stops XY, and only then raises the
pen. With a technical pen, that stationary interval deposits an ink dot at the
stroke endpoint. A negative pen-up delay can overlap raising with the following
pen-up transit, but it cannot begin the lift while the plotted stroke is still
moving.

The local fork changes only that missing boundary. It retains upstream SVG
parsing, path planning, acceleration, clipping, pause/resume, progress-digest,
and serial execution, then transforms the already-planned pen-down motion list
to introduce Z motion near its endpoints. This is substantially less risky than
reimplementing full SVG plotting in the server's direct-EBB control layer.

## Local changes

There are two opt-in transforms:

1. `--soft_out_mm` splits the final pen-down motion command at the requested
   distance from the endpoint and inserts a non-blocking pen-up command. This
   was the first proof and remains available as an experimental setting.
2. The gradual profile splits both ends of a pen-down path into short,
   time-preserving XY segments and interleaves absolute servo targets:

   - `--gradual_ramp_mm`: distance used to lower into a stroke
   - `--gradual_exit_ramp_mm`: distance used to raise out of a stroke; defaults
     to the lowering distance for backward compatibility
   - `--gradual_tail_mm`: fully raised tail at the end of the stroke
   - `--gradual_segment_mm`: spacing between absolute servo targets

The built-in Marsmatic profile currently uses independent entry and exit
distances: 4.375 mm down, 4.6875 mm up, a 0.4375 mm fully raised tail, and
approximately 0.5 mm between servo targets. Separating the distances keeps the
clean pen-down result while allowing a slightly earlier lift for residual end
dots.

The gradual transform preserves the upstream XY step totals, duration,
distance, and endpoint. The EBB executor paces short servo/motion segments close
to real time so the interleaved commands cannot saturate the controller queue.

Both features default to zero/off and preserve upstream behavior. When a
gradual profile is enabled, it handles the complete entry and exit and takes
precedence over the single-command soft-out lift.

## Runtime selection and job snapshots

`server.pen_profiles` copies profile values into every new job. Consequently,
changing the selected profile does not silently alter queued or running work.
`server.server.axicli_for_job()` selects:

- `venv/bin/axicli` for a Standard job with both transforms disabled;
- `scripts/axicli-softout` when `soft_out_mm` or a gradual profile is enabled.

The wrapper prepends this directory to `PYTHONPATH` and launches the vendored
`axicli` and `axidrawinternal` together. It does not modify or shadow the
installed driver for unrelated commands.

## Preserved invariants

The transform and its tests require that:

- zero/off returns upstream behavior unchanged;
- signed motor-step totals, motion time, geometric distance, and final XY
  endpoint remain unchanged;
- short paths cap each ramp so a middle portion remains;
- the fully raised tail is part of the original stroke geometry, not extra XY;
- the upstream progress SVG and pause/resume pipeline remain authoritative;
- auto-dip scheduling still occurs only between complete SVG strokes;
- CLI options are registered in both the public `axicli` parser and the
  internal option plumbing.

The last point is important: adding an internal option without exposing it in
`axicli/axidraw_cli.py` causes a job to fail before motion with “unrecognized
arguments.” `tests/test_axidraw_softout.py` therefore checks the wrapper's
actual help surface as well as the transform.

## Files intentionally changed from upstream

- `axicli/axidraw_cli.py` and `axicli/utils.py`: public CLI flags and option
  forwarding.
- `axidrawinternal/axidraw_options/common_options.py`,
  `axidrawinternal/axidraw.py`, and `axidrawinternal/axidraw_control.py`:
  internal option defaults, validation, and secondary-copy support.
- `axidrawinternal/motion.py`: applies the selected transform to planned
  pen-down motion.
- `axidrawinternal/pen_handling.py`: executes interleaved profile commands
  without restoring normal pen endpoints too early.
- `axidrawinternal/dripfeed.py`: paces profile segments and dispatches the new
  command markers.
- `axidrawinternal/softout.py`: single-command soft-out transform.
- `axidrawinternal/gradual.py`: gradual entry/exit transform.

Other files are an unmodified matching snapshot needed so the fork does not
mix internal modules from different AxiDraw releases.

## Verification

Automated tests do not open the serial port:

```bash
venv/bin/python -m unittest tests.test_axidraw_softout
PLOTTER_DISABLE_WORKER=1 venv/bin/python -m unittest discover -v
```

The wrapper can be exercised end-to-end without hardware by passing a real SVG,
all profile arguments, and `--preview`. Physical experiments, safety notes, and
retained proof SVGs are documented in
[`../../docs/z-ramp-stroke-experiment.md`](../../docs/z-ramp-stroke-experiment.md).

## Updating the fork

1. Record the exact upstream release URL, version, and archive hash.
2. Build a clean upstream tree separately; do not overwrite this directory.
3. Diff the clean release against this directory and reapply only the files
   listed above.
4. Review upstream changes to motion-list structure, pen handling, dripfeed,
   pause/resume, and CLI option plumbing before resolving conflicts.
5. Run the full non-hardware suite and an `--preview` command.
6. Only then perform a labelled scrap-paper test with the server stopped and
   the pen initially up.

The normal installed `venv/bin/axicli` must remain usable and unchanged after
an update.
