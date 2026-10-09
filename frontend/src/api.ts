export interface SimilarIssue {
  number: number;
  title: string;
  state: string;
  url: string;
}

export interface Draft {
  type: 'draft';
  draft_index: number;
  draft_count: number;
  status?: string;
  title: string;
  body: string;
  readiness: unknown;
  issue_type: string;
  similar_issues: SimilarIssue[];
  questions?: string[];
}

export interface DraftReview {
  draft_index: number;
  status?: string;
  title: string;
  body: string;
  readiness: string;
  issue_type: string;
  similar_issues: SimilarIssue[];
  questions: string[];
  events: { agent: string; message: string; level: string }[];
}

export type AgentEvent =
  | { type: 'status'; message: string }
  | { type: 'log'; agent: string; message: string; level: string }
  | ({ type: 'draft' } & Draft)
  | { type: 'question'; message: string; questions: string[] }
  | { type: 'error'; message: string }
  | { type: 'done'; status: string };

function record(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function stringList(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((item: unknown) => typeof item === 'string');
}

function similarIssue(value: unknown): value is SimilarIssue {
  return record(value) && typeof value.number === 'number'
    && typeof value.title === 'string' && typeof value.state === 'string'
    && typeof value.url === 'string';
}

export function draftReview(value: unknown): DraftReview | null {
  if (!record(value) || typeof value.title !== 'string' || typeof value.body !== 'string'
    || typeof value.readiness !== 'string' || typeof value.issue_type !== 'string'
    || !Array.isArray(value.similar_issues) || !value.similar_issues.every(similarIssue)
    || !stringList(value.questions) || !Array.isArray(value.events)) return null;
  const events = value.events.filter((event: unknown): event is { agent: string; message: string; level: string } =>
    record(event) && typeof event.agent === 'string'
      && typeof event.message === 'string' && typeof event.level === 'string',
  );
  return {
    draft_index: typeof value.draft_index === 'number' ? value.draft_index : 0,
    ...(typeof value.status === 'string' ? { status: value.status } : {}),
    title: value.title,
    body: value.body,
    readiness: value.readiness,
    issue_type: value.issue_type,
    similar_issues: value.similar_issues,
    questions: value.questions,
    events,
  };
}

export function parseEvent(data: string): AgentEvent | null {
  const value: unknown = JSON.parse(data);
  if (!record(value) || typeof value.type !== 'string') {
    throw new Error('The API sent an invalid event.');
  }
  switch (value.type) {
    case 'status':
    case 'error':
      if (typeof value.message === 'string') return { type: value.type, message: value.message };
      break;
    case 'log':
      if (typeof value.agent === 'string' && typeof value.message === 'string' && typeof value.level === 'string') {
        return { type: 'log', agent: value.agent, message: value.message, level: value.level };
      }
      break;
    case 'question':
      if (typeof value.message === 'string' && stringList(value.questions)) {
        return { type: 'question', message: value.message, questions: value.questions };
      }
      break;
    case 'done':
      if (typeof value.status === 'string') return { type: 'done', status: value.status };
      break;
    case 'draft':
      if (typeof value.title === 'string' && typeof value.body === 'string'
        && typeof value.issue_type === 'string' && Array.isArray(value.similar_issues)
        && value.similar_issues.every(similarIssue)) {
        return {
          type: 'draft',
          draft_index: typeof value.draft_index === 'number' ? value.draft_index : 0,
          draft_count: typeof value.draft_count === 'number' ? value.draft_count : 1,
          ...(typeof value.status === 'string' ? { status: value.status } : {}),
          title: value.title, body: value.body, readiness: value.readiness,
          issue_type: value.issue_type, similar_issues: value.similar_issues,
          ...(stringList(value.questions) ? { questions: value.questions } : {}),
        };
      }
      break;
    default:
      // Unknown event types are ignored so additions to the protocol remain compatible.
      return null;
  }
  throw new Error(`The API sent an invalid "${value.type}" event.`);
}

export function outcomeError(value: unknown): string | null {
  if (!record(value)) return null;
  if (value.error || value.success === false || value.status === 'error' || value.status === 'failed') {
    if (typeof value.message === 'string') return value.message;
    if (typeof value.error === 'string') return value.error;
    return 'The API reported that the request failed.';
  }
  return null;
}

async function checked(response: Response): Promise<Response> {
  if (response.ok) return response;
  const text = await response.text();
  let detail = text.slice(0, 500);
  if (response.headers.get('content-type')?.includes('json')) {
    const value: unknown = JSON.parse(text);
    if (record(value)) {
      const message = value.message ?? value.detail ?? value.error;
      detail = typeof message === 'string' ? message : JSON.stringify(message ?? value);
    }
  }
  throw new Error(`API ${response.status}${detail ? `: ${detail}` : ` ${response.statusText}`}`);
}

export async function readEvents(response: Response, onEvent: (event: AgentEvent) => void): Promise<void> {
  await checked(response);
  if (!response.headers.get('content-type')?.includes('text/event-stream')) {
    throw new Error('Expected an event stream from the API.');
  }
  if (!response.body) throw new Error('The API response has no event stream.');
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let data: string[] = [];
  let done = false;
  let streamError: string | null = null;

  const dispatch = () => {
    if (!data.length) return;
    const event = parseEvent(data.join('\n'));
    data = [];
    if (!event) return;
    onEvent(event);
    if (event.type === 'error') streamError = event.message;
    if (event.type === 'done') {
      done = true;
      if (event.status === 'error' || event.status === 'failed') {
        streamError ??= `Agent request ended with status: ${event.status}`;
      }
    }
  };
  const line = (value: string) => {
    if (value === '') dispatch();
    else if (value.startsWith('data:')) data.push(value.slice(5).replace(/^ /, ''));
  };
  try {
    while (!done) {
      const chunk = await reader.read();
      buffer += decoder.decode(chunk.value, { stream: !chunk.done });
      // Hold a trailing CR until the next chunk, in case a CRLF pair was split.
      let match: RegExpExecArray | null;
      while ((match = /\r\n|\r|\n/.exec(buffer))) {
        if (!chunk.done && match[0] === '\r' && match.index === buffer.length - 1) break;
        line(buffer.slice(0, match.index));
        buffer = buffer.slice(match.index + match[0].length);
        if (done) break;
      }
      if (chunk.done) {
        if (buffer) line(buffer);
        dispatch();
        break;
      }
    }
    if (streamError) throw new Error(streamError);
    if (!done) throw new Error('The event stream ended before a done event. Please retry.');
  } finally {
    try {
      await reader.cancel();
    } finally {
      reader.releaseLock();
    }
  }
}

export async function chat(threadId: string, message: string, signal: AbortSignal, onEvent: (event: AgentEvent) => void) {
  const response = await fetch('/api/chat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
    body: JSON.stringify({ thread_id: threadId, message }),
    signal,
  });
  await readEvents(response, onEvent);
}

