from __future__ import annotations

import hashlib
import json
import os
import stat
import time
import urllib.request
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .core import append_manifest_occurrence, validate_fonds_path
from .ingest import capture_fonds_path
from .spool import (
    BUFFER_BYTES,
    DEAD_LETTER_REASON_FIELDS,
    PermanentSpoolError,
    dead_letter,
    dead_letter_reason_path,
    ensure_layout,
    is_dead_letter_sidecar,
    requeue_stale,
    upgrade_legacy_job,
    validate_blob,
    validate_job,
    validate_legacy_job,
)

SPOOL_STATES = ("incoming", "processing", "dead-letter")


@dataclass(frozen=True)
class Finding:
    code: str
    severity: str
    path: str
    detail: str
    repairable: bool = False


def _redirected(path: Path, path_stat: os.stat_result) -> bool:
    if stat.S_ISLNK(path_stat.st_mode):
        return True
    is_junction = getattr(path, "is_junction", None)
    if is_junction is not None:
        try:
            if is_junction():
                return True
        except OSError:
            return True
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(getattr(path_stat, "st_file_attributes", 0) & reparse)


def _scan_tree(root: Path):
    pending = [root]
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                path = directory / entry.name
                yield path
                if entry.is_dir(follow_symlinks=False):
                    pending.append(path)


def _topology_finding(
        path: Path, expected: str, code: str) -> Finding | None:
    try:
        path_stat = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as exc:
        return Finding(code, "error", str(path), str(exc))
    if _redirected(path, path_stat):
        return Finding(code, "error", str(path), "redirected path")
    valid = (stat.S_ISDIR(path_stat.st_mode) if expected == "directory"
             else stat.S_ISREG(path_stat.st_mode))
    if not valid:
        return Finding(code, "error", str(path), f"expected {expected}")
    return None


def _ancestor_topology(path: Path, code: str) -> list[Finding]:
    findings: list[Finding] = []
    chain = [path, *path.parents]
    for component in reversed(chain):
        if not os.path.lexists(component):
            continue
        finding = _topology_finding(component, "directory", code)
        if finding is not None:
            findings.append(finding)
            break
    return findings


def _spool_topology(spool_root: Path, *, repair: bool) -> list[Finding]:
    code = "repair_topology_invalid" if repair else "spool_topology_invalid"
    findings = _ancestor_topology(spool_root, code) if repair else []
    root_finding = _topology_finding(spool_root, "directory", code)
    if root_finding is not None:
        findings.append(root_finding)
        return findings
    if not os.path.lexists(spool_root):
        return findings
    paths = _spool_paths(spool_root)
    for path in paths.values():
        finding = _topology_finding(path, "directory", code)
        if finding is not None:
            findings.append(finding)
    lock_finding = _topology_finding(
        spool_root / "state.lock", "regular file", code)
    if lock_finding is not None:
        findings.append(lock_finding)
    return findings


def _archive_topology(archive_root: Path, *, repair: bool) -> list[Finding]:
    code = "repair_topology_invalid" if repair else "archive_topology_invalid"
    findings = _ancestor_topology(archive_root, code) if repair else []
    root_finding = _topology_finding(archive_root, "directory", code)
    if root_finding is not None:
        findings.append(root_finding)
        return findings
    if not os.path.lexists(archive_root):
        return findings
    for path in (
            archive_root / "objects",
            archive_root / "objects" / "sha256",
            archive_root / "fonds"):
        finding = _topology_finding(path, "directory", code)
        if finding is not None:
            findings.append(finding)
    for path in (
            archive_root / "manifest.jsonl",
            archive_root / "manifest.lock"):
        finding = _topology_finding(path, "regular file", code)
        if finding is not None:
            findings.append(finding)
    object_root = archive_root / "objects" / "sha256"
    if not findings and os.path.lexists(object_root):
        try:
            for path in _scan_tree(object_root):
                path_stat = path.stat(follow_symlinks=False)
                if _redirected(path, path_stat):
                    findings.append(Finding(
                        code, "error", str(path), "redirected archive shard"))
        except OSError as exc:
            findings.append(Finding(code, "error", str(object_root), str(exc)))
    return findings


def _repair_refusals(findings: list[Finding]) -> list[Finding]:
    return [Finding(
        "repair_refused", "error", item.path,
        f"unsafe topology: {item.detail}") for item in findings]


