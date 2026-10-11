"""Configuration loading and validation (YAML + ${ENV} expansion)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml

RULE_TYPES = {"threshold", "change", "contains", "new_item"}
OPERATORS = {">", ">=", "<", "<=", "==", "!="}
SEVERITIES = {"info", "warning", "critical"}

_ENV = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


class ConfigError(ValueError):
    """Raised when the configuration file is invalid."""


def _expand(obj: Any) -> Any:
    if isinstance(obj, str):
        return _ENV.sub(lambda m: os.environ.get(m.group(1)) or m.group(2) or "", obj)
    if isinstance(obj, list):
        return [_expand(i) for i in obj]
    if isinstance(obj, dict):
        return {k: _expand(v) for k, v in obj.items()}
    return obj


@dataclass
class RuleConfig:
    name: str
    path: str
    type: str
    operator: str = ">"
    value: Any = None
    direction: str = "any"  # change rules: up | down | any
    severity: str = "warning"
    cooldown: float | None = None
    message: str | None = None
    # threshold
    edge_trigger: bool = True
    notify_resolved: bool = False
    # new_item
    id_path: str = "id"
    title_path: str | None = None
    url_path: str | None = None
    notify_existing: bool = False


@dataclass
class SourceConfig:
    name: str
    url: str
    rules: list[RuleConfig]
    method: str = "GET"
    interval: float = 30.0
    timeout: float = 10.0
    headers: dict[str, str] = field(default_factory=dict)
    params: dict[str, Any] = field(default_factory=dict)
    cooldown: float = 300.0
    failure_threshold: int = 3
    max_backoff: float = 300.0
    enabled: bool = True


@dataclass
class DiscordConfig:
    webhook_url: str = ""
    username: str = "Alert Monitor"
    avatar_url: str | None = None
    critical_mention: str | None = None


@dataclass
class AppConfig:
    discord: DiscordConfig
    sources: list[SourceConfig]
    state_path: str = "data/state.db"
    health_port: int = 0
    health_host: str = "0.0.0.0"
    log_level: str = "INFO"


def _make(cls: type, raw: dict[str, Any], ctx: str):
    if not isinstance(raw, dict):
        raise ConfigError(f"{ctx}: expected a mapping")
    allowed = {f.name for f in fields(cls)}
    unknown = set(raw) - allowed
    if unknown:
        raise ConfigError(f"{ctx}: unknown key(s): {', '.join(sorted(unknown))}")
    try:
        return cls(**raw)
    except TypeError as exc:
        raise ConfigError(f"{ctx}: {exc}") from exc


def _validate_rule(rule: RuleConfig, ctx: str) -> None:
    if rule.type not in RULE_TYPES:
        raise ConfigError(f"{ctx}: type must be one of {sorted(RULE_TYPES)}")
    if rule.severity not in SEVERITIES:
        raise ConfigError(f"{ctx}: severity must be one of {sorted(SEVERITIES)}")
    if rule.type == "threshold":
        if rule.operator not in OPERATORS:
            raise ConfigError(f"{ctx}: operator must be one of {sorted(OPERATORS)}")
        try:
            rule.value = float(rule.value)
        except (TypeError, ValueError):
            raise ConfigError(f"{ctx}: threshold 'value' must be a number") from None
    elif rule.type == "change":
        if rule.direction not in {"up", "down", "any"}:
            raise ConfigError(f"{ctx}: direction must be up, down or any")
        try:
            rule.value = float(rule.value)
        except (TypeError, ValueError):
            raise ConfigError(f"{ctx}: change 'value' must be a percentage number") from None
    elif rule.type == "contains":
        if isinstance(rule.value, str):
            rule.value = [rule.value]
        if not rule.value or not all(isinstance(v, str) and v for v in rule.value):
            raise ConfigError(f"{ctx}: contains 'value' must be a string or list of strings")


def _build_source(raw: dict[str, Any], defaults: dict[str, Any], idx: int) -> SourceConfig:
    ctx = f"sources[{idx}]"
    if not isinstance(raw, dict):
        raise ConfigError(f"{ctx}: expected a mapping")
    raw = {**defaults, **raw}
    rules_raw = raw.pop("rules", None)
    if not raw.get("name") or not raw.get("url"):
        raise ConfigError(f"{ctx}: 'name' and 'url' are required")
    ctx = f"source '{raw['name']}'"
    if not rules_raw:
        raise ConfigError(f"{ctx}: at least one rule is required")
    rules = []
    for j, r in enumerate(rules_raw):
        rctx = f"{ctx} rule[{j}]"
        rule = _make(RuleConfig, r, rctx)
        _validate_rule(rule, rctx)
        rules.append(rule)
    if len({r.name for r in rules}) != len(rules):
        raise ConfigError(f"{ctx}: rule names must be unique")
    source = _make(SourceConfig, {**raw, "rules": rules}, ctx)
    source.method = source.method.upper()
    if source.interval < 1:
        raise ConfigError(f"{ctx}: interval must be >= 1 second")
    return source


def parse_config(data: dict[str, Any]) -> AppConfig:
    data = _expand(data or {})
    discord = _make(DiscordConfig, data.get("discord") or {}, "discord")
    defaults = data.get("defaults") or {}
    allowed_defaults = {"interval", "timeout", "cooldown", "failure_threshold", "max_backoff"}
    bad = set(defaults) - allowed_defaults
    if bad:
        raise ConfigError(f"defaults: unknown key(s): {', '.join(sorted(bad))}")
    sources_raw = data.get("sources") or []
    if not sources_raw:
        raise ConfigError("at least one source is required")
    sources = [_build_source(s, defaults, i) for i, s in enumerate(sources_raw)]
    names = [s.name for s in sources]
    if len(set(names)) != len(names):
        raise ConfigError("source names must be unique")
    return AppConfig(
        discord=discord,
        sources=[s for s in sources if s.enabled],
        state_path=data.get("state_path", "data/state.db"),
        health_port=int(data.get("health_port", 0)),
        health_host=data.get("health_host", "0.0.0.0"),
        log_level=str(data.get("log_level", "INFO")).upper(),
    )


def load_config(path: str | Path) -> AppConfig:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read config file: {exc}") from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML: {exc}") from exc
    return parse_config(data)
