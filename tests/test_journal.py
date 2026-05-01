"""Tests for the SQLite trade journal + session/kill-switch bookkeeping."""

from __future__ import annotations

from pathlib import Path

import pytest

from scanner.broker.journal import Journal


@pytest.fixture
def journal(tmp_path: Path) -> Journal:
    return Journal(tmp_path / "journal.sqlite")


def test_record_fill_is_idempotent(journal: Journal) -> None:
    journal.record_fill(
        exec_id="x1", ts="2026-05-01T15:00:00Z",
        symbol="AAPL", side="buy", qty=10, price=100.0,
    )
    # Re-recording the same exec_id is a no-op — neither raises nor double-counts.
    journal.record_fill(
        exec_id="x1", ts="2026-05-01T15:00:00Z",
        symbol="AAPL", side="buy", qty=10, price=100.0,
    )
    # No close-trade yet — only one entry fill, no opposing fill.
    assert journal.list_trades() == []


def test_long_round_trip_pairs_fifo(journal: Journal) -> None:
    journal.record_fill(
        exec_id="e1", ts="2026-05-01T15:00:00Z",
        symbol="AAPL", side="buy", qty=10, price=100.0,
        planned_stop=95.0, score_at_entry=8.0,
    )
    closed = journal.record_fill(
        exec_id="e2", ts="2026-05-01T16:00:00Z",
        symbol="AAPL", side="sell", qty=10, price=110.0,
    )
    assert closed is not None
    assert closed["side"] == "long"
    assert closed["qty"] == 10
    assert closed["entry_avg"] == pytest.approx(100.0)
    assert closed["exit_avg"] == pytest.approx(110.0)
    assert closed["pnl_abs"] == pytest.approx(100.0)  # 10 shares * $10
    # R = (110 - 100) / (100 - 95) = 2.0
    assert closed["r_multiple"] == pytest.approx(2.0)
    assert closed["win"] == 1
    assert closed["holding_sec"] == 3600


def test_short_round_trip(journal: Journal) -> None:
    journal.record_fill(
        exec_id="e1", ts="2026-05-01T15:00:00Z",
        symbol="TSLA", side="sell", qty=5, price=200.0,
        planned_stop=210.0,
    )
    closed = journal.record_fill(
        exec_id="e2", ts="2026-05-01T16:00:00Z",
        symbol="TSLA", side="buy", qty=5, price=190.0,
    )
    assert closed is not None
    assert closed["side"] == "short"
    assert closed["pnl_abs"] == pytest.approx(50.0)  # 5 * (200-190)
    # R = (200 - 190) / (210 - 200) = 1.0
    assert closed["r_multiple"] == pytest.approx(1.0)


def test_partial_close_leaves_residual(journal: Journal) -> None:
    journal.record_fill(
        exec_id="e1", ts="2026-05-01T15:00:00Z",
        symbol="MSFT", side="buy", qty=100, price=300.0,
    )
    first_close = journal.record_fill(
        exec_id="e2", ts="2026-05-01T16:00:00Z",
        symbol="MSFT", side="sell", qty=40, price=310.0,
    )
    assert first_close is not None
    assert first_close["qty"] == 40
    # Residual 60 shares — second close should pair with them.
    second_close = journal.record_fill(
        exec_id="e3", ts="2026-05-01T17:00:00Z",
        symbol="MSFT", side="sell", qty=60, price=320.0,
    )
    assert second_close is not None
    assert second_close["qty"] == 60
    assert second_close["entry_avg"] == pytest.approx(300.0)
    assert second_close["exit_avg"] == pytest.approx(320.0)


def test_scaling_in_weighted_entry(journal: Journal) -> None:
    journal.record_fill(
        exec_id="e1", ts="2026-05-01T15:00:00Z",
        symbol="NVDA", side="buy", qty=50, price=100.0,
        planned_stop=95.0,
    )
    journal.record_fill(
        exec_id="e2", ts="2026-05-01T15:30:00Z",
        symbol="NVDA", side="buy", qty=50, price=110.0,
        planned_stop=105.0,
    )
    closed = journal.record_fill(
        exec_id="e3", ts="2026-05-01T16:00:00Z",
        symbol="NVDA", side="sell", qty=100, price=120.0,
    )
    assert closed is not None
    # qty-weighted entry: (50*100 + 50*110) / 100 = 105
    assert closed["entry_avg"] == pytest.approx(105.0)
    assert closed["planned_stop"] == pytest.approx(100.0)  # weighted stop
    # opened_at takes the EARLIEST entry fill
    assert closed["opened_at"] == "2026-05-01T15:00:00Z"


def test_session_baseline_is_sticky(journal: Journal) -> None:
    sess = journal.get_or_init_session("2026-05-01", 10000.0)
    assert sess["start_equity"] == 10000.0
    # Subsequent calls do not overwrite the baseline.
    sess2 = journal.get_or_init_session("2026-05-01", 9500.0)
    assert sess2["start_equity"] == 10000.0
    assert sess2["kill_active"] == 0


def test_kill_switch_persists_and_resets(journal: Journal) -> None:
    journal.get_or_init_session("2026-05-01", 10000.0)
    journal.trip_kill("2026-05-01", "drawdown breach")
    sess = journal.get_session("2026-05-01")
    assert sess is not None
    assert sess["kill_active"] == 1
    assert sess["kill_reason"] == "drawdown breach"
    journal.reset_kill("2026-05-01")
    sess = journal.get_session("2026-05-01")
    assert sess is not None
    assert sess["kill_active"] == 0
    assert sess["kill_reason"] is None


def test_stats_aggregate(journal: Journal) -> None:
    # Win
    journal.record_fill(
        exec_id="e1", ts="2026-05-01T15:00:00Z", symbol="A",
        side="buy", qty=10, price=100, planned_stop=95,
    )
    journal.record_fill(
        exec_id="e2", ts="2026-05-01T16:00:00Z", symbol="A",
        side="sell", qty=10, price=110,
    )
    # Loss
    journal.record_fill(
        exec_id="e3", ts="2026-05-01T15:00:00Z", symbol="B",
        side="buy", qty=10, price=100, planned_stop=95,
    )
    journal.record_fill(
        exec_id="e4", ts="2026-05-01T16:00:00Z", symbol="B",
        side="sell", qty=10, price=90,
    )
    stats = journal.stats()
    assert stats["n_trades"] == 2
    assert stats["wins"] == 1
    assert stats["losses"] == 1
    assert stats["win_rate_pct"] == pytest.approx(50.0)
