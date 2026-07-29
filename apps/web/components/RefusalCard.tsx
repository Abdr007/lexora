"use client";

import { ScoreLadder } from "@/components/ScoreLadder";
import type { ChunkView } from "@/lib/api";

/**
 * The refusal state — deliberately the most carefully set surface in this app.
 *
 * Most interfaces treat a non-answer as a failure and style it accordingly: small, grey,
 * apologetic. That hierarchy is wrong here. A grounded system declining to answer is the
 * product working exactly as designed, and it is the hardest thing in the stack to build.
 * So it gets the full treatment — ochre rather than red, because nothing has gone wrong;
 * a plain statement of what happened; and the near misses set underneath as marginalia,
 * with their real scores.
 *
 * Those near misses are the argument. They prove the corpus was searched, that the best
 * candidate was measured against a fixed floor, and by exactly how much it fell short.
 */
export function RefusalCard({
  text,
  nearMisses,
  floor,
  bestScore,
  onOpen,
}: {
  text: string;
  nearMisses: ChunkView[];
  floor: number | null;
  bestScore: number | null;
  onOpen: (chunkId: string) => void;
}) {
  const [lead, ...rest] = text.split("\n\n");

  return (
    <section className="cut-in border-l-2 border-ochre bg-ochre-wash/60 pl-5 pr-4 py-4">
      <p className="marginal mb-2 text-ochre">Not covered by the indexed corpus</p>

      <p className="statute max-w-[62ch] text-ink">{lead}</p>
      {rest.map((paragraph, index) => (
        <p key={index} className="statute mt-2 max-w-[62ch] text-ink-soft">
          {paragraph}
        </p>
      ))}

      {bestScore !== null && floor !== null ? (
        <table className="instrument mt-4 border-collapse">
          <tbody>
            <tr>
              <td className="pr-4 text-ink-faint">best candidate</td>
              <td className="pr-6 text-right text-ochre">
                {bestScore >= 0 ? "+" : ""}
                {bestScore.toFixed(2)}
              </td>
              <td className="pr-4 text-ink-faint">floor</td>
              <td className="pr-6 text-right text-ink">{floor.toFixed(2)}</td>
              <td className="pr-4 text-ink-faint">short by</td>
              <td className="text-right text-ink">{(floor - bestScore).toFixed(2)}</td>
            </tr>
          </tbody>
        </table>
      ) : null}

      {nearMisses.length > 0 ? (
        <div className="mt-5">
          <p className="marginal mb-2">Closest passages considered</p>
          <ul className="divide-y divide-rule border-y border-rule">
            {nearMisses.map((chunk) => (
              <li key={chunk.chunk_id}>
                <button
                  type="button"
                  onClick={() => onOpen(chunk.chunk_id)}
                  className="w-full py-2.5 text-left opacity-60 transition-opacity hover:opacity-100 focus-visible:opacity-100"
                >
                  <div className="flex items-baseline justify-between gap-4">
                    <span className="truncate font-medium text-[0.9rem] text-ink">
                      {chunk.heading}
                    </span>
                    <span className="instrument shrink-0 text-ochre">
                      {chunk.rerank_score !== null
                        ? `${chunk.rerank_score >= 0 ? "+" : ""}${chunk.rerank_score.toFixed(2)}`
                        : "—"}
                    </span>
                  </div>
                  <p className="mt-0.5 line-clamp-2 text-[0.82rem] leading-snug text-ink-soft">
                    {chunk.text.split("\n").slice(1).join(" ")}
                  </p>
                  <ScoreLadder chunk={chunk} className="mt-1.5" />
                </button>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </section>
  );
}
