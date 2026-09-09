#!/usr/bin/env python3
"""Derive immutable disaster warning-fidelity v2 artifacts from one raw run."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.disaster_metric_v2_core import (  # noqa: E402
    DerivedCollisionError,
    DerivedPublicationError,
    InputValidationError,
    analyze_run,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--derived-root", required=True, type=Path)
    parser.add_argument("--metric-spec-sha256", required=True)
    parser.add_argument(
        "--require-declared-metric",
        action="store_true",
        help=(
            "fail unless run_meta.metric_version prospectively declares "
            "disaster-metric-v2.0.0"
        ),
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        output = analyze_run(
            run_dir=args.run_dir,
            derived_root=args.derived_root,
            expected_metric_spec_sha256=args.metric_spec_sha256,
            require_declared_metric=args.require_declared_metric,
        )
    except DerivedCollisionError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 3
    except InputValidationError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    except DerivedPublicationError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        raise
    except BaseException as error:
        print(
            f"ERROR: disaster metric v2 failed: {type(error).__name__}",
            file=sys.stderr,
        )
        return 1
    print(f"Disaster metric v2 derived output: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
