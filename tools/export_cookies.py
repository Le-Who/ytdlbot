#!/usr/bin/env python3
"""
YouTube Cookie Exporter for ytdlbot
====================================
Extracts YouTube session cookies from your browser via yt-dlp,
encodes them as base64, and outputs a ready-to-paste .env line.

Usage:
    python export_cookies.py                    # Chrome default profile
    python export_cookies.py chrome             # Chrome — pick profile interactively
    python export_cookies.py chrome "Profile 3" # Chrome — specific profile
    python export_cookies.py firefox            # Firefox
    python export_cookies.py edge               # Edge

Requirements:
    - Python 3.8+
    - yt-dlp installed (pip install yt-dlp)
"""

import base64
import io
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile

# Force UTF-8 stdout on Windows, line-buffered to prevent garbled output
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace", line_buffering=True)
else:
    # Even if encoding is fine, ensure line buffering
    sys.stdout.reconfigure(line_buffering=True)  # type: ignore[attr-defined]

# ── Config ───────────────────────────────────────────────────────
SUPPORTED_BROWSERS = ("chrome", "firefox", "edge", "brave", "opera", "vivaldi")
ENV_VAR_NAME = "YTDLP_COOKIES_B64"
# Dummy URL — yt-dlp requires at least one URL arg even when only exporting cookies.
DUMMY_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

# Chrome/Edge/Brave User Data directories per OS
_CHROMIUM_USER_DATA = {
    "chrome": {
        "Windows": os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "User Data"),
        "Darwin": os.path.expanduser("~/Library/Application Support/Google/Chrome"),
        "Linux": os.path.expanduser("~/.config/google-chrome"),
    },
    "edge": {
        "Windows": os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "Edge", "User Data"),
        "Darwin": os.path.expanduser("~/Library/Application Support/Microsoft Edge"),
        "Linux": os.path.expanduser("~/.config/microsoft-edge"),
    },
    "brave": {
        "Windows": os.path.join(os.environ.get("LOCALAPPDATA", ""), "BraveSoftware", "Brave-Browser", "User Data"),
        "Darwin": os.path.expanduser("~/Library/Application Support/BraveSoftware/Brave-Browser"),
        "Linux": os.path.expanduser("~/.config/BraveSoftware/Brave-Browser"),
    },
}


def _print_banner() -> None:
    print()
    print("  +-------------------------------------------+")
    print("  |   YouTube Cookie Exporter  v1.0           |")
    print("  |       for ytdlbot (.env ready)            |")
    print("  +-------------------------------------------+")
    print()


def _check_ytdlp() -> str:
    """Return the path to yt-dlp binary, or exit with instructions."""
    path = shutil.which("yt-dlp")
    if path:
        return path

    # Maybe installed as a Python module
    try:
        subprocess.run(
            [sys.executable, "-m", "yt_dlp", "--version"],
            capture_output=True, check=True, timeout=10,
        )
        return f"{sys.executable} -m yt_dlp"
    except Exception:
        pass

    print("  [X]  yt-dlp not found!")
    print()
    print("  Install it:")
    print("      pip install yt-dlp")
    print()
    sys.exit(1)


def _discover_chromium_profiles(browser: str) -> list[dict]:
    """
    Scan the Chromium-based browser's User Data directory for profiles.
    Returns a list of dicts: [{"dir": "Default", "name": "Person 1", "email": "..."}]
    """
    paths = _CHROMIUM_USER_DATA.get(browser)
    if not paths:
        return []

    user_data_dir = paths.get(platform.system(), "")
    if not user_data_dir or not os.path.isdir(user_data_dir):
        return []

    profiles = []
    for entry in sorted(os.listdir(user_data_dir)):
        prefs_path = os.path.join(user_data_dir, entry, "Preferences")
        if not os.path.isfile(prefs_path):
            continue

        try:
            with open(prefs_path, "r", encoding="utf-8") as f:
                prefs = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue

        profile_info = prefs.get("profile", {})
        name = profile_info.get("name", entry)

        # Try to find the Google account email
        account_info = prefs.get("account_info", [])
        email = ""
        if account_info and isinstance(account_info, list):
            email = account_info[0].get("email", "")

        # Also check gaia_info_picture_url presence as a sign of logged-in profile
        gaia_name = profile_info.get("gaia_name", "")

        profiles.append({
            "dir": entry,
            "name": name,
            "email": email,
            "gaia_name": gaia_name,
        })

    return profiles


