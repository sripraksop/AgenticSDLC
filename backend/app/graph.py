from __future__ import annotations

import os
from pathlib import Path
import re
from typing import Literal, TypedDict

import httpx
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field
from dotenv import load_dotenv

from .prompts import (
    INTAKE_PROMPT,
    QUALITY_REVIEW_PROMPT,
    build_authoring_prompt,
)

OWNER = "sripraksop"
REPOSITORY = "AgenticSDLC"
GITHUB_API = "https://api.github.com"
load_dotenv(Path(__file__).resolve().parents[1] / ".env")


class ConversationState(TypedDict, total=False):
    thread_id: str
    messages: list[dict[str, str]]
    issue_type: str
    requirement_summary: str
    facts: list[str]
    issue_summaries: list[str]
    drafts: list[dict]
    questions: list[str]
    title: str
    body: str
    readiness: str
    similar_issues: list[dict[str, str | int]]
    events: list[dict[str, str]]
    status: str
    redactions_made: bool


class IntakeResult(BaseModel):
    issue_type: Literal["Feature", "Bug"]
    requirement_summary: str
    facts: list[str] = Field(default_factory=list)
    issue_summaries: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    clarification_questions: list[str] = Field(default_factory=list)
    ready_for_draft: bool = True


class IssueDraft(BaseModel):
    title: str
    body: str
    assumptions: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)


class ReviewedDraft(BaseModel):
    title: str
    body: str
    readiness: Literal["ready", "ready_with_questions", "needs_input"]
    questions: list[str] = Field(default_factory=list)


def _llm():
    if os.getenv("DEMO_MODE", "true").lower() == "true":
        return None
    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError(
            "DEMO_MODE=false requires OPENAI_API_KEY to be configured."
        )
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(
        model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        temperature=0,
    )


def _transcript(state: ConversationState) -> str:
    return "\n".join(
        f"{message['role']}: {message['content']}"
        for message in state.get("messages", [])
    )


def _draft_quality_gaps(issue_type: str, title: str, body: str) -> list[str]:
    gaps: list[str] = []
    prefix = f"{issue_type} - "
    if not title.startswith(prefix) or len(title[len(prefix):].split()) > 8:
        gaps.append(f"Use the required `{prefix}<short title>` format with no more than eight words after the prefix.")

    headings = (
        [
            "Objective",
            "User Story",
            "Background",
            "Functional Requirements",
            "Validation Rules",
            "Technical Requirements",
            "Out of Scope",
            "Assumptions",
            "Open Questions",
            "Acceptance Criteria",
            "Definition of Done",
            "Validation",
        ]
        if issue_type == "Feature"
        else [
            "Summary",
            "Related",
            "Environment",
            "Steps to Reproduce",
            "Expected Behavior",
            "Actual Behavior",
            "Evidence",
            "Impact",
            "Acceptance Criteria",
            "Definition of Done",
            "Validation",
        ]
    )
    positions = [
        match.start()
        if (match := re.search(rf"(?m)^## {re.escape(heading)}\s*$", body))
        else -1
        for heading in headings
    ]
    if any(position < 0 for position in positions):
        gaps.append("Include every required section from the issue template.")
    elif positions != sorted(positions):
        gaps.append("Put the required issue sections in the specified order.")
    if re.search(r"<[^>]+>|\b(?:TBD|TODO)\b", body, flags=re.IGNORECASE):
        gaps.append("Remove placeholder text from the issue body.")
    if issue_type == "Bug" and not re.search(
        r"- Severity: (Critical|High|Medium|Low)\b", body
    ):
        gaps.append("Confirm the Bug severity using the severity guide before publication.")
    return gaps


