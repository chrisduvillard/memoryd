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
from .spool import (
    BUFFER_BYTES,
    PermanentSpoolError,
    dead_letter,
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


def _spool_paths(spool_root: Path) -> dict[str, Path]:
    return {name: spool_root / name for name in
            ("blobs", *SPOOL_STATES)}


def _spool_manifests(spool_root: Path) -> list[Path]:
    paths = _spool_paths(spool_root)
    manifests = sorted(spool_root.glob("*.json"))
    for state in SPOOL_STATES:
        for manifest in sorted(paths[state].glob("*.json")):
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


def inspect_spool(spool_root: Path) -> list[Finding]:
    """Inspect spool evidence without creating directories or lock files."""
    paths = _spool_paths(spool_root)
    findings: list[Finding] = []
    manifests = _spool_manifests(spool_root)
    for manifest in manifests:
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

    dead_count = sum(
        manifest.parent == paths["dead-letter"] for manifest in manifests)
    if dead_count:
        findings.append(Finding(
            "dead_letter_jobs", "error", str(paths["dead-letter"]),
            str(dead_count)))
    return findings


def _valid_sha(value: object) -> bool:
    return (isinstance(value, str) and len(value) == 64 and
            all(char in "0123456789abcdef" for char in value))


def _archive_objects(
        archive_root: Path) -> tuple[dict[str, Path], list[Finding]]:
    object_root = archive_root / "objects" / "sha256"
    objects: dict[str, Path] = {}
    findings: list[Finding] = []
    try:
        candidates = object_root.rglob("*")
        for path in candidates:
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
    findings: list[Finding] = []
    objects, object_findings = _archive_objects(archive_root)
    findings.extend(object_findings)
    mentioned: set[str] = set()
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
    actions: list[Finding] = []
    ensure_layout(spool_root)
    try:
        requeued = requeue_stale(spool_root)
    except OSError as exc:
        actions.append(Finding(
            "stale_requeue_failed", "error", str(spool_root), str(exc)))
    else:
        if requeued:
            actions.append(Finding(
                "stale_jobs_requeued", "info", str(spool_root),
                str(requeued)))

    paths = _spool_paths(spool_root)
    manifests = [*sorted(spool_root.glob("*.json")),
                 *sorted(paths["incoming"].glob("*.json"))]
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
            validate_legacy_job(value)
        except PermanentSpoolError as exc:
            code = ("corrupt_job_dead_lettered"
                    if isinstance(value, dict) and
                    value.get("schema_version") == 2
                    else "invalid_job_dead_lettered")
            actions.append(_dead_letter_invalid(
                spool_root, manifest, str(exc), code))
            continue

        try:
            upgraded = upgrade_legacy_job(spool_root, manifest)
        except (OSError, PermanentSpoolError, ValueError) as exc:
            actions.append(_dead_letter_invalid(
                spool_root, manifest, str(exc),
                "invalid_job_dead_lettered"))
            continue
        actions.append(Finding(
            "legacy_upgraded" if upgraded else "legacy_dead_lettered",
            "info", str(manifest),
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
        set[str],
        Counter[tuple[str, str, str]],
        Counter[tuple[str, str]],
] | None:
    job_ids: set[str] = set()
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
                    job_ids.add(job_id)
                else:
                    legacy_pairs[(sha, fonds)] += 1
                    if isinstance(occurrence_at, str):
                        fallbacks[(sha, occurrence_at, fonds)] += 1
    except FileNotFoundError:
        pass
    except (OSError, UnicodeError):
        return None
    return job_ids, fallbacks, legacy_pairs


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


def repair_archive(archive_root: Path, spool_root: Path) -> list[Finding]:
    archive_root.mkdir(parents=True, exist_ok=True)
    ensure_layout(spool_root)
    manifest = archive_root / "manifest.jsonl"
    identities = _manifest_identities(manifest)
    if identities is None:
        return [Finding(
            "archive_repair_skipped", "error", str(manifest),
            "manifest cannot be read safely")]
    job_ids, fallbacks, legacy_pairs = identities
    actions: list[Finding] = []
    for job_path in _spool_manifests(spool_root):
        value, read_error = _read_json(job_path)
        if read_error is not None:
            continue
        evidence = _archive_evidence(value)
        if evidence is None:
            continue
        sha, expected_bytes, created, session_id, job_id = evidence
        fonds = f"claude-code/{created:%Y/%m/%d}/{session_id}.jsonl"
        try:
            validate_fonds_path(archive_root, fonds)
        except (OSError, ValueError):
            continue

        occurrence_at = created.isoformat()
        fallback = (sha, occurrence_at, fonds)
        pair = (sha, fonds)
        if job_id is not None:
            if job_id in job_ids:
                continue
            if _consume_legacy_pair(pair, fallbacks, legacy_pairs):
                continue
        else:
            if fallbacks[fallback]:
                continue
            if _consume_legacy_pair(pair, fallbacks, legacy_pairs):
                continue

        obj = (archive_root / "objects" / "sha256" /
               sha[:2] / sha[2:4] / sha)
        try:
            obj_stat = obj.stat(follow_symlinks=False)
            if not stat.S_ISREG(obj_stat.st_mode):
                continue
            if expected_bytes is not None and obj_stat.st_size != expected_bytes:
                continue
            if _sha256_file(obj) != sha:
                continue
        except OSError:
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
            append_manifest_occurrence(archive_root, occurrence)
        except OSError as exc:
            actions.append(Finding(
                "archive_repair_failed", "error", str(manifest), str(exc)))
            continue
        if job_id is not None:
            job_ids.add(job_id)
        else:
            legacy_pairs[pair] += 1
            fallbacks[fallback] += 1
        actions.append(Finding(
            "manifest_occurrence_reconstructed", "info", str(obj),
            str(job_path)))
    return actions


def _spool_counts(spool_root: Path) -> dict[str, int]:
    paths = _spool_paths(spool_root)
    manifests = _spool_manifests(spool_root)
    return {
        "incoming": sum(
            path.parent in (spool_root, paths["incoming"])
            for path in manifests),
        "processing": sum(
            path.parent == paths["processing"] for path in manifests),
        "dead_letter": sum(
            path.parent == paths["dead-letter"] for path in manifests),
    }


def main(repair: bool = False) -> int:
    """Run integrity checks; enable conservative filesystem repair explicitly."""
    from .core import CFG, pool

    actions: list[Finding] = []
    if repair:
        CFG.ensure_dirs()
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
