# Per-2-Minute Scoring — Worked Example

**Purpose:** Faster-cadence variant of the watchlist scanner for users who want sub-5-minute reaction time without the noise of 1m. Use this when the 5m cadence feels too slow but 1m feels too jumpy.

**Companion to:** `CORE_ENGINE_SPEC.md`, `PER_MINUTE_SCORING.md`, `PER_5_MINUTE_SCORING.md`.

---

## 1. What "scoring per 2 minutes" means

Run the same `score_symbol()` engine, but trigger the cycle every 2 minutes (on :00, :02, :04, :06, :08, :10 ... boundaries) instead of every 5. The math is identical — same 9 signals, same weights summing to 1.00, same composite formula, same TOD multiplier, same tier logic.

**The difference is purely cadence, not logic.**

That said, 2m as a *cadence* implies the user wants reactivity, which interacts subtly with how some signals behave. Sections 4 and 5 cover those nuances.

---

## 2. When to use 2m vs 1m vs 5m

| Cadence | Best for | Tradeoff |
|---|---|---|
| **5m** (default) | Watchlist + sector rotation, day-trade hold ≥10 min | Misses fast moves; "feels slow" during volatile opens |
| **2m** | Tactical entries when watching a specific name; gappers; volatile midday | Some signals are still 5m-bound (read 5m bars) — score doesn't fully refresh between 5m boundaries |
| **1m** | Breakout-event scanners, alert-driven watching, scalping | Noisy; signals like `vwap_distance_atr` and `trend_stack_5m` can't change between 5m closes |

**The honest truth about 2m:** since 4 of the 9 signals read 5m bars, the score only meaningfully changes when a 5m bar closes. Running the cycle every 2 minutes captures the 1m-based signals (`rvol_30m`, `momentum_atr`, `breakout_proximity`, `mtf_alignment` 1m branch, disqualifiers) freshly, but the 5m-based signals stay frozen between 5m closes. That's fine — the 1m-based signals are the ones that benefit most from fast refresh anyway.

If you want **truly fresh 2m signals**, you'd need to add a `trend_stack_2m`, `vwap_distance_2m_atr`, `rsi_2m`, etc. variant set. That's a different design — see Section 6 for what it would take.

---

## 3. Same signal-to-timeframe mapping

| Signal | Reads | Updates per 2m cycle? |
|---|---|---|
| `rvol_30m` | 1m bars | ✓ (fresh — last 30 1m bars) |
| `rvol_cumulative` | 1m bars | ✓ (fresh — running cumulative) |
| `momentum_atr` | 1m + daily | ✓ (fresh — latest 1m close) |
| `vwap_distance_atr` | 5m bars | ☓ frozen until next 5m close |
| `trend_stack_5m` | 5m bars | ☓ frozen until next 5m close |
| `mtf_alignment` | 1m + 5m + 15m | partial (1m branch fresh; 5m/15m branches frozen) |
| `rsi_intraday` | 5m bars | ☓ frozen until next 5m close |
| `breakout_proximity` | 1m bars | ✓ (fresh) |
| `clean_structure` | 5m bars | ☓ frozen until next 5m close |

**5 signals refresh on 2m boundaries; 4 signals stay frozen until next 5m boundary.** This is the explicit tradeoff — you get faster response on the 1m-based signals (which are the most volatile anyway) without paying for full recomputation of the 5m-based signals.

---

## 4. Weights (unchanged from base spec)

Same as 1m and 5m versions:

| Signal | Weight |
|---|---|
| `rvol_30m` | 0.15 |
| `rvol_cumulative` | 0.10 |
| `momentum_atr` | 0.15 |
| `vwap_distance_atr` | 0.10 |
| `trend_stack_5m` | 0.10 |
| `mtf_alignment` | 0.10 |
| `rsi_intraday` | 0.10 |
| `breakout_proximity` | 0.10 |
| `clean_structure` | 0.10 |
| **Sum** | **1.00** |

---

## 5. Worked example — NVDA at 10:44:00 ET, 2026-04-15

This is **2 minutes after the 1m worked example** (T=10:42) and **1 minute before the 5m worked example** (T=10:45). It's a 2m boundary but NOT a 5m boundary, so the 5m-bound signals still hold their 10:40 values.

### 5.1 Step 1 — Build SignalContext at T = 10:44:00 ET

