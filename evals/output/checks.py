"""Step 3: deterministic checks (no LLM).

(a) quote integrity  every kept claim's quote is page_text[char_start:char_end] on the cited page
(b) figures          every amount/date/percentage in the summary and claims is in the sources
(c) doc membership   claims cite documents of the case, that the model was shown
(d) reply wording    counts, grammar, ordering language, codes, empty fields, size

Each check returns {"name", "ok", "detail"}; "ok" None means not applicable.
"""

import itertools
import re
from decimal import Decimal

from agent.citations.claims import MAX_SUMMARY_SENTENCES, extract_figures, split_sentences
from agent.citations.ground import MAX_QUOTE_CHARS, MIN_QUOTE_CHARS, normalise_text
from agent.models import MatterInfo

# ---------------------------------------------------------------- (a)


def quote_checks(gen: dict, pages_by_doc: dict[str, list[str]]) -> list[dict]:
    out = []
    for c in gen["kept"]:
        pages = pages_by_doc.get(c["doc_external_id"])
        ok, why = True, []
        if pages is None or not 1 <= c["page"] <= len(pages):
            ok, why = False, ["cited page not in extracted pages"]
        else:
            span = pages[c["page"] - 1][c["char_start"] : c["char_end"]]
            if span != c["quote"]:
                ok = False
                why.append("stored quote != page_text[char_start:char_end]")
            if normalise_text(span) != normalise_text(c["quote"]):
                ok = False
                why.append("normalised mismatch")
        n = len(normalise_text(c["quote"]))
        if not MIN_QUOTE_CHARS <= n <= MAX_QUOTE_CHARS:
            ok = False
            why.append(f"quote length {n} outside [{MIN_QUOTE_CHARS},{MAX_QUOTE_CHARS}]")
        out.append({
            "claim_id": c["id"], "ok": ok, "detail": "; ".join(why),
            "fuzzy": c["score"] < 100, "score": c["score"], "page_corrected_from": c["page_corrected_from"],
        })
    return out


# ---------------------------------------------------------------- (b)
# Independent of claims._MONEY so that formats the production filter misses show up here.
_BARE_SCALED = re.compile(r"(?<![$\d.,])(\d{1,3}(?:,\d{3})*(?:\.\d+)?)\s?(million|billion)\b", re.IGNORECASE)
_DOLLAR_ANY = re.compile(r"(?:US|C|CA|CAD)?\$\s?\d[\d,]*(?:\.\d+)?(?:\s?(?:million|billion|thousand|[MBK])\b)?",
                         re.IGNORECASE)
