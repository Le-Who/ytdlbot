
import asyncio
import os
import sys
from unittest.mock import MagicMock

# Add repo root to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.services.ytdlp.service import YtDlpService

def test_base_opts_listing():
    service = YtDlpService()
    opts = service._base_opts(for_list_formats=True)
    
    # Check that skip is removed
    extractor_args = opts.get("extractor_args", {})
    youtube_args = extractor_args.get("youtube", {})
    assert "skip" not in youtube_args, "Skip should be removed from listing opts"
    
    # Check that format is not set to worst
    assert "format" not in opts, "Format should not be set for listing opts"
    print("✅ _base_opts for listing looks correct!")

def test_base_opts_download():
    service = YtDlpService()
    opts = service._base_opts(for_list_formats=False)
    assert opts.get("format"), "Format SHOULD be set for download"
    print("✅ _base_opts for download looks correct!")

if __name__ == "__main__":
    test_base_opts_listing()
    test_base_opts_download()
