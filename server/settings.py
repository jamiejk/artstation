import math

from fastapi import HTTPException


AXIDRAW_PEN_POSITION_MIN = 0
AXIDRAW_PEN_POSITION_MAX = 100

PAPER_SIZES_MM = {
    "A0": {"width_mm": 841.0, "height_mm": 1189.0},
    "A1": {"width_mm": 594.0, "height_mm": 841.0},
    "A2": {"width_mm": 420.0, "height_mm": 594.0},
    "A3": {"width_mm": 297.0, "height_mm": 420.0},
    "A4": {"width_mm": 210.0, "height_mm": 297.0},
}


def paper_dimensions(settings: dict) -> dict:
    size = str(settings.get("size") or "A3").upper()
    if size not in PAPER_SIZES_MM:
        raise ValueError(f"Unsupported paper size: {size}")
    orientation = str(settings.get("orientation") or "portrait").lower()
    if orientation not in {"portrait", "landscape"}:
        raise ValueError("Paper orientation must be portrait or landscape")
    base = PAPER_SIZES_MM[size]
    width = base["width_mm"]
    height = base["height_mm"]
    if orientation == "landscape":
        width, height = height, width
    return {"width_mm": width, "height_mm": height}


def validate_paper_settings(settings: dict, *, bed_width_mm: float, bed_height_mm: float) -> dict:
    size = str(settings.get("size") or "A3").upper()
    orientation = str(settings.get("orientation") or "portrait").lower()
    candidate = {
        "state_version": 1,
        "enabled": bool(settings.get("enabled", True)),
        "size": size,
        "orientation": orientation,
        "top_right": settings.get("top_right"),
    }
    dimensions = paper_dimensions(candidate)
    top_right = candidate.get("top_right")
    if top_right is not None:
        if not isinstance(top_right, dict) or "x_mm" not in top_right or "y_mm" not in top_right:
            raise ValueError("Paper top_right must contain x_mm and y_mm")
        x_mm = float(top_right["x_mm"])
        y_mm = float(top_right["y_mm"])
        if not math.isfinite(x_mm) or not math.isfinite(y_mm):
            raise HTTPException(status_code=400, detail="Target coordinates must be finite numbers")
        if not (0 <= x_mm <= bed_width_mm and 0 <= y_mm <= bed_height_mm):
            raise HTTPException(
                status_code=400,
                detail=f"Target must be within 0..{bed_width_mm:g}mm X and 0..{bed_height_mm:g}mm Y",
            )
        candidate["top_right"] = {"x_mm": x_mm, "y_mm": y_mm}
    candidate.update(dimensions)
    return candidate


def validate_speed_setting(value: int, name: str) -> int:
    value = int(value)
    if not 1 <= value <= 100:
        raise HTTPException(status_code=400, detail=f"{name} must be between 1 and 100")
    return value


def validate_pen_position(value: int, name: str) -> int:
    value = int(value)
    if not AXIDRAW_PEN_POSITION_MIN <= value <= AXIDRAW_PEN_POSITION_MAX:
        raise HTTPException(
            status_code=400,
            detail=f"{name} must be between {AXIDRAW_PEN_POSITION_MIN} and {AXIDRAW_PEN_POSITION_MAX}",
        )
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


def validate_dip_interval(value: float) -> float:
    try:
        value = float(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Pen-down travel before dip must be a number of seconds") from exc
    if not math.isfinite(value) or not 0.1 <= value <= 86400:
        raise HTTPException(status_code=400, detail="Pen-down travel before dip must be between 0.1 and 86400 seconds")
    return round(value, 3)


def resolve_auto_dip_flag(*values) -> bool:
    truthy = {"1", "true", "yes", "on", "y"}
    for value in values:
        if value is True:
            return True
        if isinstance(value, str) and value.strip().lower() in truthy:
            return True
    return False


def auto_dip_flag_was_provided(*values) -> bool:
    return any(value is not None for value in values)
