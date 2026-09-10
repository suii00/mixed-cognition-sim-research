"""Validate and package an explicit set of existing public files for GitHub Pages.

Evidence files are copied byte for byte. No simulation, analysis, sanitization,
or recursive repository copy is performed. All input checks precede output.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import stat
import sys
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from typing import Sequence
from urllib.parse import unquote, urlsplit

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.scan_publication import scan_text  # noqa: E402

SCHEMA_VERSION = "pages-assets-v1.0.0"
SITE_FILES = {"index.html": "site/index.html", "assets.json": "site/assets.json"}
MAX_SITE_BYTES = 1_000_000_000


def safe_relative(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_./-]+", value):
        raise ValueError("invalid public relative path")
    parts = PurePosixPath(value).parts
    if not parts or value.startswith("/") or any(p in {".", ".."} for p in parts):
        raise ValueError("public path must stay within the site")
    if PurePosixPath(value).as_posix() != value:
        raise ValueError("public path must be canonical")
    return value


def reject_links(path: Path, root: Path) -> None:
    for part in (path, *path.parents):
        if part == root:
            break
        if part.is_symlink():
            raise ValueError("symbolic links are not valid Pages inputs or outputs")
        if part.exists():
            info = part.lstat()
            if getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                raise ValueError("reparse points are not valid Pages inputs or outputs")
            if part.is_file() and info.st_nlink != 1:
                raise ValueError("hard links are not valid Pages inputs or outputs")


def read_input(root: Path, relative: str) -> bytes:
    path = root / safe_relative(relative)
    reject_links(path, root)
    if not path.is_file() or not path.resolve().is_relative_to(root):
        raise ValueError(f"missing or invalid input: {relative}")
    return path.read_bytes()


def scan_input(name: str, data: bytes) -> None:
    if Path(name).suffix in {".html", ".json"}:
        findings = scan_text(name, data.decode("utf-8"))
        if findings:
            first = findings[0]
            raise ValueError(f"unsafe public input: {name}:{first.line} ({first.pattern_id})")


class Links(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.urls: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for name, value in attrs:
            if name in {"href", "src", "poster"} and value:
                self.urls.append(value)


def local_links(name: str, data: bytes, available: set[str]) -> set[str]:
    parser = Links()
    parser.feed(data.decode("utf-8"))
    found: set[str] = set()
    for url in parser.urls:
        parts = urlsplit(url)
        if parts.scheme:
            if parts.scheme != "https" or not parts.netloc:
                raise ValueError(f"unsupported URL in {name}")
            continue
        if parts.netloc or parts.path.startswith("/"):
            raise ValueError(f"link must work below the project Pages prefix: {name}")
        if not parts.path:
            continue
        relative = unquote(parts.path)
        if relative.startswith("./"):
            relative = relative[2:]
        relative = safe_relative(relative)
        target = (PurePosixPath(name).parent / relative).as_posix()
        if target not in available:
            raise ValueError(f"missing local link target in {name}: {target}")
        found.add(target)
    return found


def validated_files(root: Path) -> dict[str, bytes]:
    """Read-only: validate every input and link before creating any output."""
    files = {dest: read_input(root, source) for dest, source in SITE_FILES.items()}
    catalog = json.loads(files["assets.json"])
    if (not isinstance(catalog, dict) or set(catalog) != {"schema_version", "assets"}
            or catalog["schema_version"] != SCHEMA_VERSION):
        raise ValueError("unsupported Pages asset catalog")
    assets = catalog["assets"]
    if not isinstance(assets, list) or not assets:
        raise ValueError("Pages catalog must list its assets explicitly")
    required = {"path", "bytes", "sha256", "git_blob", "source_commit"}
    for entry in assets:
        if not isinstance(entry, dict) or set(entry) != required:
            raise ValueError("invalid Pages asset entry")
        name = safe_relative(entry["path"])
        if not name.startswith("derived/") or Path(name).suffix not in {".html", ".png", ".mp4"}:
            raise ValueError("Pages evidence assets must be HTML, PNG, or MP4 under derived/")
        if name in files:
            raise ValueError("duplicate Pages asset path")
        for key, length in (("sha256", 64), ("git_blob", 40), ("source_commit", 40)):
            if not isinstance(entry[key], str) or not re.fullmatch(rf"[0-9a-f]{{{length}}}", entry[key]):
                raise ValueError(f"invalid {key} for {name}")
        data = read_input(root, name)
        if type(entry["bytes"]) is not int or len(data) != entry["bytes"]:
            raise ValueError(f"asset size mismatch: {name}")
        if hashlib.sha256(data).hexdigest() != entry["sha256"]:
            raise ValueError(f"asset SHA-256 mismatch: {name}")
        blob = hashlib.sha1(f"blob {len(data)}\0".encode("ascii") + data).hexdigest()
        if blob != entry["git_blob"]:
            raise ValueError(f"asset Git blob mismatch: {name}")
        files[name] = data
    if sum(map(len, files.values())) > MAX_SITE_BYTES:
        raise ValueError("Pages package exceeds the site size limit")
    for name, data in files.items():
        scan_input(name, data)
        if name.endswith(".html"):
            linked = local_links(name, data, set(files))
            if name == "index.html" and not {e["path"] for e in assets}.issubset(linked):
                raise ValueError("index must link to every cataloged asset")
    return files


def build_site(root: Path, output: Path | None = None) -> dict[str, object]:
    root = root.resolve()
    if output is not None:
        if not output.is_absolute():
            output = root / output
        reject_links(output, root)
        output = output.resolve()
        if output.parent != root / ".tmp" or not re.fullmatch(r"pages-site_[A-Za-z0-9_-]+", output.name):
            raise ValueError("output must be a new .tmp/pages-site_<unique-id> directory")
        if output.exists():
            raise FileExistsError("Pages output already exists; choose a new output directory")
    files = validated_files(root)
    result: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "files": len(files),
        "asset_count": len(files) - len(SITE_FILES),
        "bytes": sum(map(len, files.values())),
        "validated": True,
    }
    if output is not None:
        output.mkdir(parents=True, exist_ok=False)
        for name, data in files.items():
            destination = output / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open("xb") as stream:
                stream.write(data)
            if destination.read_bytes() != data:
                raise RuntimeError("Pages copy did not preserve input bytes")
        result["output_dir"] = output.relative_to(root).as_posix()
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--check", action="store_true", help="read-only validation without output")
    group.add_argument("--output-dir", type=Path)
    args = parser.parse_args(argv)
    output = args.output_dir
    if not args.check and output is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
        output = Path(f".tmp/pages-site_{stamp}")
    try:
        print(json.dumps(build_site(REPO_ROOT, output), indent=2))
    except (ValueError, OSError, RuntimeError) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
