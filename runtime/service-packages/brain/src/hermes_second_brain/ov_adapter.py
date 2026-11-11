from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class OvResult:
    resource_id: str
    stdout: str


class OpenVikingAdapter:
    """OpenViking 0.4.10 CLI adapter with exact-resource operations only."""

    def __init__(self, binary: str = "ov", timeout_seconds: float = 120.0, attempts: int = 3, backoff_seconds: float = 0.25):
        self.binary = binary
        self.timeout_seconds = timeout_seconds
        self.attempts = attempts
        self.backoff_seconds = backoff_seconds

    def build_target_uri(self, *, source_id: str, namespace: str, relative_path: str) -> str:
        return build_target_uri(source_id=source_id, namespace=namespace, relative_path=relative_path)

    def sync_resource(
        self,
        *,
        path: Path,
        source_id: str,
        namespace: str,
        sha256: str,
        relative_path: str,
        existing_uri: str | None,
        resume_locked: bool = False,
    ) -> OvResult:
        target_uri = existing_uri or self.build_target_uri(source_id=source_id, namespace=namespace, relative_path=relative_path)
        if not is_safe_resource_uri(target_uri):
            raise ValueError(f"unsafe OpenViking resource URI: {target_uri}")

        suffix = Path(relative_path).suffix.lower()
        metadata = self.resource_stat(target_uri)
        exists = metadata is not None
        if exists and resume_locked and metadata.get("isLocked") is True:
            # The upload is already accepted by OpenViking and still indexing.
            # A second write/rm conflicts; callers opting out of global wait
            # deliberately record acceptance and let background indexing finish.
            return OvResult(resource_id=target_uri, stdout="")
        if exists and _metadata_sha_matches(metadata, sha256):
            # Resume an interrupted add without triggering duplicate VLM work.
            # OpenViking 0.4.10 can create the resource and then return a
            # transient "Resource is busy" conflict while semantics finish.
            self.set_tags_best_effort(target_uri=target_uri, source_id=source_id, namespace=namespace, sha256=sha256)
            return OvResult(resource_id=target_uri, stdout="")
        if suffix in {".md", ".txt"}:
            if exists:
                if metadata.get("isDir") is True:
                    self.remove_resource(
                        target_uri,
                        recursive=True,
                        wait=not resume_locked,
                    )
                    stdout = self._add_or_resume(path=path, target_uri=target_uri)
                else:
                    stdout = self.write_resource(path=path, target_uri=target_uri)
            else:
                stdout = self._add_or_resume(path=path, target_uri=target_uri)
        elif suffix in {".pdf", ".docx"}:
            if exists:
                self.remove_resource(target_uri, recursive=metadata.get("isDir") is True)
            stdout = self._add_or_resume(path=path, target_uri=target_uri)
        else:
            raise ValueError(f"unsupported OpenViking resource suffix: {suffix}")

        self.set_tags_best_effort(target_uri=target_uri, source_id=source_id, namespace=namespace, sha256=sha256)
        return OvResult(resource_id=_extract_resource_id(stdout) or target_uri, stdout=stdout)

    def add_resource(self, *, path: Path, target_uri: str) -> str:
        mode = "vectors_only" if target_uri.startswith("viking://resources/ole_unconfirmed/") else "semantic_and_vectors"
        return self._run([self.binary, "add-resource", str(path), "--to", target_uri, "--processing-mode", mode, "--no-progress", "-o", "json"])

    def _add_or_resume(self, *, path: Path, target_uri: str) -> str:
        try:
            return self.add_resource(path=path, target_uri=target_uri)
        except RuntimeError as exc:
            if not _is_existing_resource_conflict(str(exc)) or not self.resource_exists(target_uri):
                raise
            return ""

    def write_resource(self, *, path: Path, target_uri: str) -> str:
        return self._run([self.binary, "write", target_uri, "--from-file", str(path), "-o", "json"])

    def resource_exists(self, target_uri: str) -> bool:
        return self.resource_stat(target_uri) is not None

    def resource_stat(self, target_uri: str) -> dict[str, object] | None:
        proc = subprocess.run([self.binary, "stat", target_uri, "-o", "json"], shell=False, text=True, capture_output=True, timeout=self.timeout_seconds, check=False)
        if proc.returncode != 0:
            return None
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return {}
        if not isinstance(payload, dict):
            return {}
        result = payload.get("result")
        return result if isinstance(result, dict) else payload

    def remove_resource(
        self,
        target_uri: str,
        recursive: bool = False,
        *,
        wait: bool = True,
    ) -> str:
        if not is_safe_resource_uri(target_uri):
            raise ValueError(f"unsafe OpenViking resource URI: {target_uri}")
        cmd = [self.binary, "rm", target_uri]
        if recursive:
            cmd.append("--recursive")
        if wait:
            cmd.extend(["--wait", "--timeout", str(int(self.timeout_seconds))])
        cmd.extend(["-o", "json"])
        return self._run(cmd)

    def wait(self) -> str:
        return self._run([self.binary, "wait", "--timeout", str(int(self.timeout_seconds)), "-o", "json"])

    def set_tags(self, *, target_uri: str, source_id: str, namespace: str, sha256: str) -> str:
        tags = ",".join((f"source_id={_safe_tag_value(source_id)}", f"sha256={_safe_tag_value(sha256)}", f"namespace={_safe_tag_value(safe_namespace(namespace))}"))
        return self._run([self.binary, "set-tags", target_uri, "--tags", tags, "--mode", "replace", "-o", "json"])

    def set_tags_best_effort(self, *, target_uri: str, source_id: str, namespace: str, sha256: str) -> str:
        try:
            return self.set_tags(target_uri=target_uri, source_id=source_id, namespace=namespace, sha256=sha256)
        except RuntimeError as exc:
            if "Resource is busy" not in str(exc):
                raise
            return ""

    def _run(self, cmd: list[str]) -> str:
        last_error = ""
        for attempt in range(1, self.attempts + 1):
            try:
                proc = subprocess.run(cmd, shell=False, text=True, capture_output=True, timeout=self.timeout_seconds, check=False)
            except subprocess.TimeoutExpired:
                last_error = f"OpenViking timed out after {self.timeout_seconds}s"
            else:
                if proc.returncode == 0:
                    return proc.stdout
                last_error = (proc.stderr or proc.stdout or f"OpenViking exited {proc.returncode}").strip()
                if "Resource is busy" in last_error:
                    break
            if attempt < self.attempts:
                time.sleep(self.backoff_seconds * attempt)
        raise RuntimeError(last_error)