def _pick_profile_interactive(profiles: list[dict]) -> str | None:
    """
    Display a numbered list of profiles and let the user pick one.
    Returns the profile directory name (e.g. "Default", "Profile 3") or None.
    """
    lines = []
    lines.append("")
    lines.append("  Available Chrome profiles:")
    lines.append("")
    for i, p in enumerate(profiles, 1):
        label = p["name"]
        if p["email"]:
            email_part = f"  ({p['email']})"
        elif p["gaia_name"]:
            email_part = f"  ({p['gaia_name']})"
        else:
            email_part = ""
        lines.append(f"    {i:>3}.  {label}{email_part}   [{p['dir']}]")
    lines.append("")
    # Print all at once to prevent interleaving
    sys.stdout.write("\n".join(lines) + "\n")
    sys.stdout.flush()

    while True:
        try:
            choice = input("  Pick a profile number (or 'q' to quit): ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return None

        if choice.lower() == "q":
            return None

        try:
            idx = int(choice) - 1
            if 0 <= idx < len(profiles):
                selected = profiles[idx]
                print(f"  -> Selected: {selected['name']} [{selected['dir']}]")
                print()
                return selected["dir"]
        except ValueError:
            pass

        print(f"  Please enter a number from 1 to {len(profiles)}")


def _pick_browser_and_profile(args: list[str]) -> tuple[str, str | None]:
    """
    Parse CLI args to determine browser and optional profile.
    Returns (browser_name, profile_dir_or_None).

    Supports:
        export_cookies.py                       -> chrome, auto-pick
        export_cookies.py chrome                -> chrome, auto-pick
        export_cookies.py chrome "Profile 3"    -> chrome, "Profile 3"
        export_cookies.py firefox               -> firefox, None
    """
    browser = "chrome"
    explicit_profile = None

    if len(args) > 1:
        browser = args[1].lower().strip()
        if browser not in SUPPORTED_BROWSERS:
            print(f"  [X]  Unknown browser: {browser}")
            print(f"  Available: {', '.join(SUPPORTED_BROWSERS)}")
            sys.exit(1)

    if len(args) > 2:
        explicit_profile = args[2].strip()

    return browser, explicit_profile


def _resolve_profile(browser: str, explicit_profile: str | None) -> str | None:
    """
    For Chromium-based browsers, resolve which profile directory to use.
    Returns the profile dir name or None (meaning default / not applicable).
    """
    if browser not in _CHROMIUM_USER_DATA:
        return None  # Firefox/Opera don't use this mechanism

    profiles = _discover_chromium_profiles(browser)

    if not profiles:
        return None  # Could not discover profiles — let yt-dlp use default

    # If user specified a profile on command line, validate it
    if explicit_profile:
        dirs = [p["dir"] for p in profiles]
        names = [p["name"].lower() for p in profiles]
        # Match by directory name or display name
        if explicit_profile in dirs:
            return explicit_profile
        if explicit_profile.lower() in names:
            idx = names.index(explicit_profile.lower())
            return profiles[idx]["dir"]
        print(f"  [X]  Profile '{explicit_profile}' not found.")
        print(f"  Available: {', '.join(p['dir'] for p in profiles)}")
        sys.exit(1)

    # Only 1 profile — use it silently
    if len(profiles) == 1:
        return profiles[0]["dir"]

    # Multiple profiles — interactive selection
    return _pick_profile_interactive(profiles)


def _check_browser_running(browser: str) -> bool:
    """Check if the browser process is currently running."""
    process_names = {
        "chrome": "chrome",
        "edge": "msedge",
        "brave": "brave",
        "firefox": "firefox",
        "opera": "opera",
        "vivaldi": "vivaldi",
    }
    target = process_names.get(browser, browser)

    if platform.system() == "Windows":
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"IMAGENAME eq {target}.exe", "/NH"],
                capture_output=True, text=True, timeout=5,
            )
            return target.lower() in result.stdout.lower()
        except Exception:
            return False
    else:
        try:
            result = subprocess.run(
                ["pgrep", "-i", target],
                capture_output=True, timeout=5,
            )
            return result.returncode == 0
        except Exception:
            return False


