#!/usr/bin/env python3
"""Derive pre-registered metrics for the direction-presentation audit."""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.direction_presentation_core import (  # noqa: E402
    DirectionPresentationAuditError,
    analyze_audit,
    json_bytes,
)
from tools.scan_publication import scan_text  # noqa: E402


DEFAULT_MANIFEST = (
    REPO_ROOT / "configs" / "direction_presentation_audit_v2" / "manifest.json"
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--runs-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        output = args.output.resolve()
        if output.exists() or output.is_symlink():
            raise DirectionPresentationAuditError(
                f"refusing to overwrite output: {output}"
            )
        result = analyze_audit(args.manifest, args.runs_root)
        data = json_bytes(result)
        if scan_text(output.name, data.decode("utf-8")):
            raise DirectionPresentationAuditError(
                "derived result failed publication scan"
            )
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(data)
    except DirectionPresentationAuditError as error:
        print(f"FAIL: {error}")
        return 1
    print(f"PASS: wrote {output}")
    print(f"SHA256: {hashlib.sha256(data).hexdigest()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
