"""The sensor-agnostic contract: `Reading` and `DistanceSensor`.

Every layer above `sensors/` (calibration, rep analysis, the web app)
depends only on what's defined here, never on a concrete sensor class.
Swapping sensors means adding a new `DistanceSensor` implementation and
a branch in `factory.create_sensor()` -- nothing above this module
needs to change.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class Reading:
    """One distance sample.

    `t` is seconds on a monotonic clock, relative to that sensor
    instance's own `open()` call -- not wall-clock time, and not
    comparable across different sensor instances or a restart.
    `valid=False` means no usable distance (e.g. an out-of-range or
    no-target read); callers must check this before trusting
    `distance_m`.
    """
    t: float
    distance_m: float
    valid: bool = True


class DistanceSensor(ABC):
    """Interface every sensor implementation (real or dummy) must satisfy.
    Layers above this (calibration, kinematics, rep analysis, UI) depend
    only on this interface, never on a concrete sensor class."""

    @abstractmethod
    def open(self) -> None:
        """Start the sensor (open the bus/device, begin ranging)."""

    @abstractmethod
    def read(self) -> Reading:
        """Block until the next distance sample is ready and return it."""

    @abstractmethod
    def close(self) -> None:
        """Stop the sensor and release the bus/device."""

    @property
    @abstractmethod
    def nominal_rate_hz(self) -> float:
        """Approximate sample rate this sensor is configured for."""
