from __future__ import annotations

import math
import re
import shutil
import subprocess
import tempfile
import uuid
from functools import lru_cache
from pathlib import Path
from threading import Event
from typing import Callable

from PIL import Image, ImageColor, ImageDraw, ImageFont

from animations import smooth, word_alpha, word_scale
from audio_engine import detect_onsets, mux_audio
from config import FORMATS, OUTPUT, PREVIEW_FORMAT, STYLES, find_encoder, find_ffmpeg, font_path
from emoji_support import emoji_tile, grapheme_clusters, is_emoji_cluster, strip_emojis
from narration import NarrationTrack, prepare_narration
from elevenlabs_tts import synthesize
from text_processor import Word, apply_auto_accent, scenes
from visual_engine import ANIMATIONS as VISUAL_ANIMATIONS, BACKGROUNDS, apply_animation, apply_tint, render_background


def next_video_path(output_dir: Path = OUTPUT) -> Path:
    """Return the next unused sequential Russian video filename."""
    pattern = re.compile(r"^ролик (\d+)\.mp4$", re.IGNORECASE)
    numbers = [int(match.group(1)) for path in output_dir.glob("*.mp4")
               if (match := pattern.fullmatch(path.name))]
    return output_dir / f"ролик {max(numbers, default=0) + 1}.mp4"


def _text_width(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, spacing: float) -> float:
    clusters = grapheme_clusters(text)
    widths = [emoji_tile(cluster, font.size)[3] if is_emoji_cluster(cluster)
              else font.getlength(cluster) for cluster in clusters]
    return sum(widths) + max(0, len(widths) - 1) * spacing


def _draw_spaced(canvas: Image.Image, xy: tuple[float, float], text: str, font: ImageFont.FreeTypeFont, color: tuple[int, int, int, int], spacing: float, stroke_width: int = 0, bold: bool = False) -> None:
    x, y = xy
    draw = ImageDraw.Draw(canvas)
    text_box = font.getbbox("H", anchor="la")
    text_center = (text_box[1] + text_box[3]) / 2
    for cluster in grapheme_clusters(text):
        if is_emoji_cluster(cluster):
            tile, x_offset, y_offset, advance = emoji_tile(cluster, font.size)
            glyph_center = y_offset + tile.height / 2
            layer = tile
            if color[3] < 255:
                layer = tile.copy()
                layer.putalpha(layer.getchannel("A").point(lambda value: round(value * color[3] / 255)))
            canvas.alpha_composite(layer, (round(x + x_offset), round(y + text_center - glyph_center)))
        else:
            draw.text((round(x), round(y)), cluster, font=font, fill=color, anchor="la",
                      stroke_width=max(stroke_width, 1 if bold else 0), stroke_fill=color)
            advance = font.getlength(cluster)
        x += advance + spacing


def map_speech_timing_to_display(groups: list[list[Word]], speech_starts: list[float],
                                 speech_ends: list[float]) -> tuple[list[float], list[float]]:
    """Attach speech timestamps to display words; emoji-only tokens follow a neighbor."""
    display_words = [word for group in groups for word in group]
    starts: list[float | None] = [None] * len(display_words)
    ends: list[float | None] = [None] * len(display_words)
    spoken_indexes = [index for index, word in enumerate(display_words)
                      if strip_emojis(word.text).strip()]
    if len(spoken_indexes) != len(speech_starts) or len(speech_starts) != len(speech_ends):
        raise RuntimeError("Текст речи и временные метки не совпали по числу слов после удаления эмодзи.")
    for speech_index, display_index in enumerate(spoken_indexes):
        starts[display_index] = speech_starts[speech_index]
        ends[display_index] = speech_ends[speech_index]
    for index in range(len(display_words)):
        if starts[index] is not None:
            continue
        following = next((candidate for candidate in spoken_indexes if candidate > index), None)
        neighbor = following if following is not None else next(
            (candidate for candidate in reversed(spoken_indexes) if candidate < index), None)
        if neighbor is None:
            raise RuntimeError("В ролике есть эмодзи, но нет слов для синхронизации с речью.")
        starts[index], ends[index] = starts[neighbor], ends[neighbor]
    return ([float(value) for value in starts], [float(value) for value in ends])


