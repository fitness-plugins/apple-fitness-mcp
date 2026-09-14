"""Build the self-contained training-progress dashboard from the health DB.

Reads the local DuckDB read-only, applies the corrections the raw export needs
(see "Cleaning" below), derives the training metrics, and renders a single
portable HTML file with all data inlined.

## Cleaning — none of these are optional

The raw export is wrong in three ways that materially change the numbers, so
this module fixes them at the query layer rather than trusting the tables:

1. **Workouts are duplicated.** Repeated exports, and any watch rename, create
   2-3 copies of the same session under different `row_hash`es (and sometimes
   different `source_name`s). A workout is identified by `(type, start minute)`;
   without this dedup a real export claims ~2x the runs and ~2x the mileage.
2. **Sleep segments overlap across sources.** iPhone and Watch both log sleep
   and their segments intersect, so `SUM(end - start)` reports ~15 h a night.
   The night total is the *union* of the intervals, computed by a gaps-and-
   islands scan.
3. **Steps / energy are counted by every device at once.** Summing all sources
   gives roughly double the truth. Each day takes the single highest-recording
   source instead.

Day boundaries come from `start_ts::DATE`, which resolves in DuckDB's session
timezone — so "a day" means the user's local day, matching what the Health app
shows them.
"""
from __future__ import annotations

import json
import math
import os
import statistics as st
import tempfile
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from . import config, storage
from .dashboard_template import HTML_TEMPLATE

# Sleep is dated by WAKE day, matching get_sleep: an onset after 18:00 belongs
# to the NEXT calendar day (you wake on it); an onset after midnight belongs to
# the day it started on. Dating by the onset evening instead put every night one
# day left of the resting HR and HRV it produced.
_NIGHT = ("CASE WHEN strftime(start_ts,'%H')::INT < 18 "
          "THEN start_ts::DATE ELSE (start_ts + INTERVAL 1 DAY)::DATE END")

_DAILY_METRICS = ("step_count", "active_energy", "exercise_time",
                  "distance_walking_running", "flights_climbed")
_VITALS = ("resting_heart_rate", "hrv", "vo2max", "walking_heart_rate_average",
           "respiratory_rate")

# Enough history for the long-range charts without dragging in the years before
# the watch, where most metrics simply do not exist.
_SINCE = "2023-12-01"
_SINCE_DAILY = "2024-01-01"


# --------------------------------------------------------------------------- #
# queries
# --------------------------------------------------------------------------- #

def _rows(con, sql: str) -> list[dict]:
    cur = con.execute(sql)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _q_daily_activity(con) -> list[dict]:
    """Per-day activity, taking the single highest-recording source per metric."""
    types = ",".join(f"'{t}'" for t in _DAILY_METRICS)
    return _rows(con, f"""
        WITH per_src AS (
            SELECT type, source_name, start_ts::DATE AS d, SUM(value) v
            FROM records_dedup
            WHERE type IN ({types}) AND start_ts >= '{_SINCE_DAILY}'
            GROUP BY 1,2,3),
        best AS (SELECT type, d, MAX(v) v FROM per_src GROUP BY 1,2)
        SELECT d,
          MAX(CASE WHEN type='step_count' THEN v END)               AS steps,
          MAX(CASE WHEN type='active_energy' THEN v END)            AS act_kcal,
          MAX(CASE WHEN type='exercise_time' THEN v END)            AS ex_min,
          MAX(CASE WHEN type='distance_walking_running' THEN v END) AS km,
          MAX(CASE WHEN type='flights_climbed' THEN v END)          AS flights
        FROM best GROUP BY 1 ORDER BY 1""")


def _q_vitals(con) -> list[dict]:
    types = ",".join(f"'{t}'" for t in _VITALS)
    return _rows(con, f"""
        SELECT start_ts::DATE AS d,
          AVG(CASE WHEN type='resting_heart_rate' THEN value END)       AS rhr,
          AVG(CASE WHEN type='hrv' THEN value END)                      AS hrv,
          AVG(CASE WHEN type='vo2max' THEN value END)                   AS vo2,
          AVG(CASE WHEN type='walking_heart_rate_average' THEN value END) AS whr,
          AVG(CASE WHEN type='respiratory_rate' THEN value END)         AS rr
        FROM records_dedup
        WHERE type IN ({types}) AND start_ts >= '{_SINCE}'
        GROUP BY 1 ORDER BY 1""")


