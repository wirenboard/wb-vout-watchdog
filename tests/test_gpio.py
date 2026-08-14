"""Unit tests for `VoutGpio`.

There is no real GPIO chip to test against here, so the seam is the syscall boundary:
`FakeGpioChip` (conftest.py) replaces the `os.open`/`fcntl.ioctl` pair and applies the ioctls to
an in-memory line. The wire format itself and the kernel ABI behind it are covered by
`tests/test_gpio_cdev.py`; this module is about what `VoutGpio` does with them.
"""

import errno
import os

import pytest

from tests.conftest import BusyBehaviour, ValuesOp, patch_gpio_chip
from wb_vout_watchdog import gpio_cdev
from wb_vout_watchdog.devicetree import VoutGpioLine
from wb_vout_watchdog.gpio import GpioBusyError, GpioError, VoutGpio
from wb_vout_watchdog.gpio_cdev import Value


@pytest.fixture(name="line")
def _line_fixture():
    return VoutGpioLine(chip_path="/dev/gpiochip3", offset=5, active_low=False)


def open_fd_count() -> int:
    return len(os.listdir("/proc/self/fd"))


class TestCapture:
    def test_line_found_enabled_is_reported_and_kept_enabled(self, monkeypatch, line):
        """Capture must report True without changing the physical level (no glitch): the line is
        switched to an output whose output value is the level just read.

        The value ioctls are the pulse itself, so they are pinned exactly: one read and no write.
        Counting the configs alone would still pass for a read-then-drive sequence.
        """
        chip = patch_gpio_chip(monkeypatch, initial_value=Value.ACTIVE)

        observed = VoutGpio(line).capture()

        assert observed is True
        assert chip.value is Value.ACTIVE
        assert len(chip.configs) == 1  # one switch to output, not a read-then-drive sequence
        assert [op.op for op in chip.values_ops] == [ValuesOp.READ]

    def test_line_found_disabled_is_reported_and_kept_disabled(self, monkeypatch, line):
        chip = patch_gpio_chip(monkeypatch, initial_value=Value.INACTIVE)

        observed = VoutGpio(line).capture()

        assert observed is False
        assert chip.value is Value.INACTIVE
        assert [op.op for op in chip.values_ops] == [ValuesOp.READ]

    def test_the_device_tree_line_is_requested_under_our_consumer(self, monkeypatch, line):
        chip = patch_gpio_chip(monkeypatch, initial_value=Value.INACTIVE)

        VoutGpio(line).capture()

        assert chip.opened_paths == ["/dev/gpiochip3"]
        capture = chip.captures[0]
        assert capture.consumer == "wb-vout-watchdog"
        assert capture.offset == 5

    def test_first_request_reads_as_is_without_changing_direction(self, monkeypatch, line):
        """As-is means neither INPUT nor OUTPUT, so reading the level cannot disturb a line that
        is already driving Vout."""
        chip = patch_gpio_chip(monkeypatch, initial_value=Value.ACTIVE)

        VoutGpio(line).capture()

        assert not chip.captures[0].flags & (gpio_cdev.LineFlag.INPUT | gpio_cdev.LineFlag.OUTPUT)

    def test_an_inverted_line_from_the_device_tree_is_captured_active_low(self, monkeypatch):
        """`active_low` has to reach the kernel, which does the inverting; missing it would turn
        every Vout command upside down."""
        chip = patch_gpio_chip(monkeypatch, initial_value=Value.INACTIVE)

        VoutGpio(VoutGpioLine(chip_path="/dev/gpiochip3", offset=5, active_low=True)).capture()

        assert chip.captures[0].flags == gpio_cdev.LineFlag.ACTIVE_LOW

    def test_busy_line_raises_the_dedicated_busy_error(self, monkeypatch, line):
        """`EBUSY` gets its own `GpioError` subclass, so `service.capture_vout_line` can tell
        the recoverable lost-startup-race case apart from other capture failures."""
        patch_gpio_chip(monkeypatch, initial_value=Value.INACTIVE, busy=BusyBehaviour.ALWAYS)

        with pytest.raises(GpioBusyError):
            VoutGpio(line).capture()

    @pytest.mark.parametrize("error_number", [errno.ENOENT, errno.EACCES, errno.EINVAL, errno.ENOTTY])
    def test_other_capture_failures_raise_a_plain_gpio_error(self, monkeypatch, line, error_number):
        """A missing chip device, a chip we may not open, a bad line offset and a device that is
        not a GPIO chardev are all fatal-but-not-busy: only `EBUSY` may reach `capture_vout_line`'s
        wb-mqtt-gpio recovery, everything else is one `GpioError` carrying the OS message."""

        def raise_open_error(*_args, **_kwargs):
            raise OSError(error_number, os.strerror(error_number))

        monkeypatch.setattr(gpio_cdev.os, "open", raise_open_error)

        with pytest.raises(GpioError) as excinfo:
            VoutGpio(line).capture()
        assert not isinstance(excinfo.value, GpioBusyError)

    def test_a_failure_after_the_request_hands_the_line_back(self, monkeypatch, line):
        """The line request itself succeeds and only the read after it fails -- what a transient
        error on the off-SoC GPIO controller looks like. The line has to be released right there:
        the object must stay uncaptured, and the failure must not be reported as busy, or
        `capture_vout_line` would stop wb-mqtt-gpio and retry against a line we hold ourselves.
        """
        chip = patch_gpio_chip(monkeypatch, initial_value=Value.ACTIVE)
        before = open_fd_count()

        def fail_on_reading_the_value(fd, request, *args, **kwargs):
            if request == gpio_cdev.Ioctl.GET_VALUES:
                raise OSError(errno.EBUSY, "Device or resource busy")
            return chip.ioctl(fd, request, *args, **kwargs)

        monkeypatch.setattr(gpio_cdev.fcntl, "ioctl", fail_on_reading_the_value)
        gpio = VoutGpio(line)

        with pytest.raises(GpioError) as excinfo:
            gpio.capture()

        assert not isinstance(excinfo.value, GpioBusyError)
        assert open_fd_count() == before
        with pytest.raises(GpioError):
            gpio.set_enabled(True)


