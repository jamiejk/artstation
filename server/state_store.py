"""Runtime JSON persistence helpers.

This module owns filesystem mechanics for local runtime state: JSON load/save,
job metadata paths, atomic writes, and log tails. It deliberately avoids
validation rules, FastAPI exceptions, and job state-transition policy.
"""

from __future__ import annotations

from pathlib import Path
import json


def read_json(path: Path, *, default=None):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: dict, *, atomic: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if atomic:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(path)
        return
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def deep_copy_jsonable(data):
    return json.loads(json.dumps(data))


def job_dir(jobs_dir: Path, job_id: str) -> Path:
    return jobs_dir / job_id


def job_meta_path(jobs_dir: Path, job_id: str) -> Path:
    return job_dir(jobs_dir, job_id) / "job.json"


def save_job(jobs_dir: Path, job_id: str, job: dict) -> None:
    write_json(job_meta_path(jobs_dir, job_id), job, atomic=True)


def delete_job_metadata(jobs_dir: Path, job_id: str) -> None:
    job_meta_path(jobs_dir, job_id).unlink(missing_ok=True)


def iter_job_meta_paths(jobs_dir: Path):
    yield from jobs_dir.glob("*/job.json")


def read_job_meta(path: Path) -> tuple[str, dict]:
    job = read_json(path)
    job_id = job.get("id") or path.parent.name
    return job_id, job


def log_tail(path: Path, max_chars: int = 4000) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")[-max_chars:]
