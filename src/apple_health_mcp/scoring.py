"""Pure recovery/readiness scoring math — numbers in, numbers out, no DB.

This is the reference implementation that will be ported 1:1 to Swift, so every
function here is deliberately dependency-free (only `math`) and side-effect-free.
The DB plumbing that feeds these functions lives in `server.py`.

Design principles (see RECOVERY_READINESS_PLAN.md):

- **Personal baseline, not absolutes.** Apple HRV is *SDNN*, not rMSSD, so no
  population thresholds are hard-coded. Every physiological metric is scored by
  its deviation (z-score) from the user's own rolling baseline, where 50 == on
  baseline (temperature is the exception, see `temp_score`).
- **Graceful degradation.** A missing metric yields a `None` sub-score; the
  weighted mean in `recovery_score` renormalizes over whatever is present.

Calibration knobs are named module constants so they can be tuned without
touching logic: `K` (z-score sensitivity), `RECOVERY_WEIGHTS` (recovery mix),
the sleep constants, the band thresholds, and the readiness penalty constants.
"""
from __future__ import annotations

import math
from typing import Optional

# --- calibration knobs --------------------------------------------------------

# Z-score sensitivity: points of sub-score per standard deviation from baseline.
# At k=18 a metric one SD "better" than baseline scores 68, two SD scores 86.
K = 18.0

# Recovery mix (WHOOP-leaning start; calibrate on real data). Keys are the
# sub-score names recovery_score consumes; weights renormalize over those present.
RECOVERY_WEIGHTS = {
    "hrv": 0.40,
    "sleeping_hr": 0.20,
    "sleep": 0.25,
    "resp": 0.10,
    "temp": 0.05,
}

# Band thresholds (WHOOP 67/34): green >= 67, yellow 34..66, red < 34.
BAND_GREEN = 67.0
BAND_YELLOW = 34.0

# Sleep sub-score constants.
SLEEP_NEED_DEFAULT_H = 8.0        # fallback nightly sleep need (hours)
SLEEP_TARGET_DEEP_REM = 0.45      # deep+REM fraction that scores 100
SLEEP_AWAKENING_PENALTY = 8.0     # sub-score points lost per awakening
SLEEP_REGULARITY_ZERO_MIN = 90.0  # bedtime SD (min) at which regularity hits 0
SLEEP_SUBWEIGHTS = {
    "duration": 0.50,
    "deep_rem": 0.25,
    "awakenings": 0.15,
    "regularity": 0.10,
}

# Readiness load-penalty constants.
ACWR_SWEET_HI = 1.3               # no penalty at/below this ACWR
ACWR_ELEVATED = 1.5               # penalty ramps steeply above this
ACWR_RAMP_PER_UNIT = 30.0         # penalty slope between sweet-hi and elevated
ACWR_STEEP_PER_UNIT = 40.0        # penalty slope above elevated
RECOVERY_TIME_PENALTY_PER_H = 0.6  # readiness points lost per unresolved rec.-hour
PENALTY_CAP = 40.0                # max total load penalty


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


# --- physiological sub-scores (each 0..100) -----------------------------------

def hrv_score(z: Optional[float]) -> Optional[float]:
    """Higher HRV than baseline is better. 50 == on baseline."""
    return None if z is None else _clamp(50.0 + K * z)


def rhr_score(z: Optional[float]) -> Optional[float]:
    """Higher resting/sleeping HR than baseline is worse. 50 == on baseline. (The
    sign convention here also serves the `sleeping_hr` sub-score.)"""
    return None if z is None else _clamp(50.0 - K * z)


def resp_score(z: Optional[float]) -> Optional[float]:
    """Higher respiratory rate than baseline is worse (illness/stress)."""
    return None if z is None else _clamp(50.0 - K * z)


def temp_score(z: Optional[float]) -> Optional[float]:
    """Wrist temperature: deviation in *either* direction is worse, so this peaks
    at 100 exactly on baseline (z=0) and falls off symmetrically."""
    return None if z is None else _clamp(100.0 - K * abs(z) * 2.0)


def sleep_score(hours: Optional[float], need: Optional[float] = None,
                deep_rem_frac: Optional[float] = None,
                awakenings: Optional[float] = None,
                regularity: Optional[float] = None) -> Optional[float]:
    """Composite sleep quality 0..100 from up to four present components.

    - `hours` vs `need` (default 8h): duration met, capped at 100.
    - `deep_rem_frac`: deep+REM as a fraction of time asleep vs a 0.45 target.
    - `awakenings`: count of wake episodes, each costs `SLEEP_AWAKENING_PENALTY`.
    - `regularity`: SD of bedtime in minutes; 0 -> 100, `SLEEP_REGULARITY_ZERO_MIN`
      -> 0.

    Each present component is scored 0..100 and combined with `SLEEP_SUBWEIGHTS`,
    renormalized over whichever components were supplied. `None` if none were.
    """
    need = need or SLEEP_NEED_DEFAULT_H
    comps: dict[str, float] = {}
    if hours is not None:
        comps["duration"] = _clamp(100.0 * hours / need)
    if deep_rem_frac is not None:
        comps["deep_rem"] = _clamp(100.0 * deep_rem_frac / SLEEP_TARGET_DEEP_REM)
    if awakenings is not None:
        comps["awakenings"] = _clamp(100.0 - awakenings * SLEEP_AWAKENING_PENALTY)
    if regularity is not None:
        comps["regularity"] = _clamp(
            100.0 * (1.0 - regularity / SLEEP_REGULARITY_ZERO_MIN))
    if not comps:
        return None
    wsum = sum(SLEEP_SUBWEIGHTS[k] for k in comps)
    return round(sum(comps[k] * SLEEP_SUBWEIGHTS[k] for k in comps) / wsum, 1)


