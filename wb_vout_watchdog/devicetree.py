import os
import struct
from dataclasses import dataclass

DEVICETREE_BASE = "/sys/firmware/devicetree/base"
IIO_DEVICES_DIR = "/sys/bus/iio/devices"
GPIO_DEVICES_DIR = "/sys/bus/gpio/devices"

ANALOG_INPUTS_NODE = "wirenboard/analog-inputs"
VIN_CHANNEL_NAME = "Vin"
DEFAULT_IIO_CHANNEL_NAME = "voltage0"

GPIOS_NODE = "wirenboard/gpios"
VOUT_GPIO_NODE_NAME = "V_OUT"

_ACTIVE_LOW_FLAG = 1


class DeviceTreeError(Exception):
    """Raised when an expected device-tree node/property, or its matching sysfs device, is missing."""


@dataclass(frozen=True)
class VinChannel:
    """Auto-detected Vin ADC channel."""

    raw_path: str
    divider_ratio: float


@dataclass(frozen=True)
class VoutGpioLine:
    """Auto-detected Vout GPIO line."""

    chip_path: str
    offset: int
    active_low: bool


def find_vin_channel(dt_base: str = DEVICETREE_BASE, iio_devices_dir: str = IIO_DEVICES_DIR) -> VinChannel:
    """Locate the Vin IIO channel's raw-value sysfs file and its divider ratio."""
    node_path = os.path.join(dt_base, ANALOG_INPUTS_NODE, VIN_CHANNEL_NAME)
    if not os.path.isdir(node_path):
        raise DeviceTreeError(f"device tree node /{ANALOG_INPUTS_NODE}/{VIN_CHANNEL_NAME} not found")

    (phandle,) = _read_dt_cells(node_path, "iio-device")
    iio_node_path = _resolve_phandle(dt_base, phandle)
    iio_device_dir = _find_iio_device_dir(iio_devices_dir, dt_base, iio_node_path)
    channel_name = _read_dt_string(node_path, "iio-channel-name") or DEFAULT_IIO_CHANNEL_NAME

    raw_path = os.path.join(iio_device_dir, f"in_{channel_name}_raw")
    if not os.path.isfile(raw_path):
        raise DeviceTreeError(f"no raw-value file for channel '{channel_name}' in {iio_device_dir}")

    return VinChannel(raw_path=raw_path, divider_ratio=_read_divider_ratio(node_path))


def find_vout_gpio_line(
    dt_base: str = DEVICETREE_BASE, gpio_devices_dir: str = GPIO_DEVICES_DIR
) -> VoutGpioLine:
    """Locate the Vout GPIO line by its `VOUT_GPIO_NODE_NAME` device tree node under
    `/wirenboard/gpios`."""
    node_path = os.path.join(dt_base, GPIOS_NODE, VOUT_GPIO_NODE_NAME)
    if not os.path.isdir(node_path):
        raise DeviceTreeError(f"device tree node /{GPIOS_NODE}/{VOUT_GPIO_NODE_NAME} not found")

    phandle, offset, flags = _read_dt_cells(node_path, "io-gpios")
    chip_node_path = _resolve_phandle(dt_base, phandle)
    chip_path = _find_gpio_chip_path(gpio_devices_dir, dt_base, chip_node_path)

    return VoutGpioLine(chip_path=chip_path, offset=offset, active_low=bool(flags & _ACTIVE_LOW_FLAG))


# --- Private ---


def _read_divider_ratio(node_path: str) -> float:
    r1_ohms = _read_dt_u32(node_path, "divider-r1-ohms")
    r2_ohms = _read_dt_u32(node_path, "divider-r2-ohms")
    if r1_ohms is None or r2_ohms is None:
        return 1.0
    return (r1_ohms + r2_ohms) / r2_ohms


def _find_iio_device_dir(iio_devices_dir: str, dt_base: str, node_path: str) -> str:
    target = os.path.join(dt_base, node_path.lstrip("/"))
    if not os.path.isdir(iio_devices_dir):
        raise DeviceTreeError(f"no IIO devices found under {iio_devices_dir}")

    for entry in sorted(os.listdir(iio_devices_dir)):
        device_dir = os.path.join(iio_devices_dir, entry)
        if _of_node_points_to(os.path.join(device_dir, "of_node"), target):
            return device_dir

    raise DeviceTreeError(f"no IIO device matches device tree node {node_path}")


def _find_gpio_chip_path(gpio_devices_dir: str, dt_base: str, node_path: str) -> str:
    target = os.path.join(dt_base, node_path.lstrip("/"))
    if not os.path.isdir(gpio_devices_dir):
        raise DeviceTreeError(f"no GPIO chips found under {gpio_devices_dir}")

    for entry in sorted(os.listdir(gpio_devices_dir)):
        chip_dir = os.path.join(gpio_devices_dir, entry)
        for of_node in (os.path.join(chip_dir, "of_node"), os.path.join(chip_dir, "device", "of_node")):
            if _of_node_points_to(of_node, target):
                return os.path.join("/dev", entry)

    raise DeviceTreeError(f"no GPIO chip matches device tree node {node_path}")


def _of_node_points_to(of_node: str, target: str) -> bool:
    """Whether the `of_node` symlink of a sysfs device resolves to the device-tree `target`."""
    return os.path.islink(of_node) and os.path.realpath(of_node) == os.path.realpath(target)


def _resolve_phandle(dt_base: str, phandle: int) -> str:
    for root, dirs, _files in os.walk(dt_base):
        dirs.sort()
        phandle_path = os.path.join(root, "phandle")
        if os.path.isfile(phandle_path) and _read_u32(phandle_path) == phandle:
            return "/" + os.path.relpath(root, dt_base)

    raise DeviceTreeError(f"no device tree node with phandle {phandle}")


def _read_dt_cells(node_path: str, prop_name: str) -> tuple:
    data = _read_property_bytes(node_path, prop_name)
    if len(data) % 4 != 0:
        raise DeviceTreeError(f"property {prop_name} in {node_path} is not a whole number of cells")
    return struct.unpack(f">{len(data) // 4}I", data)


def _read_dt_u32(node_path: str, prop_name: str):
    prop_path = os.path.join(node_path, prop_name)
    if not os.path.isfile(prop_path):
        return None
    return _read_u32(prop_path)


def _read_u32(path: str) -> int:
    with open(path, "rb") as prop_file:
        data = prop_file.read(4)
    return struct.unpack(">I", data)[0]


def _read_dt_string(node_path: str, prop_name: str):
    prop_path = os.path.join(node_path, prop_name)
    if not os.path.isfile(prop_path):
        return None
    with open(prop_path, "rb") as prop_file:
        return prop_file.read().rstrip(b"\x00").decode("ascii")


def _read_property_bytes(node_path: str, prop_name: str) -> bytes:
    prop_path = os.path.join(node_path, prop_name)
    if not os.path.isfile(prop_path):
        raise DeviceTreeError(f"property '{prop_name}' not found in {node_path}")
    with open(prop_path, "rb") as prop_file:
        return prop_file.read()
