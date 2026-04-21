import datetime
import json
import platform
import random
import secrets
import socket
import threading
import webbrowser
from pathlib import Path
import re
import difflib
from typing import Any, Callable, Iterable, cast
import os

try:
    from openai import OpenAI
except Exception:
    OpenAI = None


def find_wake_match(heard: str, wake_word: str) -> int:
    """Return the character index in `heard` after the matched wake-word, or -1 if not found.
    Attempts exact match, whole-phrase fuzzy, then token-level fuzzy matching.
    """
    if not heard or not wake_word:
        return -1
    # exact match
    m = re.search(re.escape(wake_word), heard, flags=re.IGNORECASE)
    if m:
        return m.end()

    low_heard = heard.lower()
    hw = re.sub(r"\s+", " ", wake_word.lower()).strip()
    # whole-phrase fuzzy
    ratio = difflib.SequenceMatcher(
        None, hw, re.sub(r"[^a-z0-9\s]", " ", low_heard)
    ).ratio()
    if ratio >= 0.75:
        # find approximate location by searching for the best close match substring
        words = re.sub(r"[^a-z0-9\s]", " ", low_heard).split()
        hw_words = hw.split()
        best = None
        best_ratio = 0.0
        for start in range(len(words)):
            for end in range(start + 1, min(len(words), start + len(hw_words) + 3) + 1):
                cand = " ".join(words[start:end])
                r = difflib.SequenceMatcher(None, hw, cand).ratio()
                if r > best_ratio:
                    best_ratio = r
                    best = (start, end, cand)
        if best and best_ratio >= 0.6:
            # compute end char index in original heard
            cand_text = best[2]
            idx = low_heard.find(cand_text)
            if idx != -1:
                return idx + len(cand_text)

    # token-level fuzzy: match each wake token sequentially in heard tokens allowing small mismatches
    heard_clean = re.sub(r"[^a-z0-9\s]", " ", low_heard)
    heard_words = heard_clean.split()
    hw_tokens = hw.split()
    if not hw_tokens:
        return -1
    hi = 0
    last_end = -1
    for token in hw_tokens:
        found = False
        # search in next few heard words for a close match
        for offset in range(0, min(6, len(heard_words) - hi)):
            # consider multi-word candidates (1..3 words) starting at hi+offset
            best_local = None
            best_local_r = 0.0
            max_join = min(3, len(heard_words) - (hi + offset))
            for join_count in range(1, max_join + 1):
                cand = " ".join(heard_words[hi + offset : hi + offset + join_count])
                r = difflib.SequenceMatcher(None, token, cand).ratio()
                if r > best_local_r:
                    best_local_r = r
                    best_local = (cand, join_count)
            if best_local is None:
                continue
            cand, used = best_local
            r = best_local_r
            if r >= 0.65 or token == cand:
                # locate this cand in the original heard to get end index
                search_start = 0
                if last_end != -1:
                    search_start = last_end
                pos = low_heard.find(cand, search_start)
                if pos != -1:
                    last_end = pos + len(cand)
                else:
                    pos = low_heard.find(cand)
                    if pos != -1:
                        last_end = pos + len(cand)
                    else:
                        last_end = -1
                hi = hi + offset + used
                found = True
                break
        if not found:
            return -1
    return last_end if last_end >= 0 else -1


def extract_command_after_wake_word(heard: str, wake_word: str) -> tuple[bool, str]:
    """Return whether a wake phrase was detected and the remaining command text."""
    pos = find_wake_match(heard, wake_word)
    if pos == -1:
        assistant_name = ASSISTANT_NAME.lower()
        idx = heard.lower().find(assistant_name)
        if idx != -1:
            pos = idx + len(assistant_name)
        else:
            return False, ""
    return True, heard[pos:].strip(" ,:-\t\n\r")


try:
    import pyttsx3
except ImportError:
    pyttsx3 = None

try:
    import speech_recognition as sr
except ImportError:
    sr = None

try:
    from flask import Flask, jsonify, request
except ImportError:
    Flask = None
    jsonify = None
    request = None

try:
    import requests  # type: ignore[reportMissingModuleSource]
except ImportError:
    requests = None


ASSISTANT_NAME = "FRIDAY"
USER_NAME = "Boss"
DATA_PATH = Path("friday_data.json")

STATE = {
    "armor_deployed": False,
    "flight_mode": False,
    "combat_mode": False,
    "arc_reactor": 90,
    "protocol": "passive",
    "ai_mode": "assistant",
    "session_commands": 0,
    "voice_output": True,
    "wake_word_enabled": True,
}

tts_engine = None
tts_error_message = ""
last_reported_tts_error = ""
server_started = False
server_thread = None
server_port = None


