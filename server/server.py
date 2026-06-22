from fastapi import FastAPI, UploadFile, File, Form, Header, HTTPException, Request, Body
from fastapi.responses import FileResponse
from pathlib import Path
from typing import Optional, List
import os
import re
import time
import uuid
import shutil
import queue
import threading
import subprocess
import signal
import json
import math
import xml.etree.ElementTree as ET
import serial

APP_NAME = "ArtStation Layer Plotter Server"

BASE_DIR = Path.home() / "plotter"
VERSION_PATH = BASE_DIR / "VERSION"
JOBS_DIR = BASE_DIR / "jobs"
LOGS_DIR = BASE_DIR / "logs"
STATIC_DIR = BASE_DIR / "server" / "static"
POSITION_PATH = BASE_DIR / "plotter_position.json"
PEN_SETTINGS_PATH = BASE_DIR / "plotter_pen_settings.json"
PLOT_SETTINGS_PATH = BASE_DIR / "plotter_plot_settings.json"

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
position_lock = threading.RLock()
position_offset = {"x_mm": 0.0, "y_mm": 0.0}
position_current: dict | None = None
home_position: dict | None = None
pen_settings_lock = threading.RLock()
pen_settings = {"pen_pos_up": 65, "pen_pos_down": 35}
plot_settings_lock = threading.RLock()
plot_settings = {
    "speed_pendown": 15,
    "speed_penup": 40,
    "pen_delay_down": 0,
    "pen_delay_up": 0,
    "pen_rate_raise": 75,
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
}

RUNNING_STATUSES = {
    "running",
    "queued_for_resume",
}


