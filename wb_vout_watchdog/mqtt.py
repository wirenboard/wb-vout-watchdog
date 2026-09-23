import json
import logging
import queue
import time
from dataclasses import dataclass, replace
from enum import Enum
from typing import Optional

import paho.mqtt.client as mqtt

from wb_vout_watchdog.power_logic import EnableVoutRequested, VoutSwitchRequested

DEVICE_ID = "wb-vout-watchdog"
DEVICE_TOPIC_PREFIX = f"/devices/{DEVICE_ID}"
PUBLISH_QOS = 2

# The service always talks to the local mosquitto over its unix socket,
# the way every Wiren Board service does.
MOSQUITTO_SOCKET_PATH = "/var/run/mosquitto/mosquitto.sock"

# Long enough for a healthy local broker to confirm QoS-2 delivery, short enough not to
# stall a systemd stop when the broker is unreachable.
CLEAR_RETAINED_TIMEOUT_S = 2.0

# MQTT v5 reason codes paho reports for a rejected login (a v3.1.1 CONNACK 4/5 is translated to
# them): "Bad user name or password" and "Not authorized".
LOGIN_REJECTED_REASON_CODES = (134, 135)


@dataclass(frozen=True)
class MqttConnected:
    """Pushed onto the event queue on every successful (re)connect to the broker."""


@dataclass(frozen=True)
class MqttLoginRejected:
    """Pushed onto the event queue when the broker rejects the login: a configuration problem
    paho would otherwise retry forever."""


class Control(Enum):
    UNDERVOLTAGE = "undervoltage"
    VOUT = "V_OUT"
    ENABLE_VOUT = "enable_vout"
    HEARTBEAT = "heartbeat"
    VIN = "vin"


def command_topic(control: Control) -> str:
    return f"{DEVICE_TOPIC_PREFIX}/controls/{control.value}/on"


class ControlType(Enum):
    ALARM = "alarm"
    SWITCH = "switch"
    PUSHBUTTON = "pushbutton"
    VALUE = "value"


@dataclass(frozen=True)
class ControlMeta:
    control_type: ControlType
    readonly: bool
    order: int
    title: str
    # No `title_ru` means the English title is the name in every language (e.g. "Vin")
    title_ru: Optional[str] = None
    units: Optional[str] = None

    def to_json(self) -> str:
        meta = {
            "type": self.control_type.value,
            "readonly": self.readonly,
            "order": self.order,
            "title": {"en": self.title},
        }
        if self.title_ru is not None:
            meta["title"]["ru"] = self.title_ru
        if self.units is not None:
            meta["units"] = self.units
        return json.dumps(meta, ensure_ascii=False)


CONTROL_METAS = {
    Control.UNDERVOLTAGE: ControlMeta(
        ControlType.ALARM, readonly=True, order=1, title="Undervoltage", title_ru="Просадка питания"
    ),
    Control.VOUT: ControlMeta(ControlType.SWITCH, readonly=False, order=2, title="V_OUT"),
    Control.ENABLE_VOUT: ControlMeta(
        ControlType.PUSHBUTTON, readonly=False, order=3, title="Unlock Vout", title_ru="Разблокировать Vout"
    ),
    Control.HEARTBEAT: ControlMeta(
        ControlType.VALUE, readonly=True, order=4, title="Heartbeat", title_ru="Сигнал активности сервиса"
    ),
    Control.VIN: ControlMeta(ControlType.VALUE, readonly=True, order=5, title="Vin", units="V"),
}


