#!/usr/bin/env python3
"""Milestone 3 spike: can Docling parse our papers on the team PC's CPU?

Sends a few cached library PDFs to a running docling-serve (CPU image), with OCR
off (arXiv PDFs are born-digital) and TableFormer in accurate mode, and records
for each paper: seconds, pages, tables found (with their captions and cell
grids), pictures found (saved as PNG), and the container's peak memory.

Standard library only, so it runs with the system Python on Windows.

    python scripts/parse_spike.py                      # the three default papers
    python scripts/parse_spike.py --pdf .cache/pp-tools/pdf/2310.11511v1.pdf

Results: eval/m3_spike/<timestamp>/ (summary.md, tables.md, <paper>.json, pictures/).
The JSON files are DoclingDocument exports; they are not committed (.gitignore)
because they hold the papers' text.
"""

import argparse
import base64
import json
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PDF_DIR = REPO / ".cache" / "pp-tools" / "pdf"
DEFAULT = ["2401.15884v3.pdf", "2403.14403v2.pdf", "2310.11511v1.pdf"]  # CRAG, Adaptive-RAG, Self-RAG
OPTIONS = {
    "to_formats": "json",
    "do_ocr": "false",
    "do_table_structure": "true",
    "table_mode": "accurate",
    "image_export_mode": "embedded",
    "images_scale": "2.0",
    "include_images": "true",
}


