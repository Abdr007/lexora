"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { getMetrics } from "@/lib/api";
import { cn } from "@/lib/cn";

/**
 * /metrics — the dashboard of the project's own rigour.
 *
 * Everything here is read from `eval/results/latest.json`, written by
 * `eval/ragas_run.py`. Nothing is hardcoded, and when a run has not happened the page
 * says so rather than showing a plausible-looking zero. Metrics that require an LLM
 * judge are rendered as "pending" until a run with an API key produces them, so a
 * retrieval number measured offline can never be mistaken for a faithfulness score.
 */

interface MetricSet {
  hit_rate_at_1?: number;
  hit_rate_at_5?: number;
  mrr?: number;
  context_precision?: number;
  context_recall?: number;
  refusal_accuracy?: number;
  faithfulness?: number | null;
  answer_relevancy?: number | null;
}

interface ChunkingRow {
  target_tokens: number;
  chunks: number;
  tokens_mean: number;
  over_window: number;
  hit_rate_at_5: number;
  hit_rate_at_1: number;
}

interface MetricsPayload {
  available: boolean;
  detail?: string;
  generated_at?: string;
  engine?: string;
  judge_model?: string | null;
  dataset?: { questions: number; answerable: number; traps: number };
  configurations?: Record<string, MetricSet>;
  chunking_experiment?: ChunkingRow[];
  latency_ms?: Record<string, number>;
  refusal_calibration?: {
    floor: number;
    separation: number;
    answerable_min: number;
    trap_max: number;
  };
  reranker?: string;
}

const ROWS: { key: keyof MetricSet; label: string; hint: string; judged?: boolean }[] = [
  { key: "hit_rate_at_5", label: "Hit-rate@5", hint: "labelled source article appears in the top 5" },
  { key: "hit_rate_at_1", label: "Hit-rate@1", hint: "labelled source article ranks first" },
  { key: "mrr", label: "MRR", hint: "mean reciprocal rank of the labelled source" },
  { key: "context_precision", label: "Context precision", hint: "share of returned passages that are relevant" },
  { key: "context_recall", label: "Context recall", hint: "share of labelled sources retrieved" },
  { key: "refusal_accuracy", label: "Refusal accuracy", hint: "trap questions correctly declined" },
  { key: "faithfulness", label: "Faithfulness", hint: "RAGAS, Claude as judge", judged: true },
  { key: "answer_relevancy", label: "Answer relevance", hint: "RAGAS, Claude as judge", judged: true },
];

