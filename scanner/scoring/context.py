"""Immutable input to the scoring engine.

Built once per (symbol, evaluation timestamp). The engine never reads from
anything else — bars, prior closes, today's cumulative $-volume, the
historical 30-min volume profile, etc. all live here. This keeps signals
testable in isolation.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from .types import Bar


@dataclass(frozen=True)
class SignalContext:
    """All inputs required to score one symbol at one timestamp.

    Bars in `bars` are sorted ascending by `ts_ms`. The dict keys are the
    canonical timeframe labels: "1m", "5m", "15m", "daily".

    `session_start_ms` is the 09:30 ET epoch ms for the day containing
    `now_ms`. `now_ms` is the evaluation timestamp (the close of the most
    recent 1m bar by convention).

    Volume / spread / halt / TOD-volume fields are populated by the context
    builder from cached profiles; signals consume them as primitives.
    """

    symbol: str
    now_ms: int
    session_start_ms: int
    bars: Mapping[str, Sequence[Bar]]

    today_high: float
    today_low: float
    yesterday_high: float
    yesterday_low: float
    prior_session_close: float

    today_cum_dollar_volume: float
    historical_cum_dollar_volume_at_tod: float
    historical_30min_dollar_volume: float

    adv_dollar: float
    atr_pct_20d: float
    avg_spread_pct: float
    halted_recently: bool

    last_1m_volume: float
    avg_1m_volume_20bar: float
    rolling_avg_spread_pct: float

    flags: tuple[str, ...] = field(default_factory=tuple)
