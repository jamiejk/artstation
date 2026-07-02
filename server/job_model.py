import math
from collections.abc import Callable


def plot_footprint(job: dict) -> dict | None:
    widths = []
    heights = []
    for layer in job.get("layers") or []:
        metrics = layer.get("svg_metrics") or {}
        try:
            width = float(metrics.get("width_mm"))
            height = float(metrics.get("height_mm"))
        except (TypeError, ValueError):
            continue
        if math.isfinite(width) and math.isfinite(height) and width > 0 and height > 0:
            widths.append(width)
            heights.append(height)
    if not widths or not heights:
        return None
    return {
        "width_mm": round(max(widths), 4),
        "height_mm": round(max(heights), 4),
    }


def plot_origin_for_paper(
    job: dict,
    paper: dict,
    *,
    validate_bed_target: Callable[[float, float], tuple[float, float]],
) -> dict | None:
    if not paper.get("enabled", True):
        return None
    top_right = paper.get("top_right")
    footprint = plot_footprint(job)
    if not top_right or not footprint:
        return None

    plot_width = float(footprint["width_mm"])
    plot_height = float(footprint["height_mm"])
    paper_width = float(paper["width_mm"])
    paper_height = float(paper["height_mm"])
    if plot_width > paper_width or plot_height > paper_height:
        raise ValueError(
            f"Plot footprint {plot_width:.1f}×{plot_height:.1f} mm exceeds "
            f"{paper.get('size', 'paper')} {paper.get('orientation', '')} "
            f"{paper_width:.1f}×{paper_height:.1f} mm"
        )

    x_mm = float(top_right["x_mm"]) - plot_width
    y_mm = float(top_right["y_mm"])
    validate_bed_target(x_mm, y_mm)
    validate_bed_target(float(top_right["x_mm"]), y_mm - plot_height)
    return {
        "x_mm": round(x_mm, 4),
        "y_mm": round(y_mm, 4),
        "anchor": "paper_top_right",
        "paper_top_right": dict(top_right),
        "plot_footprint": footprint,
    }


def layer_dip_estimates(layers: list[dict]) -> list[dict]:
    return [
        layer["ink_analysis"]["dip_schedule"]
        for layer in layers
        if isinstance(layer.get("ink_analysis"), dict)
        and layer["ink_analysis"].get("dip_schedule")
    ]
