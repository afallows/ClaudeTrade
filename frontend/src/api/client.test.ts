import { describe, it, expect, vi, afterEach } from 'vitest';
import { api, ApiError } from './client';

describe('api client', () => {
  const originalFetch = globalThis.fetch;

  afterEach(() => {
    globalThis.fetch = originalFetch;
    vi.restoreAllMocks();
  });

  it('listSignals GETs /api/signals with query params for every filter', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ signals: [], total: 0 }), { status: 200 }),
    );
    globalThis.fetch = fetchMock as unknown as typeof fetch;

    const result = await api.listSignals({
      direction: ['long', 'short'],
      min_score: 50,
      min_confidence: 0.5,
      strategy: ['sentiment_breakout'],
      max_days_to_earnings: 10,
      limit: 100,
    });

    expect(result).toEqual({ signals: [], total: 0 });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toContain('/api/signals?');
    expect(String(url)).toContain('direction=long');
    expect(String(url)).toContain('direction=short');
    expect(String(url)).toContain('min_score=50');
    expect(String(url)).toContain('min_confidence=0.5');
    expect(String(url)).toContain('strategy=sentiment_breakout');
    expect(String(url)).toContain('max_days_to_earnings=10');
    expect(String(url)).toContain('limit=100');
    expect(init.headers.Accept).toBe('application/json');
  });

  it('listSignals with no filters hits the bare endpoint', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(new Response(JSON.stringify({ signals: [], total: 0 }), { status: 200 }));
    globalThis.fetch = fetchMock as unknown as typeof fetch;

    await api.listSignals();
    expect(fetchMock.mock.calls[0][0]).toBe('/api/signals');
  });

  it('runScan POSTs a JSON body with the right content type', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({ session: '2024-01-01', evaluated_symbols: 0, signal_count: 0, rejected_count: 0, warnings: [] }),
        { status: 200 },
      ),
    );
    globalThis.fetch = fetchMock as unknown as typeof fetch;

    await api.runScan({ generate_thesis: true });
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe('/api/scan');
    expect(init.method).toBe('POST');
    expect(init.headers['Content-Type']).toBe('application/json');
    expect(JSON.parse(init.body as string)).toEqual({ generate_thesis: true });
  });

  it('paperOpen POSTs the signal id', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          accepted: true,
          status: 'filled',
          order_id: 'ord-1',
          symbol: 'AAA',
          direction: 'long',
          requested_shares: 10,
          filled_shares: 10,
          fill_price: 25.0,
          fill_session: '2024-01-04',
          reasons: [],
          message: 'Filled',
        }),
        { status: 200 },
      ),
    );
    globalThis.fetch = fetchMock as unknown as typeof fetch;

    const result = await api.paperOpen('sig-123');
    expect(result.status).toBe('filled');
    const [, init] = fetchMock.mock.calls[0];
    expect(JSON.parse(init.body as string)).toEqual({ signal_id: 'sig-123' });
  });

  it('throws ApiError with the server-provided detail on a non-2xx response', async () => {
    const fetchMock = vi
      .fn()
      .mockImplementation(
        async () => new Response(JSON.stringify({ detail: 'unknown signal x' }), { status: 404 }),
      );
    globalThis.fetch = fetchMock as unknown as typeof fetch;

    let caught: unknown;
    try {
      await api.getSignal('x');
    } catch (e) {
      caught = e;
    }
    expect(caught).toBeInstanceOf(ApiError);
    expect((caught as ApiError).status).toBe(404);
    expect((caught as ApiError).message).toBe('unknown signal x');
  });

  it('falls back to statusText when an error response is not JSON', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(new Response('not json', { status: 500, statusText: 'Internal Server Error' }));
    globalThis.fetch = fetchMock as unknown as typeof fetch;

    await expect(api.dashboard()).rejects.toMatchObject({ status: 500, message: 'Internal Server Error' });
  });
});
