from __future__ import annotations

import os
import shutil
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

ANDROID = sys.platform == "android" or "ANDROID_ARGUMENT" in os.environ
ROOT = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
if ANDROID:
    # APK assets are read-only; user-created media belongs in the app-private files area.
    from PySide6.QtCore import QStandardPaths
    DATA_ROOT = Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation))
else:
    DATA_ROOT = ROOT
OUTPUT = DATA_ROOT / "output"
MUSIC = DATA_ROOT / "assets" / "music"
for directory in (OUTPUT, MUSIC):
    directory.mkdir(parents=True, exist_ok=True)

FORMATS = {"Горизонтальный 1920×1080": (1920, 1080), "Вертикальный 1080×1920": (1080, 1920), "Квадратный 1080×1080": (1080, 1080)}
PREVIEW_FORMAT = (1024, 576)
STYLES = {
    "Reference Mode": {"background": "#000000", "foreground": "#F4F4F4", "accent": "#BB3040", "serif": True},
    "Classic Typography": {"background": "#F3EEE4", "foreground": "#26221F", "accent": "#A44C38", "serif": True},
    "Dark Motivation": {"background": "#0D1117", "foreground": "#F4F6F8", "accent": "#F1B74A", "serif": False},
    "Cinematic Quotes": {"background": "#15191F", "foreground": "#F3ECE1", "accent": "#D6A96F", "serif": True},
    "Minimalist Philosophy": {"background": "#F9F9F7", "foreground": "#202020", "accent": "#6A7F72", "serif": False},
}
from visual_engine import ANIMATIONS as VISUAL_ANIMATIONS

LEGACY_ANIMATIONS = ("Как в референсе", "Плавное появление", "По словам", "Посимвольно", "Плавное увеличение", "Последовательное раскрытие")
ANIMATIONS = LEGACY_ANIMATIONS + VISUAL_ANIMATIONS


@lru_cache(maxsize=1)
def find_ffmpeg() -> Path:
    override = os.environ.get("AUTO_SHORTS_FFMPEG")
    candidates = [Path(override)] if override else []
    remembered = ffmpeg_settings_path()
    try:
        saved_path = remembered.read_text(encoding="utf-8").strip()
    except OSError:
        saved_path = ""
    if saved_path:
        candidates.append(Path(saved_path))
    found = shutil.which("ffmpeg")
    if found:
        candidates.append(Path(found))
    candidates += [ROOT / "bin/ffmpeg.exe", ROOT / "ffmpeg.exe",
                   Path("C:/ffmpeg/bin/ffmpeg.exe"), Path("C:/Program Files/ffmpeg/bin/ffmpeg.exe")]
    local_appdata = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
    profile = Path(os.environ.get("USERPROFILE", str(Path.home())))
    capcut_roots = {local_appdata / "CapCut/Apps", profile / "AppData/Local/CapCut/Apps",
                    Path("C:/Program Files/CapCut/Apps")}
    for capcut in capcut_roots:
        if capcut.is_dir():
            candidates += sorted(capcut.glob("*/ffmpeg.exe"), reverse=True)
    for item in candidates:
        if item.is_file():
            return item.resolve()
    raise FileNotFoundError("FFmpeg не найден. Нажмите «Указать FFmpeg…» в левом меню и выберите ffmpeg.exe.")


def ffmpeg_settings_path() -> Path:
    roaming = Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming"))
    return roaming / "AutoShortsStudio" / "ffmpeg_path.txt"


def save_ffmpeg_path(path: str | Path) -> Path:
    executable = Path(path).expanduser().resolve()
    if not executable.is_file() or executable.name.lower() not in {"ffmpeg.exe", "ffmpeg"}:
        raise ValueError("Выберите файл ffmpeg.exe.")
    settings = ffmpeg_settings_path()
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(str(executable), encoding="utf-8")
    find_ffmpeg.cache_clear()
    find_encoder.cache_clear()
    return executable


@lru_cache(maxsize=2)
def find_encoder(ffmpeg: Path) -> str:
    result = subprocess.run([str(ffmpeg), "-hide_banner", "-encoders"], capture_output=True, text=True, errors="replace")
    listing = result.stdout + result.stderr
    for encoder in ("h264_nvenc", "h264_qsv", "h264_amf", "libx264", "h264_mf"):
        if any(line.split()[1:2] == [encoder] for line in listing.splitlines() if line.strip().startswith("V")):
            try:
                probe = subprocess.run([str(ffmpeg), "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=black:s=128x128:r=30:d=0.1", "-frames:v", "1", "-pix_fmt", "yuv420p", "-c:v", encoder, "-f", "null", "-"], capture_output=True, timeout=8)
                if probe.returncode == 0:
                    return encoder
            except subprocess.TimeoutExpired:
                pass
    raise RuntimeError("В установленном FFmpeg нет кодера H.264.")


def font_path(serif: bool, text: str) -> Path:
    if ANDROID:
        candidates = ([Path("/system/fonts/NotoSerif-Regular.ttf"), Path("/system/fonts/NotoSerifCJK-Regular.ttc")]
                      if serif else [Path("/system/fonts/Roboto-Bold.ttf"), Path("/system/fonts/Roboto-Regular.ttf")])
        for item in candidates:
            if item.is_file():
                return item
        raise FileNotFoundError("В Android не найден системный шрифт с поддержкой кириллицы.")
    root = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    kazakh = any(c in text for c in "ӘәҒғҚқҢңӨөҰұҮүҺһІі")
    options = (["times.ttf", "georgia.ttf"] if kazakh else ["times.ttf", "georgia.ttf"]) if serif else ["arialbd.ttf", "segoeuib.ttf"]
    for name in options:
        item = root / name
        if item.exists():
            return item
    raise FileNotFoundError("Не найден системный шрифт с поддержкой кириллицы.")
