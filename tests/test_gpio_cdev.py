"""Unit tests for the GPIO character-device client: the kernel ABI it transcribes and the bytes
it puts on the wire.

`FakeGpioChip` (conftest.py) replaces the `os.open`/`fcntl.ioctl` pair and reads the same
structure declarations the client writes, so `TestKernelAbi` pins what `ctypes` computed from
those declarations — sizes, field offsets, flag bits, attribute ids and the resulting ioctl
codes — against the literal numbers from `include/uapi/linux/gpio.h`. Without it a mistyped field
would move the layout for both sides at once and still pass.
"""

import ctypes

import pytest

from tests.conftest import ValuesOp, patch_gpio_chip
from wb_vout_watchdog import gpio_cdev
from wb_vout_watchdog.gpio_cdev import Value

CHIP_PATH = "/dev/gpiochip3"
LINE_OFFSET = 5
CONSUMER = "test-consumer"


@pytest.fixture(name="request_line")
def _request_line_fixture(monkeypatch):
    """Requests a line from a fake chip; yields `(chip, request_factory)` so a test can pick the
    line's polarity and then inspect what the chip was sent. Every request handed out is released
    on teardown -- cleanup is not part of any scenario here."""
    chip = patch_gpio_chip(monkeypatch, initial_value=Value.INACTIVE)
    requests = []

    def request(*, active_low=False):
        requests.append(gpio_cdev.request_line(CHIP_PATH, LINE_OFFSET, CONSUMER, active_low=active_low))
        return requests[-1]

    yield chip, request

    for line_request in requests:
        line_request.release()


class TestKernelAbi:
    """A typo in a hand-transcribed number would otherwise surface only as a puzzling `EINVAL` on
    hardware — or, worse, as a line configured the wrong way round."""

    def test_structure_sizes_match_the_uapi(self):
        assert ctypes.sizeof(gpio_cdev.LineValues) == 16
        assert ctypes.sizeof(gpio_cdev.LineAttribute) == 16
        assert ctypes.sizeof(gpio_cdev.LineConfigAttribute) == 24
        assert ctypes.sizeof(gpio_cdev.LineConfig) == 272
        assert ctypes.sizeof(gpio_cdev.LineRequest) == 592

    def test_field_offsets_match_the_uapi(self):
        """`ctypes` derives these from the `_fields_` declarations, so pinning them is what
        catches a mistyped member, a missing `padding` entry or a wrong integer width."""
        assert gpio_cdev.LineRequest.offsets.offset == 0
        assert gpio_cdev.LineRequest.consumer.offset == 256
        assert gpio_cdev.LineRequest.config.offset == 288
        assert gpio_cdev.LineRequest.num_lines.offset == 560
        assert gpio_cdev.LineRequest.fd.offset == 588
        assert gpio_cdev.LineConfig.flags.offset == 0
        assert gpio_cdev.LineConfig.num_attrs.offset == 8
        assert gpio_cdev.LineConfig.attrs.offset == 32
        assert gpio_cdev.LineValues.bits.offset == 0
        assert gpio_cdev.LineValues.mask.offset == 8
        assert gpio_cdev.LineAttribute.id.offset == 0
        assert gpio_cdev.LineAttribute.value.offset == 8
        assert gpio_cdev.LineConfigAttribute.mask.offset == 16

    def test_flag_and_attribute_values_match_the_uapi(self):
        assert gpio_cdev.LineFlag.ACTIVE_LOW == 2
        assert gpio_cdev.LineFlag.INPUT == 4
        assert gpio_cdev.LineFlag.OUTPUT == 8
        assert gpio_cdev.LineAttributeId.OUTPUT_VALUES == 2
        assert gpio_cdev.LINE_BIT == 1
        assert gpio_cdev.CONSUMER_SIZE == 32

    def test_ioctl_request_codes_match_the_uapi(self):
        """`_IOWR` embeds the size of the structure passed in, so these literals pin the sizes a
        second time -- `0x250`/`0x110`/`0x010` are 592/272/16 bytes."""
        assert gpio_cdev.Ioctl.GET_LINE == 0xC250B407
        assert gpio_cdev.Ioctl.SET_CONFIG == 0xC110B40D
        assert gpio_cdev.Ioctl.GET_VALUES == 0xC010B40E
        assert gpio_cdev.Ioctl.SET_VALUES == 0xC010B40F


