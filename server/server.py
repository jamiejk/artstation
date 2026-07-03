from fastapi import FastAPI, UploadFile, File, Form, Header, HTTPException, Request, Body
from fastapi.responses import FileResponse
from pathlib import Path
from typing import Optional, List
from types import SimpleNamespace
import os
import time
import uuid
import shutil
import queue
import threading
import subprocess
import signal
import secrets
import json
import math
import serial

try:
    from server import hardware
    from server import job_model
    from server import job_runner
    from server import plot_execution
    from server import positioning
    from server import state_store
    from server import settings as settings_utils
    from server import svg_utils
    from server import timing_log
except ImportError:
    import hardware
    import job_model
    import job_runner
    import plot_execution
    import positioning
    import state_store
    import settings as settings_utils
    import svg_utils
    import timing_log

try:
    from server.ink_dip import (
        estimate_checkpoint_schedule,
        find_keepout_collision,
        parse_plob_polylines,
        write_checkpoint_digest,
    )
except ModuleNotFoundError:
    from ink_dip import (
        estimate_checkpoint_schedule,
        find_keepout_collision,
        parse_plob_polylines,
        write_checkpoint_digest,
    )

APP_NAME = "ArtStation Layer Plotter Server"

BASE_DIR = Path.home() / "plotter"
VERSION_PATH = BASE_DIR / "VERSION"
JOBS_DIR = BASE_DIR / "jobs"
LOGS_DIR = BASE_DIR / "logs"
STATIC_DIR = BASE_DIR / "server" / "static"
POSITION_PATH = BASE_DIR / "plotter_position.json"
PEN_SETTINGS_PATH = BASE_DIR / "plotter_pen_settings.json"
PLOT_SETTINGS_PATH = BASE_DIR / "plotter_plot_settings.json"
INK_WELL_SETTINGS_PATH = BASE_DIR / "plotter_ink_well_settings.json"
PAPER_SETTINGS_PATH = BASE_DIR / "plotter_paper_settings.json"

AXICLI = os.environ.get("AXICLI", str(BASE_DIR / "venv" / "bin" / "axicli"))
PLOTTER_PORT = os.environ.get("PLOTTER_PORT", "/dev/ttyACM0")
AXICLI_CONFIG = Path(os.environ.get("AXICLI_CONFIG", str(BASE_DIR / "axidraw_servo_conf.py")))

JOBS_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title=APP_NAME)

job_queue: queue.Queue[str] = queue.Queue()
jobs: dict[str, dict] = {}
jobs_lock = threading.RLock()
hardware_lock = threading.RLock()
active_process_lock = threading.RLock()
active_process: subprocess.Popen | None = None
active_process_job_id: str | None = None
motor_resolution_cache_lock = threading.RLock()
motor_resolution_cache = {"value": None, "checked_at": 0.0}
hardware_state_lock = threading.RLock()
cached_hardware_state: dict = {
    "busy": False,
    "connected": False,
    "port": PLOTTER_PORT,
    "message": "Hardware telemetry has not been read yet",
    "telemetry_stale": True,
    "telemetry_updated_at": None,
}
manual_hardware_priority_lock = threading.RLock()
manual_hardware_priority_until = 0.0
position_lock = threading.RLock()
position_offset = {"x_mm": 0.0, "y_mm": 0.0}
position_current: dict | None = None
home_position: dict | None = None
position_calibration_id = uuid.uuid4().hex
position_calibration_enabled = True
pen_settings_lock = threading.RLock()
pen_settings = {"pen_pos_up": 65, "pen_pos_down": 35}
AXIDRAW_PEN_POSITION_MIN = settings_utils.AXIDRAW_PEN_POSITION_MIN
AXIDRAW_PEN_POSITION_MAX = settings_utils.AXIDRAW_PEN_POSITION_MAX
plot_settings_lock = threading.RLock()
plot_settings = {
    "speed_pendown": 15,
    "speed_penup": 40,
    "pen_delay_down": 0,
    "pen_delay_up": 0,
    "pen_rate_raise": 75,
    "auto_dip_enabled": False,
    "dip_interval_s": 60.0,
}
ink_well_settings_lock = threading.RLock()
ink_well_settings = {
    "state_version": 1,
    "installed": False,
    "centre": None,
    "radius_mm": None,
    "clearance_pos": None,
    "dip_pos": None,
    "dwell_ms": 1000,
    "drip_dwell_ms": 0,
    "dip_circle_count": 3,
    "dip_circle_diameter_mm": 10.0,
    "test_passed": False,
    "tested_at": None,
}
PAPER_SIZES_MM = settings_utils.PAPER_SIZES_MM
paper_settings_lock = threading.RLock()
paper_settings = {
    "state_version": 1,
    "enabled": True,
    "size": "A3",
    "orientation": "portrait",
    "top_right": None,
}
POSITION_STATE_VERSION = 2

operator_event = threading.Event()
operator_lock = threading.Lock()
operator_prompt: dict = {
    "active": False,
    "job_id": None,
    "message": None,
    "action": None,
    "created_at": None,
}


ACTIVE_CANCELLABLE_STATUSES = {
    "queued",
    "queued_for_operator",
    "waiting_for_operator",
    "dip_failed",
}

RUNNING_STATUSES = {
    "running",
    "queued_for_resume",
    "dipping",
}

PLOT_PREVIEW_STATUSES = {
    "queued",
    "queued_for_operator",
    "waiting_for_operator",
    "queued_for_resume",
    "running",
    "paused",
    "dipping",
    "dip_failed",
}

JOB_DELETE_BLOCKED_STATUSES = {
    "queued",
    "queued_for_operator",
    "waiting_for_operator",
    "queued_for_resume",
    "running",
    "dipping",
}


def required_plotter_token(env: dict | None = None) -> str:
    env = os.environ if env is None else env
    token = env.get("PLOTTER_TOKEN", "").strip()
    if not token:
        raise RuntimeError("PLOTTER_TOKEN must be set in the service environment")
    return token


def cancel_job_record(job: dict, *, reason: str) -> bool:
    if job.get("status") not in ACTIVE_CANCELLABLE_STATUSES:
        return False
    job.update(
        {
            "status": "cancelled",
            "finished_at": now(),
            "operator_message": reason,
            "cancelled_at": now(),
        }
    )
    return True


def delete_job_record_unlocked(job_id: str, *, keep_files: bool = True) -> dict:
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    status = job.get("status")
    if status in JOB_DELETE_BLOCKED_STATUSES:
        raise HTTPException(status_code=400, detail=f"Job cannot be deleted from status {status!r}")

    del jobs[job_id]

    if keep_files:
        state_store.delete_job_metadata(JOBS_DIR, job_id)
    else:
        shutil.rmtree(job_dir(job_id), ignore_errors=True)
        log_path = Path(job.get("log_path", ""))
        if log_path.exists() and log_path.is_file():
            log_path.unlink()

    return {"id": job_id, "status": status}


validate_svg_text = svg_utils.validate_svg_text


def validate_svg_file(path: Path) -> dict:
    return svg_utils.validate_svg_file(
        path,
        max_width_mm=MAX_PLOTTER_WIDTH_MM,
        max_height_mm=MAX_PLOTTER_HEIGHT_MM,
    )


PLOTTER_TOKEN = required_plotter_token()
MAX_PLOTTER_WIDTH_MM = float(os.environ.get("MAX_PLOTTER_WIDTH_MM", "609.6"))
MAX_PLOTTER_HEIGHT_MM = float(os.environ.get("MAX_PLOTTER_HEIGHT_MM", "914.4"))
BED_WIDTH_MM = float(os.environ.get("PLOTTER_BED_WIDTH_MM", "609.6"))
BED_HEIGHT_MM = float(os.environ.get("PLOTTER_BED_HEIGHT_MM", "914.4"))
DEFAULT_PEN_POS_DOWN = int(os.environ.get("PLOTTER_PEN_POS_DOWN", "35"))
DEFAULT_PEN_POS_UP = int(os.environ.get("PLOTTER_PEN_POS_UP", "65"))
DEFAULT_SPEED_PENDOWN = int(os.environ.get("PLOTTER_SPEED_PENDOWN", "15"))
DEFAULT_SPEED_PENUP = int(os.environ.get("PLOTTER_SPEED_PENUP", "40"))
DEFAULT_PEN_DELAY_DOWN = int(os.environ.get("PLOTTER_PEN_DELAY_DOWN", "0"))
DEFAULT_PEN_DELAY_UP = int(os.environ.get("PLOTTER_PEN_DELAY_UP", "0"))
DEFAULT_PEN_RATE_RAISE = int(os.environ.get("PLOTTER_PEN_RATE_RAISE", "75"))
DEFAULT_PEN_RATE_LOWER = int(os.environ.get("PLOTTER_PEN_RATE_LOWER", "50"))
THEORETICAL_MAX_XY_SPEED_MM_S = float(os.environ.get("PLOTTER_MAX_XY_SPEED_MM_S", "280"))
SAFE_MANUAL_MAX_XY_SPEED_MM_S = float(os.environ.get("PLOTTER_SAFE_MANUAL_MAX_XY_SPEED_MM_S", "200"))
DEFAULT_INK_WELL_TRAVEL_SPEED_MM_S = float(os.environ.get("PLOTTER_INK_WELL_TRAVEL_SPEED_MM_S", "120"))
MOTOR_RESOLUTION_CACHE_TTL_S = float(os.environ.get("PLOTTER_MOTOR_RESOLUTION_CACHE_TTL_S", "10"))
TELEMETRY_POLL_INTERVAL_S = float(os.environ.get("PLOTTER_TELEMETRY_POLL_INTERVAL_S", "0.5"))
TELEMETRY_SERIAL_TIMEOUT_S = float(os.environ.get("PLOTTER_TELEMETRY_SERIAL_TIMEOUT_S", "0.15"))
TELEMETRY_FULL_POLL_INTERVAL_S = float(os.environ.get("PLOTTER_TELEMETRY_FULL_POLL_INTERVAL_S", "10"))
MANUAL_HARDWARE_PRIORITY_GRACE_S = float(os.environ.get("PLOTTER_MANUAL_HARDWARE_PRIORITY_GRACE_S", "0.25"))
DIP_CIRCLE_SPEED_MM_S = float(os.environ.get("PLOTTER_DIP_CIRCLE_SPEED_MM_S", "60"))
DIP_CIRCLE_SEGMENTS = int(os.environ.get("PLOTTER_DIP_CIRCLE_SEGMENTS", "12"))
DIP_SERVO_RATE_LOWER = int(os.environ.get("PLOTTER_DIP_SERVO_RATE_LOWER", "200"))
DIP_SERVO_RATE_RAISE = int(os.environ.get("PLOTTER_DIP_SERVO_RATE_RAISE", "200"))
DIP_SERVO_EXTRA_SETTLE_MS = int(os.environ.get("PLOTTER_DIP_SERVO_EXTRA_SETTLE_MS", "0"))
NATIVE_XY_RESOLUTION_STEPS_PER_INCH = float(os.environ.get("PLOTTER_NATIVE_XY_RESOLUTION_STEPS_PER_INCH", "2032"))
NATIVE_XY_RESOLUTION_STEPS_PER_MM = float(os.environ.get("PLOTTER_NATIVE_XY_RESOLUTION_STEPS_PER_MM", "80"))
MIN_MOTION_RESOLUTION_MM = float(os.environ.get("PLOTTER_MIN_MOTION_RESOLUTION_MM", "0.0125"))
MECHANICAL_PRECISION_XY_MM = float(os.environ.get("PLOTTER_MECHANICAL_PRECISION_XY_MM", "0.1"))
LOW_SPEED_REPRODUCIBILITY_XY_MM = float(os.environ.get("PLOTTER_LOW_SPEED_REPRODUCIBILITY_XY_MM", "0.1"))
VERTICAL_PEN_TRAVEL_MM = float(os.environ.get("PLOTTER_VERTICAL_PEN_TRAVEL_MM", "10"))
APP_VERSION = VERSION_PATH.read_text(encoding="utf-8").strip() if VERSION_PATH.exists() else "development"


def motion_spec() -> dict:
    return {
        "max_xy_speed_mm_s": THEORETICAL_MAX_XY_SPEED_MM_S,
        "safe_manual_max_xy_speed_mm_s": SAFE_MANUAL_MAX_XY_SPEED_MM_S,
        "native_xy_resolution_steps_per_inch": NATIVE_XY_RESOLUTION_STEPS_PER_INCH,
        "native_xy_resolution_steps_per_mm": NATIVE_XY_RESOLUTION_STEPS_PER_MM,
        "min_motion_resolution_mm": MIN_MOTION_RESOLUTION_MM,
        "mechanical_precision_xy_mm": MECHANICAL_PRECISION_XY_MM,
        "low_speed_reproducibility_xy_mm": LOW_SPEED_REPRODUCIBILITY_XY_MM,
        "vertical_pen_travel_mm": VERTICAL_PEN_TRAVEL_MM,
    }


def now() -> float:
    return time.time()


def load_position_offset() -> None:
    global position_offset, position_current, home_position, position_calibration_id, position_calibration_enabled
    try:
        data = state_store.read_json(POSITION_PATH)
        if data is None:
            return
        if data.get("state_version") != POSITION_STATE_VERSION:
            print("Ignoring legacy position state; recalibration is required", flush=True)
            return
        position_offset = {
            "x_mm": float(data.get("x_mm", 0.0)),
            "y_mm": float(data.get("y_mm", 0.0)),
        }
        has_calibration_id = False
        if isinstance(data.get("calibration_id"), str) and data["calibration_id"]:
            position_calibration_id = data["calibration_id"]
            has_calibration_id = True
        position_calibration_enabled = bool(data.get("calibration_enabled", True))
        current = data.get("current_position")
        if isinstance(current, dict):
            position_current = {
                "x_mm": float(current.get("x_mm", 0.0)),
                "y_mm": float(current.get("y_mm", 0.0)),
            }
        home = data.get("home_position")
        if isinstance(home, dict):
            home_position = {
                "x_mm": float(home.get("x_mm", 0.0)),
                "y_mm": float(home.get("y_mm", 0.0)),
            }
        if not has_calibration_id:
            save_position_offset_unlocked()
    except Exception as exc:
        print(f"Could not load {POSITION_PATH}: {exc}", flush=True)


def save_position_offset_unlocked() -> None:
    data = {
        "state_version": POSITION_STATE_VERSION,
        "x_mm": position_offset["x_mm"],
        "y_mm": position_offset["y_mm"],
        "calibration_id": position_calibration_id,
        "calibration_enabled": position_calibration_enabled,
    }
    if position_current is not None:
        data["current_position"] = dict(position_current)
    if home_position is not None:
        data["home_position"] = dict(home_position)
    state_store.write_json(POSITION_PATH, data)


def renew_position_calibration_unlocked() -> None:
    global position_calibration_id
    position_calibration_id = uuid.uuid4().hex


def current_position_calibration_id() -> str:
    with position_lock:
        return position_calibration_id


def load_pen_settings() -> None:
    with pen_settings_lock:
        pen_settings["pen_pos_up"] = DEFAULT_PEN_POS_UP
        pen_settings["pen_pos_down"] = DEFAULT_PEN_POS_DOWN
        try:
            data = state_store.read_json(PEN_SETTINGS_PATH)
            if data is None:
                return
            pen_settings["pen_pos_up"] = int(data.get("pen_pos_up", pen_settings["pen_pos_up"]))
            pen_settings["pen_pos_down"] = int(data.get("pen_pos_down", pen_settings["pen_pos_down"]))
        except Exception as exc:
            print(f"Could not load {PEN_SETTINGS_PATH}: {exc}", flush=True)


def save_pen_settings_unlocked() -> None:
    state_store.write_json(PEN_SETTINGS_PATH, pen_settings)


def current_pen_settings() -> dict:
    with pen_settings_lock:
        return dict(pen_settings)


def apply_pen_settings_to_job(job: dict, settings: dict) -> None:
    job["pen_pos_down"] = settings["pen_pos_down"]
    job["pen_pos_up"] = settings["pen_pos_up"]


def load_plot_settings() -> None:
    with plot_settings_lock:
        plot_settings["speed_pendown"] = DEFAULT_SPEED_PENDOWN
        plot_settings["speed_penup"] = DEFAULT_SPEED_PENUP
        plot_settings["pen_delay_down"] = DEFAULT_PEN_DELAY_DOWN
        plot_settings["pen_delay_up"] = DEFAULT_PEN_DELAY_UP
        plot_settings["pen_rate_raise"] = DEFAULT_PEN_RATE_RAISE
        plot_settings["auto_dip_enabled"] = False
        plot_settings["dip_interval_s"] = 60.0
        try:
            data = state_store.read_json(PLOT_SETTINGS_PATH)
            if data is None:
                return
            plot_settings["speed_pendown"] = int(data.get("speed_pendown", plot_settings["speed_pendown"]))
            plot_settings["speed_penup"] = int(data.get("speed_penup", plot_settings["speed_penup"]))
            plot_settings["pen_delay_down"] = validate_pen_delay_down(
                data.get("pen_delay_down", plot_settings["pen_delay_down"])
            )
            plot_settings["pen_delay_up"] = validate_pen_delay_up(
                data.get("pen_delay_up", plot_settings["pen_delay_up"])
            )
            plot_settings["pen_rate_raise"] = validate_pen_rate_raise(
                data.get("pen_rate_raise", plot_settings["pen_rate_raise"])
            )
            plot_settings["auto_dip_enabled"] = resolve_auto_dip_flag(
                data.get("auto_dip_enabled", plot_settings["auto_dip_enabled"])
            )
            plot_settings["dip_interval_s"] = validate_dip_interval(
                data.get("dip_interval_s", plot_settings["dip_interval_s"])
            )
        except Exception as exc:
            print(f"Could not load {PLOT_SETTINGS_PATH}: {exc}", flush=True)


