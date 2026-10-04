"""Provider interface: one implementation per regulator portal.

Everything downstream (gate, cache, packaging, citations, delivery) is provider-agnostic.
Adding a regulator = implement `Provider`, register it, add a canary matter.
"""

import re
from collections.abc import AsyncIterator, Callable
from typing import Protocol, runtime_checkable

from agent.models import DocType, DocumentRef, DownloadedFile, MatterInfo


@runtime_checkable
class Provider(Protocol):
    name: str  # stable id, e.g. "uarb"
    display_name: str  # "Nova Scotia Utility and Review Board"
    matter_pattern: re.Pattern[str]  # how this regulator numbers its matters
    doc_types: tuple[DocType, ...]

    async def fetch_matter(self, matter: str) -> MatterInfo:
        """Metadata + per-tab counts. Raises MatterNotFound / PortalUnavailable."""
        ...

    async def list_documents(self, matter: str, doc_type: DocType, limit: int) -> list[DocumentRef]:
        """Newest-first document refs for one tab, at most `limit`."""
        ...

    def download(
        self, matter: str, refs: list[DocumentRef], dest_dir: str
    ) -> AsyncIterator[DownloadedFile]:
        """Yield files as they finish (lets packaging start before the last download)."""
        ...


_REGISTRY: dict[str, Callable[[], Provider]] = {}


def register(name: str, factory: Callable[[], Provider]) -> None:
    _REGISTRY[name] = factory


def get_provider(name: str) -> Provider:
    return _REGISTRY[name]()


def provider_for_matter(matter: str) -> str | None:
    """Pick the provider whose matter-number pattern matches. Ambiguity is a config bug."""
    hits = [n for n, f in _REGISTRY.items() if f().matter_pattern.fullmatch(matter)]
    if len(hits) > 1:
        raise RuntimeError(f"matter {matter} matches several providers: {hits}")
    return hits[0] if hits else None
