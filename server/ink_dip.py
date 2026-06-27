from __future__ import annotations

from pathlib import Path
import copy
import math
import xml.etree.ElementTree as ET


INCH_TO_MM = 25.4
AXIDRAW_MAX_SPEED_IN_S = 8.6979
INKSCAPE_NAMESPACE = "http://www.inkscape.org/namespaces/inkscape"


def parse_plob_polylines(path: Path) -> list[list[tuple[float, float]]]:
    """Read an AxiDraw PLOB digest and return ordered polylines in millimetres."""
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        raise ValueError(f"Invalid AxiDraw plot digest: {exc}") from exc

    polylines: list[list[tuple[float, float]]] = []
    for element in root.iter():
        if _local_name(element.tag) != "polyline":
            continue
        points = _parse_points(element.attrib.get("points", ""))
        if len(points) >= 2:
            polylines.append(points)

    if not polylines:
        raise ValueError("AxiDraw plot digest contains no drawable polylines")
    return polylines


def estimate_checkpoint_schedule(
    polylines: list[list[tuple[float, float]]],
    *,
    speed_pendown: int,
    interval_s: float,
) -> dict:
    if not 1 <= int(speed_pendown) <= 100:
        raise ValueError("speed_pendown must be between 1 and 100")
    if not math.isfinite(interval_s) or interval_s <= 0:
        raise ValueError("interval_s must be positive")

    speed_mm_s = int(speed_pendown) * AXIDRAW_MAX_SPEED_IN_S * INCH_TO_MM / 110.0
    stroke_lengths = [polyline_length(points) for points in polylines]
    stroke_times = [length / speed_mm_s for length in stroke_lengths]

    checkpoints: list[int] = []
    elapsed_since_dip = 0.0
    for index, stroke_time in enumerate(stroke_times, start=1):
        elapsed_since_dip += stroke_time
        if elapsed_since_dip >= interval_s and index < len(stroke_times):
            checkpoints.append(index)
            elapsed_since_dip = 0.0

    return {
        "stroke_count": len(polylines),
        "checkpoint_after_strokes": checkpoints,
        "estimated_dip_count_per_layer": 1 + len(checkpoints),
        "estimated_pen_down_time_s": round(sum(stroke_times), 3),
        "longest_stroke_time_s": round(max(stroke_times, default=0.0), 3),
        "longest_stroke_length_mm": round(max(stroke_lengths, default=0.0), 3),
        "estimated_speed_mm_s": round(speed_mm_s, 3),
        "interval_s": round(interval_s, 3),
    }


def find_keepout_collision(
    polylines: list[list[tuple[float, float]]],
    *,
    origin_mm: tuple[float, float],
    centre_mm: tuple[float, float],
    radius_mm: float,
) -> dict | None:
    """Return the first pen-down or pen-up segment intersecting the keep-out circle."""
    if not math.isfinite(radius_mm) or radius_mm <= 0:
        raise ValueError("radius_mm must be positive")

    origin = (float(origin_mm[0]), float(origin_mm[1]))
    absolute = [
        [(origin[0] + point[0], origin[1] + point[1]) for point in polyline]
        for polyline in polylines
    ]

    previous = origin
    for stroke_index, points in enumerate(absolute, start=1):
        collision = _segment_collision(previous, points[0], centre_mm, radius_mm)
        if collision:
            return {
                "motion": "pen_up",
                "stroke": stroke_index,
                "segment": 0,
                **collision,
            }

        for segment_index, (start, end) in enumerate(zip(points, points[1:]), start=1):
            collision = _segment_collision(start, end, centre_mm, radius_mm)
            if collision:
                return {
                    "motion": "pen_down",
                    "stroke": stroke_index,
                    "segment": segment_index,
                    **collision,
                }
        previous = points[-1]

    collision = _segment_collision(previous, origin, centre_mm, radius_mm)
    if collision:
        return {
            "motion": "pen_up_return_home",
            "stroke": len(absolute),
            "segment": 0,
            **collision,
        }
    return None