def save_plot_settings_unlocked() -> None:
    state_store.write_json(PLOT_SETTINGS_PATH, plot_settings)


def current_plot_settings() -> dict:
    with plot_settings_lock:
        return dict(plot_settings)


def apply_plot_settings_to_job(job: dict, settings: dict) -> None:
    for key in (
        "speed_pendown",
        "speed_penup",
        "pen_delay_down",
        "pen_delay_up",
        "pen_rate_raise",
    ):
        job[key] = settings[key]


def paper_dimensions(settings: dict) -> dict:
    return settings_utils.paper_dimensions(settings)


def validate_paper_settings(settings: dict) -> dict:
    return settings_utils.validate_paper_settings(
        settings,
        bed_width_mm=BED_WIDTH_MM,
        bed_height_mm=BED_HEIGHT_MM,
    )


def load_paper_settings() -> None:
    with paper_settings_lock:
        defaults = validate_paper_settings(
            {
                "state_version": 1,
                "enabled": True,
                "size": "A3",
                "orientation": "portrait",
                "top_right": None,
            }
        )
        paper_settings.clear()
        paper_settings.update(defaults)
        try:
            data = state_store.read_json(PAPER_SETTINGS_PATH)
            if data is None:
                return
            if data.get("state_version") != 1:
                raise ValueError("unsupported state version")
            candidate = dict(defaults)
            candidate.update(data)
            paper_settings.update(validate_paper_settings(candidate))
        except Exception as exc:
            print(f"Could not load {PAPER_SETTINGS_PATH}: {exc}", flush=True)


def save_paper_settings_unlocked() -> None:
    state_store.write_json(PAPER_SETTINGS_PATH, paper_settings)


def current_paper_settings() -> dict:
    with paper_settings_lock:
        return state_store.deep_copy_jsonable(paper_settings)


def load_ink_well_settings() -> None:
    with ink_well_settings_lock:
        defaults = {
            "state_version": 1,
            "installed": False,
            "centre": None,
            "radius_mm": None,
            "clearance_pos": None,
            "dip_pos": None,
            "dwell_ms": 1000,
            "drip_dwell_ms": 0,
            "travel_speed_mm_s": DEFAULT_INK_WELL_TRAVEL_SPEED_MM_S,
            "dip_circle_count": 3,
            "dip_circle_diameter_mm": 10.0,
            "calibration_id": None,
            "test_passed": False,
            "tested_at": None,
        }
        ink_well_settings.clear()
        ink_well_settings.update(defaults)
        try:
            data = state_store.read_json(INK_WELL_SETTINGS_PATH)
            if data is None:
                return
            if data.get("state_version") != 1:
                raise ValueError("unsupported state version")
            candidate = dict(defaults)
            candidate.update(data)
            validate_ink_well_settings(candidate, require_ready=bool(candidate.get("installed")))
            ink_well_settings.update(candidate)
        except Exception as exc:
            print(f"Could not load {INK_WELL_SETTINGS_PATH}: {exc}", flush=True)


def save_ink_well_settings_unlocked() -> None:
    state_store.write_json(INK_WELL_SETTINGS_PATH, ink_well_settings)


def current_ink_well_settings() -> dict:
    with ink_well_settings_lock:
        return state_store.deep_copy_jsonable(ink_well_settings)


def validate_ink_well_settings(settings: dict, *, require_ready: bool = False) -> dict:
    centre = settings.get("centre")
    if centre is not None:
        if not isinstance(centre, dict) or "x_mm" not in centre or "y_mm" not in centre:
            raise ValueError("Ink well centre must contain x_mm and y_mm")
        x_mm, y_mm = validate_bed_target(centre["x_mm"], centre["y_mm"])
        settings["centre"] = {"x_mm": x_mm, "y_mm": y_mm}

    radius = settings.get("radius_mm")
    if radius is not None:
        radius = float(radius)
        if not math.isfinite(radius) or not 1 <= radius <= 250:
            raise ValueError("Ink well radius_mm must be between 1 and 250")
        settings["radius_mm"] = radius

    for key in ("clearance_pos", "dip_pos"):
        value = settings.get(key)
        if value is not None:
            try:
                settings[key] = validate_pen_position(value, key)
            except HTTPException as exc:
                raise ValueError(str(exc.detail)) from exc

    for key, maximum in (("dwell_ms", 30000), ("drip_dwell_ms", 30000)):
        value = int(settings.get(key, 0))
        if not 0 <= value <= maximum:
            raise ValueError(f"{key} must be between 0 and {maximum}")
        settings[key] = value

    travel_speed_value = settings.get("travel_speed_mm_s")
    travel_speed = DEFAULT_INK_WELL_TRAVEL_SPEED_MM_S if travel_speed_value is None else float(travel_speed_value)
    if not math.isfinite(travel_speed) or not 1 <= travel_speed <= SAFE_MANUAL_MAX_XY_SPEED_MM_S:
        raise ValueError(f"travel_speed_mm_s must be between 1 and {SAFE_MANUAL_MAX_XY_SPEED_MM_S:g}")
    settings["travel_speed_mm_s"] = travel_speed

    circle_count_value = settings.get("dip_circle_count")
    circle_count = 3 if circle_count_value is None else int(circle_count_value)
    if not 0 <= circle_count <= 10:
        raise ValueError("dip_circle_count must be between 0 and 10")
    settings["dip_circle_count"] = circle_count

    circle_diameter_value = settings.get("dip_circle_diameter_mm")
    circle_diameter = 10.0 if circle_diameter_value is None else float(circle_diameter_value)
    if not math.isfinite(circle_diameter) or not 0 <= circle_diameter <= 50:
        raise ValueError("dip_circle_diameter_mm must be between 0 and 50")
    if settings.get("radius_mm") is not None and circle_diameter > float(settings["radius_mm"]) * 2:
        raise ValueError("dip_circle_diameter_mm must fit inside the ink well radius")
    settings["dip_circle_diameter_mm"] = circle_diameter

    calibration_id = settings.get("calibration_id")
    if calibration_id is not None and not isinstance(calibration_id, str):
        raise ValueError("Ink well calibration_id must be a string")

    if require_ready:
        missing = [
            key
            for key in ("centre", "radius_mm", "clearance_pos", "dip_pos")
            if settings.get(key) is None
        ]
        if missing:
            raise ValueError(f"Ink well setup is incomplete: {', '.join(missing)}")
        if not settings.get("test_passed"):
            raise ValueError("Ink well test cycle must pass before it can be marked installed")
    return settings


def require_ink_well_current_calibration(settings: dict) -> None:
    if settings.get("calibration_id") != current_position_calibration_id():
        raise ValueError(
            "Ink well centre was saved under a different plotter calibration. "
            "Move the head to the ink well centre and press Set Centre Here."
        )


def ink_well_plot_snapshot(settings: dict) -> dict:
    validate_ink_well_settings(settings, require_ready=True)
    require_ink_well_current_calibration(settings)
    return {
        "centre": dict(settings["centre"]),
        "radius_mm": settings["radius_mm"],
        "clearance_pos": settings["clearance_pos"],
        "dip_pos": settings["dip_pos"],
        "dwell_ms": settings["dwell_ms"],
        "drip_dwell_ms": settings["drip_dwell_ms"],
        "travel_speed_mm_s": settings["travel_speed_mm_s"],
        "dip_circle_count": settings["dip_circle_count"],
        "dip_circle_diameter_mm": settings["dip_circle_diameter_mm"],
        "tested_at": settings["tested_at"],
    }


def validate_speed_setting(value: int, name: str) -> int:
    return settings_utils.validate_speed_setting(value, name)


def validate_pen_position(value: int, name: str) -> int:
    return settings_utils.validate_pen_position(value, name)


def validate_pen_delay_down(value: int) -> int:
    return settings_utils.validate_pen_delay_down(value)


def validate_pen_delay_up(value: int) -> int:
    return settings_utils.validate_pen_delay_up(value)


def validate_pen_rate_raise(value: int) -> int:
    return settings_utils.validate_pen_rate_raise(value)


def validate_dip_interval(value: float) -> float:
    return settings_utils.validate_dip_interval(value)


def resolve_auto_dip_flag(*values) -> bool:
    return settings_utils.resolve_auto_dip_flag(*values)


def auto_dip_flag_was_provided(*values) -> bool:
    return settings_utils.auto_dip_flag_was_provided(*values)


def raw_xy_to_bed_xy(raw_xy: dict | None) -> dict | None:
    return positioning.raw_xy_to_bed_xy(raw_xy)


def bed_delta_to_raw_delta(x_mm: float, y_mm: float) -> dict:
    return positioning.bed_delta_to_raw_delta(x_mm, y_mm)


def apply_position_offset(raw_xy: dict | None) -> dict | None:
    bed_xy = raw_xy_to_bed_xy(raw_xy)
    if bed_xy is None:
        return None
    with position_lock:
        return {
            "x_mm": bed_xy["x_mm"] + position_offset["x_mm"],
            "y_mm": bed_xy["y_mm"] + position_offset["y_mm"],
        }


def current_position_estimate(raw_xy: dict | None) -> dict | None:
    if raw_xy is not None:
        return apply_position_offset(raw_xy)
    with position_lock:
        if position_current is not None:
            return dict(position_current)
    return None


def set_current_position_unlocked(x_mm: float, y_mm: float) -> None:
    global position_current
    position_current = positioning.clamp_bed_position(
        x_mm,
        y_mm,
        bed_width_mm=BED_WIDTH_MM,
        bed_height_mm=BED_HEIGHT_MM,
    )


def set_home_position_unlocked(x_mm: float, y_mm: float) -> None:
    global home_position
    home_position = positioning.clamp_bed_position(
        x_mm,
        y_mm,
        bed_width_mm=BED_WIDTH_MM,
        bed_height_mm=BED_HEIGHT_MM,
    )


def current_home_position() -> dict:
    with position_lock:
        if home_position is None:
            raise HTTPException(status_code=409, detail="Calibrate the plotter before returning home")
        return dict(home_position)


def invalidate_position_reference_unlocked() -> None:
    global position_current, home_position
    position_current = None
    home_position = None
    position_offset["x_mm"] = 0.0
    position_offset["y_mm"] = 0.0
    renew_position_calibration_unlocked()
    save_position_offset_unlocked()


def require_software_position() -> None:
    with position_lock:
        calibrated = position_current is not None
    if not calibrated:
        raise HTTPException(
            status_code=409,
            detail="Calibrate the current position first with Set X/Y before jogging or dragging.",
        )


def validate_bed_target(x_mm: float, y_mm: float) -> tuple[float, float]:
    x_mm = float(x_mm)
    y_mm = float(y_mm)
    if not math.isfinite(x_mm) or not math.isfinite(y_mm):
        raise HTTPException(status_code=400, detail="Target coordinates must be finite numbers")
    if not (0 <= x_mm <= BED_WIDTH_MM and 0 <= y_mm <= BED_HEIGHT_MM):
        raise HTTPException(
            status_code=400,
            detail=f"Target must be within 0..{BED_WIDTH_MM:g}mm X and 0..{BED_HEIGHT_MM:g}mm Y",
        )
    return x_mm, y_mm


def current_software_position() -> dict:
    with position_lock:
        if position_current is None:
            raise HTTPException(status_code=409, detail="Software position is not calibrated")
        return dict(position_current)


def job_dir(job_id: str) -> Path:
    return state_store.job_dir(JOBS_DIR, job_id)


def job_meta_path(job_id: str) -> Path:
    return state_store.job_meta_path(JOBS_DIR, job_id)


def save_job_unlocked(job_id: str) -> None:
    if job_id not in jobs:
        return
    state_store.save_job(JOBS_DIR, job_id, jobs[job_id])


def load_jobs() -> None:
    """
    Load job metadata saved on disk.

    If the server restarts while a job is active, we do NOT automatically resume it.
    We mark it interrupted and let you rerun deliberately.
    """
    with jobs_lock:
        for path in state_store.iter_job_meta_paths(JOBS_DIR):
            try:
                job_id, job = state_store.read_job_meta(path)
                changed = False

                if job.get("status") in {
                    "queued",
                    "queued_for_operator",
                    "waiting_for_operator",
                    "queued_for_resume",
                    "running",
                    "dipping",
                    "dip_failed",
                }:
                    job["status"] = "interrupted"
                    job["operator_message"] = (
                        "Server restarted while this job was active. "
                        "Use rerun to start it again from the beginning."
                    )
                    changed = True

                jobs[job_id] = job
                if changed:
                    save_job_unlocked(job_id)

            except Exception as exc:
                print(f"Could not load {path}: {exc}", flush=True)


def check_token(x_plotter_token: Optional[str]) -> None:
    if PLOTTER_TOKEN and not secrets.compare_digest(x_plotter_token or "", PLOTTER_TOKEN):
        raise HTTPException(status_code=401, detail="Bad or missing X-Plotter-Token")


def require_localhost(request: Request) -> None:
    host = request.client.host if request.client else ""

    if host not in {"127.0.0.1", "::1"}:
        raise HTTPException(
            status_code=403,
            detail="Operator controls are only available from the Linux box itself.",
        )


def update_job(job_id: str, **fields) -> None:
    with jobs_lock:
        if job_id in jobs:
            jobs[job_id].update(fields)
            save_job_unlocked(job_id)


def log_tail(path: Path, max_chars: int = 4000) -> str:
    return state_store.log_tail(path, max_chars=max_chars)


def job_timing_summary(job: dict) -> dict:
    log_path = job.get("log_path")
    if not log_path:
        return {}
    return timing_log.summarize_timing_text(log_tail(Path(log_path), max_chars=120000), limit=6)


def active_running_job_unlocked() -> dict | None:
    for job in jobs.values():
        if job.get("status") in RUNNING_STATUSES:
            return job
    return None


def job_plot_footprint(job: dict) -> dict | None:
    return job_model.plot_footprint(job)


def _job_layer_for_index(job: dict, layer_index: int) -> dict | None:
    return next((layer for layer in job.get("layers") or [] if int(layer.get("index") or 0) == layer_index), None)


def _job_layer_input_path(job: dict, layer: dict) -> Path:
    path = Path(layer.get("input_svg") or "")
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="Layer SVG not found")
    try:
        path.resolve().relative_to(job_dir(job["id"]).resolve())
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Layer SVG path is outside the job directory") from exc
    return path


def job_plot_preview(job: dict) -> dict | None:
    footprint = job_plot_footprint(job)
    if not footprint:
        return None

    layers = []
    for layer in job.get("layers") or []:
        metrics = layer.get("svg_metrics") or {}
        try:
            index = int(layer.get("index") or 0)
            width = float(metrics.get("width_mm"))
            height = float(metrics.get("height_mm"))
            input_path = _job_layer_input_path(job, layer)
        except (TypeError, ValueError, HTTPException):
            continue
        if index <= 0 or not math.isfinite(width) or not math.isfinite(height) or width <= 0 or height <= 0:
            continue
        try:
            version = input_path.stat().st_mtime_ns
        except OSError:
            version = int(now() * 1000)
        layers.append(
            {
                "index": index,
                "name": layer.get("name") or f"Layer {index}",
                "width_mm": round(width, 4),
                "height_mm": round(height, 4),
                "url": f"/jobs/{job['id']}/layers/{index}/preview.svg?v={version}",
            }
        )

    if not layers:
        return None
    return {
        "footprint": footprint,
        "origin": job.get("plot_origin"),
        "layers": sorted(layers, key=lambda item: item["index"]),
    }


def job_plot_origin_for_paper(job: dict, paper: dict | None = None) -> dict | None:
    paper = paper or current_paper_settings()
    return job_model.plot_origin_for_paper(
        job,
        paper,
        validate_bed_target=validate_bed_target,
    )


def apply_paper_alignment_to_job(job: dict) -> None:
    paper = current_paper_settings()
    origin = job_plot_origin_for_paper(job, paper)
    if origin is None:
        job["paper"] = paper
        job["plot_origin"] = None
        return
    job["paper"] = paper
    job["plot_origin"] = origin


def job_plot_start_position(job: dict) -> dict:
    try:
        start = current_home_position()
    except HTTPException:
        start = job.get("plot_start_position")
    if not isinstance(start, dict):
        start = current_home_position()

    x_mm, y_mm = validate_bed_target(start["x_mm"], start["y_mm"])
    return {"x_mm": x_mm, "y_mm": y_mm}


def layer_dip_estimates(layers: list[dict]) -> list[dict]:
    return job_model.layer_dip_estimates(layers)


def plot_origin_for_layer_metrics(svg_metrics: dict, paper: dict | None = None) -> dict | None:
    return job_plot_origin_for_paper({"layers": [{"svg_metrics": svg_metrics}]}, paper)