export async function confirm(
  threadId: string,
  action: 'create' | 'cancel' | 'edit',
  fields: { title: string; body: string } | undefined,
  signal: AbortSignal,
  onEvent: (event: AgentEvent) => void,
  draftIndex = 0,
): Promise<unknown> {
  const response = await checked(await fetch('/api/confirm', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'application/json, text/event-stream' },
    body: JSON.stringify({
      thread_id: threadId,
      action,
      ...(draftIndex > 0 ? { draft_index: draftIndex } : {}),
      ...fields,
    }),
    signal,
  }));
  if (response.headers.get('content-type')?.includes('text/event-stream')) {
    await readEvents(response, onEvent);
    return { status: 'Stream completed' };
  }
  const outcome: unknown = await response.json();
  const error = outcomeError(outcome);
  if (error) throw new Error(error);
  return outcome;
}

export async function health(signal: AbortSignal): Promise<unknown> {
  const response = await checked(await fetch('/api/health', {
    signal: AbortSignal.any([signal, AbortSignal.timeout(8000)]),
  }));
  const result: unknown = await response.json();
  const error = outcomeError(result);
  if (error) throw new Error(error);
  return result;
}

export function safeUrl(url: string): string | undefined {
  try {
    const parsed = new URL(url);
    return ['https:', 'http:'].includes(parsed.protocol) ? parsed.href : undefined;
  } catch {
    return undefined;
  }
}
