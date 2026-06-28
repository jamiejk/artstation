"""Job continuation, resume, and dip-recovery workflows.

The runner module owns multi-step job state transitions. It receives a context
object from ``server.py`` for storage, logging, operator prompts, and hardware
actions. This keeps FastAPI routes and module-level runtime globals out of the
workflow implementation.
"""

from __future__ import annotations

from pathlib import Path


def continue_job_after_layer(ctx, job_id: str, start_layer_number: int, log) -> None:
    with ctx.jobs_lock:
        job = ctx.jobs[job_id]
        layers = job["layers"]

    layer_count = len(layers)
    stop_current_job = False

    for layer in layers[start_layer_number:]:
        layer_number = layer["index"]

        ctx.update_job(
            job_id,
            status="running",
            current_layer=layer_number,
            current_layer_name=layer["name"],
        )

        result = ctx.run_layer_with_auto_dips(job, layer, log)
        if result == "dip_failed":
            stop_current_job = True
            break
        if result == "paused":
            ctx.update_job(
                job_id,
                status="paused",
                paused_layer=layer_number,
                log_tail=ctx.log_tail(Path(job["log_path"])),
            )
            ctx.announce_on_linux_box(
                f"Job {job_id} paused during Layer {layer_number}. Resume handling is needed before continuing."
            )
            stop_current_job = True
            break

        if result != "done":
            ctx.update_job(
                job_id,
                status="failed",
                failed_layer=layer_number,
                finished_at=ctx.now(),
                log_tail=ctx.log_tail(Path(job["log_path"])),
            )
            stop_current_job = True
            break

        ctx.update_job(job_id, last_completed_layer=layer_number, log_tail=ctx.log_tail(Path(job["log_path"])))
        home_return_code = ctx.return_home(log)
        if home_return_code != 0:
            ctx.update_job(
                job_id,
                status="failed",
                failed_layer=layer_number,
                finished_at=ctx.now(),
                error=f"walk_home failed with return code {home_return_code}",
                log_tail=ctx.log_tail(Path(job["log_path"])),
            )
            stop_current_job = True
            break

        if layer_number < layer_count:
            next_layer = layers[layer_number]
            if not ctx.wait_for_operator(
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
        ctx.update_job(
            job_id,
            status="done",
            current_layer=None,
            current_layer_name=None,
            finished_at=ctx.now(),
            log_tail=ctx.log_tail(Path(job["log_path"])),
        )


def resume_paused_job(ctx, job_id: str) -> None:
    with ctx.jobs_lock:
        job = ctx.jobs[job_id]
        paused_layer_number = int(job.get("paused_layer") or job.get("current_layer") or 1)
        layers = job["layers"]
        layer = next((item for item in layers if item["index"] == paused_layer_number), None)

    if layer is None:
        ctx.update_job(job_id, status="failed", error=f"Paused layer not found: {paused_layer_number}", finished_at=ctx.now())
        return

    log_path = Path(job["log_path"])
    try:
        with log_path.open("a", encoding="utf-8", errors="replace") as log:
            ctx.update_job(
                job_id,
                status="running",
                current_layer=paused_layer_number,
                current_layer_name=layer["name"],
                resumed_at=ctx.now(),
            )

            result = ctx.run_layer_with_auto_dips(job, layer, log, resume=True)

            if result == "dip_failed":
                return

            if result == "paused":
                ctx.update_job(
                    job_id,
                    status="paused",
                    paused_layer=paused_layer_number,
                    log_tail=ctx.log_tail(log_path),
                )
                return

            if result != "done":
                ctx.update_job(
                    job_id,
                    status="failed",
                    failed_layer=paused_layer_number,
                    finished_at=ctx.now(),
                    log_tail=ctx.log_tail(log_path),
                )
                return

            ctx.update_job(job_id, last_completed_layer=paused_layer_number, log_tail=ctx.log_tail(log_path))

            home_return_code = ctx.return_home(log)
            if home_return_code != 0:
                ctx.update_job(
                    job_id,
                    status="failed",
                    failed_layer=paused_layer_number,
                    finished_at=ctx.now(),
                    error=f"walk_home failed with return code {home_return_code}",
                    log_tail=ctx.log_tail(log_path),
                )
                return

            continue_job_after_layer(ctx, job_id, paused_layer_number, log)

    except Exception as exc:
        ctx.update_job(
            job_id,
            status="failed",
            finished_at=ctx.now(),
            error=repr(exc),
            log_tail=ctx.log_tail(log_path),
        )
        ctx.announce_on_linux_box(f"Job {job_id} resume failed: {exc!r}")


def recover_dip_failed_job(ctx, job_id: str, *, retry_dip: bool) -> None:
    with ctx.jobs_lock:
        job = ctx.jobs[job_id]
        failure = dict(job.get("dip_failure") or {})
        layer_number = int(failure.get("layer") or job.get("current_layer") or 1)
        layer = next((item for item in job["layers"] if item["index"] == layer_number), None)
    log_path = Path(job["log_path"])

    if layer is None:
        ctx.update_job(job_id, status="failed", error=f"Dip recovery layer not found: {layer_number}")
        return

    try:
        with log_path.open("a", encoding="utf-8", errors="replace") as log:
            return_position = failure.get("return_position")
            if not isinstance(return_position, dict):
                raise RuntimeError("Dip recovery has no verified checkpoint return position")

            ctx.update_job(
                job_id,
                status="dipping",
                operator_message=None,
                dip_recovery="retry" if retry_dip else "skip",
            )
            if retry_dip:
                recovery_result = ctx.execute_dip_cycle(job, log, return_position=return_position)
                ctx.update_job(job_id, dip_count=int(job.get("dip_count", 0)) + 1)
            else:
                recovery_result = ctx.return_from_failed_dip_without_loading_ink(
                    job,
                    log,
                    return_position,
                )
            ctx.update_job(
                job_id,
                last_dip=recovery_result,
                dip_failure=None,
                dip_recovery=None,
                status="running",
                dip_phase=None,
            )

            resume_from_progress = failure.get("phase") == "checkpoint"
            result = ctx.run_layer_with_auto_dips(
                job,
                layer,
                log,
                resume=resume_from_progress,
                perform_initial_dip=False,
            )
            if result in {"dip_failed", "paused"}:
                if result == "paused":
                    ctx.update_job(job_id, status="paused", paused_layer=layer_number)
                return
            if result != "done":
                ctx.update_job(
                    job_id,
                    status="failed",
                    failed_layer=layer_number,
                    finished_at=ctx.now(),
                    log_tail=ctx.log_tail(log_path),
                )
                return

            ctx.update_job(job_id, last_completed_layer=layer_number, log_tail=ctx.log_tail(log_path))
            home_return_code = ctx.return_home(log)
            if home_return_code != 0:
                ctx.update_job(
                    job_id,
                    status="failed",
                    failed_layer=layer_number,
                    finished_at=ctx.now(),
                    error=f"walk_home failed with return code {home_return_code}",
                )
                return
            continue_job_after_layer(ctx, job_id, layer_number, log)
    except Exception as exc:
        with log_path.open("a", encoding="utf-8", errors="replace") as recovery_log:
            ctx.attempt_dip_clearance_raise(job, recovery_log)
        ctx.update_job(
            job_id,
            status="dip_failed",
            operator_message=(
                "Dip recovery failed. Check the machine, then Retry Dip, Skip Dip & Resume, or Cancel."
            ),
            dip_failure={
                **failure,
                "error": repr(exc),
                "created_at": ctx.now(),
            },
            log_tail=ctx.log_tail(log_path),
        )
        ctx.announce_on_linux_box(f"Job {job_id} dip recovery failed: {exc!r}")