def _spool_paths(spool_root: Path) -> dict[str, Path]:
    return {name: spool_root / name for name in
            ("blobs", *SPOOL_STATES)}


def _json_files(path: Path) -> tuple[list[Path], Finding | None]:
    try:
        with os.scandir(path) as entries:
            return sorted(
                path / entry.name for entry in entries
                if entry.name.endswith(".json")), None
    except FileNotFoundError:
        return [], None
    except OSError as exc:
        return [], Finding(
            "spool_topology_unreadable", "error", str(path), str(exc))


def _repair_evidence_topology(spool_root: Path) -> list[Finding]:
    findings: list[Finding] = []
    paths = _spool_paths(spool_root)
    for directory in (spool_root, *(paths[state] for state in SPOOL_STATES)):
        candidates, error = _json_files(directory)
        if error is not None:
            findings.append(error)
            continue
        for candidate in candidates:
            finding = _topology_finding(
                candidate, "regular file", "repair_topology_invalid")
            if finding is not None:
                findings.append(finding)
                continue
            value, read_error = _read_json(candidate)
            if read_error is not None or not isinstance(value, dict):
                continue
            if value.get("schema_version") == 2:
                sha = value.get("blob_sha256")
                if _valid_sha(sha):
                    blob_finding = _topology_finding(
                        paths["blobs"] / sha, "regular file",
                        "repair_topology_invalid")
                    if blob_finding is not None:
                        findings.append(blob_finding)
    return findings


def _spool_manifests(spool_root: Path) -> list[Path]:
    paths = _spool_paths(spool_root)
    manifests, _ = _json_files(spool_root)
    for state in SPOOL_STATES:
        candidates, _ = _json_files(paths[state])
        for manifest in candidates:
            if (state == "dead-letter" and
                    is_dead_letter_sidecar(manifest)):
                continue
            manifests.append(manifest)
    return manifests


