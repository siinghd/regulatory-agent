"""Provider interface: one implementation per regulator portal.

Everything downstream (gate, cache, packaging, citations, delivery) is provider-agnostic.
Adding a regulator = implement `Provider` (including its document categories), register it,
add a canary matter.
"""

import re
from collections.abc import AsyncIterator, Callable, Iterable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from agent.models import DocumentRef, DownloadedFile, MatterInfo


@dataclass(frozen=True)
class Category:
    """A group of documents as the regulator presents them (a UARB tab, an OEB document type)."""

    name: str  # shown to users and stored on requests and documents
    aliases: tuple[str, ...]  # lowercase phrases a requester might write for it
    description: str  # one line for the classifier prompt


@runtime_checkable
class Provider(Protocol):
    """A regulator portal.

    A provider may also define `normalise(raw: str) -> str | None`, turning a matter number as
    a person writes it ("m 12205") into the canonical form ("M12205"). Without one, a mention
    is accepted when its upper-case form fully matches `matter_pattern`.
    """

    name: str  # stable id, e.g. "uarb"
    display_name: str  # "Nova Scotia Utility and Review Board"
    portal_url: str  # public home of the document database, linked from our emails
    matter_pattern: re.Pattern[str]  # canonical matter number, e.g. M12205
    mention_pattern: re.Pattern[str]  # a matter number as it may appear in free text
    matter_example: str  # "M12205"
    categories: tuple[Category, ...]  # in the order the portal presents them

    async def fetch_matter(self, matter: str) -> MatterInfo:
        """Metadata + per-category counts. Raises MatterNotFound / PortalUnavailable."""
        ...

    async def list_documents(self, matter: str, doc_type: str, limit: int) -> list[DocumentRef]:
        """Newest-first document refs for one category, at most `limit`."""
        ...

    async def list_matter_and_documents(
        self, matter: str, doc_type: str, limit: int
    ) -> tuple[MatterInfo, list[DocumentRef]]:
        """Both in one portal visit: the common path for a request."""
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


def all_providers() -> list[Provider]:
    return [factory() for factory in _REGISTRY.values()]


def provider_for_matter(matter: str) -> Provider | None:
    """Pick the provider whose canonical matter pattern matches. Ambiguity is a config bug."""
    hits = [p for p in all_providers() if p.matter_pattern.fullmatch(matter)]
    if len(hits) > 1:
        raise RuntimeError(f"matter {matter} matches several providers: {[p.name for p in hits]}")
    return hits[0] if hits else None


def normalise_with(provider: Provider, raw: str) -> str | None:
    """`raw` in `provider`'s canonical matter format, or None if it isn't one of its matters."""
    custom: Callable[[str], str | None] | None = getattr(provider, "normalise", None)
    if custom is not None:
        return custom(raw.strip())
    candidate = raw.strip().upper()
    return candidate if provider.matter_pattern.fullmatch(candidate) else None


def normalise_matter(raw: str) -> tuple[str, str] | None:
    """(provider name, canonical matter) for a matter number as a person might write it."""
    for provider in all_providers():
        matter = normalise_with(provider, raw)
        if matter:
            return provider.name, matter
    return None


def find_category(categories: Iterable[Category], name: str) -> Category | None:
    """Case-insensitive lookup by name: LLM output and stored values are matched this way."""
    wanted = name.strip().casefold()
    return next((c for c in categories if c.name.casefold() == wanted), None)
