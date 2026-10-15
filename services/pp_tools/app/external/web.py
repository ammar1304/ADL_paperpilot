"""Web search for the search agent: the first provider that answers wins.

Providers are tried in order; a failure is recorded and the next one is tried.
The order is chosen at startup (app/main.py): Semantic Scholar first when an API
key is configured, OpenAlex first otherwise (D29).
"""

from typing import Protocol

from .semantic_scholar import ExternalPaper

SOURCE_NAMES = {"semantic_scholar": "Semantic Scholar", "openalex": "OpenAlex"}


class Provider(Protocol):
    def search(self, query: str, limit: int = 10) -> list[ExternalPaper]: ...

    def close(self) -> None: ...


class WebSearchError(Exception):
    pass


class WebSearch:
    def __init__(self, providers: list[tuple[str, Provider]]) -> None:
        self.providers = providers

    @property
    def names(self) -> list[str]:
        return [name for name, _ in self.providers]

    def search(self, query: str, limit: int = 10) -> tuple[list[ExternalPaper], str, list[str]]:
        """(papers, the provider that answered, errors from providers tried before it)."""
        errors: list[str] = []
        for name, provider in self.providers:
            try:
                return provider.search(query, limit), name, errors
            except Exception as exc:  # noqa: BLE001 - every provider error is recorded and the next is tried
                errors.append(f"{SOURCE_NAMES.get(name, name)}: {str(exc)[:180]}")
        raise WebSearchError("; ".join(errors) or "no web search provider configured")

    def close(self) -> None:
        for _, provider in self.providers:
            provider.close()