def _read_json(path: Path) -> tuple[object | None, Exception | None]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except (OSError, UnicodeError, ValueError) as exc:
        return None, exc


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(BUFFER_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_legacy_extraction(value: object) -> None:
    if not isinstance(value, dict) or value.get("extract_only") is not True:
        raise PermanentSpoolError(
            "invalid extract_only: expected boolean true")
    session_id = value.get("session_id")
    if not isinstance(session_id, str) or not session_id.strip():
        raise PermanentSpoolError("invalid session_id")
    attempts = value.get("attempts", 0)
    if type(attempts) is not int or attempts < 0:
        raise PermanentSpoolError("invalid attempts")


def _validate_blob_read_only(spool_root: Path, job: dict) -> None:
    sha = job["blob_sha256"]
    blob = spool_root / "blobs" / sha
    try:
        blob_stat = blob.stat(follow_symlinks=False)
        if not stat.S_ISREG(blob_stat.st_mode):
            raise OSError("not a regular file")
        if blob_stat.st_size != job["blob_bytes"]:
            raise PermanentSpoolError(
                f"spool blob size mismatch: {sha}")
        if _sha256_file(blob) != sha:
            raise PermanentSpoolError(
                f"spool blob checksum mismatch: {sha}")
    except FileNotFoundError as exc:
        raise PermanentSpoolError(f"missing spool blob: {sha}") from exc
    except OSError as exc:
        raise PermanentSpoolError(
            f"unreadable spool blob: {sha}: {exc}") from exc


def _valid_spool_value(value: object) -> bool:
    try:
        if isinstance(value, dict) and "schema_version" in value:
            validate_job(value)
        elif isinstance(value, dict) and value.get("extract_only") is True:
            _validate_legacy_extraction(value)
        else:
            validate_legacy_job(value)
    except PermanentSpoolError:
        return False
    return True


def _reason_record(value: object) -> dict | None:
    if not isinstance(value, dict) or set(value) != DEAD_LETTER_REASON_FIELDS:
        return None
    manifest = value.get("manifest")
    reason = value.get("reason")
    dead_lettered_at = value.get("dead_lettered_at")
    if (not isinstance(manifest, str) or not manifest or
            Path(manifest).name != manifest or
            not isinstance(reason, str) or
            not isinstance(dead_lettered_at, str) or not dead_lettered_at):
        return None
    return value


def _dead_letter_evidence(
        dead_root: Path,
) -> tuple[list[Path], int, list[Finding]]:
    candidates, enumeration_error = _json_files(dead_root)
    if enumeration_error is not None:
        return [], 0, [enumeration_error]
    topology_findings: list[Finding] = []
    safe_candidates: list[Path] = []
    for path in candidates:
        finding = _topology_finding(
            path, "regular file", "spool_topology_invalid")
        if finding is not None:
            topology_findings.append(finding)
        else:
            safe_candidates.append(path)
    candidates = safe_candidates
    parsed = {path: _read_json(path) for path in candidates}
    valid_jobs = {
        path for path, (value, error) in parsed.items()
        if error is None and _valid_spool_value(value)}
    records = {
        path: record
        for path, (value, error) in parsed.items()
        if error is None and (record := _reason_record(value)) is not None}
    referenced = {
        dead_root / record["manifest"] for record in records.values()
        if dead_root / record["manifest"] in parsed}
    evidence = valid_jobs | referenced
    expected = {dead_letter_reason_path(path): path for path in evidence}
    for path in candidates:
        if path in records:
            continue
        if path in expected and path not in valid_jobs:
            continue
        evidence.add(path)
    expected = {dead_letter_reason_path(path): path for path in evidence}
    findings: list[Finding] = topology_findings

    for reason_path, manifest in expected.items():
        if reason_path not in parsed:
            findings.append(Finding(
                "dead_letter_reason_missing", "error", str(manifest),
                str(reason_path)))
            continue
        record = records.get(reason_path)
        if record is None or record["manifest"] != manifest.name:
            findings.append(Finding(
                "dead_letter_reason_invalid", "error", str(reason_path),
                f"expected reason for {manifest.name}"))

    for reason_path, record in records.items():
        manifest = dead_root / record["manifest"]
        expected_path = dead_letter_reason_path(manifest)
        if reason_path != expected_path:
            findings.append(Finding(
                "dead_letter_reason_mismatched", "error", str(reason_path),
                f"expected {expected_path}"))
        elif manifest not in evidence:
            findings.append(Finding(
                "dead_letter_reason_orphan", "error", str(reason_path),
                f"missing evidence manifest: {manifest}"))

    return sorted(evidence), len(evidence), findings


def _unmanifested_capture_evidence(blob_root: Path) -> list[Finding]:
    findings: list[Finding] = []
    try:
        with os.scandir(blob_root) as entries:
            candidates = [
                blob_root / entry.name for entry in entries
                if (entry.name.startswith(".collision.") or
                    (entry.name.startswith(".job_") and
                     entry.name.endswith(".tmp")))
            ]
    except FileNotFoundError:
        return findings
    except OSError as exc:
        return [Finding(
            "spool_topology_unreadable", "error", str(blob_root), str(exc))]
    for path in sorted(candidates):
        topology = _topology_finding(
            path, "regular file", "spool_topology_invalid")
        if topology is not None:
            findings.append(topology)
            continue
        findings.append(Finding(
            "unmanifested_capture_evidence", "error", str(path),
            "preserved capture bytes have no job manifest; retain for review"))
    return findings


def inspect_spool(spool_root: Path) -> list[Finding]:
    """Inspect spool evidence without creating directories or lock files."""
    paths = _spool_paths(spool_root)
    findings = _spool_topology(spool_root, repair=False)
    if findings:
        return findings
    findings.extend(_unmanifested_capture_evidence(paths["blobs"]))
    manifests, enumeration_error = _json_files(spool_root)
    if enumeration_error is not None:
        findings.append(enumeration_error)
    for state in ("incoming", "processing"):
        state_manifests, enumeration_error = _json_files(paths[state])
        manifests.extend(state_manifests)
        if enumeration_error is not None:
            findings.append(enumeration_error)
    dead_manifests, dead_count, reason_findings = _dead_letter_evidence(
        paths["dead-letter"])
    manifests.extend(dead_manifests)
    findings.extend(reason_findings)
    for manifest in manifests:
        topology_finding = _topology_finding(
            manifest, "regular file", "spool_topology_invalid")
        if topology_finding is not None:
            findings.append(topology_finding)
            continue
        if manifest.parent == paths["processing"]:
            try:
                if time.time() - manifest.stat().st_mtime > 900:
                    findings.append(Finding(
                        "stale_processing_job", "error", str(manifest),
                        "older than 15 minutes", True))
            except OSError as exc:
                findings.append(Finding(
                    "invalid_manifest", "error", str(manifest),
                    f"cannot inspect lease age: {exc}", True))

        value, read_error = _read_json(manifest)
        if read_error is not None:
            findings.append(Finding(
                "invalid_manifest", "error", str(manifest),
                str(read_error), True))
            continue
        try:
            if isinstance(value, dict) and "schema_version" in value:
                job = validate_job(value)
                if job["kind"] == "capture_snapshot":
                    try:
                        _validate_blob_read_only(spool_root, job)
                    except PermanentSpoolError as exc:
                        findings.append(Finding(
                            "spool_blob_invalid", "error", str(manifest),
                            str(exc), True))
            elif isinstance(value, dict) and value.get("extract_only") is True:
                _validate_legacy_extraction(value)
            else:
                legacy = validate_legacy_job(value)
                source = Path(legacy["transcript_path"]).expanduser()
                if not source.is_file():
                    findings.append(Finding(
                        "legacy_source_missing", "error", str(manifest),
                        str(source), True))
        except PermanentSpoolError as exc:
            findings.append(Finding(
                "invalid_manifest", "error", str(manifest), str(exc), True))

    if dead_count:
        findings.append(Finding(
            "dead_letter_jobs", "error", str(paths["dead-letter"]),
            str(dead_count)))
    for job_id, identities in _spool_identity_collisions(spool_root).items():
        findings.append(Finding(
            "occurrence_identity_collision", "error", str(spool_root),
            f"{job_id}: {sorted(identities)!r}"))
    return findings


def _valid_sha(value: object) -> bool:
    return (isinstance(value, str) and len(value) == 64 and
            all(char in "0123456789abcdef" for char in value))


def _archive_objects(
        archive_root: Path) -> tuple[dict[str, Path], list[Finding]]:
    object_root = archive_root / "objects" / "sha256"
    objects: dict[str, Path] = {}
    findings: list[Finding] = []
    if not os.path.lexists(object_root):
        return objects, findings
    try:
        for path in _scan_tree(object_root):
            try:
                path_stat = path.stat(follow_symlinks=False)
            except OSError as exc:
                findings.append(Finding(
                    "archive_object_unreadable", "error", str(path),
                    str(exc)))
                continue
            if stat.S_ISDIR(path_stat.st_mode):
                continue
            if not stat.S_ISREG(path_stat.st_mode):
                findings.append(Finding(
                    "invalid_archive_object", "error", str(path),
                    "expected a regular file"))
                continue
            relative = path.relative_to(object_root)
            sha = path.name
            parts = relative.parts
            if (not _valid_sha(sha) or len(parts) != 3 or
                    parts[0] != sha[:2] or parts[1] != sha[2:4]):
                findings.append(Finding(
                    "invalid_object_name", "error", str(path),
                    "expected objects/sha256/aa/bb/<sha256>"))
                continue
            objects[sha] = path
    except OSError as exc:
        findings.append(Finding(
            "archive_object_unreadable", "error", str(object_root),
            str(exc)))
    return objects, findings


def inspect_archive(archive_root: Path) -> list[Finding]:
    findings = _archive_topology(archive_root, repair=False)
    if findings:
        return findings
    objects, object_findings = _archive_objects(archive_root)
    findings.extend(object_findings)
    mentioned: set[str] = set()
    occurrence_identities: dict[str, set[tuple[str, str]]] = {}
    manifest = archive_root / "manifest.jsonl"
    try:
        with manifest.open("r", encoding="utf-8") as handle:
            for line_no, line in enumerate(handle, 1):
                location = f"{manifest}:{line_no}"
                try:
                    entry = json.loads(line)
                    if not isinstance(entry, dict):
                        raise ValueError("manifest entry must be an object")
                    sha = entry.get("sha256")
                    if not _valid_sha(sha):
                        raise ValueError(
                            "sha256 must be a lowercase SHA-256 string")
                except (TypeError, ValueError) as exc:
                    findings.append(Finding(
                        "invalid_manifest_line", "error", location,
                        str(exc)))
                    continue

                mentioned.add(sha)
                fonds_path = entry.get("fonds_path")
                try:
                    if not isinstance(fonds_path, str):
                        raise ValueError("fonds_path must be a string")
                    validate_fonds_path(archive_root, fonds_path)
                except (OSError, ValueError) as exc:
                    findings.append(Finding(
                        "unsafe_fonds_path", "error", location, str(exc)))
                job_id = entry.get("ingest_job_id")
                if (isinstance(job_id, str) and job_id and
                        isinstance(fonds_path, str)):
                    occurrence_identities.setdefault(job_id, set()).add(
                        (sha, fonds_path))
                if sha not in objects:
                    findings.append(Finding(
                        "manifest_object_missing", "error", location, sha))
    except FileNotFoundError:
        pass
    except (OSError, UnicodeError) as exc:
        findings.append(Finding(
            "invalid_manifest_file", "error", str(manifest), str(exc)))

    for sha, path in objects.items():
        try:
            actual = _sha256_file(path)
        except OSError as exc:
            findings.append(Finding(
                "archive_object_unreadable", "error", str(path), str(exc)))
            continue
        if actual != sha:
            findings.append(Finding(
                "object_hash_mismatch", "error", str(path), actual))
        if sha not in mentioned:
            findings.append(Finding(
                "orphan_object", "warning", str(path), sha))
    for job_id, identities in occurrence_identities.items():
        if len(identities) > 1:
            findings.append(Finding(
                "occurrence_identity_collision", "error", str(manifest),
                f"{job_id}: {sorted(identities)!r}"))
    return findings


def _dead_letter_invalid(
        spool_root: Path, manifest: Path, reason: str,
        code: str) -> Finding:
    try:
        path_stat = manifest.stat(follow_symlinks=False)
        if not stat.S_ISREG(path_stat.st_mode):
            raise OSError("evidence is not a regular file")
        preserved = dead_letter(spool_root, manifest, reason)
    except OSError as exc:
        return Finding(
            "job_repair_skipped", "error", str(manifest), str(exc))
    return Finding(code, "info", str(preserved), reason)


def repair_spool(spool_root: Path) -> list[Finding]:
    topology = [
        *_spool_topology(spool_root, repair=True),
        *_repair_evidence_topology(spool_root),
    ]
    if topology:
        return _repair_refusals(topology)
    actions: list[Finding] = []
    try:
        ensure_layout(spool_root)
    except OSError as exc:
        return [Finding("repair_refused", "error", str(spool_root), str(exc))]
    try:
        requeued = requeue_stale(spool_root)
    except OSError as exc:
        actions.append(Finding(
            "repair_refused", "error", str(spool_root), str(exc)))
    else:
        if requeued:
            actions.append(Finding(
                "stale_jobs_requeued", "info", str(spool_root),
                str(requeued)))

    paths = _spool_paths(spool_root)
    root_manifests, _ = _json_files(spool_root)
    incoming_manifests, _ = _json_files(paths["incoming"])
    manifests = [*root_manifests, *incoming_manifests]
    for manifest in manifests:
        value, read_error = _read_json(manifest)
        if read_error is not None:
            actions.append(_dead_letter_invalid(
                spool_root, manifest, f"invalid manifest: {read_error}",
                "invalid_job_dead_lettered"))
            continue
        try:
            if isinstance(value, dict) and "schema_version" in value:
                job = validate_job(value)
                if job["kind"] == "capture_snapshot":
                    validate_blob(spool_root, job)
                continue
            if isinstance(value, dict) and value.get("extract_only") is True:
                _validate_legacy_extraction(value)
                continue
            legacy = validate_legacy_job(value)
        except PermanentSpoolError as exc:
            code = ("corrupt_job_dead_lettered"
                    if isinstance(value, dict) and
                    value.get("schema_version") == 2
                    else "invalid_job_dead_lettered")
            actions.append(_dead_letter_invalid(
                spool_root, manifest, str(exc), code))
            continue

        source = Path(legacy["transcript_path"]).expanduser()
        if not source.is_file():
            actions.append(_dead_letter_invalid(
                spool_root, manifest, "legacy transcript source missing",
                "legacy_dead_lettered"))
            continue
        before_dead = set(_dead_letter_evidence(paths["dead-letter"])[0])
        try:
            upgraded = upgrade_legacy_job(spool_root, manifest)
        except (OSError, PermanentSpoolError, ValueError) as exc:
            actions.append(_dead_letter_invalid(
                spool_root, manifest, str(exc),
                "invalid_job_dead_lettered"))
            continue
        preserved_path = manifest
        if upgraded is None:
            after_dead = set(_dead_letter_evidence(paths["dead-letter"])[0])
            created = sorted(after_dead - before_dead)
            if len(created) == 1:
                preserved_path = created[0]
        actions.append(Finding(
            "legacy_upgraded" if upgraded else "legacy_dead_lettered",
            "info", str(upgraded or preserved_path),
            str(upgraded or "preserved in dead-letter")))
    return actions


def _parse_created_at(value: object) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("created_at must be a nonempty string")
    created = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return created


def _manifest_identities(
        manifest: Path,
) -> tuple[
        dict[str, set[tuple[str, str]]],
        Counter[tuple[str, str, str]],
        Counter[tuple[str, str]],
] | None:
    job_identities: dict[str, set[tuple[str, str]]] = {}
    fallbacks: Counter[tuple[str, str, str]] = Counter()
    legacy_pairs: Counter[tuple[str, str]] = Counter()
    try:
        with manifest.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    entry = json.loads(line)
                except (TypeError, ValueError):
                    continue
                if not isinstance(entry, dict):
                    continue
                sha = entry.get("sha256")
                fonds = entry.get("fonds_path")
                occurrence_at = entry.get("occurrence_at")
                if not _valid_sha(sha) or not isinstance(fonds, str):
                    continue
                job_id = entry.get("ingest_job_id")
                if isinstance(job_id, str) and job_id:
                    job_identities.setdefault(job_id, set()).add((sha, fonds))
                else:
                    legacy_pairs[(sha, fonds)] += 1
                    if isinstance(occurrence_at, str):
                        fallbacks[(sha, occurrence_at, fonds)] += 1
    except FileNotFoundError:
        pass
    except (OSError, UnicodeError):
        return None
    return job_identities, fallbacks, legacy_pairs


def _archive_evidence(
        value: object,
) -> tuple[str, int | None, datetime, str, str | None] | None:
    try:
        if isinstance(value, dict) and "schema_version" in value:
            job = validate_job(value)
            if job["kind"] != "capture_snapshot":
                return None
            return (
                job["blob_sha256"], job["blob_bytes"],
                _parse_created_at(job["created_at"]), job["session_id"],
                job["job_id"],
            )
        legacy = validate_legacy_job(value)
        sha = legacy.get("blob_sha256")
        if not _valid_sha(sha):
            return None
        blob_bytes = legacy.get("blob_bytes")
        if blob_bytes is not None and (
                type(blob_bytes) is not int or blob_bytes < 0):
            return None
        return (
            sha, blob_bytes, _parse_created_at(legacy.get("created_at")),
            legacy.get("session_id", "unknown"), None,
        )
    except (PermanentSpoolError, TypeError, ValueError):
        return None


def _spool_identity_collisions(
        spool_root: Path) -> dict[str, set[tuple[str, str]]]:
    identities: dict[str, set[tuple[str, str]]] = {}
    for job_path in _spool_manifests(spool_root):
        value, error = _read_json(job_path)
        if error is not None:
            continue
        evidence = _archive_evidence(value)
        if evidence is None:
            continue
        sha, _size, created, session_id, job_id = evidence
        if job_id is None:
            continue
        fonds = capture_fonds_path(session_id, created)
        identities.setdefault(job_id, set()).add((sha, fonds))
    return {
        job_id: values for job_id, values in identities.items()
        if len(values) > 1}


def _consume_legacy_pair(
        pair: tuple[str, str],
        fallbacks: Counter[tuple[str, str, str]],
        legacy_pairs: Counter[tuple[str, str]],
) -> bool:
    if not legacy_pairs[pair]:
        return False
    legacy_pairs[pair] -= 1
    sha, fonds = pair
    for fallback, count in fallbacks.items():
        if count and fallback[0] == sha and fallback[2] == fonds:
            fallbacks[fallback] -= 1
            break
    return True


def _same_file_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        stat.S_ISREG(left.st_mode) and stat.S_ISREG(right.st_mode) and
        (left.st_dev, left.st_ino, left.st_size) ==
        (right.st_dev, right.st_ino, right.st_size)
    )


