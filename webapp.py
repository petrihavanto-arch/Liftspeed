"""Web UI for sampling the distance sensor and analysing reps.

Run ``webapp.py`` and open the printed address in a browser. The page has a
single toggle button that starts and stops the sampler. While sampling, the
page shows the elapsed time and sample count; after stopping it adds that
run's up speeds as a new column in the Sets table. The full rep table, raw
samples, and down-phase columns are opt-in with ``-r/--reps-too``,
``-s/--samples``, and ``--down-data-too``. The New Block button writes the
accumulated sets to a timestamped CSV file in ``log/`` and clears them.

The sampler itself is the same background process used by ``main.py``. The
server keeps one sampler at a time in memory; it is not a multi-client or
production deployment. By default it binds to all interfaces (``0.0.0.0``),
so the page can be opened from other machines on the network; pass
``--host 127.0.0.1`` to restrict it to the local machine.

Third-party package: Flask. The sensor dependency is the same as in
``main.py`` (VL53L1X on a Raspberry Pi); use ``--platform pc`` for the dummy
sensor when testing without hardware.
"""

import argparse
import csv
import dataclasses
import logging
import multiprocessing
import threading
import time
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, render_template

import main
import sampler

WEBAPP_LOG_PATH = Path(__file__).resolve().with_suffix(".log")
BLOCK_LOG_DIR = Path(__file__).resolve().parent / "log"
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 5000

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


class SamplerManager:
    """Own the single background sampler and its collected samples.

    All public methods take the lock, so the Flask request threads and the
    status polling cannot race each other. The sampler runs with an unlimited
    duration and is stopped explicitly through the web UI.
    """

    def __init__(
        self,
        *,
        platform: str,
        i2c_bus: int,
        i2c_address: int,
        debug: bool,
        log_path: Path = WEBAPP_LOG_PATH,
        block_log_dir: Path = BLOCK_LOG_DIR,
    ) -> None:
        self._platform = platform
        self._i2c_bus = i2c_bus
        self._i2c_address = i2c_address
        self._debug = debug
        self._log_path = log_path
        self._block_log_dir = block_log_dir

        self._lock = threading.Lock()
        self._process = None
        self._sample_queue = None
        self._stop_event = None
        self._samples: list[tuple[float, float]] = []
        self._error: str | None = None
        self._started_at: float | None = None
        self._elapsed_s = 0.0
        self._running = False
        self._result: dict | None = None
        self._sets: list[list[float]] = []

    def start(self) -> dict:
        """Start sampling; return the current status."""
        with self._lock:
            if self._running:
                raise RuntimeError("sampling is already running")

            context = multiprocessing.get_context("spawn")
            sample_queue = context.Queue()
            stop_event = context.Event()
            process = context.Process(
                target=sampler.sample_worker,
                args=(
                    float("inf"),
                    self._i2c_bus,
                    self._i2c_address,
                    stop_event,
                    sample_queue,
                    self._debug,
                    self._platform,
                    self._log_path,
                ),
                name="vl53l1x-sampler",
            )

            self._samples = []
            self._error = None
            self._result = None
            self._elapsed_s = 0.0
            self._sample_queue = sample_queue
            self._stop_event = stop_event
            self._process = process

            try:
                process.start()
            except Exception as exc:  # noqa: BLE001 - report start failures
                logger.exception("failed to start sampler")
                self._error = str(exc)
                self._process = None
                self._sample_queue = None
                self._stop_event = None
                return self._status_locked()

            self._started_at = time.monotonic()
            self._running = True
            logger.debug("sampler started")
            return self._status_locked()

    def status(self) -> dict:
        """Drain queued samples and report the sampler state."""
        with self._lock:
            if self._running:
                self._drain_locked()
                if self._process is not None and not self._process.is_alive():
                    # The worker stopped on its own, normally because the
                    # sensor failed. Finalise so the results are available.
                    logger.debug("sampler exited without a stop request")
                    self._finalize_locked()
            return self._status_locked()

    def stop(self) -> dict:
        """Stop sampling if needed and return samples, reps, and any error.

        Calling this when nothing is running is harmless and returns the last
        result again.
        """
        with self._lock:
            if self._running or self._process is not None:
                self._finalize_locked()
            if self._result is not None:
                return self._result
            return {
                "samples": [],
                "reps": [],
                "sets": self._sets_copy(),
                "error": self._error,
            }

    def new_block(self) -> dict:
        """Write the completed sets to a CSV file and clear them.

        The file is named after the local time in ISO 8601 form and placed in
        the block log directory. Starting a new block discards the in-memory
        sets, so the next Start/Stop run begins a fresh block.
        """
        with self._lock:
            if self._running:
                raise RuntimeError("stop sampling before starting a new block")
            if not self._sets:
                raise RuntimeError("there is no collected data to save")

            path = self._write_block_locked()
            self._sets = []
            self._samples = []
            self._result = None
            self._error = None
            self._elapsed_s = 0.0
            logger.debug("saved block to %s", path)
            return {
                "file": str(path),
                "sets": self._sets_copy(),
            }

    def _write_block_locked(self) -> Path:
        """Write the accumulated sets as long-format CSV and return the path."""
        self._block_log_dir.mkdir(parents=True, exist_ok=True)

        # Colons are not valid in Windows filenames, so use dashes in the
        # time part while keeping the ISO 8601 ordering.
        stamp = datetime.now().astimezone().strftime("%Y-%m-%dT%H-%M-%S")
        path = self._block_log_dir / f"{stamp}_Block_data.csv"
        suffix = 1
        while path.exists():
            path = self._block_log_dir / f"{stamp}-{suffix}_Block_data.csv"
            suffix += 1

        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["set", "rep", "up_speed_m_s"])
            for set_number, up_speeds in enumerate(self._sets, start=1):
                for rep_number, speed in enumerate(up_speeds, start=1):
                    writer.writerow([set_number, rep_number, f"{speed:.6f}"])
        return path

    def _sets_copy(self) -> list[list[float]]:
        """Return the completed sets, safe to serialise outside the lock."""
        return [list(up_speeds) for up_speeds in self._sets]

    def _status_locked(self) -> dict:
        if self._running and self._started_at is not None:
            self._elapsed_s = time.monotonic() - self._started_at
        return {
            "running": self._running,
            "sample_count": len(self._samples),
            "elapsed_s": round(self._elapsed_s, 2),
            "error": self._error,
            "has_result": self._result is not None,
            "sets": self._sets_copy(),
        }

    def _drain_locked(self) -> None:
        if self._sample_queue is None:
            return
        error = sampler.drain_queue(self._sample_queue, self._samples)
        if error is not None:
            self._error = error

    def _finalize_locked(self) -> None:
        """Stop the worker, collect the rest, and analyse the samples."""
        process = self._process
        if process is None:
            return

        self._drain_locked()
        self._stop_event.set()
        process.join(timeout=5.0)
        if process.is_alive():
            logger.warning("sampler did not stop in time; terminating it")
            process.terminate()
            process.join(timeout=1.0)
        self._drain_locked()

        if process.exitcode not in (0, None) and self._error is None:
            self._error = f"sampler process exited with code {process.exitcode}"

        if self._started_at is not None:
            self._elapsed_s = time.monotonic() - self._started_at

        reps = main.analyze_reps(
            self._samples,
            smooth_window=main.DEFAULT_SMOOTH_WINDOW,
            turn_threshold_m=main.DEFAULT_TURN_THRESHOLD_MM / 1000.0,
            min_rep_m=main.DEFAULT_MIN_REP_MM / 1000.0,
            min_rep_duration_s=main.DEFAULT_MIN_REP_DURATION_S,
        )

        # Each Start/Stop cycle is one set; only the up speeds are kept for
        # the accumulated sets view.
        self._sets.append([rep.up_speed_m_s for rep in reps])

        self._result = {
            "samples": [[t, distance] for t, distance in self._samples],
            "reps": [dataclasses.asdict(rep) for rep in reps],
            "sets": self._sets_copy(),
            "error": self._error,
        }
        self._running = False
        self._process = None
        self._sample_queue = None
        self._stop_event = None
        logger.debug(
            "sampler stopped with %d samples, %d reps, %d sets",
            len(self._samples),
            len(reps),
            len(self._sets),
        )


