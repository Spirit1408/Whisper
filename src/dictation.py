"""One active request. Inference runs outside the lock that orders cancel and commit."""
from __future__ import annotations

from copy import copy
from dataclasses import dataclass, field, replace
import threading
from uuid import uuid4

from history import StaleAttempt, StorageError


@dataclass
class Request:
    processor: object
    id: str = field(default_factory=lambda: uuid4().hex)
    cancelled: bool = False
    finished: bool = False
    auto: bool = False
    target: object | None = None
    pending: Outcome | None = None
    dispatch_started: bool = False
    cancel_requested: threading.Event = field(default_factory=threading.Event)


@dataclass(frozen=True)
class Outcome:
    id: str
    text: str
    processing: str
    storage: str
    delivery: str = "skipped"
    error: str | None = None
    reason: str = ""

    @property
    def message(self):
        saved = {"none": "not saved to history", "original": "Original saved",
                 "result": "Original and Smart result saved"}[self.storage]
        if self.error == "result_write_failed":
            saved += ", Smart result not saved"
        if self.error == "status_write_failed":
            saved += ", processing status not saved"
        delivery = {"confirmed": "Delivery confirmed by the field",
                    "paste_dispatched": "Paste dispatched. Check the field before using Copy",
                    "unknown": "Delivery unknown. Check the field before using Copy",
                    "not_requested": "Manual copy only"}.get(self.delivery, self.reason or "Artifact only")
        return f"{self.processing} · {saved}. {delivery}." + (
            " Use Copy." if self.delivery in ("skipped", "not_requested") else "")


class Coordinator:
    def __init__(self, store, publish, delivery=None):
        self.store = store
        self.publish = publish
        self.delivery = delivery
        self.lock = threading.RLock()
        self.active = None
        self.closed = False
        self.unsaved = {}

    def admit(self, processor, auto=False):
        with self.lock:
            if self.closed or self.active is not None:
                return None
            self.active = Request(copy(processor), auto=auto)
            if auto and self.delivery is not None:
                try:
                    self.active.target = self.delivery.capture()
                except (OSError, RuntimeError, ValueError):
                    pass
            return self.active

    def _finish_status(self, job, processing, error=None):
        try:
            self.store.finish(job.id, processing, error)
        except StorageError:
            return False
        return True

    def cancel(self, closing=False):
        # An intent commit may be waiting on SQLite with the coordinator lock
        # held. Record the request before waiting for that commit.
        requested = self.active
        if requested is not None:
            requested.cancel_requested.set()
        with self.lock:
            self.closed |= closing
            job = self.active
            if job is not requested and not closing:
                return False
            if job is None or job.cancelled or job.finished or job.dispatch_started:
                return False
            job.cancelled = True
            if job.pending is not None:
                self._publish(job, replace(job.pending, reason="Paste cancelled"))
            else:
                self._finish_status(job, "cancelled")
            return True

    def release(self, job):
        with self.lock:
            if self.active is job:
                self.active = None
                if job.target is not None:
                    try:
                        job.target.close()
                    except OSError:
                        pass
                    job.target = None

    def _current(self, job):
        return self.active is job and not job.cancelled and not job.finished and not self.closed

    def ram_results(self):
        with self.lock:
            return tuple(self.unsaved.values())

    def _publish(self, job, outcome):
        job.finished = True
        job.pending = None
        if outcome.error in ("original_write_failed", "result_write_failed"):
            self.unsaved[outcome.id] = outcome
        if not self.closed:
            self.publish(outcome)

    def _ready(self, job, outcome):
        if not job.auto:
            reason = "Artifact only"
        elif outcome.error or outcome.processing not in ("Raw", "Smart"):
            reason = "Paste skipped (Smart fallback or save error)"
        elif job.target is None:
            reason = "Paste skipped (document field could not be verified)"
        else:
            job.pending = outcome
            return
        self._publish(job, replace(outcome, reason=reason))

    def _deliver(self, job):
        # Verification may block. Cancel can still claim the saved result.
        with self.lock:
            if not self._current(job) or job.pending is None:
                return
        try:
            valid = job.target.verify()
        except (OSError, RuntimeError, ValueError):
            valid = False
        with self.lock:
            if not self._current(job):
                return
            outcome = job.pending
            if not valid:
                self._publish(job, replace(outcome, reason="Paste skipped (recipient changed)"))
                return
            try:
                self.store.begin_delivery(job.id, outcome.storage)
            except StorageError:
                self._publish(job, replace(outcome, reason="Paste skipped (delivery unavailable)"))
                return
            if job.cancel_requested.is_set():
                try:
                    self.store.end_delivery(job.id, "skipped")
                except StorageError:
                    pass  # Startup conservatively recovers the durable intent as unknown.
                self._publish(job, replace(outcome, reason="Paste cancelled"))
                return
            job.dispatch_started = True
        try:
            status = job.target.deliver(outcome.text)
        except Exception:
            status = "unknown"
        if status not in ("skipped", "confirmed", "paste_dispatched"):
            status = "unknown"
        with self.lock:
            try:
                self.store.end_delivery(job.id, status)
            except StorageError:
                status = "unknown"
            self._publish(job, replace(outcome, delivery=status, reason="Paste skipped (final check failed)"))

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
                self._ready(job, Outcome(job.id, original, "Raw", "original"))
        if not smart:
            self._deliver(job)
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
            self._ready(job, outcome)
        self._deliver(job)
