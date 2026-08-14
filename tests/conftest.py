"""Shared test doubles and fixture builders.

- A fake `/sys/firmware/devicetree/base` + matching fake `/sys/bus/iio/devices` and
  `/sys/bus/gpio/devices`, standing in for the real sysfs trees that `devicetree.py` and
  `adc.py` read from.
- `FakeMqttClient`: stands in for `paho.mqtt.client.Client`, so tests never touch a real
  socket. It self-registers into `FakeMqttClient.instances` on construction, so tests can get
  hold of the specific instance a production object created without reaching into that
  object's private attributes.
- `FakeGpioChip`: stands in for a real GPIO chip behind the `os.open`/`fcntl.ioctl` pair that
  `gpio_cdev.py` uses, so tests exercise the real ioctl encoding without a chip.
- Shared assertion/setup helpers used by more than one test module (`latest_meta`,
  `patch_gpio_chip`, the `subprocess_commands` recorder fixture).
"""

import enum
import errno
import fcntl
import json
import os
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple, Optional

import pytest

import wb_vout_watchdog.service as service_module
from wb_vout_watchdog import gpio_cdev
from wb_vout_watchdog.mqtt import DEVICE_TOPIC_PREFIX, Control


@dataclass
class DtFixture:
    dt_base: str
    iio_devices_dir: str
    gpio_devices_dir: str


@dataclass
class FakeReasonCode:
    is_failure: bool = False


@dataclass
class FakeMessage:
    topic: str
    payload: bytes
    retain: bool = False


@dataclass
class FakeMessageInfo:
    """Stands in for `paho.mqtt.client.MQTTMessageInfo`: everything published through the fake
    counts as instantly confirmed by the broker."""

    def wait_for_publish(self, timeout=None):
        del timeout


class FakeMqttClient:
    """Stands in for `paho.mqtt.client.Client`: records calls instead of touching a socket.

    Not a dataclass: `callback_api_version`/`client_id`/`transport` only exist to match
    `Client`'s constructor signature, no test needs to inspect them, so they're accepted and
    discarded rather than stored (keeping the number of attributes that matter down).
    """

    instances = []

    def __init__(self, callback_api_version, client_id=None, transport=None):
        del callback_api_version, client_id, transport
        # the on_connect/on_message slots are assigned by MqttDevice.__init__
        self.published = []
        self.subscriptions = []
        self.connect_args = None
        self.loop_started = False
        self.logger_enabled = False
        FakeMqttClient.instances.append(self)

    def enable_logger(self, logger=None):
        del logger
        self.logger_enabled = True

    def connect_async(self, host, port=1883):
        self.connect_args = (host, port)

    def loop_start(self):
        self.loop_started = True

    def loop_stop(self):
        self.loop_started = False

    def disconnect(self):
        pass

    def publish(self, topic, payload=None, qos=0, retain=False):
        self.published.append((topic, payload, retain, qos))
        return FakeMessageInfo()

    def subscribe(self, topic, qos=0):
        self.subscriptions.append((topic, qos))


def latest_meta(fake_client, control: Control) -> dict:
    """The most recently published meta JSON blob for `control`, decoded."""
    topic = f"{DEVICE_TOPIC_PREFIX}/controls/{control.value}/meta"
    payload = next(p for t, p, *_ in reversed(fake_client.published) if t == topic)
    return json.loads(payload)


@pytest.fixture(autouse=True)
def _reset_fake_mqtt_client_registry():
    FakeMqttClient.instances = []


CHIP_PATH_PREFIX = "/dev/gpiochip"

# Captured before any test patches them, so the fake chip can still reach the real ones -- and so
# it can pass through everything that is not addressed to it.
_REAL_OS_OPEN = os.open
_REAL_IOCTL = fcntl.ioctl


def _value_of(bits: int) -> gpio_cdev.Value:
    return gpio_cdev.Value.ACTIVE if bits else gpio_cdev.Value.INACTIVE


