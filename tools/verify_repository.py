"""Verify the public repository without rewriting any tracked artifact."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from engine.config import load_config  # noqa: E402
from tools.scan_publication import scan_tree  # noqa: E402
from tools.validate_run import validate_run  # noqa: E402


RUNTIME_BINDING_PREFIX = "runtime-bindings."


def _public_config_paths(root: Path) -> list[Path]:
    config_root = root / "configs"
    return [
        path
        for path in sorted(config_root.iterdir())
        if path.is_file()
        and path.suffix.lower() in {".json", ".yaml", ".yml"}
        and not path.name.startswith(RUNTIME_BINDING_PREFIX)
    ]


def verify_repository(root: Path) -> list[str]:
    root = root.resolve()
    errors: list[str] = []

    for config_path in _public_config_paths(root):
        try:
            load_config(config_path)
        except Exception as error:  # fail closed at the repository boundary
            errors.append(
                f"invalid public config {config_path.relative_to(root).as_posix()}: "
                f"{type(error).__name__}: {error}"
            )

    runs_root = root / "runs"
    if runs_root.is_dir():
        for run_dir in sorted(runs_root.glob("output_*")):
            if not run_dir.is_dir() or run_dir.is_symlink():
                errors.append(
                    f"run path must be a real directory: "
                    f"{run_dir.relative_to(root).as_posix()}"
                )
                continue
            report = validate_run(run_dir, strict=True)
            errors.extend(
                f"{run_dir.relative_to(root).as_posix()}: {message}"
                for message in report.errors
            )

    errors.extend(
        f"unsafe publication value {finding.pattern_id} at "
        f"{finding.path}:{finding.line}"
        for finding in scan_tree(root)
    )
    return errors


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "root",
        nargs="?",
        type=Path,
        default=REPO_ROOT,
    )
    parser.add_argument("--json", action="store_true", dest="as_json")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    errors = verify_repository(args.root)
    if args.as_json:
        print(json.dumps({"valid": not errors, "errors": errors}, indent=2))
    elif errors:
        for error in errors:
            print(f"FAIL: {error}")
        print(f"FAIL: repository verification found {len(errors)} error(s)")
    else:
        print("PASS: public configs, tracked runs, and repository scan are valid")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
