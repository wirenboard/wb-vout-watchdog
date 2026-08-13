import logging
import os
import queue
import socket
import subprocess
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import NamedTuple, Optional, Union

from wb_vout_watchdog import devicetree
from wb_vout_watchdog.adc import VinReader
from wb_vout_watchdog.config import Config
from wb_vout_watchdog.gpio import GpioBusyError, VoutGpio
from wb_vout_watchdog.mqtt import MqttConnected, MqttDevice
from wb_vout_watchdog.power_logic import (
    EnableVoutRequested,
    LogicOutput,
    PowerLogic,
    VinReadError,
    VinSample,
    VoutStartupRestore,
    VoutSwitchRequested,
)

MAIN_LOOP_TICK_S = 0.2

# Standard FHS runtime-state location for this daemon. Not packaged and not created by this
# code: `StateDirectory=` in the systemd unit makes systemd create it (with the right
# ownership/permissions) before the process starts.
STATE_DIRECTORY = "/var/lib/wb-vout-watchdog"
UNDERVOLTAGE_MARKER_FILENAME = "undervoltage"
VOUT_STATE_FILENAME = "vout"

# The one service that may legitimately hold the Vout line when this one starts: despite the
# unit's `Before=wb-mqtt-gpio.service` ordering, independent simultaneous restarts of both
# services can race, and losing that race is recoverable
# by stopping wb-mqtt-gpio, capturing the line while it's down, and starting it back.
CONFLICTING_GPIO_SERVICE = "wb-mqtt-gpio.service"
CONFLICTING_GPIO_SERVICE_SYSTEMCTL_TIMEOUT_S = 30.0

ServiceEvent = Union[EnableVoutRequested, VoutSwitchRequested, MqttConnected]


class SystemctlAction(Enum):
    """The systemctl verbs `capture_vout_line` applies to the conflicting GPIO service."""

    STOP = "stop"
    START = "start"

    @property
    def argv(self) -> list[str]:
        """`start` is issued from inside this service's own startup, and the unit is ordered
        `Before=wb-mqtt-gpio.service` -- so a blocking start job cannot run until this service is
        ready, and would only sit there until the timeout. `--no-block` queues it instead: systemd
        runs it the moment we signal readiness. Stopping stays synchronous, because the retry needs
        the line actually released, not a queued intention to release it.
        """
        if self is SystemctlAction.START:
            return ["systemctl", "--no-block", self.value, CONFLICTING_GPIO_SERVICE]
        return ["systemctl", self.value, CONFLICTING_GPIO_SERVICE]


def capture_vout_line(gpio: VoutGpio) -> None:
    """Capture the Vout line, recovering once from a lost startup race with wb-mqtt-gpio.

    If the line is busy (`EBUSY`), stop `wb-mqtt-gpio` -- after a successful stop the line is
    guaranteed to be free -- and retry the capture once; a second failure (of any kind)
    propagates and is fatal. wb-mqtt-gpio is started back in every case, even when the retry
    fails, so its other channels keep working. systemctl failures are only logged: a failed
    stop means the retry reports the definitive capture error anyway, and a failed start is
    not this service's problem to fix.
    """
    try:
        gpio.capture()
        return
    except GpioBusyError as exc:
        logging.warning("%s; stopping %s and retrying", exc, CONFLICTING_GPIO_SERVICE)

    _systemctl_conflicting_gpio_service(SystemctlAction.STOP)
    try:
        gpio.capture()
    finally:
        _systemctl_conflicting_gpio_service(SystemctlAction.START)


def _systemctl_conflicting_gpio_service(action: SystemctlAction) -> None:
    try:
        subprocess.run(action.argv, timeout=CONFLICTING_GPIO_SERVICE_SYSTEMCTL_TIMEOUT_S, check=True)
    except (OSError, subprocess.SubprocessError) as exc:
        logging.warning("could not %s %s: %s", action.value, CONFLICTING_GPIO_SERVICE, exc)