def load_data() -> dict:
    default_data = {"notes": [], "reminders": [], "api_key": ""}
    # ensure voice settings exist and include a selectable voice index
    default_data.setdefault("voice", {})
    default_data["voice"].setdefault("enabled", True)
    default_data["voice"].setdefault("wake_word", "hey friday")
    default_data["voice"].setdefault("rate", 178)
    default_data["voice"].setdefault("volume", 1.0)
    default_data["voice"].setdefault("index", 0)
    default_data["voice"].setdefault("backend", "auto")
    default_data["voice"].setdefault("language", "en-US")
    default_data["voice"].setdefault("timeout", 6)
    default_data["voice"].setdefault("phrase_time_limit", 8)
    default_data["voice"].setdefault("ambient_duration", 0.6)
    default_data["voice"].setdefault("openai_model", "gpt-4o-mini-transcribe")
    if not DATA_PATH.exists():
        return default_data
    try:
        data = json.loads(DATA_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return default_data
        # ensure voice config keys exist for older files
        data.setdefault("voice", {})
        data["voice"].setdefault("enabled", True)
        data["voice"].setdefault("wake_word", "hey friday")
        data["voice"].setdefault("rate", 178)
        data["voice"].setdefault("volume", 1.0)
        data["voice"].setdefault("index", 0)
        data["voice"].setdefault("backend", "auto")
        data["voice"].setdefault("language", "en-US")
        data["voice"].setdefault("timeout", 6)
        data["voice"].setdefault("energy_threshold", 300)
        data["voice"].setdefault("dynamic_energy_ratio", 1.5)
        data["voice"].setdefault("output_backend", "auto")
        data["voice"].setdefault("output_model", "nova")
        data["voice"].setdefault("openai_model", "gpt-4o-mini-transcribe")
        return data
    except (json.JSONDecodeError, OSError):
        return default_data


def save_data(data: dict) -> None:
    DATA_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def get_api_key() -> str:
    data = load_data()
    api_key = data.get("api_key", "").strip()
    if not api_key:
        api_key = secrets.token_urlsafe(18)
        data["api_key"] = api_key
        save_data(data)
    return api_key


def get_voice_settings() -> dict[str, Any]:
    voice = load_data().get("voice", {})
    if not isinstance(voice, dict):
        voice = {}
    defaults = {
        "enabled": True,
        "wake_word": "hey friday",
        "rate": 178,
        "volume": 1.0,
        "index": 0,
        "backend": "auto",
        "language": "en-US",
        "timeout": 6,
        "phrase_time_limit": 8,
        "ambient_duration": 0.6,
        "energy_threshold": 300,
        "dynamic_energy_ratio": 1.5,
        "output_backend": "auto",
        "output_model": "nova",
        "openai_model": "gpt-4o-mini-transcribe",
    }
    merged = defaults.copy()
    merged.update(voice)
    return merged


def sync_voice_state_from_config() -> None:
    STATE["voice_output"] = bool(get_voice_settings().get("enabled", True))


def format_runtime_error(exc: Exception) -> str:
    message = str(exc).strip()
    if not message:
        message = exc.__class__.__name__
    if len(message) > 140:
        message = f"{message[:137]}..."
    return message


def friendly_tts_error(exc: Exception) -> str:
    message = format_runtime_error(exc)
    lowered = message.lower()
    if "access is denied" in lowered:
        return "Windows SAPI access was denied."
    return message


def friendly_microphone_error(exc: Exception) -> str:
    message = format_runtime_error(exc)
    lowered = message.lower()
    if "waittimeouterror" in lowered or "listening timed out" in lowered:
        return "No voice detected."
    if "pyaudio" in lowered:
        return "Voice input needs PyAudio installed."
    if "access is denied" in lowered or "permission" in lowered:
        return "Microphone access was denied."
    if "no default input device" in lowered or "invalid input device" in lowered:
        return "No microphone input device is available."
    return f"Voice input unavailable: {message}"


def reset_tts_engine() -> None:
    global tts_engine, tts_error_message, last_reported_tts_error
    if tts_engine is not None:
        try:
            tts_engine.stop()
        except Exception:
            pass
    tts_engine = None
    tts_error_message = ""
    last_reported_tts_error = ""


def report_tts_issue_once() -> None:
    global last_reported_tts_error
    if not tts_error_message or tts_error_message == last_reported_tts_error:
        return
    print(f"{ASSISTANT_NAME}: Voice output unavailable ({tts_error_message})")
    last_reported_tts_error = tts_error_message


def looks_configured(value: str) -> bool:
    lowered = value.strip().lower()
    if not lowered:
        return False
    placeholder_tokens = ("xxxx", "your_", "replace", "changeme", "placeholder")
    return not any(token in lowered for token in placeholder_tokens)


def get_configured_openai_api_key() -> str:
    env_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if looks_configured(env_key):
        return env_key
    data = load_data()
    integrations = data.get("integrations", {}) if isinstance(data, dict) else {}
    candidates = [
        str((integrations.get("openai", {}) or {}).get("api_key", "")).strip(),
        str(data.get("openai_api_key", "")).strip(),
    ]
    for candidate in candidates:
        if looks_configured(candidate):
            return candidate
    return ""


def build_recognition_attempts(
    recognizer: Any, audio: Any, voice_cfg: dict[str, Any]
) -> tuple[list[tuple[str, Callable[[], str]]], str | None]:
    backend = str(voice_cfg.get("backend", "auto")).strip().lower() or "auto"
    language = str(voice_cfg.get("language", "en-US")).strip() or "en-US"
    openai_model = (
        str(voice_cfg.get("openai_model", "gpt-4o-mini-transcribe")).strip()
        or "gpt-4o-mini-transcribe"
    )
    attempts: list[tuple[str, Callable[[], str]]] = []
    setup_notes: list[str] = []

    if backend == "auto":
        ordered_backends = ["google", "openai", "sphinx"]
    elif backend in {"google", "openai", "sphinx"}:
        ordered_backends = [backend]
    else:
        return [], f"Unsupported voice backend '{backend}'."

    for name in ordered_backends:
        if name == "google":
            recognize_google = getattr(recognizer, "recognize_google", None)
            if callable(recognize_google):
                attempts.append(
                    (
                        "google",
                        lambda fn=recognize_google, lang=language: str(
                            fn(audio, language=lang)
                        ),
                    )
                )
            else:
                setup_notes.append("Google speech recognition is unavailable.")
            continue

        if name == "openai":
            recognize_openai = getattr(recognizer, "recognize_openai", None)
            openai_key = get_configured_openai_api_key()
            if callable(recognize_openai) and openai_key:
                attempts.append(
                    (
                        "openai",
                        lambda fn=recognize_openai, key=openai_key, model=openai_model, lang=language: call_openai_recognizer(
                            fn, audio, key, model, lang
                        ),
                    )
                )
            elif not openai_key:
                setup_notes.append("OpenAI speech recognition needs a real API key.")
            else:
                setup_notes.append("OpenAI speech recognition is unavailable.")
            continue

        recognize_sphinx = getattr(recognizer, "recognize_sphinx", None)
        if callable(recognize_sphinx):
            attempts.append(("sphinx", lambda fn=recognize_sphinx: str(fn(audio))))
        else:
            setup_notes.append("PocketSphinx speech recognition is unavailable.")

    if attempts:
        return attempts, None
    return [], (
        setup_notes[0] if setup_notes else "No speech recognition backend available."
    )


def call_openai_recognizer(
    recognizer_fn: Callable[..., Any],
    audio: Any,
    api_key: str,
    model: str,
    language: str,
) -> str:
    previous_key = os.environ.get("OPENAI_API_KEY")
    os.environ["OPENAI_API_KEY"] = api_key
    try:
        return str(recognizer_fn(audio, model=model, language=language))
    finally:
        if previous_key is None:
            os.environ.pop("OPENAI_API_KEY", None)
        else:
            os.environ["OPENAI_API_KEY"] = previous_key


def recognize_audio_with_fallbacks(
    recognizer: Any, audio: Any, voice_cfg: dict[str, Any]
) -> tuple[str, str | None]:
    attempts, setup_error = build_recognition_attempts(recognizer, audio, voice_cfg)
    if not attempts:
        return "", setup_error or "No speech recognition backend available."

    unknown_value_type = (
        getattr(sr, "UnknownValueError", None) if sr is not None else None
    )
    errors: list[str] = []
    heard_unclear_audio = False

    for name, attempt in attempts:
        try:
            text = str(attempt()).strip()
            if text:
                return text, None
            heard_unclear_audio = True
        except Exception as exc:
            if unknown_value_type is not None and isinstance(exc, unknown_value_type):
                heard_unclear_audio = True
                continue
            errors.append(f"{name}: {format_runtime_error(exc)}")

    if heard_unclear_audio and not errors:
        return "", "I could not understand that."
    if errors:
        return "", f"Speech recognition failed ({'; '.join(errors[:2])})."
    return "", "I could not understand that."


sync_voice_state_from_config()


def call_gemini(prompt: str, model: str | None = None, timeout: int = 15) -> str:
    """Call a configured Gemini-like API endpoint. Configuration is read from friday_data.json under
    integrations.gemini. The config may specify provider, api_key, endpoint, model, and auth_header.
    This is a best-effort, generic wrapper — adjust endpoint/model in configuration for your provider.
    """
    if requests is None:
        return "Requests library not installed. Install with 'pip install requests'."
    data = load_data()
    gem = data.get("integrations", {}).get("gemini", {}) or {}
    if not gem.get("enabled"):
        return "Gemini integration is disabled in configuration."
    api_key = (gem.get("api_key") or "").strip()
    if not api_key:
        return "Gemini API key not configured."
    provider = (gem.get("provider") or "").lower()
    endpoint = gem.get("endpoint") or ""
    cfg_model = gem.get("model") or model or ""
    auth_header = (gem.get("auth_header") or "bearer").lower()

    # Build default endpoint for Google generative API if none provided and provider is google
    if not endpoint and provider == "google":
        if cfg_model:
            endpoint = f"https://generativelanguage.googleapis.com/v1beta2/models/{cfg_model}:generateText"
        else:
            return "No endpoint or model configured for Google provider."

    if not endpoint:
        return "No Gemini endpoint configured. Set integrations.gemini.endpoint in friday_data.json."

    headers = {"Content-Type": "application/json"}
    if auth_header in {"bearer", "authorization"}:
        headers["Authorization"] = f"Bearer {api_key}"
    elif auth_header in {"x-api-key", "api-key"}:
        headers["x-api-key"] = api_key
    else:
        headers[auth_header] = api_key

    # Build provider-specific payloads
    if provider == "google":
        # Google Generative Language API expects a prompt object
        payload = {
            "prompt": {"text": prompt},
            "temperature": 0.2,
            "maxOutputTokens": 512,
        }
    else:
        payload = {"prompt": prompt}

    try:
        resp = requests.post(endpoint, headers=headers, json=payload, timeout=timeout)
    except Exception as e:
        return f"Request error: {e}"
    try:
        j = resp.json()
    except Exception:
        return resp.text or f"HTTP {resp.status_code}"

    # Heuristic extraction of text from various API responses
    if isinstance(j, dict):
        # common: choices -> [ { message: {content: '...'} } ]
        if "choices" in j and isinstance(j["choices"], list) and j["choices"]:
            first = j["choices"][0]
            if isinstance(first, dict):
                # openai-like
                msg = first.get("message") or first.get("text") or first.get("content")
                if isinstance(msg, dict):
                    return msg.get("content") or str(msg)
                if isinstance(msg, str):
                    return msg
        # google-like: look for 'candidates' or 'output' fields
        if "candidates" in j and isinstance(j["candidates"], list) and j["candidates"]:
            cand = j["candidates"][0]
            if isinstance(cand, dict):
                return cand.get("output") or cand.get("content") or str(cand)
            if isinstance(cand, str):
                return cand
        # generic fields
        for key in ("output", "text", "response", "result", "generated_text"):
            if key in j:
                val = j[key]
                if isinstance(val, str):
                    return val
                if isinstance(val, dict):
                    return json.dumps(val)
    # Fallback: stringify whole JSON
    try:
        return json.dumps(j)
    except Exception:
        return str(j)


def call_openai(prompt: str, model: str = "gpt-4o-mini", timeout: int = 15) -> str:
    """Call OpenAI Chat Completions via the `openai` package.

    The function reads the API key from the `OPENAI_API_KEY` environment
    variable. Do NOT hardcode secrets into source files.
    """
    if OpenAI is None:
        return "OpenAI SDK not installed. Install with `pip install openai`."
    api_key = os.getenv("OPENAI_API_KEY")
    print(
        f"DEBUG: call_openai initial env OPENAI_API_KEY={'set' if api_key else 'unset'}"
    )
    # fallback: check configuration file for stored OpenAI key
    if not api_key:
        try:
            cfg = load_data()
            api_key = (cfg.get("integrations", {}).get("openai", {}) or {}).get(
                "api_key"
            ) or cfg.get("openai_api_key")
            print(
                f"DEBUG: call_openai loaded cfg OPENAI key present={bool(api_key)} via load_data()"
            )
        except Exception:
            api_key = None
    # fallback: try reading the config file next to this module (handles differing CWDs)
    if not api_key:
        try:
            cfg_path = Path(__file__).parent / "friday_data.json"
            if cfg_path.exists():
                raw = json.loads(cfg_path.read_text(encoding="utf-8"))
                api_key = (raw.get("integrations", {}).get("openai", {}) or {}).get(
                    "api_key"
                ) or raw.get("openai_api_key")
        except Exception:
            api_key = None
    if not api_key:
        # try explicit file next to module
        try:
            cfg_path = Path(__file__).parent / "friday_data.json"
            print(f"DEBUG: call_openai trying file {cfg_path}")
            if cfg_path.exists():
                raw = json.loads(cfg_path.read_text(encoding="utf-8"))
                api_key = (raw.get("integrations", {}).get("openai", {}) or {}).get(
                    "api_key"
                ) or raw.get("openai_api_key")
                print(f"DEBUG: call_openai file-based key present={bool(api_key)}")
        except Exception as e:
            print(f"DEBUG: call_openai file read error: {e}")
        if not api_key:
            return "OPENAI_API_KEY not set in environment."
    try:
        client = OpenAI(api_key=api_key)
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": "You are FRIDAY, a helpful AI assistant.",
                },
                {"role": "user", "content": prompt},
            ],
            timeout=timeout,
        )
        # Extract text content safely and ensure we always return a string
        try:
            # Some SDK responses may have `content` as None; coerce to string fallback
            content = getattr(resp.choices[0].message, "content", None)
            if content is None:
                return str(resp)
            return str(content)
        except Exception:
            return str(resp)
    except Exception as e:
        return f"OpenAI request error: {e}"


