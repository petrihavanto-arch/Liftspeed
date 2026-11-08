import math
import time

from .base import DistanceSensor, Reading


class DummySensor(DistanceSensor):
    """PC development stand-in. Produces plausible, non-crashing values
    so downstream code (calibration, kinematics, rep detection, UI)
    never sees NaNs/None/out-of-range garbage. Not meant to mimic a
    real rep waveform — just enough motion for the pipeline to exercise
    its logic end-to-end without hardware."""

    def __init__(self, rate_hz: float = 25.0):
        self._rate_hz = rate_hz
        self._t0 = None

    def open(self) -> None:
        self._t0 = time.monotonic()

    @property
    def nominal_rate_hz(self) -> float:
        return self._rate_hz

    def read(self) -> Reading:
        time.sleep(1.0 / self._rate_hz)
        t = time.monotonic() - self._t0
        d = 0.85 + 0.15 * math.sin(t)
        return Reading(t=t, distance_m=d)

    def close(self) -> None:
        pass
