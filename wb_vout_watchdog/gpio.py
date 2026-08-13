import errno
import logging

import gpiod
from gpiod.line import Direction, Value

from wb_vout_watchdog.devicetree import VoutGpioLine

CONSUMER = "wb-vout-watchdog"


class GpioError(Exception):
    """Raised when the Vout GPIO line cannot be captured (busy, missing chip, permission, ...)."""


class GpioBusyError(GpioError):
    """Raised when the Vout GPIO line is already held by another process (`EBUSY`)"""


class VoutGpio:
    def __init__(self, line: VoutGpioLine, consumer: str = CONSUMER):
        self._line = line
        self._consumer = consumer
        self._request = None

    def capture(self) -> bool:
        """Take exclusive control of the line and return the physical state observed before that.

        To avoid a glitch, the line is first requested `AS_IS` (direction untouched) purely to
        read its current value, and only then reconfigured as an output with that same value as
        `output_value` -- so the physical pin level never changes across the switch to output.
        """
        try:
            self._request = gpiod.request_lines(
                self._line.chip_path,
                consumer=self._consumer,
                config={
                    self._line.offset: gpiod.LineSettings(
                        direction=Direction.AS_IS,
                        active_low=self._line.active_low,
                    )
                },
            )
        except OSError as exc:
            if exc.errno == errno.EBUSY:
                raise GpioBusyError(f"Vout GPIO line is busy: {exc}") from exc
            raise GpioError(f"cannot capture Vout GPIO line: {exc}") from exc

        value = self._request.get_value(self._line.offset)
        self._request.reconfigure_lines(
            {
                self._line.offset: gpiod.LineSettings(
                    direction=Direction.OUTPUT,
                    active_low=self._line.active_low,
                    output_value=value,
                )
            }
        )
        logging.debug(
            "captured Vout line %s offset %d, observed level=%s",
            self._line.chip_path,
            self._line.offset,
            bool(value),
        )
        return bool(value)

    def set_enabled(self, enabled: bool) -> None:
        """Drive the line on or off."""
        self._check_captured()
        self._request.set_value(self._line.offset, Value.ACTIVE if enabled else Value.INACTIVE)

    def close(self) -> None:
        """Release the line request. Vout is left as-is; this does not turn it off."""
        if self._request is not None:
            self._request.release()
            self._request = None

    # --- Private ---

    def _check_captured(self) -> None:
        if self._request is None:
            raise GpioError("Vout GPIO line has not been captured yet")