class BusyBehaviour(enum.Enum):
    """When a line request gets `EBUSY`. `FIRST_ATTEMPT` is a lost startup race that one stop of
    wb-mqtt-gpio resolves."""

    NEVER = "never"
    FIRST_ATTEMPT = "first-attempt"
    ALWAYS = "always"


@dataclass
class ChipFaults:
    """What the fake chip fails at: a line request answered `EBUSY`, and leading value writes that
    fail the way a transient error on the off-SoC GPIO controller does."""

    busy: BusyBehaviour = BusyBehaviour.NEVER
    set_failures: int = 0


@dataclass(frozen=True)
class RecordedCapture:
    """One `GET_LINE` as the chip received it, plus the descriptor handed back for it."""

    consumer: str
    offset: int
    num_lines: int
    flags: int
    num_attrs: int
    fd: int


@dataclass(frozen=True)
class RecordedConfig:
    flags: int
    num_attrs: int
    attr_id: int
    attr_values: int
    attr_mask: int


class ValuesOp(enum.Enum):
    READ = "read"
    WRITE = "write"


@dataclass(frozen=True)
class RecordedValues:
    """One values ioctl as decoded from the wire — the mask says which lines it addresses."""

    op: ValuesOp
    bits: int
    mask: int


@dataclass
class FakeGpioChip:
    """Stands in for a GPIO chip behind `os.open`/`fcntl.ioctl`: reads the uAPI structures the
    client really sends, applies them to an in-memory line value, and records them for assertions.

    Both patched functions are process-wide, so the fake answers only what is addressed to it --
    chip paths for `open`, its own descriptors for `ioctl` -- and passes everything else to the
    real function. A chip open and a line request each get a real descriptor (on /dev/null), so
    `release()` closes a real fd and the fd numbers are unique while they are open.
    """

    value: gpio_cdev.Value
    faults: ChipFaults = field(default_factory=ChipFaults)
    own_fds: set[int] = field(default_factory=set)
    opened_paths: list[str] = field(default_factory=list)
    captures: list[RecordedCapture] = field(default_factory=list)
    configs: list[RecordedConfig] = field(default_factory=list)
    values_ops: list[RecordedValues] = field(default_factory=list)

    @property
    def is_output(self) -> bool:
        """The line drives only once a `SET_CONFIG` says so; as-is until then."""
        return bool(self.configs and self.configs[-1].flags & gpio_cdev.LineFlag.OUTPUT)

    def open(self, path, flags, *args, **kwargs):
        if not str(path).startswith(CHIP_PATH_PREFIX):
            return _REAL_OS_OPEN(path, flags, *args, **kwargs)
        self.opened_paths.append(str(path))
        return self._new_fd()

    def ioctl(self, fd, request, *args, **kwargs):
        """Which descriptor it is decides only whether the call is ours at all: the fake exposes a
        single line, so beyond that the descriptor adds nothing."""
        if fd not in self.own_fds:
            return _REAL_IOCTL(fd, request, *args, **kwargs)
        if request == gpio_cdev.Ioctl.GET_LINE:
            return self._get_line(*args)
        if request == gpio_cdev.Ioctl.SET_CONFIG:
            return self._set_config(*args)
        if request == gpio_cdev.Ioctl.GET_VALUES:
            return self._get_values(*args)
        if request == gpio_cdev.Ioctl.SET_VALUES:
            return self._set_values(*args)
        raise AssertionError(f"unexpected ioctl {request:#x}")

    # --- Private ---

    def _new_fd(self) -> int:
        fd = _REAL_OS_OPEN(os.devnull, os.O_RDWR)
        self.own_fds.add(fd)
        return fd

    def _get_line(self, request):
        if self.faults.busy is not BusyBehaviour.NEVER:
            if self.faults.busy is BusyBehaviour.FIRST_ATTEMPT:
                self.faults.busy = BusyBehaviour.NEVER  # the next attempt finds the line free
            raise OSError(errno.EBUSY, "Device or resource busy")

        fd = self._new_fd()
        request.fd = fd
        self.captures.append(
            RecordedCapture(
                consumer=request.consumer.decode(),
                offset=request.offsets[0],
                num_lines=request.num_lines,
                flags=request.config.flags,
                num_attrs=request.config.num_attrs,
                fd=fd,
            )
        )
        return 0

    def _set_config(self, config):
        attribute = config.attrs[0]
        self.configs.append(
            RecordedConfig(
                flags=config.flags,
                num_attrs=config.num_attrs,
                attr_id=attribute.attr.id,
                attr_values=attribute.attr.value.values,
                attr_mask=attribute.mask,
            )
        )
        if config.num_attrs and attribute.attr.id == gpio_cdev.LineAttributeId.OUTPUT_VALUES:
            self.value = _value_of(attribute.attr.value.values & attribute.mask)
        elif self.is_output:
            self.value = gpio_cdev.Value.INACTIVE  # an output without that attribute is driven low
        return 0

    def _get_values(self, values):
        self.values_ops.append(RecordedValues(op=ValuesOp.READ, bits=values.bits, mask=values.mask))
        self._reject_unaddressed(values.mask)
        values.bits = gpio_cdev.LINE_BIT if self.value is gpio_cdev.Value.ACTIVE else 0
        return 0

    def _set_values(self, values):
        self.values_ops.append(RecordedValues(op=ValuesOp.WRITE, bits=values.bits, mask=values.mask))
        self._reject_unaddressed(values.mask)
        if not self.is_output:
            # The kernel refuses to drive a line that is not an output -- so would a stray write
            # during the glitch-free capture, instead of silently going through.
            raise OSError(errno.EPERM, "Operation not permitted")
        if self.faults.set_failures > 0:
            self.faults.set_failures -= 1
            raise OSError("simulated GPIO bus error")
        self.value = _value_of(values.bits & values.mask & gpio_cdev.LINE_BIT)
        return 0

    @staticmethod
    def _reject_unaddressed(mask):
        """A values ioctl whose mask selects no line of the request is `EINVAL` to the kernel —
        emulated, so a wrong mask fails loudly instead of silently doing nothing."""
        if not mask & gpio_cdev.LINE_BIT:
            raise OSError(errno.EINVAL, "Invalid argument")


