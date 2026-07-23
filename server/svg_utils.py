from pathlib import Path
import re
import xml.etree.ElementTree as ET


SVG_NAMESPACE = "http://www.w3.org/2000/svg"
QUARTER_TURN_ROTATIONS = {0, 90, 180, 270}
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


def validate_rotation_degrees(value: int | str | None) -> int:
    if value is None or value == "":
        return 0
    try:
        numeric_rotation = float(value)
        if not numeric_rotation.is_integer():
            raise ValueError
        rotation = int(numeric_rotation)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("rotation_degrees must be one of 0, 90, 180, or 270") from exc
    if rotation not in QUARTER_TURN_ROTATIONS:
        raise ValueError("rotation_degrees must be one of 0, 90, 180, or 270")
    return rotation


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


def validate_svg_file(path: Path, *, max_width_mm: float, max_height_mm: float) -> dict:
    return validate_svg_text(
        path.read_text(encoding="utf-8", errors="replace"),
        max_width_mm=max_width_mm,
        max_height_mm=max_height_mm,
    )


def rotate_svg_text(svg_text: str, rotation_degrees: int | str | None) -> str:
    """Rotate an SVG clockwise inside a resized viewport."""
    rotation = validate_rotation_degrees(rotation_degrees)
    if rotation == 0:
        return svg_text

    try:
        root = ET.fromstring(svg_text)
    except ET.ParseError as exc:
        raise ValueError(f"Invalid SVG XML: {exc}") from exc
    if _local_name(root.tag) != "svg":
        raise ValueError("Uploaded file is not an SVG document")

    view_box = _parse_view_box(root.attrib.get("viewBox", ""))
    if view_box is None:
        viewport_width = _length_to_user_units(root.attrib.get("width"))
        viewport_height = _length_to_user_units(root.attrib.get("height"))
        if viewport_width is None or viewport_height is None:
            raise ValueError("SVG must define positive width and height or a usable viewBox")
        view_box = (0.0, 0.0, viewport_width, viewport_height)

    min_x, min_y, width, height = view_box
    if width <= 0 or height <= 0:
        raise ValueError("SVG must define positive width and height or a usable viewBox")

    if rotation == 90:
        matrix = (0.0, 1.0, -1.0, 0.0, min_y + height, -min_x)
        rotated_width, rotated_height = height, width
    elif rotation == 180:
        matrix = (-1.0, 0.0, 0.0, -1.0, min_x + width, min_y + height)
        rotated_width, rotated_height = width, height
    else:
        matrix = (0.0, -1.0, 1.0, 0.0, -min_y, min_x + width)
        rotated_width, rotated_height = height, width

    group = ET.Element(f"{{{SVG_NAMESPACE}}}g")
    group.set("transform", f"matrix({' '.join(_format_number(value) for value in matrix)})")
    for child in list(root):
        root.remove(child)
        group.append(child)
    root.append(group)

    if rotation in {90, 270}:
        original_width = root.attrib.get("width")
        original_height = root.attrib.get("height")
        if original_height is not None:
            root.set("width", original_height)
        else:
            root.attrib.pop("width", None)
        if original_width is not None:
            root.set("height", original_width)
        else:
            root.attrib.pop("height", None)

    root.set(
        "viewBox",
        f"0 0 {_format_number(rotated_width)} {_format_number(rotated_height)}",
    )
    ET.register_namespace("", SVG_NAMESPACE)
    return ET.tostring(root, encoding="unicode")


def rotate_svg_file(path: Path, rotation_degrees: int | str | None) -> None:
    rotated = rotate_svg_text(
        path.read_text(encoding="utf-8", errors="replace"),
        rotation_degrees,
    )
    path.write_text(rotated, encoding="utf-8")


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


def _length_to_user_units(value: str | None) -> float | None:
    if value is None or value == "":
        return None
    match = _LENGTH_RE.match(value)
    if not match:
        raise ValueError(f"Unsupported SVG length: {value!r}")
    number = float(match.group(1))
    unit = match.group(2).lower()
    if unit not in _UNIT_TO_MM:
        raise ValueError(f"Unsupported SVG length unit: {unit!r}")
    if unit == "":
        return number
    return number * _UNIT_TO_MM[unit] * 96.0 / 25.4


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


def _format_number(value: float) -> str:
    if abs(value) < 1e-12:
        value = 0.0
    return f"{value:.12g}"
