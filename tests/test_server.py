import importlib
import asyncio
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from fastapi import HTTPException


_home = tempfile.TemporaryDirectory()
os.environ["HOME"] = _home.name
os.environ["PLOTTER_TOKEN"] = "test-token"
os.environ["PLOTTER_DISABLE_WORKER"] = "1"
server = importlib.import_module("server.server")
ink_dip = importlib.import_module("server.ink_dip")


class RouteContractTests(unittest.TestCase):
    def test_public_application_routes_are_stable(self):
        builtin_paths = {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}
        routes = sorted(
            (tuple(sorted(route.methods or [])), route.path)
            for route in server.app.routes
            if getattr(route, "methods", None) and route.path not in builtin_paths
        )

        self.assertEqual(
            routes,
            [
                (("GET",), "/control"),
                (("GET",), "/control/config"),
                (("GET",), "/health"),
                (("GET",), "/jobs"),
                (("GET",), "/jobs/{job_id}"),
                (("GET",), "/jobs/{job_id}/layers/{layer_index}/preview.svg"),
                (("GET",), "/jobs/{job_id}/log"),
                (("GET",), "/operator/next"),
                (("GET",), "/plotter/ink_well"),
                (("GET",), "/plotter/paper"),
                (("GET",), "/plotter/state"),
                (("POST",), "/jobs/clear"),
                (("POST",), "/jobs/{job_id}/auto_dip"),
                (("POST",), "/jobs/{job_id}/cancel"),
                (("POST",), "/jobs/{job_id}/delete"),
                (("POST",), "/jobs/{job_id}/dip_interval"),
                (("POST",), "/jobs/{job_id}/dip_now"),
                (("POST",), "/jobs/{job_id}/dip_recovery"),
                (("POST",), "/jobs/{job_id}/pause"),
                (("POST",), "/jobs/{job_id}/rerun"),
                (("POST",), "/jobs/{job_id}/resume"),
                (("POST",), "/operator/continue"),
                (("POST",), "/plot/layers"),
                (("POST",), "/plotter/home/return"),
                (("POST",), "/plotter/home/set"),
                (("POST",), "/plotter/ink_well"),
                (("POST",), "/plotter/ink_well/confirm_test"),
                (("POST",), "/plotter/ink_well/test"),
                (("POST",), "/plotter/jog"),
                (("POST",), "/plotter/motors"),
                (("POST",), "/plotter/move"),
                (("POST",), "/plotter/move_to"),
                (("POST",), "/plotter/paper"),
                (("POST",), "/plotter/pen"),
                (("POST",), "/plotter/pen/calibrate"),
                (("POST",), "/plotter/pen/jog"),
                (("POST",), "/plotter/pen/seat"),
                (("POST",), "/plotter/pen_settings"),
                (("POST",), "/plotter/plot_settings"),
                (("POST",), "/plotter/position/calibration"),
                (("POST",), "/plotter/position/set"),
            ],
        )


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
    def setUp(self):
        server.invalidate_motor_resolution_cache()

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
        with mock.patch.object(server.hardware, "serial_query", side_effect=replies) as query:
            server.require_enabled_high_resolution_motors(object())
        self.assertEqual(query.call_count, 5)

    def test_motor_state_cache_skips_repeated_resolution_reads(self):
        replies = [
            ("PI,0", "OK"),
            ("PI,0", "OK"),
            ("PI,1", "OK"),
            ("PI,1", "OK"),
            ("PI,1", "OK"),
        ]
        with mock.patch.object(server.hardware, "serial_query", side_effect=replies) as query:
            server.require_cached_high_resolution_motors(object())
            server.require_cached_high_resolution_motors(object())
        self.assertEqual(query.call_count, 5)

    def test_read_hardware_state_returns_cache_without_serial_access(self):
        with server.hardware_state_lock:
            server.cached_hardware_state.clear()
            server.cached_hardware_state.update(
                {
                    "busy": False,
                    "connected": True,
                    "port": "/dev/test",
                    "telemetry_stale": False,
                    "telemetry_updated_at": server.now(),
                }
            )
        with mock.patch.object(server.serial, "Serial", side_effect=AssertionError("serial access")):
            state = server.read_hardware_state()

        self.assertTrue(state["connected"])
        self.assertEqual(state["port"], "/dev/test")

    def test_active_plot_marks_telemetry_busy_without_reading_serial(self):
        with (
            mock.patch.object(server, "active_process", object()),
            mock.patch.object(server, "manual_hardware_priority_active", return_value=False),
            mock.patch.object(server.serial, "Serial", side_effect=AssertionError("serial access")),
        ):
            updated = server.poll_hardware_state_once()

        self.assertTrue(updated)
        with server.hardware_state_lock:
            self.assertTrue(server.cached_hardware_state["busy"])
            self.assertTrue(server.cached_hardware_state["telemetry_stale"])

    def test_motor_pin_telemetry_decodes_enable_state(self):
        self.assertTrue(server.motor_enabled_from_raw_pins({"axis_1": "PI,0", "axis_2": "PI,0"}))
        self.assertFalse(server.motor_enabled_from_raw_pins({"axis_1": "PI,0", "axis_2": "PI,1"}))
        self.assertIsNone(server.motor_enabled_from_raw_pins({"axis_1": "", "axis_2": ""}))

    def test_external_motor_disable_invalidates_saved_cursor(self):
        with server.position_lock:
            server.position_bed_calibrated = True
            server.position_current = {"x_mm": 10.0, "y_mm": 20.0}
            server.home_position = {"x_mm": 10.0, "y_mm": 20.0}

        with mock.patch.object(server, "save_position_offset_unlocked"):
            changed = server.invalidate_position_if_motors_disabled(
                {"axis_1": "PI,0", "axis_2": "PI,1"}
            )

        self.assertTrue(changed)
        self.assertIsNone(server.position_current)
        self.assertIsNone(server.home_position)
        self.assertEqual(server.position_reference_reason, "motors_disabled")

    def test_motor_disable_uses_direct_ebb_command(self):
        request = mock.Mock()
        request.client.host = "127.0.0.1"
        with server.position_lock:
            server.set_current_position_unlocked(10, 20)
            server.set_home_position_unlocked(10, 20)

        with (
            mock.patch.object(server.serial, "Serial") as serial_cls,
            mock.patch.object(server, "raw_command", return_value="OK") as raw,
            mock.patch.object(server, "run_manual_command") as manual,
        ):
            serial_port = serial_cls.return_value.__enter__.return_value
            result = server.plotter_motors(
                request,
                {"enabled": False},
                x_plotter_token="test-token",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["method"], "direct_ebb")
        self.assertTrue(result["position_invalidated"])
        raw.assert_called_once_with(serial_port, "EM,0,0\r")
        manual.assert_not_called()

    def test_motor_enable_uses_direct_ebb_command_when_needed(self):
        request = mock.Mock()
        request.client.host = "127.0.0.1"

        with (
            mock.patch.object(server.serial, "Serial") as serial_cls,
            mock.patch.object(server, "read_motor_resolution", return_value=(0, 0)) as resolution,
            mock.patch.object(server, "raw_command", return_value="OK") as raw,
            mock.patch.object(server, "run_manual_command") as manual,
        ):
            serial_port = serial_cls.return_value.__enter__.return_value
            result = server.plotter_motors(
                request,
                {"enabled": True},
                x_plotter_token="test-token",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["method"], "direct_ebb")
        self.assertFalse(result["position_invalidated"])
        resolution.assert_called_once_with(serial_port)
        raw.assert_called_once_with(serial_port, "EM,1,1\r")
        manual.assert_not_called()


class PlotPositionLifecycleTests(unittest.TestCase):
    def test_axicli_exit_reconciles_cursor_from_controller_steps(self):
        with server.position_lock:
            server.position_calibration_enabled = True
            server.position_bed_calibrated = True
            server.position_offset = {"x_mm": 10.0, "y_mm": 20.0}
            server.position_current = {"x_mm": 500.0, "y_mm": 600.0}

        with (
            mock.patch.object(server.serial, "Serial"),
            mock.patch.object(
                server,
                "read_step_position",
                return_value=(1, 2, {"x_mm": -300.0, "y_mm": -125.0}),
            ),
            mock.patch.object(server, "save_position_offset_unlocked"),
        ):
            actual = server.reconcile_position_after_axicli("job-1", io.StringIO())

        self.assertEqual(actual, {"x_mm": 135.0, "y_mm": 320.0})
        self.assertEqual(server.current_software_position(), actual)
        self.assertEqual(server.position_reference_reason, "plotter_reconciled")

    def test_axicli_reconcile_failure_invalidates_position_reference(self):
        with (
            mock.patch.object(
                server.serial,
                "Serial",
                side_effect=server.serial.SerialException("port unavailable"),
            ),
            mock.patch.object(server.time, "sleep"),
            mock.patch.object(server, "invalidate_position_reference_unlocked") as invalidate,
        ):
            actual = server.reconcile_position_after_axicli("job-1", io.StringIO())

        self.assertIsNone(actual)
        invalidate.assert_called_once_with("plot_position_unverified")

    def test_run_axicli_reconciles_after_process_releases_serial(self):
        process = mock.Mock()
        process.wait.return_value = 0
        with (
            mock.patch.object(server.subprocess, "Popen", return_value=process),
            mock.patch.object(server, "reconcile_position_after_axicli") as reconcile,
        ):
            result = server.run_axicli_command(["axicli", "plot.svg"], io.StringIO(), job_id="job-1")

        self.assertEqual(result, 0)
        reconcile.assert_called_once()
        self.assertIsNone(server.active_process)


