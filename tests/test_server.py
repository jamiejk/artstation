import importlib
import os
import tempfile
import unittest
from unittest import mock

from fastapi import HTTPException


_home = tempfile.TemporaryDirectory()
os.environ["HOME"] = _home.name
os.environ["PLOTTER_TOKEN"] = "test-token"
os.environ["PLOTTER_DISABLE_WORKER"] = "1"
server = importlib.import_module("server.server")


class SvgValidationTests(unittest.TestCase):
    def test_accepts_svg_inside_bounds(self):
        metrics = server.validate_svg_text(
            '<svg xmlns="http://www.w3.org/2000/svg" width="100mm" height="200mm"/>',
            max_width_mm=110,
            max_height_mm=210,
        )
        self.assertEqual(metrics, {"width_mm": 100.0, "height_mm": 200.0})

    def test_rejects_svg_outside_bounds(self):
        with self.assertRaisesRegex(ValueError, "exceeds plotter bounds"):
            server.validate_svg_text(
                '<svg xmlns="http://www.w3.org/2000/svg" width="111mm" height="200mm"/>',
                max_width_mm=110,
                max_height_mm=210,
            )


class MotionSafetyTests(unittest.TestCase):
    def test_accepts_bed_edges(self):
        self.assertEqual(
            server.validate_bed_target(server.BED_WIDTH_MM, server.BED_HEIGHT_MM),
            (server.BED_WIDTH_MM, server.BED_HEIGHT_MM),
        )

    def test_rejects_out_of_bounds_and_non_finite_targets(self):
        for target in [(-0.01, 1), (1, server.BED_HEIGHT_MM + 0.01), (float("nan"), 1)]:
            with self.subTest(target=target), self.assertRaises(HTTPException) as raised:
                server.validate_bed_target(*target)
            self.assertEqual(raised.exception.status_code, 400)

    def test_motor_state_check_does_not_send_enable_command(self):
        replies = [
            ("PI,0", "OK"),
            ("PI,0", "OK"),
            ("PI,1", "OK"),
            ("PI,1", "OK"),
            ("PI,1", "OK"),
        ]
        with mock.patch.object(server, "serial_query", side_effect=replies) as query:
            server.require_enabled_high_resolution_motors(object())
        self.assertEqual(query.call_count, 5)


class PlotSettingsTests(unittest.TestCase):
    def test_validates_pen_positions(self):
        self.assertEqual(server.validate_pen_position(0, "pen_pos_down"), 0)
        self.assertEqual(server.validate_pen_position(100, "pen_pos_up"), 100)
        for value in (-1, 101):
            with self.subTest(value=value), self.assertRaises(HTTPException):
                server.validate_pen_position(value, "pen_pos_down")

    def test_accepts_axidraw_pen_down_delay_range(self):
        self.assertEqual(server.validate_pen_delay_down(-500), -500)
        self.assertEqual(server.validate_pen_delay_down(0), 0)
        self.assertEqual(server.validate_pen_delay_down(500), 500)

    def test_rejects_pen_down_delay_outside_axidraw_range(self):
        for value in (-501, 501):
            with self.subTest(value=value), self.assertRaises(HTTPException) as raised:
                server.validate_pen_delay_down(value)
            self.assertEqual(raised.exception.status_code, 400)

    def test_validates_pen_up_timing(self):
        self.assertEqual(server.validate_pen_delay_up(-500), -500)
        self.assertEqual(server.validate_pen_delay_up(500), 500)
        self.assertEqual(server.validate_pen_rate_raise(1), 1)
        self.assertEqual(server.validate_pen_rate_raise(100), 100)

        for value in (-501, 501):
            with self.subTest(delay=value), self.assertRaises(HTTPException):
                server.validate_pen_delay_up(value)
        for value in (0, 101):
            with self.subTest(rate=value), self.assertRaises(HTTPException):
                server.validate_pen_rate_raise(value)

    def test_current_plot_settings_returns_a_copy(self):
        current = server.current_plot_settings()
        current["pen_delay_down"] = -50
        self.assertNotEqual(server.current_plot_settings()["pen_delay_down"], -50)

    def test_current_plot_settings_can_be_applied_to_paused_job(self):
        job = {"pen_pos_down": 35, "pen_pos_up": 60, "pen_delay_up": 0}
        settings = {
            "speed_pendown": 20,
            "speed_penup": 40,
            "pen_delay_down": -50,
            "pen_delay_up": -25,
            "pen_rate_raise": 90,
        }

        server.apply_plot_settings_to_job(job, settings)

        for key, value in settings.items():
            self.assertEqual(job[key], value)
        self.assertEqual(job["pen_pos_down"], 35)
        self.assertEqual(job["pen_pos_up"], 60)

    def test_current_pen_settings_can_be_applied_to_paused_job(self):
        job = {"pen_pos_down": 35, "pen_pos_up": 60, "pen_delay_down": -50}

        server.apply_pen_settings_to_job(job, {"pen_pos_down": 30, "pen_pos_up": 60})

        self.assertEqual(job["pen_pos_down"], 30)
        self.assertEqual(job["pen_pos_up"], 60)
        self.assertEqual(job["pen_delay_down"], -50)


