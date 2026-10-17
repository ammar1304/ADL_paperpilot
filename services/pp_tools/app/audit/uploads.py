"""PDFs uploaded through the form, kept on disk and named by their SHA-256.

<cache_dir>/uploads/<sha256>.pdf

The form cannot give pp-tools a URL to download (the file only exists in the
browser), so n8n posts the bytes to POST /uploads first and the run refers to
the upload by its id. Naming by content hash makes a second upload of the same
file free, and the id is not guessable from anything but the file itself.
"""

import os
import re
import tempfile
from pathlib import Path

from .store import sha256

UPLOAD_ID = re.compile(r"^[0-9a-f]{64}$")
PDF_MAGIC = b"%PDF-"


class UploadError(ValueError):
    """The bytes are not an acceptable PDF."""


class Uploads:
    def __init__(self, root: Path, max_bytes: int):
        self.root = root
        self.max_bytes = max_bytes

    def path(self, upload_id: str) -> Path:
        if not UPLOAD_ID.match(upload_id or ""):
            raise LookupError("bad upload id")
        return self.root / f"{upload_id}.pdf"

    def exists(self, upload_id: str) -> bool:
        try:
            return self.path(upload_id).exists()
        except LookupError:
            return False

    def save(self, data: bytes) -> str:
        if not data:
            raise UploadError("empty upload")
        if len(data) > self.max_bytes:
            raise UploadError(f"the PDF is larger than {self.max_bytes // 1_000_000} MB")
        # The PDF header may follow a few bytes of junk; readers accept it within the first 1 KB.
        if PDF_MAGIC not in data[:1024]:
            raise UploadError("not a PDF file")
        upload_id = sha256(data)
        target = self.path(upload_id)
        if not target.exists():
            self.root.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.root, suffix=".part")
            try:
                with os.fdopen(fd, "wb") as fh:
                    fh.write(data)
                os.replace(tmp, target)  # atomic: a reader never sees half a file
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
        return upload_id

    def read(self, upload_id: str) -> bytes:
        path = self.path(upload_id)
        if not path.exists():
            raise LookupError(f"no uploaded PDF with id {upload_id[:12]}...")
        return path.read_bytes()