def init_tts(force_retry: bool = False) -> bool:
    global tts_engine, tts_error_message
    if pyttsx3 is None:
        tts_error_message = "pyttsx3 is not installed."
        return False
    if tts_engine is not None:
        return True
    if tts_error_message and not force_retry:
        return False

    try:
        tts_engine = pyttsx3.init()
    except Exception as exc:
        tts_engine = None
        tts_error_message = friendly_tts_error(exc)
        return False

    vcfg = get_voice_settings()
    try:
        rate = int(vcfg.get("rate", 178))
    except Exception:
        rate = 178
    try:
        vol = float(vcfg.get("volume", 1.0))
    except Exception:
        vol = 1.0
    try:
        idx = int(vcfg.get("index", 0))
    except Exception:
        idx = 0

    try:
        tts_engine.setProperty("rate", rate)
    except Exception:
        pass
    try:
        tts_engine.setProperty("volume", vol)
    except Exception:
        pass
    # select voice by index when available
    try:
        voices = list(
            cast(Iterable[Any], cast(Any, tts_engine).getProperty("voices") or [])
        )
        if voices and 0 <= idx < len(voices):
            tts_engine.setProperty("voice", voices[idx].id)
    except Exception:
        pass
    tts_error_message = ""
    return True


def speak(message: str) -> None:
    global tts_engine, tts_error_message
    print(f"{ASSISTANT_NAME}: {message}")
    if not STATE["voice_output"]:
        return

    vcfg = get_voice_settings()
    out_backend = str(vcfg.get("output_backend", "auto")).lower()

    # Determine if we should use cloud (OpenAI)
    use_cloud = False
    
    def is_important(text: str) -> bool:
        lowered = text.lower()
        
        keywords = ["warning", "error", "alert", "anomaly", "diagnostics"]
        strong_words = ["critical", "threat", "danger", "fatal", "breach", "reactor"]
        negative_patterns = [
            "no error", "not critical", "safe", "stable",
            "no issue", "all good", "working fine",
            "no problem", "not unsafe"
        ]
        
        score = 0
        if any(k in lowered for k in keywords):
            score += 1
        if any(w in lowered for w in strong_words):
            score += 2
        if any(n in lowered for n in negative_patterns):
            score -= 2
            
        return score >= 1

    if out_backend == "openai":
        use_cloud = True
    elif out_backend == "auto":
        # Cloud Only When Needed: Use local TTS for instant short responses,
        # upgrade to Cloud TTS for longer responses or important alerts.
        if is_important(message):
            use_cloud = True
        elif len(message) < 60:
            use_cloud = False
        else:
            use_cloud = True

    # Hybrid Smart Voice Integration (OpenAI TTS)
    if use_cloud and OpenAI is not None:
        api_key = get_configured_openai_api_key()
        if api_key:
            try:
                import winsound
                client = OpenAI(api_key=api_key)
                model_voice = str(vcfg.get("output_model", "nova")).lower()
                
                response = client.audio.speech.create(
                    model="tts-1",
                    voice=model_voice,
                    input=message,
                    response_format="wav"
                )
                
                temp_file = f"temp_reply_{secrets.token_hex(4)}.wav"
                if hasattr(response, "write_to_file"):
                    response.write_to_file(temp_file)
                else:
                    with open(temp_file, "wb") as f:
                        f.write(response.content)
                        
                winsound.PlaySound(temp_file, winsound.SND_FILENAME)
                try:
                    os.remove(temp_file)
                except Exception:
                    pass
                return  # Successfully spoke with Smart Voice
            except Exception as e:
                print(f"DEBUG: Smart TTS failed ({e}). Falling back to local.")

    # Fallback to local pyttsx3
    if pyttsx3 is None:
        return
    if not init_tts():
        report_tts_issue_once()
        return
    if tts_engine is not None:
        try:
            tts_engine.say(message)
            tts_engine.runAndWait()
        except KeyboardInterrupt:
            # Stop TTS immediately and ignore the interrupt so the assistant keeps running
            try:
                tts_engine.stop()
            except Exception:
                pass
            return
        except Exception as exc:
            try:
                tts_engine.stop()
            except Exception:
                pass
            tts_engine = None
            tts_error_message = friendly_tts_error(exc)
            report_tts_issue_once()
            return


