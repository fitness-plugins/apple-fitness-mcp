"""The NDJSON delta wire format must produce the parser's payloads exactly.

The whole point of the adapter is that `storage.import_stream` sees one shape,
whether a row came from a 1.15 GB export.xml or from a few kilobytes the phone
pushed. So this file states the same handful of samples twice — once as Apple's
XML, once as the NDJSON the iOS app sends — parses both, and demands the
payloads match field for field.

Divergences that are *by design* are asserted explicitly rather than ignored:
a quantity record's `value_str` is NULL on the delta path (the numeric value is
identity after the row-hash migration; reproducing Apple's string formatting is
exactly what that migration removed the need for).
"""
from __future__ import annotations

import json

import pytest

from apple_health_mcp import sync_protocol

# Non-breaking space (U+00A0) inside the source name, and U+00B7 in a unit:
# both appear verbatim in the real export and both are inside a row hash, so
# nothing anywhere may normalize them.
NBSP_SOURCE = "Apple Watch — Maksim"
SOURCE = "Maksim's Apple Watch"

EXPORT_XML = f"""<?xml version="1.0" encoding="UTF-8"?>
<HealthData locale="en_US">
 <ExportDate value="2026-08-20 15:00:00 +0300"/>
 <Record type="HKQuantityTypeIdentifierHeartRate" sourceName="{NBSP_SOURCE}" sourceVersion="26.6" device="&lt;&lt;HKDevice: 0x1&gt;&gt;" unit="count/min" creationDate="2026-08-20 13:40:02 +0300" startDate="2026-08-20 13:39:37 +0300" endDate="2026-08-20 13:39:37 +0300" value="172"/>
 <Record type="HKQuantityTypeIdentifierPhysicalEffort" sourceName="{SOURCE}" sourceVersion="26.6" unit="kcal/hr&#183;kg" creationDate="2026-08-20 13:41:00 +0300" startDate="2026-08-20 13:40:00 +0300" endDate="2026-08-20 13:41:00 +0300" value="11.8638"/>
 <Record type="HKCategoryTypeIdentifierSleepAnalysis" sourceName="{SOURCE}" sourceVersion="26.6" creationDate="2026-08-20 07:00:00 +0300" startDate="2026-08-20 01:12:00 +0300" endDate="2026-08-20 02:04:30 +0300" value="HKCategoryValueSleepAnalysisAsleepCore"/>
 <Workout workoutActivityType="HKWorkoutActivityTypeRunning" duration="50.48292259971301" durationUnit="min" sourceName="{SOURCE}" sourceVersion="26.6" creationDate="2026-08-20 14:21:35 +0300" startDate="2026-08-20 13:30:59 +0300" endDate="2026-08-20 14:21:28 +0300" totalDistance="8.741" totalDistanceUnit="km" totalEnergyBurned="666.3" totalEnergyBurnedUnit="kcal">
  <MetadataEntry key="HKIndoorWorkout" value="0"/>
  <WorkoutEvent type="HKWorkoutEventTypeSegment" date="2026-08-20 13:30:59 +0300" duration="5.541540004809698" durationUnit="min"/>
  <WorkoutEvent type="HKWorkoutEventTypeMarker" date="2026-08-20 13:40:00 +0300"/>
  <WorkoutActivity uuid="D98A8A68-6D91-4EE0-B873-0F3028FB6CA8" startDate="2026-08-20 13:30:59 +0300" endDate="2026-08-20 13:39:37 +0300" duration="8.641769770781199" durationUnit="min">
   <WorkoutEvent type="HKWorkoutEventTypeSegment" date="2026-08-20 13:30:59 +0300" duration="5.541540004809698" durationUnit="min"/>
   <WorkoutStatistics type="HKQuantityTypeIdentifierDistanceWalkingRunning" startDate="2026-08-20 13:30:59 +0300" endDate="2026-08-20 13:39:37 +0300" sum="1.50851" unit="km"/>
   <WorkoutStatistics type="HKQuantityTypeIdentifierHeartRate" startDate="2026-08-20 13:30:59 +0300" endDate="2026-08-20 13:39:37 +0300" average="164.01" minimum="132" maximum="172" unit="count/min"/>
   <MetadataEntry key="WOIntervalStepKeyPath" value="0.0.0"/>
   <MetadataEntry key="HKElevationAscended" value="262 cm"/>
   <MetadataEntry key="WOIntervalStepSuccessful" value="1"/>
  </WorkoutActivity>
  <WorkoutActivity uuid="C8F129F7-4694-4619-BE8D-E05E23FD5B98" startDate="2026-08-20 13:39:37 +0300" endDate="2026-08-20 13:44:38 +0300" duration="5.021404461065928" durationUnit="min">
   <MetadataEntry key="WOIntervalStepKeyPath" value="1.0.0"/>
   <MetadataEntry key="WOIntervalStepSuccessful" value="0"/>
  </WorkoutActivity>
  <WorkoutStatistics type="HKQuantityTypeIdentifierHeartRate" startDate="2026-08-20 13:30:59 +0300" endDate="2026-08-20 14:21:28 +0300" average="171" minimum="98" maximum="197" unit="count/min"/>
 </Workout>
</HealthData>
"""

