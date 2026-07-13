"""Streaming parser for Apple Health export.xml.

Uses ElementTree.iterparse (NOT parse) so that 200-800 MB exports with
millions of <Record> elements are processed incrementally with a flat memory
profile. Top-level elements are cleared and detached from the root as soon as
they are consumed.

The public entry point is ``iter_export`` which yields ``(kind, payload)``
tuples in a single pass over the file. ``kind`` is one of:
    "record", "workout", "activity_summary", "clinical", "sleep"
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
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


def iter_export(xml_path: str | Path) -> Iterator[Tuple[str, dict]]:
    """Single-pass streaming parse. Yields (kind, payload) tuples.

    Tolerant of a truncated/malformed file: parsing stops cleanly at the point
    of corruption and whatever was parsed before it is still yielded.
    """
    xml_path = Path(xml_path)
    context = ET.iterparse(str(xml_path), events=("start", "end"))
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
                yield "workout", _workout_payload(elem)
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
