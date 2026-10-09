from __future__ import annotations

import asyncio
import json
import logging
from typing import Literal
from uuid import uuid4

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .graph import (
    _redact_sensitive_data,
    build_graph,
    create_github_issue,
    draft_open_questions,
    review_node,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="AgenticSDLC Business Analyst API",
    description="Human-in-the-loop requirement analysis and GitHub issue drafting.",
    version="0.1.0",
)
graph = build_graph()
sessions: dict[str, dict] = {}
session_locks: dict[str, asyncio.Lock] = {}


class ChatRequest(BaseModel):
    thread_id: str = Field(default_factory=lambda: str(uuid4()))
    message: str = Field(min_length=1, max_length=20_000)


class ConfirmRequest(BaseModel):
    thread_id: str
    action: Literal["create", "edit", "cancel"]
    draft_index: int = Field(default=0, ge=0)
    title: str | None = Field(default=None, max_length=256)
    body: str | None = Field(default=None, max_length=65_000)


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "business-analyst-agent"}


@app.post("/api/chat")
async def chat(request: ChatRequest) -> StreamingResponse:
    thread_id = request.thread_id
    lock = session_locks.setdefault(thread_id, asyncio.Lock())
    async with lock:
        session = sessions.setdefault(
            thread_id,
            {
                "thread_id": thread_id,
                "messages": [],
                "status": "new",
                "drafts": [],
            },
        )
        session["messages"] = [
            *session.get("messages", []),
            {"role": "user", "content": request.message},
        ]
        initial_state = dict(session)

    async def stream():
        run_lock = session_locks[thread_id]
        try:
            async with run_lock:
                yield _sse({"type": "status", "message": "Starting requirement analysis."})
                async for mode, chunk in graph.astream(
                    initial_state,
                    stream_mode=["updates", "custom"],
                ):
                    if mode == "custom":
                        if isinstance(chunk, dict) and isinstance(chunk.get("agent"), str):
                            yield _sse({"type": "log", **chunk})
                        continue
                    if mode != "updates" or not isinstance(chunk, dict):
                        continue
                    for node_name, output in chunk.items():
                        if not isinstance(output, dict):
                            continue
                        sessions[thread_id].update(output)
                        if isinstance(output.get("drafts"), list):
                            sessions[thread_id]["drafts"] = output["drafts"]
                        yield _sse(
                            {
                                "type": "status",
                                "message": {
                                    "intake": "Analyzing and classifying the requirement.",
                                    "author": "Authoring the issue draft.",
                                    "review": "Checking quality and searching for similar issues.",
                                }.get(node_name, "Processing."),
                            }
                        )
                        for event in output.get("events", []):
                            yield _sse({"type": "log", **event})
                        if output.get("status") == "awaiting_input":
                            if not output.get("drafts"):
                                yield _sse(
                                    {
                                        "type": "question",
                                        "message": "Please clarify these points before I draft the issue.",
                                        "questions": output.get("questions", []),
                                    }
                                )
                        drafts = output.get("drafts")
                        if (
                            isinstance(drafts, list)
                            and drafts
                            and all("readiness" in draft for draft in drafts)
                        ):
                            for index, draft in enumerate(drafts):
                                yield _sse(
                                    {
                                        "type": "draft",
                                        "draft_index": index,
                                        "draft_count": len(drafts),
                                        "status": draft["status"],
                                        "title": draft["title"],
                                        "body": draft["body"],
                                        "readiness": draft["readiness"],
                                        "issue_type": sessions[thread_id]["issue_type"],
                                        "questions": draft.get("questions", []),
                                        "similar_issues": draft.get("similar_issues", []),
                                    }
                                )
                        elif output.get("status") == "awaiting_confirmation":
                            yield _sse(
                                {
                                    "type": "draft",
                                    "draft_index": 0,
                                    "draft_count": 1,
                                    "title": output["title"],
                                    "body": output["body"],
                                    "readiness": output["readiness"],
                                    "issue_type": sessions[thread_id]["issue_type"],
                                    "questions": output.get("questions", []),
                                    "similar_issues": output.get("similar_issues", []),
                                }
                            )
                yield _sse({"type": "done", "status": sessions[thread_id].get("status", "complete")})
        except (RuntimeError, httpx.HTTPError, ValueError) as error:
            logger.exception("Agent workflow failed for thread %s", thread_id)
            sessions[thread_id]["status"] = "error"
            yield _sse(
                {
                    "type": "error",
                    "message": str(error),
                    "options": ["Retry once", "Edit the draft", "Copy the Markdown"],
                }
            )
            yield _sse({"type": "done", "status": "error"})

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/confirm")
async def confirm(request: ConfirmRequest) -> dict:
    session = sessions.get(request.thread_id)
    if session is None:
        raise HTTPException(status_code=404, detail="This conversation was not found.")
    lock = session_locks.setdefault(request.thread_id, asyncio.Lock())
    async with lock:
        drafts = session.get("drafts") or [
            {
                "title": session.get("title", ""),
                "body": session.get("body", ""),
                "readiness": session.get("readiness", "ready"),
                "issue_type": session.get("issue_type", "Feature"),
                "questions": session.get("questions", []),
                "similar_issues": session.get("similar_issues", []),
                "status": session.get("status"),
            }
        ]
        if request.draft_index >= len(drafts):
            raise HTTPException(status_code=404, detail="This issue draft was not found.")
        draft = drafts[request.draft_index]
        can_edit_or_cancel = draft.get("status") in (
            "awaiting_confirmation",
            "awaiting_input",
        )
        if request.action == "create" and (
            draft.get("status") not in (None, "awaiting_confirmation")
            or draft.get("readiness") == "needs_input"
        ):
            raise HTTPException(
                status_code=409,
                detail="There is no draft awaiting confirmation for this conversation.",
            )
        if request.action in ("edit", "cancel") and not can_edit_or_cancel:
            raise HTTPException(
                status_code=409,
                detail="There is no draft available to edit or cancel for this conversation.",
            )

        if request.action == "cancel":
            draft["status"] = "cancelled"
            session["drafts"] = drafts
            session["status"] = (
                "cancelled"
                if all(item.get("status") in ("published", "cancelled") for item in drafts)
                else "awaiting_confirmation"
            )
            return {
                "status": "cancelled",
                "draft_index": request.draft_index,
                "message": "Issue draft cancelled. No GitHub issue was created for this draft.",
            }

        if request.action == "edit":
            if request.title is None or request.body is None:
                raise HTTPException(status_code=422, detail="Both an edited title and body are required.")
            title, title_redacted = _redact_sensitive_data(request.title.strip())
            if not title:
                raise HTTPException(status_code=422, detail="The issue title cannot be empty.")
            body, body_redacted = _redact_sensitive_data(request.body)
            review = await review_node(
                {
                    **session,
                    **draft,
                    "title": title,
                    "body": body,
                    "questions": draft_open_questions(draft["issue_type"], body),
                }
            )
            if title_redacted or body_redacted:
                review["events"] = [
                    *review["events"],
                    {
                        "agent": "Privacy Check",
                        "message": "Potential credentials or email addresses were redacted from the edited draft.",
                        "level": "warning",
                    },
                ]
            updated_draft = {**draft, **review}
            drafts[request.draft_index] = updated_draft
            session["drafts"] = drafts
            session.update(updated_draft)
            return {
                "status": (
                    "draft_ready"
                    if updated_draft["status"] == "awaiting_confirmation"
                    else "needs_input"
                ),
                "draft_index": request.draft_index,
                "title": updated_draft["title"],
                "body": updated_draft["body"],
                "readiness": updated_draft["readiness"],
                "issue_type": updated_draft["issue_type"],
                "similar_issues": updated_draft.get("similar_issues", []),
                "questions": updated_draft.get("questions", []),
                "events": updated_draft.get("events", []),
            }

        draft["status"] = "publishing"
        session["drafts"] = drafts
        try:
            issue, used_type_fallback = await create_github_issue(
                draft["title"],
                draft["body"],
                draft["issue_type"],
            )
        except RuntimeError as error:
            draft["status"] = "awaiting_confirmation"
            raise HTTPException(status_code=503, detail=str(error)) from error
        except httpx.HTTPStatusError as error:
            draft["status"] = "awaiting_confirmation"
            logger.exception("GitHub issue creation failed for thread %s", request.thread_id)
            response_text = error.response.text[:1000]
            if error.response.status_code in (401, 403):
                detail = (
                    "I don't have permission to create issues in AgenticSDLC. "
                    "Please check the GitHub connection."
                )
            elif error.response.status_code == 404:
                detail = "The repository was not found or is not accessible with the current GitHub connection."
            elif error.response.status_code == 422:
                detail = f"GitHub rejected the issue data: {response_text}"
            else:
                detail = (
                    f"GitHub rejected issue creation ({error.response.status_code}): "
                    f"{response_text}"
                )
            raise HTTPException(
                status_code=502,
                detail=detail,
            ) from error
        except httpx.HTTPError as error:
            draft["status"] = "awaiting_confirmation"
            logger.exception("GitHub issue creation failed for thread %s", request.thread_id)
            raise HTTPException(
                status_code=502,
                detail=f"GitHub could not be reached: {error}",
            ) from error

        draft["status"] = "published"
        draft["published_issue"] = issue
        session["drafts"] = drafts
        session["status"] = (
            "published" if all(item.get("status") in ("published", "cancelled") for item in drafts)
            else "awaiting_confirmation"
        )
        session["published_issue"] = issue
        return {
            "status": "created",
            "draft_index": request.draft_index,
            "number": issue["number"],
            "title": issue["title"],
            "url": issue["html_url"],
            "remaining_drafts": sum(
                item.get("status") not in ("published", "cancelled") for item in drafts
            ),
            "notice": (
                f"GitHub rejected the issue type; the issue was created with the "
                f"'{draft['issue_type'].lower()}' label instead."
                if used_type_fallback
                else None
            ),
        }