def build_target_uri(*, source_id: str, namespace: str, relative_path: str) -> str:
    safe_ns = safe_namespace(namespace)
    safe_id = _safe_uri_segment(source_id)
    suffix = Path(relative_path).suffix.lower()
    if suffix not in {".md", ".txt", ".pdf", ".docx"}:
        raise ValueError(f"unsupported resource suffix: {suffix}")
    return f"viking://resources/{safe_ns}/{safe_id}{suffix}"


def safe_namespace(namespace: str) -> str:
    return _safe_uri_segment(namespace)


def is_safe_resource_uri(uri: str) -> bool:
    return (re.fullmatch(r"viking://resources/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+\.(md|txt|pdf|docx)", uri) is not None or re.fullmatch(r"viking://resources/ole_unconfirmed/context/[\w.()-]{1,180}",uri) is not None) and ".." not in uri


def _safe_uri_segment(value: str) -> str:
    segment = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._-")
    segment = re.sub(r"\.{2,}", "_", segment)
    if not segment or segment in {".", ".."} or ".." in segment:
        raise ValueError(f"unsafe URI segment from value: {value!r}")
    return segment


def _safe_tag_value(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._:-]+", "_", value)


def _is_existing_resource_conflict(error: str) -> bool:
    return "Resource is busy" in error or "already exists" in error


def _extract_resource_id(stdout: str) -> str | None:
    text = stdout.strip()
    if not text:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return text.splitlines()[-1].strip() or None
    if isinstance(data, dict):
        value = data.get("resource_id") or data.get("id") or data.get("uri") or data.get("path")
        return str(value) if value else None
    return None


def _metadata_sha_matches(metadata: dict[str, object] | None, sha256: str) -> bool:
    if not metadata:
        return False
    tags = metadata.get("tags")
    if isinstance(tags, dict):
        return str(tags.get("sha256") or "") == sha256
    if isinstance(tags, list):
        for item in tags:
            if isinstance(item, str) and item == f"sha256={sha256}":
                return True
            if isinstance(item, dict) and str(item.get("key") or "") == "sha256" and str(item.get("value") or "") == sha256:
                return True
    return False
