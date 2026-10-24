"""ElevenLabs narration with word timings and a local, parameter-aware cache."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from config import OUTPUT, ROOT
from emoji_support import strip_emojis

MODELS = {
    "Eleven Multilingual v2": "eleven_multilingual_v2",
    "Eleven Flash v2.5": "eleven_flash_v2_5",
}
DEFAULT_MODEL = "eleven_multilingual_v2"
RED_TAGS = re.compile(r"\[/?red\]", re.IGNORECASE)
WORD_RE = re.compile(r"\S+", re.UNICODE)
CACHE_DIR = OUTPUT / ".elevenlabs_cache"


@dataclass(frozen=True)
class Credentials:
    api_key: str
    voice_id: str


@dataclass(frozen=True)
class VoiceAlignment:
    text: str
    audio_path: Path
    word_starts: list[float]
    word_ends: list[float]
    seconds: float
    cached: bool
    fallback: bool


def _parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def _credential_roots(root: Path) -> list[Path]:
    roots = [root]
    for parent in list(root.parents)[:5]:
        if (parent / "desktop.py").is_file() and (parent / "config.py").is_file():
            roots.append(parent)
            break
    return roots


def load_credentials(root: Path = ROOT, environ: Mapping[str, str] | None = None) -> Credentials:
    """Load process environment first, then .env, API_KEY.env and legacy .env.txt."""
    values: dict[str, str] = {}
    roots = _credential_roots(root)
    for filename in (".env.txt", "API_KEY.env", ".env"):
        for candidate in reversed(roots):
            values.update(_parse_env_file(candidate / filename))
    values.update(os.environ if environ is None else environ)
    return Credentials(values.get("ELEVENLABS_API_KEY", "").strip(),
                      values.get("ELEVENLABS_VOICE_ID", "").strip())


def clean_text(text: str) -> str:
    # This is the Speech Text path. The original display string is kept by the
    # renderer; emoji and [red] markup never go into the paid voice request.
    return " ".join(strip_emojis(RED_TAGS.sub("", text)).split())


def estimate_characters(text: str) -> int:
    return len(clean_text(text))


def _sdk() -> tuple[Any, Any]:
    try:
        from elevenlabs import ElevenLabs, VoiceSettings
    except ImportError as exc:
        raise RuntimeError("Не установлен официальный пакет elevenlabs. Запустите установку зависимостей desktop_requirements.txt.") from exc
    return ElevenLabs, VoiceSettings


def make_client(credentials: Credentials | None = None) -> Any:
    credentials = credentials or load_credentials()
    if not credentials.api_key:
        raise ValueError("Не найден ELEVENLABS_API_KEY в окружении, .env, API_KEY.env или .env.txt.")
    ElevenLabs, _ = _sdk()
    import httpx
    transport = httpx.HTTPTransport(retries=0)
    session = httpx.Client(transport=transport, timeout=35.0)
    client = ElevenLabs(api_key=credentials.api_key, httpx_client=session)
    client._auto_shorts_httpx_client = session
    return client


def _close_client(client: Any) -> None:
    session = getattr(client, "_auto_shorts_httpx_client", None)
    if session is not None:
        session.close()


def probe_connection(credentials: Credentials | None = None, client: Any | None = None) -> dict[str, Any]:
    """Read-only check: fetch configured voice and available TTS models."""
    credentials = credentials or load_credentials()
    if not credentials.voice_id:
        raise ValueError("Не найден ELEVENLABS_VOICE_ID. Вставьте Voice ID вашего голоса в .env.txt или .env.")
    owned_client = client is None
    client = client or make_client(credentials)
    try:
        options = {"max_retries": 0, "timeout_in_seconds": 35}
        voice = client.voices.get(credentials.voice_id, request_options=options)
        models = client.models.list(request_options=options)
    except Exception as exc:
        # Do not include request headers, client reprs, or credentials in any UI error.
        status = getattr(exc, "status_code", None)
        detail = {401: "API-ключ отклонён.", 403: "У ключа нет доступа к голосу или API.",
                  404: "Voice ID не найден или недоступен этому ключу.",
                  429: "Лимит запросов API исчерпан."}.get(status, "Не удалось проверить API. Проверьте сеть и настройки ключа.")
        raise RuntimeError(detail) from None
    finally:
        if owned_client:
            _close_client(client)
    model_map = {getattr(item, "model_id", ""): item for item in models}
    supported = {model_id: bool(getattr(model_map.get(model_id), "can_do_text_to_speech", False))
                 for model_id in MODELS.values()}
    return {"voice_name": getattr(voice, "name", "Ваш голос"),
            "voice_id": credentials.voice_id, "models": supported}


def list_account_voices(credentials: Credentials | None = None, client: Any | None = None) -> list[dict[str, str]]:
    """Return the account's selectable voices. This read-only call does not generate audio."""
    credentials = credentials or load_credentials()
    owned_client = client is None
    client = client or make_client(credentials)
    try:
        response = client.voices.get_all(request_options={"max_retries": 0, "timeout_in_seconds": 35})
        items = getattr(response, "voices", None) or (response.get("voices", []) if isinstance(response, dict) else [])
        result = []
        for voice in items:
            if isinstance(voice, dict):
                voice_id, name, category = voice.get("voice_id", ""), voice.get("name", "Голос"), voice.get("category", "")
            else:
                voice_id, name, category = getattr(voice, "voice_id", ""), getattr(voice, "name", "Голос"), getattr(voice, "category", "")
            if voice_id:
                result.append({"id": str(voice_id), "name": str(name), "category": str(category or "")})
        return result
    except Exception as exc:
        status = getattr(exc, "status_code", None)
        detail = {401: "API-ключ отклонён.", 403: "Ключу недоступен список голосов.",
                  429: "Лимит запросов API исчерпан."}.get(status, "Не удалось загрузить голоса. Проверьте сеть и настройки ключа.")
        raise RuntimeError(detail) from None
    finally:
        if owned_client:
            _close_client(client)


