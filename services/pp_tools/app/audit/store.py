"""Parsed papers on disk, keyed by the PDF's SHA-256: parsing the same PDF twice costs nothing.

<cache_dir>/audit/<sha256>/
    docling.json      the DoclingDocument without embedded images
    paper.json        the normalised paper (app.audit.paper.Paper, without images)
    figures/<id>.png  one PNG per figure, named by its id ("figure:2" -> figure_2.png)
"""

import hashlib
import json
from pathlib import Path
from typing import Any

from .paper import Paper


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def figure_filename(figure_id: str) -> str:
    return figure_id.replace(":", "_").replace("/", "_") + ".png"


class PaperStore:
    def __init__(self, root: Path):
        self.root = root

    def folder(self, digest: str) -> Path:
        if len(digest) != 64 or not all(c in "0123456789abcdef" for c in digest):
            raise ValueError("bad digest")
        return self.root / digest

    def has(self, digest: str) -> bool:
        return (self.folder(digest) / "paper.json").exists()

    def save(self, digest: str, docling_doc: dict[str, Any], paper: Paper) -> None:
        folder = self.folder(digest)
        (folder / "figures").mkdir(parents=True, exist_ok=True)
        (folder / "docling.json").write_text(json.dumps(docling_doc), encoding="utf-8")
        for f in paper.figures:
            if f.image_png:
                (folder / "figures" / figure_filename(f.id)).write_bytes(f.image_png)
        # paper.json last: its presence means the folder is complete
        (folder / "paper.json").write_text(json.dumps(paper.as_dict(), ensure_ascii=False), encoding="utf-8")

    def paper(self, digest: str) -> dict[str, Any]:
        return json.loads((self.folder(digest) / "paper.json").read_text(encoding="utf-8"))

    def figure(self, digest: str, figure_id: str) -> bytes | None:
        path = self.folder(digest) / "figures" / figure_filename(figure_id)
        return path.read_bytes() if path.exists() else None
