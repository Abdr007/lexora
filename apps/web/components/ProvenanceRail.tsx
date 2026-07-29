"use client";

import { cn } from "@/lib/cn";
import type { GateView, RetrievalEvent, TimingView } from "@/lib/api";

/**
 * The Provenance Rail — set as a filing stamp, not a progress bar.
 *
 * Every other RAG demo shows a spinner while it thinks. This shows the machine thinking:
 * five stages, each stamped as its SSE event lands, each carrying the number it actually
 * produced. The claim the product makes is "you can audit me", so the loading state is
 * the audit trail rather than a substitute for one.
 *
 * Nothing here is decoration. Fused candidate count, how many passages both retrievers
 * agreed on, the winning cross-encoder score against the floor it had to clear, and the
 * milliseconds each stage cost.
 */

export type StageKey = "gate" | "fuse" | "rerank" | "ground" | "verify";

export interface RailState {
  active: StageKey | null;
  done: Set<StageKey>;
  gate: GateView | null;
  retrieval: RetrievalEvent | null;
  timings: TimingView | null;
  verified: number | null;
  unsupported: number | null;
  refused: boolean;
  blocked: boolean;
}

export const emptyRail = (): RailState => ({
  active: null,
  done: new Set<StageKey>(),
  gate: null,
  retrieval: null,
  timings: null,
  verified: null,
  unsupported: null,
  refused: false,
  blocked: false,
});

const STAGES: { key: StageKey; label: string; note: string }[] = [
  { key: "gate", label: "Gate", note: "rewrite · screen" },
  { key: "fuse", label: "Fuse", note: "dense + BM25 · RRF" },
  { key: "rerank", label: "Rerank", note: "cross-encoder" },
  { key: "ground", label: "Ground", note: "context-only" },
  { key: "verify", label: "Verify", note: "citations resolve" },
];

function readout(stage: StageKey, state: RailState): string | null {
  const { gate, retrieval, timings } = state;
  switch (stage) {
    case "gate":
      if (!gate) return null;
      if (state.blocked) return "blocked";
      return gate.rewritten ? "rewritten" : "standalone";
    case "fuse":
      if (!retrieval) return null;
      return `${retrieval.candidates} fused · ${retrieval.overlap} both`;
    case "rerank":
      if (!retrieval || retrieval.best_score === null) return null;
      return `${retrieval.best_score >= 0 ? "+" : ""}${retrieval.best_score.toFixed(2)} / ${retrieval.floor.toFixed(1)}`;
    case "ground":
      if (state.refused) return "declined";
      if (!timings) return null;
      return `${Math.round(timings.generation_ms)} ms`;
    case "verify":
      if (state.verified === null) return null;
      return state.unsupported && state.unsupported > 0
        ? `${state.unsupported} unsupported`
        : `${state.verified} verified`;
  }
}

function stageMs(stage: StageKey, timings: TimingView | null): number | null {
  if (!timings) return null;
  switch (stage) {
    case "gate": return timings.gate_ms;
    case "fuse": return timings.retrieval_ms;
    case "rerank": return timings.rerank_ms;
    case "ground": return timings.generation_ms;
    case "verify": return timings.verify_ms;
  }
}

export function ProvenanceRail({ state, className }: { state: RailState; className?: string }) {
  return (
    <aside className={cn("sheet", className)} aria-label="Retrieval pipeline trace">
      <div className="flex items-baseline justify-between border-b border-rule px-3 py-2">
        <span className="marginal">Provenance</span>
        <span className="instrument text-ink-faint">
          {state.timings ? `${Math.round(state.timings.total_ms)} ms` : state.active ? "···" : "—"}
        </span>
      </div>

      <ol>
        {STAGES.map((stage) => {
          const isDone = state.done.has(stage.key);
          const isActive = state.active === stage.key;
          const value = readout(stage.key, state);
          const ms = stageMs(stage.key, state.timings);
          const flagged =
            (stage.key === "gate" && state.blocked) ||
            (stage.key === "ground" && state.refused) ||
            (stage.key === "verify" && (state.unsupported ?? 0) > 0);

          return (
            <li
              key={stage.key}
              className="flex items-baseline gap-2.5 border-b border-rule px-3 py-1.5 last:border-b-0"
            >
              <span
                aria-hidden
                className={cn(
                  "mt-[0.3em] size-[7px] shrink-0",
                  flagged
                    ? "bg-ochre"
                    : isDone
                      ? "bg-indigo"
                      : isActive
                        ? "stamp bg-ink"
                        : "border border-rule bg-transparent",
                )}
              />
              <span
                className={cn(
                  "instrument w-[3.9rem] shrink-0",
                  isDone || isActive ? "text-ink" : "text-ink-faint",
                )}
              >
                {stage.label}
              </span>
              <span className="instrument min-w-0 flex-1 truncate text-ink-soft">
                {value ?? stage.note}
              </span>
              <span className="instrument shrink-0 text-ink-faint">
                {ms !== null && ms > 0 ? `${Math.round(ms)}ms` : ""}
              </span>
            </li>
          );
        })}
      </ol>
    </aside>
  );
}
