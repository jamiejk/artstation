#!/usr/bin/env python3
"""Probe AxiDraw pen-servo control via direct EBB SC/SP or AxiCLI.

Default mode is dry-run. Pass --execute to move the servo.

The direct path intentionally mirrors server.server._run_pen_servo_on_port_locked:
  SC,4,<up_pwm>
  SC,5,<down_pwm>
  SC,11,<up_rate>
  SC,12,<down_rate>
  SC,8,8
  SP,<0|1>,<delay_ms>,<servo_pin>
"""

from __future__ import annotations

import argparse
import os
import runpy
import subprocess
import sys
import time
from pathlib import Path

import serial


BASE_DIR = Path.home() / "plotter"
DEFAULT_PORT = os.environ.get("PLOTTER_PORT", "/dev/ttyACM0")
DEFAULT_AXICLI = os.environ.get("AXICLI", str(BASE_DIR / "venv" / "bin" / "axicli"))
DEFAULT_CONFIG = Path(os.environ.get("AXICLI_CONFIG", str(BASE_DIR / "axidraw_servo_conf.py")))


def load_servo_config(path: Path) -> dict:
    config = {
        "servo_pin": 1,
        "servo_min": 9855,
        "servo_max": 27831,
        "servo_sweep_time": 200,
        "servo_move_min": 45,
        "servo_move_slope": 2.69,
    }
    if path.exists():
        loaded = runpy.run_path(str(path))
        for key in config:
            if key in loaded:
                config[key] = loaded[key]
    return {
        "servo_pin": int(config["servo_pin"]),
        "servo_min": int(config["servo_min"]),
        "servo_max": int(config["servo_max"]),
        "servo_sweep_time": float(config["servo_sweep_time"]),
        "servo_move_min": float(config["servo_move_min"]),
        "servo_move_slope": float(config["servo_move_slope"]),
    }


def servo_pwm_for_pen_position(config: dict, pen_position: float) -> int:
    return int(round(config["servo_min"] + (config["servo_max"] - config["servo_min"]) * float(pen_position) / 100.0))


def servo_rate_value(config: dict, rate_percent: float) -> int:
    servo_range = max(1, config["servo_max"] - config["servo_min"])
    servo_sweep_time = max(1.0, float(config["servo_sweep_time"]))
    return max(1, int(round(float(servo_range) * 0.24 / servo_sweep_time * float(rate_percent))))


def servo_travel_delay_ms(config: dict, up_position: float, down_position: float, rate_percent: float) -> int:
    travel_percent = abs(float(up_position) - float(down_position))
    if travel_percent < 0.9:
        return 0
    rate_percent = max(1.0, float(rate_percent))
    mechanical_ms = config["servo_move_slope"] * travel_percent + config["servo_move_min"]
    sweep_ms = config["servo_sweep_time"] * travel_percent / rate_percent
    return max(0, int(round((mechanical_ms**4 + sweep_ms**4) ** 0.25)))


def direct_commands(config: dict, *, position: str, up: int, down: int, raise_rate: int, lower_rate: int) -> list[str]:
    raised = position == "up"
    up_pwm = servo_pwm_for_pen_position(config, up)
    down_pwm = servo_pwm_for_pen_position(config, down)
    rate = raise_rate if raised else lower_rate
    delay_ms = servo_travel_delay_ms(config, up, down, rate)
    return [
        f"SC,4,{up_pwm}\r",
        f"SC,5,{down_pwm}\r",
        f"SC,11,{servo_rate_value(config, raise_rate)}\r",
        f"SC,12,{servo_rate_value(config, lower_rate)}\r",
        "SC,8,8\r",
        f"SP,{1 if raised else 0},{delay_ms},{config['servo_pin']}\r",
    ]


def serial_query(port: serial.Serial, command: str, *, read_ack: bool = True) -> tuple[str, str | None]:
    port.write(command.encode("ascii"))
    value = port.readline().decode("ascii", errors="replace").strip()
    ack = port.readline().decode("ascii", errors="replace").strip() if read_ack else None
    return value, ack


