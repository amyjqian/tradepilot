# Per-15-Minute Scoring — Worked Example

**Purpose:** Show how the 15-minute timeframe fits into the engine — both as the **regime / direction** layer used by every score, AND as an optional **slow-cadence scanner** for swing-leaning intraday holds.

**Companion to:** `CORE_ENGINE_SPEC.md`, `PER_MINUTE_SCORING.md`, `PER_5_MINUTE_SCORING.md`, `PER_2_MINUTE_SCORING.md`.

> **Bar-scope correction (vs earlier drafts):** earlier versions implied 15m bars were session-only, which would force you to wait until 14:45 ET for EMA21 to be valid. The corrected engine uses **rolling multi-day 15m bars** for EMA computation (so bias is valid from the first 15m close of the session), while keeping VWAP session-anchored. See `CORE_ENGINE_SPEC.md` Section 4 ("Bar series scope") and Section 8.1.

---

## 1. Two distinct roles for 15m

This is the most important thing to understand about 15m in this system:

| Role | What it does | Cadence | Where it lives |
|---|---|---|---|
| **A. Direction / Regime filter** | Determines `bias_15m` for every score; gates long vs short | Reads on every cycle (1m, 2m, 5m — all cadences) | Inside `score_symbol()` (Step 6) |
| **B. Slow-cadence scanner** | Score a symbol set only every 15 minutes | Every 15 min | A scanner instance with `refresh_interval_seconds() == 900` |

**Most users only need Role A.** It's already baked into the engine — every 5m or 1m cycle reads 15m bars when computing `bias_15m` and the 15m branch of `mtf_alignment`. You don't have to do anything special.

**Role B is for a specific use case:** swing-leaning intraday trades with hold times of 30 minutes to multiple hours. Refreshing the score every 15 minutes is enough; faster cadence is wasted work. Some traders run *only* a 15m-cadence scanner during slower trading sessions or after the morning prime window.

---

## 2. Same signals, same weights — different cadence

The math doesn't change. Same 9 signals, same weights summing to 1.00, same composite formula:

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

What changes for a 15m-cadence scanner: the runner sleeps for 900 seconds instead of 300.

---

## 3. Why 15m is mostly the "regime" layer, not a scanner cadence

Look at this signal-update table for a 15m-cadence cycle:

| Signal | Reads | Updates per 15m cycle? |
|---|---|---|
| `rvol_30m` | 1m bars | ✓ (last 30 1m bars; 15 brand new) |
| `rvol_cumulative` | 1m bars | ✓ (cumulative; 15 brand new) |
| `momentum_atr` | 1m + daily | ✓ |
| `vwap_distance_atr` | 5m bars | ✓ (3 new 5m bars typically) |
| `trend_stack_5m` | 5m bars | ✓ |
| `mtf_alignment` | 1m + 5m + 15m | ✓ (15m branch updates only at 15m boundaries — i.e., now) |
| `rsi_intraday` | 5m bars | ✓ |
| `breakout_proximity` | 1m bars | ✓ |
| `clean_structure` | 5m bars | ✓ |

**Every signal updates fully on 15m cycles.** That's because 15m is the slowest timeframe in the stack — by the time 15 minutes have passed, all 1m, 5m, AND 15m bars have produced new closes.

So a 15m-cadence scanner is the **most computationally efficient** scoring cycle. It just refreshes less often than you might want for active trading.

---

## 4. The role A use case — direction filter inside `score_symbol()`

Every cycle (whether 1m, 2m, 5m, or 15m cadence) calls `bias_15m(context)`:

```python
def bias_15m(context: SignalContext) -> Literal["long", "short", "neutral"]:
    closes_15m = [b.close for b in context.bars["15m"]]
    ema9 = ema(closes_15m, 9)[-1]
    ema21 = ema(closes_15m, 21)[-1]
    vwap_15m = vwap_session(context.bars["15m"], session_start)[-1]
    close_now = context.bars["15m"][-1].close

    if ema9 is None or ema21 is None:
        return "neutral"
    if ema9 > ema21 and close_now > vwap_15m:
        return "long"
    if ema9 < ema21 and close_now < vwap_15m:
        return "short"
    return "neutral"
```