def get_help_text() -> str:
    return (
        "Commands:\n"
        "- protocol <passive|defense|stealth>\n"
        "- ai mode <assistant|tactical|aggressive>\n"
        "- note add <text> / notes / clear notes\n"
        "- remind me <text> / reminders\n"
        "- voice on / voice off\n"
        "- listen (single voice command)\n"
        "- start voice mode (continuous)\n"
        "- wake word on / wake word off / wake word set <text>\n"
        "- start device server\n"
        "- voice list / voice set <index> / voice range <index> / voice rate <n> / voice volume <v> / voice info\n"
        "- voice backend <auto|google|openai|sphinx> / voice language <code>\n"
        "- gemini <prompt>\n"
        "- file search <name> / file grep <text>\n"
        "- open youtube / open google / search <query>\n"
        "- help / exit"
    )


def local_ip() -> str:
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
        sock.close()
        return ip
    except OSError:
        return "127.0.0.1"


def is_port_available(port: int, host: str = "127.0.0.1") -> bool:
    test_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        test_sock.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        test_sock.close()


def run_diagnostics() -> None:
    checks = [
        ("Armor plating", random.choice(["OK", "OK", "MINOR STRESS"])),
        ("Thrusters", random.choice(["OK", "OK", "RECALIBRATE"])),
        ("Comms", random.choice(["OK", "NOISE DETECTED"])),
    ]
    speak("Running diagnostics.")
    for name, val in checks:
        speak(f"{name}: {val}")


def suit_status() -> None:
    armor = "deployed" if STATE["armor_deployed"] else "stowed"
    flight = "online" if STATE["flight_mode"] else "offline"
    combat = "armed" if STATE["combat_mode"] else "safe"
    speak(
        f"Armor {armor}. Flight {flight}. Combat {combat}. "
        f"Reactor {STATE['arc_reactor']}%. Protocol {STATE['protocol']}. AI {STATE['ai_mode']}."
    )


def voice_listen_once() -> str:
    if sr is None:
        speak("Voice input needs SpeechRecognition and PyAudio installed.")
        return ""
    voice_cfg = get_voice_settings()
    recognizer = sr.Recognizer()
    microphone = getattr(sr, "Microphone", None)
    if microphone is None:
        speak("Voice input is unavailable because the microphone interface is missing.")
        return ""
    try:
        timeout = max(1, min(20, int(voice_cfg.get("timeout", 6))))
    except Exception:
        timeout = 6
    try:
        phrase_time_limit = max(1, min(30, int(voice_cfg.get("phrase_time_limit", 8))))
    except Exception:
        phrase_time_limit = 8
    try:
        ambient_duration = max(
            0.0, min(3.0, float(voice_cfg.get("ambient_duration", 0.6)))
        )
    except Exception:
        ambient_duration = 0.6
    try:
        with microphone() as source:
            speak("Listening.")
            try:
                thresh = int(voice_cfg.get("energy_threshold", 300))
                if thresh > 0:
                    recognizer.energy_threshold = thresh
                ratio = float(voice_cfg.get("dynamic_energy_ratio", 1.5))
                if ratio > 0:
                    recognizer.dynamic_energy_ratio = ratio
            except Exception:
                pass
            if ambient_duration:
                try:
                    recognizer.adjust_for_ambient_noise(
                        source, duration=ambient_duration
                    )
                except Exception:
                    pass
            audio = recognizer.listen(
                source,
                timeout=timeout,
                phrase_time_limit=phrase_time_limit,
            )
    except Exception as exc:
        speak(friendly_microphone_error(exc))
        return ""
    text, error = recognize_audio_with_fallbacks(recognizer, audio, voice_cfg)
    if error:
        speak(error)
        return ""
    print(f"You(voice): {text}")
    return text


