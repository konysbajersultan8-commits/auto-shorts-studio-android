"""Unicode emoji helpers for the Windows Pillow renderer."""
from __future__ import annotations

import os
import unicodedata
from functools import lru_cache
from pathlib import Path

from PIL import Image
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QFontMetricsF, QImage, QPainter
from PySide6.QtWidgets import QApplication


_application: QApplication | None = None
_emoji_family: str | None = None


def _is_extend(char: str) -> bool:
    point = ord(char)
    return (unicodedata.category(char) in {"Mn", "Mc", "Me"}
            or 0xFE00 <= point <= 0xFE0F
            or 0xE0100 <= point <= 0xE01EF
            or 0x1F3FB <= point <= 0x1F3FF
            or 0xE0020 <= point <= 0xE007F)


def grapheme_clusters(text: str) -> list[str]:
    """Group combining marks, keycaps, flags, modifiers and ZWJ emoji sequences."""
    result: list[str] = []
    index = 0
    while index < len(text):
        cluster = text[index]
        point = ord(text[index])
        index += 1
        if 0x1F1E6 <= point <= 0x1F1FF and index < len(text) and 0x1F1E6 <= ord(text[index]) <= 0x1F1FF:
            cluster += text[index]
            index += 1
        while index < len(text):
            char = text[index]
            if _is_extend(char):
                cluster += char
                index += 1
                continue
            if char == "\u200d" and index + 1 < len(text):
                cluster += char + text[index + 1]
                index += 2
                continue
            break
        result.append(cluster)
    return result


def is_emoji_cluster(cluster: str) -> bool:
    points = [ord(char) for char in cluster]
    if not points:
        return False
    if any(0x1F000 <= point <= 0x1FAFF or 0x1FC00 <= point <= 0x1FFFF
           or 0x2600 <= point <= 0x27FF
           or 0x2300 <= point <= 0x23FF
           or 0x1F1E6 <= point <= 0x1F1FF
           for point in points):
        return True
    if 0x20E3 in points:  # keycap sequence
        return True
    return 0xFE0F in points and any(point in {0x00A9, 0x00AE, 0x203C, 0x2049, 0x2122, 0x2139}
                                    or 0x2190 <= point <= 0x21FF for point in points)


def split_emoji_runs(text: str) -> list[tuple[str, bool]]:
    """Return adjacent text runs tagged as normal text or color emoji."""
    runs: list[tuple[str, bool]] = []
    for cluster in grapheme_clusters(text):
        is_emoji = is_emoji_cluster(cluster)
        if runs and runs[-1][1] == is_emoji:
            runs[-1] = (runs[-1][0] + cluster, is_emoji)
        else:
            runs.append((cluster, is_emoji))
    return runs


def strip_emojis(text: str) -> str:
    """Speech text: remove emoji graphemes while retaining surrounding words."""
    return " ".join("".join(cluster for cluster in grapheme_clusters(text)
                           if not is_emoji_cluster(cluster)).split())


def emoji_font_file() -> Path:
    if os.environ.get("ANDROID_ARGUMENT") or os.sys.platform == "android":
        candidates = (Path("/system/fonts/NotoColorEmoji.ttf"), Path("/system/fonts/NotoEmoji-Regular.ttf"))
        for candidate in candidates:
            if candidate.is_file():
                return candidate
        raise FileNotFoundError("В системе Android не найден шрифт Noto Color Emoji.")
    windows = Path(os.environ.get("WINDIR", "C:/Windows"))
    candidates = (windows / "Fonts" / "seguiemj.ttf", windows / "Fonts" / "SegoeUIEmoji.ttf")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("Не найден цветной системный шрифт Segoe UI Emoji. Установите обновления шрифтов Windows.")


def initialize_emoji_font() -> None:
    """Register the Windows color font with Qt's DirectWrite text shaper."""
    global _application, _emoji_family
    if QApplication.instance() is None:
        _application = QApplication([])
    if _emoji_family:
        return
    font_id = QFontDatabase.addApplicationFont(str(emoji_font_file()))
    families = QFontDatabase.applicationFontFamilies(font_id) if font_id >= 0 else []
    if not families:
        raise RuntimeError("Не удалось подключить цветной шрифт Segoe UI Emoji.")
    _emoji_family = families[0]


@lru_cache(maxsize=32)
def _emoji_font(size: int) -> QFont:
    initialize_emoji_font()
    font = QFont(_emoji_family or "Segoe UI Emoji")
    font.setPixelSize(size)
    return font


@lru_cache(maxsize=1024)
def emoji_tile(cluster: str, text_size: int) -> tuple[Image.Image, int, int, float]:
    """Rasterize one shaped emoji grapheme as RGBA; cache the color glyph image."""
    size = max(12, round(text_size * 0.72))
    font = _emoji_font(size)
    metrics = QFontMetricsF(font)
    bounds = metrics.boundingRect(cluster)
    advance = max(1, round(metrics.horizontalAdvance(cluster)))
    pad = max(4, size // 8)
    width = max(advance, round(bounds.width())) + 2 * pad
    height = max(size * 2, round(bounds.height())) + 2 * pad
    qimage = QImage(width, height, QImage.Format.Format_RGBA8888)
    qimage.fill(Qt.GlobalColor.transparent)
    painter = QPainter(qimage)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
    painter.setFont(font)
    painter.setPen(QColor("white"))
    painter.drawText(QPointF(pad - bounds.x(), pad - bounds.y()), cluster)
    painter.end()
    canvas = Image.frombytes("RGBA", (width, height), qimage.bits().tobytes(),
                             "raw", "RGBA", qimage.bytesPerLine())
    bbox = canvas.getchannel("A").getbbox()
    if bbox is None:
        raise ValueError(f"Windows не смог отрисовать эмодзи {cluster!r}.")
    tile = canvas.crop(bbox)
    x_offset = bbox[0] - pad
    y_offset = bbox[1] - pad
    return tile, x_offset, y_offset, float(advance)
