"""Offline Windows SAPI narration and phrase-aligned audio preparation."""
from __future__ import annotations

import subprocess
import wave
import sys
if sys.platform == "win32":
    import winreg
else:  # SAPI is available only on desktop Windows.
    winreg = None
from dataclasses import dataclass
from pathlib import Path
from threading import Event

import numpy as np

from config import find_ffmpeg
from text_processor import Word, apply_auto_accent, scenes


VOICE_REGISTRY = r"SOFTWARE\Microsoft\Speech\Voices\Tokens"
PRESETS = ("Базовый", "Dramatic Narrator")
RUSSIAN_VOICE_HELP = (
    "Русский голос SAPI не найден. Откройте Параметры Windows → Время и язык → "
    "Язык и регион → Добавить язык → Русский, установите компонент «Речь», "
    "затем перезапустите AUTO SHORTS STUDIO."
)


@dataclass(frozen=True)
class VoiceInfo:
    id: str
    name: str
    language: str

    @property
    def russian(self) -> bool:
        return any(part.strip().lower() in {"419", "0419"} for part in self.language.split(";"))


@dataclass(frozen=True)
class PhraseSegment:
    words: list[Word]
    start: float
    speech_seconds: float
    scene_seconds: float


@dataclass(frozen=True)
class NarrationTrack:
    path: Path
    segments: list[PhraseSegment]
    seconds: float


def list_voices() -> list[VoiceInfo]:
    """Read SAPI desktop tokens and verify that each token loads through COM."""
    if winreg is None:
        return []
    import comtypes
    import comtypes.client

    comtypes.CoInitialize()
    voices: list[VoiceInfo] = []
    try:
        for hive, prefix in ((winreg.HKEY_LOCAL_MACHINE, "HKEY_LOCAL_MACHINE"),
                             (winreg.HKEY_CURRENT_USER, "HKEY_CURRENT_USER")):
            try:
                key = winreg.OpenKey(hive, VOICE_REGISTRY)
            except FileNotFoundError:
                continue
            with key:
                for index in range(winreg.QueryInfoKey(key)[0]):
                    name = winreg.EnumKey(key, index)
                    voice_id = f"{prefix}\\{VOICE_REGISTRY}\\{name}"
                    try:
                        token = comtypes.client.CreateObject("SAPI.SpObjectToken", dynamic=True)
                        token.SetId(voice_id, "", False)
                        voices.append(VoiceInfo(voice_id, token.GetDescription(), token.GetAttribute("Language")))
                    except Exception:
                        continue
    finally:
        comtypes.CoUninitialize()
    return sorted(voices, key=lambda item: (not item.russian, item.name.casefold()))


def phrase_groups(text: str) -> list[list[Word]]:
    """Keep existing sentence boundaries and split longer scenes at punctuation."""
    groups: list[list[Word]] = []
    for group in scenes(text):
        start = 0
        for index, word in enumerate(group):
            if word.text.endswith((",", ";", ":", "—")) and index - start >= 2 and len(group) - index >= 3:
                groups.append(apply_auto_accent(group[start:index + 1]))
                start = index + 1
        if start < len(group):
            groups.append(apply_auto_accent(group[start:]))
    return groups


def _speak_phrases(groups: list[list[Word]], voice_id: str, destination: Path, *, cancel: Event | None) -> list[Path]:
    import comtypes
    import comtypes.client

    comtypes.CoInitialize()
    output: list[Path] = []
    try:
        voice = comtypes.client.CreateObject("SAPI.SpVoice", dynamic=True)
        token = comtypes.client.CreateObject("SAPI.SpObjectToken", dynamic=True)
        token.SetId(voice_id, "", False)
        voice.Voice = token
        voice.Rate = 0
        voice.Volume = 100
        for index, words in enumerate(groups):
            if cancel and cancel.is_set():
                raise InterruptedError("Генерация остановлена.")
            path = destination / f"raw_{index:03}.wav"
            stream = comtypes.client.CreateObject("SAPI.SpFileStream", dynamic=True)
            stream.Open(str(path), 3, False)  # SSFMCreateForWrite
            try:
                voice.AudioOutputStream = stream
                voice.Speak(" ".join(word.text for word in words), 0)
            finally:
                stream.Close()
            output.append(path)
    finally:
        comtypes.CoUninitialize()
    return output


