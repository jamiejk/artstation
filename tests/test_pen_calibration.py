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

from server import pen_calibration

server = importlib.import_module("server.server")


class PenCalibrationGeometryTests(unittest.TestCase):
    def test_working_range_ladder_spans_lift_to_lower(self):
        heights = pen_calibration.contact_heights_for_working_range(100, 80, step=2, extra_deep=3)
        self.assertEqual(heights[0], 99)  # just under max lift
        self.assertEqual(heights[-1], 77)  # max lower - 3
        self.assertIn(80, heights)  # always includes max lower
        self.assertTrue(all(h < 100 for h in heights))

    def test_footprint_uses_working_range(self):
        footprint = pen_calibration.calibration_footprint(pen_up=100, pen_down=80, contact_step=2)
        self.assertEqual(footprint["pen_up"], 100)
        self.assertEqual(footprint["pen_down"], 80)
        self.assertEqual(footprint["contact_high"], 99)
        self.assertLessEqual(footprint["contact_low"], 80)

    def test_action_sequence_has_absolute_height_taps(self):
        actions = pen_calibration.build_calibration_actions(
            {"x_mm": 100.0, "y_mm": 200.0},
            pen_up=100,
            pen_down=80,
            contact_step=5,
            extra_deep=0,
        )
        height_actions = [a for a in actions if a["type"] == "height"]
        pen_actions = [a for a in actions if a["type"] == "pen"]
        move_actions = [a for a in actions if a["type"] == "move"]

        # First height is max lift
        self.assertEqual(height_actions[0]["height"], 100.0)
        # Contact taps include working heights
        tap_heights = [a["height"] for a in height_actions if "Contact tap" in a["label"]]
        self.assertIn(99.0, tap_heights)
        self.assertIn(80.0, tap_heights)
        # Clearance still uses binary pen
        self.assertTrue(any(not a["raised"] for a in pen_actions))
        self.assertEqual(move_actions[-1]["x_mm"], 100.0)

    def test_rejects_pattern_that_leaves_the_bed(self):
        def validate(x, y):
            if x > 50:
                raise HTTPException(status_code=400, detail="out of bed")
            return x, y

        with self.assertRaises(HTTPException):
            pen_calibration.build_calibration_actions(
                {"x_mm": 40.0, "y_mm": 10.0},
                pen_up=100,
                pen_down=80,
                validate_bed_target=validate,
            )

    def test_describe_mentions_working_range(self):
        text = pen_calibration.describe_what_to_look_for(pen_up=100, pen_down=80)
        self.assertIn("80", text)
        self.assertIn("100", text)
        self.assertIn("max lower", text.lower())