def cache_key(text: str, voice_id: str, model_id: str, settings: Mapping[str, Any]) -> str:
    payload = json.dumps({"text": clean_text(text), "voice_id": voice_id, "model_id": model_id,
                          "settings": dict(settings)}, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def cached_audio_path(text: str, voice_id: str, model_id: str,
                      settings: Mapping[str, Any], cache_dir: Path = CACHE_DIR) -> Path:
    return cache_dir / f"{cache_key(text, voice_id, model_id, settings)}.mp3"


def has_cached_audio(text: str, voice_id: str, model_id: str,
                     settings: Mapping[str, Any], cache_dir: Path = CACHE_DIR) -> bool:
    audio = cached_audio_path(text, voice_id, model_id, settings, cache_dir)
    return audio.is_file() and audio.with_suffix(".json").is_file()


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        # ElevenLabs' Pydantic response names this field ``audio_base_64`` in
        # Python, but exposes it to JSON as ``audio_base64``. Use aliases to
        # match the wire format expected by the rest of this module.
        try:
            return value.model_dump(by_alias=True)
        except TypeError:
            return value.model_dump()
    return {name: getattr(value, name) for name in
            ("audio_base64", "audio_base_64", "alignment", "characters", "character_start_times_seconds", "character_end_times_seconds")
            if hasattr(value, name)}


def _alignment_for_words(text: str, alignment: Mapping[str, Any]) -> tuple[list[float], list[float]]:
    characters = list(alignment.get("characters", []))
    starts = list(alignment.get("character_start_times_seconds", []))
    ends = list(alignment.get("character_end_times_seconds", []))
    clean = clean_text(text)
    # API alignment represents its normalized request text. Exact text matching protects
    # against attaching timestamps to different words after normalization.
    joined = "".join(str(c) for c in characters)
    if joined != clean or len(starts) != len(characters) or len(ends) != len(characters):
        raise ValueError("ElevenLabs вернул выравнивание для нормализованного текста; включён резервный тайминг.")
    word_starts: list[float] = []
    word_ends: list[float] = []
    for match in WORD_RE.finditer(clean):
        word_starts.append(float(starts[match.start()]))
        word_ends.append(float(max(ends[match.start():match.end()])))
    return word_starts, word_ends


def _fallback_alignment(text: str, seconds: float) -> tuple[list[float], list[float]]:
    spans = list(WORD_RE.finditer(clean_text(text)))
    weights = [max(1, len(item.group())) for item in spans]
    total = sum(weights) or 1
    cursor = 0.0
    starts: list[float] = []
    ends: list[float] = []
    speech_seconds = max(0.1, seconds * 0.88)
    for weight in weights:
        starts.append(cursor)
        cursor += speech_seconds * weight / total
        ends.append(cursor)
    return starts, ends


def _measure_audio_seconds(path: Path) -> float | None:
    """Read duration from FFmpeg's media probe; None means an estimate is needed."""
    try:
        from config import find_ffmpeg
        ffmpeg = find_ffmpeg()
        result = subprocess.run([str(ffmpeg), "-hide_banner", "-i", str(path), "-f", "null", "-"],
                                capture_output=True, text=True, errors="replace", timeout=20)
        match = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", result.stderr)
        if not match:
            return None
        hours, minutes, seconds = match.groups()
        duration = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
        return duration if duration > 0 else None
    except Exception:
        return None


def synthesize(text: str, destination: Path, *, model_id: str = DEFAULT_MODEL,
               stability: float = 0.52, similarity_boost: float = 0.78,
               style: float = 0.10, speaker_boost: bool = True, speed: float = 0.82,
               credentials: Credentials | None = None, voice_id: str = "", client: Any | None = None,
               cache_dir: Path = CACHE_DIR) -> VoiceAlignment:
    if model_id not in MODELS.values():
        raise ValueError("Выбрана неизвестная модель ElevenLabs.")
    if not text.strip():
        raise ValueError("Введите текст для озвучки.")
    if not (0 <= stability <= 1 and 0 <= similarity_boost <= 1 and 0 <= style <= 1 and 0.7 <= speed <= 1.2):
        raise ValueError("Параметры ElevenLabs выходят за допустимый диапазон.")
    credentials = credentials or load_credentials()
    if voice_id:
        credentials = Credentials(credentials.api_key, voice_id)
    if not credentials.voice_id:
        raise ValueError("Не найден ELEVENLABS_VOICE_ID. Вставьте Voice ID вашего голоса в .env.txt или .env.")
    if not clean_text(text):
        raise ValueError("После удаления тегов выделения не осталось текста для озвучки.")
    settings = {"stability": stability, "similarity_boost": similarity_boost, "style": style,
                "use_speaker_boost": speaker_boost, "speed": speed}
    key = cache_key(text, credentials.voice_id, model_id, settings)
    audio_path = cache_dir / f"{key}.mp3"
    meta_path = cache_dir / f"{key}.json"
    cache_dir.mkdir(parents=True, exist_ok=True)
    if audio_path.is_file() and meta_path.is_file():
        metadata = json.loads(meta_path.read_text(encoding="utf-8"))
        if metadata.get("fallback"):
            starts, ends = _fallback_alignment(text, float(metadata["seconds"]))
        else:
            starts, ends = _alignment_for_words(text, metadata.get("alignment", {}))
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(audio_path.read_bytes())
        return VoiceAlignment(clean_text(text), destination, starts, ends,
                              float(metadata["seconds"]), True, bool(metadata.get("fallback")))

    owned_client = client is None
    client = client or make_client(credentials)
    _, VoiceSettings = _sdk()
    try:
        response = client.text_to_speech.convert_with_timestamps(
            voice_id=credentials.voice_id, text=clean_text(text), model_id=model_id,
            output_format="mp3_44100_128",
            voice_settings=VoiceSettings(stability=stability, similarity_boost=similarity_boost,
                                         style=style, use_speaker_boost=speaker_boost, speed=speed),
            request_options={"max_retries": 0, "timeout_in_seconds": 240},
        )
    finally:
        if owned_client:
            _close_client(client)
    response_data = _as_dict(response)
    audio_base64 = response_data.get("audio_base64") or response_data.get("audio_base_64")
    if not audio_base64:
        raise RuntimeError("ElevenLabs вернул ответ без аудиоданных. Озвучка не сохранена; повторный запрос не выполнялся.")
    try:
        audio_bytes = base64.b64decode(audio_base64, validate=True)
    except (ValueError, TypeError) as exc:
        raise RuntimeError("ElevenLabs вернул аудио в неожиданном формате. Озвучка не сохранена.") from exc
    alignment = _as_dict(response_data.get("alignment") or {})
    cache_dir.mkdir(parents=True, exist_ok=True)
    probe_path = cache_dir / f"{key}.probe.mp3"
    probe_path.write_bytes(audio_bytes)
    measured_seconds = _measure_audio_seconds(probe_path)
    probe_path.unlink(missing_ok=True)
    try:
        starts, ends = _alignment_for_words(text, alignment)
        if not ends:
            raise ValueError("alignment is empty")
        audio_seconds = measured_seconds or max(ends)
        seconds = audio_seconds + 0.35
        fallback = False
    except (ValueError, TypeError, IndexError):
        audio_seconds = measured_seconds or max(0.5, len(clean_text(text)) / (14 * speed))
        starts, ends = _fallback_alignment(text, audio_seconds)
        seconds = audio_seconds + 0.35
        fallback = True
    audio_part = audio_path.with_suffix(".mp3.part")
    meta_part = meta_path.with_suffix(".json.part")
    audio_part.write_bytes(audio_bytes)
    meta_part.write_text(json.dumps({"alignment": alignment, "seconds": seconds,
                                    "audio_seconds": audio_seconds,
                                    "fallback": fallback}, ensure_ascii=False), encoding="utf-8")
    audio_part.replace(audio_path)
    meta_part.replace(meta_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(audio_bytes)
    return VoiceAlignment(clean_text(text), destination, starts, ends, seconds, False, fallback)
