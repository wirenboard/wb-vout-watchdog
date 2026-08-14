import errno
import logging

from wb_vout_watchdog import gpio_cdev
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

        To avoid a glitch, the line is first requested as-is (direction untouched) purely to read
        its current value, and only then reconfigured as an output driving that same value -- the
        pin level never changes, and the line is never released in between.

        A failure of those two steps hands the line back at once and leaves the object uncaptured,
        so the caller's retry starts from a free line instead of racing our own hold.
        """
        try:
            request = gpio_cdev.request_line(
                self._line.chip_path,
                self._line.offset,
                consumer=self._consumer,
                active_low=self._line.active_low,
            )
        except OSError as exc:
            if exc.errno == errno.EBUSY:
                raise GpioBusyError(f"Vout GPIO line is busy: {exc}") from exc
            raise GpioError(f"cannot capture Vout GPIO line: {exc}") from exc

        try:
            value = request.get_value()
            request.reconfigure_as_output(value)
        except OSError as exc:
            request.release()
            raise GpioError(f"cannot drive the captured Vout GPIO line: {exc}") from exc

        self._request = request
        logging.debug(
            "captured Vout line %s offset %d, observed level=%s",
            self._line.chip_path,
            self._line.offset,
            value.name,
        )
        return value is gpio_cdev.Value.ACTIVE

    def set_enabled(self, enabled: bool) -> None:
        """Drive the line on or off."""
        self._check_captured()
        self._request.set_value(gpio_cdev.Value.ACTIVE if enabled else gpio_cdev.Value.INACTIVE)

    def close(self) -> None:
        """Release the line request. Vout is left as-is; this does not turn it off."""
        if self._request is not None:
            self._request.release()
            self._request = None

    # --- Private ---

    def _check_captured(self) -> None:
        if self._request is None:
            raise GpioError("Vout GPIO line has not been captured yet")