def _union_sleep(con, stages: tuple[str, ...], by_stage: bool) -> list[dict]:
    """Merge overlapping sleep intervals, then total per night.

    Gaps-and-islands: a segment starts a new island when it begins after the
    running max end-time of everything before it. Summing island spans counts
    each minute once, however many devices logged it.
    """
    part = "night, stage" if by_stage else "night"
    stage_sel = "stage," if by_stage else ""
    st_list = ",".join(f"'{s}'" for s in stages)
    return _rows(con, f"""
        WITH s AS (
            SELECT {_NIGHT} AS night, stage, start_ts, end_ts
            FROM sleep WHERE stage IN ({st_list}) AND start_ts >= '{_SINCE}'),
        m AS (SELECT *, MAX(end_ts) OVER (
                  PARTITION BY {part} ORDER BY start_ts
                  ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) prev_end
              FROM s),
        g AS (SELECT *, SUM(CASE WHEN prev_end IS NULL OR start_ts > prev_end
                                 THEN 1 ELSE 0 END)
                  OVER (PARTITION BY {part} ORDER BY start_ts) grp
              FROM m),
        u AS (SELECT night, {stage_sel} grp, MIN(start_ts) s0, MAX(end_ts) e0
              FROM g GROUP BY ALL)
        SELECT night, {stage_sel}
          -- `hours` is a reserved word in some DuckDB builds; do not use it.
          SUM(EXTRACT(EPOCH FROM (e0 - s0)))/3600.0 AS sleep_hours,
          strftime(MIN(s0),'%H:%M') AS bed
        FROM u GROUP BY ALL ORDER BY 1""")


def _q_workouts(con) -> list[dict]:
    """All workouts, deduplicated to one row per (type, start minute).

    DISTINCT ON collapses the export's repeat copies; without it the same run
    appears 2-3 times. Dates come back as ISO strings so every downstream
    comparison is string-vs-string.
    """
    rows = _rows(con, f"""
        SELECT * FROM (
            SELECT DISTINCT ON (type, date_trunc('minute', start_ts))
                   type,
                   start_ts::DATE AS d,
                   strftime(start_ts,'%H:%M') AS tm,
                   duration AS dur_min, distance AS km, energy AS kcal,
                   avg_hr, max_hr
            FROM workouts WHERE start_ts >= '{_SINCE_DAILY}'
            ORDER BY type, date_trunc('minute', start_ts))
        ORDER BY d, tm""")
    for r in rows:
        r["d"] = _iso(r["d"])
    return rows


def _q_raw_workout_count(con) -> int:
    return con.execute(
        f"SELECT count(*) FROM workouts WHERE start_ts >= '{_SINCE_DAILY}'"
    ).fetchone()[0]


def _q_hrr(con) -> list[dict]:
    return _rows(con, """
        SELECT start_ts::DATE d, MAX(value) v FROM records_dedup
        WHERE type='heart_rate_recovery' GROUP BY 1 ORDER BY 1""")


# --------------------------------------------------------------------------- #
# series helpers
# --------------------------------------------------------------------------- #

def _iso(v) -> str:
    return v.isoformat() if isinstance(v, (date, datetime)) else str(v)


def _pairs(rows: list[dict], key: str, dkey: str = "d",
           since: Optional[str] = None, scale: float = 1.0,
           nd: int = 2) -> list[list]:
    """[(iso_date, value)] for one column, dropping NULLs."""
    out = []
    for r in rows:
        v = r.get(key)
        if v is None:
            continue
        d = _iso(r[dkey])
        if since and d < since:
            continue
        out.append([d, round(float(v) * scale, nd)])
    return out


