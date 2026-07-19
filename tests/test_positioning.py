import unittest

from server import positioning


class PositioningTests(unittest.TestCase):
    def test_raw_xy_to_bed_xy_swaps_and_negates_axes(self):
        result = positioning.raw_xy_to_bed_xy({"x_mm": 100.0, "y_mm": 50.0})
        self.assertEqual(result, {"x_mm": -50.0, "y_mm": -100.0})

    def test_raw_xy_to_bed_xy_none_input(self):
        self.assertIsNone(positioning.raw_xy_to_bed_xy(None))

    def test_bed_delta_to_raw_delta_same_transform(self):
        result = positioning.bed_delta_to_raw_delta(30.0, 70.0)
        self.assertEqual(result, {"x_mm": -70.0, "y_mm": -30.0})

    def test_clamp_within_bounds(self):
        result = positioning.clamp_bed_position(100.0, 200.0, bed_width_mm=600.0, bed_height_mm=900.0)
        self.assertEqual(result, {"x_mm": 100.0, "y_mm": 200.0})

    def test_clamp_negative_to_zero(self):
        result = positioning.clamp_bed_position(-10.0, -20.0, bed_width_mm=600.0, bed_height_mm=900.0)
        self.assertEqual(result, {"x_mm": 0.0, "y_mm": 0.0})

    def test_clamp_over_max(self):
        result = positioning.clamp_bed_position(700.0, 1000.0, bed_width_mm=600.0, bed_height_mm=900.0)
        self.assertEqual(result, {"x_mm": 600.0, "y_mm": 900.0})


if __name__ == "__main__":
    unittest.main()
