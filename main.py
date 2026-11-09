"""Sample the VL53L1X distance sensor in a background process.

The sampler runs for the duration given with ``--duration`` (default 5 s) or
until Space is pressed in the terminal, whichever comes first. The collected
samples are printed as a table once the sampler has stopped.

Third-party package: VL53L1X (Pimoroni), imported lazily by
``sensors.vl53l1x_sensor``. It is only available where the sensor hardware
and library are installed (e.g. a Raspberry Pi).
"""

import argparse
import logging
import multiprocessing
import os
import sys
import time
from pathlib import Path
from queue import Empty

from sensors.base import DistanceSensor
from sensors.vl53l1x_sensor import VL53L1XSensor

DEFAULT_DURATION_S = 5.0
LOG_PATH = Path(__file__).resolve().with_suffix(".log")

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


class KeyboardWatcher:
    """Detect a spacebar press without requiring Enter.

    Uses ``msvcrt`` on Windows and ``termios`` on POSIX, but only when stdin
    is an interactive terminal; otherwise polling is a no-op.
    """

    def __init__(self) -> None:
        self._enabled = False

    @property
    def enabled(self) -> bool:
        return self._enabled

    def __enter__(self) -> "KeyboardWatcher":
        try:
            if sys.stdin is None or not sys.stdin.isatty():
                return self
        except (AttributeError, ValueError):
            return self

        if os.name == "nt":
            import msvcrt

            self._msvcrt = msvcrt
        else:
            try:
                import termios
                import tty

                self._fd = sys.stdin.fileno()
                self._saved = termios.tcgetattr(self._fd)
                tty.setcbreak(self._fd)
                self._termios = termios
            except OSError:
                return self

        self._enabled = True
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if self._enabled and os.name != "nt":
            try:
                self._termios.tcsetattr(
                    self._fd, self._termios.TCSADRAIN, self._saved
                )
            except OSError:
                pass
        self._enabled = False
        return False

    def poll(self, timeout: float) -> bool:
        """Wait up to `timeout` seconds and return True on a spacebar press."""
        if not self._enabled:
            time.sleep(timeout)
            return False

        if os.name == "nt":
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if self._msvcrt.kbhit() and self._msvcrt.getwch() == " ":
                    return True
                time.sleep(0.01)
            return False

        import select

        ready, _, _ = select.select([self._fd], [], [], timeout)
        if not ready:
            return False
        return os.read(self._fd, 1) == b" "


def build_arg_parser() -> argparse.ArgumentParser:
    """Build the command-line parser for this script."""
    parser = argparse.ArgumentParser(
        description=(
            "Sample the VL53L1X distance sensor in a background process and "
            "print the samples as a table."
        ),
    )
    parser.add_argument(
        "-d",
        "--debug",
        action="store_true",
        help=f"write DEBUG-level logs to {LOG_PATH.name} next to this script",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=DEFAULT_DURATION_S,
        metavar="SECONDS",
        help=f"sampling duration in seconds (default: {DEFAULT_DURATION_S})",
    )
    parser.add_argument(
        "--i2c-bus",
        type=int,
        default=1,
        metavar="BUS",
        help="I2C bus number (default: 1)",
    )
    parser.add_argument(
        "--i2c-address",
        type=lambda value: int(value, 0),
        default=0x29,
        metavar="ADDR",
        help="I2C address, decimal or 0x-prefixed (default: 0x29)",
    )
    return parser


def configure_logging(debug: bool) -> None:
    """Log DEBUG messages to LOG_PATH when --debug is given.

    Console output is deliberately left untouched, so -d only adds a log file.
    """
    if not debug:
        return
    handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.addHandler(handler)


