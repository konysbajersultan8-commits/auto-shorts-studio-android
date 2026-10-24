from __future__ import annotations

import shutil
import json
import sys
import tempfile
import time
import uuid
from pathlib import Path
from threading import Event

from PySide6.QtCore import Qt, QThread, QUrl, Signal, QSettings, QTimer
from PySide6.QtGui import QColor, QDesktopServices, QFont, QFontDatabase, QImage, QPixmap
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QColorDialog, QComboBox, QDoubleSpinBox, QFileDialog,
    QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget, QMainWindow,
    QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QScrollArea, QSlider,
    QSpinBox, QStackedWidget, QVBoxLayout, QWidget, QListWidgetItem, QInputDialog,
)

from batch_generator import generate_batch, parse_quotes
from audio_export import export_audio, next_audio_path
from config import ANIMATIONS, FORMATS, MUSIC, OUTPUT, STYLES, find_encoder, find_ffmpeg, save_ffmpeg_path
from narration import PRESETS, RUSSIAN_VOICE_HELP, list_voices, prepare_narration
from elevenlabs_tts import DEFAULT_MODEL, MODELS, estimate_characters, has_cached_audio, list_account_voices, load_credentials, probe_connection, synthesize
from renderer import render_frame, render_video
from emoji_support import initialize_emoji_font
from visual_engine import ANIMATIONS as VISUAL_ANIMATIONS, BACKGROUNDS, PRESETS as VISUAL_PRESETS, available_fonts, render_background
from text_processor import scenes


class RenderThread(QThread):
    progress = Signal(float, str)
    completed = Signal(object)

    def __init__(self, *, text: str = "", quotes: list[str] | None = None, options: dict, random_music: bool = False):
        super().__init__()
        self.text = text
        self.quotes = quotes
        self.options = options
        self.random_music = random_music
        self.cancel_event = Event()

    def run(self) -> None:
        try:
            if self.quotes is None:
                path = render_video(self.text, **self.options, cancel=self.cancel_event,
                                    progress=lambda value: self.progress.emit(value, "Рендеринг видео"))
                self.completed.emit({"state": "done", "files": [path], "report": None, "errors": []})
            else:
                report, report_path = generate_batch(
                    self.quotes, self.options, random_music=self.random_music,
                    cancel=self.cancel_event, progress=lambda value, message: self.progress.emit(value, message),
                )
                self.completed.emit({
                    "state": "stopped" if self.cancel_event.is_set() else "done",
                    "files": [Path(row["file"]) for row in report if row["status"] == "ok"],
                    "report": report_path,
                    "errors": [f"#{row['index']}: {row['error']}" for row in report if row["status"] == "error"],
                })
        except InterruptedError:
            self.completed.emit({"state": "stopped", "files": [], "report": None, "errors": []})
        except Exception as exc:
            self.completed.emit({"state": "error", "files": [], "report": None, "errors": [str(exc)]})


class AudioExportThread(QThread):
    progress = Signal(float, str)
    completed = Signal(object)

    def __init__(self, destination: Path, options: dict):
        super().__init__()
        self.destination = destination
        self.options = options
        self.cancel_event = Event()

    def run(self) -> None:
        try:
            path = export_audio(**self.options, destination=self.destination,
                                cancel=self.cancel_event,
                                progress=lambda value, message: self.progress.emit(value, message))
            self.completed.emit({"state": "done", "path": path})
        except InterruptedError as exc:
            self.destination.unlink(missing_ok=True)
            self.completed.emit({"state": "stopped", "message": str(exc)})
        except Exception as exc:
            self.destination.unlink(missing_ok=True)
            self.completed.emit({"state": "error", "message": str(exc)})


class VoicePreviewThread(QThread):
    completed = Signal(object)

    def __init__(self, voice_id: str, speed: float, volume: int, preset: str, reverb: float):
        super().__init__()
        self.voice_id = voice_id
        self.speed = speed
        self.volume = volume
        self.preset = preset
        self.reverb = reverb

    def run(self) -> None:
        try:
            with tempfile.TemporaryDirectory(prefix=".voice_preview_", dir=OUTPUT) as folder:
                track = prepare_narration("Пока ты ищешь мотивацию, кто-то уже ищет клиентов",
                                          self.voice_id, Path(folder), speed=self.speed,
                                          volume=self.volume, preset=self.preset,
                                          reverb=self.reverb)
                destination = OUTPUT / f".voice_preview_{uuid.uuid4().hex[:10]}.wav"
                shutil.copy2(track.path, destination)
            self.completed.emit({"path": destination})
        except Exception as exc:
            self.completed.emit({"error": str(exc)})


class ElevenLabsCheckThread(QThread):
    completed = Signal(object)

    def run(self) -> None:
        try:
            self.completed.emit({"result": probe_connection()})
        except Exception as exc:
            self.completed.emit({"error": str(exc)})


class ElevenLabsVoicesThread(QThread):
    completed = Signal(object)

    def run(self) -> None:
        try:
            self.completed.emit({"voices": list_account_voices()})
        except Exception as exc:
            self.completed.emit({"error": str(exc)})


class ElevenLabsPreviewThread(QThread):
    completed = Signal(object)

    def run(self) -> None:
        try:
            destination = OUTPUT / "voice_test.mp3"
            track = synthesize("Пока ты ищешь оправдания, другие уже зарабатывают. Время не ждёт никого",
                               destination, **self.options)
            self.completed.emit({"path": str(destination), "cached": track.cached,
                                 "seconds": track.seconds, "fallback": track.fallback})
        except Exception as exc:
            self.completed.emit({"error": str(exc)})

    def __init__(self, options: dict):
        super().__init__()
        self.options = options


def label(text: str, *, muted: bool = False) -> QLabel:
    item = QLabel(text)
    item.setObjectName("muted" if muted else "fieldLabel")
    return item


def field(layout: QVBoxLayout | QGridLayout, title: str, widget: QWidget, row: int | None = None, col: int = 0) -> None:
    if row is None:
        layout.addWidget(label(title))
        layout.addWidget(widget)
    else:
        box = QWidget()
        inner = QVBoxLayout(box)
        inner.setContentsMargins(0, 0, 0, 0)
        inner.setSpacing(7)
        inner.addWidget(label(title))
        inner.addWidget(widget)
        layout.addWidget(box, row, col)


