# Per-Minute Scoring — Worked Example

**Purpose:** Concrete reference showing what the engine does at a 1-minute boundary. Use this to verify your implementation produces the same numbers when given the same input.

**Companion to:** `CORE_ENGINE_SPEC.md` (engine logic) and `VERIFICATION_MVP_SPEC.md` (system wrapping).

> **⚠️ Weight correction (vs earlier drafts):** the spec previously had weights summing to 0.90, not 1.0. This document uses the **corrected weights summing to 1.00 exactly**. If you see older numbers (e.g., base_score = 68.56 for NVDA at 10:42), they were computed against the buggy weights. The numbers in this document are authoritative.

---

## 1. What "scoring per minute" means

The engine computes **one score per symbol per evaluation cycle.** A cycle is triggered every 60 seconds (default) by the scanner runner. At each cycle, every symbol in the active universe is re-scored using the latest bars across all three timeframes.

Importantly: there is NOT one score per timeframe. There is **one score that uses bars from all three timeframes**. The 1m / 5m / 15m discipline is about *which signal reads which timeframe*, not about producing separate scores.

---

## 2. The cycle trigger

```
WebSocket tick arrives
        │
        ▼
bar_aggregator updates the in-progress 1m bar
        │
        ▼
At minute boundary (e.g., 10:42:00 ET):
  - 1m bar closes → emit barClosed(symbol, bar)
  - aggregator updates in-progress 5m bar (close, high, low)
  - aggregator updates in-progress 15m bar
  - if 5m boundary crossed → close prior 5m bar, start new one
  - if 15m boundary crossed → close prior 15m bar, start new one
        │
        ▼
ScannerRunner.loop() (every 60s):
  for each enabled scanner:
    universe = scanner.get_universe()
    for symbol in universe:
        result = score_symbol(build_context(symbol, now_ts))
    rankings = sort by final_score
    emit rankingsUpdated(scanner_id, rankings)
```

The 60-second cadence is configurable per scanner via `Scanner.refresh_interval_seconds()`.

---

## 3. Which signals read which timeframe

| Signal | Reads | What it actually looks at |
|---|---|---|
| `rvol_30m` | 1m bars | Sum of $-volume over last 30 1m bars |
| `rvol_cumulative` | 1m bars | Cumulative $-volume since 09:30 today |
| `momentum_atr` | 1m + daily | Today's % move from prior close, normalized by daily ATR% |
| `vwap_distance_atr` | 5m bars | (close − VWAP) / 5m ATR |
| `trend_stack_5m` | 5m bars | close vs VWAP, EMA9/20/50 stacking on 5m |
| `mtf_alignment` | 1m + 5m + 15m | Trend stack on each TF, count how many ≥ 0.75 |
| `rsi_intraday` | 5m bars | RSI(14) on 5m closes |
| `breakout_proximity` | 1m bars | Distance from HoD/PDH; last 5 1m bars must show higher highs |
| `clean_structure` | 5m bars | Last 20 5m bars: count close-direction flips |

Bars are aggregated client-side: 5m and 15m are built from the 1m stream. Polygon delivers 1m bars only.

---

## 4. Weights (corrected, sum to 1.00)

| # | Signal | Weight |
|---|---|---|
| 1 | `rvol_30m` | 0.15 |
| 2 | `rvol_cumulative` | 0.10 |
| 3 | `momentum_atr` | 0.15 |
| 4 | `vwap_distance_atr` | 0.10 |
| 5 | `trend_stack_5m` | 0.10 |
| 6 | `mtf_alignment` | 0.10 |
| 7 | `rsi_intraday` | 0.10 |
| 8 | `breakout_proximity` | 0.10 |
| 9 | `clean_structure` | 0.10 |
| | **Sum** | **1.00** |

`rvol_30m` and `momentum_atr` are the dominant signals at 0.15. All seven other signals are equal at 0.10. Strict validation: `assert abs(sum(weights) - 1.0) < 1e-9`.

---

## 5. The 8-step scoring flow

