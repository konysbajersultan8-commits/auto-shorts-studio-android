from __future__ import annotations


def clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def smooth(value: float) -> float:
    t = clamp(value)
    return t * t * (3 - 2 * t)


def word_alpha(mode: str, index: int, count: int, local_time: float, duration: float, speed: float) -> float:
    fade_out = smooth((duration - local_time) / 0.22)
    if mode == "Плавное появление":
        return smooth(local_time / 0.35) * fade_out
    if mode == "Плавное увеличение":
        return smooth(local_time / 0.45) * fade_out
    if mode in {"Fade In", "Fade Out", "Blur Reveal", "Mask Reveal", "Slide Up", "Slide Down",
                "Slide Left", "Slide Right", "Zoom In", "Zoom Out", "Pop", "Glitch", "Flicker",
                "Subtle Pulse", "Floating", "Shake", "Highlight Sweep", "Karaoke Highlight"}:
        return fade_out
    if mode == "Посимвольно":
        return fade_out
    step = (0.26 if mode in {"Как в референсе", "Word by Word"} else 0.22) / speed
    start = index * step
    return smooth((local_time - start) / 0.14) * fade_out


def word_scale(mode: str, local_time: float) -> float:
    if mode == "Плавное увеличение":
        return 0.92 + 0.08 * smooth(local_time / 0.45)
    return 1.0
