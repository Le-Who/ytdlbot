import base64
import json
import os
import re
import subprocess
import tempfile
import atexit
import logging
import shutil
import threading
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
import yt_dlp

from .constants import (
    AUDIO_FORMAT_ID,
    GIF_FORMAT_ID,
    VIDEO_EXTENSIONS,
    HEIGHT_PATTERN,
)

HEIGHT_REGEX = re.compile(HEIGHT_PATTERN)

logger = logging.getLogger("ytdlp_service")


@dataclass
class FormatItem:
    """Представление формата видео"""

    format_id: str
    label: str
    ext: str
    height: Optional[int]
    filesize: Optional[int]


class CookiesManager:
    """Управление временным файлом cookies"""

    def __init__(self):
        self.cookies_path: Optional[str] = None
        self._initialize_cookies()

    def _initialize_cookies(self) -> None:
        """Создаёт временный файл cookies из переменной окружения"""
        b64 = os.getenv("YTDLP_COOKIES_B64", "").strip()
        if not b64:
            return

        try:
            data = base64.b64decode(b64.encode("utf-8"))
            fd, self.cookies_path = tempfile.mkstemp(prefix="cookies_", suffix=".txt")

            with os.fdopen(fd, "wb") as f:
                f.write(data)

            logger.info(f"Cookies initialized at {self.cookies_path}")
            atexit.register(self.cleanup)
        except Exception as e:
            logger.error(f"Failed to initialize cookies: {e}")
            self.cookies_path = None

    def cleanup(self) -> None:
        """Удаляет временный файл cookies"""
        if self.cookies_path and os.path.exists(self.cookies_path):
            try:
                os.unlink(self.cookies_path)
                logger.info("Cookies file cleaned up")
            except Exception as e:
                logger.warning(f"Failed to cleanup cookies: {e}")


