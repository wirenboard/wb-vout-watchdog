"""Unit tests for the consecutive-ADC-error counter and the Vin reader."""

import pytest

from wb_vout_watchdog.adc import AdcError, AdcErrorCounter, VinReader
from wb_vout_watchdog.devicetree import VinChannel
from wb_vout_watchdog.power_logic import VinReadError


class TestAdcErrorCounter:
    def test_single_failure_does_not_alarm(self):
        counter = AdcErrorCounter(threshold=3)

        counter.record_error()

        assert counter.alarm is False

    def test_failures_below_threshold_followed_by_success_reset_the_count(self):
        """Two failures (below the threshold of 3), then a success, then one more failure:
        the counter must not have carried over any progress from before the success."""
        counter = AdcErrorCounter(threshold=3)

        counter.record_error()
        counter.record_error()
        counter.record_success()
        counter.record_error()

        assert counter.alarm is False

    def test_failures_reaching_threshold_alarm(self):
        counter = AdcErrorCounter(threshold=3)

        counter.record_error()
        counter.record_error()
        assert counter.alarm is False
        counter.record_error()

        assert counter.alarm is True


@pytest.fixture(name="channel")
def _channel_fixture(tmp_path):
    raw_path = tmp_path / "in_voltage3_raw"
    scale_path = tmp_path / "in_voltage3_scale"
    raw_path.write_text("27000")
    scale_path.write_text("0.732421875")
    return VinChannel(raw_path=str(raw_path), divider_ratio=40.0)


class TestVinReader:
    def test_poll_converts_raw_counts_to_volts(self, channel):
        """volts = raw * scale(mV) / 1000 * divider_ratio."""
        reader = VinReader(channel, error_threshold=3)

        vin = reader.poll()

        assert vin == pytest.approx(27000 * 0.732421875 / 1000 * 40.0)

    def test_poll_failure_returns_read_error_below_threshold(self, tmp_path):
        channel = VinChannel(raw_path=str(tmp_path / "missing_raw"), divider_ratio=1.0)
        reader = VinReader(channel, error_threshold=3)

        result = reader.poll()

        assert result == VinReadError(alarm=False)

    def test_poll_failure_alarms_once_threshold_reached(self, tmp_path):
        channel = VinChannel(raw_path=str(tmp_path / "missing_raw"), divider_ratio=1.0)
        reader = VinReader(channel, error_threshold=2)

        reader.poll()
        result = reader.poll()

        assert result == VinReadError(alarm=True)

    def test_success_after_failures_resets_the_error_counter(self, tmp_path):
        """One failed poll, then a successful one, then another failure: the counter must not
        carry over the failure from before the success, so the second failure alone doesn't
        alarm even with a threshold of 2."""
        raw_path = tmp_path / "in_voltage3_raw"
        scale_path = tmp_path / "in_voltage3_scale"
        scale_path.write_text("0.732421875")
        channel = VinChannel(raw_path=str(raw_path), divider_ratio=1.0)
        reader = VinReader(channel, error_threshold=2)

        reader.poll()  # raw_path does not exist yet: failure #1

        raw_path.write_text("27000")
        reader.poll()  # now readable: success, resets the counter

        raw_path.unlink()
        result = reader.poll()  # failure #1 of a new streak

        assert result == VinReadError(alarm=False)

    def test_check_available_raises_on_missing_channel(self, tmp_path):
        channel = VinChannel(raw_path=str(tmp_path / "missing_raw"), divider_ratio=1.0)
        reader = VinReader(channel, error_threshold=3)

        with pytest.raises(AdcError):
            reader.check_available()

    def test_check_available_succeeds_when_channel_is_readable(self, channel):
        reader = VinReader(channel, error_threshold=3)

        reader.check_available()
