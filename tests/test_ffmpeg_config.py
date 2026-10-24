from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config


class FfmpegConfigTests(unittest.TestCase):
    def tearDown(self) -> None:
        config.find_ffmpeg.cache_clear()
        config.find_encoder.cache_clear()

    def test_selected_ffmpeg_path_is_saved_and_reused(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root = Path(directory)
            appdata = root / "Roaming"
            ffmpeg = root / "tools" / "ffmpeg.exe"
            ffmpeg.parent.mkdir()
            ffmpeg.touch()
            with patch.dict(os.environ, {"APPDATA": str(appdata), "AUTO_SHORTS_FFMPEG": ""}), \
                    patch("config.shutil.which", return_value=None):
                saved = config.save_ffmpeg_path(ffmpeg)
                self.assertEqual(saved, ffmpeg.resolve())
                self.assertEqual(config.find_ffmpeg(), ffmpeg.resolve())
                self.assertEqual(config.ffmpeg_settings_path().read_text(encoding="utf-8"), str(ffmpeg.resolve()))


if __name__ == "__main__":
    unittest.main()