_WORKOUT_REF = {"raw_type": "HKWorkoutActivityTypeRunning", "source_name": SOURCE,
                "start": "2026-08-20T13:30:59+03:00",
                "end": "2026-08-20T14:21:28+03:00"}

# The same samples as the iOS app sends them: raw HealthKit identifiers, numeric
# values, ISO-8601 timestamps with an offset.
NDJSON_LINES = [
    {"kind": "record", "raw_type": "HKQuantityTypeIdentifierHeartRate",
     "source_name": NBSP_SOURCE, "source_version": "26.6",
     "device": "<<HKDevice: 0x1>>", "unit": "count/min", "value": 172.0,
     "value_str": None, "start": "2026-08-20T13:39:37+03:00",
     "end": "2026-08-20T13:39:37+03:00", "created": "2026-08-20T13:40:02+03:00"},
    {"kind": "record", "raw_type": "HKQuantityTypeIdentifierPhysicalEffort",
     "source_name": SOURCE, "source_version": "26.6", "device": None,
     "unit": "kcal/hr·kg", "value": 11.8638, "value_str": None,
     "start": "2026-08-20T13:40:00+03:00", "end": "2026-08-20T13:41:00+03:00",
     "created": "2026-08-20T13:41:00+03:00"},
    {"kind": "record", "raw_type": "HKCategoryTypeIdentifierSleepAnalysis",
     "source_name": SOURCE, "source_version": "26.6", "device": None,
     "unit": None, "value": None,
     "value_str": "HKCategoryValueSleepAnalysisAsleepCore",
     "start": "2026-08-20T01:12:00+03:00", "end": "2026-08-20T02:04:30+03:00",
     "created": "2026-08-20T07:00:00+03:00"},
    {"kind": "workout", "raw_type": "HKWorkoutActivityTypeRunning",
     "source_name": SOURCE, "source_version": "26.6", "device": None,
     "duration": 50.48292259971301, "duration_unit": "min",
     "distance": 8.741, "distance_unit": "km",
     "energy": 666.3, "energy_unit": "kcal", "avg_hr": 171.0, "max_hr": 197.0,
     "start": "2026-08-20T13:30:59+03:00", "end": "2026-08-20T14:21:28+03:00"},
    {"kind": "workout_event", "workout": _WORKOUT_REF,
     "raw_event_type": "HKWorkoutEventTypeSegment",
     "date": "2026-08-20T13:30:59+03:00", "duration": 5.541540004809698,
     "duration_unit": "min", "step_index": 0, "activity_uuid": None},
    {"kind": "workout_event", "workout": _WORKOUT_REF,
     "raw_event_type": "HKWorkoutEventTypeMarker",
     "date": "2026-08-20T13:40:00+03:00", "duration": None,
     "duration_unit": None, "step_index": 1, "activity_uuid": None},
    {"kind": "workout_activity", "workout": _WORKOUT_REF,
     "activity_uuid": "D98A8A68-6D91-4EE0-B873-0F3028FB6CA8",
     "start": "2026-08-20T13:30:59+03:00", "end": "2026-08-20T13:39:37+03:00",
     "duration": 8.641769770781199, "duration_unit": "min", "step_index": 0,
     "metadata": {"WOIntervalStepKeyPath": "0.0.0",
                  "HKElevationAscended": "262 cm",
                  "WOIntervalStepSuccessful": "1"},
     "statistics": [
         {"raw_type": "HKQuantityTypeIdentifierDistanceWalkingRunning",
          "start": "2026-08-20T13:30:59+03:00", "end": "2026-08-20T13:39:37+03:00",
          "sum": 1.50851, "average": None, "minimum": None, "maximum": None,
          "unit": "km"},
         {"raw_type": "HKQuantityTypeIdentifierHeartRate",
          "start": "2026-08-20T13:30:59+03:00", "end": "2026-08-20T13:39:37+03:00",
          "sum": None, "average": 164.01, "minimum": 132, "maximum": 172,
          "unit": "count/min"}]},
    {"kind": "workout_event", "workout": _WORKOUT_REF,
     "raw_event_type": "HKWorkoutEventTypeSegment",
     "date": "2026-08-20T13:30:59+03:00", "duration": 5.541540004809698,
     "duration_unit": "min", "step_index": 0,
     "activity_uuid": "D98A8A68-6D91-4EE0-B873-0F3028FB6CA8"},
    {"kind": "workout_activity", "workout": _WORKOUT_REF,
     "activity_uuid": "C8F129F7-4694-4619-BE8D-E05E23FD5B98",
     "start": "2026-08-20T13:39:37+03:00", "end": "2026-08-20T13:44:38+03:00",
     "duration": 5.021404461065928, "duration_unit": "min", "step_index": 1,
     "metadata": {"WOIntervalStepKeyPath": "1.0.0",
                  "WOIntervalStepSuccessful": "0"},
     "statistics": []},
]


