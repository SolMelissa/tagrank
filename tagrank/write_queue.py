"""Background queue for Hydrus write-back calls that don't need to finish before the next
comparison pair can be shown - the like/dislike rating and MMR/confidence writes in
rating.py/pool.py are one-way persistence side effects nothing else in the app reads back
synchronously, so blocking a request on them (up to 6 sequential Hydrus HTTP calls per judged
pair) only delays the next pair for no correctness benefit. Enqueueing them here instead lets
Undertow's /tagrank/compare/result route return the next pair immediately while these drain in
the background, one at a time, in submission order.
"""

from __future__ import annotations

import logging
import queue
import threading
from typing import Callable

logger = logging.getLogger(__name__)

_queue: "queue.Queue[Callable[[], None]]" = queue.Queue()
_worker: threading.Thread | None = None
_lock = threading.Lock()


def _run_worker() -> None:
    while True:
        job = _queue.get()
        try:
            job()
        except Exception:
            logger.exception("Background Hydrus write failed")
        finally:
            _queue.task_done()


def _ensure_worker() -> None:
    global _worker
    with _lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_run_worker, name="tagrank-hydrus-writer", daemon=True)
            _worker.start()


def enqueue(job: Callable[[], None]) -> None:
    """Fire-and-forget: runs `job` on a single background worker thread (FIFO, one job at a
    time - so writes for the same file/session still land in submission order even though
    they no longer block the caller)."""
    _ensure_worker()
    _queue.put(job)


def wait_idle() -> None:
    """Block until every currently-queued job has run. For tests/clean shutdown - never call
    this from a request handler, or it defeats the entire point of the queue."""
    _queue.join()
