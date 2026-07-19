import unittest

from server import job_model


def _layer(width, height):
    return {"svg_metrics": {"width_mm": width, "height_mm": height}}


class PlotFootprintTests(unittest.TestCase):
    def test_returns_max_dimensions_across_layers(self):
        job = {"layers": [_layer(100, 200), _layer(150, 120)]}
        result = job_model.plot_footprint(job)
        self.assertEqual(result, {"width_mm": 150.0, "height_mm": 200.0})

    def test_returns_none_for_no_valid_layers(self):
        self.assertIsNone(job_model.plot_footprint({}))
        self.assertIsNone(job_model.plot_footprint({"layers": []}))
        self.assertIsNone(job_model.plot_footprint({"layers": [{"svg_metrics": None}]}))

    def test_skips_non_finite_metrics(self):
        job = {"layers": [_layer(100, 200), {"svg_metrics": {"width_mm": "bad", "height_mm": 50}}]}
        result = job_model.plot_footprint(job)
        self.assertEqual(result, {"width_mm": 100.0, "height_mm": 200.0})


class PlotOriginForPaperTests(unittest.TestCase):
    def _paper(self, width=300, height=400, tr_x=300, tr_y=400):
        return {
            "enabled": True,
            "width_mm": width,
            "height_mm": height,
            "top_right": {"x_mm": tr_x, "y_mm": tr_y},
            "size": "A4",
            "orientation": "portrait",
        }

    def test_origin_at_paper_top_right(self):
        job = {"layers": [_layer(100, 200)]}
        result = job_model.plot_origin_for_paper(job, self._paper(), validate_bed_target=lambda x, y: (x, y))
        self.assertEqual(result["x_mm"], 200.0)
        self.assertEqual(result["y_mm"], 400.0)
        self.assertEqual(result["anchor"], "paper_top_right")

    def test_disabled_paper_returns_none(self):
        paper = self._paper()
        paper["enabled"] = False
        self.assertIsNone(job_model.plot_origin_for_paper({"layers": [_layer(100, 200)]}, paper, validate_bed_target=lambda x, y: (x, y)))

    def test_plot_larger_than_paper_raises(self):
        job = {"layers": [_layer(500, 500)]}
        with self.assertRaises(ValueError) as ctx:
            job_model.plot_origin_for_paper(job, self._paper(width=300, height=400), validate_bed_target=lambda x, y: (x, y))
        self.assertIn("exceeds", str(ctx.exception))

    def test_validates_bed_targets(self):
        job = {"layers": [_layer(100, 200)]}
        calls = []

        def validate(x, y):
            calls.append((x, y))
            return (x, y)

        job_model.plot_origin_for_paper(job, self._paper(), validate_bed_target=validate)
        self.assertEqual(len(calls), 2)


class LayerDipEstimatesTests(unittest.TestCase):
    def test_extracts_dip_schedules(self):
        layers = [
            {"ink_analysis": {"dip_schedule": {"checkpoint_after_strokes": [3, 7]}}},
            {"ink_analysis": None},
            {"ink_analysis": {"dip_schedule": None}},
        ]
        result = job_model.layer_dip_estimates(layers)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["checkpoint_after_strokes"], [3, 7])


if __name__ == "__main__":
    unittest.main()