class YtDlpService:
    """Сервис для работы с yt-dlp"""

    SOCKET_TIMEOUT = 30
    MAX_RETRIES = 5
    USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0"
    BYTES_IN_KB = 1024
    BITS_IN_BYTE = 8

    def __init__(self):
        self.cookies_manager = CookiesManager()
        # Проверяем наличие aria2c в системе
        self.has_aria2 = shutil.which("aria2c") is not None
        if self.has_aria2:
            logger.info("🚀 Aria2c detected! Download acceleration enabled.")
        else:
            logger.info("⚠️ Aria2c not found. Standard download mode.")

        # Thread-local storage for reuse of YoutubeDL instances
        self._thread_local = threading.local()

    @property
    def cookies_path(self) -> Optional[str]:
        return self.cookies_manager.cookies_path

    @staticmethod
    def _is_youtube(url: str) -> bool:
        return "youtube.com" in url.lower() or "youtu.be" in url.lower()

    def _base_opts(self, for_list_formats: bool = False) -> Dict[str, Any]:
        """Базовые опции для yt-dlp"""
        opts: Dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "socket_timeout": self.SOCKET_TIMEOUT,
            "retries": self.MAX_RETRIES,
            "force_ipv4": True,
            "legacyserverconnect": True,
            "user_agent": self.USER_AGENT,
            "nocheckcertificate": True,
            "prefer_free_formats": True,
            "extractor_args": {
                "youtube": {
                    "player_client": ["android", "web", "mweb", "ios"],
                }
            },
            # --- Professional Refinements ---
            "geo_bypass": True,
            "ignoreconfig": True,
            "noprogress": True,
            "no_mtime": True,
            "concurrent_fragment_downloads": 5,
            "hls_use_mpegts": True,
        }
        
        if not for_list_formats:
            opts["format_sort"] = ["res:1080", "vcodec:vp9", "br", "size"]
            opts["format"] = "bestvideo+bestaudio/bestvideo+bestaudio/best/bestvideo/best"
        else:
            # Для list_formats убираем принудительную сортировку, чтобы избежать "Requested format is not available"
            opts.pop("format_sort", None)

        if self.cookies_path:
            opts["cookiefile"] = self.cookies_path

        return opts

    def _extract_youtube_via_subprocess(self, url: str) -> Optional[Dict[str, Any]]:
        """Извлечение через yt-dlp CLI для YouTube — надёжный обход ошибок API."""
        base_cmd = [
            "yt-dlp",
            "--dump-json",
            "--no-download",
            "--no-warnings",
            "--no-playlist",
            "--force-ipv4",
            "--geo-bypass",
            "--ignore-config",
            "--no-check-certificate",
        ]
        if self.cookies_path:
            base_cmd.extend(["--cookies", self.cookies_path])

        # Стратегия: 1. Спец. клиенты (android/web) 2. Без аргументов (стандартное поведение)
        arg_variants = [
            ["--extractor-args", "youtube:player_client=android,web,mweb,ios"],
            [],
        ]

        for args in arg_variants:
            cmd = base_cmd + args + [url]
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
                if result.returncode == 0 and result.stdout.strip():
                    return json.loads(result.stdout)
                else:
                    logger.debug(
                        f"Subprocess attempt failed (args={args}), retcode={result.returncode}"
                    )
            except (
                subprocess.TimeoutExpired,
                json.JSONDecodeError,
                FileNotFoundError,
            ) as e:
                logger.debug(
                    f"YouTube subprocess fallback exception (args={args}): {e}"
                )

        return None

    def extract(self, url: str, for_list_formats: bool = False) -> Dict[str, Any]:
        """Извлекает метаданные видео."""
        opts = self._base_opts(for_list_formats=for_list_formats)

        # Optimization: Reuse YoutubeDL instance for list_formats (hot path)
        # Avoids expensive re-initialization (~100ms) per request.
        if for_list_formats:
            if not hasattr(self._thread_local, "ydl_list_formats"):
                self._thread_local.ydl_list_formats = yt_dlp.YoutubeDL(opts)
            ydl = self._thread_local.ydl_list_formats
            # Note: extract_info is generally stateless regarding the instance configuration
            return ydl.extract_info(url, download=False)

        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)

    @staticmethod
    def _is_tiktok(url: str) -> bool:
        return "tiktok.com" in url.lower()

    @staticmethod
    def _is_pinterest(url: str) -> bool:
        return "pinterest.com" in url.lower() or "pin.it" in url.lower()

    @staticmethod
    def _format_duration(seconds: Optional[float]) -> str:
        """Форматирует длительность в формат HH:MM:SS или MM:SS

        Принимает int или float (yt-dlp может возвращать float для некоторых платформ)
        """
        if not seconds:
            return "??"
        # Конвертируем в int для корректного форматирования
        total_seconds = int(seconds)
        m, s = divmod(total_seconds, 60)
        h, m = divmod(m, 60)
        return f"{h}:{m:02d}:{s:02d}" if h > 0 else f"{m:02d}:{s:02d}"

    @staticmethod
    def _extract_height(format_note: str) -> Optional[int]:
        match = HEIGHT_REGEX.search(format_note or "")
        if match:
            return int(match.group(1))
        return None

    def _calculate_filesize(
        self, format_dict: Dict[str, Any], duration_sec: Optional[float]
    ) -> Optional[int]:
        """Вычисляет размер файла, обрабатывая int и float для duration"""
        fs = format_dict.get("filesize")
        if fs:
            return int(fs) if isinstance(fs, float) else fs
        fs = format_dict.get("filesize_approx")
        if fs:
            return int(fs) if isinstance(fs, float) else fs
        tbr = format_dict.get("tbr")
        if tbr and duration_sec:
            # Конвертируем duration в число для расчета
            duration = float(duration_sec) if duration_sec else 0
            return int((float(tbr) * self.BYTES_IN_KB / self.BITS_IN_BYTE) * duration)
        return None

    def _create_format_label(
        self,
        height: Optional[int],
        filesize: Optional[int],
        protocol: str,
        is_tiktok: bool,
    ) -> str:
        parts = []

        # 1. Icon & Type
        if is_tiktok:
            parts.append("🎵 TikTok")
        else:
            if height:
                if height >= 1080:
                    icon = "📺"
                elif height >= 720:
                    icon = "📹"
                else:
                    icon = "📱"
                parts.append(f"{icon} {height}p")
            else:
                parts.append("📹 ???p")

        # 2. Size / Protocol
        if filesize:
            mb = filesize / self.BYTES_IN_KB / self.BYTES_IN_KB
            if mb < 1:
                size_str = f"{int(filesize / self.BYTES_IN_KB)} KB"
            else:
                size_str = f"{mb:.1f} MB"
            parts.append(f"• {size_str}")
        elif "m3u8" in protocol:
            parts.append("• HLS")
        else:
            parts.append("• ?")

        return " ".join(parts)

    def _parse_format(
        self,
        format_dict: Dict[str, Any],
        duration_sec: Optional[float],
        is_tiktok: bool,
    ) -> Optional[FormatItem]:
        if format_dict.get("vcodec") == "none" and not is_tiktok:
            return None
        ext = format_dict.get("ext")
        protocol = format_dict.get("protocol") or ""
        if ext not in VIDEO_EXTENSIONS and "m3u8" not in protocol:
            return None

        fid = format_dict.get("format_id")
        if not fid:
            return None

        height = format_dict.get("height")
        if not height:
            note = format_dict.get("format_note", "")
            height = self._extract_height(note)
            if not height and is_tiktok:
                height = 720

        filesize = self._calculate_filesize(format_dict, duration_sec)
        label = self._create_format_label(height, filesize, protocol, is_tiktok)

        return FormatItem(
            format_id=fid, label=label, ext="mp4", height=height or 0, filesize=filesize
        )

    def _deduplicate_formats(
        self, formats: List[FormatItem], is_tiktok: bool
    ) -> List[FormatItem]:
        if is_tiktok:
            return formats
        unique_formats = []
        seen_heights = set()
        for fmt in formats:
            h = fmt.height
            if h and h not in seen_heights:
                unique_formats.append(fmt)
                seen_heights.add(h)
            elif not h:
                unique_formats.append(fmt)
        return unique_formats

    def list_formats(
        self, url: str, max_items: int = 12
    ) -> Tuple[str, List[FormatItem], FormatItem, str]:
        """Извлекает форматы видео с обработкой ошибок для разных платформ"""
        info: Optional[Dict[str, Any]] = None
        used_subprocess = False
        try:
            info = self.extract(url, for_list_formats=True)
        except Exception as e:
            if self._is_youtube(url):
                info = self._extract_youtube_via_subprocess(url)
                used_subprocess = True
            if not info:
                error_msg = str(e).lower()
                if "403" in error_msg or "forbidden" in error_msg:
                    raise Exception(
                        "Доступ запрещен. Возможно, контент приватный или требуется авторизация."
                    )
                elif "404" in error_msg or "not found" in error_msg:
                    raise Exception("Видео не найдено. Проверьте ссылку.")
                elif "live" in error_msg and "available" not in error_msg:
                    raise Exception("Прямые трансляции (Live) не поддерживаются. Дождитесь окончания стрима.")
                elif "none" in error_msg or "nonetype" in error_msg:
                    raise Exception(
                        "Ошибка парсинга данных. Попробуйте позже или используйте другую ссылку."
                    )
                else:
                    logger.error("YtDlp Extraction Error: %s", e, exc_info=True)
                    # Если subprocess тоже не дал инфо, выбрасываем оригинальную ошибку или уточнение
                    msg = str(e)
                    if "format is not available" in msg.lower():
                        msg = "Выбранный формат или видео недоступны. Попробуйте другую ссылку."
                    raise Exception(f"Ошибка извлечения: {msg[:300]}")

        # Проверка на Live стрим
        if info.get("is_live") or info.get("live_status") == "is_live":
             raise Exception("⚠️ Это прямая трансляция. Загрузка активных стримов не поддерживается.")

        title = info.get("title") or "Видео"
        duration_sec = info.get("duration")
        duration_str = self._format_duration(duration_sec)

        raw_formats = info.get("formats", [])
        if not raw_formats and self._is_youtube(url) and not used_subprocess:
            info2 = self._extract_youtube_via_subprocess(url)
            used_subprocess = True
            if info2:
                raw_formats = info2.get("formats", [])
        is_tiktok = self._is_tiktok(url)

        formats: List[FormatItem] = []
        for raw_fmt in raw_formats:
            fmt = self._parse_format(raw_fmt, duration_sec, is_tiktok)
            if fmt:
                formats.append(fmt)

        # Если после фильтрации нет форматов (например, вернулось только аудио), пробуем fallback
        if not formats and self._is_youtube(url) and not used_subprocess:
            logger.info(
                "No video formats parsed via API, trying subprocess fallback..."
            )
            info2 = self._extract_youtube_via_subprocess(url)
            if info2:
                raw_formats2 = info2.get("formats", [])
                for raw_fmt in raw_formats2:
                    fmt = self._parse_format(raw_fmt, duration_sec, is_tiktok)
                    if fmt:
                        formats.append(fmt)

        formats.sort(key=lambda x: (x.height or 0, x.filesize or 0), reverse=True)
        formats = self._deduplicate_formats(formats, is_tiktok)
        formats = formats[:max_items]

        is_pinterest = self._is_pinterest(url)
        if is_pinterest:
            # Для Pinterest заменяем "Только аудио" на "Только GIF"
            audio = FormatItem(
                format_id=GIF_FORMAT_ID,
                label="🎬 Только GIF",
                ext="gif",
                height=None,
                filesize=None,
            )
        else:
            audio = FormatItem(
                format_id=AUDIO_FORMAT_ID,
                label="🎵 Только аудио (best)",
                ext="audio",
                height=None,
                filesize=None,
            )
        return title, formats, audio, duration_str

    def build_command(
        self,
        page_url: str,
        format_id: str,
        height: Optional[int],
        output: str,
        max_filesize: Optional[int] = None,
        use_aria2: bool = False,
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
            # yt-dlp скачает видео без аудио, затем нужно будет конвертировать в GIF через ffmpeg
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
            if self.cookies_path:
                cmd.extend(["--cookies", self.cookies_path])
            if max_filesize:
                cmd.extend(["--max-filesize", f"{max_filesize}M"])
            # Для прогресс-бара
            if output != "-":
                cmd.extend(["--progress", "--newline"])
                if use_aria2 and self.has_aria2:
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
            if self.cookies_path:
                cmd.extend(["--cookies", self.cookies_path])
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
            "--no-check-certificate",
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
        elif use_aria2 and self.has_aria2:
            # Ускорение для скачивания на диск
            cmd.extend(
                [
                    "--external-downloader",
                    "aria2c",
                    "--external-downloader-args",
                    "-x 16 -s 16 -k 1M",
                ]
            )

        if self.cookies_path:
            cmd.extend(["--cookies", self.cookies_path])
        if max_filesize:
            cmd.extend(["--max-filesize", f"{max_filesize}M"])

        cmd.append(page_url)
        return cmd
