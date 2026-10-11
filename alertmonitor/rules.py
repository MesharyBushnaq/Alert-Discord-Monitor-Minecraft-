"""Rule evaluation: turn API payloads into alert matches."""

from __future__ import annotations

import hashlib
import json
import operator
from dataclasses import dataclass, field
from typing import Any

from .config import RuleConfig
from .models import Alert

OPS = {
    ">": operator.gt,
    ">=": operator.ge,
    "<": operator.lt,
    "<=": operator.le,
    "==": operator.eq,
    "!=": operator.ne,
}


@dataclass
class Match:
    detail: str
    suffix: str = ""  # distinguishes cooldown keys (e.g. item id, content hash)
    title: str | None = None
    url: str | None = None
    value: Any = None
    fields: dict[str, Any] = field(default_factory=dict)


def extract(data: Any, path: str) -> Any:
    """Resolve a dotted path such as ``items.0.price``. Raises LookupError."""
    if path in ("", "."):
        return data
    cur = data
    for part in path.split("."):
        try:
            if isinstance(cur, list):
                cur = cur[int(part)]
            elif isinstance(cur, dict):
                cur = cur[part]
            else:
                raise KeyError(part)
        except (KeyError, IndexError, ValueError):
            raise LookupError(f"path '{path}' not found (stopped at '{part}')") from None
    return cur


def to_float(value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError("boolean is not numeric")
    return float(value)


def _short_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:10]


def item_ids(rule: RuleConfig, value: Any) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"path '{rule.path}' is not a list")
    return [str(extract(item, rule.id_path)) for item in value]


def evaluate(rule: RuleConfig, value: Any, previous: float | None, seen: set[str]) -> list[Match]:
    if rule.type == "threshold":
        num = to_float(value)
        if OPS[rule.operator](num, rule.value):
            return [Match(f"`{rule.path}` is **{num:g}** ({rule.operator} {rule.value:g})", value=num,
                          fields={"Value": f"{num:g}", "Threshold": f"{rule.operator} {rule.value:g}"})]
        return []

    if rule.type == "change":
        num = to_float(value)
        if previous is None:
            return []
        if previous == 0:
            pct = 0.0 if num == 0 else float("inf") * (1 if num > 0 else -1)
        else:
            pct = (num - previous) / abs(previous) * 100
        wanted = (
            (rule.direction == "up" and pct > 0)
            or (rule.direction == "down" and pct < 0)
            or rule.direction == "any"
        )
        if wanted and abs(pct) >= rule.value:
            return [Match(f"`{rule.path}` moved **{pct:+.2f}%** ({previous:g} → {num:g})",
                          suffix=f"{num:g}", value=num,
                          fields={"Previous": f"{previous:g}", "Current": f"{num:g}", "Change": f"{pct:+.2f}%"})]
        return []

    if rule.type == "contains":
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        low = text.lower()
        hits = [n for n in rule.value if n.lower() in low]
        if not hits:
            return []
        pos = low.find(hits[0].lower())
        excerpt = text[max(0, pos - 80): pos + len(hits[0]) + 120].strip()
        return [Match(f"Matched {', '.join(f'`{h}`' for h in hits)}\n> {excerpt}",
                      suffix=_short_hash(text), value=text,
                      fields={"Keywords": ", ".join(hits)})]

    if rule.type == "new_item":
        if not isinstance(value, list):
            raise ValueError(f"path '{rule.path}' is not a list")
        out = []
        for item in value:
            item_id = str(extract(item, rule.id_path))
            if item_id in seen:
                continue
            title = _opt(item, rule.title_path)
            url = _opt(item, rule.url_path)
            out.append(Match(detail=title or f"New item `{item_id}`", suffix=item_id, title=title,
                             url=url, value=item_id, fields={"ID": item_id}))
        return out

    raise ValueError(f"unknown rule type {rule.type}")


def _opt(item: Any, path: str | None) -> str | None:
    if not path:
        return None
    try:
        v = extract(item, path)
    except LookupError:
        return None
    return None if v is None else str(v)


class _Safe(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def build_alert(source: str, rule: RuleConfig, match: Match, previous: float | None = None) -> Alert:
    description = match.detail
    if rule.message:
        description = rule.message.format_map(_Safe(
            value=match.value, previous=previous, source=source, rule=rule.name, detail=match.detail))
    title = rule.name if rule.type != "new_item" or not match.title else f"{rule.name}: {match.title}"
    return Alert(source=source, title=title, description=description, severity=rule.severity,
                 url=match.url, fields=match.fields)
