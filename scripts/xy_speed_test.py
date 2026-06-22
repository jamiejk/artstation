#!/usr/bin/env python3
import argparse
import math
import sys
import time

import serial


NATIVE_RES_FACTOR = 1016.0


def send(port: serial.Serial, command: str, *, ack: bool = True) -> tuple[str, str | None]:
    port.write(command.encode("ascii"))
    value = port.readline().decode("ascii", errors="replace").strip()
    ack_value = None
    if ack:
        ack_value = port.readline().decode("ascii", errors="replace").strip()
    return value, ack_value


def raw_command(port: serial.Serial, command: str) -> str:
    port.write(command.encode("ascii"))
    return port.readline().decode("ascii", errors="replace").strip()


def wait_idle(port: serial.Serial, timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        status_text, _ = send(port, "QG\r", ack=False)
        try:
            status = int(status_text, 16)
        except ValueError:
            time.sleep(0.05)
            continue
        if status & 15 == 0:
            return
        time.sleep(0.04)
    raise TimeoutError("Timed out waiting for motion queue to become idle")


def xy_mm_to_steps(x_mm: float, y_mm: float) -> tuple[int, int]:
    x_in = x_mm / 25.4
    y_in = y_mm / 25.4
    axis_1 = int(round(2 * NATIVE_RES_FACTOR * (x_in + y_in)))
    axis_2 = int(round(2 * NATIVE_RES_FACTOR * (x_in - y_in)))
    return axis_1, axis_2


def bed_delta_to_raw_delta(x_mm: float, y_mm: float) -> tuple[float, float]:
    return -y_mm, -x_mm


def move_bed_delta(port: serial.Serial, x_mm: float, y_mm: float, speed_mm_s: float) -> None:
    raw_x_mm, raw_y_mm = bed_delta_to_raw_delta(x_mm, y_mm)
    axis_1, axis_2 = xy_mm_to_steps(raw_x_mm, raw_y_mm)
    distance = math.hypot(x_mm, y_mm)
    duration_ms = max(40, int(round(distance / speed_mm_s * 1000)))
    response = raw_command(port, f"SM,{duration_ms},{axis_1},{axis_2}\r")
    if not response.startswith("OK"):
        raise RuntimeError(f"SM failed for {x_mm:g},{y_mm:g}mm at {speed_mm_s:g}mm/s: {response!r}")
    wait_idle(port, max(2.0, duration_ms / 1000.0 + 2.0))


def main() -> int:
    parser = argparse.ArgumentParser(description="Run standalone CoreXY travel speed tests with the pen up.")
    parser.add_argument("--port", default="/dev/ttyACM0")
    parser.add_argument("--speed", type=float, required=True, help="XY travel speed in mm/s")
    parser.add_argument("--width", type=float, default=300.0, help="Rectangle width in bed X mm")
    parser.add_argument("--height", type=float, default=150.0, help="Rectangle height in bed Y mm")
    parser.add_argument("--cycles", type=int, default=1)
    parser.add_argument("--dx", type=float, help="Run one bed-space X move instead of a rectangle")
    parser.add_argument("--dy", type=float, help="Run one bed-space Y move instead of a rectangle")
    parser.add_argument("--settle", type=float, default=0.15, help="Pause between moves in seconds")
    parser.add_argument("--allow-pen-down", action="store_true")
    args = parser.parse_args()

    if args.speed <= 0:
        parser.error("--speed must be positive")
    single_move = args.dx is not None or args.dy is not None

    if not single_move and (args.width <= 0 or args.height <= 0):
        parser.error("--width and --height must be positive")
    if args.cycles < 1:
        parser.error("--cycles must be at least 1")

    if single_move:
        moves = [(args.dx or 0.0, args.dy or 0.0)]
    else:
        moves = [
            (args.width, 0.0),
            (0.0, args.height),
            (-args.width, 0.0),
            (0.0, -args.height),
        ]

    with serial.Serial(args.port, timeout=1) as port:
        version, _ = send(port, "v\r", ack=False)
        pen_up, pen_ack = send(port, "QP\r")
        print(f"Port: {args.port}")
        print(f"Firmware: {version}")
        print(f"Pen up: {pen_up == '1'} ({pen_up}, {pen_ack})")
        if pen_up != "1" and not args.allow_pen_down:
            print("Refusing to run with pen down. Raise the pen or pass --allow-pen-down.", file=sys.stderr)
            return 2

        enable_response = raw_command(port, "EM,1,1\r")
        print(f"Enable motors: {enable_response}")
        if not enable_response.startswith("OK"):
            raise RuntimeError(f"Could not enable motors: {enable_response!r}")

        if single_move:
            print(f"Running one move: dx={moves[0][0]:g} dy={moves[0][1]:g} mm at {args.speed:g} mm/s")
        else:
            print(
                f"Running {args.cycles} cycle(s): {args.width:g} x {args.height:g} mm "
                f"at {args.speed:g} mm/s"
            )
        for cycle in range(args.cycles):
            if not single_move:
                print(f"Cycle {cycle + 1}/{args.cycles}")
            for x_mm, y_mm in moves:
                start = time.monotonic()
                move_bed_delta(port, x_mm, y_mm, args.speed)
                elapsed = time.monotonic() - start
                print(f"  move dx={x_mm:g} dy={y_mm:g} elapsed={elapsed:.3f}s")
                time.sleep(args.settle)

        if single_move:
            print("Done. Final position has intentionally changed.")
        else:
            print("Done. Final command should have returned to the start point.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
