"""Publication packaging boundaries, independent of GPU execution."""

import hashlib
import json
import os
from pathlib import Path
from unittest import mock

import pytest

from tools.build_pages_site import SCHEMA_VERSION, build_site


@pytest.fixture
def public_tree(tmp_path):
    root = tmp_path / "repository"
    (root / "site").mkdir(parents=True)
    (root / "derived").mkdir()
    data = b'<!doctype html><title>Saved evidence</title><p>Original data.</p>'
    (root / "derived/report.html").write_bytes(data)
    (root / "site/index.html").write_text(
        '<!doctype html><a href="derived/report.html">Report</a><a href="assets.json">Files</a>',
        encoding="utf-8",
    )
    catalog = {"schema_version": SCHEMA_VERSION, "assets": [{
        "path": "derived/report.html", "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "git_blob": hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest(),
        "source_commit": "a" * 40,
    }]}
    (root / "site/assets.json").write_text(json.dumps(catalog), encoding="utf-8")
    return root


def test_only_declared_files_are_copied_without_changes(public_tree):
    root = public_tree
    (root / "derived/unlisted.html").write_text("Do not publish", encoding="utf-8")
    output = root / ".tmp/pages-site_test"
    result = build_site(root, output)
    assert result["asset_count"] == 1
    assert {p.relative_to(output).as_posix() for p in output.rglob("*") if p.is_file()} == {
        "index.html", "assets.json", "derived/report.html",
    }
    for destination, source in (("index.html", "site/index.html"),
                                ("assets.json", "site/assets.json"),
                                ("derived/report.html", "derived/report.html")):
        assert (output / destination).read_bytes() == (root / source).read_bytes()


def test_check_creates_nothing(public_tree):
    before = {p.relative_to(public_tree) for p in public_tree.rglob("*")}
    assert build_site(public_tree)["validated"] is True
    assert {p.relative_to(public_tree) for p in public_tree.rglob("*")} == before


@pytest.mark.parametrize("change,match", [
    ("changed_asset", "size mismatch"),
    ("changed_digest", "SHA-256 mismatch"),
    ("changed_blob", "Git blob mismatch"),
    ("traversal", "within the site"),
    ("raw_log", "under derived"),
    ("duplicate", "duplicate"),
    ("missing_asset", "missing or invalid"),
    ("broken_link", "missing local link"),
    ("root_link", "project Pages prefix"),
    ("hidden_asset", "every cataloged asset"),
    ("unsafe_index", "unsafe public input"),
])
def test_rejects_invalid_inputs_before_output(public_tree, change, match):
    root = public_tree
    catalog_path = root / "site/assets.json"
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    entry = catalog["assets"][0]
    if change == "changed_asset":
        (root / "derived/report.html").write_bytes(b"changed")
    elif change == "changed_digest":
        entry["sha256"] = "0" * 64
    elif change == "changed_blob":
        entry["git_blob"] = "0" * 40
    elif change == "traversal":
        entry["path"] = "derived/../../outside.html"
    elif change == "raw_log":
        entry["path"] = "runs/output_example/agent_log.jsonl"
    elif change == "duplicate":
        catalog["assets"].append(dict(entry))
    elif change == "missing_asset":
        (root / "derived/report.html").unlink()
    elif change in {"broken_link", "root_link", "hidden_asset", "unsafe_index"}:
        extra = {
            "broken_link": '<a href="missing.html">Missing</a>',
            "root_link": '<a href="/derived/report.html">Root</a>',
            "hidden_asset": "<p>No report link</p>",
            "unsafe_index": "<p>" + "192.168." + "40.20" + "</p>",
        }[change]
        index = root / "site/index.html"
        old = "" if change == "hidden_asset" else index.read_text(encoding="utf-8")
        index.write_text(old + extra, encoding="utf-8")
    catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
    with pytest.raises(ValueError, match=match):
        build_site(root, root / ".tmp/pages-site_invalid")
    assert not (root / ".tmp").exists()


def test_output_collision_preserves_existing_bytes(public_tree):
    output = public_tree / ".tmp/pages-site_existing"
    output.mkdir(parents=True)
    (output / "keep.txt").write_bytes(b"keep")
    with pytest.raises(FileExistsError):
        build_site(public_tree, output)
    assert (output / "keep.txt").read_bytes() == b"keep"
    assert list(output.iterdir()) == [output / "keep.txt"]


@pytest.mark.parametrize("destination", ["derived/pages-site_bad", "runs/output_new", ".tmp/../outside"])
def test_output_cannot_enter_evidence_directories(public_tree, destination):
    with pytest.raises(ValueError, match="output must"):
        build_site(public_tree, Path(destination))


def test_input_hard_link_is_rejected(public_tree):
    os.link(public_tree / "derived/report.html", public_tree / "derived/alias.html")
    with pytest.raises(ValueError, match="hard links"):
        build_site(public_tree, Path(".tmp/pages-site_hardlink"))
    assert not (public_tree / ".tmp").exists()


def test_symlink_is_rejected_before_output(public_tree):
    # Windows creation privileges differ; inject the link observation itself.
    original = Path.is_symlink
    with mock.patch.object(Path, "is_symlink", lambda p: p.name == "report.html" or original(p)):
        with pytest.raises(ValueError, match="symbolic links"):
            build_site(public_tree, Path(".tmp/pages-site_symlink"))
    assert not (public_tree / ".tmp").exists()
