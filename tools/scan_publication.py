"""Scan a publication tree (and optionally every reachable commit) for unsafe data."""

from __future__ import annotations

import argparse
import fnmatch
import ipaddress
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Mapping, Sequence


TEXT_EXTENSIONS = {
    ".csv", ".html", ".json", ".jsonl", ".md", ".py", ".sh", ".txt",
    ".yaml", ".yml", ".toml", ".ini", ".cfg", ".ps1", ".bat",
}
ARCHIVE_SUFFIXES = (
    ".bundle", ".tar", ".tar.gz", ".tgz", ".zip", ".7z", ".rar", ".gz",
)


@dataclass(frozen=True)
class Pattern:
    pattern_id: str
    regex: re.Pattern[str]


PATTERNS = (
    Pattern("posix_home_path", re.compile(r"/(?:home|Users)/[^/\s\"'<>]+")),
    Pattern("windows_user_path", re.compile(r"(?i)(?:[A-Z]:[\\/])Users[\\/][^\\/\s\"'<>]+")),
    Pattern("internal_hostname", re.compile(r"(?i)\bgpu-sv(?:[-._][A-Za-z0-9]+)*\b")),
    Pattern(
        "gpu_uuid",
        re.compile(
            r"GPU-[0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
            r"[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12})?"
        ),
    ),
    Pattern("mac_address", re.compile(r"(?i)(?<![0-9a-f])(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}(?![0-9a-f])")),
    Pattern("pci_id", re.compile(r"(?i)(?<![0-9a-f])(?:[0-9a-f]{4,8}:)?[0-9a-f]{2}:[0-9a-f]{2}\.[0-7](?![0-9a-f])")),
    Pattern("socket_uri", re.compile(r"(?i)\b(?:tcp|ssh|redis|postgres(?:ql)?|mysql|mongodb)://[^\s\"'<>]+")),
    Pattern("sshd_session", re.compile(r"(?i)\bsshd(?:\[[0-9]+\])?(?::|\b)")),
    Pattern("email", re.compile(r"(?i)(?<![A-Z0-9._%+-])[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}(?![A-Z0-9._%+-])")),
    Pattern("github_token", re.compile(r"\b(?:gh[opusr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b")),
    Pattern("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    Pattern("private_key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    Pattern("bearer_token", re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]{20,}={0,2}\b")),
    Pattern(
        "credential_assignment",
        re.compile(
            r"(?i)\b(?:api[_-]?key|secret|password|passwd|access[_-]?token)\b"
            r"\s*[:=]\s*[\"']?[A-Za-z0-9._~+/-]{12,}[\"']?"
        ),
    ),
    Pattern("credential_url", re.compile(r"(?i)\bhttps?://[^\s/@:]+:[^\s/@]+@[^\s/]+")),
)

PRIVATE_IP_CANDIDATE_RE = re.compile(r"(?<![0-9])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?![0-9])")


@dataclass(frozen=True)
class Finding:
    path: str
    pattern_id: str
    line: int


def _private_ip_findings(text: str) -> Iterable[int]:
    for line_number, line in enumerate(text.splitlines(), 1):
        for match in PRIVATE_IP_CANDIDATE_RE.finditer(line):
            try:
                address = ipaddress.ip_address(match.group(0))
            except ValueError:
                continue
            if address.is_private and not address.is_loopback:
                yield line_number


def scan_text(path: str, text: str) -> list[Finding]:
    findings: list[Finding] = [
        Finding(path=path, pattern_id="private_ip", line=line)
        for line in _private_ip_findings(text)
    ]
    lines = text.splitlines()
    for pattern in PATTERNS:
        for line_number, line in enumerate(lines, 1):
            if pattern.regex.search(line):
                findings.append(Finding(path, pattern.pattern_id, line_number))
    return findings


def _read_text_file(path: Path) -> str | None:
    if path.suffix.lower() not in TEXT_EXTENSIONS:
        return None
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return None


def scan_tree(root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for path in sorted(root.rglob("*")):
        relative_path = path.relative_to(root)
        if (
            any(part in {".git", ".venv", "__pycache__", ".tmp"} for part in relative_path.parts)
            or any(part.startswith("validation-output-") for part in relative_path.parts)
            or path.is_symlink()
        ):
            continue
        relative = relative_path.as_posix()
        if path.is_file() and relative.lower().endswith(ARCHIVE_SUFFIXES):
            findings.append(Finding(relative, "archive_file", 0))
        if not path.is_file():
            continue
        text = _read_text_file(path)
        if text is not None:
            findings.extend(scan_text(relative, text))
    return findings


def _git(args: Sequence[str], root: Path, *, text: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        check=False,
        capture_output=True,
        text=text,
        timeout=120,
    )


def scan_git_history(root: Path) -> list[Finding]:
    if _git(["rev-parse", "--is-inside-work-tree"], root).returncode != 0:
        raise RuntimeError("--git-history requires a Git work tree")
    revisions = _git(["rev-list", "--all"], root)
    if revisions.returncode != 0:
        raise RuntimeError("cannot enumerate reachable Git history")
    findings: list[Finding] = []
    seen_blobs: set[str] = set()
    for revision in revisions.stdout.splitlines():
        tree = _git(["ls-tree", "-r", "--full-tree", revision], root)
        if tree.returncode != 0:
            raise RuntimeError(f"cannot enumerate tree for {revision}")
        for row in tree.stdout.splitlines():
            try:
                metadata, path = row.split("\t", 1)
                _mode, object_type, blob = metadata.split(" ", 2)
            except ValueError:
                continue
            if object_type != "blob" or blob in seen_blobs:
                continue
            seen_blobs.add(blob)
            lower = path.lower()
            if lower.endswith(ARCHIVE_SUFFIXES):
                findings.append(Finding(f"{revision}:{path}", "archive_file", 0))
            if Path(path).suffix.lower() not in TEXT_EXTENSIONS:
                continue
            content = _git(["cat-file", "blob", blob], root, text=False)
            if content.returncode != 0:
                raise RuntimeError(f"cannot read Git blob {blob}")
            try:
                decoded = content.stdout.decode("utf-8")
            except UnicodeDecodeError:
                continue
            findings.extend(
                Finding(f"{revision}:{finding.path}", finding.pattern_id, finding.line)
                for finding in scan_text(path, decoded)
            )
    return findings


def load_allowlist(path: Path | None) -> list[Mapping[str, object]]:
    if path is None:
        return []
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("schema_version") != "publication-scan-allowlist-v1.0.0":
        raise ValueError("unsupported scanner allowlist schema_version")
    entries = value.get("entries")
    if not isinstance(entries, list):
        raise ValueError("scanner allowlist entries must be a list")
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("scanner allowlist entry must be an object")
        if not all(isinstance(entry.get(key), str) and entry[key] for key in ("path", "pattern", "reason")):
            raise ValueError("each scanner allowlist entry needs path, pattern, and reason")
    return entries


def apply_allowlist(
    findings: Sequence[Finding], entries: Sequence[Mapping[str, object]]
) -> tuple[list[Finding], list[dict[str, object]]]:
    allowed: list[Finding] = []
    remaining: list[Finding] = []
    counts = [0] * len(entries)
    for finding in findings:
        matched = False
        logical_path = finding.path.split(":", 1)[-1] if re.match(r"^[0-9a-f]{40}:", finding.path) else finding.path
        for index, entry in enumerate(entries):
            if entry["pattern"] == finding.pattern_id and fnmatch.fnmatch(logical_path, str(entry["path"])):
                max_matches = entry.get("max_matches")
                if max_matches is not None and counts[index] >= int(max_matches):
                    continue
                counts[index] += 1
                allowed.append(finding)
                matched = True
                break
        if not matched:
            remaining.append(finding)
    usage = [
        {
            "path": entry["path"],
            "pattern": entry["pattern"],
            "reason": entry["reason"],
            "matches": counts[index],
        }
        for index, entry in enumerate(entries)
    ]
    return remaining, usage


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--allowlist", type=Path)
    parser.add_argument("--git-history", action="store_true")
    parser.add_argument("--json-output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = args.root.resolve()
    try:
        findings = scan_tree(root)
        if args.git_history:
            findings.extend(scan_git_history(root))
        entries = load_allowlist(args.allowlist)
        remaining, usage = apply_allowlist(findings, entries)
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as error:
        print(f"ERROR: {type(error).__name__}: {error}")
        return 2
    summary = {
        "schema_version": "publication-scan-result-v1.0.0",
        "root": str(root),
        "git_history_scanned": bool(args.git_history),
        "finding_count": len(remaining),
        "allowlisted_count": len(findings) - len(remaining),
        "allowlist_usage": usage,
        "findings": [finding.__dict__ for finding in remaining],
    }
    if args.json_output:
        args.json_output.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    for finding in remaining:
        location = finding.path if finding.line == 0 else f"{finding.path}:{finding.line}"
        print(f"FAIL: {finding.pattern_id}: {location}")
    if remaining:
        print(f"FAIL: publication scan found {len(remaining)} unapproved match(es)")
        return 1
    print(
        "PASS: publication scan found 0 unapproved matches "
        f"({len(findings)} allowlisted match(es))"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