```
T            = 10:44:00 ET → 1744730640000 ms epoch
session_start = 09:30:00 ET → 1744726200000 ms epoch

bars["1m"]   = 74 bars     (09:30 → 10:43 inclusive)
bars["5m"]   = 14 closed   (09:30 → 10:40, plus 10:40-10:45 STILL in-progress)
bars["15m"]  = 4 closed    (09:30 → 10:30, plus 10:30-10:45 in-progress)
bars["daily"] = last 30 closed daily bars

today_high           = $125.10
today_low            = $121.80
yesterday_high       = $124.50
prior_session_close  = $122.10
```

**What changed since 10:42:**
- 2 new 1m bars closed (10:42, 10:43)
- **No new 5m bar yet** — the 10:40-10:45 5m bar is still forming
- **No new 15m bar yet** — the 10:30-10:45 15m bar is still forming
- Price moved $124.30 → $124.45

### 5.2 Step 3 — Compute 9 signal strengths

I'll only show the signals that change vs the 10:42 example. The 5m-bound signals are FROZEN.

#### `rvol_30m` (FRESH)

```python
recent_$vol  = $25.3M    # 2 new 1m bars added a bit more
historical_avg = $7.85M  # tiny TOD shift
ratio        = 25.3 / 7.85 = 3.223
strength     = clip((3.223 - 1.5) / 2.5, 0, 1) = 0.689   # was 0.656
```

#### `rvol_cumulative` (FRESH)

```python
today_cum_$vol = $131.5M    # +$3.5M since 10:42
typical_cum    = $53.0M     # rises with TOD
ratio          = 131.5 / 53.0 = 2.481
strength       = clip((2.481 - 1.0) / 2.0, 0, 1) = 0.741   # was 0.731
```

#### `momentum_atr` (FRESH)

```python
today_close  = $124.45
pct_move     = (124.45 - 122.10) / 122.10 = 1.925%
atr_pct      = 2.10%
move_in_atr  = 1.925 / 2.10 = 0.917
strength     = clip(0.917 / 2.0, 0, 1) = 0.458   # was 0.429
```

#### `vwap_distance_atr` (**FROZEN** — same 5m bar as at 10:42)

```python
strength = 0.619    # unchanged from 10:42
```

#### `trend_stack_5m` (**FROZEN**)

```python
strength = 1.000    # unchanged
```

#### `mtf_alignment` (PARTIAL — 1m branch fresh, 5m/15m frozen)

```python
stack_1m  = 1.00   # 1m EMAs/VWAP recomputed with 2 new bars; still stacked
stack_5m  = 1.00   # frozen
stack_15m = 0.75   # frozen (still partial)
count = 3
strength = 1.000   # unchanged in this case
```

#### `rsi_intraday` (**FROZEN**)

```python
strength = 1.000    # 5m RSI unchanged
```

#### `breakout_proximity` (FRESH)

```python
target = min($125.10, $124.50) = $124.50
close  = $124.45
distance_pct = (124.50 - 124.45) / 124.45 × 100 = 0.040%

last 5 1m highs ascending? yes
strength = clip(1.0 - 0.040/5.0, 0, 1) = 0.992   # was 0.968 — getting closer to break
```

#### `clean_structure` (**FROZEN**)

```python
strength = 0.800    # unchanged
```

### 5.3 Step 4 — Composite

```python
raw =   0.15 × 0.689     # rvol_30m            = 0.1034   (was 0.0984)
      + 0.10 × 0.741     # rvol_cumulative     = 0.0741   (was 0.0731)
      + 0.15 × 0.458     # momentum_atr        = 0.0687   (was 0.0644)
      + 0.10 × 0.619     # vwap_distance_atr   = 0.0619   (frozen)
      + 0.10 × 1.000     # trend_stack_5m      = 0.1000   (frozen)
      + 0.10 × 1.000     # mtf_alignment       = 0.1000
      + 0.10 × 1.000     # rsi_intraday        = 0.1000   (frozen)
      + 0.10 × 0.992     # breakout_proximity  = 0.0992   (was 0.0968)
      + 0.10 × 0.800     # clean_structure     = 0.0800   (frozen)
                                              ─────────
                                                0.7873

base_score = 78.73    # was 77.46 at 10:42
```

### 5.4 Step 5–8 — TOD, bias, disqualifiers, tier

```python
tod_mult     = 1.10  (10:30-11:30 window)
final_score  = 78.73 × 1.10 = 86.60   # was 85.21 at 10:42

bias_15m  = "long"  (still — 15m bar still forming, EMAs still stacked)
disqualifiers: PASS

tier = "A"   (≥85 + long bias)
```

