from typing import List, Optional
from app.constants import GIF_FORMAT_ID

def build_command(
    page_url: str,
    format_id: str,
    height: Optional[int],
    output: str,
    cookies_path: Optional[str] = None,
    max_filesize: Optional[int] = None,
    use_aria2: bool = False,
    has_aria2_installed: bool = False,
) -> List[str]:
    """Строит команду yt-dlp с поддержкой aria2c"""

    # Проверяем, является ли это GIF форматом для Pinterest
    is_gif_format = format_id == GIF_FORMAT_ID

    # 1. Селектор видео
    if height:
        video_sel = f"bestvideo[height={height}]"
        prog_sel = f"best[height={height}]"
    elif (
        "+" not in format_id
        and format_id not in ("bestaudio/best", "best")
        and not is_gif_format
    ):
        video_sel = format_id
        prog_sel = f"best"
    elif is_gif_format:
        # Для GIF используем bestvideo без аудио
        cmd = [
            "yt-dlp",
            "--format",
            "bestvideo[ext=mp4]/bestvideo/best[ext=mp4]/best",
            "--output",
            output,
            "--quiet",
            "--no-warnings",
            "--no-playlist",
            "--force-ipv4",
        ]
        if cookies_path:
            cmd.extend(["--cookies", cookies_path])
        if max_filesize:
            cmd.extend(["--max-filesize", f"{max_filesize}M"])
        # Для прогресс-бара
        if output != "-":
            cmd.extend(["--progress", "--newline"])
            if use_aria2 and has_aria2_installed:
                cmd.extend(
                    [
                        "--external-downloader",
                        "aria2c",
                        "--external-downloader-args",
                        "-x 8 -k 1M",
                    ]
                )
        cmd.append(page_url)
        return cmd
    else:
        # Аудио/Raw
        cmd = [
            "yt-dlp",
            "--format",
            format_id,
            "--output",
            output,
            "--quiet",
            "--no-warnings",
            "--no-playlist",
            "--force-ipv4",
        ]
        if cookies_path:
            cmd.extend(["--cookies", cookies_path])
        if max_filesize:
            cmd.extend(["--max-filesize", f"{max_filesize}M"])
        cmd.append(page_url)
        return cmd

    # 2. Селектор аудио (Original -> English -> OrigTag -> Any)
    audio_sel = "bestaudio[format_note*=original]/bestaudio[language^=en]/bestaudio[language^=orig]/bestaudio/bestaudio[ext=m4a]/bestaudio"
    
    # 3. Финальный селектор с каскадным fallback
    final_fmt = f"{video_sel}+({audio_sel})/{prog_sel}/bestvideo+bestaudio/best"

    cmd = [
        "yt-dlp",
        "--format",
        final_fmt,
        "--output",
        output,
        "--quiet",
        "--no-warnings",
        "--no-playlist",
        "--force-ipv4",
        "--geo-bypass",
        "--ignore-config",
        "--no-mtime",
        "--concurrent-fragments", "5",
        # Для прогресс-бара нам нужен вывод в stdout/stderr
        "--progress",
        "--newline",
        "--postprocessor-args",
        "Merger+ffmpeg:-movflags frag_keyframe+empty_moov",
    ]

    # Если стримим в pipe ("-"), то aria2c использовать нельзя, и прогресс тоже мешает
    if output == "-":
        # Убираем --progress для чистого стрима
        cmd = [c for c in cmd if c not in ["--progress", "--newline"]]
    elif use_aria2 and has_aria2_installed:
        # Ускорение для скачивания на диск
        cmd.extend(
            [
                "--external-downloader",
                "aria2c",
                "--external-downloader-args",
                "-x 16 -s 16 -k 1M",
            ]
        )

    if cookies_path:
        cmd.extend(["--cookies", cookies_path])
    if max_filesize:
        cmd.extend(["--max-filesize", f"{max_filesize}M"])

    cmd.append(page_url)
    return cmd
