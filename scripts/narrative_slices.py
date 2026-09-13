"""Cut a built dashboard.html into one data slice per narrative block.

Deterministic and LLM-free on purpose: the reasoning pass gets ONLY the numbers
its own block is about, so a writer cannot reach for a figure that does not
belong to it, and a verifier can check every number it used against the same
slice. Run against the dashboard.html that build_dashboard just wrote.
"""
import json, sys

def payload(path):
    src = open(path, encoding="utf-8").read()
    i = src.index("const D = {") + len("const D = ")
    j = src.index("\nconst K = D.kpi;")
    return json.loads(src[i:j].rstrip().rstrip(";"))


def slices(D):
    K, M = D["kpi"], D["month"]
    last = lambda a, n: a[-n:] if a else []
    blockruns = M["runs"]
    tenk = sorted([r for r in D["runs"] if 9.7 <= r["km"] <= 10.6],
                  key=lambda r: r["pace"])[:5]
    return {
        "month.hero": {
            "what": "One sentence under a big '+X bpm' number on the month tab.",
            "hrr_series_in_block": [p for p in D["hrr"] if p[0] >= M["block_start"]],
            "hrr_first": M["hrr_a"], "hrr_first_date": M["hrr_a_d"],
            "hrr_last": M["hrr_b"], "hrr_last_date": M["hrr_b_d"],
            "n_measurements": M["hrr_n"], "block_start": M["block_start"],
        },
        "month.stand": {
            "what": "Frames the four first-half vs second-half movers shown right below it.",
            "window": [M["w0"], M["w1"]], "midpoint": M["h_mid"],
            "resting_hr": M["rhr"], "hrv": M["hrv"], "sleep": M["sleep"], "steps": M["steps"],
            "runs_in_window": M["n30"], "km_in_window": M["km30"],
            "weeks_with_run_in_block": K["weeks_run_q3"], "weeks_in_block": K["weeks_q3"],
        },
        "month.read": {
            "what": "The verdict on the month: what is working, what to watch, what is next.",
            "resting_hr": M["rhr"], "hrv": M["hrv"], "sleep": M["sleep"], "steps": M["steps"],
            "hrr_first": M["hrr_a"], "hrr_last": M["hrr_b"], "n_measurements": M["hrr_n"],
            "weeks_with_run_in_block": K["weeks_run_q3"], "weeks_in_block": K["weeks_q3"],
            "weeks_with_run_all_time": K["weeks_run_total"], "weeks_tracked": K["weeks_total"],
            "weekly_board": M["board"], "runs_in_block": blockruns,
            "sleep_now_h": K["sleep_now"],
            "history": "Previous blocks died at five or six weeks. Gaps between "
                       "seasons: 75, 92, 204 and 220 days.",
        },
        "perf.intro": {
            "what": "Opens the Running tab: what the whole running history says.",
            "weeks_with_run_in_block": K["weeks_run_q3"], "weeks_in_block": K["weeks_q3"],
            "weeks_with_run_all_time": K["weeks_run_total"], "weeks_tracked": K["weeks_total"],
            "block_start": M["block_start"], "first_run_in_block": blockruns[0]["d"],
            "avg_run_hr_block": K["hr_block"], "avg_run_hr_prior": K["hr_prior"],
            "avg_km_run_block": K["km_run_block"], "avg_km_run_prior": K["km_run_prior"],
            "pace_block_min_per_km": K["pace_block"], "pace_prior_min_per_km": K["pace_prior"],
            "weekly_km_block": K["km_block"], "weekly_km_prior": K["km_prior"],
            "hrr_blockstart": K["hrr_blockstart"], "hrr_now": K["hrr_now"],
            "longest_run": K["longest"], "km_2026": K["km_2026"],
            "gaps_days": [75, 92, 204, 220],
        },
        "perf.tenk": {
            "what": "Whether a 45:00 10 km is reachable, and by when.",
            "fastest_10k_efforts": tenk, "functional_hrmax": D["hrmax"],
            "target_time_min": 45.0, "target_pace_min_per_km": 4.5,
            "weekly_km_block": K["km_block"],
            "threshold_pace_min_per_km": [5.0, 5.083],
            "threshold_hr_band": [178, 186],
            # Threshold pace is NOT race pace. A 10 km lasting 45-52 min is raced
            # ABOVE lactate threshold, roughly 2-3% faster than threshold pace.
            # Without this the writer silently equates the two and overstates the
            # gain required by about 2.5% -- an error the verifier cannot catch,
            # because nothing in the slice would contradict it.
            "race_pace_vs_threshold_pct_faster": [2.0, 3.0],
            "note": "A well-executed 8-week threshold block moves threshold pace 3-5%. "
                    "Runners going under 45:00 typically run 40-60 km/week. "
                    "Moscow winter costs 15-30 s/km on ice from December to February.",
        },
        "engine.intro": {
            "what": "The 24/7 markers that are not hostage to training gaps.",
            "resting_hr_first": K["rhr_first"], "resting_hr_year_ago": K["rhr_then"],
            "resting_hr_now": K["rhr_now"], "hrv_year_ago": K["hrv_then"], "hrv_now": K["hrv_now"],
            "walking_hr_first": K["whr_first"], "walking_hr_now": K["whr_now"],
            "hrr_now": K["hrr_now"], "vo2max_now": K["vo2_now"], "vo2max_peak": K["vo2_peak"],
            "steps_now": K["steps_now"], "sleep_now_h": K["sleep_now"],
            "vo2_caveat": "Apple infers VO2max from pace-at-HR on outdoor runs, so "
                          "deliberately slow running feeds it lower numbers.",
        },
        "load.intro": {
            "what": "Fitness / fatigue / form, and whether the block is sustainable.",
            "fitness_ctl": K["ctl"], "fatigue_atl": K["atl"], "form_tsb": K["tsb"],
            "fitness_series_last_90d": {k: last(v, 90) for k, v in D["load"].items()},
            "weekly_board": M["board"],
            "sleep_now_h": K["sleep_now"],
            "note": "In 2024 and 2025 fitness climbed for six to eight weeks and then "
                    "fell away for months. Bedtime has drifted from ~22:00 in mid-2024 "
                    "to past 01:00 now; duration has held.",
        },
    }


if __name__ == "__main__":
    D = payload(sys.argv[1] if len(sys.argv) > 1 else "dashboard.html")
    out = {"generated_for": D["generated"], "slices": slices(D)}
    json.dump(out, open("slices.json", "w", encoding="utf-8"),
              ensure_ascii=False, separators=(",", ":"))
    for k, v in out["slices"].items():
        print(f"{k:14s} {len(json.dumps(v)):>7d} bytes")
    print("data date:", out["generated_for"])
