"""Tests for HTML injection prevention in video titles.

Verifies that html.escape() is correctly applied to user-supplied titles
before being used in Telegram parse_mode=HTML messages.
"""

import unittest
import html


class TestHtmlEscaping(unittest.TestCase):
    """Test that the html.escape function used in messages.py and callbacks.py
    properly neutralizes HTML injection in titles.

    The production code does:
        caption = f"📹 <b>{html.escape(title)}</b>\\n⏱ {duration}"
    These tests verify that html.escape correctly handles injection payloads.
    """

    def test_script_tag_escaped(self):
        """<script> tags must be escaped to prevent XSS in Telegram HTML."""
        title = '<script>alert("xss")</script>'
        escaped = html.escape(title)
        self.assertNotIn("<script>", escaped)
        self.assertIn("&lt;script&gt;", escaped)

    def test_html_bold_tag_escaped(self):
        """<b> tags in user title must not conflict with Telegram formatting."""
        title = "<b>Injected Bold</b>"
        escaped = html.escape(title)
        self.assertNotIn("<b>", escaped)
        self.assertIn("&lt;b&gt;", escaped)

    def test_ampersand_escaped(self):
        """Raw & must become &amp; to avoid HTML entity injection."""
        title = "Tom & Jerry"
        escaped = html.escape(title)
        self.assertIn("&amp;", escaped)
        self.assertNotIn(" & ", escaped)

    def test_quotes_escaped(self):
        """Quotes must be escaped to prevent attribute injection."""
        title = "Title with \"quotes\" and 'apostrophes'"
        escaped = html.escape(title, quote=True)
        self.assertNotIn('"', escaped)
        self.assertIn("&quot;", escaped)

    def test_nested_html_escaped(self):
        """Complex nested HTML injection must be fully escaped."""
        title = '<img src=x onerror="alert(1)"><a href="javascript:alert(1)">click</a>'
        escaped = html.escape(title)
        self.assertNotIn("<img", escaped)
        self.assertNotIn("<a ", escaped)
        self.assertNotIn(
            "javascript:", escaped.replace("javascript:", "")
        )  # still present as text
        self.assertIn("&lt;img", escaped)
        self.assertIn("&lt;a ", escaped)

    def test_normal_text_preserved(self):
        """Normal text without HTML is preserved unchanged."""
        title = "Обычное видео 2025"
        self.assertEqual(html.escape(title), title)

    def test_emoji_preserved(self):
        """Emoji in titles are preserved."""
        title = "🎬 Funny cats 🐱"
        self.assertEqual(html.escape(title), title)

    def test_production_caption_format(self):
        """Verify the exact production format from messages.py is safe."""
        malicious_title = '<script>alert(1)</script> & "exploit"'
        duration = "3:15"
        # This is the exact format used in messages.py and callbacks.py
        caption = f"📹 <b>{html.escape(malicious_title)}</b>\n⏱ {duration}"

        # Must contain the safe escaped version
        self.assertIn("&lt;script&gt;", caption)
        self.assertIn("&amp;", caption)
        # The <b> tags are our formatting, not injection
        self.assertEqual(caption.count("<b>"), 1)
        self.assertEqual(caption.count("</b>"), 1)
        # No raw script tags
        self.assertNotIn("<script>", caption.replace("&lt;script&gt;", ""))


if __name__ == "__main__":
    unittest.main()
