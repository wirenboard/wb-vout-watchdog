"""Unit tests for `MqttDevice`.

Exercises the class against `FakeMqttClient` (see conftest.py) -- no real broker/socket
involved. `on_connect`/`on_message` are public: paho-mqtt itself is an external caller of
these methods (they're assigned to its callback slots), which also makes them the natural
seam for testing the connect/command-message behaviour without a real broker. The event
queue is a constructor dependency owned by the caller, so tests keep their own reference to
the exact `queue.Queue` they passed in, rather than reading it back off the device.
"""

import json
import logging
import queue

import pytest

import wb_vout_watchdog.mqtt as mqtt_module
from tests.conftest import (
    FakeMessage,
    FakeMessageInfo,
    FakeMqttClient,
    FakeReasonCode,
    latest_meta,
)
from wb_vout_watchdog.mqtt import (
    DEVICE_TOPIC_PREFIX,
    Control,
    ControlMeta,
    ControlType,
    MqttConnected,
    MqttDevice,
    command_topic,
)
from wb_vout_watchdog.power_logic import EnableVoutRequested, VoutSwitchRequested


@pytest.fixture(name="event_queue")
def _event_queue_fixture():
    return queue.Queue()


@pytest.fixture(name="device")
def _device_fixture(monkeypatch, event_queue):
    monkeypatch.setattr(mqtt_module.mqtt, "Client", FakeMqttClient)
    return MqttDevice(event_queue)


@pytest.fixture(name="fake_client")
def _fake_client_fixture(device):
    del device  # only needed to order this fixture after "device" has created the fake client
    return FakeMqttClient.instances[-1]


class TestStartStop:
    def test_start_connects_asynchronously_and_starts_the_network_loop(self, device, fake_client):
        device.start()

        assert fake_client.connect_args[0] == mqtt_module.MOSQUITTO_SOCKET_PATH
        assert fake_client.loop_started is True

    def test_stop_stops_the_loop(self, device, fake_client):
        device.start()
        device.stop()

        assert fake_client.loop_started is False

    def test_creating_the_device_wires_paho_logging_into_stdlib_logging(self, device, fake_client):
        """paho-mqtt stays silent unless `enable_logger()` is called on the client; `MqttDevice`
        must call it at construction so `--debug` (root logger level) reveals paho's logs."""
        del device
        assert fake_client.logger_enabled is True


class TestControlPublishing:
    def test_publish_undervoltage(self, device, fake_client):
        device.publish_undervoltage(True)

        # published[0], not [-1]: the V_OUT meta republish (see TestDynamicVoutReadonly)
        # follows the flag value on the wire
        topic, payload, retain, _qos = fake_client.published[0]
        assert topic == f"{DEVICE_TOPIC_PREFIX}/controls/undervoltage"
        assert payload == "1"
        assert retain is True

    def test_publish_vout(self, device, fake_client):
        device.publish_vout(False)

        topic, payload, retain, _qos = fake_client.published[-1]
        assert topic == f"{DEVICE_TOPIC_PREFIX}/controls/V_OUT"
        assert payload == "0"
        assert retain is True

    def test_publish_heartbeat(self, device, fake_client):
        device.publish_heartbeat(1234567890)

        topic, payload, _retain, _qos = fake_client.published[-1]
        assert topic == f"{DEVICE_TOPIC_PREFIX}/controls/heartbeat"
        assert payload == "1234567890"

    def test_publish_vin_also_clears_the_error_meta(self, device, fake_client):
        device.publish_vin(21.5)

        topics = [entry[0] for entry in fake_client.published]
        assert f"{DEVICE_TOPIC_PREFIX}/controls/vin" in topics
        error_topic, error_payload, _retain, _qos = fake_client.published[-1]
        assert error_topic == f"{DEVICE_TOPIC_PREFIX}/controls/vin/meta/error"
        assert error_payload == ""

    def test_publish_vin_error_sets_the_error_meta(self, device, fake_client):
        device.publish_vin_error()

        topic, payload, retain, _qos = fake_client.published[-1]
        assert topic == f"{DEVICE_TOPIC_PREFIX}/controls/vin/meta/error"
        assert payload == "r"
        assert retain is True


