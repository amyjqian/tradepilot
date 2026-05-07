# Delta Patch — PER_15_MINUTE_SCORING.md

**Context:** The engine's bar series scope was corrected. EMAs (1m, 5m, 15m at all periods) and RSI now use **rolling multi-day bars**; only VWAP and "today's" levels remain session-anchored. Without this fix, 15m EMA21 wouldn't be valid until ~14:45 ET and `bias_15m` would silently return "neutral" for most of the day.

This patch updates `PER_15_MINUTE_SCORING.md` to reflect the corrected behavior. Two edits, no number changes.

**Companion changes (already applied):**
- `CORE_ENGINE_SPEC.md` — Section 4 (Bar series scope), Section 8.1 (`bias_15m`)
- `VERIFICATION_MVP_SPEC.md` — Section 5 (BarAggregator API), Section 6 (build_context)
- `PER_MINUTE_SCORING.md`, `PER_2_MINUTE_SCORING.md`, `PER_5_MINUTE_SCORING.md` — no changes needed (their numbers were always consistent with the corrected interpretation)

---

## Edit 1 — Add a note at the top of the file

**Location:** immediately after the existing `**Companion to:**` line near the top.

**Insert:**

```markdown
> **Bar-scope correction (vs earlier drafts):** earlier versions implied 15m bars were session-only, which would force you to wait until 14:45 ET for EMA21 to be valid. The corrected engine uses **rolling multi-day 15m bars** for EMA computation (so bias is valid from the first 15m close of the session), while keeping VWAP session-anchored. See `CORE_ENGINE_SPEC.md` Section 4 ("Bar series scope") and Section 8.1.
```

---

## Edit 2 — Fix Section 9 (Edge cases)

**Location:** Section 9 currently begins with this incorrect bullet:

```markdown
- **First 15m bar of session (09:45:00 ET):** EMA21 needs 21 bars; first valid 15m EMA21 doesn't exist until ~14:45 ET. Until then, `bias_15m` returns "neutral" and most cycles produce no Tier A or B (Tier B requires `bias == "long"`).

  **Implication:** the 15m-cadence scanner produces meaningful tier outputs only after ~5 hours into the session (early afternoon). Use 5m cadence for the first half of the session.
```

**Replace with:**

```markdown
- **Cold start with no historical 15m bars:** if the data layer has never seen a symbol before (no backfill on disk yet), the rolling 15m series will have fewer than 21 bars and `bias_15m` returns "neutral" until backfill completes. This is a startup-only concern; once the first session of backfill is on disk, every subsequent session has full bias from 09:30.

  **Implication:** at first launch, run `scripts/backfill_universe.py` per `VERIFICATION_MVP_SPEC.md` Section 12 before starting the live scanner. Backfill takes seconds at the 1000/min Polygon rate.

- **First 15m bar of session (09:45:00 ET):** the in-progress 09:30-09:45 bar can be read live (close updates with each tick), so bias is technically computable from 09:30 onward using the rolling EMAs and the in-progress bar's running close. Most implementations wait for the first full 15m bar close at 09:45 for stability — that's the convention the daily routine uses anyway.
```

---

## Edit 3 — Update Section 10 (Summary)

**Location:** Section 10's last bullet currently reads:

```markdown
- **Use 15m cadence for swing-leaning intraday** holds (30+ min), or as a deep-filter alongside 5m. Don't use it as your primary scanner during the morning prime window.
```

**Add a new bullet immediately above it:**

```markdown
- **Bias is valid from session open** (09:30 ET) thanks to rolling multi-day 15m bars. No "wait until afternoon" warmup. The previous spec drafts incorrectly stated EMA21 needed 5 hours of session-only data.
```

---

## Verification after applying

After Claude Code applies this patch, the file should:

1. Have a "Bar-scope correction" callout near the top (same pattern as `CORE_ENGINE_SPEC.md`)
2. NOT contain the phrases `14:45 ET` or `5 hours into the session` anywhere
3. Section 9's first bullet should be about cold start / backfill, not about EMA warmup
4. Section 10 should mention bias is valid from session open

Confirm with:

```bash
grep -n "14:45\|5 hours into\|early afternoon" PER_15_MINUTE_SCORING.md
# Should return no matches.

grep -n "rolling multi-day\|bar-scope correction" PER_15_MINUTE_SCORING.md
# Should return matches in the top callout and Section 10.
```

---

## Why this is a delta patch instead of a regenerated file

The numerical worked example (NVDA at 11:00:00 ET → score 71.57, Tier C) is unchanged. The score numbers were always computed assuming valid EMAs, which only the rolling-bar interpretation supports. Only the meta-commentary about "when does this become valid?" was wrong.

A delta patch keeps the diff small and reviewable; you can see exactly what's being changed and why. Hand this file plus the existing `PER_15_MINUTE_SCORING.md` to Claude Code with the prompt:

> "Apply the three edits described in `PER_15_MINUTE_SCORING_PATCH.md` to `PER_15_MINUTE_SCORING.md`. After applying, run the two grep verification commands at the end of the patch file and confirm both pass. Do not change any numerical values in the worked example — only the prose edits described."
