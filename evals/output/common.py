"""Shared paths, case definitions and cache helpers for the output-quality eval.

Run everything from the repo root (Settings reads .env relative to the working directory):

    .venv/bin/python -m evals.output.fetch      # step 1: documents + metadata -> cache/
    .venv/bin/python -m evals.output.run        # steps 2-5: summarise, check, judge, report
"""

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
CACHE = HERE / "cache"
FILES = CACHE / "files"  # content-addressed copies (hardlinks) of every document
PAGES = CACHE / "pages"  # <sha>.json: extracted page texts
CASES_DIR = CACHE / "cases"  # <case_id>.json: MatterInfo + refs + files for one case
GEN = CACHE / "gen"  # <case_id>__<key>.json: generator output
JUDGE = CACHE / "judge"  # <key>.json: judge verdicts

MAX_DOCS = 10  # Settings.max_docs_per_request: what the pipeline fetches per request


@dataclass(frozen=True)
class Case:
    provider: str
    matter: str
    category: str
    extra: bool = False  # not in the brief's list; added because it was already cached

    @property
    def id(self) -> str:
        return f"{self.provider}_{self.matter}_{self.category}".replace(" ", "_")


CASES: tuple[Case, ...] = (
    Case("uarb", "M12205", "Other Documents"),
    Case("uarb", "M12205", "Key Documents"),
    Case("uarb", "M12205", "Exhibits"),
    Case("uarb", "M12383", "Other Documents"),
    Case("uarb", "M12383", "Key Documents"),
    Case("uarb", "M12383", "Exhibits", extra=True),
    Case("oeb", "EB-2024-0111", "Decisions and Orders"),
    Case("oeb", "EB-2023-0195", "Decisions and Orders"),
    Case("oeb", "EB-2025-0064", "Application and Evidence"),
    Case("ferc", "ER24-1234-000", "Applications and Filings"),
    Case("ferc", "RM22-14", "Orders and Decisions"),
    Case("ferc", "EL16-92", "Orders and Decisions"),
)


def ensure_dirs() -> None:
    for d in (FILES, PAGES, CASES_DIR, GEN, JUDGE):
        d.mkdir(parents=True, exist_ok=True)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text())


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False, default=str))
    os.replace(tmp, path)


def stable_key(*parts: Any) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:16]


def cached_file(sha256: str, ext: str) -> Path:
    return FILES / f"{sha256}{ext}"
