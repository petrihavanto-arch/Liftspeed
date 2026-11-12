"""Background sampling process shared by the CLI and the web app.

``sample_worker`` is the entry point of the separate process that reads the
distance sensor and streams samples through a queue. ``main.py`` and
``webapp.py`` both start it and use ``drain_queue`` to move the queued samples
into their own collection.

Third-party package: the sensor dependency is imported lazily by the concrete
sensor module; on a PC use ``platform="pc"`` for the dummy sensor.
"""

import logging
import time
from pathlib import Path
from queue import Empty
from types import SimpleNamespace

from sensors.factory import create_sensor

SAMPLER_LOG_PATH = Path(__file__).resolve().with_suffix(".log")

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


def configure_logging(debug: bool, log_path: Path = SAMPLER_LOG_PATH) -> None:
    """Log DEBUG messages to `log_path` when `debug` is given.

    Console output is deliberately left untouched, so -d only adds a log file.
    """
    if not debug:
        return
    handler = logging.FileHandler(log_path, encoding="utf-8")
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
    platform: str = "rpi",
    log_path: Path = SAMPLER_LOG_PATH,
) -> None:
    """Background-process entry point: sample until the deadline or a stop request.

    Samples are streamed to `sample_queue` as ("sample", t, distance_m, valid)
    tuples; a failure is reported as a single ("error", message) tuple.
    """
    configure_logging(debug, log_path)
    logger.debug("sampler process starting")

    sensor = None
    try:
        sensor = create_sensor(
            SimpleNamespace(
                platform=platform,
                i2c_bus=i2c_bus,
                i2c_address=i2c_address,
            )
        )
        sensor.open()
        deadline = time.monotonic() + duration_s
        while not stop_event.is_set() and time.monotonic() < deadline:
            reading = sensor.read()
            sample_queue.put(
                ("sample", reading.t, reading.distance_m, reading.valid)
            )
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
            _, t, distance_m, valid = item
            if valid:
                samples.append((t, distance_m))
            else:
                logger.debug("skipping invalid sample at t=%.4f s", t)
        elif kind == "error":
            error = item[1]
