"""Raw-file storage: files live in a local folder, the database holds references.

Content-addressed: a file is stored at `{sha[:2]}/{sha}{suffix}` under the root, so the same
bytes are written once however many emails, uploads or tenants carry them. Tenant isolation is
in the database rows, which hold the relative path in `source_document.storage_path`.
"""

import hashlib
import os
import tempfile
from pathlib import Path, PurePosixPath


class RawStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def put(self, raw: bytes, suffix: str = "") -> str:
        """Store the bytes and return their path relative to the root (POSIX form).

        Writing is atomic (temp file then rename) and skipped when the file already exists.
        """
        sha = hashlib.sha256(raw).hexdigest()
        rel = PurePosixPath(sha[:2]) / f"{sha}{suffix.lower()}"
        dest = self.root / rel
        if not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=".tmp-")
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(raw)
                os.chmod(tmp, 0o644)  # mkstemp makes 0600; a worker may run as another user
                os.replace(tmp, dest)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
        return str(rel)

    def path(self, rel: str) -> Path:
        """The absolute path of a stored file. Rejects paths that escape the root."""
        full = (self.root / rel).resolve()
        if not full.is_relative_to(self.root.resolve()):
            raise ValueError(f"storage path escapes the raw store: {rel!r}")
        return full

    def read(self, rel: str) -> bytes:
        return self.path(rel).read_bytes()