_PERCENT = re.compile(r"(\d+(?:\.\d+)?)\s?(?:%|per\s?cent|percent)", re.IGNORECASE)
_NUMERIC_DATE = re.compile(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b")
_SCALE = {"million": 10**6, "billion": 10**9}


def _num(s: str) -> Decimal:
    return Decimal(s.replace(",", ""))


def figure_report(text: str, source_text: str, context_text: str) -> dict:
    """Figures in `text` and whether each is in all source pages + metadata / in what the model saw."""
    src, ctx = extract_figures(source_text), extract_figures(context_text)
    src_norm = normalise_text(source_text)
    found: list[dict] = []

    def add(kind: str, raw: str, in_src: bool, in_ctx: bool, prod_sees: bool = True) -> None:
        found.append({"kind": kind, "text": raw, "in_sources": in_src, "in_context": in_ctx,
                      "production_filter_sees_it": prod_sees})

    fig = extract_figures(text)
    for v in sorted(fig.money):
        add("money", f"${v:,}", v in src.money, v in ctx.money)
    for y, m, d in sorted(fig.days):
        add("date", f"{y}-{m:02}-{d:02}", (y, m, d) in src.days, (y, m, d) in ctx.days)
    for y, m in sorted(fig.months):
        known = src.months | {(a, b) for a, b, _ in src.days}
        known_ctx = ctx.months | {(a, b) for a, b, _ in ctx.days}
        add("month", f"{y}-{m:02}", (y, m) in known, (y, m) in known_ctx)
    # formats claims.extract_figures does not parse: "69.3 million" without "$", percentages, 10/23/2025
    for mm in _BARE_SCALED.finditer(text):
        v = _num(mm[1]) * _SCALE[mm[2].lower()]
        add("money_no_symbol", mm[0], v in src.money or normalise_text(mm[0]) in src_norm,
            v in ctx.money or normalise_text(mm[0]) in normalise_text(context_text), prod_sees=False)
    for mm in _PERCENT.finditer(text):
        pat = re.compile(rf"(?<![\d.]){re.escape(mm[1])}\s?(?:%|per\s?cent|percent)", re.IGNORECASE)
        add("percent", mm[0], bool(pat.search(source_text)), bool(pat.search(context_text)), prod_sees=False)
    for mm in _NUMERIC_DATE.finditer(text):
        add("numeric_date", mm[0], mm[0] in source_text, mm[0] in context_text, prod_sees=False)
    # "$" amounts the production regex turns into a different number (e.g. "$1.2B" parsed as $1.2)
    for mm in _DOLLAR_ANY.finditer(text):
        if re.search(r"\d\s?[MBK]\b", mm[0]) and not re.search(r"million|billion|thousand", mm[0], re.IGNORECASE):
            add("money_abbrev", mm[0], False, False, prod_sees=False)
    return {"figures": found, "ok": all(f["in_sources"] for f in found),
            "unsupported": [f["text"] for f in found if not f["in_sources"]],
            "not_in_context": [f["text"] for f in found if f["in_sources"] and not f["in_context"]]}


# ---------------------------------------------------------------- (c)


def doc_checks(gen: dict, case_doc_ids: set[str], context_pages: dict[str, list[int]]) -> list[dict]:
    out = []
    for c in gen["kept"]:
        doc, page = c["doc_external_id"], c["page"]
        shown = context_pages.get(doc, [])
        out.append({
            "claim_id": c["id"],
            "in_case": doc in case_doc_ids,
            "doc_in_context": doc in context_pages,
            "page_in_context": page in shown,
            "page_near_context": any(abs(page - p) <= 1 for p in shown),
            "ok": doc in case_doc_ids and doc in context_pages,
        })
    return out


# ---------------------------------------------------------------- (d)
_EMPTY = re.compile(r"\b(?:None|null|NULL|N/A|nan)\b|is about \.|\bIt is an? +matter\b|in the\s+category|"
                    r"\bdecided on unknown\b|\breceived on unknown\b|\(\s*\)")
_CODE_VALUE = re.compile(r"[A-Z]{2,5}")
_SIZE = re.compile(r"\((\d+(?:\.\d+)?) MB\)")


def _parse_found(sentence: str, names: list[str]) -> dict[str, int] | None:
    m = re.search(r"I found (.*)\.$", sentence)
    if not m:
        return None
    body = m.group(1)
    got: dict[str, int] = {}
    present, _, absent = body.partition(", and no ")
    if body.startswith("no documents"):
        present, absent = "", body.removeprefix("no documents").removeprefix(", and no ")
    for name in sorted(names, key=len, reverse=True):
        mm = re.search(rf"(\d+) {re.escape(name)}(?=,|$)", present)
        if mm:
            got[name] = int(mm.group(1))
            present = present.replace(mm.group(0), "")
    for name in names:
        if re.search(rf"(?:^|, | or ){re.escape(name)}(?=,| or |$)", absent):
            got.setdefault(name, 0)
    return got


def reply_checks(rec: dict, info: MatterInfo, reply: dict, provider, files_count: int) -> list[dict]:
    text, sentence = reply["text"], reply["matter_sentence"]
    names = [c.name for c in provider.categories]
    cat = rec["category"]
    total = info.counts.get(cat, 0)
    got, confidential, failed = files_count, rec["confidential"], len(rec["failed_titles"])
    checks: list[dict] = []

    def check(name: str, ok: bool | None, detail: str = "") -> None:
        checks.append({"name": name, "ok": ok, "detail": detail})

    parsed = _parse_found(sentence, names)
    want = {n: info.counts.get(n, 0) for n in names}
    check("counts_sentence_matches", parsed == want, "" if parsed == want else f"parsed={parsed} want={want}")

    bad_plural = [m.group(0) for n in names if n.endswith("s")
                  for m in re.finditer(rf"\b(?:all |the )?1 (?:public |most recent of the \d+ )?{re.escape(n)}\b", text)]
    bad_plural += re.findall(r"\b1 more are\b|\b1 of the [\w ]+ listed are\b|\bthe 1 most recent\b|\ball 1\b", text)
    check("singular_plural", not bad_plural, "; ".join(bad_plural))

    articles = [m.group(0) for m in re.finditer(r"\b(a|an) ([A-Za-z]+)", sentence)
                if (m.group(1) == "a") == (m.group(2)[0].lower() in "aeiou")]
    check("article_a_an", not articles, "; ".join(articles))

    dates = [f["filed_on"] for f in rec["refs"] if f["filed_on"]]
    is_desc = all(a >= b for a, b in itertools.pairwise(dates))
    says_recent = "most recent" in text
    says_listed = "in the order the portal lists them" in text
    if says_recent:
        check("ordering_language", is_desc, "says 'most recent' but listing is not newest-first" if not is_desc else "")
    elif says_listed:
        check("ordering_language", not is_desc, "says 'as listed' but listing is newest-first" if is_desc else "")
    else:
        check("ordering_language", None, "reply says 'all N': no ordering claim")
    readme_newest = "newest first" in reply["readme"]
    check("readme_ordering", (not readme_newest) or is_desc,
          "README says 'newest first' but the documents are not" if readme_newest and not is_desc else "")

    explained = got == total or (confidential and got + confidential == total) or got >= 10 or failed
    gap = total - got - confidential - failed
    check("downloaded_vs_total_explained", bool(explained) or gap <= 0,
          "" if explained else f"got {got} of {total} with limit 10, {confidential} confidential, {failed} failed: "
          f"{gap} unaccounted for, yet the reply implies a truncated selection")

    codes = [f"{k}={v!r}" for k, v in (("type", info.type), ("category", info.category), ("status", info.status),
                                       ("outcome", info.outcome)) if v and _CODE_VALUE.fullmatch(v)]
    codes += re.findall(r"\bDKT\b", text)
    check("no_portal_codes", not codes, "; ".join(dict.fromkeys(codes)))

    empties = _EMPTY.findall(text)
    check("no_empty_fields", not empties, "; ".join(empties))

    words = re.findall(r"[A-Za-z]+", info.title)
    check("title_informative", len(words) >= 3, f"title={info.title!r}")

    m = _SIZE.search(text)
    size_ok = bool(m) and float(m.group(1)) > 0 and abs(float(m.group(1)) - reply["download_size"] / 1e6) < 0.06
    check("size_string_sane", size_ok, m.group(0) if m else "no size string")

    found_list = re.search(r"I found (.*)\.$", sentence)
    items = found_list.group(1) if found_list else ""
    joined = (", and no " in items or " and no " in items or items.count(",") == 0
              or re.search(r", and \d+ ", items) is not None)
    check("count_list_conjunction", joined, "" if joined else f"list without 'and' before the last item: {items[-80:]!r}")

    runon = re.findall(r"in the order the portal lists them and (?:packaged|attached)", text)
    check("fetched_sentence_punctuation", not runon, "; ".join(runon))

    title_tail = re.search(r"\d{1,2}:\d{2}(?::\d{2})? ?[AP]M|…\.", sentence)
    check("title_rendering", title_tail is None, title_tail.group(0) if title_tail else "")

    summary = (rec.get("_summary") or "")
    n_sent = len(split_sentences(summary)) if summary else 0
    check("summary_sentence_count", (1 <= n_sent <= MAX_SUMMARY_SENTENCES) if summary else None,
          f"{n_sent} sentences")
    leaks = re.findall(r"\b(?:the )?metadata\b|\bprovided (?:pages|documents)\b|\bthe pages\b|"
                       r"\bdocuments provided\b|\bthe (?:provided )?excerpts?\b", summary, re.IGNORECASE)
    check("summary_no_prompt_vocabulary", (not leaks) if summary else None, "; ".join(leaks))
    return checks