def _process_phrase(source: Path, destination: Path, *, speed: float, volume: int,
                    preset: str, reverb: float, ffmpeg: Path) -> float:
    if not 0.7 <= speed <= 1.5 or not 0 <= volume <= 100 or not 0 <= reverb <= 0.35:
        raise ValueError("Параметры озвучки вне допустимого диапазона.")
    if preset not in PRESETS:
        raise ValueError("Неизвестный пресет озвучки.")
    filters = [f"atempo={speed:.3f}"]
    if preset == "Dramatic Narrator":
        filters += ["highpass=f=70", "bass=g=3:f=180", "equalizer=f=3000:t=q:w=1.2:g=2",
                    "acompressor=threshold=0.18:ratio=2.2:attack=25:release=220"]
    if reverb:
        filters.append(f"aecho=0.85:0.75:45:{0.04 + reverb * 0.45:.3f}")
    filters += ["loudnorm=I=-18:TP=-2:LRA=8", f"volume={volume / 100:.3f}"]
    command = [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
               "-af", ",".join(filters), "-ac", "1", "-ar", "22050", "-c:a", "pcm_s16le", str(destination)]
    done = subprocess.run(command, capture_output=True, text=True, errors="replace")
    if done.returncode:
        raise RuntimeError("Ошибка обработки озвучки FFmpeg: " + done.stderr[-1200:])
    _trim_quiet_edges(destination)
    with wave.open(str(destination), "rb") as audio:
        if audio.getnchannels() != 1 or audio.getsampwidth() != 2 or audio.getframerate() != 22050:
            raise RuntimeError("Неверный формат WAV после обработки голоса.")
        return audio.getnframes() / audio.getframerate()


def _trim_quiet_edges(path: Path) -> None:
    """Remove SAPI's long silent padding while retaining margins around speech."""
    with wave.open(str(path), "rb") as audio:
        params = audio.getparams()
        pcm = audio.readframes(audio.getnframes())
    samples = np.frombuffer(pcm, dtype="<i2")
    hop = 441  # 20 ms at 22.05 kHz
    count = len(samples) // hop
    if count < 2:
        return
    blocks = samples[:count * hop].astype(np.float32).reshape(count, hop) / 32768.0
    rms = np.sqrt(np.mean(blocks * blocks, axis=1))
    active = np.flatnonzero(rms >= 0.0025)
    if len(active) == 0:
        return
    first = max(0, int(active[0]) * hop - round(0.08 * 22050))
    last = min(len(samples), (int(active[-1]) + 1) * hop + round(0.15 * 22050))
    if first == 0 and last == len(samples):
        return
    trimmed = path.with_name(path.stem + "_trimmed.wav")
    with wave.open(str(trimmed), "wb") as output:
        output.setparams(params)
        output.writeframes(samples[first:last].tobytes())
    trimmed.replace(path)


def prepare_narration(text: str, voice_id: str, destination: Path, *, speed: float = 0.82,
                      volume: int = 100, preset: str = "Dramatic Narrator", reverb: float = 0.08,
                      pause: float = 0.35, cancel: Event | None = None) -> NarrationTrack:
    if not 0.25 <= pause <= 1.2:
        raise ValueError("Пауза между фразами должна быть от 0,25 до 1,2 с.")
    voices = {voice.id: voice for voice in list_voices()}
    selected = voices.get(voice_id)
    if selected is None:
        raise ValueError("Выбранный голос Windows больше не доступен. Обновите список голосов.")
    if any("А" <= letter.upper() <= "Я" for letter in text) and not selected.russian:
        raise ValueError(RUSSIAN_VOICE_HELP)
    destination.mkdir(parents=True, exist_ok=True)
    groups = phrase_groups(text)
    raw = _speak_phrases(groups, voice_id, destination, cancel=cancel)
    ffmpeg = find_ffmpeg()
    processed: list[Path] = []
    durations: list[float] = []
    for index, source in enumerate(raw):
        if cancel and cancel.is_set():
            raise InterruptedError("Генерация остановлена.")
        path = destination / f"processed_{index:03}.wav"
        durations.append(_process_phrase(source, path, speed=speed, volume=volume,
                                         preset=preset, reverb=reverb, ffmpeg=ffmpeg))
        processed.append(path)
    combined = destination / "narration.wav"
    segments: list[PhraseSegment] = []
    frame_position = 0
    silence_frames = round(pause * 22050)
    with wave.open(str(combined), "wb") as output:
        output.setparams((1, 2, 22050, 0, "NONE", "not compressed"))
        for words, path, seconds in zip(groups, processed, durations):
            with wave.open(str(path), "rb") as source:
                audio_frames = source.getnframes()
                segments.append(PhraseSegment(words, frame_position / 22050, seconds,
                                              (audio_frames + silence_frames) / 22050))
                output.writeframes(source.readframes(audio_frames))
                output.writeframes(b"\0\0" * silence_frames)
                frame_position += audio_frames + silence_frames
    return NarrationTrack(combined, segments, frame_position / 22050)