def _display_groups_for_speech(groups: list[list[Word]], speech_counts: list[int]) -> list[list[Word]]:
    """Insert emoji-only display tokens beside speech phrases without voicing them."""
    words = [word for group in groups for word in group]
    result: list[list[Word]] = []
    cursor = 0
    for speech_count in speech_counts:
        group: list[Word] = []
        spoken = 0
        while cursor < len(words) and spoken < speech_count:
            word = words[cursor]
            group.append(word)
            cursor += 1
            if strip_emojis(word.text).strip():
                spoken += 1
        result.append(apply_auto_accent(group))
    if cursor < len(words):
        if not result:
            raise RuntimeError("Не удалось сопоставить эмодзи с фразами озвучки.")
        result[-1].extend(words[cursor:])
    if sum(bool(strip_emojis(word.text).strip()) for group in result for word in group) != sum(speech_counts):
        raise RuntimeError("Отображаемый текст и фразы речи не совпали.")
    return result


def _layout_uncached(words: list[Word], size: tuple[int, int], font_file: Path, target_size: int, max_width: float, letter_spacing: float = .035, line_spacing: float = 1.75) -> tuple[ImageFont.FreeTypeFont, list[list[tuple[Word, float]]], float]:
    probe = Image.new("RGB", (1, 1))
    draw = ImageDraw.Draw(probe)
    for font_size in range(target_size, 11, -2):
        font = ImageFont.truetype(str(font_file), font_size)
        letter_space = font_size * letter_spacing
        space = font_size * 0.30
        lines: list[list[tuple[Word, float]]] = [[]]
        current = 0.0
        for word in words:
            width = _text_width(draw, word.text, font, letter_space)
            if current and current + space + width > max_width:
                lines.append([])
                current = 0.0
            if current:
                current += space
            lines[-1].append((word, width))
            current += width
        fits_width = all(sum(w for _, w in line) + space * (len(line) - 1) <= max_width + 2 for line in lines)
        fits_height = font_size * (1 + line_spacing * (len(lines) - 1)) <= size[1] * 0.70
        if len(lines) <= (9 if size[1] > size[0] else 6) and fits_width and fits_height:
            return font, lines, letter_space
    raise ValueError("Текст содержит слишком длинное слово для выбранного формата. Сократите его или выберите другой формат.")


@lru_cache(maxsize=512)
def _layout_cached(words_key: tuple[tuple[str, bool], ...], size: tuple[int, int], font_file: str,
                   target_size: int, max_width: float, letter_spacing: float, line_spacing: float):
    words = [Word(text, accent) for text, accent in words_key]
    return _layout_uncached(words, size, Path(font_file), target_size, max_width, letter_spacing, line_spacing)


def _layout(words: list[Word], size: tuple[int, int], font_file: Path, target_size: int,
            max_width: float, letter_spacing: float = .035, line_spacing: float = 1.75):
    return _layout_cached(tuple((word.text, word.accent) for word in words), size, str(font_file),
                          target_size, max_width, letter_spacing, line_spacing)


def scene_duration(words: list[Word], speed: float, override: float | None) -> float:
    if override and override > 0:
        return override
    return max(1.5, min(6.0, (1.05 + len(words) * 0.28) / speed))


