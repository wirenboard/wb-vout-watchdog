import argparse
import logging
import signal
import sys
from types import FrameType
from typing import Optional

from wb_vout_watchdog.adc import AdcError
from wb_vout_watchdog.config import ConfigError, load_config
from wb_vout_watchdog.devicetree import DeviceTreeError
from wb_vout_watchdog.gpio import GpioError
from wb_vout_watchdog.service import Service

EXIT_SUCCESS = 0
EXIT_FAILURE = 1
# Neither of these two is fixed by a restart, so the unit's `RestartPreventExitStatus=2 6` leaves
# the unit failed until the cause is fixed: 2 is bad CLI arguments (argparse's own exit) or a login
# the MQTT broker rejects, 6 is a broken config -- the WB "not configured" code, as in wb-mqtt-dali.
# Every other startup error is transient (`EXIT_FAILURE`), so systemd restarts us after
# `RestartSec`, indefinitely.
EXIT_INVALIDARGUMENT = 2
EXIT_NOTCONFIGURED = 6

CONFIG_FILEPATH = "/etc/wb-vout-watchdog.conf"

# Startup errors other than a bad config -- a missing DT node, the line held by another
# process, an unreadable ADC. Retrying can clear them (e.g. a lost startup race), so they exit
# with `EXIT_FAILURE` and let systemd restart us, unlike a config error.
RESTARTABLE_STARTUP_ERRORS = (DeviceTreeError, GpioError, AdcError)


def main(argv):
    parser = argparse.ArgumentParser(description="Wiren Board Vout undervoltage watchdog")
    parser.add_argument("-c", "--config", default=CONFIG_FILEPATH, help="Config file path")
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug-level logging",
    )
    args = parser.parse_args(argv[1:])

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(levelname)s: %(name)s: %(message)s" if args.debug else "%(levelname)s: %(message)s",
    )

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        logging.error("%s", exc)
        return EXIT_NOTCONFIGURED

    service = Service(config)
    _install_stop_signal_handlers(service)

    try:
        service.run()
    except RESTARTABLE_STARTUP_ERRORS as exc:
        logging.error("%s", exc)
        return EXIT_FAILURE

    if service.login_rejected:
        return EXIT_INVALIDARGUMENT
    return EXIT_SUCCESS


def _install_stop_signal_handlers(service: Service) -> None:
    def handle_stop_signal(_signum: int, _frame: Optional[FrameType]) -> None:
        service.stop()

    signal.signal(signal.SIGTERM, handle_stop_signal)
    signal.signal(signal.SIGINT, handle_stop_signal)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
