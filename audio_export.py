"""Audio-only narration export for AUTO SHORTS STUDIO."""
from __future__ import annotations

import re
import subprocess
import tempfile
import wave
from pathlib import Path
from threading import Event
from typing import Callable

from config import OUTPUT, find_ffmpeg
from elevenlabs_tts import synthesize
from narration import prepare_narration
from elevenlabs_tts import clean_text


Progress = Callable[[float, str], None]
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?…])\s+|[\r\n]+")
_AUDIO_NAME = re.compile(r"^Аудиозапись (\d+)\.(?:wav|mp3)$", re.IGNORECASE)


def split_phrases(text: str) -> list[str]:
    speech = clean_text(text)
    if not speech:
        raise ValueError("Введите текст для озвучки.")
    return [part.strip() for part in _SENTENCE_SPLIT.split(speech) if part.strip()]


def next_audio_path(output: Path = OUTPUT, extension: str = "mp3") -> Path:
    extension = extension.lower().lstrip(".")
    if extension not in {"mp3", "wav"}:
        raise ValueError("Формат аудио должен быть MP3 или WAV.")
    existing = [int(match.group(1)) for path in output.iterdir()
                if path.is_file() and (match := _AUDIO_NAME.match(path.name))]
    number = max(existing, default=0) + 1
    return output / f"Аудиозапись {number}.{extension}"