This is the **regime filter.** Without it:
- A symbol with score = 87 but downward 15m trend would still get Tier A → bad trade
- The system would chase intraday strength against the larger structure

With it:
- Tier A requires `final_score >= 85 AND bias == "long"`
- Tier B requires `final_score >= 75 AND bias == "long"`
- A high score against a bearish 15m bias gets filtered to no tier (or only Tier C)

The 15m bars don't have to update fresh on every cycle for this to work — they update at 15m boundaries (every 15 min). Between boundaries, the in-progress 15m bar is read live (close updates with each tick), and the EMA9/EMA21 comparison still produces a stable signal because EMAs over 9 and 21 bars don't flip on partial-bar moves.

---

## 5. Worked example — NVDA at 11:00:00 ET, 2026-04-15

This is **15 minutes after the 1m worked example** at 10:42. At 11:00, a 15m bar just closed (the 10:45-11:00 bar). A 15m-cadence scanner would run now.

### 5.1 Step 1 — Build SignalContext at T = 11:00:00 ET

```
T            = 11:00:00 ET → 1744731600000 ms epoch
session_start = 09:30:00 ET → 1744726200000 ms epoch

bars["1m"]   = 90 bars     (09:30 → 10:59)
bars["5m"]   = 18 closed   (09:30 → 11:00) ← 3 new since 10:45
bars["15m"]  = 6 closed    (09:30 → 11:00) ← 1 new (10:45-11:00)
bars["daily"] = last 30 closed daily bars

today_high           = $125.40    # rose from $125.10
today_low            = $121.80
yesterday_high       = $124.50
prior_session_close  = $122.10
```

**What changed since 10:45:**
- 15 new 1m bars closed
- 3 new 5m bars closed (10:45-10:50, 10:50-10:55, 10:55-11:00)
- 1 new 15m bar closed (10:45-11:00)
- Today's high pushed to $125.40 (a new HoD)
- Price now $124.85 (up from $124.55 at 10:45)

### 5.2 Step 3 — Compute 9 signal strengths

#### `rvol_30m`

```python
recent_$vol  = $22.5M    # cooled off after the breakout move
historical_avg = $7.7M
ratio        = 22.5 / 7.7 = 2.922
strength     = clip((2.922 - 1.5) / 2.5, 0, 1) = 0.569
```

Volume cooled off as the move matured. Still strong but no longer 3.3×.

#### `rvol_cumulative`

```python
today_cum_$vol = $148M
typical_cum    = $58M
ratio          = 2.552
strength       = clip((2.552 - 1.0) / 2.0, 0, 1) = 0.776
```

#### `momentum_atr`

```python
today_close  = $124.85
pct_move     = (124.85 - 122.10) / 122.10 = 2.252%
atr_pct      = 2.10%
move_in_atr  = 2.252 / 2.10 = 1.072
strength     = clip(1.072 / 2.0, 0, 1) = 0.536
```

Move is now a full ATR. Stronger momentum reading.

#### `vwap_distance_atr`

```python
close       = $124.85
vwap_5m     = $124.10    # VWAP rose with the move
atr_5m      = $0.45
distance_atr = (124.85 - 124.10) / 0.45 = 1.667 ATR

# Triangular still in 0.5-2.0 decay zone:
strength = (2.0 - 1.667) / 1.5 = 0.222
```

Even further extended past the +0.5 ATR optimum. Signal continues to weaken.

#### `trend_stack_5m`

```python
ema9_5m    = $124.45
ema20_5m   = $123.95
ema50_5m   = $123.30
vwap_5m    = $124.10
close_5m   = $124.85

count = 4 (all four conditions still True)
strength = 1.000
```

#### `mtf_alignment` (15m branch FRESHLY updated)

