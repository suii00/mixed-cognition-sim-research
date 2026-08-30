#!/usr/bin/env python3
"""Build or verify the frozen six-cell directional-amplification audit bundle."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.directional_amplification_core import (  # noqa: E402
    DirectionalAuditError,
    build_audit_bundle,
    json_bytes,
)


DEFAULT_PLAN = (
    REPO_ROOT / "configs" / "directional_amplification_audit_v1" / "plan.json"
)


def _write_once(path: Path, data: bytes) -> None:
    if path.exists() or path.is_symlink():
        raise DirectionalAuditError(f"refusing to overwrite generated artifact: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def build(plan_path: Path) -> None:
    manifest, configs = build_audit_bundle(plan_path)
    root = plan_path.resolve().parent
    manifest_path = root / "manifest.json"
    config_root = root / "configs"
    if manifest_path.exists() or manifest_path.is_symlink():
        raise DirectionalAuditError(f"refusing to overwrite manifest: {manifest_path}")
    if config_root.exists() or config_root.is_symlink():
        raise DirectionalAuditError(f"refusing to overwrite config directory: {config_root}")
    for relative_path, data in configs.items():
        _write_once(root / relative_path, data)
    _write_once(manifest_path, json_bytes(manifest))
    print(
        f"wrote {len(configs)} configs and manifest under "
        f"{root.relative_to(REPO_ROOT)}"
    )


def check(plan_path: Path) -> None:
    manifest, configs = build_audit_bundle(plan_path)
    root = plan_path.resolve().parent
    manifest_path = root / "manifest.json"
    if not manifest_path.is_file() or manifest_path.read_bytes() != json_bytes(manifest):
        raise DirectionalAuditError("generated manifest differs from deterministic builder")
    expected_paths = {str(Path(relative).as_posix()) for relative in configs}
    config_root = root / "configs"
    observed_paths = (
        {
            str(path.relative_to(root).as_posix())
            for path in config_root.rglob("*")
            if path.is_file()
        }
        if config_root.is_dir()
        else set()
    )
    if observed_paths != expected_paths:
        raise DirectionalAuditError("generated config file set differs from manifest")
    for relative_path, data in configs.items():
        path = root / relative_path
        if path.read_bytes() != data:
            raise DirectionalAuditError(f"generated config differs: {relative_path}")
    print(
        f"verified {len(configs)} configs and manifest under "
        f"{root.relative_to(REPO_ROOT)}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, default=DEFAULT_PLAN)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    try:
        if args.check:
            check(args.plan)
        else:
            build(args.plan)
    except DirectionalAuditError as error:
        print(f"FAIL: {error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
