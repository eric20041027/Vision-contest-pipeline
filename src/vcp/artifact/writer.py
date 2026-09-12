"""``ArtifactWriter`` (spec 7): claims an id by ``os.mkdir``, writes every file through
``write_once``, and commits by writing ``manifest.json`` last. Nothing before that line is an
artifact; nothing after it can be changed. The writer never deletes anything."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from vcp.artifact import store
from vcp.artifact.schema import (
    ArtifactManifest,
    ArtifactSpec,
    FailureRecord,
    FileEntry,
    SpecRecord,
    check_file_name,
)
from vcp.core.atomic import write_once, write_once_stream
from vcp.core.build import build_string
from vcp.core.errors import IntegrityError, ValidationFailed
from vcp.core.hashing import sha256_file
from vcp.core.paths import artifact_dir, resolve_stored_path
from vcp.core.proc import redact
from vcp.core.time import stamp

_CHUNK = 1 << 20


def _json_bytes(obj: Any) -> bytes:
    return (json.dumps(obj, ensure_ascii=False, indent=1) + "\n").encode("utf-8")


def _chunks(path: Path) -> Iterator[bytes]:
    with path.open("rb") as f:
        while chunk := f.read(_CHUNK):
            yield chunk


class ArtifactWriter:
    """``with ArtifactWriter.create(spec, data_root=root) as art:`` ... ``art.commit()``.

    Leaving the block without ``commit()`` keeps the directory as a partial (plus
    ``failure.json`` when an exception did it); the id stays claimed until ``vcp artifact clean``.
    """

    def __init__(self, spec: ArtifactSpec, data_root: Path, directory: Path) -> None:
        self.spec = spec
        self.data_root = data_root
        self.dir = directory
        self.manifest: ArtifactManifest | None = None
        self._files: dict[str, FileEntry] = {}
        self._reserved: list[str] = []
        self._closed = False

    @classmethod
    def create(cls, spec: ArtifactSpec, *, data_root: Path) -> ArtifactWriter:
        """Open: resolve the inputs (their sha is fixed now), require a supersedes target that
        exists and is committed, claim the directory, write ``spec.json``."""
        data_root = Path(data_root)
        resolved = store.resolve_inputs(spec, data_root)
        if resolved.supersedes is not None:
            store.load_manifest(data_root, resolved.kind, resolved.supersedes)
        directory = artifact_dir(data_root, resolved.kind, resolved.id)
        directory.parent.mkdir(parents=True, exist_ok=True)
        try:
            directory.mkdir()
        except FileExistsError:
            raise ValidationFailed(
                f"exists: artifact {resolved.kind}/{resolved.id} already exists (complete or "
                "partial); pick a new id, or supersede it",
                fields={"kind": resolved.kind, "id": resolved.id},
            ) from None
        record = SpecRecord(spec=resolved, opened_at=stamp(), vcp_version=build_string())
        write_once(directory / store.SPEC, _json_bytes(record.model_dump(mode="json")))
        return cls(resolved, data_root, directory)

    # -- context manager ----------------------------------------------------------------------

    def __enter__(self) -> ArtifactWriter:
        return self

    def __exit__(self, exc_type: object, exc: BaseException | None, tb: object) -> None:
        if exc is not None and self.manifest is None:
            self._record_failure(exc)
        self._closed = True

    def _record_failure(self, exc: BaseException) -> None:
        record = FailureRecord(ts=stamp(), exception=type(exc).__name__, message=redact(str(exc)))
        try:
            write_once(self.dir / store.FAILURE, _json_bytes(record.model_dump(mode="json")))
        except Exception:
            # The job's own exception is the one to surface; failing to record it must not hide it.
            pass

    # -- files --------------------------------------------------------------------------------

    def _ident(self) -> dict[str, str]:
        return {"kind": self.spec.kind, "id": self.spec.id}

    def _claim(self, name: str) -> Path:
        if self._closed:
            raise ValidationFailed(
                f"closed: artifact {self.spec.kind}/{self.spec.id} is committed or closed; "
                "open a new id",
                fields=self._ident(),
            )
        try:
            check_file_name(name)
        except ValueError as e:
            raise ValidationFailed(str(e), fields={**self._ident(), "file": name}) from e
        if name in self._files or name in self._reserved:
            raise ValidationFailed(
                f"exists: {name!r} is already written or reserved in artifact "
                f"{self.spec.kind}/{self.spec.id}",
                fields={**self._ident(), "file": name},
            )
        target = self.dir / name
        target.parent.mkdir(parents=True, exist_ok=True)
        return target

    def _record(self, name: str, size: int, digest: str) -> FileEntry:
        entry = FileEntry(name=name, bytes=size, sha256=digest)
        self._files[name] = entry
        return entry

    def write_bytes(self, name: str, data: bytes) -> FileEntry:
        target = self._claim(name)
        return self._record(name, len(data), write_once(target, data))

    def write_text(self, name: str, text: str) -> FileEntry:
        return self.write_bytes(name, text.encode("utf-8"))

    def write_json(self, name: str, obj: Any) -> FileEntry:
        return self.write_bytes(name, _json_bytes(obj))

    def add_file(self, name: str, src: Path) -> FileEntry:
        """Copy ``src`` in, hashing as it streams (a 20 GB file is read once)."""
        src = Path(src)
        if not src.is_file():
            raise ValidationFailed(
                f"not_found: {src} (add_file source)", fields={**self._ident(), "file": name}
            )
        target = self._claim(name)
        size, digest = write_once_stream(target, _chunks(src))
        return self._record(name, size, digest)

    def reserve(self, name: str) -> Path:
        """Register a name and return its final path for the caller to write directly (numpy,
        memmap). Hashed at commit; ``not_found:`` then if it was never written."""
        target = self._claim(name)
        self._reserved.append(name)
        return target

    # -- commit -------------------------------------------------------------------------------

    def _check_reserved(self) -> None:
        for name in self._reserved:
            path = self.dir / name
            if not path.is_file():
                raise ValidationFailed(
                    f"not_found: reserved file {name!r} was never written",
                    fields={**self._ident(), "file": name},
                )
            self._record(name, path.stat().st_size, sha256_file(path))

    def _check_drift(self) -> None:
        """Re-hash every input that has a path; a sha that moved since open is ``drift:``."""
        for ref in self.spec.inputs:
            if ref.path is None or ref.sha256 is None:
                continue
            path = resolve_stored_path(ref.path, self.data_root)
            now = sha256_file(path) if path.is_file() else "gone"
            if now != ref.sha256:
                raise IntegrityError(
                    f"drift: input {ref.name!r} changed during the job "
                    f"({ref.sha256[:12]} -> {now[:12]})",
                    location=str(path),
                    fields={**self._ident(), "input": ref.name},
                )

    def commit(self) -> ArtifactManifest:
        """Reserved files present → inputs unchanged → ``manifest.json`` (the commit point).
        Afterwards the writer is closed."""
        if self._closed:
            raise ValidationFailed(
                f"closed: artifact {self.spec.kind}/{self.spec.id} is committed or closed",
                fields=self._ident(),
            )
        self._check_reserved()
        self._check_drift()
        manifest = ArtifactManifest(
            spec=self.spec,
            files=[self._files[n] for n in sorted(self._files)],
            created_at=stamp(),
            vcp_version=build_string(),
        )
        write_once(self.dir / store.MANIFEST, _json_bytes(manifest.model_dump(mode="json")))
        self.manifest = manifest
        self._closed = True
        return manifest