def start_device_server(port: int = 5050) -> str:
    global server_started, server_thread, server_port
    if Flask is None:
        return "Device server needs Flask installed."
    if server_started:
        active_port = server_port if server_port is not None else port
        return f"Device server already running at http://{local_ip()}:{active_port}"

    selected_port = None
    for candidate in range(port, port + 10):
        if is_port_available(candidate):
            selected_port = candidate
            break
    if selected_port is None:
        return f"No free port found between {port} and {port + 9}."

    app = Flask(__name__)
    from flask import request as _request, jsonify as _jsonify

    def check_auth(payload: dict | None = None) -> bool:
        header_key = _request.headers.get("x-api-key", "")
        auth_hdr = _request.headers.get("Authorization", "")
        bearer = ""
        if isinstance(auth_hdr, str) and auth_hdr.lower().startswith("bearer "):
            bearer = auth_hdr.split(" ", 1)[1].strip()
        body_key = ""
        if payload and isinstance(payload, dict):
            body_key = str(payload.get("api_key", "")).strip()
        api_key = get_api_key()
        return header_key == api_key or bearer == api_key or body_key == api_key

    @app.get("/status")
    def status():
        return _jsonify(
            {
                "assistant": ASSISTANT_NAME,
                "reactor": STATE["arc_reactor"],
                "protocol": STATE["protocol"],
                "armor_deployed": STATE["armor_deployed"],
                "ai_mode": STATE["ai_mode"],
            }
        )

    @app.post("/command")
    def remote_command():
        payload = _request.get_json(silent=True) or {}
        header_key = _request.headers.get("x-api-key", "")
        body_key = str(payload.get("api_key", "")).strip()
        api_key = get_api_key()
        if not check_auth(payload):
            return _jsonify({"ok": False, "error": "Unauthorized"}), 401
        cmd = (payload.get("command") or "").strip()
        if not cmd:
            return _jsonify({"ok": False, "error": "Missing command"}), 400
        keep_running, reply = execute_command(cmd, remote=True)
        return _jsonify({"ok": True, "keep_running": keep_running, "reply": reply})

    @app.get("/memories")
    def api_get_memories():
        header_key = _request.headers.get("x-api-key", "")
        api_key = get_api_key()
        if not check_auth():
            return _jsonify({"ok": False, "error": "Unauthorized"}), 401
        # support optional filtering: ?query=...&limit=...&since=...
        args = _request.args
        query = (args.get("query") or "").strip().lower()
        limit = args.get("limit")
        try:
            limit = int(limit) if limit is not None else 50
        except Exception:
            limit = 50
        limit = max(1, min(200, limit))
        since = (args.get("since") or "").strip()

        data = load_data()
        mems = list(data.get("memories", []))

        def parse_stamp(entry: str):
            # entries are stored as "%d %b %Y %I:%M %p - text"
            try:
                parts = entry.split(" - ", 1)
                if not parts:
                    return None
                stamp = parts[0].strip()
                return datetime.datetime.strptime(stamp, "%d %b %Y %I:%M %p")
            except Exception:
                try:
                    return datetime.datetime.fromisoformat(stamp)
                except Exception:
                    return None

        if since:
            try:
                # try parsing provided since as ISO first, then stored format
                try:
                    since_dt = datetime.datetime.fromisoformat(since)
                except Exception:
                    since_dt = datetime.datetime.strptime(since, "%d %b %Y %I:%M %p")
            except Exception:
                return _jsonify({"ok": False, "error": "Invalid since format"}), 400
        else:
            since_dt = None

        out = []
        for entry in mems:
            text = entry.lower()
            if query and query not in text:
                continue
            if since_dt is not None:
                st = parse_stamp(entry)
                if st is None or st < since_dt:
                    continue
            out.append(entry)
            if len(out) >= limit:
                break

        return _jsonify({"ok": True, "count": len(out), "memories": out})

    @app.post("/memories")
    def api_add_memory():
        payload = _request.get_json(silent=True) or {}
        header_key = _request.headers.get("x-api-key", "")
        body_key = str(payload.get("api_key", "")).strip()
        api_key = get_api_key()
        if not check_auth(payload):
            return _jsonify({"ok": False, "error": "Unauthorized"}), 401
        text = (payload.get("text") or "").strip()
        if not text:
            return _jsonify({"ok": False, "error": "Missing text"}), 400
        data = load_data()
        data.setdefault("memories", [])
        stamp = datetime.datetime.now().strftime("%d %b %Y %I:%M %p")
        entry = f"{stamp} - {text}"
        data["memories"].append(entry)
        save_data(data)
        return _jsonify({"ok": True, "memory": entry})

    @app.delete("/memories")
    def api_clear_memories():
        header_key = _request.headers.get("x-api-key", "")
        api_key = get_api_key()
        if not check_auth():
            return _jsonify({"ok": False, "error": "Unauthorized"}), 401
        data = load_data()
        data["memories"] = []
        save_data(data)
        return _jsonify({"ok": True, "cleared": True})

    @app.delete("/memories/<int:index>")
    def api_delete_memory(index: int):
        header_key = _request.headers.get("x-api-key", "")
        api_key = get_api_key()
        if not check_auth():
            return _jsonify({"ok": False, "error": "Unauthorized"}), 401
        data = load_data()
        mems = data.get("memories", [])
        if index < 1 or index > len(mems):
            return _jsonify({"ok": False, "error": "Index out of range"}), 404
        removed = mems.pop(index - 1)
        data["memories"] = mems
        save_data(data)
        return _jsonify({"ok": True, "removed": removed})

    def run_server():
        import waitress
        waitress.serve(app, host="0.0.0.0", port=selected_port)

    server_thread = threading.Thread(target=run_server, daemon=True)
    server_thread.start()
    server_started = True
    server_port = selected_port
    if selected_port != port:
        return f"Port {port} busy. Device server started at http://{local_ip()}:{selected_port}"
    return f"Device server started at http://{local_ip()}:{selected_port}"


