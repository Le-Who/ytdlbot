"""Property-based tests using hypothesis for parsers and URL validation."""

import unittest
from hypothesis import given, strategies as st, settings

from app.services.ytdlp.parsers import (
    _is_tiktok,
    _is_youtube,
    _is_pinterest,
    _is_facebook,
    _format_duration,
    _calculate_filesize,
    classify_tiktok_error,
    TikTokError,
    deduplicate_formats,
)
from app.services.ytdlp.models import FormatMetadata
from app.core.utils import is_supported_url, extract_supported_url


# ── Duration formatting properties ───────────────────────────────


class TestDurationProperties(unittest.TestCase):
    @given(st.floats(min_value=0, max_value=100_000, allow_nan=False))
    @settings(max_examples=100)
    def test_format_duration_never_crashes(self, seconds):
        """_format_duration should never raise for any non-negative float."""
        result = _format_duration(seconds)
        self.assertIsInstance(result, str)

    @given(st.integers(min_value=1, max_value=100_000))
    @settings(max_examples=100)
    def test_format_duration_contains_colon(self, seconds):
        """Any positive duration contains at least one colon."""
        result = _format_duration(seconds)
        self.assertIn(":", result)

    @given(st.integers(min_value=3600, max_value=100_000))
    @settings(max_examples=50)
    def test_format_duration_hours_have_two_colons(self, seconds):
        """Durations >= 1h contain exactly two colons (HH:MM:SS)."""
        result = _format_duration(seconds)
        self.assertEqual(result.count(":"), 2)


# ── Format label properties ──────────────────────────────────────


class TestFormatLabelProperties(unittest.TestCase):
    @given(
        height=st.one_of(st.none(), st.integers(min_value=1, max_value=4320)),
        filesize=st.one_of(st.none(), st.integers(min_value=0, max_value=10**10)),
        protocol=st.sampled_from(["https", "http", "m3u8", "m3u8_native", "rtmp"]),
        is_tiktok=st.booleans(),
    )
    @settings(max_examples=200)
    def test_create_format_label_never_crashes(
        self, height, filesize, protocol, is_tiktok
    ):
        from app.bot.format_formatter import format_label
        from app.services.ytdlp.models import FormatItem

        """_create_format_label should never raise for any input."""
        item = FormatItem(
            "id", "mp4", height, filesize, is_tiktok=is_tiktok, protocol=protocol
        )
        result = format_label(item)
        self.assertIsInstance(result, str)
        self.assertTrue(len(result) > 0)

    @given(
        filesize=st.integers(min_value=0, max_value=10**10),
        protocol=st.sampled_from(["https", "http"]),
    )
    @settings(max_examples=50)
    def test_tiktok_label_always_starts_with_tiktok(self, filesize, protocol):
        from app.bot.format_formatter import format_label
        from app.services.ytdlp.models import FormatItem

        """TikTok labels always contain 'TikTok'."""
        item = FormatItem("id", "mp4", 720, filesize, is_tiktok=True, protocol=protocol)
        result = format_label(item)
        self.assertIn("TikTok", result)

    @given(height=st.integers(min_value=1080, max_value=8000))
    @settings(max_examples=50)
    def test_high_res_gets_tv_icon(self, height):
        from app.bot.format_formatter import format_label
        from app.services.ytdlp.models import FormatItem

        """Height >= 1080 gets 📺 icon."""
        item = FormatItem("id", "mp4", height, None, is_tiktok=False, protocol="https")
        result = format_label(item)
        self.assertIn("📺", result)


# ── Filesize calculation properties ──────────────────────────────


class TestFilesizeProperties(unittest.TestCase):
    @given(filesize=st.integers(min_value=1, max_value=10**10))
    @settings(max_examples=50)
    def test_explicit_filesize_returned_directly(self, filesize):
        """Explicit filesize takes priority over everything else."""
        result = _calculate_filesize({"filesize": filesize, "tbr": 999}, 999.0)
        self.assertEqual(result, filesize)

    @given(
        tbr=st.floats(
            min_value=1, max_value=100_000, allow_nan=False, allow_infinity=False
        ),
        factor=st.floats(
            min_value=0.1, max_value=10_000, allow_nan=False, allow_infinity=False
        ),
    )
    @settings(max_examples=50)
    def test_tbr_calculation_is_positive(self, tbr, factor):
        """TBR-based calculation always returns a positive int."""
        result = _calculate_filesize({"tbr": tbr}, factor)
        self.assertIsNotNone(result)
        self.assertGreaterEqual(result, 0)


# ── TikTok error classification properties ───────────────────────


class TestTikTokErrorProperties(unittest.TestCase):
    @given(msg=st.text(min_size=0, max_size=500))
    @settings(max_examples=200)
    def test_classify_tiktok_error_always_returns_enum(self, msg):
        """classify_tiktok_error always returns a TikTokError member."""
        result = classify_tiktok_error(msg)
        self.assertIsInstance(result, TikTokError)


# ── URL detection properties ─────────────────────────────────────


class TestURLDetectionProperties(unittest.TestCase):
    @given(url=st.text(min_size=0, max_size=300))
    @settings(max_examples=200)
    def test_platform_detectors_never_crash(self, url):
        """Platform detectors never raise for any string."""
        _is_tiktok(url)
        _is_youtube(url)
        _is_pinterest(url)
        _is_facebook(url)

    @given(url=st.text(min_size=0, max_size=300))
    @settings(max_examples=200)
    def test_is_supported_url_returns_bool(self, url):
        """is_supported_url always returns a bool."""
        result = is_supported_url(url)
        self.assertIsInstance(result, bool)

    @given(text=st.text(min_size=0, max_size=500))
    @settings(max_examples=200)
    def test_extract_supported_url_returns_str_or_none(self, text):
        """extract_supported_url always returns str or None."""
        result = extract_supported_url(text)
        self.assertTrue(result is None or isinstance(result, str))


# ── Deduplication properties ─────────────────────────────────────


class TestDeduplicationProperties(unittest.TestCase):
    @given(
        heights=st.lists(
            st.integers(min_value=0, max_value=4320), min_size=0, max_size=20
        ),
    )
    @settings(max_examples=100)
    def test_dedup_never_adds_formats(self, heights):
        """Deduplication never returns more items than input."""
        formats = [
            FormatMetadata(f"f{i}", "mp4", h, i * 1000, "https")
            for i, h in enumerate(heights)
        ]
        result = deduplicate_formats(formats, is_tiktok_url=False)
        self.assertLessEqual(len(result), len(formats))

    @given(
        sizes=st.lists(
            st.integers(min_value=0, max_value=10**8), min_size=0, max_size=20
        ),
    )
    @settings(max_examples=100)
    def test_tiktok_dedup_never_adds_formats(self, sizes):
        """TikTok dedup by filesize never returns more items than input."""
        formats = [
            FormatMetadata(f"f{i}", "mp4", 720, s, "https") for i, s in enumerate(sizes)
        ]
        result = deduplicate_formats(formats, is_tiktok_url=True)
        self.assertLessEqual(len(result), len(formats))


if __name__ == "__main__":
    unittest.main()
