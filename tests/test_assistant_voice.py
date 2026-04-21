import sys
from pathlib import Path
from types import SimpleNamespace

# ensure project root is on sys.path so imports work under pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import assistant


def test_speak_does_not_crash_when_tts_init_fails(monkeypatch, capsys):
    class BrokenPyttsx3:
        @staticmethod
        def init():
            raise RuntimeError("Access is denied.")

    assistant.reset_tts_engine()
    monkeypatch.setattr(assistant, "pyttsx3", BrokenPyttsx3)
    monkeypatch.setitem(assistant.STATE, "voice_output", True)

    assistant.speak("Voice check")

    output = capsys.readouterr().out
    assert "FRIDAY: Voice check" in output
    assert "Voice output unavailable" in output
    assert "Windows SAPI access was denied." in output


def test_voice_listen_once_falls_back_to_openai(monkeypatch):
    calls = []

    class DummyRecognizer:
        def adjust_for_ambient_noise(self, source, duration):
            calls.append(("adjust", duration))

        def listen(self, source, timeout, phrase_time_limit):
            calls.append(("listen", timeout, phrase_time_limit))
            return object()

        def recognize_google(self, audio, language):
            calls.append(("google", language))
            raise RuntimeError("google down")

        def recognize_openai(self, audio, model, language):
            calls.append(("openai", model, language))
            return "hey friday suit up"

    class DummyMicrophone:
        def __enter__(self):
            return object()

        def __exit__(self, exc_type, exc, tb):
            return False

    fake_sr = SimpleNamespace(
        Recognizer=DummyRecognizer,
        Microphone=DummyMicrophone,
        UnknownValueError=ValueError,
    )

    monkeypatch.setattr(assistant, "sr", fake_sr)
    monkeypatch.setattr(
        assistant,
        "get_voice_settings",
        lambda: {
            "backend": "auto",
            "language": "en-US",
            "timeout": 6,
            "phrase_time_limit": 8,
            "ambient_duration": 0.2,
            "openai_model": "gpt-4o-mini-transcribe",
        },
    )
    monkeypatch.setattr(
        assistant, "get_configured_openai_api_key", lambda: "sk-real-test-key"
    )
    messages = []
    monkeypatch.setattr(assistant, "speak", messages.append)

    heard = assistant.voice_listen_once()

    assert heard == "hey friday suit up"
    assert messages == ["Listening."]
    assert ("google", "en-US") in calls
    assert ("openai", "gpt-4o-mini-transcribe", "en-US") in calls


def test_voice_listen_once_reports_missing_microphone(monkeypatch):
    class DummyRecognizer:
        pass

    class BrokenMicrophone:
        def __enter__(self):
            raise OSError("No Default Input Device Available")

        def __exit__(self, exc_type, exc, tb):
            return False

    fake_sr = SimpleNamespace(Recognizer=DummyRecognizer, Microphone=BrokenMicrophone)
    messages = []

    monkeypatch.setattr(assistant, "sr", fake_sr)
    monkeypatch.setattr(assistant, "speak", messages.append)

    heard = assistant.voice_listen_once()

    assert heard == ""
    assert messages[-1] == "No microphone input device is available."


def test_listen_executes_command_after_wake_word(monkeypatch):
    spoken_messages = []

    monkeypatch.setattr(assistant, "voice_listen_once", lambda: "hey Friday suit up")
    monkeypatch.setattr(assistant, "speak", spoken_messages.append)
    monkeypatch.setitem(assistant.STATE, "wake_word_enabled", True)
    monkeypatch.setitem(assistant.STATE, "armor_deployed", False)

    keep_running, reply = assistant.execute_command("listen")

    assert keep_running is True
    assert reply == "Suit deployed."
    assert assistant.STATE["armor_deployed"] is True
    assert spoken_messages == ["Suit deployed."]


def test_listen_prompts_then_executes_follow_up_command(monkeypatch):
    spoken_messages = []
    heard_inputs = iter(["hey Friday", "suit up"])

    monkeypatch.setattr(assistant, "voice_listen_once", lambda: next(heard_inputs))
    monkeypatch.setitem(assistant.STATE, "armor_deployed", False)
    monkeypatch.setattr(assistant, "speak", spoken_messages.append)
    monkeypatch.setitem(assistant.STATE, "wake_word_enabled", True)

    keep_running, reply = assistant.execute_command("listen")

    assert keep_running is True
    assert reply == "Suit deployed."
    assert assistant.STATE["armor_deployed"] is True
    assert spoken_messages == ["Yes?", "Suit deployed."]


def test_listen_reports_missing_follow_up_after_wake_word(monkeypatch):
    spoken_messages = []
    heard_inputs = iter(["hey Friday", ""])

    monkeypatch.setattr(assistant, "voice_listen_once", lambda: next(heard_inputs))
    monkeypatch.setattr(assistant, "speak", spoken_messages.append)
    monkeypatch.setitem(assistant.STATE, "wake_word_enabled", True)

    keep_running, reply = assistant.execute_command("listen")

    assert keep_running is True
    assert reply == "I didn't catch that."
    assert spoken_messages == ["Yes?", "I didn't catch that."]