class HomePositionTests(unittest.TestCase):
    def setUp(self):
        with server.position_lock:
            server.position_current = None
            server.home_position = None

    def test_home_is_independent_from_current_bed_position(self):
        with server.position_lock:
            server.set_current_position_unlocked(0, server.BED_HEIGHT_MM)
            server.set_home_position_unlocked(0, server.BED_HEIGHT_MM)
            server.set_current_position_unlocked(125, 300)

        self.assertEqual(server.current_home_position(), {"x_mm": 0.0, "y_mm": server.BED_HEIGHT_MM})
        self.assertEqual(server.current_software_position(), {"x_mm": 125.0, "y_mm": 300.0})

    def test_user_can_replace_home_without_changing_current_position(self):
        with server.position_lock:
            server.set_current_position_unlocked(125, 300)
            server.set_home_position_unlocked(125, 300)

        self.assertEqual(server.current_home_position(), {"x_mm": 125.0, "y_mm": 300.0})
        self.assertEqual(server.current_software_position(), {"x_mm": 125.0, "y_mm": 300.0})

    def test_hardware_steps_override_stale_cached_position(self):
        with server.position_lock:
            server.position_offset = {"x_mm": 10.0, "y_mm": 20.0}
            server.set_current_position_unlocked(500, 600)

        position = server.current_position_estimate({"x_mm": -300.0, "y_mm": -125.0})
        self.assertEqual(position, {"x_mm": 135.0, "y_mm": 320.0})


class CancellationTests(unittest.TestCase):
    def setUp(self):
        server.jobs.clear()
        server.operator_event.clear()
        server.operator_prompt.update(
            {"active": True, "job_id": "active-job", "message": "Ready", "created_at": 1}
        )
        server.jobs.update(
            {
                "active-job": {"id": "active-job", "status": "waiting_for_operator"},
                "queued-job": {"id": "queued-job", "status": "queued"},
            }
        )

    def test_cancelling_other_job_does_not_release_active_prompt(self):
        server.cancel_job("queued-job", "test-token")
        self.assertFalse(server.operator_event.is_set())

    def test_cancelling_prompt_owner_releases_wait(self):
        server.cancel_job("active-job", "test-token")
        self.assertTrue(server.operator_event.is_set())


class OperatorPromptTests(unittest.TestCase):
    def test_start_action_is_published_for_initial_confirmation(self):
        captured_prompt = {}
        event = mock.Mock()
        event.wait.side_effect = lambda: captured_prompt.update(server.operator_prompt)
        server.jobs["new-job"] = {"id": "new-job", "status": "queued"}

        with (
            mock.patch.object(server, "operator_event", event),
            mock.patch.object(server, "announce_on_linux_box"),
            mock.patch.object(server, "save_job_unlocked"),
        ):
            self.assertTrue(server.wait_for_operator("new-job", "Ready", action="start"))

        self.assertEqual(captured_prompt["action"], "start")
        server.jobs.pop("new-job", None)


if __name__ == "__main__":
    unittest.main()