### 5.5 Final ScoreResult

```python
ScoreResult(
    symbol = "NVDA",
    timestamp = 1744730640000,    # 10:44:00 ET
    base_score = 78.73,
    tod_mult = 1.10,
    final_score = 86.60,          # ↑ slightly from 85.21 at 10:42
    bias_15m = "long",
    flags = [],
    tier = "A",
)
```

---

## 6. Comparison across cadences (same NVDA fixture)

| Timestamp | Cadence | base_score | final_score | tier |
|---|---|---|---|---|
| 10:42:00 ET | 1m | 77.46 | 85.21 | A |
| 10:44:00 ET | 2m | 78.73 | 86.60 | A |
| 10:45:00 ET | 5m | 67.07 | 73.78 | C |

**Notice the cliff at 10:45.** Between 10:44 (86.60) and 10:45 (73.78), the score dropped 13 points in a single minute. Why?

At 10:45, the 5m bar (10:40-10:45) **closed**, which:
1. Triggered fresh computation of `vwap_distance_atr` → dropped from 0.619 to 0.357 (price extended further past VWAP)
2. Triggered fresh `breakout_proximity` evaluation against the new bar → price had broken through, signal collapsed to 0
3. Triggered the 15m branch update of `mtf_alignment` (also a 15m close)

Until 10:45, the 2m-cadence scanner was looking at "stale" 5m signals from the 10:40 bar. As soon as the 5m bar closed at 10:45, all four 5m-bound signals re-evaluated and the score reflected the new reality.

**This is a real artifact of 2m cadence on a system with 5m-bound signals.** You see fast-moving signals fluctuate but the 5m foundation only updates every 5 min. The score "lags" until the 5m boundary hits, then can jump suddenly.

---

## 7. Should you use 2m cadence?

**Probably not** for the watchlist scanner. The cliff effect above is the symptom of cadence-signal-mismatch. You're paying for fresh computation 60% more often (5 cycles per 5min instead of 1) without getting proportionally fresher information.

**Use 2m if:**
- You're specifically watching gappers or news-driven names where the 1m-based signals (`rvol_30m`, `momentum_atr`, `breakout_proximity`) are the dominant decision drivers
- You want faster reaction time but find 1m too noisy
- You're OK with the score "stepping" at 5m boundaries

**Stick with 5m if:**
- You're scoring a watchlist or sector rotation (the default)
- You want score values to evolve smoothly
- You hold trades 10+ minutes after entry

**Use 1m if:**
- You need event-level reaction (breakout-the-second-it-happens)
- You're running an alert-driven specialty scanner
- You're scalping (hold time < 5 min)

---

## 8. If you really want fresh 2m signals (advanced)

Adding a true 2m timeframe would require:

1. Bar aggregator emits 2m bars (2-minute boundary closes from 1m stream)
2. New signal variants:
   - `trend_stack_2m` (close vs 2m VWAP, 2m EMAs)
   - `vwap_distance_2m_atr`
   - `rsi_2m`
   - Add `2m_stack` to `mtf_alignment` (now checks 4 timeframes, not 3)
3. Reweight everything (current 1.00 sum redistributed)
4. Re-validate signal mappings (RSI behaves very differently on 2m vs 5m bars)

This is a significant redesign. **Don't do it for the MVP.** Validate the engine on 1m/5m first, gather real-world feedback, then decide if 2m is worth the complexity.

---

## 9. Implementation hint

```python
class TwoMinuteScanner(CompositeScanner):
    type_id = "watchlist_2m"
    type_name = "Watchlist (2m cadence)"
    
    def refresh_interval_seconds(self) -> int:
        return 120   # 2 minutes
```

That's the entire change vs a 5m-cadence scanner. Same engine, same signals, same weights — just a different sleep interval in the runner loop.

---

## 10. Summary

- **2m is a cadence variant, not a different engine.** Math is identical to 1m and 5m.
- **5 of 9 signals refresh on 2m boundaries; 4 stay frozen until 5m boundaries.** This creates a visible "cliff" in score evolution at 5m boundaries.
- **2m is rarely the right default.** Use 5m for normal watchlist scanning; use 1m only for alert-driven specialty scanners.
- **If you really want fresh 2m signals,** you need a real 2m timeframe with new signal variants. Save that for v2.
- **Worked example shows NVDA at T=10:44 scores 86.60 (A)** — between the 1m example (85.21 at 10:42) and 5m example (73.78 at 10:45).
