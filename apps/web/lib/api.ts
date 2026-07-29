/**
 * Typed client for the Lexora API.
 *
 * The streaming path parses Server-Sent Events by hand rather than using
 * `EventSource`, for one reason: `EventSource` can only issue GET requests, and the
 * question, conversation history and law filter belong in a POST body. The parser
 * below is a small state machine over the response stream — it buffers until a blank
 * line terminates an event, which is the one detail a naive `split("\n\n")` gets wrong
 * when a chunk boundary lands mid-event.
 */

export const API_URL = (
  process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:7861"
).replace(/\/+$/, "");

export type AnswerKind = "answer" | "refusal" | "blocked";
export type CitationStatus = "verified" | "unsupported";

export interface ChunkView {
  chunk_id: string;
  law_id: string;
  law_label: string;
  law_title: string;
  part_title: string;
  article_no: number;
  article_title: string;
  section: string;
  seq: number;
  seq_total: number;
  page_start: number;
  page_end: number;
  text: string;
  heading: string;
  citation_key: string;
  source: string;
  dense_rank: number | null;
  dense_score: number | null;
  sparse_rank: number | null;
  sparse_score: number | null;
  rrf_score: number | null;
  fused_rank: number | null;
  rerank_score: number | null;
  rerank_probability: number | null;
  final_rank: number | null;
}

export interface CitationView {
  raw: string;
  law_label: string;
  article_no: number;
  status: CitationStatus;
  chunk_id: string | null;
  citation_key: string | null;
  start: number;
  end: number;
}

export interface VerificationView {
  passed: boolean;
  verified_count: number;
  unsupported_count: number;
  uncited_sentences: string[];
}

export interface GateView {
  decision: "allow" | "block";
  search_query: string;
  rewritten: boolean;
  reason: string;
  signals: string[];
  engine: string;
  latency_ms: number;
}

export interface TimingView {
  gate_ms: number;
  retrieval_ms: number;
  rerank_ms: number;
  generation_ms: number;
  verify_ms: number;
  total_ms: number;
}

export interface AnswerView {
  kind: AnswerKind;
  text: string;
  engine: string;
  citations: CitationView[];
  evidence: ChunkView[];
  near_misses: ChunkView[];
  verification: VerificationView | null;
  gate: GateView;
  timings: TimingView;
  usage: { input_tokens: number; output_tokens: number; usd: number };
  retrieval: {
    candidates_considered: number;
    dense_hits: number;
    sparse_hits: number;
    overlap: number;
  };
}

export interface HealthView {
  status: "ok" | "degraded";
  version: string;
  engine: string;
  chunks_indexed: number;
  vector_points: number;
  laws: number;
  reranker: string;
  embedding_model: string;
  langfuse: boolean;
  detail: string;
}

export interface LawView {
  law_id: string;
  label: string;
  title: string;
  jurisdiction: string;
  publisher: string;
  official_url: string;
  article_count: number;
  chunk_count: number;
}

export interface RetrievalEvent {
  candidates: number;
  dense_hits: number;
  sparse_hits: number;
  overlap: number;
  covered: boolean;
  best_score: number | null;
  floor: number;
  reranked: boolean;
}

export type StreamEvent =
  | { type: "gate"; data: Omit<GateView, "latency_ms" | "reason"> }
  | { type: "retrieval"; data: RetrievalEvent }
  | { type: "token"; data: { text: string } }
  | { type: "final"; data: AnswerView }
  | { type: "error"; data: { detail: string; request_id?: string } };

export interface Turn {
  role: "user" | "assistant";
  content: string;
}

async function getJson<T>(path: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(`${API_URL}${path}`, {
    signal: signal ?? null,
    headers: { Accept: "application/json" },
  });
  if (!response.ok) {
    throw new Error(`${path} responded ${response.status}`);
  }
  return (await response.json()) as T;
}

export const getHealth = (signal?: AbortSignal) => getJson<HealthView>("/api/health", signal);
export const getLaws = (signal?: AbortSignal) => getJson<LawView[]>("/api/laws", signal);
export const getMetrics = (signal?: AbortSignal) =>
  getJson<Record<string, unknown>>("/api/metrics", signal);

interface AskOptions {
  question: string;
  history: Turn[];
  lawId: string | null;
  rerank: boolean;
  signal: AbortSignal;
  onEvent: (event: StreamEvent) => void;
}

/**
 * POST the question and dispatch each SSE event as it arrives.
 *
 * Resolves when the stream closes. Errors from the transport are surfaced as an
 * `error` event rather than thrown, so a caller only has to handle one failure path.
 */
export async function askStream({
  question,
  history,
  lawId,
  rerank,
  signal,
  onEvent,
}: AskOptions): Promise<void> {
  let response: Response;
  try {
    response = await fetch(`${API_URL}/api/ask/stream`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question, history, law_id: lawId, rerank }),
      signal,
    });
  } catch {
    // An aborted request is the user pressing Stop, not a failure to report.
    if (signal.aborted) return;
    onEvent({
      type: "error",
      data: { detail: `Cannot reach the Lexora API at ${API_URL}. Is it running?` },
    });
    return;
  }

  if (!response.ok || !response.body) {
    const detail =
      response.status === 429
        ? "Rate limit reached — Lexora allows 10 questions per minute per IP."
        : response.status === 503
          ? "The index is still loading. Try again in a moment."
          : `The API responded ${response.status}.`;
    onEvent({ type: "error", data: { detail } });
    return;
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      // An SSE event ends at a blank line. Anything after the last one is a partial
      // event and must stay in the buffer until more bytes arrive.
      let boundary = buffer.indexOf("\n\n");
      while (boundary !== -1) {
        const raw = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        const parsed = parseEvent(raw);
        if (parsed) onEvent(parsed);
        boundary = buffer.indexOf("\n\n");
      }
    }
  } catch {
    if (!signal.aborted) {
      onEvent({ type: "error", data: { detail: "The connection dropped mid-answer." } });
    }
  } finally {
    reader.releaseLock();
  }
}

function parseEvent(raw: string): StreamEvent | null {
  let name = "";
  const dataLines: string[] = [];
  for (const line of raw.split("\n")) {
    if (line.startsWith("event:")) name = line.slice(6).trim();
    else if (line.startsWith("data:")) dataLines.push(line.slice(5).trimStart());
  }
  if (!name || dataLines.length === 0) return null;
  try {
    const data = JSON.parse(dataLines.join("\n")) as unknown;
    return { type: name, data } as StreamEvent;
  } catch {
    return null;
  }
}