```python
stack_1m  = 1.00   # 1m EMAs/VWAP all fresh
stack_5m  = 1.00   # 5m freshly closed
stack_15m = 1.00   # ← 15m bar just closed; ema9/21 firmly stacked

count = 3
strength = 1.000
```

#### `rsi_intraday`

```python
rsi_5m_value = 73.5    # rose with the move; still in 60-80 plateau
strength = 1.000
```

#### `breakout_proximity`

```python
target = min(today_high, yesterday_high)
       = min(125.40, 124.50)
       = $124.50

close = $124.85
# Still above target → already broken through
strength = 0   # signal stays at 0 once broken
```

#### `clean_structure`

```python
last_20_closes_5m: includes the recent run-up, mostly monotonic
flips = 3  (cleaner than at 10:45)
strength = max(0, 1 - 3/20) = 0.850
```

### 5.3 Step 4 — Composite

```python
raw =   0.15 × 0.569     # rvol_30m            = 0.0854
      + 0.10 × 0.776     # rvol_cumulative     = 0.0776
      + 0.15 × 0.536     # momentum_atr        = 0.0804
      + 0.10 × 0.222     # vwap_distance_atr   = 0.0222   ← weak (extended)
      + 0.10 × 1.000     # trend_stack_5m      = 0.1000
      + 0.10 × 1.000     # mtf_alignment       = 0.1000   ← all 3 TFs aligned
      + 0.10 × 1.000     # rsi_intraday        = 0.1000
      + 0.10 × 0.000     # breakout_proximity  = 0.0000   ← already broken
      + 0.10 × 0.850     # clean_structure     = 0.0850
                                              ─────────
                                                0.6506

base_score = 65.06
```

### 5.4 Step 5–8

```python
tod_mult     = 1.10  (10:30-11:30 window)
final_score  = 65.06 × 1.10 = 71.57

bias_15m  = "long"  (newly-closed 15m bar confirms uptrend)
disqualifiers: PASS

tier = "C"  (≥70, but bias=long doesn't help here since <75 needed for B)
```

### 5.5 Final ScoreResult

```python
ScoreResult(
    symbol = "NVDA",
    timestamp = 1744731600000,    # 11:00:00 ET
    base_score = 65.06,
    tod_mult = 1.10,
    final_score = 71.57,
    bias_15m = "long",
    flags = [],
    tier = "C",
)
```

---

## 6. Comparison across all cadences (same NVDA fixture)

| Timestamp | Cadence | base_score | final_score | tier |
|---|---|---|---|---|
| 10:42:00 ET | 1m | 77.46 | 85.21 | **A** |
| 10:44:00 ET | 2m | 78.73 | 86.60 | **A** |
| 10:45:00 ET | 5m | 67.07 | 73.78 | C |
| 11:00:00 ET | 15m | 65.06 | 71.57 | C |

**The story across the whole sequence:**

1. **10:42 (Tier A):** Optimal entry. Price approaching breakout, healthy VWAP extension, high RVol.
2. **10:44 (Tier A):** Slightly better — about to break.
3. **10:45 (Tier C):** Breakout happened. `breakout_proximity` collapsed; `vwap_distance_atr` decayed.
4. **11:00 (Tier C):** Move has matured. Volume cooled, price extended even further past VWAP. `rvol_30m` dropped from 0.722 to 0.569; `vwap_distance_atr` from 0.357 to 0.222. The trade is well past optimal entry now.

**This is the score doing exactly what it's designed to do:** scream "BUY!" before the move happens, then quiet down as the easy edge dissipates. Traders who entered at 10:42 are now sitting on +$0.55 (about +1.4R if their stop was below VWAP). Traders waiting for 11:00 are paying the full premium.

---

## 7. When to use a 15m-cadence scanner

**Yes, use 15m cadence when:**

