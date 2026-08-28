"""Deterministic blinded candidate-discovery projection."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, Optional

from engine.provenance import (
    RAW_JSONL_FILES,
    InvalidRunIdError,
    collect_git_info,
    normalize_run_id,
)
from tools.validate_run import validate_run


PROJECTION_VERSION = "candidate-projection-v1.0.0"
AUDIT_SCHEMA_VERSION = "candidate-projection-audit-v1.0.0"
PROJECTION_SPEC_PATH = (
    Path(__file__).resolve().parents[1]
    / "docs"
    / "CANDIDATE_DISCOVERY_PROJECTION_SPEC.md"
)
REVIEWER_MESSAGE_FIELDS = ("blind_message_id", "message")
REQUIRED_OUTPUT_FILES = (
    "reviewer/messages.jsonl",
    "reviewer/manifest.json",
    "audit/projection_meta.json",
    "audit/source_map.jsonl",
)
AUDIT_MANIFEST_FILE = "audit/manifest.json"
ALL_OUTPUT_FILES = (*REQUIRED_OUTPUT_FILES, AUDIT_MANIFEST_FILE)
SOURCE_EVIDENCE_FILES = (*RAW_JSONL_FILES, "run_meta.json")

PublicationHook = Callable[[str, Path], None]


class CandidateProjectionError(RuntimeError):
    """Base class for expected candidate-projection failures."""


class ProjectionInputError(CandidateProjectionError):
    """The source, specification, identifier, or output path is invalid."""


class ProjectionCollisionError(CandidateProjectionError):
    """The final leaf exists or another process owns publication."""


class ProjectionPublicationError(CandidateProjectionError):
    """A staged projection could not be verified or published."""


class _DuplicateJsonKey(ValueError):
    pass


@dataclass(frozen=True)
class PreparedProjection:
    projection_id: str
    source_run_id: str
    source_snapshot: Dict[str, Dict[str, Any]]
    files: Dict[str, bytes]


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def json_document_bytes(value: Any) -> bytes:
    return canonical_json_bytes(value) + b"\n"


def jsonl_bytes(values: Iterable[Dict[str, Any]]) -> bytes:
    return b"".join(json_document_bytes(value) for value in values)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey(key)
        result[key] = value
    return result


def _load_json_object(raw: bytes, label: str) -> Dict[str, Any]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ProjectionInputError(f"{label} is not UTF-8") from error
    try:
        value = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except (_DuplicateJsonKey, json.JSONDecodeError) as error:
        raise ProjectionInputError(
            f"{label} is not valid unambiguous JSON"
        ) from error
    if not isinstance(value, dict):
        raise ProjectionInputError(f"{label} root must be a JSON object")
    return value


def _require_sha256(value: str, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ProjectionInputError(
            f"{label} must be a lowercase 64-character SHA-256 digest"
        )
    return value


def _snapshot_source(run_dir: Path) -> Dict[str, Dict[str, Any]]:
    snapshot: Dict[str, Dict[str, Any]] = {}
    for filename in SOURCE_EVIDENCE_FILES:
        path = run_dir / filename
        if path.is_symlink() or not path.is_file():
            raise ProjectionInputError(
                f"source raw artifact is not a regular file: {filename}"
            )
        try:
            raw = path.read_bytes()
        except OSError as error:
            raise ProjectionInputError(
                f"source raw artifact cannot be read: {filename}"
            ) from error
        snapshot[filename] = {
            "sha256": sha256_bytes(raw),
            "bytes": len(raw),
        }
    return snapshot


def _read_phase1_messages(
    run_dir: Path,
) -> tuple[list[Dict[str, Any]], list[Dict[str, Any]]]:
    path = run_dir / "phase1_raw.jsonl"
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise ProjectionInputError("phase1_raw.jsonl cannot be read") from error

    occurrences: Dict[str, list[Dict[str, Any]]] = {}
    for line_number, line_bytes in enumerate(raw.splitlines(keepends=True), 1):
        if not line_bytes.strip():
            raise ProjectionInputError(
                f"phase1_raw.jsonl line {line_number} is empty"
            )
        record = _load_json_object(
            line_bytes,
            f"phase1_raw.jsonl line {line_number}",
        )
        parsed = record.get("parsed")
        if not isinstance(parsed, dict):
            continue
        message = parsed.get("message")
        if not isinstance(message, str) or not message:
            continue
        occurrences.setdefault(message, []).append({
            "file": "phase1_raw.jsonl",
            "line_number": line_number,
            "record_sha256": sha256_bytes(line_bytes),
            "message_sha256": sha256_bytes(message.encode("utf-8")),
        })

    ordered_messages = sorted(
        occurrences,
        key=lambda message: (
            sha256_bytes(message.encode("utf-8")),
            message.encode("utf-8"),
        ),
    )
    reviewer_rows: list[Dict[str, Any]] = []
    source_rows: list[Dict[str, Any]] = []
    for index, message in enumerate(ordered_messages, 1):
        blind_message_id = f"message-{index:06d}"
        reviewer_rows.append({
            "blind_message_id": blind_message_id,
            "message": message,
        })
        source_rows.append({
            "blind_message_id": blind_message_id,
            "occurrence_count": len(occurrences[message]),
            "source_references": occurrences[message],
        })
    return reviewer_rows, source_rows


def _artifact_entry(content: bytes) -> Dict[str, Any]:
    return {
        "sha256": sha256_bytes(content),
        "bytes": len(content),
        "lines": content.count(b"\n"),
    }


def prepare_projection(
    run_dir: Path | str,
    projection_id: str,
    projection_spec_sha256: str,
) -> PreparedProjection:
    try:
        normalized_projection_id = normalize_run_id(projection_id)
    except InvalidRunIdError as error:
        raise ProjectionInputError(f"invalid projection ID: {error}") from error

    expected_spec = _require_sha256(
        projection_spec_sha256,
        "projection specification SHA-256",
    )
    try:
        actual_spec = sha256_bytes(PROJECTION_SPEC_PATH.read_bytes())
    except OSError as error:
        raise ProjectionInputError(
            "candidate projection specification cannot be read"
        ) from error
    if actual_spec != expected_spec:
        raise ProjectionInputError(
            "candidate projection specification SHA-256 mismatch"
        )

    path = Path(run_dir)
    try:
        resolved_run = path.resolve(strict=True)
    except OSError as error:
        raise ProjectionInputError("source run directory does not exist") from error
    if path.is_symlink() or not resolved_run.is_dir():
        raise ProjectionInputError("source run must be a real directory")

    initial_snapshot = _snapshot_source(resolved_run)
    report = validate_run(resolved_run, strict=True)
    if not report.valid:
        details = "; ".join(report.errors[:3])
        raise ProjectionInputError(f"strict run validation failed: {details}")

    try:
        meta_raw = (resolved_run / "run_meta.json").read_bytes()
    except OSError as error:
        raise ProjectionInputError("run_meta.json cannot be read") from error
    meta = _load_json_object(meta_raw, "run_meta.json")
    if (
        meta.get("status") != "completed"
        or meta.get("aborted") is not False
        or meta.get("raw_manifest_status") != "available"
        or not isinstance(meta.get("raw_manifest"), dict)
    ):
        raise ProjectionInputError(
            "source run is not a completed raw-manifest-backed run"
        )
    source_run_id = meta.get("run_id")
    if not isinstance(source_run_id, str) or not source_run_id:
        raise ProjectionInputError("source run ID is unavailable")

    required_provenance = (
        "git_sha",
        "config_hash",
        "prompt_hash",
        "protocol_version",
        "log_schema_version",
        "metric_version",
    )
    if any(not isinstance(meta.get(field), str) or not meta[field] for field in required_provenance):
        raise ProjectionInputError("source run provenance is incomplete")

    generator_source = collect_git_info(Path(__file__).resolve().parents[1])
    if (
        generator_source.get("git_probe_status") != "available"
        or not isinstance(generator_source.get("git_sha"), str)
        or generator_source.get("git_dirty") is not False
    ):
        raise ProjectionInputError(
            "projection generator must have available clean Git provenance"
        )

    reviewer_rows, source_rows = _read_phase1_messages(resolved_run)
    if _snapshot_source(resolved_run) != initial_snapshot:
        raise ProjectionInputError("source evidence changed during preparation")

    files: Dict[str, bytes] = {}
    files["reviewer/messages.jsonl"] = jsonl_bytes(reviewer_rows)
    files["reviewer/manifest.json"] = json_document_bytes({
        "schema_version": PROJECTION_VERSION,
        "projection_spec_sha256": actual_spec,
        "reviewer_visible_fields": list(REVIEWER_MESSAGE_FIELDS),
        "unique_message_count": len(reviewer_rows),
        "files": {
            "messages.jsonl": _artifact_entry(files["reviewer/messages.jsonl"]),
        },
    })
    files["audit/projection_meta.json"] = json_document_bytes({
        "schema_version": AUDIT_SCHEMA_VERSION,
        "projection_version": PROJECTION_VERSION,
        "projection_id": normalized_projection_id,
        "projection_spec_sha256": actual_spec,
        "source_run_id": source_run_id,
        "source_git_sha": meta["git_sha"],
        "source_config_hash": meta["config_hash"],
        "source_prompt_hash": meta["prompt_hash"],
        "source_protocol_version": meta["protocol_version"],
        "source_log_schema_version": meta["log_schema_version"],
        "source_metric_version": meta["metric_version"],
        "source_raw_manifest": meta["raw_manifest"],
        "source_snapshot": initial_snapshot,
        "generator_source_git_sha": generator_source["git_sha"],
        "generator_source_dirty": False,
        "strict_validator_valid": True,
        "strict_validator_unverifiable": list(report.unverifiable),
        "reviewer_boundary": {
            "condition_labels_hidden": True,
            "model_labels_hidden": True,
            "bloc_labels_hidden": True,
            "agent_ids_hidden": True,
            "receiver_ids_accessed_by_reviewer": False,
            "steps_hidden": True,
            "source_lines_hidden": True,
            "occurrence_frequencies_hidden": True,
            "later_target_outputs_accessed_by_reviewer": False,
        },
        "unique_message_count": len(reviewer_rows),
        "source_occurrence_count": sum(
            row["occurrence_count"] for row in source_rows
        ),
    })
    files["audit/source_map.jsonl"] = jsonl_bytes(source_rows)
    files[AUDIT_MANIFEST_FILE] = json_document_bytes({
        "schema_version": AUDIT_SCHEMA_VERSION,
        "algorithm": "sha256",
        "files": {
            filename: _artifact_entry(files[filename])
            for filename in REQUIRED_OUTPUT_FILES
        },
    })
    return PreparedProjection(
        projection_id=normalized_projection_id,
        source_run_id=source_run_id,
        source_snapshot=initial_snapshot,
        files=files,
    )


def _is_within(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def _path_lexists(path: Path) -> bool:
    return os.path.lexists(path)


def _create_real_directory(path: Path, label: str) -> None:
    if _path_lexists(path) and (path.is_symlink() or not path.is_dir()):
        raise ProjectionInputError(f"{label} must be a real directory")
    try:
        path.mkdir(exist_ok=True)
    except OSError as error:
        raise ProjectionInputError(
            f"{label} cannot be created: {type(error).__name__}"
        ) from error
    if path.is_symlink() or not path.is_dir():
        raise ProjectionInputError(f"{label} must be a real directory")


def _prepare_layout(
    run_dir: Path | str,
    derived_root: Path | str,
    projection_id: str,
) -> tuple[Path, Path, Path, Path]:
    raw_path = Path(run_dir).resolve(strict=True)
    root = Path(derived_root)
    resolved_root = root.resolve(strict=False)
    if _is_within(resolved_root, raw_path):
        raise ProjectionInputError("derived root may not be inside the raw run")
    if root.exists() and root.is_symlink():
        raise ProjectionInputError("derived root may not be a symbolic link")
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise ProjectionInputError(
            f"derived root cannot be created: {type(error).__name__}"
        ) from error
    if root.is_symlink() or _is_within(root.resolve(), raw_path):
        raise ProjectionInputError("derived root resolves inside the raw run")

    version_directory = root / PROJECTION_VERSION
    _create_real_directory(version_directory, "projection version directory")
    resolved_version = version_directory.resolve(strict=True)
    if resolved_version != root.resolve(strict=True) / PROJECTION_VERSION:
        raise ProjectionInputError(
            "projection version directory must remain inside the derived root"
        )

    lock_directory = version_directory / ".locks"
    staging_directory = version_directory / ".staging"
    _create_real_directory(lock_directory, "publication lock directory")
    _create_real_directory(staging_directory, "staging directory")
    if lock_directory.resolve(strict=True).parent != resolved_version:
        raise ProjectionInputError(
            "publication lock directory must remain inside the projection directory"
        )
    if staging_directory.resolve(strict=True).parent != resolved_version:
        raise ProjectionInputError(
            "staging directory must remain inside the projection directory"
        )
    if os.stat(staging_directory).st_dev != os.stat(version_directory).st_dev:
        raise ProjectionInputError(
            "staging and final outputs must use the same filesystem"
        )

    return (
        version_directory,
        staging_directory,
        lock_directory / f"{projection_id}.lock",
        version_directory / projection_id,
    )


def _open_lock_file(lock_path: Path):
    if _path_lexists(lock_path) and lock_path.is_symlink():
        raise ProjectionInputError("publication lock file may not be a symlink")
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as error:
        raise ProjectionInputError(
            f"publication lock file cannot be opened: {type(error).__name__}"
        ) from error
    return os.fdopen(descriptor, "r+b", buffering=0)


@contextmanager
def _publication_lock(lock_path: Path, projection_id: str) -> Iterator[None]:
    handle = _open_lock_file(lock_path)
    locked = False
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
            os.fsync(handle.fileno())
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                raise ProjectionCollisionError(
                    "projection publication is already in progress for "
                    f"projection ID {projection_id!r}"
                ) from error
            raise ProjectionInputError(
                f"publication lock cannot be acquired: {type(error).__name__}"
            ) from error
        locked = True
        yield
    finally:
        if locked:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        handle.close()


def _create_staging_leaf(staging_directory: Path, projection_id: str) -> Path:
    for _ in range(16):
        staging_leaf = staging_directory / f"{projection_id}-{uuid.uuid4().hex}"
        try:
            staging_leaf.mkdir(exist_ok=False)
        except FileExistsError:
            continue
        except OSError as error:
            raise ProjectionPublicationError(
                f"staging directory cannot be created: {type(error).__name__}"
            ) from error
        return staging_leaf
    raise ProjectionPublicationError("a unique staging directory could not be created")


def _fsync_directory(path: Path) -> None:
    if os.name != "posix":
        return
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _verify_staging_leaf(
    staging_leaf: Path,
    prepared: PreparedProjection,
) -> None:
    actual_files = {
        item.relative_to(staging_leaf).as_posix()
        for item in staging_leaf.rglob("*")
        if item.is_file()
    }
    if actual_files != set(ALL_OUTPUT_FILES):
        raise ProjectionPublicationError("staging leaf has an invalid file set")
    for relative_name in ALL_OUTPUT_FILES:
        path = staging_leaf / relative_name
        if path.is_symlink() or not path.is_file():
            raise ProjectionPublicationError(
                f"staged projection artifact is not a regular file: {relative_name}"
            )
        if path.read_bytes() != prepared.files[relative_name]:
            raise ProjectionPublicationError(
                f"staged projection differs from prepared bytes: {relative_name}"
            )

    try:
        manifest = json.loads(
            (staging_leaf / AUDIT_MANIFEST_FILE).read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ProjectionPublicationError("audit manifest cannot be decoded") from error
    if (
        manifest.get("algorithm") != "sha256"
        or manifest.get("schema_version") != AUDIT_SCHEMA_VERSION
        or not isinstance(manifest.get("files"), dict)
        or set(manifest["files"]) != set(REQUIRED_OUTPUT_FILES)
    ):
        raise ProjectionPublicationError("audit manifest structure is invalid")
    for relative_name in REQUIRED_OUTPUT_FILES:
        content = (staging_leaf / relative_name).read_bytes()
        if manifest["files"][relative_name] != _artifact_entry(content):
            raise ProjectionPublicationError(
                f"audit manifest entry is invalid: {relative_name}"
            )


def write_prepared_projection(
    prepared: PreparedProjection,
    run_dir: Path | str,
    derived_root: Path | str,
    before_claim: Optional[Callable[[], None]] = None,
    publication_hook: Optional[PublicationHook] = None,
) -> Path:
    (
        version_directory,
        staging_directory,
        lock_path,
        final_leaf,
    ) = _prepare_layout(run_dir, derived_root, prepared.projection_id)
    del version_directory
    if before_claim is not None:
        before_claim()
    with _publication_lock(lock_path, prepared.projection_id):
        if _path_lexists(final_leaf):
            raise ProjectionCollisionError(
                f"projection output already exists for {prepared.projection_id!r}"
            )
        if _snapshot_source(Path(run_dir).resolve(strict=True)) != prepared.source_snapshot:
            raise ProjectionInputError("source evidence changed before publication")
        staging_leaf = _create_staging_leaf(
            staging_directory,
            prepared.projection_id,
        )
        created_parents: set[Path] = set()
        for relative_name in ALL_OUTPUT_FILES:
            path = staging_leaf / relative_name
            if path.parent not in created_parents:
                path.parent.mkdir(parents=True, exist_ok=True)
                created_parents.add(path.parent)
            with path.open("xb") as handle:
                handle.write(prepared.files[relative_name])
                handle.flush()
                os.fsync(handle.fileno())
            if publication_hook is not None:
                publication_hook(f"after_{relative_name}_write", staging_leaf)
        _verify_staging_leaf(staging_leaf, prepared)
        _fsync_directory(staging_leaf / "reviewer")
        _fsync_directory(staging_leaf / "audit")
        _fsync_directory(staging_leaf)
        if publication_hook is not None:
            publication_hook("before_publish", staging_leaf)
        if _snapshot_source(Path(run_dir).resolve(strict=True)) != prepared.source_snapshot:
            raise ProjectionInputError("source evidence changed before publication")
        if _path_lexists(final_leaf):
            raise ProjectionCollisionError(
                f"projection output already exists for {prepared.projection_id!r}"
            )
        try:
            os.rename(staging_leaf, final_leaf)
        except OSError as error:
            if _path_lexists(final_leaf):
                raise ProjectionCollisionError(
                    f"projection output already exists for {prepared.projection_id!r}"
                ) from error
            raise ProjectionPublicationError(
                f"staged projection cannot be published: {type(error).__name__}"
            ) from error
    return final_leaf


def project_run(
    run_dir: Path | str,
    projection_id: str,
    projection_spec_sha256: str,
    derived_root: Path | str,
    before_claim: Optional[Callable[[], None]] = None,
    publication_hook: Optional[PublicationHook] = None,
) -> Path:
    prepared = prepare_projection(
        run_dir,
        projection_id,
        projection_spec_sha256,
    )
    return write_prepared_projection(
        prepared,
        run_dir,
        derived_root,
        before_claim=before_claim,
        publication_hook=publication_hook,
    )
