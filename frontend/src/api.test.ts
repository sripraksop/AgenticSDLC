import { afterEach, describe, expect, it, vi } from 'vitest';
import { chat, confirm, health, parseEvent, readEvents, safeUrl, type AgentEvent } from './api';

const draft = {
  type: 'draft', draft_index: 0, draft_count: 1,
  title: 'Fix login', body: 'Steps to reproduce\nExpected behavior',
  readiness: 0.9, issue_type: 'bug',
  similar_issues: [{ number: 42, title: 'Login error', state: 'open', url: 'https://example.com/issues/42' }],
};

function stream(chunks: string[] | Uint8Array[]): Response {
  const encoder = new TextEncoder();
  return new Response(new ReadableStream<Uint8Array>({
    start(controller) {
      chunks.forEach((chunk) => controller.enqueue(typeof chunk === 'string' ? encoder.encode(chunk) : chunk));
      controller.close();
    },
  }), { headers: { 'Content-Type': 'text/event-stream; charset=utf-8' } });
}

function json(value: unknown, status = 200) {
  return new Response(JSON.stringify(value), { status, headers: { 'Content-Type': 'application/json' } });
}

afterEach(() => vi.unstubAllGlobals());

describe('event stream', () => {
  it('streams every contracted event type across split CRLF frames and ignores comments', async () => {
    const events: AgentEvent[] = [];
    await readEvents(stream([
      ': keepalive\r', '\nevent: update\r\ndata: {"type":"status","message":"Starting"}\r', '\n\r',
      '\ndata: {"type":"log","agent":"Planner","message":"Investigating","level":"info"}\n\n',
      `data: ${JSON.stringify(draft)}\r\r`,
      'data: {"type":"question","message":"More detail?",\ndata: "questions":["Which browser?"]}\n\n',
      'data: {"type":"done","status":"completed"}\n\n',
    ]), (event) => events.push(event));
    expect(events.map((event) => event.type)).toEqual(['status', 'log', 'draft', 'question', 'done']);
    expect(events[2]).toEqual(draft);
  });

  it('decodes UTF-8 even when each byte arrives separately and flushes the last frame', async () => {
    const bytes = new TextEncoder().encode('data: {"type":"status","message":"café ✦"}\n\ndata: {"type":"done","status":"ok"}');
    const events: AgentEvent[] = [];
    await readEvents(stream(Array.from(bytes, (byte) => Uint8Array.of(byte))), (event) => events.push(event));
    expect(events[0]).toEqual({ type: 'status', message: 'café ✦' });
    expect(events[1]).toEqual({ type: 'done', status: 'ok' });
  });

  it('delivers events before the connection finishes and cancels after done', async () => {
    let source: ReadableStreamDefaultController<Uint8Array> | undefined;
    const canceled = vi.fn();
    const response = new Response(new ReadableStream<Uint8Array>({
      start(controller) { source = controller; },
      cancel: canceled,
    }), { headers: { 'Content-Type': 'text/event-stream' } });
    const received = vi.fn();
    const reading = readEvents(response, received);
    source!.enqueue(new TextEncoder().encode('data: {"type":"status","message":"Live now"}\n\n'));
    await vi.waitFor(() => expect(received).toHaveBeenCalledWith({ type: 'status', message: 'Live now' }));
    source!.enqueue(new TextEncoder().encode('data: {"type":"done","status":"ok"}\n\n'));
    await reading;
    expect(canceled).toHaveBeenCalledOnce();
  });

  it('surfaces an error event even if a done event follows', async () => {
    const received = vi.fn();
    await expect(readEvents(stream([
      'data: {"type":"error","message":"Rate limited"}\n\n',
      'data: {"type":"done","status":"error"}\n\n',
    ]), received)).rejects.toThrow('Rate limited');
    expect(received).toHaveBeenCalledWith({ type: 'error', message: 'Rate limited' });
  });

  it('does not mistake an interrupted stream for success', async () => {
    await expect(readEvents(stream(['data: {"type":"status","message":"Working"}\n\n']), vi.fn()))
      .rejects.toThrow('before a done event');
  });

  it('rejects failed completion even without an error event', async () => {
    await expect(readEvents(stream(['data: {"type":"done","status":"failed"}\n\n']), vi.fn()))
      .rejects.toThrow('status: failed');
  });

  it('rejects malformed JSON, invalid known payloads, and unexpected content types', async () => {
    await expect(readEvents(stream(['data: {broken}\n\n']), vi.fn())).rejects.toThrow();
    expect(() => parseEvent('{"type":"question","message":"Question","questions":[12]}')).toThrow('invalid');
    expect(() => parseEvent(JSON.stringify({ ...draft, similar_issues: [{}] }))).toThrow('invalid');
    await expect(readEvents(json({ message: 'Not SSE' }), vi.fn())).rejects.toThrow('Expected an event stream');
  });

  it('ignores future event types without dropping known events', async () => {
    const received = vi.fn();
    await readEvents(stream(['data: {"type":"future","payload":123}\n\ndata: {"type":"done","status":"ok"}\n\n']), received);
    expect(received).toHaveBeenCalledOnce();
  });
});

