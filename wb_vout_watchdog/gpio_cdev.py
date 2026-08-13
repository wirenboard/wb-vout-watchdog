"""Client for the Linux GPIO character device (v2 uAPI, needs Linux 5.10 or newer).

The service must install from one architecture-independent .deb onto images that have no
`python3-libgpiod` and no network to fetch it, so the ioctls libgpiod would send are issued here
directly. wb-mqtt-gpio drives its own lines the same way (v1 uAPI).

The structures below are a field-for-field transcription of the `gpio_v2_*` structs in the
kernel's `include/uapi/linux/gpio.h`; `ctypes` computes the layout from them. The kernel leaves
no implicit padding there (hence the explicit `padding` members), so the layout is the same on
32- and 64-bit targets. `tests/test_gpio_cdev.py` pins the resulting sizes, field offsets and
ioctl request codes against the numbers from that header.
"""

import ctypes
import enum
import fcntl
import os

CONSUMER_SIZE = 32  # `GPIO_MAX_NAME_SIZE`, NUL-terminated
LINES_MAX = 64  # `GPIO_V2_LINES_MAX`
NUM_ATTRS_MAX = 10  # `GPIO_V2_LINE_NUM_ATTRS_MAX`

# A request here holds exactly one line, so it is always bit 0 of every values/attribute mask.
LINE_BIT = 1

_GPIO_IOC_MAGIC = 0xB4
_IOC_READ_WRITE = 3


class LineFlag(enum.IntFlag):
    """Setting neither `INPUT` nor `OUTPUT` is how the uAPI spells "as-is": the kernel leaves the
    line's direction alone."""

    NONE = 0
    ACTIVE_LOW = 1 << 1
    INPUT = 1 << 2
    OUTPUT = 1 << 3


class LineAttributeId(enum.IntEnum):
    OUTPUT_VALUES = 2


class Value(enum.Enum):
    """A line's logical level. The kernel has already applied the `active_low` inversion, so
    `ACTIVE` means the line is on regardless of the physical polarity."""

    INACTIVE = 0
    ACTIVE = 1


# pylint: disable=too-few-public-methods


class LineValues(ctypes.Structure):
    """`struct gpio_v2_line_values`."""

    _fields_ = [("bits", ctypes.c_uint64), ("mask", ctypes.c_uint64)]


class _LineAttributeValue(ctypes.Union):
    _fields_ = [
        ("flags", ctypes.c_uint64),
        ("values", ctypes.c_uint64),
        ("debounce_period_us", ctypes.c_uint32),
    ]


class LineAttribute(ctypes.Structure):
    """`struct gpio_v2_line_attribute`."""

    _fields_ = [
        ("id", ctypes.c_uint32),
        ("padding", ctypes.c_uint32),
        ("value", _LineAttributeValue),
    ]


class LineConfigAttribute(ctypes.Structure):
    """`struct gpio_v2_line_config_attribute`."""

    _fields_ = [("attr", LineAttribute), ("mask", ctypes.c_uint64)]


class LineConfig(ctypes.Structure):
    """`struct gpio_v2_line_config`."""

    _fields_ = [
        ("flags", ctypes.c_uint64),
        ("num_attrs", ctypes.c_uint32),
        ("padding", ctypes.c_uint32 * 5),
        ("attrs", LineConfigAttribute * NUM_ATTRS_MAX),
    ]


class LineRequest(ctypes.Structure):
    """`struct gpio_v2_line_request`."""

    _fields_ = [
        ("offsets", ctypes.c_uint32 * LINES_MAX),
        ("consumer", ctypes.c_char * CONSUMER_SIZE),
        ("config", LineConfig),
        ("num_lines", ctypes.c_uint32),
        ("event_buffer_size", ctypes.c_uint32),
        ("padding", ctypes.c_uint32 * 5),
        ("fd", ctypes.c_int32),
    ]


# pylint: enable=too-few-public-methods


def _iowr(number: int, size: int) -> int:
    """`_IOWR(0xB4, number, <struct of `size` bytes>)`, per `asm-generic/ioctl.h`."""
    return (_IOC_READ_WRITE << 30) | (size << 16) | (_GPIO_IOC_MAGIC << 8) | number


class Ioctl(enum.IntEnum):
    GET_LINE = _iowr(0x07, ctypes.sizeof(LineRequest))
    SET_CONFIG = _iowr(0x0D, ctypes.sizeof(LineConfig))
    GET_VALUES = _iowr(0x0E, ctypes.sizeof(LineValues))
    SET_VALUES = _iowr(0x0F, ctypes.sizeof(LineValues))


class GpioLineRequest:
    """Owns the descriptor the kernel returns for one requested line: while it stays open, no
    other process can take that line."""

    def __init__(self, fd: int, flags: LineFlag):
        self._fd = fd
        self._flags = flags

    def get_value(self) -> Value:
        """The logical level, already de-inverted by the kernel for an `ACTIVE_LOW` line."""
        values = LineValues(bits=0, mask=LINE_BIT)
        fcntl.ioctl(self._fd, Ioctl.GET_VALUES, values)
        return Value.ACTIVE if values.bits & LINE_BIT else Value.INACTIVE

    def set_value(self, value: Value) -> None:
        values = LineValues(bits=_value_bits(value), mask=LINE_BIT)
        fcntl.ioctl(self._fd, Ioctl.SET_VALUES, values)

    def reconfigure_as_output(self, value: Value) -> None:
        """Turn the line into an output already driving `value`, keeping the request open.

        `SET_CONFIG` replaces the whole configuration, so the requested flags are resent too.
        """
        config = _output_values_config(self._flags | LineFlag.OUTPUT, value)
        fcntl.ioctl(self._fd, Ioctl.SET_CONFIG, config)

    def release(self) -> None:
        os.close(self._fd)


def request_line(chip_path: str, offset: int, consumer: str, active_low: bool) -> GpioLineRequest:
    """Take exclusive hold of line `offset` on `chip_path`, leaving its direction untouched.

    Raises `OSError`: `EBUSY` if another process holds the line, `EINVAL` for a bad line offset or
    a kernel without the v2 uAPI, `ENOTTY` if `chip_path` is not a GPIO character device, plus the
    usual `ENOENT`/`EACCES` from opening the chip.
    """
    flags = LineFlag.ACTIVE_LOW if active_low else LineFlag.NONE
    request = LineRequest(
        consumer=consumer.encode()[: CONSUMER_SIZE - 1],
        config=LineConfig(flags=flags),  # no attributes: the request must not drive the line
        num_lines=1,
    )
    request.offsets[0] = offset

    chip_fd = os.open(chip_path, os.O_RDWR | os.O_CLOEXEC)
    try:
        fcntl.ioctl(chip_fd, Ioctl.GET_LINE, request)
    finally:
        os.close(chip_fd)

    return GpioLineRequest(request.fd, flags)


# --- Private ---


def _value_bits(value: Value) -> int:
    return LINE_BIT if value is Value.ACTIVE else 0


def _output_values_config(flags: LineFlag, value: Value) -> LineConfig:
    config = LineConfig(flags=flags, num_attrs=1)
    config.attrs[0] = LineConfigAttribute(
        attr=LineAttribute(
            id=LineAttributeId.OUTPUT_VALUES,
            value=_LineAttributeValue(values=_value_bits(value)),
        ),
        mask=LINE_BIT,
    )
    return config