def _verify_repair_object(
        path: Path, sha: str, expected_bytes: int | None,
) -> tuple[object, os.stat_result]:
    path_before = path.stat(follow_symlinks=False)
    if (_redirected(path, path_before) or
            not stat.S_ISREG(path_before.st_mode)):
        raise ValueError("canonical object is redirected or not regular")
    if expected_bytes is not None and path_before.st_size != expected_bytes:
        raise ValueError("canonical object size does not match job evidence")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags)
    handle = os.fdopen(fd, "rb")
    try:
        opened = os.fstat(handle.fileno())
        if not _same_file_identity(path_before, opened):
            raise ValueError("canonical object changed before open")
        digest = hashlib.sha256()
        while chunk := handle.read(BUFFER_BYTES):
            digest.update(chunk)
        opened_after = os.fstat(handle.fileno())
        if not _same_file_identity(opened, opened_after):
            raise ValueError("canonical object changed while hashing")
        if digest.hexdigest() != sha:
            raise ValueError("canonical object checksum mismatch")
        path_after = path.stat(follow_symlinks=False)
        if (_redirected(path, path_after) or
                not _same_file_identity(opened_after, path_after)):
            raise ValueError("canonical object path changed after hashing")
        return handle, opened_after
    except Exception:
        handle.close()
        raise


