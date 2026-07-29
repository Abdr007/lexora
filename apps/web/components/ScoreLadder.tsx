import { cn } from "@/lib/cn";
import type { ChunkView } from "@/lib/api";

/**
 * The score ladder: how a passage got here, left to right.
 *
 *   D·3  S·7  →  RRF .0312  →  CE +5.11
 *
 * This is the structural device of the interface, and it is not decorative numbering —
 * every figure is a real intermediate value from the retrieval run. A passage found by
 * only one retriever visibly shows the gap where the other rank would be, which is the
 * clearest possible argument for why hybrid retrieval exists.
 */
export function ScoreLadder({ chunk, className }: { chunk: ChunkView; className?: string }) {
  return (
    <div className={cn("instrument flex flex-wrap items-center gap-x-2 gap-y-0.5", className)}>
      <span
        className={chunk.dense_rank ? "text-ink-soft" : "text-ink-faint/50"}
        title={
          chunk.dense_score !== null
            ? `dense rank ${chunk.dense_rank}, cosine ${chunk.dense_score.toFixed(4)}`
            : "not returned by dense retrieval"
        }
      >
        D·{chunk.dense_rank ?? "—"}
      </span>
      <span
        className={chunk.sparse_rank ? "text-ink-soft" : "text-ink-faint/50"}
        title={
          chunk.sparse_score !== null
            ? `BM25 rank ${chunk.sparse_rank}, score ${chunk.sparse_score.toFixed(3)}`
            : "not returned by sparse retrieval"
        }
      >
        S·{chunk.sparse_rank ?? "—"}
      </span>
      <span aria-hidden className="text-ink-faint/40">→</span>
      <span className="text-ink-soft" title="Reciprocal Rank Fusion score">
        RRF {chunk.rrf_score !== null ? chunk.rrf_score.toFixed(4).replace(/^0/, "") : "—"}
      </span>
      {chunk.rerank_score !== null ? (
        <>
          <span aria-hidden className="text-ink-faint/40">→</span>
          <span
            className={chunk.rerank_score >= 0 ? "text-indigo" : "text-ochre"}
            title={
              chunk.rerank_probability !== null
                ? `cross-encoder logit; ${(chunk.rerank_probability * 100).toFixed(1)}% relevance`
                : "cross-encoder logit"
            }
          >
            CE {chunk.rerank_score >= 0 ? "+" : ""}
            {chunk.rerank_score.toFixed(2)}
          </span>
        </>
      ) : null}
    </div>
  );
}
