#!/usr/bin/env python3
"""Manual experiment: soft pen entry/exit via mid-stroke Z (direct EBB).

Stop the plotter server before running. Park pen UP at the start of scrap paper.
See docs/z-ramp-stroke-experiment.md for the morning procedure.

This is intentionally outside automated unittest discovery.
"""

from __future__ import annotations

import argparse
import math
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import serial

# Repo root = parent of manual_tests/
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SERVO_CONFIG = ROOT / "axidraw_servo_conf.py"
NATIVE_RES_FACTOR = 1016.0

# End-dot / clearance focus.
# Per-row: ramp_mm, tail_mm, curve (linear|front), out_speed, hold_z (for clearance).
# hold_z mode: move full stroke at fixed height — any mark means that height still contacts.
ROW_SPECS = {
    # --- Clearance ladder: should leave NO ink if height truly clears paper ---
    "A": {
        "name": "A_clear_100",
        "mode": "hold_z",
        "hold_z": 100,
        "ramp_mm": 0,
    },
    "B": {
        "name": "B_clear_96",
        "mode": "hold_z",
        "hold_z": 96,
        "ramp_mm": 0,
    },
    "C": {
        "name": "C_clear_90",
        "mode": "hold_z",
        "hold_z": 90,
        "ramp_mm": 0,
    },
    "D": {
        "name": "D_clear_80",
        "mode": "hold_z",
        "hold_z": 80,
        "ramp_mm": 0,
    },
    "E": {
        "name": "E_clear_70",
        "mode": "hold_z",
        "hold_z": 70,
        "ramp_mm": 0,
    },
    # --- Soft-out with max software up (100); lighter mid-stroke contact optional via CLI ---
    "F": {
        "name": "F_hard",
        "mode": "hard_in_hard_out",
        "ramp_mm": 0,
    },
    "G": {
        "name": "G_front_r14_t8",
        "mode": "hard_in_soft_out",
        "ramp_mm": 14,
        "tail_mm": 8.0,
        "curve": "front",
        "out_speed": 15.0,
    },
    "H": {
        "name": "H_front_r16_t10",
        "mode": "hard_in_soft_out",
        "ramp_mm": 16,
        "tail_mm": 10.0,
        "curve": "front",
        "out_speed": 12.0,
    },
    "I": {
        "name": "I_front_r18_t12",
        "mode": "hard_in_soft_out",
        "ramp_mm": 18,
        "tail_mm": 12.0,
        "curve": "front",
        "out_speed": 10.0,
    },
    # Raise early, long slow clear, then true SP,1 top before stop (see draw_stroke)
    "J": {
        "name": "J_front_early_settle",
        "mode": "hard_in_soft_out",
        "ramp_mm": 20,
        "tail_mm": 12.0,
        "curve": "front",
        "out_speed": 10.0,
        "settle_tail": True,
    },
    # --- Long-line gradual entry + exit comparison ---
    "K": {
        "name": "K_long_hard_baseline",
        "mode": "hard_in_hard_out",
        "ramp_mm": 0,
    },
    "L": {
        "name": "L_gradual_both_r5",
        "mode": "soft_in_soft_out",
        "ramp_mm": 5,
        "tail_mm": 0.5,
        "curve": "linear",
    },
    "M": {
        "name": "M_gradual_both_r10",
        "mode": "soft_in_soft_out",
        "ramp_mm": 10,
        "tail_mm": 1.0,
        "curve": "linear",
    },
    "N": {
        "name": "N_gradual_both_r20",
        "mode": "soft_in_soft_out",
        "ramp_mm": 20,
        "tail_mm": 2.0,
        "curve": "linear",
    },
    "O": {
        "name": "O_gradual_both_r1_25",
        "mode": "soft_in_soft_out",
        "ramp_mm": 1.25,
        "tail_mm": 0.125,
        "curve": "linear",
    },
    "P": {
        "name": "P_gradual_both_r2_5",
        "mode": "soft_in_soft_out",
        "ramp_mm": 2.5,
        "tail_mm": 0.25,
        "curve": "linear",
    },
    "Q": {
        "name": "Q_gradual_both_r3_75",
        "mode": "soft_in_soft_out",
        "ramp_mm": 3.75,
        "tail_mm": 0.375,
        "curve": "linear",
    },
    "R": {
        "name": "R_gradual_both_r4_0625",
        "mode": "soft_in_soft_out",
        "ramp_mm": 4.0625,
        "tail_mm": 0.40625,
        "curve": "linear",
    },
    "S": {
        "name": "S_gradual_both_r4_375",
        "mode": "soft_in_soft_out",
        "ramp_mm": 4.375,
        "tail_mm": 0.4375,
        "curve": "linear",
    },
    "T": {
        "name": "T_gradual_both_r4_6875",
        "mode": "soft_in_soft_out",
        "ramp_mm": 4.6875,
        "tail_mm": 0.46875,
        "curve": "linear",
    },
}

