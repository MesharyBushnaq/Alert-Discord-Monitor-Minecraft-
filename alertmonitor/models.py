"""Shared data models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

SEVERITY_COLORS = {
    "info": 0x3498DB,
    "warning": 0xF1C40F,
    "critical": 0xE74C3C,
    "resolved": 0x2ECC71,
}
SEVERITY_ICONS = {"info": "ℹ️", "warning": "⚠️", "critical": "🚨", "resolved": "✅"}


def clip(text: Any, limit: int) -> str:
    text = str(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


@dataclass
class Alert:
    source: str
    title: str
    description: str
    severity: str = "warning"
    url: str | None = None
    fields: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_embed(self) -> dict[str, Any]:
        icon = SEVERITY_ICONS.get(self.severity, "")
        embed: dict[str, Any] = {
            "title": clip(f"{icon} {self.title}".strip(), 256),
            "description": clip(self.description, 4096),
            "color": SEVERITY_COLORS.get(self.severity, SEVERITY_COLORS["info"]),
            "timestamp": self.timestamp.isoformat(),
            "footer": {"text": clip(f"{self.source} • {self.severity.upper()}", 2048)},
        }
        if self.url:
            embed["url"] = self.url
        if self.fields:
            embed["fields"] = [
                {"name": clip(k, 256), "value": clip(v, 1024) or "-", "inline": True}
                for k, v in list(self.fields.items())[:25]
            ]
        return embed