class TestDynamicVoutReadonly:
    def test_raising_undervoltage_republishes_the_vout_meta_with_readonly_true(self, device, fake_client):
        """`publish_undervoltage(True)` owns the "V_OUT is writable exactly while the flag is
        down" invariant: besides the flag value itself it must republish the `V_OUT` meta with
        `readonly=true` (retained), so homeui hides the toggle."""
        device.publish_undervoltage(True)

        topic, _payload, retain, _qos = fake_client.published[-1]
        assert topic == f"{DEVICE_TOPIC_PREFIX}/controls/V_OUT/meta"
        assert retain is True
        assert latest_meta(fake_client, Control.VOUT)["readonly"] is True

    def test_clearing_undervoltage_makes_the_control_writable_again(self, device, fake_client):
        device.publish_undervoltage(True)
        device.publish_undervoltage(False)

        assert latest_meta(fake_client, Control.VOUT)["readonly"] is False

    def test_reconnect_republishes_the_last_readonly_value(self, device, fake_client):
        """The device must remember the dynamic readonly across a reconnect: `_publish_metas`
        (triggered by `on_connect`) has to publish the `V_OUT` meta with the value last set by
        `publish_undervoltage`, not the static default."""
        device.publish_undervoltage(True)
        fake_client.published.clear()

        device.on_connect(fake_client, None, None, FakeReasonCode(is_failure=False))

        assert latest_meta(fake_client, Control.VOUT)["readonly"] is True


class TestOnConnect:
    def test_first_connect_publishes_metas_and_subscribes_to_both_command_topics(self, device, fake_client):
        device.on_connect(fake_client, None, None, FakeReasonCode(is_failure=False))

        assert any(topic == f"{DEVICE_TOPIC_PREFIX}/meta" for topic, *_ in fake_client.published)
        subscribed = {topic for topic, _qos in fake_client.subscriptions}
        assert command_topic(Control.VOUT) in subscribed
        assert command_topic(Control.ENABLE_VOUT) in subscribed

    def test_every_successful_connect_enqueues_a_connected_event(self, device, fake_client, event_queue):
        """`MqttConnected` must be enqueued on the first connect too, not only on reconnects:
        it is what orders the service's initial full-state publish after the metas this
        callback publishes (the full-state publish is idempotent, so the doubled publish at
        startup is harmless)."""
        device.on_connect(fake_client, None, None, FakeReasonCode(is_failure=False))
        device.on_connect(fake_client, None, None, FakeReasonCode(is_failure=False))

        assert isinstance(event_queue.get_nowait(), MqttConnected)
        assert isinstance(event_queue.get_nowait(), MqttConnected)
        assert event_queue.empty()

    def test_failed_connect_does_not_publish_subscribe_or_enqueue(self, device, fake_client, event_queue):
        device.on_connect(fake_client, None, None, FakeReasonCode(is_failure=True))

        assert fake_client.published == []
        assert fake_client.subscriptions == []
        assert event_queue.empty()


class TestTitleTranslations:
    def test_control_meta_with_a_russian_title_publishes_both_languages(self):
        meta = ControlMeta(
            ControlType.ALARM, readonly=True, order=1, title="Undervoltage", title_ru="Просадка питания"
        )

        assert json.loads(meta.to_json())["title"] == {"en": "Undervoltage", "ru": "Просадка питания"}

    def test_control_meta_without_a_russian_title_publishes_english_only(self):
        meta = ControlMeta(ControlType.VALUE, readonly=True, order=1, title="Vin")

        assert json.loads(meta.to_json())["title"] == {"en": "Vin"}

    def test_enable_vout_is_titled_as_an_unlock_action(self, device, fake_client):
        """`enable_vout` only unlocks `V_OUT` (it never turns Vout on), so its human-facing
        titles must say "Unlock", not "Enable" -- in both languages."""
        device.on_connect(fake_client, None, None, FakeReasonCode(is_failure=False))

        assert latest_meta(fake_client, Control.ENABLE_VOUT)["title"] == {
            "en": "Unlock Vout",
            "ru": "Разблокировать Vout",
        }

    def test_device_meta_carries_a_russian_title(self, device, fake_client):
        """Connecting publishes the device meta blob; its `title` must carry both the English
        name and the Russian translation, per the WB multi-language title convention."""
        device.on_connect(fake_client, None, None, FakeReasonCode(is_failure=False))

        device_meta_payload = next(
            payload for topic, payload, *_ in fake_client.published if topic == f"{DEVICE_TOPIC_PREFIX}/meta"
        )
        assert json.loads(device_meta_payload)["title"] == {"en": "Vout Watchdog", "ru": "Сторож Vout"}