class TestSetEnabled:
    def test_set_enabled_drives_the_line(self, monkeypatch, line):
        chip = patch_gpio_chip(monkeypatch, initial_value=Value.INACTIVE)
        gpio = VoutGpio(line)
        gpio.capture()

        gpio.set_enabled(True)
        assert chip.value is Value.ACTIVE

        gpio.set_enabled(False)
        assert chip.value is Value.INACTIVE

    def test_a_failed_write_propagates_as_an_oserror(self, monkeypatch, line):
        """`Service._flush_pending_vout` retries on `OSError`, so a failed write must keep
        surfacing as one instead of being swallowed here."""
        patch_gpio_chip(monkeypatch, initial_value=Value.INACTIVE, set_failures=1)
        gpio = VoutGpio(line)
        gpio.capture()

        with pytest.raises(OSError):
            gpio.set_enabled(True)

    def test_set_enabled_before_capture_raises(self, line):
        with pytest.raises(GpioError):
            VoutGpio(line).set_enabled(True)


class TestClose:
    def test_close_releases_the_request(self, monkeypatch, line):
        """Capture must leave exactly one descriptor behind -- the line request, with the chip
        device already closed -- and `close()` must give that one up too: `EBADF` from `fstat` is
        the proof it was really closed rather than just dereferenced. A descriptor leaked per
        capture attempt would accumulate over the months the service runs."""
        chip = patch_gpio_chip(monkeypatch, initial_value=Value.INACTIVE)
        before = open_fd_count()

        gpio = VoutGpio(line)
        gpio.capture()
        assert open_fd_count() == before + 1

        gpio.close()

        assert open_fd_count() == before
        with pytest.raises(OSError) as excinfo:
            os.fstat(chip.captures[0].fd)
        assert excinfo.value.errno == errno.EBADF

    def test_close_without_capture_is_a_no_op(self, line):
        VoutGpio(line).close()