```
score_symbol(context) →

  Step 1: Build SignalContext (caller does this)
  Step 2: Apply universe gates  →  pass / fail
  Step 3: Compute 9 signal strengths
  Step 4: Composite weighted sum × 100
  Step 5: Apply TOD multiplier
  Step 6: Compute 15m bias (long / short / neutral)
  Step 7: Check live disqualifiers
  Step 8: Assign tier (A / B / C / None)

  → return ScoreResult
```

---

## 6. Worked example — NVDA at 10:42:00 ET, 2026-04-15

Numbers are illustrative but internally consistent. Use the same fixture data and your implementation should produce these exact numbers (within float tolerance).

### 6.1 Step 1 — Build SignalContext

```
T            = 10:42:00 ET → 1744730520000 ms epoch
session_start = 09:30:00 ET → 1744726200000 ms epoch

bars["1m"]   = 72 bars     (09:30 → 10:41 inclusive)
bars["5m"]   = 14 closed   (09:30 → 10:40, plus 10:40-10:45 in-progress)
bars["15m"]  = 4 closed    (09:30 → 10:30, plus 10:30-10:45 in-progress)
bars["daily"] = last 30 closed daily bars

today_high           = $125.10
today_low            = $121.80
yesterday_high       = $124.50
yesterday_low        = $119.40
prior_session_close  = $122.10

historical_30min_volumes[10:12-10:42 TOD] = $7.8M
```

### 6.2 Step 2 — Universe gates

```
Price $124.30 ∈ [$5, $500]?            ✓
20-day ADV $480M ≥ $20M?                ✓
Today cum $-vol $128M ≥ $3M?            ✓
Avg spread 0.04% ≤ 0.10%?               ✓
20-day ATR% 2.1% ≥ 2.0%?                ✓
Halted in last 30s?                     no
                                        ─────
                                        ALL PASS → continue
```

### 6.3 Step 3 — Compute 9 signal strengths

#### 6.3.1 `rvol_30m` (weight 0.15)

```python
recent_$vol     = sum(dollar_volume(b) for b in bars["1m"][-30:])
                = $24.5M

historical_avg  = mean(historical_30min_volumes[current_tod])
                = $7.8M

ratio           = 24.5 / 7.8 = 3.141

# Mapping: 1.5× → 0, 4.0× → 1, linear clip
strength        = clip((3.141 - 1.5) / (4.0 - 1.5), 0, 1)
                = clip(1.641 / 2.5, 0, 1)
                = 0.656
```

#### 6.3.2 `rvol_cumulative` (weight 0.10)

```python
today_cum_$vol  = $128.0M
typical_cum     = $52.0M
ratio           = 128.0 / 52.0 = 2.462

# Mapping: 1.0× → 0, 3.0× → 1, linear clip
strength        = clip((2.462 - 1.0) / 2.0, 0, 1)
                = 0.731
```

#### 6.3.3 `momentum_atr` (weight 0.15)

```python
today_close     = bars["1m"][-1].close = $124.30
pct_move        = (124.30 - 122.10) / 122.10 = 0.01802 (1.802%)

atr_pct_value   = atr_pct(bars["daily"][-21:], 14)[-1] = 2.10%
move_in_atr     = 1.802 / 2.10 = 0.858 ATR

# Mapping: 0 ATR → 0, 2 ATR → 1, linear; negative clipped
strength        = clip(0.858 / 2.0, 0, 1)
                = 0.429
```

#### 6.3.4 `vwap_distance_atr` (weight 0.10)

```python
close           = bars["5m"][-1].close = $124.30
vwap_5m         = vwap_session(bars["5m"], session_start)[-1] = $123.85
atr_5m          = atr(bars["5m"], 14)[-1] = $0.42

distance_atr    = (124.30 - 123.85) / 0.42 = 1.071 ATR

# Triangular: 0→0, +0.5 ATR→1, +2 ATR→0
# distance is between 0.5 and 2.0:
strength        = (2.0 - 1.071) / (2.0 - 0.5)
                = 0.929 / 1.5
                = 0.619
```