class PaperSettingsTests(unittest.TestCase):
    def test_paper_dimensions_follow_orientation(self):
        portrait = server.validate_paper_settings({"size": "A3", "orientation": "portrait"})
        landscape = server.validate_paper_settings({"size": "A3", "orientation": "landscape"})

        self.assertEqual(portrait["width_mm"], 297.0)
        self.assertEqual(portrait["height_mm"], 420.0)
        self.assertEqual(landscape["width_mm"], 420.0)
        self.assertEqual(landscape["height_mm"], 297.0)

    def test_paper_top_right_must_be_on_bed(self):
        settings = server.validate_paper_settings(
            {
                "size": "A4",
                "orientation": "portrait",
                "top_right": {"x_mm": server.BED_WIDTH_MM, "y_mm": server.BED_HEIGHT_MM},
            }
        )
        self.assertEqual(
            settings["top_right"],
            {"x_mm": server.BED_WIDTH_MM, "y_mm": server.BED_HEIGHT_MM},
        )

        with self.assertRaises(HTTPException):
            server.validate_paper_settings(
                {
                    "size": "A4",
                    "orientation": "portrait",
                    "top_right": {"x_mm": server.BED_WIDTH_MM + 1, "y_mm": server.BED_HEIGHT_MM},
                }
            )

    def test_job_plot_origin_aligns_svg_right_edge_to_paper_top_right(self):
        job = {
            "layers": [
                {
                    "svg_metrics": {
                        "width_mm": 291.3,
                        "height_mm": 255.522,
                    }
                }
            ]
        }
        paper = server.validate_paper_settings(
            {
                "size": "A3",
                "orientation": "portrait",
                "top_right": {"x_mm": 502.6125, "y_mm": 725.15},
            }
        )

        origin = server.job_plot_origin_for_paper(job, paper)

        self.assertEqual(origin["x_mm"], 211.3125)
        self.assertEqual(origin["y_mm"], 725.15)
        self.assertEqual(origin["anchor"], "paper_top_right")

    def test_disabled_paper_keeps_settings_but_does_not_align_job(self):
        job = {
            "layers": [
                {
                    "svg_metrics": {
                        "width_mm": 291.3,
                        "height_mm": 255.522,
                    }
                }
            ]
        }
        paper = server.validate_paper_settings(
            {
                "enabled": False,
                "size": "A3",
                "orientation": "portrait",
                "top_right": {"x_mm": 502.6125, "y_mm": 725.15},
            }
        )

        self.assertFalse(paper["enabled"])
        self.assertEqual(paper["top_right"], {"x_mm": 502.6125, "y_mm": 725.15})
        self.assertIsNone(server.job_plot_origin_for_paper(job, paper))

    def test_job_plot_origin_rejects_plot_larger_than_paper(self):
        job = {
            "layers": [
                {
                    "svg_metrics": {
                        "width_mm": 300,
                        "height_mm": 200,
                    }
                }
            ]
        }
        paper = server.validate_paper_settings(
            {
                "size": "A4",
                "orientation": "portrait",
                "top_right": {"x_mm": 400, "y_mm": 500},
            }
        )

        with self.assertRaisesRegex(ValueError, "exceeds"):
            server.job_plot_origin_for_paper(job, paper)

    def test_layer_dip_estimates_ignores_layers_without_ink_analysis(self):
        layers = [
            {"ink_analysis": None},
            {},
            {"ink_analysis": {"dip_schedule": {"estimated_dip_count_per_layer": 2}}},
        ]

        self.assertEqual(
            server.layer_dip_estimates(layers),
            [{"estimated_dip_count_per_layer": 2}],
        )

    def test_layer_plot_origin_uses_paper_top_right(self):
        paper = server.validate_paper_settings(
            {
                "size": "A3",
                "orientation": "portrait",
                "top_right": {"x_mm": 453.19375, "y_mm": 723.40625},
            }
        )

        origin = server.plot_origin_for_layer_metrics(
            {"width_mm": 291.3, "height_mm": 255.522},
            paper,
        )

        self.assertEqual(origin["x_mm"], 161.8938)
        self.assertEqual(origin["y_mm"], 723.4062)


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

    def test_validates_soft_out_distance(self):
        self.assertEqual(server.validate_soft_out_mm(0), 0.0)
        self.assertEqual(server.validate_soft_out_mm("2.5"), 2.5)
        for value in (-0.1, 50.1, "bad", float("nan")):
            with self.subTest(value=value), self.assertRaises(HTTPException):
                server.validate_soft_out_mm(value)

    def test_soft_out_selects_vendored_axicli_only_when_enabled(self):
        self.assertEqual(server.axicli_for_job({"soft_out_mm": 0}), server.AXICLI)
        self.assertEqual(server.axicli_for_job({"soft_out_mm": 2}), server.AXICLI_SOFT_OUT)
        self.assertEqual(
            server.axicli_for_job({"soft_out_mm": 0, "gradual_ramp_mm": 4.375}),
            server.AXICLI_SOFT_OUT,
        )

    def test_current_plot_settings_returns_a_copy(self):
        current = server.current_plot_settings()
        current["pen_delay_down"] = -50
        self.assertNotEqual(server.current_plot_settings()["pen_delay_down"], -50)

    def test_auto_dip_is_saved_as_server_plot_setting(self):
        request = mock.Mock()
        request.client.host = "127.0.0.1"
        original = server.current_plot_settings()

        try:
            result = server.plotter_plot_settings(
                request,
                {"auto_dip_enabled": True, "dip_interval_s": 45},
                x_plotter_token="test-token",
            )
        finally:
            with server.plot_settings_lock:
                server.plot_settings.update(original)

        self.assertTrue(result["plot_settings"]["auto_dip_enabled"])
        self.assertEqual(result["plot_settings"]["dip_interval_s"], 45.0)

    def test_auto_dip_setting_does_not_treat_false_string_as_enabled(self):
        request = mock.Mock()
        request.client.host = "127.0.0.1"
        original = server.current_plot_settings()

        try:
            result = server.plotter_plot_settings(
                request,
                {"auto_dip_enabled": "false"},
                x_plotter_token="test-token",
            )
        finally:
            with server.plot_settings_lock:
                server.plot_settings.update(original)

        self.assertFalse(result["plot_settings"]["auto_dip_enabled"])

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

        server.apply_pen_settings_to_job(
            job,
            {
                "pen_pos_down": 30,
                "pen_pos_up": 60,
                "pen_profile_id": "staedtler_marsmatic",
            },
        )

        self.assertEqual(job["pen_pos_down"], 30)
        self.assertEqual(job["pen_pos_up"], 60)
        self.assertEqual(job["pen_delay_down"], -50)
        self.assertEqual(job["pen_profile_name"], "Staedtler Marsmatic")
        self.assertEqual(job["gradual_ramp_mm"], 4.375)
        self.assertEqual(job["gradual_exit_ramp_mm"], 4.6875)

    def test_plotter_pen_uses_direct_ebb_before_axicli(self):
        request = mock.Mock()
        request.client.host = "127.0.0.1"
        serial_port = object()

        with (
            mock.patch.object(server, "require_hardware_idle"),
            mock.patch.object(server.serial, "Serial") as serial_cls,
            mock.patch.object(
                server,
                "_run_pen_servo_on_port_locked",
                return_value={"ok": True, "method": "direct_ebb", "position": "down"},
            ) as direct,
            mock.patch.object(server, "run_axicli_pen_manual") as axicli,
        ):
            serial_cls.return_value.__enter__.return_value = serial_port
            result = server.plotter_pen(
                request,
                {"position": "down", "pen_pos_down": 0, "pen_pos_up": 100},
                x_plotter_token="test-token",
            )

        self.assertEqual(result["method"], "direct_ebb")
        direct.assert_called_once()
        self.assertIs(direct.call_args.args[0], serial_port)
        axicli.assert_not_called()

    def test_plotter_pen_falls_back_to_axicli_when_direct_ebb_fails(self):
        request = mock.Mock()
        request.client.host = "127.0.0.1"

        with (
            mock.patch.object(server, "require_hardware_idle"),
            mock.patch.object(server.serial, "Serial", side_effect=server.serial.SerialException("no serial")),
            mock.patch.object(
                server,
                "run_axicli_pen_manual",
                return_value={"ok": True, "method": "axicli_manual", "position": "up"},
            ) as axicli,
        ):
            result = server.plotter_pen(
                request,
                {"position": "up", "pen_pos_down": 0, "pen_pos_up": 100},
                x_plotter_token="test-token",
            )

        self.assertEqual(result["method"], "axicli_manual")
        self.assertEqual(result["fallback_from"], "direct_ebb")
        self.assertIn("no serial", result["direct_error"])
        axicli.assert_called_once()

    def test_resume_progress_replaces_stable_progress_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            progress_svg = Path(tmpdir) / "progress.svg"
            resumed_svg = server.resume_progress_output_path(progress_svg)
            progress_svg.write_text("old", encoding="utf-8")
            resumed_svg.write_text("new", encoding="utf-8")

            changed = server.finalize_resume_progress(progress_svg, resumed_svg)

            self.assertTrue(changed)
            self.assertEqual(progress_svg.read_text(encoding="utf-8"), "new")
            self.assertFalse(resumed_svg.exists())

    def test_resume_progress_does_not_create_timestamped_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            progress_svg = Path(tmpdir) / "progress.svg"
            log_path = Path(tmpdir) / "job.log"
            progress_svg.write_text("old", encoding="utf-8")
            log_path.write_text("", encoding="utf-8")
            layer = {"index": 1, "name": "Layer", "progress_svg": str(progress_svg)}
            job = {
                "id": "job",
                "log_path": str(log_path),
                "speed_pendown": 10,
                "speed_penup": 20,
                "pen_pos_down": 0,
                "pen_pos_up": 100,
            }

            def fake_axicli(_cmd, _log, *, job_id=None):
                Path(server.resume_progress_output_path(progress_svg)).write_text("new", encoding="utf-8")
                return 0

            with mock.patch.object(server, "run_axicli_command", side_effect=fake_axicli):
                result = server.resume_layer(job, layer, mock.Mock())

            self.assertEqual(result, "done")
            self.assertEqual(layer["progress_svg"], str(progress_svg))
            self.assertEqual(progress_svg.read_text(encoding="utf-8"), "new")
            self.assertEqual(list(Path(tmpdir).glob("*resumed*")), [])