@pytest.fixture()
def xml_file(tmp_path):
    path = tmp_path / "export.xml"
    path.write_text(EXPORT_XML, encoding="utf-8")
    return path


def _xml_events(xml_file):
    from apple_health_mcp import parser
    return list(parser.iter_export(xml_file))


def _wire_events():
    return list(sync_protocol.iter_events(
        json.dumps(line) for line in NDJSON_LINES))


def _by_kind(events, kind):
    return [p for k, p in events if k == kind]


def test_same_kinds_in_the_same_order(xml_file):
    """The wire yields the same event stream the XML parser yields."""
    xml_kinds = [k for k, _ in _xml_events(xml_file)]
    wire_kinds = [k for k, _ in _wire_events()]
    assert wire_kinds == xml_kinds


def test_workout_payload_matches_xml(xml_file):
    assert _by_kind(_wire_events(), "workout") == \
        _by_kind(_xml_events(xml_file), "workout")


def test_workout_structure_matches_xml_field_for_field(xml_file):
    """Repetitions and segment boundaries — including the stats_json blob.

    workout_events rows are what the interval analytics read, so a delta-synced
    structured session has to be indistinguishable from the XML-imported one.
    """
    xml_rows = _by_kind(_xml_events(xml_file), "workout_event")
    wire_rows = _by_kind(_wire_events(), "workout_event")
    assert len(wire_rows) == len(xml_rows) == 5
    for wire, xml in zip(wire_rows, xml_rows):
        assert wire == xml
    kinds = [r["event_kind"] for r in wire_rows]
    assert kinds == ["event", "event", "activity", "activity_event", "activity"]


def test_sleep_is_derived_from_the_record_exactly_as_the_parser_does(xml_file):
    xml_sleep = _by_kind(_xml_events(xml_file), "sleep")
    wire_sleep = _by_kind(_wire_events(), "sleep")
    assert wire_sleep == xml_sleep
    assert wire_sleep[0]["stage"] == "core"
    assert wire_sleep[0]["raw_value"] == "HKCategoryValueSleepAnalysisAsleepCore"


