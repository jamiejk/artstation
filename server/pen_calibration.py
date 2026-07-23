"""Pen-height calibration plot geometry.

Two checks from the current head position:

1. **Contact dots** — taps stepped through the *working* range between max lift
   and max lower (plus a little deeper). Heights are absolute map units derived
   from the operator's Set Max Lift / Set Max Lower values, not a fixed 50–100 ladder.

2. **Clearance dashes** — short strokes at max lower with pen-up travel at max
   lift so the operator can see drag trails if lift is too low.

Execution stays in ``server.py``; this module stays free of serial I/O.
"""

from __future__ import annotations

from typing import Callable


# --- Clearance strip (dual dash + third row) ---
DEFAULT_DASH_MM = 12.0
DEFAULT_GAP_MM = 6.0
DEFAULT_ROW_SPACING_MM = 8.0
DEFAULT_DRAW_SPEED_MM_S = 30.0
DEFAULT_TRANSIT_SPEED_MM_S = 80.0

# --- Contact-dot ladder (relative to max lift / max lower) ---
DEFAULT_DOT_SPACING_MM = 5.0
DEFAULT_DOT_DWELL_MS = 250
DEFAULT_CONTACT_STEP = 2
# How many map units past max lower (deeper) to include on the ladder.
DEFAULT_CONTACT_EXTRA_DEEP = 3
# Vertical gap between the contact-dot row and the clearance strip.
DEFAULT_SECTION_GAP_MM = 10.0


def contact_heights(
    *,
    high: int,
    low: int,
    step: int = DEFAULT_CONTACT_STEP,
) -> list[int]:
    """Descending pen positions for the contact ladder (light → heavy)."""
    high = int(high)
    low = int(low)
    step = int(step)
    if step < 1:
        raise ValueError("contact step must be at least 1")
    if not 0 <= low <= high <= 100:
        raise ValueError("contact high/low must satisfy 0 <= low <= high <= 100")
    heights = list(range(high, low - 1, -step))
    if not heights or heights[-1] != low:
        if low not in heights:
            heights.append(low)
    return heights


def contact_heights_for_working_range(
    pen_up: int,
    pen_down: int,
    *,
    step: int = DEFAULT_CONTACT_STEP,
    extra_deep: int = DEFAULT_CONTACT_EXTRA_DEEP,
) -> list[int]:
    """Build tap heights from just below max lift down through max lower (+ deeper).

    Higher map values = more raised. Ladder is light → heavy left to right.
    """
    pen_up = int(pen_up)
    pen_down = int(pen_down)
    if not 0 <= pen_down < pen_up <= 100:
        raise ValueError("pen_up must be greater than pen_down (0–100)")
    step = max(1, int(step))
    extra_deep = max(0, int(extra_deep))

    # First taps just under max lift (often no mark); last a bit past max lower.
    high = max(pen_down, pen_up - 1)
    low = max(0, pen_down - extra_deep)
    if high < low:
        high = low
    heights = contact_heights(high=high, low=low, step=step)
    # Always include max lower so the ladder hits the drawing height.
    if pen_down not in heights:
        heights.append(pen_down)
        heights = sorted(set(heights), reverse=True)
    return heights


def calibration_footprint(
    *,
    pen_up: int,
    pen_down: int,
    dash_mm: float = DEFAULT_DASH_MM,
    gap_mm: float = DEFAULT_GAP_MM,
    row_spacing_mm: float = DEFAULT_ROW_SPACING_MM,
    dot_spacing_mm: float = DEFAULT_DOT_SPACING_MM,
    section_gap_mm: float = DEFAULT_SECTION_GAP_MM,
    contact_step: int = DEFAULT_CONTACT_STEP,
    extra_deep: int = DEFAULT_CONTACT_EXTRA_DEEP,
) -> dict:
    """Axis-aligned size of the full calibration pattern from its start corner."""
    heights = contact_heights_for_working_range(
        pen_up,
        pen_down,
        step=contact_step,
        extra_deep=extra_deep,
    )
    dots_width = max(0, len(heights) - 1) * float(dot_spacing_mm)
    clearance_width = 2.0 * float(dash_mm) + float(gap_mm)
    width_mm = max(dots_width, clearance_width)
    height_mm = float(section_gap_mm) + float(row_spacing_mm)
    return {
        "width_mm": round(width_mm, 4),
        "height_mm": round(height_mm, 4),
        "dash_mm": float(dash_mm),
        "gap_mm": float(gap_mm),
        "row_spacing_mm": float(row_spacing_mm),
        "dot_spacing_mm": float(dot_spacing_mm),
        "section_gap_mm": float(section_gap_mm),
        "contact_heights": heights,
        "contact_high": heights[0] if heights else None,
        "contact_low": heights[-1] if heights else None,
        "contact_step": int(contact_step),
        "pen_up": int(pen_up),
        "pen_down": int(pen_down),
    }


