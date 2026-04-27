"""Pluggable notifiers for scan results.

Three implementations are provided: `ConsoleNotifier` (always on), and
`SlackNotifier` / `DiscordNotifier` (each activated by an env var holding a
webhook URL). Missing env vars produce notifiers that no-op rather than
raising — keeps local dev frictionless.
"""

from __future__ import annotations

import logging
import os
from typing import Protocol, runtime_checkable

import httpx
from rich.box import SIMPLE_HEAVY
from rich.console import Console
from rich.table import Table

from scanner.engine import ScanResult

log = logging.getLogger(__name__)

TRADINGVIEW_URL = "https://www.tradingview.com/chart/?symbol={ticker}"


@runtime_checkable
class Notifier(Protocol):
    def send(self, results: list[ScanResult]) -> None: ...


def _top_reasons(r: ScanResult, k: int = 3) -> list[str]:
    return list(r.reasons[:k])


def _tv_link(ticker: str) -> str:
    return TRADINGVIEW_URL.format(ticker=ticker)


class ConsoleNotifier:
    """Rich-formatted table to stdout."""

    def __init__(self, console: Console | None = None) -> None:
        self._console = console or Console()

    def send(self, results: list[ScanResult]) -> None:
        if not results:
            self._console.print("[yellow]No bullish candidates after filtering.[/yellow]")
            return
        table = Table(title="Bullish Scan", box=SIMPLE_HEAVY, show_lines=False)
        table.add_column("Ticker", style="bold")
        table.add_column("Score", justify="right")
        table.add_column("Price", justify="right")
        table.add_column("%Δ", justify="right")
        table.add_column("RelVol", justify="right")
        table.add_column("RSI", justify="right")
        table.add_column("Reasons")
        for r in results:
            reasons = "; ".join(_top_reasons(r)) or "—"
            pct_style = "green" if r.pct_change >= 0 else "red"
            table.add_row(
                r.ticker,
                f"{r.score:.1f}",
                f"${r.price:,.2f}",
                f"[{pct_style}]{r.pct_change:+.2f}%[/{pct_style}]",
                f"{r.rel_volume:.2f}x",
                f"{r.rsi:.0f}",
                reasons,
            )
        self._console.print(table)


class _WebhookNotifier:
    """Shared base: POSTs a payload to a webhook URL."""

    def __init__(self, webhook_url: str | None, platform: str) -> None:
        self._url = webhook_url
        self._platform = platform

    @property
    def enabled(self) -> bool:
        return bool(self._url)

    def _payload_lines(self, results: list[ScanResult]) -> list[str]:
        lines = ["*Bullish Scanner — top candidates*"]
        for r in results:
            reasons = ", ".join(_top_reasons(r)) or "—"
            lines.append(
                f"• *{r.ticker}* — score {r.score:.1f} | ${r.price:,.2f} "
                f"({r.pct_change:+.2f}%) | relVol {r.rel_volume:.2f}x | {reasons} "
                f"<{_tv_link(r.ticker)}|chart>"
            )
        return lines

    def send(self, results: list[ScanResult]) -> None:  # pragma: no cover - network path
        if not self._url:
            log.info("%s notifier skipped: no webhook URL configured", self._platform)
            return
        if not results:
            return
        text = "\n".join(self._payload_lines(results))
        try:
            httpx.post(self._url, json={"text": text}, timeout=5.0).raise_for_status()
        except httpx.HTTPError:
            log.exception("%s webhook POST failed", self._platform)
            raise


class SlackNotifier(_WebhookNotifier):
    def __init__(self, webhook_url: str | None = None) -> None:
        super().__init__(webhook_url or os.environ.get("SLACK_WEBHOOK_URL"), "slack")


class DiscordNotifier(_WebhookNotifier):
    def __init__(self, webhook_url: str | None = None) -> None:
        super().__init__(webhook_url or os.environ.get("DISCORD_WEBHOOK_URL"), "discord")

    def send(self, results: list[ScanResult]) -> None:  # pragma: no cover - network path
        # Discord expects {"content": ...} rather than {"text": ...}.
        if not self._url or not results:
            if not self._url:
                log.info("discord notifier skipped: no webhook URL configured")
            return
        content = "\n".join(self._payload_lines(results))
        try:
            httpx.post(self._url, json={"content": content}, timeout=5.0).raise_for_status()
        except httpx.HTTPError:
            log.exception("discord webhook POST failed")
            raise


def build_notifiers(names: list[str]) -> list[Notifier]:
    """Build notifiers by name. Unknown names are logged and skipped."""
    notifiers: list[Notifier] = []
    for name in names:
        n = name.strip().lower()
        if n == "console":
            notifiers.append(ConsoleNotifier())
        elif n == "slack":
            slack = SlackNotifier()
            if slack.enabled:
                notifiers.append(slack)
            else:
                log.info("slack notifier disabled (SLACK_WEBHOOK_URL not set)")
        elif n == "discord":
            disc = DiscordNotifier()
            if disc.enabled:
                notifiers.append(disc)
            else:
                log.info("discord notifier disabled (DISCORD_WEBHOOK_URL not set)")
        else:
            log.warning("unknown notifier %r; skipping", name)
    return notifiers