class MqttDevice:
    def __init__(self, event_queue: "queue.Queue"):
        self._event_queue = event_queue
        self._vout_readonly = False

        self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=DEVICE_ID, transport="unix")
        # paho-mqtt never logs unless its logger is explicitly wired into stdlib logging
        self._client.enable_logger()
        self._client.on_connect = self.on_connect
        self._client.on_message = self.on_message

    def start(self) -> None:
        """Start connecting in the background; safe to call even if the broker is unreachable."""
        self._client.connect_async(MOSQUITTO_SOCKET_PATH)
        self._client.loop_start()

    def stop(self) -> None:
        self._client.loop_stop()
        self._client.disconnect()

    def clear_retained(self) -> None:
        """Delete every retained topic this device owns (device meta, control values and metas,
        the `vin` error flag) by publishing empty retained payloads, so a cleanly stopped
        service doesn't leave a dead device panel in homeui. Must be called while the network
        loop is still running (i.e. before `stop()`). Without a connection to the broker it is a
        no-op that logs an error: the retained topics stay (a stale panel remains in homeui) and
        the stop still exits with 0, as the service guideline requires."""
        if not self._client.is_connected():
            logging.error("MQTT broker is not connected, retained topics cannot be removed")
            return
        infos = [self._publish_cleared(f"{DEVICE_TOPIC_PREFIX}/meta")]
        for control in Control:
            infos.append(self._publish_cleared(f"{DEVICE_TOPIC_PREFIX}/controls/{control.value}"))
            infos.append(self._publish_cleared(f"{DEVICE_TOPIC_PREFIX}/controls/{control.value}/meta"))
        infos.append(self._publish_cleared(f"{DEVICE_TOPIC_PREFIX}/controls/{Control.VIN.value}/meta/error"))

        deadline = time.monotonic() + CLEAR_RETAINED_TIMEOUT_S
        for info in infos:
            try:
                info.wait_for_publish(timeout=max(0.0, deadline - time.monotonic()))
            except (ValueError, RuntimeError) as exc:
                logging.warning("could not clear retained topics: %s", exc)
                return

    def publish_undervoltage(self, value: bool) -> None:
        """Publish the flag and republish the `V_OUT` meta with `readonly` mirroring it (the
        control is writable exactly while the flag is down; this method is the single point
        that maintains the invariant). The readonly value is remembered for the meta republish
        a reconnect triggers."""
        self._vout_readonly = value
        self._publish_control(Control.UNDERVOLTAGE, "1" if value else "0")
        self._publish_control_meta(Control.VOUT)

    def publish_vout(self, value: bool) -> None:
        self._publish_control(Control.VOUT, "1" if value else "0")

    def publish_heartbeat(self, timestamp: int) -> None:
        self._publish_control(Control.HEARTBEAT, str(timestamp))

    def publish_vin(self, value: float) -> None:
        self._publish_control(Control.VIN, f"{value:.2f}")
        self._publish_vin_meta_error("")

    def publish_vin_error(self) -> None:
        self._publish_vin_meta_error("r")

    def on_connect(self, client, _userdata, _flags, reason_code, _properties=None) -> None:
        if reason_code.is_failure:
            logging.error("MQTT connect failed: %s", reason_code)
            if reason_code.value in LOGIN_REJECTED_REASON_CODES:
                self._event_queue.put(MqttLoginRejected())
            return

        self._publish_metas()
        for control in (Control.VOUT, Control.ENABLE_VOUT):
            client.subscribe(command_topic(control), qos=PUBLISH_QOS)

        logging.debug("MQTT connected: metas published, subscribed to command topics")
        self._event_queue.put(MqttConnected())

    def on_message(self, _client, _userdata, message) -> None:
        logging.debug("MQTT message on %s: %r (retain=%s)", message.topic, message.payload, message.retain)
        if message.retain:
            # the broker's replay of a retained command on (re)subscribe is not a fresh
            # command: acting on it could clear the undervoltage latch without anyone asking
            return
        if message.topic == command_topic(Control.ENABLE_VOUT):
            if message.payload == b"1":
                self._event_queue.put(EnableVoutRequested())
        elif message.topic == command_topic(Control.VOUT):
            if message.payload in (b"0", b"1"):
                self._event_queue.put(VoutSwitchRequested(enabled=message.payload == b"1"))

    # --- Private ---

    def _publish_metas(self) -> None:
        device_meta = json.dumps(
            {"driver": DEVICE_ID, "title": {"en": "Vout Watchdog", "ru": "Сторож Vout"}}, ensure_ascii=False
        )
        self._publish_retained(f"{DEVICE_TOPIC_PREFIX}/meta", device_meta)
        for control in Control:
            self._publish_control_meta(control)

    def _publish_control_meta(self, control: Control) -> None:
        meta = CONTROL_METAS[control]
        if control is Control.VOUT:
            meta = replace(meta, readonly=self._vout_readonly)
        self._publish_retained(f"{DEVICE_TOPIC_PREFIX}/controls/{control.value}/meta", meta.to_json())

    def _publish_cleared(self, topic: str) -> mqtt.MQTTMessageInfo:
        return self._client.publish(topic, None, retain=True, qos=PUBLISH_QOS)

    def _publish_control(self, control: Control, value: str) -> None:
        self._publish_retained(f"{DEVICE_TOPIC_PREFIX}/controls/{control.value}", value)

    def _publish_vin_meta_error(self, value: str) -> None:
        self._publish_retained(f"{DEVICE_TOPIC_PREFIX}/controls/{Control.VIN.value}/meta/error", value)

    def _publish_retained(self, topic: str, payload: str) -> None:
        """Publish only while connected. paho would otherwise queue every Vin sample and
        heartbeat published while the broker is away and replay the whole backlog on reconnect
        (mosquitto answers that with "Quota exceeded" and drops the connection again); the
        current state is republished on every connect anyway (`MqttConnected`)."""
        if not self._client.is_connected():
            return
        self._client.publish(topic, payload, retain=True, qos=PUBLISH_QOS)