def build_calibration_actions(
    start: dict,
    *,
    pen_up: int,
    pen_down: int,
    dash_mm: float = DEFAULT_DASH_MM,
    gap_mm: float = DEFAULT_GAP_MM,
    row_spacing_mm: float = DEFAULT_ROW_SPACING_MM,
    draw_speed_mm_s: float = DEFAULT_DRAW_SPEED_MM_S,
    transit_speed_mm_s: float = DEFAULT_TRANSIT_SPEED_MM_S,
    dot_spacing_mm: float = DEFAULT_DOT_SPACING_MM,
    dot_dwell_ms: int = DEFAULT_DOT_DWELL_MS,
    section_gap_mm: float = DEFAULT_SECTION_GAP_MM,
    contact_step: int = DEFAULT_CONTACT_STEP,
    extra_deep: int = DEFAULT_CONTACT_EXTRA_DEEP,
    validate_bed_target: Callable[[float, float], tuple[float, float]] | None = None,
) -> list[dict]:
    """Build ordered actions for contact dots + clearance strip.

    Contact dots use absolute map heights (type ``height``). Clearance strokes
    use binary pen up/down at the operator max lift / max lower.
    """
    start_x = float(start["x_mm"])
    start_y = float(start["y_mm"])
    pen_up = int(pen_up)
    pen_down = int(pen_down)
    if not 0 <= pen_down < pen_up <= 100:
        raise ValueError("pen_up must be greater than pen_down (0–100)")

    dash = float(dash_mm)
    gap = float(gap_mm)
    row = float(row_spacing_mm)
    draw = max(1.0, float(draw_speed_mm_s))
    transit = max(1.0, float(transit_speed_mm_s))
    spacing = float(dot_spacing_mm)
    dwell_ms = max(0, int(dot_dwell_ms))
    section = float(section_gap_mm)

    if dash < 2.0 or gap < 1.0 or row < 2.0:
        raise ValueError("Clearance pattern dimensions are too small")
    if spacing < 2.0:
        raise ValueError("dot_spacing_mm must be at least 2")

    heights = contact_heights_for_working_range(
        pen_up,
        pen_down,
        step=contact_step,
        extra_deep=extra_deep,
    )
    if not heights:
        raise ValueError("No contact-dot heights in the working range")

    def move(x_mm: float, y_mm: float, speed_mm_s: float, label: str) -> dict:
        return {
            "type": "move",
            "x_mm": x_mm,
            "y_mm": y_mm,
            "speed_mm_s": speed_mm_s,
            "label": label,
        }

    def pen(raised: bool, label: str) -> dict:
        return {
            "type": "pen",
            "raised": raised,
            "up_pos": pen_up,
            "down_pos": pen_down,
            "label": label,
        }

    def height(h: float, label: str) -> dict:
        return {"type": "height", "height": float(h), "label": label}

    def dwell(ms: int, label: str) -> dict:
        return {"type": "dwell", "ms": int(ms), "label": label}

    points: list[tuple[float, float]] = [(start_x, start_y)]
    for i, _h in enumerate(heights):
        points.append((start_x + i * spacing, start_y))

    clearance_y0 = start_y + section
    clearance_y1 = clearance_y0 + row
    p0 = (start_x, clearance_y0)
    p1 = (start_x + dash, clearance_y0)
    p2 = (start_x + dash + gap, clearance_y0)
    p3 = (start_x + 2.0 * dash + gap, clearance_y0)
    p4 = (start_x, clearance_y1)
    p5 = (start_x + dash, clearance_y1)
    points.extend([p0, p1, p2, p3, p4, p5])

    if validate_bed_target is not None:
        for x_mm, y_mm in points:
            validate_bed_target(x_mm, y_mm)

    actions: list[dict] = [
        height(pen_up, "Raise to max lift before contact dots"),
        move(start_x, start_y, transit, "Move to contact-dot row start"),
    ]

    for i, h in enumerate(heights):
        x = start_x + i * spacing
        y = start_y
        if i > 0:
            actions.append(move(x, y, transit, f"Transit to contact dot height={h}"))
        # Tap at absolute map height (same path as Z jog).
        actions.append(height(h, f"Contact tap at map height {h}"))
        if dwell_ms > 0:
            actions.append(dwell(dwell_ms, f"Dwell contact tap height={h}"))
        actions.append(height(pen_up, f"Raise to max lift after tap height={h}"))

    # Clearance strip at operator max lower / max lift.
    actions.extend(
        [
            move(p0[0], p0[1], transit, "Pen-up transit to clearance strip"),
            pen(False, "Lower for first clearance dash (max lower)"),
            move(p1[0], p1[1], draw, "Draw first clearance dash"),
            pen(True, "Raise before gap transit (max lift clearance check)"),
            move(p2[0], p2[1], transit, "Pen-up transit across gap"),
            pen(False, "Lower for second clearance dash"),
            move(p3[0], p3[1], draw, "Draw second clearance dash"),
            pen(True, "Raise before second-row transit"),
            move(p4[0], p4[1], transit, "Pen-up transit to second clearance row"),
            pen(False, "Lower for third clearance dash"),
            move(p5[0], p5[1], draw, "Draw third clearance dash"),
            pen(True, "Raise after calibration"),
            move(start_x, start_y, transit, "Return to start (pen up)"),
        ]
    )

    return actions


def describe_what_to_look_for(
    *,
    pen_up: int,
    pen_down: int,
    contact_step: int = DEFAULT_CONTACT_STEP,
    extra_deep: int = DEFAULT_CONTACT_EXTRA_DEEP,
) -> str:
    heights = contact_heights_for_working_range(
        pen_up,
        pen_down,
        step=contact_step,
        extra_deep=extra_deep,
    )
    legend = ", ".join(str(h) for h in heights)
    return (
        "Contact dots (top row, left→right) step through your working Z range "
        f"[{legend}] from just under max lift ({pen_up}) toward max lower ({pen_down}) "
        f"and a little deeper. Left = lighter; right = heavier. "
        "You should see no mark, then light marks, then solid around max lower. "
        "Clearance dashes (lower rows) draw at max lower; the gap must stay clean at max lift."
    )


def suggested_pen_down_note() -> str:
    return (
        "If every contact tap is blank, max lower is still too high (too raised) or the pen "
        "needs reseating deeper. If only the deepest taps mark, Set Max Lower to the lightest "
        "solid height. Max lift must clear the paper in the dash gaps."
    )
