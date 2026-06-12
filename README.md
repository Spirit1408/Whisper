# Local Dictation (Whisper)

System-wide push-to-talk dictation for Windows. Hold a hotkey, speak, release — the text
is typed into the active window. Runs fully locally: speech-to-text via
[faster-whisper](https://github.com/SYSTRAN/faster-whisper) on your GPU, optional text
cleanup via a local LLM in [LM Studio](https://lmstudio.ai/).

## Features

- Push-to-talk global hotkey (default: `Ctrl+Win`)
- Russian + English with automatic language detection
- GPU transcription (CUDA, `large-v3-turbo` by default, ~1.5 GB VRAM)
- Dark-themed GUI: editable transcription field, Copy/Clear, history cards
  (click a card to load its text, hover to delete)
- Optional punctuation/filler-word cleanup via LM Studio (checkbox with automatic
  server availability check and a manual re-check button)
- "Start with Windows" checkbox (managed via the HKCU Run registry key)
- Closing the window hides the app to tray; it keeps working. Quit via tray menu
- Tray icon with state indication (blue = idle, red = recording, orange = processing)
- Checkbox states and history persist between launches (`state.json`, `history.json`)
- Clipboard is preserved when pasting text

## Requirements

- Windows 10/11
- NVIDIA GPU with CUDA support (tested target: RTX 4070 SUPER)
- Python 3.11+
- (Optional) LM Studio running a local server on `localhost:1234`

## Setup

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

The Whisper model is downloaded automatically on first run (~1.6 GB for large-v3-turbo).

## Run

- **`run.bat`** — double-click, runs with a console window (good for debugging)
- **`run_hidden.vbs`** — double-click, runs without a console; logs go to `app.log`
- Manual: `.venv\Scripts\python.exe src\main.py`

Autostart with Windows is toggled by the "Start with Windows" checkbox in the app
window (it points the registry Run entry at `run_hidden.vbs`).

Wait for the "Ready" tray notification (model loads in ~5-15 s), then hold `Ctrl+Win`
and speak. Release to insert the text into the focused window.

## LM Studio (optional, for post-processing only)

The app works fully without LM Studio — it is used only for text cleanup when the
"LM Studio post-processing" checkbox is enabled. On startup the app probes the server;
if it is unreachable, the checkbox is unchecked and disabled — use the ⟳ button to
re-check after starting the server. If LM Studio goes down mid-session, the raw
transcription is inserted silently.

For post-processing to work, LM Studio's local server must be running with a model
loaded. To avoid starting it manually after each reboot:

1. In LM Studio: **Settings → Developer → enable "Local LLM Service (headless)"** —
   the server starts on login without opening the LM Studio window.
2. Enable **JIT model loading** (Developer settings) so the model is loaded
   automatically on the first request, or keep your model loaded persistently.
3. Alternatively, use the CLI: `lms server start` (can be added to startup).

## Configuration

Edit `config.yaml`:

- `hotkey` — push-to-talk combo, e.g. `ctrl+windows`, `ctrl+alt`, `f9`
- `model.name` — `large-v3-turbo` (fast) or `large-v3` (max accuracy)
- `language` — `auto`, `ru` or `en` (fixing the language is slightly faster/more accurate)
- `postprocess.base_url` — LM Studio server address (cleanup itself is toggled
  by the checkbox in the app window)

## Notes

- The hotkey will not work inside windows running as Administrator unless this app is
  also started as Administrator (limitation of low-level keyboard hooks).
- If CUDA DLLs are not found, make sure `nvidia-cublas-cu12` and `nvidia-cudnn-cu12`
  are installed in the same virtualenv (they are in `requirements.txt`).