def require_hardware_idle() -> None:
    with jobs_lock:
        active = active_running_job_unlocked()
    if active:
        raise HTTPException(
            status_code=409,
            detail=f"Plotter is busy with running job {active.get('id')}",
        )


def set_active_process(proc: subprocess.Popen | None, job_id: str | None = None) -> None:
    global active_process, active_process_job_id
    with active_process_lock:
        active_process = proc
        active_process_job_id = job_id


def run_axicli_command(cmd: list[str], log, *, job_id: str | None = None) -> int:
    # Hold hardware_lock only for Popen + registration, not for proc.wait().
    # Releasing before wait() lets the telemetry worker run; it checks
    # active_process to avoid touching the serial port while axicli owns it.
    # active_process_lock is held across Popen + set_active_process so that
    # pause_job cannot observe a half-registered process (SIGINT race fix).
    with hardware_lock:
        with active_process_lock:
            proc = subprocess.Popen(
                cmd,
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
            )
            set_active_process(proc, job_id)
    try:
        return proc.wait()
    finally:
        set_active_process(None, None)


def axicli_cmd() -> list[str]:
    return plot_execution.axicli_cmd(AXICLI, AXICLI_CONFIG)


def run_axicli_pen_manual(
    *,
    raised: bool,
    up_pos: int,
    down_pos: int,
    raise_rate: int,
    lower_rate: int,
    delay_up_ms: int,
    delay_down_ms: int,
) -> dict:
    if not (0 <= up_pos <= 100 and 0 <= down_pos <= 100):
        raise ValueError("AxiCLI manual pen commands require pen positions between 0 and 100")
    cmd = axicli_cmd() + [
        "--mode",
        "manual",
        "--manual_cmd",
        "raise_pen" if raised else "lower_pen",
        "--port",
        PLOTTER_PORT,
        "--pen_pos_up",
        str(up_pos),
        "--pen_pos_down",
        str(down_pos),
        "--pen_rate_raise",
        str(raise_rate),
        "--pen_rate_lower",
        str(lower_rate),
        "--pen_delay_up",
        str(delay_up_ms),
        "--pen_delay_down",
        str(delay_down_ms),
    ]
    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    return {
        "ok": proc.returncode == 0,
        "position": "up" if raised else "down",
        "method": "axicli_manual",
        "returncode": proc.returncode,
        "output": proc.stdout,
    }


def run_pen_manual_direct_first(
    *,
    raised: bool,
    up_pos: int,
    down_pos: int,
    raise_rate: int,
    lower_rate: int,
    delay_up_ms: int,
    delay_down_ms: int,
) -> dict:
    try:
        with serial.Serial(PLOTTER_PORT, timeout=2) as port:
            return _run_pen_servo_on_port_locked(
                port,
                raised=raised,
                up_pos=up_pos,
                down_pos=down_pos,
                raise_rate=raise_rate,
                lower_rate=lower_rate,
                delay_up_ms=delay_up_ms,
                delay_down_ms=delay_down_ms,
            )
    except Exception as direct_exc:
        fallback = run_axicli_pen_manual(
            raised=raised,
            up_pos=up_pos,
            down_pos=down_pos,
            raise_rate=raise_rate,
            lower_rate=lower_rate,
            delay_up_ms=delay_up_ms,
            delay_down_ms=delay_down_ms,
        )
        fallback["fallback_from"] = "direct_ebb"
        fallback["direct_error"] = repr(direct_exc)
        return fallback


def generate_plot_digest(input_svg: Path, output_svg: Path, job_settings: dict) -> None:
    plot_execution.generate_plot_digest(input_svg, output_svg, job_settings, axicli=AXICLI, axicli_config=AXICLI_CONFIG)


def analyse_layer_for_ink_well(
    input_svg: Path,
    digest_svg: Path,
    *,
    job_settings: dict,
    home: dict,
    well: dict,
    dip_interval_s: float | None = None,
) -> dict:
    return plot_execution.analyse_layer_for_ink_well(
        input_svg,
        digest_svg,
        job_settings=job_settings,
        home=home,
        well=well,
        axicli=AXICLI,
        axicli_config=AXICLI_CONFIG,
        dip_interval_s=dip_interval_s,
        generate_plot_digest_fn=generate_plot_digest,
    )


def prepare_auto_dip_layer(layer: dict, analysis: dict) -> None:
    plot_execution.prepare_auto_dip_layer(layer, analysis, write_checkpoint_digest_fn=write_checkpoint_digest)


PRE_START_JOB_STATUSES = {"queued", "queued_for_operator", "waiting_for_operator"}


def configure_job_auto_dip(job: dict, *, enabled: bool, dip_interval_s: float | None) -> None:
    if job.get("status") not in PRE_START_JOB_STATUSES:
        raise HTTPException(status_code=400, detail=f"Job auto-dip can only be changed before plotting starts; status is {job.get('status')!r}")

    if not enabled:
        for layer in job.get("layers") or []:
            layer["plot_svg"] = None
            layer["auto_dip_checkpoint_count"] = 0
        job["auto_dip_enabled"] = False
        job["dip_interval_s"] = None
        job["dip_failure"] = None
        job["operator_message"] = "Automatic ink dipping disabled before plot start."
        return

    interval_s = validate_dip_interval(dip_interval_s)
    well_settings = current_ink_well_settings()
    if not well_settings.get("installed"):
        raise HTTPException(
            status_code=409,
            detail="Complete the ink well test and mark it installed before enabling automatic dipping",
        )
    try:
        well_snapshot = ink_well_plot_snapshot(well_settings)
        start_home = current_home_position()
    except (ValueError, HTTPException) as exc:
        detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
        raise HTTPException(status_code=409, detail=detail) from exc

    job_settings = {
        "speed_pendown": job.get("speed_pendown", current_plot_settings()["speed_pendown"]),
        "speed_penup": job.get("speed_penup", current_plot_settings()["speed_penup"]),
        "pen_pos_down": job.get("pen_pos_down", current_pen_settings()["pen_pos_down"]),
        "pen_pos_up": job.get("pen_pos_up", current_pen_settings()["pen_pos_up"]),
    }

    for layer in job.get("layers") or []:
        input_svg = Path(layer["input_svg"])
        digest_svg = Path(layer.get("plot_digest_svg") or input_svg.parent / "plot_digest.svg")
        svg_metrics = layer.get("svg_metrics") or validate_svg_file(input_svg)
        analysis_origin = plot_origin_for_layer_metrics(svg_metrics) or start_home
        try:
            analysis = analyse_layer_for_ink_well(
                input_svg,
                digest_svg,
                job_settings=job_settings,
                home=analysis_origin,
                well=well_snapshot,
                dip_interval_s=interval_s,
            )
            layer["svg_metrics"] = svg_metrics
            layer["plot_digest_svg"] = str(digest_svg)
            layer["ink_analysis"] = analysis
            prepare_auto_dip_layer(layer, analysis)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    job["ink_well"] = well_snapshot
    job["plot_start_position"] = start_home
    job["auto_dip_enabled"] = True
    job["dip_interval_s"] = interval_s
    job["dip_count"] = 0
    job["dip_failure"] = None
    job["operator_message"] = f"Automatic ink dipping enabled before plot start; interval {interval_s:g}s."


def run_control_command(cmd: list[str]) -> dict:
    require_hardware_idle()
    with hardware_lock:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "command": cmd,
        "output": proc.stdout,
    }


def run_manual_command(manual_cmd: str, extra: list[str] | None = None) -> dict:
    cmd = manual_command_cmd(manual_cmd, extra)
    result = run_control_command(cmd)
    if not result["ok"]:
        raise HTTPException(status_code=500, detail=result)
    return result


def manual_command_cmd(manual_cmd: str, extra: list[str] | None = None) -> list[str]:
    cmd = axicli_cmd() + [
        "--mode",
        "manual",
        "--manual_cmd",
        manual_cmd,
        "--port",
        PLOTTER_PORT,
    ]
    if extra:
        cmd.extend(extra)
    return cmd


def checked_return_home() -> dict:
    require_hardware_idle()
    home = current_home_position()

    actions = []
    pen_defaults = current_pen_settings()
    plot_defaults = current_plot_settings()

    with hardware_lock:
        try:
            with serial.Serial(PLOTTER_PORT, timeout=2) as port:
                require_cached_high_resolution_motors(port)
                pen_up, pen_ack = serial_query(port, "QP\r")
                actions.append(
                    {
                        "action": "check_pen",
                        "pen_up": pen_up == "1",
                        "raw": pen_up,
                        "ack": pen_ack,
                    }
                )

                if pen_up != "1":
                    raise_result = _run_pen_servo_on_port_locked(
                        port,
                        raised=True,
                        up_pos=pen_defaults["pen_pos_up"],
                        down_pos=pen_defaults["pen_pos_down"],
                        raise_rate=plot_defaults.get("pen_rate_raise", DEFAULT_PEN_RATE_RAISE),
                        lower_rate=DEFAULT_PEN_RATE_LOWER,
                        delay_up_ms=plot_defaults.get("pen_delay_up", DEFAULT_PEN_DELAY_UP),
                        delay_down_ms=plot_defaults.get("pen_delay_down", DEFAULT_PEN_DELAY_DOWN),
                    )
                    raise_result["action"] = "raise_pen"
                    actions.append(raise_result)

                actual = _move_to_bed_target_on_port_locked(
                    port,
                    home,
                    speed_mm_s=min(SAFE_MANUAL_MAX_XY_SPEED_MM_S, 120.0),
                )
                actions.append({"action": "move_home", "ok": True, "home_position": home, "actual_position": actual})
        except serial.SerialException as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail={"actions": actions, "error": repr(exc)}) from exc

    home_error_mm = math.hypot(actual["x_mm"] - home["x_mm"], actual["y_mm"] - home["y_mm"])

    if home_error_mm > 0.5:
        raise HTTPException(
            status_code=500,
            detail={
                "message": "Controller did not return to the saved home position",
                "home_position": home,
                "actual_position": actual,
                "error_mm": round(home_error_mm, 4),
                "actions": actions,
            },
        )

    return {
        "ok": True,
        "pen_was_up": actions[0]["pen_up"],
        "raised_pen": any(action["action"] == "raise_pen" for action in actions),
        "position_estimate": actual,
        "home_error_mm": round(home_error_mm, 4),
        "actions": actions,
    }


def serial_query(port: serial.Serial, command: str, *, ack: bool = True) -> tuple[str, str | None]:
    return hardware.serial_query(port, command, ack=ack)


def steps_to_xy_mm(axis_1: int, axis_2: int) -> dict:
    return hardware.steps_to_xy_mm(axis_1, axis_2)


def xy_mm_to_steps(x_mm: float, y_mm: float) -> tuple[int, int]:
    return hardware.xy_mm_to_steps(x_mm, y_mm)


def raw_command(port: serial.Serial, command: str) -> str:
    return hardware.raw_command(port, command)


def read_motor_resolution(port: serial.Serial) -> tuple[int | None, int | None]:
    return hardware.read_motor_resolution(port)


def require_enabled_high_resolution_motors(port: serial.Serial) -> None:
    resolution = read_motor_resolution(port)
    with motor_resolution_cache_lock:
        motor_resolution_cache["value"] = resolution
        motor_resolution_cache["checked_at"] = time.monotonic()
    if resolution != (1, 1):
        raise HTTPException(
            status_code=409,
            detail=(
                "Motors must already be enabled in high-resolution mode. "
                "Enable motors, then recalibrate before moving."
            ),
        )


def require_cached_high_resolution_motors(port: serial.Serial) -> None:
    with motor_resolution_cache_lock:
        cached_value = motor_resolution_cache["value"]
        checked_at = motor_resolution_cache["checked_at"]
    if cached_value == (1, 1) and time.monotonic() - checked_at <= MOTOR_RESOLUTION_CACHE_TTL_S:
        return
    require_enabled_high_resolution_motors(port)


def invalidate_motor_resolution_cache() -> None:
    with motor_resolution_cache_lock:
        motor_resolution_cache["value"] = None
        motor_resolution_cache["checked_at"] = 0.0


def wait_for_motion_idle(port: serial.Serial, timeout_s: float) -> None:
    hardware.wait_for_motion_idle(port, timeout_s)


def read_step_position(port: serial.Serial) -> tuple[int, int, dict]:
    return hardware.read_step_position(port)


def ebb_command(port: serial.Serial, command: str) -> str:
    return hardware.ebb_command(port, command)


def current_axidraw_servo_config() -> dict:
    return hardware.current_axidraw_servo_config(AXICLI_CONFIG)


def servo_pwm_for_pen_position(config: dict, pen_position: float) -> int:
    return hardware.servo_pwm_for_pen_position(config, pen_position)


def servo_rate_value(config: dict, rate_percent: float) -> int:
    return hardware.servo_rate_value(config, rate_percent)


def servo_travel_delay_ms(
    config: dict,
    up_position: float,
    down_position: float,
    rate_percent: float,
    *,
    extra_settle_ms: int = 0,
) -> int:
    return hardware.servo_travel_delay_ms(
        config,
        up_position,
        down_position,
        rate_percent,
        extra_settle_ms=extra_settle_ms,
    )


