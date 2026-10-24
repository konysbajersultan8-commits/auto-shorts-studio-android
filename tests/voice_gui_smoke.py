"""Native Windows GUI smoke check for SAPI preview and voiced MP4 generation."""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "windows")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication

from desktop import Studio


def wait_for(thread, app: QApplication, timeout_ms: int = 60000) -> None:
    loop = QEventLoop()
    done = {"value": False}
    thread.finished.connect(lambda: done.__setitem__("value", True))
    thread.finished.connect(loop.quit)
    QTimer.singleShot(timeout_ms, loop.quit)
    loop.exec()
    app.processEvents()
    if not done["value"]:
        raise TimeoutError("Ожидание фоновой генерации превысило лимит.")


def main() -> None:
    app = QApplication([])
    window = Studio()
    window.show()
    window.show_page(4)
    app.processEvents()
    assert window.voice_combo.count() >= 1
    assert window.voice_combo.currentText().endswith("Русский")
    window.voice_test_button.click()
    assert window.voice_preview_thread is not None
    wait_for(window.voice_preview_thread, app)
    assert window.voice_preview_path and window.voice_preview_path.is_file(), window.voice_status.text()
    loop = QEventLoop()
    window.voice_player.durationChanged.connect(loop.quit)
    QTimer.singleShot(3000, loop.quit)
    loop.exec()
    assert window.voice_player.hasAudio()
    print("voice preview:", window.voice_player.duration(), "ms")
    window.voice_player.stop()
    window.voice_enabled.setChecked(True)
    window.quote.setPlainText("Пока ты ищешь мотивацию, кто-то уже ищет клиентов")
    window.quick_preview.setChecked(True)
    window.generate_button.click()
    assert window.current_thread is not None
    wait_for(window.current_thread, app)
    assert window.video_path and window.video_path.is_file(), window.single_status.text()
    loop = QEventLoop()
    window.player.durationChanged.connect(loop.quit)
    QTimer.singleShot(3000, loop.quit)
    loop.exec()
    assert window.player.hasAudio() and window.player.hasVideo()
    assert window.player.duration() >= 4500
    print("voiced MP4:", window.video_path, window.player.duration(), "ms")
    window.close()


if __name__ == "__main__":
    main()
