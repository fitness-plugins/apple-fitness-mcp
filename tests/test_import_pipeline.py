"""Tests for the archive-level import path: streaming, fingerprinting, stats.

Synthetic archives only — no real personal data. These cover the three things
`import_pipeline` is responsible for that the parser tests do not touch:

  * the export XML is read *out of* the .zip, nothing is unpacked to disk;
  * the "have I already imported this?" fingerprint is content-based, and an
    `import_state.json` written by an older version migrates cleanly;
  * `seen` (parsed) is reported next to `added` (inserted).
"""
from __future__ import annotations

import json
import os
import zipfile
from pathlib import Path

import pytest

from apple_health_mcp import config, import_pipeline

EXPORT_XML = """<?xml version="1.0" encoding="UTF-8"?>
<HealthData locale="en_US">
 <ExportDate value="2024-06-01 09:00:00 -0700"/>
 <Record type="HKQuantityTypeIdentifierStepCount" sourceName="iPhone" unit="count" value="500" startDate="2024-05-01 08:00:00 -0700" endDate="2024-05-01 08:10:00 -0700" creationDate="2024-05-01 08:10:00 -0700"/>
 <Record type="HKQuantityTypeIdentifierStepCount" sourceName="iPhone" unit="count" value="300" startDate="2024-05-01 09:00:00 -0700" endDate="2024-05-01 09:10:00 -0700" creationDate="2024-05-01 09:10:00 -0700"/>
 <Record type="HKCategoryTypeIdentifierSleepAnalysis" sourceName="Watch" value="HKCategoryValueSleepAnalysisAsleepDeep" startDate="2024-05-01 23:30:00 -0700" endDate="2024-05-02 00:30:00 -0700" creationDate="2024-05-02 07:00:00 -0700"/>
 <Workout workoutActivityType="HKWorkoutActivityTypeRunning" sourceName="Watch" duration="30" durationUnit="min" startDate="2024-05-03 06:00:00 -0700" endDate="2024-05-03 06:30:00 -0700"/>
</HealthData>
"""

# Deliberately not well-formed: if anything ever parses export_cda.xml, these
# tests fail loudly instead of silently paying for it.
CDA_XML = "<ClinicalDocument> this is not valid XML < & > "


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    """Point config at a throwaway DB + export dir under tmp_path."""
    db = tmp_path / "health.duckdb"
    exp = tmp_path / "AppleHealthExport"
    exp.mkdir()
    monkeypatch.setattr(config, "DB_PATH", db)
    monkeypatch.setattr(config, "EXPORT_DIR", exp)
    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    monkeypatch.setattr(config, "IMPORT_STATE_PATH", tmp_path / "import_state.json")
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    return tmp_path


def _write_archive(folder: Path, xml: str = EXPORT_XML, name: str = "export.zip",
                   *, extras: bool = True) -> Path:
    """A realistically-shaped export: export.xml plus the ballast Apple ships."""
    archive = folder / name
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        if extras:
            zf.writestr("apple_health_export/export_cda.xml", CDA_XML)
            zf.writestr("apple_health_export/workout-routes/route_1.gpx", "<gpx/>")
            zf.writestr("apple_health_export/workout-routes/route_2.gpx", "<gpx/>")
        zf.writestr("apple_health_export/export.xml", xml)
    return archive


# --- streaming: nothing is unpacked ------------------------------------------

def test_import_never_extracts_the_archive(sandbox, monkeypatch):
    """The 1.7 GB unpack is gone: export.xml is streamed out of the zip.

    `extractall` and `extract` are booby-trapped, so any reintroduction of the
    unpack-to-tempdir path fails this test rather than quietly costing a
    gigabyte of disk and the I/O to write it.
    """
    def _boom(*args, **kwargs):
        raise AssertionError("the importer must not extract the archive")

    monkeypatch.setattr(zipfile.ZipFile, "extractall", _boom)
    monkeypatch.setattr(zipfile.ZipFile, "extract", _boom)

    archive = _write_archive(config.EXPORT_DIR)
    stats = import_pipeline.import_archive(archive)
    assert stats is not None
    assert stats["totals"]["records"] == 3


def test_export_cda_is_never_opened(sandbox):
    """export_cda.xml (456 MB in a real export) is not read, only skipped.

    It is unparseable in this fixture, so an import that succeeds proves it was
    never fed to the parser.
    """
    archive = _write_archive(config.EXPORT_DIR)
    with zipfile.ZipFile(archive) as zf:
        assert import_pipeline._find_export_entry(zf) == \
            "apple_health_export/export.xml"
    stats = import_pipeline.import_archive(archive)
    assert stats is not None and stats["totals"]["records"] == 3


def test_find_export_entry_prefers_the_shallowest_match(tmp_path):
    archive = tmp_path / "nested.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("a/b/c/export.xml", "<HealthData/>")
        zf.writestr("export.xml", "<HealthData/>")
        zf.writestr("a/export_cda.xml", "<x/>")
    with zipfile.ZipFile(archive) as zf:
        assert import_pipeline._find_export_entry(zf) == "export.xml"