#### 6.3.5 `trend_stack_5m` (weight 0.10)

```python
closes_5m  = [b.close for b in bars["5m"]]
ema9_5m    = ema(closes_5m, 9)[-1]   = $124.05
ema20_5m   = ema(closes_5m, 20)[-1]  = $123.60
ema50_5m   = ema(closes_5m, 50)[-1]  = $123.10
vwap_5m    = $123.85
close_5m   = $124.30

count = sum([
    close_5m > vwap_5m,        # 124.30 > 123.85 → True
    close_5m > ema9_5m,        # 124.30 > 124.05 → True
    ema9_5m > ema20_5m,        # 124.05 > 123.60 → True
    ema20_5m > ema50_5m,       # 123.60 > 123.10 → True
])
# count = 4
strength = 4 / 4 = 1.000
```

#### 6.3.6 `mtf_alignment` (weight 0.10)

```python
stack_1m  = trend_stack_at("1m")  = 1.00     # ≥ 0.75 → pass
stack_5m  = trend_stack_at("5m")  = 1.00     # ≥ 0.75 → pass
stack_15m = trend_stack_at("15m") = 0.75     # ≥ 0.75 → pass

count = 3
strength = 3 / 3 = 1.000
```

#### 6.3.7 `rsi_intraday` (weight 0.10)

```python
rsi_5m_value = rsi([b.close for b in bars["5m"]], 14)[-1] = 67.2

# Shape:
#   < 50: 0
#   50→60: ramp 0→1
#   60→80: hold at 1.0      ← we're here
#   80→90: decay 1→0.3
#   > 90: 0

strength = 1.000
```

#### 6.3.8 `breakout_proximity` (weight 0.10)

```python
target = min(today_high, yesterday_high)
       = min(125.10, 124.50)
       = $124.50

close = bars["1m"][-1].close = $124.30
distance_pct = (124.50 - 124.30) / 124.30 × 100 = 0.161%

# Check higher highs in last 5 1m bars:
last5_highs = [124.05, 124.12, 124.18, 124.25, 124.30]
all monotonic ascending → True

# Mapping: 0% off → 1, 5% off → 0, linear
strength = clip(1.0 - 0.161 / 5.0, 0, 1)
         = clip(0.968, 0, 1)
         = 0.968
```

#### 6.3.9 `clean_structure` (weight 0.10)

```python
last_20_closes_5m = [c1, c2, ..., c20]
flips = count of i where (c[i] > c[i-1]) != (c[i-1] > c[i-2])
      = 4   # 4 direction changes in 20 bars

strength = max(0, 1 - 4/20) = 0.800
```

### 6.4 Step 4 — Composite

```python
raw =   0.15 × 0.656     # rvol_30m            = 0.0984
      + 0.10 × 0.731     # rvol_cumulative     = 0.0731
      + 0.15 × 0.429     # momentum_atr        = 0.0644
      + 0.10 × 0.619     # vwap_distance_atr   = 0.0619
      + 0.10 × 1.000     # trend_stack_5m      = 0.1000
      + 0.10 × 1.000     # mtf_alignment       = 0.1000
      + 0.10 × 1.000     # rsi_intraday        = 0.1000
      + 0.10 × 0.968     # breakout_proximity  = 0.0968
      + 0.10 × 0.800     # clean_structure     = 0.0800
                                              ─────────
                                                0.7746

base_score = 0.7746 × 100 = 77.46
```

### 6.5 Step 5 — TOD multiplier

```python
T = 10:42 ET
TOD_MULTIPLIERS:
  09:30–09:45 → 0.85
  09:45–10:30 → 1.20
  10:30–11:30 → 1.10  ← we're here
  11:30–14:00 → 0.85
  14:00–15:30 → 1.10
  15:30–16:00 → 0.95

mult = 1.10
final_score = 77.46 × 1.10 = 85.21
```

### 6.6 Step 6 — 15m bias