def sample_worker(
    duration_s: float,
    i2c_bus: int,
    i2c_address: int,
    stop_event,
    sample_queue,
    debug: bool,
) -> None:
    """Background-process entry point: sample until the deadline or a stop request.

    Samples are streamed to `sample_queue` as ("sample", t, distance_m) tuples;
    a failure is reported as a single ("error", message) tuple.
    """
    configure_logging(debug)
    logger.debug("sampler process starting")

    sensor = None
    try:
        sensor = VL53L1XSensor(i2c_bus=i2c_bus, i2c_address=i2c_address)
        sensor.open()
        deadline = time.monotonic() + duration_s
        while not stop_event.is_set() and time.monotonic() < deadline:
            reading = sensor.read()
            sample_queue.put(("sample", reading.t, reading.distance_m))
            logger.debug(
                "sample t=%.4f s distance=%.3f m valid=%s",
                reading.t,
                reading.distance_m,
                reading.valid,
            )
    except Exception as exc:  # noqa: BLE001 - report any hardware/driver failure
        logger.exception("sampling failed")
        sample_queue.put(("error", str(exc)))
    finally:
        if sensor is not None:
            try:
                sensor.close()
            except Exception:  # noqa: BLE001 - never mask the original failure
                logger.exception("failed to close sensor")
    logger.debug("sampler process finished")


def drain_queue(
    sample_queue, samples: list[tuple[float, float]]
) -> str | None:
    """Move all queued samples into `samples`; return the first error seen."""
    error = None
    while True:
        try:
            item = sample_queue.get_nowait()
        except Empty:
            return error
        kind = item[0]
        if kind == "sample":
            _, t, distance_m = item
            samples.append((t, distance_m))
        elif kind == "error":
            error = item[1]


def format_table(samples: list[tuple[float, float]]) -> str:
    """Render `(t, distance_m)` pairs as a right-aligned text table."""
    header = ("time_s", "distance_m")
    rows = [(f"{t:.4f}", f"{d:.3f}") for t, d in samples]

    time_width = max([len(header[0])] + [len(t) for t, _ in rows])
    distance_width = max([len(header[1])] + [len(d) for _, d in rows])

    lines = [
        f"{header[0]:>{time_width}}  {header[1]:>{distance_width}}",
        f"{'-' * time_width}  {'-' * distance_width}",
    ]
    lines.extend(f"{t:>{time_width}}  {d:>{distance_width}}" for t, d in rows)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    if args.duration <= 0:
        parser.error("--duration must be greater than zero")

    configure_logging(args.debug)
    logger.debug("starting with arguments: %s", args)

    ctx = multiprocessing.get_context("spawn")
    sample_queue = ctx.Queue()
    stop_event = ctx.Event()
    process = ctx.Process(
        target=sample_worker,
        args=(
            args.duration,
            args.i2c_bus,
            args.i2c_address,
            stop_event,
            sample_queue,
            args.debug,
        ),
        name="vl53l1x-sampler",
    )

    samples: list[tuple[float, float]] = []
    error: str | None = None
    interrupted = False

    process.start()
    try:
        with KeyboardWatcher() as keyboard:
            if keyboard.enabled:
                print(
                    f"Sampling for up to {args.duration:g} s; "
                    "press Space to stop.",
                    file=sys.stderr,
                    flush=True,
                )
            while process.is_alive():
                if keyboard.poll(0.05):
                    logger.debug("spacebar pressed; requesting sampler stop")
                    break
                error = drain_queue(sample_queue, samples) or error
    except KeyboardInterrupt:
        interrupted = True
        logger.debug("interrupt received; requesting sampler stop")
    finally:
        stop_event.set()
        process.join()

    error = drain_queue(sample_queue, samples) or error
    logger.debug(
        "sampler finished (exit code %s) with %d samples",
        process.exitcode,
        len(samples),
    )

    print(format_table(samples))

    if error is not None:
        print(f"error: {error}", file=sys.stderr)
        return 1
    if process.exitcode not in (0, None):
        print(
            f"error: sampler process exited with code {process.exitcode}",
            file=sys.stderr,
        )
        return 1
    if interrupted:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
