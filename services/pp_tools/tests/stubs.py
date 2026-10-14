"""Offline stand-ins for arXiv and the embedding server, for tests and sandbox runs.

    python tests/stubs.py --port 8765     # serves both on one port

- /api/query       answers search_query and id_list like the arXiv Atom API
- /pdf/<id>v<n>    returns a generated two-column paper PDF
- /embed, /health  answer like Text Embeddings Inference, with hashed
                   bag-of-words vectors (texts sharing words are similar)
- /openai/v1/chat/completions
                   answers like Groq's OpenAI-compatible API with canned,
                   rule-based JSON for the decomposer and the novelty judge.
                   Words at the start of the idea steer it, to exercise error paths
                   (the stub decomposer keeps only the first 12 words):
                     HALLUCINATE  the judge adds a paper that was not retrieved
                                  (and drops it when given feedback)
                     STUBBORN     the judge keeps hallucinating after feedback
                     FAIL_LLM     the Groq endpoint returns HTTP 500 (the
                                  Gemini endpoint still answers: fallback)
                     FAIL_ALL     both endpoints return HTTP 500
- /v1beta/models/<model>:generateContent
                   answers like the Gemini API with the same canned JSON

These are NOT the real services: they exist so the build, search and n8n
workflows can be exercised end to end without internet access.
"""

import argparse
import hashlib
import io
import json
import math
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

DIM = 64

# Three fake papers about retrieval-augmented QA, with distinct vocabularies.
PAPERS = [
    {
        "id": "2101.00001",
        "version": 2,
        "title": "Dense Passage Reranking for Open-Domain Question Answering",
        "abstract": "We study reranking of dense passages for open-domain question answering. "
        "A cross-encoder reranker improves exact match on Natural Questions and TriviaQA.",
        "published": "2021-01-04T10:00:00Z",
        "topic": ["reranker", "cross-encoder", "passages", "natural", "questions"],
    },
    {
        "id": "2203.00002",
        "version": 1,
        "title": "Iterative Query Rewriting for Multi-Hop Retrieval-Augmented Generation",
        "abstract": "We propose rewriting the search query after each retrieval step so that a "
        "language model can answer multi-hop questions on HotpotQA and MuSiQue.",
        "published": "2022-03-07T10:00:00Z",
        "topic": ["rewriting", "multi-hop", "hotpotqa", "musique", "iterative"],
    },
    {
        "id": "2405.00003",
        "version": 3,
        "title": "Citation Faithfulness in Retrieval-Augmented Question Answering",
        "abstract": "We measure whether generated answers cite passages that actually support "
        "them and propose a faithfulness metric for attributed question answering.",
        "published": "2024-05-02T10:00:00Z",
        "topic": ["citation", "faithfulness", "attribution", "support", "metric"],
    },
]


# ---- arXiv Atom ------------------------------------------------------------


def atom_entry(p: dict) -> str:
    vid = f"{p['id']}v{p['version']}"
    return f"""
  <entry>
    <id>http://arxiv.org/abs/{vid}</id>
    <published>{p['published']}</published>
    <title>{p['title']}</title>
    <summary>  {p['abstract']}
    </summary>
    <author><name>Ada Example</name></author>
    <author><name>Alan Sample</name></author>
    <arxiv:primary_category xmlns:arxiv="http://arxiv.org/schemas/atom" term="cs.CL" scheme="http://arxiv.org/schemas/atom"/>
    <category term="cs.CL" scheme="http://arxiv.org/schemas/atom"/>
    <category term="cs.IR" scheme="http://arxiv.org/schemas/atom"/>
    <link href="http://arxiv.org/abs/{vid}" rel="alternate" type="text/html"/>
    <link title="pdf" href="http://arxiv.org/pdf/{vid}" rel="related" type="application/pdf"/>
  </entry>"""


def atom_feed(papers: list[dict], total: int) -> str:
    entries = "".join(atom_entry(p) for p in papers)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title type="html">ArXiv Query</title>
  <opensearch:totalResults xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">{total}</opensearch:totalResults>
  <opensearch:startIndex xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">0</opensearch:startIndex>{entries}
