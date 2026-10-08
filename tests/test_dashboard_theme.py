"""Unit tests for CareerOS sage dashboard theme integration.

Tests theme file existence, theme CSS loading, color tokens,
SVG brand mark, reduced motion support, and navigation mapping.
IMPORTANT: Does not touch or connect to any database.
"""

import unittest
from pathlib import Path
from src.dashboard_theme import (
    CAREEROS_SVG_MARK,
    MODE_LABELS,
    WORKSPACE_MODES,
    format_mode_label,
    get_theme_css,
    render_brand_header,
    render_login_header,
)

ROOT = Path(__file__).resolve().parents[1]


class TestDashboardTheme(unittest.TestCase):
    def test_css_file_exists_and_loads(self):
        css = get_theme_css()
        self.assertTrue(len(css) > 500, "Theme CSS should not be empty")

    def test_approved_sage_color_tokens(self):
        css = get_theme_css()
        # Canvas off-white #edf1ec
        self.assertIn("#edf1ec", css)
        # Panels #fcfdf9
        self.assertIn("#fcfdf9", css)
        # Navigation #263f35
        self.assertIn("#263f35", css)
        # Actions #315e52
        self.assertIn("#315e52", css)
        # Borders #dce3d7
        self.assertIn("#dce3d7", css)
        # Text #253c32
        self.assertIn("#253c32", css)

    def test_prefers_reduced_motion_present(self):
        css = get_theme_css()
        self.assertIn("prefers-reduced-motion", css)
        self.assertIn("animation: none", css)

    def test_button_and_panel_timing_tokens(self):
        css = get_theme_css()
        self.assertIn("140ms", css)
        self.assertIn("240ms", css)

    def test_svg_mark_structure(self):
        self.assertIn('class="os-mark"', CAREEROS_SVG_MARK)
        self.assertIn("<path", CAREEROS_SVG_MARK)
        self.assertIn("#b8d4b2", CAREEROS_SVG_MARK)

    def test_workspace_modes_and_label_mapping(self):
        self.assertIn("Genel Bakış", WORKSPACE_MODES)
        self.assertIn("İlan Değerlendirme", WORKSPACE_MODES)
        self.assertIn("Performans & Analiz", WORKSPACE_MODES)
        self.assertIn("Belgelerim", WORKSPACE_MODES)

        self.assertEqual(format_mode_label("Genel Bakış"), "Bugün")
        self.assertEqual(format_mode_label("İlan Değerlendirme"), "İlan Masası")
        self.assertEqual(format_mode_label("Performans & Analiz"), "Analiz")
        self.assertEqual(format_mode_label("Belgelerim"), "Belgeler")

    def test_render_login_header(self):
        header_html = render_login_header()
        self.assertIn("CareerOS", header_html)
        self.assertIn("Kariyer alanına hoş geldin.", header_html)
        self.assertIn("os-mark", header_html)
        # No emoji
        self.assertNotIn("🧬", header_html)

    def test_render_brand_header(self):
        header_html = render_brand_header()
        self.assertIn("CareerOS", header_html)
        self.assertIn("Kariyer çalışma masası", header_html)
        self.assertIn("os-mark", header_html)
        self.assertNotIn("🧬", header_html)

    def test_password_eye_not_hidden_in_css(self):
        css = get_theme_css()
        # Should NOT hide password buttons
        self.assertNotIn('button[aria-label*="password" i] {\n    display: none', css)
        self.assertIn("visibility: visible", css)


if __name__ == "__main__":
    unittest.main()
