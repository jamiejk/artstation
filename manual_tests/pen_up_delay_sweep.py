#!/usr/bin/env python3
"""Pen-up delay sweep test — direct EBB, no server.

Draws a grid of short horizontal lines with varying pen-up delay and
raise-rate values so you can visually compare which combinations eliminate
the endpoint dot/blot.

Stop the plotter server first:  sudo systemctl stop plotter
"""

from __future__ import annotations

import argparse
import math
import serial
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server.hardware import (
    current_axidraw_servo_config,
    raw_command,
    read_step_position,
    run_pen_servo_on_port,
    wait_for_motion_idle,
    xy_mm_to_steps,
)

DEFAULT_SERVO_CONFIG = ROOT / "axidraw_servo_conf.py"


def move_to_on_port(
    port: serial.Serial,
    target_x_mm: float,
    target_y_mm: float,
    *,
    speed_mm_s: float,
    current: dict | None = None,
) -> dict:
    """Absolute XY move at fixed speed. Returns new position dict."""
    if current is None:
        _a1, _a2, current = read_step_position(port)

    dx = target_x_mm - current["x_mm"]
    dy = target_y_mm - current["y_mm"]
    dist = math.hypot(dx, dy)
    if dist < 0.002:
        return current

    axis_1, axis_2 = xy_mm_to_steps(dx, dy)
    dur_ms = max(40, int(round(dist / speed_mm_s * 1000)))
    resp = raw_command(port, f"SM,{dur_ms},{axis_1},{axis_2}\r")
    if not resp.startswith("OK"):
        raise RuntimeError(f"EBB move failed: {resp!r}")
    wait_for_motion_idle(port, max(2.0, dur_ms / 1000.0 + 1.0))
    _a1, _a2, new_pos = read_step_position(port)
    return new_pos



def draw_stroke(
    port: serial.Serial,
    *,
    from_x: float,
    y: float,
    length_mm: float,
    speed_mm_s: float,
    axicli_config: Path,
    up_pos: int,
    down_pos: int,
    raise_rate: int,
    delay_up_ms: int,
    delay_down_ms: int,
    pos: dict | None = None,
) -> dict:
    """Lower pen, draw a horizontal line, raise pen, return new position."""
    run_pen_servo_on_port(
        port,
        axicli_config=axicli_config,
        raised=False,
        up_pos=up_pos,
        down_pos=down_pos,
        raise_rate=raise_rate,
        lower_rate=20,
        delay_up_ms=delay_up_ms,
        delay_down_ms=delay_down_ms,
    )
    pos = move_to_on_port(
        port, from_x + length_mm, y, speed_mm_s=speed_mm_s, current=pos,
    )
    run_pen_servo_on_port(
        port,
        axicli_config=axicli_config,
        raised=True,
        up_pos=up_pos,
        down_pos=down_pos,
        raise_rate=raise_rate,
        lower_rate=20,
        delay_up_ms=delay_up_ms,
        delay_down_ms=delay_down_ms,
    )
    return pos



def main() -> int:
    parser = argparse.ArgumentParser(
        description="Pen-up delay sweep — draws short lines with graduated pen-up timing."
    )
    parser.add_argument("--port", default="/dev/ttyACM0")
    parser.add_argument("--servo-config", type=Path, default=DEFAULT_SERVO_CONFIG)
    parser.add_argument("--up", type=int, default=100, help="pen_pos_up (0-100)")
    parser.add_argument("--down", type=int, default=80, help="pen_pos_down (0-100)")
    parser.add_argument("--speed", type=float, default=40, help="draw speed mm/s")
    parser.add_argument("--length", type=float, default=30, help="line length mm")
    parser.add_argument("--gap-mm", type=float, default=8, help="spacing between rows")
    parser.add_argument("--start-x", type=float, default=60, help="left edge of lines")
    parser.add_argument("--start-y", type=float, default=60, help="top row Y")
    parser.add_argument("--pen-delay-down", type=int, default=-50)
    parser.add_argument("--go", action="store_true", help="actually move hardware")
    args = parser.parse_args()

    delays = [-175, -150, -125, -100, -75, -50, -25, 0]
    rates = [100, 75, 50]

    config = current_axidraw_servo_config(args.servo_config)

    # Print plan
    print(f"Config:  min={config['servo_min']}  max={config['servo_max']}  "
          f"sweep_time={config['servo_sweep_time']}")
    print(f"Pen:     up={args.up}  down={args.down}")
    print(f"Stroke:  {args.length} mm at {args.speed} mm/s")
    print(f"Start:   ({args.start_x:.0f}, {args.start_y:.0f})")
    print()
    print(f"{'Row':>4s}  {'delay_up':>8s}  {'raise_rate':>10s}  "
          f"{'expect':>24s}")
    print("-" * 62)

    row = 0
    for rate in rates:
        for delay in delays:
            row += 1
            if delay < -100:
                expect = "XY before lift → drag?"
            elif delay < -50:
                expect = "mid-lift → short tail?"
            elif delay < 0:
                expect = "near end of lift → crisp?"
            elif delay == 0:
                expect = "full settle → maybe dot?"
            else:
                expect = f"+{delay}ms pause → dot likely"
            print(f"{row:>4d}  {delay:>8d}  {rate:>10d}  {expect:>24s}")

    if not args.go:
        print("\nAdd --go to run this test on the hardware.")
        return 0

    print("\nConnecting to plotter...")
    with serial.Serial(args.port, timeout=2) as port:
        _a1, _a2, pos = read_step_position(port)
        print(f"  start: ({pos['x_mm']:.2f}, {pos['y_mm']:.2f})")

        run_pen_servo_on_port(
            port,
            axicli_config=args.servo_config,
            raised=True,
            up_pos=args.up,
            down_pos=args.down,
            raise_rate=100,
            lower_rate=20,
            delay_up_ms=0,
            delay_down_ms=args.pen_delay_down,
        )

        row = 0
        y = args.start_y
        for rate in rates:
            for delay in delays:
                row += 1
                print(f"  row {row:>2d}: delay_up={delay:>4d}  "
                      f"rate={rate:>3d}  y={y:.1f}")
                pos = move_to_on_port(
                    port, args.start_x, y, speed_mm_s=80, current=pos,
                )
                pos = draw_stroke(
                    port,
                    axicli_config=args.servo_config,
                    from_x=args.start_x,
                    y=y,
                    length_mm=args.length,
                    speed_mm_s=args.speed,
                    up_pos=args.up,
                    down_pos=args.down,
                    raise_rate=rate,
                    delay_up_ms=delay,
                    delay_down_ms=args.pen_delay_down,
                    pos=pos,
                )
                y += args.gap_mm

        run_pen_servo_on_port(
            port,
            axicli_config=args.servo_config,
            raised=True,
            up_pos=args.up,
            down_pos=args.down,
            raise_rate=100,
            lower_rate=20,
            delay_up_ms=0,
            delay_down_ms=args.pen_delay_down,
        )
        print(f"\n  done — final: ({pos['x_mm']:.2f}, {pos['y_mm']:.2f})")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
