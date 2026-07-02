from pathlib import Path
import re
import xml.etree.ElementTree as ET


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
