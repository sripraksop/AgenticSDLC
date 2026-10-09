import pytest
from fastapi.testclient import TestClient

from app import graph as workflow
from app import main as api
from app.main import app, sessions

client = TestClient(app)


@pytest.fixture(autouse=True)
def force_demo_mode(monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "true")


def test_health_endpoint():
    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_confirmation_is_required_before_publication(monkeypatch):
    async def no_similar_issues(_title):
        return []

    monkeypatch.setattr(workflow, "search_similar_issues", no_similar_issues)
    thread_id = "test-unconfirmed"
    sessions[thread_id] = {
        "thread_id": thread_id,
        "messages": [],
        "status": "awaiting_confirmation",
        "title": "Feature - Example",
        "body": "Draft body",
        "issue_type": "Feature",
        "readiness": "ready",
    }

    response = client.post(
        "/api/confirm",
        json={"thread_id": thread_id, "action": "edit", "title": "Feature - Revised", "body": "Revised draft"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "needs_input"
    assert response.json()["readiness"] == "needs_input"
    assert sessions[thread_id]["status"] == "awaiting_input"

    create_response = client.post(
        "/api/confirm",
        json={"thread_id": thread_id, "action": "create"},
    )
    assert create_response.status_code == 409

    valid_body = workflow._demo_draft(
        "Feature",
        "Build a LangGraph requirement analyst.",
    ).body
    revised = client.post(
        "/api/confirm",
        json={
            "thread_id": thread_id,
            "action": "edit",
            "title": "Feature - AI Assisted Business Analyst Agent",
            "body": valid_body,
        },
    )
    assert revised.status_code == 200
    assert revised.json()["status"] == "draft_ready"
    assert sessions[thread_id]["status"] == "awaiting_confirmation"


def test_confirmation_rejects_threads_without_a_reviewed_draft():
    response = client.post(
        "/api/confirm",
        json={"thread_id": "unknown-thread", "action": "create"},
    )

    assert response.status_code == 404


def test_edited_draft_is_rechecked_before_it_can_be_published(monkeypatch):
    async def no_similar_issues(_title):
        return []

    monkeypatch.setattr(workflow, "search_similar_issues", no_similar_issues)
    thread_id = "test-reviewed-edit"
    title = "Feature - AI Assisted Business Analyst Agent"
    body = workflow._demo_draft(
        "Feature",
        "Build a LangGraph requirement analyst.",
    ).body
    sessions[thread_id] = {
        "thread_id": thread_id,
        "messages": [],
        "status": "awaiting_confirmation",
        "title": title,
        "body": body,
        "issue_type": "Feature",
        "readiness": "ready",
        "questions": [],
    }

    response = client.post(
        "/api/confirm",
        json={"thread_id": thread_id, "action": "edit", "title": title, "body": body},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "draft_ready"
    assert response.json()["readiness"] == "ready_with_questions"
    assert any(event["agent"] == "Quality Review" for event in response.json()["events"])
    assert sessions[thread_id]["status"] == "awaiting_confirmation"


def test_chat_streams_reviewed_draft_without_publishing(monkeypatch):
    async def no_similar_issues(_title):
        return []

    async def publishing_must_not_run(*_args):
        raise AssertionError("A chat request must never publish an issue.")

    monkeypatch.setattr(workflow, "search_similar_issues", no_similar_issues)
    monkeypatch.setattr(api, "create_github_issue", publishing_must_not_run)

    response = client.post(
        "/api/chat",
        json={
            "thread_id": "test-draft-stream",
            "message": "Build a requirement analyst agent with a chat dashboard.",
        },
    )

    assert response.status_code == 200
    assert '"agent": "Requirement Intake"' in response.text
    assert "Using deterministic demo-mode classification" in response.text
    assert '"agent": "Feature Authoring"' in response.text
    assert '"agent": "Quality Review"' in response.text
    assert "Searching the fixed target repository" in response.text
    assert '"type": "draft"' in response.text
    assert '"status": "awaiting_confirmation"' in response.text
    assert sessions["test-draft-stream"]["status"] == "awaiting_confirmation"


def test_demo_intake_asks_for_bug_facts_before_drafting(monkeypatch):
    async def no_similar_issues(_title):
        return []

    monkeypatch.setattr(workflow, "search_similar_issues", no_similar_issues)
    response = client.post(
        "/api/chat",
        json={"thread_id": "test-bug-intake", "message": "Bug: the dashboard crashes."},
    )

    assert response.status_code == 200
    assert '"type": "question"' in response.text
    assert '"type": "draft"' not in response.text
    assert sessions["test-bug-intake"]["status"] == "awaiting_input"

    follow_up = client.post(
        "/api/chat",
        json={
            "thread_id": "test-bug-intake",
            "message": (
                "Module: Dashboard\nEnvironment: QA\nSteps to reproduce: 1. Open dashboard; 2. Click reports\n"
                "Expected behavior: Reports should open\nActual behavior: Dashboard crashes\n"
                "Frequency: Always\nSeverity: High\nImpact: QA cannot test reports\nEvidence: Not provided"
            ),
        },
    )

    assert follow_up.status_code == 200
    assert '"type": "draft"' in follow_up.text
    assert '"title": "Bug - Dashboard Reported Defect"' in follow_up.text
    assert sessions["test-bug-intake"]["readiness"] == "ready"


def test_demo_feature_draft_uses_the_submitted_requirement(monkeypatch):
    async def no_similar_issues(_title):
        return []

    monkeypatch.setattr(workflow, "search_similar_issues", no_similar_issues)
    requirement = "Add CSV export for report rows."
    response = client.post(
        "/api/chat",
        json={"thread_id": "test-generic-feature", "message": requirement},
    )

    assert response.status_code == 200
    assert '"title": "Feature - Stakeholder Requirement Draft"' in response.text
    assert requirement in response.text
    assert sessions["test-generic-feature"]["readiness"] == "ready_with_questions"


def test_demo_mode_splits_independent_bullets_into_reviewable_issue_drafts(monkeypatch):
    async def no_similar_issues(_title):
        return []

    monkeypatch.setattr(workflow, "search_similar_issues", no_similar_issues)
    response = client.post(
        "/api/chat",
        json={
            "thread_id": "test-multiple-drafts",
            "message": "Please implement these independent features:\n- Add CSV export\n- Add dashboard filters",
        },
    )

    assert response.status_code == 200
    assert '"draft_index": 0' in response.text
    assert '"draft_index": 1' in response.text
    assert '"draft_count": 2' in response.text
    assert len(sessions["test-multiple-drafts"]["drafts"]) == 2
    assert sessions["test-multiple-drafts"]["status"] == "awaiting_confirmation"


def test_each_issue_draft_requires_its_own_confirmation(monkeypatch):
    published = []

    async def create_issue(title, body, issue_type):
        issue = {
            "number": len(published) + 1,
            "title": title,
            "html_url": f"https://github.com/sripraksop/AgenticSDLC/issues/{len(published) + 1}",
        }
        published.append((title, body, issue_type))
        return issue, False

    monkeypatch.setattr(api, "create_github_issue", create_issue)
    thread_id = "test-create-individual-drafts"
    sessions[thread_id] = {
        "thread_id": thread_id,
        "status": "awaiting_confirmation",
        "issue_type": "Feature",
        "drafts": [
            {"title": "Feature - First", "body": "First body", "issue_type": "Feature", "readiness": "ready", "status": "awaiting_confirmation"},
            {"title": "Feature - Second", "body": "Second body", "issue_type": "Feature", "readiness": "ready", "status": "awaiting_confirmation"},
        ],
    }

    second = client.post(
        "/api/confirm",
        json={"thread_id": thread_id, "action": "create", "draft_index": 1},
    )
    assert second.status_code == 200
    assert second.json()["draft_index"] == 1
    assert second.json()["remaining_drafts"] == 1
    assert sessions[thread_id]["drafts"][0]["status"] == "awaiting_confirmation"
    assert sessions[thread_id]["drafts"][1]["status"] == "published"

    first = client.post(
        "/api/confirm",
        json={"thread_id": thread_id, "action": "create", "draft_index": 0},
    )
    assert first.status_code == 200
    assert first.json()["remaining_drafts"] == 0
    assert [entry[0] for entry in published] == ["Feature - Second", "Feature - First"]


def test_sensitive_values_are_redacted_from_issue_drafts():
    value, changed = workflow._redact_sensitive_data(
        "Contact alex@example.com; password=secret-value; token=ghp_abcdefghijklmnopqrstuvwxyz123456"
    )

    assert changed
    assert "alex@example.com" not in value
    assert "secret-value" not in value
    assert "ghp_abcdefghijklmnopqrstuvwxyz123456" not in value
    assert value.count("[REDACTED]") == 3
