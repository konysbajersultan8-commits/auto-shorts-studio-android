"""Touch-friendly Android entry point. It reuses the existing local render/audio engines."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from threading import Event

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Qt, Signal, Slot
from PySide6.QtWidgets import (
    QApplication, QComboBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel,
    QLineEdit, QMainWindow, QMessageBox, QProgressBar, QPushButton, QSlider,
    QTabWidget, QTextEdit, QVBoxLayout, QWidget,
)

# The APK stores output/cache in its app-private files directory. Secrets are held
# only for the current process and are never written into the APK or preferences.
class JobSignals(QObject):
    progress = Signal(float, str)
    done = Signal(str)
    failed = Signal(str)


class VoiceSignals(QObject):
    ready = Signal(list)
    failed = Signal(str)


class VoiceListJob(QRunnable):
    def __init__(self, api_key: str, voice_id: str):
        super().__init__()
        self.api_key, self.voice_id = api_key, voice_id
        self.signals = VoiceSignals()

    @Slot()
    def run(self):
        try:
            from elevenlabs_tts import Credentials, list_account_voices
            voices = list_account_voices(Credentials(self.api_key, self.voice_id))
            self.signals.ready.emit(voices)
        except Exception as exc:
            self.signals.failed.emit(str(exc))


class RenderJob(QRunnable):
    def __init__(self, mode: str, args: dict):
        super().__init__()
        self.mode, self.args = mode, args
        self.cancel = Event()
        self.signals = JobSignals()

    @Slot()
    def run(self):
        try:
            from config import OUTPUT
            if self.mode == "audio":
                from audio_export import export_audio, next_audio_path
                out = next_audio_path(OUTPUT, self.args.pop("extension"))
                path = export_audio(**self.args, destination=out, cancel=self.cancel,
                                    progress=lambda p, m: self.signals.progress.emit(p, m))
            else:
                if self.mode == "batch":
                    from batch_generator import generate_batch
                    report, report_path = generate_batch(self.args["quotes"], self.args["options"],
                        progress=lambda p, m: self.signals.progress.emit(p, m), cancel=self.cancel)
                    succeeded = sum(row.get("status") == "ok" for row in report)
                    self.signals.done.emit(f"Пакет завершён: {succeeded}/{len(report)} роликов. CSV: {report_path}")
                    return
                from renderer import render_video, next_video_path
                out = next_video_path(OUTPUT)
                path = render_video(**self.args, progress=lambda p: self.signals.progress.emit(p, "Рендеринг видео"),
                                    cancel=self.cancel)
            self.signals.done.emit(str(path))
        except InterruptedError:
            self.signals.failed.emit("Обработка остановлена.")
        except Exception as exc:
            self.signals.failed.emit(str(exc))


class Studio(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("AUTO SHORTS STUDIO")
        self.resize(440, 860)
        self.pool = QThreadPool.globalInstance()
        self.job: RenderJob | None = None
        self.music: str | None = None
        self.setStyleSheet("""
            QWidget { background:#101216; color:#f2f4f8; font-size:16px; }
            QTabWidget::pane { border:1px solid #303744; border-radius:10px; }
            QTabBar::tab { background:#1c2028; padding:12px 15px; margin:2px; border-radius:7px; }
            QTabBar::tab:selected { background:#cf3d5b; }
            QLineEdit,QTextEdit,QComboBox { background:#171b22; border:1px solid #414957; border-radius:8px; padding:10px; }
            QPushButton { background:#cf3d5b; border:0; border-radius:9px; padding:13px; font-weight:600; }
            QPushButton:disabled { background:#48505d; }
            QProgressBar { background:#252b34; border:0; border-radius:6px; text-align:center; min-height:20px; }
            QProgressBar::chunk { background:#cf3d5b; border-radius:6px; }
        """)
        root = QWidget(); layout = QVBoxLayout(root)
        header = QLabel("AUTO SHORTS STUDIO\nВидео и аудио на телефоне")
        header.setStyleSheet("font-size:21px;font-weight:700;padding:8px")
        layout.addWidget(header)
        self.tabs = QTabWidget(); layout.addWidget(self.tabs)
        self.text = QTextEdit(); self.text.setPlaceholderText("Введите текст. Акценты отмечайте тегами [red]...[/red]")
        self.audio_text = QTextEdit(); self.audio_text.setPlaceholderText("Текст для аудиозаписи")
        video = QWidget(); vl = QVBoxLayout(video)
        vl.addWidget(QLabel("Текст для ролика")); vl.addWidget(self.text, 1)
        self.style = QComboBox(); self.style.addItems(["Reference Mode", "Classic Typography", "Dark Motivation", "Cinematic Quotes", "Minimalist Philosophy"])
        self.animation = QComboBox(); self.animation.addItems(["Как в референсе", "По словам", "Плавное появление", "Плавное увеличение"])
        self.format = QComboBox(); self.format.addItems(["Вертикальный 1080×1920", "Квадратный 1080×1080", "Горизонтальный 1920×1080"])
        self.narration = QComboBox(); self.narration.addItems(["Только текст", "Озвучить через ElevenLabs"])
        self.music_button = QPushButton("Выбрать фоновую музыку")
        self.music_button.clicked.connect(self.choose_music)
        self.video_button = QPushButton("Создать MP4")
        self.video_button.clicked.connect(self.create_video)
        form = QFormLayout(); form.addRow("Стиль", self.style); form.addRow("Анимация", self.animation)
        form.addRow("Формат кадра", self.format); form.addRow("Звук", self.narration)
        vl.addLayout(form); vl.addWidget(self.music_button); vl.addWidget(self.video_button)
        self.tabs.addTab(video, "Видео")

        audio = QWidget(); al = QVBoxLayout(audio)
        al.addWidget(QLabel("Отдельная аудиозапись — без видеорендеринга")); al.addWidget(QLabel("Текст озвучки")); al.addWidget(self.audio_text, 1)
        self.voice_id = QComboBox(); self.voice_id.setEditable(True); self.voice_id.setPlaceholderText("Voice ID ElevenLabs")
        self.api_key = QLineEdit(); self.api_key.setPlaceholderText("API ключ (только в памяти до закрытия приложения)"); self.api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.load_voices_button = QPushButton("Загрузить голоса ElevenLabs")
        self.load_voices_button.clicked.connect(self.load_voices)
        self.speed = QSlider(Qt.Orientation.Horizontal); self.speed.setRange(60, 140); self.speed.setValue(82)
        self.speed_label = QLabel("Скорость речи: 0.82×")
        self.speed.valueChanged.connect(lambda value: self.speed_label.setText(f"Скорость речи: {value / 100:.2f}×"))
        self.voice_volume = QSlider(Qt.Orientation.Horizontal); self.voice_volume.setRange(0, 150); self.voice_volume.setValue(100)
        self.reverb = QSlider(Qt.Orientation.Horizontal); self.reverb.setRange(0, 35); self.reverb.setValue(8)
        self.pause = QSlider(Qt.Orientation.Horizontal); self.pause.setRange(0, 200); self.pause.setValue(35)
        self.music_volume = QSlider(Qt.Orientation.Horizontal); self.music_volume.setRange(0, 100); self.music_volume.setValue(18)
        self.audio_music = QPushButton("Выбрать музыку")
        self.audio_music.clicked.connect(self.choose_music)
        self.extension = QComboBox(); self.extension.addItems(["mp3", "wav"])
        af = QFormLayout(); af.addRow("Voice ID", self.voice_id); af.addRow("API ключ", self.api_key)
        af.addRow(self.load_voices_button)
        af.addRow(self.speed_label, self.speed); af.addRow("Экспорт", self.extension)
        af.addRow("Громкость голоса", self.voice_volume); af.addRow("Глубина / реверберация", self.reverb)
        af.addRow("Пауза между фразами", self.pause); af.addRow("Громкость музыки", self.music_volume)
        al.addLayout(af); al.addWidget(self.audio_music)
        self.audio_button = QPushButton("Создать аудиозапись")
        self.audio_button.clicked.connect(self.create_audio); al.addWidget(self.audio_button)
        self.tabs.addTab(audio, "Аудио")

        batch = QWidget(); bl = QVBoxLayout(batch)
        bl.addWidget(QLabel("По одной цитате на строку (TXT, CSV или JSON)"))
        self.batch_text = QTextEdit(); self.batch_text.setPlaceholderText("Цитата первая\nЦитата вторая")
        bl.addWidget(self.batch_text, 1)
        self.batch_file_button = QPushButton("Открыть файл цитат")
        self.batch_file_button.clicked.connect(self.load_batch_file); bl.addWidget(self.batch_file_button)
        self.batch_button = QPushButton("Создать пакет видео")
        self.batch_button.clicked.connect(self.create_batch); bl.addWidget(self.batch_button)
        self.tabs.addTab(batch, "Пакет")

        settings = QWidget(); sl = QVBoxLayout(settings)
        sl.addWidget(QLabel("Файлы хранятся в личной папке приложения. Для синтеза нужен ваш API ключ ElevenLabs.\n\nГолос по фотографии не определяется. Для видео доступны локальные фоны/стили, эмодзи, [red]-акценты и анимации общего движка.\n\nВ Android не работают Windows SAPI и системные пути Windows. Для рендеринга в APK требуется встроенный Android FFmpeg."))
        self.tabs.addTab(settings, "Настройки")

        self.status = QLabel("Готово")
        self.progress = QProgressBar(); self.progress.setValue(0)
        self.cancel_button = QPushButton("Отменить"); self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel_job)
        layout.addWidget(self.status); layout.addWidget(self.progress); layout.addWidget(self.cancel_button)
        self.setCentralWidget(root)

    def choose_music(self):
        path, _ = QFileDialog.getOpenFileName(self, "Выберите музыку", "", "Аудио (*.mp3 *.wav)")
        if path:
            self.music = path
            self.music_button.setText(Path(path).name)
            self.audio_music.setText(Path(path).name)

    def _credentials(self):
        key = self.api_key.text().strip()
        voice = self.voice_id.currentData() or self.voice_id.currentText().strip()
        if not key or not voice:
            raise ValueError("Для ElevenLabs укажите API ключ и Voice ID в разделе «Аудио».")
        os.environ["ELEVENLABS_API_KEY"] = key
        os.environ["ELEVENLABS_VOICE_ID"] = voice
        return voice

    def create_audio(self):
        text = self.audio_text.toPlainText().strip()
        if not text:
            QMessageBox.warning(self, "Нет текста", "Введите текст для аудиозаписи."); return
        try:
            voice = self._credentials()
        except ValueError as exc:
            QMessageBox.warning(self, "Нужен ElevenLabs", str(exc)); return
        self._start("audio", dict(text=text, provider="elevenlabs", voice_id=voice,
                                   options={"model_id": "eleven_multilingual_v2", "speed": self.speed.value() / 100},
                                   speed=self.speed.value() / 100, music=Path(self.music) if self.music else None,
                                   voice_volume=self.voice_volume.value(), reverb=self.reverb.value() / 100,
                                   pause=self.pause.value() / 100, music_volume=self.music_volume.value() / 100,
                                   extension=self.extension.currentText()))

    def create_video(self):
        text = self.text.toPlainText().strip()
        if not text:
            QMessageBox.warning(self, "Нет текста", "Введите текст для видео."); return
        narration = self.narration.currentIndex() == 1
        try:
            voice = self._credentials() if narration else ""
        except ValueError as exc:
            QMessageBox.warning(self, "Нужен ElevenLabs", str(exc)); return
        self._start("video", dict(text=text, style=self.style.currentText(), animation=self.animation.currentText(),
                    format_name=self.format.currentText(), music=Path(self.music) if self.music else None,
                    narration=narration, tts_provider="elevenlabs", voice_id=voice,
                    background="По стилю"))

    def load_voices(self):
        key, voice = self.api_key.text().strip(), self.voice_id.currentText().strip()
        if not key:
            QMessageBox.warning(self, "Нужен ключ", "Введите API ключ ElevenLabs."); return
        self.load_voices_button.setEnabled(False)
        worker = VoiceListJob(key, voice)
        worker.signals.ready.connect(self.on_voices_loaded)
        worker.signals.failed.connect(self.on_voices_error)
        self.pool.start(worker)

    @Slot(list)
    def on_voices_loaded(self, voices: list):
        self.load_voices_button.setEnabled(True)
        selected = self.voice_id.currentData() or self.voice_id.currentText()
        self.voice_id.clear()
        for voice in voices:
            self.voice_id.addItem(voice.get("name", "ElevenLabs voice"), voice.get("id", ""))
        index = next((i for i in range(self.voice_id.count()) if self.voice_id.itemData(i) == selected), -1)
        if index >= 0: self.voice_id.setCurrentIndex(index)
        elif selected: self.voice_id.setEditText(selected)
        if not voices: QMessageBox.information(self, "Голоса", "Аккаунт не вернул голосов.")

    @Slot(str)
    def on_voices_error(self, message: str):
        self.load_voices_button.setEnabled(True)
        QMessageBox.warning(self, "ElevenLabs", message)

    def load_batch_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "Файл цитат", "", "Цитаты (*.txt *.csv *.json)")
        if not path: return
        try:
            from batch_generator import parse_quotes
            quotes = parse_quotes(path, Path(path).read_text(encoding="utf-8-sig"))
            self.batch_text.setPlainText("\n".join(quotes))
        except Exception as exc:
            QMessageBox.warning(self, "Файл цитат", str(exc))

    def create_batch(self):
        raw = self.batch_text.toPlainText().strip()
        if not raw:
            QMessageBox.warning(self, "Нет цитат", "Введите несколько цитат или откройте файл."); return
        try:
            from batch_generator import parse_quotes
            quotes = parse_quotes("quotes.txt", raw)
            narration = self.narration.currentIndex() == 1
            voice = self._credentials() if narration else ""
        except Exception as exc:
            QMessageBox.warning(self, "Пакет", str(exc)); return
        options = dict(style=self.style.currentText(), animation=self.animation.currentText(),
            format_name=self.format.currentText(), music=Path(self.music) if self.music else None,
            narration=narration, tts_provider="elevenlabs", voice_id=voice, background="По стилю")
        self._start("batch", {"quotes": quotes, "options": options})

    def _start(self, mode: str, args: dict):
        if self.job and self.pool.activeThreadCount():
            QMessageBox.information(self, "Обработка", "Дождитесь завершения текущей задачи."); return
        self.job = RenderJob(mode, args)
        self.job.signals.progress.connect(self.on_progress)
        self.job.signals.done.connect(self.on_done)
        self.job.signals.failed.connect(self.on_failed)
        self.progress.setValue(0); self.status.setText("Запуск…")
        self.cancel_button.setEnabled(True)
        self.pool.start(self.job)

    @Slot(float, str)
    def on_progress(self, value: float, message: str):
        self.progress.setValue(max(0, min(100, round(value * 100)))); self.status.setText(message)

    @Slot(str)
    def on_done(self, path: str):
        self.cancel_button.setEnabled(False); self.progress.setValue(100)
        self.status.setText(f"Сохранено: {path}")
        QMessageBox.information(self, "Готово", f"Файл сохранён в личной папке приложения:\n{path}")

    @Slot(str)
    def on_failed(self, message: str):
        self.cancel_button.setEnabled(False); self.status.setText(message)
        QMessageBox.warning(self, "Ошибка", message)

    def cancel_job(self):
        if self.job:
            self.job.cancel.set()
            self.status.setText("Остановка после безопасной точки…")


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("AutoShortsStudio")
    app.setOrganizationName("AutoShortsStudio")
    window = Studio(); window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