def _run_pen_servo_on_port_locked(
    port: serial.Serial,
    *,
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
    return hardware.run_pen_servo_on_port(
        port,
        axicli_config=AXICLI_CONFIG,
        raised=raised,
        up_pos=up_pos,
        down_pos=down_pos,
        raise_rate=raise_rate,
        lower_rate=lower_rate,
        delay_up_ms=delay_up_ms,
        delay_down_ms=delay_down_ms,
        extra_settle_ms=extra_settle_ms,
        label=label,
        log=log,
    )


def _run_dip_servo_on_port_locked(port: serial.Serial, job: dict, *, raised: bool, log) -> None:
    well = job["ink_well"]
    _run_pen_servo_on_port_locked(
        port,
        raised=raised,
        up_pos=well["clearance_pos"],
        down_pos=well["dip_pos"],
        raise_rate=DIP_SERVO_RATE_RAISE,
        lower_rate=DIP_SERVO_RATE_LOWER,
        extra_settle_ms=DIP_SERVO_EXTRA_SETTLE_MS,
        label="Raise to clearance" if raised else "Lower into ink",
        log=log,
    )


def _run_dip_servo_locked(job: dict, *, raised: bool, log) -> None:
    with serial.Serial(PLOTTER_PORT, timeout=2) as port:
        _run_dip_servo_on_port_locked(port, job, raised=raised, log=log)


def _move_to_bed_target_on_port_locked(
    port: serial.Serial,
    target: dict,
    *,
    speed_mm_s: float,
    log=None,
) -> dict:
    _axis_1, _axis_2, raw_current = read_step_position(port)
    current = current_position_estimate(raw_current)
    if current is None:
        raise RuntimeError("Could not calculate current position")

    def update_position(raw_after: dict) -> dict:
        actual = current_position_estimate(raw_after)
        if actual is None:
            raise RuntimeError("Could not calculate position after movement")
        with position_lock:
            set_current_position_unlocked(actual["x_mm"], actual["y_mm"])
            save_position_offset_unlocked()
        return actual

    return hardware.move_to_bed_target_on_port(
        port,
        target,
        current_position=current,
        bed_delta_to_raw_delta=bed_delta_to_raw_delta,
        validate_bed_target=validate_bed_target,
        update_position=update_position,
        speed_mm_s=speed_mm_s,
        log=log,
    )


def _move_to_bed_target_locked(target: dict, *, speed_mm_s: float, log) -> dict:
    with serial.Serial(PLOTTER_PORT, timeout=2) as port:
        require_enabled_high_resolution_motors(port)
        return _move_to_bed_target_on_port_locked(port, target, speed_mm_s=speed_mm_s, log=log)


def current_hardware_bed_position_locked() -> dict:
    with serial.Serial(PLOTTER_PORT, timeout=2) as port:
        require_enabled_high_resolution_motors(port)
        _axis_1, _axis_2, raw_current = read_step_position(port)
    current = current_position_estimate(raw_current)
    if current is None:
        raise RuntimeError("Could not calculate current hardware position")
    with position_lock:
        set_current_position_unlocked(current["x_mm"], current["y_mm"])
        save_position_offset_unlocked()
    return current


def align_job_to_plot_origin(job: dict, log) -> dict | None:
    origin = job.get("plot_origin")
    if not isinstance(origin, dict):
        return None
    target_x, target_y = validate_bed_target(origin["x_mm"], origin["y_mm"])
    pen_defaults = current_pen_settings()
    plot_defaults = current_plot_settings()
    log.write(
        f"\nAligning job origin to paper: ({target_x:.3f}, {target_y:.3f}) "
        f"anchor={origin.get('anchor', '-')}\n"
    )
    log.flush()

    with hardware_lock:
        with serial.Serial(PLOTTER_PORT, timeout=2) as port:
            require_cached_high_resolution_motors(port)
            _run_pen_servo_on_port_locked(
                port,
                raised=True,
                up_pos=pen_defaults["pen_pos_up"],
                down_pos=pen_defaults["pen_pos_down"],
                raise_rate=plot_defaults.get("pen_rate_raise", DEFAULT_PEN_RATE_RAISE),
                lower_rate=DEFAULT_PEN_RATE_LOWER,
                delay_up_ms=plot_defaults.get("pen_delay_up", DEFAULT_PEN_DELAY_UP),
                delay_down_ms=plot_defaults.get("pen_delay_down", DEFAULT_PEN_DELAY_DOWN),
                label="Raise pen before paper-origin alignment",
                log=log,
            )
            actual = _move_to_bed_target_on_port_locked(
                port,
                {"x_mm": target_x, "y_mm": target_y},
                speed_mm_s=min(SAFE_MANUAL_MAX_XY_SPEED_MM_S, 120.0),
                log=log,
            )
            response = raw_command(port, "CS\r")
            if not response.startswith("OK"):
                raise RuntimeError(f"Unexpected CS response while aligning job origin: {response!r}")

    with position_lock:
        position_offset["x_mm"] = target_x
        position_offset["y_mm"] = target_y
        set_home_position_unlocked(target_x, target_y)
        set_current_position_unlocked(target_x, target_y)
        save_position_offset_unlocked()

    result = {
        "requested_position": {"x_mm": target_x, "y_mm": target_y},
        "actual_before_zero": actual,
        "controller_zero_response": response,
    }
    update_job(job["id"], last_origin_alignment=result)
    return result


def _run_bed_delta_locked(
    port: serial.Serial,
    delta_x: float,
    delta_y: float,
    *,
    speed_mm_s: float,
) -> None:
    distance = math.hypot(delta_x, delta_y)
    if distance < 0.001:
        return
    raw_delta = bed_delta_to_raw_delta(delta_x, delta_y)
    axis_1_delta, axis_2_delta = xy_mm_to_steps(raw_delta["x_mm"], raw_delta["y_mm"])
    duration_ms = max(40, int(round(distance / speed_mm_s * 1000)))
    response = raw_command(port, f"SM,{duration_ms},{axis_1_delta},{axis_2_delta}\r")
    if not response.startswith("OK"):
        raise RuntimeError(f"EBB dip-circle move failed: {response!r}")
    wait_for_motion_idle(port, max(2.0, duration_ms / 1000.0 + 1.0))


def _run_dip_circles_on_port_locked(port: serial.Serial, job: dict, log) -> dict:
    well = job["ink_well"]
    count = int(well.get("dip_circle_count", 0) or 0)
    diameter_mm = float(well.get("dip_circle_diameter_mm", 0.0) or 0.0)
    if count <= 0 or diameter_mm <= 0:
        return {"count": 0, "diameter_mm": diameter_mm}

    centre = well["centre"]
    centre_x, centre_y = validate_bed_target(centre["x_mm"], centre["y_mm"])
    radius_mm = diameter_mm / 2.0
    segments = max(8, int(DIP_CIRCLE_SEGMENTS))
    speed_mm_s = min(SAFE_MANUAL_MAX_XY_SPEED_MM_S, max(1.0, DIP_CIRCLE_SPEED_MM_S))
    targets: list[tuple[float, float]] = [(centre_x + radius_mm, centre_y)]
    for _circle in range(count):
        for segment in range(1, segments + 1):
            theta = (math.tau * segment) / segments
            targets.append(
                (
                    centre_x + radius_mm * math.cos(theta),
                    centre_y + radius_mm * math.sin(theta),
                )
            )
    targets.append((centre_x, centre_y))
    for target_x, target_y in targets:
        validate_bed_target(target_x, target_y)

    log.write(
        f"Dip circle agitation: {count} circle(s), diameter={diameter_mm:.1f} mm, "
        f"speed={speed_mm_s:.1f} mm/s\n"
    )
    log.flush()

    current_x, current_y = centre_x, centre_y
    for target_x, target_y in targets:
        _run_bed_delta_locked(
            port,
            target_x - current_x,
            target_y - current_y,
            speed_mm_s=speed_mm_s,
        )
        current_x, current_y = target_x, target_y
    _axis_1_after, _axis_2_after, raw_after = read_step_position(port)

    actual = current_position_estimate(raw_after)
    if actual is None:
        actual = {"x_mm": centre_x, "y_mm": centre_y}
    with position_lock:
        set_current_position_unlocked(actual["x_mm"], actual["y_mm"])
        save_position_offset_unlocked()
    return {
        "count": count,
        "diameter_mm": diameter_mm,
        "actual_position": actual,
    }


def _run_dip_circles_locked(job: dict, log) -> dict:
    with serial.Serial(PLOTTER_PORT, timeout=2) as port:
        require_enabled_high_resolution_motors(port)
        return _run_dip_circles_on_port_locked(port, job, log)


def execute_dip_cycle(job: dict, log, *, return_position: dict | None = None) -> dict:
    cycle_start = timing_log.monotonic()
    well = job.get("ink_well")
    if not job.get("auto_dip_enabled") or not well:
        raise RuntimeError("Automatic dipping is not configured for this job")

    if return_position is None:
        return_position = current_software_position()

    speed_mm_s = min(
        SAFE_MANUAL_MAX_XY_SPEED_MM_S,
        float(well.get("travel_speed_mm_s", DEFAULT_INK_WELL_TRAVEL_SPEED_MM_S)),
    )
    with hardware_lock:
        with serial.Serial(PLOTTER_PORT, timeout=2) as port:
            phase_start = timing_log.monotonic()
            require_enabled_high_resolution_motors(port)
            _run_dip_servo_on_port_locked(port, job, raised=True, log=log)
            timing_log.write_timing(log, "dip_clearance_raise", phase_start, job_id=job.get("id"))
            update_job(job["id"], dip_return_position=dict(return_position))
            phase_start = timing_log.monotonic()
            _move_to_bed_target_on_port_locked(port, well["centre"], speed_mm_s=speed_mm_s, log=log)
            timing_log.write_timing(log, "dip_travel_to_well", phase_start, job_id=job.get("id"))
            phase_start = timing_log.monotonic()
            _run_dip_servo_on_port_locked(port, job, raised=False, log=log)
            circle_result = _run_dip_circles_on_port_locked(port, job, log)
            time.sleep(well["dwell_ms"] / 1000.0)
            timing_log.write_timing(
                log,
                "dip_load_pen",
                phase_start,
                job_id=job.get("id"),
                dwell_ms=well.get("dwell_ms"),
                circles=circle_result.get("count"),
            )
            phase_start = timing_log.monotonic()
            _run_dip_servo_on_port_locked(port, job, raised=True, log=log)
            if well.get("drip_dwell_ms", 0):
                time.sleep(well["drip_dwell_ms"] / 1000.0)
            timing_log.write_timing(
                log,
                "dip_raise_after_load",
                phase_start,
                job_id=job.get("id"),
                drip_dwell_ms=well.get("drip_dwell_ms", 0),
            )
            phase_start = timing_log.monotonic()
            actual = _move_to_bed_target_on_port_locked(port, return_position, speed_mm_s=speed_mm_s, log=log)
            timing_log.write_timing(log, "dip_return_to_plot", phase_start, job_id=job.get("id"))

    error_mm = math.hypot(
        actual["x_mm"] - return_position["x_mm"],
        actual["y_mm"] - return_position["y_mm"],
    )
    if error_mm > 0.5:
        raise RuntimeError(f"Dip return verification failed with {error_mm:.4f} mm error")
    result = {
        "return_position": dict(return_position),
        "actual_position": actual,
        "return_error_mm": round(error_mm, 4),
        "dip_circles": circle_result,
    }
    result["timing"] = timing_log.write_timing(
        log,
        "dip_cycle_total",
        cycle_start,
        job_id=job.get("id"),
        return_error_mm=result["return_error_mm"],
    )
    return result


def attempt_dip_clearance_raise(job: dict, log) -> None:
    try:
        with hardware_lock:
            _run_dip_servo_locked(job, raised=True, log=log)
    except Exception as exc:
        log.write(f"Emergency clearance raise also failed: {exc!r}\n")
        log.flush()


def return_from_failed_dip_without_loading_ink(job: dict, log, return_position: dict) -> dict:
    with hardware_lock:
        well = job.get("ink_well") or {}
        speed_mm_s = min(
            SAFE_MANUAL_MAX_XY_SPEED_MM_S,
            float(well.get("travel_speed_mm_s", DEFAULT_INK_WELL_TRAVEL_SPEED_MM_S)),
        )
        _run_dip_servo_locked(job, raised=True, log=log)
        actual = _move_to_bed_target_locked(return_position, speed_mm_s=speed_mm_s, log=log)
    error_mm = math.hypot(
        actual["x_mm"] - return_position["x_mm"],
        actual["y_mm"] - return_position["y_mm"],
    )
    if error_mm > 0.5:
        raise RuntimeError(f"Skip-dip return verification failed with {error_mm:.4f} mm error")
    return {
        "return_position": dict(return_position),
        "actual_position": actual,
        "return_error_mm": round(error_mm, 4),
        "skipped": True,
    }


def mark_manual_hardware_priority(duration_s: float | None = None) -> None:
    global manual_hardware_priority_until
    duration = MANUAL_HARDWARE_PRIORITY_GRACE_S if duration_s is None else float(duration_s)
    with manual_hardware_priority_lock:
        manual_hardware_priority_until = max(manual_hardware_priority_until, time.monotonic() + max(0.0, duration))


def manual_hardware_priority_active() -> bool:
    with manual_hardware_priority_lock:
        return time.monotonic() < manual_hardware_priority_until


def overlay_live_hardware_fields(state: dict) -> dict:
    result = dict(state)
    with active_process_lock:
        active_pid = active_process.pid if active_process else None
        active_job = active_process_job_id
    with position_lock:
        raw_xy_mm = result.get("raw_position_estimate")
        result["position_offset"] = dict(position_offset)
        result["position_calibration_id"] = position_calibration_id
        result["position_calibration_enabled"] = position_calibration_enabled
        result["home_position"] = dict(home_position) if home_position is not None else None
        result["position_source"] = "software" if position_current is not None else "step_counter"
        if position_current is not None:
            result["position_estimate"] = dict(position_current)
        else:
            result["position_estimate"] = current_position_estimate(raw_xy_mm)
    result["active_process"] = {"pid": active_pid, "job_id": active_job}
    result["paper"] = current_paper_settings()
    return result


def cached_hardware_snapshot() -> dict:
    with hardware_state_lock:
        state = json.loads(json.dumps(cached_hardware_state))
    state = overlay_live_hardware_fields(state)
    updated_at = state.get("telemetry_updated_at")
    state["telemetry_age_s"] = round(now() - updated_at, 3) if updated_at else None
    return state


def update_cached_hardware_state(fields: dict) -> None:
    with hardware_state_lock:
        cached_hardware_state.clear()
        cached_hardware_state.update(fields)


def read_hardware_state_from_device(previous: dict | None = None, *, full: bool = False) -> dict:
    previous = previous or {}
    try:
        with serial.Serial(PLOTTER_PORT, timeout=TELEMETRY_SERIAL_TIMEOUT_S) as port:
            version = previous.get("firmware")
            motor_enable_raw = previous.get("motor_enable_raw")
            if full or not version:
                version, _ = serial_query(port, "v\r", ack=False)

            pen_up, pen_ack = serial_query(port, "QP\r")
            button, button_ack = serial_query(port, "QB\r")
            steps, steps_ack = serial_query(port, "QS\r")

            axis_1 = axis_2 = None
            raw_xy_mm = None
            try:
                axis_1_text, axis_2_text = steps.split(",", 1)
                axis_1 = int(axis_1_text)
                axis_2 = int(axis_2_text)
                raw_xy_mm = steps_to_xy_mm(axis_1, axis_2)
            except ValueError:
                pass

            if full or not motor_enable_raw:
                motor_1_raw, _ = serial_query(port, "PI,E,0\r", ack=False)
                motor_2_raw, _ = serial_query(port, "PI,C,1\r", ack=False)
                motor_enable_raw = {"axis_1": motor_1_raw, "axis_2": motor_2_raw}
    except serial.SerialException as exc:
        return {
            "busy": False,
            "connected": False,
            "port": PLOTTER_PORT,
            "error": str(exc),
            "telemetry_stale": False,
            "telemetry_updated_at": now(),
        }

    return overlay_live_hardware_fields(
        {
            "busy": False,
            "connected": True,
            "port": PLOTTER_PORT,
            "firmware": version,
            "pen_up": pen_up == "1",
            "button_pressed": button == "1",
            "steps": {"axis_1": axis_1, "axis_2": axis_2, "raw": steps},
            "raw_position_estimate": raw_xy_mm,
            "bed_position_unoffset": raw_xy_to_bed_xy(raw_xy_mm),
            "motor_enable_raw": motor_enable_raw,
            "acks": {"pen": pen_ack, "button": button_ack, "steps": steps_ack},
            "telemetry_stale": False,
            "telemetry_updated_at": now(),
        }
    )


def poll_hardware_state_once(*, full: bool = False) -> bool:
    if manual_hardware_priority_active():
        return False
    # While an axicli subprocess owns the serial port, do not attempt to read
    # the device.  Mark the cache as busy/stale so the UI shows a meaningful
    # state instead of frozen telemetry.
    with active_process_lock:
        has_active_process = active_process is not None
    if has_active_process:
        with hardware_state_lock:
            cached_hardware_state[busy] = True
            cached_hardware_state[telemetry_stale] = True
            cached_hardware_state[telemetry_updated_at] = now()
        return True
    if not hardware_lock.acquire(blocking=False):
        return False
    try:
        previous = cached_hardware_snapshot()
        update_cached_hardware_state(read_hardware_state_from_device(previous, full=full))
        return True
    finally:
        hardware_lock.release()


def hardware_telemetry_worker() -> None:
    last_full_poll = 0.0
    while True:
        try:
            monotonic_now = time.monotonic()
            full = monotonic_now - last_full_poll >= TELEMETRY_FULL_POLL_INTERVAL_S
            if poll_hardware_state_once(full=full):
                if full:
                    last_full_poll = monotonic_now
        except Exception as exc:
            snapshot = cached_hardware_snapshot()
            snapshot.update(
                {
                    "busy": False,
                    "connected": False,
                    "port": PLOTTER_PORT,
                    "error": repr(exc),
                    "telemetry_stale": True,
                    "telemetry_updated_at": now(),
                }
            )
            update_cached_hardware_state(snapshot)
        time.sleep(max(0.05, TELEMETRY_POLL_INTERVAL_S))


def read_hardware_state() -> dict:
    return cached_hardware_snapshot()


def bleep(times: int = 3, gap: float = 0.25) -> None:
    for _ in range(times):
        print("\a", end="", flush=True)
        time.sleep(gap)


def announce_on_linux_box(message: str) -> None:
    print("\n" + "=" * 72, flush=True)
    print(message, flush=True)
    print("=" * 72 + "\n", flush=True)
    bleep()


def wait_for_operator(job_id: str, message: str, action: str = "continue") -> bool:
    """
    Block the plotter worker until the local Linux operator console calls
    /operator/continue.
    """
    global operator_prompt

    with operator_lock:
        operator_event.clear()
        operator_prompt = {
            "active": True,
            "job_id": job_id,
            "message": message,
            "action": action,
            "created_at": now(),
        }

    update_job(job_id, status="waiting_for_operator", operator_message=message)
    announce_on_linux_box(message)

    operator_event.wait()

    with operator_lock:
        operator_prompt = {
            "active": False,
            "job_id": None,
            "message": None,
            "action": None,
            "created_at": None,
        }

    with jobs_lock:
        cancelled = jobs.get(job_id, {}).get("status") == "cancelled"

    update_job(job_id, operator_message=None)
    return not cancelled


def return_home(log) -> int:
    """
    Return to the start/home position after a layer.

    This uses the direct EBB motion path so job cleanup follows the same
    calibrated bed-coordinate model as browser Home and ink-well movement.
    """
    with position_lock:
        home = dict(home_position) if home_position is not None else None
    if home is None:
        log.write("Cannot return home: calibrate the plotter first.\n")
        log.flush()
        return 1

    log.write("\nReturning to start/home:\n")
    log.flush()

    with hardware_lock:
        try:
            with serial.Serial(PLOTTER_PORT, timeout=2) as port:
                require_cached_high_resolution_motors(port)
                pen_up, pen_ack = serial_query(port, "QP\r")

                log.write(f"Pen state before homing: {'up' if pen_up == '1' else 'down'}")
                if pen_ack:
                    log.write(f" ({pen_ack})")
                log.write("\n")

                if pen_up != "1":
                    pen_defaults = current_pen_settings()
                    plot_defaults = current_plot_settings()
                    _run_pen_servo_on_port_locked(
                        port,
                        raised=True,
                        up_pos=pen_defaults["pen_pos_up"],
                        down_pos=pen_defaults["pen_pos_down"],
                        raise_rate=plot_defaults.get("pen_rate_raise", DEFAULT_PEN_RATE_RAISE),
                        lower_rate=DEFAULT_PEN_RATE_LOWER,
                        delay_up_ms=plot_defaults.get("pen_delay_up", DEFAULT_PEN_DELAY_UP),
                        delay_down_ms=plot_defaults.get("pen_delay_down", DEFAULT_PEN_DELAY_DOWN),
                        label="Raise pen before homing",
                        log=log,
                    )

                actual = _move_to_bed_target_on_port_locked(
                    port,
                    home,
                    speed_mm_s=min(SAFE_MANUAL_MAX_XY_SPEED_MM_S, 120.0),
                    log=log,
                )
            home_error_mm = math.hypot(actual["x_mm"] - home["x_mm"], actual["y_mm"] - home["y_mm"])
            if home_error_mm > 0.5:
                log.write(
                    f"Home verification failed: expected {home}, actual {actual}, "
                    f"error={home_error_mm:.4f} mm\n"
                )
                log.flush()
                return 1
        except (serial.SerialException, RuntimeError, ValueError) as exc:
            log.write(f"Could not return home: {exc}\n")
            log.flush()
            return 1
        return 0


def run_layer(job: dict, layer: dict, log) -> str:
    """
    Plot one SVG layer.

    Returns:
      "done"
      "paused"
      "failed"
    """
    return plot_execution.run_layer(
        job,
        layer,
        log,
        axicli=AXICLI,
        axicli_config=AXICLI_CONFIG,
        plotter_port=PLOTTER_PORT,
        run_axicli_command=run_axicli_command,
    )


def resume_progress_output_path(progress_svg: Path) -> Path:
    return plot_execution.resume_progress_output_path(progress_svg)


def finalize_resume_progress(progress_svg: Path, resumed_svg: Path) -> bool:
    return plot_execution.finalize_resume_progress(progress_svg, resumed_svg)


def resume_layer(job: dict, layer: dict, log) -> str:
    """
    Resume a layer from its AxiDraw progress SVG.

    Returns:
      "done"
      "paused"
      "failed"
    """
    return plot_execution.resume_layer(
        job,
        layer,
        log,
        axicli=AXICLI,
        axicli_config=AXICLI_CONFIG,
        plotter_port=PLOTTER_PORT,
        run_axicli_command=run_axicli_command,
    )


def prime_pen_down_after_manual_dip(job: dict, log) -> dict | None:
    pending = job.get("manual_dip_needs_pen_down")
    if not pending:
        return None

    pen_defaults = current_pen_settings()
    plot_defaults = current_plot_settings()
    with hardware_lock:
        with serial.Serial(PLOTTER_PORT, timeout=2) as port:
            require_cached_high_resolution_motors(port)
            result = _run_pen_servo_on_port_locked(
                port,
                raised=False,
                up_pos=job.get("pen_pos_up", pen_defaults["pen_pos_up"]),
                down_pos=job.get("pen_pos_down", pen_defaults["pen_pos_down"]),
                raise_rate=plot_defaults.get("pen_rate_raise", DEFAULT_PEN_RATE_RAISE),
                lower_rate=DEFAULT_PEN_RATE_LOWER,
                delay_up_ms=job.get("pen_delay_up", plot_defaults.get("pen_delay_up", DEFAULT_PEN_DELAY_UP)),
                delay_down_ms=job.get("pen_delay_down", plot_defaults.get("pen_delay_down", DEFAULT_PEN_DELAY_DOWN)),
                label="Prime pen down before resuming after manual dip",
                log=log,
            )
    update_job(job["id"], manual_dip_needs_pen_down=False, last_manual_dip_pen_prime=result)
    return result


def run_layer_with_auto_dips(
    job: dict,
    layer: dict,
    log,
    *,
    resume: bool = False,
    perform_initial_dip: bool = True,
) -> str:
    if not job.get("auto_dip_enabled"):
        return resume_layer(job, layer, log) if resume else run_layer(job, layer, log)

    def perform_dip(phase: str) -> bool:
        dip_start = timing_log.monotonic()
        update_job(
            job["id"],
            status="dipping",
            dip_phase=phase,
            dip_layer=layer["index"],
        )
        try:
            if phase == "initial" and not resume:
                return_position = job_plot_start_position(job)
            else:
                return_position = current_hardware_bed_position_locked()
            update_job(job["id"], dip_return_position=dict(return_position))
            result = execute_dip_cycle(job, log, return_position=return_position)
            update_job(
                job["id"],
                dip_count=int(job.get("dip_count", 0)) + 1,
                last_dip=result,
                status="running",
                dip_phase=None,
            )
            timing_log.write_timing(
                log,
                "auto_dip_phase_complete",
                dip_start,
                job_id=job.get("id"),
                layer=layer.get("index"),
                phase=phase,
            )
            return True
        except Exception as exc:
            attempt_dip_clearance_raise(job, log)
            update_job(
                job["id"],
                status="dip_failed",
                dip_failure={
                    "error": repr(exc),
                    "layer": layer["index"],
                    "phase": phase,
                    "return_position": jobs.get(job["id"], {}).get("dip_return_position"),
                    "created_at": now(),
                },
                operator_message=(
                    "Automatic dip failed. Use Retry Dip, Skip Dip & Resume, or Cancel. "
                    "The job will not resume automatically."
                ),
            )
            announce_on_linux_box(f"Job {job['id']} automatic dip failed: {exc!r}")
            timing_log.write_timing(
                log,
                "auto_dip_phase_failed",
                dip_start,
                job_id=job.get("id"),
                layer=layer.get("index"),
                phase=phase,
            )
            return False

    if not resume and perform_initial_dip and not perform_dip("initial"):
        return "dip_failed"

    update_job(job["id"], status="running", dip_phase=None)
    if resume:
        prime_pen_down_after_manual_dip(job, log)
    plot_start = timing_log.monotonic()
    plot_result = resume_layer(job, layer, log) if resume else run_layer(job, layer, log)
    timing_log.write_timing(
        log,
        "auto_dip_plot_segment",
        plot_start,
        job_id=job.get("id"),
        layer=layer.get("index"),
        result=plot_result,
        resume=resume,
    )
    while plot_result == "auto_dip_pause":
        checkpoint_start = timing_log.monotonic()
        if not perform_dip("checkpoint"):
            return "dip_failed"
        resume_start = timing_log.monotonic()
        plot_result = resume_layer(job, layer, log)
        timing_log.write_timing(
            log,
            "auto_dip_resume_after_checkpoint",
            resume_start,
            job_id=job.get("id"),
            layer=layer.get("index"),
            result=plot_result,
        )
        timing_log.write_timing(
            log,
            "auto_dip_checkpoint_round_trip",
            checkpoint_start,
            job_id=job.get("id"),
            layer=layer.get("index"),
            result=plot_result,
        )
    return plot_result


def job_runner_context() -> SimpleNamespace:
    return SimpleNamespace(
        jobs=jobs,
        jobs_lock=jobs_lock,
        update_job=update_job,
        log_tail=log_tail,
        now=now,
        run_layer_with_auto_dips=run_layer_with_auto_dips,
        return_home=return_home,
        wait_for_operator=wait_for_operator,
        announce_on_linux_box=announce_on_linux_box,
        execute_dip_cycle=execute_dip_cycle,
        return_from_failed_dip_without_loading_ink=return_from_failed_dip_without_loading_ink,
        attempt_dip_clearance_raise=attempt_dip_clearance_raise,
    )


def continue_job_after_layer(job_id: str, start_layer_number: int, log) -> None:
    job_runner.continue_job_after_layer(job_runner_context(), job_id, start_layer_number, log)


def resume_paused_job(job_id: str) -> None:
    job_runner.resume_paused_job(job_runner_context(), job_id)


def recover_dip_failed_job(job_id: str, *, retry_dip: bool) -> None:
    job_runner.recover_dip_failed_job(job_runner_context(), job_id, retry_dip=retry_dip)


def worker() -> None:
    while True:
        job_id = job_queue.get()

        with jobs_lock:
            job = jobs.get(job_id)
            if job is None:
                job_queue.task_done()
                continue
            status = job.get("status")
            if status == "cancelled":
                job_queue.task_done()
                continue

        # Resume and dip-recovery go through the same queue so the
        # single-worker invariant holds (no ad-hoc threads).
        if status == "queued_for_resume":
            try:
                with jobs_lock:
                    retry_dip = jobs[job_id].pop("dip_recovery_retry", None)
                if retry_dip is not None:
                    recover_dip_failed_job(job_id, retry_dip=retry_dip)
                else:
                    resume_paused_job(job_id)
            except Exception as exc:
                update_job(job_id, status="failed", error=repr(exc), finished_at=now())
            finally:
                job_queue.task_done()
            continue

        log_path = Path(job["log_path"])

        try:
            with log_path.open("w", encoding="utf-8", errors="replace") as log:
                update_job(job_id, status="queued_for_operator")

                wait_for_operator(
                    job_id,
                    (
                        f"Job {job_id} is ready. Load paper, connect motors, "
                        "check start position, check pen, then press Enter on the Linux box "
                        "to start Layer 1."
                    ),
                    action="start",
                )

                with jobs_lock:
                    if jobs[job_id].get("status") == "cancelled":
                        log.write("Job cancelled before hardware execution.\n")
                        log.flush()
                        continue

                update_job(
                    job_id,
                    status="running",
                    started_at=now(),
                    current_layer=None,
                    current_layer_name=None,
                    last_completed_layer=None,
                )

                if job.get("plot_origin"):
                    origin = job["plot_origin"]
                    log.write(
                        "\nPaper origin is recorded for the UI only; "
                        "automatic pre-plot alignment is disabled for safety. "
                        f"origin=({float(origin.get('x_mm', 0.0)):.3f}, "
                        f"{float(origin.get('y_mm', 0.0)):.3f}) "
                        f"anchor={origin.get('anchor', '-')}\n"
                    )
                    log.flush()

                layers = job["layers"]
                stop_current_job = False

                for layer in layers:
                    layer_number = layer["index"]
                    layer_count = len(layers)

                    update_job(
                        job_id,
                        status="running",
                        current_layer=layer_number,
                        current_layer_name=layer["name"],
                    )

                    result = run_layer_with_auto_dips(job, layer, log)

                    if result == "dip_failed":
                        stop_current_job = True
                        break

                    if result == "paused":
                        update_job(
                            job_id,
                            status="paused",
                            paused_layer=layer_number,
                            log_tail=log_tail(log_path),
                        )
                        announce_on_linux_box(
                            f"Job {job_id} paused during Layer {layer_number}. "
                            "Resume handling is needed before continuing."
                        )
                        stop_current_job = True
                        break

                    if result != "done":
                        update_job(
                            job_id,
                            status="failed",
                            failed_layer=layer_number,
                            finished_at=now(),
                            log_tail=log_tail(log_path),
                        )
                        announce_on_linux_box(
                            f"Job {job_id} failed during Layer {layer_number}. Check the log."
                        )
                        stop_current_job = True
                        break

                    update_job(
                        job_id,
                        last_completed_layer=layer_number,
                        log_tail=log_tail(log_path),
                    )

                    home_return_code = return_home(log)

                    if home_return_code != 0:
                        update_job(
                            job_id,
                            status="failed",
                            failed_layer=layer_number,
                            finished_at=now(),
                            error=f"walk_home failed with return code {home_return_code}",
                            log_tail=log_tail(log_path),
                        )
                        announce_on_linux_box(
                            f"Layer {layer_number} plotted, but return-home failed. "
                            "Do not continue until the start position is checked."
                        )
                        stop_current_job = True
                        break

                    if layer_number < layer_count:
                        next_layer = layers[layer_number]

                        if not wait_for_operator(
                            job_id,
                            (
                                f"Layer {layer_number} complete. "
                                f"Change pen for Layer {next_layer['index']}: {next_layer['name']}. "
                                "Check paper and start position. Then press Enter on the Linux box."
                            ),
                        ):
                            stop_current_job = True
                            break

                if stop_current_job:
                    continue

                update_job(
                    job_id,
                    status="done",
                    finished_at=now(),
                    current_layer=None,
                    current_layer_name=None,
                    operator_message=None,
                    log_tail=log_tail(log_path),
                )

                announce_on_linux_box(f"Job {job_id} complete. All layers plotted.")

        except Exception as exc:
            update_job(
                job_id,
                status="failed",
                finished_at=now(),
                error=repr(exc),
                log_tail=log_tail(log_path),
            )
            announce_on_linux_box(f"Job {job_id} failed: {exc!r}")

        finally:
            job_queue.task_done()


load_position_offset()
load_pen_settings()
load_plot_settings()
load_paper_settings()
load_ink_well_settings()
load_jobs()
if os.environ.get("PLOTTER_DISABLE_WORKER") != "1":
    threading.Thread(target=hardware_telemetry_worker, daemon=True).start()
    threading.Thread(target=worker, daemon=True).start()


@app.get("/health")
def health(x_plotter_token: Optional[str] = Header(default=None)):
    check_token(x_plotter_token)

    return {
        "ok": True,
        "name": APP_NAME,
        "version": APP_VERSION,
        "plotter_port": PLOTTER_PORT,
        "axicli": AXICLI,
        "axicli_config": str(AXICLI_CONFIG) if AXICLI_CONFIG.exists() else None,
        "queue_size": job_queue.qsize(),
        "token_required": bool(PLOTTER_TOKEN),
        "motion_spec": motion_spec(),
    }


@app.get("/control")
def control_page():
    return FileResponse(STATIC_DIR / "control.html")


@app.get("/control/config")
def control_config(request: Request):
    require_localhost(request)
    pen_defaults = current_pen_settings()
    plot_defaults = current_plot_settings()
    well_defaults = current_ink_well_settings()
    paper_defaults = current_paper_settings()
    forwarded_proto = request.headers.get("x-forwarded-proto")
    scheme = forwarded_proto or request.url.scheme
    http_host = request.headers.get("host") or f"{request.url.hostname}:{request.url.port}"
    return {
        "token": PLOTTER_TOKEN,
        "server": "local",
        "http_scheme": scheme,
        "http_host": http_host,
        "http_origin": f"{scheme}://{http_host}",
        "plotter_port": PLOTTER_PORT,
        "pen_pos_up": pen_defaults["pen_pos_up"],
        "pen_pos_down": pen_defaults["pen_pos_down"],
        "speed_pendown": plot_defaults["speed_pendown"],
        "speed_penup": plot_defaults["speed_penup"],
        "pen_delay_down": plot_defaults["pen_delay_down"],
        "pen_delay_up": plot_defaults["pen_delay_up"],
        "pen_rate_raise": plot_defaults["pen_rate_raise"],
        "auto_dip_enabled": plot_defaults["auto_dip_enabled"],
        "dip_interval_s": plot_defaults["dip_interval_s"],
        "ink_well": well_defaults,
        "paper": paper_defaults,
        "paper_sizes": PAPER_SIZES_MM,
        "motion_spec": motion_spec(),
        "axicli_config": str(AXICLI_CONFIG) if AXICLI_CONFIG.exists() else None,
    }


@app.post("/plot/layers")
async def plot_layers(
    files: List[UploadFile] = File(...),
    layer_names: Optional[str] = Form(None),
    speed_pendown: Optional[int] = Form(None),
    speed_penup: Optional[int] = Form(None),
    pen_delay_down: Optional[int] = Form(None),
    pen_delay_up: Optional[int] = Form(None),
    pen_rate_raise: Optional[int] = Form(None),
    pen_pos_down: Optional[int] = Form(None),
    pen_pos_up: Optional[int] = Form(None),
    auto_dip: Optional[bool] = Form(None),
    auto_dip_enabled: Optional[bool] = Form(None),
    ink_dip: Optional[bool] = Form(None),
    ink_dipping: Optional[bool] = Form(None),
    automatic_ink_dipping: Optional[bool] = Form(None),
    autoDip: Optional[bool] = Form(None),
    dip_interval_s: Optional[float] = Form(None),
    x_plotter_token: Optional[str] = Header(default=None),
):
    """
    Upload a multi-layer plot.

    Files are plotted in the order they are sent:
      layer 1, layer 2, layer 3, etc.

    Optional layer_names is comma-separated:
      "Light blue,Dark blue,Black,White"
    """
    check_token(x_plotter_token)
    pen_defaults = current_pen_settings()
    plot_defaults = current_plot_settings()
    well_settings = current_ink_well_settings()
    auto_dip_values = (auto_dip, auto_dip_enabled, ink_dip, ink_dipping, automatic_ink_dipping, autoDip)
    auto_dip = (
        resolve_auto_dip_flag(*auto_dip_values)
        if auto_dip_flag_was_provided(*auto_dip_values)
        else bool(plot_defaults.get("auto_dip_enabled")) and bool(well_settings.get("installed"))
    )
    if dip_interval_s is None and auto_dip:
        dip_interval_s = plot_defaults.get("dip_interval_s")
    if speed_pendown is None:
        speed_pendown = plot_defaults["speed_pendown"]
    if speed_penup is None:
        speed_penup = plot_defaults["speed_penup"]
    speed_pendown = validate_speed_setting(speed_pendown, "speed_pendown")
    speed_penup = validate_speed_setting(speed_penup, "speed_penup")
    if pen_delay_down is None:
        pen_delay_down = plot_defaults["pen_delay_down"]
    pen_delay_down = validate_pen_delay_down(pen_delay_down)
    if pen_delay_up is None:
        pen_delay_up = plot_defaults["pen_delay_up"]
    pen_delay_up = validate_pen_delay_up(pen_delay_up)
    if pen_rate_raise is None:
        pen_rate_raise = plot_defaults["pen_rate_raise"]
    pen_rate_raise = validate_pen_rate_raise(pen_rate_raise)
    if pen_pos_down is None:
        pen_pos_down = pen_defaults["pen_pos_down"]
    if pen_pos_up is None:
        pen_pos_up = pen_defaults["pen_pos_up"]
    pen_pos_down = validate_pen_position(pen_pos_down, "pen_pos_down")
    pen_pos_up = validate_pen_position(pen_pos_up, "pen_pos_up")

    upload_settings = {
        "speed_pendown": speed_pendown,
        "speed_penup": speed_penup,
        "pen_pos_down": pen_pos_down,
        "pen_pos_up": pen_pos_up,
    }
    well_snapshot = None
    upload_home = None
    if auto_dip:
        if not well_settings.get("installed"):
            raise HTTPException(
                status_code=409,
                detail="Complete the ink well test and mark it installed before enabling automatic dipping",
            )
        if dip_interval_s is None:
            raise HTTPException(status_code=400, detail="dip_interval_s is required when auto_dip is enabled")
        dip_interval_s = validate_dip_interval(dip_interval_s)
    if well_settings.get("installed"):
        try:
            well_snapshot = ink_well_plot_snapshot(well_settings)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        try:
            upload_home = current_home_position()
        except HTTPException as exc:
            raise HTTPException(
                status_code=409,
                detail="Calibrate and save Home before uploading while the ink well is installed",
            ) from exc

    if not files:
        raise HTTPException(status_code=400, detail="No files uploaded")

    if len(files) > 8:
        raise HTTPException(status_code=400, detail="Too many layers for one job")

    names = []
    if layer_names:
        names = [x.strip() for x in layer_names.split(",")]

    job_id = uuid.uuid4().hex[:12]
    this_job_dir = job_dir(job_id)
    this_job_dir.mkdir(parents=True, exist_ok=True)

    layers = []

    for idx, uploaded in enumerate(files, start=1):
        if not uploaded.filename.lower().endswith(".svg"):
            raise HTTPException(
                status_code=400,
                detail=f"Only .svg files are accepted: {uploaded.filename}",
            )

        layer_dir = this_job_dir / f"layer_{idx:02d}"
        layer_dir.mkdir(parents=True, exist_ok=True)

        input_svg = layer_dir / "input.svg"
        progress_svg = layer_dir / "progress.svg"
        digest_svg = layer_dir / "plot_digest.svg"

        with input_svg.open("wb") as out:
            shutil.copyfileobj(uploaded.file, out)

        try:
            svg_metrics = validate_svg_file(input_svg)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        ink_analysis = None
        if well_snapshot is not None:
            try:
                analysis_origin = plot_origin_for_layer_metrics(svg_metrics) or upload_home
                ink_analysis = analyse_layer_for_ink_well(
                    input_svg,
                    digest_svg,
                    job_settings=upload_settings,
                    home=analysis_origin,
                    well=well_snapshot,
                    dip_interval_s=dip_interval_s if auto_dip else None,
                )
                if auto_dip:
                    layer_stub = {
                        "plot_digest_svg": str(digest_svg),
                    }
                    prepare_auto_dip_layer(layer_stub, ink_analysis)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

        name = (
            names[idx - 1]
            if idx - 1 < len(names) and names[idx - 1]
            else uploaded.filename
        )

        layers.append(
            {
                "index": idx,
                "name": name,
                "original_filename": uploaded.filename,
                "input_svg": str(input_svg),
                "progress_svg": str(progress_svg),
                "plot_digest_svg": str(digest_svg) if ink_analysis is not None else None,
                "plot_svg": layer_stub.get("plot_svg") if auto_dip else None,
                "svg_metrics": svg_metrics,
                "ink_analysis": ink_analysis,
            }
        )

    log_path = LOGS_DIR / f"{job_id}.log"

    job = {
        "id": job_id,
        "status": "queued",
        "created_at": now(),
        "layer_count": len(layers),
        "layers": layers,
        "log_path": str(log_path),
        "speed_pendown": speed_pendown,
        "speed_penup": speed_penup,
        "pen_delay_down": pen_delay_down,
        "pen_delay_up": pen_delay_up,
        "pen_rate_raise": pen_rate_raise,
        "pen_pos_down": pen_pos_down,
        "pen_pos_up": pen_pos_up,
        "ink_well": well_snapshot,
        "auto_dip_enabled": auto_dip,
        "dip_interval_s": dip_interval_s if auto_dip else None,
        "dip_count": 0,
        "dip_failure": None,
        "current_layer": None,
        "current_layer_name": None,
        "last_completed_layer": None,
        "operator_message": None,
        "plot_start_position": upload_home,
    }
    try:
        apply_paper_alignment_to_job(job)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    response_payload = {
        "job_id": job_id,
        "status": "queued",
        "layer_count": len(layers),
        "auto_dip_enabled": auto_dip,
        "dip_estimates": layer_dip_estimates(layers),
        "status_url": f"/jobs/{job_id}",
    }

    with jobs_lock:
        jobs[job_id] = job
        save_job_unlocked(job_id)

    job_queue.put(job_id)

    return response_payload


@app.get("/jobs")
def list_jobs(
    x_plotter_token: Optional[str] = Header(default=None),
):
    check_token(x_plotter_token)

    with jobs_lock:
        summaries = []

        for job_id, job in jobs.items():
            status = job.get("status")
            summaries.append(
                {
                    "id": job_id,
                    "status": status,
                    "created_at": job.get("created_at"),
                    "started_at": job.get("started_at"),
                    "finished_at": job.get("finished_at"),
                    "layer_count": job.get("layer_count"),
                    "current_layer": job.get("current_layer"),
                    "current_layer_name": job.get("current_layer_name"),
                    "last_completed_layer": job.get("last_completed_layer"),
                    "speed_pendown": job.get("speed_pendown"),
                    "speed_penup": job.get("speed_penup"),
                    "pen_delay_down": job.get("pen_delay_down", DEFAULT_PEN_DELAY_DOWN),
                    "pen_delay_up": job.get("pen_delay_up", DEFAULT_PEN_DELAY_UP),
                    "pen_rate_raise": job.get("pen_rate_raise", DEFAULT_PEN_RATE_RAISE),
                    "pen_pos_down": job.get("pen_pos_down"),
                    "pen_pos_up": job.get("pen_pos_up"),
                    "operator_message": job.get("operator_message"),
                    "auto_dip_enabled": job.get("auto_dip_enabled", False),
                    "dip_interval_s": job.get("dip_interval_s"),
                    "dip_count": job.get("dip_count", 0),
                    "dip_failure": job.get("dip_failure"),
                    "plot_footprint": job_plot_footprint(job),
                    "plot_preview": job_plot_preview(job) if status in PLOT_PREVIEW_STATUSES else None,
                    "plot_origin": job.get("plot_origin"),
                    "paper": job.get("paper"),
                    "rerun_of": job.get("rerun_of"),
                    "log_path": job.get("log_path"),
                    "timing_summary": job_timing_summary(job),
                }
            )

    summaries.sort(key=lambda x: x.get("created_at") or 0, reverse=True)

    return {"jobs": summaries}


@app.get("/jobs/{job_id}")
def get_job(
    job_id: str,
    x_plotter_token: Optional[str] = Header(default=None),
):
    check_token(x_plotter_token)

    with jobs_lock:
        job = jobs.get(job_id)

    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    return job


@app.get("/jobs/{job_id}/layers/{layer_index}/preview.svg")
def job_layer_preview(
    request: Request,
    job_id: str,
    layer_index: int,
):
    require_localhost(request)

    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        layer = _job_layer_for_index(job, layer_index)
        if not layer:
            raise HTTPException(status_code=404, detail="Layer not found")
        path = _job_layer_input_path(job, layer)

    return FileResponse(
        path,
        media_type="image/svg+xml",
        headers={
            "Content-Security-Policy": "default-src 'none'; img-src data:; style-src 'unsafe-inline'",
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.post("/jobs/{job_id}/cancel")
def cancel_job(
    job_id: str,
    x_plotter_token: Optional[str] = Header(default=None),
):
    check_token(x_plotter_token)

    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        cancelled = cancel_job_record(job, reason="Cancelled by operator request.")
        if cancelled:
            save_job_unlocked(job_id)

    if not cancelled:
        raise HTTPException(status_code=400, detail=f"Job cannot be cancelled from status {job.get('status')!r}")

    with operator_lock:
        if operator_prompt.get("active") and operator_prompt.get("job_id") == job_id:
            operator_event.set()
    return {"job_id": job_id, "status": "cancelled"}


@app.get("/jobs/{job_id}/log")
def get_job_log(
    job_id: str,
    x_plotter_token: Optional[str] = Header(default=None),
):
    check_token(x_plotter_token)

    with jobs_lock:
        job = jobs.get(job_id)

    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    log_path = Path(job["log_path"])

    return {
        "job_id": job_id,
        "log_path": str(log_path),
        "tail": log_tail(log_path, max_chars=12000),
    }


@app.post("/jobs/{job_id}/pause")
def pause_job(
    job_id: str,
    x_plotter_token: Optional[str] = Header(default=None),
):
    check_token(x_plotter_token)

    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        if job.get("status") != "running":
            raise HTTPException(status_code=400, detail=f"Job is not running: {job.get('status')!r}")

    with active_process_lock:
        proc = active_process
        proc_job_id = active_process_job_id
        if not proc or proc_job_id != job_id:
            raise HTTPException(status_code=409, detail="No active AxiCLI process is associated with this job")
        proc.send_signal(signal.SIGINT)

    return {"ok": True, "job_id": job_id, "message": "Pause signal sent to AxiCLI"}


@app.post("/jobs/{job_id}/resume")
def resume_job(
    job_id: str,
    x_plotter_token: Optional[str] = Header(default=None),
):
    check_token(x_plotter_token)
    require_hardware_idle()
    resume_plot_settings = current_plot_settings()
    resume_pen_settings = current_pen_settings()

    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        if job.get("status") != "paused":
            raise HTTPException(status_code=400, detail=f"Job is not paused: {job.get('status')!r}")
        paused_layer_number = int(job.get("paused_layer") or job.get("current_layer") or 1)
        layer = next((item for item in job.get("layers", []) if item["index"] == paused_layer_number), None)
        if not layer:
            raise HTTPException(status_code=400, detail=f"Paused layer not found: {paused_layer_number}")
        if not Path(layer["progress_svg"]).exists():
            raise HTTPException(status_code=400, detail=f"Progress SVG not found: {layer['progress_svg']}")
        apply_plot_settings_to_job(job, resume_plot_settings)
        apply_pen_settings_to_job(job, resume_pen_settings)
        job["status"] = "queued_for_resume"
        job["operator_message"] = "Resume requested."
        save_job_unlocked(job_id)

    job_queue.put(job_id)

    return {
        "ok": True,
        "job_id": job_id,
        "status": "queued_for_resume",
        "paused_layer": paused_layer_number,
        "plot_settings": resume_plot_settings,
        "pen_settings": resume_pen_settings,
        "message": "Resume queued.",
    }


@app.post("/jobs/{job_id}/dip_recovery")
def recover_dip_job(
    job_id: str,
    payload: dict = Body(...),
    x_plotter_token: Optional[str] = Header(default=None),
):
    check_token(x_plotter_token)
    require_hardware_idle()
    action = str(payload.get("action", "")).strip().lower()
    if action not in {"retry", "skip"}:
        raise HTTPException(status_code=400, detail="action must be 'retry' or 'skip'")

    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        if job.get("status") != "dip_failed":
            raise HTTPException(status_code=400, detail=f"Job is not dip_failed: {job.get('status')!r}")
        failure = job.get("dip_failure") or {}
        if not isinstance(failure.get("return_position"), dict):
            raise HTTPException(
                status_code=409,
                detail="No verified return position is available; cancel and rerun after recalibration",
            )
        job["status"] = "queued_for_resume"
        job["dip_recovery_retry"] = action == "retry"
        job["operator_message"] = f"Dip recovery requested: {action}."
        save_job_unlocked(job_id)

    job_queue.put(job_id)
    return {
        "ok": True,
        "job_id": job_id,
        "status": "queued_for_resume",
        "action": action,
    }


@app.post("/jobs/{job_id}/dip_now")
def dip_paused_job_now(
    job_id: str,
    x_plotter_token: Optional[str] = Header(default=None),
):
    check_token(x_plotter_token)
    require_hardware_idle()

    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        if job.get("status") != "paused":
            raise HTTPException(status_code=400, detail=f"Job is not paused: {job.get('status')!r}")
        if not job.get("auto_dip_enabled") or not job.get("ink_well"):
            raise HTTPException(status_code=400, detail="Job does not have automatic dipping configured")
        log_path = Path(job["log_path"])

    try:
        with log_path.open("a", encoding="utf-8", errors="replace") as log:
            log.write(f"\nManual Dip Now requested at {now():.3f}\n")
            log.flush()
            return_position = current_hardware_bed_position_locked()
            update_job(
                job_id,
                status="dipping",
                dip_phase="manual",
                operator_message="Manual dip requested while paused.",
            )
            result = execute_dip_cycle(job, log, return_position=return_position)
    except Exception as exc:
        with log_path.open("a", encoding="utf-8", errors="replace") as log:
            attempt_dip_clearance_raise(job, log)
        update_job(
            job_id,
            status="dip_failed",
            dip_failure={
                "error": repr(exc),
                "layer": job.get("current_layer") or job.get("paused_layer") or 1,
                "phase": "manual",
                "return_position": jobs.get(job_id, {}).get("dip_return_position"),
                "created_at": now(),
            },
            operator_message=(
                "Manual dip failed. Use Retry Dip, Skip Dip & Resume, or Cancel. "
                "The job will not resume automatically."
            ),
        )
        raise HTTPException(status_code=500, detail={"message": "Manual dip failed", "error": repr(exc)}) from exc

    update_job(
        job_id,
        status="paused",
        dip_count=int(job.get("dip_count", 0)) + 1,
        last_dip=result,
        dip_phase=None,
        manual_dip_needs_pen_down=True,
        operator_message="Manual dip completed; job remains paused.",
    )
    return {
        "ok": True,
        "job_id": job_id,
        "status": "paused",
        "message": "Manual dip completed; job remains paused.",
        "result": result,
    }


@app.post("/jobs/{job_id}/dip_interval")
def update_job_dip_interval(
    job_id: str,
    payload: dict = Body(...),
    x_plotter_token: Optional[str] = Header(default=None),
):
    check_token(x_plotter_token)
    interval_s = validate_dip_interval(payload.get("dip_interval_s"))

    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        if not job.get("auto_dip_enabled"):
            raise HTTPException(status_code=400, detail="Job does not have automatic dipping configured")
        if job.get("status") not in {"paused", "queued", "queued_for_operator", "waiting_for_operator"}:
            raise HTTPException(status_code=400, detail=f"Job interval cannot be changed while status is {job.get('status')!r}")
        job["dip_interval_s"] = interval_s
        job["operator_message"] = (
            f"Dip interval set to {interval_s:g}s. Existing prepared checkpoints are unchanged; "
            "use Dip Now for this paused plot or rerun for a regenerated schedule."
        )
        save_job_unlocked(job_id)

    return {
        "ok": True,
        "job_id": job_id,
        "dip_interval_s": interval_s,
        "message": jobs[job_id]["operator_message"],
    }


@app.post("/jobs/{job_id}/auto_dip")
def update_job_auto_dip(
    job_id: str,
    payload: dict = Body(default={}),
    x_plotter_token: Optional[str] = Header(default=None),
):
    check_token(x_plotter_token)
    enabled = resolve_auto_dip_flag(payload.get("auto_dip_enabled", payload.get("enabled", False)))
    interval_s = payload.get("dip_interval_s")
    if enabled and interval_s is None:
        interval_s = current_plot_settings().get("dip_interval_s")

    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        configure_job_auto_dip(job, enabled=enabled, dip_interval_s=interval_s)
        save_job_unlocked(job_id)
        return {
            "ok": True,
            "job_id": job_id,
            "status": job.get("status"),
            "auto_dip_enabled": job.get("auto_dip_enabled", False),
            "dip_interval_s": job.get("dip_interval_s"),
            "dip_estimates": layer_dip_estimates(job.get("layers") or []),
            "message": job.get("operator_message"),
        }


@app.post("/jobs/clear")
def clear_jobs(
    payload: dict = Body(default={}),
    x_plotter_token: Optional[str] = Header(default=None),
):
    check_token(x_plotter_token)

    keep_files = bool(payload.get("keep_files", True))
    clear_statuses = set(
        payload.get(
            "statuses",
            ["done", "failed", "cancelled", "interrupted", "paused", "dip_failed"],
        )
    )

    removed = []
    skipped = []

    with jobs_lock:
        for job_id, job in list(jobs.items()):
            status = job.get("status")
            if status in JOB_DELETE_BLOCKED_STATUSES:
                skipped.append({"id": job_id, "status": status})
                continue
            if status not in clear_statuses:
                skipped.append({"id": job_id, "status": status})
                continue

            try:
                removed.append(delete_job_record_unlocked(job_id, keep_files=keep_files))
            except Exception as exc:
                skipped.append({"id": job_id, "status": status, "error": repr(exc)})

    return {"removed": removed, "skipped": skipped, "keep_files": keep_files}


@app.post("/jobs/{job_id}/delete")
def delete_job(
    job_id: str,
    payload: dict = Body(default={}),
    x_plotter_token: Optional[str] = Header(default=None),
):
    check_token(x_plotter_token)
    keep_files = bool(payload.get("keep_files", True))

    with jobs_lock:
        removed = delete_job_record_unlocked(job_id, keep_files=keep_files)

    return {"removed": removed, "keep_files": keep_files}


@app.post("/jobs/{job_id}/rerun")
def rerun_job(
    job_id: str,
    x_plotter_token: Optional[str] = Header(default=None),
):
    """
    Requeue the same uploaded layer files from the beginning.

    Use this if the plotter was not physically ready:
      motors unplugged,
      wrong pen,
      bad origin,
      paper not loaded, etc.
    """
    check_token(x_plotter_token)

    with jobs_lock:
        original = jobs.get(job_id)

    if not original:
        raise HTTPException(status_code=404, detail="Original job not found")

    if original.get("status") in {
        "queued",
        "queued_for_operator",
        "waiting_for_operator",
        "queued_for_resume",
        "running",
        "dipping",
    }:
        raise HTTPException(
            status_code=400,
            detail="Original job is active. Wait until it stops before rerunning.",
        )

    new_job_id = uuid.uuid4().hex[:12]
    new_job_dir = job_dir(new_job_id)
    new_job_dir.mkdir(parents=True, exist_ok=True)

    current_plot_defaults = current_plot_settings()
    rerun_settings = {
        "speed_pendown": current_plot_defaults["speed_pendown"],
        "speed_penup": current_plot_defaults["speed_penup"],
        "pen_pos_down": original.get("pen_pos_down", 35),
        "pen_pos_up": original.get("pen_pos_up", 65),
    }
    auto_dip = bool(original.get("auto_dip_enabled"))
    dip_interval_s = original.get("dip_interval_s")
    well_settings = current_ink_well_settings()
    well_snapshot = None
    rerun_home = None
    if auto_dip and not well_settings.get("installed"):
        raise HTTPException(status_code=409, detail="The ink well must be tested and installed before rerunning this dip job")
    if well_settings.get("installed"):
        try:
            well_snapshot = ink_well_plot_snapshot(well_settings)
            rerun_home = current_home_position()
        except (ValueError, HTTPException) as exc:
            detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
            raise HTTPException(status_code=409, detail=detail) from exc

    new_layers = []

    for layer in original["layers"]:
        idx = layer["index"]

        layer_dir = new_job_dir / f"layer_{idx:02d}"
        layer_dir.mkdir(parents=True, exist_ok=True)

        old_input = Path(layer["input_svg"])
        new_input = layer_dir / "input.svg"
        new_progress = layer_dir / "progress.svg"
        new_digest = layer_dir / "plot_digest.svg"

        if not old_input.exists():
            raise HTTPException(
                status_code=400,
                detail=f"Original layer file missing: {old_input}",
            )

        shutil.copy(old_input, new_input)

        new_layer = {
            "index": idx,
            "name": layer["name"],
            "original_filename": layer.get(
                "original_filename",
                f"layer_{idx:02d}.svg",
            ),
            "input_svg": str(new_input),
            "progress_svg": str(new_progress),
            "svg_metrics": validate_svg_file(new_input),
        }
        if well_snapshot is not None:
            try:
                analysis_origin = plot_origin_for_layer_metrics(new_layer["svg_metrics"]) or rerun_home
                analysis = analyse_layer_for_ink_well(
                    new_input,
                    new_digest,
                    job_settings=rerun_settings,
                    home=analysis_origin,
                    well=well_snapshot,
                    dip_interval_s=dip_interval_s if auto_dip else None,
                )
                new_layer["plot_digest_svg"] = str(new_digest)
                new_layer["ink_analysis"] = analysis
                if auto_dip:
                    prepare_auto_dip_layer(new_layer, analysis)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        new_layers.append(new_layer)

    log_path = LOGS_DIR / f"{new_job_id}.log"

    new_job = {
        "id": new_job_id,
        "status": "queued",
        "created_at": now(),
        "layer_count": len(new_layers),
        "layers": new_layers,
        "log_path": str(log_path),
        # Reruns use the current Speeds-panel values so an operator can tune
        # plot motion or pen timing before repeating the same artwork.
        "speed_pendown": current_plot_defaults["speed_pendown"],
        "speed_penup": current_plot_defaults["speed_penup"],
        "pen_delay_down": current_plot_defaults["pen_delay_down"],
        "pen_delay_up": current_plot_defaults["pen_delay_up"],
        "pen_rate_raise": current_plot_defaults["pen_rate_raise"],
        "pen_pos_down": original.get("pen_pos_down", 35),
        "pen_pos_up": original.get("pen_pos_up", 65),
        "ink_well": well_snapshot,
        "auto_dip_enabled": auto_dip,
        "dip_interval_s": dip_interval_s if auto_dip else None,
        "dip_count": 0,
        "dip_failure": None,
        "current_layer": None,
        "current_layer_name": None,
        "last_completed_layer": None,
        "operator_message": None,
        "rerun_of": job_id,
        "plot_start_position": rerun_home,
    }
    try:
        apply_paper_alignment_to_job(new_job)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    with jobs_lock:
        jobs[new_job_id] = new_job
        save_job_unlocked(new_job_id)

    job_queue.put(new_job_id)

    return {
        "job_id": new_job_id,
        "status": "queued",
        "rerun_of": job_id,
        "layer_count": len(new_layers),
        "status_url": f"/jobs/{new_job_id}",
    }


@app.get("/plotter/state")
def plotter_state(
    x_plotter_token: Optional[str] = Header(default=None),
):
    check_token(x_plotter_token)

    with jobs_lock:
        latest_jobs = [
            {
                "id": job.get("id"),
                "status": job.get("status"),
                "current_layer": job.get("current_layer"),
                "current_layer_name": job.get("current_layer_name"),
                "operator_message": job.get("operator_message"),
                "plot_footprint": job_plot_footprint(job),
                "plot_origin": job.get("plot_origin"),
                "paper": job.get("paper"),
            }
            for job in sorted(jobs.values(), key=lambda item: item.get("created_at") or 0, reverse=True)[:5]
        ]

    return {
        "server": {
            "ok": True,
            "queue_size": job_queue.qsize(),
            "operator_prompt": dict(operator_prompt),
        },
        "hardware": read_hardware_state(),
        "jobs": latest_jobs,
    }


@app.post("/plotter/pen")
def plotter_pen(
    request: Request,
    payload: dict = Body(...),
    x_plotter_token: Optional[str] = Header(default=None),
):
    require_localhost(request)
    check_token(x_plotter_token)

    position = str(payload.get("position", "")).lower()
    if position not in {"up", "down"}:
        raise HTTPException(status_code=400, detail="position must be 'up' or 'down'")

    saved_pen_settings = current_pen_settings()
    if "pen_pos_down" in payload:
        payload["pen_pos_down"] = validate_pen_position(payload["pen_pos_down"], "pen_pos_down")
    if "pen_pos_up" in payload:
        payload["pen_pos_up"] = validate_pen_position(payload["pen_pos_up"], "pen_pos_up")
    up_pos = int(payload.get("pen_pos_up", saved_pen_settings["pen_pos_up"]))
    down_pos = int(payload.get("pen_pos_down", saved_pen_settings["pen_pos_down"]))
    plot_defaults = current_plot_settings()

    require_hardware_idle()
    try:
        with hardware_lock:
            result = run_pen_manual_direct_first(
                raised=position == "up",
                up_pos=up_pos,
                down_pos=down_pos,
                raise_rate=plot_defaults.get("pen_rate_raise", DEFAULT_PEN_RATE_RAISE),
                lower_rate=DEFAULT_PEN_RATE_LOWER,
                delay_up_ms=plot_defaults.get("pen_delay_up", DEFAULT_PEN_DELAY_UP),
                delay_down_ms=plot_defaults.get("pen_delay_down", DEFAULT_PEN_DELAY_DOWN),
            )
    except serial.SerialException as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=repr(exc)) from exc
    if not result.get("ok"):
        raise HTTPException(status_code=500, detail=result)

    if result["ok"] and ("pen_pos_down" in payload or "pen_pos_up" in payload):
        with pen_settings_lock:
            if "pen_pos_down" in payload:
                pen_settings["pen_pos_down"] = int(payload["pen_pos_down"])
            if "pen_pos_up" in payload:
                pen_settings["pen_pos_up"] = int(payload["pen_pos_up"])
            save_pen_settings_unlocked()
            result["pen_settings"] = dict(pen_settings)
    else:
        result["pen_settings"] = saved_pen_settings

    return result


@app.post("/plotter/pen_settings")
def plotter_pen_settings(
    request: Request,
    payload: dict = Body(...),
    x_plotter_token: Optional[str] = Header(default=None),
):
    """Save pen positions without moving the servo."""
    require_localhost(request)
    check_token(x_plotter_token)

    with pen_settings_lock:
        if "pen_pos_down" in payload:
            pen_settings["pen_pos_down"] = validate_pen_position(payload["pen_pos_down"], "pen_pos_down")
        if "pen_pos_up" in payload:
            pen_settings["pen_pos_up"] = validate_pen_position(payload["pen_pos_up"], "pen_pos_up")
        save_pen_settings_unlocked()
        return {"ok": True, "pen_settings": dict(pen_settings)}


@app.post("/plotter/plot_settings")
def plotter_plot_settings(
    request: Request,
    payload: dict = Body(...),
    x_plotter_token: Optional[str] = Header(default=None),
):
    require_localhost(request)
    check_token(x_plotter_token)

    with plot_settings_lock:
        if "speed_pendown" in payload:
            plot_settings["speed_pendown"] = validate_speed_setting(payload["speed_pendown"], "speed_pendown")
        if "speed_penup" in payload:
            plot_settings["speed_penup"] = validate_speed_setting(payload["speed_penup"], "speed_penup")
        if "pen_delay_down" in payload:
            plot_settings["pen_delay_down"] = validate_pen_delay_down(payload["pen_delay_down"])
        if "pen_delay_up" in payload:
            plot_settings["pen_delay_up"] = validate_pen_delay_up(payload["pen_delay_up"])
        if "pen_rate_raise" in payload:
            plot_settings["pen_rate_raise"] = validate_pen_rate_raise(payload["pen_rate_raise"])
        if "auto_dip_enabled" in payload:
            plot_settings["auto_dip_enabled"] = resolve_auto_dip_flag(payload["auto_dip_enabled"])
        if "dip_interval_s" in payload:
            plot_settings["dip_interval_s"] = validate_dip_interval(payload["dip_interval_s"])
        save_plot_settings_unlocked()
        return {"ok": True, "plot_settings": dict(plot_settings)}


@app.get("/plotter/paper")
def plotter_paper(
    request: Request,
    x_plotter_token: Optional[str] = Header(default=None),
):
    require_localhost(request)
    check_token(x_plotter_token)
    return {"paper": current_paper_settings(), "paper_sizes": PAPER_SIZES_MM}


@app.post("/plotter/paper")
def plotter_paper_update(
    request: Request,
    payload: dict = Body(...),
    x_plotter_token: Optional[str] = Header(default=None),
):
    require_localhost(request)
    check_token(x_plotter_token)

    editable = {"enabled", "size", "orientation", "top_right"}
    unknown = set(payload) - editable - {"top_right_from_current"}
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unknown paper fields: {', '.join(sorted(unknown))}")

    with paper_settings_lock:
        candidate = current_paper_settings()
        if payload.get("top_right_from_current"):
            candidate["top_right"] = current_software_position()
        for key in editable:
            if key in payload:
                candidate[key] = payload[key]
        try:
            validated = validate_paper_settings(candidate)
        except (ValueError, HTTPException) as exc:
            detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
            raise HTTPException(status_code=400, detail=detail) from exc
        paper_settings.clear()
        paper_settings.update(validated)
        save_paper_settings_unlocked()
        return {"ok": True, "paper": current_paper_settings(), "paper_sizes": PAPER_SIZES_MM}


@app.get("/plotter/ink_well")
def plotter_ink_well(
    request: Request,
    x_plotter_token: Optional[str] = Header(default=None),
):
    require_localhost(request)
    check_token(x_plotter_token)
    return {"ink_well": current_ink_well_settings()}


@app.post("/plotter/ink_well")
def plotter_ink_well_update(
    request: Request,
    payload: dict = Body(...),
    x_plotter_token: Optional[str] = Header(default=None),
):
    require_localhost(request)
    check_token(x_plotter_token)

    editable = {
        "centre",
        "radius_mm",
        "clearance_pos",
        "dip_pos",
        "dwell_ms",
        "drip_dwell_ms",
        "travel_speed_mm_s",
        "dip_circle_count",
        "dip_circle_diameter_mm",
        "installed",
    }
    unknown = set(payload) - editable - {"centre_from_current"}
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unknown ink well fields: {', '.join(sorted(unknown))}")

    with ink_well_settings_lock:
        candidate = current_ink_well_settings()
        if payload.get("centre_from_current"):
            candidate["centre"] = current_software_position()
            candidate["calibration_id"] = current_position_calibration_id()
        for key in editable:
            if key in payload:
                candidate[key] = payload[key]

        calibration_keys = {
            "centre",
            "radius_mm",
            "clearance_pos",
            "dip_pos",
            "dwell_ms",
            "drip_dwell_ms",
            "dip_circle_count",
            "dip_circle_diameter_mm",
            "calibration_id",
        }
        calibration_changed = bool(payload.get("centre_from_current")) or any(
            candidate.get(key) != ink_well_settings.get(key)
            for key in calibration_keys
        )
        if calibration_changed:
            candidate["test_passed"] = False
            candidate["tested_at"] = None
            candidate["installed"] = False

        try:
            validate_ink_well_settings(
                candidate,
                require_ready=bool(candidate.get("installed")),
            )
            if candidate.get("installed"):
                require_ink_well_current_calibration(candidate)
        except (ValueError, HTTPException) as exc:
            detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
            raise HTTPException(status_code=400, detail=detail) from exc

        ink_well_settings.clear()
        ink_well_settings.update(candidate)
        save_ink_well_settings_unlocked()
        return {
            "ok": True,
            "ink_well": current_ink_well_settings(),
            "test_required": not bool(ink_well_settings.get("test_passed")),
        }


@app.post("/plotter/ink_well/test")
def plotter_ink_well_test(
    request: Request,
    x_plotter_token: Optional[str] = Header(default=None),
):
    require_localhost(request)
    check_token(x_plotter_token)
    require_hardware_idle()
    require_software_position()

    settings = current_ink_well_settings()
    try:
        validate_ink_well_settings(settings, require_ready=False)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    missing = [
        key
        for key in ("centre", "radius_mm", "clearance_pos", "dip_pos")
        if settings.get(key) is None
    ]
    if missing:
        raise HTTPException(status_code=400, detail=f"Ink well setup is incomplete: {', '.join(missing)}")
    try:
        require_ink_well_current_calibration(settings)
    except ValueError as exc:
        raise HTTPException(
            status_code=409,
            detail=f"{exc} Run the test again after re-setting the centre.",
        ) from exc

    test_job = {
        "id": "__ink_well_test__",
        "auto_dip_enabled": True,
        "ink_well": ink_well_plot_snapshot({**settings, "test_passed": True}),
    }
    test_log_path = LOGS_DIR / "ink-well-test.log"
    try:
        with test_log_path.open("a", encoding="utf-8", errors="replace") as log:
            log.write(f"\nInk well test at {now():.3f}\n")
            result = execute_dip_cycle(test_job, log)
    except Exception as exc:
        with test_log_path.open("a", encoding="utf-8", errors="replace") as log:
            attempt_dip_clearance_raise(test_job, log)
        raise HTTPException(
            status_code=500,
            detail={"message": "Ink well test failed", "error": repr(exc)},
        ) from exc

    with ink_well_settings_lock:
        ink_well_settings["test_passed"] = False
        ink_well_settings["tested_at"] = now()
        ink_well_settings["installed"] = False
        save_ink_well_settings_unlocked()
        saved = current_ink_well_settings()
    return {
        "ok": True,
        "message": "Ink well test cycle finished. Confirm it passed only if the nib touched fluid cleanly.",
        "result": result,
        "ink_well": saved,
        "log_path": str(test_log_path),
    }


@app.post("/plotter/ink_well/confirm_test")
def plotter_ink_well_confirm_test(
    request: Request,
    x_plotter_token: Optional[str] = Header(default=None),
):
    require_localhost(request)
    check_token(x_plotter_token)

    with ink_well_settings_lock:
        candidate = current_ink_well_settings()
        if candidate.get("tested_at") is None:
            raise HTTPException(status_code=400, detail="Run the ink well test cycle before confirming it passed")
        candidate["test_passed"] = True
        candidate["installed"] = True
        try:
            validate_ink_well_settings(candidate, require_ready=True)
            require_ink_well_current_calibration(candidate)
        except (ValueError, HTTPException) as exc:
            detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
            raise HTTPException(status_code=400, detail=detail) from exc
        ink_well_settings.clear()
        ink_well_settings.update(candidate)
        save_ink_well_settings_unlocked()
        saved = current_ink_well_settings()
    return {
        "ok": True,
        "message": "Ink well test confirmed and ink well check enabled.",
        "ink_well": saved,
    }


@app.post("/plotter/motors")
def plotter_motors(
    request: Request,
    payload: dict = Body(...),
    x_plotter_token: Optional[str] = Header(default=None),
):
    require_localhost(request)
    check_token(x_plotter_token)

    enabled = bool(payload.get("enabled"))
    with hardware_lock:
        try:
            with serial.Serial(PLOTTER_PORT, timeout=1) as port:
                if enabled:
                    motor_1, motor_2 = read_motor_resolution(port)
                    if (motor_1, motor_2) == (1, 1):
                        return {
                            "ok": True,
                            "message": "Motors already enabled; position calibration preserved.",
                            "position_invalidated": False,
                        }
                response = raw_command(port, "EM,1,1\r" if enabled else "EM,0,0\r")
        except serial.SerialException as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    if not response.startswith("OK"):
        raise HTTPException(status_code=500, detail=f"Unexpected motor response: {response!r}")

    result = {
        "ok": True,
        "method": "direct_ebb",
        "response": response,
    }

    invalidate_motor_resolution_cache()
    with hardware_state_lock:
        cached_hardware_state.pop("motor_enable_raw", None)
    if enabled:
        result["position_invalidated"] = False
        result["message"] = "Motors enabled; position calibration preserved."
    else:
        with position_lock:
            invalidate_position_reference_unlocked()
        result["position_invalidated"] = True
        result["message"] = "Motors disabled; recalibrate after re-enabling before moving."
    return result


@app.post("/plotter/home/set")
def plotter_set_home(
    request: Request,
    x_plotter_token: Optional[str] = Header(default=None),
):
    require_localhost(request)
    check_token(x_plotter_token)
    require_hardware_idle()

    with hardware_lock:
        with serial.Serial(PLOTTER_PORT, timeout=1) as port:
            require_enabled_high_resolution_motors(port)
            _axis_1, _axis_2, raw_current = read_step_position(port)
            current = current_position_estimate(raw_current)
            if current is None:
                raise HTTPException(status_code=500, detail="Could not calculate current hardware position")
            port.write(b"CS\r")
            response = port.readline().decode("ascii", errors="replace").strip()

    if not response.startswith("OK"):
        raise HTTPException(status_code=500, detail=f"Unexpected CS response: {response!r}")

    with position_lock:
        # CS makes the current physical point the controller origin. Preserve
        # its calibrated bed coordinate by making raw zero map to that point.
        position_offset["x_mm"] = current["x_mm"]
        position_offset["y_mm"] = current["y_mm"]
        set_current_position_unlocked(current["x_mm"], current["y_mm"])
        set_home_position_unlocked(current["x_mm"], current["y_mm"])
        save_position_offset_unlocked()

    return {
        "ok": True,
        "message": "Current calibrated position is now home",
        "response": response,
        "position_offset": dict(position_offset),
        "home_position": current,
    }


@app.post("/plotter/position/set")
def plotter_set_position(
    request: Request,
    payload: dict = Body(...),
    x_plotter_token: Optional[str] = Header(default=None),
):
    require_localhost(request)
    check_token(x_plotter_token)
    require_hardware_idle()

    if "x_mm" not in payload and "y_mm" not in payload:
        raise HTTPException(status_code=400, detail="Provide x_mm, y_mm, or both")

    reset_home = bool(payload.get("reset_home", False))

    with hardware_lock:
        try:
            with serial.Serial(PLOTTER_PORT, timeout=1) as port:
                motor_1, motor_2 = read_motor_resolution(port)
                if reset_home and (motor_1, motor_2) != (1, 1):
                    enable_response = raw_command(port, "EM,1,1\r")
                    if not enable_response.startswith("OK"):
                        raise HTTPException(status_code=500, detail=f"Could not enable motors: {enable_response!r}")
                _axis_1, _axis_2, raw_current = read_step_position(port)
                bed_current = raw_xy_to_bed_xy(raw_current)
                if bed_current is None:
                    raise HTTPException(status_code=500, detail="Could not transform current position")
                with position_lock:
                    current = current_position_estimate(raw_current) or {"x_mm": 0.0, "y_mm": 0.0}
                    next_x = float(payload["x_mm"]) if "x_mm" in payload else current["x_mm"]
                    next_y = float(payload["y_mm"]) if "y_mm" in payload else current["y_mm"]
                    next_x, next_y = validate_bed_target(next_x, next_y)
                calibration_response = None
                if reset_home:
                    port.write(b"CS\r")
                    calibration_response = port.readline().decode("ascii", errors="replace").strip()
        except serial.SerialException as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=500, detail=f"Could not read current position: {exc}") from exc

    with position_lock:
        renew_position_calibration_unlocked()
        if reset_home:
            if not calibration_response or not calibration_response.startswith("OK"):
                raise HTTPException(status_code=500, detail=f"Unexpected CS response: {calibration_response!r}")
            position_offset["x_mm"] = next_x
            position_offset["y_mm"] = next_y
            set_home_position_unlocked(next_x, next_y)
        else:
            if "x_mm" in payload:
                position_offset["x_mm"] = next_x - bed_current["x_mm"]
            if "y_mm" in payload:
                position_offset["y_mm"] = next_y - bed_current["y_mm"]
        set_current_position_unlocked(next_x, next_y)
        save_position_offset_unlocked()
        offset = dict(position_offset)
        current = dict(position_current)

    return {
        "ok": True,
        "raw_position": raw_current,
        "bed_position_unoffset": bed_current,
        "position_offset": offset,
        "position_estimate": current,
        "home_position": dict(home_position) if home_position is not None else None,
        "home_reset": reset_home,
        "position_source": "software",
    }


@app.post("/plotter/position/calibration")
def plotter_position_calibration_toggle(
    request: Request,
    payload: dict = Body(...),
    x_plotter_token: Optional[str] = Header(default=None),
):
    require_localhost(request)
    check_token(x_plotter_token)
    enabled = bool(payload.get("enabled"))
    acknowledged = bool(payload.get("acknowledge_unsafe"))
    if not enabled and not acknowledged:
        raise HTTPException(
            status_code=400,
            detail="Disabling plot-bed calibration may make absolute moves unsafe; acknowledge_unsafe is required.",
        )
    global position_calibration_enabled
    with position_lock:
        position_calibration_enabled = enabled
        save_position_offset_unlocked()
        return {
            "ok": True,
            "calibration_enabled": position_calibration_enabled,
            "position_calibration_id": position_calibration_id,
            "position_estimate": dict(position_current) if position_current is not None else None,
            "home_position": dict(home_position) if home_position is not None else None,
            "message": "Plot-bed calibration enabled." if enabled else "Plot-bed calibration disabled; saved calibration is retained but absolute moves may be unsafe.",
        }


@app.post("/plotter/home/return")
def plotter_return_home(
    request: Request,
    x_plotter_token: Optional[str] = Header(default=None),
):
    require_localhost(request)
    check_token(x_plotter_token)

    return checked_return_home()


@app.post("/plotter/move")
def plotter_move(
    request: Request,
    payload: dict = Body(...),
    x_plotter_token: Optional[str] = Header(default=None),
):
    require_localhost(request)
    check_token(x_plotter_token)
    require_hardware_idle()
    require_software_position()

    x_value = payload.get("x_mm")
    y_value = payload.get("y_mm")
    if x_value is None and y_value is None:
        raise HTTPException(status_code=400, detail="Provide x_mm, y_mm, or both")

    absolute = bool(payload.get("absolute", False))
    x_delta = float(x_value) if x_value is not None else 0.0
    y_delta = float(y_value) if y_value is not None else 0.0

    current = current_software_position()
    target_x = float(x_value) if absolute and x_value is not None else current["x_mm"] + x_delta
    target_y = float(y_value) if absolute and y_value is not None else current["y_mm"] + y_delta
    target_x, target_y = validate_bed_target(target_x, target_y)

    results = []
    with hardware_lock:
        with serial.Serial(PLOTTER_PORT, timeout=1) as port:
            require_enabled_high_resolution_motors(port)
            if absolute:
                steps, _ = serial_query(port, "QS\r")
                try:
                    axis_1_text, axis_2_text = steps.split(",", 1)
                    raw_current = steps_to_xy_mm(int(axis_1_text), int(axis_2_text))
                    current = current_position_estimate(raw_current)
                    if current is None:
                        raise ValueError("No current position available")
                except ValueError as exc:
                    raise HTTPException(status_code=500, detail=f"Could not parse current position: {steps!r}") from exc

                x_delta = (float(x_value) - current["x_mm"]) if x_value is not None else 0.0
                y_delta = (float(y_value) - current["y_mm"]) if y_value is not None else 0.0

        raw_delta = bed_delta_to_raw_delta(x_delta, y_delta)

        for manual_cmd, delta in (("walk_mmx", raw_delta["x_mm"]), ("walk_mmy", raw_delta["y_mm"])):
            if abs(delta) < 0.001:
                continue
            cmd = axicli_cmd() + [
                "--mode",
                "manual",
                "--manual_cmd",
                manual_cmd,
                "--dist",
                f"{delta:.4f}",
                "--port",
                PLOTTER_PORT,
            ]
            proc = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            result = {
                "ok": proc.returncode == 0,
                "returncode": proc.returncode,
                "command": cmd,
                "output": proc.stdout,
            }
            results.append(result)
            if proc.returncode != 0:
                raise HTTPException(status_code=500, detail={"failed": result, "results": results})

        with position_lock:
            current = current_position_estimate(None) or {"x_mm": 0.0, "y_mm": 0.0}
            set_current_position_unlocked(current["x_mm"] + x_delta, current["y_mm"] + y_delta)
            save_position_offset_unlocked()
            current = dict(position_current)

    return {
        "ok": True,
        "absolute": absolute,
        "x_delta_mm": x_delta,
        "y_delta_mm": y_delta,
        "raw_delta_mm": raw_delta,
        "position_estimate": current,
        "results": results,
    }


@app.post("/plotter/jog")
def plotter_jog(
    request: Request,
    payload: dict = Body(...),
    x_plotter_token: Optional[str] = Header(default=None),
):
    require_localhost(request)
    check_token(x_plotter_token)
    require_hardware_idle()
    require_software_position()

    x_mm = float(payload.get("x_mm", 0.0) or 0.0)
    y_mm = float(payload.get("y_mm", 0.0) or 0.0)
    speed_mm_s = float(payload.get("speed_mm_s", 25.0) or 25.0)

    if abs(x_mm) < 0.001 and abs(y_mm) < 0.001:
        raise HTTPException(status_code=400, detail="Jog distance is zero")
    if speed_mm_s <= 0:
        raise HTTPException(status_code=400, detail="speed_mm_s must be positive")
    if speed_mm_s > SAFE_MANUAL_MAX_XY_SPEED_MM_S:
        raise HTTPException(
            status_code=400,
            detail=f"speed_mm_s must be <= {SAFE_MANUAL_MAX_XY_SPEED_MM_S:g}",
        )
    if max(abs(x_mm), abs(y_mm)) > 50:
        raise HTTPException(status_code=400, detail="Jog distance must be <= 50 mm per command")

    current = current_software_position()
    target_x, target_y = validate_bed_target(current["x_mm"] + x_mm, current["y_mm"] + y_mm)

    raw_delta = bed_delta_to_raw_delta(x_mm, y_mm)
    axis_1, axis_2 = xy_mm_to_steps(raw_delta["x_mm"], raw_delta["y_mm"])
    distance = (x_mm * x_mm + y_mm * y_mm) ** 0.5
    duration_ms = max(40, int(round(distance / speed_mm_s * 1000)))
    mark_manual_hardware_priority(duration_ms / 1000.0 + MANUAL_HARDWARE_PRIORITY_GRACE_S)

    with hardware_lock:
        try:
            with serial.Serial(PLOTTER_PORT, timeout=2) as port:
                require_cached_high_resolution_motors(port)
                move_response = raw_command(port, f"SM,{duration_ms},{axis_1},{axis_2}\r")
                wait_for_motion_idle(port, max(2.0, duration_ms / 1000.0 + 1.0))
        except serial.SerialException as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        except TimeoutError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    if not move_response.startswith("OK"):
        raise HTTPException(
            status_code=500,
            detail={
                "move_response": move_response,
            },
        )

    with position_lock:
        set_current_position_unlocked(target_x, target_y)
        save_position_offset_unlocked()
        current = dict(position_current)

    return {
        "ok": True,
        "x_mm": x_mm,
        "y_mm": y_mm,
        "target": {"x_mm": target_x, "y_mm": target_y},
        "raw_delta_mm": raw_delta,
        "position_estimate": current,
        "axis_1_steps": axis_1,
        "axis_2_steps": axis_2,
        "duration_ms": duration_ms,
    }


@app.post("/plotter/move_to")
def plotter_move_to(
    request: Request,
    payload: dict = Body(...),
    x_plotter_token: Optional[str] = Header(default=None),
):
    require_localhost(request)
    check_token(x_plotter_token)
    require_hardware_idle()
    require_software_position()

    if "x_mm" not in payload or "y_mm" not in payload:
        raise HTTPException(status_code=400, detail="Provide both x_mm and y_mm")
    target_x, target_y = validate_bed_target(payload["x_mm"], payload["y_mm"])
    speed_mm_s = float(payload.get("speed_mm_s", 60.0) or 60.0)

    if speed_mm_s <= 0:
        raise HTTPException(status_code=400, detail="speed_mm_s must be positive")
    if speed_mm_s > SAFE_MANUAL_MAX_XY_SPEED_MM_S:
        raise HTTPException(
            status_code=400,
            detail=f"speed_mm_s must be <= {SAFE_MANUAL_MAX_XY_SPEED_MM_S:g}",
        )
    with hardware_lock:
        try:
            with serial.Serial(PLOTTER_PORT, timeout=2) as port:
                require_enabled_high_resolution_motors(port)
                _axis_1_now, _axis_2_now, raw_current = read_step_position(port)
                current = current_position_estimate(raw_current)
                if current is None:
                    raise ValueError("No current position available")
                delta_x = target_x - current["x_mm"]
                delta_y = target_y - current["y_mm"]
                raw_delta = bed_delta_to_raw_delta(delta_x, delta_y)
                axis_1_delta, axis_2_delta = xy_mm_to_steps(raw_delta["x_mm"], raw_delta["y_mm"])
                distance = (delta_x * delta_x + delta_y * delta_y) ** 0.5
                duration_ms = max(40, int(round(distance / speed_mm_s * 1000)))

                if abs(delta_x) < 0.001 and abs(delta_y) < 0.001:
                    return {
                        "ok": True,
                        "message": "Already at target",
                        "target": {"x_mm": target_x, "y_mm": target_y},
                        "current": current,
                    }

                move_response = raw_command(port, f"SM,{duration_ms},{axis_1_delta},{axis_2_delta}\r")
                wait_for_motion_idle(port, max(2.0, duration_ms / 1000.0 + 1.0))
                _axis_1_after, _axis_2_after, raw_after = read_step_position(port)
        except serial.SerialException as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=500, detail=f"Could not read current position: {exc}") from exc
        except TimeoutError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    if not move_response.startswith("OK"):
        raise HTTPException(
            status_code=500,
            detail={
                "move_response": move_response,
            },
        )

    with position_lock:
        actual = current_position_estimate(raw_after)
        if actual is None:
            raise HTTPException(status_code=500, detail="Could not calculate position after movement")
        set_current_position_unlocked(actual["x_mm"], actual["y_mm"])
        save_position_offset_unlocked()
        end = dict(position_current)

    return {
        "ok": True,
        "target": {"x_mm": target_x, "y_mm": target_y},
        "start": current,
        "end": end,
        "delta": {"x_mm": delta_x, "y_mm": delta_y},
        "raw_delta_mm": raw_delta,
        "axis_1_steps": axis_1_delta,
        "axis_2_steps": axis_2_delta,
        "duration_ms": duration_ms,
    }


