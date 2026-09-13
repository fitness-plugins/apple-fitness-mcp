"""Streaming parser for Apple Health export.xml.

Uses ElementTree.iterparse (NOT parse) so that 200-800 MB exports with
millions of <Record> elements are processed incrementally with a flat memory
profile. Top-level elements are cleared and detached from the root as soon as
they are consumed.

The public entry point is ``iter_export`` which yields ``(kind, payload)``
tuples in a single pass over the file. ``kind`` is one of:
    "record", "workout", "workout_event", "activity_summary", "clinical", "sleep"

``workout_event`` carries the *structure* of a workout — the pieces Apple ships
as child elements of <Workout> and that the uniform HR/pace binning cannot
recover:

  * ``<WorkoutActivity>``  — one repetition of a structured (workout-builder)
    session: warm-up, each work interval, each recovery, cool-down. Carries its
    own <WorkoutStatistics> (per-rep HR/pace/power/distance) and the
    ``WOIntervalStepKeyPath`` / ``WOIntervalStepSuccessful`` metadata that say
    where the rep sits in the plan and whether its target was met.
  * ``<WorkoutEvent>``     — segment / pause / resume / marker boundaries, both
    the workout's own and the ones nested inside a <WorkoutActivity>.

Each payload repeats the *parent workout's identity fields* (type, source_name,
start, end) because the parent's ``row_hash`` is computed in DuckDB, not here;
storage recomputes the identical hash expression to link child to parent.
"""
from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from datetime import timedelta
from pathlib import Path
from typing import Iterator, Tuple

from . import normalize

TOP_LEVEL_TAGS = {"Record", "Workout", "ActivitySummary", "ClinicalRecord"}

# Distance-family workout statistic identifiers we treat as "distance".
_DISTANCE_STATS = {
    "HKQuantityTypeIdentifierDistanceWalkingRunning",
    "HKQuantityTypeIdentifierDistanceCycling",
    "HKQuantityTypeIdentifierDistanceSwimming",
    "HKQuantityTypeIdentifierDistanceDownhillSnowSports",
}

_QUANTITY_PREFIX = "HKQuantityTypeIdentifier"
_EVENT_TYPE_PREFIX = "HKWorkoutEventType"

# Metadata keys the Apple Watch workout builder writes onto each repetition.
_KEY_PATH_META = "WOIntervalStepKeyPath"
_STEP_OK_META = "WOIntervalStepSuccessful"
_ELEVATION_META = "HKElevationAscended"

# Statistic fields lifted out of <WorkoutStatistics> into typed columns. Every
# key is always present in the payload (None when absent) so the columnar Arrow
# insert in storage always sees a rectangular batch.
_STAT_FIELDS = (
    "distance", "distance_unit", "energy", "energy_unit",
    "avg_hr", "min_hr", "max_hr", "avg_speed", "speed_unit",
    "avg_power", "power_unit", "step_count",
)


def _record_payload(elem: ET.Element) -> dict:
    a = elem.attrib
    return {
        "type": normalize.normalize_type(a.get("type")),
        "raw_type": a.get("type"),
        "source_name": a.get("sourceName"),
        "source_version": a.get("sourceVersion"),
        "device": a.get("device"),
        "unit": a.get("unit"),
        "value": normalize.to_float(a.get("value")),
        "value_str": a.get("value"),
        "start": normalize.parse_ts(a.get("startDate")),
        "end": normalize.parse_ts(a.get("endDate")),
        "created": normalize.parse_ts(a.get("creationDate")),
    }


def _sleep_payload(rec: dict, elem: ET.Element) -> dict:
    return {
        "source_name": rec["source_name"],
        "device": rec["device"],
        "stage": normalize.normalize_sleep_stage(elem.attrib.get("value")),
        "raw_value": elem.attrib.get("value"),
        "start": rec["start"],
        "end": rec["end"],
    }