```python
closes_15m = [b.close for b in bars["15m"]]
ema9_15m   = ema(closes_15m, 9)[-1]  = $123.85
ema21_15m  = ema(closes_15m, 21)[-1] = $123.20
vwap_15m   = vwap_session(bars["15m"], session_start)[-1] = $123.50
close_15m  = $124.30

ema9 > ema21?      yes
close > vwap_15m?  yes
                   ────────
                   bias = "long"
```

### 6.7 Step 7 — Live disqualifiers

```python
halted_recently?                         no
spread (0.04%) > 2× rolling avg (0.04%)? no
last_1m_volume / avg_1m_volume = 1.3?    no   (< 0.5 would trigger)
ATR-scale gap-down through VWAP?         no
                                          ────────
                                          PASS
```

### 6.8 Step 8 — Tier

```python
final_score = 85.21
bias = "long"

if final_score >= 85 and bias == "long":   tier = "A"   ← we're here
elif final_score >= 75 and bias == "long": tier = "B"
elif final_score >= 70:                    tier = "C"
else:                                       tier = None

tier = "A"
```

### 6.9 Final ScoreResult

```python
ScoreResult(
    symbol = "NVDA",
    timestamp = 1744730520000,
    base_score = 77.46,
    tod_mult = 1.10,
    final_score = 85.21,
    bias_15m = "long",
    flags = [],
    tier = "A",
    components = {
        "rvol_30m":          SignalResult("rvol_30m", 0.656, 3.141),
        "rvol_cumulative":   SignalResult("rvol_cumulative", 0.731, 2.462),
        "momentum_atr":      SignalResult("momentum_atr", 0.429, 0.858),
        "vwap_distance_atr": SignalResult("vwap_distance_atr", 0.619, 1.071),
        "trend_stack_5m":    SignalResult("trend_stack_5m", 1.000, 4),
        "mtf_alignment":     SignalResult("mtf_alignment", 1.000, 3),
        "rsi_intraday":      SignalResult("rsi_intraday", 1.000, 67.2),
        "breakout_proximity":SignalResult("breakout_proximity", 0.968, 0.161),
        "clean_structure":   SignalResult("clean_structure", 0.800, 4),
    },
)
```

---

## 7. What 1m bars specifically contribute

The 1m timeframe is the foundation of everything. Even signals that read 5m or 15m use bars that were aggregated from 1m by the bar aggregator. Direct 1m consumers:

| Component | How 1m bars are used |
|---|---|
| `rvol_30m` | Sums 1m $-volume over last 30 bars |
| `rvol_cumulative` | Sums 1m $-volume since 09:30 |
| `momentum_atr` | Reads the most recent 1m close as "now's price" |
| `mtf_alignment` (1m branch) | Computes 1m EMA9/20/50 stack and 1m VWAP |
| `breakout_proximity` | Checks last 5 1m bars for higher-highs structure |
| Disqualifiers | Last 1m bar volume vs 20-bar rolling avg |
| Universe gates | Today's cumulative $-volume from 1m bars |

If 1m bars are wrong (gap, missing, stale), these signals are wrong. **The bar aggregator's correctness is the single most important thing to verify in the data layer.**

---

## 8. Verification approach

To verify your implementation:

1. **Pick a real fixture day** for NVDA (or any liquid name). Fetch via `scripts/fetch_test_fixtures.py` per `CORE_ENGINE_SPEC.md` Section 12.
2. **Pick a specific evaluation timestamp** within that day (e.g., 10:42:00 ET).
3. **Manually compute the same trace** using a spreadsheet or notebook — pull bars up to T, compute each signal, the composite, the TOD mult, the bias, the tier.
4. **Run your `score_symbol()`** on the same fixture truncated to T.
5. **Compare numbers** — they should match within ~0.001 tolerance for floats.

If numbers diverge, the divergence pinpoints the bug:

