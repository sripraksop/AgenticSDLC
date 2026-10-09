# AgenticSDLC Business Analyst Agent

A local demo of a human-in-the-loop requirement analyst. A LangGraph workflow
classifies a stakeholder request, authors and reviews a Feature or Bug issue,
checks for related issues, and streams agent activity to a React dashboard. A
GitHub issue is created only after the user explicitly confirms the final draft.

## Requirements

- Python 3.10 or later
- Node.js 20 or later
- A GitHub personal access token with permission to create issues in
  `sripraksop/AgenticSDLC` (only needed to publish)
- An OpenAI API key is optional; the default demo mode works without one

## Run locally

1. Start the API:

   ```powershell
   cd backend
   py -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   Copy-Item .env.example .env
   # Set GITHUB_TOKEN in .env only if you intend to test publishing.
   uvicorn app.main:app --reload
   ```

2. In a second terminal, start the dashboard:

   ```powershell
   cd frontend
   npm install
   npm run dev
   ```

3. Open the Vite URL printed in the frontend terminal. Submit a requirement, review
   the draft and agent logs, and use the draft controls to edit, cancel, or confirm.

The API is available at `http://localhost:8000`; its health check is
`http://localhost:8000/api/health` and the interactive API docs are at
`http://localhost:8000/docs`.

## Configuration

Copy `backend/.env.example` to `backend/.env` and configure:

| Variable | Default | Purpose |
|---|---|---|
| `DEMO_MODE` | `true` | Deterministic local drafting without a model API key. |
| `OPENAI_API_KEY` | empty | Used when `DEMO_MODE=false`. |
| `OPENAI_MODEL` | `gpt-4o-mini` | LangChain OpenAI chat model used by LangGraph. |
| `GITHUB_TOKEN` | empty | GitHub token used for duplicate searches and issue creation. |

For `GITHUB_TOKEN`, prefer a fine-grained personal access token scoped to the
`sripraksop` resource owner and only the `AgenticSDLC` repository. Grant the
repository permission **Issues: Read and write** (metadata read access is
automatic). This covers related-issue searches and creating issues. If the
organization requires approval for fine-grained tokens, an organization owner
must approve it. Store the token in `backend/.env`, not in source files.

The publisher is intentionally restricted to `sripraksop/AgenticSDLC`. In demo
mode, conversations are held in API process memory and are lost when the API
restarts; use a persistent store and authentication before deploying beyond a
local demo. The agent can split independently deliverable requirements into
separate drafts; related requirements remain grouped as one issue. Review, edit,
cancel, and confirm each draft independently. Demo mode recognizes explicit
bullet or numbered lists as separate deliverables. Updating or commenting on
existing issues is not included in this version. Demo-mode Bug intake asks for
missing facts using `Label: value` lines before it drafts the report.

## Agent architecture and live logs

The LangGraph workflow currently has **three agent nodes**:

1. **Requirement Intake** classifies the conversation as Feature or Bug, captures
   supplied facts, and asks for missing Bug details instead of drafting early.
2. **Issue Authoring** uses the matching Feature or Bug prompt and returns a
   structured title and Markdown body. Privacy redaction runs before the draft is
   placed in workflow state.
3. **Quality Review** checks title format, required sections, placeholders, and
   Bug severity for each draft, then searches the fixed GitHub repository for
   possible duplicates. A draft that needs essential information is returned for
   more input instead of being made publishable.

These are three LangGraph nodes and three primary agent roles. Feature and Bug
authoring share one node with different prompts. Privacy redaction and duplicate
search are helper steps, not separate LangGraph agents. The Coordinator entries
in the activity panel are API-level workflow status messages, not another agent.
Human approval and each final GitHub create call are handled by `/api/confirm`
outside the drafting graph; the graph itself never publishes an issue. Every
separately drafted issue requires its own explicit confirmation.

Agent nodes emit structured progress events while they run using LangGraph's
custom stream. FastAPI forwards those events as Server-Sent Events (`log` events)
alongside coarse workflow `status` events. The React activity panel renders the
agent name, message, severity level, and browser-local timestamp as each event
arrives. Logs report progress and counts, not raw prompts, requirement text,
credentials, or entire issue bodies. The panel retains only its latest 100 events.

## Checks

Backend tests:

```powershell
cd backend
pytest
```

Frontend production build:

```powershell
cd frontend
npm run build
```
