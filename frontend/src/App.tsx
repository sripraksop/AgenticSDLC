import { useEffect, useRef, useState } from 'react';
import { chat, confirm, draftReview, health, safeUrl, type AgentEvent, type Draft } from './api';

interface Message {
  id: string;
  role: 'user' | 'assistant';
  text: string;
  questions?: string[];
}
interface Activity {
  id: string;
  agent: string;
  message: string;
  level: string;
  time: string;
}
type Operation = 'chat' | 'create' | 'cancel' | 'edit';
const suggestions = ['Report a bug', 'Propose a feature', 'Improve developer experience'];

function describe(value: unknown): string {
  return typeof value === 'string' ? value : JSON.stringify(value, null, 2) ?? 'Not provided';
}
function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

export default function App() {
  const [threadId] = useState(() => crypto.randomUUID());
  const [messages, setMessages] = useState<Message[]>([]);
  const [activity, setActivity] = useState<Activity[]>([]);
  const [input, setInput] = useState('');
  const [drafts, setDrafts] = useState<Draft[]>([]);
  const [activeDraftIndex, setActiveDraftIndex] = useState(0);
  const draft = drafts[activeDraftIndex] ?? null;
  const [editing, setEditing] = useState(false);
  const [title, setTitle] = useState('');
  const [body, setBody] = useState('');
  const [operation, setOperation] = useState<Operation | null>(null);
  const [status, setStatus] = useState('Ready when you are');
  const [error, setError] = useState<string | null>(null);
  const [githubError, setGithubError] = useState(false);
  const [githubRetryUsed, setGithubRetryUsed] = useState(false);
  const [copyStatus, setCopyStatus] = useState<string | null>(null);
  const [outcome, setOutcome] = useState<{ action: string; value: unknown } | null>(null);
  const [connection, setConnection] = useState<'checking' | 'online' | 'offline'>('checking');
  const [healthError, setHealthError] = useState<string | null>(null);
  const [healthAttempt, setHealthAttempt] = useState(0);
  const request = useRef<AbortController | null>(null);
  const bottom = useRef<HTMLDivElement | null>(null);
  const composer = useRef<HTMLTextAreaElement | null>(null);
  const busy = operation !== null;

  useEffect(() => {
    const controller = new AbortController();
    setConnection('checking');
    setHealthError(null);
    health(controller.signal).then(() => {
      setConnection('online');
    }).catch((failure: unknown) => {
      if (controller.signal.aborted) return;
      setConnection('offline');
      setHealthError(errorMessage(failure));
    });
    return () => controller.abort();
  }, [healthAttempt]);

  useEffect(() => () => request.current?.abort(), []);
  useEffect(() => {
    bottom.current?.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }, [messages, operation]);

  function addMessage(text: string, questions?: string[]) {
    setMessages((previous) => [...previous, { id: crypto.randomUUID(), role: 'assistant', text, questions }]);
  }

  function onEvent(event: AgentEvent) {
    if (event.type === 'status') setStatus(event.message);
    if (event.type === 'status' || event.type === 'log' || event.type === 'error') {
      setActivity((previous) => [...previous.slice(-99), {
        id: crypto.randomUUID(),
        agent: event.type === 'log' ? event.agent : 'Coordinator',
        message: event.message,
        level: event.type === 'log' ? event.level : event.type === 'error' ? 'error' : 'info',
        time: new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }),
      }]);
    }
    if (event.type === 'draft') {
      setDrafts((previous) => {
        const next = [...previous];
        next[event.draft_index] = event;
        return next;
      });
      if (event.draft_index === 0) setActiveDraftIndex(0);
      if (event.draft_index === activeDraftIndex) {
        setTitle(event.title);
        setBody(event.body);
        setEditing(false);
      }
      addMessage(
        event.draft_count > 1
          ? `Issue draft ${event.draft_index + 1} of ${event.draft_count} is ready for review. Each issue needs separate confirmation.`
          : 'Your issue draft is ready for review. You can edit it directly or tell me what to change.',
      );
    }
    if (event.type === 'question') addMessage(event.message, event.questions);
    if (event.type === 'error') setError(event.message);
    if (event.type === 'done') setStatus(event.status);
  }

  async function run(action: Operation, message?: string) {
    if (request.current) return;
    const controller = new AbortController();
    request.current = controller;
    let receivedDraft = false;
    const handleEvent = (event: AgentEvent) => {
      if (controller.signal.aborted || request.current !== controller) return;
      if (event.type === 'draft') receivedDraft = true;
      onEvent(event);
    };
    setOperation(action);
    setError(null);
    setGithubError(false);
    setCopyStatus(null);
    setOutcome(null);
    setStatus(action === 'chat' ? 'Connecting to your agent team...' : `Sending ${action} request...`);
    try {
      if (action === 'chat') {
        if (!message) throw new Error('A message is required.');
        await chat(threadId, message, controller.signal, handleEvent);
      } else {
        const result = await confirm(
          threadId,
          action,
          action === 'cancel' ? undefined : { title, body },
          controller.signal,
          handleEvent,
          activeDraftIndex,
        );
        if (controller.signal.aborted || request.current !== controller) return;
        setOutcome({ action, value: result });
        if (action === 'edit') {
          const reviewed = draftReview(result);
          if (reviewed) {
            setDrafts((previous) => previous.map((item, index) =>
              index === reviewed.draft_index
                ? { ...item, ...reviewed, type: 'draft', draft_count: previous.length }
                : item,
            ));
            setTitle(reviewed.title);
            setBody(reviewed.body);
            setActivity((previous) => [
              ...previous.slice(-99),
              ...reviewed.events.map((event) => ({
                ...event,
                id: crypto.randomUUID(),
                time: new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }),
              })),
            ].slice(-100));
          } else if (!receivedDraft) {
            setDrafts((previous) => previous.map((item, index) =>
              index === activeDraftIndex ? { ...item, title, body } : item,
            ));
          }
          setEditing(false);
          addMessage(
            reviewed?.readiness === 'needs_input'
              ? 'The edited draft needs more information before it can be confirmed. Review the quality feedback and edit again.'
              : 'The edited draft passed quality review. Review the updated draft before confirming.',
          );
        } else {
          setDrafts((previous) => previous.map((item, index) =>
            index === activeDraftIndex
              ? { ...item, status: action === 'create' ? 'published' : 'cancelled' }
              : item,
          ));
          setEditing(false);
          addMessage(`${action === 'create' ? 'Create' : 'Cancel'} request completed. The API outcome is shown below.`);
        }
        setStatus(`${action === 'create' ? 'Create' : action === 'cancel' ? 'Cancel' : 'Edit'} request completed`);
      }
    } catch (failure: unknown) {
      if (!controller.signal.aborted) {
        setError(errorMessage(failure));
        setGithubError(action === 'create');
        setStatus('Request failed');
      }
    } finally {
      if (request.current === controller) {
        request.current = null;
        setOperation(null);
      }
    }
  }

  function send() {
    const message = input.trim();
    if (!message || busy) return;
    setMessages((previous) => [...previous, { id: crypto.randomUUID(), role: 'user', text: message }]);
    setInput('');
    void run('chat', message);
  }

  function useSuggestion(text: string) {
    setInput(text);
    composer.current?.focus();
  }

  async function copyDraftMarkdown() {
    try {
      await navigator.clipboard.writeText(`# ${title}\n\n${body}`);
      setCopyStatus('Draft Markdown copied to the clipboard.');
      setError(null);
      setGithubError(false);
    } catch (failure: unknown) {
      setError(`Could not copy the draft Markdown: ${errorMessage(failure)}`);
      setGithubError(false);
    }
  }

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <a className="brand" href="./" aria-label="ADLC Studio home">
          <span className="brand-icon" aria-hidden="true">
            <svg viewBox="0 0 36 36" fill="none">
              <path d="M7 26 17.5 8 29 26M11 20h14" />
              <path d="M21 26h8" />
              <circle cx="27.5" cy="11" r="3.5" />
            </svg>
          </span>
          <span className="brand-name">ADLC <span>Studio</span></span>
        </a>
        <div className="workspace-label">YOUR WORKSPACE</div>
        <div className="nav-item"><span aria-hidden="true">▦</span> Discovery Agent <span className="nav-dot" /></div>
        <div className="agent-nav-list" aria-label="Software delivery agents">
          <div className="agent-nav-item"><span aria-hidden="true">◇</span>Solution Design Agent</div>
          <div className="agent-nav-item"><span aria-hidden="true">⌘</span>Developer Agent</div>
          <div className="agent-nav-item"><span aria-hidden="true">✓</span>QA Agent</div>
          <div className="agent-nav-item"><span aria-hidden="true">↗</span>Release Agent</div>
          <div className="agent-nav-item"><span aria-hidden="true">◎</span>Operations Agent</div>
        </div>
        <div className="sidebar-note"><span className="spark" aria-hidden="true">✦</span><h3>The Autonomous Software Factory.</h3></div>
        <div className="sidebar-bottom"><span className="avatar small">AD</span><div><strong>ADLC Studio</strong><span>Collaborative issue workspace</span></div></div>
      </aside>

      <main className="main">
        <header className="topbar"><div className="breadcrumb">Workspace <span>/</span> <strong>Discovery Agent</strong></div>
          <div className={`connection ${connection}`} title={healthError ?? 'Backend health'}><span className="dot" />{connection === 'checking' ? 'Checking API' : connection === 'online' ? 'API connected' : 'API unavailable'}</div>
        </header>
        <section className="page-heading"><div><div className="eyebrow">AGENTIC SDLC</div><h1 className="brand-headline">Agentic SDLC: Smarter Development. Faster Delivery. Better Outcomes.</h1><p>Talk it through with your agents. Review the draft. Make it happen.</p></div><span className="workspace-badge">ADLC STUDIO <span aria-hidden="true">↗</span></span></section>

        {healthError && <div className="notice warning" role="status"><div><strong>Backend is unavailable.</strong> {healthError}<span className="notice-hint">Start the backend on port 8000. You can still write your message; requests will show any API errors.</span></div><button className="text-button" onClick={() => setHealthAttempt((value) => value + 1)} disabled={connection === 'checking'}>Check again</button></div>}
        {error && <div className="notice error" role="alert"><div><strong>{githubError ? 'GitHub could not create the issue.' : 'Something went wrong.'}</strong> {error}<span className="notice-hint">{githubError ? 'You can retry once, edit the draft, or copy its Markdown.' : 'Your draft is kept for review. Retry the action or send another message.'}</span>{githubError && draft && <div className="notice-actions">{!githubRetryUsed && <button className="text-button" onClick={() => { setGithubRetryUsed(true); void run('create'); }}>Retry once</button>}<button className="text-button" onClick={() => { setEditing(true); setError(null); setGithubError(false); }}>Edit draft</button><button className="text-button" onClick={() => void copyDraftMarkdown()}>Copy Markdown</button></div>}</div><button className="text-button" onClick={() => { setError(null); setGithubError(false); }} aria-label="Dismiss request error">Dismiss</button></div>}
        {copyStatus && <div className="notice success" role="status">{copyStatus}</div>}

        <div className="dashboard">
          <section className="panel conversation">
            <div className="panel-header"><div><span className="section-icon" aria-hidden="true">◌</span><h2>Conversation</h2></div><span className="subtle-label">WITH YOUR AGENT TEAM</span></div>
            <div className="message-area">
              {messages.length === 0 && <div className="welcome"><div className="welcome-icon" aria-hidden="true">✦</div><span className="eyebrow">A GOOD ISSUE STARTS HERE</span><h2>Welcome My Friend!</h2><p>I'm your Discovery &amp; Business Analysis Agent. I can help you define requirements, analyze business needs, create user stories, and shape solutions. How can I assist you today?</p><div className="suggestions">{suggestions.map((suggestion) => <button key={suggestion} onClick={() => useSuggestion(suggestion)}>{suggestion}<span aria-hidden="true">↗</span></button>)}</div></div>}
              {messages.map((message) => <div className={`message ${message.role}`} key={message.id}><span className={`avatar ${message.role === 'assistant' ? 'agent-avatar' : ''}`}>{message.role === 'user' ? 'You' : '✦'}</span><div className="message-content"><div className="message-author">{message.role === 'user' ? 'You' : 'Agent team'}</div><div className="message-text">{message.text}</div>{message.questions && <ul className="questions">{message.questions.map((question, index) => <li key={`${index}-${question}`}>{question}</li>)}</ul>}</div></div>)}
              {busy && <div className="working" role="status"><span className="spinner" />{status}</div>}
              <div ref={bottom} />
            </div>
            {outcome && <details className="outcome" open><summary>{outcome.action === 'create' ? 'Create' : outcome.action === 'edit' ? 'Edit' : 'Cancel'} API outcome</summary><pre>{describe(outcome.value)}</pre></details>}
            <form className="composer" onSubmit={(event) => { event.preventDefault(); send(); }}>
              <label className="sr-only" htmlFor="message">Message your agent team</label>
              <div className="composer-box"><textarea id="message" ref={composer} value={input} onChange={(event) => setInput(event.target.value)} placeholder="Describe what you have in mind..." disabled={busy} rows={3} onKeyDown={(event) => { if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); send(); } }} /><div className="composer-tools"><span>Enter to send <span className="keyboard-hint">· Shift + Enter for a new line</span></span><button className="send-button" disabled={busy || !input.trim()} type="submit" aria-label="Send message">{busy ? <span className="spinner" /> : '↑'}</button></div></div>
              <p className="composer-note"><span aria-hidden="true">◇</span> You're in control. No issue is created until you confirm.</p>
            </form>
          </section>

          <div className="right-column">
            <section className="panel draft-panel"><div className="panel-header"><div><span className="section-icon" aria-hidden="true">▤</span><h2>{drafts.length > 1 ? 'Issue drafts' : 'Issue draft'}</h2></div><span className={`pill ${draft ? 'ready' : ''}`}>{draft ? draft.status === 'published' ? 'Created' : draft.status === 'cancelled' ? 'Cancelled' : 'For review' : 'Awaiting input'}</span></div>
              {drafts.length > 1 && <div className="draft-tabs" role="tablist" aria-label="Issue drafts">{drafts.map((item, index) => <button key={`${index}-${item.title}`} className={`button secondary ${index === activeDraftIndex ? 'active' : ''}`} role="tab" aria-selected={index === activeDraftIndex} disabled={busy} onClick={() => { setActiveDraftIndex(index); setTitle(item.title); setBody(item.body); setEditing(false); }}>Issue {index + 1}{item.status === 'published' ? ' · Created' : item.status === 'cancelled' ? ' · Cancelled' : ''}</button>)}</div>}
              {!draft ? <div className="empty-draft"><div className="document-art" aria-hidden="true"><span /><span /><span /><span /></div><h3>A home for your next issue</h3><p>As your conversation takes shape,<br />your draft will appear here.</p><div className="draft-steps"><span>01 &nbsp; Describe</span><span>02 &nbsp; Refine</span><span>03 &nbsp; Confirm</span></div></div> : <div className="draft-content">
                <div className="draft-meta"><span className="pill">{draft.issue_type}</span><span className="readiness">Readiness: {describe(draft.readiness).replaceAll('_', ' ')}</span></div>
                {editing ? <div className="draft-fields"><label htmlFor="draft-title">Title</label><input id="draft-title" value={title} onChange={(event) => setTitle(event.target.value)} disabled={busy} /><label htmlFor="draft-body">Description</label><textarea id="draft-body" value={body} onChange={(event) => setBody(event.target.value)} rows={10} disabled={busy} /></div> : <><h3 className="draft-title">{draft.title}</h3><div className="draft-body">{draft.body}</div></>}
                {draft.questions && draft.questions.length > 0 && <div className="draft-feedback"><strong>{draft.readiness === 'needs_input' ? 'Required before confirmation' : 'Open questions'}</strong><ul className="questions">{draft.questions.map((question, index) => <li key={`${index}-${question}`}>{question}</li>)}</ul></div>}
                {draft.similar_issues.length > 0 && <div className="similar-issues"><h4>Similar issues</h4>{draft.similar_issues.map((issue) => <a key={`${issue.number}-${issue.url}`} href={safeUrl(issue.url)} target="_blank" rel="noreferrer"><span>#{issue.number} · {issue.state}</span><strong>{issue.title}</strong><span aria-hidden="true">↗</span></a>)}</div>}
                <div className="draft-actions">{editing ? <><button className="button secondary" disabled={busy} onClick={() => { setEditing(false); setTitle(draft.title); setBody(draft.body); }}>Discard edits</button><button className="button primary" disabled={busy || !title.trim() || !body.trim()} onClick={() => void run('edit')}>{operation === 'edit' ? 'Reviewing...' : 'Save & review'}</button></> : draft.status === 'published' || draft.status === 'cancelled' ? <p className="draft-footnote">This issue draft has already been {draft.status === 'published' ? 'created' : 'cancelled'}.</p> : <><button className="button secondary" disabled={busy} onClick={() => setEditing(true)}>Edit</button><button className="button secondary" disabled={busy} onClick={() => void run('cancel')}>{operation === 'cancel' ? 'Canceling...' : 'Cancel'}</button><button className="button primary" disabled={busy || draft.readiness === 'needs_input' || !title.trim() || !body.trim()} onClick={() => void run('create')}>{operation === 'create' ? 'Creating...' : 'Confirm Create'}</button></>}</div>
                <p className="draft-footnote">Prefer to talk it through? Send your changes in the conversation.</p>
              </div>}
            </section>
            <section className="panel activity-panel"><div className="panel-header"><div><span className={`dot ${busy ? 'live-dot' : ''}`} /><h2>Agent activity</h2></div><span className="subtle-label">{busy ? 'LIVE' : 'EVENT STREAM'}</span></div><div className="activity-list" role="log" aria-label="Realtime agent events" aria-live="polite" aria-relevant="additions">{activity.length === 0 ? <div className="empty-activity"><span className="activity-orbit" aria-hidden="true">⌁</span><div><strong>All quiet, for now.</strong><p>Live status and agent logs will appear here.</p></div></div> : activity.map((item) => <div className={`activity-item ${item.level.toLowerCase()}`} key={item.id}><div className="activity-heading"><strong>{item.agent}</strong><time>{item.time}</time></div><p>{item.message}</p><span className="log-level">{item.level}</span></div>)}</div><div className="activity-footer" role="status"><span className={`dot ${busy ? 'live-dot' : ''}`} /><span>{status}</span>{busy && <button className="text-button" onClick={() => { request.current?.abort(); setStatus('Request stopped locally'); setError('Request stopped locally. The backend may still be processing it; verify its outcome before retrying creation.'); }}>Stop</button>}</div></section>
          </div>
        </div>
        <footer className="page-footer"><span>Built for thoughtful collaboration.</span><span>Thread <code>{threadId.slice(0, 8)}</code></span></footer>
      </main>
    </div>
  );
}