def test_records_match_except_the_deliberately_null_value_str(xml_file):
    xml_records = _by_kind(_xml_events(xml_file), "record")
    wire_records = _by_kind(_wire_events(), "record")
    assert len(wire_records) == len(xml_records) == 3
    for wire, xml in zip(wire_records, xml_records):
        assert {k: v for k, v in wire.items() if k != "value_str"} == \
               {k: v for k, v in xml.items() if k != "value_str"}
    # Quantity types: the number is identity, the string is not sent.
    assert wire_records[0]["value"] == 172.0 and wire_records[0]["value_str"] is None
    assert wire_records[1]["value"] == pytest.approx(11.8638)
    # Category types: `value` is NULL and the category name IS identity, so it
    # must survive verbatim.
    assert wire_records[2]["value"] is None
    assert wire_records[2]["value_str"] == "HKCategoryValueSleepAnalysisAsleepCore"


def test_non_ascii_source_and_unit_survive_verbatim():
    """U+00A0 in sourceName and U+00B7 in the unit are inside the row hash."""
    records = _by_kind(_wire_events(), "record")
    assert records[0]["source_name"] == NBSP_SOURCE
    assert " " in records[0]["source_name"]
    assert records[1]["unit"] == "kcal/hr·kg"


def test_timestamps_are_truncated_to_whole_seconds():
    """export.xml has second precision, so the delta path matches it.

    The migrated row hash renders timestamps to the second in UTC, so a
    fraction would not duplicate the row — but it would store a different
    instant than the XML twin's, and whichever arrived first would win. Dropped
    by truncation, never rounding.
    """
    line = dict(NDJSON_LINES[0], start="2026-08-20T13:39:37.874+03:00")
    (_, payload), = sync_protocol.events_from_object(line)
    assert payload["start"].microsecond == 0
    assert payload["start"].second == 37


def test_a_timestamp_without_an_offset_is_rejected():
    """Guessing a timezone would silently move the instant, and the hash."""
    with pytest.raises(sync_protocol.WireError):
        sync_protocol.events_from_object(
            dict(NDJSON_LINES[0], start="2026-08-20T13:39:37"))


def test_zulu_and_apple_timestamp_forms_are_both_accepted():
    for form in ("2026-08-20T10:39:37Z", "2026-08-20 13:39:37 +0300"):
        (_, payload), = sync_protocol.events_from_object(
            dict(NDJSON_LINES[0], start=form))
        assert payload["start"].utcoffset() is not None


def test_a_bad_line_is_counted_and_skipped_not_raised():
    """A batch was already acknowledged to the phone; one bad line must not
    throw away the rest of it, and must not be silent either."""
    adapter = sync_protocol.BatchAdapter()
    events = list(adapter.iter_events([
        json.dumps(NDJSON_LINES[0]),
        "{not json",
        json.dumps({"kind": "record"}),               # no raw_type
        json.dumps({"kind": "activity_summary"}),     # out of scope for v1
        json.dumps({"kind": "martian"}),
        "",
        json.dumps(NDJSON_LINES[3]),
    ]))
    assert [k for k, _ in events] == ["record", "workout"]
    summary = adapter.summary()
    assert summary["skipped_lines"] == 3        # bad json, no raw_type, unknown
    assert summary["ignored_lines"] == 1        # activity_summary
    assert summary["payloads"] == 2
    assert len(summary["errors"]) == 3


def test_the_published_wire_kinds_are_the_kinds_we_actually_handle():
    """WIRE_KINDS is the contract an iOS client is written against; it may not
    drift from the adapters that implement it."""
    assert set(sync_protocol._ADAPTERS) == set(sync_protocol.WIRE_KINDS)
    assert "workout_activity" in sync_protocol.WIRE_KINDS


def test_numbers_are_rendered_the_way_the_export_writes_them():
    assert sync_protocol.num_str(132.0) == "132"
    assert sync_protocol.num_str(164.01) == "164.01"
    assert sync_protocol.num_str(5.541540004809698) == "5.541540004809698"
    assert sync_protocol.num_str(None) is None
