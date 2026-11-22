"""One active request. Inference runs outside the lock that orders cancel and commit."""
from __future__ import annotations

from copy import copy
from dataclasses import dataclass, field
import threading
from uuid import uuid4

from history import StaleAttempt, StorageError


@dataclass
class Request:
    processor: object
    id: str = field(default_factory=lambda: uuid4().hex)
    cancelled: bool = False
    finished: bool = False


@dataclass(frozen=True)
class Outcome:
    id: str
    text: str
    processing: str
    storage: str
    delivery: str = "skipped"
    error: str | None = None

    @property
    def message(self):
        saved = {"none": "not saved to history", "original": "Original saved",
                 "result": "Original and Smart result saved"}[self.storage]
        if self.error == "result_write_failed":
            saved += ", Smart result not saved"
        if self.error == "status_write_failed":
            saved += ", processing status not saved"
        delivery = "Paste skipped" if self.delivery == "skipped" else "Manual copy only"
        return f"{self.processing} · {saved}. {delivery}. Use Copy."


class Coordinator:
    def __init__(self, store, publish):
        self.store = store
        self.publish = publish
        self.lock = threading.RLock()
        self.active = None
        self.closed = False
        self.unsaved = {}

    def admit(self, processor):
        with self.lock:
            if self.closed or self.active is not None:
                return None
            self.active = Request(copy(processor))
            return self.active

    def _finish_status(self, job, processing, error=None):
        try:
            self.store.finish(job.id, processing, error)
        except StorageError:
            return False
        return True

    def cancel(self, closing=False):
        with self.lock:
            self.closed |= closing
            job = self.active
            if job is None or job.cancelled or job.finished:
                return False
            job.cancelled = True
            self._finish_status(job, "cancelled")
            return True

    def release(self, job):
        with self.lock:
            if self.active is job:
                self.active = None

    def _current(self, job):
        return self.active is job and not job.cancelled and not job.finished and not self.closed

    def ram_results(self):
        with self.lock:
            return tuple(self.unsaved.values())

    def _publish(self, job, outcome):
        job.finished = True
        if outcome.error in ("original_write_failed", "result_write_failed"):
            self.unsaved[outcome.id] = outcome
        self.publish(outcome)

    def started_capture(self, job):
        with self.lock:
            try:
                self.store.start(job.id, "capturing")
            except StorageError:
                pass

    def finish_capture(self, job, processing, error=None):
        with self.lock:
            self._finish_status(job, processing, error)
            self.release(job)

    def run(self, job, audio, transcribe):
        with self.lock:
            if not self._current(job):
                return
            try:
                self.store.start(job.id)
            except StorageError:
                pass  # Still obtain the user's text for manual recovery.
        try:
            original = transcribe(audio)
        except Exception:
            with self.lock:
                if self._current(job):
                    job.finished = True
                    self._finish_status(job, "failed", "stt_failed")
                    raise
            return
        with self.lock:
            if not self._current(job):
                return
            if not original:
                job.finished = True
                self._finish_status(job, "silence")
                return
            smart = job.processor.enabled
            try:
                self.store.original(job.id, original, smart)
            except StorageError:
                self._finish_status(job, "skipped" if smart else "ready", "original_write_failed")
                self._publish(job, Outcome(job.id, original, "Smart skipped" if smart else "Raw", "none",
                                           delivery="not_requested", error="original_write_failed"))
                return
            if not smart:
                self._publish(job, Outcome(job.id, original, "Raw", "original"))
                return
        # STT and HTTP may return long after cancellation. Only this attempt can commit.
        try:
            candidate, label = job.processor.process(original)
        except Exception:
            candidate, label = original, "Smart fallback (backend error)"
        with self.lock:
            if not self._current(job):
                return
            if label != "Smart":
                saved_status = self._finish_status(job, "fallback", "smart_failed")
                outcome = Outcome(job.id, original, label, "original",
                                  error=None if saved_status else "status_write_failed")
            else:
                try:
                    self.store.result(job.id, candidate)
                    outcome = Outcome(job.id, candidate, label, "result")
                except StaleAttempt:
                    job.finished = True
                    self._finish_status(job, "cancelled")
                    return
                except StorageError:
                    self._finish_status(job, "ready", "result_write_failed")
                    outcome = Outcome(job.id, candidate, label, "original", error="result_write_failed")
            self._publish(job, outcome)