def patch_gpio_chip(
    monkeypatch,
    initial_value: gpio_cdev.Value,
    *,
    busy: BusyBehaviour = BusyBehaviour.NEVER,
    set_failures: int = 0,
) -> FakeGpioChip:
    """Point the chardev GPIO client at a `FakeGpioChip` for the duration of the test.

    `initial_value` is the line's physical state as found by the as-is read in
    `VoutGpio.capture()`; `set_failures` is how many leading `set_value` calls raise `OSError`,
    standing in for a transient error on the off-SoC GPIO controller.
    """
    chip = FakeGpioChip(value=initial_value, faults=ChipFaults(busy=busy, set_failures=set_failures))
    monkeypatch.setattr(gpio_cdev.os, "open", chip.open)
    monkeypatch.setattr(gpio_cdev.fcntl, "ioctl", chip.ioctl)
    return chip


@pytest.fixture(name="subprocess_commands")
def _subprocess_commands_fixture(monkeypatch):
    """Replaces `subprocess.run` as seen by service.py with a recorder, so no real process is
    ever spawned; yields the list that collects each call's argv, for asserting which
    systemctl commands were issued and in what order."""
    commands = []
    monkeypatch.setattr(service_module.subprocess, "run", lambda command, **kwargs: commands.append(command))
    return commands


def _write_u32(path: Path, value: int) -> None:
    path.write_bytes(struct.pack(">I", value))


def _write_cells(path: Path, *values: int) -> None:
    path.write_bytes(struct.pack(f">{len(values)}I", *values))


def _write_string(path: Path, text: str) -> None:
    path.write_bytes(text.encode("ascii") + b"\x00")


class DividerOhms(NamedTuple):
    r1_ohms: int
    r2_ohms: int