def _run(command: list[str], cancel: Event | None = None) -> None:
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    while True:
        try:
            _stdout, stderr = process.communicate(timeout=0.2)
            break
        except subprocess.TimeoutExpired:
            if cancel and cancel.is_set():
                process.terminate()
                try:
                    _stdout, stderr = process.communicate(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    _stdout, stderr = process.communicate()
                raise InterruptedError("Создание аудиозаписи остановлено.")
    if process.returncode:
        detail = stderr.decode("utf-8", errors="replace")[-1400:]
        raise RuntimeError("Ошибка FFmpeg при обработке аудио: " + detail)


def _cancelled(cancel: Event | None) -> None:
    if cancel and cancel.is_set():
        raise InterruptedError("Создание аудиозаписи остановлено.")


def _media_duration(ffmpeg: Path, path: Path) -> float:
    result = subprocess.run([str(ffmpeg), "-hide_banner", "-i", str(path)],
                            capture_output=True, text=True, errors="replace", timeout=30)
    match = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", result.stderr)
    if not match:
        raise RuntimeError(f"Не удалось определить длительность аудиофайла: {path.name}")
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def _write_silence(path: Path, seconds: float) -> None:
    frames = round(max(0.0, seconds) * 22050)
    with wave.open(str(path), "wb") as stream:
        stream.setparams((1, 2, 22050, frames, "NONE", "not compressed"))
        stream.writeframes(b"\0\0" * frames)


def _ffmpeg_filters(preset: str, reverb: float, volume: int) -> str:
    filters: list[str] = []
    if preset == "Dramatic Narrator":
        filters += ["highpass=f=70", "bass=g=3:f=180",
                    "equalizer=f=3000:t=q:w=1.2:g=2",
                    "acompressor=threshold=0.18:ratio=2.2:attack=25:release=220"]
    if reverb:
        filters.append(f"aecho=0.85:0.75:45:{0.04 + reverb * 0.45:.3f}")
    filters += ["loudnorm=I=-18:TP=-2:LRA=8", f"volume={max(0, min(volume, 100)) / 100:.3f}"]
    return ",".join(filters)


def _elevenlabs_voice(text: str, folder: Path, *, options: dict, voice_id: str,
                      pause: float, preset: str, reverb: float, volume: int,
                      cancel: Event | None, progress: Progress | None) -> tuple[Path, float]:
    ffmpeg = find_ffmpeg()
    phrases = split_phrases(text)
    concat_paths: list[Path] = []
    audio_options = dict(options)
    model_id = audio_options.pop("model_id")
    for index, phrase in enumerate(phrases):
        _cancelled(cancel)
        source = folder / f"phrase_{index + 1:03}.mp3"
        synthesize(phrase, source, model_id=model_id, voice_id=voice_id, **audio_options)
        processed = folder / f"phrase_{index + 1:03}.wav"
        command = [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
                   "-af", _ffmpeg_filters(preset, reverb, volume), "-ac", "1", "-ar", "22050",
                   "-c:a", "pcm_s16le", str(processed)]
        _run(command, cancel)
        concat_paths.append(processed)
        if index < len(phrases) - 1 and pause:
            silence = folder / f"pause_{index + 1:03}.wav"
            _write_silence(silence, pause)
            concat_paths.append(silence)
        if progress:
            progress(0.12 + 0.58 * (index + 1) / len(phrases),
                     f"Озвучиваю фразу {index + 1} из {len(phrases)}")
    manifest = folder / "voice_concat.txt"
    manifest.write_text("".join(f"file '{path.resolve().as_posix().replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39))}'\n"
                                 for path in concat_paths), encoding="utf-8")
    voice = folder / "voice.wav"
    _run([str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y", "-f", "concat",
          "-safe", "0", "-i", str(manifest), "-c:a", "pcm_s16le", str(voice)], cancel)
    with wave.open(str(voice), "rb") as stream:
        duration = stream.getnframes() / stream.getframerate()
    return voice, duration


def export_audio(text: str, destination: Path, *, provider: str = "elevenlabs",
                 voice_id: str = "", options: dict | None = None, sapi_voice_id: str = "",
                 speed: float = 0.82, voice_volume: int = 100,
                 preset: str = "Dramatic Narrator", reverb: float = 0.08,
                 pause: float = 0.35, music: Path | None = None,
                 music_volume: float = 0.18, duck_music: bool = True,
                 loop_music: bool = True,
                 cancel: Event | None = None, progress: Progress | None = None) -> Path:
    """Synthesize speech by phrase, mix optional music, and write an audio-only file."""
    if not 0 <= pause <= 2 or not 0 <= music_volume <= 1.5 or not 0 <= reverb <= 0.35:
        raise ValueError("Проверьте паузу, громкость музыки и реверберацию.")
    if provider not in {"elevenlabs", "sapi"}:
        raise ValueError("Неизвестный движок озвучки.")
    if music is not None and (not music.is_file() or music.suffix.lower() not in {".mp3", ".wav"}):
        raise ValueError("Выберите существующий локальный MP3 или WAV с музыкой.")
    if progress:
        progress(0.02, "Подготовка текста и аудио")
    destination.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg = find_ffmpeg()
    with tempfile.TemporaryDirectory(prefix=".audio_export_", dir=destination.parent) as temp:
        folder = Path(temp)
        if provider == "sapi":
            if not sapi_voice_id:
                raise ValueError("Выберите установленный голос Windows SAPI.")
            track = prepare_narration(text, sapi_voice_id, folder, speed=speed,
                                      volume=voice_volume, preset=preset, reverb=reverb,
                                      pause=max(0.25, pause), cancel=cancel)
            voice_path = track.path
            voice_seconds = track.seconds
            if progress:
                progress(0.72, "Русская озвучка готова")
        else:
            selected = dict(options or {})
            voice_path, voice_seconds = _elevenlabs_voice(
                text, folder, options=selected, voice_id=voice_id, pause=pause,
                preset=preset, reverb=reverb, volume=voice_volume,
                cancel=cancel, progress=progress)
        _cancelled(cancel)
        if not voice_path.is_file() or voice_seconds <= 0:
            raise RuntimeError("Не удалось получить звуковую дорожку речи.")
        if progress:
            progress(0.78, "Сведение речи и музыки" if music else "Подготовка итогового аудио")

        command = [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y", "-i", str(voice_path)]
        fade = min(0.6, voice_seconds / 3)
        if music:
            if loop_music:
                command.append("-stream_loop")
                command.append("-1")
            command += ["-i", str(music)]
            music_seconds = voice_seconds if loop_music else _media_duration(ffmpeg, music)
            music_fade = min(0.6, music_seconds / 3)
            music_fade_out_start = max(0.0, music_seconds - music_fade)
            if duck_music:
                graph = (f"[1:a]volume={music_volume:.3f},afade=t=in:st=0:d={music_fade:.3f},"
                         f"afade=t=out:st={music_fade_out_start:.3f}:d={music_fade:.3f}[bed];"
                         "[0:a]asplit=2[voice][key];"
                         "[bed][key]sidechaincompress=threshold=0.025:ratio=8:attack=30:release=400[duck];"
                         "[voice][duck]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,"
                         "alimiter=limit=0.95,loudnorm=I=-16:TP=-1.5:LRA=11[out]")
            else:
                graph = (f"[1:a]volume={music_volume:.3f},afade=t=in:st=0:d={music_fade:.3f},"
                         f"afade=t=out:st={music_fade_out_start:.3f}:d={music_fade:.3f}[bed];"
                         "[0:a][bed]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,"
                         "alimiter=limit=0.95,loudnorm=I=-16:TP=-1.5:LRA=11[out]")
            command += ["-filter_complex", graph, "-map", "[out]"]
        else:
            command += ["-map", "0:a:0", "-af", "alimiter=limit=0.95,loudnorm=I=-16:TP=-1.5:LRA=11"]
        command += ["-vn", "-t", f"{voice_seconds:.4f}"]
        command += ["-ac", "2", "-ar", "44100"]
        if destination.suffix.lower() == ".mp3":
            command += ["-codec:a", "libmp3lame", "-q:a", "2"]
        elif destination.suffix.lower() == ".wav":
            command += ["-codec:a", "pcm_s16le"]
        else:
            raise ValueError("Формат аудио должен быть MP3 или WAV.")
        command.append(str(destination))
        _run(command, cancel)
    _cancelled(cancel)
    if not destination.is_file() or destination.stat().st_size == 0:
        raise RuntimeError("FFmpeg не создал итоговый аудиофайл.")
    if progress:
        progress(1.0, f"Готово: {destination.name}")
    return destination
