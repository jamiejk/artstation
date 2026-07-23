import unittest

from server import pen_profiles


class PenProfileTests(unittest.TestCase):
    def test_standard_profile_does_not_enable_gradual_motion(self):
        snapshot = pen_profiles.job_snapshot("standard")

        self.assertEqual(snapshot["pen_profile_name"], "Standard")
        self.assertFalse(pen_profiles.gradual_enabled(snapshot))

    def test_marsmatic_profile_uses_physically_selected_ramp(self):
        snapshot = pen_profiles.job_snapshot("staedtler_marsmatic")

        self.assertEqual(snapshot["pen_profile_name"], "Staedtler Marsmatic")
        self.assertEqual(snapshot["gradual_ramp_mm"], 4.375)
        self.assertEqual(snapshot["gradual_exit_ramp_mm"], 4.6875)
        self.assertEqual(snapshot["gradual_tail_mm"], 0.4375)
        self.assertEqual(snapshot["gradual_segment_mm"], 0.5)
        self.assertTrue(pen_profiles.gradual_enabled(snapshot))

    def test_unknown_profile_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unknown pen profile"):
            pen_profiles.resolve_profile("unknown")


if __name__ == "__main__":
    unittest.main()
