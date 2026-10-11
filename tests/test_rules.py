import pytest

from alertmonitor.config import ConfigError, RuleConfig, parse_config
from alertmonitor.rules import build_alert, evaluate, extract
from alertmonitor.state import StateStore


def rule(**kw):
    base = dict(name="r", path="v", type="threshold", operator=">", value=10.0)
    base.update(kw)
    return RuleConfig(**base)


def test_extract_nested_and_lists():
    data = {"a": {"b": [{"c": 5}]}}
    assert extract(data, "a.b.0.c") == 5
    assert extract(data, ".") == data
    with pytest.raises(LookupError):
        extract(data, "a.x")
    with pytest.raises(LookupError):
        extract(data, "a.b.9")


def test_threshold():
    assert evaluate(rule(), 11, None, set())
    assert not evaluate(rule(), 9, None, set())
    assert not evaluate(rule(operator="<="), 11, None, set())


def test_change_percent_and_direction():
    r = rule(type="change", value=5.0, direction="up")
    assert evaluate(r, 106, 100, set())
    assert not evaluate(r, 94, 100, set())          # down move ignored
    assert not evaluate(r, 101, 100, set())         # below percent
    assert not evaluate(r, 100, None, set())        # no previous value yet
    assert evaluate(rule(type="change", value=5.0), 94, 100, set())


def test_contains_case_insensitive_and_hash_suffix():
    r = rule(type="contains", value=["major"])
    a = evaluate(r, "A MAJOR outage", None, set())
    b = evaluate(r, "A MAJOR outage", None, set())
    c = evaluate(r, "Another major outage", None, set())
    assert a and a[0].suffix == b[0].suffix != c[0].suffix
    assert not evaluate(r, "all good", None, set())


def test_new_item_skips_seen():
    r = rule(type="new_item", id_path="id", title_path="t")
    items = [{"id": 1, "t": "one"}, {"id": 2, "t": "two"}]
    out = evaluate(r, items, None, {"1"})
    assert [m.suffix for m in out] == ["2"] and out[0].title == "two"


def test_message_template_and_alert():
    r = rule(message="price {value:.1f} (was {previous})")
    m = evaluate(r, 12.34, None, set())[0]
    assert build_alert("src", r, m).description == "price 12.3 (was None)"


def test_cooldown_and_seen_state():
    s = StateStore()
    assert s.cooldown_ok("k", 60, now=1000)
    assert not s.cooldown_ok("k", 60, now=1030)
    assert s.cooldown_ok("k", 60, now=1061)
    s.add_seen("x", ["1", "2"])
    assert s.seen_ids("x") == {"1", "2"}


def test_config_validation():
    good = {"sources": [{"name": "a", "url": "http://x", "rules": [
        {"name": "r", "path": "v", "type": "threshold", "value": "5"}]}]}
    cfg = parse_config(good)
    assert cfg.sources[0].rules[0].value == 5.0
    for bad in (
        {"sources": []},
        {"sources": [{"name": "a", "url": "http://x", "rules": [{"name": "r", "path": "v", "type": "nope"}]}]},
        {"sources": [{"name": "a", "url": "http://x", "rules": [
            {"name": "r", "path": "v", "type": "threshold", "value": "abc"}]}]},
        {"sources": [{"name": "a", "url": "http://x", "bogus": 1, "rules": [
            {"name": "r", "path": "v", "type": "threshold", "value": 1}]}]},
    ):
        with pytest.raises(ConfigError):
            parse_config(bad)
