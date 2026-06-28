import tempfile
import unittest
from pathlib import Path

from server import hardware


class FakeSerialPort:
    def __init__(self, responses):
        self.responses = list(responses)
        self.writes = []

    def write(self, data):
        self.writes.append(data.decode("ascii"))

    def readline(self):
        if not self.responses:
            return b""
        return (self.responses.pop(0) + "\n").encode("ascii")


class HardwareProtocolTests(unittest.TestCase):
    def test_step_conversion_round_trips_xy_mm(self):
        axis_1, axis_2 = hardware.xy_mm_to_steps(25.4, 12.7)
        xy = hardware.steps_to_xy_mm(axis_1, axis_2)

        self.assertAlmostEqual(xy["x_mm"], 25.4, places=3)
        self.assertAlmostEqual(xy["y_mm"], 12.7, places=3)

    def test_direct_pen_servo_writes_expected_standard_commands(self):
        port = FakeSerialPort(["OK", "OK", "OK", "OK", "OK", "OK"])

        with tempfile.TemporaryDirectory() as tmpdir:
            config = Path(tmpdir) / "axidraw_servo_conf.py"
            config.write_text("servo_min = 9000\nservo_max = 28500\n", encoding="utf-8")
            result = hardware.run_pen_servo_on_port(
                port,
                axicli_config=config,
                raised=False,
                up_pos=100,
                down_pos=0,
                raise_rate=100,
                lower_rate=20,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["method"], "direct_ebb")
        self.assertEqual(port.writes[:5], ["SC,4,28500\r", "SC,5,9000\r", "SC,11,2340\r", "SC,12,468\r", "SC,8,8\r"])
        self.assertTrue(port.writes[5].startswith("SP,0,"))
        self.assertTrue(port.writes[5].endswith(",1\r"))

    def test_move_to_bed_target_sends_sm_and_returns_updated_position(self):
        axis_1, axis_2 = hardware.xy_mm_to_steps(25.4, 0.0)
        port = FakeSerialPort(["OK", "0", f"{axis_1},{axis_2}", "OK"])

        result = hardware.move_to_bed_target_on_port(
            port,
            {"x_mm": 25.4, "y_mm": 0.0},
            current_position={"x_mm": 0.0, "y_mm": 0.0},
            bed_delta_to_raw_delta=lambda x, y: {"x_mm": x, "y_mm": y},
            validate_bed_target=lambda x, y: (x, y),
            update_position=lambda raw: raw,
            speed_mm_s=25.4,
        )

        self.assertEqual(port.writes[0], f"SM,1000,{axis_1},{axis_2}\r")
        self.assertEqual(port.writes[1], "QG\r")
        self.assertEqual(port.writes[2], "QS\r")
        self.assertEqual(result, {"x_mm": 25.4, "y_mm": 0.0})


if __name__ == "__main__":
    unittest.main()
