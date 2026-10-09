# Discovery Agent frontend

Standalone React + TypeScript dashboard. All application changes are contained in this directory.

## Run

```powershell
cd C:\work\AgenticSDLC\frontend
npm install
npm run dev
```

Open the local URL printed by Vite (usually `http://localhost:5173`). The dev server proxies relative `/api` requests to `http://localhost:8000`. Start your backend separately; if unavailable, the UI displays health and request errors rather than fabricated agent responses.

```powershell
npm test
npm run build
npm run preview
```

For production, serve `dist` and route `/api` to the backend on the same origin. The development proxy does not apply to the production bundle or preview server.

## API behavior

- `GET /api/health`: checks connectivity on load and on **Check again**.
- `POST /api/chat`: sends `{ thread_id, message }` and incrementally consumes SSE `data:` events. Status and logs populate the live activity panel; questions enter the conversation; drafts populate the review panel.
- `POST /api/confirm`: sends `{ thread_id, action }`, including `title` and `body` for create/edit. Edit supports either JSON or SSE. JSON outcomes are shown verbatim for transparency; HTTP errors, explicit error outcomes, invalid events, and interrupted streams are visible.
- **Edit** opens local fields; **Save edits** submits an edit request. Further conversational edits use the same thread through `/api/chat`. **Cancel** submits cancellation; **Confirm Create** is the only action that requests creation.
- Threads last for the current page session. **Stop** only aborts the local connection; verify backend outcomes before retrying creation.
- SSE supports chunked UTF-8, CRLF/LF/CR framing, comments, and multiline data. Known event payloads are validated; unknown event types are ignored for forward compatibility. A `done` event is required for successful stream completion.
- Draft descriptions are rendered as plain text (including Markdown source), not HTML. Similar-issue links allow only HTTP(S). Activity keeps the latest 100 events.

Styling uses local CSS with system fonts; no external fonts, assets, or mock backend are required.
