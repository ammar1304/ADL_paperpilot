"""Client for docling-serve (the `parser` service, D31), and the PDF download for audits."""

import ipaddress
import logging
import socket
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

import httpx

log = logging.getLogger("pp_tools.parser")

# What the audit needs from Docling (D31): no OCR (arXiv PDFs are born-digital),
# TableFormer in accurate mode for cell grids, pictures embedded as PNG at 2x.
CONVERT_OPTIONS: dict[str, str] = {
    "to_formats": "json",
    "do_ocr": "false",
    "do_table_structure": "true",
    "table_mode": "accurate",
    "image_export_mode": "embedded",
    "images_scale": "2.0",
    "include_images": "true",
}


class ParserError(Exception):
    pass


class FetchError(Exception):
    pass


class DoclingClient:
    def __init__(self, base_url: str, timeout_s: float = 1800.0, poll_s: float = 3.0, request_timeout_s: float = 60.0):
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.poll_s = poll_s
        self.http = httpx.Client(timeout=request_timeout_s)

    def close(self) -> None:
        self.http.close()

    def healthy(self) -> bool:
        try:
            return self.http.get(f"{self.base_url}/health", timeout=3.0).status_code == 200
        except httpx.HTTPError:
            return False

    def convert(self, pdf: bytes, filename: str, progress: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
        """The DoclingDocument (json_content) for one PDF, through the asynchronous API."""
        started = time.monotonic()
        try:
            resp = self.http.post(
                f"{self.base_url}/v1/convert/file/async",
                files={"files": (filename, pdf, "application/pdf")},
                data=CONVERT_OPTIONS,
            )
            resp.raise_for_status()
            task_id = resp.json()["task_id"]
            while True:
                status = self.http.get(f"{self.base_url}/v1/status/poll/{task_id}").json()
                state = status.get("task_status")
                if progress:
                    progress({"stage": "parsing", "parser_status": state, "seconds": round(time.monotonic() - started)})
                if state in ("success", "failure"):
                    break
                if time.monotonic() - started > self.timeout_s:
                    raise ParserError(f"parser still '{state}' after {self.timeout_s:.0f} s")
                time.sleep(self.poll_s)
            result = self.http.get(f"{self.base_url}/v1/result/{task_id}", timeout=300).json()
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            raise ParserError(f"docling-serve: {type(exc).__name__}: {str(exc)[:300]}") from exc
        doc = (result.get("document") or {}).get("json_content")
        if result.get("status") not in ("success", "partial_success") or not doc:
            raise ParserError(f"conversion {result.get('status')}: {str(result.get('errors'))[:300]}")
        return doc


def _public_host(host: str) -> bool:
    """False if the host resolves to a private, loopback or link-local address (SSRF guard)."""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise FetchError(f"cannot resolve {host}") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return False
    return True


def fetch_pdf(url: str, max_bytes: int, allow_private: bool = False, timeout_s: float = 60.0) -> bytes:
    """Download a PDF from a public http(s) URL, at most ``max_bytes``.

    The URL comes from the user, so pp-tools refuses addresses inside the Docker
    network or the host (postgres, qdrant...) unless ``allow_private`` is set
    (tests). Redirects are followed by hand so every hop is checked.
    """
    for _ in range(5):
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise FetchError("only http(s) URLs are accepted")
        if not allow_private and not _public_host(parsed.hostname):
            raise FetchError(f"{parsed.hostname} is not a public address")
        with httpx.Client(timeout=timeout_s, follow_redirects=False,
                          headers={"User-Agent": "PaperPilot/0.1 (student research project)"}) as client:
            with client.stream("GET", url) as resp:
                if resp.is_redirect:
                    url = str(resp.next_request.url) if resp.next_request else resp.headers.get("location", "")
                    continue
                if resp.status_code != 200:
                    raise FetchError(f"HTTP {resp.status_code} for {url}")
                chunks, size = [], 0
                for chunk in resp.iter_bytes():
                    size += len(chunk)
                    if size > max_bytes:
                        raise FetchError(f"the PDF is larger than {max_bytes // 1_000_000} MB")
                    chunks.append(chunk)
        data = b"".join(chunks)
        if not data.startswith(b"%PDF"):
            raise FetchError("the URL did not return a PDF")
        return data
    raise FetchError("too many redirects")