def test_missing_export_xml_is_reported(sandbox):
    archive = config.EXPORT_DIR / "export.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("apple_health_export/export_cda.xml", CDA_XML)
    assert import_pipeline.import_archive(archive) is None
    assert import_pipeline.reload()["status"] == "error"


# --- fingerprint: content, not mtime -----------------------------------------

def test_fingerprint_is_content_based(sandbox):
    archive = _write_archive(config.EXPORT_DIR)
    fp = import_pipeline._archive_fingerprint(archive)
    assert fp.startswith("crc1:")

    # A touch / re-sync / AirDrop changes mtime but not content.
    os.utime(archive, (1, 1))
    assert import_pipeline._archive_fingerprint(archive) == fp

    # A byte-identical copy under a different name is the same export.
    copy = config.EXPORT_DIR / "export-2.zip"
    copy.write_bytes(archive.read_bytes())
    os.utime(copy, (99999, 99999))
    assert import_pipeline._archive_fingerprint(copy) == fp

    # Different content -> different fingerprint.
    other = _write_archive(config.EXPORT_DIR, EXPORT_XML.replace("500", "600"),
                           name="other.zip")
    assert import_pipeline._archive_fingerprint(other) != fp


def test_touching_the_archive_does_not_force_a_reimport(sandbox):
    archive = _write_archive(config.EXPORT_DIR)
    assert import_pipeline.import_archive(archive) is not None
    os.utime(archive, (1, 1))          # the old name|size|mtime scheme broke here
    assert import_pipeline.import_archive(archive) is None
    assert import_pipeline.reload()["status"] == "already_current"


def test_non_zip_still_gets_a_fingerprint_and_reports_an_error(sandbox):
    bad = config.EXPORT_DIR / "broken.zip"
    bad.write_bytes(b"not a zip file")
    fp = import_pipeline._archive_fingerprint(bad)
    assert fp.startswith("stat1:")     # falls back, does not raise
    assert import_pipeline.reload()["status"] == "error"


# --- fingerprint: migrating an existing import_state.json --------------------

def test_legacy_state_migrates_without_reimporting(sandbox):
    """An `import_state.json` from the old scheme must not re-parse everything.

    The stored value is a bare md5 of name|size|mtime. It cannot equal a CRC
    fingerprint, so a naive comparison would re-parse millions of records once.
    The legacy value is recognized, the import is skipped, and the file is
    rewritten in the new scheme.
    """
    archive = _write_archive(config.EXPORT_DIR)
    legacy = import_pipeline._legacy_fingerprint(archive)
    assert ":" not in legacy
    config.IMPORT_STATE_PATH.write_text(
        json.dumps({"last_fingerprint": legacy, "last_archive": archive.name})
    )

    assert import_pipeline.import_archive(archive) is None      # skipped

    state = json.loads(config.IMPORT_STATE_PATH.read_text())
    assert state["last_fingerprint"] == import_pipeline._archive_fingerprint(archive)
    assert state["last_fingerprint"].startswith("crc1:")
    # Migrated once and for all — the legacy branch is not taken again.
    assert import_pipeline.reload()["status"] == "already_current"


def test_legacy_state_for_a_different_archive_reimports(sandbox):
    """A legacy fingerprint that does not match falls through to a real import.

    Re-importing is the safe direction: it is idempotent (row hashes insert
    nothing new) and costs only time, whereas a spurious skip would leave new
    data out of the database with nothing downstream to notice.
    """
    archive = _write_archive(config.EXPORT_DIR)
    config.IMPORT_STATE_PATH.write_text(
        json.dumps({"last_fingerprint": "0" * 32, "last_archive": "old.zip"})
    )
    stats = import_pipeline.import_archive(archive)
    assert stats is not None
    assert stats["totals"]["records"] == 3


def test_unknown_tagged_scheme_reimports(sandbox):
    """A tagged fingerprint we do not recognize is a mismatch, not a migration."""
    archive = _write_archive(config.EXPORT_DIR)
    config.IMPORT_STATE_PATH.write_text(
        json.dumps({"last_fingerprint": "crc99:deadbeef:1"})
    )
    assert import_pipeline.import_archive(archive) is not None


# --- reporting: parsed next to inserted --------------------------------------

def test_stats_report_seen_next_to_added(sandbox):
    archive = _write_archive(config.EXPORT_DIR)
    stats = import_pipeline.import_archive(archive)
    assert stats["seen"]["record"] == 3
    assert stats["seen"]["sleep"] == 1
    assert stats["seen"]["workout"] == 1
    assert stats["added"]["records"] == 3


def test_reload_surfaces_seen(sandbox):
    archive = _write_archive(config.EXPORT_DIR)
    first = import_pipeline.reload()
    assert first["status"] == "imported"
    assert first["seen"]["record"] == 3
    assert first["added"]["records"] == 3
    assert "parsed 3 records" in first["message"]
    assert "inserted 3 new" in first["message"]

    # The point of showing both: a re-import parses everything and inserts
    # nothing.
    again = import_pipeline.reload(force=True)
    assert again["seen"]["record"] == 3
    assert again["added"]["records"] == 0
    assert "parsed 3 records" in again["message"]
    assert "inserted 0 new" in again["message"]
