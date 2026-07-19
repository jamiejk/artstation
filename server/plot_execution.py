"""AxiCLI plotting and layer-progress execution helpers.

This module owns the mechanics of building AxiCLI commands, analysing prepared
plot digests, running/resuming one layer, and maintaining stable progress SVGs.
It deliberately does not own job queue state, operator prompts, FastAPI routes,
or hardware recovery policy.
"""

from __future__ import annotations

from pathlib import Path
import subprocess

try:
    import timing_log
except ImportError:
    from server import timing_log

try:
    from ink_dip import (
        estimate_checkpoint_schedule,
        find_keepout_collision,
        parse_plob_polylines,
        write_checkpoint_digest,
    )
except ImportError:
    from server.ink_dip import (
        estimate_checkpoint_schedule,
        find_keepout_collision,
        parse_plob_polylines,
        write_checkpoint_digest,
    )


AXICLI_DIGEST_TIMEOUT_S = 120


def axicli_cmd(axicli: str, axicli_config: Path) -> list[str]:
    cmd = [axicli]
    if axicli_config.exists():
        cmd.extend(["--config", str(axicli_config)])
    return cmd


def generate_plot_digest(input_svg: Path, output_svg: Path, job_settings: dict, *, axicli: str, axicli_config: Path) -> None:
    cmd = axicli_cmd(axicli, axicli_config) + [
        str(input_svg),
        "--digest",
        "2",
        "--output_file",
        str(output_svg),
        "--speed_pendown",
        str(job_settings["speed_pendown"]),
        "--speed_penup",
        str(job_settings["speed_penup"]),
        "--pen_pos_down",
        str(job_settings["pen_pos_down"]),
        "--pen_pos_up",
        str(job_settings["pen_pos_up"]),
    ]
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=AXICLI_DIGEST_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError(
            f"AxiDraw did not produce a plot digest within {AXICLI_DIGEST_TIMEOUT_S}s; "
            f"the process may be stuck or the serial port may be locked"
        ) from exc
    if proc.returncode != 0 or not output_svg.exists():
        raise ValueError(f"AxiDraw could not prepare the plot digest: {proc.stdout[-2000:]}")


def analyse_layer_for_ink_well(
    input_svg: Path,
    digest_svg: Path,
    *,
    job_settings: dict,
    home: dict,
    well: dict,
    axicli: str,
    axicli_config: Path,
    dip_interval_s: float | None = None,
    generate_plot_digest_fn=None,
) -> dict:
    if generate_plot_digest_fn is None:
        generate_plot_digest_fn = lambda source, target, settings: generate_plot_digest(
            source,
            target,
            settings,
            axicli=axicli,
            axicli_config=axicli_config,
        )
    generate_plot_digest_fn(input_svg, digest_svg, job_settings)
    polylines = parse_plob_polylines(digest_svg)
    collision = find_keepout_collision(
        polylines,
        origin_mm=(home["x_mm"], home["y_mm"]),
        centre_mm=(well["centre"]["x_mm"], well["centre"]["y_mm"]),
        radius_mm=well["radius_mm"],
    )
    if collision:
        raise ValueError(
            "Prepared plot intersects the installed ink well keep-out zone "
            f"during {collision['motion']} motion at stroke {collision['stroke']}"
        )

    result = {
        "stroke_count": len(polylines),
        "keepout_clear": True,
    }
    if dip_interval_s is not None:
        result["dip_schedule"] = estimate_checkpoint_schedule(
            polylines,
            speed_pendown=job_settings["speed_pendown"],
            interval_s=dip_interval_s,
        )
    return result


def prepare_auto_dip_layer(layer: dict, analysis: dict, *, write_checkpoint_digest_fn=write_checkpoint_digest) -> None:
    digest_svg = Path(layer["plot_digest_svg"])
    prepared_svg = digest_svg.with_name("auto_dip_plot.svg")
    schedule = analysis["dip_schedule"]
    write_checkpoint_digest_fn(
        digest_svg,
        prepared_svg,
        schedule["checkpoint_after_strokes"],
    )
    layer["plot_svg"] = str(prepared_svg)
    layer["auto_dip_checkpoint_count"] = len(schedule["checkpoint_after_strokes"])