class Studio(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("AUTO SHORTS STUDIO")
        self.resize(1260, 820)
        self.setMinimumSize(850, 620)
        self.current_thread: RenderThread | None = None
        self.voice_preview_thread: VoicePreviewThread | None = None
        self.elevenlabs_preview_thread: ElevenLabsPreviewThread | None = None
        self.elevenlabs_check_thread: ElevenLabsCheckThread | None = None
        self.elevenlabs_voices_thread: ElevenLabsVoicesThread | None = None
        self.audio_export_thread: AudioExportThread | None = None
        self.audio_preview_path: Path | None = None
        self.audio_result_path: Path | None = None
        self.audio_preview_mode = False
        self.voice_preview_path: Path | None = None
        self.batch_quotes: list[str] = []
        self.video_path: Path | None = None
        self._smoke_mode = False
        self.voice_settings_store = QSettings("AutoShortsStudio", "Voice")
        self.audio_settings_store = QSettings("AutoShortsStudio", "Audio")
        self.visual_settings_store = QSettings("AutoShortsStudio", "Visual")
        self.custom_preset_names: set[str] = set()
        try:
            saved_presets = json.loads(self.visual_settings_store.value("custom_presets", "{}"))
            if isinstance(saved_presets, dict):
                custom = {str(key): value for key, value in saved_presets.items() if isinstance(value, dict)}
                VISUAL_PRESETS.update(custom)
                self.custom_preset_names.update(custom)
        except (TypeError, ValueError):
            pass
        self.elevenlabs_credentials = load_credentials()
        self._build_ui()
        self._apply_style()
        self.refresh_music()
        self.refresh_voices()
        for old_preview in OUTPUT.glob(".voice_preview_*.wav"):
            if time.time() - old_preview.stat().st_mtime > 3600:
                try:
                    old_preview.unlink()
                except PermissionError:
                    pass
        for old_preview in OUTPUT.glob(".audio_preview_*.wav"):
            try:
                if time.time() - old_preview.stat().st_mtime > 3600:
                    old_preview.unlink()
            except PermissionError:
                pass
        try:
            ffmpeg = find_ffmpeg()
            self.engine_label.setText(f"H.264: {find_encoder(ffmpeg)}")
        except Exception as exc:
            self.engine_label.setText(f"FFmpeg: {exc}")

    def _build_ui(self) -> None:
        root = QWidget()
        self.setCentralWidget(root)
        outer = QHBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(220)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(18, 22, 18, 20)
        side.setSpacing(8)
        brand = QLabel("AUTO SHORTS\nSTUDIO")
        brand.setObjectName("brand")
        side.addWidget(brand)
        side.addSpacing(32)
        self.nav_buttons: list[QPushButton] = []
        for index, title in enumerate(("Генератор", "Предпросмотр", "Пакетная генерация", "Настройки", "Визуальный стиль", "Озвучка ElevenLabs", "Аудиозапись")):
            button = QPushButton(title)
            button.setObjectName("nav")
            button.setCheckable(True)
            button.clicked.connect(lambda checked=False, i=index: self.show_page(i))
            side.addWidget(button)
            self.nav_buttons.append(button)
        side.addStretch()
        self.engine_label = label("Проверка FFmpeg…", muted=True)
        self.engine_label.setWordWrap(True)
        side.addWidget(self.engine_label)
        self.ffmpeg_button = QPushButton("Указать FFmpeg…")
        self.ffmpeg_button.setObjectName("nav")
        self.ffmpeg_button.setToolTip("Выберите ffmpeg.exe один раз. Путь сохранится для следующих запусков.")
        self.ffmpeg_button.clicked.connect(self.pick_ffmpeg)
        side.addWidget(self.ffmpeg_button)
        local_note = label("ElevenLabs API · Windows SAPI доступен как резерв", muted=True)
        local_note.setWordWrap(True)
        side.addWidget(local_note)
        outer.addWidget(sidebar)

        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(28, 22, 28, 26)
        content_layout.setSpacing(16)
        top = QHBoxLayout()
        self.heading = QLabel("Генератор")
        self.heading.setObjectName("heading")
        top.addWidget(self.heading)
        top.addStretch()
        folder = QPushButton("Открыть папку с результатами")
        folder.setObjectName("secondary")
        folder.clicked.connect(self.open_output)
        top.addWidget(folder)
        content_layout.addLayout(top)
        self.pages = QStackedWidget()
        content_layout.addWidget(self.pages, 1)
        self.pages.addWidget(self._generator_page())
        self.pages.addWidget(self._preview_page())
        self.pages.addWidget(self._batch_page())
        self.pages.addWidget(self._settings_page())
        self.pages.addWidget(self._visual_page())
        self.pages.addWidget(self._voice_page())
        self.pages.addWidget(self._audio_page())
        outer.addWidget(content, 1)
        self.show_page(0)

    def _card(self) -> tuple[QFrame, QVBoxLayout]:
        card = QFrame()
        card.setObjectName("card")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(11)
        return card, layout

    def _generator_page(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        page = QWidget()
        page.setObjectName("scrollPage")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)
        card, body = self._card()
        body.addWidget(QLabel("01  ·  ВАШ ТЕКСТ"))
        self.quote = QPlainTextEdit("Тот, кто боится проиграть, уже [red]проиграл[/red]")
        self.quote.setPlaceholderText("Введите цитату или короткий текст…")
        self.quote.setMinimumHeight(145)
        field(body, "Цитата или короткий текст", self.quote)
        body.addWidget(label("Для ручного акцента: [red]важное слово[/red]", muted=True))
        options = QGridLayout()
        options.setHorizontalSpacing(18)
        options.setVerticalSpacing(12)
        self.style_combo = QComboBox()
        self.style_combo.addItems(STYLES)
        self.style_combo.currentTextChanged.connect(self._set_theme_colors)
        field(options, "Визуальный стиль", self.style_combo, 0, 0)
        self.animation_combo = QComboBox()
        self.animation_combo.addItems(ANIMATIONS)
        field(options, "Анимация", self.animation_combo, 0, 1)
        self.format_combo = QComboBox()
        self.format_combo.addItems(FORMATS)
        self.format_combo.setCurrentText("Вертикальный 1080×1920")
        field(options, "Формат кадра", self.format_combo, 1, 0)
        self.speed = QDoubleSpinBox()
        self.speed.setRange(0.5, 2.0)
        self.speed.setSingleStep(0.1)
        self.speed.setValue(1.0)
        self.speed.setSuffix(" ×")
        field(options, "Скорость", self.speed, 1, 1)
        body.addLayout(options)
        music_row = QHBoxLayout()
        self.music_combo = QComboBox()
        music_row.addWidget(self.music_combo, 1)
        add_music = QPushButton("+ Добавить MP3/WAV")
        add_music.setObjectName("secondary")
        add_music.clicked.connect(self.add_music)
        music_row.addWidget(add_music)
        body.addWidget(label("Музыка"))
        body.addLayout(music_row)
        self.quick_preview = QCheckBox("Быстрый просмотр 1024×576")
        body.addWidget(self.quick_preview)
        self.generate_button = QPushButton("Сгенерировать видео")
        self.generate_button.setObjectName("primary")
        self.generate_button.clicked.connect(self.start_single)
        body.addWidget(self.generate_button)
        layout.addWidget(card)
        status_card, status = self._card()
        status.addWidget(QLabel("02  ·  ПРОГРЕСС"))
        self.single_status = label("Готово к генерации", muted=True)
        status.addWidget(self.single_status)
        self.single_progress = QProgressBar()
        self.single_progress.setValue(0)
        status.addWidget(self.single_progress)
        self.single_result = QLabel("")
        self.single_result.setWordWrap(True)
        status.addWidget(self.single_result)
        layout.addWidget(status_card)
        layout.addStretch()
        scroll.setWidget(page)
        return scroll

    def _preview_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        card, body = self._card()
        body.addWidget(QLabel("ПРЕДПРОСМОТР MP4"))
        self.video = QVideoWidget()
        self.video.setMinimumHeight(340)
        self.video.setStyleSheet("background:#000;border-radius:8px")
        body.addWidget(self.video, 1)
        self.audio_output = QAudioOutput(self)
        self.player = QMediaPlayer(self)
        self.player.setAudioOutput(self.audio_output)
        self.player.setVideoOutput(self.video)
        self.player.positionChanged.connect(self._position_changed)
        self.player.durationChanged.connect(self._duration_changed)
        self.player.playbackStateChanged.connect(self._playback_changed)
        self.player.errorOccurred.connect(self._player_error)
        controls = QHBoxLayout()
        self.play_button = QPushButton("Воспроизвести")
        self.play_button.setObjectName("secondary")
        self.play_button.clicked.connect(self.toggle_playback)
        controls.addWidget(self.play_button)
        self.seek = QSlider(Qt.Horizontal)
        self.seek.setRange(0, 0)
        self.seek.sliderMoved.connect(self.player.setPosition)
        controls.addWidget(self.seek, 1)
        self.time_label = QLabel("0:00 / 0:00")
        controls.addWidget(self.time_label)
        body.addLayout(controls)
        self.preview_status = label("Сгенерируйте видео или откройте готовый MP4.", muted=True)
        self.preview_status.setWordWrap(True)
        body.addWidget(self.preview_status)
        actions = QHBoxLayout()
        open_video = QPushButton("Открыть MP4…")
        open_video.setObjectName("secondary")
        open_video.clicked.connect(self.open_video_dialog)
        actions.addWidget(open_video)
        self.open_video_folder_button = QPushButton("Показать файл в папке")
        self.open_video_folder_button.setObjectName("secondary")
        self.open_video_folder_button.clicked.connect(self.show_video_file)
        actions.addWidget(self.open_video_folder_button)
        actions.addStretch()
        body.addLayout(actions)
        layout.addWidget(card, 1)
        return page

    def _batch_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        card, body = self._card()
        body.addWidget(QLabel("ПАКЕТНАЯ ГЕНЕРАЦИЯ"))
        body.addWidget(label("TXT: одна цитата на строку · CSV: первый столбец · JSON: список цитат", muted=True))
        select = QPushButton("Выбрать TXT / CSV / JSON…")
        select.setObjectName("secondary")
        select.clicked.connect(self.pick_batch_file)
        body.addWidget(select)
        self.batch_file_label = label("Файл не выбран", muted=True)
        body.addWidget(self.batch_file_label)
        self.random_music = QCheckBox("Случайная музыка из локальной библиотеки")
        body.addWidget(self.random_music)
        self.batch_style_mode = QComboBox()
        self.batch_style_mode.addItems(("Один выбранный стиль", "Случайные пресеты", "Чередовать пресеты"))
        field(body, "Оформление пакета", self.batch_style_mode)
        self.batch_presets = QListWidget()
        self.batch_presets.setMaximumHeight(150)
        for name in VISUAL_PRESETS:
            item = QListWidgetItem(name, self.batch_presets)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked)
        body.addWidget(label("Стили для случайного выбора или чередования", muted=True))
        body.addWidget(self.batch_presets)
        body.addWidget(label("Один стиль использует текущие настройки генератора. Выбранные стили сохраняются в CSV-отчёте.", muted=True))
        buttons = QHBoxLayout()
        self.batch_start_button = QPushButton("Запустить пакет")
        self.batch_start_button.setObjectName("primary")
        self.batch_start_button.clicked.connect(self.start_batch)
        buttons.addWidget(self.batch_start_button)
        self.batch_stop_button = QPushButton("Остановить")
        self.batch_stop_button.setObjectName("danger")
        self.batch_stop_button.setEnabled(False)
        self.batch_stop_button.clicked.connect(self.stop_batch)
        buttons.addWidget(self.batch_stop_button)
        buttons.addStretch()
        body.addLayout(buttons)
        layout.addWidget(card)
        report_card, report_layout = self._card()
        self.batch_status = label("Ожидание файла", muted=True)
        report_layout.addWidget(self.batch_status)
        self.batch_progress = QProgressBar()
        self.batch_progress.setValue(0)
        report_layout.addWidget(self.batch_progress)
        self.batch_files = QListWidget()
        self.batch_files.itemDoubleClicked.connect(lambda item: self.load_video(Path(item.data(Qt.UserRole))))
        report_layout.addWidget(self.batch_files, 1)
        self.report_button = QPushButton("Открыть CSV-отчёт")
        self.report_button.setObjectName("secondary")
        self.report_button.setEnabled(False)
        self.report_button.clicked.connect(self.open_report)
        report_layout.addWidget(self.report_button)
        layout.addWidget(report_card, 1)
        return page

    def _settings_page(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        page = QWidget()
        page.setObjectName("scrollPage")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        card, body = self._card()
        grid = QGridLayout()
        grid.setHorizontalSpacing(18)
        grid.setVerticalSpacing(12)
        self.fps = QComboBox()
        self.fps.addItems(("30", "60"))
        field(grid, "FPS", self.fps, 0, 0)
        self.duration = QDoubleSpinBox()
        self.duration.setRange(0, 15)
        self.duration.setSingleStep(0.1)
        self.duration.setSpecialValueText("Автоматически")
        field(grid, "Длительность сцены, с", self.duration, 0, 1)
        self.delay = QDoubleSpinBox()
        self.delay.setRange(0, 2)
        self.delay.setSingleStep(0.1)
        field(grid, "Задержка текста, с", self.delay, 1, 0)
        self.opacity = QDoubleSpinBox()
        self.opacity.setRange(0, 1)
        self.opacity.setSingleStep(0.05)
        self.opacity.setValue(1)
        field(grid, "Прозрачность текста", self.opacity, 1, 1)
        self.scale = QDoubleSpinBox()
        self.scale.setRange(0.5, 1.5)
        self.scale.setSingleStep(0.05)
        self.scale.setValue(1)
        field(grid, "Масштаб текста", self.scale, 2, 0)
        self.position_x = QSpinBox()
        self.position_x.setRange(-25, 25)
        self.position_x.setSuffix(" %")
        field(grid, "Смещение X", self.position_x, 2, 1)
        self.position_y = QSpinBox()
        self.position_y.setRange(-25, 25)
        self.position_y.setSuffix(" %")
        self.position_y.setValue(-8)
        field(grid, "Смещение Y", self.position_y, 3, 0)
        self.volume = QDoubleSpinBox()
        self.volume.setRange(0, 2)
        self.volume.setSingleStep(0.05)
        self.volume.setValue(0.7)
        field(grid, "Громкость музыки", self.volume, 3, 1)
        self.fade = QDoubleSpinBox()
        self.fade.setRange(0, 5)
        self.fade.setSingleStep(0.1)
        self.fade.setValue(0.3)
        field(grid, "Аудиопереходы, с", self.fade, 4, 0)
        self.sync_mode = QComboBox()
        self.sync_mode.addItems(("Равномерно", "Акценты музыки (экспериментально)"))
        field(grid, "Синхронизация текста", self.sync_mode, 4, 1)
        self.foreground = QPushButton("#F4F4F4")
        self.foreground.setProperty("color", "#F4F4F4")
        self.foreground.clicked.connect(lambda: self.pick_color(self.foreground))
        field(grid, "Основной цвет", self.foreground, 5, 0)
        self.accent = QPushButton("#BB3040")
        self.accent.setProperty("color", "#BB3040")
        self.accent.clicked.connect(lambda: self.pick_color(self.accent))
        field(grid, "Акцентный цвет", self.accent, 5, 1)
        self.font_path = QLineEdit()
        self.font_path.setPlaceholderText("Системный шрифт по стилю")
        font_row = QWidget()
        font_layout = QHBoxLayout(font_row)
        font_layout.setContentsMargins(0, 0, 0, 0)
        font_layout.addWidget(self.font_path, 1)
        choose_font = QPushButton("Обзор…")
        choose_font.clicked.connect(self.pick_font)
        font_layout.addWidget(choose_font)
        field(grid, "Свой шрифт TTF/OTF/TTC (необязательно)", font_row, 6, 0)
        body.addLayout(grid)
        body.addWidget(label("Синхронизация с музыкой экспериментальная. При слабых акцентах используется равномерный ритм.", muted=True))
        layout.addWidget(card)
        layout.addStretch()
        scroll.setWidget(page)
        return scroll

    def _visual_page(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        page = QWidget()
        page.setObjectName("scrollPage")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        card, body = self._card()
        body.addWidget(QLabel("ВИЗУАЛЬНЫЙ СТИЛЬ"))
        self.visual_preset = QComboBox()
        self.visual_preset.addItems(VISUAL_PRESETS)
        self.visual_preset.currentTextChanged.connect(self.apply_visual_preset)
        field(body, "Готовый пресет", self.visual_preset)
        self.animation_in = QComboBox(); self.animation_in.addItems(("None", *VISUAL_ANIMATIONS))
        self.animation_hold = QComboBox(); self.animation_hold.addItems(("None", *VISUAL_ANIMATIONS))
        self.animation_out = QComboBox(); self.animation_out.addItems(("None", *VISUAL_ANIMATIONS))
        animation_grid = QGridLayout()
        field(animation_grid, "Появление", self.animation_in, 0, 0)
        field(animation_grid, "Удержание", self.animation_hold, 0, 1)
        field(animation_grid, "Исчезновение", self.animation_out, 1, 0)
        body.addWidget(label("Библиотека включает 20 эффектов; здесь выбираются независимые фазы.", muted=True))
        body.addLayout(animation_grid)
        self.background_combo = QComboBox(); self.background_combo.addItems(BACKGROUNDS)
        self.background_combo.currentTextChanged.connect(self._update_background_swatch)
        field(body, "Тип фона", self.background_combo)
        self.background_swatch = QLabel()
        self.background_swatch.setMinimumHeight(110)
        self.background_swatch.setMaximumHeight(170)
        self.background_swatch.setAlignment(Qt.AlignCenter)
        self.background_swatch.setStyleSheet("background:#08090c;border-radius:8px")
        body.addWidget(self.background_swatch)
        self.background_path = QLineEdit()
        self.background_path.setPlaceholderText("Без файла — использовать встроенный фон")
        bg_row = QWidget(); bg_layout = QHBoxLayout(bg_row); bg_layout.setContentsMargins(0, 0, 0, 0)
        bg_layout.addWidget(self.background_path, 1)
        bg_button = QPushButton("Выбрать изображение / видео…")
        bg_button.clicked.connect(self.pick_background)
        bg_layout.addWidget(bg_button)
        field(body, "Собственный фон JPG / PNG / WEBP / MP4 / MOV / WEBM", bg_row)
        tint_row = QWidget(); tint_layout = QHBoxLayout(tint_row); tint_layout.setContentsMargins(0, 0, 0, 0)
        self.background_tint = QLineEdit()
        self.background_tint.setPlaceholderText("Без оттенка (например, #302040)")
        self.background_tint.textChanged.connect(self._update_background_swatch)
        tint_layout.addWidget(self.background_tint, 1)
        tint_button = QPushButton("Выбрать цвет…")
        tint_button.clicked.connect(self.pick_background_tint)
        tint_layout.addWidget(tint_button)
        field(body, "Цветовой оттенок фона", tint_row)
        grid = QGridLayout()
        self.background_intensity = QDoubleSpinBox(); self.background_intensity.setRange(0, 1); self.background_intensity.setSingleStep(.05); self.background_intensity.setValue(.25)
        self.background_speed = QDoubleSpinBox(); self.background_speed.setRange(.1, 3); self.background_speed.setSingleStep(.1); self.background_speed.setValue(1)
        self.animation_strength = QDoubleSpinBox(); self.animation_strength.setRange(0, 2); self.animation_strength.setSingleStep(.1); self.animation_strength.setValue(1)
        self.animation_duration = QDoubleSpinBox(); self.animation_duration.setRange(.05, 3); self.animation_duration.setSingleStep(.05); self.animation_duration.setValue(.35); self.animation_duration.setSuffix(" с")
        self.animation_speed = QDoubleSpinBox(); self.animation_speed.setRange(.2, 3); self.animation_speed.setSingleStep(.1); self.animation_speed.setValue(1); self.animation_speed.setSuffix(" ×")
        self.easing = QComboBox(); self.easing.addItem("Плавная (Smooth)", "Smooth"); self.easing.addItem("Линейная (Linear)", "Linear")
        self.background_brightness = QDoubleSpinBox(); self.background_brightness.setRange(-.5, .5); self.background_brightness.setSingleStep(.05)
        self.background_contrast = QDoubleSpinBox(); self.background_contrast.setRange(.5, 2); self.background_contrast.setSingleStep(.05); self.background_contrast.setValue(1)
        field(grid, "Интенсивность фона / затемнение видео", self.background_intensity, 0, 0)
        field(grid, "Скорость движения фона", self.background_speed, 0, 1)
        field(grid, "Интенсивность анимации", self.animation_strength, 1, 0)
        field(grid, "Длительность перехода", self.animation_duration, 1, 1)
        field(grid, "Скорость анимации", self.animation_speed, 2, 0)
        field(grid, "Кривая плавности", self.easing, 2, 1)
        field(grid, "Яркость собственного фона", self.background_brightness, 3, 0)
        field(grid, "Контраст собственного фона", self.background_contrast, 3, 1)
        body.addLayout(grid)
        self.visual_font = QComboBox()
        for name, path in available_fonts().items(): self.visual_font.addItem(name, path)
        self.visual_font.currentIndexChanged.connect(self._select_visual_font)
        field(body, "Шрифт с кириллицей · 15 системных + локальная папка fonts", self.visual_font)
        self.font_sample = QLabel("Пример: Своя история начинается сегодня")
        self.font_sample.setMinimumHeight(42)
        self.font_sample.setStyleSheet("font-size:18px; font-weight:700; padding:8px; background:#0e1116; border-radius:7px")
        body.addWidget(self.font_sample)
        self._select_visual_font(self.visual_font.currentIndex())
        type_grid = QGridLayout()
        self.visual_font_size = QDoubleSpinBox(); self.visual_font_size.setRange(.5, 1.5); self.visual_font_size.setSingleStep(.05); self.visual_font_size.setValue(1)
        self.visual_bold = QCheckBox("Полужирный")
        self.visual_letter_spacing = QDoubleSpinBox(); self.visual_letter_spacing.setRange(-.1, .2); self.visual_letter_spacing.setSingleStep(.005); self.visual_letter_spacing.setValue(.035)
        self.visual_line_spacing = QDoubleSpinBox(); self.visual_line_spacing.setRange(1, 2.5); self.visual_line_spacing.setSingleStep(.05); self.visual_line_spacing.setValue(1.75)
        self.visual_align = QComboBox(); self.visual_align.addItem("По центру", "center"); self.visual_align.addItem("Слева", "left"); self.visual_align.addItem("Справа", "right")
        self.visual_shadow = QDoubleSpinBox(); self.visual_shadow.setRange(0, 2); self.visual_shadow.setSingleStep(.1); self.visual_shadow.setValue(.3)
        self.visual_outline = QDoubleSpinBox(); self.visual_outline.setRange(0, 8); self.visual_outline.setSingleStep(.5)
        field(type_grid, "Размер текста", self.visual_font_size, 0, 0)
        type_grid.addWidget(self.visual_bold, 0, 1)
        field(type_grid, "Межбуквенный интервал", self.visual_letter_spacing, 1, 0)
        field(type_grid, "Межстрочный интервал", self.visual_line_spacing, 1, 1)
        field(type_grid, "Выравнивание", self.visual_align, 2, 0)
        field(type_grid, "Тень", self.visual_shadow, 2, 1)
        field(type_grid, "Обводка", self.visual_outline, 3, 0)
        body.addWidget(label("Цвет текста, прозрачность и ручной цветовой акцент доступны в разделе «Настройки». Размер автоматически подгоняется под кадр.", muted=True))
        body.addLayout(type_grid)
        controls = QHBoxLayout()
        reset = QPushButton("Сбросить настройки")
        reset.clicked.connect(self.reset_visual_settings)
        controls.addWidget(reset)
        save_preset = QPushButton("Сохранить пользовательский пресет")
        save_preset.clicked.connect(self.save_visual_preset)
        controls.addWidget(save_preset)
        self.anim_preview_button = QPushButton("Быстрый предпросмотр без ElevenLabs")
        self.anim_preview_button.clicked.connect(self.preview_visual_frame)
        controls.addWidget(self.anim_preview_button)
        body.addLayout(controls)
        self.anim_preview_label = QLabel("Предпросмотр появится здесь")
        self.anim_preview_label.setAlignment(Qt.AlignCenter)
        self.anim_preview_label.setMinimumHeight(220)
        self.anim_preview_label.setStyleSheet("background:#08090c;border-radius:8px")
        body.addWidget(self.anim_preview_label)
        self.anim_preview_timer = QTimer(self)
        self.anim_preview_timer.setInterval(50)
        self.anim_preview_frame = 0
        self.anim_preview_timer.timeout.connect(self._preview_visual_tick)
        self.apply_visual_preset("CLASSIC DARK")
        self._update_background_swatch()
        layout.addWidget(card)
        layout.addStretch()
        scroll.setWidget(page)
        return scroll

    def _voice_page(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        page = QWidget()
        page.setObjectName("scrollPage")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        card, body = self._card()
        body.addWidget(QLabel("ELEVENLABS · ОСНОВНОЙ ДВИЖОК"))
        self.voice_enabled = QCheckBox("Включить автоматическую озвучку роликов")
        self.voice_enabled.setChecked(self.voice_settings_store.value(
            "enabled", bool(self.elevenlabs_credentials.api_key and self.elevenlabs_credentials.voice_id), type=bool))
        body.addWidget(self.voice_enabled)
        self.tts_provider = QComboBox()
        self.tts_provider.addItem("ElevenLabs API", "elevenlabs")
        self.tts_provider.addItem("Windows SAPI · локально", "sapi")
        default_provider = "elevenlabs" if self.elevenlabs_credentials.api_key and self.elevenlabs_credentials.voice_id else "sapi"
        saved_provider = self.voice_settings_store.value("provider", default_provider)
        provider_index = self.tts_provider.findData(saved_provider)
        self.tts_provider.setCurrentIndex(provider_index if provider_index >= 0 else 0)
        field(body, "Движок озвучки", self.tts_provider)
        self.elevenlabs_status = label("Подключение не проверено. Используйте кнопку ниже; аудио создаваться не будет.", muted=True)
        self.elevenlabs_status.setWordWrap(True)
        body.addWidget(self.elevenlabs_status)
        self.elevenlabs_voice_name = label("Голос: не проверен", muted=True)
        body.addWidget(self.elevenlabs_voice_name)
        self.elevenlabs_voice_combo = QComboBox()
        saved_voice = self.voice_settings_store.value("elevenlabs_voice_id", self.elevenlabs_credentials.voice_id)
        if self.elevenlabs_credentials.voice_id:
            self.elevenlabs_voice_combo.addItem("Настроенный голос · " + self.elevenlabs_credentials.voice_id,
                                                self.elevenlabs_credentials.voice_id)
        idx = self.elevenlabs_voice_combo.findData(saved_voice) if saved_voice else -1
        if idx >= 0:
            self.elevenlabs_voice_combo.setCurrentIndex(idx)
        field(body, "Голос ElevenLabs", self.elevenlabs_voice_combo)
        refresh_api_voices = QPushButton("Загрузить голоса аккаунта")
        refresh_api_voices.setObjectName("secondary")
        refresh_api_voices.clicked.connect(self.refresh_elevenlabs_voices)
        body.addWidget(refresh_api_voices, 0, Qt.AlignLeft)
        refresh_api = QPushButton("Проверить подключение без генерации аудио")
        refresh_api.setObjectName("secondary")
        refresh_api.clicked.connect(self.check_elevenlabs_connection)
        body.addWidget(refresh_api, 0, Qt.AlignLeft)
        self.elevenlabs_model = QComboBox()
        for title, model_id in MODELS.items():
            self.elevenlabs_model.addItem(title, model_id)
        saved_model = self.voice_settings_store.value("model", DEFAULT_MODEL)
        model_index = self.elevenlabs_model.findData(saved_model)
        self.elevenlabs_model.setCurrentIndex(model_index if model_index >= 0 else 0)
        field(body, "Модель", self.elevenlabs_model)
        settings_grid = QGridLayout()
        settings_grid.setVerticalSpacing(12)
        self.el_stability = QDoubleSpinBox()
        self.el_stability.setRange(0, 1)
        self.el_stability.setSingleStep(0.05)
        self.el_stability.setValue(float(self.voice_settings_store.value("stability", 0.52)))
        field(settings_grid, "Stability", self.el_stability, 0, 0)
        self.el_similarity = QDoubleSpinBox()
        self.el_similarity.setRange(0, 1)
        self.el_similarity.setSingleStep(0.05)
        self.el_similarity.setValue(float(self.voice_settings_store.value("similarity", 0.78)))
        field(settings_grid, "Similarity Boost", self.el_similarity, 0, 1)
        self.el_style = QDoubleSpinBox()
        self.el_style.setRange(0, 1)
        self.el_style.setSingleStep(0.05)
        self.el_style.setValue(float(self.voice_settings_store.value("style", 0.10)))
        field(settings_grid, "Выразительность", self.el_style, 1, 0)
        self.el_speed = QDoubleSpinBox()
        self.el_speed.setRange(0.7, 1.2)
        self.el_speed.setSingleStep(0.05)
        saved_speed = self.voice_settings_store.value("speed", None)
        # Move installs using the old fast default to a slower narrator pace.
        if saved_speed is None or abs(float(saved_speed) - 0.95) < 0.001:
            saved_speed = 0.82
        self.el_speed.setValue(float(saved_speed))
        self.el_speed.setSuffix(" ×")
        field(settings_grid, "Скорость речи", self.el_speed, 1, 1)
        self.el_volume = QSpinBox()
        self.el_volume.setRange(0, 100)
        self.el_volume.setSuffix(" %")
        self.el_volume.setValue(int(self.voice_settings_store.value("el_volume", 100)))
        field(settings_grid, "Громкость голоса", self.el_volume, 2, 0)
        self.el_speaker_boost = QCheckBox("Использовать Speaker Boost")
        self.el_speaker_boost.setChecked(self.voice_settings_store.value("speaker_boost", True, type=bool))
        settings_grid.addWidget(self.el_speaker_boost, 3, 0, 1, 2)
        body.addLayout(settings_grid)
        self.el_estimate = label("Оценка: — символов до запроса", muted=True)
        body.addWidget(self.el_estimate)
        self.voice_enabled.toggled.connect(self._update_voice_estimate)
        self.tts_provider.currentIndexChanged.connect(self._update_voice_estimate)
        self.elevenlabs_model.currentIndexChanged.connect(self._update_voice_estimate)
        self.quote.textChanged.connect(self._update_voice_estimate)
        body.addWidget(QLabel("ЛОКАЛЬНЫЙ РЕЗЕРВНЫЙ РЕЖИМ · WINDOWS SAPI"))
        explanation = label("Локальная озвучка сохраняется как дополнительный вариант. Она использует фактическую длительность каждой фразы.", muted=True)
        explanation.setWordWrap(True)
        body.addWidget(explanation)
        self.voice_combo = QComboBox()
        refresh = QPushButton("Обновить голоса")
        refresh.setObjectName("secondary")
        refresh.clicked.connect(self.refresh_voices)
        body.addWidget(label("Установленный голос Windows"))
        body.addWidget(self.voice_combo)
        body.addWidget(refresh, 0, Qt.AlignLeft)
        self.voice_help = label("", muted=True)
        self.voice_help.setWordWrap(True)
        body.addWidget(self.voice_help)
        grid = QGridLayout()
        grid.setVerticalSpacing(12)
        self.voice_speed = QDoubleSpinBox()
        self.voice_speed.setRange(0.7, 1.5)
        self.voice_speed.setSingleStep(0.05)
        self.voice_speed.setValue(0.82)
        self.voice_speed.setSuffix(" ×")
        field(grid, "Скорость речи без изменения высоты тона", self.voice_speed, 0, 0)
        self.voice_volume = QSpinBox()
        self.voice_volume.setRange(0, 100)
        self.voice_volume.setValue(100)
        self.voice_volume.setSuffix(" %")
        field(grid, "Громкость голоса", self.voice_volume, 1, 0)
        self.voice_preset = QComboBox()
        self.voice_preset.addItems(PRESETS)
        self.voice_preset.setCurrentText("Dramatic Narrator")
        field(grid, "Пресет обработки", self.voice_preset, 2, 0)
        self.voice_reverb = QDoubleSpinBox()
        self.voice_reverb.setRange(0, 0.35)
        self.voice_reverb.setSingleStep(0.02)
        self.voice_reverb.setValue(0.08)
        field(grid, "Реверберация", self.voice_reverb, 3, 0)
        self.phrase_pause = QDoubleSpinBox()
        self.phrase_pause.setRange(0.25, 1.2)
        self.phrase_pause.setSingleStep(0.05)
        self.phrase_pause.setValue(0.35)
        self.phrase_pause.setSuffix(" с")
        field(grid, "Пауза между фразами", self.phrase_pause, 4, 0)
        body.addLayout(grid)
        preset_note = label("Dramatic Narrator добавляет тепло, разборчивость и мягкую компрессию. Обработка не меняет природный тембр выбранного голоса.", muted=True)
        preset_note.setWordWrap(True)
        body.addWidget(preset_note)
        self.voice_test_button = QPushButton("Тест озвучки")
        self.voice_test_button.setObjectName("primary")
        self.voice_test_button.clicked.connect(self.preview_voice)
        body.addWidget(self.voice_test_button)
        self.voice_status = label("Готово к прослушиванию", muted=True)
        self.voice_status.setWordWrap(True)
        body.addWidget(self.voice_status)
        self.voice_audio_output = QAudioOutput(self)
        self.voice_player = QMediaPlayer(self)
        self.voice_player.setAudioOutput(self.voice_audio_output)
        layout.addWidget(card)
        layout.addStretch()
        scroll.setWidget(page)
        return scroll

    def _audio_page(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        page = QWidget()
        page.setObjectName("scrollPage")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)

        card, body = self._card()
        body.addWidget(QLabel("АУДИОЗАПИСЬ · БЕЗ ВИДЕОРЕНДЕРА"))
        self.audio_text = QPlainTextEdit()
        self.audio_text.setPlaceholderText("Введите текст. Предложения будут озвучены по отдельности…")
        self.audio_text.setMinimumHeight(145)
        saved_text = self.audio_settings_store.value("text", "")
        self.audio_text.setPlainText(saved_text)
        field(body, "Текст озвучки", self.audio_text)

        grid = QGridLayout()
        grid.setHorizontalSpacing(18)
        grid.setVerticalSpacing(12)
        self.audio_provider = QComboBox()
        self.audio_provider.addItem("ElevenLabs API", "elevenlabs")
        self.audio_provider.addItem("Windows SAPI · локально", "sapi")
        preferred_provider = self.audio_settings_store.value(
            "provider", "elevenlabs" if self.elevenlabs_credentials.api_key and self.elevenlabs_credentials.voice_id else "sapi")
        provider_index = self.audio_provider.findData(preferred_provider)
        self.audio_provider.setCurrentIndex(provider_index if provider_index >= 0 else 0)
        field(grid, "Движок озвучки", self.audio_provider, 0, 0)
        self.audio_voice_combo = QComboBox()
        field(grid, "Голос", self.audio_voice_combo, 0, 1)
        self.audio_provider.currentIndexChanged.connect(self._refresh_audio_voice_choices)
        self.audio_voice_combo.currentIndexChanged.connect(self._update_audio_estimate)
        self.audio_text.textChanged.connect(self._update_audio_estimate)
        body.addLayout(grid)

        api_row = QHBoxLayout()
        api_refresh = QPushButton("Загрузить голоса ElevenLabs")
        api_refresh.setObjectName("secondary")
        api_refresh.clicked.connect(self.refresh_elevenlabs_voices)
        api_row.addWidget(api_refresh)
        sapi_refresh = QPushButton("Обновить голоса Windows")
        sapi_refresh.setObjectName("secondary")
        sapi_refresh.clicked.connect(self.refresh_voices)
        api_row.addWidget(sapi_refresh)
        api_row.addStretch()
        body.addLayout(api_row)
        self.audio_estimate = label("Оценка тарификации: —", muted=True)
        body.addWidget(self.audio_estimate)
        voice_help = label(
            "Голос по фотографии определить нельзя. Создание нового голоса по описанию и клонирование прямо из AUTO SHORTS STUDIO пока не поддерживаются. Выберите существующий голос аккаунта; созданный отдельно в ElevenLabs голос появится после обновления списка. Клонируйте только запись, на которую есть разрешение владельца.",
            muted=True)
        voice_help.setWordWrap(True)
        body.addWidget(voice_help)

        options = QGridLayout()
        options.setHorizontalSpacing(18)
        options.setVerticalSpacing(12)
        self.audio_speed = QDoubleSpinBox()
        self.audio_speed.setRange(0.7, 1.2)
        self.audio_speed.setSingleStep(0.05)
        self.audio_speed.setValue(float(self.audio_settings_store.value("speed", 0.82)))
        self.audio_speed.setSuffix(" ×")
        field(options, "Скорость речи", self.audio_speed, 0, 0)
        self.audio_voice_volume = QSpinBox()
        self.audio_voice_volume.setRange(0, 100)
        self.audio_voice_volume.setValue(int(self.audio_settings_store.value("voice_volume", 100)))
        self.audio_voice_volume.setSuffix(" %")
        field(options, "Громкость голоса", self.audio_voice_volume, 0, 1)
        self.audio_preset = QComboBox()
        self.audio_preset.addItems(PRESETS)
        self.audio_preset.setCurrentText(self.audio_settings_store.value("preset", "Dramatic Narrator"))
        field(options, "Обработка голоса", self.audio_preset, 1, 0)
        self.audio_reverb = QDoubleSpinBox()
        self.audio_reverb.setRange(0, 0.35)
        self.audio_reverb.setSingleStep(0.02)
        self.audio_reverb.setValue(float(self.audio_settings_store.value("reverb", 0.08)))
        field(options, "Глубина / реверберация", self.audio_reverb, 1, 1)
        self.audio_pause = QDoubleSpinBox()
        self.audio_pause.setRange(0, 2)
        self.audio_pause.setSingleStep(0.05)
        self.audio_pause.setValue(float(self.audio_settings_store.value("pause", 0.35)))
        self.audio_pause.setSuffix(" с")
        field(options, "Пауза между предложениями", self.audio_pause, 2, 0)
        self.audio_format = QComboBox()
        self.audio_format.addItem("MP3", "mp3")
        self.audio_format.addItem("WAV", "wav")
        self.audio_format.setCurrentIndex(1 if self.audio_settings_store.value("format", "mp3") == "wav" else 0)
        field(options, "Формат результата", self.audio_format, 2, 1)
        body.addLayout(options)

        music_row = QHBoxLayout()
        self.audio_music_path = QLineEdit(self.audio_settings_store.value("music", ""))
        self.audio_music_path.setPlaceholderText("Без музыки")
        music_row.addWidget(self.audio_music_path, 1)
        self.audio_music_button = QPushButton("Выбрать музыку…")
        self.audio_music_button.setObjectName("secondary")
        self.audio_music_button.clicked.connect(self.pick_audio_music)
        music_row.addWidget(self.audio_music_button)
        self.audio_clear_music = QPushButton("Убрать")
        self.audio_clear_music.setObjectName("secondary")
        self.audio_clear_music.clicked.connect(self.audio_music_path.clear)
        music_row.addWidget(self.audio_clear_music)
        body.addWidget(label("Фоновая музыка MP3/WAV"))
        body.addLayout(music_row)
        music_options = QHBoxLayout()
        self.audio_music_volume = QDoubleSpinBox()
        self.audio_music_volume.setRange(0, 1.5)
        self.audio_music_volume.setSingleStep(0.05)
        self.audio_music_volume.setValue(float(self.audio_settings_store.value("music_volume", 0.18)))
        field(music_options, "Громкость музыки", self.audio_music_volume)
        self.audio_duck = QCheckBox("Приглушать музыку во время речи")
        self.audio_duck.setChecked(self.audio_settings_store.value("duck", True, type=bool))
        music_options.addWidget(self.audio_duck, 1, Qt.AlignVCenter)
        self.audio_loop_music = QCheckBox("Зациклить музыку, если она короче речи")
        self.audio_loop_music.setChecked(self.audio_settings_store.value("loop_music", True, type=bool))
        music_options.addWidget(self.audio_loop_music, 1, Qt.AlignVCenter)
        body.addLayout(music_options)
        layout.addWidget(card)

        result_card, result = self._card()
        self.audio_status = label("Готово к созданию аудиозаписи", muted=True)
        self.audio_status.setWordWrap(True)
        result.addWidget(self.audio_status)
        self.audio_progress = QProgressBar()
        self.audio_progress.setValue(0)
        result.addWidget(self.audio_progress)
        buttons = QHBoxLayout()
        self.audio_export_button = QPushButton("Создать аудиозапись")
        self.audio_export_button.setObjectName("primary")
        self.audio_export_button.clicked.connect(self.start_audio_export)
        buttons.addWidget(self.audio_export_button)
        self.audio_preview_voice_button = QPushButton("Прослушать голос")
        self.audio_preview_voice_button.setObjectName("secondary")
        self.audio_preview_voice_button.clicked.connect(self.start_audio_preview)
        buttons.addWidget(self.audio_preview_voice_button)
        self.audio_preview_mix_button = QPushButton("Прослушать готовый микс")
        self.audio_preview_mix_button.setObjectName("secondary")
        self.audio_preview_mix_button.setEnabled(False)
        self.audio_preview_mix_button.clicked.connect(self.play_audio_result)
        buttons.addWidget(self.audio_preview_mix_button)
        self.audio_cancel_button = QPushButton("Остановить")
        self.audio_cancel_button.setObjectName("danger")
        self.audio_cancel_button.setEnabled(False)
        self.audio_cancel_button.clicked.connect(self.stop_audio_export)
        buttons.addWidget(self.audio_cancel_button)
        result.addLayout(buttons)
        self.audio_now_playing = label("", muted=True)
        self.audio_now_playing.setWordWrap(True)
        result.addWidget(self.audio_now_playing)
        self.audio_player_output = QAudioOutput(self)
        self.audio_player = QMediaPlayer(self)
        self.audio_player.setAudioOutput(self.audio_player_output)
        layout.addWidget(result_card)
        layout.addStretch()
        scroll.setWidget(page)
        self._refresh_audio_voice_choices()
        return scroll

    def _refresh_audio_voice_choices(self, *_args) -> None:
        if not hasattr(self, "audio_voice_combo"):
            return
        previous = self.audio_voice_combo.currentData() or self.audio_settings_store.value("voice_id", "")
        self.audio_voice_combo.clear()
        if self.audio_provider.currentData() == "sapi":
            for index in range(self.voice_combo.count()):
                self.audio_voice_combo.addItem(self.voice_combo.itemText(index), self.voice_combo.itemData(index))
            self.audio_voice_combo.setEnabled(self.audio_voice_combo.count() > 0)
        else:
            for index in range(self.elevenlabs_voice_combo.count()):
                self.audio_voice_combo.addItem(self.elevenlabs_voice_combo.itemText(index), self.elevenlabs_voice_combo.itemData(index))
            self.audio_voice_combo.setEnabled(bool(self.elevenlabs_credentials.api_key and self.audio_voice_combo.count()))
        selected_index = self.audio_voice_combo.findData(previous)
        if selected_index >= 0:
            self.audio_voice_combo.setCurrentIndex(selected_index)
        self._update_audio_estimate()

    def _update_audio_estimate(self, *_args) -> None:
        if not hasattr(self, "audio_estimate"):
            return
        if self.audio_provider.currentData() == "sapi":
            self.audio_estimate.setText("Локальная озвучка Windows · без расхода API-кредитов")
        else:
            count = estimate_characters(self.audio_text.toPlainText())
            self.audio_estimate.setText(f"Оценка ElevenLabs: около {count} тарифицируемых символов; точный тариф зависит от модели")

    def pick_audio_music(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Выберите фоновую музыку", str(MUSIC),
                                              "Аудио (*.mp3 *.wav)")
        if path:
            self.audio_music_path.setText(path)

    def _audio_export_options(self) -> dict:
        provider = self.audio_provider.currentData()
        options = self._elevenlabs_options() if provider == "elevenlabs" else {}
        if provider == "elevenlabs":
            options["speed"] = self.audio_speed.value()
        music_text = self.audio_music_path.text().strip()
        return {
            "text": self.audio_text.toPlainText(),
            "provider": provider,
            "voice_id": self.audio_voice_combo.currentData() if provider == "elevenlabs" else "",
            "options": options,
            "sapi_voice_id": self.audio_voice_combo.currentData() if provider == "sapi" else "",
            "speed": self.audio_speed.value(),
            "voice_volume": self.audio_voice_volume.value(),
            "preset": self.audio_preset.currentText(),
            "reverb": self.audio_reverb.value(),
            "pause": self.audio_pause.value(),
            "music": Path(music_text) if music_text else None,
            "music_volume": self.audio_music_volume.value(),
            "duck_music": self.audio_duck.isChecked(),
            "loop_music": self.audio_loop_music.isChecked(),
        }

    def start_audio_export(self) -> None:
        self._start_audio_task(preview=False)

    def start_audio_preview(self) -> None:
        self._start_audio_task(preview=True)

    def _start_audio_task(self, *, preview: bool) -> None:
        if self.audio_export_thread and self.audio_export_thread.isRunning():
            return
        try:
            options = self._audio_export_options()
            if not options["text"].strip():
                raise ValueError("Введите текст для озвучки.")
            if options["provider"] == "elevenlabs":
                if not self.elevenlabs_credentials.api_key:
                    raise ValueError("Не найден ELEVENLABS_API_KEY. Настройте его в .env.txt или выберите Windows SAPI.")
                if not options["voice_id"]:
                    raise ValueError("Загрузите голоса аккаунта или настройте ELEVENLABS_VOICE_ID.")
            elif not options["sapi_voice_id"]:
                raise ValueError("Русский голос Windows SAPI не найден. Установите компонент «Речь» для русского языка или используйте ElevenLabs.")
            if preview:
                if self.audio_preview_path:
                    self.audio_player.stop()
                    self.audio_player.setSource(QUrl())
                    try:
                        self.audio_preview_path.unlink(missing_ok=True)
                    except PermissionError:
                        pass
                destination = OUTPUT / f".audio_preview_{uuid.uuid4().hex[:10]}.wav"
                options["music"] = None
                self.audio_preview_mode = True
            else:
                destination = next_audio_path(OUTPUT, self.audio_format.currentData())
                self.audio_preview_mode = False
        except Exception as exc:
            QMessageBox.warning(self, "Аудиозапись", str(exc))
            return
        self.audio_progress.setValue(0)
        self.audio_export_button.setEnabled(False)
        self.audio_preview_voice_button.setEnabled(False)
        self.audio_cancel_button.setEnabled(True)
        self.audio_status.setText("Подготавливаю тест голоса…" if preview else "Создаю аудиозапись в фоновом потоке…")
        worker = AudioExportThread(destination, options)
        self.audio_export_thread = worker
        worker.progress.connect(self._audio_progress_changed)
        worker.completed.connect(self._audio_export_completed)
        worker.finished.connect(lambda current=worker: self._audio_export_finished(current))
        worker.start()

    def _audio_progress_changed(self, value: float, message: str) -> None:
        self.audio_progress.setValue(round(value * 100))
        self.audio_status.setText(message)

    def _audio_export_completed(self, result: dict) -> None:
        state = result.get("state")
        if state == "done":
            path = Path(result["path"])
            if self.audio_preview_mode:
                self.audio_preview_path = path
                self.audio_now_playing.setText("Предпрослушивание голоса (без музыки): " + str(path))
            else:
                self.audio_result_path = path
                self.audio_preview_mix_button.setEnabled(True)
                self.audio_now_playing.setText("Файл сохранён: " + str(path))
            player = self.audio_player
            player.stop()
            player.setSource(QUrl.fromLocalFile(str(path.resolve())))
            player.play()
            self.audio_status.setText("Предпрослушивание запущено" if self.audio_preview_mode else "Аудиозапись создана и запущена для прослушивания")
            self.audio_progress.setValue(100)
        elif state == "stopped":
            self.audio_status.setText(result.get("message", "Создание остановлено"))
        else:
            self.audio_status.setText("Ошибка: " + result.get("message", "не удалось создать файл"))
            QMessageBox.warning(self, "Ошибка создания аудиозаписи", result.get("message", "Неизвестная ошибка"))

    def _audio_export_finished(self, worker: AudioExportThread) -> None:
        if self.audio_export_thread is worker:
            self.audio_export_thread = None
        self.audio_export_button.setEnabled(True)
        self.audio_preview_voice_button.setEnabled(True)
        self.audio_cancel_button.setEnabled(False)

    def stop_audio_export(self) -> None:
        if self.audio_export_thread:
            self.audio_status.setText("Останавливаю после завершения текущей операции…")
            self.audio_export_thread.cancel_event.set()

    def play_audio_result(self) -> None:
        if self.audio_result_path and self.audio_result_path.is_file():
            self.audio_player.stop()
            self.audio_player.setSource(QUrl.fromLocalFile(str(self.audio_result_path.resolve())))
            self.audio_player.play()
            self.audio_now_playing.setText("Воспроизведение: " + str(self.audio_result_path))

    def _apply_style(self) -> None:
        self.setStyleSheet("""
            QMainWindow { background:#0b0d11; }
            QWidget { color:#eef0f3; font-family:'Arial'; font-size:13px; }
            QWidget#scrollPage, QScrollArea, QScrollArea QWidget#qt_scrollarea_viewport { background:#0b0d11; }
            QScrollBar:vertical { background:#0b0d11; width:11px; border:0; }
            QScrollBar::handle:vertical { background:#3b4350; border-radius:5px; min-height:25px; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height:0; }
            QFrame#sidebar { background:#101318; border-right:1px solid #272c35; }
            QLabel#brand { color:#fff; font-weight:800; font-size:17px; letter-spacing:1px; }
            QLabel#heading { font-size:28px; font-weight:700; }
            QLabel#muted { color:#949eaa; }
            QLabel#fieldLabel { color:#cbd1d9; font-weight:600; }
            QFrame#card { background:#15191f; border:1px solid #303640; border-radius:14px; }
            QPushButton { background:#242a33; color:#eef0f3; border:1px solid #3d4653; border-radius:8px; padding:10px 13px; text-align:center; }
            QPushButton:hover { border-color:#cf5268; background:#2e333e; }
            QPushButton:disabled { color:#6f7781; background:#1b1f25; }
            QPushButton#nav { background:transparent; border:0; text-align:left; padding:13px; color:#aeb5bf; }
            QPushButton#nav:checked { background:#2b1b23; border-left:3px solid #d14c63; color:white; }
            QPushButton#primary { background:#cf4059; border:0; font-weight:700; padding:13px; }
            QPushButton#primary:hover { background:#e34e67; }
            QPushButton#danger { background:#3a2028; border-color:#8e4050; }
            QPlainTextEdit, QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QListWidget {
                background:#0e1116; color:#f3f4f6; border:1px solid #38404b; border-radius:8px; padding:8px;
                selection-background-color:#994152;
            }
            QPlainTextEdit:focus, QLineEdit:focus, QComboBox:focus { border-color:#d14c63; }
            QScrollArea { border:0; background:transparent; }
            QProgressBar { border:0; border-radius:5px; background:#303741; height:9px; text-align:center; }
            QProgressBar::chunk { background:#d64c64; border-radius:5px; }
            QSlider::groove:horizontal { height:6px; background:#303741; border-radius:3px; }
            QSlider::handle:horizontal { background:#e06378; width:14px; margin:-4px 0; border-radius:7px; }
            QCheckBox { color:#b6bec8; spacing:8px; }
        """)

    def show_page(self, index: int) -> None:
        self.pages.setCurrentIndex(index)
        self.heading.setText(("Генератор", "Предпросмотр", "Пакетная генерация", "Настройки", "Визуальный стиль", "Озвучка ElevenLabs", "Аудиозапись")[index])
        for number, button in enumerate(self.nav_buttons):
            button.setChecked(number == index)

    def _set_theme_colors(self, style: str) -> None:
        if style not in STYLES:
            return
        for button, key in ((self.foreground, "foreground"), (self.accent, "accent")):
            color = STYLES[style][key]
            button.setProperty("color", color)
            button.setText(color.upper())

    def pick_color(self, button: QPushButton) -> None:
        color = QColorDialog.getColor(QColor(button.property("color")), self, "Выберите цвет")
        if color.isValid():
            button.setProperty("color", color.name())
            button.setText(color.name().upper())

    def pick_font(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Выберите шрифт", "C:/Windows/Fonts", "Шрифты (*.ttf *.otf *.ttc)")
        if path:
            self.font_path.setText(path)

    def _select_visual_font(self, index: int) -> None:
        path = self.visual_font.itemData(index)
        if path:
            self.font_path.setText(path)
            self.font_sample.setFont(QFont(self.visual_font.currentText(), 18, QFont.Bold))

    def _update_background_swatch(self, _name: str = "") -> None:
        if not hasattr(self, "background_swatch"):
            return
        try:
            frame = render_background((480, 200), self.background_combo.currentText(), .2,
                                      self.background_intensity.value(), self.background_speed.value(),
                                      tint=self.background_tint.text().strip() if hasattr(self, "background_tint") else "")
            image = QImage(frame.tobytes(), frame.width, frame.height, frame.width * 3, QImage.Format_RGB888).copy()
            self.background_swatch.setPixmap(QPixmap.fromImage(image).scaled(
                self.background_swatch.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))
        except Exception:
            self.background_swatch.clear()

    def apply_visual_preset(self, name: str) -> None:
        preset = VISUAL_PRESETS.get(name)
        if not preset or not hasattr(self, "style_combo"):
            return
        self.style_combo.setCurrentText(preset["style"])
        self.animation_combo.setCurrentText(preset["animation"])
        self.background_combo.setCurrentText(preset["background"])
        self.foreground.setProperty("color", preset["foreground"]); self.foreground.setText(preset["foreground"])
        self.accent.setProperty("color", preset["accent"]); self.accent.setText(preset["accent"])
        self._set_theme_colors(self.style_combo.currentText())
        self.foreground.setProperty("color", preset["foreground"]); self.foreground.setText(preset["foreground"])
        self.accent.setProperty("color", preset["accent"]); self.accent.setText(preset["accent"])
        self.animation_in.setCurrentText("Fade In")
        self.animation_hold.setCurrentText("None")
        self.animation_out.setCurrentText("Fade Out")
        self.animation_in.setCurrentText(preset["animation_in"])
        self.animation_out.setCurrentText(preset["animation_out"])
        font_index = self.visual_font.findText(preset["font"])
        if font_index >= 0:
            self.visual_font.setCurrentIndex(font_index)
        if preset.get("font_file") and Path(preset["font_file"]).is_file():
            self.font_path.setText(preset["font_file"])
        self.visual_font_size.setValue(float(preset.get("font_size", 1)))
        self.visual_bold.setChecked(bool(preset.get("bold", preset["serif"])))
        self.visual_letter_spacing.setValue(float(preset.get("letter_spacing", .035)))
        self.visual_line_spacing.setValue(float(preset.get("line_spacing", 1.75)))
        self.visual_align.setCurrentIndex(self.visual_align.findData(preset.get("text_align", "center")))
        self.visual_shadow.setValue(float(preset.get("text_shadow", .3)))
        self.visual_outline.setValue(float(preset.get("text_outline", 0)))
        self.position_y.setValue(round(float(preset.get("position_y", 0)) * 100))
        self.background_intensity.setValue(float(preset.get("background_intensity", .25)))
        self.background_speed.setValue(float(preset.get("background_speed", 1)))
        self.animation_strength.setValue(float(preset.get("animation_intensity", 1)))
        self.animation_duration.setValue(float(preset.get("animation_duration", .35)))
        self.animation_speed.setValue(float(preset.get("animation_speed", 1)))
        self.background_brightness.setValue(float(preset.get("background_brightness", 0)))
        self.background_contrast.setValue(float(preset.get("background_contrast", 1)))
        if hasattr(self, "background_tint"):
            self.background_tint.setText(str(preset.get("background_tint", "")))
        self.animation_hold.setCurrentText(preset.get("animation_hold", "None"))
        easing_index = self.easing.findData(preset.get("easing", "Smooth"))
        if easing_index >= 0:
            self.easing.setCurrentIndex(easing_index)

    def reset_visual_settings(self) -> None:
        self.background_path.clear()
        self.visual_preset.setCurrentIndex(-1)
        self.visual_preset.setCurrentText("CLASSIC DARK")

    def save_visual_preset(self) -> None:
        name, accepted = QInputDialog.getText(self, "Сохранить пресет", "Название пресета:")
        name = name.strip()
        if not accepted or not name:
            return
        preset = dict(style=self.style_combo.currentText(), animation=self.animation_combo.currentText(),
                      background=self.background_combo.currentText(), foreground=self.foreground.property("color"),
                      accent=self.accent.property("color"), serif=False, font=self.visual_font.currentText(),
                      font_file=self.font_path.text().strip(), font_size=self.visual_font_size.value(),
                      bold=self.visual_bold.isChecked(), letter_spacing=self.visual_letter_spacing.value(),
                      line_spacing=self.visual_line_spacing.value(), text_align=self.visual_align.currentData(),
                      text_shadow=self.visual_shadow.value(), text_outline=self.visual_outline.value(),
                      background_intensity=self.background_intensity.value(), background_speed=self.background_speed.value(),
                      animation_intensity=self.animation_strength.value(), animation_duration=self.animation_duration.value(),
                      animation_speed=self.animation_speed.value(),
                      animation_in=self.animation_in.currentText(), animation_hold=self.animation_hold.currentText(),
                      animation_out=self.animation_out.currentText(), easing=self.easing.currentData(),
                      background_brightness=self.background_brightness.value(), background_contrast=self.background_contrast.value(),
                      background_tint=self.background_tint.text().strip(), position_y=self.position_y.value() / 100)
        VISUAL_PRESETS[name] = preset
        self.custom_preset_names.add(name)
        self.visual_settings_store.setValue("custom_presets", json.dumps(
            {key: VISUAL_PRESETS[key] for key in self.custom_preset_names}, ensure_ascii=False))
        if self.visual_preset.findText(name) < 0:
            self.visual_preset.addItem(name)
        self.visual_preset.setCurrentText(name)

    def pick_background(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Файл фона", str(Path.home()),
                                              "Фоны (*.jpg *.jpeg *.png *.webp *.bmp *.mp4 *.mov *.webm *.mkv *.avi)")
        if path:
            self.background_path.setText(path)

    def pick_background_tint(self) -> None:
        initial = QColor(self.background_tint.text().strip() or "#000000")
        color = QColorDialog.getColor(initial, self, "Оттенок фона")
        if color.isValid():
            self.background_tint.setText(color.name())

    def preview_visual_frame(self) -> None:
        self.anim_preview_frame = 0
        self.anim_preview_timer.start()

    def _preview_visual_tick(self) -> None:
        try:
            words = scenes("Своя история начинается сегодня")
            time_point = self.anim_preview_frame / 20
            frame = render_frame(words[0], (480, 270), self.style_combo.currentText(),
                                 self.animation_combo.currentText(), time_point, 1.4, 1.0,
                                 self.font_path.text().strip(), self.foreground.property("color"),
                                 self.accent.property("color"), background=self.background_combo.currentText(),
                                 background_intensity=self.background_intensity.value(),
                                 background_speed=self.background_speed.value(),
                                 animation_intensity=self.animation_strength.value(),
                                 animation_speed=self.animation_speed.value(),
                                 background_brightness=self.background_brightness.value(),
                                 background_contrast=self.background_contrast.value(),
                                 background_tint=self.background_tint.text().strip(),
                                 animation_in=self.animation_in.currentText(), animation_hold=self.animation_hold.currentText(),
                                 animation_out=self.animation_out.currentText())
            image = QImage(frame.tobytes(), frame.width, frame.height, frame.width * 3, QImage.Format_RGB888).copy()
            self.anim_preview_label.setPixmap(QPixmap.fromImage(image).scaled(self.anim_preview_label.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))
            self.anim_preview_frame += 1
            if self.anim_preview_frame > 28:
                self.anim_preview_timer.stop()
        except Exception as exc:
            self.anim_preview_timer.stop()
            QMessageBox.warning(self, "Предпросмотр", str(exc))

    def refresh_music(self, selected: Path | None = None) -> None:
        self.music_combo.clear()
        self.music_combo.addItem("Без музыки", None)
        for path in sorted(MUSIC.iterdir()):
            if path.suffix.lower() in {".mp3", ".wav"}:
                self.music_combo.addItem(path.name, str(path))
                if selected == path:
                    self.music_combo.setCurrentIndex(self.music_combo.count() - 1)

    def refresh_voices(self) -> None:
        previous = self.voice_combo.currentData()
        self.voice_combo.clear()
        try:
            voices = list_voices()
        except Exception as exc:
            voices = []
            self.voice_help.setText(f"Не удалось прочитать голоса Windows: {exc}")
        else:
            russian = [voice for voice in voices if voice.russian]
            self.voice_help.setText("Русский голос найден." if russian else RUSSIAN_VOICE_HELP)
        for voice in voices:
            language = "Русский" if voice.russian else voice.language
            self.voice_combo.addItem(f"{voice.name} · {language}", voice.id)
        if voices:
            index = self.voice_combo.findData(previous) if previous else -1
            self.voice_combo.setCurrentIndex(index if index >= 0 else 0)
        sapi_available = bool(voices)
        elevenlabs_available = bool(self.elevenlabs_credentials.api_key and
                                    (self.elevenlabs_voice_combo.currentData() or self.elevenlabs_credentials.voice_id))
        self.voice_combo.setEnabled(sapi_available)
        self.voice_enabled.setEnabled(sapi_available or elevenlabs_available)
        self.voice_test_button.setEnabled(sapi_available or elevenlabs_available)
        self._refresh_audio_voice_choices()
        if not voices and not elevenlabs_available and self.tts_provider.currentData() == "sapi":
            self.voice_enabled.setChecked(False)

    def _elevenlabs_options(self) -> dict:
        return {
            "model_id": self.elevenlabs_model.currentData(),
            "stability": self.el_stability.value(),
            "similarity_boost": self.el_similarity.value(),
            "style": self.el_style.value(),
            "speaker_boost": self.el_speaker_boost.isChecked(),
            "speed": self.el_speed.value(),
        }

    def _update_voice_estimate(self, *_args) -> None:
        if not hasattr(self, "el_estimate"):
            return
        if self.tts_provider.currentData() == "elevenlabs":
            text = self.quote.toPlainText() if hasattr(self, "quote") else ""
            count = estimate_characters(text)
            self.el_estimate.setText(f"Оценка: около {count} символов тарификации · фактический тариф зависит от плана и модели")
        else:
            self.el_estimate.setText("Оценка: локальный SAPI, без расхода API-кредитов")

    def check_elevenlabs_connection(self) -> None:
        if self.elevenlabs_check_thread and self.elevenlabs_check_thread.isRunning():
            return
        self.elevenlabs_status.setText("Проверка ключа, голоса и моделей (без генерации)…")
        thread = ElevenLabsCheckThread()
        self.elevenlabs_check_thread = thread
        thread.completed.connect(self._elevenlabs_connection_ready)
        thread.finished.connect(lambda: setattr(self, "elevenlabs_check_thread", None))
        thread.start()

    def refresh_elevenlabs_voices(self) -> None:
        if self.elevenlabs_voices_thread and self.elevenlabs_voices_thread.isRunning():
            return
        self.elevenlabs_status.setText("Загружаю доступные голоса аккаунта…")
        thread = ElevenLabsVoicesThread()
        self.elevenlabs_voices_thread = thread
        thread.completed.connect(self._elevenlabs_voices_ready)
        thread.finished.connect(lambda: setattr(self, "elevenlabs_voices_thread", None))
        thread.start()

    def _elevenlabs_voices_ready(self, result: dict) -> None:
        if "error" in result:
            self.elevenlabs_status.setText("ElevenLabs: " + result["error"])
            return
        selected = self.elevenlabs_voice_combo.currentData() or self.voice_settings_store.value(
            "elevenlabs_voice_id", self.elevenlabs_credentials.voice_id)
        self.elevenlabs_voice_combo.clear()
        for voice in result["voices"]:
            category = f" · {voice['category']}" if voice["category"] else ""
            self.elevenlabs_voice_combo.addItem(voice["name"] + category, voice["id"])
        idx = self.elevenlabs_voice_combo.findData(selected)
        if idx >= 0:
            self.elevenlabs_voice_combo.setCurrentIndex(idx)
        elif self.elevenlabs_voice_combo.count():
            self.elevenlabs_voice_combo.setCurrentIndex(0)
        self.elevenlabs_status.setText(f"Загружено голосов аккаунта: {self.elevenlabs_voice_combo.count()} · без генерации аудио")
        available = bool(self.elevenlabs_credentials.api_key and self.elevenlabs_voice_combo.count())
        sapi_available = bool(self.voice_combo.count())
        self.voice_enabled.setEnabled(available or sapi_available)
        self.voice_test_button.setEnabled(available or sapi_available)
        self._refresh_audio_voice_choices()

    def _elevenlabs_connection_ready(self, result: dict) -> None:
        if "error" in result:
            self.elevenlabs_status.setText("ElevenLabs: " + result["error"])
            self.elevenlabs_voice_name.setText("Голос: недоступен")
            return
        status = result["result"]
        self.elevenlabs_status.setText("Подключение ElevenLabs подтверждено · проверка без расхода символов")
        self.elevenlabs_voice_name.setText(f"Голос: {status['voice_name']} · Voice ID из локального файла настроек")
        models = status["models"]
        unsupported = [name for name, model_id in MODELS.items() if not models.get(model_id)]
        if unsupported:
            self.elevenlabs_status.setText("API подключён; недоступны модели: " + ", ".join(unsupported))

    def _confirm_elevenlabs_cost(self, texts: list[str], action: str) -> bool:
        if not self.voice_enabled.isChecked() or self.tts_provider.currentData() != "elevenlabs":
            return True
        selected_voice_id = self.elevenlabs_voice_combo.currentData() or self.elevenlabs_credentials.voice_id
        if not self.elevenlabs_credentials.api_key or not selected_voice_id:
            QMessageBox.warning(self, "Настройте ElevenLabs",
                                "Нужны ELEVENLABS_API_KEY и ELEVENLABS_VOICE_ID в .env.txt, API_KEY.env или .env.")
            return False
        options = self._elevenlabs_options()
        model_id = options.pop("model_id")
        misses = [text for text in texts if not has_cached_audio(text, selected_voice_id,
                                                                  model_id, options)]
        if not misses:
            return True
        chars = sum(estimate_characters(text) for text in misses)
        answer = QMessageBox.question(
            self, "Подтвердите запрос ElevenLabs",
            f"{action}: {len(misses)} новых озвучек, около {chars} тарифицируемых символов.\n"
            "Повторные озвучки будут взяты из кэша. Продолжить и отправить запросы ElevenLabs?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        return answer == QMessageBox.Yes

    def preview_voice(self) -> None:
        if ((self.voice_preview_thread and self.voice_preview_thread.isRunning()) or
                (self.elevenlabs_preview_thread and self.elevenlabs_preview_thread.isRunning())):
            return
        if self.tts_provider.currentData() == "elevenlabs":
            test_text = "Пока ты ищешь оправдания, другие уже зарабатывают. Время не ждёт никого"
            if not self._confirm_elevenlabs_cost([test_text], "Тестовая озвучка"):
                return
            self._release_voice_preview()
            self.voice_status.setText("Создаётся тест ElevenLabs в фоновом потоке…")
            self.voice_test_button.setEnabled(False)
            options = self._elevenlabs_options()
            options["voice_id"] = self.elevenlabs_voice_combo.currentData() or self.elevenlabs_credentials.voice_id
            worker = ElevenLabsPreviewThread(options)
            self.elevenlabs_preview_thread = worker
            worker.completed.connect(self._elevenlabs_preview_ready)
            worker.finished.connect(lambda: self._elevenlabs_preview_finished(worker))
            worker.start()
            return
        voice_id = self.voice_combo.currentData()
        if not voice_id:
            self.voice_status.setText(RUSSIAN_VOICE_HELP)
            return
        self._release_voice_preview()
        self.voice_status.setText("Создаётся тестовая озвучка…")
        self.voice_test_button.setEnabled(False)
        worker = VoicePreviewThread(voice_id, self.voice_speed.value(), self.voice_volume.value(),
                                    self.voice_preset.currentText(), self.voice_reverb.value())
        self.voice_preview_thread = worker
        worker.completed.connect(self._voice_preview_ready)
        worker.finished.connect(lambda: self._voice_preview_finished(worker))
        worker.start()

    def _voice_preview_ready(self, result: dict) -> None:
        if "error" in result:
            self.voice_status.setText("Ошибка озвучки: " + result["error"])
            return
        self.voice_preview_path = result["path"]
        self.voice_player.setSource(QUrl.fromLocalFile(str(self.voice_preview_path)))
        self.voice_audio_output.setVolume(self.voice_volume.value() / 100)
        self.voice_player.play()
        self.voice_status.setText("Воспроизведение тестовой озвучки")

    def _elevenlabs_preview_ready(self, result: dict) -> None:
        if "error" in result:
            self.voice_status.setText("Ошибка ElevenLabs: " + result["error"])
            return
        self.voice_player.setSource(QUrl.fromLocalFile(result["path"]))
        self.voice_audio_output.setVolume(self.el_volume.value() / 100)
        self.voice_player.play()
        origin = "из кэша" if result["cached"] else "новая генерация"
        timing = "· оценочное выравнивание" if result["fallback"] else "· тайминг API"
        self.voice_status.setText(f"Тест ElevenLabs · {origin} · {result['seconds']:.1f} с {timing} · output/voice_test.mp3")

    def _elevenlabs_preview_finished(self, worker: ElevenLabsPreviewThread) -> None:
        if self.elevenlabs_preview_thread is worker:
            self.elevenlabs_preview_thread = None
        self.voice_test_button.setEnabled(bool(self.voice_combo.count() or
                                                (self.elevenlabs_credentials.api_key and
                                                 (self.elevenlabs_voice_combo.currentData() or self.elevenlabs_credentials.voice_id))))

    def _voice_preview_finished(self, worker: VoicePreviewThread) -> None:
        if self.voice_preview_thread is worker:
            self.voice_preview_thread = None
        self.voice_test_button.setEnabled(bool(self.voice_combo.count()))
        worker.deleteLater()

    def _release_voice_preview(self) -> None:
        self.voice_player.stop()
        self.voice_player.setSource(QUrl())
        if self.voice_preview_path:
            try:
                self.voice_preview_path.unlink(missing_ok=True)
            except PermissionError:
                pass  # Windows may release the decoder handle after the event loop exits.
            self.voice_preview_path = None

    def add_music(self) -> None:
        source, _ = QFileDialog.getOpenFileName(self, "Выберите музыку", str(Path.home()), "Аудио (*.mp3 *.wav)")
        if not source:
            return
        path = Path(source)
        target = MUSIC / f"{uuid.uuid4().hex[:8]}_{path.name}"
        try:
            shutil.copy2(path, target)
            self.refresh_music(target)
        except Exception as exc:
            QMessageBox.critical(self, "Ошибка загрузки музыки", str(exc))

    def _options(self) -> dict:
        music = self.music_combo.currentData()
        font = self.font_path.text().strip()
        if font and not Path(font).is_file():
            raise ValueError("Выбранный шрифт не найден.")
        provider = self.tts_provider.currentData()
        elevenlabs = self._elevenlabs_options()
        return dict(
            style=self.style_combo.currentText(), animation=self.animation_combo.currentText(),
            format_name=self.format_combo.currentText(), fps=int(self.fps.currentText()),
            speed=self.speed.value(), music=Path(music) if music else None,
            volume=self.volume.value(), fade=self.fade.value(),
            duration=self.duration.value() or None, font_override=font,
            foreground=self.foreground.property("color"), accent=self.accent.property("color"),
            preview=self.quick_preview.isChecked(), delay=self.delay.value(),
            opacity=self.opacity.value(), scale=self.scale.value(),
            position_x=self.position_x.value() / 100, position_y=self.position_y.value() / 100,
            sync_mode=self.sync_mode.currentText(),
            background=self.background_combo.currentText(),
            background_intensity=self.background_intensity.value(),
            background_speed=self.background_speed.value(),
            background_brightness=self.background_brightness.value(),
            background_contrast=self.background_contrast.value(),
            background_tint=self.background_tint.text().strip(),
            background_path=self.background_path.text().strip(),
            animation_intensity=self.animation_strength.value(),
            animation_duration=self.animation_duration.value(),
            animation_speed=self.animation_speed.value(),
            easing=self.easing.currentData(),
            animation_in=self.animation_in.currentText(),
            animation_hold=self.animation_hold.currentText(),
            animation_out=self.animation_out.currentText(),
            font_size=self.visual_font_size.value(),
            letter_spacing=self.visual_letter_spacing.value(),
            line_spacing=self.visual_line_spacing.value(),
            text_align=self.visual_align.currentData(),
            bold=self.visual_bold.isChecked(),
            text_shadow=self.visual_shadow.value(),
            text_outline=self.visual_outline.value(),
            narration=self.voice_enabled.isChecked(), tts_provider=provider,
            voice_id=((self.elevenlabs_voice_combo.currentData() or self.elevenlabs_credentials.voice_id)
                      if provider == "elevenlabs" else (self.voice_combo.currentData() or "")),
            voice_speed=elevenlabs["speed"] if provider == "elevenlabs" else self.voice_speed.value(),
            voice_volume=self.el_volume.value() if provider == "elevenlabs" else self.voice_volume.value(),
            voice_preset=self.voice_preset.currentText(), voice_reverb=self.voice_reverb.value(),
            phrase_pause=self.phrase_pause.value(),
            elevenlabs_model=elevenlabs["model_id"],
            elevenlabs_stability=elevenlabs["stability"],
            elevenlabs_similarity=elevenlabs["similarity_boost"],
            elevenlabs_style=elevenlabs["style"],
            elevenlabs_speaker_boost=elevenlabs["speaker_boost"],
        )

    def start_single(self) -> None:
        if self.current_thread and self.current_thread.isRunning():
            return
        try:
            text = self.quote.toPlainText().strip()
            if not text:
                raise ValueError("Введите текст цитаты.")
            options = self._options()
        except Exception as exc:
            QMessageBox.warning(self, "Проверьте настройки", str(exc))
            return
        if not self._confirm_elevenlabs_cost([text], "Генерация видео"):
            return
        self.single_status.setText("Подготовка…")
        self.single_progress.setValue(0)
        self.single_result.setText("")
        self.generate_button.setEnabled(False)
        self.batch_start_button.setEnabled(False)
        self._start_worker(RenderThread(text=text, options=options), batch=False)

    def pick_batch_file(self) -> None:
        source, _ = QFileDialog.getOpenFileName(self, "Список цитат", str(Path.home()), "Цитаты (*.txt *.csv *.json)")
        if not source:
            return
        try:
            content = Path(source).read_text(encoding="utf-8-sig")
            self.batch_quotes = parse_quotes(source, content)
            self.batch_file_label.setText(f"{Path(source).name} · {len(self.batch_quotes)} цитат")
        except Exception as exc:
            self.batch_quotes = []
            QMessageBox.warning(self, "Ошибка файла", str(exc))

    def start_batch(self) -> None:
        if self.current_thread and self.current_thread.isRunning():
            return
        if not self.batch_quotes:
            QMessageBox.warning(self, "Список пуст", "Выберите файл с цитатами.")
            return
        try:
            options = self._options()
        except Exception as exc:
            QMessageBox.warning(self, "Проверьте настройки", str(exc))
            return
        if not self._confirm_elevenlabs_cost(self.batch_quotes, "Пакетная генерация"):
            return
        self.batch_files.clear()
        self.batch_progress.setValue(0)
        self.batch_status.setText("Подготовка…")
        self.report_button.setEnabled(False)
        self.batch_start_button.setEnabled(False)
        self.generate_button.setEnabled(False)
        self.batch_stop_button.setEnabled(True)
        mode = self.batch_style_mode.currentIndex()
        presets = [self.batch_presets.item(i).text() for i in range(self.batch_presets.count())
                   if self.batch_presets.item(i).checkState() == Qt.Checked]
        if mode in (1, 2) and not presets:
            QMessageBox.warning(self, "Пакетная генерация", "Выберите хотя бы один визуальный пресет.")
            self.batch_start_button.setEnabled(True)
            self.generate_button.setEnabled(True)
            self.batch_stop_button.setEnabled(False)
            return
        options["batch_style_mode"] = mode
        options["batch_presets"] = presets
        self._start_worker(RenderThread(quotes=self.batch_quotes.copy(), options=options,
                                        random_music=self.random_music.isChecked()), batch=True)

    def _start_worker(self, worker: RenderThread, *, batch: bool) -> None:
        self.current_thread = worker
        worker.progress.connect(lambda value, message: self._on_progress(value, message, batch))
        worker.completed.connect(lambda result: self._on_completed(result, batch))
        worker.finished.connect(lambda: self._worker_finished(worker))
        worker.start()

    def _on_progress(self, value: float, message: str, batch: bool) -> None:
        (self.batch_progress if batch else self.single_progress).setValue(round(value * 100))
        (self.batch_status if batch else self.single_status).setText(message)

    def _on_completed(self, result: dict, batch: bool) -> None:
        self.batch_stop_button.setEnabled(False)
        status = {"done": "Готово", "stopped": "Остановлено", "error": "Ошибка"}[result["state"]]
        files = result["files"]
        if batch:
            self.batch_status.setText(f"{status} · готово файлов: {len(files)}")
            for path in files:
                self.batch_files.addItem(path.name)
                self.batch_files.item(self.batch_files.count() - 1).setData(Qt.UserRole, str(path))
            self.report_path = result["report"]
            self.report_button.setEnabled(bool(self.report_path))
        else:
            self.single_status.setText(status)
            if files:
                self.single_result.setText(str(files[0]))
                self.load_video(files[0])
                self.show_page(1)
        if result["errors"]:
            QMessageBox.warning(self, "Ошибки генерации", "\n".join(result["errors"][:10]))

    def _worker_finished(self, worker: RenderThread) -> None:
        if self.current_thread is worker:
            self.current_thread = None
        self.generate_button.setEnabled(True)
        self.batch_start_button.setEnabled(True)
        worker.deleteLater()

    def stop_batch(self) -> None:
        if self.current_thread:
            self.current_thread.cancel_event.set()
            self.batch_status.setText("Остановка после текущего кадра…")
            self.batch_stop_button.setEnabled(False)

    def load_video(self, path: Path) -> None:
        self.video_path = path
        self.player.stop()
        self.player.setSource(QUrl.fromLocalFile(str(path.resolve())))
        self.preview_status.setText(str(path))
        self.show_page(1)
        self.player.play()
        self.play_button.setText("Пауза")

    def open_video_dialog(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Открыть MP4", str(OUTPUT), "Видео MP4 (*.mp4)")
        if path:
            self.load_video(Path(path))

    def show_video_file(self) -> None:
        if self.video_path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.video_path.parent)))

    def toggle_playback(self) -> None:
        if not self.video_path:
            return
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()
            self.play_button.setText("Воспроизвести")
        else:
            self.player.play()
            self.play_button.setText("Пауза")

    def _playback_changed(self, state) -> None:
        self.play_button.setText("Пауза" if state == QMediaPlayer.PlayingState else "Воспроизвести")

    def _position_changed(self, position: int) -> None:
        if not self.seek.isSliderDown():
            self.seek.setValue(position)
        self.time_label.setText(f"{position // 60000}:{position // 1000 % 60:02d} / {self.player.duration() // 60000}:{self.player.duration() // 1000 % 60:02d}")

    def _duration_changed(self, duration: int) -> None:
        self.seek.setRange(0, duration)

    def _player_error(self, error, description: str) -> None:
        if error != QMediaPlayer.NoError:
            self.preview_status.setText(f"Ошибка воспроизведения: {description}")

    def pick_ffmpeg(self) -> None:
        initial = str(Path.home())
        try:
            current = find_ffmpeg()
            initial = str(current.parent)
        except (FileNotFoundError, OSError):
            pass
        path, _ = QFileDialog.getOpenFileName(self, "Выберите FFmpeg", initial,
                                              "FFmpeg (ffmpeg.exe);;Все файлы (*.*)")
        if not path:
            return
        try:
            executable = save_ffmpeg_path(path)
            self.engine_label.setText(f"Проверка кодека: {executable.name}…")
            self.engine_label.setText(f"H.264: {find_encoder(executable)}")
            QMessageBox.information(self, "FFmpeg настроен",
                                    "Путь сохранён. Теперь запустите генерацию ещё раз.")
        except Exception as exc:
            self.engine_label.setText(f"FFmpeg: {exc}")
            QMessageBox.warning(self, "Не удалось настроить FFmpeg", str(exc))

    def open_output(self) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(OUTPUT)))

    def open_report(self) -> None:
        if getattr(self, "report_path", None):
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.report_path)))

    def closeEvent(self, event) -> None:
        if self.audio_export_thread and self.audio_export_thread.isRunning():
            self.audio_export_thread.cancel_event.set()
            if not self.audio_export_thread.wait(5000):
                QMessageBox.information(self, "Аудиозапись", "Дождитесь остановки обработки аудио и закройте окно снова.")
                event.ignore()
                return
        if self.current_thread and self.current_thread.isRunning():
            self.current_thread.cancel_event.set()
            if not self.current_thread.wait(5000):
                QMessageBox.information(self, "Генерация", "Дождитесь остановки генерации и закройте окно снова.")
                event.ignore()
                return
        if self.voice_preview_thread and self.voice_preview_thread.isRunning():
            if not self.voice_preview_thread.wait(5000):
                QMessageBox.information(self, "Озвучка", "Дождитесь завершения тестовой озвучки и закройте окно снова.")
                event.ignore()
                return
        if self.elevenlabs_preview_thread and self.elevenlabs_preview_thread.isRunning():
            if not self.elevenlabs_preview_thread.wait(5000):
                QMessageBox.information(self, "Озвучка", "Дождитесь завершения тестовой озвучки и закройте окно снова.")
                event.ignore()
                return
        if self.elevenlabs_check_thread and self.elevenlabs_check_thread.isRunning():
            if not self.elevenlabs_check_thread.wait(12000):
                QMessageBox.information(self, "Проверка ElevenLabs", "Дождитесь завершения проверки подключения и закройте окно снова.")
                event.ignore()
                return
        if self.elevenlabs_voices_thread and self.elevenlabs_voices_thread.isRunning():
            if not self.elevenlabs_voices_thread.wait(12000):
                QMessageBox.information(self, "Голоса ElevenLabs", "Дождитесь загрузки списка голосов и закройте окно снова.")
                event.ignore()
                return
        if not self._smoke_mode:
            self.voice_settings_store.setValue("enabled", self.voice_enabled.isChecked())
            self.voice_settings_store.setValue("provider", self.tts_provider.currentData())
            self.voice_settings_store.setValue("elevenlabs_voice_id", self.elevenlabs_voice_combo.currentData() or "")
            self.voice_settings_store.setValue("model", self.elevenlabs_model.currentData())
            self.voice_settings_store.setValue("stability", self.el_stability.value())
            self.voice_settings_store.setValue("similarity", self.el_similarity.value())
            self.voice_settings_store.setValue("style", self.el_style.value())
            self.voice_settings_store.setValue("speed", self.el_speed.value())
            self.voice_settings_store.setValue("el_volume", self.el_volume.value())
            self.voice_settings_store.setValue("speaker_boost", self.el_speaker_boost.isChecked())
            self.audio_settings_store.setValue("text", self.audio_text.toPlainText())
            self.audio_settings_store.setValue("provider", self.audio_provider.currentData())
            self.audio_settings_store.setValue("voice_id", self.audio_voice_combo.currentData() or "")
            self.audio_settings_store.setValue("speed", self.audio_speed.value())
            self.audio_settings_store.setValue("voice_volume", self.audio_voice_volume.value())
            self.audio_settings_store.setValue("preset", self.audio_preset.currentText())
            self.audio_settings_store.setValue("reverb", self.audio_reverb.value())
            self.audio_settings_store.setValue("pause", self.audio_pause.value())
            self.audio_settings_store.setValue("format", self.audio_format.currentData())
            self.audio_settings_store.setValue("music", self.audio_music_path.text().strip())
            self.audio_settings_store.setValue("music_volume", self.audio_music_volume.value())
            self.audio_settings_store.setValue("duck", self.audio_duck.isChecked())
            self.audio_settings_store.setValue("loop_music", self.audio_loop_music.isChecked())
        self._release_voice_preview()
        self.player.stop()
        self.audio_player.stop()
        self.audio_player.setSource(QUrl())
        if self.audio_preview_path:
            try:
                self.audio_preview_path.unlink(missing_ok=True)
            except PermissionError:
                pass
        super().closeEvent(event)


