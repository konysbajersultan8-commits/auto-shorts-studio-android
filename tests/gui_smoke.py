r"""Run manually: .venv\Scripts\python.exe tests\gui_smoke.py"""
from __future__ import annotations

import os
import math
import struct
import sys
import wave
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "windows")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication

from desktop import Studio
from config import MUSIC


def await_worker(app: QApplication, window: Studio, seconds: int = 90) -> None:
    worker = window.current_thread
    assert worker is not None
    loop = QEventLoop()
    worker.finished.connect(loop.quit)
    QTimer.singleShot(seconds * 1000, loop.quit)
    loop.exec()
    app.processEvents()
    if window.current_thread is not None:
        raise TimeoutError(f"GUI task did not finish: {window.single_status.text()} / {window.single_progress.value()}%")


def main() -> None:
    app = QApplication([])
    QFontDatabase.addApplicationFont("C:/Windows/Fonts/arial.ttf")
    app.setFont(QFont("Arial", 10))
    window = Studio()
    window.show()
    app.processEvents()
    received_frames: list[bool] = []
    window.video.videoSink().videoFrameChanged.connect(lambda frame: received_frames.append(frame.isValid()))
    screenshot = Path(__file__).resolve().parents[1] / "desktop_window.png"
    assert window.grab().save(str(screenshot))
    window.quick_preview.setChecked(True)
    window.generate_button.click()
    await_worker(app, window)
    assert window.video_path and window.video_path.is_file(), window.single_status.text()
    if window.player.duration() <= 0:
        loop = QEventLoop()
        window.player.durationChanged.connect(loop.quit)
        QTimer.singleShot(10000, loop.quit)
        loop.exec()
    assert window.player.duration() > 1000
    if not any(received_frames):
        loop = QEventLoop()
        QTimer.singleShot(2500, loop.quit)
        loop.exec()
    print("player status:", window.player.mediaStatus(), "state:", window.player.playbackState(), "error:", window.player.errorString(), "hasVideo:", window.player.hasVideo(), "position:", window.player.position())
    assert window.player.hasVideo(), "Qt player did not detect the video track"
    assert window.player.position() > 0, "Qt player did not advance playback"
    print("single:", window.video_path.name, "duration_ms:", window.player.duration(), "frames:", sum(received_frames))
    window.player.stop()

    tone = Path(__file__).parent / "smoke_tone.wav"
    with wave.open(str(tone), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(22050)
        stream.writeframes(b"".join(struct.pack("<h", int(5000 * math.sin(2 * math.pi * 440 * i / 22050))) for i in range(22050)))
    with patch("desktop.QFileDialog.getOpenFileName", return_value=(str(tone), "")):
        window.add_music()
    copied_music = Path(window.music_combo.currentData())
    assert copied_music.is_file() and copied_music.parent == MUSIC
    window.show_page(0)
    window.generate_button.click()
    await_worker(app, window)
    assert window.video_path and window.video_path.is_file()
    if window.player.duration() <= 0:
        loop = QEventLoop()
        window.player.durationChanged.connect(loop.quit)
        QTimer.singleShot(10000, loop.quit)
        loop.exec()
    assert window.player.hasAudio(), "Qt player did not detect the AAC track"
    print("audio:", window.video_path.name, "hasAudio:", window.player.hasAudio())
    window.player.stop()
    tone.unlink()
    copied_music.unlink()
    window.refresh_music()

    window.batch_quotes = ["Первый [red]шаг[/red]", "Второй шаг"]
    window.batch_start_button.click()
    await_worker(app, window)
    assert window.batch_files.count() == 2, window.batch_status.text()
    assert window.report_path and window.report_path.is_file()
    print("batch:", window.batch_files.count(), "report:", window.report_path.name)

    window.batch_quotes = ["Длинный важный текст про движение и действие"] * 20
    window.batch_start_button.click()
    assert window.current_thread is not None
    window.batch_stop_button.click()
    await_worker(app, window)
    assert "Остановлено" in window.batch_status.text()
    print("stop:", window.batch_status.text())
    window.close()
    print("screenshot:", screenshot)


if __name__ == "__main__":
    main()
