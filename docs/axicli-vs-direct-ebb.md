# AxiDraw CLI vs direct EBB control

The project intentionally uses two control paths:

1. `axicli` for full SVG plotting and AxiDraw job execution.
2. A small direct EiBotBoard (EBB) serial control layer for interactive, latency-sensitive operator actions.

This split keeps AxiDraw's plotting behavior where it matters, while avoiding process startup and repeated USB-session overhead for simple manual commands.

## Use `axicli` for plotting

Keep `axicli` as the source of truth for:

- SVG parsing and plotting.
- Layer execution.
- AxiDraw path planning, acceleration, and plot-time behavior.
- Programmatic pause handling used by automatic ink-dip checkpoints.
- Compatibility with AxiDraw plot configuration and future AxiDraw driver changes.

These operations are long-running enough that `axicli` startup cost is not the bottleneck, and the driver abstraction is valuable.

## Use direct EBB serial for interactive control

Use direct serial commands for short operator actions where human-visible latency matters:

- Jog and move-to commands.
- Pen up/down from the control panel.
- Browser **Home** return to the saved bed coordinate.
- Automatic post-layer return to the saved Home coordinate.
- Ink-well test/dip cycles, including servo raise/lower, travel to the well, pickup circles, and return.
- Cached hardware telemetry and motor-state checks.

These operations use the EBB command protocol directly, for example:

- `SM` for XY stepper movement.
- `QS` for step position.
- `QP`, `QB`, and `PI` for hardware state.
- `SC` and `SP` for servo configuration and pen up/down.

## What the direct layer must preserve

The direct layer is deliberately narrow. It must preserve:

- Bed-coordinate safety checks before movement.
- Software-position calibration and persistence.
- Motor enabled/high-resolution checks.
- Hardware locking so two commands cannot drive the plotter at once.
- Servo calibration compatibility with `axidraw_servo_conf.py`.
- Return-position verification for ink-dip operations.
- Operator-visible failure states instead of automatic recovery after a failed dip.

## Tradeoffs

Direct EBB control gains responsiveness but loses some `axicli` abstraction:

- Less portability across untested controller variants.
- Less automatic compatibility with future AxiDraw driver behavior.
- Less built-in driver state tracking.
- More local responsibility for servo timing, motor checks, and coordinate transforms.

The current rule is pragmatic: use direct EBB control for fast, simple, operator-facing movement; use `axicli` for full plot execution.

## Runtime state

The direct layer stores local calibration state in runtime JSON files, not in Git:

- `plotter_position.json`
- `plotter_pen_settings.json`
- `plotter_plot_settings.json`
- `plotter_ink_well_settings.json`
- `plotter_paper_settings.json`

These files describe the local machine setup and should not be published as project defaults.

Layer resume state is kept in each layer directory as a stable `progress.svg`.
When an AxiDraw resume produces updated progress, the server writes it to
`progress.next.svg` and then replaces `progress.svg`; it does not create a new
timestamped progress file for every resume.