def _extract_cookies(ytdlp_path: str, browser: str, profile: str | None) -> str | None:
    """
    Run yt-dlp to extract cookies from the browser and dump them
    into a temporary Netscape cookie file. Returns file content or None.
    """
    tmp_dir = tempfile.mkdtemp(prefix="ytdlbot_cookies_")
    cookie_file = os.path.join(tmp_dir, "cookies.txt")

    # Build the command
    if " " in ytdlp_path:
        cmd = ytdlp_path.split()
    else:
        cmd = [ytdlp_path]

    # yt-dlp syntax: --cookies-from-browser BROWSER[:PROFILE]
    browser_arg = browser
    if profile:
        browser_arg = f"{browser}:{profile}"

    cmd.extend([
        "--cookies-from-browser", browser_arg,
        "--cookies", cookie_file,
        "--skip-download",
        "--no-warnings",
        "--quiet",
        "--", DUMMY_URL,
    ])

    profile_label = f" (profile: {profile})" if profile else ""
    print(f"  [..]  Extracting cookies from {browser.title()}{profile_label}...")
    print(f"        (if the browser asks for permission, click 'Allow')")
    print()

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        print("  [X]  Timeout. Try closing the browser and running again.")
        return None
    except FileNotFoundError:
        print("  [X]  Could not launch yt-dlp.")
        return None

    if not os.path.isfile(cookie_file):
        stderr = result.stderr.strip() if result.stderr else ""
        print("  [X]  yt-dlp did not create a cookie file.")
        if stderr:
            last_line = [l for l in stderr.splitlines() if l.strip()][-1] if stderr.splitlines() else stderr
            print(f"       Reason: {last_line}")
        print()

        # Specific guidance for Chrome App-Bound Encryption (DPAPI) failure
        if "dpapi" in stderr.lower() or "app_bound" in stderr.lower() or "10927" in stderr:
            print("  [!]  CHROME APP-BOUND ENCRYPTION ERROR")
            print("       Chrome 127+ encrypts cookies with a system service that")
            print("       requires Administrator privileges to read from outside Chrome.")
            print()
            print("  FIX: Re-run this script as Administrator:")
            print("       1. Close this terminal")
            print("       2. Right-click on PowerShell / CMD icon")
            print("       3. Select 'Run as Administrator'")
            print("       4. Navigate back to the project folder and run:")
            print(f"          python tools/export_cookies.py {browser}")
            print()
            print("  OR use Firefox instead (no App-Bound Encryption):")
            print("       python tools/export_cookies.py firefox")
        else:
            print("  Tips:")
            print("    - Make sure you are logged in to YouTube in this browser")
            print("    - CLOSE the browser completely before running (check system tray!)")
            print("    - For Chrome you may need to run as Administrator")
        return None

    with open(cookie_file, "r", encoding="utf-8") as f:
        content = f.read()

    # Cleanup
    try:
        os.unlink(cookie_file)
        os.rmdir(tmp_dir)
    except OSError:
        pass

    return content


def _filter_youtube_cookies(raw_content: str) -> str:
    """
    Keep only YouTube/Google-related cookie lines.
    This reduces the base64 payload and avoids leaking unrelated sessions.
    """
    youtube_domains = (
        ".youtube.com",
        ".google.com",
        ".googlevideo.com",
        "youtube.com",
        "google.com",
        "accounts.google.com",
    )
    header = "# Netscape HTTP Cookie File\n"
    lines = []
    for line in raw_content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("//"):
            continue
        # Netscape format: domain \t flag \t path \t secure \t expiry \t name \t value
        parts = stripped.split("\t")
        if len(parts) >= 7:
            domain = parts[0].lstrip(".")
            if any(domain == d.lstrip(".") or domain.endswith("." + d.lstrip(".")) for d in youtube_domains):
                lines.append(stripped)

    if not lines:
        return raw_content

    return header + "\n".join(lines) + "\n"


def _encode_base64(content: str) -> str:
    return base64.b64encode(content.encode("utf-8")).decode("ascii")


def _copy_to_clipboard(text: str) -> bool:
    """Try to copy text to clipboard. Returns True on success."""
    if platform.system() != "Windows":
        return False
    try:
        process = subprocess.Popen(
            ["clip.exe"],
            stdin=subprocess.PIPE,
            shell=False,
        )
        process.communicate(input=text.encode("utf-8"), timeout=5)
        return process.returncode == 0
    except Exception:
        return False


def main() -> None:
    _print_banner()

    ytdlp_path = _check_ytdlp()
    browser, explicit_profile = _pick_browser_and_profile(sys.argv)

    print(f"  Browser: {browser.title()}")
    print(f"  yt-dlp:  {ytdlp_path}")
    print()

    # Check if browser is running
    if _check_browser_running(browser):
        print(f"  [!!]  {browser.title()} is currently running!")
        print(f"        Close it completely (check system tray) and press Enter.")
        print()
        try:
            input("        Press Enter when ready...")
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit(0)
        print()

    # Resolve profile (interactive selection for Chromium browsers)
    profile = _resolve_profile(browser, explicit_profile)

    raw_cookies = _extract_cookies(ytdlp_path, browser, profile)
    if raw_cookies is None:
        sys.exit(1)

    filtered = _filter_youtube_cookies(raw_cookies)
    cookie_lines = [l for l in filtered.splitlines() if l.strip() and not l.startswith("#")]
    b64 = _encode_base64(filtered)

    print(f"  [OK]  Extracted {len(cookie_lines)} YouTube/Google cookies")
    print(f"  [OK]  Base64 size: {len(b64)} chars")
    print()

    env_line = f"{ENV_VAR_NAME}={b64}"

    # Try clipboard
    copied = _copy_to_clipboard(env_line)

    print("  +--- Ready .env line -------------------------")
    print("  |")
    preview_len = 80
    if len(env_line) > preview_len:
        print(f"  |  {env_line[:preview_len]}...")
    else:
        print(f"  |  {env_line}")
    print("  |")
    print("  +---------------------------------------------")
    print()

    if copied:
        print("  [CLIPBOARD] Copied! Just paste into .env on the server.")
    else:
        print("  Copy the line above and paste it into .env on the server.")

    print()
    print("  Then restart the bot:")
    print("      docker compose restart bot")
    print()


if __name__ == "__main__":
    main()
