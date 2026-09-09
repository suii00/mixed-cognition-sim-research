#!/usr/bin/env python3
"""Read-only refuge-layout artifact audit; publish newly computed check facts."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys

VERSION = "refuge-layout-study-verification-v1.0.0"
DEFAULT_SOURCE = "a5f1421818bbbed15fa810b0906fddcc991fa885"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha(payload):
    return hashlib.sha256(payload).hexdigest()


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def fact(payload):
    return {"sha256": sha(payload), "bytes": len(payload), "lines": payload.count(b"\n")}


def safe_member(name):
    require(isinstance(name, str) and name and "\\" not in name and ":" not in name,
            "invalid manifest/reference member")
    path = PurePosixPath(name)
    require(path.parts and not path.is_absolute() and all(p not in (".", "..", "") for p in path.parts)
            and path.as_posix() == name, "unsafe manifest/reference member")
    return path


def reject_links(path):
    for member in (path, *path.parents):
        require(not member.is_symlink() and not (member.exists() and
            getattr(member.lstat(), "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)),
            "link or reparse ancestry rejected")


def tree(path):
    reject_links(path)
    require(path.is_dir(), "input directory missing")
    result = {}
    for member in sorted(path.rglob("*")):
        reject_links(member)
        require(member.is_file() or member.is_dir(), "nonregular input entry")
        if member.is_file():
            result[member.relative_to(path).as_posix()] = fact(member.read_bytes())
    return result


def match_fact(actual, expected):
    require(isinstance(expected, dict) and "sha256" in expected and "bytes" in expected,
            "manifest hash/bytes missing")
    require(all(actual.get(k) == v for k, v in expected.items() if k in ("sha256", "bytes", "lines")),
            "manifest hash/bytes/lines mismatch")


def verify_members(root, manifest, members_key, snapshot, manifest_name, counts):
    members = manifest[members_key]
    require(isinstance(members, dict), "manifest members must be a mapping")
    for name, expected in members.items():
        safe_member(name)
        require(name in snapshot, "manifest input missing")
        match_fact(snapshot[name], expected)
        counts["manifest_members"] += 1
    require(set(snapshot) == set(members) | {manifest_name}, "unexpected or unmanifested artifact file")


class References:
    def __init__(self, runs):
        self.runs = runs
        self.cache = {}
        self.counts = Counter()

    def resolve(self, reference, inherited_run):
        run_id = reference.get("run_id", inherited_run)
        require(run_id in self.runs, "reference has no recognized run")
        name = reference["file"]
        safe_member(name)
        require(name.endswith(".jsonl") and "/" not in name, "reference must name a raw JSONL file")
        key = run_id, name
        if key not in self.cache:
            self.cache[key] = (self.runs[run_id] / name).read_bytes().splitlines(keepends=True)
        number = reference.get("line_number", reference.get("line"))
        require(isinstance(number, int) and not isinstance(number, bool) and 1 <= number <= len(self.cache[key]),
                "reference line outside raw coverage")
        raw = self.cache[key][number - 1]
        expected_sha = reference.get("line_sha256", reference.get("sha256"))
        require(sha(raw) == expected_sha, "raw reference SHA mismatch")
        if "line_bytes" in reference:
            require(reference["line_bytes"] == len(raw), "raw reference byte count mismatch")
        self.counts["raw_line_references"] += 1
        return json.loads(raw)

    def walk(self, value, inherited_run=None):
        if isinstance(value, list):
            for child in value:
                self.walk(child, inherited_run)
        elif isinstance(value, dict):
            context = value.get("run_id", inherited_run)
            if "file" in value and ("line_number" in value or "line" in value):
                self.resolve(value, context)
                return
            if "reference" in value and ("raw_row" in value or "row" in value):
                actual = self.resolve(value["reference"], context)
                require(actual == value.get("raw_row", value.get("row")), "example/replay raw row differs from source")
                self.counts["raw_row_equalities"] += 1
            for key, child in value.items():
                if key not in ("raw_row", "row"):
                    self.walk(child, context)


def load_json(path):
    return json.loads(path.read_bytes())


def git_bytes(repo, source, member):
    safe_member(member)
    result = subprocess.run(["git", "show", f"{source}:{member}"], cwd=repo, capture_output=True, check=False)
    require(result.returncode == 0, "source Git object unavailable")
    return result.stdout


def audit(args):
    repo = args.repo.resolve(strict=True)
    sys.path.insert(0, str(repo))
    from engine.provenance import compute_config_hash
    from tools.artifact_boundaries import find_immutable_artifact_ancestor
    from tools.build_refuge_layout_study import load_verified_manifest
    from tools.plot_refuge_layout_geometry import geometry_from_configs
    from tools.render_disaster_run import build_replay, read_records
    from tools.run_disaster_behavior_pilot import public_tree_safe
    from tools.scan_publication import scan_text
    from tools.validate_run import ALL_COUNTERS, validate_run

    source = args.source_sha
    require(re.fullmatch(r"[0-9a-f]{40}", source), "invalid expected source SHA")
    runs = [p.resolve(strict=True) for p in args.runs]
    analysis, geometry = args.analysis.resolve(strict=True), args.geometry.resolve(strict=True)
    replays = [p.resolve(strict=True) for p in args.replays]
    # Check supplied ancestry before resolving, so aliases cannot conceal links.
    for p in [args.repo, *args.runs, args.analysis, args.geometry, *args.replays]:
        reject_links(p)
    require(len(set(runs)) == 4 and len(set(replays)) == 4, "four distinct raw and replay directories required")
    inputs = [*runs, analysis, geometry, *replays]
    require(len(set(inputs)) == 10, "all input trees must be distinct")
    snapshots = {p: tree(p) for p in inputs}
    require(all(public_tree_safe(p, []) for p in inputs), "read-only publication/decoded-body boundary failure")
    counts = Counter({"publication_scanned_trees": len(inputs), "strict_validated_runs": 0})
    watched = {}

    def frozen(member, expected=None):
        safe_member(member)
        p = repo / member
        reject_links(p)
        payload = p.read_bytes()
        require(payload == git_bytes(repo, source, member), "current input source differs from frozen source")
        if expected:
            match_fact(fact(payload), expected if isinstance(expected, dict) else {"sha256": expected, "bytes": len(payload)})
        watched[p] = fact(payload)
        counts["frozen_source_checks"] += 1
        return payload

    config_root = repo / "configs/refuge_layout_study_v1"
    manifest = load_verified_manifest(config_root)
    manifest_payload = frozen("configs/refuge_layout_study_v1/manifest.json")
    rows = {r["run_id"]: r for r in manifest["rows"]}
    require(len(rows) == 4, "fixed source manifest does not contain four runs")
    configs = {rid: json.loads(frozen("configs/refuge_layout_study_v1/" + row["filename"])) for rid, row in rows.items()}
    metric_sha = sha(frozen("docs/REFUGE_LAYOUT_STUDY_METRIC_V1_SPEC.md"))
    warning_sha = sha(frozen("docs/DISASTER_METRIC_V2_SPEC.md"))
    run_paths, metas, starts, validation_facts = {}, {}, [], []
    for raw in runs:
        meta = load_json(raw / "run_meta.json")
        rid = meta["run_id"]
        require(rid in rows and rid not in run_paths and raw.name == "output_" + rid, "unexpected or duplicate raw run")
        require(meta["git_sha"] == source and meta["git_dirty"] is False, "raw inference source mismatch")
        require(meta["config"] == configs[rid] and meta["config_hash"] == compute_config_hash(configs[rid]), "raw config mismatch")
        duration = rows[rid]["duration"]
        require(meta["expected_agents"] == meta["observed_agents"] == 24, "raw population mismatch")
        require(meta["expected_steps"] == duration, "raw planned duration mismatch")
        report = validate_run(raw, strict=True)
        require(report.valid, "raw failed strict validation")
        counts["strict_validated_runs"] += 1
        verify_members(raw, meta["raw_manifest"], "files", snapshots[raw], "run_meta.json", counts)
        positions = [json.loads(line) for line in (raw / "positions.jsonl").read_bytes().splitlines()]
        initial = sorted((p for p in positions if p["step"] == 0), key=lambda p: p["agent_id"])
        require([p["agent_id"] for p in initial] == list(range(24)), "initial agent coverage mismatch")
        starts.append([{k: p[k] for k in ("agent_id", "model", "bloc", "position")} for p in initial])
        run_paths[rid], metas[rid] = raw, meta
        validation_facts.append({"run_id": rid, "status": meta["status"], "completed_steps": meta["completed_steps"],
            "expected_steps": duration, "strict_valid": True, "strict_unverifiable": report.unverifiable,
            "counters": {k: meta.get(k) for k in ALL_COUNTERS}})
    require(set(run_paths) == set(rows) and all(s == starts[0] for s in starts), "four initial worlds/assignments differ")
    refs = References(run_paths)

    # Analysis manifests, every JSON/JSONL reference, and embedded raw examples.
    derived_manifest = load_json(analysis / "derived_manifest.json")
    verify_members(analysis, derived_manifest, "files", snapshots[analysis], "derived_manifest.json", counts)
    ameta = load_json(analysis / "analysis_meta.json")
    require(ameta["metric_version"] == "refuge-layout-study-metric-v1.0.0" and ameta["metric_spec_sha256"] == metric_sha,
            "analysis metric version/spec mismatch")
    require(ameta["warning_metric_spec_sha256"] == warning_sha and ameta["manifest_sha256"] == sha(manifest_payload),
            "analysis warning/config manifest SHA mismatch")
    require(ameta["inference_source_commits"] == [source] and ameta["source_state"]["git_dirty"] is False,
            "analysis source-state mismatch")
    for name, expected in ameta["implementation_manifests"].items():
        frozen(name, expected)
    summary = load_json(analysis / "summary.json")
    require(summary["batch_id"] == manifest["batch_id"] and len(summary["runs"]) == 4, "analysis batch mismatch")
    for row in summary["runs"]:
        rid = row["run_id"]
        require(row["raw_file_manifests"] == snapshots[run_paths[rid]], "analysis raw tree snapshot differs")
        require(row["source_sha"] == source and row["config_sha256"] == sha((config_root / rows[rid]["filename"]).read_bytes()),
                "analysis per-run source/config mismatch")
    for path in sorted(analysis.iterdir()):
        if path.suffix == ".json":
            refs.walk(load_json(path))
        elif path.suffix == ".jsonl":
            for line in path.read_bytes().splitlines():
                refs.walk(json.loads(line))
                counts["analysis_jsonl_rows"] += 1
    require(load_json(analysis / "example.json")["example"] == summary["example"], "example outputs disagree")

    # Configuration-derived geometry is fully rederived, not merely hash checked.
    gmanifest = load_json(geometry / "artifact_manifest.json")
    verify_members(geometry, gmanifest, "files", snapshots[geometry], "artifact_manifest.json", counts)
    gmeta = load_json(geometry / "input_manifest.json")
    require(gmeta["source_state"]["git_sha"] == source and gmeta["source_state"]["git_dirty"] is False,
            "geometry source mismatch")
    require(gmeta["source_manifest_sha256"] == sha(manifest_payload), "geometry source manifest mismatch")
    for name, expected in gmeta["implementations"].items():
        frozen(name, expected)
    for name, expected in gmeta["configs"].items():
        frozen("configs/refuge_layout_study_v1/" + name, expected)
    expected_geometry = geometry_from_configs({rows[rid]["layout"]: config for rid, config in configs.items() if rows[rid]["duration"] == 60})
    geometry_data = load_json(geometry / "geometry.json")
    require(geometry_data == expected_geometry, "geometry data differs from public-config reconstruction")
    for panel in geometry_data["panels"]:
        signature = [{"agent_id": p["agent_id"], "bloc": p["model_name"], "position": p["position"]} for p in panel["initial_agents"]]
        require(signature == [{k: p[k] for k in ("agent_id", "bloc", "position")} for p in starts[0]],
                "raw starts differ from configuration-derived geometry starts")
        counts["geometry_cell_distances_rederived"] += len(panel["eligible_cells"])
    counts["matched_initial_agent_positions"] = 4 * 24

    # Replay JSON is parsed as inert data. Neither JS nor model-generated text is executed.
    replay_runs = set()
    for directory in replays:
        rmeta = load_json(directory / "input_manifest.json")
        verify_members(directory, rmeta, "artifacts", snapshots[directory], "input_manifest.json", counts)
        rid = rmeta["run_id"]
        require(rid in run_paths and rid not in replay_runs, "unexpected/duplicate replay run")
        replay_runs.add(rid)
        require(rmeta["raw_snapshot_unchanged"] is True, "replay raw snapshot check absent")
        require(rmeta["config"] == configs[rid] and rmeta["config_sha256"] == compute_config_hash(configs[rid])
                and rmeta["source_git_sha"] == source, "replay source/config mismatch")
        expected_raw = {name: {k: v for k, v in f.items() if k in ("sha256", "bytes")} for name, f in snapshots[run_paths[rid]].items()}
        require(rmeta["raw_files"] == expected_raw, "replay raw tree snapshot differs")
        for name, expected in rmeta["implementation"].items():
            frozen(name, expected)
        html = (directory / "replay.html").read_text(encoding="utf-8")
        payloads = re.findall(r'<script id="data" type="application/json">(.*?)</script>', html, flags=re.S)
        require(len(payloads) == 1, "replay JSON data element missing or duplicated")
        data = json.loads(payloads[0])
        require(data["run_id"] == rid and data["raw_files"] == expected_raw, "replay embedded provenance mismatch")
        refs.walk(data, rid)
        expected_replay = build_replay(metas[rid], read_records(run_paths[rid]))
        for key, value in expected_replay.items():
            require(data.get(key) == value, "replay frame/semantics data differs from raw derivation")
        counts["replay_frames_rederived"] += len(data["frames"])
    require(replay_runs == set(rows), "not all four runs have replay artifacts")
    counts.update(refs.counts)

    # Snapshot comparisons include every input file, not just selected raw logs.
    require(all(tree(path) == original for path, original in snapshots.items()), "input tree changed during audit")
    require(all(fact(path.read_bytes()) == original for path, original in watched.items()), "source/config changed during audit")
    protocols = {config["simulation"]["protocol_version"] for config in configs.values()}
    require(protocols == {"refuge-layout-study-v1.0.0"}, "config protocol mismatch")
    public = {"verification_version": VERSION, "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "protocol_version": next(iter(protocols)), "metric_version": ameta["metric_version"],
        "batch_id": manifest["batch_id"], "passed": True, "inference_source_sha": source,
        "study_metric_spec_sha256": metric_sha, "warning_metric_spec_sha256": warning_sha,
        "config_manifest_sha256": sha(manifest_payload), "counts": dict(counts), "runs": validation_facts,
        "all_input_trees_unchanged": True, "all_frozen_source_inputs_unchanged": True,
        "initial_positions_and_model_assignment_equal": True, "geometry_reconstruction_matches_raw_starts": True,
        "checker_sha256": sha(Path(__file__).read_bytes()),
        "input_trees": [{"name": path.name, "files": snapshot} for path, snapshot in snapshots.items()],
        "omissions": ["Browser interaction and rendered HTML visual QA were blocked by browser URL policy.",
            "PNG/SVG contents are hash-verified here; visual QA is a separate check.",
            "This check recomputes geometry and replay frames and resolves all analysis references; it does not independently rederive every aggregate analysis metric.",
            "Runtime binding identities and external signatures are not independently attested by this local check."],
        "llm_outputs_executed": False, "raw_or_derived_inputs_modified": False}
    record = encoded(public)
    require(not scan_text("verification.json", record.decode("utf-8")), "unsafe verification facts")
    if args.check_only:
        return {"passed": True, "counts": dict(counts), "output_created": False}
    output = args.output
    require(output is not None, "output is required unless check-only")
    reject_links(output)
    require(not output.exists() and not output.is_symlink(), "verification output collision")
    output = output.resolve(strict=False)
    require(output.parent == (repo / "derived").resolve() and re.fullmatch(re.escape(VERSION) + r"_\d{8}T\d{6}(?:\d{6})?Z", output.name),
            "verification output must be a new versioned timestamp directly under derived")
    require(find_immutable_artifact_ancestor(output) is None, "verification output inside immutable input")
    out_manifest = encoded({"verification_version": VERSION, "algorithm": "sha256", "files": {"verification.json": fact(record)}})
    require(not scan_text("artifact_manifest.json", out_manifest.decode("utf-8")), "unsafe verification manifest")
    output.mkdir(parents=False, exist_ok=False)
    with (output / "verification.json").open("xb") as handle:
        handle.write(record)
    require(all(tree(path) == original for path, original in snapshots.items()), "input changed during verification publication")
    with (output / "artifact_manifest.json").open("xb") as handle:
        handle.write(out_manifest)
    return {"passed": True, "counts": dict(counts), "output_created": True, "output_name": output.name}


def self_test():
    # No filesystem writes or LLM-output execution. Test both raw-line reference contracts.
    payload = b'{"step":10,"agent_id":1}\n'
    refs = References({"fixture": Path("unused")})
    refs.cache[("fixture", "positions.jsonl")] = [payload]
    analysis_ref = {"run_id": "fixture", "file": "positions.jsonl", "line_number": 1,
                    "line_sha256": sha(payload), "line_bytes": len(payload)}
    replay_ref = {"file": "positions.jsonl", "line": 1, "sha256": sha(payload)}
    refs.walk({"reference": analysis_ref, "raw_row": json.loads(payload)})
    refs.walk({"reference": replay_ref, "row": json.loads(payload)}, "fixture")
    require(refs.counts["raw_row_equalities"] == 2, "fixture observation count mismatch")
    for bad in ({**replay_ref, "line": 2}, {**replay_ref, "sha256": "0" * 64},
                {**analysis_ref, "line_bytes": 0}, {**replay_ref, "file": "../positions.jsonl"}):
        try:
            refs.resolve(bad, "fixture")
        except ValueError:
            pass
        else:
            raise ValueError("fixture unsafe reference accepted")
    try:
        refs.walk({"reference": replay_ref, "row": {"step": 11}}, "fixture")
    except ValueError:
        pass
    else:
        raise ValueError("fixture altered raw row accepted")
    match_fact(fact(payload), {"sha256": sha(payload), "bytes": len(payload)})
    print(json.dumps({"self_test": "passed", "reference_contracts": 2, "rejection_cases": 5}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--repo", type=Path)
    parser.add_argument("--runs", type=Path, nargs=4)
    parser.add_argument("--analysis", type=Path)
    parser.add_argument("--geometry", type=Path)
    parser.add_argument("--replays", type=Path, nargs=4)
    parser.add_argument("--source-sha", default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    try:
        if args.self_test:
            self_test()
        else:
            require(all((args.repo, args.runs, args.analysis, args.geometry, args.replays)), "all input arguments are required")
            print(json.dumps(audit(args), ensure_ascii=False, sort_keys=True))
    except (ValueError, OSError, KeyError, TypeError) as error:
        # Error messages are locally authored; avoid dumping paths or unsafe input contents.
        message = str(error) if type(error) is ValueError else type(error).__name__
        print(json.dumps({"passed": False, "error": message}), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
