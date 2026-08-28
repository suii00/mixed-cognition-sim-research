#!/usr/bin/env python3
"""Create a blinded candidate-discovery projection from one strict-valid run."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.candidate_projection_core import (  # noqa: E402
    ProjectionCollisionError,
    ProjectionInputError,
    project_run,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create an immutable blinded candidate-discovery projection"
    )
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--projection-id", required=True)
    parser.add_argument("--projection-spec-sha256", required=True)
    parser.add_argument("--derived-root", required=True, type=Path)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        output = project_run(
            run_dir=args.run_dir,
            projection_id=args.projection_id,
            projection_spec_sha256=args.projection_spec_sha256,
            derived_root=args.derived_root,
        )
    except ProjectionCollisionError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 3
    except ProjectionInputError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        raise
    except BaseException as error:
        print(
            f"ERROR: candidate projection failed: {type(error).__name__}",
            file=sys.stderr,
        )
        return 1
    print(f"Candidate projection output: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
