import logging

from wb_vout_watchdog.devicetree import VinChannel
from wb_vout_watchdog.power_logic import VinReadError, VinSample

_MILLIVOLTS_PER_VOLT = 1000.0


class AdcError(Exception):
    """Raised when the Vin ADC channel is unavailable at service startup."""


class AdcErrorCounter:
    """Counts consecutive ADC read failures."""

    def __init__(self, threshold: int):
        self._threshold = threshold
        self._consecutive_errors = 0

    def record_success(self) -> None:
        self._consecutive_errors = 0

    def record_error(self) -> None:
        self._consecutive_errors += 1

    @property
    def alarm(self) -> bool:
        """True once the consecutive-failure count has reached the configured threshold."""
        return self._consecutive_errors >= self._threshold

    @property
    def count(self) -> int:
        """The current run of consecutive read failures."""
        return self._consecutive_errors


class VinReader:
    def __init__(self, channel: VinChannel, error_threshold: int):
        self._channel = channel
        self._errors = AdcErrorCounter(error_threshold)

    def check_available(self) -> None:
        """Raise `AdcError` if the Vin channel cannot be read at all (fatal at startup)."""
        try:
            self._read_raw()
        except (OSError, ValueError) as exc:
            raise AdcError(f"Vin ADC channel unavailable: {exc}") from exc

    def poll(self) -> VinSample:
        """Read Vin once, in volts, or a `VinReadError` if the poll failed."""
        try:
            raw = self._read_raw()
            scale = self._read_scale()
        except (OSError, ValueError) as exc:
            logging.warning("failed to read Vin ADC channel: %s", exc)
            self._errors.record_error()
            logging.debug("consecutive ADC read errors: %d", self._errors.count)
            return VinReadError(alarm=self._errors.alarm)

        self._errors.record_success()
        volts = raw * scale / _MILLIVOLTS_PER_VOLT * self._channel.divider_ratio
        logging.debug(
            "Vin %.2f V (raw=%d, scale=%s, divider=%.4f)", volts, raw, scale, self._channel.divider_ratio
        )
        return volts

    # --- Private ---

    def _read_raw(self) -> int:
        return int(_read_text(self._channel.raw_path))

    def _read_scale(self) -> float:
        return float(_read_text(self._scale_path()))

    def _scale_path(self) -> str:
        return self._channel.raw_path[: -len("_raw")] + "_scale"


def _read_text(path: str) -> str:
    with open(path, "r", encoding="ascii") as attr_file:
        return attr_file.read().strip()
