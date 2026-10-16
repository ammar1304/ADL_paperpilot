"""Service configuration, read from environment variables only."""

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", extra="ignore", populate_by_name=True)

    # ---- dependencies ---------------------------------------------------------
    # postgresql://user:password@host:port/dbname  (the app database, not n8n's)
    database_url: str = Field(alias="APP_DATABASE_URL")
    # Base URL of the Qdrant HTTP API, e.g. http://qdrant:6333.
    # "local::memory:" or "local:/some/dir" runs Qdrant in-process (tests and
    # sandboxes without Docker only; never in the real stack).
    qdrant_url: str = Field(alias="QDRANT_URL")
    # Text Embeddings Inference server serving the embedding model.
    embedder_url: str = Field(default="http://embedder:80", alias="EMBEDDER_URL")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    # Seconds allowed for each dependency probe in /health
    health_timeout_s: float = Field(default=2.0, alias="HEALTH_TIMEOUT_S")
    # Seconds to keep retrying the schema setup at startup before giving up
    startup_db_wait_s: float = Field(default=30.0, alias="STARTUP_DB_WAIT_S")

    # ---- corpus -------------------------------------------------------------------
    # Folder holding manifest.csv (committed to git).
    corpus_dir: Path = Field(default=Path("/pp/corpus"), alias="CORPUS_DIR")
    # Downloaded metadata, PDFs and extracted text (a Docker volume, never git).
    cache_dir: Path = Field(default=Path("/var/pp/cache"), alias="CACHE_DIR")
    arxiv_api_url: str = Field(default="https://export.arxiv.org/api/query", alias="ARXIV_API_URL")
    arxiv_pdf_url: str = Field(default="https://arxiv.org/pdf", alias="ARXIV_PDF_URL")
    # arXiv asks for at most one request every 3 seconds, one connection at a time.
    arxiv_delay_s: float = Field(default=3.0, alias="ARXIV_DELAY_S")
    # Sent as the User-Agent so arXiv can contact you about your traffic.
    arxiv_contact: str = Field(default="", alias="ARXIV_CONTACT_EMAIL")

    # ---- chunking and embedding ---------------------------------------------
    # Sizes are in approximate tokens (see app/textutil.py).
    chunk_tokens: int = Field(default=512, alias="CHUNK_TOKENS")
    chunk_overlap: int = Field(default=64, alias="CHUNK_OVERLAP")
    embed_batch_size: int = Field(default=16, alias="EMBED_BATCH_SIZE")
    embed_timeout_s: float = Field(default=120.0, alias="EMBED_TIMEOUT_S")
    # Rebuilds reuse vectors already in Qdrant for chunks whose text and embedding
    # model are unchanged, so adding 30 papers does not re-embed the other 500.
    reuse_vectors: bool = Field(default=True, alias="REUSE_VECTORS")

    # ---- prompts and novelty branch (milestone 2) ------------------------------
    prompts_dir: Path = Field(default=Path("/pp/prompts"), alias="PROMPTS_DIR")
    # Which chunking strategy the novelty branch searches (decide from E1 results).
    novelty_strategy: str = Field(default="structured", alias="NOVELTY_STRATEGY")
    novelty_top_papers: int = Field(default=6, alias="NOVELTY_TOP_PAPERS")
    # Fused candidates re-ordered by the reranker before the top papers go to the judge.
    novelty_pool_papers: int = Field(default=30, alias="NOVELTY_POOL_PAPERS")
    novelty_rerank: bool = Field(default=True, alias="NOVELTY_RERANK")
    # Also search with the idea text itself, next to the decomposer's queries.
    novelty_idea_query: bool = Field(default=True, alias="NOVELTY_IDEA_QUERY")
    novelty_rerank_abstract: bool = Field(default=False, alias="NOVELTY_RERANK_ABSTRACT")
    # "rerank" or "hybrid" (D28): how the final order combines vector search and reranker.
    novelty_order: str = Field(default="hybrid", alias="NOVELTY_ORDER")
    # Text Embeddings Inference server running the cross-encoder; empty disables reranking.
    reranker_url: str = Field(default="", alias="RERANKER_URL")

    # ---- search agent (D29) ----------------------------------------------------------
    # Rounds the planner may add after the first retrieval; 0 turns the agent off.
    novelty_max_search_rounds: int = Field(default=2, ge=0, le=4, alias="NOVELTY_MAX_SEARCH_ROUNDS")
    # New papers the agent may add to the judge's candidates (it never removes any).
    novelty_agent_extra_papers: int = Field(default=2, ge=0, le=6, alias="NOVELTY_AGENT_EXTRA_PAPERS")
    novelty_agent_min_relevance: float = Field(default=0.2, ge=0, le=1, alias="NOVELTY_AGENT_MIN_RELEVANCE")
    semantic_scholar_url: str = Field(default="https://api.semanticscholar.org/graph/v1", alias="SEMANTIC_SCHOLAR_URL")
    # Optional; with a key Semantic Scholar allows 1 request per second, without one
    # requests share a public pool and are often refused (HTTP 429).
    semantic_scholar_api_key: str = Field(default="", alias="SEMANTIC_SCHOLAR_API_KEY")
    semantic_scholar_results: int = Field(default=10, ge=1, le=50, alias="SEMANTIC_SCHOLAR_RESULTS")
    semantic_scholar_interval_s: float = Field(default=1.1, alias="SEMANTIC_SCHOLAR_INTERVAL_S")
    # OpenAlex: the other web source; no key needed (10 requests per second). Empty URL disables it.
    openalex_url: str = Field(default="https://api.openalex.org", alias="OPENALEX_URL")
    openalex_api_key: str = Field(default="", alias="OPENALEX_API_KEY")
    rerank_timeout_s: float = Field(default=120.0, alias="RERANK_TIMEOUT_S")

    # ---- audit branch (milestone 3) -------------------------------------------------
    # docling-serve (D31); empty disables parsing.
    parser_url: str = Field(default="", alias="PARSER_URL")
    # Seconds one PDF may take to parse (CPU: 2 to 5 minutes per paper in the spike).
    parser_timeout_s: float = Field(default=1800.0, alias="PARSER_TIMEOUT_S")
    audit_max_pdf_mb: int = Field(default=30, ge=1, le=200, alias="AUDIT_MAX_PDF_MB")
    # Only for tests and local stand-ins: allow PDF URLs on private addresses.
    audit_allow_private_urls: bool = Field(default=False, alias="AUDIT_ALLOW_PRIVATE_URLS")

    @property
    def manifest_path(self) -> Path:
        return self.corpus_dir / "manifest.csv"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
