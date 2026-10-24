import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageChops

from emoji_support import emoji_tile, grapheme_clusters, strip_emojis
from renderer import (_display_groups_for_speech, map_speech_timing_to_display,
                      next_video_path, render_frame)
from text_processor import apply_auto_accent, parse_words, scenes
from visual_engine import ANIMATIONS, BACKGROUNDS, PRESETS, SYSTEM_FONTS, available_fonts, render_background


class TextTests(unittest.TestCase):
    def test_reference_words_remain_visible_after_each_word_is_spoken(self):
        words = parse_words("Первое второе")
        with patch("renderer._draw_spaced") as draw:
            render_frame(words, (640, 360), "Reference Mode", "Как в референсе", .55, 1.0, 1.0,
                         word_times=[0.0, .2], word_end_times=[.1, .3])
        self.assertEqual(draw.call_count, 2)

    def test_emoji_clusters_and_speech_text_are_separate(self):
        self.assertEqual(grapheme_clusters("👩🏽‍💻"), ["👩🏽‍💻"])
        self.assertEqual(grapheme_clusters("🇰🇿 1️⃣"), ["🇰🇿", " ", "1️⃣"])
        self.assertEqual(strip_emojis("💸 [red]ДЕНЬГИ[/red] 🎓"), "[red]ДЕНЬГИ[/red]")
        self.assertLess(emoji_tile("👩🏽‍💻", 72)[3], emoji_tile("💻", 72)[3] * 1.5)
        self.assertLess(emoji_tile("👨‍👩‍👧‍👦", 72)[3], emoji_tile("😀", 72)[3] * 1.5)

    def test_display_emojis_follow_speech_phrase_without_entering_sapi_words(self):
        display = scenes("💸 [red]Money changes.[/red]\n\n🚀 Next step.")
        phrases = _display_groups_for_speech(display, [2, 2])
        self.assertEqual([[word.text for word in phrase] for phrase in phrases],
                         [["💸", "MONEY", "CHANGES."], ["🚀", "NEXT", "STEP."]])

    def test_color_emoji_render_in_both_aspect_ratios_and_sync_mapping(self):
        text = "💸 [red]ДЕНЬГИ МЕНЯЮТ ВСЁ.[/red]"
        groups = scenes(text)
        mapped_starts, mapped_ends = map_speech_timing_to_display(
            groups, [0.0, 0.4, 0.8], [0.35, 0.75, 1.2])
        self.assertEqual(len(mapped_starts), 4)
        self.assertEqual(mapped_starts[0], mapped_starts[1])
        self.assertEqual(mapped_ends[0], mapped_ends[1])
        for size in ((288, 512), (512, 288)):
            with self.subTest(size=size):
                frame = render_frame(groups[0], size, "Reference Mode", "Плавное появление", 1, 2, 1)
                pixels = frame.tobytes()
                colorful = sum(1 for index in range(0, len(pixels), 3)
                               for red, green, blue in [pixels[index:index + 3]]
                               if max(red, green, blue) - min(red, green, blue) > 35
                               and not (red > 150 and red > green * 1.5 and red > blue * 1.5))
                self.assertGreater(colorful, 40)

    def test_sequential_video_names_skip_existing_numbers(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            output = Path(directory)
            (output / "ролик 1.mp4").touch()
            (output / "ролик 3.mp4").touch()
            self.assertEqual(next_video_path(output).name, "ролик 4.mp4")

    def test_manual_accent_and_kazakh(self):
        words = parse_words("Өмір [red]алға[/red] бастайды")
        self.assertEqual([w.text for w in words], ["ӨМІР", "АЛҒА", "БАСТАЙДЫ"])
        self.assertEqual([w.accent for w in words], [False, True, False])

    def test_partial_red_tag_does_not_split_a_word_for_timing(self):
        text = "Пока ты ищешь мотива[red]цию[/red], кто-то уже ищет клиентов"
        words = [word for group in scenes(text) for word in group]
        self.assertEqual([word.text for word in words],
                         ["ПОКА", "ТЫ", "ИЩЕШЬ", "МОТИВАЦИЮ,", "КТО-ТО", "УЖЕ", "ИЩЕТ", "КЛИЕНТОВ"])
        self.assertEqual(sum(word.accent for word in words), 1)

    def test_sentence_split_and_auto_accent(self):
        groups = scenes("Первый шаг. Второй важный шаг!")
        self.assertEqual(len(groups), 2)
        self.assertTrue(any(w.accent for w in apply_auto_accent(groups[1])))

    def test_all_animation_modes_render(self):
        words = parse_words("Тот [red]проиграл[/red]")
        modes = ("Как в референсе", "Плавное появление", "По словам", "Посимвольно", "Плавное увеличение", "Последовательное раскрытие", *ANIMATIONS)
        baseline = render_frame(words, (512, 288), "Reference Mode", "Плавное появление", .2, 2, 1,
                                background="Pure Black", animation_in="None")
        for mode in modes:
            with self.subTest(mode=mode):
                frame = render_frame(words, (512, 288), "Reference Mode", mode, .2, 2, 1,
                                     background="Pure Black", animation_in="None")
                self.assertIsInstance(frame, Image.Image)
                self.assertIsNotNone(frame.convert("L").getbbox())
                if mode in ANIMATIONS:
                    difference = ImageChops.difference(baseline, frame).convert("L")
                    self.assertIsNotNone(difference.getbbox(), f"{mode} не изменил кадр")

    def test_background_library_presets_and_fonts(self):
        words = parse_words("Сила в ясности")
        for background in BACKGROUNDS:
            with self.subTest(background=background):
                frame = render_background((256, 144), background, .4, .25, 1)
                self.assertEqual(frame.size, (256, 144))
                if background not in {"По стилю", "Pure Black"}:
                    self.assertGreater(ImageChops.difference(frame, Image.new("RGB", frame.size, "#000000")).convert("L").getbbox() is not None, 0)
        self.assertEqual(len(ANIMATIONS), 20)
        self.assertEqual(len(PRESETS), 10)
        self.assertGreaterEqual(len(available_fonts()), 15)
        self.assertGreaterEqual(len(SYSTEM_FONTS), 15)

    def test_background_tint_changes_background_pixels(self):
        original = render_background((48, 32), "Pure Black", intensity=.5)
        tinted = render_background((48, 32), "Pure Black", intensity=.5, tint="#CC3355")
        self.assertIsNotNone(ImageChops.difference(original, tinted).getbbox())


if __name__ == "__main__":
    unittest.main()