def send_ebb(port: serial.Serial, command: str) -> str:
    port.write(command.encode("ascii"))
    response = port.readline().decode("ascii", errors="replace").strip()
    return response


def run_direct(args: argparse.Namespace, config: dict, position: str) -> int:
    commands = direct_commands(
        config,
        position=position,
        up=args.up,
        down=args.down,
        raise_rate=args.raise_rate,
        lower_rate=args.lower_rate,
    )
    print(f"\nDirect EBB {position}:")
    for command in commands:
        print(f"  {command.strip()}")
    if not args.execute:
        return 0

    with serial.Serial(args.port, timeout=args.timeout) as port:
        before, before_ack = serial_query(port, "QP\r")
        print(f"  QP before -> {before!r}, {before_ack!r}")
        for command in commands:
            response = send_ebb(port, command)
            print(f"  {command.strip()} -> {response!r}")
            if not response.startswith("OK"):
                return 1
        time.sleep(args.settle)
        after, after_ack = serial_query(port, "QP\r")
        print(f"  QP after  -> {after!r}, {after_ack!r}")
    return 0


def run_axicli(args: argparse.Namespace, position: str) -> int:
    cmd = [
        args.axicli,
        "--mode",
        "manual",
        "--manual_cmd",
        "raise_pen" if position == "up" else "lower_pen",
        "--port",
        args.port,
        "--pen_pos_up",
        str(args.up),
        "--pen_pos_down",
        str(args.down),
        "--pen_rate_raise",
        str(args.raise_rate),
        "--pen_rate_lower",
        str(args.lower_rate),
    ]
    if args.config.exists():
        cmd[1:1] = ["--config", str(args.config)]
    print(f"\nAxiCLI {position}:")
    print("  " + " ".join(cmd))
    if not args.execute:
        return 0
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    print(proc.stdout.rstrip())
    return proc.returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--method", choices=["direct", "axicli", "both"], default="both")
    parser.add_argument("--sequence", default="down,up", help="Comma-separated positions: up,down")
    parser.add_argument("--up", type=int, default=100)
    parser.add_argument("--down", type=int, default=0)
    parser.add_argument("--raise-rate", type=int, default=100)
    parser.add_argument("--lower-rate", type=int, default=50)
    parser.add_argument("--port", default=DEFAULT_PORT)
    parser.add_argument("--axicli", default=DEFAULT_AXICLI)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--timeout", type=float, default=1.0)
    parser.add_argument("--settle", type=float, default=0.75)
    parser.add_argument("--execute", action="store_true", help="Actually move the pen servo")
    args = parser.parse_args(argv)

    for name, value in (("up", args.up), ("down", args.down)):
        if not 0 <= value <= 100:
            parser.error(f"{name} must be in AxiCLI's 0..100 range")

    config = load_servo_config(args.config)
    print("Servo config:")
    print(f"  config={args.config if args.config.exists() else '(defaults)'}")
    print(f"  port={args.port}")
    print(f"  servo_pin={config['servo_pin']}")
    print(f"  servo_min={config['servo_min']} servo_max={config['servo_max']}")
    print(f"  up={args.up} -> pwm={servo_pwm_for_pen_position(config, args.up)}")
    print(f"  down={args.down} -> pwm={servo_pwm_for_pen_position(config, args.down)}")
    if not args.execute:
        print("\nDry run only. Re-run with --execute to move the servo.")

    rc = 0
    for position in [part.strip().lower() for part in args.sequence.split(",") if part.strip()]:
        if position not in {"up", "down"}:
            parser.error(f"Invalid sequence position: {position!r}")
        if args.method in {"direct", "both"}:
            rc = run_direct(args, config, position) or rc
        if args.method in {"axicli", "both"}:
            rc = run_axicli(args, position) or rc
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
