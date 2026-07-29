"use client";

import { AnimatePresence, motion } from "framer-motion";
import { useEffect, useMemo, useRef } from "react";
import { ScoreLadder } from "@/components/ScoreLadder";
import { highlightRanges } from "@/lib/citations";
import type { ChunkView } from "@/lib/api";

/**
 * The Evidence sheet: the full clause behind a citation, with the matched span marked and
 * the score trail that put it there.
 *
 * It slides in as a fresh sheet of paper laid over the page, with a hard ink edge — the
 * conversation stays where it was, so the reader keeps their place. It is a reference
 * being held up beside the text, not a navigation away from it.
 */
export function EvidencePanel({
  chunk,
  answerText,
  onClose,
}: {
  chunk: ChunkView | null;
  answerText: string;
  onClose: () => void;
}) {
  const closeRef = useRef<HTMLButtonElement>(null);
  const open = chunk !== null;

  useEffect(() => {
    if (!open) return;
    closeRef.current?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  return (
    <AnimatePresence>
      {chunk ? (
        <>
          <motion.div
            key="scrim"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.14 }}
            onClick={onClose}
            className="fixed inset-0 z-40 bg-ink/25"
            aria-hidden
          />
          <motion.aside
            key="sheet"
            role="dialog"
            aria-modal="true"
            aria-label={`${chunk.law_label} Article ${chunk.article_no}`}
            initial={{ x: "100%" }}
            animate={{ x: 0 }}
            exit={{ x: "100%" }}
            transition={{ type: "spring", stiffness: 400, damping: 40 }}
            className="fixed inset-y-0 right-0 z-50 flex w-full max-w-[38rem] flex-col border-l-2 border-ink bg-sheet"
          >
            <header className="flex items-start gap-4 border-b border-rule px-6 py-5">
              <div className="min-w-0 flex-1">
                <p className="marginal mb-1.5">{chunk.law_label} · {chunk.part_title}</p>
                <h2 className="display text-[1.75rem] text-ink">
                  Article {chunk.article_no}
                </h2>
                {chunk.article_title ? (
                  <p className="mt-1 font-medium text-[0.95rem] text-ink-soft">
                    {chunk.article_title}
                  </p>
                ) : chunk.section ? (
                  <p className="mt-1 text-[0.95rem] text-ink-soft">{chunk.section}</p>
                ) : null}
              </div>
              <button
                ref={closeRef}
                type="button"
                onClick={onClose}
                aria-label="Close evidence"
                className="marginal border border-rule px-2 py-1 transition-colors hover:border-ink hover:text-ink"
              >
                Esc
              </button>
            </header>

            <div className="flex flex-wrap items-center gap-x-5 gap-y-1 border-b border-rule px-6 py-2.5">
              <ScoreLadder chunk={chunk} />
              <span className="instrument text-ink-faint">
                p.{chunk.page_start}
                {chunk.page_end !== chunk.page_start ? `–${chunk.page_end}` : ""}
              </span>
              {chunk.seq_total > 1 ? (
                <span className="instrument text-ink-faint">
                  {chunk.seq + 1}/{chunk.seq_total}
                </span>
              ) : null}
            </div>

            <div className="min-h-0 flex-1 overflow-y-auto px-6 py-5">
              <Highlighted text={stripHeader(chunk.text)} answer={answerText} />
            </div>

            <footer className="border-t border-rule px-6 py-3">
              <p className="text-[0.78rem] leading-relaxed text-ink-faint">
                Quoted verbatim from {chunk.law_title}. Marked spans are the sentences the
                answer drew on.
              </p>
            </footer>
          </motion.aside>
        </>
      ) : null}
    </AnimatePresence>
  );
}

/** The chunk text carries a context header line; the sheet shows the clause itself. */
function stripHeader(text: string): string {
  const newline = text.indexOf("\n");
  return newline === -1 ? text : text.slice(newline + 1);
}

function Highlighted({ text, answer }: { text: string; answer: string }) {
  const ranges = useMemo(() => highlightRanges(text, answer), [text, answer]);

  if (ranges.length === 0) {
    return <p className="statute text-[1.02rem] text-ink">{text}</p>;
  }

  const parts: React.ReactNode[] = [];
  let cursor = 0;
  ranges.forEach(([start, end], index) => {
    if (start > cursor) parts.push(text.slice(cursor, start));
    parts.push(<mark key={index}>{text.slice(start, end)}</mark>);
    cursor = end;
  });
  if (cursor < text.length) parts.push(text.slice(cursor));

  return <p className="statute text-[1.02rem] text-ink">{parts}</p>;
}