def draft_open_questions(issue_type: str, body: str) -> list[str]:
    if issue_type != "Feature":
        return []
    section = re.search(
        r"(?ms)^## Open Questions[ \t]*$\n(.*?)(?=^## |\Z)",
        body,
    )
    if section is None:
        return []
    lines = [
        re.sub(r"^[-*]\s*", "", line.strip())
        for line in section.group(1).splitlines()
        if line.strip()
    ]
    if len(lines) == 1 and lines[0].lower() in ("none", "none identified"):
        return []
    return lines


def _redact_sensitive_data(text: str) -> tuple[str, bool]:
    patterns = [
        r"(?i)\b(password|passwd|secret|api[_-]?key|token|authorization)\s*[:=]\s*['\"]?[^'\"\s,;]+",
        r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,}|AKIA[A-Z0-9]{16})\b",
        r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b",
    ]
    redacted = text
    for pattern in patterns:
        redacted = re.sub(pattern, "[REDACTED]", redacted, flags=re.IGNORECASE)
    return redacted, redacted != text


BUG_INTAKE_FIELDS = {
    "Module": "the affected module or screen",
    "Environment": "the environment (Dev, QA, UAT, or Prod)",
    "Steps to reproduce": "numbered steps to reproduce",
    "Expected behavior": "the expected behavior",
    "Actual behavior": "the actual behavior",
    "Frequency": "whether it happens always or intermittently",
    "Severity": "the impact severity (Critical, High, Medium, or Low)",
    "Impact": "the affected users or business impact",
}


