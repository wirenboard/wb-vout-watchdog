"""Integration-style tests for `Service`: wires the real `PowerLogic`/`VinReader`/`VoutGpio`/
`MqttDevice` together, with fakes only at the true I/O boundaries (device tree/sysfs via real
temp files, the GPIO chip's `os.open`/`fcntl.ioctl` and `paho.mqtt.client.Client` via the fakes
in conftest.py).
No real broker or GPIO chip is involved: an "emulated sysfs + local broker" integration style,
adapted to this project's dependency-injection seams instead of spawning a real mosquitto
(not a build dependency of this package).

`Service` runs its main loop on a background thread, the way it would run under systemd;
tests interact with it exactly as the outside world would -- by writing to the Vin sysfs file
and by driving the fake MQTT client's callbacks (which are simply the public
`MqttDevice.on_connect`/`on_message` methods, the same ones paho-mqtt would call) -- never by
reaching into `Service`'s own attributes.
"""

import logging
import socket
import subprocess
import threading
import time

import pytest

import wb_vout_watchdog.devicetree as devicetree_module
import wb_vout_watchdog.mqtt as mqtt_module
import wb_vout_watchdog.service as service_module
from tests.conftest import (
    BusyBehaviour,
    FakeMessage,
    FakeMqttClient,
    FakeReasonCode,
    latest_meta,
    patch_gpio_chip,
)
from wb_vout_watchdog.config import Config
from wb_vout_watchdog.devicetree import VinChannel, VoutGpioLine
from wb_vout_watchdog.gpio import GpioBusyError, VoutGpio
from wb_vout_watchdog.gpio_cdev import Value
from wb_vout_watchdog.mqtt import DEVICE_TOPIC_PREFIX, Control, command_topic
from wb_vout_watchdog.power_logic import PowerThresholds
from wb_vout_watchdog.service import (
    UNDERVOLTAGE_MARKER_FILENAME,
    VOUT_STATE_FILENAME,
    Service,
    capture_vout_line,
    notify_systemd_ready,
)

FAST_CONFIG = Config(
    thresholds=PowerThresholds(
        alarm_threshold_v=20.0,
        min_low_voltage_duration_s=0.05,
        battery_backup_threshold_v=11.0,
    ),
    adc_poll_period_s=0.02,
    adc_error_threshold=3,
    heartbeat_period_s=0.5,
)

WAIT_TIMEOUT_S = 3.0

# Long enough for several FAST_CONFIG poll/duration periods to elapse, so "nothing happened
# during this window" assertions are meaningful; short enough not to drag the suite out.
NO_REACTION_WINDOW_S = 0.2


