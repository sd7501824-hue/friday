import datetime
import os
from flask import Flask, jsonify, render_template, request
import assistant

app = Flask(__name__)


def build_dashboard_payload() -> dict:
    try:
        data = assistant.load_data()
    except Exception:
        data = {}

    voice_cfg = data.get("voice", {}) if isinstance(data, dict) else {}
    notes = list(data.get("notes", [])) if isinstance(data, dict) else []
    reminders = list(data.get("reminders", [])) if isinstance(data, dict) else []
    memories = list(data.get("memories", [])) if isinstance(data, dict) else []

    return {
        "assistant": assistant.ASSISTANT_NAME,
        "user": assistant.USER_NAME,
        "status": {
            "reactor": assistant.STATE.get("arc_reactor"),
            "protocol": assistant.STATE.get("protocol"),
            "armor_deployed": assistant.STATE.get("armor_deployed"),
            "flight_mode": assistant.STATE.get("flight_mode"),
            "combat_mode": assistant.STATE.get("combat_mode"),
            "ai_mode": assistant.STATE.get("ai_mode"),
            "voice_output": assistant.STATE.get("voice_output"),
            "wake_word_enabled": assistant.STATE.get("wake_word_enabled"),
        },
        "voice": {
            "wake_word": voice_cfg.get("wake_word", "hey friday"),
            "backend": voice_cfg.get("backend", "auto"),
            "language": voice_cfg.get("language", "en-US"),
        },
        "counts": {
            "notes": len(notes),
            "reminders": len(reminders),
            "memories": len(memories),
        },
        "notes": notes[-5:][::-1],
        "reminders": reminders[-5:][::-1],
        "memories": memories[-5:][::-1],
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
    }


@app.route("/")
def home():
    return render_template(
        "index.html",
        assistant_name=assistant.ASSISTANT_NAME,
        user_name=assistant.USER_NAME,
    )


@app.get("/api/dashboard")
def dashboard():
    return jsonify(build_dashboard_payload())


@app.post("/ask")
def ask():
    """Forward the message to the real assistant and return its reply."""
    payload = request.get_json(silent=True) or {}
    user_message = (payload.get("message") or "").strip()
    if not user_message:
        return jsonify({"reply": ""})
    try:
        _, reply = assistant.execute_command(user_message, remote=True)
    except Exception as e:
        reply = f"Error executing command: {e}"
    return jsonify({"reply": reply})


@app.post("/echo")
def echo():
    """Return a simple echo reply useful for testing without invoking assistant logic."""
    payload = request.get_json(silent=True) or {}
    user_message = (payload.get("message") or "").strip()
    return jsonify({"reply": f"{assistant.ASSISTANT_NAME}: {user_message}"})


def check_auth_payload(payload: dict | None = None) -> bool:
    header_key = request.headers.get("x-api-key", "")
    auth_hdr = request.headers.get("Authorization", "")
    bearer = ""
    if isinstance(auth_hdr, str) and auth_hdr.lower().startswith("bearer "):
        bearer = auth_hdr.split(" ", 1)[1].strip()
    body_key = ""
    if payload and isinstance(payload, dict):
        body_key = str(payload.get("api_key", "")).strip()
    # Prefer an explicit environment variable for the API key in deployed
    # environments. Fall back to the stored/generated key from assistant.
    api_key = os.environ.get("FRIDAY_API_KEY") or assistant.get_api_key()
    return api_key in (header_key, bearer, body_key)


@app.get("/status")
def status():
    return jsonify(
        {
            "assistant": assistant.ASSISTANT_NAME,
            "reactor": assistant.STATE.get("arc_reactor"),
            "protocol": assistant.STATE.get("protocol"),
            "armor_deployed": assistant.STATE.get("armor_deployed"),
            "ai_mode": assistant.STATE.get("ai_mode"),
        }
    )


@app.get("/debug_info")
def debug_info():
    try:
        cfg = assistant.load_data()
    except Exception:
        cfg = {}
    return jsonify(
        {
            "assistant_module": getattr(assistant, "__file__", None),
            "cwd": os.getcwd(),
            "env_openai": os.environ.get("OPENAI_API_KEY"),
            "config_openai": (cfg.get("integrations", {}) or {}).get("openai", {}),
        }
    )


@app.post("/command")
def remote_command():
    payload = request.get_json(silent=True) or {}
    if not check_auth_payload(payload):
        return jsonify({"ok": False, "error": "Unauthorized"}, 401)
    cmd = (payload.get("command") or "").strip()
    if not cmd:
        return jsonify({"ok": False, "error": "Missing command"}, 400)
    keep_running, reply = assistant.execute_command(cmd, remote=True)
    return jsonify({"ok": True, "keep_running": keep_running, "reply": reply})


@app.get("/memories")
def api_get_memories():
    if not check_auth_payload():
        return jsonify({"ok": False, "error": "Unauthorized"}, 401)
    # support query, limit, since (handled here simply by reusing assistant logic)
    args = request.args
    query = (args.get("query") or "").strip().lower()
    limit = args.get("limit")
    try:
        limit = int(limit) if limit is not None else 50
    except Exception:
        limit = 50
    limit = max(1, min(200, limit))
    since = (args.get("since") or "").strip()

    data = assistant.load_data()
    mems = list(data.get("memories", []))

    def parse_stamp(entry: str):
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
            try:
                since_dt = datetime.datetime.fromisoformat(since)
            except Exception:
                since_dt = datetime.datetime.strptime(since, "%d %b %Y %I:%M %p")
        except Exception:
            return jsonify({"ok": False, "error": "Invalid since format"}, 400)
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

    return jsonify({"ok": True, "count": len(out), "memories": out})


@app.post("/memories")
def api_add_memory():
    payload = request.get_json(silent=True) or {}
    if not check_auth_payload(payload):
        return jsonify({"ok": False, "error": "Unauthorized"}, 401)
    text = (payload.get("text") or "").strip()
    if not text:
        return jsonify({"ok": False, "error": "Missing text"}, 400)
    data = assistant.load_data()
    data.setdefault("memories", [])
    stamp = datetime.datetime.now().strftime("%d %b %Y %I:%M %p")
    entry = f"{stamp} - {text}"
    data["memories"].append(entry)
    assistant.save_data(data)
    return jsonify({"ok": True, "memory": entry})


@app.delete("/memories")
def api_clear_memories():
    if not check_auth_payload():
        return jsonify({"ok": False, "error": "Unauthorized"}, 401)
    data = assistant.load_data()
    data["memories"] = []
    assistant.save_data(data)
    return jsonify({"ok": True, "cleared": True})


@app.delete("/memories/<int:index>")
def api_delete_memory(index: int):
    if not check_auth_payload():
        return jsonify({"ok": False, "error": "Unauthorized"}, 401)
    data = assistant.load_data()
    mems = data.get("memories", [])
    if index < 1 or index > len(mems):
        return jsonify({"ok": False, "error": "Index out of range"}, 404)
    removed = mems.pop(index - 1)
    data["memories"] = mems
    assistant.save_data(data)
    return jsonify({"ok": True, "removed": removed})


if __name__ == "__main__":
    import waitress
    print("Starting FRIDAY server on http://0.0.0.0:5050")
    waitress.serve(app, host="0.0.0.0", port=5050)
