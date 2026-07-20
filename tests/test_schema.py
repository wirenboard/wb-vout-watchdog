"""Unit tests for the confed JSON Schema: valid JSON Schema, and its defaults must not drift
from `config.py`'s defaults (the schema is confed-only data, not read by the service itself,
so nothing else keeps the two in sync automatically).
"""

import json
import os

import pytest

from wb_vout_watchdog import config as config_module

SCHEMA_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "wb-vout-watchdog.schema.json")

# No jsonschema-validating library in requirements-dev.txt (config.py deliberately avoids the
# jsonschema dependency); this checks the shape confed cares about by hand instead of pulling
# that dependency in just for this one test.
_VALID_JSON_SCHEMA_TYPES = {
    "object",
    "array",
    "string",
    "number",
    "integer",
    "boolean",
    "null",
}


@pytest.fixture(name="schema")
def _schema_fixture():
    with open(SCHEMA_PATH, "r", encoding="utf-8") as schema_file:
        return json.load(schema_file)


def test_schema_top_level_shape_is_well_formed(schema):
    assert schema["type"] == "object"
    assert isinstance(schema["properties"], dict)
    assert isinstance(schema["required"], list)


def test_every_property_declares_a_valid_json_schema_type(schema):
    for name, prop in schema["properties"].items():
        assert prop["type"] in _VALID_JSON_SCHEMA_TYPES, f"{name} has an invalid 'type'"
        if prop["type"] == "object":
            for nested_name, nested_prop in prop.get("properties", {}).items():
                assert (
                    nested_prop["type"] in _VALID_JSON_SCHEMA_TYPES
                ), f"{name}.{nested_name} has an invalid 'type'"


def test_top_level_defaults_match_config_defaults(schema):
    properties = schema["properties"]

    assert properties["alarm_threshold_v"]["default"] == config_module.DEFAULT_ALARM_THRESHOLD_V
    assert (
        properties["min_low_voltage_duration_s"]["default"]
        == config_module.DEFAULT_MIN_LOW_VOLTAGE_DURATION_S
    )
    assert (
        properties["battery_backup_threshold_v"]["default"]
        == config_module.DEFAULT_BATTERY_BACKUP_THRESHOLD_V
    )
    assert properties["adc_poll_period_s"]["default"] == config_module.DEFAULT_ADC_POLL_PERIOD_S
    assert properties["adc_error_threshold"]["default"] == config_module.DEFAULT_ADC_ERROR_THRESHOLD
    assert properties["heartbeat_period_s"]["default"] == config_module.DEFAULT_HEARTBEAT_PERIOD_S


def test_config_file_path_matches_the_default_used_by_main(schema):
    assert schema["configFile"]["path"] == "/etc/wb-vout-watchdog.conf"
