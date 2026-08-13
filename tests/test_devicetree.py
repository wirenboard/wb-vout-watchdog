"""Unit tests for device-tree auto-detection of the Vin ADC channel and Vout GPIO line."""

import os

import pytest

from tests.conftest import DividerOhms, GpioNodeSpec, VinNodeSpec, make_dt_fixture
from wb_vout_watchdog.devicetree import (
    DeviceTreeError,
    VinChannel,
    VoutGpioLine,
    find_vin_channel,
    find_vout_gpio_line,
)


class TestFindVinChannel:
    def test_finds_channel_with_divider(self, tmp_path):
        """A Vin node with a resistor divider yields the raw sysfs path and (r1+r2)/r2 ratio."""
        fixture = make_dt_fixture(tmp_path, vin=VinNodeSpec(divider=DividerOhms(390000, 10000)))

        channel = find_vin_channel(fixture.dt_base, fixture.iio_devices_dir)

        assert channel == VinChannel(
            raw_path=f"{fixture.iio_devices_dir}/iio:device0/in_voltage0_raw",
            divider_ratio=(390000 + 10000) / 10000,
        )

    def test_finds_channel_without_divider(self, tmp_path):
        """No divider properties on the Vin node means no scaling: ratio is exactly 1.0."""
        fixture = make_dt_fixture(tmp_path, vin=VinNodeSpec(divider=None))

        channel = find_vin_channel(fixture.dt_base, fixture.iio_devices_dir)

        assert channel.divider_ratio == 1.0

    def test_honors_an_explicit_iio_channel_name(self, tmp_path):
        """`iio-channel-name` overrides the default `voltage0` channel name -- e.g. for a
        differential channel named in_voltageN-voltageM_raw."""
        fixture = make_dt_fixture(
            tmp_path,
            vin=VinNodeSpec(
                iio_channel_name="voltage3-voltage5", raw_filename="in_voltage3-voltage5_raw", raw_value="100"
            ),
        )

        channel = find_vin_channel(fixture.dt_base, fixture.iio_devices_dir)

        assert channel.raw_path == f"{fixture.iio_devices_dir}/iio:device0/in_voltage3-voltage5_raw"

    def test_missing_vin_node_raises(self, tmp_path):
        fixture = make_dt_fixture(tmp_path, vin=VinNodeSpec(present=False))

        with pytest.raises(DeviceTreeError):
            find_vin_channel(fixture.dt_base, fixture.iio_devices_dir)

    def test_missing_raw_file_raises(self, tmp_path):
        """The phandle resolves to a real IIO device, but it doesn't expose the expected channel."""
        fixture = make_dt_fixture(
            tmp_path, vin=VinNodeSpec(raw_filename=None, raw_value=None, scale_value=None)
        )

        with pytest.raises(DeviceTreeError):
            find_vin_channel(fixture.dt_base, fixture.iio_devices_dir)

    def test_unresolvable_phandle_raises(self, tmp_path):
        """iio-device points at a phandle that no device tree node actually has."""
        fixture = make_dt_fixture(tmp_path, vin=VinNodeSpec(phandle=7))
        # Break the resolution by pointing the ADC node's own phandle property elsewhere.
        adc_phandle_path = f"{fixture.dt_base}/soc/adc@0/phandle"
        with open(adc_phandle_path, "wb") as phandle_file:
            phandle_file.write((123).to_bytes(4, "big"))

        with pytest.raises(DeviceTreeError):
            find_vin_channel(fixture.dt_base, fixture.iio_devices_dir)

    def test_no_iio_device_matching_the_node_raises(self, tmp_path):
        """The `iio-device` phandle resolves to a real device tree node, but no sysfs IIO
        device links back to it (`of_node` removed) -- a board-misconfiguration failure that
        must be distinguished from the node simply being absent."""
        fixture = make_dt_fixture(tmp_path)
        os.remove(f"{fixture.iio_devices_dir}/iio:device0/of_node")

        with pytest.raises(DeviceTreeError):
            find_vin_channel(fixture.dt_base, fixture.iio_devices_dir)


class TestFindVoutGpioLine:
    def test_finds_line_by_node_name(self, tmp_path):
        fixture = make_dt_fixture(tmp_path, gpio=GpioNodeSpec(offset=5, flags=0, gpiochip_name="gpiochip3"))

        line = find_vout_gpio_line(fixture.dt_base, fixture.gpio_devices_dir)

        assert line == VoutGpioLine(chip_path="/dev/gpiochip3", offset=5, active_low=False)

    def test_active_low_flag_is_decoded(self, tmp_path):
        """Bit 0 of the io-gpios specifier's flags cell is GPIO_ACTIVE_LOW."""
        fixture = make_dt_fixture(tmp_path, gpio=GpioNodeSpec(flags=1))

        line = find_vout_gpio_line(fixture.dt_base, fixture.gpio_devices_dir)

        assert line.active_low is True

    def test_missing_node_name_raises(self, tmp_path):
        """A device tree with gpios present, but none of them named `V_OUT`, is indistinguishable
        from the line simply not existing on this board."""
        fixture = make_dt_fixture(tmp_path, gpio=GpioNodeSpec(node_name="SOME_OTHER_NODE"))

        with pytest.raises(DeviceTreeError):
            find_vout_gpio_line(fixture.dt_base, fixture.gpio_devices_dir)

    def test_missing_gpios_node_raises(self, tmp_path):
        fixture = make_dt_fixture(tmp_path, gpio=GpioNodeSpec(present=False))

        with pytest.raises(DeviceTreeError):
            find_vout_gpio_line(fixture.dt_base, fixture.gpio_devices_dir)

    def test_no_gpio_chip_matching_the_node_raises(self, tmp_path):
        """The `io-gpios` phandle resolves, but no sysfs GPIO chip links back to that node
        (`of_node` removed) -- a real chip-not-found failure, distinct from the node absent."""
        fixture = make_dt_fixture(tmp_path, gpio=GpioNodeSpec(gpiochip_name="gpiochip3"))
        os.remove(f"{fixture.gpio_devices_dir}/gpiochip3/of_node")

        with pytest.raises(DeviceTreeError):
            find_vout_gpio_line(fixture.dt_base, fixture.gpio_devices_dir)

    def test_missing_io_gpios_property_raises(self, tmp_path):
        """The V_OUT node exists but carries no `io-gpios` specifier at all."""
        fixture = make_dt_fixture(tmp_path)
        os.remove(f"{fixture.dt_base}/wirenboard/gpios/V_OUT/io-gpios")

        with pytest.raises(DeviceTreeError):
            find_vout_gpio_line(fixture.dt_base, fixture.gpio_devices_dir)

    def test_malformed_io_gpios_property_raises(self, tmp_path):
        """An `io-gpios` value whose byte length is not a whole number of 4-byte cells is
        rejected rather than silently mis-unpacked."""
        fixture = make_dt_fixture(tmp_path)
        with open(f"{fixture.dt_base}/wirenboard/gpios/V_OUT/io-gpios", "wb") as io_gpios_file:
            io_gpios_file.write(b"\x00\x00\x00\x09\x00")  # 5 bytes: not a whole number of cells

        with pytest.raises(DeviceTreeError):
            find_vout_gpio_line(fixture.dt_base, fixture.gpio_devices_dir)
