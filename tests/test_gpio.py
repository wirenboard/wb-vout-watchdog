"""Unit tests for `VoutGpio`.

There is no real GPIO chip to test against here, so `gpiod.request_lines` is replaced with a
fake line request that just tracks the calls made to it. `gpiod.LineSettings`/`Direction`/
`Value` are used for real -- they are plain enums/dataclasses with no I/O of their own.
"""

import errno

import gpiod
import pytest
from gpiod.line import Direction, Value

from wb_vout_watchdog.devicetree import VoutGpioLine
from wb_vout_watchdog.gpio import GpioBusyError, GpioError, VoutGpio


class FakeLineRequest:
    """Stands in for a `gpiod.LineRequest`: tracks calls instead of touching real hardware."""

    def __init__(self, initial_value: Value):
        self.value = initial_value
        self.released = False
        self.reconfigure_calls = []

    def get_value(self, _offset):
        return self.value

    def reconfigure_lines(self, config):
        self.reconfigure_calls.append(config)
        (settings,) = config.values()
        self.value = settings.output_value

    def set_value(self, _offset, value):
        self.value = value

    def release(self):
        self.released = True


@pytest.fixture(name="line")
def _line_fixture():
    return VoutGpioLine(chip_path="/dev/gpiochip3", offset=5, active_low=False)


def _patch_request_lines(monkeypatch, fake_request, calls):
    def fake_request_lines(chip_path, consumer=None, config=None):
        calls.append({"chip_path": chip_path, "consumer": consumer, "config": config})
        return fake_request

    monkeypatch.setattr(gpiod, "request_lines", fake_request_lines)


class TestCapture:
    def test_line_found_enabled_is_reported_and_kept_enabled(self, monkeypatch, line):
        """Capture must report True without changing the physical level (no glitch)."""
        fake_request = FakeLineRequest(Value.ACTIVE)
        calls = []
        _patch_request_lines(monkeypatch, fake_request, calls)

        gpio = VoutGpio(line)
        observed = gpio.capture()

        assert observed is True
        assert calls[0]["chip_path"] == "/dev/gpiochip3"
        (settings,) = fake_request.reconfigure_calls[0].values()
        assert settings.direction == Direction.OUTPUT
        assert settings.output_value == Value.ACTIVE

    def test_line_found_disabled_is_reported_and_kept_disabled(self, monkeypatch, line):
        fake_request = FakeLineRequest(Value.INACTIVE)
        _patch_request_lines(monkeypatch, fake_request, [])

        gpio = VoutGpio(line)
        observed = gpio.capture()

        assert observed is False

    def test_first_request_reads_as_is_without_changing_direction(self, monkeypatch, line):
        fake_request = FakeLineRequest(Value.INACTIVE)
        calls = []
        _patch_request_lines(monkeypatch, fake_request, calls)

        VoutGpio(line).capture()

        (settings,) = calls[0]["config"].values()
        assert settings.direction == Direction.AS_IS

    def test_busy_line_raises_the_dedicated_busy_error(self, monkeypatch, line):
        """`EBUSY` gets its own `GpioError` subclass, so `service.capture_vout_line` can tell
        the recoverable lost-startup-race case apart from other capture failures."""

        def raise_busy(*_args, **_kwargs):
            raise OSError(errno.EBUSY, "Device or resource busy")

        monkeypatch.setattr(gpiod, "request_lines", raise_busy)

        with pytest.raises(GpioBusyError):
            VoutGpio(line).capture()

    def test_other_capture_failures_raise_a_plain_gpio_error(self, monkeypatch, line):
        def raise_no_entry(*_args, **_kwargs):
            raise OSError(errno.ENOENT, "No such file or directory")

        monkeypatch.setattr(gpiod, "request_lines", raise_no_entry)

        with pytest.raises(GpioError) as excinfo:
            VoutGpio(line).capture()
        assert not isinstance(excinfo.value, GpioBusyError)


class TestSetEnabled:
    def test_set_enabled_drives_the_line(self, monkeypatch, line):
        fake_request = FakeLineRequest(Value.INACTIVE)
        _patch_request_lines(monkeypatch, fake_request, [])
        gpio = VoutGpio(line)
        gpio.capture()

        gpio.set_enabled(True)
        assert fake_request.value == Value.ACTIVE

        gpio.set_enabled(False)
        assert fake_request.value == Value.INACTIVE

    def test_set_enabled_before_capture_raises(self, line):
        with pytest.raises(GpioError):
            VoutGpio(line).set_enabled(True)


class TestClose:
    def test_close_releases_the_request(self, monkeypatch, line):
        fake_request = FakeLineRequest(Value.INACTIVE)
        _patch_request_lines(monkeypatch, fake_request, [])
        gpio = VoutGpio(line)
        gpio.capture()

        gpio.close()

        assert fake_request.released is True

    def test_close_without_capture_is_a_no_op(self, line):
        VoutGpio(line).close()
