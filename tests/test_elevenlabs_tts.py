from __future__ import annotations

import base64
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from elevenlabs_tts import (Credentials, _alignment_for_words, _as_dict, cache_key, clean_text,
                            estimate_characters, has_cached_audio, list_account_voices, load_credentials, probe_connection,
                            synthesize)


class FakeClient:
    def __init__(self, response=None):
        self.calls = 0
        self.response = response
        self.last_kwargs = None
        self.voices = SimpleNamespace(get=lambda voice_id, **kwargs: SimpleNamespace(name="Мой тестовый голос"))
        self.voices.get_all = lambda **kwargs: SimpleNamespace(voices=[
            SimpleNamespace(voice_id="voice-a", name="Глубокий голос", category="premade")])
        self.models = SimpleNamespace(list=lambda **kwargs: [
            SimpleNamespace(model_id="eleven_multilingual_v2", can_do_text_to_speech=True),
            SimpleNamespace(model_id="eleven_flash_v2_5", can_do_text_to_speech=True),
        ])
        self.text_to_speech = SimpleNamespace(convert_with_timestamps=self._convert)

    def _convert(self, **kwargs):
        self.calls += 1
        self.last_kwargs = kwargs
        return self.response


class ElevenLabsIntegrationTests(unittest.TestCase):
    def test_account_voice_list_is_read_only_and_returns_selectable_ids(self):
        result = list_account_voices(Credentials("secret", "configured"), client=FakeClient())
        self.assertEqual(result, [{"id": "voice-a", "name": "Глубокий голос", "category": "premade"}])

    def test_sdk_model_dump_uses_wire_alias_for_audio_field(self):
        class SDKResponse:
            def model_dump(self, *, by_alias=False):
                return {"audio_base64" if by_alias else "audio_base_64": "encoded"}

        self.assertEqual(_as_dict(SDKResponse()), {"audio_base64": "encoded"})

    def test_response_object_python_field_name_is_supported(self):
        self.assertEqual(_as_dict(SimpleNamespace(audio_base_64="encoded")),
                         {"audio_base_64": "encoded"})

    def test_red_tags_removed_and_char_count_matches_request_text(self):
        text = "💸 Пока ты [red]ищешь[/red] 🎓 клиентов"
        self.assertEqual(clean_text(text), "Пока ты ищешь клиентов")
        self.assertEqual(estimate_characters(text), len("Пока ты ищешь клиентов"))

    def test_character_timing_maps_words_and_punctuation(self):
        clean = "Да, мир!"
        alignment = {
            "characters": list(clean),
            "character_start_times_seconds": [i * 0.1 for i in range(len(clean))],
            "character_end_times_seconds": [i * 0.1 + 0.08 for i in range(len(clean))],
        }
        starts, ends = _alignment_for_words(clean, alignment)
        self.assertEqual(starts, [0.0, 0.4])
        self.assertEqual(ends, [0.28, 0.78])

    def test_audio_and_alignment_are_cached_for_identical_parameters(self):
        text = "💸 Пока ты [red]ищешь[/red] 🎓 клиентов"
        clean = clean_text(text)
        alignment = {"characters": list(clean),
                     "character_start_times_seconds": [i * 0.05 for i in range(len(clean))],
                     "character_end_times_seconds": [i * 0.05 + 0.04 for i in range(len(clean))]}
        response = SimpleNamespace(audio_base64=base64.b64encode(b"fake mp3 bytes").decode(),
                                   alignment=alignment)
        client = FakeClient(response)
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temp:
            root = Path(temp)
            cache = root / "cache"
            with patch("elevenlabs_tts._sdk", return_value=(None, lambda **kw: SimpleNamespace(**kw))):
                first = synthesize(text, root / "first.mp3", credentials=Credentials("secret", "voice"),
                                   client=client, cache_dir=cache)
                second = synthesize(text, root / "second.mp3", credentials=Credentials("secret", "voice"),
                                    client=client, cache_dir=cache)
            self.assertFalse(first.cached)
            self.assertTrue(second.cached)
            self.assertEqual(client.calls, 1)
            self.assertEqual(client.last_kwargs["text"], clean)
            self.assertEqual((root / "first.mp3").read_bytes(), b"fake mp3 bytes")
            self.assertEqual((root / "second.mp3").read_bytes(), b"fake mp3 bytes")
            self.assertTrue(has_cached_audio(text, "voice", "eleven_multilingual_v2",
                                              {"stability": 0.52, "similarity_boost": 0.78,
                                               "style": 0.1, "use_speaker_boost": True, "speed": 0.82}, cache))

    def test_selected_voice_overrides_configured_default_voice(self):
        clean = "Привет мир"
        alignment = {"characters": list(clean),
                     "character_start_times_seconds": [i * .1 for i in range(len(clean))],
                     "character_end_times_seconds": [i * .1 + .08 for i in range(len(clean))]}
        response = SimpleNamespace(audio_base64=base64.b64encode(b"audio").decode(), alignment=alignment)
        client = FakeClient(response)
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temp:
            with patch("elevenlabs_tts._sdk", return_value=(None, lambda **kw: SimpleNamespace(**kw))):
                synthesize(clean, Path(temp) / "test.mp3", credentials=Credentials("secret", "configured"),
                           voice_id="selected", client=client, cache_dir=Path(temp) / "cache")
        self.assertEqual(client.last_kwargs["voice_id"], "selected")

    def test_cache_key_changes_when_settings_change(self):
        base = {"stability": 0.5, "speed": 1.0}
        changed = {"stability": 0.6, "speed": 1.0}
        self.assertNotEqual(cache_key("Привет", "voice", "eleven_multilingual_v2", base),
                            cache_key("Привет", "voice", "eleven_multilingual_v2", changed))

    def test_packaged_app_falls_back_to_project_credentials_file(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temp:
            project = Path(temp) / "AUTO_SHORTS"
            packaged = project / "desktop_release" / "AUTO SHORTS STUDIO"
            packaged.mkdir(parents=True)
            (project / "desktop.py").write_text("", encoding="utf-8")
            (project / "config.py").write_text("", encoding="utf-8")
            (project / ".env.txt").write_text("ELEVENLABS_API_KEY=local-only\nELEVENLABS_VOICE_ID=voice-local\n", encoding="utf-8")
            result = load_credentials(packaged, environ={})
            self.assertEqual(result, Credentials("local-only", "voice-local"))

    def test_connection_probe_reads_voice_and_models_only(self):
        client = FakeClient()
        result = probe_connection(Credentials("secret", "voice"), client=client)
        self.assertEqual(result["voice_name"], "Мой тестовый голос")
        self.assertTrue(all(result["models"].values()))
        self.assertEqual(client.calls, 0)


if __name__ == "__main__":
    unittest.main()
