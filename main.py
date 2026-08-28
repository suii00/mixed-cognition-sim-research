import argparse
from contextlib import contextmanager
import signal
import sys
import threading
from pathlib import Path
from typing import Iterator, Optional, Sequence

import yaml

from engine.config import load_config, load_runtime_bindings, required_endpoint_ids
from engine.provenance import InvalidRunIdError, RunCollisionError
from engine.sim import (
    Simulation,
    SimulationAbortedError,
    SimulationSignalInterrupt,
)


def _nonzero_system_exit_code(error: SystemExit) -> int:
    code = error.code
    return code if isinstance(code, int) and code != 0 else 1


@contextmanager
def _translate_sigterm() -> Iterator[None]:
    """Turn SIGTERM into a catchable interruption on the main thread."""
    if (
        not hasattr(signal, "SIGTERM")
        or threading.current_thread() is not threading.main_thread()
    ):
        yield
        return
    previous = signal.getsignal(signal.SIGTERM)

    def handle_sigterm(signum, _frame):
        raise SimulationSignalInterrupt(signal.Signals(signum).name)

    signal.signal(signal.SIGTERM, handle_sigterm)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def _run_config(
    config,
    *,
    output_root: Optional[Path] = None,
    repo_root: Optional[Path] = None,
    runtime_bindings=None,
) -> int:
    simulation_options = {}
    if output_root is not None:
        simulation_options["output_root"] = output_root
    if repo_root is not None:
        simulation_options["repo_root"] = repo_root
    if runtime_bindings is not None:
        simulation_options["runtime_bindings"] = runtime_bindings
    try:
        sim = Simulation(config, **simulation_options)
    except (InvalidRunIdError, RunCollisionError) as error:
        print(f"[ERROR] Run cannot start: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("[ABORT] Interrupted while starting the run", file=sys.stderr)
        return 130
    except SystemExit as error:
        print("[ERROR] Internal SystemExit while starting the run", file=sys.stderr)
        return _nonzero_system_exit_code(error)

    try:
        sim.run()
    except SimulationAbortedError as error:
        print(f"[ABORT] {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("[ABORT] Interrupted by user or signal", file=sys.stderr)
        return 130
    except SystemExit as error:
        print("[ERROR] Internal SystemExit during the run", file=sys.stderr)
        return _nonzero_system_exit_code(error)

    terminal_meta = sim.run_lifecycle.meta
    if (
        terminal_meta.get("status") != "completed"
        or terminal_meta.get("aborted") is not False
    ):
        print("[ERROR] Run returned without completed metadata", file=sys.stderr)
        return 1
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Mixed-cognition agent simulation"
    )
    parser.add_argument("--config", required=True, help="Path to YAML config")
    parser.add_argument(
        "--runtime-bindings",
        required=True,
        help=(
            "Path to an operational endpoint mapping. Values are used in memory "
            "and are never copied into run artifacts."
        ),
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        help="Existing directory under which the unique run directory is created",
    )
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
        runtime_bindings = load_runtime_bindings(
            args.runtime_bindings,
            required_endpoint_ids(config),
        )
    except (OSError, TypeError, ValueError, yaml.YAMLError) as error:
        print(f"[ERROR] Invalid configuration: {error}", file=sys.stderr)
        return 2

    with _translate_sigterm():
        return _run_config(
            config,
            output_root=args.output_root,
            runtime_bindings=runtime_bindings,
            repo_root=(
                Path(__file__).resolve().parent
                if args.output_root is not None
                else None
            ),
        )


if __name__ == "__main__":
    raise SystemExit(main())
