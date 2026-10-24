"""Visual libraries for AUTO SHORTS STUDIO's Pillow renderer."""
from __future__ import annotations

import math
import os
import sys
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageChops, ImageColor, ImageDraw, ImageEnhance, ImageFilter


ANIMATIONS = (
    "Fade In", "Typewriter", "Word by Word", "Letter by Letter", "Slide Up", "Slide Down",
    "Slide Left", "Slide Right", "Zoom In", "Zoom Out", "Pop", "Blur Reveal", "Mask Reveal",
    "Glitch", "Flicker", "Subtle Pulse", "Floating", "Shake", "Highlight Sweep", "Karaoke Highlight",
)
BACKGROUNDS = (
    "По стилю",
    "Pure Black", "Dark Gray", "White Minimal", "Deep Navy", "Black Gradient", "Moving Gradient",
    "Film Grain", "Floating Particles", "Dark Smoke", "Soft Light", "Vignette Pulse", "Digital Noise",
    "Grid Motion", "Abstract Waves", "Subtle Red Glow",
)
PRESETS = {
    "CLASSIC DARK": dict(style="Reference Mode", background="Pure Black", animation="Word by Word", foreground="#F4F4F4", accent="#BB3040", serif=True),
    "CINEMATIC": dict(style="Cinematic Quotes", background="Film Grain", animation="Blur Reveal", foreground="#F3ECE1", accent="#D6A96F", serif=True),
    "LUXURY": dict(style="Classic Typography", background="Black Gradient", animation="Fade In", foreground="#E8D8B8", accent="#B6985A", serif=True),
    "AGGRESSIVE": dict(style="Dark Motivation", background="Digital Noise", animation="Glitch", foreground="#FFFFFF", accent="#FF425B", serif=False),
    "PHILOSOPHY": dict(style="Minimalist Philosophy", background="Deep Navy", animation="Fade In", foreground="#F2F2ED", accent="#8AAE9B", serif=True),
    "MOTIVATION": dict(style="Dark Motivation", background="Subtle Red Glow", animation="Pop", foreground="#FFFFFF", accent="#F1B74A", serif=False),
    "GLITCH": dict(style="Dark Motivation", background="Digital Noise", animation="Glitch", foreground="#FFFFFF", accent="#48E4ED", serif=False),
    "CLEAN WHITE": dict(style="Minimalist Philosophy", background="White Minimal", animation="Fade In", foreground="#202020", accent="#6A7F72", serif=False),
    "DARK RED": dict(style="Reference Mode", background="Subtle Red Glow", animation="Word by Word", foreground="#F4F4F4", accent="#E13D52", serif=True),
    "FUTURISTIC": dict(style="Dark Motivation", background="Grid Motion", animation="Mask Reveal", foreground="#E9F5FF", accent="#43C7FF", serif=False),
}
for _preset in PRESETS.values():
    _preset["font"] = "Georgia" if _preset["serif"] else "Arial"
    _preset["animation_in"] = "Fade In"
    _preset["animation_out"] = "Fade Out"
PRESETS["CLASSIC DARK"].update(background="По стилю", font="Times New Roman", animation="Как в референсе",
                               animation_in="None", animation_out="None", animation_hold="None",
                               bold=False, text_shadow=0.0, text_outline=0.0, position_y=-0.08)

# These are installed Windows fonts with broad Cyrillic coverage. User font files
# in <application>/fonts are listed dynamically as additional choices.
SYSTEM_FONTS = {
    "Arial": "arial.ttf", "Arial Narrow": "ARIALN.TTF", "Bahnschrift": "bahnschrift.ttf",
    "Calibri": "calibri.ttf", "Cambria": "cambria.ttc", "Candara": "Candara.ttf",
    "Constantia": "constan.ttf", "Corbel": "corbel.ttf", "Georgia": "georgia.ttf",
    "Segoe UI": "segoeui.ttf", "Sylfaen": "sylfaen.ttf", "Times New Roman": "times.ttf",
    "Trebuchet MS": "trebuc.ttf", "Verdana": "verdana.ttf", "Bookman Old Style": "BOOKOS.TTF",
}