def _workout_payload(elem: ET.Element) -> dict:
    a = elem.attrib
    avg_hr = max_hr = None
    distance = normalize.to_float(a.get("totalDistance"))
    distance_unit = a.get("totalDistanceUnit")
    energy = normalize.to_float(a.get("totalEnergyBurned"))
    energy_unit = a.get("totalEnergyBurnedUnit")

    # Newer exports carry distance/energy/HR as <WorkoutStatistics> children.
    for stat in elem.findall("WorkoutStatistics"):
        st = stat.attrib.get("type")
        if st == "HKQuantityTypeIdentifierHeartRate":
            avg_hr = normalize.to_float(stat.attrib.get("average")) or avg_hr
            max_hr = normalize.to_float(stat.attrib.get("maximum")) or max_hr
        elif st == "HKQuantityTypeIdentifierActiveEnergyBurned":
            s = normalize.to_float(stat.attrib.get("sum"))
            if s is not None:
                energy = s
                energy_unit = stat.attrib.get("unit") or energy_unit
        elif st in _DISTANCE_STATS:
            s = normalize.to_float(stat.attrib.get("sum"))
            if s is not None:
                distance = s
                distance_unit = stat.attrib.get("unit") or distance_unit

    return {
        "type": normalize.normalize_type(a.get("workoutActivityType")),
        "raw_type": a.get("workoutActivityType"),
        "source_name": a.get("sourceName"),
        "device": a.get("device"),
        "duration": normalize.to_float(a.get("duration")),
        "duration_unit": a.get("durationUnit"),
        "distance": distance,
        "distance_unit": distance_unit,
        "energy": energy,
        "energy_unit": energy_unit,
        "avg_hr": avg_hr,
        "max_hr": max_hr,
        "start": normalize.parse_ts(a.get("startDate")),
        "end": normalize.parse_ts(a.get("endDate")),
    }


# --- workout structure (repetitions / segment boundaries) ---------------------

def _normalize_event_type(raw: str | None) -> str | None:
    """HKWorkoutEventTypeSegment -> segment."""
    if not raw:
        return None
    stripped = raw
    if stripped.startswith(_EVENT_TYPE_PREFIX):
        stripped = stripped[len(_EVENT_TYPE_PREFIX):]
    return normalize._snake(stripped) if stripped else None


def _split_quantity(value: str | None) -> tuple[float | None, str | None]:
    """'262 cm' -> (262.0, 'cm'). Tolerates a bare number or junk."""
    if not value:
        return None, None
    parts = value.strip().split(None, 1)
    num = normalize.to_float(parts[0])
    unit = parts[1].strip() if len(parts) > 1 else None
    return num, unit


def _empty_stats() -> dict:
    return {k: None for k in _STAT_FIELDS}


def _collect_stats(el: ET.Element) -> tuple[dict, str | None]:
    """Lift <WorkoutStatistics> children into typed fields + a raw JSON blob.

    The JSON blob keeps every statistic Apple shipped (ground contact time,
    vertical oscillation, stride length, basal energy, ...) so nothing is lost
    to the fixed column set.
    """
    fields = _empty_stats()
    raw: dict[str, dict] = {}
    for stat in el.findall("WorkoutStatistics"):
        a = stat.attrib
        st = a.get("type") or ""
        short = st[len(_QUANTITY_PREFIX):] if st.startswith(_QUANTITY_PREFIX) else st
        raw[short] = {k: v for k, v in a.items() if k != "type"}
        if st == "HKQuantityTypeIdentifierHeartRate":
            fields["avg_hr"] = normalize.to_float(a.get("average"))
            fields["min_hr"] = normalize.to_float(a.get("minimum"))
            fields["max_hr"] = normalize.to_float(a.get("maximum"))
        elif st == "HKQuantityTypeIdentifierActiveEnergyBurned":
            fields["energy"] = normalize.to_float(a.get("sum"))
            fields["energy_unit"] = a.get("unit")
        elif st in _DISTANCE_STATS:
            fields["distance"] = normalize.to_float(a.get("sum"))
            fields["distance_unit"] = a.get("unit")
        elif st == "HKQuantityTypeIdentifierRunningSpeed":
            fields["avg_speed"] = normalize.to_float(a.get("average"))
            fields["speed_unit"] = a.get("unit")
        elif st == "HKQuantityTypeIdentifierRunningPower":
            fields["avg_power"] = normalize.to_float(a.get("average"))
            fields["power_unit"] = a.get("unit")
        elif st == "HKQuantityTypeIdentifierStepCount":
            fields["step_count"] = normalize.to_float(a.get("sum"))
    return fields, (json.dumps(raw, sort_keys=True) if raw else None)


def _parent_identity(wk: dict) -> dict:
    """Parent-workout identity fields, repeated on every child payload.

    The workout's row_hash is computed in DuckDB (see storage._WORKOUT_HASH),
    so children cannot carry it directly; they carry the inputs instead and
    storage recomputes the identical expression.
    """
    return {
        "workout_type": wk["type"],
        "workout_source_name": wk["source_name"],
        "workout_start": wk["start"],
        "workout_end": wk["end"],
    }


