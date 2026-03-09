import unittest
from app.services.ytdlp.parsers import _calculate_filesize, BITRATE_COEFFICIENT


class TestCalculateFilesize(unittest.TestCase):
    def test_filesize_present_int(self):
        """Should return filesize when present as int"""
        format_dict = {"filesize": 1024}
        self.assertEqual(_calculate_filesize(format_dict, None), 1024)

    def test_filesize_present_float(self):
        """Should return filesize as int when present as float"""
        format_dict = {"filesize": 1024.5}
        self.assertEqual(_calculate_filesize(format_dict, None), 1024)

    def test_filesize_approx_present(self):
        """Should fallback to filesize_approx if filesize missing"""
        format_dict = {"filesize_approx": 2048}
        self.assertEqual(_calculate_filesize(format_dict, None), 2048)

    def test_filesize_approx_present_float(self):
        """Should fallback to filesize_approx as int if float"""
        format_dict = {"filesize_approx": 2048.7}
        self.assertEqual(_calculate_filesize(format_dict, None), 2048)

    def test_filesize_priority(self):
        """Should prefer filesize over filesize_approx"""
        format_dict = {"filesize": 1000, "filesize_approx": 2000}
        self.assertEqual(_calculate_filesize(format_dict, None), 1000)

    def test_calculate_from_tbr(self):
        """Should calculate from tbr and duration factor if sizes missing"""
        tbr = 1000  # 1000 kbps
        duration = 10.0
        duration_factor = duration * BITRATE_COEFFICIENT
        # Expected: tbr * duration_factor
        expected = int(1000 * duration_factor)
        format_dict = {"tbr": tbr}
        self.assertEqual(_calculate_filesize(format_dict, duration_factor), expected)

    def test_calculate_from_tbr_float(self):
        """Should handle float tbr correctly"""
        tbr = 500.5
        duration = 2.0
        duration_factor = duration * BITRATE_COEFFICIENT
        expected = int(500.5 * duration_factor)
        format_dict = {"tbr": tbr}
        self.assertEqual(_calculate_filesize(format_dict, duration_factor), expected)

    def test_tbr_missing_duration(self):
        """Should return None if duration factor is missing/None for tbr calc"""
        format_dict = {"tbr": 1000}
        self.assertIsNone(_calculate_filesize(format_dict, None))

    def test_tbr_zero_duration(self):
        """Should return None if duration factor is 0"""
        format_dict = {"tbr": 1000}
        self.assertIsNone(_calculate_filesize(format_dict, 0))

    def test_all_missing(self):
        """Should return None if all fields missing"""
        format_dict = {"other": "value"}
        self.assertIsNone(_calculate_filesize(format_dict, 10.0 * BITRATE_COEFFICIENT))

    def test_filesize_zero(self):
        """Should skip filesize=0 and try approx"""
        format_dict = {"filesize": 0, "filesize_approx": 500}
        self.assertEqual(_calculate_filesize(format_dict, None), 500)

    def test_filesize_zero_approx_zero_tbr(self):
        """Should fallback to tbr if both sizes are 0"""
        format_dict = {"filesize": 0, "filesize_approx": 0, "tbr": 100}
        duration = 10
        duration_factor = duration * BITRATE_COEFFICIENT
        expected = int(100 * duration_factor)
        self.assertEqual(_calculate_filesize(format_dict, duration_factor), expected)


if __name__ == "__main__":
    unittest.main()