def multipart(fields: dict[str, str], file_path: Path) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    parts = []
    for k, v in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="files"; filename="{file_path.name}"\r\n'
        f"Content-Type: application/pdf\r\n\r\n".encode() + file_path.read_bytes() + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def call(method: str, url: str, body: bytes | None = None, ctype: str | None = None, timeout: float = 60) -> dict:
    req = urllib.request.Request(url, data=body, method=method, headers={"Content-Type": ctype} if ctype else {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


class MemorySampler(threading.Thread):
    """Peak memory of the docling container, from `docker stats` every 2 s."""

    def __init__(self, container: str):
        super().__init__(daemon=True)
        self.container, self.peak_mib, self.stop = container, 0.0, threading.Event()

    def run(self) -> None:
        while not self.stop.is_set():
            try:
                out = subprocess.run(["docker", "stats", "--no-stream", "--format", "{{.MemUsage}}", self.container],
                                     capture_output=True, text=True, timeout=20).stdout.split("/")[0].strip()
                value = float("".join(c for c in out if c.isdigit() or c == "."))
                mib = value * 1024 if "GiB" in out else value / 1024 if "KiB" in out else value
                self.peak_mib = max(self.peak_mib, mib)
            except Exception:  # noqa: BLE001 - sampling is best effort
                pass
            self.stop.wait(2)


def convert(base: str, pdf: Path, timeout_s: float) -> tuple[dict, float]:
    body, ctype = multipart(OPTIONS, pdf)
    started = time.monotonic()
    task = call("POST", f"{base}/v1/convert/file/async", body, ctype)
    task_id = task["task_id"]
    while True:
        status = call("GET", f"{base}/v1/status/poll/{task_id}")
        if status.get("task_status") in ("success", "failure"):
            break
        if time.monotonic() - started > timeout_s:
            raise TimeoutError(f"{pdf.name}: still {status.get('task_status')} after {timeout_s:.0f} s")
        time.sleep(3)
    result = call("GET", f"{base}/v1/result/{task_id}", timeout=300)
    return result, time.monotonic() - started


def resolve(doc: dict, ref: dict) -> dict:
    """Follow a {"$ref": "#/texts/12"} pointer inside the DoclingDocument."""
    node: object = doc
    for part in ref["$ref"].lstrip("#/").split("/"):
        node = node[int(part)] if isinstance(node, list) else node[part]
    return node  # type: ignore[return-value]


def caption(doc: dict, item: dict) -> str:
    return " ".join(resolve(doc, c).get("text", "") for c in item.get("captions", [])).strip()


def grid(table: dict) -> list[list[str]]:
    data = table.get("data", {})
    rows, cols = data.get("num_rows", 0), data.get("num_cols", 0)
    g = [["" for _ in range(cols)] for _ in range(rows)]
    for cell in data.get("table_cells", []):
        for r in range(cell["start_row_offset_idx"], cell["end_row_offset_idx"]):
            for c in range(cell["start_col_offset_idx"], cell["end_col_offset_idx"]):
                if r < rows and c < cols:
                    g[r][c] = cell.get("text", "")
    return g


def as_markdown(g: list[list[str]], max_rows: int = 12) -> str:
    if not g:
        return "(empty)"
    esc = [[c.replace("|", "\\|").replace("\n", " ") for c in row] for row in g]
    lines = ["| " + " | ".join(esc[0]) + " |", "|" + " --- |" * len(esc[0])]
    lines += ["| " + " | ".join(r) + " |" for r in esc[1:max_rows]]
    if len(esc) > max_rows:
        lines.append(f"| ... {len(esc) - max_rows} more rows |")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default="http://127.0.0.1:5001")
    ap.add_argument("--container", default="docling-spike")
    ap.add_argument("--pdf", action="append", type=Path)
    ap.add_argument("--timeout", type=float, default=1800)
    args = ap.parse_args()
    pdfs = args.pdf or [PDF_DIR / n for n in DEFAULT]
    out = REPO / "eval" / "m3_spike" / datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    (out / "pictures").mkdir(parents=True)

    sampler = MemorySampler(args.container)
    sampler.start()
    summary = ["| Paper | Pages | Seconds | Seconds/page | Status | Tables | Pictures | Captions found |", "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    tables_md = []
    for pdf in pdfs:
        print(f"{pdf.name}: converting...", flush=True)
        try:
            result, secs = convert(args.url, pdf, args.timeout)
        except (urllib.error.URLError, TimeoutError, KeyError) as exc:
            print(f"  failed: {exc}")
            summary.append(f"| {pdf.stem} | | | | error: {str(exc)[:80]} | | | |")
            continue
        doc = (result.get("document") or {}).get("json_content") or {}
        (out / f"{pdf.stem}.json").write_text(json.dumps(doc), encoding="utf-8")
        pages = len(doc.get("pages", {}))
        tables, pictures = doc.get("tables", []), doc.get("pictures", [])
        captioned = sum(1 for t in tables + pictures if t.get("captions"))
        summary.append(f"| {pdf.stem} | {pages} | {secs:.0f} | {secs / max(pages, 1):.1f} | {result.get('status')} "
                       f"| {len(tables)} | {len(pictures)} | {captioned}/{len(tables) + len(pictures)} |")
        print(f"  {pages} pages, {secs:.0f} s, {len(tables)} tables, {len(pictures)} pictures", flush=True)
        tables_md.append(f"## {pdf.stem}\n")
        for i, t in enumerate(tables, 1):
            page = (t.get("prov") or [{}])[0].get("page_no")
            g = grid(t)
            tables_md.append(f"### Table {i} (page {page}, {len(g)}×{len(g[0]) if g else 0})\n\n**Caption:** {caption(doc, t) or '(none)'}\n\n{as_markdown(g)}\n")
        for i, p in enumerate(pictures, 1):
            uri = ((p.get("image") or {}).get("uri") or "")
            if uri.startswith("data:image/png;base64,"):
                (out / "pictures" / f"{pdf.stem}-{i:02d}.png").write_bytes(base64.b64decode(uri.split(",", 1)[1]))
    sampler.stop.set()
    sampler.join(timeout=5)
    text = (f"# Docling CPU spike\n\nOptions: `{json.dumps(OPTIONS)}`\n\nPeak container memory: {sampler.peak_mib:.0f} MiB "
            f"(sampled every 2 s; 0 means docker stats was unavailable)\n\n" + "\n".join(summary) + "\n")
    (out / "summary.md").write_text(text, encoding="utf-8")
    (out / "tables.md").write_text("\n".join(tables_md), encoding="utf-8")
    print("\n" + text + f"\nWrote {out.relative_to(REPO)} (summary.md, tables.md, pictures/)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
