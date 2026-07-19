import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from server import job_runner


def make_context(jobs):
    updates = []
    ctx = SimpleNamespace()
    ctx.jobs = jobs
    ctx.jobs_lock = mock.MagicMock()
    ctx.jobs_lock.__enter__.return_value = None
    ctx.jobs_lock.__exit__.return_value = None
    ctx.now = mock.Mock(return_value=123.0)
    ctx.log_tail = mock.Mock(return_value="tail")
    ctx.update_job = mock.Mock(side_effect=lambda job_id, **fields: (jobs[job_id].update(fields), updates.append((job_id, fields))))
    ctx.run_layer_with_auto_dips = mock.Mock(return_value="done")
    ctx.return_home = mock.Mock(return_value=0)
    ctx.wait_for_operator = mock.Mock(return_value=True)
    ctx.announce_on_linux_box = mock.Mock()
    recovery_result = {
        "actual_position": {"x_mm": 1.0, "y_mm": 2.0},
        "return_error_mm": 0,
    }
    ctx.execute_dip_cycle = mock.Mock(return_value=recovery_result)
    ctx.return_from_failed_dip_without_loading_ink = mock.Mock(return_value=recovery_result)
    ctx.attempt_dip_clearance_raise = mock.Mock()
    ctx.updates = updates
    return ctx


class JobRunnerTests(unittest.TestCase):
    def test_continue_job_marks_done_after_final_layer(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "job.log"
            log_path.write_text("", encoding="utf-8")
            jobs = {
                "job": {
                    "id": "job",
                    "log_path": str(log_path),
                    "layers": [{"index": 1, "name": "Layer 1"}],
                }
            }
            ctx = make_context(jobs)

            job_runner.continue_job_after_layer(ctx, "job", 0, mock.Mock())

        self.assertEqual(jobs["job"]["status"], "done")
        ctx.run_layer_with_auto_dips.assert_called_once()
        ctx.return_home.assert_called_once()

    def test_resume_paused_job_fails_when_home_return_fails(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "job.log"
            log_path.write_text("", encoding="utf-8")
            jobs = {
                "job": {
                    "id": "job",
                    "status": "paused",
                    "paused_layer": 1,
                    "log_path": str(log_path),
                    "layers": [{"index": 1, "name": "Layer 1"}],
                }
            }
            ctx = make_context(jobs)
            ctx.return_home.return_value = 1

            job_runner.resume_paused_job(ctx, "job")

        self.assertEqual(jobs["job"]["status"], "failed")
        self.assertEqual(jobs["job"]["failed_layer"], 1)
        self.assertIn("walk_home failed", jobs["job"]["error"])

    def test_recover_dip_retry_runs_dip_then_resumes_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "job.log"
            log_path.write_text("", encoding="utf-8")
            jobs = {
                "job": {
                    "id": "job",
                    "status": "dip_failed",
                    "current_layer": 1,
                    "dip_count": 2,
                    "dip_failure": {
                        "layer": 1,
                        "phase": "checkpoint",
                        "return_position": {"x_mm": 1, "y_mm": 2},
                    },
                    "log_path": str(log_path),
                    "layers": [{"index": 1, "name": "Layer 1"}],
                }
            }
            ctx = make_context(jobs)

            job_runner.recover_dip_failed_job(ctx, "job", retry_dip=True)

        ctx.execute_dip_cycle.assert_called_once()
        self.assertEqual(jobs["job"]["dip_count"], 3)
        ctx.run_layer_with_auto_dips.assert_called_once()
        self.assertTrue(ctx.run_layer_with_auto_dips.call_args.kwargs["resume"])


if __name__ == "__main__":
    unittest.main()