def _labeled_bug_facts(transcript: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for label in (*BUG_INTAKE_FIELDS, "Evidence", "Browser / Device", "Build / Version"):
        match = re.search(
            rf"(?im)^\s*(?:(?:user|assistant)\s*:\s*)?(?:[-*]\s*)?"
            rf"(?:\*\*)?{re.escape(label)}(?:\*\*)?\s*:\s*(.+?)\s*$",
            transcript,
        )
        if match:
            result[label] = match.group(1).strip()
    return result


def _demo_intake(state: ConversationState) -> IntakeResult:
    text = _transcript(state)
    latest_message = state.get("messages", [])[-1]["content"]
    listed_deliverables = [
        re.sub(r"^\s*(?:[-*]|\d+[.)])\s*", "", line).strip()
        for line in latest_message.splitlines()
        if re.match(r"^\s*(?:[-*]|\d+[.)])\s+\S", line)
    ]
    bug_reported = re.search(
        r"\b(?:bug|broken|crash(?:es|ed)?|incorrect|does not work|doesn't work|"
        r"not working|regression|failure|fails?)\b",
        text,
        flags=re.IGNORECASE,
    )
    issue_type = "Bug" if bug_reported else "Feature"
    if issue_type == "Bug":
        facts = _labeled_bug_facts(text)
        missing = [label for label in BUG_INTAKE_FIELDS if label not in facts]
        if missing:
            return IntakeResult(
                issue_type="Bug",
                requirement_summary=text,
                facts=list(facts.values()),
                clarification_questions=[
                    "Please provide these Bug details as `Label: value` lines: "
                    + "; ".join(f"{label} ({BUG_INTAKE_FIELDS[label]})" for label in missing)
                    + ". For evidence, write `Evidence: Not provided` if none is available."
                ],
                ready_for_draft=False,
            )
    return IntakeResult(
        issue_type=issue_type,
        requirement_summary=text,
        facts=[state["messages"][-1]["content"]],
        issue_summaries=(
            [text]
            if issue_type == "Bug"
            else listed_deliverables if len(listed_deliverables) > 1 else [text]
        ),
        open_questions=(
            ["Which model provider and deployment environment should production use?"]
            if issue_type == "Feature"
            else []
        ),
    )


def _demo_draft(issue_type: str, summary: str) -> IssueDraft:
    clean_summary = " ".join(summary.split())
    if issue_type == "Bug":
        bug_facts = _labeled_bug_facts(summary)
        module = bug_facts["Module"]
        module_name = " ".join(module.split()[:4])
        short_problem = f"{module_name} Reported Defect"
        title = f"Bug - {short_problem}"
        step_lines = [line.strip() for line in bug_facts["Steps to reproduce"].splitlines() if line.strip()]
        steps = "\n".join(
            line if re.match(r"^\d+\.\s", line) else f"{index}. {line}"
            for index, line in enumerate(step_lines, start=1)
        )
        severity = bug_facts["Severity"].strip().capitalize()
        evidence = bug_facts.get("Evidence", "Not provided")
        body = f"""# Bug Report: {short_problem}

## Summary
{bug_facts["Actual behavior"]}

## Related
- Feature / Issue: Not provided
- Pull Request: Not provided

## Environment
- Environment: {bug_facts["Environment"]}
- Module / Screen: {module}
- Browser / Device: {bug_facts.get("Browser / Device", "Not provided")}
- Build / Version: {bug_facts.get("Build / Version", "Not provided")}

## Steps to Reproduce
{steps}

## Expected Behavior
- {bug_facts["Expected behavior"]}

## Actual Behavior
- {bug_facts["Actual behavior"]}

## Evidence
{evidence}

## Impact
- Severity: {severity}
- Frequency: {bug_facts["Frequency"]}
- Affected users / business impact: {bug_facts["Impact"]}

---

## Acceptance Criteria
- [ ] Following the supplied Steps to Reproduce results in the supplied Expected Behavior.
- [ ] Related functionality has no regression.

## Definition of Done
- [ ] Root cause is identified and fixed.
- [ ] Unit tests cover the defect.
- [ ] Existing unit tests pass and the project builds successfully.

## Validation
- Re-run the supplied Steps to Reproduce in the supplied Environment.
- Run relevant unit tests and the full build.
"""
        return IssueDraft(title=title, body=body)

    is_business_analyst_build = any(
        phrase in summary.lower()
        for phrase in ("business analyst agent", "requirement analyst", "langgraph")
    )
    if not is_business_analyst_build:
        title = "Feature - Stakeholder Requirement Draft"
        body = f"""# Feature Request: Stakeholder Requirement Draft

## Objective
Prepare an issue draft for the stakeholder's supplied request:

> {clean_summary}

## User Story
As a stakeholder, I want the supplied request to be implemented, so that its stated objective can be achieved.

## Background
The supplied request is the source of truth for this draft. No additional application context was provided.

---

## Functional Requirements
### 1. Stakeholder Request
- The implementation shall address the supplied request quoted in the Objective section.

## Validation Rules
- No field formats, limits, or other validation rules were supplied.

## Technical Requirements
- Reuse existing APIs, services, and patterns where applicable.
- Follow existing UI design patterns and application styling standards.
- Implement proper error handling and user notifications.

## Out of Scope
- None identified

## Assumptions
- The request is classified as a Feature for this draft.

## Open Questions
- Which user roles and entry points are in scope?
- What testable acceptance criteria should define completion?
- Are there specific data, validation, UI, performance, or security requirements?

---

## Acceptance Criteria
### Stakeholder request
- [ ] The stakeholder confirms that the delivered behavior addresses the exact request quoted in the Objective section.
### Quality
- [ ] All existing and newly created unit tests pass.
- [ ] The solution builds successfully without errors.
- [ ] No regression issues are introduced.

## Definition of Done
- [ ] The stakeholder-approved request is implemented.
- [ ] Unit tests are created or updated and pass.
- [ ] The project build completes successfully.
- [ ] Code follows established coding and quality standards.

## Validation
- Run all relevant unit tests.
- Run the complete application build.
- Manually validate every Acceptance Criterion.
"""
        return IssueDraft(
            title=title,
            body=body,
            open_questions=[
                "Which user roles and entry points are in scope?",
                "What testable acceptance criteria should define completion?",
                "Are there specific data, validation, UI, performance, or security requirements?",
            ],
        )

    title = "Feature - AI Assisted Business Analyst Agent"
    body = f"""# Feature Request: AI Assisted Business Analyst Agent

## Objective
Build a human-in-the-loop business analyst agent that turns stakeholder requirements into GitHub issue drafts. This enables review and approval before an issue is created.

## User Story
As a stakeholder, I want to describe a requirement in a chat interface, so that an agent can prepare a reviewable GitHub issue.

## Background
The requested demo combines LangGraph agents exposed through an API with a React dashboard for chat, agent status, logs, and real-time interactions. The target repository is sripraksop/AgenticSDLC.

---

## Functional Requirements
### 1. Requirement Analysis and Issue Drafting
- The API shall use LangGraph to classify stakeholder input as a Feature or Bug and prepare an issue draft.
- The draft shall follow the matching authoring template and include open questions or assumptions when needed.
- The quality review shall check the draft before it is shown.

### 2. Human Approval and Issue Publishing
- The user shall be able to review, edit, cancel, or explicitly confirm a draft before publication.
- The system shall publish only to sripraksop/AgenticSDLC and only after explicit confirmation.
- The system shall check for similar issues before presenting the draft.

### 3. Dashboard and Agent Activity
- The React dashboard shall provide a chat interface for submitting requirements to the API.
- The dashboard shall show streamed agent status and interaction logs while a request is processed.
- The dashboard shall display the proposed issue title and body and provide approval controls.

## Validation Rules
- The repository owner shall be sripraksop and the repository shall be AgenticSDLC.
- The system shall not create an issue before the user explicitly confirms the final draft.
- The issue type shall be Feature or Bug.

## Technical Requirements
- The agent workflow shall be implemented with LangGraph and Python.
- The agent workflow shall be exposed through an API.
- The user interface shall use React and may use Tailwind CSS.
- The API shall surface model or GitHub errors without reporting a successful publication.

## Out of Scope
- Production authentication and authorization design.
- Hosting or deployment to a specific cloud provider.

## Assumptions
- The demo shall support a local demo mode that does not require model credentials.
- GitHub issue creation shall use a configured GitHub token.
- One approved draft creates one new issue.

## Open Questions
- Which model provider and deployment environment should production use?
- What authentication and authorization should be used for dashboard users?
- Should issue updates and comments be included in addition to creating new issues?

---

## Acceptance Criteria
### Requirement analysis and review
- [ ] Submitting a requirement produces a Feature or Bug draft with required template sections.
- [ ] The draft displays assumptions and unresolved questions instead of inventing missing details.
- [ ] The draft is quality-checked before it is displayed.
- [ ] Similar issues are shown when found by the repository search.
### Approval and dashboard
- [ ] The dashboard streams agent status and log events while processing a request.
- [ ] The user can edit or cancel a draft before publication.
- [ ] No issue is created when a draft is cancelled or left unconfirmed.
- [ ] Confirming a draft creates one issue in sripraksop/AgenticSDLC and returns its URL.
- [ ] API or GitHub failures are shown to the user without a success message.
### Quality
- [ ] Relevant unit tests pass.
- [ ] The application builds successfully.
- [ ] No regression issues are introduced.

## Definition of Done
- [ ] LangGraph workflow and API are implemented.
- [ ] React dashboard supports chat, streamed events, and draft approval.
- [ ] Unit tests cover workflow and publication gating.
- [ ] The application builds successfully.

## Validation
- Run backend unit tests.
- Build the frontend.
- Manually verify drafting, streamed activity, cancellation, editing, and confirmed issue creation.
"""
    return IssueDraft(
        title=title,
        body=body,
        assumptions=[
            "The demo supports a local mode without model credentials.",
            "GitHub issue creation requires a configured GitHub token.",
            "One approval creates one new issue.",
        ],
        open_questions=[
            "Which model provider and deployment environment should production use?",
            "What authentication and authorization should be used for dashboard users?",
            "Should issue updates and comments be included in addition to creating new issues?",
        ],
    )


async def search_similar_issues(title: str) -> list[dict[str, str | int]]:
    words = [word.strip(" -:") for word in title.split() if len(word.strip(" -:")) > 3]
    query_words = " ".join(words[:4])
    headers = {"Accept": "application/vnd.github+json"}
    token = os.getenv("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get(
            f"{GITHUB_API}/search/issues",
            params={"q": f"repo:{OWNER}/{REPOSITORY} {query_words} is:issue"},
            headers=headers,
        )
        response.raise_for_status()
    return [
        {
            "number": issue["number"],
            "title": issue["title"],
            "state": issue["state"],
            "url": issue["html_url"],
        }
        for issue in response.json().get("items", [])[:5]
    ]


async def intake_node(state: ConversationState) -> dict:
    writer = get_stream_writer()
    writer(
        {
            "agent": "Requirement Intake",
            "message": "Starting intake: reviewing the conversation and classifying the request.",
            "level": "info",
        }
    )
    model = _llm()
    if model is None:
        writer(
            {
                "agent": "Requirement Intake",
                "message": "Using deterministic demo-mode classification; no model call is made.",
                "level": "info",
            }
        )
        result = _demo_intake(state)
    else:
        writer(
            {
                "agent": "Requirement Intake",
                "message": "Calling the configured chat model for structured Feature/Bug intake.",
                "level": "info",
            }
        )
        result = await model.with_structured_output(IntakeResult).ainvoke(
            [
                ("system", INTAKE_PROMPT),
                (
                    "human",
                    f"Prior clarification questions: {state.get('questions', [])}\n\n"
                    f"Conversation:\n{_transcript(state)}",
                ),
            ]
        )
    if not result.ready_for_draft and result.clarification_questions:
        writer(
            {
                "agent": "Requirement Intake",
                "message": f"Intake needs clarification; prepared {len(result.clarification_questions[:5])} question(s).",
                "level": "warning",
            }
        )
        return {
            "issue_type": result.issue_type,
            "requirement_summary": result.requirement_summary,
            "facts": result.facts,
            "issue_summaries": result.issue_summaries or [result.requirement_summary],
            "questions": result.clarification_questions[:5],
            "status": "awaiting_input",
            "events": [
                {"agent": "Requirement Intake", "message": "Clarifying details are needed before drafting.", "level": "info"}
            ],
        }
    writer(
        {
            "agent": "Requirement Intake",
            "message": (
                f"Classified request as {result.issue_type}; captured "
                f"{len(result.facts)} fact(s) and {len(result.open_questions)} open question(s)."
            ),
            "level": "info",
        }
    )
    return {
        "issue_type": result.issue_type,
        "requirement_summary": result.requirement_summary,
        "facts": result.facts,
        "issue_summaries": result.issue_summaries or [result.requirement_summary],
        "questions": result.open_questions[:5],
        "status": "drafting",
        "events": [
            {"agent": "Requirement Intake", "message": f"Classified this request as {result.issue_type}.", "level": "info"}
        ],
    }


def route_after_intake(state: ConversationState) -> str:
    if state.get("status") == "awaiting_input":
        return END
    return "author"


async def author_node(state: ConversationState) -> dict:
    writer = get_stream_writer()
    writer(
        {
            "agent": f"{state['issue_type']} Authoring",
            "message": f"Selecting and applying the {state['issue_type']} issue template.",
            "level": "info",
        }
    )
    model = _llm()
    summaries = (
        [state["requirement_summary"]]
        if state["issue_type"] == "Bug"
        else state.get("issue_summaries") or [state["requirement_summary"]]
    )
    drafts: list[dict] = []
    if model is None:
        writer(
            {
                "agent": f"{state['issue_type']} Authoring",
                "message": "Using deterministic demo-mode authoring; no model call is made.",
                "level": "info",
            }
        )
        for summary in summaries:
            draft = _demo_draft(state["issue_type"], summary).model_dump()
            if len(summaries) > 1 and state["issue_type"] == "Feature":
                title_words = re.findall(r"[A-Za-z0-9']+", summary)[:6]
                if title_words:
                    draft["title"] = f"Feature - {' '.join(title_words).title()}"
            drafts.append(draft)
    else:
        writer(
            {
                "agent": f"{state['issue_type']} Authoring",
                "message": "Calling the configured chat model for a structured title and issue body.",
                "level": "info",
            }
        )
        for summary in summaries:
            draft = await model.with_structured_output(IssueDraft).ainvoke(
                [
                    (
                        "system",
                        build_authoring_prompt(state["issue_type"])
                        + "\nAuthor only the following independent deliverable; do not include other deliverables.",
                    ),
                    ("human", f"Requirement facts for this issue:\n{summary}"),
                ]
            )
            drafts.append(draft.model_dump())
    safe_drafts = []
    redactions_made = False
    for draft in drafts:
        safe_title, title_redacted = _redact_sensitive_data(draft["title"])
        safe_body, body_redacted = _redact_sensitive_data(draft["body"])
        redactions_made = redactions_made or title_redacted or body_redacted
        safe_drafts.append(
            {
                **draft,
                "issue_type": state["issue_type"],
                "title": safe_title,
                "body": safe_body,
                "questions": list(dict.fromkeys(state.get("questions", []) + draft.get("open_questions", [])))[:5],
            }
        )
    first_draft = safe_drafts[0]
    writer(
        {
            "agent": f"{state['issue_type']} Authoring",
            "message": (
                f"Prepared {len(safe_drafts)} issue draft(s) from the requested deliverable(s)."
            ),
            "level": "info",
        }
    )
    authoring_events = [
        {"agent": f"{state['issue_type']} Authoring", "message": "Prepared an issue draft from the supplied facts.", "level": "info"}
    ]
    if redactions_made:
        authoring_events.append(
            {
                "agent": "Privacy Check",
                "message": "Potential credentials or email addresses were redacted from the draft.",
                "level": "warning",
            }
        )
    return {
        "title": first_draft["title"],
        "body": first_draft["body"],
        "questions": first_draft["questions"],
        "drafts": safe_drafts,
        "status": "reviewing",
        "events": authoring_events,
        "redactions_made": redactions_made,
    }


async def review_node(state: ConversationState) -> dict:
    return await _review_node(state)


async def _review_node(state: ConversationState, writer=None) -> dict:
    def log(message: str, level: str = "info") -> None:
        if writer is not None:
            writer(
                {
                    "agent": "Quality Review",
                    "message": message,
                    "level": level,
                }
            )

    log("Starting quality review of the title, template sections, placeholders, and readiness.")
    model = _llm()
    if model is None:
        log("Using deterministic template and required-section checks in demo mode.")
        title = state["title"]
        body = state["body"]
        review_questions: list[str] = []
        readiness = "ready"
    else:
        log("Calling the configured chat model to review completeness and testability.")
        review = await model.with_structured_output(ReviewedDraft).ainvoke(
            [
                ("system", QUALITY_REVIEW_PROMPT),
                (
                    "human",
                    f"Issue type: {state['issue_type']}\n"
                    f"Title:\n{state['title']}\n\nBody:\n{state['body']}\n\n"
                    f"Known open questions:\n{state.get('questions', [])}",
                ),
            ]
        )
        title, body = review.title, review.body
        readiness = review.readiness
        review_questions = review.questions
    questions = list(dict.fromkeys(state.get("questions", []) + review_questions))
    quality_gaps = _draft_quality_gaps(state["issue_type"], title, body)
    if quality_gaps:
        readiness = "needs_input"
        questions = list(dict.fromkeys(questions + quality_gaps))
    if readiness == "needs_input" and not questions:
        questions = [
            "Please clarify the missing information identified by the quality review before publication."
        ]
    elif questions and readiness == "ready":
        readiness = "ready_with_questions"
    log(
        f"Quality checks complete: readiness is {readiness.replace('_', ' ')}; "
        f"{len(questions)} open question(s); {len(quality_gaps)} template/format issue(s).",
        "warning" if readiness == "needs_input" else "info",
    )
    similar: list[dict[str, str | int]] = []
    events = [
        {"agent": "Quality Review", "message": f"Review complete: {readiness.replace('_', ' ')}.", "level": "info"}
    ]
    try:
        log("Searching the fixed target repository for potentially related issues.")
        similar = await search_similar_issues(title)
        log(f"Duplicate search completed; found {len(similar)} possible match(es).")
        events.append(
            {
                "agent": "GitHub Duplicate Check",
                "message": f"Found {len(similar)} similar issue(s)." if similar else "No similar issues found.",
                "level": "info",
            }
        )
    except httpx.HTTPError as error:
        log(f"Duplicate search failed: {error}. The draft remains available for review.", "warning")
        events.append(
            {
                "agent": "GitHub Duplicate Check",
                "message": f"Could not check for duplicates: {error}. You can retry, edit the draft, or copy its Markdown.",
                "level": "warning",
            }
        )
    return {
        "title": title,
        "body": body,
        "readiness": readiness,
        "questions": questions,
        "similar_issues": similar,
        "status": "awaiting_input" if readiness == "needs_input" else "awaiting_confirmation",
        "events": events,
    }


async def review_graph_node(state: ConversationState) -> dict:
    writer = get_stream_writer()
    drafts = state.get("drafts")
    if not drafts:
        return await _review_node(state, writer)
    reviewed_drafts = []
    events = []
    for draft in drafts:
        reviewed = await _review_node(
            {
                **state,
                **draft,
                "questions": draft.get("questions", []),
            },
            writer,
        )
        reviewed_drafts.append({**draft, **reviewed})
        events.extend(reviewed.get("events", []))
    first_draft = reviewed_drafts[0]
    statuses = [draft["status"] for draft in reviewed_drafts]
    return {
        **first_draft,
        "drafts": reviewed_drafts,
        "status": (
            "awaiting_confirmation"
            if "awaiting_confirmation" in statuses
            else "awaiting_input"
        ),
        "events": events,
    }


def build_graph():
    workflow = StateGraph(ConversationState)
    workflow.add_node("intake", intake_node)
    workflow.add_node("author", author_node)
    workflow.add_node("review", review_graph_node)
    workflow.add_edge(START, "intake")
    workflow.add_conditional_edges(
        "intake",
        route_after_intake,
        {"author": "author", END: END},
    )
    workflow.add_edge("author", "review")
    workflow.add_edge("review", END)
    return workflow.compile()


def github_headers() -> dict[str, str]:
    token = os.getenv("GITHUB_TOKEN")
    if not token:
        raise RuntimeError(
            "GITHUB_TOKEN is not configured. Add a token with issue-write access to publish."
        )
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }


async def create_github_issue(title: str, body: str, issue_type: str) -> tuple[dict, bool]:
    payload = {"title": title, "body": body, "type": issue_type}
    used_type_fallback = False
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(
            f"{GITHUB_API}/repos/{OWNER}/{REPOSITORY}/issues",
            headers=github_headers(),
            json=payload,
        )
        if response.status_code == 422 and "type" in response.text.lower():
            used_type_fallback = True
            payload = {
                "title": title,
                "body": body,
                "labels": [issue_type.lower()],
            }
            response = await client.post(
                f"{GITHUB_API}/repos/{OWNER}/{REPOSITORY}/issues",
                headers=github_headers(),
                json=payload,
            )
        response.raise_for_status()
    return response.json(), used_type_fallback
