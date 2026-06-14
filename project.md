# Project: Local Dictation (Whisper)

System-wide push-to-talk dictation app for Windows. Fully local: faster-whisper on GPU
(RTX 4070 SUPER) + optional text cleanup via LM Studio.

## Tech Stack

- Python 3.11+, virtualenv
- STT: `faster-whisper` (CTranslate2, CUDA), model `large-v3-turbo` (int8_float16)
- Audio: `sounddevice` (16 kHz mono float32)
- Hotkeys: `keyboard` (low-level hook for push-to-talk)
- Text insertion: `pyperclip` + Ctrl+V with clipboard restore
- GUI + tray: PySide6 (dark QSS theme, QSystemTrayIcon); pystray/Pillow removed
- Post-processing: LM Studio OpenAI-compatible API (`requests`, localhost:1234)
- Config: `config.yaml` (PyYAML); UI state in `state.json`, history in `history.json`

## Architecture & Decisions

- Flow: hotkey press → record → release → transcribe (GPU) → optional LLM cleanup → paste
  → history card in GUI
- Model loads once at startup with warm-up (dummy transcription) for low dictation latency
- LM Studio is LLM-only (cannot do STT) — used solely for punctuation/filler cleanup,
  with graceful fallback to raw text on timeout/unavailability
- LM Studio availability probed at startup (GET /v1/models, 2 s timeout); checkbox
  disabled while server offline, manual ⟳ re-check button
- Worker threads emit Qt signals via `GuiBridge` (thread-safe GUI updates)
- Closing the window hides to tray (`closeEvent` + `really_quit` app property);
  quit only via tray menu
- Autostart via HKCU Run registry key (`src/autostart.py`), checkbox reflects
  actual registry state; old .bat/.lnk approach removed
- Mic selector: MME device enumeration (WASAPI indices reject 16000 Hz),
  device name persisted in state.json, resolved to MME index at runtime;
  priority: GUI selection > config.yaml > system default; live switching (stream
  is created per recording)
- Clipboard paste chosen over char-by-char typing for reliable Cyrillic input
- cuBLAS/cuDNN DLLs come from pip packages (`nvidia-*-cu12`), registered via
  `os.add_dll_directory` in `src/transcriber.py`

## Current Status & Roadmap

- [x] Phases 1-4 done: dictation pipeline, tray, launch scripts, e2e tested (RU + EN)
- [x] GUI (PySide6): dark window — autostart/LM checkboxes, editable text field,
      Copy/Clear, history cards (click → field, hover → delete), close-to-tray
- [x] Persistence: state.json (checkboxes), history.json (50 entries cap)
- [x] GUI verified live: dictation → history, autostart toggle writes registry
- [x] Mic selector combobox (+ ⟳ refresh) verified live: switching works on the fly
- [x] LM Studio post-processing works (checkbox restores on startup when server up)

## Important Files & Modules

- `src/main.py` — entry point, `DictationApp` wires everything, Qt event loop
- `src/gui.py` — `MainWindow` (dark QSS), `HistoryCard`, `GuiBridge` signals
- `src/tray.py` — `TrayIcon` (QSystemTrayIcon), state colors, Show/Quit menu
- `src/transcriber.py` — faster-whisper wrapper, NVIDIA DLL setup, silence filter
- `src/hotkey.py` — `PushToTalkHotkey`, combo press/release tracking
- `src/audio_recorder.py` — mic capture, `list_input_devices()`, `resolve_device()`
- `src/text_inserter.py` — clipboard paste with restore
- `src/postprocessor.py` — LM Studio cleanup with fallback + `check_server()`
- `src/settings.py` / `src/history.py` / `src/autostart.py` — persistence & registry
- `config.yaml` — user settings

## Known Problems / Technical Debt

- `keyboard` hotkey doesn't reach elevated windows unless app runs as admin
- Whisper may hallucinate on silence — mitigated by RMS threshold + `vad_filter=True`
- No settings UI yet (config file only)

## Open Questions / TODO

- Autostart with Windows (Phase 4)
- Possibly a small history window of recent dictations
