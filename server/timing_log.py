"""Small helpers for structured timing lines in job logs."""

from __future__ import annotations

import time


def monotonic() -> float:
    return time.monotonic()


def write_timing(log, event: str, start_s: float, **fields) -> dict:
    elapsed_ms = round((time.monotonic() - start_s) * 1000.0, 1)
    parts = [f"[timing] {event}", f"elapsed_ms={elapsed_ms:.1f}"]
    for key, value in fields.items():
        if value is None:
            continue
        parts.append(f"{key}={value}")
    log.write(" ".join(parts) + "\n")
    log.flush()
    return {"event": event, "elapsed_ms": elapsed_ms, **{k: v for k, v in fields.items() if v is not None}}