def _canonical_identity_matches(path: Path, verified: os.stat_result) -> bool:
    try:
        current = path.stat(follow_symlinks=False)
    except OSError:
        return False
    return (not _redirected(path, current) and
            _same_file_identity(verified, current))


def _open_identity_matches(handle: object, path: Path,
                           verified: os.stat_result) -> bool:
    try:
        opened = os.fstat(handle.fileno())
    except OSError:
        return False
    return (_same_file_identity(verified, opened) and
            _canonical_identity_matches(path, verified))


def _manifest_contains_occurrence(
        manifest: Path, job_id: str | None, pair: tuple[str, str],
        fallback: tuple[str, str, str]) -> bool:
    identities = _manifest_identities(manifest)
    if identities is None:
        raise ValueError("manifest cannot be read safely under append lock")
    job_identities, fallbacks, _legacy_pairs = identities
    if job_id is None:
        return bool(fallbacks[fallback])
    existing = job_identities.get(job_id)
    if existing is None:
        return False
    if existing == {pair}:
        return True
    raise ValueError(
        f"occurrence identity collision: {job_id}: "
        f"existing={sorted(existing)!r}, candidate={pair!r}")


def repair_archive(archive_root: Path, spool_root: Path) -> list[Finding]:
    topology = [
        *_spool_topology(spool_root, repair=True),
        *_repair_evidence_topology(spool_root),
        *_archive_topology(archive_root, repair=True),
    ]
    if topology:
        return _repair_refusals(topology)
    try:
        archive_root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return [Finding("repair_refused", "error", str(archive_root), str(exc))]
    manifest = archive_root / "manifest.jsonl"
    identities = _manifest_identities(manifest)
    if identities is None:
        return [Finding(
            "archive_repair_skipped", "error", str(manifest),
            "manifest cannot be read safely")]
    job_identities, fallbacks, legacy_pairs = identities
    collisions = {
        job_id: values for job_id, values in job_identities.items()
        if len(values) > 1}
    actions = [Finding(
        "occurrence_identity_collision", "error", str(manifest),
        f"{job_id}: {sorted(values)!r}")
        for job_id, values in sorted(collisions.items())]
    spool_collisions = _spool_identity_collisions(spool_root)
    actions.extend(Finding(
        "occurrence_identity_collision", "error", str(spool_root),
        f"{job_id}: {sorted(values)!r}")
        for job_id, values in sorted(spool_collisions.items()))
    for job_path in _spool_manifests(spool_root):
        value, read_error = _read_json(job_path)
        if read_error is not None:
            continue
        evidence = _archive_evidence(value)
        if evidence is None:
            continue
        sha, expected_bytes, created, session_id, job_id = evidence
        fonds = capture_fonds_path(session_id, created)
        try:
            validate_fonds_path(archive_root, fonds)
        except (OSError, ValueError):
            continue

        occurrence_at = created.isoformat()
        fallback = (sha, occurrence_at, fonds)
        pair = (sha, fonds)
        if job_id is not None:
            if job_id in spool_collisions:
                continue
            if job_id in collisions:
                continue
            existing_identities = job_identities.get(job_id)
            if existing_identities is not None:
                if existing_identities == {pair}:
                    continue
                actions.append(Finding(
                    "occurrence_identity_collision", "error", str(job_path),
                    f"{job_id}: existing={sorted(existing_identities)!r}, "
                    f"candidate={pair!r}"))
                continue
            if _consume_legacy_pair(pair, fallbacks, legacy_pairs):
                continue
        else:
            if fallbacks[fallback]:
                fallbacks[fallback] -= 1
                legacy_pairs[pair] -= 1
                continue
            if _consume_legacy_pair(pair, fallbacks, legacy_pairs):
                continue

        obj = (archive_root / "objects" / "sha256" /
               sha[:2] / sha[2:4] / sha)
        try:
            obj_handle, obj_stat = _verify_repair_object(
                obj, sha, expected_bytes)
        except (OSError, ValueError) as exc:
            actions.append(Finding(
                "archive_object_untrusted", "error", str(obj), str(exc)))
            continue

        occurrence = {
            "sha256": sha,
            "bytes": obj_stat.st_size,
            "mime": "application/x-jsonl",
            "first_seen": datetime.fromtimestamp(
                obj_stat.st_mtime, timezone.utc).isoformat(),
            "occurrence_at": occurrence_at,
            "fonds_path": fonds,
            "ingest_job_id": job_id,
        }
        try:
            if not _canonical_identity_matches(obj, obj_stat):
                actions.append(Finding(
                    "archive_object_untrusted", "error", str(obj),
                    "canonical object changed before manifest append"))
                continue
            appended = append_manifest_occurrence(
                archive_root, occurrence,
                pre_append=lambda: _open_identity_matches(
                    obj_handle, obj, obj_stat),
                post_append=lambda: _open_identity_matches(
                    obj_handle, obj, obj_stat),
                skip_if=lambda: _manifest_contains_occurrence(
                    manifest, job_id, pair, fallback),
                invalidate_occurrence_index=True)
        except (OSError, ValueError) as exc:
            actions.append(Finding(
                "archive_repair_failed", "error", str(manifest), str(exc)))
            continue
        finally:
            obj_handle.close()
        if not appended:
            continue
        if job_id is not None:
            job_identities[job_id] = {pair}
        actions.append(Finding(
            "manifest_occurrence_reconstructed", "info", str(obj),
            str(job_path)))
    return actions


