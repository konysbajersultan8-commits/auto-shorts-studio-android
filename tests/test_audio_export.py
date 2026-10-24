from __future__ import annotations

import subprocess
import tempfile
import unittest
import wave
from pathlib import Path
from threading import Event

from audio_export import export_audio, next_audio_path, split_phrases
from config import OUTPUT, find_ffmpeg
from narration import list_voices


class AudioExportTests(unittest.TestCase):
    def test_speech_text_is_split_and_markup_and_emoji_are_removed(self):
        self.assertEqual(split_phrases("💡 [red]Первый шаг.[/red] 🚀\nВторое предложение!"),
                         ["Первый шаг.", "Второе предложение!"])

    def test_audio_names_are_counted_across_wav_and_mp3(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder)
            (output / "Аудиозапись 1.mp3").touch()
            (output / "Аудиозапись 3.wav").touch()
            (output / "other 10.mp3").touch()
            self.assertEqual(next_audio_path(output, "mp3").name, "Аудиозапись 4.mp3")
            self.assertEqual(next_audio_path(output, "wav").name, "Аудиозапись 4.wav")

    def test_cancel_stops_audio_before_output_is_written(self):
        voices = [voice for voice in list_voices() if voice.russian]
        if not voices:
            self.skipTest("Русский Windows SAPI-голос не установлен")
        with tempfile.TemporaryDirectory(dir=OUTPUT) as folder:
            destination = Path(folder) / "cancelled.wav"
            cancel = Event()
            cancel.set()
            with self.assertRaises(InterruptedError):
                export_audio("Отмена озвучки.", destination, provider="sapi",
                             sapi_voice_id=voices[0].id, cancel=cancel)
            self.assertFalse(destination.exists())

    def test_sapi_speech_and_music_export_to_audio_only_wav(self):
        voices = [voice for voice in list_voices() if voice.russian]
        if not voices:
            self.skipTest("Русский Windows SAPI-голос не установлен")
        ffmpeg = find_ffmpeg()
        with tempfile.TemporaryDirectory(dir=OUTPUT) as folder:
            root = Path(folder)
            music = root / "test_bed.wav"
            subprocess.run([str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
                            "-f", "lavfi", "-i", "sine=frequency=220:duration=1.5",
                            "-ac", "2", "-c:a", "pcm_s16le", str(music)], check=True)
            destination = root / "Аудиозапись 1.wav"
            export_audio(
                "Не жди идеального момента. Его не будет. Есть только сейчас.", destination,
                provider="sapi", sapi_voice_id=voices[0].id, speed=0.82,
                pause=0.25, music=music, music_volume=0.12, duck_music=True,
            )
            self.assertTrue(destination.is_file())
            self.assertGreater(destination.stat().st_size, 1000)
            with wave.open(str(destination), "rb") as stream:
                self.assertGreater(stream.getnframes() / stream.getframerate(), 2.5)
                self.assertEqual(stream.getnchannels(), 2)
            probe = subprocess.run([str(ffmpeg), "-hide_banner", "-i", str(destination),
                                    "-f", "null", "-"], capture_output=True, text=True)
            self.assertIn("Audio:", probe.stderr)
            self.assertNotIn("Video:", probe.stderr)
            self.assertFalse(any(root.glob("*.mp4")), "Аудиорежим не должен создавать MP4")

            trimmed = root / "trim_music.wav"
            export_audio(
                "Не жди идеального момента. Его не будет. Есть только сейчас.", trimmed,
                provider="sapi", sapi_voice_id=voices[0].id, speed=0.82,
                pause=0.25, music=music, music_volume=0.12, duck_music=True,
                loop_music=False,
            )
            with wave.open(str(trimmed), "rb") as stream:
                self.assertGreater(stream.getnframes() / stream.getframerate(), 2.5,
                                   "Речь должна продолжаться после короткого трека")


if __name__ == "__main__":
    unittest.main()
