"""Public progress page for one request: /r/{track_token}.

The token is an unguessable 16-char secret that only the requester receives (in the ack email).
The page shows the request's own status and timeline, nothing else: no other requests, no
email body, and a masked sender address.
"""

from typing import Annotated

from fastapi import APIRouter, Path, Request
from fastapi.responses import JSONResponse, Response

from agent import store

router = APIRouter()
Token = Annotated[str, Path(pattern=r"^[A-Za-z0-9_-]{12,32}$")]

STEPS = [
    ("received", "Email received"),
    ("accepted", "Sender verified and request understood"),
    ("fetching", "Fetching from the UARB database"),
    ("packaging", "Packaging and writing a cited summary"),
    ("replying", "Sending your reply"),
    ("done", "Done"),
]
_ORDER = {s: i for i, (s, _) in enumerate(STEPS)}
_EVENT_LABELS = {
    "received": "Email received",
    "state:accepted": "Request accepted",
    "sent:ack": "Confirmation sent",
    "state:fetching": "Started fetching from the portal",
    "state:packaging": "Downloads complete",
    "summary": "Cited summary written",
    "state:replying": "Reply ready",
    "sent:reply": "Reply sent",
    "state:done": "Finished",
    "state:failed": "Stopped after repeated errors",
    "state:clarify": "Asked you a question",
    "state:rejected": "Not processed",
    "retry": "Hit a problem, retrying",
}
# What a requester may learn about a rejection (internal reasons stay internal).
_PUBLIC_REJECT = {
    "injection_attempt": "The request asked for something I can't do.",
    "unrelated": "The email didn't look like a document request.",
}


def _mask(addr: str) -> str:
    local, _, domain = addr.partition("@")
    return f"{local[:1]}***@{domain}" if domain else "unknown"


async def _view(token: str) -> dict | None:
    row = await store.get_by_token(token)
    if row is None:
        return None
    events = await store.events(row["id"])
    state = row["state"]
    progress = row["progress"] or {}
    result = row["result"] or {}
    return {
        "matter": row["matter"],
        "doc_type": row["doc_type"],
        "from": _mask(row["from_addr"]),
        "state": state,
        "terminal": state in store.TERMINAL or state == "clarify",
        "step": progress.get("step"),
        "done": progress.get("done"),
        "total": progress.get("total"),
        "steps": [
            {"key": k, "label": label,
             "status": "done" if _ORDER.get(state, -1) > i or state == "done" else
                       "active" if _ORDER.get(state, -1) == i else "todo"}
            for i, (k, label) in enumerate(STEPS)
        ] if state in _ORDER else [],
        "timeline": [
            {"label": _EVENT_LABELS[e["kind"]], "at": e["at"].isoformat()}
            for e in events if e["kind"] in _EVENT_LABELS
        ],
        "outcome": _outcome(row, result),
        "received_at": row["received_at"].isoformat(),
        "updated_at": row["updated_at"].isoformat(),
    }


def _outcome(row, result: dict) -> str | None:
    state = row["state"]
    if state == "done" and result.get("files"):
        return f"Sent {result['files']} documents" + (f" with {result['citations']} cited key points." if result.get("citations") else ".")
    if state == "done":
        return "Answered by email."
    if state == "failed":
        return "The regulator's website kept failing. You've been emailed; please try again later."
    if state == "clarify":
        return "I emailed you a question. Reply to that email to continue."
    if state == "rejected":
        return _PUBLIC_REJECT.get(row["reject_reason"] or "", "This email wasn't processed.")
    return None


@router.get("/r/{token}.json")
async def progress_json(token: Token) -> Response:
    view = await _view(token)
    if view is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(view, headers={"Cache-Control": "no-store"})


@router.api_route("/r/{token}", methods=["GET", "HEAD"])
async def progress_page(request: Request, token: Token) -> Response:
    from agent.web.app import _error_page, templates  # shared renderer and error page

    view = await _view(token)
    if view is None:
        return _error_page(request, 404)
    return templates.TemplateResponse(
        request, "progress.html", {"v": view, "token": token},
        headers={"Cache-Control": "no-store"},
    )