def _spool_counts(spool_root: Path) -> dict[str, int]:
    if _spool_topology(spool_root, repair=False):
        return {"incoming": 0, "processing": 0, "dead_letter": 0}
    paths = _spool_paths(spool_root)
    root_manifests, _ = _json_files(spool_root)
    incoming, _ = _json_files(paths["incoming"])
    processing, _ = _json_files(paths["processing"])
    _, dead_count, _ = _dead_letter_evidence(paths["dead-letter"])
    return {
        "incoming": len(root_manifests) + len(incoming),
        "processing": len(processing),
        "dead_letter": dead_count,
    }


def main(repair: bool = False) -> int:
    """Run integrity checks; enable conservative filesystem repair explicitly."""
    from .core import CFG, pool

    actions: list[Finding] = []
    if repair:
        topology = [
            *_spool_topology(CFG.spool, repair=True),
            *_repair_evidence_topology(CFG.spool),
            *_archive_topology(CFG.archive, repair=True),
        ]
        if topology:
            actions = _repair_refusals(topology)
        else:
            actions = [
                *repair_spool(CFG.spool),
                *repair_archive(CFG.archive, CFG.spool),
            ]
        for action in actions:
            label = "REPAIRED" if action.severity == "info" else "REPAIR FAILED"
            print(f"{label} {action.code}: {action.path} ({action.detail})")

    counts = _spool_counts(CFG.spool)
    print("memoryd doctor: " + ", ".join(
        f"{key}={value}" for key, value in counts.items()))
    findings = [*inspect_spool(CFG.spool), *inspect_archive(CFG.archive)]
    try:
        with pool().connection() as conn:
            conn.execute("SELECT 1")
    except Exception as exc:  # noqa: BLE001 — health check reports the cause
        findings.append(Finding(
            "database_unreachable", "error", "database", str(exc)))
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{CFG.port}/health", timeout=2) as response:
            health = json.loads(response.read())
            if not isinstance(health, dict) or not health.get("ok"):
                raise RuntimeError("health response was not ok")
    except Exception as exc:  # noqa: BLE001 — health check reports the cause
        findings.append(Finding(
            "daemon_unreachable", "error", "daemon", str(exc)))
    for item in findings:
        print(f"{item.severity.upper()} {item.code}: "
              f"{item.path} ({item.detail})")
    if not findings:
        print("memoryd doctor: no integrity defects found")
    return int(any(
        item.severity == "error" for item in [*actions, *findings]))
