import importlib.util
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOFTOUT_PATH = ROOT / "vendor" / "axidraw-softout" / "axidrawinternal" / "softout.py"
SPEC = importlib.util.spec_from_file_location("axidraw_softout_transform", SOFTOUT_PATH)
softout = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(softout)
VENDOR_ROOT = ROOT / "vendor" / "axidraw-softout"
sys.path.insert(0, str(VENDOR_ROOT))
from axidrawinternal import gradual
sys.path.pop(0)


def sm(steps_2, steps_1, duration_ms, x, y, distance):
    return ["SM", (steps_2, steps_1, duration_ms), [x, y, False, distance]]


class SoftOutTransformTests(unittest.TestCase):
    def test_zero_is_exactly_disabled(self):
        moves = [sm(100, -20, 100, 1.0, 0.0, 1.0)]
        transformed, applied = softout.overlap_final_lift(moves, 0.0, 0.0, 0)
        self.assertFalse(applied)
        self.assertEqual(transformed, moves)

    def test_splits_final_move_and_preserves_motion_totals(self):
        moves = [sm(100, -20, 100, 1.0, 0.0, 1.0)]
        transformed, applied = softout.overlap_final_lift(
            moves, 0.0, 0.0, 5.08
        )

        self.assertTrue(applied)
        self.assertEqual([move[0] for move in transformed],
                         ["SM", "soft_raise", "SM", "soft_raise_finish"])
        xy_moves = [move for move in transformed if move[0] == "SM"]
        self.assertEqual(sum(move[1][0] for move in xy_moves), 100)
        self.assertEqual(sum(move[1][1] for move in xy_moves), -20)
        self.assertEqual(sum(move[1][2] for move in xy_moves), 100)
        self.assertAlmostEqual(sum(move[2][3] for move in xy_moves), 1.0)
        self.assertEqual(xy_moves[-1][2][0:2], [1.0, 0.0])
        self.assertAlmostEqual(xy_moves[0][2][0], 0.8)

    def test_overlap_is_capped_at_half_of_short_path(self):
        moves = [sm(40, 0, 40, 0.1, 0.0, 0.1)]
        transformed, applied = softout.overlap_final_lift(
            moves, 0.0, 0.0, 50
        )

        self.assertTrue(applied)
        xy_moves = [move for move in transformed if move[0] == "SM"]
        self.assertAlmostEqual(xy_moves[0][2][3], 0.05)
        self.assertAlmostEqual(xy_moves[1][2][3], 0.05)

    def test_inserts_into_last_relevant_segment(self):
        moves = [
            sm(10, 0, 10, 0.25, 0.0, 0.25),
            sm(30, 0, 30, 1.0, 0.0, 0.75),
        ]
        transformed, applied = softout.overlap_final_lift(
            moves, 0.0, 0.0, 6.35
        )

        self.assertTrue(applied)
        self.assertEqual(transformed[0], moves[0])
        self.assertEqual([move[0] for move in transformed],
                         ["SM", "SM", "soft_raise", "SM", "soft_raise_finish"])
        xy_moves = [move for move in transformed if move[0] == "SM"]
        self.assertEqual(sum(move[1][0] for move in xy_moves), 40)
        self.assertEqual(sum(move[1][2] for move in xy_moves), 40)


class GradualProfileTransformTests(unittest.TestCase):
    def test_zero_ramp_is_exactly_disabled(self):
        moves = [sm(100, -20, 100, 1.0, 0.0, 1.0)]
        transformed, applied = gradual.gradual_entry_exit(
            moves,
            0.0,
            0.0,
            ramp_mm=0,
            tail_mm=0,
            segment_mm=0.5,
            pen_up=90,
            pen_down=80,
        )
        self.assertFalse(applied)
        self.assertEqual(transformed, moves)

    def test_linear_profile_preserves_all_xy_totals(self):
        moves = [sm(1000, -200, 1000, 4.0, 0.0, 4.0)]
        transformed, applied = gradual.gradual_entry_exit(
            moves,
            0.0,
            0.0,
            ramp_mm=4.375,
            tail_mm=0.4375,
            segment_mm=0.5,
            pen_up=90,
            pen_down=80,
        )

        self.assertTrue(applied)
        self.assertEqual(transformed[0][0], "profile_begin")
        self.assertEqual(transformed[-1][0], "profile_finish")
        xy_moves = [move for move in transformed if move[0] in {"SM", "profile_SM"}]
        self.assertEqual(sum(move[1][0] for move in xy_moves), 1000)
        self.assertEqual(sum(move[1][1] for move in xy_moves), -200)
        self.assertEqual(sum(move[1][2] for move in xy_moves), 1000)
        self.assertAlmostEqual(sum(move[2][3] for move in xy_moves), 4.0)
        self.assertEqual(xy_moves[-1][2][0:2], [4.0, 0.0])

    def test_height_targets_lower_then_raise(self):
        moves = [sm(1000, 0, 1000, 4.0, 0.0, 4.0)]
        transformed, _ = gradual.gradual_entry_exit(
            moves,
            0.0,
            0.0,
            ramp_mm=4.375,
            tail_mm=0.4375,
            segment_mm=0.5,
            pen_up=90,
            pen_down=80,
        )
        heights = [move[1] for move in transformed if move[0] == "profile_height"]
        minimum = heights.index(min(heights))
        self.assertEqual(heights[0], 90)
        self.assertEqual(heights[minimum], 80)
        self.assertEqual(heights[-1], 90)
        self.assertTrue(all(a >= b for a, b in zip(heights[:minimum], heights[1:minimum + 1])))
        self.assertTrue(all(a <= b for a, b in zip(heights[minimum:], heights[minimum + 1:])))


class SoftOutCliTests(unittest.TestCase):
    def test_wrapper_loads_vendored_cli_option(self):
        result = subprocess.run(
            [str(ROOT / "scripts" / "axicli-softout"), "--help"],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertIn("--soft_out_mm", result.stdout)
        self.assertIn("--gradual_exit_ramp_mm", result.stdout)


if __name__ == "__main__":
    unittest.main()