class ResumeEndpointTests(unittest.TestCase):
    def setUp(self):
        server.jobs.clear()
        while not server.job_queue.empty():
            server.job_queue.get_nowait()

    def tearDown(self):
        server.jobs.clear()
        while not server.job_queue.empty():
            server.job_queue.get_nowait()

    def test_paper_off_job_without_plot_origin_can_resume(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            progress_svg = Path(tmpdir) / "progress.svg"
            progress_svg.write_text("<svg/>", encoding="utf-8")
            job_id = "paper-off-paused-job"
            server.jobs[job_id] = {
                "id": job_id,
                "status": "paused",
                "current_layer": 1,
                "paused_layer": 1,
                "plot_origin": None,
                "paper": {
                    "enabled": False,
                    "top_right": {"x_mm": 500.0, "y_mm": 700.0},
                },
                "layers": [
                    {
                        "index": 1,
                        "name": "Layer 1",
                        "progress_svg": str(progress_svg),
                    }
                ],
            }

            with (
                mock.patch.object(server, "require_hardware_idle"),
                mock.patch.object(server, "save_job_unlocked"),
            ):
                result = server.resume_job(job_id, x_plotter_token="test-token")

            self.assertEqual(result["status"], "queued_for_resume")
            self.assertEqual(server.jobs[job_id]["status"], "queued_for_resume")
            self.assertEqual(server.job_queue.get_nowait(), job_id)


class PauseClearanceTests(unittest.TestCase):
    def test_pause_clearance_uses_dedicated_position_and_only_raises(self):
        job = {
            "id": "pause-job",
            "pen_pos_down": 35,
            "pen_rate_raise": 80,
            "pen_delay_up": 25,
            "pen_delay_down": 10,
        }
        original = server.current_pen_settings()
        try:
            with server.pen_settings_lock:
                server.pen_settings["pause_clearance_pos"] = 100
            with mock.patch.object(
                server,
                "run_pen_manual_direct_first",
                return_value={"ok": True, "method": "direct_ebb"},
            ) as raise_pen:
                result = server.attempt_pause_clearance_raise(job, io.StringIO())
        finally:
            with server.pen_settings_lock:
                server.pen_settings.update(original)

        self.assertTrue(result["ok"])
        self.assertEqual(result["position"], 100)
        self.assertTrue(raise_pen.call_args.kwargs["raised"])
        self.assertEqual(raise_pen.call_args.kwargs["up_pos"], 100)
        self.assertEqual(raise_pen.call_args.kwargs["down_pos"], 35)

    def test_pause_state_is_preserved_when_clearance_raise_fails(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "job.log"
            log_path.write_text("", encoding="utf-8")
            job_id = "pause-failure-job"
            server.jobs[job_id] = {
                "id": job_id,
                "status": "running",
                "log_path": str(log_path),
            }
            with (
                log_path.open("a", encoding="utf-8") as log,
                mock.patch.object(
                    server,
                    "attempt_pause_clearance_raise",
                    return_value={"ok": False, "position": 100, "error": "servo fault"},
                ),
                mock.patch.object(server, "save_job_unlocked"),
                mock.patch.object(server, "announce_on_linux_box"),
            ):
                result = server.finalize_paused_job(job_id, 1, log)

        self.assertFalse(result["ok"])
        self.assertEqual(server.jobs[job_id]["status"], "paused")
        self.assertIn("failed", server.jobs[job_id]["operator_message"])
        server.jobs.pop(job_id, None)


class HomePositionTests(unittest.TestCase):
    def setUp(self):
        with server.position_lock:
            server.position_calibration_enabled = True
            server.position_bed_calibrated = True
            server.position_offset.update({"x_mm": 0.0, "y_mm": 0.0})
            server.position_current = None
            server.home_position = None

    def test_home_is_independent_from_current_bed_position(self):
        with server.position_lock:
            server.set_current_position_unlocked(0, server.BED_HEIGHT_MM)
            server.set_home_position_unlocked(0, server.BED_HEIGHT_MM)
            server.set_current_position_unlocked(125, 300)

        self.assertEqual(server.current_home_position(), {"x_mm": 0.0, "y_mm": server.BED_HEIGHT_MM})
        self.assertEqual(server.current_software_position(), {"x_mm": 125.0, "y_mm": 300.0})

    def test_disabling_position_calibration_requires_acknowledgement(self):
        request = mock.Mock()
        request.client.host = "127.0.0.1"

        with self.assertRaises(HTTPException) as raised:
            server.plotter_position_calibration_toggle(
                request,
                {"enabled": False},
                x_plotter_token="test-token",
            )
        self.assertEqual(raised.exception.status_code, 400)

        result = server.plotter_position_calibration_toggle(
            request,
            {"enabled": False, "acknowledge_unsafe": True},
            x_plotter_token="test-token",
        )
        self.assertFalse(result["calibration_enabled"])

        server.plotter_position_calibration_toggle(
            request,
            {"enabled": True},
            x_plotter_token="test-token",
        )

    def test_calibration_off_keeps_local_position_unbounded(self):
        with server.position_lock:
            server.position_calibration_enabled = False
            server.set_current_position_unlocked(-12.5, 930.0)

        self.assertEqual(server.current_software_position(), {"x_mm": -12.5, "y_mm": 930.0})

    def test_set_home_requires_completed_bed_calibration_when_enabled(self):
        request = mock.Mock()
        request.client.host = "127.0.0.1"
        with server.position_lock:
            server.position_calibration_enabled = True
            server.position_bed_calibrated = False

        with mock.patch.object(server, "require_hardware_idle"):
            with self.assertRaises(HTTPException) as raised:
                server.plotter_set_home(request, x_plotter_token="test-token")

        self.assertEqual(raised.exception.status_code, 409)
        self.assertIn("Set Bed Top Left", str(raised.exception.detail))

    def test_calibration_off_set_home_creates_local_origin(self):
        request = mock.Mock()
        request.client.host = "127.0.0.1"
        serial_port = mock.Mock()
        serial_port.readline.return_value = b"OK\r\n"

        with server.position_lock:
            server.position_calibration_enabled = False
            server.position_offset.update({"x_mm": 200.0, "y_mm": 300.0})

        with (
            mock.patch.object(server, "require_hardware_idle"),
            mock.patch.object(server.serial, "Serial") as serial_cls,
            mock.patch.object(server, "require_enabled_high_resolution_motors"),
            mock.patch.object(
                server,
                "read_step_position",
                return_value=(100, 200, {"x_mm": 50.0, "y_mm": 75.0}),
            ),
            mock.patch.object(server, "save_position_offset_unlocked"),
        ):
            serial_cls.return_value.__enter__.return_value = serial_port
            result = server.plotter_set_home(request, x_plotter_token="test-token")

        self.assertEqual(result["position_estimate"], {"x_mm": 0.0, "y_mm": 0.0})
        self.assertEqual(result["home_position"], {"x_mm": 0.0, "y_mm": 0.0})
        self.assertIn("local Home", result["message"])
        serial_port.write.assert_called_once_with(b"CS\r")

    def test_calibration_off_allows_negative_relative_jog(self):
        request = mock.Mock()
        request.client.host = "127.0.0.1"
        serial_port = object()
        with server.position_lock:
            server.position_calibration_enabled = False
            server.position_offset.update({"x_mm": 0.0, "y_mm": 0.0})
            server.position_current = None

        with (
            mock.patch.object(server, "require_hardware_idle"),
            mock.patch.object(server.serial, "Serial") as serial_cls,
            mock.patch.object(server, "require_cached_high_resolution_motors"),
            mock.patch.object(
                server,
                "read_step_position",
                side_effect=[
                    (0, 0, {"x_mm": 0.0, "y_mm": 0.0}),
                    (0, 0, {"x_mm": 0.0, "y_mm": 2.0}),
                ],
            ),
            mock.patch.object(server, "raw_command", return_value="OK") as command,
            mock.patch.object(server, "wait_for_motion_idle"),
            mock.patch.object(server, "save_position_offset_unlocked"),
        ):
            serial_cls.return_value.__enter__.return_value = serial_port
            result = server.plotter_jog(
                request,
                {"x_mm": -2.0, "y_mm": 0.0, "speed_mm_s": 25.0},
                x_plotter_token="test-token",
            )

        self.assertEqual(result["position_estimate"], {"x_mm": -2.0, "y_mm": 0.0})
        command.assert_called_once()

    def test_pending_calibration_allows_unrestricted_relative_position(self):
        with server.position_lock:
            server.position_calibration_enabled = True
            server.position_bed_calibrated = False
            server.set_current_position_unlocked(-5.0, 920.0)

        self.assertEqual(server.current_software_position(), {"x_mm": -5.0, "y_mm": 920.0})

    def test_calibration_off_rejects_absolute_bed_dragging(self):
        request = mock.Mock()
        request.client.host = "127.0.0.1"
        with server.position_lock:
            server.position_calibration_enabled = False

        with mock.patch.object(server, "require_hardware_idle"):
            with self.assertRaises(HTTPException) as raised:
                server.plotter_move_to(
                    request,
                    {"x_mm": 10, "y_mm": 20},
                    x_plotter_token="test-token",
                )

        self.assertEqual(raised.exception.status_code, 409)
        self.assertIn("arrow controls", str(raised.exception.detail))

    def test_return_home_uses_direct_ebb_motion(self):
        with server.position_lock:
            server.set_home_position_unlocked(10, 20)
        serial_port = object()

        with (
            mock.patch.object(server.serial, "Serial") as serial_cls,
            mock.patch.object(server, "require_cached_high_resolution_motors") as resolution,
            mock.patch.object(server, "serial_query", return_value=("1", "OK")) as query,
            mock.patch.object(server, "_move_to_bed_target_on_port_locked", return_value={"x_mm": 10, "y_mm": 20}) as move,
            mock.patch.object(server, "_run_pen_servo_on_port_locked") as pen,
            mock.patch.object(server.subprocess, "run") as subprocess_run,
        ):
            serial_cls.return_value.__enter__.return_value = serial_port
            result = server.return_home(mock.Mock())

        self.assertEqual(result, 0)
        resolution.assert_called_once_with(serial_port)
        query.assert_called_once_with(serial_port, "QP\r")
        move.assert_called_once()
        self.assertIs(move.call_args.args[0], serial_port)
        self.assertEqual(move.call_args.args[1], {"x_mm": 10.0, "y_mm": 20.0})
        pen.assert_not_called()
        subprocess_run.assert_not_called()

    def test_return_home_raises_pen_directly_when_needed(self):
        with server.position_lock:
            server.set_home_position_unlocked(10, 20)
        serial_port = object()

        with (
            mock.patch.object(server.serial, "Serial") as serial_cls,
            mock.patch.object(server, "require_cached_high_resolution_motors"),
            mock.patch.object(server, "serial_query", return_value=("0", "OK")),
            mock.patch.object(server, "_run_pen_servo_on_port_locked", return_value={"ok": True}) as pen,
            mock.patch.object(server, "_move_to_bed_target_on_port_locked", return_value={"x_mm": 10, "y_mm": 20}),
        ):
            serial_cls.return_value.__enter__.return_value = serial_port
            result = server.return_home(mock.Mock())

        self.assertEqual(result, 0)
        pen.assert_called_once()
        self.assertIs(pen.call_args.args[0], serial_port)
        self.assertTrue(pen.call_args.kwargs["raised"])

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

    def test_relative_move_requires_enabled_motors_before_axicli_walk(self):
        request = mock.Mock()
        request.client.host = "127.0.0.1"
        with server.position_lock:
            server.set_current_position_unlocked(10, 20)
            server.set_home_position_unlocked(10, 20)

        with (
            mock.patch.object(server.serial, "Serial") as serial_cls,
            mock.patch.object(
                server,
                "require_enabled_high_resolution_motors",
                side_effect=HTTPException(status_code=409, detail="motors off"),
            ) as motors,
            mock.patch.object(server.subprocess, "run") as subprocess_run,
        ):
            serial_port = serial_cls.return_value.__enter__.return_value
            with self.assertRaises(HTTPException) as raised:
                server.plotter_move(
                    request,
                    {"x_mm": 1, "y_mm": 0},
                    x_plotter_token="test-token",
                )

        self.assertEqual(raised.exception.status_code, 409)
        motors.assert_called_once_with(serial_port)
        subprocess_run.assert_not_called()


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


class JobClearTests(unittest.TestCase):
    def test_load_jobs_does_not_rewrite_unchanged_metadata(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            jobs_dir = Path(tmpdir) / "jobs"
            server.state_store.save_job(
                jobs_dir,
                "done-job",
                {"id": "done-job", "status": "done", "created_at": 123},
            )

            with (
                mock.patch.object(server, "JOBS_DIR", jobs_dir),
                mock.patch.object(server, "save_job_unlocked") as save_job,
            ):
                server.jobs.clear()
                position_uncertain = server.load_jobs()

        self.assertEqual(server.jobs["done-job"]["status"], "done")
        self.assertFalse(position_uncertain)
        save_job.assert_not_called()
        server.jobs.clear()

    def test_load_jobs_rewrites_active_metadata_as_interrupted(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            jobs_dir = Path(tmpdir) / "jobs"
            server.state_store.save_job(
                jobs_dir,
                "running-job",
                {"id": "running-job", "status": "running", "created_at": 123},
            )

            with (
                mock.patch.object(server, "JOBS_DIR", jobs_dir),
                mock.patch.object(server, "save_job_unlocked") as save_job,
            ):
                server.jobs.clear()
                position_uncertain = server.load_jobs()

        self.assertEqual(server.jobs["running-job"]["status"], "interrupted")
        self.assertTrue(position_uncertain)
        self.assertIn("Server restarted", server.jobs["running-job"]["operator_message"])
        save_job.assert_called_once_with("running-job")
        server.jobs.clear()

    def test_load_jobs_cancels_pending_job_abandoned_by_restart(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            jobs_dir = Path(tmpdir) / "jobs"
            server.state_store.save_job(
                jobs_dir,
                "pending-job",
                {"id": "pending-job", "status": "waiting_for_operator", "created_at": 123},
            )

            with (
                mock.patch.object(server, "JOBS_DIR", jobs_dir),
                mock.patch.object(server, "save_job_unlocked") as save_job,
            ):
                server.jobs.clear()
                position_uncertain = server.load_jobs()

        self.assertEqual(server.jobs["pending-job"]["status"], "cancelled")
        self.assertIn("before this job began", server.jobs["pending-job"]["operator_message"])
        self.assertFalse(position_uncertain)
        save_job.assert_called_once_with("pending-job")
        server.jobs.clear()

    def test_load_jobs_migrates_never_started_interrupted_job_to_cancelled(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            jobs_dir = Path(tmpdir) / "jobs"
            server.state_store.save_job(
                jobs_dir,
                "legacy-job",
                {
                    "id": "legacy-job",
                    "status": "interrupted",
                    "created_at": 123,
                    "started_at": None,
                    "current_layer": None,
                    "last_completed_layer": None,
                },
            )

            with mock.patch.object(server, "JOBS_DIR", jobs_dir):
                server.jobs.clear()
                server.load_jobs()

        self.assertEqual(server.jobs["legacy-job"]["status"], "cancelled")
        server.jobs.clear()

    def test_delete_single_stopped_job_keeps_files_and_removes_metadata(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            jobs_dir = Path(tmpdir) / "jobs"
            artifact = server.state_store.job_dir(jobs_dir, "old-job") / "layer_01" / "plot.svg"
            artifact.parent.mkdir(parents=True)
            artifact.write_text("<svg/>", encoding="utf-8")
            job = {
                "id": "old-job",
                "status": "paused",
                "log_path": str(Path(tmpdir) / "old-job.log"),
            }
            server.state_store.save_job(jobs_dir, "old-job", job)

            with mock.patch.object(server, "JOBS_DIR", jobs_dir):
                server.jobs.clear()
                server.jobs["old-job"] = dict(job)

                result = server.delete_job(
                    "old-job",
                    {"keep_files": True},
                    x_plotter_token="test-token",
                )

                self.assertEqual(result["removed"], {"id": "old-job", "status": "paused"})
                self.assertNotIn("old-job", server.jobs)
                self.assertFalse(server.state_store.job_meta_path(jobs_dir, "old-job").exists())
                self.assertTrue(artifact.exists())

        server.jobs.clear()

    def test_delete_single_queued_job_is_blocked(self):
        server.jobs.clear()
        server.jobs["queued-job"] = {"id": "queued-job", "status": "queued"}

        with self.assertRaises(HTTPException) as raised:
            server.delete_job(
                "queued-job",
                {"keep_files": True},
                x_plotter_token="test-token",
            )

        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("queued-job", server.jobs)
        server.jobs.clear()

    def test_clear_keep_files_deletes_metadata_so_jobs_do_not_reload(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            jobs_dir = Path(tmpdir) / "jobs"
            artifact = server.state_store.job_dir(jobs_dir, "old-job") / "layer_01" / "plot.svg"
            artifact.parent.mkdir(parents=True)
            artifact.write_text("<svg/>", encoding="utf-8")
            job = {
                "id": "old-job",
                "status": "paused",
                "log_path": str(Path(tmpdir) / "old-job.log"),
            }
            server.state_store.save_job(jobs_dir, "old-job", job)

            with mock.patch.object(server, "JOBS_DIR", jobs_dir):
                server.jobs.clear()
                server.jobs["old-job"] = dict(job)

                result = server.clear_jobs({"keep_files": True}, x_plotter_token="test-token")
                self.assertEqual(result["removed"], [{"id": "old-job", "status": "paused"}])
                self.assertNotIn("old-job", server.jobs)
                self.assertFalse(server.state_store.job_meta_path(jobs_dir, "old-job").exists())
                self.assertTrue(artifact.exists())

                server.load_jobs()
                self.assertNotIn("old-job", server.jobs)

        server.jobs.clear()


class JobPreviewTests(unittest.TestCase):
    def test_job_summary_includes_layer_preview_metadata(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            jobs_dir = Path(tmpdir) / "jobs"
            layer_dir = server.state_store.job_dir(jobs_dir, "preview-job") / "layer_01"
            layer_dir.mkdir(parents=True)
            input_svg = layer_dir / "input.svg"
            input_svg.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="10mm" height="20mm"/>', encoding="utf-8")
            job = {
                "id": "preview-job",
                "status": "queued",
                "created_at": 123,
                "layers": [
                    {
                        "index": 1,
                        "name": "Black",
                        "input_svg": str(input_svg),
                        "svg_metrics": {"width_mm": 10.0, "height_mm": 20.0},
                    }
                ],
            }

            with mock.patch.object(server, "JOBS_DIR", jobs_dir):
                server.jobs.clear()
                server.jobs["preview-job"] = job
                result = server.list_jobs(x_plotter_token="test-token")

        preview = result["jobs"][0]["plot_preview"]
        self.assertEqual(preview["footprint"], {"width_mm": 10.0, "height_mm": 20.0})
        self.assertEqual(preview["layers"][0]["name"], "Black")
        self.assertIn("/jobs/preview-job/layers/1/preview.svg", preview["layers"][0]["url"])
        server.jobs.clear()

    def test_job_layer_preview_serves_local_layer_svg(self):
        request = mock.Mock()
        request.client.host = "127.0.0.1"
        with tempfile.TemporaryDirectory() as tmpdir:
            jobs_dir = Path(tmpdir) / "jobs"
            layer_dir = server.state_store.job_dir(jobs_dir, "preview-job") / "layer_01"
            layer_dir.mkdir(parents=True)
            input_svg = layer_dir / "input.svg"
            input_svg.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="10mm" height="20mm"/>', encoding="utf-8")
            job = {
                "id": "preview-job",
                "status": "queued",
                "layers": [{"index": 1, "input_svg": str(input_svg), "svg_metrics": {"width_mm": 10.0, "height_mm": 20.0}}],
            }

            with mock.patch.object(server, "JOBS_DIR", jobs_dir):
                server.jobs.clear()
                server.jobs["preview-job"] = job
                response = server.job_layer_preview(request, "preview-job", 1)

        self.assertEqual(Path(response.path), input_svg)
        self.assertEqual(response.media_type, "image/svg+xml")
        server.jobs.clear()

    def test_job_layer_preview_rejects_path_outside_job_directory(self):
        request = mock.Mock()
        request.client.host = "127.0.0.1"
        with tempfile.TemporaryDirectory() as tmpdir:
            jobs_dir = Path(tmpdir) / "jobs"
            outside = Path(tmpdir) / "outside.svg"
            outside.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="10mm" height="20mm"/>', encoding="utf-8")
            job = {
                "id": "preview-job",
                "status": "queued",
                "layers": [{"index": 1, "input_svg": str(outside), "svg_metrics": {"width_mm": 10.0, "height_mm": 20.0}}],
            }

            with mock.patch.object(server, "JOBS_DIR", jobs_dir):
                server.jobs.clear()
                server.jobs["preview-job"] = job
                with self.assertRaises(HTTPException) as raised:
                    server.job_layer_preview(request, "preview-job", 1)

        self.assertEqual(raised.exception.status_code, 400)
        server.jobs.clear()


class InkDipGeometryTests(unittest.TestCase):
    def test_parses_plob_points_as_millimetres(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "digest.svg"
            path.write_text(
                '<svg xmlns="http://www.w3.org/2000/svg">'
                '<g><polyline points="0,0 1,0 1,2"/></g></svg>',
                encoding="utf-8",
            )

            self.assertEqual(
                ink_dip.parse_plob_polylines(path),
                [[(0.0, 0.0), (25.4, 0.0), (25.4, 50.8)]],
            )

    def test_schedules_dips_only_after_complete_strokes(self):
        strokes = [
            [(0.0, 0.0), (10.0, 0.0)],
            [(10.0, 0.0), (20.0, 0.0)],
            [(20.0, 0.0), (30.0, 0.0)],
        ]
        estimate = ink_dip.estimate_checkpoint_schedule(
            strokes,
            speed_pendown=10,
            interval_s=0.75,
            stroke_overhead_s=0.0,
        )

        self.assertEqual(estimate["checkpoint_after_strokes"], [2])
        self.assertEqual(estimate["estimated_dip_count_per_layer"], 2)

    def test_auto_dip_schedule_caps_high_nominal_plot_speed(self):
        strokes = [
            [(float(index * 10), 0.0), (float((index + 1) * 10), 0.0)]
            for index in range(10)
        ]
        estimate = ink_dip.estimate_checkpoint_schedule(
            strokes,
            speed_pendown=50,
            interval_s=2.0,
            max_effective_speed_mm_s=30.0,
            stroke_overhead_s=0.0,
        )

        self.assertEqual(estimate["checkpoint_after_strokes"], [7])
        self.assertEqual(estimate["estimated_speed_mm_s"], 30.0)
        self.assertGreater(estimate["nominal_speed_mm_s"], estimate["estimated_speed_mm_s"])

    def test_auto_dip_schedule_accounts_for_short_stroke_overhead(self):
        strokes = [
            [(float(index * 10), 0.0), (float((index + 1) * 10), 0.0)]
            for index in range(10)
        ]
        estimate = ink_dip.estimate_checkpoint_schedule(
            strokes,
            speed_pendown=50,
            interval_s=3.0,
            max_effective_speed_mm_s=30.0,
            stroke_overhead_s=0.5,
        )

        self.assertEqual(estimate["checkpoint_after_strokes"], [4, 8])
        self.assertEqual(estimate["stroke_overhead_s"], 0.5)

    def test_does_not_schedule_redundant_dip_after_final_stroke(self):
        strokes = [[(0.0, 0.0), (100.0, 0.0)]]
        estimate = ink_dip.estimate_checkpoint_schedule(
            strokes,
            speed_pendown=10,
            interval_s=0.1,
            stroke_overhead_s=0.0,
        )

        self.assertEqual(estimate["checkpoint_after_strokes"], [])
        self.assertEqual(estimate["estimated_dip_count_per_layer"], 1)
        self.assertGreater(estimate["longest_stroke_time_s"], estimate["interval_s"])

    def test_detects_pen_down_keepout_collision(self):
        collision = ink_dip.find_keepout_collision(
            [[(0.0, 0.0), (100.0, 0.0)]],
            origin_mm=(10.0, 10.0),
            centre_mm=(60.0, 12.0),
            radius_mm=5.0,
        )

        self.assertEqual(collision["motion"], "pen_down")
        self.assertEqual(collision["stroke"], 1)

    def test_detects_pen_up_travel_and_return_home_collisions(self):
        travel_collision = ink_dip.find_keepout_collision(
            [[(100.0, 0.0), (110.0, 0.0)]],
            origin_mm=(0.0, 0.0),
            centre_mm=(50.0, 0.0),
            radius_mm=2.0,
        )
        return_collision = ink_dip.find_keepout_collision(
            [[(100.0, 0.0), (110.0, 0.0)]],
            origin_mm=(0.0, 0.0),
            centre_mm=(55.0, 0.0),
            radius_mm=2.0,
        )

        self.assertEqual(travel_collision["motion"], "pen_up")
        self.assertEqual(return_collision["motion"], "pen_up")

    def test_accepts_geometry_clear_of_keepout(self):
        collision = ink_dip.find_keepout_collision(
            [[(0.0, 0.0), (100.0, 0.0)]],
            origin_mm=(0.0, 0.0),
            centre_mm=(50.0, 20.0),
            radius_mm=5.0,
        )
        self.assertIsNone(collision)

    def test_writes_programmatic_pauses_only_at_checkpoints(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.svg"
            target = Path(directory) / "prepared.svg"
            source.write_text(
                '<svg xmlns="http://www.w3.org/2000/svg" '
                'xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape">'
                '<g inkscape:groupmode="layer" inkscape:label="original">'
                '<polyline id="1" points="0,0 1,0"/>'
                '<polyline id="2" points="1,0 2,0"/>'
                '<polyline id="3" points="2,0 3,0"/>'
                '</g><plotdata application="axidraw" plob_version="1"/></svg>',
                encoding="utf-8",
            )

            group_count = ink_dip.write_checkpoint_digest(source, target, [1, 2])
            root = ink_dip.ET.parse(target).getroot()
            groups = [item for item in root if item.tag.endswith("g")]
            labels = [
                group.attrib.get(f"{{{ink_dip.INKSCAPE_NAMESPACE}}}label")
                for group in groups
            ]

            self.assertEqual(group_count, 3)
            self.assertEqual(labels, ["original", "!ink-dip-2", "!ink-dip-3"])
            self.assertEqual(
                [len([child for child in group if child.tag.endswith("polyline")]) for group in groups],
                [1, 1, 1],
            )

    def test_rejects_checkpoint_after_final_stroke(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.svg"
            source.write_text(
                '<svg xmlns="http://www.w3.org/2000/svg"><g>'
                '<polyline points="0,0 1,0"/></g></svg>',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "before the final stroke"):
                ink_dip.write_checkpoint_digest(source, Path(directory) / "target.svg", [1])

    def test_rejects_existing_programmatic_pause_layers(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.svg"
            source.write_text(
                '<svg xmlns="http://www.w3.org/2000/svg" '
                'xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape">'
                '<g inkscape:label="!operator-pause"><polyline points="0,0 1,0"/></g>'
                '</svg>',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "cannot be combined"):
                ink_dip.write_checkpoint_digest(source, Path(directory) / "target.svg", [])


class InkWellSettingsTests(unittest.TestCase):
    def setUp(self):
        with server.position_lock:
            server.position_calibration_id = "test-calibration"
        with server.ink_well_settings_lock:
            server.ink_well_settings.clear()
            server.ink_well_settings.update(
                {
                    "state_version": 1,
                    "installed": False,
                    "centre": None,
                    "radius_mm": None,
                    "clearance_pos": None,
                    "dip_pos": None,
                    "dwell_ms": 1000,
                    "drip_dwell_ms": 0,
                    "travel_speed_mm_s": server.DEFAULT_INK_WELL_TRAVEL_SPEED_MM_S,
                    "dip_circle_count": 3,
                    "dip_circle_diameter_mm": 10.0,
                    "calibration_id": None,
                    "test_passed": False,
                    "tested_at": None,
                }
            )

    def test_installed_well_requires_complete_tested_calibration(self):
        settings = {
            "installed": True,
            "centre": {"x_mm": 10, "y_mm": 20},
            "radius_mm": 15,
            "clearance_pos": 80,
            "dip_pos": 20,
            "dwell_ms": 1000,
            "drip_dwell_ms": 0,
            "dip_circle_count": 3,
            "dip_circle_diameter_mm": 10,
            "test_passed": False,
        }
        with self.assertRaisesRegex(ValueError, "test cycle must pass"):
            server.validate_ink_well_settings(settings, require_ready=True)

        settings["test_passed"] = True
        self.assertEqual(
            server.validate_ink_well_settings(settings, require_ready=True)["radius_mm"],
            15.0,
        )
        self.assertEqual(settings["travel_speed_mm_s"], server.DEFAULT_INK_WELL_TRAVEL_SPEED_MM_S)

    def test_rejects_ink_well_travel_speed_above_safe_manual_limit(self):
        settings = {
            "installed": False,
            "centre": {"x_mm": 10, "y_mm": 20},
            "radius_mm": 15,
            "clearance_pos": 80,
            "dip_pos": 20,
            "dwell_ms": 1000,
            "drip_dwell_ms": 0,
            "travel_speed_mm_s": server.SAFE_MANUAL_MAX_XY_SPEED_MM_S + 1,
            "dip_circle_count": 3,
            "dip_circle_diameter_mm": 10,
            "test_passed": False,
        }

        with self.assertRaisesRegex(ValueError, "travel_speed_mm_s"):
            server.validate_ink_well_settings(settings)

    def test_setting_centre_under_new_calibration_requires_retest(self):
        request = mock.Mock()
        request.client.host = "127.0.0.1"
        with server.position_lock:
            server.set_current_position_unlocked(10, 20)
            server.position_calibration_id = "new-calibration"
        with server.ink_well_settings_lock:
            server.ink_well_settings.update(
                {
                    "installed": True,
                    "centre": {"x_mm": 10, "y_mm": 20},
                    "radius_mm": 15,
                    "clearance_pos": 80,
                    "dip_pos": 20,
                    "dwell_ms": 1000,
                    "drip_dwell_ms": 0,
                    "travel_speed_mm_s": 120,
                    "dip_circle_count": 3,
                    "dip_circle_diameter_mm": 10,
                    "calibration_id": "old-calibration",
                    "test_passed": True,
                    "tested_at": 123,
                }
            )

        result = server.plotter_ink_well_update(
            request,
            {"centre_from_current": True},
            x_plotter_token="test-token",
        )

        self.assertEqual(result["ink_well"]["calibration_id"], "new-calibration")
        self.assertFalse(result["ink_well"]["installed"])
        self.assertFalse(result["ink_well"]["test_passed"])
        self.assertIsNone(result["ink_well"]["tested_at"])

    def test_disabling_ink_well_check_preserves_setup(self):
        request = mock.Mock()
        request.client.host = "127.0.0.1"
        with server.ink_well_settings_lock:
            server.ink_well_settings.update(
                {
                    "installed": True,
                    "centre": {"x_mm": 10, "y_mm": 20},
                    "radius_mm": 15,
                    "clearance_pos": 80,
                    "dip_pos": 20,
                    "dwell_ms": 1000,
                    "drip_dwell_ms": 0,
                    "travel_speed_mm_s": 120,
                    "dip_circle_count": 3,
                    "dip_circle_diameter_mm": 10,
                    "calibration_id": server.position_calibration_id,
                    "test_passed": True,
                    "tested_at": 123,
                }
            )

        result = server.plotter_ink_well_update(
            request,
            {"installed": False},
            x_plotter_token="test-token",
        )

        self.assertFalse(result["ink_well"]["installed"])
        self.assertEqual(result["ink_well"]["centre"], {"x_mm": 10, "y_mm": 20})
        self.assertEqual(result["ink_well"]["radius_mm"], 15.0)
        self.assertTrue(result["ink_well"]["test_passed"])

    def test_changing_ink_well_travel_speed_preserves_test_and_install_state(self):
        request = mock.Mock()
        request.client.host = "127.0.0.1"
        with server.ink_well_settings_lock:
            server.ink_well_settings.update(
                {
                    "installed": True,
                    "centre": {"x_mm": 10, "y_mm": 20},
                    "radius_mm": 15,
                    "clearance_pos": 80,
                    "dip_pos": 20,
                    "dwell_ms": 1000,
                    "drip_dwell_ms": 0,
                    "travel_speed_mm_s": 120,
                    "dip_circle_count": 3,
                    "dip_circle_diameter_mm": 10,
                    "calibration_id": server.position_calibration_id,
                    "test_passed": True,
                    "tested_at": 123,
                }
            )

        result = server.plotter_ink_well_update(
            request,
            {"travel_speed_mm_s": 160},
            x_plotter_token="test-token",
        )

        self.assertTrue(result["ink_well"]["installed"])
        self.assertTrue(result["ink_well"]["test_passed"])
        self.assertEqual(result["ink_well"]["tested_at"], 123)
        self.assertEqual(result["ink_well"]["centre"], {"x_mm": 10, "y_mm": 20})
        self.assertEqual(result["ink_well"]["travel_speed_mm_s"], 160.0)

    def test_plot_snapshot_is_independent_from_later_calibration_changes(self):
        settings = {
            "installed": True,
            "centre": {"x_mm": 10, "y_mm": 20},
            "radius_mm": 15,
            "clearance_pos": 80,
            "dip_pos": 20,
            "dwell_ms": 1000,
            "drip_dwell_ms": 100,
            "travel_speed_mm_s": 140,
            "dip_circle_count": 3,
            "dip_circle_diameter_mm": 10,
            "calibration_id": "test-calibration",
            "test_passed": True,
            "tested_at": 123,
        }
        snapshot = server.ink_well_plot_snapshot(settings)
        settings["centre"]["x_mm"] = 99

        self.assertEqual(snapshot["centre"]["x_mm"], 10.0)
        self.assertEqual(snapshot["travel_speed_mm_s"], 140.0)
        self.assertEqual(snapshot["dip_circle_count"], 3)
        self.assertEqual(snapshot["dip_circle_diameter_mm"], 10.0)

    def test_rejects_dip_circle_larger_than_well(self):
        settings = {
            "installed": False,
            "centre": {"x_mm": 10, "y_mm": 20},
            "radius_mm": 4,
            "clearance_pos": 80,
            "dip_pos": 20,
            "dwell_ms": 1000,
            "drip_dwell_ms": 0,
            "dip_circle_count": 3,
            "dip_circle_diameter_mm": 10,
            "test_passed": False,
        }

        with self.assertRaisesRegex(ValueError, "fit inside"):
            server.validate_ink_well_settings(settings)

    def test_layer_analysis_rejects_keepout_collision(self):
        with tempfile.TemporaryDirectory() as directory:
            digest = Path(directory) / "digest.svg"
            digest.write_text(
                '<svg xmlns="http://www.w3.org/2000/svg">'
                '<polyline points="0,0 4,0"/></svg>',
                encoding="utf-8",
            )
            with (
                mock.patch.object(server, "generate_plot_digest"),
                self.assertRaisesRegex(ValueError, "keep-out zone"),
            ):
                server.analyse_layer_for_ink_well(
                    Path(directory) / "input.svg",
                    digest,
                    job_settings={
                        "speed_pendown": 15,
                        "speed_penup": 40,
                        "pen_pos_down": 35,
                        "pen_pos_up": 65,
                    },
                    home={"x_mm": 0, "y_mm": 0},
                    well={
                        "centre": {"x_mm": 50, "y_mm": 0},
                        "radius_mm": 5,
                    },
                )

    def test_ink_well_test_cycle_does_not_auto_confirm_passed(self):
        request = mock.Mock()
        request.client.host = "127.0.0.1"
        with server.ink_well_settings_lock:
            server.ink_well_settings.update(
                {
                    "centre": {"x_mm": 10, "y_mm": 20},
                    "radius_mm": 15,
                    "clearance_pos": 80,
                    "dip_pos": 20,
                    "dwell_ms": 1000,
                    "drip_dwell_ms": 0,
                    "dip_circle_count": 3,
                    "dip_circle_diameter_mm": 10,
                    "calibration_id": "test-calibration",
                    "test_passed": False,
                    "tested_at": None,
                }
            )
        with server.position_lock:
            server.set_current_position_unlocked(10, 20)

        with mock.patch.object(server, "execute_dip_cycle", return_value={"return_error_mm": 0}):
            result = server.plotter_ink_well_test(request, x_plotter_token="test-token")

        self.assertFalse(result["ink_well"]["test_passed"])
        self.assertIsNotNone(result["ink_well"]["tested_at"])

    def test_ink_well_test_rejects_stale_position_calibration(self):
        request = mock.Mock()
        request.client.host = "127.0.0.1"
        with server.ink_well_settings_lock:
            server.ink_well_settings.update(
                {
                    "centre": {"x_mm": 10, "y_mm": 20},
                    "radius_mm": 15,
                    "clearance_pos": 80,
                    "dip_pos": 20,
                    "dwell_ms": 1000,
                    "drip_dwell_ms": 0,
                    "dip_circle_count": 3,
                    "dip_circle_diameter_mm": 10,
                    "calibration_id": "old-calibration",
                    "test_passed": False,
                    "tested_at": None,
                }
            )
        with server.position_lock:
            server.set_current_position_unlocked(10, 20)
            server.position_calibration_id = "new-calibration"

        with self.assertRaises(HTTPException) as caught:
            server.plotter_ink_well_test(request, x_plotter_token="test-token")

        self.assertEqual(caught.exception.status_code, 409)
        self.assertIn("different plotter calibration", caught.exception.detail)

    def test_ink_well_test_confirmation_marks_passed_and_installs_after_cycle(self):
        request = mock.Mock()
        request.client.host = "127.0.0.1"
        with server.ink_well_settings_lock:
            server.ink_well_settings.update(
                {
                    "centre": {"x_mm": 10, "y_mm": 20},
                    "radius_mm": 15,
                    "clearance_pos": 80,
                    "dip_pos": 20,
                    "dwell_ms": 1000,
                    "drip_dwell_ms": 0,
                    "dip_circle_count": 3,
                    "dip_circle_diameter_mm": 10,
                    "calibration_id": server.position_calibration_id,
                    "test_passed": False,
                    "tested_at": 123,
                }
            )

        result = server.plotter_ink_well_confirm_test(request, x_plotter_token="test-token")

        self.assertTrue(result["ink_well"]["test_passed"])
        self.assertTrue(result["ink_well"]["installed"])
        self.assertIn("enabled", result["message"])


class AutoDipExecutionTests(unittest.TestCase):
    def setUp(self):
        server.jobs.clear()
        with server.position_lock:
            server.position_current = None

    def test_resolves_auto_dip_upload_aliases(self):
        self.assertTrue(server.resolve_auto_dip_flag(None, False, True))
        self.assertTrue(server.resolve_auto_dip_flag(None, "on"))
        self.assertTrue(server.resolve_auto_dip_flag(None, "yes"))
        self.assertFalse(server.resolve_auto_dip_flag(None, False, "false", "0"))

    def test_upload_ignores_default_auto_dip_when_ink_well_is_off(self):
        plot_defaults = server.current_plot_settings()
        plot_defaults["auto_dip_enabled"] = True
        plot_defaults["dip_interval_s"] = 45
        uploaded = mock.Mock()
        uploaded.filename = "layer.svg"
        uploaded.file = io.BytesIO(b'<svg xmlns="http://www.w3.org/2000/svg" width="10mm" height="10mm"/>')

        with (
            mock.patch.object(server, "current_plot_settings", return_value=plot_defaults),
            mock.patch.object(server, "current_ink_well_settings", return_value={"installed": False}),
            mock.patch.object(server.job_queue, "put") as enqueue,
        ):
            result = asyncio.run(
                server.plot_layers(
                    files=[uploaded],
                    layer_names=None,
                    speed_pendown=None,
                    speed_penup=None,
                    pen_delay_down=None,
                    pen_delay_up=None,
                    pen_rate_raise=None,
                    pen_pos_down=None,
                    pen_pos_up=None,
                    auto_dip=None,
                    auto_dip_enabled=None,
                    ink_dip=None,
                    ink_dipping=None,
                    automatic_ink_dipping=None,
                    autoDip=None,
                    dip_interval_s=None,
                    rotation_degrees=0,
                    x_plotter_token="test-token",
                )
            )

        self.assertFalse(result["auto_dip_enabled"])
        enqueue.assert_called_once_with(result["job_id"])
        server.jobs.pop(result["job_id"], None)

    def test_upload_rotates_svg_before_validation_and_records_orientation(self):
        uploaded = mock.Mock()
        uploaded.filename = "portrait.svg"
        uploaded.file = io.BytesIO(
            b'<svg xmlns="http://www.w3.org/2000/svg" width="10mm" height="20mm" '
            b'viewBox="0 0 10 20"><path d="M 0 0 L 10 20"/></svg>'
        )

        with (
            tempfile.TemporaryDirectory() as tmpdir,
            mock.patch.object(server, "JOBS_DIR", Path(tmpdir) / "jobs"),
            mock.patch.object(server, "LOGS_DIR", Path(tmpdir) / "logs"),
            mock.patch.object(server, "current_ink_well_settings", return_value={"installed": False}),
            mock.patch.object(server.job_queue, "put") as enqueue,
        ):
            result = asyncio.run(
                server.plot_layers(
                    files=[uploaded],
                    layer_names=None,
                    speed_pendown=None,
                    speed_penup=None,
                    pen_delay_down=None,
                    pen_delay_up=None,
                    pen_rate_raise=None,
                    pen_pos_down=None,
                    pen_pos_up=None,
                    auto_dip=False,
                    auto_dip_enabled=None,
                    ink_dip=None,
                    ink_dipping=None,
                    automatic_ink_dipping=None,
                    autoDip=None,
                    dip_interval_s=None,
                    rotation_degrees=90,
                    x_plotter_token="test-token",
                )
            )
            job = server.jobs[result["job_id"]]
            input_svg = Path(job["layers"][0]["input_svg"])

            self.assertEqual(result["rotation_degrees"], 90)
            self.assertEqual(job["rotation_degrees"], 90)
            self.assertEqual(job["layers"][0]["rotation_degrees"], 90)
            self.assertEqual(
                job["layers"][0]["svg_metrics"],
                {"width_mm": 20.0, "height_mm": 10.0},
            )
            self.assertEqual(server.job_plot_footprint(job), {"width_mm": 20.0, "height_mm": 10.0})
            self.assertIn('viewBox="0 0 20 10"', input_svg.read_text(encoding="utf-8"))

            job["status"] = "done"
            rerun_result = server.rerun_job(
                result["job_id"],
                x_plotter_token="test-token",
            )
            rerun = server.jobs[rerun_result["job_id"]]
            rerun_svg = Path(rerun["layers"][0]["input_svg"]).read_text(encoding="utf-8")

            self.assertEqual(rerun_result["rotation_degrees"], 90)
            self.assertEqual(rerun["rotation_degrees"], 90)
            self.assertEqual(rerun["layers"][0]["svg_metrics"], {"width_mm": 20.0, "height_mm": 10.0})
            self.assertEqual(rerun_svg.count('transform="matrix('), 1)

            rotated_rerun_result = server.rerun_job(
                result["job_id"],
                {"rotation_degrees": 180},
                x_plotter_token="test-token",
            )
            rotated_rerun = server.jobs[rotated_rerun_result["job_id"]]
            rotated_rerun_svg = Path(rotated_rerun["layers"][0]["input_svg"]).read_text(encoding="utf-8")

            self.assertEqual(rotated_rerun_result["rotation_degrees"], 180)
            self.assertEqual(rotated_rerun["rotation_degrees"], 180)
            self.assertEqual(
                rotated_rerun["layers"][0]["svg_metrics"],
                {"width_mm": 10.0, "height_mm": 20.0},
            )
            self.assertEqual(rotated_rerun_svg.count('transform="matrix('), 2)

        self.assertEqual(
            enqueue.call_args_list,
            [
                mock.call(result["job_id"]),
                mock.call(rerun_result["job_id"]),
                mock.call(rotated_rerun_result["job_id"]),
            ],
        )
        server.jobs.pop(result["job_id"], None)
        server.jobs.pop(rerun_result["job_id"], None)
        server.jobs.pop(rotated_rerun_result["job_id"], None)

    def test_prepares_checkpoint_digest_for_layer(self):
        analysis = {
            "dip_schedule": {
                "checkpoint_after_strokes": [2, 5],
            }
        }
        layer = {"plot_digest_svg": "/tmp/source.svg"}
        with mock.patch.object(server, "write_checkpoint_digest", return_value=3) as write:
            server.prepare_auto_dip_layer(layer, analysis)

        self.assertEqual(layer["plot_svg"], "/tmp/auto_dip_plot.svg")
        self.assertEqual(layer["auto_dip_checkpoint_count"], 2)
        write.assert_called_once()

    def test_programmatic_pause_runs_dip_then_resumes(self):
        job = {
            "id": "dip-job",
            "auto_dip_enabled": True,
            "dip_count": 0,
            "ink_well": {},
            "plot_start_position": {"x_mm": 10, "y_mm": 20},
        }
        layer = {"index": 1}
        server.jobs[job["id"]] = job

        with (
            mock.patch.object(server, "current_home_position", return_value={"x_mm": 10, "y_mm": 20}),
            mock.patch.object(server, "execute_dip_cycle", return_value={"return_error_mm": 0}),
            mock.patch.object(
                server,
                "current_hardware_bed_position_locked",
                return_value={"x_mm": 30, "y_mm": 40},
            ),
            mock.patch.object(server, "run_layer", return_value="auto_dip_pause") as run,
            mock.patch.object(server, "resume_layer", return_value="done") as resume,
            mock.patch.object(server, "save_job_unlocked"),
        ):
            result = server.run_layer_with_auto_dips(job, layer, mock.Mock())

        self.assertEqual(result, "done")
        self.assertEqual(job["dip_count"], 2)
        run.assert_called_once()
        resume.assert_called_once()

    def test_auto_dip_runs_initial_dip_before_first_plot(self):
        job = {
            "id": "dip-job",
            "auto_dip_enabled": True,
            "dip_count": 0,
            "ink_well": {},
            "plot_start_position": {"x_mm": 10, "y_mm": 20},
        }
        layer = {"index": 1}
        server.jobs[job["id"]] = job
        events = []

        def dip(_job, _log, *, return_position):
            self.assertEqual(return_position, {"x_mm": 10.0, "y_mm": 20.0})
            events.append("dip")
            return {"return_error_mm": 0}

        def plot(_job, _layer, _log):
            events.append("plot")
            return "done"

        with (
            mock.patch.object(server, "current_home_position", return_value={"x_mm": 10, "y_mm": 20}),
            mock.patch.object(server, "execute_dip_cycle", side_effect=dip),
            mock.patch.object(server, "run_layer", side_effect=plot),
            mock.patch.object(server, "save_job_unlocked"),
        ):
            result = server.run_layer_with_auto_dips(job, layer, mock.Mock())

        self.assertEqual(result, "done")
        self.assertEqual(events, ["dip", "plot"])
        self.assertEqual(job["dip_count"], 1)

    def test_initial_auto_dip_returns_to_current_home_not_current_position(self):
        job = {
            "id": "dip-job",
            "auto_dip_enabled": True,
            "dip_count": 0,
            "ink_well": {},
            "plot_start_position": {"x_mm": 111, "y_mm": 222},
        }
        layer = {"index": 1}
        server.jobs[job["id"]] = job

        with (
            mock.patch.object(
                server,
                "current_home_position",
                return_value={"x_mm": 123, "y_mm": 456},
            ),
            mock.patch.object(
                server,
                "current_software_position",
                return_value={"x_mm": 500, "y_mm": 800},
            ),
            mock.patch.object(server, "execute_dip_cycle", return_value={"return_error_mm": 0}) as dip,
            mock.patch.object(server, "run_layer", return_value="done"),
            mock.patch.object(server, "save_job_unlocked"),
        ):
            result = server.run_layer_with_auto_dips(job, layer, mock.Mock())

        self.assertEqual(result, "done")
        dip.assert_called_once()
        self.assertEqual(
            dip.call_args.kwargs["return_position"],
            {"x_mm": 123.0, "y_mm": 456.0},
        )

    def test_initial_auto_dip_falls_back_to_upload_home_snapshot(self):
        job = {
            "id": "dip-job",
            "auto_dip_enabled": True,
            "dip_count": 0,
            "ink_well": {},
            "plot_start_position": {"x_mm": 123, "y_mm": 456},
        }
        layer = {"index": 1}
        server.jobs[job["id"]] = job

        with (
            mock.patch.object(server, "current_home_position", side_effect=HTTPException(status_code=409)),
            mock.patch.object(server, "execute_dip_cycle", return_value={"return_error_mm": 0}) as dip,
            mock.patch.object(server, "run_layer", return_value="done"),
            mock.patch.object(server, "save_job_unlocked"),
        ):
            result = server.run_layer_with_auto_dips(job, layer, mock.Mock())

        self.assertEqual(result, "done")
        dip.assert_called_once()
        self.assertEqual(
            dip.call_args.kwargs["return_position"],
            {"x_mm": 123.0, "y_mm": 456.0},
        )

    def test_checkpoint_auto_dip_returns_to_live_hardware_position(self):
        job = {
            "id": "dip-job",
            "auto_dip_enabled": True,
            "dip_count": 0,
            "ink_well": {},
            "plot_start_position": {"x_mm": 10, "y_mm": 20},
        }
        layer = {"index": 1}
        server.jobs[job["id"]] = job

        with (
            mock.patch.object(server, "current_home_position", return_value={"x_mm": 10, "y_mm": 20}),
            mock.patch.object(server, "execute_dip_cycle", return_value={"return_error_mm": 0}) as dip,
            mock.patch.object(
                server,
                "current_hardware_bed_position_locked",
                return_value={"x_mm": 222, "y_mm": 333},
            ) as hardware_position,
            mock.patch.object(server, "run_layer", return_value="auto_dip_pause"),
            mock.patch.object(server, "resume_layer", return_value="done"),
            mock.patch.object(server, "save_job_unlocked"),
        ):
            result = server.run_layer_with_auto_dips(job, layer, mock.Mock())

        self.assertEqual(result, "done")
        hardware_position.assert_called_once()
        self.assertEqual(
            dip.call_args_list[0].kwargs["return_position"],
            {"x_mm": 10.0, "y_mm": 20.0},
        )
        self.assertEqual(
            dip.call_args_list[1].kwargs["return_position"],
            {"x_mm": 222, "y_mm": 333},
        )

    def test_dip_failure_never_starts_plot(self):
        job = {
            "id": "dip-job",
            "auto_dip_enabled": True,
            "dip_count": 0,
            "ink_well": {},
            "plot_start_position": {"x_mm": 10, "y_mm": 20},
        }
        layer = {"index": 1}
        server.jobs[job["id"]] = job

        with (
            mock.patch.object(server, "current_home_position", return_value={"x_mm": 10, "y_mm": 20}),
            mock.patch.object(server, "execute_dip_cycle", side_effect=RuntimeError("servo failed")),
            mock.patch.object(server, "attempt_dip_clearance_raise"),
            mock.patch.object(server, "run_layer") as run,
            mock.patch.object(server, "announce_on_linux_box"),
            mock.patch.object(server, "save_job_unlocked"),
        ):
            result = server.run_layer_with_auto_dips(job, layer, mock.Mock())

        self.assertEqual(result, "dip_failed")
        self.assertEqual(job["status"], "dip_failed")
        self.assertEqual(job["dip_failure"]["phase"], "initial")
        run.assert_not_called()

    def test_dip_cycle_requires_calibrated_software_position(self):
        job = {
            "id": "dip-job",
            "auto_dip_enabled": True,
            "ink_well": {
                "centre": {"x_mm": 10, "y_mm": 20},
                "clearance_pos": 80,
                "dip_pos": 20,
                "dwell_ms": 1000,
                "drip_dwell_ms": 0,
            },
        }

        with self.assertRaises(HTTPException) as raised:
            server.execute_dip_cycle(job, mock.Mock())

        self.assertEqual(raised.exception.status_code, 409)

    def test_validate_dip_interval(self):
        self.assertEqual(server.validate_dip_interval(0.1), 0.1)
        self.assertEqual(server.validate_dip_interval(0.25), 0.25)
        self.assertEqual(server.validate_dip_interval(60), 60.0)
        for value in (None, "bad", 0, 0.09, 86401, float("nan")):
            with self.subTest(value=value), self.assertRaises(HTTPException):
                server.validate_dip_interval(value)

    def test_updates_paused_job_dip_interval(self):
        job = {
            "id": "dip-job",
            "status": "paused",
            "auto_dip_enabled": True,
            "dip_interval_s": 60,
        }
        server.jobs[job["id"]] = job

        with mock.patch.object(server, "save_job_unlocked"):
            result = server.update_job_dip_interval(
                job["id"],
                {"dip_interval_s": 30},
                x_plotter_token="test-token",
            )

        self.assertEqual(result["dip_interval_s"], 30.0)
        self.assertEqual(job["dip_interval_s"], 30.0)
        self.assertIn("Existing prepared checkpoints are unchanged", job["operator_message"])

    def test_enables_auto_dip_for_queued_job_before_start(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            input_svg = Path(tmpdir) / "input.svg"
            input_svg.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="10mm" height="10mm"/>', encoding="utf-8")
            job = {
                "id": "queued-job",
                "status": "queued_for_operator",
                "layers": [
                    {
                        "index": 1,
                        "name": "Layer 1",
                        "input_svg": str(input_svg),
                        "svg_metrics": {"width_mm": 10.0, "height_mm": 10.0},
                    }
                ],
                "speed_pendown": 20,
                "speed_penup": 40,
                "pen_pos_down": 10,
                "pen_pos_up": 90,
            }
            server.jobs[job["id"]] = job

            def prepare(layer, _analysis):
                layer["plot_svg"] = str(Path(layer["plot_digest_svg"]).with_name("auto_dip_plot.svg"))
                layer["auto_dip_checkpoint_count"] = 2

            with (
                mock.patch.object(server, "current_ink_well_settings", return_value={"installed": True}),
                mock.patch.object(server, "ink_well_plot_snapshot", return_value={"centre": {"x_mm": 1, "y_mm": 2}}),
                mock.patch.object(server, "current_home_position", return_value={"x_mm": 3, "y_mm": 4}),
                mock.patch.object(server, "analyse_layer_for_ink_well", return_value={"dip_schedule": {"checkpoint_after_strokes": [1, 2]}}) as analyse,
                mock.patch.object(server, "prepare_auto_dip_layer", side_effect=prepare),
                mock.patch.object(server, "save_job_unlocked"),
            ):
                result = server.update_job_auto_dip(
                    job["id"],
                    {"auto_dip_enabled": True, "dip_interval_s": 45},
                    x_plotter_token="test-token",
                )

        self.assertTrue(result["auto_dip_enabled"])
        self.assertEqual(result["dip_interval_s"], 45.0)
        self.assertTrue(job["auto_dip_enabled"])
        self.assertEqual(job["plot_start_position"], {"x_mm": 3, "y_mm": 4})
        self.assertEqual(job["layers"][0]["auto_dip_checkpoint_count"], 2)
        self.assertTrue(job["layers"][0]["plot_svg"].endswith("auto_dip_plot.svg"))
        analyse.assert_called_once()
        server.jobs.pop(job["id"], None)

    def test_disables_auto_dip_for_queued_job_before_start(self):
        job = {
            "id": "queued-job",
            "status": "waiting_for_operator",
            "auto_dip_enabled": True,
            "dip_interval_s": 60,
            "dip_failure": {"error": "old"},
            "layers": [{"index": 1, "plot_svg": "/tmp/auto.svg", "auto_dip_checkpoint_count": 3}],
        }
        server.jobs[job["id"]] = job

        with mock.patch.object(server, "save_job_unlocked"):
            result = server.update_job_auto_dip(
                job["id"],
                {"auto_dip_enabled": False},
                x_plotter_token="test-token",
            )

        self.assertFalse(result["auto_dip_enabled"])
        self.assertFalse(job["auto_dip_enabled"])
        self.assertIsNone(job["dip_interval_s"])
        self.assertIsNone(job["layers"][0]["plot_svg"])
        self.assertEqual(job["layers"][0]["auto_dip_checkpoint_count"], 0)
        server.jobs.pop(job["id"], None)

    def test_start_operator_continue_applies_checked_auto_dip_before_releasing_prompt(self):
        request = mock.Mock()
        request.client.host = "127.0.0.1"
        job = {
            "id": "start-job",
            "status": "waiting_for_operator",
            "auto_dip_enabled": False,
        }
        server.jobs[job["id"]] = job
        with server.operator_lock:
            server.operator_prompt = {
                "active": True,
                "job_id": job["id"],
                "message": "ready",
                "action": "start",
                "created_at": 123,
            }

        with (
            mock.patch.object(server, "configure_job_auto_dip") as configure,
            mock.patch.object(server, "save_job_unlocked") as save_job,
            mock.patch.object(server.operator_event, "set") as release,
        ):
            result = server.operator_continue(
                request,
                {"auto_dip_enabled": True, "dip_interval_s": 45},
            )

        self.assertTrue(result["ok"])
        configure.assert_called_once_with(job, enabled=True, dip_interval_s=45)
        save_job.assert_called_once_with(job["id"])
        release.assert_called_once()
        server.jobs.pop(job["id"], None)
        with server.operator_lock:
            server.operator_prompt = {
                "active": False,
                "job_id": None,
                "message": None,
                "action": None,
                "created_at": None,
            }

    def test_start_operator_continue_applies_unchecked_auto_dip_before_releasing_prompt(self):
        request = mock.Mock()
        request.client.host = "127.0.0.1"
        job = {
            "id": "start-job",
            "status": "waiting_for_operator",
            "auto_dip_enabled": True,
        }
        server.jobs[job["id"]] = job
        with server.operator_lock:
            server.operator_prompt = {
                "active": True,
                "job_id": job["id"],
                "message": "ready",
                "action": "start",
                "created_at": 123,
            }

        with (
            mock.patch.object(server, "configure_job_auto_dip") as configure,
            mock.patch.object(server, "save_job_unlocked"),
            mock.patch.object(server.operator_event, "set"),
        ):
            server.operator_continue(
                request,
                {"auto_dip_enabled": False, "dip_interval_s": 45},
            )

        configure.assert_called_once_with(job, enabled=False, dip_interval_s=45)
        server.jobs.pop(job["id"], None)
        with server.operator_lock:
            server.operator_prompt = {
                "active": False,
                "job_id": None,
                "message": None,
                "action": None,
                "created_at": None,
            }

    def test_start_operator_continue_ignores_default_auto_dip_when_ink_well_is_off(self):
        request = mock.Mock()
        request.client.host = "127.0.0.1"
        plot_defaults = server.current_plot_settings()
        plot_defaults["auto_dip_enabled"] = True
        plot_defaults["dip_interval_s"] = 45
        job = {
            "id": "start-job",
            "status": "waiting_for_operator",
            "auto_dip_enabled": True,
        }
        server.jobs[job["id"]] = job
        with server.operator_lock:
            server.operator_prompt = {
                "active": True,
                "job_id": job["id"],
                "message": "ready",
                "action": "start",
                "created_at": 123,
            }

        with (
            mock.patch.object(server, "current_plot_settings", return_value=plot_defaults),
            mock.patch.object(server, "current_ink_well_settings", return_value={"installed": False}),
            mock.patch.object(server, "configure_job_auto_dip") as configure,
            mock.patch.object(server, "save_job_unlocked"),
            mock.patch.object(server.operator_event, "set"),
        ):
            server.operator_continue(request, {})

        configure.assert_called_once_with(job, enabled=False, dip_interval_s=None)
        server.jobs.pop(job["id"], None)
        with server.operator_lock:
            server.operator_prompt = {
                "active": False,
                "job_id": None,
                "message": None,
                "action": None,
                "created_at": None,
            }

    def test_dip_now_keeps_paused_job_paused(self):
        job = {
            "id": "dip-job",
            "status": "paused",
            "auto_dip_enabled": True,
            "dip_count": 1,
            "ink_well": {"centre": {"x_mm": 10, "y_mm": 20}},
            "log_path": "/tmp/dip-job-test.log",
            "current_layer": 1,
        }
        server.jobs[job["id"]] = job

        with (
            mock.patch.object(server, "require_hardware_idle"),
            mock.patch.object(server, "current_hardware_bed_position_locked", return_value={"x_mm": 5, "y_mm": 6}),
            mock.patch.object(server, "execute_dip_cycle", return_value={"return_error_mm": 0}) as dip,
            mock.patch.object(server, "save_job_unlocked"),
        ):
            result = server.dip_paused_job_now(job["id"], x_plotter_token="test-token")

        self.assertEqual(result["status"], "paused")
        self.assertEqual(job["status"], "paused")
        self.assertEqual(job["dip_count"], 2)
        self.assertTrue(job["manual_dip_needs_pen_down"])
        dip.assert_called_once()
        self.assertEqual(dip.call_args.kwargs["return_position"], {"x_mm": 5, "y_mm": 6})

    def test_manual_dip_primes_pen_down_once_before_resume(self):
        job = {
            "id": "dip-job",
            "auto_dip_enabled": True,
            "dip_count": 0,
            "ink_well": {},
            "manual_dip_needs_pen_down": True,
            "pen_pos_down": 0,
            "pen_pos_up": 100,
            "pen_delay_down": -50,
            "pen_delay_up": -175,
        }
        layer = {"index": 1}
        server.jobs[job["id"]] = job

        with (
            mock.patch.object(server, "prime_pen_down_after_manual_dip", return_value={"ok": True}) as prime,
            mock.patch.object(server, "resume_layer", return_value="done"),
            mock.patch.object(server, "save_job_unlocked"),
        ):
            result = server.run_layer_with_auto_dips(job, layer, mock.Mock(), resume=True)

        self.assertEqual(result, "done")
        prime.assert_called_once()


if __name__ == "__main__":
    unittest.main()
