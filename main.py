"""Sample the VL53L1X distance sensor in a background process.

The sampler runs for the duration given with ``--duration`` (default 5 s) or
until Space is pressed in the terminal, whichever comes first. The collected
samples are printed as a table once the sampler has stopped, followed by a
per-repetition analysis: every upward movement is a rep, the down movement
after it is included when present, and the average speed of each phase is
measured over its 10%-90% part.

With ``-f/--file`` the samples are read from an existing table instead of the
sensor, which is useful for analysing a saved ``log.txt`` on a PC.

Third-party package: VL53L1X (Pimoroni), imported lazily by
``sensors.vl53l1x_sensor``. It is only available where the sensor hardware
and library are installed (e.g. a Raspberry Pi).

"""

import argparse
import logging
import math
import multiprocessing
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import sampler

DEFAULT_DURATION_S = 5.0
DEFAULT_SMOOTH_WINDOW = 3
DEFAULT_TURN_THRESHOLD_MM = 3.0
DEFAULT_MIN_REP_MM = 25.0
DEFAULT_MIN_REP_DURATION_S = 0.2
LOG_PATH = Path(__file__).resolve().with_suffix(".log")

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


@dataclass
class Rep:
    """One repetition: an upward movement and, when present, the down movement.

    The up-phase runs from a bottom to a top. The down-phase, when the
    recording contains one, runs from that top to the next bottom; without a
    following down-phase it is ``None`` and the rep is still valid.
    ``*_t10_s``/``*_t90_s`` delimit the part of each phase used for its
    average speed (10%-90% going up, 90%-10% going down).
    """

    up_start_s: float
    up_end_s: float
    up_t10_s: float
    up_t90_s: float
    up_amplitude_m: float
    up_speed_m_s: float
    down_start_s: float | None = None
    down_end_s: float | None = None
    down_t90_s: float | None = None
    down_t10_s: float | None = None
    down_amplitude_m: float | None = None
    down_speed_m_s: float | None = None


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
            "Sample the VL53L1X distance sensor in a background process, "
            "print the samples as a table, and report the average up and down "
            "speed of each detected repetition (up movement, plus the down "
            "movement when present)."
        ),
    )
    parser.add_argument(
        "-d",
        "--debug",
        action="store_true",
        help=f"write DEBUG-level logs to {LOG_PATH.name} next to this script",
    )
    parser.add_argument(
        "-f",
        "--file",
        type=Path,
        metavar="PATH",
        help=(
            "analyse samples from this table (time_s distance_m per line) "
            "instead of sampling the sensor"
        ),
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=DEFAULT_DURATION_S,
        metavar="SECONDS",
        help=f"sampling duration in seconds (default: {DEFAULT_DURATION_S})",
    )
    parser.add_argument(
        "--platform",
        choices=("rpi", "pc"),
        default="rpi",
        help=(
            "sensor backend: 'rpi' for the VL53L1X, 'pc' for the dummy "
            "sensor (default: rpi)"
        ),
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
    parser.add_argument(
        "--smooth-window",
        type=int,
        default=DEFAULT_SMOOTH_WINDOW,
        metavar="N",
        help=(
            "moving-average window in samples used before detecting turns "
            f"(default: {DEFAULT_SMOOTH_WINDOW})"
        ),
    )
    parser.add_argument(
        "--turn-threshold-mm",
        type=float,
        default=DEFAULT_TURN_THRESHOLD_MM,
        metavar="MM",
        help=(
            "how far the distance must reverse before a turn is accepted, "
            f"in millimetres (default: {DEFAULT_TURN_THRESHOLD_MM:g})"
        ),
    )
    parser.add_argument(
        "--min-rep-mm",
        type=float,
        default=DEFAULT_MIN_REP_MM,
        metavar="MM",
        help=(
            "smallest up-phase counted as a rep, and smallest down-phase "
            f"reported, in millimetres (default: {DEFAULT_MIN_REP_MM:g})"
        ),
    )
    parser.add_argument(
        "--min-rep-duration",
        type=float,
        default=DEFAULT_MIN_REP_DURATION_S,
        metavar="SECONDS",
        help=(
            "shortest up-phase of a rep, in seconds "
            f"(default: {DEFAULT_MIN_REP_DURATION_S:g})"
        ),
    )
    return parser


def collect_samples(
    args: argparse.Namespace,
) -> tuple[list[tuple[float, float]], str | None, bool, int | None]:
    """Sample the sensor; return samples, error, interrupted, exit code."""
    ctx = multiprocessing.get_context("spawn")
    sample_queue = ctx.Queue()
    stop_event = ctx.Event()
    process = ctx.Process(
        target=sampler.sample_worker,
        args=(
            args.duration,
            args.i2c_bus,
            args.i2c_address,
            stop_event,
            sample_queue,
            args.debug,
            args.platform,
            LOG_PATH,
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
                error = sampler.drain_queue(sample_queue, samples) or error
    except KeyboardInterrupt:
        interrupted = True
        logger.debug("interrupt received; requesting sampler stop")
    finally:
        stop_event.set()
        process.join()

    error = sampler.drain_queue(sample_queue, samples) or error
    logger.debug(
        "sampler finished (exit code %s) with %d samples",
        process.exitcode,
        len(samples),
    )
    return samples, error, interrupted, process.exitcode


def decode_text(data: bytes) -> tuple[str, str]:
    """Decode `data`, trying UTF-8 first and falling back to cp1252."""
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1"), "latin-1"


def parse_samples(text: str) -> list[tuple[float, float]]:
    """Parse whitespace-separated ``time_s distance_m`` rows.

    Non-numeric lines (headers, separators, blank lines) are ignored.
    """
    samples: list[tuple[float, float]] = []
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 2:
            continue
        try:
            t = float(fields[0])
            distance_m = float(fields[1])
        except ValueError:
            continue
        if math.isfinite(t) and math.isfinite(distance_m):
            samples.append((t, distance_m))
    return samples


def read_samples_file(path: Path) -> list[tuple[float, float]]:
    """Read and parse a sample table; raise OSError or ValueError on failure."""
    text, encoding = decode_text(path.read_bytes())
    logger.debug("read %s using %s", path, encoding)
    samples = parse_samples(text)
    if len(samples) < 2:
        raise ValueError(f"{path} contains fewer than two numeric samples")
    return samples


def moving_average(values: list[float], window: int) -> list[float]:
    """Centered moving average; the window shrinks at the series edges."""
    if window <= 1 or len(values) < 3:
        return list(values)
    half = window // 2
    last = len(values) - 1
    smoothed: list[float] = []
    for index in range(len(values)):
        low = max(0, index - half)
        high = min(last, index + half)
        smoothed.append(sum(values[low : high + 1]) / (high - low + 1))
    return smoothed


def find_extrema(
    values: list[float], threshold: float
) -> list[tuple[int, str, bool]]:
    """Find alternating minima and maxima with a `threshold` hysteresis.

    Returns ``(index, kind, confirmed)`` tuples in time order, where kind is
    ``"min"`` or ``"max"``. A turn is recorded at the actual extreme sample,
    but only confirmed once the signal has moved `threshold` away from it.
    The first and last turns cannot be confirmed and are marked ``False``.
    """
    extrema: list[tuple[int, str, bool]] = []
    if not values:
        return extrema

    direction = 0
    min_index = 0
    max_index = 0
    for index, value in enumerate(values):
        if direction == 0:
            if value >= values[min_index] + threshold:
                extrema.append((min_index, "min", False))
                direction = 1
                max_index = index
            elif value <= values[max_index] - threshold:
                extrema.append((max_index, "max", False))
                direction = -1
                min_index = index
            else:
                if value < values[min_index]:
                    min_index = index
                if value > values[max_index]:
                    max_index = index
        elif direction == 1:
            if value > values[max_index]:
                max_index = index
            elif value <= values[max_index] - threshold:
                extrema.append((max_index, "max", True))
                direction = -1
                min_index = index
        else:
            if value < values[min_index]:
                min_index = index
            elif value >= values[min_index] + threshold:
                extrema.append((min_index, "min", True))
                direction = 1
                max_index = index

    if direction == 1:
        extrema.append((max_index, "max", False))
    elif direction == -1:
        extrema.append((min_index, "min", False))
    return extrema


def crossing_time(
    times: list[float],
    values: list[float],
    start_index: int,
    end_index: int,
    level: float,
) -> float:
    """Linearly interpolate when `values` first reaches `level`."""
    if values[start_index] >= level:
        return times[start_index]
    for index in range(start_index + 1, end_index + 1):
        previous = values[index - 1]
        current = values[index]
        if current >= level and previous < level:
            span = current - previous
            if span <= 0:
                return times[index]
            fraction = (level - previous) / span
            return times[index - 1] + fraction * (
                times[index] - times[index - 1]
            )
    return times[end_index]


def descending_crossing_time(
    times: list[float],
    values: list[float],
    start_index: int,
    end_index: int,
    level: float,
) -> float:
    """Linearly interpolate when `values` first drops to `level`."""
    if values[start_index] <= level:
        return times[start_index]
    for index in range(start_index + 1, end_index + 1):
        previous = values[index - 1]
        current = values[index]
        if current <= level and previous > level:
            span = previous - current
            if span <= 0:
                return times[index]
            fraction = (previous - level) / span
            return times[index - 1] + fraction * (
                times[index] - times[index - 1]
            )
    return times[end_index]


def analyze_reps(
    samples: list[tuple[float, float]],
    *,
    smooth_window: int,
    turn_threshold_m: float,
    min_rep_m: float,
    min_rep_duration_s: float,
) -> list[Rep]:
    """Detect reps and measure their up speed and, when present, down speed.

    Turns are found on a smoothed copy of the signal, so sensor noise does not
    create spurious reps. A rep is an upward phase (minimum to maximum); the
    downward phase (that maximum to the next minimum) is included when the
    recording contains one. An up-phase without a following down is still a
    valid rep. The up-phase must be at least `min_rep_m` tall and last at
    least `min_rep_duration_s`; the down-phase is reported only when it is at
    least `min_rep_m` tall.

    Each speed is the distance covered between the 10% and 90% levels of that
    phase's own amplitude divided by the time between those two crossings.
    The down speed is measured over the 90%-10% part of the down-phase.
    """
    if len(samples) < 2:
        return []

    times = [t for t, _ in samples]
    distances = [distance for _, distance in samples]
    smoothed = moving_average(distances, smooth_window)
    extrema = find_extrema(smoothed, turn_threshold_m)
    logger.debug("found %d turns: %s", len(extrema), extrema)

    reps: list[Rep] = []
    last_position = len(extrema) - 1
    for position, (up_start_index, up_start_kind, _) in enumerate(extrema):
        if up_start_kind != "min" or position + 1 > last_position:
            continue
        top_index, top_kind, _ = extrema[position + 1]
        if top_kind != "max":
            continue

        # The first turn of the recording is unconfirmed but still usable as
        # a start, since a rep can begin at rest. An unconfirmed top is the
        # last sample of the recording; the up-phase is kept anyway, because
        # an up without a following down is still a valid rep.
        up_amplitude = smoothed[top_index] - smoothed[up_start_index]
        up_duration = times[top_index] - times[up_start_index]
        if up_amplitude < min_rep_m or up_duration < min_rep_duration_s:
            logger.debug(
                "ignoring up-phase at %.4f-%.4f s: amplitude %.3f m, "
                "duration %.3f s",
                times[up_start_index],
                times[top_index],
                up_amplitude,
                up_duration,
            )
            continue

        up_lower = smoothed[up_start_index] + 0.10 * up_amplitude
        up_upper = smoothed[up_start_index] + 0.90 * up_amplitude
        up_t10 = crossing_time(
            times, smoothed, up_start_index, top_index, up_lower
        )
        up_t90 = crossing_time(
            times, smoothed, up_start_index, top_index, up_upper
        )
        if up_t90 <= up_t10:
            logger.debug(
                "ignoring up-phase at %.4f-%.4f s: no usable speed window",
                times[up_start_index],
                times[top_index],
            )
            continue

        down_start_s: float | None = None
        down_end_s: float | None = None
        down_t90_s: float | None = None
        down_t10_s: float | None = None
        down_amplitude_m: float | None = None
        down_speed_m_s: float | None = None

        if position + 2 <= last_position:
            down_end_index, down_end_kind, down_end_confirmed = extrema[
                position + 2
            ]
            down_is_usable = down_end_confirmed or position + 2 == last_position
            if down_end_kind == "min" and down_is_usable:
                down_amplitude = (
                    smoothed[top_index] - smoothed[down_end_index]
                )
                if down_amplitude >= min_rep_m:
                    down_lower = (
                        smoothed[down_end_index] + 0.10 * down_amplitude
                    )
                    down_upper = (
                        smoothed[down_end_index] + 0.90 * down_amplitude
                    )
                    down_t90 = descending_crossing_time(
                        times, smoothed, top_index, down_end_index, down_upper
                    )
                    down_t10 = descending_crossing_time(
                        times, smoothed, top_index, down_end_index, down_lower
                    )
                    if down_t10 > down_t90:
                        down_start_s = times[top_index]
                        down_end_s = times[down_end_index]
                        down_t90_s = down_t90
                        down_t10_s = down_t10
                        down_amplitude_m = down_amplitude
                        down_speed_m_s = (down_upper - down_lower) / (
                            down_t10 - down_t90
                        )
                    else:
                        logger.debug(
                            "no usable down speed for rep at %.4f-%.4f s",
                            times[up_start_index],
                            times[top_index],
                        )
                else:
                    logger.debug(
                        "no down data for rep at %.4f-%.4f s: "
                        "down amplitude %.3f m",
                        times[up_start_index],
                        times[top_index],
                        down_amplitude,
                    )

        reps.append(
            Rep(
                up_start_s=times[up_start_index],
                up_end_s=times[top_index],
                up_t10_s=up_t10,
                up_t90_s=up_t90,
                up_amplitude_m=up_amplitude,
                up_speed_m_s=(up_upper - up_lower) / (up_t90 - up_t10),
                down_start_s=down_start_s,
                down_end_s=down_end_s,
                down_t90_s=down_t90_s,
                down_t10_s=down_t10_s,
                down_amplitude_m=down_amplitude_m,
                down_speed_m_s=down_speed_m_s,
            )
        )
    logger.debug("detected %d reps", len(reps))
    return reps


def format_table(
    header: tuple[str, ...], rows: list[tuple[str, ...]]
) -> str:
    """Render string cells as a right-aligned text table."""
    widths = [
        max([len(header[column])] + [len(row[column]) for row in rows])
        for column in range(len(header))
    ]
    lines = [
        "  ".join(f"{cell:>{widths[column]}}" for column, cell in enumerate(header)),
        "  ".join("-" * width for width in widths),
    ]
    lines.extend(
        "  ".join(
            f"{cell:>{widths[column]}}" for column, cell in enumerate(row)
        )
        for row in rows
    )
    return "\n".join(lines)


def format_samples(samples: list[tuple[float, float]]) -> str:
    """Render `(t, distance_m)` pairs as a table."""
    header = ("time_s", "distance_m")
    rows = [(f"{t:.4f}", f"{distance:.3f}") for t, distance in samples]
    return format_table(header, rows)


def format_optional(value: float | None, spec: str) -> str:
    """Format a number, or return a dash when it is missing."""
    return "-" if value is None else format(value, spec)


def format_reps(reps: list[Rep]) -> str:
    """Render the detected reps as a table."""
    header = (
        "rep",
        "up_start_s",
        "up_end_s",
        "up_t10_s",
        "up_t90_s",
        "up_amplitude_m",
        "up_speed_m_s",
        "down_start_s",
        "down_end_s",
        "down_t90_s",
        "down_t10_s",
        "down_amplitude_m",
        "down_speed_m_s",
    )
    rows = [
        (
            str(number),
            f"{rep.up_start_s:.4f}",
            f"{rep.up_end_s:.4f}",
            f"{rep.up_t10_s:.4f}",
            f"{rep.up_t90_s:.4f}",
            f"{rep.up_amplitude_m:.3f}",
            f"{rep.up_speed_m_s:.3f}",
            format_optional(rep.down_start_s, ".4f"),
            format_optional(rep.down_end_s, ".4f"),
            format_optional(rep.down_t90_s, ".4f"),
            format_optional(rep.down_t10_s, ".4f"),
            format_optional(rep.down_amplitude_m, ".3f"),
            format_optional(rep.down_speed_m_s, ".3f"),
        )
        for number, rep in enumerate(reps, start=1)
    ]
    return format_table(header, rows)


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if args.file is None and args.duration <= 0:
        parser.error("--duration must be greater than zero")
    if args.smooth_window < 1:
        parser.error("--smooth-window must be at least 1")
    if args.turn_threshold_mm <= 0:
        parser.error("--turn-threshold-mm must be greater than zero")
    if args.min_rep_mm <= 0:
        parser.error("--min-rep-mm must be greater than zero")
    if args.min_rep_duration <= 0:
        parser.error("--min-rep-duration must be greater than zero")

    sampler.configure_logging(args.debug, LOG_PATH)
    logger.debug("starting with arguments: %s", args)

    if args.file is not None:
        try:
            samples = read_samples_file(args.file)
        except OSError as exc:
            print(f"error: cannot read {args.file}: {exc}", file=sys.stderr)
            return 1
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        error = None
        interrupted = False
        exitcode = None
    else:
        samples, error, interrupted, exitcode = collect_samples(args)

    print(format_samples(samples))

    reps = analyze_reps(
        samples,
        smooth_window=args.smooth_window,
        turn_threshold_m=args.turn_threshold_mm / 1000.0,
        min_rep_m=args.min_rep_mm / 1000.0,
        min_rep_duration_s=args.min_rep_duration,
    )
    print()
    if reps:
        print(format_reps(reps))
    else:
        print("no reps detected")

    if error is not None:
        print(f"error: {error}", file=sys.stderr)
        return 1
    if exitcode not in (0, None):
        print(
            f"error: sampler process exited with code {exitcode}",
            file=sys.stderr,
        )
        return 1
    if interrupted:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
