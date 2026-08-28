#!/usr/bin/env python3
"""Command-line interface for deterministic Metric v2 analysis."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional, Sequence

from tools.metric_v2_core import (
    DerivedCollisionError,
    InputValidationError,
    analyze_run,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Metric v2 exact-expression analysis")
    subparsers = parser.add_subparsers(dest="command", required=True)
    analyze = subparsers.add_parser("analyze", help="analyze one completed run")
    analyze.add_argument("--run-dir", required=True, type=Path)
    analyze.add_argument("--registry", required=True, type=Path)
    analyze.add_argument("--registry-sha256", required=True)
    analyze.add_argument("--metric-spec-sha256", required=True)
    analyze.add_argument("--derived-root", required=True, type=Path)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        output = analyze_run(
            run_dir=args.run_dir,
            registry_path=args.registry,
            registry_sha256=args.registry_sha256,
            metric_spec_sha256=args.metric_spec_sha256,
            derived_root=args.derived_root,
        )
    except DerivedCollisionError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 3
    except InputValidationError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        raise
    except BaseException as error:
        print(
            f"ERROR: Metric v2 analysis failed: {type(error).__name__}",
            file=sys.stderr,
        )
        return 1
    print(f"Metric v2 derived output: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