def available_fonts() -> dict[str, str]:
    if sys.platform == "android" or os.environ.get("ANDROID_ARGUMENT"):
        result = {}
        for name, filename in {"Roboto": "Roboto-Regular.ttf", "Roboto Bold": "Roboto-Bold.ttf",
                               "Noto Serif": "NotoSerif-Regular.ttf"}.items():
            path = Path("/system/fonts") / filename
            if path.is_file():
                result[name] = str(path)
        return result
    root = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    result = {name: str(root / filename) for name, filename in SYSTEM_FONTS.items()
              if (root / filename).is_file()}
    app_root = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
    local = app_root / "fonts"
    if local.is_dir():
        for path in sorted(local.iterdir()):
            if path.suffix.lower() in {".ttf", ".otf", ".ttc"}:
                result.setdefault(path.stem, str(path))
    return result


def _lerp(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    t = max(0.0, min(1.0, t))
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))


@lru_cache(maxsize=24)
def _solid_background(size: tuple[int, int], color: str) -> Image.Image:
    return Image.new("RGB", size, color)


def apply_tint(image: Image.Image, tint: str = "", intensity: float = .25) -> Image.Image:
    if not tint:
        return image
    color = ImageColor.getrgb(tint)
    overlay = Image.new("RGB", image.size, color)
    return Image.blend(image.convert("RGB"), overlay, max(0.0, min(.55, intensity * .35)))


