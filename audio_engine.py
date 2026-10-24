from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np


def detect_onsets(ffmpeg: Path, music: Path, seconds: float) -> list[float]:
    """Return coarse energy transients. Empty result means uniform timing should be used."""
    command = [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-stream_loop", "-1", "-i", str(music), "-t", f"{seconds:.3f}", "-ac", "1", "-ar", "8000", "-f", "f32le", "-"]
    done = subprocess.run(command, capture_output=True)
    if done.returncode:
        return []
    samples = np.frombuffer(done.stdout, dtype="<f4")
    hop = 400  # 50 ms
    if len(samples) < hop * 8:
        return []
    samples = samples[:len(samples) // hop * hop].reshape(-1, hop)
    energy = np.sqrt(np.mean(samples * samples, axis=1))
    rise = np.maximum(0, np.diff(energy, prepend=energy[0]))
    threshold = max(float(np.median(rise) + 1.4 * np.std(rise)), 0.008)
    peaks = []
    last = -10.0
    for index in range(1, len(rise) - 1):
        current = index * 0.05
        if rise[index] >= threshold and rise[index] >= rise[index-1] and rise[index] >= rise[index+1] and current-last >= 0.18:
            peaks.append(current)
            last = current
    return peaks


def mux_audio(ffmpeg: Path, video: Path, music: Path | None, result: Path, seconds: float,
              volume: float, fade: float, voice: Path | None = None, voice_volume: int = 100) -> None:
    if music is None and voice is None:
        video.replace(result)
        return
    if music is not None and (not music.is_file() or music.suffix.lower() not in {".mp3", ".wav"}):
        raise ValueError("Музыка должна быть локальным MP3 или WAV.")
    if voice is not None and not voice.is_file():
        raise ValueError("Файл озвучки не найден.")
    fade = max(0.0, min(fade, seconds / 2))
    filters = [f"volume={max(0.0, min(volume, 2.0)):.3f}"]
    if fade:
        filters += [f"afade=t=in:st=0:d={fade:.3f}", f"afade=t=out:st={seconds-fade:.3f}:d={fade:.3f}"]
    command = [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y", "-i", str(video)]
    if music is not None:
        command += ["-stream_loop", "-1", "-i", str(music)]
    if voice is not None:
        command += ["-i", str(voice)]
    command += ["-map", "0:v:0", "-c:v", "copy", "-c:a", "aac", "-b:a", "160k"]
    if music is not None and voice is not None:
        graph = (f"[1:a]{','.join(filters)}[music];"
                 f"[2:a]volume={max(0, min(voice_volume, 100)) / 100:.3f},asplit=2[voice][side];"
                 "[music][side]sidechaincompress=threshold=0.035:ratio=9:attack=45:release=300[duck];"
                 "[duck][voice]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[mix]")
        command += ["-filter_complex", graph, "-map", "[mix]"]
    elif music is not None:
        command += ["-map", "1:a:0", "-af", ",".join(filters)]
    else:
        command += ["-map", "1:a:0", "-af", f"volume={max(0, min(voice_volume, 100)) / 100:.3f},apad"]
    command += ["-t", f"{seconds:.4f}", "-movflags", "+faststart", str(result)]
    done = subprocess.run(command, capture_output=True, text=True, errors="replace")
    if done.returncode:
        raise RuntimeError("Ошибка добавления музыки: " + done.stderr[-1500:])
