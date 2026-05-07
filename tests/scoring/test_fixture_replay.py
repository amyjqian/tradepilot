"""Replay-test for the scoring engine against committed JSON fixtures.

Each fixture under `tests/scoring/fixtures/` was produced by
`scripts/fetch_test_fixtures.py`. The fixture captures both the input
`SignalContext` and the engine's output `ScoreResult`. This test
reconstructs the context and re-runs `score_symbol_sync`, asserting the
output still matches within float tolerance.

These are the load-bearing regression tests: drift in any of the 9
signals, the composite, the TOD multiplier, the bias logic, or the tier
thresholds will fail at least one assertion here.

Adding a new fixture: run

    python scripts/fetch_test_fixtures.py --symbol X --date YYYY-MM-DD \\
        --eval-time HH:MM --provider {synthetic,polygon}

and commit the resulting JSON. This test discovers fixtures
automatically.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from scanner.scoring.context import SignalContext
from scanner.scoring.engine import score_symbol_sync
from scanner.scoring.types import Bar

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _bar_from_dict(d: dict[str, Any]) -> Bar:
    return Bar(
        ts_ms=int(d["ts_ms"]),
        open=float(d["open"]),
        high=float(d["high"]),
        low=float(d["low"]),
        close=float(d["close"]),
        volume=float(d["volume"]),
    )


def _ctx_from_payload(payload: dict[str, Any]) -> SignalContext:
    c = payload["context"]
    return SignalContext(
        symbol=c["symbol"],
        now_ms=int(c["now_ms"]),
        session_start_ms=int(c["session_start_ms"]),
        bars={tf: [_bar_from_dict(b) for b in bars] for tf, bars in c["bars"].items()},
        today_high=c["today_high"],
        today_low=c["today_low"],
        yesterday_high=c["yesterday_high"],
        yesterday_low=c["yesterday_low"],
        prior_session_close=c["prior_session_close"],
        today_cum_dollar_volume=c["today_cum_dollar_volume"],
        historical_cum_dollar_volume_at_tod=c["historical_cum_dollar_volume_at_tod"],
        historical_30min_dollar_volume=c["historical_30min_dollar_volume"],
        adv_dollar=c["adv_dollar"],
        atr_pct_20d=c["atr_pct_20d"],
        avg_spread_pct=c["avg_spread_pct"],
        halted_recently=c["halted_recently"],
        last_1m_volume=c["last_1m_volume"],
        avg_1m_volume_20bar=c["avg_1m_volume_20bar"],
        rolling_avg_spread_pct=c["rolling_avg_spread_pct"],
    )


def _all_fixtures() -> list[Path]:
    if not FIXTURES_DIR.exists():
        return []
    return sorted(FIXTURES_DIR.glob("*.json"))


@pytest.mark.parametrize("fixture_path", _all_fixtures(), ids=lambda p: p.stem)
def test_fixture_replay_matches_captured_output(fixture_path: Path) -> None:
    payload = json.loads(fixture_path.read_text())
    ctx = _ctx_from_payload(payload)
    result = score_symbol_sync(ctx)

    expected = payload["score_result"]
    if expected is None:
        assert result is None, (
            f"Fixture {fixture_path.name} captured score_result=None "
            f"(symbol failed gates/DQ) but engine now returns a result"
        )
        return

    assert result is not None, (
        f"Fixture {fixture_path.name} captured a score result but engine returned None"
    )
    assert result.base_score == pytest.approx(expected["base_score"], abs=1e-3)
    assert result.tod_mult == pytest.approx(expected["tod_mult"], abs=1e-9)
    assert result.final_score == pytest.approx(expected["final_score"], abs=1e-3)
    assert result.bias_15m == expected["bias_15m"]
    assert result.tier == expected["tier"]
    assert sorted(result.flags) == sorted(expected["flags"])

    for name, expected_c in expected["components"].items():
        actual_c = result.components.get(name)
        assert actual_c is not None, f"missing component {name}"
        assert actual_c.strength == pytest.approx(expected_c["strength"], abs=1e-3)
        if expected_c["raw"] is None:
            assert actual_c.raw is None
        else:
            assert actual_c.raw == pytest.approx(expected_c["raw"], abs=1e-3)
