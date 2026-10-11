"""Command-line entry point."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

import httpx

from . import __version__
from .config import AppConfig, ConfigError, load_config
from .metrics import Metrics, start_health_server
from .models import Alert
from .notifier import DiscordNotifier
from .poller import SourceRunner
from .state import StateStore

log = logging.getLogger("alertmonitor")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="discord-alert-monitor",
                                description="Poll APIs and send alerts to a Discord webhook.")
    p.add_argument("-c", "--config", default="config.yaml", help="path to config file (default: config.yaml)")
    p.add_argument("--dry-run", action="store_true", help="log alerts instead of sending them to Discord")
    p.add_argument("--once", action="store_true", help="poll every source once, then exit (cron/CI friendly)")
    p.add_argument("--validate", action="store_true", help="validate the config and exit")
    p.add_argument("--test-webhook", action="store_true", help="send a test message to Discord and exit")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p.parse_args(argv)


def setup_logging(level: str, verbose: bool) -> None:
    logging.basicConfig(level=logging.DEBUG if verbose else level,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)


async def run(cfg: AppConfig, args: argparse.Namespace) -> int:
    limits = httpx.Limits(max_connections=50, max_keepalive_connections=20, keepalive_expiry=60)
    headers = {"User-Agent": f"discord-alert-monitor/{__version__}"}
    metrics = Metrics()
    state = StateStore(":memory:" if args.once and args.dry_run else cfg.state_path)

    async with httpx.AsyncClient(limits=limits, headers=headers, follow_redirects=True) as client:
        notifier = DiscordNotifier(
            client, cfg.discord.webhook_url, username=cfg.discord.username,
            avatar_url=cfg.discord.avatar_url, critical_mention=cfg.discord.critical_mention,
            dry_run=args.dry_run, metrics=metrics)
        await notifier.start()

        if args.test_webhook:
            notifier.submit(Alert(source="self-test", title="Test alert", severity="info",
                                  description="Discord Alert Monitor is connected. 🎉"))
            await notifier.close()
            return 0 if not metrics.counters.get("discord_errors") else 1

        runners = [SourceRunner(s, client, state, notifier, metrics) for s in cfg.sources]
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except NotImplementedError:  # e.g. Windows
                pass

        health = None
        if cfg.health_port and not args.once:
            health = await start_health_server(metrics, cfg.health_host, cfg.health_port)

        log.info("monitoring %d source(s)%s", len(runners), " [dry-run]" if args.dry_run else "")
        if args.once:
            await asyncio.gather(*(r.cycle() for r in runners))
        else:
            tasks = [asyncio.create_task(r.run(stop), name=r.source.name) for r in runners]
            await stop.wait()
            log.info("shutting down...")
            await asyncio.gather(*tasks, return_exceptions=True)

        if health:
            health.close()
            await health.wait_closed()
        await notifier.close()
        state.close()
        return 1 if args.once and any(r.consecutive_failures for r in runners) else 0


def cli(argv: list[str] | None = None) -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    args = parse_args(argv)
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        sys.exit(2)
    setup_logging(cfg.log_level, args.verbose)
    if args.validate:
        print(f"OK: {len(cfg.sources)} source(s), "
              f"{sum(len(s.rules) for s in cfg.sources)} rule(s)")
        return
    if not cfg.discord.webhook_url and not args.dry_run:
        print("config error: discord.webhook_url is empty (set DISCORD_WEBHOOK_URL or use --dry-run)",
              file=sys.stderr)
        sys.exit(2)
    sys.exit(asyncio.run(run(cfg, args)))


if __name__ == "__main__":
    cli()