| Signal differs | Most likely cause |
|---|---|
| `rvol_30m`, `rvol_cumulative` | Wrong TOD anchoring or historical volume lookup |
| `momentum_atr` | ATR period mis-set, or prior_close from wrong day |
| `vwap_distance_atr` | VWAP not session-anchored, or 5m ATR formula wrong |
| `trend_stack_5m` | EMA period drift, or VWAP not freshly computed |
| `mtf_alignment` | Trend-stack threshold (0.75) not applied consistently |
| `rsi_intraday` | RSI smoothing different from Wilder's; check first-bar handling |
| `breakout_proximity` | "Higher highs" check not strict, or wrong target level |
| `clean_structure` | Off-by-one on flip count |
| `final_score` differs but components match | TOD multiplier wrong or DST not handled |
| `tier` differs but score matches | Threshold edge case (75.0 exactly: B or C?); strict-vs-permissive C tier |

---

## 9. Edge cases worth testing

- **First minute of session (09:30:00):** `rvol_30m` has < 30 bars; should it return 0, None, or partial? (Convention: return 0 strength if < N bars.)
- **First 14 minutes (RSI not formed):** `rsi_intraday` returns 0 strength.
- **Pre-market (08:00 ET):** TOD multiplier returns 1.0 (outside session windows). Scoring is technically possible but typically gated off by the scanner.
- **Across a halt:** stale bars; the disqualifier fires regardless of score.
- **DST transition day (Mar 8 / Nov 1, 2026):** TOD multiplier must use `zoneinfo` correctly. Test these two dates explicitly.
- **Symbol with very low ATR%:** `momentum_atr` denominator is small → strengths can clip to 1.0 too easily. Ensure `atr_pct ≥ 2.0` gate prevents this.

---

## 10. Implementation hint for `score_symbol()`

```python
async def score_symbol(context: SignalContext,
                       weights: ScoringWeights = ScoringWeights(),
                       gates: GateThresholds = GateThresholds()) -> ScoreResult | None:

    # Step 2
    passed, _ = passes_gates(context, gates)
    if not passed:
        return None

    # Step 3
    signals = {
        "rvol_30m":           rvol_30m(context),
        "rvol_cumulative":    rvol_cumulative(context),
        "momentum_atr":       momentum_atr(context),
        "vwap_distance_atr":  vwap_distance_atr(context),
        "trend_stack_5m":     trend_stack_5m(context),
        "mtf_alignment":      mtf_alignment(context),
        "rsi_intraday":       rsi_intraday(context),
        "breakout_proximity": breakout_proximity(context),
        "clean_structure":    clean_structure(context),
    }

    # Step 4
    base_score = composite_score(signals, weights)

    # Step 5
    tod_mult    = tod_multiplier(context.now_ts)
    final_score = base_score * tod_mult

    # Step 6
    bias = bias_15m(context)

    # Step 7
    dq, flags = is_disqualified(context)
    if dq:
        return None

    # Step 8
    tier = None
    if final_score >= 85 and bias == "long":   tier = "A"
    elif final_score >= 75 and bias == "long": tier = "B"
    elif final_score >= 70:                    tier = "C"

    return ScoreResult(
        symbol      = context.symbol,
        timestamp   = context.now_ts,
        base_score  = base_score,
        tod_mult    = tod_mult,
        final_score = final_score,
        components  = signals,
        bias_15m    = bias,
        flags       = flags,
        tier        = tier,
    )
```

---

## 11. Summary

- **Weights sum to 1.00 exactly.** Validate with `assert abs(sum(weights) - 1.0) < 1e-9`. Earlier drafts had a typo summing to 0.90; this document is corrected.
- **One score per cycle, not per timeframe.** The cycle runs every 60s; each cycle uses bars from all 3 TFs.
- **1m is the foundation.** 5m and 15m are aggregated from 1m by the bar aggregator. If 1m is wrong, everything is wrong.
- **The 8-step flow is deterministic.** Same input → same output. Use the worked example in Section 6 to verify implementation correctness.
- **Edge cases matter.** Sessions don't start with full data; first 14 minutes have no RSI, first 30 have no `rvol_30m`. Don't crash; return 0 strength.
- **DST is a real bug source.** Test 2026-03-08 and 2026-11-01 explicitly.