def wait_until(predicate, timeout=WAIT_TIMEOUT_S, interval=0.01):
    """Polls `predicate` until it's truthy. Tolerates exceptions while polling (e.g. a
    predicate indexing into a list the background thread hasn't populated yet) -- those only
    mean "not ready yet", same as a falsy result; only a final timeout is a real failure."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if predicate():
                return
        except (IndexError, AttributeError, StopIteration):
            pass
        time.sleep(interval)
    raise AssertionError(f"condition not met within {timeout}s")


def latest_value(fake_client, control: str):
    topic = f"{DEVICE_TOPIC_PREFIX}/controls/{control}"
    for published_topic, payload, _retain, _qos in reversed(fake_client.published):
        if published_topic == topic:
            return payload
    return None


def control_values(fake_client, control: str):
    """Every payload published to the control's value topic so far, in publish order -- for
    asserting on transition sequences that a `latest_value` poll could race past."""
    topic = f"{DEVICE_TOPIC_PREFIX}/controls/{control}"
    return [payload for published_topic, payload, *_ in fake_client.published if published_topic == topic]


def send_command(fake_client, control: Control, payload: bytes) -> None:
    fake_client.on_message(fake_client, None, FakeMessage(topic=command_topic(control), payload=payload))


def set_vin_volts(raw_path, volts: float) -> None:
    # scale is fixed at 1.0 mV/count and divider_ratio at 1.0 in vin_channel below, so
    # raw counts (in mV) equal volts * 1000.
    raw_path.write_text(str(int(volts * 1000)))


@pytest.fixture(name="vin_channel")
def _vin_channel_fixture(tmp_path):
    raw_path = tmp_path / "in_voltage3_raw"
    scale_path = tmp_path / "in_voltage3_scale"
    scale_path.write_text("1.0")
    set_vin_volts(raw_path, 24.0)
    return VinChannel(raw_path=str(raw_path), divider_ratio=1.0), raw_path


@pytest.fixture(name="vout_line")
def _vout_line_fixture():
    return VoutGpioLine(chip_path="/dev/gpiochip3", offset=5, active_low=False)


@pytest.fixture(name="gpio_chip")
def _gpio_chip_fixture(monkeypatch):
    """The Vout line as the service finds it on a normal start: free to capture and off."""
    return patch_gpio_chip(monkeypatch, initial_value=Value.INACTIVE)


@pytest.fixture(name="service")
def _service_fixture(monkeypatch, tmp_path, vin_channel, vout_line, gpio_chip):
    del gpio_chip  # requested for the patching alone; tests that inspect it ask for it too
    channel, _raw_path = vin_channel
    monkeypatch.setattr(devicetree_module, "find_vin_channel", lambda: channel)
    monkeypatch.setattr(devicetree_module, "find_vout_gpio_line", lambda: vout_line)
    monkeypatch.setattr(mqtt_module.mqtt, "Client", FakeMqttClient)

    # No marker file under `tmp_path` unless a test creates one -- same as a fresh install with
    # no prior undervoltage state (see TestUndervoltageMarkerPersistence).
    return Service(FAST_CONFIG, state_directory=str(tmp_path))


@pytest.fixture(name="running_service")
def _running_service_fixture(service, gpio_chip):
    thread = threading.Thread(target=service.run, daemon=True)
    thread.start()
    wait_until(lambda: latest_value(FakeMqttClient.instances[-1], "vin") is not None)
    yield service, FakeMqttClient.instances[-1], gpio_chip
    service.stop()
    thread.join(timeout=WAIT_TIMEOUT_S)


class TestStartup:
    def test_line_found_off_is_left_off_and_full_state_is_published(self, running_service):
        _service, fake_client, fake_gpio = running_service

        assert fake_gpio.value is Value.INACTIVE
        assert latest_value(fake_client, "undervoltage") == "0"
        assert latest_value(fake_client, "V_OUT") == "0"
        assert latest_meta(fake_client, Control.VOUT)["readonly"] is False
        assert latest_value(fake_client, "vin") == "24.00"

    def test_line_found_on_is_driven_off_without_a_persisted_vout_state(
        self, monkeypatch, tmp_path, vin_channel, vout_line
    ):
        """Flag down and no persisted Vout state (no vout file): startup restores Vout to off,
        so a line found physically on at capture is driven off, without raising undervoltage."""
        channel, _raw_path = vin_channel
        monkeypatch.setattr(devicetree_module, "find_vin_channel", lambda: channel)
        monkeypatch.setattr(devicetree_module, "find_vout_gpio_line", lambda: vout_line)
        monkeypatch.setattr(mqtt_module.mqtt, "Client", FakeMqttClient)
        chip = patch_gpio_chip(monkeypatch, initial_value=Value.ACTIVE)

        service = Service(FAST_CONFIG, state_directory=str(tmp_path))
        thread = threading.Thread(target=service.run, daemon=True)
        thread.start()
        try:
            wait_until(lambda: chip.value is Value.INACTIVE)
            fake_client = FakeMqttClient.instances[-1]
            assert latest_value(fake_client, "V_OUT") == "0"
            assert latest_value(fake_client, "undervoltage") == "0"  # not a Vin-based alarm
        finally:
            service.stop()
            thread.join(timeout=WAIT_TIMEOUT_S)

    def test_start_with_vout_state_file_restores_vout_on(self, service, gpio_chip, tmp_path):
        """Flag down and a persisted Vout state file present: startup restores Vout on (like
        wb-mqtt-gpio's load_previous_state), driving the line on without a fresh command. The
        `service` fixture points `Service` at this `tmp_path` with a line captured off and no
        marker; the vout file is read in `run()`, so creating it here before start restores on."""
        (tmp_path / VOUT_STATE_FILENAME).write_text("")

        thread = threading.Thread(target=service.run, daemon=True)
        thread.start()
        try:
            wait_until(lambda: latest_value(FakeMqttClient.instances[-1], "V_OUT") == "1")
            assert gpio_chip.value is Value.ACTIVE
            assert latest_value(FakeMqttClient.instances[-1], "undervoltage") == "0"
        finally:
            service.stop()
            thread.join(timeout=WAIT_TIMEOUT_S)

    def test_start_with_flag_and_vout_both_persisted_does_not_re_power_vout(
        self, service, gpio_chip, tmp_path
    ):
        """After a latched alarm, a restart must not re-power Vout even though the vout state
        file says it was on: the raised flag forces the line off and keeps it blocked."""
        (tmp_path / UNDERVOLTAGE_MARKER_FILENAME).write_text("")
        (tmp_path / VOUT_STATE_FILENAME).write_text("")

        thread = threading.Thread(target=service.run, daemon=True)
        thread.start()
        try:
            wait_until(lambda: latest_value(FakeMqttClient.instances[-1], "undervoltage") == "1")
            time.sleep(NO_REACTION_WINDOW_S)
            assert gpio_chip.value is Value.INACTIVE
            assert latest_value(FakeMqttClient.instances[-1], "V_OUT") == "0"
        finally:
            service.stop()
            thread.join(timeout=WAIT_TIMEOUT_S)

    def test_transient_gpio_write_error_is_retried_not_crashed(
        self, monkeypatch, tmp_path, vin_channel, vout_line
    ):
        """A transient `OSError` from the GPIO write must not crash the loop: startup restore
        drives Vout on, the first write fails, and the retry on the next loop pass lands it --
        the line ends up driven and the run thread stays alive."""
        channel, _raw_path = vin_channel
        (tmp_path / VOUT_STATE_FILENAME).write_text("")  # restore on -> a set that first fails
        monkeypatch.setattr(devicetree_module, "find_vin_channel", lambda: channel)
        monkeypatch.setattr(devicetree_module, "find_vout_gpio_line", lambda: vout_line)
        monkeypatch.setattr(mqtt_module.mqtt, "Client", FakeMqttClient)
        chip = patch_gpio_chip(monkeypatch, initial_value=Value.INACTIVE, set_failures=1)

        service = Service(FAST_CONFIG, state_directory=str(tmp_path))
        thread = threading.Thread(target=service.run, daemon=True)
        thread.start()
        try:
            wait_until(lambda: chip.value is Value.ACTIVE)
            assert thread.is_alive()
        finally:
            service.stop()
            thread.join(timeout=WAIT_TIMEOUT_S)


class TestFullCycle:
    def test_alarm_latches_then_unlock_then_switch_on(self, running_service, vin_channel):
        """The full Scenario 1->3->4 cycle: a sustained low voltage cuts Vout and raises the flag; Vin
        recovery on its own changes nothing (the flag latches); `enable_vout` clears the flag
        without touching Vout; a separate `V_OUT` write finally turns it on."""
        _service, fake_client, fake_gpio = running_service
        _channel, raw_path = vin_channel

        set_vin_volts(raw_path, 15.0)  # normal-zone low voltage: between battery and alarm thresholds
        wait_until(
            lambda: fake_gpio.value is Value.INACTIVE and latest_value(fake_client, "undervoltage") == "1"
        )

        set_vin_volts(raw_path, 24.0)  # Vin recovers: must not clear anything by itself
        wait_until(lambda: latest_value(fake_client, "vin") == "24.00")
        time.sleep(NO_REACTION_WINDOW_S)
        assert latest_value(fake_client, "undervoltage") == "1"  # the flag latches
        assert fake_gpio.value is Value.INACTIVE

        send_command(fake_client, Control.ENABLE_VOUT, b"1")
        wait_until(lambda: latest_value(fake_client, "undervoltage") == "0")
        assert fake_gpio.value is Value.INACTIVE  # unlocking never enables Vout by itself

        send_command(fake_client, Control.VOUT, b"1")
        wait_until(lambda: fake_gpio.value is Value.ACTIVE)
        assert latest_value(fake_client, "V_OUT") == "1"

    def test_vout_write_is_ignored_while_the_flag_is_up(self, running_service, vin_channel):
        """A `V_OUT/on` command arriving while `undervoltage` is set must change nothing --
        neither the physical line nor the published control value."""
        _service, fake_client, fake_gpio = running_service
        _channel, raw_path = vin_channel

        set_vin_volts(raw_path, 15.0)
        wait_until(lambda: latest_value(fake_client, "undervoltage") == "1")

        send_command(fake_client, Control.VOUT, b"1")
        time.sleep(NO_REACTION_WINDOW_S)

        assert fake_gpio.value is Value.INACTIVE
        assert latest_value(fake_client, "V_OUT") == "0"

    def test_unlock_without_a_raised_flag_is_a_no_op(self, running_service):
        """`enable_vout` when there is nothing to confirm must not enable Vout or change any
        published state."""
        _service, fake_client, fake_gpio = running_service

        send_command(fake_client, Control.ENABLE_VOUT, b"1")
        time.sleep(NO_REACTION_WINDOW_S)

        assert fake_gpio.value is Value.INACTIVE
        assert latest_value(fake_client, "V_OUT") == "0"
        assert latest_value(fake_client, "undervoltage") == "0"

    def test_realarm_fires_again_when_the_low_voltage_persists_across_an_unlock(
        self, running_service, vin_channel
    ):
        """Scenario 4's re-alarm case: unlocking during a continuing low voltage is accepted (the flag
        drops to 0), but the alarm immediately fires again -- the published `undervoltage`
        sequence ends with 1 -> 0 -> 1."""
        _service, fake_client, _fake_gpio = running_service
        _channel, raw_path = vin_channel

        set_vin_volts(raw_path, 15.0)
        wait_until(lambda: latest_value(fake_client, "undervoltage") == "1")

        send_command(fake_client, Control.ENABLE_VOUT, b"1")

        wait_until(lambda: control_values(fake_client, "undervoltage")[-3:] == ["1", "0", "1"])

    def test_battery_backup_low_voltage_does_not_touch_vout_or_undervoltage(
        self, running_service, vin_channel
    ):
        _service, fake_client, fake_gpio = running_service
        _channel, raw_path = vin_channel

        set_vin_volts(raw_path, 5.0)  # below the battery-backup threshold
        wait_until(lambda: latest_value(fake_client, "vin") == "5.00")
        time.sleep(NO_REACTION_WINDOW_S)

        assert fake_gpio.value is Value.INACTIVE  # unchanged (it started off, and stays off)
        assert latest_value(fake_client, "undervoltage") == "0"


class TestDynamicVoutReadonly:
    def test_vout_meta_flips_to_readonly_on_alarm(self, running_service, vin_channel):
        """When the alarm raises the flag, the `V_OUT` meta must be republished with
        `readonly=true`, so homeui hides the toggle."""
        _service, fake_client, _fake_gpio = running_service
        _channel, raw_path = vin_channel

        assert latest_meta(fake_client, Control.VOUT)["readonly"] is False

        set_vin_volts(raw_path, 15.0)
        wait_until(lambda: latest_meta(fake_client, Control.VOUT)["readonly"] is True)

    def test_vout_meta_flips_back_to_writable_on_unlock(self, running_service, vin_channel):
        """When `enable_vout` clears the flag, the `V_OUT` meta must be republished with
        `readonly=false`, making the switch operable again."""
        _service, fake_client, _fake_gpio = running_service
        _channel, raw_path = vin_channel

        set_vin_volts(raw_path, 15.0)
        wait_until(lambda: latest_meta(fake_client, Control.VOUT)["readonly"] is True)

        set_vin_volts(raw_path, 24.0)  # end the low voltage so the alarm doesn't immediately re-fire
        wait_until(lambda: latest_value(fake_client, "vin") == "24.00")
        send_command(fake_client, Control.ENABLE_VOUT, b"1")
        wait_until(lambda: latest_meta(fake_client, Control.VOUT)["readonly"] is False)


class TestUndervoltageMarkerPersistence:
    """Scenario 5: the marker file's mere existence under the state directory persists
    `undervoltage` across a restart -- its content is never read or written. `running_service`
    (via the `service` fixture) already points `Service` at `tmp_path` with no marker file
    pre-created, so `tmp_path` here is the exact same directory `Service` was constructed with.
    """

    @pytest.mark.parametrize("marker_content", ["", "garbage-not-json{{{"])
    def test_start_with_marker_file_begins_with_undervoltage_set(self, service, tmp_path, marker_content):
        """A marker file left over from a previous run makes the very first published state show
        `undervoltage=1` (and `V_OUT` readonly), without waiting through a fresh alarm duration.
        Its content is never read -- only its existence is checked -- so an empty file and one
        holding arbitrary garbage behave identically. The `service` fixture points `Service` at
        this same `tmp_path` (state directory) with healthy Vin and no marker pre-created; the
        marker is read in `run()`, so creating it here before starting the thread seeds the flag.
        """
        (tmp_path / UNDERVOLTAGE_MARKER_FILENAME).write_text(marker_content)

        thread = threading.Thread(target=service.run, daemon=True)
        thread.start()
        try:
            wait_until(lambda: latest_value(FakeMqttClient.instances[-1], "vin") is not None)
            assert latest_value(FakeMqttClient.instances[-1], "undervoltage") == "1"
            assert latest_meta(FakeMqttClient.instances[-1], Control.VOUT)["readonly"] is True
        finally:
            service.stop()
            thread.join(timeout=WAIT_TIMEOUT_S)

    def test_marker_file_appears_on_alarm_and_disappears_only_on_unlock(
        self, running_service, tmp_path, vin_channel
    ):
        """Exercises the write side of persistence: the file is created when the alarm raises
        the flag, survives a Vin recovery (the flag latches), and is removed only when
        `enable_vout` clears the flag."""
        _service, fake_client, _fake_gpio = running_service
        _channel, raw_path = vin_channel
        marker_path = tmp_path / UNDERVOLTAGE_MARKER_FILENAME

        assert not marker_path.exists()

        set_vin_volts(raw_path, 15.0)  # normal-zone low voltage
        wait_until(lambda: latest_value(fake_client, "undervoltage") == "1")
        assert marker_path.exists()

        set_vin_volts(raw_path, 24.0)  # Vin recovery does not clear the latched flag
        wait_until(lambda: latest_value(fake_client, "vin") == "24.00")
        time.sleep(NO_REACTION_WINDOW_S)
        assert marker_path.exists()

        send_command(fake_client, Control.ENABLE_VOUT, b"1")
        wait_until(lambda: latest_value(fake_client, "undervoltage") == "0")
        assert not marker_path.exists()

    def test_marker_write_failure_does_not_crash_and_undervoltage_is_still_published(
        self, monkeypatch, caplog, vin_channel, vout_line
    ):
        """The state directory does not exist, so the undervoltage `PresenceFlagFile.set()`
        `open()` fails with `FileNotFoundError` (an `OSError` subclass) on the first alarm.
        Persistence is best-effort: the service must keep running and still publish
        `undervoltage=1` over MQTT (Scenario 1), and log a warning about the failed write."""
        channel, raw_path = vin_channel
        set_vin_volts(raw_path, 15.0)  # normal-zone low voltage: crosses the alarm threshold
        monkeypatch.setattr(devicetree_module, "find_vin_channel", lambda: channel)
        monkeypatch.setattr(devicetree_module, "find_vout_gpio_line", lambda: vout_line)
        monkeypatch.setattr(mqtt_module.mqtt, "Client", FakeMqttClient)
        patch_gpio_chip(monkeypatch, initial_value=Value.INACTIVE)
        caplog.set_level(logging.WARNING)

        # "missing" is never created next to raw_path, so the marker path's parent directory
        # does not exist.
        service = Service(FAST_CONFIG, state_directory=str(raw_path.parent / "missing"))
        thread = threading.Thread(target=service.run, daemon=True)
        thread.start()
        try:
            wait_until(lambda: latest_value(FakeMqttClient.instances[-1], "undervoltage") == "1")
        finally:
            service.stop()
            thread.join(timeout=WAIT_TIMEOUT_S)

        assert "failed to update state file" in caplog.text


class TestBusyLineRecovery:
    """`capture_vout_line`: recovering from a lost startup race against wb-mqtt-gpio by
    stopping it (via `systemctl`, mocked here), retrying the capture once while the line is
    guaranteed free, and starting it back in every case."""

    def test_busy_line_stops_wb_mqtt_gpio_recaptures_and_starts_it_back(
        self, monkeypatch, subprocess_commands, vout_line
    ):
        """First capture attempt hits EBUSY; the conflicting service is stopped, the retried
        capture succeeds, and the service is started back -- exactly one stop followed by
        exactly one start.

        The stop blocks (the retry needs the line released); the start does not, because it is
        issued from inside this service's own startup and the unit is ordered before
        wb-mqtt-gpio -- a blocking start job would wait for a readiness this call is holding up.
        """
        chip = patch_gpio_chip(monkeypatch, initial_value=Value.INACTIVE, busy=BusyBehaviour.FIRST_ATTEMPT)

        capture_vout_line(VoutGpio(vout_line))

        assert subprocess_commands == [
            ["systemctl", "stop", "wb-mqtt-gpio.service"],
            ["systemctl", "--no-block", "start", "wb-mqtt-gpio.service"],
        ]
        assert len(chip.captures) == 1  # the one successful (retried) capture

    def test_line_still_busy_after_the_stop_is_fatal_but_the_driver_is_started_back(
        self, monkeypatch, subprocess_commands, vout_line
    ):
        """If the retry hits EBUSY again, the error propagates (fatal startup error, systemd
        takes over) -- but wb-mqtt-gpio is still started back, so its other channels keep
        working while this service sits in failed/restarting."""
        patch_gpio_chip(monkeypatch, initial_value=Value.INACTIVE, busy=BusyBehaviour.ALWAYS)

        with pytest.raises(GpioBusyError):
            capture_vout_line(VoutGpio(vout_line))

        assert subprocess_commands == [
            ["systemctl", "stop", "wb-mqtt-gpio.service"],
            ["systemctl", "--no-block", "start", "wb-mqtt-gpio.service"],
        ]

    def test_failed_stop_is_logged_and_the_capture_is_still_retried(self, monkeypatch, caplog, vout_line):
        """A `systemctl stop` failure (here: a timeout) must not raise out of
        `capture_vout_line` -- it is logged and the one retry still happens (and may well
        succeed, e.g. if the line was freed meanwhile)."""

        def run_times_out(command, **kwargs):
            del kwargs
            raise subprocess.TimeoutExpired(cmd=command, timeout=1.0)

        chip = patch_gpio_chip(monkeypatch, initial_value=Value.ACTIVE, busy=BusyBehaviour.FIRST_ATTEMPT)
        monkeypatch.setattr(service_module.subprocess, "run", run_times_out)
        caplog.set_level(logging.WARNING)

        capture_vout_line(VoutGpio(vout_line))

        assert len(chip.captures) == 1  # the retry captured the line
        assert "could not stop wb-mqtt-gpio.service" in caplog.text


class TestNotifySystemdReady:
    def test_without_notify_socket_env_it_is_a_no_op(self, monkeypatch):
        """Outside systemd (or without `Type=notify`) there is no `$NOTIFY_SOCKET`; the
        function must simply return without raising."""
        monkeypatch.delenv("NOTIFY_SOCKET", raising=False)

        notify_systemd_ready()

    def test_ready_datagram_is_sent_to_the_notify_socket(self, monkeypatch, tmp_path):
        """With `$NOTIFY_SOCKET` pointing at a bound unix datagram socket (the way systemd
        exposes it to `Type=notify` units), exactly a `READY=1` datagram must arrive."""
        socket_path = str(tmp_path / "notify.sock")
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as server:
            server.bind(socket_path)
            server.settimeout(WAIT_TIMEOUT_S)
            monkeypatch.setenv("NOTIFY_SOCKET", socket_path)

            notify_systemd_ready()

            assert server.recv(64) == b"READY=1"

    def test_ready_datagram_is_sent_to_an_abstract_namespace_socket(self, monkeypatch, tmp_path):
        """systemd commonly hands `Type=notify` units an abstract-namespace socket, advertised
        in `$NOTIFY_SOCKET` with a leading `@` standing in for the NUL byte (per sd_notify(3)).
        `notify_systemd_ready` must rewrite that `@` to `\\0` and deliver `READY=1` there --
        the realistic production path, distinct from the filesystem-path case above."""
        abstract_name = "wb-vout-watchdog-test-" + tmp_path.name
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as server:
            server.bind("\0" + abstract_name)
            server.settimeout(WAIT_TIMEOUT_S)
            monkeypatch.setenv("NOTIFY_SOCKET", "@" + abstract_name)

            notify_systemd_ready()

            assert server.recv(64) == b"READY=1"

    def test_unreachable_notify_socket_is_logged_and_does_not_raise(self, monkeypatch, tmp_path, caplog):
        """A socket error (nothing bound at the advertised path) must not take the watchdog
        down -- readiness signalling is best-effort, the failure is only logged."""
        monkeypatch.setenv("NOTIFY_SOCKET", str(tmp_path / "nothing-bound-here.sock"))
        caplog.set_level(logging.WARNING)

        notify_systemd_ready()

        assert "could not notify systemd of readiness" in caplog.text


def test_mqtt_reconnect_republishes_full_state_and_a_fresh_heartbeat(running_service):
    """A broker reconnect (a fresh `on_connect` after everything already published is lost
    with the old session) must make the service republish every control's current state."""
    _service, fake_client, _fake_gpio = running_service
    fake_client.published.clear()

    fake_client.on_connect(fake_client, None, None, FakeReasonCode(is_failure=False))

    wait_until(lambda: latest_value(fake_client, "heartbeat") is not None)
    assert latest_value(fake_client, "undervoltage") == "0"
    assert latest_value(fake_client, "V_OUT") == "0"
    assert latest_meta(fake_client, Control.VOUT)["readonly"] is False
    assert latest_value(fake_client, "vin") == "24.00"


