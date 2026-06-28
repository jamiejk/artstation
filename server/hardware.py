"""Low-level EiBotBoard/AxiDraw hardware primitives.

This module intentionally avoids FastAPI, job state, and UI concepts. It owns
serial command formatting, native step conversion, motor-state reads, and direct
pen-servo control. Higher-level orchestration remains in ``server.py``.
"""

from __future__ import annotations

from pathlib import Path
import math
import runpy
import time

import serial


def serial_query(port: serial.Serial, command: str, *, ack: bool = True) -> tuple[str, str | None]:
    port.write(command.encode("ascii"))
    value = port.readline().decode("ascii", errors="replace").strip()
    ack_value = None
    if ack:
        ack_value = port.readline().decode("ascii", errors="replace").strip()
    return value, ack_value


def steps_to_xy_mm(axis_1: int, axis_2: int) -> dict:
    # Matches AxiDraw's walk_home math for high-resolution mode.
    native_res_factor = 1016.0
    x_in = (axis_1 + axis_2) / (4 * native_res_factor)
    y_in = (axis_1 - axis_2) / (4 * native_res_factor)
    return {"x_mm": x_in * 25.4, "y_mm": y_in * 25.4}


def xy_mm_to_steps(x_mm: float, y_mm: float) -> tuple[int, int]:
    # Inverse of steps_to_xy_mm. Axis values are EBB motor step deltas.
    native_res_factor = 1016.0
    x_in = x_mm / 25.4
    y_in = y_mm / 25.4
    axis_1 = int(round(2 * native_res_factor * (x_in + y_in)))
    axis_2 = int(round(2 * native_res_factor * (x_in - y_in)))
    return axis_1, axis_2


def raw_command(port: serial.Serial, command: str) -> str:
    port.write(command.encode("ascii"))
    return port.readline().decode("ascii", errors="replace").strip()


def read_motor_resolution(port: serial.Serial) -> tuple[int | None, int | None]:
    def pin(command: str) -> bool:
        value, _ack = serial_query(port, command, ack=False)
        return value.rsplit(",", 1)[-1].strip() == "1"

    enable_1 = not pin("PI,E,0\r")
    enable_2 = not pin("PI,C,1\r")
    ms_1 = pin("PI,E,2\r")
    ms_2 = pin("PI,E,1\r")
    ms_3 = pin("PI,A,6\r")

    if ms_1 and ms_2 and ms_3:
        resolution = 1
    elif ms_1 and ms_2:
        resolution = 2
    elif ms_2:
        resolution = 3
    elif ms_1:
        resolution = 4
    else:
        resolution = 5
    return resolution if enable_1 else 0, resolution if enable_2 else 0