def _roll(pts: list[list], win: int, minpts: Optional[int] = None) -> list[list]:
    """Trailing rolling mean over a *calendar* window, not a row window.

    Row windows lie when the data is gappy (a month with the watch off would
    average across the gap as if the days were adjacent); a calendar window
    simply produces no point until enough real readings fall inside it.
    """
    minpts = minpts or max(2, win // 6)
    dts = [(date.fromisoformat(d), v) for d, v in pts]
    out = []
    for i, (d, _) in enumerate(dts):
        lo = d - timedelta(days=win - 1)
        vals = [v for dd, v in dts[max(0, i - win * 3):i + 1] if dd >= lo]
        if len(vals) >= minpts:
            out.append([d.isoformat(), round(sum(vals) / len(vals), 2)])
    return out


def _win(pts: list[list], a: str, b: str) -> list[float]:
    return [v for d, v in pts if a <= d <= b]


def _mean(xs, nd: int = 2):
    return round(st.mean(xs), nd) if xs else None


# --------------------------------------------------------------------------- #
# derivation
# --------------------------------------------------------------------------- #

def _collect(con) -> dict:
    """Everything the dashboard needs, cleaned and derived."""
    acts = _q_daily_activity(con)
    vitals = _q_vitals(con)
    wos = _q_workouts(con)
    raw_wo = _q_raw_workout_count(con)
    hrr = _pairs(_q_hrr(con), "v", nd=1)

    sleep_tot = _union_sleep(con, ("core", "rem", "deep", "asleep"), by_stage=False)
    stages = _union_sleep(con, ("deep", "rem"), by_stage=True)
    st_by_night: dict[str, dict] = defaultdict(dict)
    for r in stages:
        st_by_night[_iso(r["night"])][r["stage"]] = r["sleep_hours"]

    sleep = []
    for r in sleep_tot:
        n = _iso(r["night"])
        sleep.append({"d": n, "sleep_h": r["sleep_hours"], "bed": r["bed"],
                      "deep_h": st_by_night[n].get("deep"),
                      "rem_h": st_by_night[n].get("rem")})

    if not wos:
        raise ValueError("no workouts in the database — import an export first")

    today = max(
        [_iso(r["d"]) for r in acts] + [_iso(r["d"]) for r in vitals] +
        [w["d"] for w in wos] + [r["d"] for r in sleep]
    )
    TODAY = date.fromisoformat(today)

    # --- resting-HR baseline, used for HR-reserve maths -------------------- #
    rhr_raw = _pairs(vitals, "rhr", nd=1)
    rhr30 = dict(_roll(rhr_raw, 30))
    _rhr_keys = sorted(rhr30)

    def rhr_at(d: str) -> float:
        if d in rhr30:
            return rhr30[d]
        prior = [k for k in _rhr_keys if k <= d]
        return rhr30[prior[-1]] if prior else 72.0

    # Functional HRmax = 95th percentile of per-run maxima. The single highest
    # reading in an export is almost always a sensor artefact and would skew
    # every zone boundary if used directly.
    #
    # Use an interpolated quantile, NOT `sorted[int(n * 0.95)]` — for small n
    # that index *is* the last element, so the outlier the percentile exists to
    # exclude would set the value anyway.
    run_max = sorted(float(w["max_hr"]) for w in wos
                     if w["type"] == "running" and w.get("max_hr"))
    if len(run_max) >= 4:
        hrmax = st.quantiles(run_max, n=20, method="inclusive")[18]
    elif run_max:
        hrmax = run_max[-1]
    else:
        hrmax = 200.0

    # --- runs --------------------------------------------------------------- #
    runs = []
    for w in wos:
        if w["type"] != "running" or not w.get("km") or w["km"] < 1.0:
            continue
        if not w.get("avg_hr") or not w.get("dur_min"):
            continue
        rest = rhr_at(w["d"])
        if w["avg_hr"] <= rest or hrmax <= rest:
            continue
        speed = w["km"] * 1000 / w["dur_min"]          # metres per minute
        frac = (w["avg_hr"] - rest) / (hrmax - rest)   # heart-rate reserve
        runs.append({
            "d": w["d"], "km": round(w["km"], 2), "min": round(w["dur_min"], 1),
            "pace": round(w["dur_min"] / w["km"], 2),
            "hr": round(w["avg_hr"]), "maxhr": w.get("max_hr"),
            "kcal": round(w["kcal"]) if w.get("kcal") else None,
            # Efficiency index: metres/min per heartbeat above rest, x10.
            "ei": round(speed / (w["avg_hr"] - rest) * 10, 1),
            "hrr": round(frac, 3),
            # Absolute named bands, not %HR-reserve: the reserve model grades
            # against a resting HR that falls over time, so its boundaries drift
            # (Z2/Z3 slid 169 -> 164 bpm across this dataset) and it has no band
            # for the 166-177 grey zone this block exists to avoid.
            # 1 recovery <=149 | 2 easy 150-165 | 3 grey 166-177
            # 4 threshold 178-186 | 5 vo2max 187+
            "zone": (1 if w["avg_hr"] <= 149 else 2 if w["avg_hr"] <= 165 else
                     3 if w["avg_hr"] <= 177 else 4 if w["avg_hr"] <= 186 else 5),
        })

    # --- training load (Banister TRIMP) ------------------------------------- #
    day_load: dict[str, float] = defaultdict(float)
    for w in wos:
        if not w.get("avg_hr") or not w.get("dur_min"):
            continue
        rest = rhr_at(w["d"])
        if hrmax <= rest:
            continue
        x = max(0.0, min(1.0, (w["avg_hr"] - rest) / (hrmax - rest)))
        day_load[w["d"]] += w["dur_min"] * x * 0.64 * math.exp(1.92 * x)

    atl = ctl = 0.0
    load_atl, load_ctl, load_tsb = [], [], []
    cur = date.fromisoformat(min(day_load)) if day_load else TODAY
    while cur <= TODAY:
        k = cur.isoformat()
        l = day_load.get(k, 0.0)
        atl += (l - atl) / 7      # fatigue: 7-day exponential average
        ctl += (l - ctl) / 42     # fitness: 42-day
        load_atl.append([k, round(atl, 1)])
        load_ctl.append([k, round(ctl, 1)])
        load_tsb.append([k, round(ctl - atl, 1)])
        cur += timedelta(days=1)

    # --- weekly grid (zero weeks are the point, so never skip them) --------- #
    wk: dict[str, dict] = defaultdict(lambda: {"km": 0.0, "n": 0, "min": 0.0,
                                               "load": 0.0})
    for w in wos:
        if w["type"] != "running":
            continue
        d = date.fromisoformat(w["d"])
        k = (d - timedelta(days=d.weekday())).isoformat()
        wk[k]["km"] += w.get("km") or 0.0
        wk[k]["n"] += 1
        wk[k]["min"] += w.get("dur_min") or 0.0
    for k, l in day_load.items():
        d = date.fromisoformat(k)
        wk[(d - timedelta(days=d.weekday())).isoformat()]["load"] += l

    weekly = []
    if wk:
        w0 = date.fromisoformat(min(wk))
        w0 -= timedelta(days=w0.weekday())
        wN = TODAY - timedelta(days=TODAY.weekday())
        cur = w0
        while cur <= wN:
            v = wk.get(cur.isoformat(), {"km": 0.0, "n": 0, "min": 0.0, "load": 0.0})
            weekly.append({"w": cur.isoformat(), "km": round(v["km"], 1),
                           "n": v["n"], "min": round(v["min"]),
                           "load": round(v["load"])})
            cur += timedelta(days=7)

    # --- sleep series -------------------------------------------------------- #
    sl = _pairs(sleep, "sleep_h", since=_SINCE_DAILY)
    deep = _pairs(sleep, "deep_h", since=_SINCE_DAILY)
    rem = _pairs(sleep, "rem_h", since=_SINCE_DAILY)

    def bed_hours(v: str) -> float:
        h, m = map(int, v.split(":"))
        t = h + m / 60
        return t - 24 if t > 18 else t     # 23:30 -> -0.5, so the axis is linear

    bed = [[_iso(r["d"]), round(bed_hours(r["bed"]), 2)] for r in sleep
           if r.get("bed") and _iso(r["d"]) >= _SINCE_DAILY]

    hrv_raw = _pairs(vitals, "hrv", nd=1)
    steps = _pairs(acts, "steps", nd=0)

    S: dict[str, Any] = {
        "generated": today,
        "hrmax": hrmax,
        "dupes": raw_wo - len(wos),
        "n_workouts": len(wos),
        "runs": runs,
        "weekly": weekly,
        "load": {"atl": load_atl[-540:], "ctl": load_ctl[-540:],
                 "tsb": load_tsb[-540:]},
        "vo2": _roll(_pairs(vitals, "vo2"), 21, 3),
        "rhr": _roll(rhr_raw, 14, 4), "rhr_raw": rhr_raw,
        "hrv": _roll(hrv_raw, 14, 4), "hrv_raw": hrv_raw,
        "whr": _roll(_pairs(vitals, "whr", nd=1), 21, 4),
        "whr_raw": _pairs(vitals, "whr", nd=1),
        "rr": _roll(_pairs(vitals, "rr"), 21, 4),
        "steps": _roll(steps, 28, 8), "steps_raw": steps,
        "kcal": _roll(_pairs(acts, "act_kcal", nd=0), 28, 8),
        "exmin": _roll(_pairs(acts, "ex_min", nd=0), 28, 8),
        "sleep": _roll(sl, 14, 4), "sleep_raw": sl,
        "deep": _roll(deep, 21, 5), "rem": _roll(rem, 21, 5),
        "bed": _roll(bed, 21, 5),
        "hrr": hrr,
    }
    S["kpi"] = _kpi(S, TODAY)
    S["month"] = _month(S, TODAY)
    S["plan"] = _plan(TODAY)
    S["txt"] = _narratives(S["generated"])
    return S


def _kpi(S: dict, TODAY: date) -> dict:
    """Headline figures for the long-range tabs."""
    d = lambda n: (TODAY - timedelta(days=n)).isoformat()
    w0, w1 = d(29), TODAY.isoformat()
    y0, y1 = d(394), d(365)          # the same 30-day window a year earlier
    runs, weekly = S["runs"], S["weekly"]

    block_start = _block_start(weekly, TODAY)
    cur_week = (TODAY - timedelta(days=TODAY.weekday())).isoformat()
    block = [w for w in weekly if block_start <= w["w"] < cur_week]
    prior = [w for w in weekly if w["w"] < block_start and w["n"] > 0]
    recent = [r for r in runs if r["d"] >= block_start]
    older = [r for r in runs if r["d"] < block_start]

    def m(xs, nd=1):
        return round(st.mean(xs), nd) if xs else None

    weeks_since = [w for w in weekly if w["w"] >= block_start]
    return {
        "rhr_now": _mean(_win(S["rhr_raw"], w0, w1)),
        "rhr_then": _mean(_win(S["rhr_raw"], y0, y1)),
        "rhr_first": S["rhr"][0][1] if S["rhr"] else None,
        "hrv_now": _mean(_win(S["hrv_raw"], w0, w1)),
        "hrv_then": _mean(_win(S["hrv_raw"], y0, y1)),
        "hrr_now": S["hrr"][-1][1] if S["hrr"] else None,
        "hrr_blockstart": next((v for k, v in S["hrr"] if k >= block_start),
                               S["hrr"][0][1] if S["hrr"] else None),
        "whr_now": _mean(_win(S["whr_raw"], w0, w1)),
        "whr_first": S["whr"][0][1] if S["whr"] else None,
        "vo2_now": S["vo2"][-1][1] if S["vo2"] else None,
        "vo2_peak": max(S["vo2"], key=lambda x: x[1]) if S["vo2"] else None,
        "km_block": m([w["km"] for w in block]) or 0,
        "km_prior": m([w["km"] for w in prior]) or 0,
        "weeks_run_q3": sum(1 for w in weeks_since if w["n"] > 0),
        "weeks_q3": len(weeks_since),
        "weeks_run_total": sum(1 for w in weekly if w["n"] > 0),
        "weeks_total": len(weekly),
        "km_2026": round(sum(w["km"] for w in weekly
                             if w["w"][:4] == str(TODAY.year))),
        "longest": max(runs, key=lambda r: r["km"]),
        "sleep_now": _mean(_win(S["sleep_raw"], w0, w1)),
        "steps_now": round(_mean(_win(S["steps_raw"], w0, w1)) or 0),
        "hr_block": round(m([r["hr"] for r in recent]) or 0),
        "hr_prior": round(m([r["hr"] for r in older]) or 0),
        "pace_block": m([r["pace"] for r in recent], 2),
        "pace_prior": m([r["pace"] for r in older], 2),
        "km_run_block": m([r["km"] for r in recent]),
        "km_run_prior": m([r["km"] for r in older]),
        "ctl": S["load"]["ctl"][-1][1], "atl": S["load"]["atl"][-1][1],
        "tsb": S["load"]["tsb"][-1][1],
        "dupes": S["dupes"], "hrmax": int(S["hrmax"]),
    }


def _narratives(generated: str) -> Optional[dict]:
    """Model-written prose for the dashboard's narrative blocks, or None.

    The file is written by a separate reasoning pass (see
    `scripts/narrative_slices.py`), never by this module — keeping the build
    itself deterministic and offline.

    A narrative is only served if its `generated_for` matches the data date of
    this build. Prose written against last week's numbers describing this
    week's chart is worse than no prose, so a mismatch silently falls back to
    the template's own derived sentences rather than being shown with a
    disclaimer.
    """
    try:
        path = config.NARRATIVES_OUTPUT_PATH
        if not path.exists():
            return None
        doc = json.loads(path.read_text("utf-8"))
    except Exception:
        return None
    if not isinstance(doc, dict) or not isinstance(doc.get("blocks"), dict):
        return None
    if doc.get("generated_for") != generated:
        return None
    return doc


def _plan(today: Optional[date] = None) -> Optional[dict]:
    """The weekly plan `save_weekly_plan` last wrote, or None.

    Returned verbatim so the template renders whatever schema the plan happens
    to use; a missing, unreadable or empty plan is not an error, the dashboard
    simply omits the section.
    """
    try:
        path = config.PLAN_OUTPUT_PATH
        if not path.exists():
            return None
        plan = json.loads(path.read_text("utf-8"))
    except Exception:
        return None
    if not isinstance(plan, dict) or not plan.get("planned"):
        return None
    # Stale prose is worse than none — the same rule _narratives applies. A plan
    # is forward-looking, so allow the current week and the one just gone.
    wk = plan.get("week_of")
    if today and isinstance(wk, str):
        try:
            if (today - date.fromisoformat(wk)).days > 13:
                return None
        except ValueError:
            pass
    return plan


def _block_start(weekly: list[dict], TODAY: date) -> str:
    """First week of the current unbroken training block.

    Walks back from this week while weeks keep containing a run, so the "current
    block" framing stays true as the block grows instead of being pinned to a
    date that was only correct the day it was written.
    """
    if not weekly:
        return TODAY.isoformat()
    idx = len(weekly) - 1
    # The in-progress week may legitimately have no run yet; don't let that
    # truncate the block to nothing.
    while idx > 0 and weekly[idx]["n"] == 0:
        idx -= 1
    start = idx
    while start > 0 and weekly[start - 1]["n"] > 0:
        start -= 1
    return weekly[start]["w"]


def _month(S: dict, TODAY: date) -> dict:
    """The last-30-days tab: movement *within* the window, not against a baseline.

    A month-over-month delta is meaningless when the prior month has a data gap
    (watch not worn), which is common. Comparing the two halves of the current
    window is always well-defined.
    """
    d = lambda n: (TODAY - timedelta(days=n)).isoformat()
    W0, W1 = d(29), TODAY.isoformat()
    H0, H1, H2, H3 = d(29), d(15), d(14), TODAY.isoformat()
    block_start = _block_start(S["weekly"], TODAY)

    def half(key: str):
        a, b = _win(S[key], H0, H1), _win(S[key], H2, H3)
        if not a or not b:
            return None
        return {"a": round(st.mean(a), 1), "b": round(st.mean(b), 1),
                "d": round(st.mean(b) - st.mean(a), 1),
                "na": len(a), "nb": len(b)}

    block_hrr_pts = [(k, v) for k, v in S["hrr"] if k >= block_start]
    block_hrr = [v for _, v in block_hrr_pts]
    board = []
    for w in S["weekly"][-8:]:
        a = w["w"]
        b = (date.fromisoformat(a) + timedelta(days=6)).isoformat()
        rr = [r for r in S["runs"] if a <= r["d"] <= b]
        board.append({
            "w": a, "km": w["km"], "n": w["n"], "load": w["load"],
            "hr": round(st.mean([r["hr"] for r in rr])) if rr else None,
            "pace": round(st.mean([r["pace"] for r in rr]), 2) if rr else None,
            "rhr": _mean(_win(S["rhr_raw"], a, b)),
            "hrv": _mean(_win(S["hrv_raw"], a, b)),
            "sleep": _mean(_win(S["sleep_raw"], a, b)),
        })

    return {
        "w0": W0, "w1": W1, "h_mid": H2, "block_start": block_start,
        "rhr": half("rhr_raw"), "hrv": half("hrv_raw"),
        "sleep": half("sleep_raw"), "steps": half("steps_raw"),
        "hrr_a": block_hrr[0] if block_hrr else None,
        "hrr_b": block_hrr[-1] if block_hrr else None,
        "hrr_n": len(block_hrr),
        "hrr_a_d": block_hrr_pts[0][0] if block_hrr_pts else None,
        "hrr_b_d": block_hrr_pts[-1][0] if block_hrr_pts else None,
        "runs": [r for r in S["runs"] if r["d"] >= block_start],
        "km30": round(sum(r["km"] for r in S["runs"] if W0 <= r["d"] <= W1), 1),
        "n30": sum(1 for r in S["runs"] if W0 <= r["d"] <= W1),
        "board": board,
    }


# --------------------------------------------------------------------------- #
# render + write
# --------------------------------------------------------------------------- #

def render(payload: dict) -> str:
    """Inline the payload into the HTML shell. No network, no external assets."""
    runs = payload["runs"]
    first = min(r["d"] for r in runs)
    return (HTML_TEMPLATE
            # `</` must not survive into the inline <script>: json.dumps does not
            # escape it, so a literal "</script>" in model-written plan prose would
            # close the tag and blank the whole document.
            .replace("__DATA__", json.dumps(payload, separators=(",", ":"))
                     .replace("</", "<\\/"))
            .replace("__FIRST__", date.fromisoformat(first).strftime("%b %Y"))
            .replace("__NOW__",
                     date.fromisoformat(payload["generated"]).strftime("%d %b %Y"))
            .replace("__NRUN__", str(len(runs)))
            .replace("__NWK__", str(len(payload["weekly"]))))


def _atomic_write(path: Path, text: str) -> None:
    """Write via a temp file in the same dir + os.replace, so a browser holding
    the old file never reads a half-written document."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".dash-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def build(path: Optional[str | Path] = None) -> dict:
    """Regenerate the dashboard from whatever is currently in the DB.

    Opens a read-only connection, so this is safe to call while the server is
    serving queries. Returns a summary dict for the MCP tool to hand back.
    """
    con = storage.connect_readonly()
    try:
        payload = _collect(con)
    finally:
        con.close()

    html = render(payload)
    out = Path(path).expanduser() if path else config.DASHBOARD_OUTPUT_PATH
    _atomic_write(out, html)

    k = payload["kpi"]
    return {
        "status": "built",
        "path": str(out),
        "bytes": len(html.encode("utf-8")),
        "data_through": payload["generated"],
        "runs": len(payload["runs"]),
        "workouts": payload["n_workouts"],
        "duplicate_workout_records_removed": payload["dupes"],
        "weeks_tracked": len(payload["weekly"]),
        "current_block_start": payload["month"]["block_start"],
        "headline": {
            "resting_hr_30d": k["rhr_now"], "hrv_30d": k["hrv_now"],
            "vo2max": k["vo2_now"], "fitness_ctl": k["ctl"],
            "fatigue_atl": k["atl"], "form_tsb": k["tsb"],
        },
    }