# --- bands, recovery, readiness -----------------------------------------------

def band(score: Optional[float]) -> Optional[str]:
    """Map a 0..100 score to green (>=67) / yellow (34..66) / red (<34)."""
    if score is None:
        return None
    if score >= BAND_GREEN:
        return "green"
    if score >= BAND_YELLOW:
        return "yellow"
    return "red"


def recovery_score(subscores: dict[str, Optional[float]]) -> dict:
    """Weighted mean over the *present* sub-scores, weights renormalized.

    `subscores` maps names in `RECOVERY_WEIGHTS` to 0..100 (or `None`/absent).
    Returns `{score, band, contributors}` where each contributor carries its
    renormalized weight and its point contribution to the final score. When no
    sub-score is present, score/band are `None`.
    """
    present = {k: v for k, v in subscores.items()
               if v is not None and k in RECOVERY_WEIGHTS}
    if not present:
        return {"score": None, "band": None, "contributors": []}
    wsum = sum(RECOVERY_WEIGHTS[k] for k in present)
    score = sum(present[k] * RECOVERY_WEIGHTS[k] for k in present) / wsum
    contributors = [
        {
            "metric": k,
            "score": round(present[k], 1),
            "weight": round(RECOVERY_WEIGHTS[k] / wsum, 3),
            "contribution": round(present[k] * RECOVERY_WEIGHTS[k] / wsum, 1),
        }
        for k in sorted(present, key=lambda x: RECOVERY_WEIGHTS[x], reverse=True)
    ]
    s = round(score, 1)
    return {"score": s, "band": band(s), "contributors": contributors}


def load_penalty(acwr: Optional[float], acute_load: Optional[float] = None,
                 recovery_time_hours: Optional[float] = None) -> float:
    """Readiness penalty (0..PENALTY_CAP) from accumulated training load.

    No penalty while ACWR sits at/below the 1.3 sweet-spot ceiling; a gentle ramp
    between the ceiling and 1.5; a steep ramp above 1.5 (overreaching). Any
    unresolved `recovery_time_hours` (recovery still owed from the last hard
    session) adds a small per-hour penalty. `acute_load` is unused directly here
    — it feeds `recovery_time_hours` upstream — but is accepted so the signature
    matches the documented `g(ACWR, acute load, recovery time)`.
    """
    p = 0.0
    if acwr is not None:
        if acwr <= ACWR_SWEET_HI:
            p += 0.0
        elif acwr <= ACWR_ELEVATED:
            p += (acwr - ACWR_SWEET_HI) * ACWR_RAMP_PER_UNIT
        else:
            base = (ACWR_ELEVATED - ACWR_SWEET_HI) * ACWR_RAMP_PER_UNIT
            p += base + (acwr - ACWR_ELEVATED) * ACWR_STEEP_PER_UNIT
    if recovery_time_hours:
        p += recovery_time_hours * RECOVERY_TIME_PENALTY_PER_H
    return min(p, PENALTY_CAP)


def _recommendation(readiness_band: Optional[str], acwr: Optional[float]) -> str:
    """Short text guidance for the day."""
    if readiness_band == "green":
        msg = "Ready for a hard session."
    elif readiness_band == "yellow":
        msg = "Moderate — keep it aerobic, hold off on a big effort."
    elif readiness_band == "red":
        msg = "Prioritize rest and easy recovery."
    else:
        msg = "Insufficient data to assess readiness."
    if acwr is not None and acwr > ACWR_ELEVATED:
        msg += f" Training load is elevated (ACWR {acwr:.2f}) — injury risk up."
    return msg


def readiness_score(recovery: Optional[float], acwr: Optional[float],
                    acute_load: Optional[float] = None,
                    recovery_time_hours: Optional[float] = None) -> dict:
    """Recovery adjusted down for accumulated training load.

    `readiness = clamp(recovery - load_penalty(...), 0, 100)`. Returns
    `{score, band, penalty, recommendation}`. When `recovery` is `None`
    (insufficient data) so is the readiness score.
    """
    if recovery is None:
        return {"score": None, "band": None, "penalty": None,
                "recommendation": _recommendation(None, acwr)}
    penalty = load_penalty(acwr, acute_load, recovery_time_hours)
    score = round(_clamp(recovery - penalty), 1)
    b = band(score)
    return {"score": score, "band": b, "penalty": round(penalty, 1),
            "recommendation": _recommendation(b, acwr)}