def test_clear_retained_deletes_every_retained_topic_of_the_device(device, fake_client):
    """After a connect has published the device (metas + subscriptions), `clear_retained`
    must publish an empty retained payload to the device meta, every control's value and
    meta topics, and the `vin` error topic -- deleting them from the broker."""
    device.on_connect(fake_client, None, None, FakeReasonCode(is_failure=False))

    device.clear_retained()

    cleared = {topic for topic, payload, retain, _qos in fake_client.published if payload is None and retain}
    assert f"{DEVICE_TOPIC_PREFIX}/meta" in cleared
    for control in Control:
        assert f"{DEVICE_TOPIC_PREFIX}/controls/{control.value}" in cleared
        assert f"{DEVICE_TOPIC_PREFIX}/controls/{control.value}/meta" in cleared
    assert f"{DEVICE_TOPIC_PREFIX}/controls/vin/meta/error" in cleared


def test_clear_retained_when_broker_unreachable_is_logged_and_does_not_raise(
    monkeypatch, device, fake_client, caplog
):
    """If the broker is unreachable at stop time, `wait_for_publish` raises; `clear_retained`
    must log a warning and return without propagating, leaving the retained topics in place."""
    device.on_connect(fake_client, None, None, FakeReasonCode(is_failure=False))

    def raise_disconnected(_self, timeout=None):
        del timeout
        raise RuntimeError("the client is not currently connected")

    monkeypatch.setattr(FakeMessageInfo, "wait_for_publish", raise_disconnected)
    caplog.set_level(logging.WARNING)

    device.clear_retained()

    assert "could not clear retained topics" in caplog.text


class TestOnMessage:
    def test_enable_vout_payload_one_enqueues_enable_vout_requested(self, device, event_queue):
        device.on_message(None, None, FakeMessage(topic=command_topic(Control.ENABLE_VOUT), payload=b"1"))

        assert isinstance(event_queue.get_nowait(), EnableVoutRequested)

    def test_enable_vout_other_payloads_are_ignored(self, device, event_queue):
        device.on_message(None, None, FakeMessage(topic=command_topic(Control.ENABLE_VOUT), payload=b"0"))

        assert event_queue.empty()

    def test_vout_payload_one_enqueues_a_switch_on_request(self, device, event_queue):
        device.on_message(None, None, FakeMessage(topic=command_topic(Control.VOUT), payload=b"1"))

        assert event_queue.get_nowait() == VoutSwitchRequested(enabled=True)

    def test_vout_payload_zero_enqueues_a_switch_off_request(self, device, event_queue):
        device.on_message(None, None, FakeMessage(topic=command_topic(Control.VOUT), payload=b"0"))

        assert event_queue.get_nowait() == VoutSwitchRequested(enabled=False)

    def test_vout_other_payloads_are_ignored(self, device, event_queue):
        device.on_message(None, None, FakeMessage(topic=command_topic(Control.VOUT), payload=b"on"))

        assert event_queue.empty()

    def test_messages_on_unknown_topics_are_ignored(self, device, event_queue):
        device.on_message(
            None, None, FakeMessage(topic=f"{DEVICE_TOPIC_PREFIX}/controls/heartbeat/on", payload=b"1")
        )

        assert event_queue.empty()

    def test_retained_enable_vout_command_is_ignored(self, device, event_queue):
        """A retained `enable_vout/on` replayed by the broker on (re)subscribe is not a fresh
        confirmation from the external system and must not clear the undervoltage latch."""
        device.on_message(
            None, None, FakeMessage(topic=command_topic(Control.ENABLE_VOUT), payload=b"1", retain=True)
        )

        assert event_queue.empty()

    def test_retained_vout_command_is_ignored(self, device, event_queue):
        """Same for a retained `V_OUT/on`: only a live command may switch Vout."""
        device.on_message(
            None, None, FakeMessage(topic=command_topic(Control.VOUT), payload=b"1", retain=True)
        )

        assert event_queue.empty()