</feed>"""


# ---- PDF -------------------------------------------------------------------


def make_pdf(p: dict) -> bytes:
    """A small two-column paper: headings, a results table with caption, a figure caption, references."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import BaseDocTemplate, Frame, PageTemplate, Paragraph, Spacer, Table

    styles = getSampleStyleSheet()
    body, head = styles["BodyText"], styles["Heading2"]
    words = p["topic"]
    buf = io.BytesIO()
    doc = BaseDocTemplate(buf, pagesize=A4, leftMargin=40, rightMargin=40, topMargin=40, bottomMargin=40)
    gap = 20
    width = (doc.width - gap) / 2
    frames = [
        Frame(doc.leftMargin, doc.bottomMargin, width, doc.height, id="left"),
        Frame(doc.leftMargin + width + gap, doc.bottomMargin, width, doc.height, id="right"),
    ]
    doc.addPageTemplates([PageTemplate(id="two", frames=frames)])

    def para(n: int, focus: str) -> str:
        return (
            f"This paragraph discusses {focus} in detail. Our approach relies on {words[0]} and {words[1]} "
            f"to improve retrieval quality. Experiments on {words[3]} show consistent gains. " * n
        )

    story = [
        Paragraph(p["title"], styles["Title"]),
        Paragraph("Ada Example, Alan Sample", body),
        Paragraph("Abstract", head),
        Paragraph(p["abstract"], body),
        Paragraph("1 Introduction", head),
        Paragraph(para(3, "the motivation for " + words[0]), body),
        Paragraph("2 Related Work", head),
        Paragraph(para(2, "prior work on " + words[2]), body),
        Paragraph("3 Method", head),
        Paragraph(para(4, "our " + words[1] + " method"), body),
        Paragraph("4 Experiments", head),
        Paragraph(para(2, "the evaluation of " + words[4]), body),
        Table([["Model", "EM", "F1"], ["BM25", "41.2", "48.9"], ["Ours", "47.8", "55.3"]]),
        Spacer(1, 6),
        Paragraph(f"Table 1: Exact match and F1 of {words[0]} on {words[3]}.", body),
        Spacer(1, 6),
        Paragraph(f"Figure 1: Accuracy of {words[1]} as the number of passages grows.", body),
        Paragraph("5 Conclusion", head),
        Paragraph(para(1, "the conclusions about " + words[0]), body),
        Paragraph("References", head),
        Paragraph("A. Author. 2020. Some cited paper. In Proceedings of ACL.", body),
    ]
    doc.build(story)
    return buf.getvalue()


# ---- embeddings ------------------------------------------------------------


def embed(text: str) -> list[float]:
    vec = [0.0] * DIM
    for word in re.findall(r"\w+", text.lower()):
        h = int(hashlib.md5(word.encode()).hexdigest(), 16)
        vec[h % DIM] += 1.0 if (h >> 8) % 2 else -1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


# ---- chat model ---------------------------------------------------------------

LLM_CALLS: list[dict] = []  # every request received, for tests


def _decompose(idea: str) -> dict:
    words = re.findall(r"[A-Za-z][\w-]+", idea)
    head = " ".join(words[:12]) or "unspecified method"
    return {
        "facets": {"task": "question answering", "method": head, "data": "not specified", "result": "not specified"},
        "queries": [
            {"text": " ".join(words[:10]) or "retrieval question answering", "facets": ["method"]},
            {"text": " ".join(words[5:15]) or "question answering retrieval", "facets": ["method", "task"]},
            {"text": "question answering " + " ".join(words[-4:]), "facets": ["task"]},
        ],
    }


def _judge(user: str) -> dict:
    matches = []
    for block in re.split(r"\n(?=\[C\d+\])", user):
        m = re.search(r"paper_id: (\S+)", block)
        if not m:
            continue
        chunks = re.findall(r"^\s+- \(([^)]+)\)", block, re.MULTILINE)
        matches.append(
            {
                "paper_id": m.group(1),
                "overlap": {"task": "same", "method": "partial", "data": "unknown", "result": "unknown"},
                "evidence": chunks[:1],
                "rationale": "Both address question answering; the passages describe a related retrieval method.",
            }
        )
    feedback = "FEEDBACK ON YOUR PREVIOUS ANSWER" in user
    if "STUBBORN" in user or ("HALLUCINATE" in user and not feedback):
        matches.append(
            {
                "paper_id": "arxiv:9999.99999",
                "overlap": {"task": "same", "method": "same", "data": "same", "result": "same"},
                "evidence": ["arxiv:9999.99999#abs"],
                "rationale": "A well-known paper that does exactly this.",
            }
        )
    return {"matches": matches, "summary": "The closest papers share the task; the method overlaps only partly."}


