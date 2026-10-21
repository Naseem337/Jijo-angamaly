"""Background jobs inside the web process (Railway: one always-on container, no separate cron).

Every `ALERTS_EVERY_MINUTES` (0 = off, the local default) each client gets:
  - an alert run (it enforces its own send window, daily caps and dedupe, so running it
    often only makes alerts faster, never more frequent per customer);
  - the 24 h auto-resume sweep for paused chats.
Runs in a daemon thread; a failing job is logged and retried on the next tick.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from app.runtime import Runtime

log = logging.getLogger(__name__)


def run_jobs_once(runtime: Runtime) -> None:
    for client_id, client in runtime.clients.items():
        try:
            report = client.alerts.run()
            if report.aborted or report.sent or report.failed:
                log.info("Alerts for %s: sent %d, failed %d%s", client_id, report.sent, report.failed,
                         f", ABORTED: {report.aborted}" if report.aborted else "")
        except Exception:
            log.exception("Alert run for %s crashed", client_id)
        try:
            resumed = client.handoff.auto_resume_all()
            if resumed:
                log.info("Auto-resumed %d paused chat(s) for %s", resumed, client_id)
        except Exception:
            log.exception("Auto-resume sweep for %s crashed", client_id)


def start_background_jobs(runtime: Runtime, every_minutes: int) -> Callable[[], None]:
    """Start the loop; returns a function that stops it."""
    stop = threading.Event()

    def loop() -> None:
        if stop.wait(60):  # first run a minute after start, once the server is up
            return
        while True:
            run_jobs_once(runtime)
            if stop.wait(every_minutes * 60):
                return

    if every_minutes > 0:
        threading.Thread(target=loop, name="background-jobs", daemon=True).start()
        log.info("Background jobs every %d min (alerts, auto-resume)", every_minutes)
    return stop.set
