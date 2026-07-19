"""Small helpers for JSON-backed mutable settings dictionaries."""

from __future__ import annotations

from pathlib import Path
from threading import RLock
from typing import Callable

try:
    from server import state_store
except ImportError:
    import state_store


SettingsNormalizer = Callable[[dict, dict], dict]


def load_settings(
    *,
    path: Path,
    target: dict,
    lock: RLock,
    defaults: dict | Callable[[], dict],
    normalize: SettingsNormalizer | None = None,
) -> None:
    """Reset target to defaults, then merge and validate any saved JSON."""
    with lock:
        default_values = defaults() if callable(defaults) else dict(defaults)
        target.clear()
        target.update(default_values)
        try:
            data = state_store.read_json(path)
            if data is None:
                return
            next_values = normalize(default_values, data) if normalize else {**default_values, **data}
            target.clear()
            target.update(next_values)
        except Exception as exc:
            print(f"Could not load {path}: {exc}", flush=True)


def save_settings_unlocked(path: Path, target: dict) -> None:
    state_store.write_json(path, target)


def current_settings(target: dict, lock: RLock, *, deep: bool = False) -> dict:
    with lock:
        if deep:
            return state_store.deep_copy_jsonable(target)
        return dict(target)