def test_stop_clears_the_device_retained_topics(service):
    """A graceful stop must delete the device's retained topics from the broker (empty retained
    publishes), so a stopped service doesn't leave a dead device panel in homeui."""
    thread = threading.Thread(target=service.run, daemon=True)
    thread.start()
    wait_until(lambda: latest_value(FakeMqttClient.instances[-1], "vin") is not None)

    service.stop()
    thread.join(timeout=WAIT_TIMEOUT_S)

    fake_client = FakeMqttClient.instances[-1]
    cleared = {topic for topic, payload, retain, _qos in fake_client.published if payload is None and retain}
    assert f"{DEVICE_TOPIC_PREFIX}/meta" in cleared
    assert f"{DEVICE_TOPIC_PREFIX}/controls/undervoltage" in cleared
    assert f"{DEVICE_TOPIC_PREFIX}/controls/V_OUT" in cleared
    assert f"{DEVICE_TOPIC_PREFIX}/controls/V_OUT/meta" in cleared
    assert f"{DEVICE_TOPIC_PREFIX}/controls/vin/meta/error" in cleared


def test_stop_makes_run_return_promptly(service):
    thread = threading.Thread(target=service.run, daemon=True)
    thread.start()
    wait_until(lambda: FakeMqttClient.instances)

    service.stop()
    thread.join(timeout=WAIT_TIMEOUT_S)

    assert not thread.is_alive()