class TestRequestLine:
    def test_one_line_is_requested_by_offset_under_the_given_consumer(self, request_line):
        """The consumer label is what makes the hold visible to `gpioinfo` and wb-mqtt-gpio."""
        chip, request = request_line

        request()

        assert chip.opened_paths == [CHIP_PATH]
        capture = chip.captures[0]
        assert capture.consumer == CONSUMER
        assert capture.offset == LINE_OFFSET
        assert capture.num_lines == 1

    def test_a_plain_line_is_requested_as_is(self, request_line):
        """No flags at all: as-is leaves the direction alone, and nothing must add `ACTIVE_LOW`
        to a line the device tree did not mark inverted."""
        chip, request = request_line

        request()

        capture = chip.captures[0]
        assert capture.flags == gpio_cdev.LineFlag.NONE
        assert capture.num_attrs == 0  # no output-values attribute: the request drives nothing

    def test_an_inverted_line_is_requested_active_low(self, request_line):
        chip, request = request_line

        request(active_low=True)

        assert chip.captures[0].flags == gpio_cdev.LineFlag.ACTIVE_LOW


class TestReconfigureAsOutput:
    def test_the_line_becomes_an_output_already_driving_the_value(self, request_line):
        """The output value travels as an `OUTPUT_VALUES` attribute masked to our single line —
        this is what makes the switch to output glitch-free."""
        chip, request = request_line
        line_request = request()

        line_request.reconfigure_as_output(Value.ACTIVE)

        config = chip.configs[0]
        assert config.flags == gpio_cdev.LineFlag.OUTPUT
        assert config.num_attrs == 1
        assert config.attr_id == gpio_cdev.LineAttributeId.OUTPUT_VALUES
        assert config.attr_mask == gpio_cdev.LINE_BIT
        assert config.attr_values & config.attr_mask == gpio_cdev.LINE_BIT
        assert chip.value is Value.ACTIVE

    def test_an_inactive_output_carries_a_cleared_value_bit(self, request_line):
        chip, request = request_line
        line_request = request()

        line_request.reconfigure_as_output(Value.INACTIVE)

        assert chip.configs[0].attr_values & chip.configs[0].attr_mask == 0
        assert chip.value is Value.INACTIVE

    def test_active_low_survives_the_switch_to_output(self, request_line):
        """`SET_CONFIG` replaces the whole configuration, so dropping the requested flags here
        would silently un-invert the line."""
        chip, request = request_line
        line_request = request(active_low=True)

        line_request.reconfigure_as_output(Value.INACTIVE)

        assert chip.configs[0].flags == gpio_cdev.LineFlag.ACTIVE_LOW | gpio_cdev.LineFlag.OUTPUT


class TestValues:
    def test_reading_addresses_our_line_only(self, request_line):
        """A mask that selects no line is `EINVAL` to the kernel, so the read would fail outright
        on hardware while looking fine against a fake that ignored the mask."""
        chip, request = request_line
        line_request = request()

        line_request.get_value()

        read = chip.values_ops[0]
        assert read.op is ValuesOp.READ
        assert read.mask == gpio_cdev.LINE_BIT

    def test_writing_addresses_our_line_only_and_carries_the_value(self, request_line):
        """The line is switched to an output first -- the kernel (and the fake) refuse a write to
        a line that is not driving. That switch records a config, not a value op, so the write is
        still the first entry."""
        chip, request = request_line
        line_request = request()
        line_request.reconfigure_as_output(Value.INACTIVE)

        line_request.set_value(Value.ACTIVE)

        write = chip.values_ops[0]
        assert write.op is ValuesOp.WRITE
        assert write.mask == gpio_cdev.LINE_BIT
        assert write.bits & write.mask == gpio_cdev.LINE_BIT

    def test_the_value_round_trips_in_both_directions(self, request_line):
        _chip, request = request_line
        line_request = request()
        line_request.reconfigure_as_output(Value.INACTIVE)

        line_request.set_value(Value.ACTIVE)
        assert line_request.get_value() is Value.ACTIVE

        line_request.set_value(Value.INACTIVE)
        assert line_request.get_value() is Value.INACTIVE