@dataclass(frozen=True)
class VinNodeSpec:
    """What to put under /wirenboard/analog-inputs/Vin (and the matching fake IIO device)."""

    present: bool = True
    divider: Optional[DividerOhms] = DividerOhms(r1_ohms=390000, r2_ohms=10000)
    iio_channel_name: Optional[str] = None
    raw_filename: Optional[str] = "in_voltage0_raw"
    raw_value: Optional[str] = "27000"
    scale_value: Optional[str] = "0.732421875"
    phandle: int = 7


@dataclass(frozen=True)
class GpioNodeSpec:
    """What to put under /wirenboard/gpios (and the matching fake GPIO chip)."""

    present: bool = True
    node_name: str = "V_OUT"
    phandle: int = 9
    offset: int = 5
    flags: int = 0
    gpiochip_name: str = "gpiochip3"


def make_dt_fixture(
    tmp_path: Path, *, vin: VinNodeSpec = VinNodeSpec(), gpio: GpioNodeSpec = GpioNodeSpec()
) -> DtFixture:
    """Build a fake device-tree + matching IIO/GPIO sysfs layout for one test case.

    `vin`/`gpio` default to "everything present and normal"; tests override just the spec
    field they're exercising.
    """
    dt_base = tmp_path / "devicetree"
    iio_devices_dir = tmp_path / "iio-devices"
    gpio_devices_dir = tmp_path / "gpio-devices"
    dt_base.mkdir()
    iio_devices_dir.mkdir()
    gpio_devices_dir.mkdir()

    if vin.present:
        _add_vin_node(dt_base, iio_devices_dir, vin)

    if gpio.present:
        _add_gpio_node(dt_base, gpio_devices_dir, gpio)

    return DtFixture(str(dt_base), str(iio_devices_dir), str(gpio_devices_dir))


def _add_vin_node(dt_base: Path, iio_devices_dir: Path, spec: VinNodeSpec) -> None:
    vin_node = dt_base / "wirenboard" / "analog-inputs" / "Vin"
    vin_node.mkdir(parents=True)

    _write_u32(vin_node / "iio-device", spec.phandle)
    if spec.iio_channel_name is not None:
        _write_string(vin_node / "iio-channel-name", spec.iio_channel_name)

    if spec.divider is not None:
        _write_u32(vin_node / "divider-r1-ohms", spec.divider.r1_ohms)
        _write_u32(vin_node / "divider-r2-ohms", spec.divider.r2_ohms)

    adc_node = dt_base / "soc" / "adc@0"
    adc_node.mkdir(parents=True)
    _write_u32(adc_node / "phandle", spec.phandle)

    iio_device_dir = iio_devices_dir / "iio:device0"
    iio_device_dir.mkdir()
    os.symlink(adc_node, iio_device_dir / "of_node")

    if spec.raw_filename is not None and spec.raw_value is not None:
        (iio_device_dir / spec.raw_filename).write_text(spec.raw_value)
    if spec.scale_value is not None:
        scale_filename = (
            spec.raw_filename[: -len("_raw")] + "_scale" if spec.raw_filename else "in_voltage0_scale"
        )
        (iio_device_dir / scale_filename).write_text(spec.scale_value)


def _add_gpio_node(dt_base: Path, gpio_devices_dir: Path, spec: GpioNodeSpec) -> None:
    gpios_dir = dt_base / "wirenboard" / "gpios"
    gpios_dir.mkdir(parents=True)

    node = gpios_dir / spec.node_name
    node.mkdir()
    _write_cells(node / "io-gpios", spec.phandle, spec.offset, spec.flags)

    chip_node = dt_base / "soc" / "gpio@0"
    chip_node.mkdir(parents=True)
    _write_u32(chip_node / "phandle", spec.phandle)

    chip_dir = gpio_devices_dir / spec.gpiochip_name
    chip_dir.mkdir()
    os.symlink(chip_node, chip_dir / "of_node")
