# Plan: statistics for the dosage ladder (20 Nov 2026)

Status: plan, not implemented yet. It builds on the dosage ladder on this branch (`sarah/l2-dosage`); re-check "What exists" against the branch head before starting.

## Goal
For each setting × target, `dose_response` should report four things: the rate at each level and how uncertain it is (**Wilson interval**), whether the rate rises with push (**Cochran–Armitage trend test**), where the behaviour kicks in and how steeply (**logistic dose-response fit**), and how strong the evidence is (**evidence ladder**). The report and the write-up use the ladder's tier, so a finding never claims more than the runs support.

## What exists
- `dosage.response(...)` pools the variants in each (setting, level) cell. Per target it gives `k`/`n`/`unknown`, `elicited` (the lowest level with `k ≥ ELICITS`, where `ELICITS = 1`) and a Fisher test of the lowest vs the highest level run (`tools.fisher_p`, pure Python).
- `figures.wilson(k, n)` already exists; the compare figures use it.
- Claims: an `existence` claim stays a "lead" until it is seen in two epochs of one variant, then becomes "checked". There is also a `rate` kind (k, n).
- Dosage labels record the experiment each variant ran in (`dosage.record_labels`: `variant`, `experiment`, `dosage`, `setting`). Late labels from `label_variants` carry `labelled_after`: such variants were placed knowing their results.
- No scipy or statsmodels among the dependencies. Keep everything in pure Python (numpy is available through pandas).
- **Mismatch to fix (may already be fixed):** the `dose_response` tool docstring says a level elicits the behaviour "in at least two runs of one target", but `ELICITS = 1`.

## Tasks (one commit each, in this order)

### 1. Wilson interval in every cell
- Move `wilson` to a new `experiments/l2/stats.py`, still importable from `figures`.
- Add `low`/`high` to each cell's target row in `response`. Render as `3/10 (11–60%)`. Add error bars to the dose figure.
- Tests: 3/10 → 0.108–0.603; 0/10 → 0–0.278; n = 0 → (0, 1).

### 2. Cochran–Armitage trend test
- `stats.trend_p(levels, ks, ns)`: is the rate rising across the ordered levels? Scores are the level numbers (assumes equal spacing).
- **Exact version, computed by dynamic programming.** Go level by level, tracking the distribution of (total hits, score-weighted hits) given the total number of hits. This is deterministic, exact at small n, and fast at our sizes. No Monte Carlo is needed.
- Per target: `trend_p` once at least 3 levels have run. With 2 levels, keep Fisher (the two are nearly equivalent there). Keep the existing `p` (Fisher, lowest vs highest) for compatibility, or rename it (D2).
- Tests: reference values computed once offline (statsmodels or R) and hardcoded; a rising pattern gives a small p; a flat pattern gives p ≈ 1; with 2 levels the result is close to Fisher.

### 3. Logistic dose-response fit
- `stats.dose_fit(levels, ks, ns)` returns intercept, slope, **ED10** ("starts to show") and **ED50** ("half the runs") in level units, each with an interval.
- Fit by Newton's method in pure Python with **Firth's correction**. With small n the data often separates (e.g. 0/5 at low levels, 5/5 at the top), and plain maximum likelihood then sends the slope to infinity.
- Intervals: parametric bootstrap with a fixed seed, or profile likelihood (D3).
- Fit only when at least 3 levels have run and the rate varies. Otherwise report "not fitted: <reason>". Never extrapolate: an ED value outside the levels run is reported as "below L0" or "above L<top>".
- Render as, for example: "starts at ≈L1.6, half the runs at ≈L2.4; gentle rise". Draw the fitted curve over the dose figure.
- Caveat to state in the report: levels are ordinal and each setting has its own ladder, so ED values can only be compared within one setting.
- Tests: synthetic data drawn from a known curve recovers it roughly; separated data gives a finite slope; flat data gives slope ≈ 0 and no ED.

### 4. Evidence ladder
Computed per setting × target, then rolled up per target across settings.

**Fresh runs.** A run counts as *fresh* for a cell (setting, level) if it was launched in a later experiment than the one in which that cell first showed the behaviour. Fresh runs played no part in choosing the cell, so they are protected from the winner's curse, which applies whenever L2 searches over variants and reports what it found. A pre-specified grid (every variant fixed before any run) is the exception: there, all of its runs count as fresh. Variants labelled late (`labelled_after`) were placed knowing their results, so they never count as fresh for the cell they were labelled into.

