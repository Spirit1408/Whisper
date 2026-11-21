# Local Dictation (Whisper)

System-wide push-to-talk dictation for Windows. Hold a hotkey, speak, release — the text
is pasted into the currently focused window. Speech recognition runs locally via
[faster-whisper](https://github.com/SYSTRAN/faster-whisper) on your GPU, optional text
cleanup via a local LLM in [LM Studio](https://lmstudio.ai/).

## Документация проекта

Это руководство описывает работающий Local Dictation из исходников. Local AI Input
Workspace — утверждённая цель развития. M1b обозначает стабильную Raw-диктовку,
M1c — первый стабильный релиз с Raw и Smart. Исходная версия до изменений поведения сохранена в M0-A.

| Что нужно узнать | Документ |
|---|---|
| Возможности продукта, ограничения и границы версии | [Product Vision и спецификация](docs/PRODUCT_SPEC.md) |
| Текущий и целевой путь диктовки, данные и контракты | [Архитектура](docs/ARCHITECTURE.md) |
| Какие решения утверждены и что ещё проверить | [Решения](docs/DECISIONS.md) и [риски](docs/RISKS.md) |
| Порядок этапов и условия выпуска | [Roadmap](docs/ROADMAP.md) |
| Что предстоит делать и как принимать работу | [Backlog и пакеты M0](docs/BACKLOG.md) и [стратегия тестирования](docs/TESTING.md) |
| Как согласовывать и фиксировать изменения | [Правила для агентов](AGENTS.md) и [журнал изменений](CHANGELOG.md) |
| Как восстановить рабочую версию Raw и Smart до изменений M0-B | [Снимок M0-A, версии и процедура проверки](docs/BASELINE.md) |

## Features

- Push-to-talk global hotkey (default: `Ctrl+Win`)
- Russian + English with automatic language detection
- GPU transcription (CUDA, `large-v3-turbo` by default)
- Dark-themed GUI: editable transcription field, Copy/Clear, history cards
  (click a card to load its text, hover to delete)
- Microphone selector with a refresh button — switch the capture device on the fly;
  the choice persists and overrides `audio.device` from `config.yaml`
- Optional punctuation/filler-word cleanup via LM Studio: a model dropdown plus a
  ⟳ button that starts the server and loads a model (via the `lms` CLI), then
  enables the post-processing checkbox
- "Start with Windows" checkbox (managed via the HKCU Run registry key)
- Closing the window hides the app to tray; it keeps working. Quit via tray menu
- Tray icon with state indication (blue = idle, red = recording, orange = processing)
- Checkbox states and history persist between launches (`state.json`, `history.json`)
- Paste attempts to restore previously copied plain text. A new copy made during paste or a non-text clipboard format may be lost.

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

The current loader can contact the model hub and download missing weights (~1.6 GB for large-v3-turbo).

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
"LM Studio post-processing" checkbox is enabled. On startup the app does a
non-intrusive probe; if the server is offline the checkbox stays disabled.

Click the **⟳ button** to bring LM Studio to a ready state: it starts the local
server (`lms server start`) and, if no LLM is loaded, loads one (`lms load`). Once a
model is loaded the **model dropdown** activates and the checkbox is enabled. Pick a
different model in the dropdown to load it on the fly — the choice is remembered in
`state.json` and reused next time. If LM Studio goes down mid-session, the raw
transcription is inserted silently.

In the current implementation, switching models can unload other LLMs in the same LM Studio
service. The current INFO log can also retain both the transcription and the edited text
after a history card is deleted. M0 addresses these behaviours.

This requires LM Studio's **`lms` CLI** (bundled with the desktop app; run
`lms bootstrap` once if `lms` is not on your PATH). To have the server ready
automatically at login instead, enable **Settings → Developer → "Local LLM Service
(headless)"** and keep a model loaded (or enable **JIT model loading**).

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
