import tempfile
import unittest
from pathlib import Path

from server import svg_utils


SVG_VALID = '<svg xmlns="http://www.w3.org/2000/svg" width="200mm" height="150mm"></svg>'
SVG_VIEWBOX = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 150"></svg>'
SVG_TOO_BIG = '<svg xmlns="http://www.w3.org/2000/svg" width="999mm" height="999mm"></svg>'
SVG_INCHES = '<svg xmlns="http://www.w3.org/2000/svg" width="10in" height="5in"></svg>'
SVG_NON_SVG = '<html><body>not svg</body></html>'
SVG_NO_DIMS = '<svg xmlns="http://www.w3.org/2000/svg"></svg>'
SVG_INVALID_XML = '<svg><unclosed>'


class SvgUtilsTests(unittest.TestCase):
    def test_valid_svg_with_mm_dimensions(self):
        result = svg_utils.validate_svg_text(SVG_VALID, max_width_mm=600, max_height_mm=400)
        self.assertEqual(result, {"width_mm": 200.0, "height_mm": 150.0})

    def test_viewbox_fallback(self):
        result = svg_utils.validate_svg_text(SVG_VIEWBOX, max_width_mm=600, max_height_mm=400)
        self.assertEqual(result, {"width_mm": 200.0, "height_mm": 150.0})

    def test_inch_conversion(self):
        result = svg_utils.validate_svg_text(SVG_INCHES, max_width_mm=600, max_height_mm=400)
        self.assertAlmostEqual(result["width_mm"], 254.0, places=2)
        self.assertAlmostEqual(result["height_mm"], 127.0, places=2)

    def test_rejects_too_large(self):
        with self.assertRaises(ValueError) as ctx:
            svg_utils.validate_svg_text(SVG_TOO_BIG, max_width_mm=600, max_height_mm=400)
        self.assertIn("exceeds plotter bounds", str(ctx.exception))

    def test_rejects_non_svg_root(self):
        with self.assertRaises(ValueError) as ctx:
            svg_utils.validate_svg_text(SVG_NON_SVG, max_width_mm=600, max_height_mm=400)
        self.assertIn("not an SVG", str(ctx.exception))

    def test_rejects_missing_dimensions(self):
        with self.assertRaises(ValueError) as ctx:
            svg_utils.validate_svg_text(SVG_NO_DIMS, max_width_mm=600, max_height_mm=400)
        self.assertIn("positive width and height", str(ctx.exception))

    def test_rejects_invalid_xml(self):
        with self.assertRaises(ValueError) as ctx:
            svg_utils.validate_svg_text(SVG_INVALID_XML, max_width_mm=600, max_height_mm=400)
        self.assertIn("Invalid SVG XML", str(ctx.exception))

    def test_validate_svg_file_reads_file(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".svg", delete=False) as f:
            f.write(SVG_VALID)
            f.flush()
            result = svg_utils.validate_svg_file(Path(f.name), max_width_mm=600, max_height_mm=400)
        self.assertEqual(result, {"width_mm": 200.0, "height_mm": 150.0})


if __name__ == "__main__":
    unittest.main()
