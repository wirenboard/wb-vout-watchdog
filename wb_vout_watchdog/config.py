import json
from dataclasses import dataclass
from numbers import Number

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
        alarm_threshold_v=_non_negative_number(raw, "alarm_threshold_v", DEFAULT_ALARM_THRESHOLD_V),
        min_low_voltage_duration_s=_non_negative_number(
            raw, "min_low_voltage_duration_s", DEFAULT_MIN_LOW_VOLTAGE_DURATION_S
        ),
        battery_backup_threshold_v=_non_negative_number(
            raw, "battery_backup_threshold_v", DEFAULT_BATTERY_BACKUP_THRESHOLD_V
        ),
    )

    config = Config(
        thresholds=thresholds,
        adc_poll_period_s=_positive_number(raw, "adc_poll_period_s", DEFAULT_ADC_POLL_PERIOD_S),
        adc_error_threshold=_positive_int(raw, "adc_error_threshold", DEFAULT_ADC_ERROR_THRESHOLD),
        heartbeat_period_s=_positive_number(raw, "heartbeat_period_s", DEFAULT_HEARTBEAT_PERIOD_S),
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


def _number(raw: dict, key: str, default: float) -> float:
    """Read `key` as a float, falling back to `default` when absent. `bool` is a subclass of
    `int`, so reject it explicitly — otherwise JSON `true`/`false` would pass as 1.0/0.0."""
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, Number):
        raise ConfigError(f"'{key}' must be a number")
    return float(value)


def _non_negative_number(raw: dict, key: str, default: float) -> float:
    value = _number(raw, key, default)
    if value < 0:
        raise ConfigError(f"'{key}' must not be negative")
    return value


def _positive_number(raw: dict, key: str, default: float) -> float:
    value = _number(raw, key, default)
    if value <= 0:
        raise ConfigError(f"'{key}' must be greater than 0")
    return value


def _positive_int(raw: dict, key: str, default: int) -> int:
    """Read `key` as an int >= 1. `bool` is a subclass of `int`, so reject it explicitly."""
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"'{key}' must be an integer")
    if value < 1:
        raise ConfigError(f"'{key}' must be at least 1")
    return value