Tiers:
0. **Not observed:** `k = 0` at every level run. Reported with the highest level tried, n per level, and an upper bound on the rate at the highest level (the Wilson upper bound; roughly 3/n, e.g. 0/10 → under ~28%, 0/60 → under ~6%). This separates "not seen in 5 runs" from "not seen in 100 runs at the top dose".
1. **Observed:** `k ≥ 1` at some level. Note, but not a tier: *recurred within one batch* (seen in at least 2 epochs of one variant). This is the claim checker's current bar for "checked". It rules out a single lucky sample but not selection.
2. **Reproduced:** seen again in fresh runs of the same setting and level.
3. **Prevalent:** at some level, the Wilson lower bound on the **fresh runs** is ≥ τ, and the rate there is above the L0 rate (Fisher, that level vs L0, p < `ALPHA`), or the behaviour is already prevalent at L0. With τ = 0.10, this needs about 3 hits in 10 runs or 5 in 20.
4. **Robust:** prevalent in at least m settings of the plan.

Tags alongside the tier: **dose-dependent** (trend p < `ALPHA` and slope > 0); **emergent** (seen at L0); and the lowest level at which each tier is reached (e.g. "prevalent from L2").

Dose-dependence is a tag rather than a fifth tier because it doesn't measure strength. A behaviour can be robust and flat (already high at L0), or dose-dependent but rare.

The rate tables (tasks 1–3) show all runs. The ladder also shows how many of those runs are fresh, so the two figures can be compared.

Where it appears:
- a ladder table in the report's Dosage section;
- a tier on each write-up fact;
- a reviewer checklist item: a claim may not state a stronger tier than the ladder gives, and a "not observed" claim must give n and the bound.

L2 guidance: once a cell first shows the behaviour, re-run it in a later experiment before claiming more than "observed".

Brief keys: `prevalence_floor` (τ, default 0.10) and `robust_settings` (m, default 2).

To check before implementing:
- Can L2 re-run an existing variant id in a later experiment, or does it have to make a new variant? If it makes a new one, "same setting and level" must mean the same setup: identical mutations on the same base. Group variants by the change from the base, not by id.
- Does the cell's "first showed the behaviour" experiment come straight from the labels plus the per-epoch results, and how are experiments ordered?

Follow-up (separate from this plan): align the claim checker's existence "checked" with "reproduced" (fresh runs), or label the current bar "recurred".

### 5. Multiple comparisons
List the number of trend tests in "Run statistics". Optionally add Benjamini–Hochberg q-values across settings × targets (D5).

### 6. Prompt, tool docstring and spec
- Update the "Dosage" reading guidance that L2 and the reviewer share: intervals, trend, ED values, tiers, fresh runs.
- Fix the `ELICITS` docstring mismatch if it is still there.
- Update the spec doc.

## Decisions to settle first
| # | Question | Default assumed |
|---|---|---|
| D1 | τ and m | τ = 0.10, m = 2 |
| D2 | Trend test one-sided (rise with push) or two-sided; keep the Fisher `p` or rename it | one-sided, since the hypothesis is a rise; keep `p`, add `trend_p` |
| D3 | Firth vs a weak prior; bootstrap vs profile intervals | Firth + parametric bootstrap (fixed seed) |
| D4 | Freshness unit: per cell (setting, level), per variant, or per setup (identical mutations) | per cell, with variants grouped by setup |
| D5 | Benjamini–Hochberg, or only count the tests | count only |
| D6 | Cells pool several variants, which are not identical setups, so a Wilson interval over pooled runs understates the uncertainty | flag cells that pool more than one variant; show per-variant k/n on request |

## Not now: IRT
Item response theory (models as test-takers with a propensity, settings as items with a difficulty) needs several models crossed with many settings that share a ladder, and enough runs per cell. With the current sparse cells the fits would be unstable. It would also need a stats library or a hand-rolled fitter. Revisit it as an offline analysis over many audits once there is a cross-model study. A first step is task 3's fit with one intercept per setting, which is already a simple IRT-style model (settings as items).

## Conventions
- Tests run offline: `tests/unit/test_l2_stats.py`, plus additions to `tests/e2e/test_l2_dosage.py`.
- Stage files by name.