- You're trading swing-style intraday holds (30 min to several hours)
- It's lunch period or a slow midday session and you don't want signal noise
- You're running multiple scanners and want the 15m one to be the "deep filter" — only act on names that show up consistently across multiple 15m cycles
- You're on a less-liquid universe where the 5m noise/signal ratio is poor

**No, stick with 5m or 1m when:**

- You're trading the morning prime window (9:45–11:00 ET) — too slow to catch the action
- Your hold time is under 15 minutes (the score might not even refresh during your trade)
- You want active alerts for breakouts or RVol surges

**The "deep filter" pattern** is genuinely useful: run a 5m scanner for active picking AND a 15m scanner alongside it. Names that appear in BOTH lists are the highest-conviction ideas. Names that only show up on 5m and not on 15m are momentum chases that the slower view doesn't validate. Names that show up on 15m but not on 5m might be waking up — worth watching.

---

## 8. Implementation hint

```python
class FifteenMinuteScanner(CompositeScanner):
    type_id = "watchlist_15m"
    type_name = "Watchlist (15m cadence)"

    def refresh_interval_seconds(self) -> int:
        return 900   # 15 minutes
```

Same as the 2m and 5m scanners — only the cadence changes. Engine, signals, weights all unchanged.

To align scoring exactly to 15m bar closes (so all signals are fully fresh):

```python
async def _sleep_until_next_15m_close(self):
    now_et = datetime.now(ET)
    minutes_past = now_et.minute % 15
    if minutes_past == 0 and now_et.second < 5:
        return
    sleep_seconds = (15 - minutes_past) * 60 - now_et.second
    await asyncio.sleep(sleep_seconds)
```

---

## 9. Edge cases for 15m cadence

- **Cold start with no historical 15m bars:** if the data layer has never seen a symbol before (no backfill on disk yet), the rolling 15m series will have fewer than 21 bars and `bias_15m` returns "neutral" until backfill completes. This is a startup-only concern; once the first session of backfill is on disk, every subsequent session has full bias from 09:30.

  **Implication:** at first launch, run `scripts/backfill_universe.py` per `VERIFICATION_MVP_SPEC.md` Section 12 before starting the live scanner. Backfill takes seconds at the 1000/min Polygon rate.

- **First 15m bar of session (09:45:00 ET):** the in-progress 09:30-09:45 bar can be read live (close updates with each tick), so bias is technically computable from 09:30 onward using the rolling EMAs and the in-progress bar's running close. Most implementations wait for the first full 15m bar close at 09:45 for stability — that's the convention the daily routine uses anyway.

- **Lunch chop (11:30–14:00 ET):** TOD multiplier is 0.85. Even a base_score of 80 → final 68 → no Tier B. The score is intentionally hard to clear during lunch hours.

- **15m boundary aligned with session start:** all 15m bars start on `:00`, `:15`, `:30`, `:45`. The first complete bar of the session is 09:30-09:45.

- **Late-session 15m bar (15:45-16:00):** the day's last 15m bar. Most 15m-cadence scanners stop running here since the daily routine is in flatten mode.

---

## 10. Summary

- **15m has two roles:** the regime/direction filter inside every score (used by all cadences), AND an optional slow-cadence scanner.
- **Math is unchanged:** same 9 signals, same weights summing to 1.00, same engine.
- **A 15m-cadence cycle refreshes everything fully** because all 1m, 5m, and 15m bars have produced new closes within 15 min.
- **The 15m bias filter** is what separates A/B tiers from C — high scores against the 15m trend never reach Tier A.
- **Score evolution shows expected behavior:** Tier A at optimal entry (10:42-10:44), drops to C as the move matures (10:45, 11:00). The system rewards anticipation, not chasing.
- **Bias is valid from session open** (09:30 ET) thanks to rolling multi-day 15m bars. No "wait until afternoon" warmup. The previous spec drafts incorrectly stated EMA21 needed 5 hours of session-only data.
- **Use 15m cadence for swing-leaning intraday** holds (30+ min), or as a deep-filter alongside 5m. Don't use it as your primary scanner during the morning prime window.
