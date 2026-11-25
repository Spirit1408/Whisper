"""One active request. Inference runs outside the lock that orders cancel and commit."""
from __future__ import annotations

from copy import copy
from dataclasses import dataclass, field, replace
import logging
import re
import threading
from uuid import uuid4

from history import StaleAttempt, StorageError
from postprocessor import SmartFailure

logger = logging.getLogger(__name__)


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
    repeat_artifact: str | None = None
    saved_level: str = "original"
    delivery_revoked: threading.Event = field(default_factory=threading.Event)
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
    status_saved: bool = True
    artifact: str | None = None
    keep_editor: bool = False

    @property
    def message(self):
        saved = {"none": "not saved to history", "original": "Original saved", "legacy": "legacy text saved",
                 "result": "result saved"}[self.storage]
        if self.error == "result_write_failed":
            saved += ", new result not saved"
        if not self.status_saved:
            saved += ", processing status not saved"
        delivery = {"confirmed": "Delivery confirmed by the field",
                    "paste_dispatched": "Paste dispatched. Check the field before using Copy",
                    "unknown": "Delivery unknown. Check the field before using Copy",
                    "not_requested": "Manual copy only"}.get(self.delivery, self.reason or "Artifact only")
        return f"{self.processing} · {saved}. {delivery}." + (
            " Use Copy." if self.delivery in ("skipped", "not_requested") else "")


class Coordinator:
    def __init__(self, store, publish, delivery=None, notice=None):
        self.store = store
        self.publish = publish
        self.delivery = delivery
        self.notice = notice
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

    def disable_auto(self):
        job = self.active
        if job is not None:
            job.delivery_revoked.set()

    def _publish(self, job, outcome):
        if job.repeat_artifact:
            outcome = replace(outcome, artifact=job.repeat_artifact)
        job.finished = True
        job.pending = None
        if outcome.error in ("original_write_failed", "result_write_failed"):
            self.unsaved[outcome.id] = outcome
        if not self.closed:
            self.publish(outcome)

    def _ready(self, job, outcome, manual_reason=""):
        if not job.auto:
            self._publish(job, replace(outcome, delivery="not_requested"))
            return
        if manual_reason:
            reason = manual_reason
        elif job.delivery_revoked.is_set() or outcome.error or (job.processor.enabled and outcome.storage != "result"):
            reason = "Paste skipped"
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
            if job.cancel_requested.is_set() or job.delivery_revoked.is_set():
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
                self.store.start(job.id, "capturing", auto=job.auto)
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
                self.store.start(job.id, auto=job.auto)
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
                self.store.original(job.id, original, smart, auto=job.auto,
                                    preset=getattr(job.processor, "preset", None),
                                    model=getattr(job.processor, "model", ""))
            except StaleAttempt:
                job.finished = True
                return
            except StorageError:
                saved = self._finish_status(job, "skipped" if smart else "ready", "original_write_failed")
                label = job.processor.preset.name + " skipped" if smart else "Raw"
                self._ready(job, Outcome(job.id, original, label, "none",
                                         error="original_write_failed", status_saved=saved))
                return
            if not smart:
                self._ready(job, Outcome(job.id, original, "Raw", "original"))
        if not smart:
            self._deliver(job)
            return
        self._transform(job, original)
        self._deliver(job)

    def prepare_repeat(self, job, artifact, source, revision):
        with self.lock:
            if not self._current(job) or job.auto:
                raise StaleAttempt("repeat_unavailable")
            job.saved_level = self.store.start_repeat(job.id, artifact, source, revision,
                                                     job.processor.preset, job.processor.model)
            job.repeat_artifact = artifact

    def run_repeat(self, job):
        self._transform(job, "")

    def _stale(self, job):
        job.finished = True
        self._finish_status(job, "cancelled")
        if job.repeat_artifact and self.notice and not self.closed:
            self.notice("Reprocessing stopped. The saved version changed or the entry was removed.")

    def _smart_failure(self, job, outcome, processing, code):
        try:
            self.store.finish(job.id, processing, code)
        except StaleAttempt:
            self._stale(job)
            return
        except StorageError:
            outcome = replace(outcome, status_saved=False)
        self._ready(job, outcome)

    def _transform(self, job, transcript):
        preset = job.processor.preset
        with self.lock:
            if not self._current(job) or job.cancel_requested.is_set():
                return
            try:
                original = self.store.smart_source(job.id)
            except StaleAttempt:
                self._stale(job)
                return
            except StorageError:
                outcome = Outcome(job.id, transcript, f"{preset.name} skipped (source read failed)",
                                  job.saved_level, error="original_read_failed", keep_editor=bool(job.repeat_artifact))
                self._smart_failure(job, outcome, "skipped", outcome.error)
                return
        # Neither the SQLite transaction nor the coordinator lock spans inference.
        failure = None
        try:
            candidate = job.processor.process(original) if job.processor.enabled else original
        except SmartFailure as exc:
            failure = exc
        except Exception:
            failure = SmartFailure("smart_failed")
        with self.lock:
            if not self._current(job) or job.cancel_requested.is_set():
                return
            if failure is not None:
                logger.warning("smart_fallback code=%s", failure.code)
                label = "Smart fallback" if preset.correction else f"{preset.name} failed"
                status = "fallback" if preset.correction else "failed"
                outcome = Outcome(job.id, original, f"{label} ({failure})", job.saved_level,
                                  keep_editor=bool(job.repeat_artifact))
                self._smart_failure(job, outcome, status, failure.code)
                return
            try:
                self.store.result(job.id, candidate)
            except StaleAttempt:
                self._stale(job)
            except StorageError:
                outcome = Outcome(job.id, candidate, preset.name, job.saved_level, error="result_write_failed")
                self._smart_failure(job, outcome, "ready", outcome.error)
            else:
                reason = ""
                if job.auto and not preset.correction and preset.id != "raw":
                    source_words = re.findall(r"\w+", original.casefold())
                    if source_words == re.findall(r"\w+", candidate.casefold()):
                        reason = "Paste skipped (output repeats source wording)"
                self._ready(job, Outcome(job.id, candidate, preset.name, "result"), reason)
