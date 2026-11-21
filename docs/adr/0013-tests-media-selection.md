# Preserve requested media through fallback and clipping

Check every yt-dlp video selector branch for the requested short-edge limit and audio requirement. A fallback can otherwise silently return the wrong quality or a video without audio.

Feed legacy provider fakes a whole video when testing clip requests. Those native APIs have no section argument, so the delivered bytes must come from the route that honors the requested interval. Assert the FFmpeg cap and final output duration independently because the source can end before the requested cap.
