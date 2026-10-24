from __future__ import annotations

import unittest
import wave
from pathlib import Path
from tempfile import TemporaryDirectory

from config import OUTPUT, find_ffmpeg
from narration import list_voices, phrase_groups, prepare_narration


class NarrationTests(unittest.TestCase):
    def test_russian_sapi_and_phrase_timeline(self) -> None:
        try:
            find_ffmpeg()
        except FileNotFoundError:
            self.skipTest("Для обработки и объединения озвучки нужен установленный FFmpeg")
        text = "Пока ты ищешь мотивацию, кто-то уже ищет клиентов"
        groups = phrase_groups(text)
        self.assertEqual(len(groups), 2)
        self.assertEqual(" ".join(word.text for group in groups for word in group), text.upper())
        voices = [voice for voice in list_voices() if voice.russian]
        if not voices:
            self.skipTest("Русский SAPI-голос отсутствует на этой машине")
        with TemporaryDirectory(dir=OUTPUT) as folder:
            track = prepare_narration(text, voices[0].id, Path(folder))
            self.assertEqual(len(track.segments), 2)
            self.assertTrue(all(segment.speech_seconds > 1 for segment in track.segments))
            self.assertAlmostEqual(track.segments[1].start, track.segments[0].scene_seconds, places=3)
            with wave.open(str(track.path), "rb") as audio:
                self.assertAlmostEqual(audio.getnframes() / audio.getframerate(), track.seconds, places=3)
            self.assertGreaterEqual(track.seconds, track.segments[-1].start + track.segments[-1].speech_seconds + 0.25)


if __name__ == "__main__":
    unittest.main()
