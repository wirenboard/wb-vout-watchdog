"""Unit tests for the `main` entry point: the startup-error exit-code contract and the
stop-signal wiring.

Every collaborator (`load_config`, `Service`) is replaced with a fake, so no config file,
device tree, GPIO chip or broker is ever touched -- these tests only pin main()'s own glue:
a bad config exits with the no-restart code, other startup errors exit restartable, and
SIGTERM/SIGINT route to `Service.stop()`.
"""

import logging
import signal

import pytest

from wb_vout_watchdog import main as main_module
from wb_vout_watchdog.adc import AdcError
from wb_vout_watchdog.config import Config, ConfigError
from wb_vout_watchdog.devicetree import DeviceTreeError
from wb_vout_watchdog.gpio import GpioError

ARGV = ["wb-vout-watchdog"]

RESTARTABLE_STARTUP_ERRORS = [DeviceTreeError, GpioError, AdcError]


@pytest.fixture(autouse=True)
def _restore_signal_handlers():
    """main() installs process-global SIGTERM/SIGINT handlers; save and restore them so a test
    can't leak its handler into the rest of the suite."""
    saved = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
    yield
    for sig, handler in saved.items():
        signal.signal(sig, handler)


def _install_fake_service(monkeypatch, run_error=None, login_rejected=False):
    """Point main() at a fake `load_config`/`Service`; return a dict that captures the created
    service instance so a test can reach it after main() returns (e.g. to fire a signal)."""
    created = {}

    class FakeService:
        def __init__(self, config):
            del config
            self.stopped = False
            self.login_rejected = login_rejected
            created["service"] = self

        def run(self):
            if run_error is not None:
                raise run_error

        def stop(self):
            self.stopped = True

    monkeypatch.setattr(main_module, "load_config", lambda path: Config())
    monkeypatch.setattr(main_module, "Service", FakeService)
    return created


def test_invalid_config_returns_the_no_restart_code(monkeypatch, caplog):
    """A bad config is logged and exits with EXIT_NOTCONFIGURED, so the unit's
    RestartPreventExitStatus stops systemd from restarting into the same broken config."""

    def raise_config_error(_path):
        raise ConfigError("bad config")

    monkeypatch.setattr(main_module, "load_config", raise_config_error)
    caplog.set_level(logging.ERROR)

    assert main_module.main(ARGV) == main_module.EXIT_NOTCONFIGURED
    assert "bad config" in caplog.text


@pytest.mark.parametrize("error_type", RESTARTABLE_STARTUP_ERRORS)
def test_transient_startup_error_returns_restartable_failure(monkeypatch, caplog, error_type):
    """A non-config startup error from `Service.run()` (missing DT node, busy line, unreadable
    ADC) is logged and exits with EXIT_FAILURE, so systemd restarts us -- these can clear on a
    retry, unlike a bad config."""
    _install_fake_service(monkeypatch, run_error=error_type("startup failed"))
    caplog.set_level(logging.ERROR)

    assert main_module.main(ARGV) == main_module.EXIT_FAILURE
    assert "startup failed" in caplog.text


def test_clean_run_returns_success(monkeypatch):
    _install_fake_service(monkeypatch)

    assert main_module.main(ARGV) == main_module.EXIT_SUCCESS


def test_stop_signal_handler_asks_the_service_to_stop(monkeypatch):
    """main() must wire both SIGTERM and SIGINT to `Service.stop()` so systemd's graceful stop
    reaches the run loop; invoking each installed handler must call `stop()` on the service."""
    created = _install_fake_service(monkeypatch)

    assert main_module.main(ARGV) == main_module.EXIT_SUCCESS

    for sig in (signal.SIGTERM, signal.SIGINT):
        created["service"].stopped = False
        handler = signal.getsignal(sig)
        assert callable(handler)
        handler(sig, None)
        assert created["service"].stopped is True


def test_rejected_mqtt_login_returns_the_invalid_argument_code(monkeypatch):
    """A login the broker rejects is a configuration problem a restart cannot fix: `Service`
    stops itself with `login_rejected` set and main() must exit with EXIT_INVALIDARGUMENT, which
    the unit's RestartPreventExitStatus matches."""
    _install_fake_service(monkeypatch, login_rejected=True)

    assert main_module.main(ARGV) == main_module.EXIT_INVALIDARGUMENT