def render_background(size: tuple[int, int], name: str, t: float = 0.0,
                      intensity: float = 0.25, speed: float = 1.0,
                      source: Image.Image | None = None, brightness: float = 0.0,
                      contrast: float = 1.0, tint: str = "") -> Image.Image:
    w, h = size
    if source is not None:
        image = source.convert("RGB")
        factor = max(w / image.width, h / image.height)
        image = image.resize((round(image.width * factor), round(image.height * factor)), Image.Resampling.LANCZOS)
        left, top = (image.width - w) // 2, (image.height - h) // 2
        image = image.crop((left, top, left + w, top + h))
        image = image.filter(ImageFilter.GaussianBlur(max(0, round(intensity * 18))))
        if intensity:
            shade = Image.new("RGB", size, (0, 0, 0))
            image = Image.blend(image, shade, max(0.0, min(0.85, intensity * 0.6)))
        image = ImageEnhance.Brightness(image).enhance(max(.1, 1 + brightness))
        image = ImageEnhance.Contrast(image).enhance(max(.1, contrast))
        return apply_tint(image, tint, intensity)
    if name == "По стилю":
        return apply_tint(_solid_background(size, "#000000"), tint, intensity)
    if name == "White Minimal":
        return apply_tint(_solid_background(size, "#F8F8F6"), tint, intensity)
    bases = {"Pure Black": "#000000", "Dark Gray": "#17191D", "Deep Navy": "#091525"}
    if name in bases:
        return apply_tint(_solid_background(size, bases[name]), tint, intensity)
    # Render animated backgrounds at a small working resolution, then upscale.
    sw, sh = min(360, w), min(640, h)
    image = Image.new("RGB", (sw, sh), "#08090D")
    draw = ImageDraw.Draw(image)
    strength = max(0.0, min(1.0, intensity))
    phase = t * max(0.05, speed)
    if name in {"Black Gradient", "Moving Gradient", "Subtle Red Glow", "Soft Light", "Dark Smoke"}:
        top = (7, 8, 12)
        bottom = (25, 8, 15) if name in {"Subtle Red Glow", "Dark Smoke"} else (20, 23, 33)
        for y in range(sh):
            wave = 0.5 + 0.5 * math.sin(phase * 0.25 + y / sh * math.pi) if name == "Moving Gradient" else y / sh
            draw.line((0, y, sw, y), fill=_lerp(top, bottom, wave * strength))
        if name in {"Subtle Red Glow", "Soft Light", "Dark Smoke"}:
            glow = Image.new("RGBA", (sw, sh), (0, 0, 0, 0))
            gd = ImageDraw.Draw(glow)
            color = (177, 25, 49, round(90 * strength)) if name != "Soft Light" else (150, 155, 190, round(55 * strength))
            cx = round(sw * (0.5 + 0.25 * math.sin(phase * 0.35)))
            cy = round(sh * (0.45 + 0.12 * math.cos(phase * 0.29)))
            radius = round(min(sw, sh) * (0.5 if name == "Dark Smoke" else 0.32))
            gd.ellipse((cx-radius, cy-radius, cx+radius, cy+radius), fill=color)
            glow = glow.filter(ImageFilter.GaussianBlur(max(8, sw // 9)))
            image = Image.alpha_composite(image.convert("RGBA"), glow).convert("RGB")
    elif name in {"Film Grain", "Digital Noise"}:
        # Deterministic, frame-varying noise for reproducible renders.
        import random
        rng = random.Random(round(t * 24) if name in {"Digital Noise", "Film Grain"} else 773)
        count = int(sw * sh * (0.01 if name == "Film Grain" else 0.08) * strength)
        for _ in range(count):
            x, y = rng.randrange(sw), rng.randrange(sh)
            v = rng.randrange(100, 240) if name == "Film Grain" else rng.randrange(30, 180)
            draw.point((x, y), fill=(v, v, v) if name == "Film Grain" else (10, v, min(255, v + 20)))
        image = image.filter(ImageFilter.GaussianBlur(0.15))
    elif name in {"Floating Particles", "Vignette Pulse"}:
        import random
        rng = random.Random(19)
        if name == "Floating Particles":
            for i in range(48):
                x = int((rng.random() * sw + math.sin(phase * .2 + i) * 9) % sw)
                y = int((rng.random() * sh - phase * 6) % sh)
                r = 1 + i % 3
                draw.ellipse((x-r, y-r, x+r, y+r), fill=(100, 112, 135))
        else:
            alpha = int(30 + 30 * (0.5 + .5 * math.sin(phase)))
            vignette = Image.new("RGBA", (sw, sh), (0, 0, 0, 0))
            vd = ImageDraw.Draw(vignette)
            for inset in range(0, min(sw, sh)//2, 8):
                vd.rectangle((inset, inset, sw-inset-1, sh-inset-1), outline=(0, 0, 0, alpha))
            image = Image.alpha_composite(image.convert("RGBA"), vignette).convert("RGB")
    elif name in {"Grid Motion", "Abstract Waves"}:
        color = (50, 110, 145) if name == "Grid Motion" else (70, 60, 90)
        if name == "Grid Motion":
            shift = round(phase * 9) % 30
            for x in range(-30 + shift, sw, 30): draw.line((x, 0, x, sh), fill=color, width=1)
            for y in range(-30 + shift, sh, 30): draw.line((0, y, sw, y), fill=color, width=1)
        else:
            for row in range(8):
                points = [(x, round(sh * (0.25 + row * .075) + math.sin(x / sw * math.tau * 1.4 + phase + row) * sh * .06)) for x in range(0, sw, 3)]
                draw.line(points, fill=color, width=2)
    result = image.resize(size, Image.Resampling.BILINEAR)
    if brightness or contrast != 1:
        result = ImageEnhance.Brightness(result).enhance(max(.1, 1 + brightness))
        result = ImageEnhance.Contrast(result).enhance(max(.1, contrast))
    return apply_tint(result, tint, intensity)


def apply_animation(overlay: Image.Image, mode: str, elapsed: float, duration: float,
                    speed: float = 1.0, intensity: float = 1.0,
                    easing: str = "Smooth") -> Image.Image:
    """Apply a restrained visual transform to a transparent text layer."""
    from animations import smooth
    w, h = overlay.size
    if not mode or mode == "None":
        return overlay
    phase_in = max(0.0, min(1.0, elapsed * max(.1, speed) / .35))
    phase_out = max(0.0, min(1.0, (duration - elapsed) * max(.1, speed) / .3))
    progress = smooth(phase_in) if easing == "Smooth" else phase_in
    move_x = move_y = 0
    alpha = 1.0
    scale = 1.0
    blur = 0
    if mode == "Fade In": alpha = progress
    elif mode == "Fade Out": alpha = 1.0 - progress
    elif mode == "Slide Up": move_y = round((1-progress) * h * .08 * intensity)
    elif mode == "Slide Down": move_y = -round((1-progress) * h * .08 * intensity)
    elif mode == "Slide Left": move_x = round((1-progress) * w * .08 * intensity)
    elif mode == "Slide Right": move_x = -round((1-progress) * w * .08 * intensity)
    elif mode == "Zoom In": scale = .72 + .28 * progress
    elif mode == "Zoom Out": scale = 1.22 - .22 * progress
    elif mode == "Pop": scale = .82 + .18 * min(1.15, progress * 1.3)
    elif mode == "Blur Reveal": blur = round((1-progress) * 10 * intensity)
    elif mode in {"Typewriter", "Letter by Letter", "Word by Word"}:
        mask = Image.new("L", (w, h), 0)
        ImageDraw.Draw(mask).rectangle((0, 0, round(w * progress), h), fill=255)
        overlay.putalpha(ImageChops.multiply(overlay.getchannel("A"), mask))
    elif mode == "Mask Reveal":
        mask = Image.new("L", (w, h), 0)
        ImageDraw.Draw(mask).rectangle((0, 0, round(w * progress), h), fill=255)
        overlay.putalpha(Image.eval(Image.composite(overlay.getchannel("A"), Image.new("L", (w, h)), mask), lambda v: round(v * phase_out)))
    elif mode == "Glitch" and progress < .7:
        dx = round(math.sin(elapsed * 93) * 7 * intensity)
        shifted = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        shifted.alpha_composite(overlay, (dx, 0))
        overlay = shifted
    elif mode == "Flicker": alpha = (0.45 + .55 * ((int(elapsed * 18) % 2))) * phase_out
    elif mode == "Subtle Pulse": scale = 1 + math.sin(elapsed * 3.2) * .025 * intensity
    elif mode == "Floating": move_y = round(math.sin(elapsed * 1.5) * 8 * intensity)
    elif mode == "Shake" and elapsed < .24: move_x = round(math.sin(elapsed * 95) * 6 * intensity)
    elif mode in {"Highlight Sweep", "Karaoke Highlight"}:
        # Highlight is rendered as a subtle masked white overlay on text bounds.
        a = overlay.getchannel("A")
        sweep = Image.new("RGBA", (w, h), (255, 255, 255, 0))
        sd = ImageDraw.Draw(sweep)
        x = round((elapsed / max(.1, duration) % 1) * w)
        sd.rectangle((x - 45, 0, x + 45, h), fill=(255, 255, 255, round(35 * intensity)))
        sweep.putalpha(ImageChops.multiply(sweep.getchannel("A"), a))
        overlay = Image.alpha_composite(overlay, sweep)
    if blur:
        overlay = overlay.filter(ImageFilter.GaussianBlur(blur))
    if alpha < 1:
        overlay.putalpha(overlay.getchannel("A").point(lambda value: round(value * alpha)))
    if abs(scale - 1) > .002:
        nw, nh = max(1, round(w * scale)), max(1, round(h * scale))
        overlay = overlay.resize((nw, nh), Image.Resampling.BICUBIC)
        canvas = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        canvas.alpha_composite(overlay, ((w - nw)//2, (h - nh)//2))
        overlay = canvas
    if move_x or move_y:
        canvas = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        canvas.alpha_composite(overlay, (move_x, move_y))
        overlay = canvas
    return overlay
