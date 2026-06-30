"""Small helpers for structured timing lines in job logs."""

from __future__ import annotations

import shlex
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


def _parse_value(value: str):
    if value in {"True", "False"}:
        return value == "True"
    try:
        number = float(value)
    except ValueError:
        return value
    if number.is_integer() and "." not in value:
        return int(number)
    return number


def parse_timing_lines(text: str) -> list[dict]:
    events = []
    for line in text.splitlines():
        if not line.startswith("[timing] "):
            continue
        try:
            parts = shlex.split(line)
        except ValueError:
            continue
        if len(parts) < 3 or parts[0] != "[timing]":
            continue
        event = {"event": parts[1]}
        for token in parts[2:]:
            if "=" not in token:
                continue
            key, value = token.split("=", 1)
            event[key] = _parse_value(value)
        if "elapsed_ms" in event:
            events.append(event)
    return events


def summarize_timing_events(events: list[dict], *, limit: int = 8) -> dict:
    if not events:
        return {"events": [], "slowest": [], "totals": {}, "total_elapsed_ms": 0.0}

    totals = {}
    for event in events:
        name = str(event.get("event") or "")
        elapsed = float(event.get("elapsed_ms") or 0.0)
        bucket = totals.setdefault(name, {"count": 0, "elapsed_ms": 0.0, "max_elapsed_ms": 0.0})
        bucket["count"] += 1
        bucket["elapsed_ms"] = round(bucket["elapsed_ms"] + elapsed, 1)
        bucket["max_elapsed_ms"] = round(max(bucket["max_elapsed_ms"], elapsed), 1)

    slowest = sorted(
        events,
        key=lambda event: float(event.get("elapsed_ms") or 0.0),
        reverse=True,
    )[:limit]
    return {
        "events": events[-limit:],
        "slowest": slowest,
        "totals": totals,
        "total_elapsed_ms": round(sum(float(event.get("elapsed_ms") or 0.0) for event in events), 1),
    }


def summarize_timing_text(text: str, *, limit: int = 8) -> dict:
    return summarize_timing_events(parse_timing_lines(text), limit=limit)