_LENGTH_RE = re.compile(r"^\s*([-+]?[0-9]*\.?[0-9]+)\s*([a-zA-Z]*)\s*$")
_UNIT_TO_MM = {
    "": 1.0,
    "mm": 1.0,
    "cm": 10.0,
    "in": 25.4,
    "pt": 25.4 / 72.0,
    "pc": 25.4 / 6.0,
    "px": 25.4 / 96.0,
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


def validate_svg_text(
    svg_text: str,
    *,
    max_width_mm: float,
    max_height_mm: float,
) -> dict:
    try:
        root = ET.fromstring(svg_text)
    except ET.ParseError as exc:
        raise ValueError(f"Invalid SVG XML: {exc}") from exc

    if _local_name(root.tag) != "svg":
        raise ValueError("Uploaded file is not an SVG document")

    view_box = _parse_view_box(root.attrib.get("viewBox", ""))
    width_mm = _length_to_mm(root.attrib.get("width"), fallback=view_box[2] if view_box else None)
    height_mm = _length_to_mm(root.attrib.get("height"), fallback=view_box[3] if view_box else None)

    if width_mm is None or height_mm is None or width_mm <= 0 or height_mm <= 0:
        raise ValueError("SVG must define positive width and height or a usable viewBox")

    if width_mm > max_width_mm or height_mm > max_height_mm:
        raise ValueError(
            f"SVG dimensions {width_mm:.3f}x{height_mm:.3f}mm exceeds plotter bounds "
            f"{max_width_mm:.3f}x{max_height_mm:.3f}mm"
        )

    return {"width_mm": round(width_mm, 4), "height_mm": round(height_mm, 4)}


def validate_svg_file(path: Path) -> dict:
    return validate_svg_text(
        path.read_text(encoding="utf-8", errors="replace"),
        max_width_mm=MAX_PLOTTER_WIDTH_MM,
        max_height_mm=MAX_PLOTTER_HEIGHT_MM,
    )


def _length_to_mm(value: str | None, *, fallback: float | None = None) -> float | None:
    if value is None or value == "":
        return fallback
    match = _LENGTH_RE.match(value)
    if not match:
        raise ValueError(f"Unsupported SVG length: {value!r}")
    number = float(match.group(1))
    unit = match.group(2).lower()
    if unit not in _UNIT_TO_MM:
        raise ValueError(f"Unsupported SVG length unit: {unit!r}")
    return number * _UNIT_TO_MM[unit]


def _parse_view_box(value: str) -> tuple[float, float, float, float] | None:
    parts = value.replace(",", " ").split()
    if len(parts) != 4:
        return None
    try:
        x, y, width, height = (float(part) for part in parts)
    except ValueError:
        return None
    return x, y, width, height


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


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
THEORETICAL_MAX_XY_SPEED_MM_S = float(os.environ.get("PLOTTER_MAX_XY_SPEED_MM_S", "280"))
SAFE_MANUAL_MAX_XY_SPEED_MM_S = float(os.environ.get("PLOTTER_SAFE_MANUAL_MAX_XY_SPEED_MM_S", "200"))
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
    global position_offset, position_current, home_position
    if not POSITION_PATH.exists():
        return
    try:
        data = json.loads(POSITION_PATH.read_text(encoding="utf-8"))
        if data.get("state_version") != POSITION_STATE_VERSION:
            print("Ignoring legacy position state; recalibration is required", flush=True)
            return
        position_offset = {
            "x_mm": float(data.get("x_mm", 0.0)),
            "y_mm": float(data.get("y_mm", 0.0)),
        }
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
        elif position_current is not None:
            # Older state files had no distinct home. The most recently
            # calibrated/current point is the safest migration target.
            home_position = dict(position_current)
    except Exception as exc:
        print(f"Could not load {POSITION_PATH}: {exc}", flush=True)


def save_position_offset_unlocked() -> None:
    data = {
        "state_version": POSITION_STATE_VERSION,
        "x_mm": position_offset["x_mm"],
        "y_mm": position_offset["y_mm"],
    }
    if position_current is not None:
        data["current_position"] = dict(position_current)
    if home_position is not None:
        data["home_position"] = dict(home_position)
    POSITION_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def load_pen_settings() -> None:
    with pen_settings_lock:
        pen_settings["pen_pos_up"] = DEFAULT_PEN_POS_UP
        pen_settings["pen_pos_down"] = DEFAULT_PEN_POS_DOWN
        if not PEN_SETTINGS_PATH.exists():
            return
        try:
            data = json.loads(PEN_SETTINGS_PATH.read_text(encoding="utf-8"))
            pen_settings["pen_pos_up"] = int(data.get("pen_pos_up", pen_settings["pen_pos_up"]))
            pen_settings["pen_pos_down"] = int(data.get("pen_pos_down", pen_settings["pen_pos_down"]))
        except Exception as exc:
            print(f"Could not load {PEN_SETTINGS_PATH}: {exc}", flush=True)


def save_pen_settings_unlocked() -> None:
    PEN_SETTINGS_PATH.write_text(json.dumps(pen_settings, indent=2), encoding="utf-8")


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
        if not PLOT_SETTINGS_PATH.exists():
            return
        try:
            data = json.loads(PLOT_SETTINGS_PATH.read_text(encoding="utf-8"))
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
        except Exception as exc:
            print(f"Could not load {PLOT_SETTINGS_PATH}: {exc}", flush=True)


def save_plot_settings_unlocked() -> None:
    PLOT_SETTINGS_PATH.write_text(json.dumps(plot_settings, indent=2), encoding="utf-8")


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


def validate_speed_setting(value: int, name: str) -> int:
    value = int(value)
    if not 1 <= value <= 100:
        raise HTTPException(status_code=400, detail=f"{name} must be between 1 and 100")
    return value


def validate_pen_position(value: int, name: str) -> int:
    value = int(value)
    if not 0 <= value <= 100:
        raise HTTPException(status_code=400, detail=f"{name} must be between 0 and 100")
    return value


def validate_pen_delay_down(value: int) -> int:
    value = int(value)
    if not -500 <= value <= 500:
        raise HTTPException(status_code=400, detail="pen_delay_down must be between -500 and 500 ms")
    return value


def validate_pen_delay_up(value: int) -> int:
    value = int(value)
    if not -500 <= value <= 500:
        raise HTTPException(status_code=400, detail="pen_delay_up must be between -500 and 500 ms")
    return value


def validate_pen_rate_raise(value: int) -> int:
    value = int(value)
    if not 1 <= value <= 100:
        raise HTTPException(status_code=400, detail="pen_rate_raise must be between 1 and 100")
    return value


def raw_xy_to_bed_xy(raw_xy: dict | None) -> dict | None:
    if raw_xy is None:
        return None
    return {
        "x_mm": -raw_xy["y_mm"],
        "y_mm": -raw_xy["x_mm"],
    }


def bed_delta_to_raw_delta(x_mm: float, y_mm: float) -> dict:
    return {
        "x_mm": -y_mm,
        "y_mm": -x_mm,
    }


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
    position_current = {
        "x_mm": max(0.0, min(BED_WIDTH_MM, float(x_mm))),
        "y_mm": max(0.0, min(BED_HEIGHT_MM, float(y_mm))),
    }


def set_home_position_unlocked(x_mm: float, y_mm: float) -> None:
    global home_position
    home_position = {
        "x_mm": max(0.0, min(BED_WIDTH_MM, float(x_mm))),
        "y_mm": max(0.0, min(BED_HEIGHT_MM, float(y_mm))),
    }


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
    return JOBS_DIR / job_id


def job_meta_path(job_id: str) -> Path:
    return job_dir(job_id) / "job.json"


def save_job_unlocked(job_id: str) -> None:
    if job_id not in jobs:
        return

    path = job_meta_path(job_id)
    path.parent.mkdir(parents=True, exist_ok=True)

    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(jobs[job_id], indent=2), encoding="utf-8")
    tmp.replace(path)


