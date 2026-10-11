"""Per-source polling loop with backoff, conditional requests and rule evaluation."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any

import httpx

from .config import SourceConfig
from .metrics import Metrics
from .models import Alert
from .notifier import DiscordNotifier
from .rules import build_alert, evaluate, extract, item_ids, to_float
from .state import StateStore

log = logging.getLogger(__name__)

NOT_MODIFIED = object()


class FetchError(Exception):
    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class SourceRunner:
    def __init__(self, source: SourceConfig, client: httpx.AsyncClient, state: StateStore,
                 notifier: DiscordNotifier, metrics: Metrics) -> None:
        self.source = source
        self.client = client
        self.state = state
        self.notifier = notifier
        self.metrics = metrics
        self.consecutive_failures = 0
        self._down_notified = False
        self._etag: str | None = None
        self._last_modified: str | None = None
        self._latency_ema: float | None = None
        self.metrics.set(source.name, "up", 1)

    # ------------------------------------------------------------------ fetch
    async def _fetch(self) -> Any:
        s = self.source
        headers = dict(s.headers)
        if self._etag:
            headers["If-None-Match"] = self._etag
        if self._last_modified:
            headers["If-Modified-Since"] = self._last_modified
        try:
            resp = await self.client.request(s.method, s.url, headers=headers,
                                             params=s.params or None, timeout=s.timeout)
        except httpx.HTTPError as exc:
            raise FetchError(f"{type(exc).__name__}: {exc}") from exc

        if resp.status_code == 304:
            return NOT_MODIFIED
        if resp.status_code >= 400:
            retry_after = None
            if resp.status_code == 429:
                try:
                    retry_after = float(resp.headers.get("Retry-After", ""))
                except ValueError:
                    retry_after = None
            raise FetchError(f"HTTP {resp.status_code}", retry_after)
        try:
            data = resp.json()
        except ValueError:
            raise FetchError("response is not valid JSON") from None
        self._etag = resp.headers.get("ETag")
        self._last_modified = resp.headers.get("Last-Modified")
        return data

    # ------------------------------------------------------------------ rules
    def _process_rule(self, rule, data: Any) -> None:
        s = self.source
        skey = f"{s.name}:{rule.name}"
        value = extract(data, rule.path)
        previous: float | None = None

        if rule.type == "change":
            raw = self.state.get_value(skey + ":prev")
            previous = float(raw) if raw is not None else None
            self.state.set_value(skey + ":prev", repr(to_float(value)))

        seen: set[str] = set()
        if rule.type == "new_item":
            seen = self.state.seen_ids(skey)
        matches = evaluate(rule, value, previous, seen)

        if rule.type == "new_item":
            seeded = self.state.get_value(skey + ":seeded") is not None
            self.state.add_seen(skey, item_ids(rule, value))
            if not seeded:
                self.state.set_value(skey + ":seeded", "1")
                if not rule.notify_existing:
                    matches = []
            matches = list(reversed(matches))  # oldest first when feeds are newest-first

        if rule.type == "threshold":
            fired = bool(matches)
            was_active = self.state.get_value(skey + ":active") == "1"
            self.state.set_value(skey + ":active", "1" if fired else "0")
            if fired and was_active and rule.edge_trigger:
                matches = []
            elif not fired and was_active and rule.notify_resolved:
                self.notifier.submit(Alert(
                    source=s.name, title=f"Resolved: {rule.name}", severity="resolved",
                    description=f"`{rule.path}` is back within limits (now {to_float(value):g})."))
                self.metrics.inc("alerts_queued", s.name)

        cooldown = 0.0 if rule.type == "new_item" else (
            rule.cooldown if rule.cooldown is not None else s.cooldown)
        for m in matches:
            if cooldown and not self.state.cooldown_ok(f"{skey}:{m.suffix}", cooldown):
                self.metrics.inc("alerts_suppressed", s.name)
                continue
            self.notifier.submit(build_alert(s.name, rule, m, previous))
            self.metrics.inc("alerts_queued", s.name)

    # ------------------------------------------------------------------ cycle
    async def poll_once(self) -> None:
        t0 = time.perf_counter()
        data = await self._fetch()
        latency = (time.perf_counter() - t0) * 1000
        self._latency_ema = latency if self._latency_ema is None else 0.8 * self._latency_ema + 0.2 * latency
        self.metrics.set(self.source.name, "latency_ms", round(self._latency_ema, 1))
        if data is NOT_MODIFIED:
            self.metrics.inc("not_modified", self.source.name)
            return
        for rule in self.source.rules:
            try:
                self._process_rule(rule, data)
            except (LookupError, ValueError, TypeError) as exc:
                self.metrics.inc("rule_errors", self.source.name)
                log.warning("[%s] rule '%s' skipped: %s", self.source.name, rule.name, exc)

    def _on_success(self) -> None:
        self.metrics.inc("polls", self.source.name)
        self.metrics.set(self.source.name, "up", 1)
        self.metrics.set(self.source.name, "last_success_ts", time.time())
        if self._down_notified:
            self.notifier.submit(Alert(
                source=self.source.name, title=f"Source recovered: {self.source.name}", severity="resolved",
                description=f"`{self.source.url}` is responding again after "
                            f"{self.consecutive_failures} failed attempt(s)."))
            self._down_notified = False
        if self.consecutive_failures:
            log.info("[%s] recovered after %d failure(s)", self.source.name, self.consecutive_failures)
        self.consecutive_failures = 0

    def _on_failure(self, exc: Exception) -> None:
        s = self.source
        self.consecutive_failures += 1
        self.metrics.inc("polls", s.name)
        self.metrics.inc("failures", s.name)
        log.warning("[%s] poll failed (%d in a row): %s", s.name, self.consecutive_failures, exc)
        if self.consecutive_failures >= s.failure_threshold and not self._down_notified:
            self._down_notified = True
            self.metrics.set(s.name, "up", 0)
            self.notifier.submit(Alert(
                source=s.name, title=f"Source down: {s.name}", severity="critical",
                description=f"`{s.url}` failed {self.consecutive_failures} times in a row.\nLast error: {exc}"))

    async def cycle(self) -> float:
        """Run one poll and return the delay (seconds) before the next one."""
        s = self.source
        retry_after = None
        try:
            await self.poll_once()
            self._on_success()
        except FetchError as exc:
            retry_after = exc.retry_after
            self._on_failure(exc)
        except Exception as exc:  # defensive: a bug must not kill the loop
            log.exception("[%s] unexpected error", s.name)
            self._on_failure(exc)

        if self.consecutive_failures == 0:
            delay = s.interval
        else:
            delay = min(s.interval * 2 ** min(self.consecutive_failures, 8), s.max_backoff)
            if retry_after:
                delay = max(delay, min(retry_after, s.max_backoff))
        return delay * random.uniform(0.9, 1.1)

    async def run(self, stop: asyncio.Event) -> None:
        # Stagger start-up so sources do not all hit the network in the same instant.
        await _sleep(stop, random.uniform(0, min(self.source.interval, 2.0)))
        while not stop.is_set():
            started = time.monotonic()
            delay = await self.cycle()
            # Subtract the time the poll itself took to keep a steady cadence.
            await _sleep(stop, max(0.0, delay - (time.monotonic() - started)))


async def _sleep(stop: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass
