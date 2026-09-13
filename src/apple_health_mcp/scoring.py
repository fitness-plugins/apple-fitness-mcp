"""Pure recovery/readiness scoring math — numbers in, numbers out, no DB.

This is the reference implementation that is ported 1:1 to Swift
(`ios/ScoreEngine/Sources/ScoreEngine/ScoreEngine.swift` in the sibling
`apple-fitness-ios` repo), so every function here is deliberately
dependency-free (only `math`) and side-effect-free. The DB plumbing that feeds
these functions lives in `server.py`.

Design principles (see RECOVERY_READINESS_PLAN.md):

- **Personal baseline, not absolutes.** Apple HRV is *SDNN*, not rMSSD, so no
  population thresholds are hard-coded. Every physiological metric is scored by
  its deviation (z-score) from the user's own rolling baseline, where 50 == on
  baseline (temperature is the exception, see `temp_score`).
- **Graceful degradation.** A missing metric yields a `None` sub-score; the
  weighted mean in `recovery_score` renormalizes over whatever is present.

**Config over magic numbers.** Every tunable lives on `ScoringConfig` with the
historical values as defaults; `DEFAULT_CONFIG` is the singleton the whole
pipeline uses. Functions take an optional `cfg` so new behaviours are
adjustable and old paths stay A/B-runnable against historical data. The Swift
`ScoringConfig` struct mirrors this field-for-field.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional


# --- configuration ------------------------------------------------------------

def _default_recovery_weights() -> dict:
    # Recovery mix (WHOOP-leaning start; calibrate on real data). Keys are the
    # sub-score names recovery_score consumes; weights renormalize over present.
    # `hrv_cv` (Phase 3.1) is a low-weight variability-collapse signal.
    return {"hrv": 0.40, "sleeping_hr": 0.20, "sleep": 0.25, "resp": 0.10,
            "temp": 0.05, "hrv_cv": 0.05}


def _default_sleep_subweights() -> dict:
    # deep_rem down-weighted (Phase 3.3): Apple Watch deep/REM staging is noisy vs
    # PSG, so duration (the most reliable component) carries the freed weight.
    return {"duration": 0.60, "deep_rem": 0.15, "awakenings": 0.15,
            "regularity": 0.10}


@dataclass(frozen=True)
class ScoringConfig:
    """All scoring-math tunables in one place (mirrors Swift `ScoringConfig`).

    The historical hard-coded values are the defaults, so `ScoringConfig()` is a
    behavioural no-op. Feature flags default to the *new* behaviour but keep the
    old path selectable for A/B comparison against historical data.
    """

    # Z-score sensitivity: points of sub-score per SD from baseline. At k=18 a
    # metric one SD "better" than baseline scores 68, two SD scores 86.
    k: float = 18.0
    # HRV gets its own sensitivity because its z is computed in log space
    # (Phase 1.1), where the natural spread differs from the raw scale. Fitted on
    # 608 real scored days so a typical good day (raw z~1, old sub-score 68)
    # reproduces ~68 in log space: median log-space z on such days ~1.10, and
    # 50 + 16.6*1.10 ~= 68.3. (Whole-history regression-through-origin gives
    # 18.27, which preserves average sensitivity but slightly over-scores the
    # right tail; see CHANGES.md.)
    k_hrv: float = 16.6

    # Recovery mix; weights renormalize over whichever sub-scores are present.
    recovery_weights: dict = field(default_factory=_default_recovery_weights)

    # Band thresholds (WHOOP 67/34): green >= 67, yellow 34..66, red < 34.
    band_green: float = 67.0
    band_yellow: float = 34.0

    # Temperature (Phase 3.2). Elevated wrist temp (immune response / luteal
    # phase) is the meaningful signal, so warm deviations are penalized more than
    # cool ones. `cycle_aware` + a known luteal phase shifts the temp baseline by
    # `cycle_temp_z_shift` SD so the predictable luteal rise isn't read as illness
    # (inert until cycle phase is plumbed upstream).
    temp_asymmetric: bool = True
    temp_warm_weight: float = 1.0            # penalty weight for z > 0 (warmer)
    temp_cool_weight: float = 0.5            # penalty weight for z < 0 (cooler)
    cycle_aware: bool = True
    cycle_temp_z_shift: float = 1.0

    # Sleep sub-score constants.
    sleep_need_default_h: float = 8.0        # fallback nightly sleep need (hours)
    sleep_target_deep_rem: float = 0.45      # deep+REM fraction that scores 100
    sleep_awakening_penalty: float = 8.0     # sub-score points lost per awakening
    sleep_regularity_zero_min: float = 90.0  # bedtime SD (min) where regularity=0
    sleep_subweights: dict = field(default_factory=_default_sleep_subweights)
    # Duration curve (Phase 3.3): peaks at `need`, declines both sides; under-sleep
    # is penalized more steeply than over-sleep (oversleep can signal illness /
    # under-recovery, so it no longer maxes out). Off -> legacy monotonic ramp.
    sleep_duration_curve: bool = True
    sleep_under_penalty_per_h: float = 20.0
    sleep_over_penalty_per_h: float = 10.0
    # Personalized need (Phase 3.3): clamp(median of baseline sleep hours, min,
    # max); fall back to `sleep_need_default_h` until `sleep_need_min_samples`.
    sleep_need_min: float = 6.5
    sleep_need_max: float = 9.0
    sleep_need_min_samples: int = 5

    # Readiness load-penalty constants.
    acwr_sweet_hi: float = 1.3               # no penalty at/below this ACWR
    acwr_elevated: float = 1.5               # penalty ramps steeply above this
    acwr_ramp_per_unit: float = 30.0         # slope between sweet-hi and elevated
    acwr_steep_per_unit: float = 40.0        # slope above elevated
    recovery_time_penalty_per_h: float = 0.6  # readiness pts lost per rec.-hour
    penalty_cap: float = 40.0                # max total load penalty

    # Baseline windowing (shared with server.py orchestration).
    baseline_window_days: int = 28           # max trailing lookback (excl. today)
    min_baseline_n: int = 10                 # samples before a z is "trusted"
    min_history_days: int = 14               # days before state leaves 'calibrating'

    # Baseline estimator (Phase 2.1/2.2). 'robust' = median + 1.4826*MAD (default,
    # outlier-resistant); 'classic' = mean + sample sd. EWMA recency-weights the
    # window by `baseline_half_life_days` so the baseline tracks sustained shifts
    # without over-reacting to one-off nights. Winsorizing clips inputs at
    # ±winsor_k robust-SD before estimating (a travel night can't distort scale).
    # Continuous confidence (Phase 4.1): each metric's blend weight is scaled by
    # its baseline maturity min(1, n/min_baseline_n); the aggregate confidence
    # drives a ± CI band and the 'calibrating' state (below the OK threshold).
    confidence_weighting: bool = True
    confidence_ci_max: float = 15.0          # ± points at zero confidence
    confidence_ok_threshold: float = 0.8     # aggregate below this -> calibrating

    baseline_estimator: str = "robust"       # 'robust' | 'classic'
    baseline_ewma: bool = True
    baseline_half_life_days: float = 14.0
    winsorize_enabled: bool = True
    winsor_k: float = 3.0

    # HRV input smoothing (Phase 2.1): compare a rolling average of the last
    # `hrv_smoothing_days` (log) HRV values against the baseline, not one noisy
    # night. 1 disables smoothing.
    hrv_smoothing_days: int = 7

    # Smallest-worthwhile-change (Phase 2.3). A day is 'meaningful' when it moves
    # >= swc_multiplier * scale (~0.5 SD) from baseline, 'significant' at >= 1 SD.
    swc_multiplier: float = 0.5
    hrv_band_from_sd: bool = False           # HRV band edges from ±1 SD (opt-in)

    # HRV variability collapse (Phase 3.1): score the CV of the recent log-HRV
    # window against its own baseline; a collapsing CV (below baseline) is an
    # early overreaching signal. Low-weight contributor 'hrv_cv' (weight lives in
    # recovery_weights). Disable to drop it from the mix.
    hrv_cv_enabled: bool = True
    # Optional top-end taper (Phase 3.1): above hrv_taper_z SD, HRV sub-score
    # gains only hrv_taper_factor of its usual slope (very high HRV can mean
    # parasympathetic saturation, not better recovery). Off by default.
    hrv_taper_enabled: bool = False
    hrv_taper_z: float = 2.0
    hrv_taper_factor: float = 0.5

    # ACWR windowing (Phase 1.2). `acwr_uncoupled` drops the acute days out of
    # the chronic window so acute and chronic are not spuriously correlated.
    acwr_acute_days: int = 7
    acwr_chronic_days: int = 28
    acwr_uncoupled: bool = True

    # HRV log transform (Phase 1.1). SDNN is right-skewed/multiplicative; scoring
    # its z in log space makes the sub-score symmetric. False = legacy raw scale.
    hrv_log_transform: bool = True

    # Illness / strain co-elevation detector (Phase 1.3).
    illness_detector_enabled: bool = True
    illness_z_threshold: float = 1.0         # per-signal exceedance threshold (SD)
    illness_trigger: int = 2                 # signals needed to fire the gate
    illness_readiness_cap: float = 66.0      # cap readiness here (top of yellow)


DEFAULT_CONFIG = ScoringConfig()

# Backward-compatible module aliases (read-only mirrors of the defaults). The
# canonical source is DEFAULT_CONFIG; these exist so older references keep
# working and so the defaults are greppable by their historical names.
K = DEFAULT_CONFIG.k
RECOVERY_WEIGHTS = DEFAULT_CONFIG.recovery_weights
BAND_GREEN = DEFAULT_CONFIG.band_green
BAND_YELLOW = DEFAULT_CONFIG.band_yellow
SLEEP_NEED_DEFAULT_H = DEFAULT_CONFIG.sleep_need_default_h
SLEEP_TARGET_DEEP_REM = DEFAULT_CONFIG.sleep_target_deep_rem
SLEEP_AWAKENING_PENALTY = DEFAULT_CONFIG.sleep_awakening_penalty
SLEEP_REGULARITY_ZERO_MIN = DEFAULT_CONFIG.sleep_regularity_zero_min
SLEEP_SUBWEIGHTS = DEFAULT_CONFIG.sleep_subweights
ACWR_SWEET_HI = DEFAULT_CONFIG.acwr_sweet_hi
ACWR_ELEVATED = DEFAULT_CONFIG.acwr_elevated
ACWR_RAMP_PER_UNIT = DEFAULT_CONFIG.acwr_ramp_per_unit
ACWR_STEEP_PER_UNIT = DEFAULT_CONFIG.acwr_steep_per_unit
RECOVERY_TIME_PENALTY_PER_H = DEFAULT_CONFIG.recovery_time_penalty_per_h
PENALTY_CAP = DEFAULT_CONFIG.penalty_cap


def _clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


# --- baseline & z-score -------------------------------------------------------

def baseline(values: list[float]) -> tuple[Optional[float], float]:
    """Mean and sample standard deviation of a trailing baseline window.

    Returns `(mean, sd)`. `None`s are ignored. Empty input -> `(None, 0.0)`; a
    single value -> `(value, 0.0)` (no spread). Uses the sample SD (n-1) so a
    small window isn't over-confident.
    """
    vals = [v for v in values if v is not None]
    n = len(vals)
    if n == 0:
        return (None, 0.0)
    mean = sum(vals) / n
    if n < 2:
        return (mean, 0.0)
    var = sum((v - mean) ** 2 for v in vals) / (n - 1)
    return (mean, math.sqrt(var))


def zscore(today: Optional[float], mean: Optional[float],
           sd: Optional[float]) -> Optional[float]:
    """Standardized deviation of today's value from baseline.

    `None` when there's no today value, no baseline mean, or a degenerate spread
    (sd is None or 0) — in which case the metric can't be scored and is omitted.
    """
    if today is None or mean is None or not sd:
        return None
    return (today - mean) / sd


def _median(xs: list[float]) -> Optional[float]:
    s = sorted(xs)
    n = len(s)
    if n == 0:
        return None
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2.0


def weighted_quantile(values: list[float], weights: list[float],
                      q: float) -> Optional[float]:
    """Interpolating weighted quantile at `q` in [0,1].

    Uses the median-unbiased plotting positions `(cumsum - 0.5*w) / W` and linear
    interpolation between them, clamped to the endpoints. Interpolating (rather
    than a lower/nearest weighted median) matters: on a perfectly bimodal window
    the center lands *between* the two values, so the MAD stays non-zero instead
    of collapsing to 0. Deterministic, so the Python/Swift ports agree exactly.
    """
    pairs = sorted(zip(values, weights), key=lambda p: p[0])
    vs = [v for v, _ in pairs]
    ws = [w for _, w in pairs]
    W = sum(ws)
    if W <= 0 or not vs:
        return None
    if len(vs) == 1:
        return vs[0]
    pos, cum = [], 0.0
    for w in ws:
        cum += w
        pos.append((cum - 0.5 * w) / W)
    if q <= pos[0]:
        return vs[0]
    if q >= pos[-1]:
        return vs[-1]
    for i in range(1, len(pos)):
        if q <= pos[i]:
            span = pos[i] - pos[i - 1]
            t = (q - pos[i - 1]) / span if span > 0 else 0.0
            return vs[i - 1] + t * (vs[i] - vs[i - 1])
    return vs[-1]


def baseline_stats(values: list[float],
                   cfg: ScoringConfig = DEFAULT_CONFIG,
                   ) -> tuple[Optional[float], float, int]:
    """Baseline center/scale over a trailing window (Phase 2.1/2.2).

    `values` are oldest-first, most-recent-last (recency matters for EWMA);
    `None`s are ignored. Returns `(center, scale, n)`:

    - `baseline_ewma` recency-weights the window by `baseline_half_life_days`
      (weight halves every half-life going back), so the baseline tracks a
      sustained shift faster than a flat 28-day mean without chasing one outlier.
    - `winsorize_enabled` first clips values to `median ± winsor_k*1.4826*MAD` so
      a single artifact can't inflate the scale or drag the center.
    - `baseline_estimator='robust'` -> center = (weighted) median, scale =
      1.4826*(weighted) MAD; `'classic'` -> weighted mean, weighted sample sd.

    With `classic` + no EWMA + no winsorize this reproduces `baseline()` exactly
    (mean, sample SD), so the legacy path stays selectable for A/B.
    """
    vals = [v for v in values if v is not None]
    n = len(vals)
    if n == 0:
        return (None, 0.0, 0)
    if cfg.baseline_ewma:
        lam = 0.5 ** (1.0 / cfg.baseline_half_life_days)
        weights = [lam ** ((n - 1) - i) for i in range(n)]
    else:
        weights = [1.0] * n
    work = vals
    if cfg.winsorize_enabled and n >= 2:
        m0 = _median(vals) or 0.0
        mad0 = _median([abs(v - m0) for v in vals]) or 0.0
        spread = 1.4826 * mad0
        if spread > 0:
            lo, hi = m0 - cfg.winsor_k * spread, m0 + cfg.winsor_k * spread
            work = [min(hi, max(lo, v)) for v in vals]
    if cfg.baseline_estimator == "robust":
        center = weighted_quantile(work, weights, 0.5)
        if center is None:
            return (None, 0.0, n)
        mad = weighted_quantile([abs(v - center) for v in work], weights, 0.5)
        scale = 1.4826 * mad if mad is not None else 0.0
    else:
        W = sum(weights)
        center = sum(w * v for w, v in zip(weights, work)) / W
        if n < 2:
            scale = 0.0
        else:
            W2 = sum(w * w for w in weights)
            denom = W - W2 / W
            var = (sum(w * (v - center) ** 2 for w, v in zip(weights, work)) / denom
                   if denom > 0 else 0.0)
            scale = math.sqrt(var) if var > 0 else 0.0
    return (center, scale, n)


def significance(z: Optional[float], scale: Optional[float],
                 cfg: ScoringConfig = DEFAULT_CONFIG) -> tuple[Optional[float], bool, bool]:
    """Smallest-worthwhile-change readout (Phase 2.3): `(swc, meaningful,
    significant)`. `swc = swc_multiplier*scale` (~0.5 SD); `meaningful` when the
    day moved >= swc (|z| >= swc_multiplier); `significant` at >= 1 SD (|z| >= 1).
    `(None, False, False)` when z or scale is missing/zero."""
    if z is None or not scale:
        return (None, False, False)
    return (cfg.swc_multiplier * scale, abs(z) >= cfg.swc_multiplier, abs(z) >= 1.0)


def hrv_baseline_z(today: Optional[float], base_values: list[float],
                   cfg: ScoringConfig = DEFAULT_CONFIG,
                   recent_values: Optional[list[float]] = None,
                   ) -> tuple[Optional[float], float, Optional[float]]:
    """Baseline `(center, scale, z)` for HRV, in log space when
    `cfg.hrv_log_transform` (Phase 1.1), over the robust/EWMA baseline (Phase 2).

    SDNN is right-skewed and multiplicative, so a raw-scale z gives asymmetric
    sensitivity; `ln` first makes a drop and a rise of the same *ratio* symmetric.
    Values `<= 0` are dropped (ln undefined). When `recent_values` is given (the
    last `hrv_smoothing_days` nights incl. today, oldest-first) and smoothing is
    enabled, today's input is their rolling (log) average rather than one noisy
    night (Phase 2.1). Returned `center`/`scale` are in the transformed space
    (kept only for reporting; the sub-score consumes `z`).
    """
    def tx(v: Optional[float]) -> Optional[float]:
        if v is None:
            return None
        if cfg.hrv_log_transform:
            return math.log(v) if v > 0 else None
        return v

    bvals = [x for x in (tx(v) for v in base_values) if x is not None]
    center, scale, _ = baseline_stats(bvals, cfg)
    if cfg.hrv_smoothing_days and cfg.hrv_smoothing_days > 1 and recent_values:
        rv = [x for x in (tx(v) for v in recent_values) if x is not None]
        t = sum(rv) / len(rv) if rv else None
    else:
        t = tx(today)
    return center, scale, zscore(t, center, scale)


# --- physiological sub-scores (each 0..100) -----------------------------------

def hrv_score(z: Optional[float],
              cfg: ScoringConfig = DEFAULT_CONFIG) -> Optional[float]:
    """Higher HRV than baseline is better. 50 == on baseline. Uses `k_hrv`
    (the z is computed in log space, see `hrv_baseline_z`). With `hrv_taper_enabled`
    (Phase 3.1) the slope above `hrv_taper_z` SD is softened to `hrv_taper_factor`
    so unbounded HRV rises don't keep adding points (parasympathetic saturation)."""
    if z is None:
        return None
    if cfg.hrv_taper_enabled and z > cfg.hrv_taper_z:
        base = 50.0 + cfg.k_hrv * cfg.hrv_taper_z
        return _clamp(base + cfg.k_hrv * (z - cfg.hrv_taper_z) * cfg.hrv_taper_factor)
    return _clamp(50.0 + cfg.k_hrv * z)


def cv(values: list[float]) -> Optional[float]:
    """Coefficient of variation (sample SD / |mean|) of a small window. `None`
    when < 2 values or the mean is 0."""
    vals = [v for v in values if v is not None]
    if len(vals) < 2:
        return None
    mean, sd = baseline(vals)
    return sd / abs(mean) if mean else None


def rolling_cvs(values: list[float], window: int) -> list[float]:
    """CV of every length-`window` sliding block over `values` (oldest-first).
    Used to build the baseline distribution of HRV variability."""
    out = []
    for i in range(window - 1, len(values)):
        c = cv(values[i - window + 1: i + 1])
        if c is not None:
            out.append(c)
    return out


def hrv_cv_score(cv_today: Optional[float], baseline_cvs: list[float],
                 cfg: ScoringConfig = DEFAULT_CONFIG) -> Optional[float]:
    """HRV variability-collapse sub-score (Phase 3.1). z-scores today's CV against
    the baseline distribution of CVs; a CV below baseline (collapsing day-to-day
    variability, an early overreaching sign) scores < 50, higher scores > 50.
    `None` when today's CV or the baseline scale is unavailable."""
    if cv_today is None:
        return None
    center, scale, _ = baseline_stats(baseline_cvs, cfg)
    z = zscore(cv_today, center, scale)
    return None if z is None else _clamp(50.0 + cfg.k * z)


def rhr_score(z: Optional[float],
              cfg: ScoringConfig = DEFAULT_CONFIG) -> Optional[float]:
    """Higher resting/sleeping HR than baseline is worse. 50 == on baseline. (The
    sign convention here also serves the `sleeping_hr` sub-score.)"""
    return None if z is None else _clamp(50.0 - cfg.k * z)


def resp_score(z: Optional[float],
               cfg: ScoringConfig = DEFAULT_CONFIG) -> Optional[float]:
    """Higher respiratory rate than baseline is worse (illness/stress)."""
    return None if z is None else _clamp(50.0 - cfg.k * z)


def temp_score(z: Optional[float], cfg: ScoringConfig = DEFAULT_CONFIG,
               luteal: bool = False) -> Optional[float]:
    """Wrist temperature sub-score, peaking at 100 on baseline (z=0). Warm and
    cool deviations were penalized equally; now (Phase 3.2) warm is penalized more
    (`temp_warm_weight` > `temp_cool_weight`) because an elevated temp is the
    meaningful signal (immune response / luteal phase). With `cycle_aware` and a
    known `luteal` phase the z is shifted down by `cycle_temp_z_shift` SD so the
    predictable luteal temp rise isn't scored as illness. `temp_asymmetric=False`
    restores the old symmetric penalty."""
    if z is None:
        return None
    if cfg.cycle_aware and luteal:
        z = z - cfg.cycle_temp_z_shift
    a, b = (cfg.temp_warm_weight, cfg.temp_cool_weight) if cfg.temp_asymmetric \
        else (1.0, 1.0)
    penalty = cfg.k * 2.0 * (a * max(z, 0.0) + b * max(-z, 0.0))
    return _clamp(100.0 - penalty)


def _duration_score(hours: float, need: float,
                    cfg: ScoringConfig = DEFAULT_CONFIG) -> float:
    """Sleep-duration component. With `sleep_duration_curve` it peaks at `need`
    and declines on both sides — under-sleep steeply (`sleep_under_penalty_per_h`),
    over-sleep gently (`sleep_over_penalty_per_h`) so a big oversleep no longer
    maxes out. Otherwise the legacy monotonic ramp `100*hours/need`."""
    if not cfg.sleep_duration_curve:
        return _clamp(100.0 * hours / need)
    if hours <= need:
        return _clamp(100.0 - cfg.sleep_under_penalty_per_h * (need - hours))
    return _clamp(100.0 - cfg.sleep_over_penalty_per_h * (hours - need))


def personalized_sleep_need(baseline_hours: list[float],
                            cfg: ScoringConfig = DEFAULT_CONFIG) -> float:
    """Nightly sleep need personalized to the user (Phase 3.3): the median of the
    baseline sleep-hours, clamped to `[sleep_need_min, sleep_need_max]`. Falls
    back to `sleep_need_default_h` until at least `sleep_need_min_samples` nights
    exist (baseline still immature)."""
    vals = [v for v in baseline_hours if v is not None]
    med = _median(vals)
    if len(vals) < cfg.sleep_need_min_samples or med is None:
        return cfg.sleep_need_default_h
    return max(cfg.sleep_need_min, min(cfg.sleep_need_max, med))


def sleep_score(hours: Optional[float], need: Optional[float] = None,
                deep_rem_frac: Optional[float] = None,
                awakenings: Optional[float] = None,
                regularity: Optional[float] = None,
                cfg: ScoringConfig = DEFAULT_CONFIG) -> Optional[float]:
    """Composite sleep quality 0..100 from up to four present components.

    - `hours` vs `need` (default 8h): duration met, capped at 100.
    - `deep_rem_frac`: deep+REM as a fraction of time asleep vs a 0.45 target.
    - `awakenings`: count of wake episodes, each costs `sleep_awakening_penalty`.
    - `regularity`: SD of bedtime in minutes; 0 -> 100, `sleep_regularity_zero_min`
      -> 0.

    Each present component is scored 0..100 and combined with `sleep_subweights`,
    renormalized over whichever components were supplied. `None` if none were.
    """
    need = need or cfg.sleep_need_default_h
    comps: dict[str, float] = {}
    if hours is not None:
        comps["duration"] = _duration_score(hours, need, cfg)
    if deep_rem_frac is not None:
        comps["deep_rem"] = _clamp(100.0 * deep_rem_frac / cfg.sleep_target_deep_rem)
    if awakenings is not None:
        comps["awakenings"] = _clamp(100.0 - awakenings * cfg.sleep_awakening_penalty)
    if regularity is not None:
        comps["regularity"] = _clamp(
            100.0 * (1.0 - regularity / cfg.sleep_regularity_zero_min))
    if not comps:
        return None
    wsum = sum(cfg.sleep_subweights[k] for k in comps)
    return round(sum(comps[k] * cfg.sleep_subweights[k] for k in comps) / wsum, 1)


# --- bands, recovery, readiness -----------------------------------------------

def band(score: Optional[float],
         cfg: ScoringConfig = DEFAULT_CONFIG) -> Optional[str]:
    """Map a 0..100 score to green (>=67) / yellow (34..66) / red (<34)."""
    if score is None:
        return None
    if score >= cfg.band_green:
        return "green"
    if score >= cfg.band_yellow:
        return "yellow"
    return "red"


def metric_confidence(n: int, cfg: ScoringConfig = DEFAULT_CONFIG) -> float:
    """Per-metric confidence in [0,1] from baseline maturity: `min(1, n/N)` where
    N = `min_baseline_n` (Phase 4.1). At small n the z itself is noisy, so the
    metric should count for proportionally less."""
    if cfg.min_baseline_n <= 0:
        return 1.0
    return min(1.0, n / cfg.min_baseline_n)


def recovery_score(subscores: dict[str, Optional[float]],
                   cfg: ScoringConfig = DEFAULT_CONFIG,
                   confidences: Optional[dict[str, float]] = None) -> dict:
    """Weighted mean over the *present* sub-scores, weights renormalized.

    `subscores` maps names in `cfg.recovery_weights` to 0..100 (or `None`/absent).
    When `confidences` (metric -> [0,1]) is given and `confidence_weighting` is on
    (Phase 4.1), each blend weight is scaled by its confidence before renormalizing
    so an immature metric contributes proportionally less. Returns `{score, band,
    contributors, confidence, ci}`: `confidence` is the base-weighted aggregate,
    `ci` a ± band that widens as confidence falls. When no sub-score is present,
    score/band are `None`.
    """
    weights = cfg.recovery_weights
    present = {k: v for k, v in subscores.items()
               if v is not None and k in weights}
    if not present:
        return {"score": None, "band": None, "contributors": [],
                "confidence": None, "ci": None}
    conf = confidences or {}
    use_conf = cfg.confidence_weighting

    def cw(k: str) -> float:
        return conf.get(k, 1.0) if use_conf else 1.0

    eff = {k: weights[k] * cw(k) for k in present}
    wsum = sum(eff.values())
    if wsum <= 0:                      # all-zero confidence -> fall back to base
        eff = {k: weights[k] for k in present}
        wsum = sum(eff.values())
    score = sum(present[k] * eff[k] for k in present) / wsum
    contributors = [
        {
            "metric": k,
            "score": round(present[k], 1),
            "weight": round(eff[k] / wsum, 3),
            "contribution": round(present[k] * eff[k] / wsum, 1),
        }
        for k in sorted(present, key=lambda x: weights[x], reverse=True)
    ]
    bwsum = sum(weights[k] for k in present)
    agg_conf = (sum(weights[k] * cw(k) for k in present) / bwsum) if bwsum else 1.0
    ci = round((1.0 - agg_conf) * cfg.confidence_ci_max, 1)
    s = round(score, 1)
    return {"score": s, "band": band(s, cfg), "contributors": contributors,
            "confidence": round(agg_conf, 3), "ci": ci}


def load_penalty(acwr: Optional[float], acute_load: Optional[float] = None,
                 recovery_time_hours: Optional[float] = None,
                 cfg: ScoringConfig = DEFAULT_CONFIG) -> float:
    """Readiness penalty (0..penalty_cap) from accumulated training load.

    No penalty while ACWR sits at/below the 1.3 sweet-spot ceiling; a gentle ramp
    between the ceiling and 1.5; a steep ramp above 1.5 (overreaching). Any
    unresolved `recovery_time_hours` (recovery still owed from the last hard
    session) adds a small per-hour penalty. `acute_load` is unused directly here
    — it feeds `recovery_time_hours` upstream — but is accepted so the signature
    matches the documented `g(ACWR, acute load, recovery time)`.
    """
    p = 0.0
    if acwr is not None:
        if acwr <= cfg.acwr_sweet_hi:
            p += 0.0
        elif acwr <= cfg.acwr_elevated:
            p += (acwr - cfg.acwr_sweet_hi) * cfg.acwr_ramp_per_unit
        else:
            base = (cfg.acwr_elevated - cfg.acwr_sweet_hi) * cfg.acwr_ramp_per_unit
            p += base + (acwr - cfg.acwr_elevated) * cfg.acwr_steep_per_unit
    if recovery_time_hours:
        p += recovery_time_hours * cfg.recovery_time_penalty_per_h
    return min(p, cfg.penalty_cap)


def acwr_ratio(loads: list[float],
               cfg: ScoringConfig = DEFAULT_CONFIG) -> Optional[float]:
    """Acute:chronic workload ratio from a day-contiguous daily-load series.

    `loads` is oldest-first, most-recent-last; the caller zero-fills missing days
    (a day with no training is a rest day = 0 load). The ratio is evaluated at the
    last element:

      acute   = sum of the last `acwr_acute_days` days.
      chronic = the chronic window expressed as an acute-length-equivalent
                (so a steady load gives ratio 1.0). When `cfg.acwr_uncoupled`
                (default) the chronic window *excludes* the acute tail — days
                `[t-chronic_days .. t-acute_days)` — which removes the spurious
                acute↔chronic correlation the coupled window induces. The legacy
                coupled window (chronic includes the acute days) stays selectable.

    Returns the ratio rounded to 2 dp, or `None` when the chronic denominator is
    absent or 0 (matching the existing nil handling). Mirrors Swift `acwrRatio`.
    """
    n = len(loads)
    if n == 0:
        return None
    a, c = cfg.acwr_acute_days, cfg.acwr_chronic_days
    acute = sum(loads[max(0, n - a):])
    if cfg.acwr_uncoupled:
        window = loads[max(0, n - c):max(0, n - a)]
        denom_days = c - a
    else:
        window = loads[max(0, n - c):]
        denom_days = c
    if not window or denom_days <= 0:
        return None
    chronic = sum(window) / (denom_days / a)
    return round(acute / chronic, 2) if chronic > 0 else None