def wait_for_motion_idle(port: serial.Serial, timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        status_text, _ = serial_query(port, "QG\r", ack=False)
        try:
            status = int(status_text, 16)
        except ValueError:
            time.sleep(0.05)
            continue
        if status & 15 == 0:
            return
        time.sleep(0.04)
    raise TimeoutError("Timed out waiting for EBB motion queue to become idle")


def read_step_position(port: serial.Serial) -> tuple[int, int, dict]:
    steps, _ = serial_query(port, "QS\r")
    axis_1_text, axis_2_text = steps.split(",", 1)
    axis_1 = int(axis_1_text)
    axis_2 = int(axis_2_text)
    return axis_1, axis_2, steps_to_xy_mm(axis_1, axis_2)


def ebb_command(port: serial.Serial, command: str) -> str:
    port.write(command.encode("ascii"))
    response = ""
    for _ in range(101):
        response = port.readline().decode("ascii", errors="replace").strip()
        if response:
            break
    if not response.startswith("OK"):
        raise RuntimeError(f"Unexpected EBB response to {command.strip()!r}: {response!r}")
    return response


def current_axidraw_servo_config(axicli_config: Path) -> dict:
    config = {
        "servo_pin": 1,
        "servo_min": 9855,
        "servo_max": 27831,
        "servo_sweep_time": 200,
        "servo_move_min": 45,
        "servo_move_slope": 2.69,
    }
    if axicli_config.exists():
        try:
            loaded = runpy.run_path(str(axicli_config))
        except Exception as exc:
            raise RuntimeError(f"Could not load AxiDraw servo config {axicli_config}: {exc!r}") from exc
        for key in config:
            if key in loaded:
                config[key] = loaded[key]
    try:
        return {
            "servo_pin": int(config["servo_pin"]),
            "servo_min": int(config["servo_min"]),
            "servo_max": int(config["servo_max"]),
            "servo_sweep_time": float(config["servo_sweep_time"]),
            "servo_move_min": float(config["servo_move_min"]),
            "servo_move_slope": float(config["servo_move_slope"]),
        }
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Invalid AxiDraw servo config values in {axicli_config}") from exc


def servo_pwm_for_pen_position(config: dict, pen_position: float) -> int:
    servo_min = config["servo_min"]
    servo_max = config["servo_max"]
    servo_slope = float(servo_max - servo_min) / 100.0
    return int(round(servo_min + servo_slope * float(pen_position)))


def servo_rate_value(config: dict, rate_percent: float) -> int:
    servo_range = max(1, config["servo_max"] - config["servo_min"])
    servo_sweep_time = max(1.0, float(config["servo_sweep_time"]))
    # Matches axidrawinternal.pen_handling for the standard 8-channel servo PWM mode.
    return max(1, int(round(float(servo_range) * 0.24 / servo_sweep_time * float(rate_percent))))


def servo_travel_delay_ms(
    config: dict,
    up_position: float,
    down_position: float,
    rate_percent: float,
    *,
    extra_settle_ms: int = 0,
) -> int:
    travel_percent = abs(float(up_position) - float(down_position))
    if travel_percent < 0.9:
        return 0
    rate_percent = max(1.0, float(rate_percent))
    mechanical_ms = config["servo_move_slope"] * travel_percent + config["servo_move_min"]
    sweep_ms = config["servo_sweep_time"] * travel_percent / rate_percent
    return max(
        0,
        int(round(((mechanical_ms ** 4 + sweep_ms ** 4) ** 0.25) + extra_settle_ms)),
    )


def run_pen_servo_on_port(
    port: serial.Serial,
    *,
    axicli_config: Path,
    raised: bool,
    up_pos: int,
    down_pos: int,
    raise_rate: int,
    lower_rate: int,
    delay_up_ms: int = 0,
    delay_down_ms: int = 0,
    extra_settle_ms: int = 0,
    label: str | None = None,
    log=None,
) -> dict:
    config = current_axidraw_servo_config(axicli_config)
    up_pwm = servo_pwm_for_pen_position(config, up_pos)
    down_pwm = servo_pwm_for_pen_position(config, down_pos)
    up_rate = servo_rate_value(config, raise_rate)
    down_rate = servo_rate_value(config, lower_rate)
    rate = raise_rate if raised else lower_rate
    configured_delay_ms = delay_up_ms if raised else delay_down_ms
    delay_ms = servo_travel_delay_ms(
        config,
        up_pos,
        down_pos,
        rate,
        extra_settle_ms=extra_settle_ms + configured_delay_ms,
    )
    pen_state = 1 if raised else 0
    if log is not None:
        log.write(
            (label or ("Raise pen" if raised else "Lower pen"))
            + f": direct EBB servo command, delay={delay_ms} ms\n"
        )
        log.flush()
    ebb_command(port, f"SC,4,{up_pwm}\r")
    ebb_command(port, f"SC,5,{down_pwm}\r")
    ebb_command(port, f"SC,11,{up_rate}\r")
    ebb_command(port, f"SC,12,{down_rate}\r")
    ebb_command(port, "SC,8,8\r")
    ebb_command(port, f"SP,{pen_state},{delay_ms},{config['servo_pin']}\r")
    return {
        "ok": True,
        "position": "up" if raised else "down",
        "method": "direct_ebb",
        "delay_ms": delay_ms,
        "up_pwm": up_pwm,
        "down_pwm": down_pwm,
    }


def move_to_bed_target_on_port(
    port: serial.Serial,
    target: dict,
    *,
    current_position: dict,
    bed_delta_to_raw_delta,
    validate_bed_target,
    update_position,
    speed_mm_s: float,
    log=None,
) -> dict:
    target_x, target_y = validate_bed_target(target["x_mm"], target["y_mm"])
    delta_x = target_x - current_position["x_mm"]
    delta_y = target_y - current_position["y_mm"]
    distance = math.hypot(delta_x, delta_y)
    if distance < 0.001:
        return current_position

    raw_delta = bed_delta_to_raw_delta(delta_x, delta_y)
    axis_1_delta, axis_2_delta = xy_mm_to_steps(raw_delta["x_mm"], raw_delta["y_mm"])
    duration_ms = max(40, int(round(distance / speed_mm_s * 1000)))
    if log is not None:
        log.write(
            f"Move from ({current_position['x_mm']:.3f}, {current_position['y_mm']:.3f}) to "
            f"({target_x:.3f}, {target_y:.3f}) at {speed_mm_s:.1f} mm/s\n"
        )
        log.flush()
    response = raw_command(port, f"SM,{duration_ms},{axis_1_delta},{axis_2_delta}\r")
    if not response.startswith("OK"):
        raise RuntimeError(f"EBB move failed: {response!r}")
    wait_for_motion_idle(port, max(2.0, duration_ms / 1000.0 + 1.0))
    _axis_1_after, _axis_2_after, raw_after = read_step_position(port)
    return update_position(raw_after)
