import importlib
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
        with mock.patch.object(server, "serial_query", side_effect=replies) as query:
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
        with mock.patch.object(server, "serial_query", side_effect=replies) as query:
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
        )

        self.assertEqual(estimate["checkpoint_after_strokes"], [2])
        self.assertEqual(estimate["estimated_dip_count_per_layer"], 2)

    def test_does_not_schedule_redundant_dip_after_final_stroke(self):
        strokes = [[(0.0, 0.0), (100.0, 0.0)]]
        estimate = ink_dip.estimate_checkpoint_schedule(
            strokes,
            speed_pendown=10,
            interval_s=0.1,
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

    def test_plot_snapshot_is_independent_from_later_calibration_changes(self):
        settings = {
            "installed": True,
            "centre": {"x_mm": 10, "y_mm": 20},
            "radius_mm": 15,
            "clearance_pos": 80,
            "dip_pos": 20,
            "dwell_ms": 1000,
            "drip_dwell_ms": 100,
            "dip_circle_count": 3,
            "dip_circle_diameter_mm": 10,
            "calibration_id": "test-calibration",
            "test_passed": True,
            "tested_at": 123,
        }
        snapshot = server.ink_well_plot_snapshot(settings)
        settings["centre"]["x_mm"] = 99

        self.assertEqual(snapshot["centre"]["x_mm"], 10.0)
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

    def test_ink_well_test_confirmation_marks_passed_after_cycle(self):
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
                    "test_passed": False,
                    "tested_at": 123,
                }
            )

        result = server.plotter_ink_well_confirm_test(request, x_plotter_token="test-token")

        self.assertTrue(result["ink_well"]["test_passed"])


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
