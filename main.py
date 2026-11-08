"""Sample the VL53L1X distance sensor and print the samples as a table.

Third-party package: VL53L1X (Pimoroni), imported lazily by
``sensors.vl53l1x_sensor``. It is only available where the sensor hardware
and library are installed (e.g. a Raspberry Pi).
"""

import argparse
import logging
import sys
import time
from pathlib import Path

from sensors.base import DistanceSensor
from sensors.vl53l1x_sensor import VL53L1XSensor

DEFAULT_DURATION_S = 5.0
LOG_PATH = Path(__file__).resolve().with_suffix(".log")

logger = logging.getLogger(__name__)


def build_arg_parser() -> argparse.ArgumentParser:
    """Build the command-line parser for this script."""
    parser = argparse.ArgumentParser(
        description="Sample the VL53L1X distance sensor and print the samples as a table.",
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


def collect_samples(
    sensor: DistanceSensor, duration_s: float
) -> list[tuple[float, float]]:
    """Read as fast as the sensor allows for `duration_s`.

    Returns `(t, distance_m)` pairs, where `t` is the sensor's own
    monotonic timestamp (seconds since `open()`).
    """
    samples: list[tuple[float, float]] = []
    sensor.open()
    try:
        deadline = time.monotonic() + duration_s
        while time.monotonic() < deadline:
            reading = sensor.read()
            samples.append((reading.t, reading.distance_m))
            logger.debug(
                "sample t=%.4f s distance=%.3f m valid=%s",
                reading.t,
                reading.distance_m,
                reading.valid,
            )
    finally:
        sensor.close()
    return samples


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

    try:
        sensor = VL53L1XSensor(i2c_bus=args.i2c_bus, i2c_address=args.i2c_address)
        samples = collect_samples(sensor, args.duration)
    except Exception as exc:  # noqa: BLE001 - report any hardware/driver failure
        logger.exception("sampling failed")
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(format_table(samples))
    logger.debug("finished with %d samples", len(samples))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