def write_checkpoint_digest(
    source: Path,
    target: Path,
    checkpoint_after_strokes: list[int],
) -> int:
    """Split a PLOB into layers that pause before each scheduled continuation."""
    try:
        tree = ET.parse(source)
    except ET.ParseError as exc:
        raise ValueError(f"Invalid AxiDraw plot digest: {exc}") from exc
    root = tree.getroot()
    checkpoints = set(checkpoint_after_strokes)
    if any(index <= 0 for index in checkpoints):
        raise ValueError("Checkpoint stroke indexes must be positive")

    groups = [child for child in list(root) if _local_name(child.tag) == "g"]
    paths: list[tuple[ET.Element, ET.Element]] = []
    for group in groups:
        label = group.attrib.get(f"{{{INKSCAPE_NAMESPACE}}}label", "")
        if label.lstrip().startswith("!"):
            raise ValueError(
                "Automatic dipping cannot be combined with existing AxiDraw programmatic pause layers"
            )
        for child in list(group):
            if _local_name(child.tag) == "polyline":
                paths.append((group, child))

    if not paths:
        raise ValueError("AxiDraw plot digest contains no drawable polylines")
    if checkpoints and max(checkpoints) >= len(paths):
        raise ValueError("Checkpoints must occur before the final stroke")

    root_children = list(root)
    insert_at = min(root_children.index(group) for group in groups)
    for group in groups:
        root.remove(group)

    new_groups: list[ET.Element] = []
    active_group: ET.Element | None = None
    active_source: ET.Element | None = None
    for stroke_index, (source_group, path) in enumerate(paths, start=1):
        pause_before = stroke_index > 1 and stroke_index - 1 in checkpoints
        if active_group is None or active_source is not source_group or pause_before:
            active_group = ET.Element(source_group.tag, dict(source_group.attrib))
            active_source = source_group
            if pause_before:
                active_group.set(f"{{{INKSCAPE_NAMESPACE}}}label", f"!ink-dip-{stroke_index}")
            new_groups.append(active_group)
        active_group.append(copy.deepcopy(path))

    for offset, group in enumerate(new_groups):
        root.insert(insert_at + offset, group)

    target.parent.mkdir(parents=True, exist_ok=True)
    tree.write(target, encoding="utf-8", xml_declaration=True)
    return len(new_groups)


def polyline_length(points: list[tuple[float, float]]) -> float:
    return sum(math.dist(start, end) for start, end in zip(points, points[1:]))


def _parse_points(value: str) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []
    for pair in value.split():
        try:
            x_text, y_text = pair.split(",", 1)
            points.append((float(x_text) * INCH_TO_MM, float(y_text) * INCH_TO_MM))
        except ValueError as exc:
            raise ValueError(f"Invalid point in AxiDraw plot digest: {pair!r}") from exc
    return points


def _segment_collision(
    start: tuple[float, float],
    end: tuple[float, float],
    centre: tuple[float, float],
    radius: float,
) -> dict | None:
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    length_squared = dx * dx + dy * dy
    if length_squared == 0:
        nearest = start
    else:
        projection = (
            (centre[0] - start[0]) * dx + (centre[1] - start[1]) * dy
        ) / length_squared
        projection = max(0.0, min(1.0, projection))
        nearest = (start[0] + projection * dx, start[1] + projection * dy)

    distance = math.dist(nearest, centre)
    if distance > radius:
        return None
    return {
        "start": {"x_mm": round(start[0], 4), "y_mm": round(start[1], 4)},
        "end": {"x_mm": round(end[0], 4), "y_mm": round(end[1], 4)},
        "nearest": {"x_mm": round(nearest[0], 4), "y_mm": round(nearest[1], 4)},
        "distance_mm": round(distance, 4),
        "radius_mm": round(radius, 4),
    }


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]
