from __future__ import annotations

import os
import unittest
from pathlib import Path
from threading import Event
from unittest.mock import patch
from uuid import uuid4

from batch_generator import generate_batch, parse_quotes
from config import find_ffmpeg

try:
    from PySide6.QtWidgets import QApplication
    from desktop import Studio
except ImportError:
    QApplication = None
    Studio = None


@unittest.skipIf(QApplication is None, "PySide6 не установлен")
class DesktopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        cls.app = QApplication.instance() or QApplication([])

    def test_window_and_options(self) -> None:
        window = Studio()
        self.assertEqual(window.windowTitle(), "AUTO SHORTS STUDIO")
        self.assertEqual(window.pages.count(), 7)
        self.assertEqual(window.ffmpeg_button.text(), "Указать FFmpeg…")
        self.assertEqual(window._options()["style"], "Reference Mode")
        self.assertEqual(window._options()["animation"], "Как в референсе")
        self.assertEqual(window._options()["format_name"], "Вертикальный 1080×1920")
        self.assertEqual(window._options()["fps"], 30)
        window.show_page(2)
        self.assertEqual(window.heading.text(), "Пакетная генерация")
        window.show_page(5)
        self.assertEqual(window.heading.text(), "Озвучка ElevenLabs")
        window.show_page(6)
        self.assertEqual(window.heading.text(), "Аудиозапись")
        self.assertEqual(window.audio_format.currentData(), "mp3")
        self.assertIn(window.audio_provider.currentData(), ("sapi", "elevenlabs"))
        self.assertEqual(window._audio_export_options()["provider"], window.audio_provider.currentData())
        self.assertTrue(window.voice_combo.count() >= 1)
        self.assertIsNotNone(window.elevenlabs_voice_combo)
        self.assertEqual(window.visual_preset.count(), 10)
        self.assertGreaterEqual(window.visual_font.count(), 15)
        self.assertEqual(window._options()["background"], "По стилю")
        window.background_tint.setText("#302040")
        self.assertEqual(window._options()["background_tint"], "#302040")
        window.background_tint.clear()
        window.apply_visual_preset("CINEMATIC")
        self.assertEqual(window._options()["background"], "Film Grain")
        self.assertEqual(window._options()["style"], "Cinematic Quotes")
        window._preview_visual_tick()
        self.assertFalse(window.anim_preview_label.pixmap().isNull())
        window.voice_enabled.setChecked(True)
        self.assertTrue(window._options()["narration"])
        window._smoke_mode = True
        window.close()

    def test_batch_preset_rotation_is_recorded_without_renderer_side_effects(self) -> None:
        test_output = Path(__file__).parent / f".tmp_{uuid4().hex[:10]}"
        test_output.mkdir()
        made = []
        def fake_render(_quote, **kwargs):
            path = test_output / f"{len(made)+1}.mp4"
            path.touch()
            made.append(kwargs)
            return path
        try:
            with patch("batch_generator.OUTPUT", test_output), patch("batch_generator.render_video", side_effect=fake_render):
                report, report_path = generate_batch(
                    ["Один", "Два"],
                    {"batch_style_mode": 2, "batch_presets": ["CLASSIC DARK", "CINEMATIC"],
                     "narration": False},
                )
                self.assertEqual([item["style"] for item in made], ["Reference Mode", "Cinematic Quotes"])
                self.assertEqual([row["visual_style"] for row in report], ["CLASSIC DARK", "CINEMATIC"])
                self.assertIn("visual_style", report_path.read_text(encoding="utf-8-sig"))
        finally:
            for child in test_output.iterdir(): child.unlink()
            test_output.rmdir()

    def test_batch_two_quotes_and_cancel(self) -> None:
        try:
            find_ffmpeg()
        except FileNotFoundError:
            self.skipTest("Для интеграционного рендера нужен установленный FFmpeg")
        quotes = parse_quotes("quotes.txt", "Первый [red]шаг[/red]\nВторой шаг")
        self.assertEqual(len(quotes), 2)
        test_output = Path(__file__).parent / f".tmp_{uuid4().hex[:10]}"
        test_output.mkdir()
        try:
            with patch("renderer.OUTPUT", test_output), patch("batch_generator.OUTPUT", test_output):
                options = {"preview": True, "style": "Reference Mode"}
                report, report_path = generate_batch(quotes, options)
                self.assertEqual([row["status"] for row in report], ["ok", "ok"])
                self.assertEqual([Path(row["file"]).name for row in report],
                                 ["ролик 1.mp4", "ролик 2.mp4"])
                self.assertTrue(all(Path(row["file"]).is_file() for row in report))
                self.assertTrue(report_path.is_file())
                cancel = Event()
                cancel.set()
                stopped, stopped_report = generate_batch(quotes, options, cancel=cancel)
                self.assertEqual(stopped, [])
                self.assertTrue(stopped_report.is_file())
        finally:
            for child in test_output.iterdir():
                if child.is_file():
                    child.unlink()
            test_output.rmdir()


if __name__ == "__main__":
    unittest.main()