class PenCalibrationEndpointTests(unittest.TestCase):
    def setUp(self):
        with server.position_lock:
            server.position_current = {"x_mm": 100.0, "y_mm": 200.0}
            server.position_bed_calibrated = True
            server.position_calibration_enabled = True
        with server.pen_settings_lock:
            server.pen_settings["pen_pos_up"] = 100
            server.pen_settings["pen_pos_down"] = 80

    def test_plotter_pen_calibrate_runs_height_and_pen_actions(self):
        request = mock.Mock()
        request.client.host = "127.0.0.1"
        serial_port = object()
        height_calls = []
        pen_calls = []

        def fake_height(port, **kwargs):
            height_calls.append(kwargs)
            return {"ok": True, "height": kwargs["height"], "pwm": 1, "delay_ms": 40}

        def fake_pen(port, **kwargs):
            pen_calls.append(kwargs)
            return {"ok": True}

        def fake_move(port, target, **kwargs):
            return dict(target)

        with (
            mock.patch.object(server, "require_hardware_idle"),
            mock.patch.object(server, "mark_manual_hardware_priority"),
            mock.patch.object(server, "require_enabled_high_resolution_motors"),
            mock.patch.object(server.serial, "Serial") as serial_cls,
            mock.patch.object(server.hardware, "run_pen_to_height_on_port", side_effect=fake_height),
            mock.patch.object(server, "_run_pen_servo_on_port_locked", side_effect=fake_pen),
            mock.patch.object(server, "_move_to_bed_target_on_port_locked", side_effect=fake_move),
            mock.patch.object(server, "save_pen_settings_unlocked"),
            mock.patch.object(server.time, "sleep"),
        ):
            serial_cls.return_value.__enter__.return_value = serial_port
            result = server.plotter_pen_calibrate(
                request,
                {"pen_pos_up": 100, "pen_pos_down": 80, "contact_step": 5, "extra_deep": 0},
                x_plotter_token="test-token",
            )

        self.assertTrue(result["ok"])
        heights = result.get("contact_heights") or result["footprint"]["contact_heights"]
        self.assertIn(80, heights)
        self.assertGreater(len(height_calls), 3)
        self.assertGreater(len(pen_calls), 2)
        self.assertIn("max lower", result["message"].lower())

    def test_plotter_pen_calibrate_requires_up_above_down(self):
        request = mock.Mock()
        request.client.host = "127.0.0.1"
        with mock.patch.object(server, "require_hardware_idle"):
            with self.assertRaises(HTTPException) as ctx:
                server.plotter_pen_calibrate(
                    request,
                    {"pen_pos_up": 50, "pen_pos_down": 60},
                    x_plotter_token="test-token",
                )
        self.assertEqual(ctx.exception.status_code, 400)

    def test_plotter_pen_seat_goes_to_lower_plus_offset(self):
        request = mock.Mock()
        request.client.host = "127.0.0.1"
        with server.pen_settings_lock:
            server.pen_settings["pen_pos_up"] = 100
            server.pen_settings["pen_pos_down"] = 80
        serial_port = object()
        with (
            mock.patch.object(server, "require_hardware_idle"),
            mock.patch.object(server, "mark_manual_hardware_priority"),
            mock.patch.object(server.serial, "Serial") as serial_cls,
            mock.patch.object(
                server.hardware,
                "run_pen_to_height_on_port",
                return_value={"ok": True, "method": "direct_ebb_absolute", "height": 85, "pwm": 20000, "delay_ms": 80},
            ) as height_fn,
        ):
            serial_cls.return_value.__enter__.return_value = serial_port
            result = server.plotter_pen_seat(request, {}, x_plotter_token="test-token")
        self.assertTrue(result["ok"])
        self.assertEqual(result["seat_height"], 85.0)
        self.assertEqual(height_fn.call_args.kwargs["height"], 85.0)

    def test_plotter_pen_seat_requires_lift_above_lower(self):
        request = mock.Mock()
        request.client.host = "127.0.0.1"
        with mock.patch.object(server, "require_hardware_idle"):
            with self.assertRaises(HTTPException) as ctx:
                server.plotter_pen_seat(
                    request,
                    {"pen_pos_up": 70, "pen_pos_down": 80},
                    x_plotter_token="test-token",
                )
        self.assertEqual(ctx.exception.status_code, 400)

    def test_plotter_pen_jog_steps_absolute_height(self):
        request = mock.Mock()
        request.client.host = "127.0.0.1"
        server.set_pen_live_height(50.0)
        serial_port = object()
        with (
            mock.patch.object(server, "require_hardware_idle"),
            mock.patch.object(server, "mark_manual_hardware_priority"),
            mock.patch.object(server.serial, "Serial") as serial_cls,
            mock.patch.object(
                server.hardware,
                "run_pen_to_height_on_port",
                return_value={"ok": True, "method": "direct_ebb_absolute", "height": 52, "pwm": 19000, "delay_ms": 50},
            ) as jog_fn,
        ):
            serial_cls.return_value.__enter__.return_value = serial_port
            result = server.plotter_pen_jog(
                request,
                {"delta": 2},
                x_plotter_token="test-token",
            )
        self.assertTrue(result["ok"])
        self.assertEqual(result["pen_live_height"], 52.0)
        self.assertEqual(jog_fn.call_args.kwargs["height"], 52.0)


if __name__ == "__main__":
    unittest.main()
