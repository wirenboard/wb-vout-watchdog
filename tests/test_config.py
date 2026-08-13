"""Unit tests for /etc/wb-vout-watchdog.conf parsing and validation."""

import json

import pytest

from wb_vout_watchdog.config import Config, ConfigError, load_config
from wb_vout_watchdog.power_logic import PowerThresholds


def _write_config(tmp_path, content: dict):
    path = tmp_path / "wb-vout-watchdog.conf"
    path.write_text(json.dumps(content))
    return str(path)


class TestDefaults:
    def test_empty_config_uses_defaults(self, tmp_path):
        path = _write_config(tmp_path, {})

        assert load_config(path) == Config()

    def test_partial_config_fills_unset_keys_with_defaults(self, tmp_path):
        """A config that sets only some keys leaves every other key at its default."""
        path = _write_config(tmp_path, {"alarm_threshold_v": 19.0})

        config = load_config(path)

        assert config.thresholds.alarm_threshold_v == 19.0
        assert config.adc_poll_period_s == Config().adc_poll_period_s
        assert config.heartbeat_period_s == Config().heartbeat_period_s


class TestOverrides:
    def test_all_keys_can_be_overridden(self, tmp_path):
        path = _write_config(
            tmp_path,
            {
                "alarm_threshold_v": 19.0,
                "min_low_voltage_duration_s": 3.0,
                "battery_backup_threshold_v": 10.0,
                "adc_poll_period_s": 0.5,
                "adc_error_threshold": 5,
                "heartbeat_period_s": 15.0,
            },
        )

        config = load_config(path)

        assert config == Config(
            thresholds=PowerThresholds(
                alarm_threshold_v=19.0,
                min_low_voltage_duration_s=3.0,
                battery_backup_threshold_v=10.0,
            ),
            adc_poll_period_s=0.5,
            adc_error_threshold=5,
            heartbeat_period_s=15.0,
        )

    def test_battery_backup_threshold_zero_is_valid(self, tmp_path):
        """0 is a valid, meaningful value: it disables the battery-backup exception entirely."""
        path = _write_config(tmp_path, {"battery_backup_threshold_v": 0})

        config = load_config(path)

        assert config.thresholds.battery_backup_threshold_v == 0


class TestErrors:
    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(ConfigError):
            load_config(str(tmp_path / "does-not-exist.conf"))

    def test_invalid_json_raises(self, tmp_path):
        path = tmp_path / "wb-vout-watchdog.conf"
        path.write_text("{not valid json")

        with pytest.raises(ConfigError):
            load_config(str(path))

    def test_non_object_json_raises(self, tmp_path):
        path = _write_config(tmp_path, [])

        with pytest.raises(ConfigError):
            load_config(path)

    def test_wrong_type_raises(self, tmp_path):
        path = _write_config(tmp_path, {"alarm_threshold_v": "twenty"})

        with pytest.raises(ConfigError):
            load_config(path)

    def test_bool_is_rejected_for_numeric_field(self, tmp_path):
        """bool is a subclass of int in Python; it must not be silently accepted as 0/1."""
        path = _write_config(tmp_path, {"adc_error_threshold": True})

        with pytest.raises(ConfigError):
            load_config(path)

    def test_battery_backup_threshold_must_be_below_alarm_threshold(self, tmp_path):
        path = _write_config(tmp_path, {"alarm_threshold_v": 20.0, "battery_backup_threshold_v": 20.0})

        with pytest.raises(ConfigError):
            load_config(path)

    def test_adc_poll_period_must_be_strictly_positive(self, tmp_path):
        path = _write_config(tmp_path, {"adc_poll_period_s": 0})

        with pytest.raises(ConfigError):
            load_config(path)

    def test_negative_voltage_threshold_is_rejected(self, tmp_path):
        path = _write_config(tmp_path, {"alarm_threshold_v": -1.0})

        with pytest.raises(ConfigError):
            load_config(path)

    def test_adc_error_threshold_below_one_is_rejected(self, tmp_path):
        path = _write_config(tmp_path, {"adc_error_threshold": 0})

        with pytest.raises(ConfigError):
            load_config(path)