def build_arg_parser() -> argparse.ArgumentParser:
    """Build the command-line parser for the web server."""
    parser = argparse.ArgumentParser(
        description=(
            "Serve a web page that starts and stops distance sampling and "
            "shows each set's up speeds. Use -r/--reps-too to also show the "
            "full rep table, -s/--samples to also show the raw samples, and "
            "--down-data-too to include the down-phase columns."
        ),
    )
    parser.add_argument(
        "-d",
        "--debug",
        action="store_true",
        help=f"write DEBUG-level logs to {WEBAPP_LOG_PATH.name} next to this script",
    )
    parser.add_argument(
        "--host",
        default=DEFAULT_HOST,
        help=(
            "address to bind; 0.0.0.0 accepts connections from other "
            f"machines (default: {DEFAULT_HOST})"
        ),
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"port to bind (default: {DEFAULT_PORT})",
    )
    parser.add_argument(
        "-s",
        "--samples",
        action="store_true",
        help=(
            "show the raw samples table; samples are always collected for "
            "the rep analysis"
        ),
    )
    parser.add_argument(
        "-r",
        "--reps-too",
        action="store_true",
        help="show the full per-rep table in addition to the sets",
    )
    parser.add_argument(
        "--down-data-too",
        action="store_true",
        help="include the down-phase columns in the rep table (-r only)",
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
    return parser


def create_app(
    manager: SamplerManager,
    *,
    show_reps: bool = False,
    show_samples: bool = False,
    show_down_data: bool = False,
) -> Flask:
    """Create the Flask application backed by `manager`."""
    app = Flask(__name__)

    @app.get("/")
    def index():
        return render_template(
            "index.html",
            show_reps=show_reps,
            show_samples=show_samples,
            show_down_data=show_down_data,
        )

    @app.post("/api/start")
    def start():
        try:
            status = manager.start()
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 409
        return jsonify(status)

    @app.post("/api/stop")
    def stop():
        return jsonify(manager.stop())

    @app.get("/api/status")
    def status():
        return jsonify(manager.status())

    @app.post("/api/new-block")
    def new_block():
        try:
            saved = manager.new_block()
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 409
        return jsonify(saved)

    return app


def main_cli(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    sampler.configure_logging(args.debug, WEBAPP_LOG_PATH)
    logger.debug("starting web server with arguments: %s", args)

    manager = SamplerManager(
        platform=args.platform,
        i2c_bus=args.i2c_bus,
        i2c_address=args.i2c_address,
        debug=args.debug,
        log_path=WEBAPP_LOG_PATH,
    )
    app = create_app(
        manager,
        show_reps=args.reps_too,
        show_samples=args.samples,
        show_down_data=args.down_data_too,
    )

    print(f"Serving on http://{args.host}:{args.port}", flush=True)
    if args.host in ("0.0.0.0", "::"):
        print(
            "Reachable from other machines via this machine's network "
            "address.",
            flush=True,
        )
    # The reloader would start a second sampler manager in a child process.
    # -d only adds our log file; Flask's debugger stays off.
    app.run(host=args.host, port=args.port, use_reloader=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main_cli())