def _event_payload(parent: dict, ev: ET.Element, kind: str, index: int,
                   activity_uuid: str | None) -> dict:
    """One <WorkoutEvent>: a segment / pause / resume / marker boundary."""
    a = ev.attrib
    start = normalize.parse_ts(a.get("date"))
    duration = normalize.to_float(a.get("duration"))
    duration_unit = a.get("durationUnit")
    end = None
    if start is not None and duration is not None and duration_unit == "min":
        end = start + timedelta(minutes=duration)
    payload = dict(parent)
    payload.update(_empty_stats())
    payload.update({
        "event_kind": kind,
        "event_type": _normalize_event_type(a.get("type")),
        "raw_event_type": a.get("type"),
        "step_index": index,
        "activity_uuid": activity_uuid,
        "step_key_path": None,
        "step_block": None,
        "step_repeat": None,
        "step_slot": None,
        "step_successful": None,
        "start": start,
        "end": end,
        "duration": duration,
        "duration_unit": duration_unit,
        "elevation_ascended": None,
        "elevation_unit": None,
        "stats_json": None,
    })
    return payload


def _activity_payload(parent: dict, act: ET.Element, index: int) -> dict:
    """One <WorkoutActivity>: a single repetition of a structured session."""
    a = act.attrib
    meta = {m.attrib.get("key"): m.attrib.get("value")
            for m in act.findall("MetadataEntry")}
    stats, stats_json = _collect_stats(act)

    key_path = meta.get(_KEY_PATH_META)
    block = repeat = slot = None
    if key_path:
        nums = []
        for part in key_path.split("."):
            try:
                nums.append(int(part))
            except ValueError:
                nums.append(None)
        block = nums[0] if len(nums) > 0 else None
        repeat = nums[1] if len(nums) > 1 else None
        slot = nums[2] if len(nums) > 2 else None

    raw_ok = meta.get(_STEP_OK_META)
    successful = None
    if raw_ok is not None:
        successful = raw_ok.strip().lower() in ("1", "true", "yes")

    elevation, elevation_unit = _split_quantity(meta.get(_ELEVATION_META))

    payload = dict(parent)
    payload.update(stats)
    payload.update({
        "event_kind": "activity",
        "event_type": None,
        "raw_event_type": None,
        "step_index": index,
        "activity_uuid": a.get("uuid"),
        "step_key_path": key_path,
        "step_block": block,
        "step_repeat": repeat,
        "step_slot": slot,
        "step_successful": successful,
        "start": normalize.parse_ts(a.get("startDate")),
        "end": normalize.parse_ts(a.get("endDate")),
        "duration": normalize.to_float(a.get("duration")),
        "duration_unit": a.get("durationUnit"),
        "elevation_ascended": elevation,
        "elevation_unit": elevation_unit,
        "stats_json": stats_json,
    })
    return payload


def workout_event_payloads(elem: ET.Element, wk: dict) -> list[dict]:
    """Every structural child of one <Workout>, in document order.

    Returns a list (not a generator) so the caller can clear the element
    immediately afterwards. The list is bounded by one workout's child count,
    so memory stays flat over the whole export.

    Deliberately NOT emitted here: workout-level <MetadataEntry> (workout
    attributes, and this export repeats them verbatim before and after the
    events) and <WorkoutRoute>/<FileReference> (GPS track files, not structure).
    """
    parent = _parent_identity(wk)
    out: list[dict] = []
    for i, ev in enumerate(elem.findall("WorkoutEvent")):
        out.append(_event_payload(parent, ev, "event", i, None))
    for i, act in enumerate(elem.findall("WorkoutActivity")):
        out.append(_activity_payload(parent, act, i))
        activity_uuid = act.attrib.get("uuid")
        for j, ev in enumerate(act.findall("WorkoutEvent")):
            out.append(_event_payload(parent, ev, "activity_event", j,
                                      activity_uuid))
    return out


def _activity_summary_payload(elem: ET.Element) -> dict:
    a = elem.attrib
    return {
        "date": a.get("dateComponents"),
        "active_energy": normalize.to_float(a.get("activeEnergyBurned")),
        "active_energy_goal": normalize.to_float(a.get("activeEnergyBurnedGoal")),
        "active_energy_unit": a.get("activeEnergyBurnedUnit"),
        "exercise_minutes": normalize.to_float(a.get("appleExerciseTime")),
        "exercise_goal": normalize.to_float(a.get("appleExerciseTimeGoal")),
        "stand_hours": normalize.to_float(a.get("appleStandHours")),
        "stand_goal": normalize.to_float(a.get("appleStandHoursGoal")),
    }


