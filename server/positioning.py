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


def clamp_bed_position(x_mm: float, y_mm: float, *, bed_width_mm: float, bed_height_mm: float) -> dict:
    return {
        "x_mm": max(0.0, min(bed_width_mm, float(x_mm))),
        "y_mm": max(0.0, min(bed_height_mm, float(y_mm))),
    }
