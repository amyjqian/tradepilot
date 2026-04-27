"""Tests for scanner.alerts — notifier assembly and formatting."""

from __future__ import annotations

from scanner.alerts import (
    ConsoleNotifier,
    DiscordNotifier,
    SlackNotifier,
    build_notifiers,
)
from scanner.engine import ScanResult


def _mk_result(ticker: str = "NVDA", score: float = 72.0) -> ScanResult:
    return ScanResult(
        ticker=ticker,
        score=score,
        price=520.13,
        pct_change=2.1,
        rel_volume=2.3,
        rsi=62.0,
        above_vwap=True,
        above_ema9=True,
        ema_stacked=True,
        dist_from_20d_high_pct=0.4,
        signals={"momentum": 0.5},
        reasons=["Relative volume 2.30x average", "Up 2.10% on the day"],
    )


def test_console_notifier_renders_empty() -> None:
    n = ConsoleNotifier()
    n.send([])  # should not raise


def test_console_notifier_renders_results() -> None:
    n = ConsoleNotifier()
    n.send([_mk_result(), _mk_result("TSLA", 65.0)])  # should not raise


def test_slack_notifier_disabled_without_env(monkeypatch) -> None:
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    s = SlackNotifier()
    assert s.enabled is False
    s.send([_mk_result()])  # skipped, not raised


def test_discord_notifier_disabled_without_env(monkeypatch) -> None:
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    d = DiscordNotifier()
    assert d.enabled is False
    d.send([_mk_result()])  # skipped


def test_slack_notifier_enabled_with_env(monkeypatch) -> None:
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://example.invalid/webhook")
    s = SlackNotifier()
    assert s.enabled is True


def test_payload_lines_contain_tradingview_link() -> None:
    s = SlackNotifier(webhook_url="https://example.invalid/webhook")
    lines = s._payload_lines([_mk_result("AMD", 80.0)])
    assert any("tradingview.com/chart" in line for line in lines)
    assert any("AMD" in line for line in lines)


def test_build_notifiers_console(monkeypatch) -> None:
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    notifiers = build_notifiers(["console", "slack", "discord", "bogus"])
    # slack/discord skipped (no env); bogus skipped with a warning.
    assert len(notifiers) == 1
    assert isinstance(notifiers[0], ConsoleNotifier)


def test_build_notifiers_with_slack(monkeypatch) -> None:
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://example.invalid/webhook")
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    notifiers = build_notifiers(["slack"])
    assert len(notifiers) == 1
    assert isinstance(notifiers[0], SlackNotifier)
