"""WS7c — runtime refresh coordinator (ADR-0014).

Drives manifest refresh from three triggers, all funneling into one
``SemanticLayerManager.refresh()`` codepath:

  * **poll** — an asyncio task checks the store every N seconds;
  * **SIGHUP** — an ops signal forces an immediate refresh;
  * **admin endpoint** — ``POST /admin/reload`` (served on the admin port,
    never 5432), token-gated, forces an immediate refresh.

Refresh construction is CPU-bound + globally-stateful (adapter bootstrap), so
it runs in a thread executor and is serialized inside the manager.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from typing import Awaitable, Callable

from engine.live_layer import SemanticLayerManager

logger = logging.getLogger("flowproxy.refresh")


class RefreshCoordinator:
    """Poll loop + signal handler for manifest hot-swap."""

    def __init__(
        self,
        manager: SemanticLayerManager,
        *,
        poll_interval: float = 300.0,
    ) -> None:
        self._manager = manager
        self._poll_interval = poll_interval
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    async def _refresh_in_executor(self, *, force: bool) -> bool:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: self._manager.refresh(force=force))

    async def trigger(self, *, force: bool = True) -> bool:
        """Force an immediate refresh (admin endpoint / SIGHUP)."""
        logger.info("refresh triggered (force=%s)", force)
        return await self._refresh_in_executor(force=force)

    async def _poll_loop(self) -> None:
        if self._poll_interval <= 0:
            logger.info("manifest polling disabled (interval<=0); refresh via signal/admin only")
            return
        logger.info("manifest poll loop started (interval=%ss)", self._poll_interval)
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._poll_interval)
            except asyncio.TimeoutError:
                pass  # interval elapsed → poll
            if self._stop.is_set():
                break
            try:
                await self._refresh_in_executor(force=False)
            except Exception:
                logger.exception("poll refresh failed; will retry next interval")

    def install_signal_handler(self) -> None:
        """SIGHUP → immediate refresh (best-effort; not available on all platforms)."""
        loop = asyncio.get_running_loop()
        try:
            loop.add_signal_handler(
                signal.SIGHUP,
                lambda: asyncio.create_task(self.trigger(force=True)),
            )
            logger.info("SIGHUP refresh handler installed")
        except (NotImplementedError, AttributeError):
            logger.info("SIGHUP not available on this platform; skipping")

    def start(self) -> None:
        self._task = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            await self._task


def make_admin_reload_handler(
    coordinator: RefreshCoordinator, admin_token: str | None
) -> Callable[[str | None], Awaitable[dict]]:
    """Build the ``POST /admin/reload`` handler (token-gated).

    Returned as a plain coroutine so it can be mounted on the MCP/admin HTTP
    app without coupling this module to a specific web framework.
    """

    async def handler(provided_token: str | None) -> dict:
        if admin_token and provided_token != admin_token:
            return {"ok": False, "error": "unauthorized"}
        swapped = await coordinator.trigger(force=True)
        return {
            "ok": True,
            "swapped": swapped,
            "version": coordinator._manager.current.version,
        }

    return handler
