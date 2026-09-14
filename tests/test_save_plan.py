"""Tests for the save_weekly_plan / get_weekly_plan write tool.

These exercise only the plan file I/O and validation — no DB, no parser. The
output path is driven through config.PLAN_OUTPUT_PATH (overridable via
HEALTH_PLAN_PATH) at a tmp file, matching the sandbox convention in the other
test modules.
"""
from __future__ import annotations

import json
import uuid

import pytest

from apple_health_mcp import config, server


@pytest.fixture
def plan_path(tmp_path, monkeypatch):
    """Point the tool's output at a tmp plan.json (the tool creates parent dirs)."""
    p = tmp_path / "plans" / "plan.json"
    monkeypatch.setattr(config, "PLAN_OUTPUT_PATH", p)
    return p


def _valid_plan(**over) -> dict:
    plan = {
        "schema_version": 1,
        "week_of": "2026-07-13",
        "generated_by": "apple-fitness-mcp",
        "planned": [
            {
                "id": "mon-easy-z2",
                "title": "Easy Z2 run",
                "day": "monday",
                "constraints": {
                    "workout_type": {"any_of": ["outdoor_running"]},
                    "avg_hr": {"min": 120, "max": 145},
                },
            },
            {
                "id": "sat-long",
                "title": "Long run",
                "day": "saturday",
                "constraints": {"distance_km": {"min": 15}},
            },
        ],
    }
    plan.update(over)
    return plan


def test_autofills_plan_id_and_generated_at(plan_path):
    res = server.save_weekly_plan(_valid_plan())
    assert res["status"] == "saved"
    filled = res["plan_json"]
    # plan_id is a valid UUID, generated_at is a UTC ISO-8601 stamp.
    uuid.UUID(filled["plan_id"])
    assert filled["generated_at"].endswith("Z")
    assert res["plan_id"] == filled["plan_id"]
    assert res["week_of"] == "2026-07-13"
    assert res["planned_count"] == 2
    assert res["path"] == str(plan_path)


def test_provided_plan_id_is_preserved(plan_path):
    pid = str(uuid.uuid4())
    res = server.save_weekly_plan(_valid_plan(plan_id=pid))
    assert res["status"] == "saved"
    assert res["plan_id"] == pid
    assert res["plan_json"]["plan_id"] == pid


def test_write_is_atomic_and_rereadable(plan_path):
    res = server.save_weekly_plan(_valid_plan())
    assert res["status"] == "saved"
    # File exists and round-trips to identical JSON as the returned plan_json.
    assert plan_path.exists()
    on_disk = json.loads(plan_path.read_text(encoding="utf-8"))
    assert on_disk == res["plan_json"]
    # No leftover temp files in the directory.
    assert [q.name for q in plan_path.parent.iterdir()] == ["plan.json"]
    # get_weekly_plan reads it back.
    got = server.get_weekly_plan()
    assert got["status"] == "saved"
    assert got["plan_json"] == on_disk


def test_accepts_json_string(plan_path):
    res = server.save_weekly_plan(json.dumps(_valid_plan()))
    assert res["status"] == "saved"
    assert res["planned_count"] == 2


def test_missing_week_of_is_invalid(plan_path):
    plan = _valid_plan()
    del plan["week_of"]
    res = server.save_weekly_plan(plan)
    assert res["status"] == "invalid"
    assert any("week_of" in e for e in res["errors"])
    assert not plan_path.exists()  # nothing written


def test_empty_planned_is_invalid(plan_path):
    res = server.save_weekly_plan(_valid_plan(planned=[]))
    assert res["status"] == "invalid"
    assert any("planned" in e for e in res["errors"])
    assert not plan_path.exists()


def test_planned_item_without_id_is_invalid(plan_path):
    res = server.save_weekly_plan(
        _valid_plan(planned=[{"constraints": {"distance_km": {"min": 5}}}]))
    assert res["status"] == "invalid"
    assert any("id" in e for e in res["errors"])
    assert not plan_path.exists()


def test_duplicate_ids_are_invalid(plan_path):
    dup = [
        {"id": "x", "constraints": {}},
        {"id": "x", "constraints": {}},
    ]
    res = server.save_weekly_plan(_valid_plan(planned=dup))
    assert res["status"] == "invalid"
    assert any("unique" in e or "duplicat" in e for e in res["errors"])
    assert not plan_path.exists()


def test_bad_day_is_invalid(plan_path):
    bad = [{"id": "x", "day": "funday", "constraints": {}}]
    res = server.save_weekly_plan(_valid_plan(planned=bad))
    assert res["status"] == "invalid"
    assert any("day" in e for e in res["errors"])


def test_resaving_same_plan_id_is_idempotent(plan_path):
    first = server.save_weekly_plan(_valid_plan())
    pid = first["plan_id"]
    gen = first["plan_json"]["generated_at"]
    # Re-save the exact filled plan (same plan_id + generated_at) -> identical file.
    second = server.save_weekly_plan(first["plan_json"])
    assert second["status"] == "saved"
    assert second["plan_id"] == pid
    assert json.loads(plan_path.read_text(encoding="utf-8")) == first["plan_json"]
    assert second["plan_json"]["generated_at"] == gen


def test_get_weekly_plan_none_when_absent(plan_path):
    assert not plan_path.exists()
    assert server.get_weekly_plan()["status"] == "none"
