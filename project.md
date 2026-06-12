# Project: Local Dictation (Whisper)

System-wide push-to-talk dictation app for Windows. Fully local: faster-whisper on GPU
(RTX 4070 SUPER) + optional text cleanup via LM Studio.

## Tech Stack

- Python 3.11+, virtualenv
- STT: `faster-whisper` (CTranslate2, CUDA), model `large-v3-turbo` (int8_float16)
- Audio: `sounddevice` (16 kHz mono float32)
- Hotkeys: `keyboard` (low-level hook for push-to-talk)
- Text insertion: `pyperclip` + Ctrl+V with clipboard restore
- Tray: `pystray` + `Pillow`
- Post-processing: LM Studio OpenAI-compatible API (`requests`, localhost:1234)
- Config: `config.yaml` (PyYAML)

## Architecture & Decisions

- Flow: hotkey press → record → release → transcribe (GPU) → optional LLM cleanup → paste
- Model loads once at startup with warm-up (dummy transcription) for low dictation latency
- LM Studio is LLM-only (cannot do STT) — used solely for punctuation/filler cleanup,
  with graceful fallback to raw text on timeout/unavailability
- Processing runs in a worker thread (keyboard hook callbacks must stay fast)
- Clipboard paste chosen over char-by-char typing for reliable Cyrillic input
- cuBLAS/cuDNN DLLs come from pip packages (`nvidia-*-cu12`), registered via
  `os.add_dll_directory` in `src/transcriber.py`

## Current Status & Roadmap

- [x] Plan agreed (see `.windsurf/plans/whisper-dictation-plan-d44d8b.md`)
- [x] Phase 1-3 code written: recorder, transcriber, hotkey, inserter, tray, LM Studio postproc
- [x] Dependencies installed (.venv), CUDA verified, model downloaded + warm-up smoke test OK
- [x] End-to-end test passed (RU + EN dictation, text insertion)
- [x] Phase 4: run.bat, run_hidden.vbs (windowless, logs to app.log), autostart scripts
- [ ] LM Studio post-processing live test

## Important Files & Modules

- `src/main.py` — entry point, `DictationApp` wires everything
- `src/transcriber.py` — faster-whisper wrapper, NVIDIA DLL setup, silence filter
- `src/hotkey.py` — `PushToTalkHotkey`, combo press/release tracking
- `src/audio_recorder.py` — mic capture into numpy buffer
- `src/text_inserter.py` — clipboard paste with restore
- `src/postprocessor.py` — LM Studio cleanup with fallback
- `src/tray.py` — pystray icon, state colors, menu
- `config.yaml` — user settings

## Known Problems / Technical Debt

- `keyboard` hotkey doesn't reach elevated windows unless app runs as admin
- Whisper may hallucinate on silence — mitigated by RMS threshold + `vad_filter=True`
- No settings UI yet (config file only)

## Open Questions / TODO

- Autostart with Windows (Phase 4)
- Possibly a small history window of recent dictations
