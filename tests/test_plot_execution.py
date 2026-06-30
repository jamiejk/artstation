import tempfile
import unittest
import io
from pathlib import Path
from unittest import mock

from server import plot_execution


class PlotExecutionTests(unittest.TestCase):
    def test_layer_plot_cmd_uses_plot_svg_when_present(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = Path(tmpdir) / "axidraw_servo_conf.py"
            config.write_text("# config\n", encoding="utf-8")
            cmd = plot_execution.layer_plot_cmd(
                {
                    "speed_pendown": 10,
                    "speed_penup": 20,
                    "pen_pos_down": 0,
                    "pen_pos_up": 100,
                    "pen_delay_down": -25,
                    "pen_delay_up": 50,
                    "pen_rate_raise": 90,
                },
                {"plot_svg": "/tmp/prepared.svg", "input_svg": "/tmp/input.svg"},
                axicli="/axicli",
                axicli_config=config,
                plotter_port="/dev/ttyTEST",
                input_svg=Path("/tmp/prepared.svg"),
                output_svg=Path("/tmp/progress.svg"),
            )

        self.assertEqual(cmd[:3], ["/axicli", "--config", str(config)])
        self.assertIn("/tmp/prepared.svg", cmd)
        self.assertIn("--port", cmd)
        self.assertIn("/dev/ttyTEST", cmd)
        self.assertIn("--pen_rate_raise", cmd)
        self.assertIn("90", cmd)

    def test_classifies_programmatic_pause_before_generic_pause(self):
        self.assertEqual(
            plot_execution.classify_axicli_layer_result(0, "Plot paused programmatically\nUse the resume feature"),
            "auto_dip_pause",
        )
        self.assertEqual(plot_execution.classify_axicli_layer_result(0, "Use the resume feature"), "paused")
        self.assertEqual(plot_execution.classify_axicli_layer_result(0, ""), "done")
        self.assertEqual(plot_execution.classify_axicli_layer_result(1, ""), "failed")

    def test_resume_layer_replaces_stable_progress_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            log_path = root / "job.log"
            progress_svg = root / "progress.svg"
            log_path.write_text("", encoding="utf-8")
            progress_svg.write_text("old", encoding="utf-8")
            job = {
                "id": "job",
                "log_path": str(log_path),
                "speed_pendown": 10,
                "speed_penup": 20,
                "pen_pos_down": 0,
                "pen_pos_up": 100,
            }
            layer = {"index": 1, "name": "Layer", "progress_svg": str(progress_svg)}

            def runner(_cmd, _log, *, job_id=None):
                plot_execution.resume_progress_output_path(progress_svg).write_text("new", encoding="utf-8")
                return 0

            log = io.StringIO()
            result = plot_execution.resume_layer(
                job,
                layer,
                log,
                axicli="/axicli",
                axicli_config=root / "missing.conf",
                plotter_port="/dev/ttyTEST",
                run_axicli_command=runner,
            )

            self.assertEqual(result, "done")
            self.assertEqual(progress_svg.read_text(encoding="utf-8"), "new")
            self.assertFalse(plot_execution.resume_progress_output_path(progress_svg).exists())
            self.assertIn("[timing] axicli_layer_resume", log.getvalue())
            self.assertIn("[timing] layer_resume_result", log.getvalue())


if __name__ == "__main__":
    unittest.main()
