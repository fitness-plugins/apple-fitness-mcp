"""NDJSON delta wire format -> the exact ``(kind, payload)`` tuples the parser yields.

The iOS app (Readiness) pushes only the HealthKit samples added since its last
successful sync. Those arrive as NDJSON, one JSON object per line, each with a
``kind`` discriminator. This module is the *thin adapter* between that wire
format and ``storage.import_stream``: there is exactly one loader, so there is
exactly one set of dedup rules.

**How the shapes are guaranteed to match.** Rather than re-deriving the payload
dicts (a second implementation that would silently drift from ``parser.py``),
each line is rebuilt into the synthetic ``xml.etree`` element Apple's export
would have contained and handed to the parser's own payload builders
(``_record_payload``, ``_workout_payload``, ``_activity_payload``,
``_event_payload``). Normalization therefore stays single-sourced in
``normalize.py`` — the app sends *raw* HealthKit identifiers
(``HKQuantityTypeIdentifierHeartRate``), never normalized ones.

Wire contract v1 (an iOS client is built against this exact text):

    {"kind":"record","raw_type":"HKQuantityTypeIdentifierHeartRate",
     "source_name":"...","source_version":"26.6","device":"...|null",
     "unit":"count/min","value":172.0,"value_str":"172|null",
     "start":"2026-08-20T13:39:37+03:00","end":"...","created":"..."}
    {"kind":"workout","raw_type":"HKWorkoutActivityTypeRunning", ...}
    {"kind":"workout_activity","workout":{...},"activity_uuid":"...",
     "step_index":1,"metadata":{...},"statistics":[{...}]}
    {"kind":"workout_event","workout":{...},
     "raw_event_type":"HKWorkoutEventTypeSegment","date":"...", ...}

Two consequences of that contract worth stating plainly, because the iOS side
must agree with them:

* **Timestamps are truncated to whole seconds.** Apple's ``export.xml`` renders
  dates as ``2026-08-20 13:30:59 +0300`` — second precision, no fraction. The
  migrated row hash already renders timestamps to whole seconds in UTC, so a
  fractional second would not on its own duplicate a row; it would, however,
  leave the *stored* ``start_ts`` different from its XML twin's, with whichever
  path arrived first winning the row. The fraction is therefore dropped here
  too (truncated, not rounded), so the two ingestion paths store the same
  instant, not merely the same hash.
* **``value_str`` is not synthesized.** For a quantity type the app sends the
  number; ``value_str`` stays NULL rather than being formatted back into
  Apple's string rendering. That is the whole point of the row-hash migration
  (Part 0): identity is the numeric value. For a *category* type (sleep, stand
  hour, ...) ``value`` is NULL and ``value_str`` carries the HK category value
  name, which is still identity — the app must send it verbatim.

Sleep rows are derived here exactly as ``parser.iter_export`` derives them: a
record whose raw type is ``HKCategoryTypeIdentifierSleepAnalysis`` yields a
``sleep`` payload *and* the ``record`` payload, in that order.
"""
from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Any, Iterable, Iterator, Tuple

from . import normalize, parser

# Kinds accepted on the wire. `sleep` is not required (sleep is derived from
# sleep-analysis records, as in the XML path) but is tolerated if a client
# sends it explicitly: the row hash makes the duplicate a no-op.
WIRE_KINDS = frozenset({"record", "sleep", "workout", "workout_activity",
                        "workout_event"})

# Out of scope for wire v1 — the full export still covers these. Seen on the
# wire they are skipped and counted, never treated as an error.
IGNORED_KINDS = frozenset({"activity_summary", "clinical", "workout_route"})

_APPLE_TS = "%Y-%m-%d %H:%M:%S %z"


class WireError(ValueError):
    """A line that cannot be turned into a payload."""


# --- scalar helpers ----------------------------------------------------------

