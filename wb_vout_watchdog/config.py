import json
from dataclasses import dataclass
from numbers import Number
from typing import Any

from wb_vout_watchdog.power_logic import PowerThresholds

DEFAULT_ALARM_THRESHOLD_V = 20.0
DEFAULT_MIN_LOW_VOLTAGE_DURATION_S = 5.0
DEFAULT_BATTERY_BACKUP_THRESHOLD_V = 11.0
DEFAULT_ADC_POLL_PERIOD_S = 1.0
DEFAULT_ADC_ERROR_THRESHOLD = 3
DEFAULT_HEARTBEAT_PERIOD_S = 10.0

DEFAULT_THRESHOLDS = PowerThresholds(
    alarm_threshold_v=DEFAULT_ALARM_THRESHOLD_V,
    min_low_voltage_duration_s=DEFAULT_MIN_LOW_VOLTAGE_DURATION_S,
    battery_backup_threshold_v=DEFAULT_BATTERY_BACKUP_THRESHOLD_V,
)


class ConfigError(Exception):
    """Raised when the config file is missing, malformed, or fails a cross-field check."""


@dataclass(frozen=True)
class Config:
    thresholds: PowerThresholds = DEFAULT_THRESHOLDS
    adc_poll_period_s: float = DEFAULT_ADC_POLL_PERIOD_S
    adc_error_threshold: int = DEFAULT_ADC_ERROR_THRESHOLD
    heartbeat_period_s: float = DEFAULT_HEARTBEAT_PERIOD_S


def load_config(path: str) -> Config:
    raw = _read_json(path)

    thresholds = PowerThresholds(
        alarm_threshold_v=_get_number(raw, "alarm_threshold_v", DEFAULT_ALARM_THRESHOLD_V, min_value=0.0),
        min_low_voltage_duration_s=_get_number(
            raw, "min_low_voltage_duration_s", DEFAULT_MIN_LOW_VOLTAGE_DURATION_S, min_value=0.0
        ),
        battery_backup_threshold_v=_get_number(
            raw, "battery_backup_threshold_v", DEFAULT_BATTERY_BACKUP_THRESHOLD_V, min_value=0.0
        ),
    )

    config = Config(
        thresholds=thresholds,
        adc_poll_period_s=_get_number(
            raw, "adc_poll_period_s", DEFAULT_ADC_POLL_PERIOD_S, min_value=0.0, exclusive_min=True
        ),
        adc_error_threshold=_get_int(raw, "adc_error_threshold", DEFAULT_ADC_ERROR_THRESHOLD, min_value=1),
        heartbeat_period_s=_get_number(
            raw, "heartbeat_period_s", DEFAULT_HEARTBEAT_PERIOD_S, min_value=0.0, exclusive_min=True
        ),
    )

    _validate_cross_fields(thresholds)
    return config


# --- Private ---


def _read_json(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as config_file:
            raw = json.load(config_file)
    except OSError as exc:
        raise ConfigError(f"cannot read config file {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"invalid JSON in config file {path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError(f"config file {path} must contain a JSON object")

    return raw


def _validate_cross_fields(thresholds: PowerThresholds) -> None:
    if (
        thresholds.battery_backup_threshold_v != 0
        and thresholds.battery_backup_threshold_v >= thresholds.alarm_threshold_v
    ):
        raise ConfigError("battery_backup_threshold_v must be 0 or less than alarm_threshold_v")


def _get_number(
    raw: dict,
    key: str,
    default: float,
    min_value: float = None,
    exclusive_min: bool = False,
) -> float:
    value = _get_value(raw, key, default, Number)
    if min_value is not None:
        if exclusive_min and value <= min_value:
            raise ConfigError(f"'{key}' must be greater than {min_value}")
        if not exclusive_min and value < min_value:
            raise ConfigError(f"'{key}' must be at least {min_value}")
    return float(value)


def _get_int(raw: dict, key: str, default: int, min_value: int = None) -> int:
    value = _get_value(raw, key, default, int)
    if min_value is not None and value < min_value:
        raise ConfigError(f"'{key}' must be at least {min_value}")
    return value


def _get_value(raw: dict, key: str, default: Any, expected_type: type) -> Any:
    if key not in raw:
        return default

    value = raw[key]
    # bool is a subclass of int in Python; reject it explicitly so `true`/`false` in the
    # config JSON isn't silently accepted as 0/1 for a numeric field.
    if isinstance(value, bool) or not isinstance(value, expected_type):
        raise ConfigError(f"'{key}' must be of type {expected_type.__name__}")

    return value
