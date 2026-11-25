# Local Dictation (Whisper)

System-wide push-to-talk dictation for Windows. Hold a hotkey, speak and release. The text
appears in the editor and history. Use **Copy** to paste it yourself, or enable **Auto Paste**
for compatible Windows document fields. Speech recognition runs locally via
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

Приёмка M0 и исходные измерения описаны в [результатах M0](docs/M0-RESULTS.md). Для M1a доступны [проверки и запуск в отдельном профиле](docs/TESTING.md).

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
- History and preferences persist in SQLite. Open Original on a Smart card to recover the transcription when the edited text differs. Older entries are available in pages of 50.
- **Artifact Only** is the default. **Auto Paste** addresses the verified document range directly. Only an explicit **Copy** replaces the clipboard.

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

Wait for Ready, hold `Ctrl+Win`, speak and release. The window shows the result, save status and Copy action. If hidden, it also sends a tray notification. Cancel dictation stops capture or discards an outstanding backend response. Release the hotkey before recording again. The default capture limit is 120 seconds at 16 kHz.

### Delivering a dictation

Choose **Auto Paste · verified fields**, place the caret or select a short span in a test document, then hold the hotkey and speak after the recording signal. The two candidate profiles are packaged Windows Notepad with a native `RichEditD2DPT` document and system WordPad with `RICHEDIT50W`. Each dictation checks the active process, window, field, range and clipboard sequence. Single-line text can be inserted or replace the verified selection. A failed check leaves the result available for Copy. Terminals, browsers, chats, password and elevated windows use manual delivery.

**Delivery confirmed** means the exact text was read back from the addressed range. After **Paste dispatched** or **Delivery unknown**, check the original field before pasting manually. Restarting never resends. Choose **Artifact Only** to turn off recipient inspection. Use **Copy** on the current editor text, including edits or an unsaved result. See the [Windows test matrix and release check](docs/TESTING.md).

The default database is `%LOCALAPPDATA%/LocalAIInputWorkspace/artifacts.sqlite3`. On first launch the app imports the old `history.json` and known preferences from `state.json`, leaving both files in place. Legacy cards contain the known text, with an unknown Original. On a write error, unsaved text stays available for Copy until Quit. To use an isolated profile, run `.venv\Scripts\python.exe src\main.py --profile-dir <test-directory>`. The database, legacy inputs and log for that session belong to that directory.

## LM Studio (optional, for post-processing only)

Raw works independently of LM Studio. Enable **LM Studio post-processing** to request Smart. The app checks the local service at startup. If a previously selected Smart model is unavailable, the checkbox keeps your choice. You can uncheck it to switch explicitly to Raw.

Click **⟳** to check the local service. With the CLI on the default loopback endpoint the app can start the server and load a downloaded model. Use the dropdown when several models are available. Smart reads the saved Original and saves the completed correction before Auto Paste. On a timeout, unavailable model or invalid response, **Smart fallback** shows the reason and leaves the Original in the editor for manual Copy. If saving a valid correction fails, Copy can recover it from a RAM card until Quit.

Switching models issues no unload command. If there is insufficient memory, choose a smaller local model or manage the shared service yourself. Inference accepts a loopback endpoint and disables HTTP redirects and environment proxies.

### Presets and saved results

Choose **Active preset** for future sessions or **Next operation** for a single dictation or **Run preset**. Raw keeps the source. Smart Strict corrects punctuation and checks the words, numbers and question boundaries. Natural smooths spoken phrasing and Professional changes the register. Prompt Engineer rewrites a request, Translator translates, Formatter structures text, Task Extractor lists tasks, and Summarizer condenses the source.

Select Translator and open **Configure** to choose the target language (initially English). Each model-based preset can inherit the global model and timeout or use a loaded LM Studio model key and a timeout of 1–300 seconds. Refresh the model list after changing loaded instances. **New** opens the custom instruction editor. **Export JSON** writes the active custom definition to a new local file. **Import JSON** creates a separate custom preset.

Open a history card and select Original or a saved preset result next to **Run preset**. Reprocessing always saves to the card. **Save edit** updates the selected result. Editing Original creates a Raw result and leaves Original available. Choose **Discard edit** to reload a saved version. A failed or cancelled repeat preserves earlier results. A failed specialized dictation leaves the source available for manual recovery and skips Auto Paste.

On the first upgrade from SQLite v1, the app saves a verified v1 backup beside the database (`artifacts.sqlite3.v1-*`) before migrating to v2. For rollback, open a separate M1c profile with a copy of that backup named `artifacts.sqlite3`. Keep the v2 profile for entries and edits made after the upgrade.

## Configuration

Edit `config.yaml`:

- `hotkey` — push-to-talk combo, e.g. `ctrl+windows`, `ctrl+alt`, `f9`
- `model.name` — `large-v3-turbo` (fast) or `large-v3` (max accuracy)
- `language` — `auto`, `ru` or `en` (fixing the language is slightly faster/more accurate)
- `postprocess.base_url` — loopback LM Studio endpoint for model-based presets
- `postprocess.timeout_seconds` — global response timeout, inherited by presets unless overridden
- `delivery.excluded_processes` — additional executable filenames to exclude from Auto Paste, for example `["notepad.exe"]`.

## Notes

- The hotkey will not work inside windows running as Administrator unless this app is
  also started as Administrator (limitation of low-level keyboard hooks).
- If CUDA DLLs are not found, make sure `nvidia-cublas-cu12` and `nvidia-cudnn-cu12`
  are installed in the same virtualenv (they are in `requirements.txt`).
