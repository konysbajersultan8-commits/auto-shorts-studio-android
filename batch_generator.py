from __future__ import annotations

import csv
import io
import json
import random
from pathlib import Path
from threading import Event
from typing import Callable

from config import MUSIC, OUTPUT
from renderer import render_video
from visual_engine import PRESETS, available_fonts


def parse_quotes(filename: str, content: str) -> list[str]:
    suffix = Path(filename).suffix.lower()
    if suffix == ".txt":
        quotes = [line.strip() for line in content.splitlines() if line.strip()]
    elif suffix == ".csv":
        rows = list(csv.reader(io.StringIO(content)))
        if rows and rows[0] and rows[0][0].strip().lower() in {"quote", "text", "цитата", "текст"}:
            rows = rows[1:]
        quotes = [row[0].strip() for row in rows if row and row[0].strip()]
    elif suffix == ".json":
        data = json.loads(content)
        if not isinstance(data, list):
            raise ValueError("JSON должен содержать список цитат.")
        quotes = [item.strip() if isinstance(item, str) else str(item.get("text", item.get("quote", ""))).strip() for item in data if isinstance(item, (str, dict))]
        quotes = [item for item in quotes if item]
    else:
        raise ValueError("Поддерживаются файлы TXT, CSV и JSON.")
    if not quotes:
        raise ValueError("В файле нет цитат.")
    if len(quotes) > 500:
        raise ValueError("За один запуск можно обработать не более 500 цитат.")
    return quotes


def generate_batch(quotes: list[str], options: dict, *, random_music: bool = False, progress: Callable[[float, str], None] | None = None, cancel: Event | None = None) -> tuple[list[dict], Path]:
    choices = sorted(p for p in MUSIC.iterdir() if p.suffix.lower() in {".mp3", ".wav"})
    report: list[dict] = []
    for index, quote in enumerate(quotes):
        if cancel and cancel.is_set():
            break
        row = {"index": index + 1, "quote": quote, "file": "", "visual_style": "", "status": "", "error": ""}
        try:
            music = random.choice(choices) if random_music and choices else options.get("music")
            kwargs = dict(options)
            mode = int(kwargs.pop("batch_style_mode", 0))
            selected = kwargs.pop("batch_presets", [])
            style_name = ""
            if mode in (1, 2) and selected:
                style_name = random.choice(selected) if mode == 1 else selected[index % len(selected)]
                visual = PRESETS[style_name]
                kwargs.update(style=visual["style"], animation=visual["animation"],
                              background=visual["background"], foreground=visual["foreground"],
                              accent=visual["accent"], background_path="")
                font = available_fonts().get(visual.get("font", ""))
                if visual.get("font_file"):
                    font = visual["font_file"]
                kwargs.update(
                    font_override=font or kwargs.get("font_override", ""),
                    font_size=visual.get("font_size", kwargs.get("font_size", 1)),
                    bold=visual.get("bold", kwargs.get("bold", False)),
                    letter_spacing=visual.get("letter_spacing", kwargs.get("letter_spacing", .035)),
                    line_spacing=visual.get("line_spacing", kwargs.get("line_spacing", 1.75)),
                    text_align=visual.get("text_align", kwargs.get("text_align", "center")),
                    text_shadow=visual.get("text_shadow", kwargs.get("text_shadow", .3)),
                    text_outline=visual.get("text_outline", kwargs.get("text_outline", 0)),
                    background_intensity=visual.get("background_intensity", kwargs.get("background_intensity", .25)),
                    background_speed=visual.get("background_speed", kwargs.get("background_speed", 1)),
                    background_brightness=visual.get("background_brightness", kwargs.get("background_brightness", 0)),
                    background_contrast=visual.get("background_contrast", kwargs.get("background_contrast", 1)),
                    background_tint=visual.get("background_tint", kwargs.get("background_tint", "")),
                    animation_intensity=visual.get("animation_intensity", kwargs.get("animation_intensity", 1)),
                    animation_duration=visual.get("animation_duration", kwargs.get("animation_duration", .35)),
                    animation_speed=visual.get("animation_speed", kwargs.get("animation_speed", 1)),
                    animation_in=visual.get("animation_in", "Fade In"),
                    animation_hold=visual.get("animation_hold", "None"),
                    animation_out=visual.get("animation_out", "Fade Out"),
                    easing=visual.get("easing", "Smooth"))
            row["visual_style"] = style_name or str(kwargs.get("style", ""))
            kwargs["music"] = music
            path = render_video(quote, **kwargs, cancel=cancel, progress=(lambda fraction, i=index: progress((i + fraction) / len(quotes), f"Видео {i + 1}/{len(quotes)}") if progress else None))
            row["file"] = str(path)
            row["status"] = "ok"
        except InterruptedError:
            row["status"] = "stopped"
            report.append(row)
            break
        except Exception as exc:
            row["status"] = "error"
            row["error"] = str(exc)
        report.append(row)
        if progress:
            progress((index + 1) / len(quotes), f"Обработано {index + 1}/{len(quotes)}")
    report_path = OUTPUT / f"batch_report_{__import__('uuid').uuid4().hex[:12]}.csv"
    with report_path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["index", "quote", "file", "visual_style", "status", "error"])
        writer.writeheader()
        writer.writerows(report)
    return report, report_path