export default function MetricsPage() {
  const [data, setData] = useState<MetricsPayload | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    getMetrics(controller.signal)
      .then((payload) => setData(payload as unknown as MetricsPayload))
      .catch(() => setFailed(true));
    return () => controller.abort();
  }, []);

  const withRerank = data?.configurations?.with_rerank;
  const noRerank = data?.configurations?.no_rerank;

  return (
    <div className="mx-auto max-w-4xl px-5 pb-20 sm:px-8">
      <header className="masthead-rule mt-3 flex items-center justify-between border-b border-rule pb-2.5 pt-3">
        <Link href="/" className="display text-[1.6rem] tracking-[0.02em] text-ink">
          LEXORA
        </Link>
        <Link
          href="/"
          className="marginal underline-offset-4 hover:underline"
        >
          Back
        </Link>
      </header>

      <div className="pt-10">
        <p className="marginal mb-3">Evaluation</p>
        <h1 className="display max-w-[20ch] text-[clamp(2.1rem,5vw,3.4rem)] text-ink">
          What reranking is actually worth.
        </h1>
        <p className="statute mt-5 max-w-[58ch] text-ink-soft">
          Both configurations run the same
          {" "}questions against the same index. The only difference is whether the
          cross-encoder reranks the fused candidates — so the delta is the reranker&rsquo;s
          contribution, measured rather than asserted.
        </p>
      </div>

      {failed ? (
        <Notice tone="rose">
          Could not reach the Lexora API. Start it with <Code>make api</Code>.
        </Notice>
      ) : !data ? (
        <Notice tone="muted">Loading…</Notice>
      ) : !data.available ? (
        <Notice tone="ochre">
          {data.detail ?? "No evaluation has been recorded yet."} Run <Code>make eval</Code> to produce
          <Code>eval/results/latest.json</Code>.
        </Notice>
      ) : (
        <>
          <dl className="mt-8 grid grid-cols-2 gap-3 sm:grid-cols-4">
            <Stat label="Questions" value={data.dataset?.questions ?? 0} />
            <Stat label="Answerable" value={data.dataset?.answerable ?? 0} />
            <Stat label="Trap questions" value={data.dataset?.traps ?? 0} />
            <Stat
              label="Engine"
              value={data.engine === "anthropic" ? "Claude" : "extractive"}
              mono
            />
          </dl>

          <section className="mt-10">
            <h2 className="marginal mb-3">Before and after reranking</h2>
            <div className="overflow-x-auto border-y border-rule">
              <table className="w-full min-w-[34rem] border-collapse text-left">
                <thead>
                  <tr className="border-b border-ink">
                    <th className="marginal px-3 py-2 text-left">
                      Metric
                    </th>
                    <th className="marginal px-3 py-2 text-right">
                      No rerank
                    </th>
                    <th className="marginal px-3 py-2 text-right">
                      With rerank
                    </th>
                    <th className="marginal px-3 py-2 text-right">
                      Δ
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {ROWS.map((row) => {
                    const before = noRerank?.[row.key];
                    const after = withRerank?.[row.key];
                    const pending =
                      row.judged && (after === null || after === undefined);
                    const delta =
                      typeof before === "number" && typeof after === "number"
                        ? after - before
                        : null;
                    return (
                      <tr key={row.key} className="border-b border-rule last:border-0">
                        <td className="px-3 py-2">
                          <span className="text-[0.92rem] font-medium text-ink">{row.label}</span>
                          <span className="ml-2 text-[0.8rem] text-ink-faint">{row.hint}</span>
                        </td>
                        <td className="instrument px-3 py-2 text-right text-ink-soft">
                          {pending ? "—" : fmt(before)}
                        </td>
                        <td className="instrument px-3 py-2 text-right text-ink">
                          {pending ? (
                            <span className="text-ochre">pending key</span>
                          ) : (
                            fmt(after)
                          )}
                        </td>
                        <td
                          className={cn(
                            "instrument px-3 py-2 text-right",
                            delta === null
                              ? "text-ink-faint"
                              : delta > 0
                                ? "text-indigo"
                                : delta < 0
                                  ? "text-ochre"
                                  : "text-ink-faint",
                          )}
                        >
                          {delta === null ? "—" : `${delta > 0 ? "+" : ""}${delta.toFixed(3)}`}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </section>

          {data.chunking_experiment && data.chunking_experiment.length > 0 ? (
            <section className="mt-10">
              <h2 className="marginal mb-3">Chunk size experiment</h2>
              <p className="statute mb-4 max-w-[58ch] text-ink-soft">
                The same corpus re-indexed at three chunk sizes. The
                <span className="text-ink"> over-window</span> column is the number of chunks
                longer than the encoder&rsquo;s 512-token context — those chunks are truncated
                inside the encoder, so their tails never reach the vector.
              </p>
              <ChunkingChart rows={data.chunking_experiment} />
            </section>
          ) : null}

          {data.refusal_calibration ? (
            <section className="mt-10">
              <h2 className="marginal mb-3">Refusal calibration</h2>
              <div className="border border-rule bg-sheet p-5">
                <p className="max-w-xl text-[13px] leading-relaxed text-muted">
                  The floor sits between the worst answerable question and the best trap
                  question, so both sides are separated by construction rather than by a
                  guessed constant.
                </p>
                <div className="mt-4 flex flex-wrap gap-6 font-mono text-[12px]">
                  <Figure label="floor" value={data.refusal_calibration.floor.toFixed(2)} tone="ink" />
                  <Figure
                    label="worst answerable"
                    value={data.refusal_calibration.answerable_min.toFixed(2)}
                    tone="indigo"
                  />
                  <Figure
                    label="best trap"
                    value={data.refusal_calibration.trap_max.toFixed(2)}
                    tone="ochre"
                  />
                  <Figure
                    label="separation"
                    value={data.refusal_calibration.separation.toFixed(2)}
                    tone="ink"
                  />
                </div>
              </div>
            </section>
          ) : null}

          <p className="marginal mt-10">
            generated {data.generated_at ?? "—"} · reranker {data.reranker ?? "—"}
            {data.judge_model ? ` · judge ${data.judge_model}` : ""}
          </p>
        </>
      )}
    </div>
  );
}

function ChunkingChart({ rows }: { rows: ChunkingRow[] }) {
  const max = Math.max(...rows.map((row) => row.hit_rate_at_5), 1);
  return (
    <div className="border border-rule bg-sheet p-5">
      <ul className="space-y-4">
        {rows.map((row) => (
          <li key={row.target_tokens}>
            <div className="instrument mb-1.5 flex items-baseline justify-between gap-3">
              <span className="text-ink">{row.target_tokens} tokens</span>
              <span className="text-ink-faint">
                {row.chunks} chunks · mean {Math.round(row.tokens_mean)} ·{" "}
                <span className={row.over_window > 0 ? "text-ochre" : "text-indigo"}>
                  {row.over_window} over window
                </span>
              </span>
            </div>
            <div className="flex items-center gap-3">
              <div className="h-2 flex-1 overflow-hidden border border-rule bg-paper">
                <div
                  className="h-full bg-indigo"
                  style={{ width: `${(row.hit_rate_at_5 / max) * 100}%` }}
                />
              </div>
              <span className="instrument w-14 shrink-0 text-right text-ink">
                {row.hit_rate_at_5.toFixed(3)}
              </span>
            </div>
          </li>
        ))}
      </ul>
      <p className="marginal mt-4">bar = hit-rate@5</p>
    </div>
  );
}

function Stat({ label, value, mono }: { label: string; value: string | number; mono?: boolean }) {
  return (
    <div className="border border-rule bg-sheet p-3.5">
      <dt className="marginal">{label}</dt>
      <dd
        className={cn(
          "mt-1 leading-none text-ink",
          mono ? "instrument text-[1rem]" : "display text-[1.5rem]",
        )}
      >
        {value}
      </dd>
    </div>
  );
}

function Figure({ label, value, tone }: { label: string; value: string; tone: "ink" | "indigo" | "ochre" }) {
  return (
    <div>
      <div className="marginal">{label}</div>
      <div className={cn("mt-0.5 text-[16px] tabular-nums", tone === "indigo" ? "text-indigo" : tone === "ochre" ? "text-ochre" : "text-ink")}>{value}</div>
    </div>
  );
}

function Notice({ tone, children }: { tone: "rose" | "ochre" | "muted"; children: React.ReactNode }) {
  const border =
    tone === "rose"
      ? "border-oxblood bg-oxblood-wash/60"
      : tone === "ochre"
        ? "border-ochre bg-ochre-wash/60"
        : "border-rule bg-sheet";
  return (
    <div className={cn("statute mt-8 border-l-2 py-3 pl-5 pr-4 text-ink", border)}>{children}</div>
  );
}

function Code({ children }: { children: React.ReactNode }) {
  return (
    <code className="mx-1 border border-rule bg-paper px-1.5 py-0.5 font-mono text-[0.8rem] text-indigo">
      {children}
    </code>
  );
}

function fmt(value: number | null | undefined): string {
  return typeof value === "number" ? value.toFixed(3) : "—";
}
