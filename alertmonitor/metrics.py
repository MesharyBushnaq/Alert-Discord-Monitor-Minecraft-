"""In-process metrics plus a tiny HTTP server for /health and /metrics."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import defaultdict

log = logging.getLogger(__name__)


class Metrics:
    def __init__(self) -> None:
        self.started = time.time()
        self.counters: dict[str, float] = defaultdict(float)
        self.sources: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))

    def inc(self, name: str, source: str | None = None, n: float = 1) -> None:
        if source is None:
            self.counters[name] += n
        else:
            self.sources[source][name] += n

    def set(self, source: str, name: str, value: float) -> None:
        self.sources[source][name] = value

    def healthy(self) -> bool:
        return all(v.get("up", 1) for v in self.sources.values())

    def snapshot(self) -> dict:
        return {
            "status": "ok" if self.healthy() else "degraded",
            "uptime_seconds": round(time.time() - self.started, 1),
            "counters": dict(self.counters),
            "sources": {k: dict(v) for k, v in self.sources.items()},
        }

    def prometheus(self) -> str:
        lines = [f"alertmonitor_uptime_seconds {time.time() - self.started:.0f}"]
        for name, val in sorted(self.counters.items()):
            lines.append(f"alertmonitor_{name}_total {val:g}")
        for src, vals in sorted(self.sources.items()):
            for name, val in sorted(vals.items()):
                lines.append(f'alertmonitor_source_{name}{{source="{src}"}} {val:g}')
        return "\n".join(lines) + "\n"


async def start_health_server(metrics: Metrics, host: str, port: int) -> asyncio.AbstractServer:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await asyncio.wait_for(reader.readline(), 5)
            parts = line.decode("latin-1").split()
            path = parts[1].split("?")[0] if len(parts) > 1 else "/"
            if path == "/metrics":
                status, ctype, body = "200 OK", "text/plain; version=0.0.4", metrics.prometheus()
            elif path in ("/health", "/"):
                ok = metrics.healthy()
                status = "200 OK" if ok else "503 Service Unavailable"
                ctype, body = "application/json", json.dumps(metrics.snapshot(), indent=2)
            else:
                status, ctype, body = "404 Not Found", "text/plain", "not found\n"
            data = body.encode()
            writer.write(
                f"HTTP/1.1 {status}\r\nContent-Type: {ctype}\r\nContent-Length: {len(data)}\r\n"
                f"Connection: close\r\n\r\n".encode() + data)
            await writer.drain()
        except (asyncio.TimeoutError, ConnectionError):
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(handle, host, port)
    log.info("health server listening on %s:%s (/health, /metrics)", host, port)
    return server
