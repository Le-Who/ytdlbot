import os
import unittest
from datetime import UTC, datetime

from telegram import Chat, Message, MessageEntity

# Set env vars
os.environ["BOT_TOKEN"] = "test_token"

from app.core.utils import extract_supported_url, extract_url_from_update


class TestExtractSupportedUrl(unittest.TestCase):
    def test_extract_valid_url(self):
        text = "Check out https://youtube.com/watch?v=123"
        expected = "https://youtube.com/watch?v=123"
        self.assertEqual(extract_supported_url(text), expected)

    def test_extract_pure_url(self):
        text = "https://vm.tiktok.com/123"
        expected = "https://vm.tiktok.com/123"
        self.assertEqual(extract_supported_url(text), expected)

    def test_no_url(self):
        text = "Hello world"
        self.assertIsNone(extract_supported_url(text))

    def test_unsupported_url(self):
        text = "Check this https://google.com"
        self.assertIsNone(extract_supported_url(text))

    def test_url_cleanup(self):
        text = "Link: https://youtube.com/watch?v=123."
        expected = "https://youtube.com/watch?v=123"
        self.assertEqual(extract_supported_url(text), expected)


def test_message_skips_unsupported_urls_and_keeps_first_supported_url_and_clip():
    message = Message(
        message_id=1,
        date=datetime(2026, 1, 1, tzinfo=UTC),
        chat=Chat(1, "private"),
        text=(
            "https://example.com https://youtu.be/dQw4w9WgXcQ, "
            "https://youtube.com/watch?v=LSie-2eT6gw 00:10-00:20"
        ),
    )

    assert extract_url_from_update(message) == (
        "https://youtu.be/dQw4w9WgXcQ",
        "*00:10-00:20",
    )


def test_hidden_text_link_and_clip_take_priority_over_reply():
    chat = Chat(1, "private")
    date = datetime(2026, 1, 1, tzinfo=UTC)
    reply = Message(1, date, chat, text="https://youtu.be/LSie-2eT6gw 00:30-00:40")
    url = "https://youtu.be/dQw4w9WgXcQ"
    message = Message(
        2,
        date,
        chat,
        text="Bad Watch 00:10-00:20",
        entities=[
            MessageEntity("text_link", 0, 3, url="ftp://youtube.com/watch?v=abc"),
            MessageEntity("text_link", 4, 5, url=url),
        ],
        reply_to_message=reply,
    )

    assert extract_url_from_update(message) == (url, "*00:10-00:20")


def test_reply_command_resolves_hidden_caption_link_and_clip():
    chat = Chat(1, "private")
    date = datetime(2026, 1, 1, tzinfo=UTC)
    url = "https://www.instagram.com/s/aGlnaGxpZ2h0Ojc=/?story_media_id=41_123"
    reply = Message(
        1,
        date,
        chat,
        caption="Watch 00:10-00:20",
        caption_entities=[MessageEntity("text_link", 0, 5, url=url)],
    )
    message = Message(2, date, chat, text="/mp4", reply_to_message=reply)

    assert extract_url_from_update(message) == (url, "*00:10-00:20")


if __name__ == "__main__":
    unittest.main()
