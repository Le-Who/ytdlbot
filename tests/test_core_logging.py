import unittest
import logging
import json
import time
from app.core.logging import JsonFormatter, set_correlation_id, correlation_id_var

class TestJsonFormatter(unittest.TestCase):
    def setUp(self):
        self.formatter = JsonFormatter()
        # Reset correlation_id before each test
        correlation_id_var.set("-")

    def test_basic_formatting(self):
        record = logging.LogRecord(
            name="test_logger",
            level=logging.INFO,
            pathname=__file__,
            lineno=10,
            msg="Test message",
            args=(),
            exc_info=None
        )
        # Fix the created time so we can't really test the exact string without mocking time,
        # but we can check format validity by loading json.
        formatted = self.formatter.format(record)
        log_dict = json.loads(formatted)

        self.assertEqual(log_dict["logger"], "test_logger")
        self.assertEqual(log_dict["level"], "INFO")
        self.assertEqual(log_dict["msg"], "Test message")
        self.assertIn("ts", log_dict)
        self.assertEqual(log_dict["correlation_id"], "-")

    def test_correlation_id(self):
        set_correlation_id("test-id-123")
        record = logging.LogRecord(
            name="test_logger",
            level=logging.INFO,
            pathname=__file__,
            lineno=10,
            msg="Test message",
            args=(),
            exc_info=None
        )
        formatted = self.formatter.format(record)
        log_dict = json.loads(formatted)

        self.assertEqual(log_dict["correlation_id"], "test-id-123")

    def test_optional_fields(self):
        record = logging.LogRecord(
            name="test_logger",
            level=logging.INFO,
            pathname=__file__,
            lineno=10,
            msg="Test message",
            args=(),
            exc_info=None
        )
        record.op = "test_op"
        record.duration_ms = 100
        record.error_type = "ValueError"
        record.token = "abc"
        record.chat_id = 12345
        record.user_id = 67890
        record.url_host = "example.com"

        formatted = self.formatter.format(record)
        log_dict = json.loads(formatted)

        self.assertEqual(log_dict["op"], "test_op")
        self.assertEqual(log_dict["duration_ms"], 100)
        self.assertEqual(log_dict["error_type"], "ValueError")
        self.assertEqual(log_dict["token"], "abc")
        self.assertEqual(log_dict["chat_id"], 12345)
        self.assertEqual(log_dict["user_id"], 67890)
        self.assertEqual(log_dict["url_host"], "example.com")

if __name__ == "__main__":
    unittest.main()