def execute_command(command: str, remote: bool = False) -> tuple[bool, str]:
    normalized = command.strip().lower()
    STATE["session_commands"] += 1
    reply = ""

    def say(msg: str) -> None:
        nonlocal reply
        reply = msg
        if remote:
            print(f"{ASSISTANT_NAME}: {msg}")
        else:
            speak(msg)

    def wake_word_follow_up(heard_text: str, wake_word: str) -> tuple[str, str]:
        matched, heard_after = extract_command_after_wake_word(heard_text, wake_word)
        if not matched:
            return "", ""
        if heard_after:
            return heard_after, ""

        prompt = "Yes?"
        say(prompt)
        follow_up = voice_listen_once()
        if not follow_up:
            missed = "I didn't catch that."
            say(missed)
            return "", missed

        matched_follow_up, follow_up_after = extract_command_after_wake_word(
            follow_up, wake_word
        )
        if matched_follow_up:
            if not follow_up_after:
                missed = "I didn't catch that."
                say(missed)
                return "", missed
            return follow_up_after, ""
        return follow_up.strip(), ""

    if not normalized:
        say("Awaiting your command.")
        return True, reply

    if normalized in {"exit", "quit", "shutdown", "bye"}:
        say("Powering down.")
        return False, reply

    if normalized in {"hello", "hi"}:
        say(f"Hello {USER_NAME}. Ready.")
        return True, reply

    if normalized == "help":
        say(get_help_text())
        return True, reply

    if normalized == "time":
        say(datetime.datetime.now().strftime("Current time is %I:%M %p."))
        return True, reply

    if normalized == "date":
        say(datetime.datetime.now().strftime("Today is %A, %d %B %Y."))
        return True, reply

    if normalized == "system info":
        say(f"{platform.system()} {platform.release()} on {platform.machine()}.")
        return True, reply

    if normalized == "suit up":
        STATE["armor_deployed"] = True
        STATE["arc_reactor"] = max(5, STATE["arc_reactor"] - 2)
        say("Suit deployed.")
        return True, reply

    if normalized == "retract suit":
        STATE["armor_deployed"] = False
        STATE["flight_mode"] = False
        STATE["combat_mode"] = False
        say("Suit retracted.")
        return True, reply

    if normalized == "suit status":
        suit_status()
        return True, "Status reported."

    if normalized == "flight mode on":
        if not STATE["armor_deployed"]:
            say("Deploy suit first.")
            return True, reply
        STATE["flight_mode"] = True
        say("Flight mode online.")
        return True, reply

    if normalized == "flight mode off":
        STATE["flight_mode"] = False
        say("Flight mode offline.")
        return True, reply

    if normalized == "combat mode on":
        if not STATE["armor_deployed"]:
            say("Deploy suit first.")
            return True, reply
        STATE["combat_mode"] = True
        say("Combat mode armed.")
        return True, reply

    if normalized == "combat mode off":
        STATE["combat_mode"] = False
        say("Combat mode safe.")
        return True, reply

    if normalized == "threat scan":
        STATE["arc_reactor"] = max(5, STATE["arc_reactor"] - random.randint(1, 3))
        say(
            random.choice(
                [
                    "Airspace clean.",
                    "Unknown contact detected.",
                    "Thermal anomaly detected.",
                ]
            )
        )
        return True, reply

    if normalized == "diagnostics":
        run_diagnostics()
        return True, "Diagnostics complete."

    if normalized == "arc reactor":
        say(f"Reactor at {STATE['arc_reactor']} percent.")
        return True, reply

    if normalized == "recharge":
        old = STATE["arc_reactor"]
        STATE["arc_reactor"] = min(100, STATE["arc_reactor"] + random.randint(6, 16))
        say(f"Reactor recharged from {old}% to {STATE['arc_reactor']}%.")
        return True, reply

    if normalized.startswith("protocol "):
        mode = normalized.split(maxsplit=1)[1]
        if mode not in {"passive", "defense", "stealth"}:
            say("Use passive, defense, or stealth.")
            return True, reply
        STATE["protocol"] = mode
        say(f"Protocol {mode} active.")
        return True, reply

    if normalized.startswith("ai mode "):
        mode = normalized.split(maxsplit=2)[2]
        if mode not in {"assistant", "tactical", "aggressive"}:
            say("Use assistant, tactical, or aggressive.")
            return True, reply
        STATE["ai_mode"] = mode
        say(f"AI mode {mode} active.")
        return True, reply

    if normalized.startswith("note add "):
        txt = command.strip()[9:].strip()
        if not txt:
            say("Note text missing.")
            return True, reply
        data = load_data()
        data["notes"].append(txt)
        save_data(data)
        say("Note saved.")
        return True, reply

    if normalized == "notes":
        data = load_data()
        notes = data.get("notes", [])
        if not notes:
            say("No notes.")
            return True, reply
        for i, n in enumerate(notes, 1):
            speak(f"{i}. {n}")
        return True, "Notes listed."

    if normalized == "clear notes":
        data = load_data()
        data["notes"] = []
        save_data(data)
        say("Notes cleared.")
        return True, reply

    if normalized.startswith("remind me "):
        txt = command.strip()[10:].strip()
        if not txt:
            say("Reminder text missing.")
            return True, reply
        data = load_data()
        stamp = datetime.datetime.now().strftime("%d %b %Y %I:%M %p")
        data["reminders"].append(f"{stamp} - {txt}")
        save_data(data)
        say("Reminder saved.")
        return True, reply

    if normalized == "reminders":
        data = load_data()
        reminders = data.get("reminders", [])
        if not reminders:
            say("No reminders.")
            return True, reply
        for i, n in enumerate(reminders, 1):
            speak(f"{i}. {n}")
        return True, "Reminders listed."

    if normalized.startswith("memory add ") or normalized.startswith(
        "cognitive memory add "
    ):
        # support both 'memory add' and 'cognitive memory add' (common misspellings)
        if normalized.startswith("memory add "):
            txt = command.strip()[11:].strip()
        else:
            txt = command.strip()[22:].strip()
        if not txt:
            say("Memory text missing.")
            return True, reply
        data = load_data()
        data.setdefault("memories", [])
        stamp = datetime.datetime.now().strftime("%d %b %Y %I:%M %p")
        data["memories"].append(f"{stamp} - {txt}")
        save_data(data)
        say("Memory saved.")
        return True, reply

    if normalized == "memory list":
        data = load_data()
        mems = data.get("memories", [])
        if not mems:
            say("No memories.")
            return True, reply
        for i, m in enumerate(mems, 1):
            speak(f"{i}. {m}")
        return True, "Memories listed."

    if normalized == "memory clear":
        data = load_data()
        data["memories"] = []
        save_data(data)
        say("Memories cleared.")
        return True, reply

    if normalized == "voice off":
        STATE["voice_output"] = False
        data = load_data()
        data.setdefault("voice", {})
        data["voice"]["enabled"] = False
        save_data(data)
        print(f"{ASSISTANT_NAME}: Voice output muted.")
        return True, "Voice output muted."

    if normalized == "voice on":
        reset_tts_engine()
        STATE["voice_output"] = True
        data = load_data()
        data.setdefault("voice", {})
        data["voice"]["enabled"] = True
        save_data(data)
        speak("Voice output enabled.")
        return True, "Voice output enabled."

    if normalized == "increase voice range" or normalized == "increase mic range":
        data = load_data()
        data.setdefault("voice", {})
        current_thresh = data["voice"].get("energy_threshold", 300)
        new_thresh = max(50, current_thresh - 100)
        current_ratio = data["voice"].get("dynamic_energy_ratio", 1.5)
        new_ratio = max(1.1, current_ratio - 0.2)
        data["voice"]["energy_threshold"] = new_thresh
        data["voice"]["dynamic_energy_ratio"] = new_ratio
        save_data(data)
        say(f"Voice range increased. Mic is now more sensitive (threshold {new_thresh}).")
        return True, reply

    if normalized == "decrease voice range" or normalized == "decrease mic range":
        data = load_data()
        data.setdefault("voice", {})
        current_thresh = data["voice"].get("energy_threshold", 300)
        new_thresh = min(1000, current_thresh + 100)
        current_ratio = data["voice"].get("dynamic_energy_ratio", 1.5)
        new_ratio = min(3.0, current_ratio + 0.2)
        data["voice"]["energy_threshold"] = new_thresh
        data["voice"]["dynamic_energy_ratio"] = new_ratio
        save_data(data)
        say(f"Voice range decreased. Mic is now less sensitive (threshold {new_thresh}).")
        return True, reply

    if normalized == "voice list":
        if pyttsx3 is None:
            say("Voice listing requires pyttsx3 installed.")
            return True, reply
        if not init_tts(force_retry=True) or tts_engine is None:
            say(f"Voice output unavailable ({tts_error_message}).")
            return True, reply
        try:
            voices = list(
                cast(Iterable[Any], cast(Any, tts_engine).getProperty("voices") or [])
            )
            if not voices:
                say("No voices available.")
                return True, reply
            for i, v in enumerate(voices):
                speak(f"{i}: {getattr(v, 'name', str(v))}")
            return True, "Voices listed."
        except Exception:
            say("Unable to list voices.")
            return True, reply

    if normalized.startswith("voice backend "):
        backend = normalized.split(maxsplit=2)[2].strip()
        allowed = {"auto", "google", "openai", "sphinx"}
        if backend not in allowed:
            say("Use auto, google, openai, or sphinx.")
            return True, reply
        data = load_data()
        data.setdefault("voice", {})
        data["voice"]["backend"] = backend
        save_data(data)
        say(f"Voice backend set to {backend}.")
        return True, reply

    if normalized.startswith("voice output backend "):
        backend = normalized[len("voice output backend ") :].strip()
        allowed = {"auto", "openai", "local", "pyttsx3"}
        if backend not in allowed:
            say("Use auto, openai, or local.")
            return True, reply
        if backend == "local":
            backend = "pyttsx3"
        data = load_data()
        data.setdefault("voice", {})
        data["voice"]["output_backend"] = backend
        save_data(data)
        say(f"Voice output backend set to {backend}.")
        return True, reply

    if normalized.startswith("voice output model "):
        model = normalized[len("voice output model ") :].strip()
        allowed = {"alloy", "echo", "fable", "onyx", "nova", "shimmer"}
        if model not in allowed:
            say(f"Use {', '.join(allowed)}.")
            return True, reply
        data = load_data()
        data.setdefault("voice", {})
        data["voice"]["output_model"] = model
        save_data(data)
        say(f"Smart voice model set to {model}.")
        return True, reply

    if normalized.startswith("voice language "):
        language = command.strip()[len("voice language ") :].strip()
        if not language:
            say("Specify a language code like en-US.")
            return True, reply
        data = load_data()
        data.setdefault("voice", {})
        data["voice"]["language"] = language
        save_data(data)
        say(f"Voice language set to {language}.")
        return True, reply

    if normalized.startswith("voice set ") or normalized.startswith("voice range "):
        parts = normalized.split()
        if len(parts) < 3:
            say("Specify voice index.")
            return True, reply
        try:
            idx = int(parts[2])
        except ValueError:
            say("Index must be a number.")
            return True, reply
        data = load_data()
        data.setdefault("voice", {})
        data["voice"]["index"] = idx
        save_data(data)
        reset_tts_engine()
        init_tts(force_retry=True)
        say(f"Voice set to index {idx}.")
        return True, reply

    if normalized.startswith("voice rate "):
        parts = normalized.split()
        if len(parts) < 3:
            say("Specify rate value.")
            return True, reply
        try:
            rate = int(parts[2])
        except ValueError:
            say("Rate must be a number.")
            return True, reply
        # clamp reasonable bounds
        rate = max(50, min(400, rate))
        data = load_data()
        data.setdefault("voice", {})
        data["voice"]["rate"] = rate
        save_data(data)
        reset_tts_engine()
        init_tts(force_retry=True)
        say(f"Voice rate set to {rate}.")
        return True, reply

    if normalized.startswith("voice volume "):
        parts = normalized.split()
        if len(parts) < 3:
            say("Specify volume between 0.0 and 1.0.")
            return True, reply
        try:
            vol = float(parts[2])
        except ValueError:
            say("Volume must be a decimal number.")
            return True, reply
        vol = max(0.0, min(1.0, vol))
        data = load_data()
        data.setdefault("voice", {})
        data["voice"]["volume"] = vol
        save_data(data)
        reset_tts_engine()
        init_tts(force_retry=True)
        say(f"Voice volume set to {vol}.")
        return True, reply

    if normalized == "voice info":
        vcfg = get_voice_settings()
        idx = int(vcfg.get("index", 0))
        rate = int(vcfg.get("rate", 178))
        vol = float(vcfg.get("volume", 1.0))
        backend = str(vcfg.get("backend", "auto"))
        language = str(vcfg.get("language", "en-US"))
        state = "on" if STATE["voice_output"] else "off"
        info = (
            f"State {state}, index {idx}, rate {rate}, volume {vol}, "
            f"backend {backend}, language {language}"
        )
        # attempt to append active voice name
        if pyttsx3 is not None:
            try:
                if init_tts(force_retry=True) and tts_engine is not None:
                    voices = list(
                        cast(
                            Iterable[Any],
                            cast(Any, tts_engine).getProperty("voices") or [],
                        )
                    )
                    name = (
                        getattr(voices[idx], "name", None)
                        if voices and 0 <= idx < len(voices)
                        else None
                    )
                    if name:
                        info += f", voice '{name}'"
            except Exception:
                pass
        if tts_error_message:
            info += f", tts unavailable ({tts_error_message})"
        elif pyttsx3 is None:
            info += ", tts unavailable (pyttsx3 is not installed)"
        else:
            info += ", tts ready"
        say(info)
        return True, reply

    if normalized == "listen":
        heard = voice_listen_once()
        if not heard:
            return True, "No voice command."
        if STATE["wake_word_enabled"]:
            vcfg = load_data().get("voice", {})
            wake_word = (vcfg.get("wake_word") or "hey friday").strip()
            matched, _ = extract_command_after_wake_word(heard, wake_word)
            if not matched:
                say("Wake word not detected.")
                return True, reply
            heard, wake_reply = wake_word_follow_up(heard, wake_word)
            if not heard:
                return True, wake_reply or "No voice command."
        return execute_command(heard, remote=remote)

    if normalized == "start voice mode":
        if sr is None:
            say("Voice input needs SpeechRecognition and PyAudio installed.")
            return True, reply
        if STATE["wake_word_enabled"]:
            vcfg = load_data().get("voice", {})
            wake_word = (vcfg.get("wake_word") or "hey friday").strip()
            say(f"Voice mode started. Use wake word: {wake_word}.")
        else:
            say("Voice mode started without wake word filter.")
        try:
            while True:
                heard = voice_listen_once()
                if not heard:
                    continue
                heard_text = heard.strip()
                if STATE["wake_word_enabled"]:
                    matched, _ = extract_command_after_wake_word(heard_text, wake_word)
                    if not matched:
                        continue
                    heard_text, _ = wake_word_follow_up(heard_text, wake_word)
                    if not heard_text:
                        continue
                cont, _ = execute_command(heard_text, remote=remote)
                if not cont:
                    break
        except KeyboardInterrupt:
            say("Voice mode interrupted.")
        return True, "Voice mode ended."

    if normalized == "wake word on":
        STATE["wake_word_enabled"] = True
        say("Wake word enabled.")
        return True, reply

    if normalized.startswith("wake word set "):
        # preserve user's casing/spacing for the wake phrase
        wake_text = command.strip()[len("wake word set ") :].strip()
        if not wake_text:
            say("Specify the wake word to set.")
            return True, reply
        data = load_data()
        data.setdefault("voice", {})
        data["voice"]["wake_word"] = wake_text
        save_data(data)
        say(f"Wake word set to: {wake_text}")
        return True, reply

    if normalized == "wake word off":
        STATE["wake_word_enabled"] = False
        say("Wake word disabled.")
        return True, reply

    if normalized == "start device server":
        say(start_device_server())
        return True, reply

    if normalized == "show api key":
        say(f"API key: {get_api_key()}")
        return True, reply

    if normalized.startswith("gemini "):
        prompt = command.strip()[len("gemini ") :].strip()
        if not prompt:
            say("Specify prompt for Gemini.")
            return True, reply
        say("Querying Gemini...")
        resp = call_gemini(prompt)
        say(resp)
        return True, reply

    if normalized == "device ip":
        say(f"My device address is {local_ip()}")
        return True, reply

    if normalized.startswith("file search "):
        query = command.strip()[12:].strip()
        if not query:
            say("Specify filename to search for.")
            return True, reply
        root = Path(".")
        matches = []
        try:
            for p in root.rglob("*"):
                if p.is_file() and query.lower() in p.name.lower():
                    matches.append(str(p.as_posix()))
                    if len(matches) >= 50:
                        break
        except Exception:
            say("Error while searching files.")
            return True, reply
        if not matches:
            say("No files found.")
            return True, reply
        say(f"Found {len(matches)} file(s). Listing up to 50 results.")
        for m in matches:
            speak(m)
        return True, reply

    if normalized.startswith("file grep "):
        query = command.strip()[10:].strip()
        if not query:
            say("Specify text to search inside files.")
            return True, reply
        root = Path(".")
        results = []
        try:
            for p in root.rglob("*"):
                if not p.is_file():
                    continue
                # skip large files
                try:
                    if p.stat().st_size > 2_000_000:
                        continue
                except Exception:
                    pass
                try:
                    text = p.read_text(encoding="utf-8", errors="ignore")
                except Exception:
                    continue
                for i, line in enumerate(text.splitlines(), 1):
                    if query.lower() in line.lower():
                        snippet = line.strip()
                        results.append(f"{p.as_posix()}:{i}: {snippet}")
                        if len(results) >= 50:
                            break
                if len(results) >= 50:
                    break
        except Exception:
            say("Error while grepping files.")
            return True, reply
        if not results:
            say("No matches found.")
            return True, reply
        say(f"Found {len(results)} matches. Listing up to 50.")
        for r in results:
            speak(r)
        return True, reply

    if normalized == "open youtube":
        webbrowser.open("https://www.youtube.com")
        say("Opening YouTube.")
        return True, reply

    if normalized == "open google":
        webbrowser.open("https://www.google.com")
        say("Opening Google.")
        return True, reply

    if normalized.startswith("search "):
        q = command.strip()[7:].strip()
        if not q:
            say("Search query missing.")
            return True, reply
        webbrowser.open(f"https://www.google.com/search?q={q.replace(' ', '+')}")
        say(f"Searching for {q}.")
        return True, reply

    say("Command not recognized. Say help.")
    # Fallback: try to find a close matching command and run it automatically
    try:
        import difflib

        candidates = [
            "suit up",
            "retract suit",
            "suit status",
            "flight mode on",
            "flight mode off",
            "combat mode on",
            "combat mode off",
            "threat scan",
            "diagnostics",
            "arc reactor",
            "recharge",
            "protocol ",
            "ai mode ",
            "note add ",
            "notes",
            "clear notes",
            "remind me ",
            "reminders",
            "voice on",
            "voice off",
            "voice list",
            "voice set ",
            "voice rate ",
            "voice volume ",
            "voice backend ",
            "voice language ",
            "voice info",
            "listen",
            "start voice mode",
            "wake word on",
            "wake word off",
            "wake word set ",
            "start device server",
            "show api key",
            "device ip",
            "open youtube",
            "open google",
            "search ",
            "file search ",
            "file grep ",
            "memory add ",
            "memory list",
            "memory clear",
        ]
        # substring-based matches
        matches = [
            c
            for c in candidates
            if normalized.startswith(c.strip())
            or c.strip() in normalized
            or normalized in c.strip()
        ]
        # fuzzy matches when no substring matches
        if not matches:
            names = [c.strip() for c in candidates]
            close = difflib.get_close_matches(normalized, names, n=3, cutoff=0.6)
            matches = close
        if matches:
            # pick the best match
            best = matches[0]
            # if candidate expects args but normalized doesn't provide them, don't auto-run
            if best.endswith(" ") and best.strip() not in normalized:
                say(f"Did you mean '{best.strip()}'? Provide arguments.")
                return True, reply
            # attempt to run matched command
            say(f"Interpreting as: {best.strip()}")
            return execute_command(best.strip(), remote=remote)
    except Exception:
        pass
    return True, reply


def run_assistant() -> None:
    get_api_key()
    sync_voice_state_from_config()
    reset_tts_engine()
    startup_msg = start_device_server()
    speak(f"Good evening {USER_NAME}. {ASSISTANT_NAME} online.")
    speak("Type help for command list.")
    speak(startup_msg)
    while True:
        try:
            user_input = input("You: ")
        except EOFError:
            speak("Input stream closed. Powering down.")
            break
        except KeyboardInterrupt:
            # Ignore Ctrl+C at the prompt and continue running
            print(f"{ASSISTANT_NAME}: Interrupt ignored.")
            continue
        keep_running, _ = execute_command(user_input)
        if not keep_running:
            break


if __name__ == "__main__":
    run_assistant()
