import os
import glob

# 1. Update AsyncMockCache to include delete() globally
for fpath in glob.glob('tests/*.py'):
    with open(fpath, 'r', encoding='utf-8') as f:
        content = f.read()
    if 'class AsyncMockCache(dict):' in content:
        if 'async def delete(' not in content:
            content = content.replace(
                '    async def set(self, key, value):\n        self[key] = value\n',
                '    async def set(self, key, value):\n        self[key] = value\n    async def delete(self, key):\n        self.pop(key, None)\n'
            )
            with open(fpath, 'w', encoding='utf-8') as f:
                f.write(content)

# 2. Fix test_messages_errors.py mock targets
f_errors = 'tests/test_messages_errors.py'
if os.path.exists(f_errors):
    with open(f_errors, 'r', encoding='utf-8') as f:
        e_content = f.read()

    replacements = [
        (
            '        with patch("app.bot.messages.asyncio.get_event_loop") as mock_loop:\n            mock_loop.return_value.run_in_executor = AsyncMock(\n                side_effect=AccessDeniedError("Login required")\n            )\n            await on_message(self.update, self.context)',
            '        state.ytdlp.list_formats = AsyncMock(side_effect=AccessDeniedError("Login required"))\n        await on_message(self.update, self.context)'
        ),
        (
            '        with patch("app.bot.messages.asyncio.get_event_loop") as mock_loop:\n            mock_loop.return_value.run_in_executor = AsyncMock(\n                side_effect=VideoNotFoundError("404")\n            )\n            await on_message(self.update, self.context)',
            '        state.ytdlp.list_formats = AsyncMock(side_effect=VideoNotFoundError("404"))\n        await on_message(self.update, self.context)'
        ),
        (
            '        with patch("app.bot.messages.asyncio.get_event_loop") as mock_loop:\n            mock_loop.return_value.run_in_executor = AsyncMock(\n                side_effect=LiveStreamError("Live stream")\n            )\n            await on_message(self.update, self.context)',
            '        state.ytdlp.list_formats = AsyncMock(side_effect=LiveStreamError("Live stream"))\n        await on_message(self.update, self.context)'
        ),
        (
            '        with patch("app.bot.messages.asyncio.get_event_loop") as mock_loop:\n            mock_loop.return_value.run_in_executor = AsyncMock(\n                side_effect=ExtractionError("pinterest: failed to extract")\n            )\n            await on_message(self.update, self.context)',
            '        state.ytdlp.list_formats = AsyncMock(side_effect=ExtractionError("pinterest: failed to extract"))\n        await on_message(self.update, self.context)'
        ),
        (
            '        with patch("app.bot.messages.asyncio.get_event_loop") as mock_loop:\n            mock_loop.return_value.run_in_executor = AsyncMock(\n                side_effect=ExtractionError("some random error")\n            )\n            await on_message(self.update, self.context)',
            '        state.ytdlp.list_formats = AsyncMock(side_effect=ExtractionError("some random error"))\n        await on_message(self.update, self.context)'
        ),
        (
            '        with patch("app.bot.messages.asyncio.get_event_loop") as mock_loop:\n            mock_loop.return_value.run_in_executor = AsyncMock(\n                side_effect=RuntimeError("unexpected crash")\n            )\n            await on_message(self.update, self.context)',
            '        state.ytdlp.list_formats = AsyncMock(side_effect=RuntimeError("unexpected crash"))\n        await on_message(self.update, self.context)'
        )
    ]
    for old, new in replacements:
        e_content = e_content.replace(old, new)

    with open(f_errors, 'w', encoding='utf-8') as f:
        f.write(e_content)
