from typing import List, Optional
from app.constants import GIF_FORMAT_ID


def _get_base_cmd(format_arg: str, output: str) -> List[str]:
    """Возвращает базовую команду yt-dlp с общими флагами"""
    return [
        "yt-dlp",
        "--format",
        format_arg,
        "--output",
        output,
        "--merge-output-format",
        "mp4",
        "--quiet",
        "--no-warnings",
        "--no-playlist",
        "--force-ipv4",
        "--retries",
        "3",
        "--fragment-retries",
        "5",
        "--retry-sleep",
        "linear=1::2",
    ]


def _append_common_opts(
    cmd: List[str],
    page_url: str,
    cookies_path: Optional[str] = None,
    max_filesize: Optional[int] = None,
    proxy: Optional[str] = None,
) -> None:
    """Добавляет cookies, прокси, лимит размера и URL в конец команды"""
    if cookies_path:
        cmd.extend(["--cookies", cookies_path])
    if proxy:
        cmd.extend(["--proxy", proxy])
    if max_filesize:
        cmd.extend(["--max-filesize", f"{max_filesize}M"])
    cmd.append("--")
    cmd.append(page_url)


def build_command(
    page_url: str,
    format_id: str,
    height: Optional[int],
    output: str,
    cookies_path: Optional[str] = None,
    max_filesize: Optional[int] = None,
    proxy: Optional[str] = None,
) -> List[str]:
    """Строит команду yt-dlp"""

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
        prog_sel = "best"
    elif is_gif_format:
        # Для GIF используем bestvideo без аудио
        cmd = _get_base_cmd("bestvideo[ext=mp4]/bestvideo/best[ext=mp4]/best", output)

        # Для прогресс-бара
        if output != "-":
            cmd.extend(["--progress", "--newline"])
        _append_common_opts(cmd, page_url, cookies_path, max_filesize, proxy)
        return cmd
    else:
        # Аудио/Raw или составной формат (bestvideo+bestaudio)
        cmd = _get_base_cmd(format_id, output)
        if output != "-":
            cmd.extend(["--progress", "--newline"])
        _append_common_opts(cmd, page_url, cookies_path, max_filesize, proxy)
        return cmd

    # 2. Селектор аудио (Original -> English -> OrigTag -> Any)
    audio_sel = "bestaudio[format_note*=original]/bestaudio[language^=en]/bestaudio[language^=orig]/bestaudio/bestaudio[ext=m4a]/bestaudio"

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

    # Если стримим в pipe ("-"), то прогресс мешает
    if output != "-":
        cmd.extend(["--progress", "--newline"])

    cmd.extend(
        [
            "--postprocessor-args",
            "Merger+ffmpeg:-movflags frag_keyframe+empty_moov",
        ]
    )

    _append_common_opts(cmd, page_url, cookies_path, max_filesize, proxy)
    return cmd