def render_frame(words: list[Word], size: tuple[int, int], style: str, animation: str, local_time: float, duration: float, speed: float, font_override: str = "", foreground: str = "", accent: str = "", delay: float = 0, opacity: float = 1, scale: float = 1, position_x: float = 0, position_y: float = 0, word_times: list[float] | None = None, word_end_times: list[float] | None = None, background: str = "По стилю", background_intensity: float = 0.25, background_speed: float = 1.0, background_source: Image.Image | None = None, animation_intensity: float = 1.0, easing: str = "Smooth", animation_in: str = "None", animation_hold: str = "None", animation_out: str = "None", animation_duration: float = .35, animation_speed: float = 1.0, font_size: float = 1.0, letter_spacing: float = .035, line_spacing: float = 1.75, text_align: str = "center", bold: bool = False, text_shadow: float = 0.0, text_outline: float = 0.0, background_brightness: float = 0.0, background_contrast: float = 1.0, background_tint: str = "") -> Image.Image:
    theme = STYLES[style]
    image = (render_background(size, background, local_time, background_intensity, background_speed,
                               background_source, background_brightness, background_contrast, background_tint)
             if background_source is not None else
             apply_tint(Image.new("RGB", size, theme["background"]), background_tint, background_intensity)
             if background == "По стилю" else
             render_background(size, background, local_time, background_intensity, background_speed,
                               None, background_brightness, background_contrast, background_tint))
    draw = ImageDraw.Draw(image)
    is_portrait = size[1] > size[0]
    font_file = Path(font_override) if font_override else font_path(theme["serif"], " ".join(w.text for w in words))
    if not font_file.is_file():
        raise ValueError("Файл шрифта не найден.")
    base = round(min(size[0] / 1024, size[1] / 576) * (58 if is_portrait else 39) * scale * font_size)
    base = max(22, base)
    dynamic = animation in {"Как в референсе", "Word by Word"} and (animation != "Как в референсе" or style == "Reference Mode")
    shown = words
    if dynamic:
        step = 0.26 / speed
        count = sum(local_time >= start for start in word_times) if word_times else int(max(0, local_time - delay) / step) + 1
        count = max(1, min(len(words), count))
        shown = words[:count]
    font, lines, letter_space = _layout(shown, size, font_file, base, size[0] * (0.84 if is_portrait else 0.78), letter_spacing, line_spacing)
    line_step = round(font.size * line_spacing)
    total_height = font.size + (len(lines) - 1) * line_step
    top = (size[1] - total_height) / 2 - font.size * 0.14 + size[1] * position_y
    normal_rgb = ImageColor.getrgb(foreground or theme["foreground"])
    accent_rgb = ImageColor.getrgb(accent or theme["accent"])
    overlay = Image.new("RGBA", size, (0, 0, 0, 0))
    pen = ImageDraw.Draw(overlay)
    global_index = 0
    phrase_fade_start = max(word_end_times[-1], duration - .4) if animation == "Как в референсе" and word_end_times else duration - .4
    phrase_fade_duration = max(.01, duration - phrase_fade_start)
    for line_no, line in enumerate(lines):
        space = font.size * 0.30
        width = sum(w for _, w in line) + space * (len(line) - 1)
        align_offset = (size[0] - width) / 2 if text_align == "center" else (size[0] * .08 if text_align == "left" else size[0] * .92 - width)
        x = align_offset + size[0] * position_x
        y = top + line_no * line_step
        for word, word_width in line:
            if word_times and word_end_times and animation != "Как в референсе":
                alpha = (smooth((local_time - word_times[global_index]) / 0.08) *
                         smooth((word_end_times[global_index] - local_time) / 0.12) * opacity)
            elif word_times and animation == "Как в референсе":
                # Keep each spoken word visible until the complete phrase fades.
                alpha = (smooth((local_time - word_times[global_index]) / 0.12) *
                         smooth((duration - local_time) / phrase_fade_duration) * opacity)
            elif word_times and animation in {"Как в референсе", "По словам", "Последовательное раскрытие"}:
                alpha = smooth((local_time - word_times[global_index]) / 0.14) * smooth((duration - local_time) / 0.22) * opacity
            else:
                alpha = word_alpha(animation, global_index, len(words), local_time - delay, duration - delay, speed) * opacity
            if animation in {"Посимвольно", "Letter by Letter", "Typewriter"}:
                word_clusters = grapheme_clusters(word.text)
                chars = max(0, min(len(word_clusters), int(((local_time - delay) * (14 if animation == "Letter by Letter" else 24) * speed) - sum(len(grapheme_clusters(w.text)) + 1 for w in words[:global_index])) + 1))
                visible = "".join(word_clusters[:chars])
                alpha = smooth((duration - local_time) / 0.22) * opacity if local_time >= delay else 0
            else:
                visible = word.text
            rgb = accent_rgb if word.accent else normal_rgb
            if animation == "Karaoke Highlight" and word_times and global_index < len(word_times) and word_times[global_index] <= local_time <= (word_end_times[global_index] if word_end_times else word_times[global_index] + .35):
                rgb = accent_rgb
            if alpha > 0 and visible:
                _draw_spaced(overlay, (x, y), visible, font, (*rgb, round(255 * alpha)), letter_space,
                             round(text_outline * font.size / 50), bold)
            x += word_width + space
            global_index += 1
    if text_shadow > 0:
        from PIL import ImageFilter
        shadow = Image.new("RGBA", size, (0, 0, 0, 0))
        shadow.putalpha(overlay.getchannel("A").point(lambda value: round(value * min(1.0, text_shadow))))
        shadow = shadow.filter(ImageFilter.GaussianBlur(max(1, round(font.size * .035 * text_shadow))))
        shifted = Image.new("RGBA", size, (0, 0, 0, 0))
        shifted.alpha_composite(shadow, (round(font.size * .05), round(font.size * .06)))
        overlay = Image.alpha_composite(shifted, overlay)
    if animation in {"Плавное увеличение", "Zoom In"}:
        scale = word_scale(animation, local_time)
        if scale < 0.999:
            new_size = (max(1, round(size[0] * scale)), max(1, round(size[1] * scale)))
            overlay = overlay.resize(new_size, Image.Resampling.BICUBIC)
            canvas = Image.new("RGBA", size, (0, 0, 0, 0))
            canvas.alpha_composite(overlay, ((size[0] - new_size[0]) // 2, (size[1] - new_size[1]) // 2))
            overlay = canvas
    if animation in VISUAL_ANIMATIONS:
        overlay = apply_animation(overlay, animation, max(0, local_time - delay), max(.1, duration - delay), animation_speed, animation_intensity, easing)
    phase_in = min(animation_duration, duration * .4)
    phase_out = min(animation_duration, duration * .4)
    if animation_in != "None" and local_time < phase_in:
        overlay = apply_animation(overlay, animation_in, local_time, phase_in, animation_speed, animation_intensity, easing)
    elif animation_hold != "None" and phase_in <= local_time < duration - phase_out:
        overlay = apply_animation(overlay, animation_hold, local_time - phase_in,
                                  max(.1, duration - phase_in - phase_out), animation_speed,
                                  animation_intensity, easing)
    elif animation_out != "None" and local_time >= duration - phase_out:
        overlay = apply_animation(overlay, animation_out, local_time - (duration - phase_out), phase_out,
                                  animation_speed, animation_intensity, easing)
    return Image.alpha_composite(image.convert("RGBA"), overlay).convert("RGB")


def render_video(text: str, *, style: str = "Reference Mode", animation: str = "Как в референсе", format_name: str = "Горизонтальный 1920×1080", fps: int = 30, speed: float = 1.0, music: Path | None = None, volume: float = 0.7, fade: float = 0.3, duration: float | None = None, font_override: str = "", foreground: str = "", accent: str = "", preview: bool = False, delay: float = 0, opacity: float = 1, scale: float = 1, position_x: float = 0, position_y: float = 0, sync_mode: str = "Равномерно", narration: bool = False, voice_id: str = "", voice_speed: float = 0.82, voice_volume: int = 100, voice_preset: str = "Dramatic Narrator", voice_reverb: float = 0.08, phrase_pause: float = 0.35, tts_provider: str = "sapi", elevenlabs_model: str = "eleven_multilingual_v2", elevenlabs_stability: float = 0.52, elevenlabs_similarity: float = 0.78, elevenlabs_style: float = 0.10, elevenlabs_speaker_boost: bool = True, background: str = "По стилю", background_intensity: float = 0.25, background_speed: float = 1.0, background_path: str = "", animation_intensity: float = 1.0, easing: str = "Smooth", animation_in: str = "None", animation_hold: str = "None", animation_out: str = "None", animation_duration: float = .35, animation_speed: float = 1.0, font_size: float = 1.0, letter_spacing: float = .035, line_spacing: float = 1.75, text_align: str = "center", bold: bool = False, text_shadow: float = 0.0, text_outline: float = 0.0, background_brightness: float = 0.0, background_contrast: float = 1.0, background_tint: str = "", progress: Callable[[float], None] | None = None, cancel: Event | None = None) -> Path:
    if style not in STYLES or animation not in ("Как в референсе", "Плавное появление", "По словам", "Посимвольно", "Плавное увеличение", "Последовательное раскрытие", *VISUAL_ANIMATIONS):
        raise ValueError("Неизвестный стиль или анимация.")
    if background not in BACKGROUNDS or not 0 <= background_intensity <= 1 or not 0.1 <= background_speed <= 3:
        raise ValueError("Параметры фона вне допустимого диапазона.")
    if fps not in (30, 60) or not 0.5 <= speed <= 2.0:
        raise ValueError("FPS должен быть 30 или 60; скорость — от 0,5 до 2.")
    if not (0 <= delay <= 2 and 0 <= opacity <= 1 and 0.5 <= scale <= 1.5 and -0.25 <= position_x <= 0.25 and -0.25 <= position_y <= 0.25):
        raise ValueError("Параметры анимации вне допустимого диапазона.")
    if sync_mode not in {"Равномерно", "Акценты музыки (экспериментально)"}:
        raise ValueError("Неизвестный режим синхронизации.")
    size = PREVIEW_FORMAT if preview else FORMATS.get(format_name)
    if size is None:
        raise ValueError("Неизвестный формат видео.")
    ffmpeg = find_ffmpeg()
    encoder = find_encoder(ffmpeg)
    scratch: Path | None = None
    track: NarrationTrack | None = None
    voice_path: Path | None = None
    voice_gain = voice_volume
    timed_scenes: list[tuple[list[Word], float, list[float] | None, list[float] | None]] = []
    if narration:
        if tts_provider == "sapi" and not voice_id:
            raise ValueError("Выберите голос Windows в разделе «Озвучка ElevenLabs».")
        scratch = Path(tempfile.mkdtemp(prefix=".narration_", dir=OUTPUT))
        try:
            if tts_provider == "elevenlabs":
                if cancel and cancel.is_set():
                    raise InterruptedError("Генерация остановлена.")
                aligned = synthesize(strip_emojis(text), scratch / "narration.mp3", model_id=elevenlabs_model,
                                     stability=elevenlabs_stability, similarity_boost=elevenlabs_similarity,
                                     style=elevenlabs_style, speaker_boost=elevenlabs_speaker_boost,
                                     speed=voice_speed, voice_id=voice_id)
                voice_path = aligned.audio_path
                voice_gain = voice_volume
                groups = [apply_auto_accent(group) for group in scenes(text)]
                display_starts, display_ends = map_speech_timing_to_display(
                    groups, aligned.word_starts, aligned.word_ends)
                offset = 0
                word_count = len(display_starts)
                for index, group in enumerate(groups):
                    first = display_starts[offset]
                    next_start = display_starts[offset + len(group)] if offset + len(group) < word_count else aligned.seconds
                    scene_seconds = max(0.25, next_start - first)
                    starts = [max(0.0, item - first) for item in display_starts[offset:offset + len(group)]]
                    ends = [max(starts[i] + 0.04, item - first) for i, item in enumerate(display_ends[offset:offset + len(group)])]
                    timed_scenes.append((group, scene_seconds, starts, ends))
                    offset += len(group)
            elif tts_provider == "sapi":
                track = prepare_narration(strip_emojis(text), voice_id, scratch, speed=voice_speed,
                                          volume=voice_volume, preset=voice_preset,
                                          reverb=voice_reverb, pause=phrase_pause, cancel=cancel)
                voice_path = track.path
                display_groups = _display_groups_for_speech(
                    scenes(text),
                    [len(segment.words) for segment in track.segments])
                for group, segment in zip(display_groups, track.segments):
                    speech_starts = [0.04 + index * max(0.08, (segment.speech_seconds - 0.15) / max(1, len(segment.words)))
                                     for index in range(len(segment.words))]
                    starts, _ = map_speech_timing_to_display(
                        [group], speech_starts, [min(segment.speech_seconds, start + 0.12) for start in speech_starts])
                    timed_scenes.append((group, segment.scene_seconds, starts, None))
                voice_gain = 100  # SAPI applies its volume during FFmpeg processing.
            else:
                raise ValueError("Неизвестный движок озвучки.")
        except Exception:
            shutil.rmtree(scratch)
            raise
    else:
        groups = [apply_auto_accent(group) for group in scenes(text)]
        lengths = [scene_duration(group, speed, duration) + delay for group in groups]
        timed_scenes = [(group, length, None, None) for group, length in zip(groups, lengths)]
    total_frames = sum(math.ceil(item[1] * fps) for item in timed_scenes)
    total_seconds = total_frames / fps
    onsets = detect_onsets(ffmpeg, music, total_seconds) if music and sync_mode == "Акценты музыки (экспериментально)" else []
    name = uuid.uuid4().hex[:12]
    silent = OUTPUT / f".{name}_silent.mp4"
    result = next_video_path(OUTPUT)
    command = [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{size[0]}x{size[1]}", "-r", str(fps), "-i", "-", "-an", "-c:v", encoder, "-pix_fmt", "yuv420p", "-r", str(fps)]
    if encoder == "libx264":
        command += ["-preset", "medium", "-crf", "18"]
    else:
        command += ["-b:v", "8M" if size[0] * size[1] > 1_500_000 else "4M"]
    command += ["-movflags", "+faststart", str(silent)]
    bg_image = None
    bg_process = None
    bg_size = size[0] * size[1] * 3
    bg_file = Path(background_path) if background_path else None
    if bg_file and not bg_file.is_file():
        raise ValueError("Файл пользовательского фона не найден.")
    if bg_file and bg_file.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".bmp"}:
        bg_image = Image.open(bg_file).convert("RGB")
    try:
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
        if bg_file and bg_file.suffix.lower() in {".mp4", ".mov", ".webm", ".mkv", ".avi"}:
            # Keep FFmpeg filters to broadly supported scaling/cropping; Pillow
            # applies blur, dimming, brightness and contrast to the decoded frame.
            bg_command = [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-stream_loop", "-1", "-i", str(bg_file), "-an", "-vf", f"fps={fps},scale={size[0]}:{size[1]}:force_original_aspect_ratio=increase,crop={size[0]}:{size[1]}", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
            bg_process = subprocess.Popen(bg_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except Exception:
        if scratch:
            shutil.rmtree(scratch)
        raise
    written = 0
    try:
        elapsed = 0.0
        for scene_index, (group, seconds, word_times, word_end_times) in enumerate(timed_scenes):
            frames = math.ceil(seconds * fps)
            if word_times is None:
                scene_peaks = [peak - elapsed for peak in onsets if elapsed + delay + 0.1 <= peak <= elapsed + frames / fps - 0.3]
                word_times = [delay] + scene_peaks[:len(group)-1] if len(scene_peaks) >= len(group)-1 else None
            for index in range(frames):
                if cancel and cancel.is_set():
                    raise InterruptedError("Генерация остановлена.")
                bg_frame = bg_image
                if bg_process and bg_process.stdout:
                    raw_bg = bg_process.stdout.read(bg_size)
                    if len(raw_bg) != bg_size:
                        error = bg_process.stderr.read().decode("utf-8", "replace") if bg_process.stderr else ""
                        raise RuntimeError("Не удалось прочитать кадр пользовательского видеофона. " + error[-700:])
                    bg_frame = Image.frombytes("RGB", size, raw_bg)
                image = render_frame(group, size, style, animation, index / fps, frames / fps, speed,
                                     font_override, foreground, accent, 0 if track else delay,
                                     opacity, scale, position_x, position_y, word_times, word_end_times,
                                     background, background_intensity, background_speed, bg_frame,
                                     animation_intensity, easing, animation_in, animation_hold, animation_out, animation_duration,
                                     animation_speed,
                                     font_size, letter_spacing, line_spacing, text_align, bold, text_shadow, text_outline,
                                     background_brightness, background_contrast, background_tint)
                process.stdin.write(image.tobytes())
                written += 1
                if progress and (written % 5 == 0 or written == total_frames):
                    progress(written / total_frames)
            elapsed += frames / fps
        process.stdin.close()
        error = process.stderr.read().decode("utf-8", "replace")
        if process.wait() != 0:
            raise RuntimeError("Ошибка FFmpeg: " + error[-1600:])
        mux_audio(ffmpeg, silent, music, result, total_seconds, volume, fade,
                  voice=voice_path, voice_volume=voice_gain)
        return result
    except Exception:
        if process.poll() is None:
            process.kill()
            process.wait()
        result.unlink(missing_ok=True)
        raise
    finally:
        if process.stdin and not process.stdin.closed:
            process.stdin.close()
        if process.stderr and not process.stderr.closed:
            process.stderr.close()
        if bg_process:
            bg_process.kill()
            bg_process.wait()
            if bg_process.stdout:
                bg_process.stdout.close()
            if bg_process.stderr:
                bg_process.stderr.close()
        silent.unlink(missing_ok=True)
        if scratch:
            shutil.rmtree(scratch)