# Fixed advisory text when the illness detector fires (Phase 1.3).
ILLNESS_WARNING = "Elevated illness signals — RHR/temp/resp up, HRV down."

# Directional z orientation for the illness detector: +1 == "higher is worse"
# (temp / resp / sleeping HR), -1 == "lower is worse" (HRV). Iteration order is
# fixed so `signals` is deterministic across the Python/Swift ports.
_ILLNESS_DIRECTIONS = (("temp", 1.0), ("resp", 1.0),
                       ("sleeping_hr", 1.0), ("hrv", -1.0))


def illness_signals(zscores: dict[str, Optional[float]],
                    cfg: ScoringConfig = DEFAULT_CONFIG) -> dict:
    """Directional multi-metric illness / strain co-elevation detector (Phase 1.3).

    Early infection shows as a co-elevation pattern — sleeping HR, wrist
    temperature and respiratory rate up while HRV drops, together — 1–3 days
    before symptoms, which no single metric catches reliably. From the directional
    z-scores already computed elsewhere in the pipeline, this counts the signals
    whose deviation exceeds `cfg.illness_z_threshold` in the *bad* direction and
    sums their exceedance magnitudes into `pressure`. `triggered` once at least
    `cfg.illness_trigger` signals fire.

    Advisory only: the caller uses this to *cap* readiness, never as a weight
    inside the recovery mean. `zscores` maps metric name -> z (None/absent
    metrics simply don't contribute). Returns {count, pressure, triggered,
    signals}. Mirrors Swift `illnessSignals`.
    """
    tau = cfg.illness_z_threshold
    fired: list[str] = []
    pressure = 0.0
    for name, sign in _ILLNESS_DIRECTIONS:
        z = zscores.get(name)
        if z is None:
            continue
        excess = sign * z - tau       # oriented so positive == worse
        if excess > 0:
            fired.append(name)
            pressure += excess
    return {"count": len(fired), "pressure": round(pressure, 4),
            "triggered": len(fired) >= cfg.illness_trigger, "signals": fired}


def _recommendation(readiness_band: Optional[str], acwr: Optional[float],
                    cfg: ScoringConfig = DEFAULT_CONFIG) -> str:
    """Short text guidance for the day."""
    if readiness_band == "green":
        msg = "Ready for a hard session."
    elif readiness_band == "yellow":
        msg = "Moderate — keep it aerobic, hold off on a big effort."
    elif readiness_band == "red":
        msg = "Prioritize rest and easy recovery."
    else:
        msg = "Insufficient data to assess readiness."
    if acwr is not None and acwr > cfg.acwr_elevated:
        msg += f" Training load is elevated (ACWR {acwr:.2f}) — injury risk up."
    return msg


def readiness_score(recovery: Optional[float], acwr: Optional[float],
                    acute_load: Optional[float] = None,
                    recovery_time_hours: Optional[float] = None,
                    illness_zscores: Optional[dict[str, Optional[float]]] = None,
                    cfg: ScoringConfig = DEFAULT_CONFIG) -> dict:
    """Recovery adjusted down for accumulated training load.

    `readiness = clamp(recovery - load_penalty(...), 0, 100)`. Returns
    `{score, band, penalty, recommendation, illness}`. When `recovery` is `None`
    (insufficient data) so is the readiness score.

    When `illness_zscores` is supplied and the illness detector is enabled
    (Phase 1.3), a fired detector *caps* the readiness at
    `cfg.illness_readiness_cap` (top of the yellow band) and appends a warning to
    the recommendation. This is a gate applied on top of the load-adjusted score,
    not a weight inside the recovery mean; it only ever lowers readiness.
    """
    if recovery is None:
        return {"score": None, "band": None, "penalty": None,
                "recommendation": _recommendation(None, acwr, cfg), "illness": None}
    penalty = load_penalty(acwr, acute_load, recovery_time_hours, cfg)
    score = round(_clamp(recovery - penalty), 1)
    illness = None
    if cfg.illness_detector_enabled and illness_zscores:
        illness = illness_signals(illness_zscores, cfg)
        if illness["triggered"]:
            score = round(min(score, cfg.illness_readiness_cap), 1)
    b = band(score, cfg)
    rec = _recommendation(b, acwr, cfg)
    if illness and illness["triggered"]:
        rec += " " + ILLNESS_WARNING
    return {"score": score, "band": b, "penalty": round(penalty, 1),
            "recommendation": rec, "illness": illness}
