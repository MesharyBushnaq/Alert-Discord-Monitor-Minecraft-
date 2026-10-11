import httpx
import pytest

from alertmonitor.config import parse_config
from alertmonitor.metrics import Metrics
from alertmonitor.models import Alert
from alertmonitor.notifier import DiscordNotifier
from alertmonitor.poller import SourceRunner
from alertmonitor.state import StateStore


class FakeNotifier:
    def __init__(self):
        self.alerts: list[Alert] = []

    def submit(self, alert):
        self.alerts.append(alert)


def make_runner(handler, rules, **source_kw):
    cfg = parse_config({"sources": [{"name": "api", "url": "http://api.test/data", "interval": 1,
                                     "failure_threshold": 2, "rules": rules, **source_kw}]})
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    notifier = FakeNotifier()
    runner = SourceRunner(cfg.sources[0], client, StateStore(), notifier, Metrics())
    return runner, notifier, client


async def test_threshold_edge_trigger_and_resolve():
    values = iter([5, 15, 16, 4])
    handler = lambda req: httpx.Response(200, json={"v": next(values)})
    runner, n, client = make_runner(handler, [
        {"name": "high", "path": "v", "type": "threshold", "operator": ">", "value": 10,
         "notify_resolved": True}])
    for _ in range(4):
        await runner.cycle()
    titles = [a.title for a in n.alerts]
    assert titles == ["high", "Resolved: high"]  # no repeat while still above
    await client.aclose()


async def test_source_down_and_recovery_with_backoff():
    state = {"fail": True}

    def handler(req):
        return httpx.Response(500) if state["fail"] else httpx.Response(200, json={"v": 1})

    runner, n, client = make_runner(handler, [
        {"name": "r", "path": "v", "type": "threshold", "value": 100}])
    d1 = await runner.cycle()
    d2 = await runner.cycle()
    await runner.cycle()  # still down: must not alert twice
    assert [a.severity for a in n.alerts] == ["critical"]
    assert d2 > d1 * 1.2  # backoff grows
    state["fail"] = False
    await runner.cycle()
    assert [a.severity for a in n.alerts] == ["critical", "resolved"]
    await client.aclose()


async def test_etag_conditional_request_returns_304():
    seen_headers = []

    def handler(req):
        seen_headers.append(req.headers.get("if-none-match"))
        if req.headers.get("if-none-match") == "abc":
            return httpx.Response(304)
        return httpx.Response(200, json={"v": 1}, headers={"ETag": "abc"})

    runner, n, client = make_runner(handler, [{"name": "r", "path": "v", "type": "threshold", "value": 0}])
    await runner.cycle()
    await runner.cycle()
    assert seen_headers == [None, "abc"]
    assert runner.metrics.sources["api"]["not_modified"] == 1
    assert len(n.alerts) == 1  # second poll skipped rule evaluation
    await client.aclose()


async def test_new_item_seeds_then_alerts_oldest_first():
    pages = iter([
        [{"id": 2, "name": "b"}, {"id": 1, "name": "a"}],
        [{"id": 4, "name": "d"}, {"id": 3, "name": "c"}, {"id": 2, "name": "b"}],
    ])
    handler = lambda req: httpx.Response(200, json=next(pages))
    runner, n, client = make_runner(handler, [
        {"name": "rel", "path": ".", "type": "new_item", "id_path": "id", "title_path": "name"}])
    await runner.cycle()
    assert n.alerts == []                       # first run only seeds
    await runner.cycle()
    assert [a.title for a in n.alerts] == ["rel: c", "rel: d"]
    await client.aclose()


async def test_bad_path_is_a_rule_error_not_a_source_failure():
    handler = lambda req: httpx.Response(200, json={"other": 1})
    runner, n, client = make_runner(handler, [{"name": "r", "path": "v", "type": "threshold", "value": 1}])
    await runner.cycle()
    assert runner.consecutive_failures == 0
    assert runner.metrics.sources["api"]["rule_errors"] == 1
    await client.aclose()


async def test_notifier_retries_on_429_then_succeeds():
    calls = []

    def handler(req):
        calls.append(req)
        if len(calls) == 1:
            return httpx.Response(429, json={"retry_after": 0})
        return httpx.Response(204)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    metrics = Metrics()
    notifier = DiscordNotifier(client, "http://discord.test/hook", metrics=metrics)
    await notifier.start()
    notifier.submit(Alert(source="s", title="t", description="d"))
    await notifier.close()
    assert len(calls) == 2
    assert metrics.counters["alerts_sent"] == 1
    assert metrics.counters["discord_rate_limited"] == 1
    await client.aclose()


async def test_notifier_batches_burst_into_one_request():
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(204)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    notifier = DiscordNotifier(client, "http://discord.test/hook")
    for i in range(7):                       # queued before the worker starts
        notifier.submit(Alert(source="s", title=f"t{i}", description="d"))
    await notifier.start()
    await notifier.close()
    assert len(calls) == 1
    import json
    assert len(json.loads(calls[0].content)["embeds"]) == 7
    await client.aclose()


async def test_notifier_drops_on_client_error_without_retry():
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(404, json={"message": "Unknown Webhook"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    metrics = Metrics()
    notifier = DiscordNotifier(client, "http://discord.test/hook", metrics=metrics)
    await notifier.start()
    notifier.submit(Alert(source="s", title="t", description="d"))
    await notifier.close()
    assert len(calls) == 1 and metrics.counters["discord_errors"] == 1
    await client.aclose()
