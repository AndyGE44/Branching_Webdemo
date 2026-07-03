"""Idle auto-reset for the long-lived public demo.

A permanently-running demo accumulates whatever the last visitor left behind —
items in the cart, extra snapshots, a restored state. This module watches for
activity and, once the workspace has been *idle* for a configurable window
(default 10 minutes) AND has diverged from its freshly-built clean state, runs
the same Reset the UI button does: tear the branch down and rebuild a clean
shop. A workspace that is already at the original clean state is left alone, so
an untouched demo is never needlessly rebuilt.

"Movement" is any real request: browsing the shop (proxied through /runtime/*),
cart writes, snapshot/restore, or the control API. The liveness probe
(/healthz) is deliberately ignored so an uptime monitor can not keep the demo
forever "busy" and defeat the reset.

Configuration (environment):

- ``DEMO_IDLE_RESET_MINUTES``  idle window before auto-reset (default 10; <=0 disables)
- ``DEMO_IDLE_CHECK_SECONDS``  how often the monitor checks (default 30)
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import TYPE_CHECKING

from agent_safe_demo.control_plane.statefork import BranchError

if TYPE_CHECKING:
    from agent_safe_demo.control_plane.workspace import Workspace

log = logging.getLogger("agent_safe_demo.control_plane.idle")

# Paths that are NOT user "movement": the unauthenticated liveness probe a
# supervisor / uptime check hits. Counting it would keep the demo permanently
# "busy" and defeat the idle reset. Everything else is movement.
IGNORED_ACTIVITY_PATHS = frozenset({"/healthz"})


class ActivityTracker:
    """The demo's movement + cleanliness signal.

    ``record_request`` stamps the last-activity clock on every real request.
    ``mark_dirty`` / ``mark_clean`` track whether the runtime has diverged from
    the freshly-built shop, so the monitor can skip an already-clean workspace.
    Everything is touched only from the event-loop thread (HTTP handlers and the
    monitor coroutine), so plain attributes are safe.
    """

    def __init__(self) -> None:
        self._last_activity = time.monotonic()
        self._dirty = False

    def record_request(self, path: str) -> None:
        """Register a request as movement, unless it is an ignored path."""
        if path in IGNORED_ACTIVITY_PATHS:
            return
        self._last_activity = time.monotonic()

    def mark_dirty(self) -> None:
        """A state-changing action ran (cart write, snapshot, restore)."""
        self._dirty = True
        self._last_activity = time.monotonic()

    def mark_clean(self) -> None:
        """The workspace is back at the original clean state (fresh build / reset)."""
        self._dirty = False
        self._last_activity = time.monotonic()

    @property
    def dirty(self) -> bool:
        return self._dirty

    def idle_seconds(self) -> float:
        return time.monotonic() - self._last_activity


def should_reset(tracker: ActivityTracker, idle_window: float) -> bool:
    """Reset only a *dirty* workspace that has been idle past the window; an
    already-clean (original-state) workspace is never reset."""
    return tracker.dirty and tracker.idle_seconds() >= idle_window


def _reset_to_clean(workspace: "Workspace") -> None:
    """Tear the workspace down and rebuild a warm, clean shop. Runs on a worker
    thread because StateFork reset/build are blocking (buildah/CRIU)."""
    workspace.reset()
    try:
        # Rebuild eagerly so the next visitor lands on a ready shop instead of a
        # cold start. Best-effort: if the rebuild hiccups the mutation is still
        # gone, and the next /api/workspace call rebuilds it.
        workspace.ensure()
    except BranchError:
        log.warning("post-reset rebuild failed; next visit will rebuild", exc_info=True)


def _ensure_operational_logging() -> None:
    """Make the auto-reset log lines visible in the control-plane log.

    uvicorn configures its own loggers but not the root logger, so our INFO
    lines would otherwise be swallowed. Attach one handler to the package
    logger (idempotent — guarded so repeated startups do not stack handlers)."""
    logger = logging.getLogger("agent_safe_demo")
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)


async def run_idle_reset_monitor(workspace: "Workspace", tracker: ActivityTracker) -> None:
    """Background loop: auto-reset the workspace after an idle window. Started
    from the app lifespan; cancelled on shutdown."""
    _ensure_operational_logging()
    idle_minutes = float(os.getenv("DEMO_IDLE_RESET_MINUTES", "10"))
    if idle_minutes <= 0:
        log.info("idle auto-reset disabled (DEMO_IDLE_RESET_MINUTES=%s)", idle_minutes)
        return
    idle_window = idle_minutes * 60.0
    check_seconds = max(1.0, float(os.getenv("DEMO_IDLE_CHECK_SECONDS", "30")))
    log.info(
        "idle auto-reset armed: reset a dirty workspace after %.0f min idle "
        "(checking every %.0fs)",
        idle_minutes,
        check_seconds,
    )
    while True:
        await asyncio.sleep(check_seconds)
        try:
            if not should_reset(tracker, idle_window):
                continue
            log.info(
                "no movement for %.0fs and the shop is dirty — auto-resetting to a clean state",
                tracker.idle_seconds(),
            )
            await asyncio.to_thread(_reset_to_clean, workspace)
            tracker.mark_clean()
            log.info("idle auto-reset done — clean shop rebuilt")
        except asyncio.CancelledError:
            raise
        except Exception:  # a monitor that dies would silently stop cleaning up
            log.exception("idle auto-reset cycle failed; retrying next tick")