def layer_plot_cmd(
    job: dict,
    layer: dict,
    *,
    axicli: str,
    axicli_config: Path,
    plotter_port: str,
    input_svg: Path,
    output_svg: Path,
    mode: str | None = None,
) -> list[str]:
    cmd = axicli_cmd(axicli, axicli_config) + [str(input_svg)]
    if mode:
        cmd.extend(["--mode", mode])
    cmd.extend(
        [
            "--port",
            plotter_port,
            "-o",
            str(output_svg),
            "--speed_pendown",
            str(job["speed_pendown"]),
            "--speed_penup",
            str(job["speed_penup"]),
            "--pen_pos_down",
            str(job["pen_pos_down"]),
            "--pen_pos_up",
            str(job["pen_pos_up"]),
            "--pen_delay_down",
            str(job.get("pen_delay_down", 0)),
            "--pen_delay_up",
            str(job.get("pen_delay_up", 0)),
            "--pen_rate_raise",
            str(job.get("pen_rate_raise", 75)),
        ]
    )
    return cmd


def classify_axicli_layer_result(returncode: int, log_text: str) -> str:
    if "Plot paused programmatically" in log_text:
        return "auto_dip_pause"
    if "Plot paused" in log_text or "Use the resume feature" in log_text:
        return "paused"
    if returncode == 0:
        return "done"
    return "failed"


def resume_progress_output_path(progress_svg: Path) -> Path:
    return progress_svg.with_name(f"{progress_svg.stem}.next{progress_svg.suffix}")


def finalize_resume_progress(progress_svg: Path, resumed_svg: Path) -> bool:
    if not resumed_svg.exists():
        return False
    resumed_svg.replace(progress_svg)
    return True


def run_layer(
    job: dict,
    layer: dict,
    log,
    *,
    axicli: str,
    axicli_config: Path,
    plotter_port: str,
    run_axicli_command,
) -> str:
    cmd = layer_plot_cmd(
        job,
        layer,
        axicli=axicli,
        axicli_config=axicli_config,
        plotter_port=plotter_port,
        input_svg=Path(layer.get("plot_svg") or layer["input_svg"]),
        output_svg=Path(layer["progress_svg"]),
    )

    log.write("\nPlotting layer:\n")
    log.write(f"Layer {layer['index']}: {layer['name']}\n")
    log.write(" ".join(cmd) + "\n\n")
    log.flush()
    log_start = Path(job["log_path"]).stat().st_size

    timing_start = timing_log.monotonic()
    returncode = run_axicli_command(cmd, log, job_id=job["id"])
    timing = timing_log.write_timing(
        log,
        "axicli_layer_plot",
        timing_start,
        job_id=job.get("id"),
        layer=layer.get("index"),
        returncode=returncode,
    )
    log.flush()
    text = Path(job["log_path"]).read_text(encoding="utf-8", errors="replace")[log_start:]
    result = classify_axicli_layer_result(returncode, text)
    timing_log.write_timing(
        log,
        "layer_plot_result",
        timing_start,
        job_id=job.get("id"),
        layer=layer.get("index"),
        result=result,
        axicli_elapsed_ms=timing["elapsed_ms"],
    )
    return result


def resume_layer(
    job: dict,
    layer: dict,
    log,
    *,
    axicli: str,
    axicli_config: Path,
    plotter_port: str,
    run_axicli_command,
) -> str:
    progress_svg = Path(layer["progress_svg"])
    if not progress_svg.exists():
        raise FileNotFoundError(f"Progress SVG not found: {progress_svg}")

    resumed_svg = resume_progress_output_path(progress_svg)
    resumed_svg.unlink(missing_ok=True)
    cmd = layer_plot_cmd(
        job,
        layer,
        axicli=axicli,
        axicli_config=axicli_config,
        plotter_port=plotter_port,
        input_svg=progress_svg,
        output_svg=resumed_svg,
        mode="res_plot",
    )

    log.write("\nResuming layer:\n")
    log.write(f"Layer {layer['index']}: {layer['name']}\n")
    log.write(" ".join(cmd) + "\n\n")
    log.flush()
    resume_log_start = Path(job["log_path"]).stat().st_size

    timing_start = timing_log.monotonic()
    returncode = run_axicli_command(cmd, log, job_id=job["id"])
    timing = timing_log.write_timing(
        log,
        "axicli_layer_resume",
        timing_start,
        job_id=job.get("id"),
        layer=layer.get("index"),
        returncode=returncode,
    )
    finalize_resume_progress(progress_svg, resumed_svg)

    log.flush()
    text = Path(job["log_path"]).read_text(encoding="utf-8", errors="replace")[resume_log_start:]
    result = classify_axicli_layer_result(returncode, text)
    timing_log.write_timing(
        log,
        "layer_resume_result",
        timing_start,
        job_id=job.get("id"),
        layer=layer.get("index"),
        result=result,
        axicli_elapsed_ms=timing["elapsed_ms"],
    )
    return result
