import tempfile
import unittest
from pathlib import Path
import xml.etree.ElementTree as ET

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

    def test_rotates_clockwise_and_swaps_physical_dimensions(self):
        source = (
            '<svg xmlns="http://www.w3.org/2000/svg" width="200mm" height="150mm" '
            'viewBox="10 20 400 300"><path d="M 10 20 L 410 320"/></svg>'
        )

        rotated = svg_utils.rotate_svg_text(source, 90)
        root = ET.fromstring(rotated)
        group = list(root)[0]

        self.assertEqual(root.attrib["width"], "150mm")
        self.assertEqual(root.attrib["height"], "200mm")
        self.assertEqual(root.attrib["viewBox"], "0 0 300 400")
        self.assertEqual(group.attrib["transform"], "matrix(0 1 -1 0 320 -10)")
        self.assertEqual(
            svg_utils.validate_svg_text(rotated, max_width_mm=600, max_height_mm=400),
            {"width_mm": 150.0, "height_mm": 200.0},
        )

    def test_rotates_counterclockwise_with_270_degree_choice(self):
        source = (
            '<svg xmlns="http://www.w3.org/2000/svg" width="200mm" height="150mm" '
            'viewBox="10 20 400 300"><circle cx="20" cy="30" r="5"/></svg>'
        )

        rotated = svg_utils.rotate_svg_text(source, 270)
        root = ET.fromstring(rotated)

        self.assertEqual(root.attrib["viewBox"], "0 0 300 400")
        self.assertEqual(list(root)[0].attrib["transform"], "matrix(0 -1 1 0 -20 410)")

    def test_rotation_creates_viewbox_for_physical_size_svg(self):
        rotated = svg_utils.rotate_svg_text(SVG_VALID, 90)
        root = ET.fromstring(rotated)
        _, _, width, height = (float(value) for value in root.attrib["viewBox"].split())

        self.assertAlmostEqual(width, 150 * 96 / 25.4, places=6)
        self.assertAlmostEqual(height, 200 * 96 / 25.4, places=6)

    def test_rotation_can_make_originally_wide_svg_fit_bed(self):
        source = (
            '<svg xmlns="http://www.w3.org/2000/svg" width="800mm" height="500mm" '
            'viewBox="0 0 800 500"/>'
        )
        rotated = svg_utils.rotate_svg_text(source, 90)

        self.assertEqual(
            svg_utils.validate_svg_text(rotated, max_width_mm=609.6, max_height_mm=914.4),
            {"width_mm": 500.0, "height_mm": 800.0},
        )

    def test_zero_rotation_preserves_original_text(self):
        self.assertEqual(svg_utils.rotate_svg_text(SVG_VALID, 0), SVG_VALID)

    def test_rejects_non_quarter_turn_rotation(self):
        for value in (45, 90.5, "left", 360):
            with self.subTest(value=value), self.assertRaises(ValueError):
                svg_utils.validate_rotation_degrees(value)


if __name__ == "__main__":
    unittest.main()
