"""Normalization of HealthKit identifiers, units, and timestamps."""
from __future__ import annotations

import re
from datetime import datetime

# Explicit, friendly names for the metrics people actually ask about.
# Anything not listed falls back to a generic prefix-strip + snake_case.
TYPE_ALIASES = {
    "HKQuantityTypeIdentifierStepCount": "step_count",
    "HKQuantityTypeIdentifierHeartRate": "heart_rate",
    "HKQuantityTypeIdentifierRestingHeartRate": "resting_heart_rate",
    "HKQuantityTypeIdentifierWalkingHeartRateAverage": "walking_heart_rate_average",
    "HKQuantityTypeIdentifierHeartRateVariabilitySDNN": "hrv",
    "HKQuantityTypeIdentifierHeartRateRecoveryOneMinute": "heart_rate_recovery",
    "HKQuantityTypeIdentifierBodyMass": "weight",
    "HKQuantityTypeIdentifierBodyMassIndex": "bmi",
    "HKQuantityTypeIdentifierBodyFatPercentage": "body_fat_percentage",
    "HKQuantityTypeIdentifierLeanBodyMass": "lean_body_mass",
    "HKQuantityTypeIdentifierHeight": "height",
    "HKQuantityTypeIdentifierVO2Max": "vo2max",
    "HKQuantityTypeIdentifierActiveEnergyBurned": "active_energy",
    "HKQuantityTypeIdentifierBasalEnergyBurned": "basal_energy",
    "HKQuantityTypeIdentifierDistanceWalkingRunning": "distance_walking_running",
    "HKQuantityTypeIdentifierDistanceCycling": "distance_cycling",
    "HKQuantityTypeIdentifierDistanceSwimming": "distance_swimming",
    "HKQuantityTypeIdentifierFlightsClimbed": "flights_climbed",
    "HKQuantityTypeIdentifierAppleExerciseTime": "exercise_time",
    "HKQuantityTypeIdentifierAppleStandTime": "stand_time",
    "HKQuantityTypeIdentifierAppleWalkingSteadiness": "walking_steadiness",
    "HKQuantityTypeIdentifierRespiratoryRate": "respiratory_rate",
    "HKQuantityTypeIdentifierOxygenSaturation": "oxygen_saturation",
    "HKQuantityTypeIdentifierBloodPressureSystolic": "blood_pressure_systolic",
    "HKQuantityTypeIdentifierBloodPressureDiastolic": "blood_pressure_diastolic",
    "HKQuantityTypeIdentifierBloodGlucose": "blood_glucose",
    "HKQuantityTypeIdentifierBodyTemperature": "body_temperature",
    "HKQuantityTypeIdentifierDietaryEnergyConsumed": "dietary_energy",
    "HKQuantityTypeIdentifierDietaryWater": "water",
    "HKCategoryTypeIdentifierSleepAnalysis": "sleep_analysis",
    "HKCategoryTypeIdentifierMindfulSession": "mindful_session",
    "HKCategoryTypeIdentifierAppleStandHour": "stand_hour",
}

_PREFIXES = (
    "HKQuantityTypeIdentifier",
    "HKCategoryTypeIdentifier",
    "HKDataTypeIdentifier",
    "HKCorrelationTypeIdentifier",
    "HKCharacteristicTypeIdentifier",
    "HKClinicalTypeIdentifier",
    "HKWorkoutActivityType",
)

_camel_1 = re.compile(r"(.)([A-Z][a-z]+)")
_camel_2 = re.compile(r"([a-z0-9])([A-Z])")


def _snake(name: str) -> str:
    s = _camel_1.sub(r"\1_\2", name)
    s = _camel_2.sub(r"\1_\2", s)
    return s.lower()


def normalize_type(raw_type: str | None) -> str:
    """HKQuantityTypeIdentifierStepCount -> step_count."""
    if not raw_type:
        return "unknown"
    if raw_type in TYPE_ALIASES:
        return TYPE_ALIASES[raw_type]
    stripped = raw_type
    for p in _PREFIXES:
        if stripped.startswith(p):
            stripped = stripped[len(p):]
            break
    return _snake(stripped) if stripped else _snake(raw_type)


# Sleep stage category values -> compact stage labels.
SLEEP_STAGE_MAP = {
    "HKCategoryValueSleepAnalysisInBed": "in_bed",
    "HKCategoryValueSleepAnalysisAsleep": "asleep",
    "HKCategoryValueSleepAnalysisAsleepUnspecified": "asleep",
    "HKCategoryValueSleepAnalysisAsleepCore": "core",
    "HKCategoryValueSleepAnalysisAsleepDeep": "deep",
    "HKCategoryValueSleepAnalysisAsleepREM": "rem",
    "HKCategoryValueSleepAnalysisAwake": "awake",
}


def normalize_sleep_stage(value: str | None) -> str:
    if not value:
        return "unknown"
    if value in SLEEP_STAGE_MAP:
        return SLEEP_STAGE_MAP[value]
    return _snake(value.replace("HKCategoryValueSleepAnalysis", "")) or "unknown"


# Apple export timestamps look like: "2024-01-15 08:30:00 -0800"
_TS_FORMATS = ("%Y-%m-%d %H:%M:%S %z",)


def parse_ts(value: str | None) -> datetime | None:
    """Parse an Apple Health timestamp into a timezone-aware datetime."""
    if not value:
        return None
    value = value.strip()
    for fmt in _TS_FORMATS:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    # Last resort: ISO-8601 (some fields use it).
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def to_float(value: str | None) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