@app.get("/operator/next")
def operator_next(request: Request):
    require_localhost(request)

    with operator_lock:
        return dict(operator_prompt)


@app.post("/operator/continue")
def operator_continue(request: Request, payload: dict = Body(default={})):
    require_localhost(request)

    with operator_lock:
        if not operator_prompt["active"]:
            return {"ok": True, "message": "No operator prompt is active."}
        prompt = dict(operator_prompt)

    if prompt.get("action") == "start":
        plot_defaults = current_plot_settings()
        if "auto_dip_enabled" in payload or "enabled" in payload:
            auto_dip_enabled = resolve_auto_dip_flag(payload.get("auto_dip_enabled", payload.get("enabled")))
        else:
            well_settings = current_ink_well_settings()
            auto_dip_enabled = bool(plot_defaults.get("auto_dip_enabled")) and bool(well_settings.get("installed"))
        dip_interval_s = payload.get("dip_interval_s")
        if auto_dip_enabled and dip_interval_s is None:
            dip_interval_s = plot_defaults.get("dip_interval_s")

        with jobs_lock:
            job = jobs.get(prompt.get("job_id"))
            if job is None:
                raise HTTPException(status_code=404, detail="Job not found")
            configure_job_auto_dip(job, enabled=auto_dip_enabled, dip_interval_s=dip_interval_s)
            save_job_unlocked(job["id"])

    operator_event.set()

    return {"ok": True, "message": "Continuing.", "operator_prompt": prompt}