def notify_systemd_ready() -> None:
    """Send systemd's `READY=1` notification (the unit is `Type=notify`), so units ordered
    after this one only start once the Vout line is captured. A no-op without `$NOTIFY_SOCKET`
    (running outside systemd); a socket error is only logged -- readiness signalling must
    never take the watchdog down."""
    socket_path = os.environ.get("NOTIFY_SOCKET")
    if not socket_path:
        return
    if socket_path.startswith("@"):  # abstract-namespace socket, per sd_notify(3)
        socket_path = "\0" + socket_path[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as notify_socket:
            notify_socket.connect(socket_path)
            notify_socket.sendall(b"READY=1")
    except OSError as exc:
        logging.warning("could not notify systemd of readiness: %s", exc)


class LoopDeadlines(NamedTuple):
    """The next monotonic deadlines the main loop carries from one iteration to the next"""

    adc_poll: float
    heartbeat: float


@dataclass
class ServiceHardware:
    adc: VinReader
    gpio: VoutGpio


@dataclass(frozen=True)
class PresenceFlagFile:
    """A boolean persisted as a file's mere existence: present == True, absent == False.

    Used for the state this service carries across a restart -- the `undervoltage` flag and the
    last Vout state. Best-effort: persistence is a second sink behind MQTT, so a failed
    create/delete is logged, never raised, and must never block the publish that follows. The
    content is never read or written -- create and delete are atomic, so there is no format or
    corruption to handle.
    """

    path: str

    def exists(self) -> bool:
        return os.path.isfile(self.path)

    def set(self, value: bool) -> None:
        try:
            if value:
                with open(self.path, "a", encoding="ascii"):
                    pass  # equivalent to `Path.touch(exist_ok=True)`: create it, or leave it be
            else:
                try:
                    os.remove(self.path)
                except FileNotFoundError:
                    pass  # already absent: same no-op semantics as `Path.unlink(missing_ok=True)`
        except OSError as exc:
            logging.warning("failed to update state file %s: %s", self.path, exc)


@dataclass
class PublishedState:
    undervoltage_file: PresenceFlagFile
    vout_file: PresenceFlagFile
    undervoltage: bool = False
    vout_enabled: bool = False
    last_vin: VinSample = field(default_factory=lambda: VinReadError(alarm=False))
    pending_vout: Optional[bool] = None
    vout_drive_failing: bool = False


class Service:
    def __init__(self, config: Config, state_directory: str = STATE_DIRECTORY):
        self._config = config
        self._event_queue: "queue.Queue[ServiceEvent]" = queue.Queue()

        self._mqtt = MqttDevice(self._event_queue)
        self._logic: Optional[PowerLogic] = None
        self._hardware: Optional[ServiceHardware] = None
        self._state = PublishedState(
            undervoltage_file=PresenceFlagFile(os.path.join(state_directory, UNDERVOLTAGE_MARKER_FILENAME)),
            vout_file=PresenceFlagFile(os.path.join(state_directory, VOUT_STATE_FILENAME)),
        )

        self._running = False

    def run(self) -> None:
        # Set before the startup sequence (not after) so a `stop()` call from another thread
        # racing with startup is never clobbered by resetting this back to True afterwards.
        self._running = True
        logging.debug("config: %r", self._config)

        vin_channel = devicetree.find_vin_channel()
        vout_line = devicetree.find_vout_gpio_line()
        logging.debug("auto-detected Vin channel %r, Vout line %r", vin_channel, vout_line)

        adc = VinReader(vin_channel, self._config.adc_error_threshold)
        adc.check_available()

        gpio = VoutGpio(vout_line)
        capture_vout_line(gpio)
        notify_systemd_ready()

        initial_undervoltage = self._state.undervoltage_file.exists()
        restore_vout = self._state.vout_file.exists()
        logging.debug("persisted state: undervoltage=%s, restore Vout=%s", initial_undervoltage, restore_vout)
        self._logic = PowerLogic(self._config.thresholds, initial_undervoltage=initial_undervoltage)
        self._hardware = ServiceHardware(adc=adc, gpio=gpio)

        self._mqtt.start()
        self._startup_tick(restore_vout)

        deadlines = LoopDeadlines(adc_poll=time.monotonic(), heartbeat=time.monotonic())

        try:
            while self._running:
                deadlines = self._loop_once(deadlines)
        finally:
            gpio.close()
            self._mqtt.clear_retained()
            self._mqtt.stop()

    def stop(self) -> None:
        self._running = False

    # --- Private ---

    def _startup_tick(self, restore_vout: bool) -> None:
        now = time.monotonic()
        self._state.last_vin = self._hardware.adc.poll()
        output = self._logic.tick(now, self._state.last_vin, VoutStartupRestore(restore_vout))
        self._apply_logic_output(output)
        self._publish_full_state()

    def _loop_once(self, deadlines: LoopDeadlines) -> LoopDeadlines:
        timeout = max(0.0, min(deadlines.adc_poll, deadlines.heartbeat) - time.monotonic())
        try:
            event = self._event_queue.get(timeout=min(timeout, MAIN_LOOP_TICK_S))
        except queue.Empty:
            event = None

        if event is not None:
            self._handle_event(event)

        self._flush_pending_vout()

        now = time.monotonic()
        next_adc_poll = deadlines.adc_poll
        next_heartbeat = deadlines.heartbeat
        if now >= next_adc_poll:
            self._poll_vin(now)
            next_adc_poll = now + self._config.adc_poll_period_s

        if now >= next_heartbeat:
            self._publish_heartbeat()
            next_heartbeat = now + self._config.heartbeat_period_s

        return LoopDeadlines(adc_poll=next_adc_poll, heartbeat=next_heartbeat)

    def _handle_event(self, event: ServiceEvent) -> None:
        logging.debug("event: %r", event)
        if isinstance(event, MqttConnected):
            self._publish_full_state()
            return

        now = time.monotonic()
        output = self._logic.tick(now, self._state.last_vin, event)
        self._apply_logic_output(output)

    def _poll_vin(self, now: float) -> None:
        self._state.last_vin = self._hardware.adc.poll()
        self._apply_logic_output(self._logic.tick(now, self._state.last_vin))
        self._publish_vin_sample()

    def _apply_logic_output(self, output: LogicOutput) -> None:
        if output.undervoltage != self._state.undervoltage:
            self._state.undervoltage = output.undervoltage
            self._state.undervoltage_file.set(output.undervoltage)
            if output.undervoltage:
                logging.info("undervoltage raised: cutting Vout, re-enable blocked")
            else:
                logging.info("undervoltage cleared: Vout re-enable unlocked")
            self._mqtt.publish_undervoltage(self._state.undervoltage)

        if output.set_vout is not None:
            self._state.pending_vout = output.set_vout
            self._flush_pending_vout()

    def _flush_pending_vout(self) -> None:
        if self._state.pending_vout is None:
            return
        try:
            self._hardware.gpio.set_enabled(self._state.pending_vout)
        except OSError as exc:
            if not self._state.vout_drive_failing:
                self._state.vout_drive_failing = True
                logging.error("failed to drive Vout to %s, retrying: %s", self._state.pending_vout, exc)
            return
        if self._state.vout_drive_failing:
            self._state.vout_drive_failing = False
            logging.info("Vout drive recovered")
        self._state.vout_enabled = self._state.pending_vout
        self._state.vout_file.set(self._state.pending_vout)
        logging.info("Vout %s", "on" if self._state.vout_enabled else "off")
        self._mqtt.publish_vout(self._state.vout_enabled)
        self._state.pending_vout = None

    def _publish_vin_sample(self) -> None:
        if isinstance(self._state.last_vin, VinReadError):
            self._mqtt.publish_vin_error()
        else:
            self._mqtt.publish_vin(self._state.last_vin)

    def _publish_heartbeat(self) -> None:
        self._mqtt.publish_heartbeat(int(time.time()))

    def _publish_full_state(self) -> None:
        self._mqtt.publish_undervoltage(self._state.undervoltage)
        self._mqtt.publish_vout(self._state.vout_enabled)
        self._publish_vin_sample()
        self._publish_heartbeat()
