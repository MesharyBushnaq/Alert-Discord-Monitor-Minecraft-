"""Discord webhook notifier: queued, batched, rate-limit aware."""

from __future__ import annotations

import asyncio
import logging

import httpx

from .metrics import Metrics
from .models import Alert

log = logging.getLogger(__name__)

_STOP = object()
MAX_EMBEDS = 10  # Discord limit per message


class DiscordNotifier:
    def __init__(
        self,
        client: httpx.AsyncClient,
        webhook_url: str,
        *,
        username: str = "Alert Monitor",
        avatar_url: str | None = None,
        critical_mention: str | None = None,
        dry_run: bool = False,
        queue_size: int = 1000,
        max_retries: int = 5,
        metrics: Metrics | None = None,
    ) -> None:
        self.client = client
        self.webhook_url = webhook_url
        self.username = username
        self.avatar_url = avatar_url
        self.critical_mention = critical_mention
        self.dry_run = dry_run
        self.max_retries = max_retries
        self.metrics = metrics or Metrics()
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=queue_size)
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._worker(), name="discord-notifier")

    def submit(self, alert: Alert) -> None:
        """Non-blocking enqueue so a slow Discord never stalls polling."""
        try:
            self._queue.put_nowait(alert)
        except asyncio.QueueFull:
            self.metrics.inc("alerts_dropped")
            log.error("notifier queue full, dropping alert: %s", alert.title)

    async def close(self, timeout: float = 20.0) -> None:
        if self._task is None:
            return
        await self._queue.put(_STOP)
        try:
            await asyncio.wait_for(self._task, timeout)
        except asyncio.TimeoutError:
            log.warning("notifier did not flush within %.0fs", timeout)
            self._task.cancel()
        self._task = None

    async def _worker(self) -> None:
        stopping = False
        while not stopping:
            first = await self._queue.get()
            if first is _STOP:
                break
            batch = [first]
            # Opportunistic batching: only group what is already waiting, so a
            # lone alert is sent immediately with zero added latency.
            while len(batch) < MAX_EMBEDS:
                try:
                    nxt = self._queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if nxt is _STOP:
                    stopping = True
                    break
                batch.append(nxt)
            try:
                await self._send(batch)
            except Exception:  # never let the worker die
                log.exception("unexpected error while sending alerts")

    def _payload(self, batch: list[Alert]) -> dict:
        payload: dict = {
            "username": self.username,
            "embeds": [a.to_embed() for a in batch],
            "allowed_mentions": {"parse": []},
        }
        if self.avatar_url:
            payload["avatar_url"] = self.avatar_url
        if self.critical_mention and any(a.severity == "critical" for a in batch):
            payload["content"] = self.critical_mention
            payload["allowed_mentions"] = {"parse": ["roles", "users"]}
        return payload

    async def _send(self, batch: list[Alert]) -> None:
        if self.dry_run:
            for a in batch:
                log.info("[dry-run] %s | %s | %s", a.severity.upper(), a.title, a.description.replace("\n", " "))
            self.metrics.inc("alerts_sent", n=len(batch))
            return

        payload = self._payload(batch)
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = await self.client.post(self.webhook_url, json=payload, timeout=10)
            except httpx.HTTPError as exc:
                wait = min(2 ** attempt, 30)
                log.warning("discord request failed (%s), retry %d/%d in %ss",
                            type(exc).__name__, attempt, self.max_retries, wait)
                await asyncio.sleep(wait)
                continue

            if resp.status_code in (200, 204):
                self.metrics.inc("alerts_sent", n=len(batch))
                self.metrics.inc("discord_requests")
                # Proactively honour the bucket instead of eating a 429 next time.
                if resp.headers.get("X-RateLimit-Remaining") == "0":
                    await asyncio.sleep(_float(resp.headers.get("X-RateLimit-Reset-After"), 0, 60))
                return
            if resp.status_code == 429:
                wait = _retry_after(resp)
                self.metrics.inc("discord_rate_limited")
                log.warning("discord rate limited, waiting %.2fs", wait)
                await asyncio.sleep(wait + 0.05)
                continue
            if resp.status_code >= 500:
                wait = min(2 ** attempt, 30)
                log.warning("discord %s, retry %d/%d in %ss", resp.status_code, attempt, self.max_retries, wait)
                await asyncio.sleep(wait)
                continue
            # other 4xx: retrying will not help (bad webhook, invalid payload)
            self.metrics.inc("discord_errors")
            log.error("discord rejected message: HTTP %s %s", resp.status_code, resp.text[:200])
            return

        self.metrics.inc("discord_errors")
        self.metrics.inc("alerts_dropped", n=len(batch))
        log.error("giving up on %d alert(s) after %d attempts", len(batch), self.max_retries)


def _float(raw: str | None, default: float, cap: float) -> float:
    try:
        return max(0.0, min(float(raw), cap))
    except (TypeError, ValueError):
        return default


def _retry_after(resp: httpx.Response) -> float:
    try:
        return _float(str(resp.json().get("retry_after")), 1.0, 60)
    except (ValueError, AttributeError):
        return _float(resp.headers.get("Retry-After"), 1.0, 60)