describe('API routes', () => {
  it('posts chat to the relative route with the thread, message and abort signal', async () => {
    const fetchMock = vi.fn().mockResolvedValue(stream(['data: {"type":"done","status":"ok"}\n\n']));
    vi.stubGlobal('fetch', fetchMock);
    const controller = new AbortController();
    await chat('thread-1', 'Please investigate', controller.signal, vi.fn());
    expect(fetchMock).toHaveBeenCalledWith('/api/chat', expect.objectContaining({
      method: 'POST', signal: controller.signal,
      body: JSON.stringify({ thread_id: 'thread-1', message: 'Please investigate' }),
    }));
  });

  it.each(['create', 'edit'] as const)('posts %s with edited fields and preserves JSON outcomes', async (action) => {
    const outcome = { status: 'ok', issue: { number: 45, url: 'https://example.com/issues/45' } };
    const fetchMock = vi.fn().mockResolvedValue(json(outcome));
    vi.stubGlobal('fetch', fetchMock);
    expect(await confirm('thread-1', action, { title: 'New title', body: 'New body' }, new AbortController().signal, vi.fn())).toEqual(outcome);
    expect(fetchMock).toHaveBeenCalledWith('/api/confirm', expect.objectContaining({
      body: JSON.stringify({ thread_id: 'thread-1', action, title: 'New title', body: 'New body' }),
    }));
  });

  it('posts cancel without draft fields', async () => {
    const fetchMock = vi.fn().mockResolvedValue(json({ status: 'canceled' }));
    vi.stubGlobal('fetch', fetchMock);
    await confirm('thread-1', 'cancel', undefined, new AbortController().signal, vi.fn());
    expect(fetchMock).toHaveBeenCalledWith('/api/confirm', expect.objectContaining({
      body: JSON.stringify({ thread_id: 'thread-1', action: 'cancel' }),
    }));
  });

  it('targets the selected draft when confirming a later issue', async () => {
    const fetchMock = vi.fn().mockResolvedValue(json({ status: 'created' }));
    vi.stubGlobal('fetch', fetchMock);
    await confirm('thread-1', 'create', undefined, new AbortController().signal, vi.fn(), 2);
    expect(fetchMock).toHaveBeenCalledWith('/api/confirm', expect.objectContaining({
      body: JSON.stringify({ thread_id: 'thread-1', action: 'create', draft_index: 2 }),
    }));
  });

  it('handles an edit SSE response with a revised draft', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(stream([
      `data: ${JSON.stringify(draft)}\n\ndata: {"type":"done","status":"ok"}\n\n`,
    ])));
    const received = vi.fn();
    await confirm('thread-1', 'edit', { title: 'Input', body: 'Input' }, new AbortController().signal, received);
    expect(received).toHaveBeenCalledWith(draft);
  });

  it('surfaces HTTP errors, JSON failure outcomes and network failures', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(json({ detail: 'Service unavailable' }, 503))
      .mockResolvedValueOnce(json({ success: false, message: 'Creation denied' }))
      .mockRejectedValueOnce(new TypeError('Failed to fetch'));
    vi.stubGlobal('fetch', fetchMock);
    const signal = new AbortController().signal;
    await expect(chat('t', 'm', signal, vi.fn())).rejects.toThrow('API 503: Service unavailable');
    await expect(confirm('t', 'create', undefined, signal, vi.fn())).rejects.toThrow('Creation denied');
    await expect(health(signal)).rejects.toThrow('Failed to fetch');
  });

  it('checks health through a relative URL', async () => {
    const fetchMock = vi.fn().mockResolvedValue(json({ status: 'ok' }));
    vi.stubGlobal('fetch', fetchMock);
    expect(await health(new AbortController().signal)).toEqual({ status: 'ok' });
    expect(fetchMock).toHaveBeenCalledWith('/api/health', expect.objectContaining({ signal: expect.any(AbortSignal) }));
  });
});

describe('issue links', () => {
  it('accepts only absolute HTTP(S) URLs', () => {
    expect(safeUrl('https://example.com/issues/42')).toBe('https://example.com/issues/42');
    expect(safeUrl('javascript:alert(1)')).toBeUndefined();
    expect(safeUrl('/issues/42')).toBeUndefined();
    expect(safeUrl('not a URL')).toBeUndefined();
  });
});