DEFAULT_ROWS = "A,B,C,D,E,F,G,H,I,J"


def log(msg: str, fh=None) -> None:
    line = f"{datetime.now(timezone.utc).strftime('%H:%M:%S')} {msg}"
    print(line, flush=True)
    if fh is not None:
        fh.write(line + "\n")
        fh.flush()


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


def ebb_ok(port: serial.Serial, command: str) -> str:
    response = raw_command(port, command)
    if not response.startswith("OK"):
        raise RuntimeError(f"Unexpected EBB response to {command.strip()!r}: {response!r}")
    return response


def drain_input(port: serial.Serial, *, quiet_ms: int = 30) -> None:
    """Discard any leftover serial bytes (prevents OK/QG desync)."""
    deadline = time.monotonic() + quiet_ms / 1000.0
    while time.monotonic() < deadline:
        waiting = getattr(port, "in_waiting", 0) or 0
        if waiting:
            port.read(waiting)
            deadline = time.monotonic() + quiet_ms / 1000.0
        else:
            time.sleep(0.005)


def wait_idle(port: serial.Serial, timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        status_text, _ = send(port, "QG\r", ack=False)
        try:
            status = int(status_text, 16)
        except ValueError:
            drain_input(port)
            time.sleep(0.05)
            continue
        if status & 15 == 0:
            drain_input(port, quiet_ms=15)
            return
        time.sleep(0.04)
    raise TimeoutError("Timed out waiting for EBB motion queue to become idle")


def xy_mm_to_steps(x_mm: float, y_mm: float) -> tuple[int, int]:
    x_in = x_mm / 25.4
    y_in = y_mm / 25.4
    axis_1 = int(round(2 * NATIVE_RES_FACTOR * (x_in + y_in)))
    axis_2 = int(round(2 * NATIVE_RES_FACTOR * (x_in - y_in)))
    return axis_1, axis_2


def bed_delta_to_raw_delta(x_mm: float, y_mm: float) -> tuple[float, float]:
    # Same transform as server/positioning.py
    return -y_mm, -x_mm


def load_servo_config(path: Path) -> dict:
    config = {
        "servo_pin": 1,
        "servo_min": 9855,
        "servo_max": 27831,
        "servo_sweep_time": 200.0,
    }
    if path.exists():
        import runpy

        loaded = runpy.run_path(str(path))
        for key in list(config):
            if key in loaded:
                config[key] = loaded[key]
    return {
        "servo_pin": int(config["servo_pin"]),
        "servo_min": int(config["servo_min"]),
        "servo_max": int(config["servo_max"]),
        "servo_sweep_time": float(config["servo_sweep_time"]),
    }


def pen_pos_to_pwm(config: dict, pen_position: float) -> int:
    slope = float(config["servo_max"] - config["servo_min"]) / 100.0
    return int(round(config["servo_min"] + slope * float(pen_position)))


def servo_rate_value(config: dict, rate_percent: float) -> int:
    servo_range = max(1, config["servo_max"] - config["servo_min"])
    sweep = max(1.0, float(config["servo_sweep_time"]))
    return max(1, int(round(float(servo_range) * 0.24 / sweep * float(rate_percent))))


def configure_servo_rates(port: serial.Serial, config: dict, rate_percent: float) -> None:
    rate = servo_rate_value(config, rate_percent)
    ebb_ok(port, f"SC,11,{rate}\r")  # raise rate
    ebb_ok(port, f"SC,12,{rate}\r")  # lower rate
    ebb_ok(port, "SC,8,8\r")  # standard PWM channels


def set_pen_height(
    port: serial.Serial,
    config: dict,
    pen_position: float,
    *,
    queue_delay_ms: int = 0,
    label: str = "",
    fh=None,
) -> None:
    """Drive servo to an absolute pen-position (0-100) without long queue blocking.

    Both pen-up and pen-down targets are set to the same PWM so SP,0 reaches that
    height. queue_delay_ms=0 lets a following SM run while the servo is still moving.

    Prefer ``raise_pen_to_top`` when you need a true pen-up at the end of a test:
    collapsing SC,4/SC,5 confuses the EBB pen state (QP) and can leave the holder
    looking "stuck" when a later SP,1 thinks it is already up.
    """
    pwm = pen_pos_to_pwm(config, pen_position)
    pin = config["servo_pin"]
    delay = max(0, int(queue_delay_ms))
    ebb_ok(port, f"SC,4,{pwm}\r")
    ebb_ok(port, f"SC,5,{pwm}\r")
    ebb_ok(port, f"SP,0,{delay},{pin}\r")
    if label:
        log(f"  Z -> {pen_position:g} (pwm={pwm}, queue_delay_ms={delay}) {label}", fh)


def raise_pen_to_top(
    port: serial.Serial,
    config: dict,
    *,
    pen_up: float,
    pen_down: float,
    servo_rate: float,
    settle_ms: int = 300,
    label: str = "final top pen-up",
    fh=None,
) -> None:
    """Restore distinct up/down PWM targets and command a real SP,1 raise."""
    up_pwm = pen_pos_to_pwm(config, pen_up)
    down_pwm = pen_pos_to_pwm(config, pen_down)
    pin = config["servo_pin"]
    rate = servo_rate_value(config, servo_rate)
    delay = max(200, int(settle_ms))
    configure_servo_rates(port, config, servo_rate)
    ebb_ok(port, f"SC,4,{up_pwm}\r")
    ebb_ok(port, f"SC,5,{down_pwm}\r")
    ebb_ok(port, f"SC,11,{rate}\r")
    ebb_ok(port, f"SC,12,{rate}\r")
    # SP,1 = pen up (go to SC,4). Use a non-zero queue delay so the servo is powered
    # through the motion and QP state matches a real raise.
    ebb_ok(port, f"SP,1,{delay},{pin}\r")
    wait_idle(port, max(2.0, delay / 1000.0 + 1.0))
    time.sleep(0.1)
    # QP returns 1/0 only; do not treat as an OK-acked command.
    port.write(b"QP\r")
    qp = port.readline().decode("ascii", errors="replace").strip()
    log(
        f"  {label}: SP,1 up_pwm={up_pwm} down_pwm={down_pwm} "
        f"delay_ms={delay} QP={qp!r}",
        fh,
    )
    if qp not in {"", "1"}:
        # One more hard raise if QP still reports down.
        ebb_ok(port, f"SP,1,{delay},{pin}\r")
        wait_idle(port, max(2.0, delay / 1000.0 + 1.0))
        port.write(b"QP\r")
        qp2 = port.readline().decode("ascii", errors="replace").strip()
        log(f"  {label} retry: QP={qp2!r}", fh)


def move_bed_delta(
    port: serial.Serial,
    x_mm: float,
    y_mm: float,
    speed_mm_s: float,
    *,
    wait: bool = True,
    fh=None,
) -> int:
    raw_x, raw_y = bed_delta_to_raw_delta(x_mm, y_mm)
    axis_1, axis_2 = xy_mm_to_steps(raw_x, raw_y)
    distance = math.hypot(x_mm, y_mm)
    if distance < 0.001:
        return 0
    # Floor at 15 ms so slow soft-out (e.g. 12–18 mm/s over 0.5 mm) is not
    # clamped back up to ~40 mm/s. EBB still accepts short SM durations.
    duration_ms = max(15, int(round(distance / speed_mm_s * 1000.0)))
    response = raw_command(port, f"SM,{duration_ms},{axis_1},{axis_2}\r")
    if not response.startswith("OK"):
        raise RuntimeError(f"SM failed dx={x_mm:g} dy={y_mm:g}: {response!r}")
    log(f"  XY dx={x_mm:g} dy={y_mm:g} mm  duration_ms={duration_ms}", fh)
    if wait:
        wait_idle(port, max(2.0, duration_ms / 1000.0 + 2.0))
    return duration_ms


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def _sample_z(n: int, z0: float, z1: float, curve: str) -> list[float]:
    """Sample n pen heights from z0→z1. front = early lift (sqrt ease)."""
    if n <= 1:
        return [z1]
    curve = (curve or "linear").lower()
    out: list[float] = []
    for i in range(n):
        t = i / (n - 1)
        if curve == "front":
            # t**0.5 → half the height change by t=0.25 (get off paper early)
            t = t**0.5
        elif curve not in ("linear", "lin"):
            raise ValueError(f"Unknown curve {curve!r}; use linear|front")
        out.append(lerp(z0, z1, t))
    return out


def build_z_profile(
    mode: str,
    *,
    length_mm: float,
    ramp_mm: float,
    pen_up: float,
    pen_down: float,
    segment_mm: float,
    step_levels: int,
    light_t: float = 0.55,
    tail_mm: float | None = None,
    curve: str = "linear",
    hold_z: float | None = None,
    **kwargs,
) -> list[tuple[float, float]]:
    """Return [(segment_length_mm, pen_position), ...] covering the stroke."""
    if length_mm <= 0:
        raise ValueError("length_mm must be positive")
    ramp = max(0.0, min(float(ramp_mm), length_mm * 0.85))
    # Allow finer than 0.5 mm; floor at 0.1 so we do not flood the queue.
    seg = max(0.1, float(segment_mm))
    if "light_t" in kwargs and kwargs["light_t"] is not None:
        light_t = float(kwargs["light_t"])
    if "tail_mm" in kwargs and kwargs["tail_mm"] is not None and tail_mm is None:
        tail_mm = float(kwargs["tail_mm"])
    if "curve" in kwargs and kwargs["curve"] is not None:
        curve = str(kwargs["curve"])
    if "hold_z" in kwargs and kwargs["hold_z"] is not None and hold_z is None:
        hold_z = float(kwargs["hold_z"])

    if mode == "hold_z":
        z = float(hold_z) if hold_z is not None else pen_up
        return [(length_mm, z)]

    if mode == "hard_in_hard_out":
        return [(length_mm, pen_down)]

    if mode == "soft_in_hard_out":
        if ramp < 0.5:
            return [(length_mm, pen_down)]
        n_in = max(4, int(round(ramp / seg)))
        zs = _sample_z(n_in, pen_up, pen_down, "linear")
        parts = [(ramp / n_in, z) for z in zs]
        rest = length_mm - ramp
        if rest > 0.05:
            parts.append((rest, pen_down))
        return parts

    if mode == "hard_in_soft_out":
        if ramp < 0.5:
            return [(length_mm, pen_down)]
        # Fully-raised tail: XY keeps moving after commanded pen_up (kills end dots).
        if tail_mm is None:
            tail = min(2.0, max(0.5, seg))
        else:
            tail = max(0.1, float(tail_mm))
        tail = min(tail, ramp - 0.5) if ramp > 0.8 else min(tail, ramp * 0.5)
        lift = max(0.5, ramp - tail)
        rest = length_mm - ramp
        parts: list[tuple[float, float]] = []
        if rest > 0.05:
            parts.append((rest, pen_down))
        n_out = max(4, int(round(lift / seg)))
        zs = _sample_z(n_out, pen_down, pen_up, curve)
        parts.extend((lift / n_out, z) for z in zs)
        parts.append((tail, pen_up))
        return parts

    if mode == "soft_in_soft_out":
        if ramp < 0.5:
            return [(length_mm, pen_down)]
        n_in = max(4, int(round(ramp / seg)))
        if tail_mm is None:
            tail = min(2.0, max(0.5, seg))
        else:
            tail = max(0.1, float(tail_mm))
        tail = min(tail, ramp - 0.5) if ramp > 0.8 else min(tail, ramp * 0.5)
        lift = max(0.5, ramp - tail)
        n_out = max(4, int(round(lift / seg)))
        mid = length_mm - 2 * ramp
        parts = [(ramp / n_in, z) for z in _sample_z(n_in, pen_up, pen_down, "linear")]
        if mid > 0.05:
            parts.append((mid, pen_down))
        parts.extend((lift / n_out, z) for z in _sample_z(n_out, pen_down, pen_up, curve))
        parts.append((tail, pen_up))
        return parts

    if mode == "variable_steps":
        # Triangle: light → heavy → light. light_t in [0,1]: 0=pen_up, 1=pen_down.
        levels = max(3, int(step_levels))
        light_t = max(0.0, min(1.0, float(light_t)))
        light = lerp(pen_up, pen_down, light_t)
        heavy = pen_down
        half = length_mm / 2.0
        n = max(levels, 3)
        parts = []
        for i in range(n):
            z = lerp(light, heavy, i / (n - 1))
            parts.append((half / n, z))
        for i in range(n):
            z = lerp(heavy, light, i / (n - 1))
            parts.append((half / n, z))
        return parts

    raise ValueError(f"Unknown mode: {mode}")


def draw_stroke(
    port: serial.Serial,
    config: dict,
    *,
    mode: str,
    length_mm: float,
    ramp_mm: float,
    pen_up: float,
    pen_down: float,
    speed_mm_s: float,
    segment_mm: float,
    step_levels: int,
    servo_rate: float,
    settle_ms: int,
    light_t: float = 0.55,
    tail_mm: float | None = None,
    curve: str = "linear",
    out_speed: float | None = None,
    approach_mid: bool = True,
    hold_z: float | None = None,
    settle_tail: bool = False,
    x_direction: int = 1,
    fh=None,
) -> None:
    profile = build_z_profile(
        mode,
        length_mm=length_mm,
        ramp_mm=ramp_mm,
        pen_up=pen_up,
        pen_down=pen_down,
        segment_mm=segment_mm,
        step_levels=step_levels,
        light_t=light_t,
        tail_mm=tail_mm,
        curve=curve,
        hold_z=hold_z,
    )
    out_spd = float(out_speed) if out_speed is not None else float(speed_mm_s)
    log(
        f"  profile segments={len(profile)} total_mm={sum(s for s, _ in profile):.2f}"
        f" curve={curve} tail={tail_mm} out_speed={out_spd:g} hold_z={hold_z}",
        fh,
    )
    for i, (seg_len, z) in enumerate(profile):
        log(f"  [{i + 1}/{len(profile)}] len={seg_len:.2f} mm  z={z:.1f}", fh)

    configure_servo_rates(port, config, servo_rate)

    mid_pos = (float(pen_up) + float(pen_down)) / 2.0
    set_pen_height(port, config, pen_up, queue_delay_ms=settle_ms, label="pre-stroke clear", fh=fh)
    wait_idle(port, 2.0)
    time.sleep(settle_ms / 1000.0)

    def hard_to_down(label_prefix: str = "") -> None:
        if approach_mid and mid_pos > pen_down + 1.0:
            set_pen_height(
                port,
                config,
                mid_pos,
                queue_delay_ms=settle_ms,
                label=f"{label_prefix}approach mid {mid_pos:g}",
                fh=fh,
            )
            wait_idle(port, 2.0)
            time.sleep(settle_ms / 1000.0 * 0.5)
        set_pen_height(
            port,
            config,
            pen_down,
            queue_delay_ms=settle_ms,
            label=f"{label_prefix}hard down",
            fh=fh,
        )
        wait_idle(port, 2.0)
        time.sleep(settle_ms / 1000.0)

    # Clearance / drag test: hold fixed Z for whole stroke (no ink if truly clear).
    if mode == "hold_z":
        z = float(hold_z) if hold_z is not None else pen_up
        set_pen_height(port, config, z, queue_delay_ms=settle_ms, label=f"hold_z={z:g}", fh=fh)
        wait_idle(port, 2.0)
        time.sleep(max(0.15, settle_ms / 1000.0))
        move_bed_delta(port, x_direction * length_mm, 0.0, speed_mm_s, wait=True, fh=fh)
        set_pen_height(port, config, pen_up, queue_delay_ms=settle_ms, label="post hold_z up", fh=fh)
        wait_idle(port, 2.0)
        return

    if mode == "hard_in_hard_out":
        hard_to_down()
        move_bed_delta(port, x_direction * length_mm, 0.0, speed_mm_s, wait=True, fh=fh)
        set_pen_height(port, config, pen_up, queue_delay_ms=settle_ms, label="hard up", fh=fh)
        wait_idle(port, 2.0)
        return

    if mode == "hard_in_soft_out":
        hard_to_down()

    soft_out_start = 0
    if mode in ("hard_in_soft_out", "soft_in_soft_out") and ramp_mm > 0:
        soft_out_start = 0
        for i, (seg_len, z) in enumerate(profile):
            if seg_len >= 1.0 and abs(z - pen_down) < 0.05:
                soft_out_start = i + 1
            else:
                break

    # Optional: wait for servo to finish raise before last fully-raised tail segment.
    # Splits final pen_up tail so physical tip is high before last XY motion.
    last_i = len(profile) - 1
    for i, (seg_len, z) in enumerate(profile):
        set_pen_height(
            port,
            config,
            z,
            queue_delay_ms=0,
            label=f"seg {i + 1} target",
            fh=fh,
        )
        spd = out_spd if i >= soft_out_start else speed_mm_s
        # Before the last segment of a settle_tail soft-out: drain queue + true raise.
        if settle_tail and i == last_i and mode == "hard_in_soft_out":
            wait_idle(port, max(2.0, length_mm / min(speed_mm_s, out_spd) + 2.0))
            raise_pen_to_top(
                port,
                config,
                pen_up=pen_up,
                pen_down=pen_down,
                servo_rate=servo_rate,
                settle_ms=max(settle_ms, 350),
                label="settle_tail SP,1 before final XY",
                fh=fh,
            )
            time.sleep(0.15)
        duration_ms = move_bed_delta(
            port,
            x_direction * seg_len,
            0.0,
            spd,
            wait=False,
            fh=fh,
        )
        # Pace the paired SP+SM stream so the EBB's small motion FIFO does not
        # saturate. Keep a small lead so the next pair is available before the
        # current XY segment finishes, matching AxiDraw's dripfeed strategy.
        if duration_ms > 5:
            time.sleep((duration_ms - 5) / 1000.0)

    slowest = min(speed_mm_s, out_spd)
    wait_idle(port, max(3.0, length_mm / slowest + 4.0))
    # settle_tail already did a real SP,1; collapsing SC,4/5 again can desync QP.
    if settle_tail and mode == "hard_in_soft_out":
        raise_pen_to_top(
            port,
            config,
            pen_up=pen_up,
            pen_down=pen_down,
            servo_rate=servo_rate,
            settle_ms=max(settle_ms, 300),
            label="post-stroke top (after settle_tail)",
            fh=fh,
        )
    else:
        set_pen_height(port, config, pen_up, queue_delay_ms=settle_ms, label="post-stroke up", fh=fh)
        wait_idle(port, 2.0)


def parse_rows(text: str) -> list[str]:
    keys = []
    for part in text.replace(" ", "").split(","):
        if not part:
            continue
        key = part.upper()
        if key not in ROW_SPECS:
            raise argparse.ArgumentTypeError(f"Unknown row {part!r}; choose from {','.join(ROW_SPECS)}")
        keys.append(key)
    if not keys:
        raise argparse.ArgumentTypeError("At least one row required")
    return keys


def plan_text(args: argparse.Namespace) -> str:
    mid = (float(args.pen_up) + float(args.pen_down)) / 2.0
    lines = [
        "Z-ramp stroke experiment plan (end-dot / soft-out focus)",
        f"  port={args.port}",
        f"  pen_up={args.pen_up}  pen_down={args.pen_down}  mid_approach={mid:g}",
        f"  servo_rate={args.servo_rate}",
        f"  length_mm={args.length_mm}  default_ramp_mm={args.ramp_mm}  segment_mm={args.segment_mm}",
        f"  speed_mm_s={args.speed}  row_spacing_mm={args.row_spacing}",
        f"  settle_ms={args.settle_ms}  step_levels={args.step_levels}",
        "  Soft-in rows begin clear and lower through their sampled Z profile.",
        "  rows:",
    ]
    for key in args.rows:
        spec = ROW_SPECS[key]
        name = spec["name"]
        mode = spec["mode"]
        ramp = float(spec.get("ramp_mm", args.ramp_mm))
        light_t = float(spec.get("light_t", 0.55))
        tail = spec.get("tail_mm")
        curve = str(spec.get("curve", "linear"))
        out_speed = spec.get("out_speed")
        hold_z = spec.get("hold_z")
        profile = build_z_profile(
            mode,
            length_mm=args.length_mm,
            ramp_mm=ramp,
            pen_up=args.pen_up,
            pen_down=args.pen_down,
            segment_mm=args.segment_mm,
            step_levels=args.step_levels,
            light_t=light_t,
            tail_mm=float(tail) if tail is not None else None,
            curve=curve,
            hold_z=float(hold_z) if hold_z is not None else None,
        )
        z_summary = ", ".join(f"{z:.0f}" for _, z in profile[:8])
        if len(profile) > 8:
            z_summary += ", ..."
        extra = ""
        if mode == "hold_z":
            extra = f" hold_z={hold_z}"
        elif mode != "hard_in_hard_out":
            extra = f" ramp={ramp:g} tail={tail} curve={curve}"
            if out_speed is not None:
                extra += f" out_speed={out_speed:g}"
            if spec.get("settle_tail"):
                extra += " settle_tail"
        if mode == "variable_steps":
            extra += f" light_t={light_t:g}"
        lines.append(f"    {name}: mode={mode}{extra} segs={len(profile)} z≈[{z_summary}]")
    x_label = "+X" if args.x_direction > 0 else "-X"
    lines.append(f"  Layout: each row is {x_label} stroke, then pen-up transit +Y to next row.")
    lines.append(
        "  End position: "
        + ("returned to origin, pen up." if args.return_origin else "right end of last stroke, pen up.")
    )
    lines.append("  Look for: A–E clearance (any ink = not clear); F–J end blots.")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Direct-EBB soft pen entry/exit experiment (stop plotter server first)."
    )
    parser.add_argument("--port", default="/dev/ttyACM0")
    parser.add_argument("--servo-config", type=Path, default=DEFAULT_SERVO_CONFIG)
    parser.add_argument("--pen-up", type=float, default=100.0)
    parser.add_argument("--pen-down", type=float, default=85.0)
    parser.add_argument("--length-mm", type=float, default=40.0)
    parser.add_argument(
        "--ramp-mm",
        type=float,
        default=5.0,
        help="Default soft-in/out length when a row does not override ramp_mm",
    )
    parser.add_argument(
        "--segment-mm",
        type=float,
        default=0.5,
        help="Z sample spacing along ramps (smaller = finer raise/lower steps)",
    )
    parser.add_argument("--speed", type=float, default=40.0, help="XY speed mm/s while drawing")
    parser.add_argument("--row-spacing", type=float, default=8.0, help="Bed Y spacing between rows")
    parser.add_argument("--servo-rate", type=float, default=100.0, help="Servo rate percent 1-100")
    parser.add_argument("--settle-ms", type=int, default=120, help="Blocking settle for hard up/down")
    parser.add_argument("--step-levels", type=int, default=5, help="Steps per half-stroke on variable rows")
    parser.add_argument(
        "--rows",
        type=parse_rows,
        default=parse_rows(DEFAULT_ROWS),
        help=f"Comma-separated rows (default {DEFAULT_ROWS})",
    )
    parser.add_argument("--plan", action="store_true", help="Print plan only (no serial)")
    parser.add_argument("--go", action="store_true", help="Actually move hardware")
    parser.add_argument("--log", type=Path, help="Also write log lines to this file")
    parser.add_argument(
        "--transit-speed",
        type=float,
        default=80.0,
        help="Pen-up transit speed between rows (mm/s)",
    )
    parser.add_argument(
        "--return-origin",
        action="store_true",
        help="After the final pen-up, return XY to the experiment start",
    )
    parser.add_argument(
        "--x-direction",
        type=int,
        choices=(-1, 1),
        default=1,
        help="Stroke direction in bed X: 1 for +X, -1 for -X",
    )
    args = parser.parse_args()

    if args.pen_down >= args.pen_up:
        parser.error("--pen-down must be less than --pen-up (AxiDraw: higher = more raised)")
    if not 1 <= args.servo_rate <= 100:
        parser.error("--servo-rate must be 1-100")
    if args.speed <= 0 or args.transit_speed <= 0:
        parser.error("speeds must be positive")
    if args.length_mm < 5:
        parser.error("--length-mm too short for a useful strip")

    print(plan_text(args))
    print()

    if args.plan and not args.go:
        return 0
    if not args.go:
        print("Refusing to move hardware without --go (use --plan to preview, --go to run).")
        return 2

    log_fh = None
    if args.log:
        args.log.parent.mkdir(parents=True, exist_ok=True)
        log_fh = args.log.open("w", encoding="utf-8")
        log_fh.write(plan_text(args) + "\n\n")

    config = load_servo_config(args.servo_config)
    log(f"Servo config: {args.servo_config} min={config['servo_min']} max={config['servo_max']}", log_fh)

    try:
        with serial.Serial(args.port, timeout=1) as port:
            version, _ = send(port, "v\r", ack=False)
            pen_state, _ = send(port, "QP\r")
            log(f"Firmware: {version}", log_fh)
            log(f"QP pen_up={pen_state == '1'} raw={pen_state!r}", log_fh)

            enable = raw_command(port, "EM,1,1\r")
            log(f"Enable motors: {enable}", log_fh)
            if not enable.startswith("OK"):
                raise RuntimeError(f"Could not enable motors: {enable!r}")

            configure_servo_rates(port, config, args.servo_rate)
            set_pen_height(port, config, args.pen_up, queue_delay_ms=args.settle_ms, label="ensure up", fh=log_fh)
            wait_idle(port, 2.0)

            try:
                for index, key in enumerate(args.rows):
                    spec = ROW_SPECS[key]
                    name = spec["name"]
                    mode = spec["mode"]
                    ramp = float(spec.get("ramp_mm", args.ramp_mm))
                    light_t = float(spec.get("light_t", 0.55))
                    tail = spec.get("tail_mm")
                    curve = str(spec.get("curve", "linear"))
                    out_speed = spec.get("out_speed")
                    hold_z = spec.get("hold_z")
                    settle_tail = bool(spec.get("settle_tail", False))
                    log(
                        f"=== Row {name} ({mode} ramp={ramp:g}"
                        f" tail={tail} curve={curve} out_speed={out_speed}"
                        f" hold_z={hold_z} settle_tail={settle_tail}"
                        + (f" light_t={light_t:g}" if mode == "variable_steps" else "")
                        + ") ===",
                        log_fh,
                    )
                    draw_stroke(
                        port,
                        config,
                        mode=mode,
                        length_mm=args.length_mm,
                        ramp_mm=ramp,
                        pen_up=args.pen_up,
                        pen_down=args.pen_down,
                        speed_mm_s=args.speed,
                        segment_mm=args.segment_mm,
                        step_levels=args.step_levels,
                        servo_rate=args.servo_rate,
                        settle_ms=args.settle_ms,
                        light_t=light_t,
                        tail_mm=float(tail) if tail is not None else None,
                        curve=curve,
                        out_speed=float(out_speed) if out_speed is not None else None,
                        approach_mid=True,
                        hold_z=float(hold_z) if hold_z is not None else None,
                        settle_tail=settle_tail,
                        x_direction=args.x_direction,
                        fh=log_fh,
                    )
                    # Pen-up transit to next row start: left by length, down by spacing
                    if index < len(args.rows) - 1:
                        log("  transit to next row (pen up)", log_fh)
                        set_pen_height(
                            port,
                            config,
                            args.pen_up,
                            queue_delay_ms=args.settle_ms,
                            label="transit up",
                            fh=log_fh,
                        )
                        wait_idle(port, 2.0)
                        move_bed_delta(
                            port,
                            -args.x_direction * args.length_mm,
                            args.row_spacing,
                            args.transit_speed,
                            wait=True,
                            fh=log_fh,
                        )
            finally:
                # Always leave the pen fully raised with a real SP,1 (distinct SC up/down).
                # Mid-stroke Z uses collapsed SC targets; that must not be the final state.
                log(
                    f"Final pen-up to top position ({args.pen_up:g}) after experiment.",
                    log_fh,
                )
                try:
                    # A failed streamed command can leave a late OK response or
                    # a long XY segment occupying the FIFO. Drain it before the
                    # emergency/final servo command.
                    wait_idle(
                        port,
                        max(5.0, args.length_mm / min(args.speed, args.transit_speed) + 3.0),
                    )
                    drain_input(port, quiet_ms=60)
                    raise_pen_to_top(
                        port,
                        config,
                        pen_up=args.pen_up,
                        pen_down=args.pen_down,
                        servo_rate=args.servo_rate,
                        settle_ms=max(args.settle_ms, 400),
                        label="final top pen-up",
                        fh=log_fh,
                    )
                except Exception as exc:
                    log(f"WARNING: final pen-up failed: {exc!r}", log_fh)
                    raise

            if args.return_origin:
                return_y = -args.row_spacing * max(0, len(args.rows) - 1)
                log(
                    f"Returning to experiment origin: "
                    f"dx={-args.x_direction * args.length_mm:g} "
                    f"dy={return_y:g} mm",
                    log_fh,
                )
                move_bed_delta(
                    port,
                    -args.x_direction * args.length_mm,
                    return_y,
                    args.transit_speed,
                    wait=True,
                    fh=log_fh,
                )
                log("Done. Head returned to experiment origin, pen fully up.", log_fh)
            else:
                log("Done. Head is at the right end of the last stroke, pen fully up.", log_fh)
            log(
                "Inspect: A–E = clearance (any trail = height still on paper — seat pen higher). "
                "F = hard end blot; G–J = soft-out at pen_up=100. Prefer clean ends.",
                log_fh,
            )
            return 0
    except serial.SerialException as exc:
        print(
            f"Serial error: {exc}\n"
            "Is the plotter server stopped? Is PLOTTER_PORT correct?",
            file=sys.stderr,
        )
        return 1
    finally:
        if log_fh is not None:
            log_fh.close()


if __name__ == "__main__":
    raise SystemExit(main())
