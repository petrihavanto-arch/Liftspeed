"""Sample the VL53L1X distance sensor for 5 seconds and print a table of the samples."""

import time

from sensors.base import DistanceSensor
from sensors.vl53l1x_sensor import VL53L1XSensor

SAMPLE_DURATION_S = 5.0


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


def main() -> None:
    sensor = VL53L1XSensor()  # defaults: I2C bus 1, address 0x29
    samples = collect_samples(sensor, SAMPLE_DURATION_S)
    print(format_table(samples))


if __name__ == "__main__":
    main()