def main() -> int:
    if "--verify-ui" in sys.argv:
        app = QApplication([sys.argv[0]])
        app.setApplicationName("AUTO SHORTS STUDIO")
        app.setStyle("Fusion")
        initialize_emoji_font()
        window = Studio()
        window._smoke_mode = True
        window.show()
        app.processEvents()
        try:
            from elevenlabs import ElevenLabs, VoiceSettings
            sdk_imported = bool(ElevenLabs and VoiceSettings)
        except Exception:
            sdk_imported = False
        result = {"ok": window.pages.count() == 7 and sdk_imported and window.ffmpeg_button.isVisible(),
                  "title": window.windowTitle(), "pages": window.pages.count(),
                  "provider": window.tts_provider.currentData(),
                  "voice_enabled": window.voice_enabled.isChecked(), "elevenlabs_sdk": sdk_imported,
                  "ffmpeg_status": window.engine_label.text(),
                  "ffmpeg_picker": window.ffmpeg_button.text(),
                  "font_choices": window.visual_font.count()}
        window.close()
        OUTPUT.mkdir(parents=True, exist_ok=True)
        (OUTPUT / ".packaged_ui_check.json").write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        return 0 if result["ok"] else 1
    if "--verify-local-voice" in sys.argv:
        report = OUTPUT / ".packaged_voice_check.json"
        try:
            voices = [voice for voice in list_voices() if voice.russian]
            if not voices:
                raise RuntimeError(RUSSIAN_VOICE_HELP)
            mp4 = render_video("Пока ты ищешь мотивацию, кто-то уже ищет клиентов",
                               preview=True, narration=True, voice_id=voices[0].id)
            report.write_text(json.dumps({"ok": True, "voice": voices[0].name,
                                          "mp4": str(mp4)}, ensure_ascii=False), encoding="utf-8")
            return 0
        except Exception as exc:
            report.write_text(json.dumps({"ok": False, "error": str(exc)},
                                         ensure_ascii=False), encoding="utf-8")
            return 1
    app = QApplication(sys.argv)
    app.setApplicationName("AUTO SHORTS STUDIO")
    app.setStyle("Fusion")
    initialize_emoji_font()
    QFontDatabase.addApplicationFont("C:/Windows/Fonts/arial.ttf")
    app.setFont(QFont("Arial", 10))
    window = Studio()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
