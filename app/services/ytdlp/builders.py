from typing import List, Optional
from app.constants import GIF_FORMAT_ID, AUDIO_SELECTOR


def _get_base_cmd(format_arg: str, output: str) -> List[str]:
    """Возвращает базовую команду yt-dlp с общими флагами"""
    return [
        "yt-dlp",
        "--format",
        format_arg,
        "--output",
        output,
        "--quiet",
        "--no-warnings",
        "--no-playlist",
        "--force-ipv4",
        "--merge-output-format",
        "mp4",
    ]


def _append_common_opts(
    cmd: List[str],
    page_url: str,
    cookies_path: Optional[str] = None,
    max_filesize: Optional[int] = None,
) -> None:
    """Добавляет cookies, лимит размера и URL в конец команды"""
    if cookies_path:
        cmd.extend(["--cookies", cookies_path])
    if max_filesize:
        cmd.extend(["--max-filesize", f"{max_filesize}M"])
    cmd.append(page_url)


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
        prog_sel = "best"
    elif is_gif_format:
        # Для GIF используем bestvideo без аудио
        cmd = _get_base_cmd(GIF_FORMAT_ID, output)

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
        _append_common_opts(cmd, page_url, cookies_path, max_filesize)
        return cmd
    else:
        # Аудио/Raw
        cmd = _get_base_cmd(format_id, output)
        _append_common_opts(cmd, page_url, cookies_path, max_filesize)
        return cmd

    # 2. Селектор аудио (from centralized constant)
    audio_sel = AUDIO_SELECTOR

    # 3. Финальный селектор с каскадным fallback
    final_fmt = f"{video_sel}+({audio_sel})/{prog_sel}/bestvideo+bestaudio/best"

    cmd = _get_base_cmd(final_fmt, output)
    cmd.extend(
        [
            "--geo-bypass",
            "--ignore-config",
            "--no-mtime",
            "--concurrent-fragments",
            "5",
        ]
    )

    # Если стримим в pipe ("-"), то aria2c использовать нельзя, и прогресс тоже мешает
    if output != "-":
        # Для прогресс-бара нам нужен вывод в stdout/stderr
        cmd.extend(["--progress", "--newline"])

    cmd.extend(
        [
            "--postprocessor-args",
            "Merger+ffmpeg:-movflags frag_keyframe+empty_moov",
        ]
    )

    if output != "-" and use_aria2 and has_aria2_installed:
        # Ускорение для скачивания на диск
        cmd.extend(
            [
                "--external-downloader",
                "aria2c",
                "--external-downloader-args",
                "-x 16 -s 16 -k 1M",
            ]
        )

    _append_common_opts(cmd, page_url, cookies_path, max_filesize)
    return cmd