def _answer(system: str, user: str) -> str:
    if "idea decomposer" in system:
        idea = user.split("<<<IDEA", 1)[-1].split("IDEA>>>", 1)[0]
        body = _decompose(idea)
    elif "novelty judge" in system:
        body = _judge(user)
    else:
        body = {"note": "stub: unknown prompt"}
    return "```json\n" + json.dumps({"output": body}) + "\n```"


GEMINI_CALLS: list[dict] = []


def gemini_generate(payload: dict) -> tuple[int, dict]:
    GEMINI_CALLS.append(payload)

    def texts(content: dict | None) -> str:
        return " ".join(p.get("text", "") for p in (content or {}).get("parts", []))

    system = texts(payload.get("systemInstruction") or payload.get("system_instruction"))
    user = " ".join(texts(c) for c in payload.get("contents", []) if c.get("role") != "model")
    if not system and "idea decomposer" in user:  # system prompt merged into the contents
        system = user
    if "FAIL_ALL" in user:
        return 500, {"error": {"code": 500, "message": "stub model failure", "status": "INTERNAL"}}
    content = _answer(system or user, user)
    return 200, {
        "candidates": [{"content": {"parts": [{"text": content}], "role": "model"}, "finishReason": "STOP", "index": 0}],
        "usageMetadata": {"promptTokenCount": len(user) // 4, "candidatesTokenCount": len(content) // 4, "totalTokenCount": (len(user) + len(content)) // 4},
        "modelVersion": "stub",
    }


def chat_completion(payload: dict) -> tuple[int, dict]:
    messages = payload.get("messages", [])
    LLM_CALLS.append(payload)
    system = " ".join(str(m.get("content", "")) for m in messages if m.get("role") == "system")
    user = " ".join(str(m.get("content", "")) for m in messages if m.get("role") == "user")
    if "FAIL_LLM" in user or "FAIL_ALL" in user:
        return 500, {"error": {"message": "stub model failure", "type": "server_error"}}
    content = _answer(system, user)
    return 200, {
        "id": "chatcmpl-stub",
        "object": "chat.completion",
        "created": 0,
        "model": payload.get("model", "stub"),
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": len(user) // 4, "completion_tokens": len(content) // 4, "total_tokens": (len(user) + len(content)) // 4},
    }


# ---- server ----------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    pdf_cache: dict[str, bytes] = {}
    lock = threading.Lock()

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        if url.path == "/health":
            return self._send(200, b"", "text/plain")
        if url.path == "/info":  # like TEI
            return self._send(200, json.dumps({"model_id": "stub/hashed-bow"}).encode(), "application/json")
        if url.path == "/api/query":
            q = parse_qs(url.query)
            if "id_list" in q:
                wanted = set(q["id_list"][0].split(","))
                # Like arXiv: a versioned id asks for that version, a bare id for the latest one.
                found = [p for p in PAPERS if f"{p['id']}v{p['version']}" in wanted or p["id"] in wanted]
                return self._send(200, atom_feed(found, len(found)).encode(), "application/atom+xml")
            start = int(q.get("start", ["0"])[0])
            size = int(q.get("max_results", ["10"])[0])
            page = PAPERS[start : start + size]
            return self._send(200, atom_feed(page, len(PAPERS)).encode(), "application/atom+xml")
        m = re.match(r"^/pdf/(?P<id>[\d.]+)v(?P<v>\d+)$", url.path)
        if m:
            paper = next((p for p in PAPERS if p["id"] == m.group("id") and str(p["version"]) == m.group("v")), None)
            if not paper:
                return self._send(404, b"not found", "text/plain")
            with self.lock:
                if url.path not in self.pdf_cache:
                    self.pdf_cache[url.path] = make_pdf(paper)
            return self._send(200, self.pdf_cache[url.path], "application/pdf")
        self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length))
        if path == "/openai/v1/chat/completions":
            code, body = chat_completion(payload)
            return self._send(code, json.dumps(body).encode(), "application/json")
        if re.match(r"^/v1(beta)?/models/[^/]+:generateContent$", path):
            code, body = gemini_generate(payload)
            return self._send(code, json.dumps(body).encode(), "application/json")
        if path != "/embed":
            return self._send(404, b"not found", "text/plain")
        inputs = payload["inputs"]
        inputs = [inputs] if isinstance(inputs, str) else inputs
        self._send(200, json.dumps([embed(t) for t in inputs]).encode(), "application/json")

    def log_message(self, *args: object) -> None:
        pass


def serve(port: int = 0) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"stub arXiv + embedder on http://127.0.0.1:{args.port}", flush=True)
    srv.serve_forever()
