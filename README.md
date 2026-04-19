# Iron Man Assistant (FRIDAY / JARVIS)

This project is a command-based assistant with voice hooks and network control.

## Quick Start

```bash
python assistant.py
```

## Install Dependencies (recommended)

Voice and remote device control need extra packages:

```bash
pip install pyttsx3 SpeechRecognition pyaudio flask
```

## Core Commands

- `help`
- `suit up`
- `retract suit`
- `suit status`
- `flight mode on` / `flight mode off`
- `combat mode on` / `combat mode off`
- `threat scan`
- `diagnostics`
- `arc reactor`
- `recharge`
- `protocol passive|defense|stealth`
- `ai mode assistant|tactical|aggressive`

## Memory Commands

- `note add buy new armor paint`
- `notes`
- `clear notes`
- `remind me call Pepper at 8`
- `reminders`

## Voice Commands

- `voice on`
- `voice off`
- `listen` (single voice command)
- `start voice mode` (continuous mode)
- `wake word on`
- `wake word off`

Wake-word examples:

- `hey friday suit up`
- `hey friday suit status`

## Device Control (Phone/Laptop on same Wi-Fi)

1. Start assistant: `python assistant.py`
2. Get network address: `device ip`
3. Get API key: `show api key`
4. Send commands from another device:

```bash
curl http://YOUR_PC_IP:5050/status
curl -X POST http://YOUR_PC_IP:5050/command -H "Content-Type: application/json" -H "x-api-key: YOUR_API_KEY" -d "{\"command\":\"suit status\"}"
```

## Data Files

- `friday_data.json`
- `jarvis_data.json`

Both files now follow a shared schema (`notes`, `reminders`, `voice`, `profiles`, `security`, `missions`, `telemetry`, `integrations`), so future features can read either file.