def parse_iso(value: Any) -> datetime | None:
    """ISO-8601 with offset -> aware datetime. Tolerates 'Z' and Apple's form."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        raise WireError(f"timestamp must be a string, got {type(value).__name__}")
    text = value.strip()
    candidate = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    try:
        return datetime.fromisoformat(candidate)
    except ValueError:
        pass
    dt = normalize.parse_ts(text)          # Apple's "YYYY-MM-DD HH:MM:SS +0300"
    if dt is None:
        raise WireError(f"unparseable timestamp {value!r}")
    return dt


def apple_ts(value: Any) -> str | None:
    """Render a wire timestamp the way export.xml renders it (second precision).

    Truncating rather than rounding matches ``strftime``; both paths must agree
    or the same sample hashes twice. Naive datetimes are rejected: an offset is
    mandatory on the wire, and guessing one would silently move the instant.
    """
    dt = parse_iso(value)
    if dt is None:
        return None
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        raise WireError(f"timestamp {value!r} has no UTC offset")
    return dt.replace(microsecond=0).strftime(_APPLE_TS)


def num_str(value: Any) -> str | None:
    """Number -> the string Apple would have put in the XML attribute.

    Integral floats lose the ``.0`` (``132.0`` -> ``"132"``), which is how the
    export writes whole numbers. Only ever used for values that are *not* part
    of a row hash (workout totals, statistics blobs).
    """
    if value is None:
        return None
    if isinstance(value, bool):
        raise WireError("expected a number, got a boolean")
    if isinstance(value, str):
        return value
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise WireError(f"non-finite number {value!r}")
        return str(int(value)) if value.is_integer() else repr(value)
    raise WireError(f"expected a number, got {type(value).__name__}")


def _to_float(value: Any, field: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise WireError(f"{field} must be a number, got a boolean")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        f = normalize.to_float(value)
        if f is None:
            raise WireError(f"{field} is not a number: {value!r}")
        return f
    raise WireError(f"{field} must be a number, got {type(value).__name__}")


def _to_int(value: Any, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise WireError(f"{field} must be an integer, got a boolean")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    raise WireError(f"{field} must be an integer, got {value!r}")


def _to_str(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    raise WireError(f"{field} must be a string, got {type(value).__name__}")


def _req_str(obj: dict, field: str) -> str:
    value = _to_str(obj.get(field), field)
    if not value:
        raise WireError(f"missing required field {field!r}")
    return value


def _elem(tag: str, attrs: dict[str, Any]) -> ET.Element:
    """Synthetic export element: only non-None attributes, all as strings."""
    return ET.Element(tag, {k: v for k, v in attrs.items() if v is not None})


# --- per-kind adapters -------------------------------------------------------

def _record_events(obj: dict) -> list[Tuple[str, dict]]:
    raw_type = _req_str(obj, "raw_type")
    value = _to_float(obj.get("value"), "value")
    value_str = _to_str(obj.get("value_str"), "value_str")
    # The synthetic XML attribute: a category type's value IS its string; a
    # quantity type gets a rendering only so the element is well formed — the
    # authoritative numeric `value` is written back over it below.
    xml_value = value_str if value_str is not None else num_str(value)
    elem = _elem("Record", {
        "type": raw_type,
        "sourceName": _to_str(obj.get("source_name"), "source_name"),
        "sourceVersion": _to_str(obj.get("source_version"), "source_version"),
        "device": _to_str(obj.get("device"), "device"),
        "unit": _to_str(obj.get("unit"), "unit"),
        "value": xml_value,
        "startDate": apple_ts(obj.get("start")),
        "endDate": apple_ts(obj.get("end")),
        "creationDate": apple_ts(obj.get("created")),
    })
    payload = parser._record_payload(elem)
    if value is not None:
        payload["value"] = value              # authoritative, never re-parsed
    payload["value_str"] = value_str          # NULL for quantity types by design
    out: list[Tuple[str, dict]] = []
    if payload["type"] == "sleep_analysis":
        out.append(("sleep", parser._sleep_payload(payload, elem)))
    out.append(("record", payload))
    return out


def _sleep_events(obj: dict) -> list[Tuple[str, dict]]:
    """Explicit `sleep` line (tolerated; normally derived from the record)."""
    raw_value = _to_str(obj.get("raw_value") or obj.get("value_str"), "raw_value")
    return [("sleep", {
        "source_name": _to_str(obj.get("source_name"), "source_name"),
        "device": _to_str(obj.get("device"), "device"),
        "stage": normalize.normalize_sleep_stage(raw_value),
        "raw_value": raw_value,
        "start": parse_iso(obj.get("start")),
        "end": parse_iso(obj.get("end")),
    })]


def _workout_element(obj: dict) -> ET.Element:
    elem = _elem("Workout", {
        "workoutActivityType": _req_str(obj, "raw_type"),
        "sourceName": _to_str(obj.get("source_name"), "source_name"),
        "sourceVersion": _to_str(obj.get("source_version"), "source_version"),
        "device": _to_str(obj.get("device"), "device"),
        "duration": num_str(_to_float(obj.get("duration"), "duration")),
        "durationUnit": _to_str(obj.get("duration_unit"), "duration_unit"),
        "totalDistance": num_str(_to_float(obj.get("distance"), "distance")),
        "totalDistanceUnit": _to_str(obj.get("distance_unit"), "distance_unit"),
        "totalEnergyBurned": num_str(_to_float(obj.get("energy"), "energy")),
        "totalEnergyBurnedUnit": _to_str(obj.get("energy_unit"), "energy_unit"),
        "startDate": apple_ts(obj.get("start")),
        "endDate": apple_ts(obj.get("end")),
    })
    avg_hr = _to_float(obj.get("avg_hr"), "avg_hr")
    max_hr = _to_float(obj.get("max_hr"), "max_hr")
    if avg_hr is not None or max_hr is not None:
        # The parser lifts workout HR out of <WorkoutStatistics>, not out of an
        # attribute; feed it the same shape rather than patching the payload.
        elem.append(_elem("WorkoutStatistics", {
            "type": "HKQuantityTypeIdentifierHeartRate",
            "startDate": apple_ts(obj.get("start")),
            "endDate": apple_ts(obj.get("end")),
            "average": num_str(avg_hr),
            "maximum": num_str(max_hr),
            "unit": _to_str(obj.get("hr_unit"), "hr_unit") or "count/min",
        }))
    return elem


def _workout_events(obj: dict) -> list[Tuple[str, dict]]:
    return [("workout", parser._workout_payload(_workout_element(obj)))]


def _parent_identity(obj: dict) -> dict:
    """Parent-workout identity, from the `workout` object on a child line."""
    wk = obj.get("workout")
    if not isinstance(wk, dict):
        raise WireError("missing required object field 'workout'")
    return parser._parent_identity({
        "type": normalize.normalize_type(_req_str(wk, "raw_type")),
        "source_name": _to_str(wk.get("source_name"), "workout.source_name"),
        "start": parse_iso(apple_ts(wk.get("start"))),
        "end": parse_iso(apple_ts(wk.get("end"))),
    })


def _statistics_children(items: Any) -> list[ET.Element]:
    if items is None:
        return []
    if not isinstance(items, list):
        raise WireError("'statistics' must be a list")
    out = []
    for stat in items:
        if not isinstance(stat, dict):
            raise WireError("'statistics' entries must be objects")
        out.append(_elem("WorkoutStatistics", {
            "type": _req_str(stat, "raw_type"),
            "startDate": apple_ts(stat.get("start")),
            "endDate": apple_ts(stat.get("end")),
            "sum": num_str(_to_float(stat.get("sum"), "sum")),
            "average": num_str(_to_float(stat.get("average"), "average")),
            "minimum": num_str(_to_float(stat.get("minimum"), "minimum")),
            "maximum": num_str(_to_float(stat.get("maximum"), "maximum")),
            "unit": _to_str(stat.get("unit"), "unit"),
        }))
    return out


def _metadata_children(meta: Any) -> list[ET.Element]:
    if meta is None:
        return []
    if not isinstance(meta, dict):
        raise WireError("'metadata' must be an object")
    return [_elem("MetadataEntry", {"key": str(k), "value": _to_str(v, f"metadata[{k}]")})
            for k, v in meta.items()]


def _activity_events(obj: dict) -> list[Tuple[str, dict]]:
    parent = _parent_identity(obj)
    act = _elem("WorkoutActivity", {
        "uuid": _to_str(obj.get("activity_uuid"), "activity_uuid"),
        "startDate": apple_ts(obj.get("start")),
        "endDate": apple_ts(obj.get("end")),
        "duration": num_str(_to_float(obj.get("duration"), "duration")),
        "durationUnit": _to_str(obj.get("duration_unit"), "duration_unit"),
    })
    for child in _metadata_children(obj.get("metadata")):
        act.append(child)
    for child in _statistics_children(obj.get("statistics")):
        act.append(child)
    index = _to_int(obj.get("step_index"), "step_index")
    if index is None:
        raise WireError("missing required field 'step_index'")
    return [("workout_event", parser._activity_payload(parent, act, index))]


def _event_events(obj: dict) -> list[Tuple[str, dict]]:
    parent = _parent_identity(obj)
    ev = _elem("WorkoutEvent", {
        "type": _to_str(obj.get("raw_event_type"), "raw_event_type"),
        "date": apple_ts(obj.get("date") or obj.get("start")),
        "duration": num_str(_to_float(obj.get("duration"), "duration")),
        "durationUnit": _to_str(obj.get("duration_unit"), "duration_unit"),
    })
    index = _to_int(obj.get("step_index"), "step_index")
    if index is None:
        raise WireError("missing required field 'step_index'")
    activity_uuid = _to_str(obj.get("activity_uuid"), "activity_uuid")
    # Same split the parser makes: a <WorkoutEvent> nested inside a
    # <WorkoutActivity> is an 'activity_event' and carries that activity's uuid;
    # a workout-level one is an 'event' with no uuid.
    kind = "activity_event" if activity_uuid else "event"
    return [("workout_event", parser._event_payload(parent, ev, kind, index,
                                                    activity_uuid))]


_ADAPTERS = {
    "record": _record_events,
    "sleep": _sleep_events,
    "workout": _workout_events,
    "workout_activity": _activity_events,
    "workout_event": _event_events,
}


def events_from_object(obj: Any) -> list[Tuple[str, dict]]:
    """One decoded NDJSON object -> zero or more (kind, payload) tuples."""
    if not isinstance(obj, dict):
        raise WireError("each NDJSON line must be a JSON object")
    kind = obj.get("kind")
    if not isinstance(kind, str) or not kind:
        raise WireError("missing 'kind' discriminator")
    if kind in IGNORED_KINDS:
        return []
    adapter = _ADAPTERS.get(kind)
    if adapter is None:
        raise WireError(f"unknown kind {kind!r}")
    return adapter(obj)


def check_object(obj: Any) -> str | None:
    """Cheap structural check used by the receiver before it spools a batch.

    Deliberately shallow: the receiver's job is to durably store what the phone
    sent, not to be the last word on payload validity. A line that is not an
    object, or has no `kind`, is a protocol error worth a 400; an unfamiliar
    `kind` is not (it may be a newer client) and is left for import time.
    """
    if not isinstance(obj, dict):
        return "each NDJSON line must be a JSON object"
    kind = obj.get("kind")
    if not isinstance(kind, str) or not kind:
        return "missing 'kind' discriminator"
    return None


class BatchAdapter:
    """Streams NDJSON lines as (kind, payload) tuples, counting what it saw.

    Bad lines never abort an import: they are counted and reported (the batch
    was already acknowledged to the phone, so dropping the whole thing would
    lose data that HealthKit will not hand over again). `errors` keeps the first
    few messages, with line numbers, for `import_from_app` to surface.
    """

    MAX_REPORTED_ERRORS = 10

    def __init__(self) -> None:
        self.lines = 0
        self.emitted = 0
        self.skipped = 0
        self.ignored = 0
        self.kinds: dict[str, int] = {}
        self.errors: list[str] = []

    def _note_error(self, lineno: int, exc: Exception) -> None:
        self.skipped += 1
        if len(self.errors) < self.MAX_REPORTED_ERRORS:
            self.errors.append(f"line {lineno}: {exc}")

    def iter_events(self, lines: Iterable[str | bytes | dict]
                    ) -> Iterator[Tuple[str, dict]]:
        for lineno, line in enumerate(lines, start=1):
            if isinstance(line, (bytes, bytearray)):
                line = line.decode("utf-8", errors="replace")
            if isinstance(line, str):
                text = line.strip()
                if not text:
                    continue
                try:
                    obj = json.loads(text)
                except ValueError as exc:
                    self.lines += 1
                    self._note_error(lineno, exc)
                    continue
            else:
                obj = line
            self.lines += 1
            kind = obj.get("kind") if isinstance(obj, dict) else None
            if isinstance(kind, str):
                self.kinds[kind] = self.kinds.get(kind, 0) + 1
            try:
                events = events_from_object(obj)
            except WireError as exc:
                self._note_error(lineno, exc)
                continue
            except Exception as exc:                      # defensive: never abort
                self._note_error(lineno, exc)
                continue
            if not events:
                self.ignored += 1
                continue
            for event in events:
                self.emitted += 1
                yield event

    def summary(self) -> dict:
        return {"lines": self.lines, "payloads": self.emitted,
                "skipped_lines": self.skipped, "ignored_lines": self.ignored,
                "kinds": dict(self.kinds), "errors": list(self.errors)}


def iter_events(lines: Iterable[str | bytes | dict]) -> Iterator[Tuple[str, dict]]:
    """Convenience wrapper when the caller does not need the counters."""
    return BatchAdapter().iter_events(lines)
