"""Unit tests for `PowerLogic`: voltage thresholds, minimum-duration timing and state transitions.

Covers the alarm duration, the latching `undervoltage` flag, the `enable_vout` /
`V_OUT`-switch events, and the battery-backup exception. The ADC-error-counter behaviour is
covered by `test_adc.py::TestAdcErrorCounter`; this file only exercises how `PowerLogic`
reacts to a `VinReadError` it's handed.
"""

import pytest

from wb_vout_watchdog.power_logic import (
    EnableVoutRequested,
    LogicOutput,
    PowerLogic,
    PowerThresholds,
    VinReadError,
    VoutStartupRestore,
    VoutSwitchRequested,
)

ALARM_V = 20.0
MIN_LOW_VOLTAGE_DURATION_S = 5.0
BATTERY_V = 11.0


def _thresholds(**overrides):
    return PowerThresholds(
        **{
            "alarm_threshold_v": ALARM_V,
            "min_low_voltage_duration_s": MIN_LOW_VOLTAGE_DURATION_S,
            "battery_backup_threshold_v": BATTERY_V,
            **overrides,
        }
    )


@pytest.fixture(name="logic")
def _logic_fixture():
    return PowerLogic(_thresholds())


class TestVoltageThresholdAndDuration:
    def test_low_voltage_below_alarm_threshold_shorter_than_duration_does_not_alarm(self, logic):
        """A brief low-voltage excursion that recovers before the minimum duration elapses must not alarm."""
        logic.tick(0.0, 15.0)
        logic.tick(MIN_LOW_VOLTAGE_DURATION_S - 1.0, 15.0)
        output = logic.tick(MIN_LOW_VOLTAGE_DURATION_S - 1.0, ALARM_V + 1.0)

        assert output == LogicOutput(set_vout=None, undervoltage=False)

    def test_low_voltage_sustained_for_the_full_duration_alarms(self, logic):
        logic.tick(0.0, 15.0)
        output = logic.tick(MIN_LOW_VOLTAGE_DURATION_S, 15.0)

        assert output == LogicOutput(set_vout=False, undervoltage=True)

    def test_jitter_around_alarm_threshold_resets_the_duration_timer(self, logic):
        """Each tick just above the threshold must reset the duration clock, so jitter across the
        boundary never accumulates towards an alarm."""
        now = 0.0
        for _ in range(20):
            logic.tick(now, ALARM_V - 0.1)
            now += 1.0
            output = logic.tick(now, ALARM_V + 0.1)
            now += 1.0

        assert output.undervoltage is False

    def test_battery_backup_resets_the_duration_timer(self, logic):
        """The low voltage must be uninterrupted: dropping into the battery-backup zone resets
        the timer, so time before the interruption does not count and a fresh full duration is
        required after Vin returns to the band."""
        logic.tick(0.0, 15.0)
        logic.tick(4.0, 15.0)  # 4 s in the band, short of the 5 s duration
        logic.tick(5.0, BATTERY_V - 1.0)  # battery backup -> timer resets
        logic.tick(6.0, 15.0)  # back in the band: counting restarts from here
        early = logic.tick(6.0 + MIN_LOW_VOLTAGE_DURATION_S - 1.0, 15.0)  # only 4 s since return
        late = logic.tick(6.0 + MIN_LOW_VOLTAGE_DURATION_S, 15.0)  # a full 5 s since return

        assert early.undervoltage is False
        assert late.undervoltage is True
        assert late.set_vout is False

    def test_read_error_in_the_band_does_not_reset_the_duration_timer(self, logic):
        """Unlike leaving the band, an ADC read error while Vin is in the low-voltage band must
        not reset the timer: the low voltage is assumed to continue, so the alarm still fires a
        full duration after the band was entered (fail-safe)."""
        logic.tick(0.0, 15.0)  # enter the band
        logic.tick(2.0, VinReadError(alarm=False))  # transient read error mid-band: no reset
        output = logic.tick(MIN_LOW_VOLTAGE_DURATION_S, 15.0)  # full duration since entering

        assert output.undervoltage is True
        assert output.set_vout is False