def _clinical_payload(elem: ET.Element) -> dict:
    a = elem.attrib
    return {
        "type": normalize.normalize_type(a.get("type")),
        "raw_type": a.get("type"),
        "identifier": a.get("identifier"),
        "source_name": a.get("sourceName"),
        "fhir_version": a.get("fhirVersion"),
        "received": normalize.parse_ts(a.get("receivedDate")),
    }


def _iterparse(source):
    """iterparse over a path OR an already-open binary file object.

    The file-object form lets callers stream straight out of the export .zip
    (zipfile.ZipFile.open) without materialising the 1+ GB export.xml on disk.
    """
    if hasattr(source, "read"):
        return ET.iterparse(source, events=("start", "end"))
    return ET.iterparse(str(Path(source)), events=("start", "end"))


def iter_export(xml_path) -> Iterator[Tuple[str, dict]]:
    """Single-pass streaming parse. Yields (kind, payload) tuples.

    ``xml_path`` may be a path or an open binary file object.

    Tolerant of a truncated/malformed file: parsing stops cleanly at the point
    of corruption and whatever was parsed before it is still yielded.
    """
    context = _iterparse(xml_path)
    root = None
    try:
        for event, elem in context:
            if event == "start":
                # The very first start event is the document root (<HealthData>);
                # keep it so we can detach processed children for memory reuse.
                if root is None:
                    root = elem
                continue
            tag = elem.tag
            if tag not in TOP_LEVEL_TAGS:
                continue

            if tag == "Record":
                rec = _record_payload(elem)
                if rec["type"] == "sleep_analysis":
                    yield "sleep", _sleep_payload(rec, elem)
                yield "record", rec
            elif tag == "Workout":
                wk = _workout_payload(elem)
                yield "workout", wk
                # Structural children (repetitions + segment boundaries). Built
                # into a bounded list before the element is cleared below.
                for structural in workout_event_payloads(elem, wk):
                    yield "workout_event", structural
            elif tag == "ActivitySummary":
                yield "activity_summary", _activity_summary_payload(elem)
            elif tag == "ClinicalRecord":
                yield "clinical", _clinical_payload(elem)

            # Reclaim memory: clear the element and detach processed siblings.
            elem.clear()
            if root is not None and elem is not root:
                # Remove already-processed top-level children from the root so
                # they don't accumulate. Direct children of <HealthData>.
                for child in list(root):
                    if child is not elem:
                        root.remove(child)
    except ET.ParseError as exc:
        # Malformed/partial export: stop gracefully, keep what we parsed.
        import sys

        print(
            f"[apple-health] WARNING: XML parse stopped early ({exc}); "
            "imported all records up to the corruption point.",
            file=sys.stderr,
        )
        return


def iter_workouts_only(source) -> Iterator[Tuple[str, dict]]:
    """Like ``iter_export`` but yields only workouts and their structure.

    Used by scripts/backfill_workout_events.py: populating `workout_events` on
    an existing database must not re-parse and re-hash five million <Record>
    payloads. Same streaming/flat-memory discipline as ``iter_export`` — depth
    tracking identifies direct children of <HealthData> so each one is detached
    from the root as soon as it ends, and everything that is not a <Workout> is
    dropped without building a payload.
    """
    context = _iterparse(source)
    root = None
    depth = 0
    try:
        for event, elem in context:
            if event == "start":
                if root is None:
                    root = elem
                depth += 1
                continue
            # `elem` just ended; its own depth is the pre-decrement value.
            elem_depth = depth
            depth -= 1
            if elem_depth != 2:
                # The root itself, or a nested element that will be freed when
                # its top-level ancestor is cleared below.
                continue
            if elem.tag == "Workout":
                wk = _workout_payload(elem)
                yield "workout", wk
                for structural in workout_event_payloads(elem, wk):
                    yield "workout_event", structural
            elem.clear()
            if root is not None and elem is not root:
                root.remove(elem)
    except ET.ParseError as exc:
        import sys

        print(
            f"[apple-health] WARNING: XML parse stopped early ({exc}).",
            file=sys.stderr,
        )
        return
