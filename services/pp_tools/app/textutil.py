"""Approximate token counting, shared by every chunker so sizes are comparable.

We cannot ship the embedding model's tokenizer in pp-tools without pulling the
model files, so sizes are counted with a regex: each word and each punctuation
mark is one token. On English scientific text this undercounts the subword
tokens of XLM-RoBERTa-based models such as BGE-M3 by roughly 20 to 40%, so a
512-token chunk here is about 600 to 700 model tokens. That is far below
BGE-M3's 8192-token limit, so nothing is truncated. What matters for the
chunking experiment is that both strategies are measured with the same ruler.
"""

import re
from collections.abc import Iterator

_TOKEN_RE = re.compile(r"\w+|[^\w\s]", re.UNICODE)


def count_tokens(text: str) -> int:
    return sum(1 for _ in _TOKEN_RE.finditer(text))


def token_spans(text: str) -> list[tuple[int, int]]:
    """Character spans of each approximate token, in order."""
    return [m.span() for m in _TOKEN_RE.finditer(text)]


def windows(text: str, size: int, overlap: int) -> Iterator[str]:
    """Slide a window of `size` tokens over `text`, stepping by size - overlap.

    Windows are cut from the original string by character offsets, so spacing
    and punctuation are preserved exactly.
    """
    if size <= 0:
        raise ValueError("size must be positive")
    if not 0 <= overlap < size:
        raise ValueError("overlap must be >= 0 and smaller than size")
    spans = token_spans(text)
    if not spans:
        return
    step = size - overlap
    start = 0
    while True:
        end = min(start + size, len(spans))
        yield text[spans[start][0] : spans[end - 1][1]]
        if end == len(spans):
            return
        start += step


def normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()