class TestLatchingFlag:
    def test_vin_recovery_never_clears_the_flag(self, logic):
        """Scenario 3: the flag latches -- Vin returning far above the alarm threshold and
        staying there for a long time must not clear `undervoltage` or touch Vout."""
        logic.tick(0.0, 15.0)
        logic.tick(MIN_LOW_VOLTAGE_DURATION_S, 15.0)  # now in alarm

        for offset in range(1, 100):
            output = logic.tick(MIN_LOW_VOLTAGE_DURATION_S + offset, ALARM_V + 10.0)

        assert output.undervoltage is True
        assert output.set_vout is None

    def test_battery_zone_does_not_clear_a_preexisting_alarm(self, logic):
        """Scenario 2: if undervoltage was already raised by an earlier normal-zone low-voltage excursion,
        falling further into the battery-backup range must not clear it."""
        logic.tick(0.0, 15.0)
        logic.tick(MIN_LOW_VOLTAGE_DURATION_S, 15.0)  # now in alarm

        output = logic.tick(MIN_LOW_VOLTAGE_DURATION_S + 1.0, BATTERY_V - 1.0)

        assert output.undervoltage is True
        assert output.set_vout is None

    def test_realarm_is_immediate_when_the_low_voltage_persists_across_a_confirmation(self, logic):
        """Scenario 4: `enable_vout` during a continuing low-voltage condition is accepted (the
        flag clears), but the duration timer is not reset, so the alarm re-fires on the very next tick."""
        logic.tick(0.0, 15.0)
        logic.tick(MIN_LOW_VOLTAGE_DURATION_S, 15.0)  # now in alarm

        cleared = logic.tick(MIN_LOW_VOLTAGE_DURATION_S + 1.0, 15.0, EnableVoutRequested())
        realarmed = logic.tick(MIN_LOW_VOLTAGE_DURATION_S + 1.1, 15.0)

        assert cleared.undervoltage is False
        assert realarmed.undervoltage is True
        assert realarmed.set_vout is False

    def test_new_low_voltage_after_a_recovered_confirmation_needs_a_full_duration_again(self, logic):
        """Once Vin has recovered above the alarm threshold (resetting the duration timer) and the
        flag was cleared, a fresh low-voltage excursion must sustain the full duration before re-alarming."""
        logic.tick(0.0, 15.0)
        logic.tick(MIN_LOW_VOLTAGE_DURATION_S, 15.0)  # now in alarm
        logic.tick(MIN_LOW_VOLTAGE_DURATION_S + 1.0, ALARM_V + 5.0)  # recovered: duration timer resets
        logic.tick(MIN_LOW_VOLTAGE_DURATION_S + 2.0, ALARM_V + 5.0, EnableVoutRequested())

        low_voltage_start = MIN_LOW_VOLTAGE_DURATION_S + 3.0
        logic.tick(low_voltage_start, 15.0)
        early = logic.tick(low_voltage_start + MIN_LOW_VOLTAGE_DURATION_S - 0.1, 15.0)
        late = logic.tick(low_voltage_start + MIN_LOW_VOLTAGE_DURATION_S, 15.0)

        assert early.undervoltage is False
        assert late.undervoltage is True


class TestEvents:
    def test_confirmation_clears_the_flag_but_does_not_enable_vout(self, logic):
        """Scenario 4: `enable_vout` only unlocks -- the flag clears, Vout stays off; turning
        it on takes a separate `VoutSwitchRequested`."""
        logic.tick(0.0, 15.0)
        logic.tick(MIN_LOW_VOLTAGE_DURATION_S, 15.0)  # now in alarm

        output = logic.tick(MIN_LOW_VOLTAGE_DURATION_S + 1.0, ALARM_V + 5.0, EnableVoutRequested())

        assert output.undervoltage is False
        assert output.set_vout is None

    def test_confirmation_without_a_raised_flag_is_a_no_op(self, logic):
        output = logic.tick(0.0, ALARM_V + 5.0, EnableVoutRequested())

        assert output == LogicOutput(set_vout=None, undervoltage=False)

    def test_switch_on_request_enables_vout_when_the_flag_is_down(self, logic):
        output = logic.tick(0.0, ALARM_V + 5.0, VoutSwitchRequested(enabled=True))

        assert output.set_vout is True

    def test_switch_off_request_disables_vout_when_the_flag_is_down(self, logic):
        output = logic.tick(0.0, ALARM_V + 5.0, VoutSwitchRequested(enabled=False))

        assert output.set_vout is False

    def test_switch_request_is_ignored_entirely_while_the_flag_is_up(self, logic):
        """A write to `V_OUT` while `undervoltage` is set must change nothing: no Vout command
        and no effect on the flag."""
        logic.tick(0.0, 15.0)
        logic.tick(MIN_LOW_VOLTAGE_DURATION_S, 15.0)  # now in alarm

        output = logic.tick(
            MIN_LOW_VOLTAGE_DURATION_S + 1.0, ALARM_V + 5.0, VoutSwitchRequested(enabled=True)
        )

        assert output.set_vout is None
        assert output.undervoltage is True

    def test_low_voltage_while_vout_enabled_cuts_it_and_blocks_switch_requests(self, logic):
        """Scenario 1: a sustained low-voltage excursion cuts Vout, and `V_OUT` writes are then
        ignored until the flag clears (the alarm flag itself is what makes them a no-op)."""
        logic.tick(0.0, ALARM_V + 5.0, VoutSwitchRequested(enabled=True))

        logic.tick(1.0, 15.0)
        alarm_output = logic.tick(1.0 + MIN_LOW_VOLTAGE_DURATION_S, 15.0)
        switch_output = logic.tick(2.0 + MIN_LOW_VOLTAGE_DURATION_S, 15.0, VoutSwitchRequested(enabled=True))

        assert alarm_output.set_vout is False
        assert switch_output.set_vout is None

    def test_startup_restores_vout_off_when_persisted_off(self, logic):
        """Scenario 5, flag down: the line is driven to the persisted state, so a Vout that was
        off before the stop stays off."""
        output = logic.tick(0.0, ALARM_V + 5.0, VoutStartupRestore(enabled=False))

        assert output.set_vout is False
        assert output.undervoltage is False

    def test_startup_restores_vout_on_when_persisted_on(self, logic):
        """Scenario 5, flag down: a Vout that was on before the stop is restored on at startup,
        without waiting for a fresh command."""
        output = logic.tick(0.0, ALARM_V + 5.0, VoutStartupRestore(enabled=True))

        assert output.set_vout is True
        assert output.undervoltage is False

    def test_startup_forces_vout_off_when_the_flag_is_set(self):
        """Scenario 5, flag up: a persisted alarm forces Vout off at startup regardless of the
        persisted Vout state, and the flag stays raised."""
        logic = PowerLogic(_thresholds(), initial_undervoltage=True)

        output = logic.tick(0.0, ALARM_V + 5.0, VoutStartupRestore(enabled=True))

        assert output.set_vout is False
        assert output.undervoltage is True


