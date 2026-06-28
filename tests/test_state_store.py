import tempfile
import unittest
from pathlib import Path

from server import state_store


class StateStoreTests(unittest.TestCase):
    def test_write_and_read_json_atomically(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "state.json"

            state_store.write_json(path, {"ok": True, "value": 3})

            self.assertEqual(state_store.read_json(path), {"ok": True, "value": 3})
            self.assertFalse(path.with_suffix(".json.tmp").exists())

    def test_read_json_returns_default_for_missing_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self.assertEqual(state_store.read_json(Path(tmpdir) / "missing.json", default={}), {})

    def test_job_metadata_paths_and_load(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            jobs_dir = Path(tmpdir) / "jobs"

            state_store.save_job(jobs_dir, "abc123", {"id": "abc123", "status": "queued"})

            paths = list(state_store.iter_job_meta_paths(jobs_dir))
            self.assertEqual(paths, [jobs_dir / "abc123" / "job.json"])
            job_id, job = state_store.read_job_meta(paths[0])
            self.assertEqual(job_id, "abc123")
            self.assertEqual(job["status"], "queued")

    def test_log_tail_handles_missing_and_truncates(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            missing = Path(tmpdir) / "missing.log"
            log = Path(tmpdir) / "job.log"
            log.write_text("abcdef", encoding="utf-8")

            self.assertEqual(state_store.log_tail(missing), "")
            self.assertEqual(state_store.log_tail(log, max_chars=3), "def")


if __name__ == "__main__":
    unittest.main()
