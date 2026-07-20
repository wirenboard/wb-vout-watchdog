"""Shared test doubles and fixture builders.

- A fake `/sys/firmware/devicetree/base` + matching fake `/sys/bus/iio/devices` and
  `/sys/bus/gpio/devices`, standing in for the real sysfs trees that `devicetree.py` and
  `adc.py` read from.
- `FakeMqttClient`: stands in for `paho.mqtt.client.Client`, so tests never touch a real
  socket. It self-registers into `FakeMqttClient.instances` on construction, so tests can get
  hold of the specific instance a production object created without reaching into that
  object's private attributes.
- Shared assertion/setup helpers used by more than one test module (`latest_meta`, the
  busy-once `gpiod.request_lines` factory, the `subprocess_commands` recorder fixture).
"""

import errno
import json
import os
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple, Optional

import pytest

import wb_vout_watchdog.service as service_module
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


@dataclass
class FakeGpioLineRequest:
    """Stands in for a `gpiod.LineRequest`: tracks the line's value instead of touching real
    hardware. Self-registers into `instances`, same rationale as `FakeMqttClient`."""

    value: object
    consumer: str = None
    released: bool = False
    set_failures: int = 0  # number of leading set_value calls that raise OSError (transient bus)

    instances = []

    def __post_init__(self):
        FakeGpioLineRequest.instances.append(self)

    def get_value(self, _offset):
        return self.value

    def reconfigure_lines(self, config):
        (settings,) = config.values()
        self.value = settings.output_value

    def set_value(self, _offset, value):
        if self.set_failures > 0:
            self.set_failures -= 1
            raise OSError("simulated GPIO bus error")
        self.value = value

    def release(self):
        self.released = True


@pytest.fixture(autouse=True)
def _reset_fake_gpio_line_request_registry():
    FakeGpioLineRequest.instances = []


def make_fake_request_lines(initial_value):
    """Builds a `gpiod.request_lines` replacement that always returns a fresh
    `FakeGpioLineRequest` starting at `initial_value` (simulating the line's physical state as
    found by the `AS_IS` read in `VoutGpio.capture()`)."""

    def fake_request_lines(_chip_path, consumer=None, config=None):
        del config  # unused: the fake always exposes a single, implicit line
        return FakeGpioLineRequest(value=initial_value, consumer=consumer)

    return fake_request_lines


def make_fake_request_lines_set_failing(initial_value, set_failures):
    """Like `make_fake_request_lines`, but the returned request's first `set_failures`
    `set_value` calls raise `OSError` (a transient bus error on the off-SoC GPIO controller) --
    to exercise the write-retry path in `Service._flush_pending_vout`."""

    def fake_request_lines(_chip_path, consumer=None, config=None):
        del config
        return FakeGpioLineRequest(value=initial_value, consumer=consumer, set_failures=set_failures)

    return fake_request_lines


def make_fake_request_lines_busy_once(initial_value):
    """Like `make_fake_request_lines`, but the first call raises `EBUSY` (the line is held by
    another process, e.g. wb-mqtt-gpio after a lost startup race) and every later call
    succeeds -- simulating a race that one stop of the conflicting service resolves."""

    calls = []

    def fake_request_lines(_chip_path, consumer=None, config=None):
        del config
        calls.append(consumer)
        if len(calls) == 1:
            raise OSError(errno.EBUSY, "Device or resource busy")
        return FakeGpioLineRequest(value=initial_value, consumer=consumer)

    return fake_request_lines


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