class TestPersistedInitialUndervoltage:
    """Scenario 5: `service.py` seeds the constructor with whatever it read from the marker
    file before this class ever runs a tick -- covered here as a plain constructor argument,
    since `PowerLogic` itself does no file I/O (see power_logic.py's module docstring)."""

    def test_constructor_seeds_the_flag_immediately(self):
        logic = PowerLogic(_thresholds(), initial_undervoltage=True)

        assert logic.undervoltage is True

    def test_seeded_flag_latches_like_a_normal_one(self):
        """A seeded flag behaves exactly like a tick-raised one: healthy Vin does not clear it."""
        logic = PowerLogic(_thresholds(), initial_undervoltage=True)

        output = logic.tick(0.0, ALARM_V + 5.0)

        assert output.undervoltage is True
        assert output.set_vout is None

    def test_confirmation_clears_a_seeded_flag(self):
        logic = PowerLogic(_thresholds(), initial_undervoltage=True)

        output = logic.tick(0.0, ALARM_V + 5.0, EnableVoutRequested())

        assert output.undervoltage is False
        assert output.set_vout is None

    def test_battery_zone_keeps_a_seeded_flag(self):
        """A seeded alarm behaves like a normal one in the battery-backup zone: staying below
        the battery-backup threshold does not clear it (Scenario 2's "already raised" case)."""
        logic = PowerLogic(_thresholds(), initial_undervoltage=True)

        output = logic.tick(0.0, BATTERY_V - 1.0)

        assert output.undervoltage is True
        assert output.set_vout is None


class TestBatteryBackupException:
    def test_low_voltage_into_battery_zone_does_not_alarm_or_change_vout(self, logic):
        """Scenario 2: Vin below the battery-backup threshold is not a dangerous low-voltage
        excursion, whether Vout was on or off going in."""
        output = logic.tick(0.0, BATTERY_V - 1.0)

        assert output.set_vout is None
        assert output.undervoltage is False

    def test_normal_zone_low_voltage_still_alarms_regardless_of_battery_threshold(self, logic):
        """A reading between the battery and alarm thresholds is a normal dangerous excursion."""
        logic.tick(0.0, BATTERY_V + 1.0)
        output = logic.tick(MIN_LOW_VOLTAGE_DURATION_S, BATTERY_V + 1.0)

        assert output.undervoltage is True
        assert output.set_vout is False

    def test_battery_threshold_zero_disables_the_exception(self):
        """battery_backup_threshold_v == 0 means every reading below the alarm threshold alarms,
        no matter how deep."""
        logic = PowerLogic(_thresholds(battery_backup_threshold_v=0))

        logic.tick(0.0, 0.5)
        output = logic.tick(MIN_LOW_VOLTAGE_DURATION_S, 0.5)

        assert output.undervoltage is True
        assert output.set_vout is False


class TestAdcReadErrors:
    def test_isolated_read_error_below_threshold_does_not_alarm(self, logic):
        output = logic.tick(0.0, VinReadError(alarm=False))

        assert output.set_vout is None
        assert output.undervoltage is False

    def test_read_error_at_threshold_alarms_as_a_fail_safe(self, logic):
        output = logic.tick(0.0, VinReadError(alarm=True))

        assert output.set_vout is False
        assert output.undervoltage is True
