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

    @property
    def manifest_path(self) -> Path:
        return self.corpus_dir / "manifest.csv"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
