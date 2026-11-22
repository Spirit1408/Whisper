# Local Dictation (Whisper)

System-wide push-to-talk dictation for Windows. Hold a hotkey, speak, release — the text
appears in the editor. Use **Copy**, then paste into the intended field. Speech recognition runs locally via
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

Проверки текущей версии, измерения до/после и ручной smoke приведены в [результатах M0](docs/M0-RESULTS.md).

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
- Automatic paste is paused until a recipient can be verified. The clipboard changes only when you choose Copy.

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

Prepare the Whisper snapshot locally. Set `model.name` to a complete local model directory or a model already in the Hugging Face cache. A missing snapshot or tokenizer produces a startup notice.

## Run

- **`run.bat`** — double-click, runs with a console window (good for debugging)
- **`run_hidden.vbs`** — double-click, runs without a console; technical events go to `app-technical.log`
- Manual: `.venv\Scripts\python.exe src\main.py`

Autostart with Windows is toggled by the "Start with Windows" checkbox in the app
window (it points the registry Run entry at `run_hidden.vbs`).

Wait for Ready, hold `Ctrl+Win`, speak and release. The window shows the result, the history-write outcome and the Copy action. If hidden, the app also sends a tray notification.

## LM Studio (optional, for post-processing only)

The app works fully without LM Studio — it is used only for text cleanup when the
"LM Studio post-processing" checkbox is enabled. On startup the app does a
non-intrusive probe; if the server is offline the checkbox stays disabled.

Click **⟳** to check the local service. With the CLI on the default loopback endpoint the app can start the server and load a downloaded model. Use the dropdown when several models are available. Smart becomes available once the chosen instance is reported as loaded. A failed or incomplete response leaves the raw transcription in the editor and shows **Smart fallback**.

Switching models issues no unload command. If there is insufficient memory, choose a smaller local model or manage the shared service yourself. Inference accepts a loopback endpoint and disables HTTP redirects and environment proxies.

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
