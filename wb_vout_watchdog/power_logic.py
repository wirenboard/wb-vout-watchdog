import logging
from dataclasses import dataclass
from typing import Optional, Union


@dataclass(frozen=True)
class EnableVoutRequested:
    """External MQTT confirmation: clears the `undervoltage` flag (making `V_OUT` writable
    again), and nothing else"""


@dataclass(frozen=True)
class VoutSwitchRequested:
    """A write to the `V_OUT` switch control. Ignored entirely while `undervoltage` is set."""

    enabled: bool


@dataclass(frozen=True)
class VoutStartupRestore:
    """The persisted Vout state to restore on the first `PowerLogic.tick()` call.

    With the flag down the line is driven to `enabled` (the state persisted before the last
    stop, like wb-mqtt-gpio's `load_previous_state`); with the flag up it is forced off
    regardless, through the same command path as a normal alarm cutoff.
    """

    enabled: bool


LogicEvent = Union[EnableVoutRequested, VoutSwitchRequested, VoutStartupRestore]


@dataclass(frozen=True)
class VinReadError:
    """The ADC poll failed this tick"""

    alarm: bool


VinSample = Union[float, VinReadError]


@dataclass(frozen=True)
class LogicOutput:
    set_vout: Optional[bool]
    undervoltage: bool


@dataclass(frozen=True)
class PowerThresholds:
    alarm_threshold_v: float
    min_low_voltage_duration_s: float
    battery_backup_threshold_v: float


class PowerLogic:
    def __init__(self, thresholds: PowerThresholds, initial_undervoltage: bool = False):
        self._thresholds = thresholds

        self._undervoltage = initial_undervoltage
        # Timestamp when the current uninterrupted stay in the low-voltage band began, or None
        # when Vin is not in that band. The alarm needs Vin to stay in the band continuously for
        # `min_low_voltage_duration_s`; any reading outside the band (recovery or battery-backup)
        # resets this, so only uninterrupted time counts.
        self._low_voltage_since: Optional[float] = None

    @property
    def undervoltage(self) -> bool:
        return self._undervoltage

    def tick(self, now: float, vin: VinSample, event: Optional[LogicEvent] = None) -> LogicOutput:
        """Advance the state machine by one poll and return the resulting commands."""
        set_vout = self._handle_vin(now, vin)

        if isinstance(event, VoutStartupRestore):
            # flag up -> force off (safety); flag down -> restore the persisted Vout state
            set_vout = False if self._undervoltage else event.enabled
        elif isinstance(event, EnableVoutRequested):
            self._undervoltage = False
        elif isinstance(event, VoutSwitchRequested):
            if not self._undervoltage:
                set_vout = event.enabled

        return LogicOutput(set_vout=set_vout, undervoltage=self._undervoltage)

    # --- Private ---

    def _handle_vin(self, now: float, vin: VinSample) -> Optional[bool]:
        if isinstance(vin, VinReadError):
            # A failed read does not reset the timer: if Vin was in the low-voltage band, assume
            # it still is and keep counting (fail-safe). A sustained ADC failure is caught
            # separately by the consecutive-error counter (adc.py) and surfaces as `vin.alarm`.
            if vin.alarm and not self._undervoltage:
                return self._raise_alarm()
            return None

        if self._is_battery_backup(vin):
            self._low_voltage_since = None
            logging.debug("Vin %.2f V: battery-backup zone", vin)
            return None

        return self._handle_low_voltage(now, vin)

    def _handle_low_voltage(self, now: float, vin: float) -> Optional[bool]:
        if vin >= self._thresholds.alarm_threshold_v:
            self._low_voltage_since = None
            logging.debug("Vin %.2f V: normal", vin)
            return None

        if self._low_voltage_since is None:
            self._low_voltage_since = now
        elif (
            now - self._low_voltage_since >= self._thresholds.min_low_voltage_duration_s
            and not self._undervoltage
        ):
            return self._raise_alarm()

        logging.debug(
            "Vin %.2f V: low-voltage zone, %.2f/%.2f s",
            vin,
            now - self._low_voltage_since,
            self._thresholds.min_low_voltage_duration_s,
        )
        return None

    def _raise_alarm(self) -> bool:
        self._undervoltage = True
        return False

    def _is_battery_backup(self, vin: float) -> bool:
        threshold = self._thresholds.battery_backup_threshold_v
        return threshold > 0 and vin < threshold
