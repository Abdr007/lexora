"use client";

import { useEffect, useMemo, useState } from "react";

import { cn } from "@/lib/cn";

export type FieldState = "idle" | "searching" | "answered" | "refused" | "blocked";

interface Props {
  /** One bar per indexed passage. The corpus, drawn. */
  count: number;
  state: FieldState;
  /** How many passages the retrievers returned, and how many survived the floor. */
  retrieved?: number;
  kept?: number;
  className?: string;
}

/**
 * The corpus as a field of passages, and what a question does to it.
 *
 * This is the hero because it is the thesis. Every retrieval demo can show an answer;
 * almost none can show you the moment where the system decides it does *not* have one.
 * Here that moment is the whole picture: passages sit as bars, a question lights the ones
 * the retrievers found, the cross-encoder pushes the best above a luminous threshold, and
 * whatever clears it becomes a citation. When nothing clears it, nothing crosses the line
 * and the line turns amber — the refusal, drawn rather than described.
 *
 * The bar count is the real `chunks_indexed` from the API, so the field is the corpus and
 * not an illustration of one. Heights are derived from the index rather than randomised:
 * the same corpus always draws the same skyline, which is what makes it read as a picture
 * of something rather than as decoration.
 */
export function SpectrumField({ count, state, retrieved = 0, kept = 0, className }: Props) {
  const bars = Math.min(Math.max(count, 24), 181);

  // Deterministic pseudo-heights. A hash of the index, not Math.random: a field that
  // reshuffles on every render is noise, and it would differ between server and client.
  const heights = useMemo(
    () =>
      Array.from({ length: bars }, (_, index) => {
        const hashed = Math.sin(index * 12.9898) * 43758.5453;
        return 0.34 + (hashed - Math.floor(hashed)) * 0.66;
      }),
    [bars],
  );

  // Which bars are "found" for this question. Spread across the field rather than
  // clustered, because retrieval hits are not adjacent in the corpus.
  const foundStep = retrieved > 0 ? Math.max(1, Math.floor(bars / retrieved)) : 0;
  const keptStep = kept > 0 ? Math.max(1, Math.floor(bars / kept)) : 0;

  // The entrance runs once, so the field assembles on load instead of appearing.
  const [entered, setEntered] = useState(false);
  useEffect(() => {
    const frame = requestAnimationFrame(() => setEntered(true));
    return () => cancelAnimationFrame(frame);
  }, []);

  const refused = state === "refused" || state === "blocked";
  const searching = state === "searching";

  return (
    <div
      className={cn("relative select-none", className)}
      aria-hidden
      // Decorative: the same information is in the answer, the citations and the score
      // ladder, all of which are real text. A screen reader gains nothing from a skyline.
    >
      <div className="flex h-[104px] items-end gap-[2px] sm:gap-[3px]">
        {heights.map((height, index) => {
          const isFound = foundStep > 0 && index % foundStep === 0;
          const isKept = keptStep > 0 && index % keptStep === 0 && !refused;

          // At rest the field carries the spectrum itself, so the scale that colour
          // means is legible before a question is asked rather than only after one.
          const idleHue =
            index / bars < 0.34
              ? "var(--color-indigo)"
              : index / bars < 0.67
                ? "var(--color-violet)"
                : "var(--color-ochre)";
          const active = isKept
            ? "var(--color-indigo)"
            : isFound
              ? refused
                ? "var(--color-ochre)"
                : "var(--color-violet)"
              : idleHue;

          return (
            <span
              key={index}
              className={cn(
                "flex-1 rounded-[1px] transition-all duration-700 ease-out",
                searching ? "pulse" : "shimmer",
              )}
              style={{
                background: active,
                height: entered ? `${(isKept ? 1 : isFound ? 0.8 : 0.52) * height * 100}%` : "4%",
                opacity: entered ? (isKept ? 1 : isFound ? 0.9 : 0.42) : 0,
                // Staggered so the field assembles left to right, ~0.6s end to end, and
                // the idle shimmer inherits the same offset so it travels rather than
                // flickering in place.
                transitionDelay: `${(index % bars) * (600 / bars)}ms`,
                animationDelay: `${(index % bars) * (2600 / bars)}ms`,
              }}
            />
          );
        })}
      </div>

      {/* The threshold. Everything above it is answerable; everything below is a near
          miss. It is the only element in the interface allowed to glow. */}
      <div
        className={cn("threshold mt-3 w-full", searching && "pulse")}
        data-state={refused ? "refused" : undefined}
      />

      <div className="mt-2 flex items-baseline justify-between">
        <span className="marginal">
          {refused
            ? "nothing cleared the line — Lexora refuses"
            : state === "answered"
              ? `${kept} passage${kept === 1 ? "" : "s"} cleared the line and became citations`
              : searching
                ? "searching…"
                : `${count} passages, waiting for a question`}
        </span>
        <span className="marginal hidden sm:inline">
          {state === "idle" ? "" : `${retrieved} found`}
        </span>
      </div>
    </div>
  );
}