def load_jobs() -> None:
    """
    Load job metadata saved on disk.

    If the server restarts while a job is active, we do NOT automatically resume it.
    We mark it interrupted and let you rerun deliberately.
    """
    with jobs_lock:
        for path in JOBS_DIR.glob("*/job.json"):
            try:
                job = json.loads(path.read_text(encoding="utf-8"))
                job_id = job.get("id") or path.parent.name

                if job.get("status") in {
                    "queued",
                    "queued_for_operator",
                    "waiting_for_operator",
                    "queued_for_resume",
                    "running",
                }:
                    job["status"] = "interrupted"
                    job["operator_message"] = (
                        "Server restarted while this job was active. "
                        "Use rerun to start it again from the beginning."
                    )

                jobs[job_id] = job
                save_job_unlocked(job_id)

            except Exception as exc:
                print(f"Could not load {path}: {exc}", flush=True)


def check_token(x_plotter_token: Optional[str]) -> None:
    if PLOTTER_TOKEN and x_plotter_token != PLOTTER_TOKEN:
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
    if not path.exists():
        return ""

    return path.read_text(encoding="utf-8", errors="replace")[-max_chars:]


def active_running_job_unlocked() -> dict | None:
    for job in jobs.values():
        if job.get("status") in RUNNING_STATUSES:
            return job
    return None


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
    with hardware_lock:
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
    cmd = [AXICLI]
    if AXICLI_CONFIG.exists():
        cmd.extend(["--config", str(AXICLI_CONFIG)])
    return cmd


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

    raise_cmd = manual_command_cmd("raise_pen")
    home_cmd = manual_command_cmd("walk_home")
    actions = []

    with hardware_lock:
        try:
            with serial.Serial(PLOTTER_PORT, timeout=1) as port:
                pen_up, pen_ack = serial_query(port, "QP\r")
        except serial.SerialException as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

        actions.append(
            {
                "action": "check_pen",
                "pen_up": pen_up == "1",
                "raw": pen_up,
                "ack": pen_ack,
            }
        )

        if pen_up != "1":
            raise_proc = subprocess.run(
                raise_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            raise_result = {
                "action": "raise_pen",
                "ok": raise_proc.returncode == 0,
                "returncode": raise_proc.returncode,
                "command": raise_cmd,
                "output": raise_proc.stdout,
            }
            actions.append(raise_result)
            if raise_proc.returncode != 0:
                raise HTTPException(status_code=500, detail={"actions": actions})

        home_proc = subprocess.run(
            home_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        home_result = {
            "action": "walk_home",
            "ok": home_proc.returncode == 0,
            "returncode": home_proc.returncode,
            "command": home_cmd,
            "output": home_proc.stdout,
        }
        actions.append(home_result)
        if home_proc.returncode != 0:
            raise HTTPException(status_code=500, detail={"actions": actions})

        try:
            with serial.Serial(PLOTTER_PORT, timeout=1) as port:
                _axis_1, _axis_2, raw_after = read_step_position(port)
        except (serial.SerialException, ValueError) as exc:
            raise HTTPException(status_code=500, detail=f"Could not verify home position: {exc}") from exc

    actual = current_position_estimate(raw_after)
    if actual is None:
        raise HTTPException(status_code=500, detail="Could not calculate position after returning home")
    home_error_mm = math.hypot(actual["x_mm"] - home["x_mm"], actual["y_mm"] - home["y_mm"])

    with position_lock:
        set_current_position_unlocked(actual["x_mm"], actual["y_mm"])
        save_position_offset_unlocked()

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
        value, _ack = serial_query(port, command)
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


def require_enabled_high_resolution_motors(port: serial.Serial) -> None:
    if read_motor_resolution(port) != (1, 1):
        raise HTTPException(
            status_code=409,
            detail=(
                "Motors must already be enabled in high-resolution mode. "
                "Enable motors, then recalibrate before moving."
            ),
        )


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


def read_hardware_state() -> dict:
    if not hardware_lock.acquire(timeout=0.5):
        return {"busy": True, "message": "Hardware serial port is in use"}
    try:
        try:
            with serial.Serial(PLOTTER_PORT, timeout=1) as port:
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

                motor_1_raw, _ = serial_query(port, "PI,E,0\r")
                motor_2_raw, _ = serial_query(port, "PI,C,1\r")
        except serial.SerialException as exc:
            return {
                "busy": False,
                "connected": False,
                "port": PLOTTER_PORT,
                "error": str(exc),
            }

        with active_process_lock:
            active_pid = active_process.pid if active_process else None
            active_job = active_process_job_id

        return {
            "busy": False,
            "connected": True,
            "port": PLOTTER_PORT,
            "firmware": version,
            "pen_up": pen_up == "1",
            "button_pressed": button == "1",
            "steps": {"axis_1": axis_1, "axis_2": axis_2, "raw": steps},
            "raw_position_estimate": raw_xy_mm,
            "bed_position_unoffset": raw_xy_to_bed_xy(raw_xy_mm),
            "position_offset": dict(position_offset),
            "position_estimate": current_position_estimate(raw_xy_mm),
            "home_position": dict(home_position) if home_position is not None else None,
            "position_source": "software" if position_current is not None else "step_counter",
            "motor_enable_raw": {"axis_1": motor_1_raw, "axis_2": motor_2_raw},
            "acks": {"pen": pen_ack, "button": button_ack, "steps": steps_ack},
            "active_process": {"pid": active_pid, "job_id": active_job},
        }
    finally:
        hardware_lock.release()


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

    This uses AxiDraw's manual walk_home command. It assumes the plotter's
    start/home position was set correctly at the beginning of the job.
    """
    raise_cmd = manual_command_cmd("raise_pen")
    home_cmd = manual_command_cmd("walk_home")

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
            with serial.Serial(PLOTTER_PORT, timeout=1) as port:
                pen_up, pen_ack = serial_query(port, "QP\r")
        except serial.SerialException as exc:
            log.write(f"Could not check pen state before homing: {exc}\n")
            log.flush()
            return 1

        log.write(f"Pen state before homing: {'up' if pen_up == '1' else 'down'}")
        if pen_ack:
            log.write(f" ({pen_ack})")
        log.write("\n")

        if pen_up != "1":
            log.write("Raising pen before homing:\n")
            log.write(" ".join(raise_cmd) + "\n")
            log.flush()
            raise_proc = subprocess.run(
                raise_cmd,
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
            )
            if raise_proc.returncode != 0:
                return raise_proc.returncode

        log.write(" ".join(home_cmd) + "\n")
        log.flush()
        proc = subprocess.run(
            home_cmd,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
        )
        if proc.returncode == 0:
            try:
                with serial.Serial(PLOTTER_PORT, timeout=1) as port:
                    _axis_1, _axis_2, raw_after = read_step_position(port)
                actual = current_position_estimate(raw_after)
            except (serial.SerialException, ValueError) as exc:
                log.write(f"Could not verify home position: {exc}\n")
                log.flush()
                return 1
            if actual is None:
                log.write("Could not calculate position after returning home.\n")
                log.flush()
                return 1
            home_error_mm = math.hypot(actual["x_mm"] - home["x_mm"], actual["y_mm"] - home["y_mm"])
            with position_lock:
                set_current_position_unlocked(actual["x_mm"], actual["y_mm"])
                save_position_offset_unlocked()
            if home_error_mm > 0.5:
                log.write(
                    f"Home verification failed: expected {home}, actual {actual}, "
                    f"error={home_error_mm:.4f} mm\n"
                )
                log.flush()
                return 1
        return proc.returncode


def run_layer(job: dict, layer: dict, log) -> str:
    """
    Plot one SVG layer.

    Returns:
      "done"
      "paused"
      "failed"
    """
    cmd = axicli_cmd() + [
        str(layer["input_svg"]),
        "--port",
        PLOTTER_PORT,
        "-o",
        str(layer["progress_svg"]),
        "--speed_pendown",
        str(job["speed_pendown"]),
        "--speed_penup",
        str(job["speed_penup"]),
        "--pen_pos_down",
        str(job["pen_pos_down"]),
        "--pen_pos_up",
        str(job["pen_pos_up"]),
        "--pen_delay_down",
        str(job.get("pen_delay_down", DEFAULT_PEN_DELAY_DOWN)),
        "--pen_delay_up",
        str(job.get("pen_delay_up", DEFAULT_PEN_DELAY_UP)),
        "--pen_rate_raise",
        str(job.get("pen_rate_raise", DEFAULT_PEN_RATE_RAISE)),
    ]

    log.write("\nPlotting layer:\n")
    log.write(f"Layer {layer['index']}: {layer['name']}\n")
    log.write(" ".join(cmd) + "\n\n")
    log.flush()

    returncode = run_axicli_command(cmd, log, job_id=job["id"])
    text = log_tail(Path(job["log_path"]))

    if "Plot paused" in text or "Use the resume feature" in text:
        return "paused"

    if returncode == 0:
        return "done"

    return "failed"


def resume_layer(job: dict, layer: dict, log) -> str:
    """
    Resume a layer from its AxiDraw progress SVG.

    Returns:
      "done"
      "paused"
      "failed"
    """
    progress_svg = Path(layer["progress_svg"])
    if not progress_svg.exists():
        raise FileNotFoundError(f"Progress SVG not found: {progress_svg}")

    resumed_svg = progress_svg.with_name(f"{progress_svg.stem}_resumed_{int(now())}.svg")
    cmd = axicli_cmd() + [
        str(progress_svg),
        "--mode",
        "res_plot",
        "--port",
        PLOTTER_PORT,
        "-o",
        str(resumed_svg),
        "--speed_pendown",
        str(job["speed_pendown"]),
        "--speed_penup",
        str(job["speed_penup"]),
        "--pen_pos_down",
        str(job["pen_pos_down"]),
        "--pen_pos_up",
        str(job["pen_pos_up"]),
        "--pen_delay_down",
        str(job.get("pen_delay_down", DEFAULT_PEN_DELAY_DOWN)),
        "--pen_delay_up",
        str(job.get("pen_delay_up", DEFAULT_PEN_DELAY_UP)),
        "--pen_rate_raise",
        str(job.get("pen_rate_raise", DEFAULT_PEN_RATE_RAISE)),
    ]

    log.write("\nResuming layer:\n")
    log.write(f"Layer {layer['index']}: {layer['name']}\n")
    log.write(" ".join(cmd) + "\n\n")
    log.flush()
    resume_log_start = Path(job["log_path"]).stat().st_size

    returncode = run_axicli_command(cmd, log, job_id=job["id"])
    if resumed_svg.exists():
        layer["progress_svg"] = str(resumed_svg)
        with jobs_lock:
            save_job_unlocked(job["id"])

    log.flush()
    text = Path(job["log_path"]).read_text(encoding="utf-8", errors="replace")[resume_log_start:]
    if "Plot paused" in text or "Use the resume feature" in text:
        return "paused"

    if returncode == 0:
        return "done"

    return "failed"


def continue_job_after_layer(job_id: str, start_layer_number: int, log) -> None:
    with jobs_lock:
        job = jobs[job_id]
        layers = job["layers"]

    layer_count = len(layers)
    stop_current_job = False

    for layer in layers[start_layer_number:]:
        layer_number = layer["index"]

        update_job(
            job_id,
            status="running",
            current_layer=layer_number,
            current_layer_name=layer["name"],
        )

        result = run_layer(job, layer, log)
        if result == "paused":
            update_job(
                job_id,
                status="paused",
                paused_layer=layer_number,
                log_tail=log_tail(Path(job["log_path"])),
            )
            announce_on_linux_box(
                f"Job {job_id} paused during Layer {layer_number}. Resume handling is needed before continuing."
            )
            stop_current_job = True
            break

        if result != "done":
            update_job(
                job_id,
                status="failed",
                failed_layer=layer_number,
                finished_at=now(),
                log_tail=log_tail(Path(job["log_path"])),
            )
            stop_current_job = True
            break

        update_job(job_id, last_completed_layer=layer_number, log_tail=log_tail(Path(job["log_path"])))
        home_return_code = return_home(log)
        if home_return_code != 0:
            update_job(
                job_id,
                status="failed",
                failed_layer=layer_number,
                finished_at=now(),
                error=f"walk_home failed with return code {home_return_code}",
                log_tail=log_tail(Path(job["log_path"])),
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

    if not stop_current_job:
        update_job(
            job_id,
            status="done",
            current_layer=None,
            current_layer_name=None,
            finished_at=now(),
            log_tail=log_tail(Path(job["log_path"])),
        )


def resume_paused_job(job_id: str) -> None:
    with jobs_lock:
        job = jobs[job_id]
        paused_layer_number = int(job.get("paused_layer") or job.get("current_layer") or 1)
        layers = job["layers"]
        layer = next((item for item in layers if item["index"] == paused_layer_number), None)

    if layer is None:
        update_job(job_id, status="failed", error=f"Paused layer not found: {paused_layer_number}", finished_at=now())
        return

    log_path = Path(job["log_path"])
    try:
        with log_path.open("a", encoding="utf-8", errors="replace") as log:
            update_job(
                job_id,
                status="running",
                current_layer=paused_layer_number,
                current_layer_name=layer["name"],
                resumed_at=now(),
            )

            result = resume_layer(job, layer, log)

            if result == "paused":
                update_job(
                    job_id,
                    status="paused",
                    paused_layer=paused_layer_number,
                    log_tail=log_tail(log_path),
                )
                return

            if result != "done":
                update_job(
                    job_id,
                    status="failed",
                    failed_layer=paused_layer_number,
                    finished_at=now(),
                    log_tail=log_tail(log_path),
                )
                return

            update_job(job_id, last_completed_layer=paused_layer_number, log_tail=log_tail(log_path))

            home_return_code = return_home(log)
            if home_return_code != 0:
                update_job(
                    job_id,
                    status="failed",
                    failed_layer=paused_layer_number,
                    finished_at=now(),
                    error=f"walk_home failed with return code {home_return_code}",
                    log_tail=log_tail(log_path),
                )
                return

            continue_job_after_layer(job_id, paused_layer_number, log)

    except Exception as exc:
        update_job(
            job_id,
            status="failed",
            finished_at=now(),
            error=repr(exc),
            log_tail=log_tail(log_path),
        )
        announce_on_linux_box(f"Job {job_id} resume failed: {exc!r}")


def worker() -> None:
    while True:
        job_id = job_queue.get()

        with jobs_lock:
            job = jobs[job_id]
            if job.get("status") == "cancelled":
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

                    result = run_layer(job, layer, log)

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
load_jobs()
if os.environ.get("PLOTTER_DISABLE_WORKER") != "1":
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

        with input_svg.open("wb") as out:
            shutil.copyfileobj(uploaded.file, out)

        try:
            svg_metrics = validate_svg_file(input_svg)
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
                "svg_metrics": svg_metrics,
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
        "current_layer": None,
        "current_layer_name": None,
        "last_completed_layer": None,
        "operator_message": None,
    }

    with jobs_lock:
        jobs[job_id] = job
        save_job_unlocked(job_id)

    job_queue.put(job_id)

    return {
        "job_id": job_id,
        "status": "queued",
        "layer_count": len(layers),
        "status_url": f"/jobs/{job_id}",
    }


@app.get("/jobs")
def list_jobs(
    x_plotter_token: Optional[str] = Header(default=None),
):
    check_token(x_plotter_token)

    with jobs_lock:
        summaries = []

        for job_id, job in jobs.items():
            summaries.append(
                {
                    "id": job_id,
                    "status": job.get("status"),
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
                    "rerun_of": job.get("rerun_of"),
                    "log_path": job.get("log_path"),
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

    threading.Thread(target=resume_paused_job, args=(job_id,), daemon=True).start()

    return {
        "ok": True,
        "job_id": job_id,
        "status": "queued_for_resume",
        "paused_layer": paused_layer_number,
        "plot_settings": resume_plot_settings,
        "pen_settings": resume_pen_settings,
        "message": "Resume started.",
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
            ["done", "failed", "cancelled", "interrupted", "paused"],
        )
    )

    removed = []
    skipped = []

    with jobs_lock:
        for job_id, job in list(jobs.items()):
            status = job.get("status")
            if status in {"queued", "queued_for_operator", "waiting_for_operator", "queued_for_resume", "running"}:
                skipped.append({"id": job_id, "status": status})
                continue
            if status not in clear_statuses:
                skipped.append({"id": job_id, "status": status})
                continue

            removed.append({"id": job_id, "status": status})
            del jobs[job_id]

            if keep_files:
                continue

            try:
                shutil.rmtree(job_dir(job_id), ignore_errors=True)
                log_path = Path(job.get("log_path", ""))
                if log_path.exists() and log_path.is_file():
                    log_path.unlink()
            except Exception as exc:
                skipped.append({"id": job_id, "status": status, "error": repr(exc)})

    return {"removed": removed, "skipped": skipped, "keep_files": keep_files}


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
        "running",
    }:
        raise HTTPException(
            status_code=400,
            detail="Original job is active. Wait until it stops before rerunning.",
        )

    new_job_id = uuid.uuid4().hex[:12]
    new_job_dir = job_dir(new_job_id)
    new_job_dir.mkdir(parents=True, exist_ok=True)

    new_layers = []

    for layer in original["layers"]:
        idx = layer["index"]

        layer_dir = new_job_dir / f"layer_{idx:02d}"
        layer_dir.mkdir(parents=True, exist_ok=True)

        old_input = Path(layer["input_svg"])
        new_input = layer_dir / "input.svg"
        new_progress = layer_dir / "progress.svg"

        if not old_input.exists():
            raise HTTPException(
                status_code=400,
                detail=f"Original layer file missing: {old_input}",
            )

        shutil.copy(old_input, new_input)

        new_layers.append(
            {
                "index": idx,
                "name": layer["name"],
                "original_filename": layer.get(
                    "original_filename",
                    f"layer_{idx:02d}.svg",
                ),
                "input_svg": str(new_input),
                "progress_svg": str(new_progress),
            }
        )

    log_path = LOGS_DIR / f"{new_job_id}.log"
    current_plot_defaults = current_plot_settings()

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
        "current_layer": None,
        "current_layer_name": None,
        "last_completed_layer": None,
        "operator_message": None,
        "rerun_of": job_id,
    }

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

    extra = []
    if "pen_pos_down" in payload:
        payload["pen_pos_down"] = validate_pen_position(payload["pen_pos_down"], "pen_pos_down")
        extra.extend(["--pen_pos_down", str(payload["pen_pos_down"])])
    if "pen_pos_up" in payload:
        payload["pen_pos_up"] = validate_pen_position(payload["pen_pos_up"], "pen_pos_up")
        extra.extend(["--pen_pos_up", str(payload["pen_pos_up"])])

    result = run_manual_command("raise_pen" if position == "up" else "lower_pen", extra)

    if result["ok"] and ("pen_pos_down" in payload or "pen_pos_up" in payload):
        with pen_settings_lock:
            if "pen_pos_down" in payload:
                pen_settings["pen_pos_down"] = int(payload["pen_pos_down"])
            if "pen_pos_up" in payload:
                pen_settings["pen_pos_up"] = int(payload["pen_pos_up"])
            save_pen_settings_unlocked()
            result["pen_settings"] = dict(pen_settings)

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
        save_plot_settings_unlocked()
        return {"ok": True, "plot_settings": dict(plot_settings)}


@app.post("/plotter/motors")
def plotter_motors(
    request: Request,
    payload: dict = Body(...),
    x_plotter_token: Optional[str] = Header(default=None),
):
    require_localhost(request)
    check_token(x_plotter_token)

    enabled = bool(payload.get("enabled"))
    result = run_manual_command("enable_xy" if enabled else "disable_xy")
    with position_lock:
        invalidate_position_reference_unlocked()
    result["position_invalidated"] = True
    result["message"] = "Motor state changed; recalibrate before moving."
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
        if absolute:
            with serial.Serial(PLOTTER_PORT, timeout=1) as port:
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
    validate_bed_target(current["x_mm"] + x_mm, current["y_mm"] + y_mm)

    raw_delta = bed_delta_to_raw_delta(x_mm, y_mm)
    axis_1, axis_2 = xy_mm_to_steps(raw_delta["x_mm"], raw_delta["y_mm"])
    distance = (x_mm * x_mm + y_mm * y_mm) ** 0.5
    duration_ms = max(40, int(round(distance / speed_mm_s * 1000)))

    with hardware_lock:
        try:
            with serial.Serial(PLOTTER_PORT, timeout=2) as port:
                require_enabled_high_resolution_motors(port)
                move_response = raw_command(port, f"SM,{duration_ms},{axis_1},{axis_2}\r")
                wait_for_motion_idle(port, max(2.0, duration_ms / 1000.0 + 1.0))
                _axis_1_now, _axis_2_now, raw_current = read_step_position(port)
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
        actual = current_position_estimate(raw_current)
        if actual is not None:
            set_current_position_unlocked(actual["x_mm"], actual["y_mm"])
            save_position_offset_unlocked()
            current = dict(position_current)
        else:
            current = None

    return {
        "ok": True,
        "x_mm": x_mm,
        "y_mm": y_mm,
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
def operator_continue(request: Request):
    require_localhost(request)

    with operator_lock:
        if not operator_prompt["active"]:
            return {"ok": True, "message": "No operator prompt is active."}

    operator_event.set()

    return {"ok": True, "message": "Continuing."}
