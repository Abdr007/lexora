"use client";

import { Fragment, type ReactNode } from "react";
import { CitationChip } from "@/components/CitationChip";
import { segmentAnswer } from "@/lib/citations";
import type { CitationView } from "@/lib/api";

/**
 * Renders a generated answer: paragraphs, a small safe subset of inline markdown, and
 * citation chips.
 *
 * Everything is built as React elements — there is no `dangerouslySetInnerHTML` and no
 * markdown library. Model output is untrusted text that may quote a hostile document
 * verbatim, so the renderer is deliberately incapable of producing markup: the worst a
 * malicious passage can do is render as literal characters.
 *
 * The supported subset is what a grounded legal answer actually uses — bold for the
 * provision being named, inline code, numbered and bulleted clause lists — and nothing
 * more. Links are intentionally NOT rendered: the only trustworthy destination in this
 * product is a citation chip, which resolves through the verifier.
 */

const BOLD = /\*\*([^*]+)\*\*/g;
const CODE = /`([^`]+)`/g;

export function AnswerText({
  text,
  citations,
  onOpenEvidence,
}: {
  text: string;
  citations: CitationView[];
  onOpenEvidence: (chunkId: string) => void;
}) {
  const blocks = text.split(/\n{2,}/).filter((block) => block.trim().length > 0);

  return (
    <div className="statute max-w-prose space-y-3 text-[15px] text-ink/95">
      {blocks.map((block, blockIndex) => {
        const lines = block.split("\n").filter((line) => line.trim().length > 0);
        const isList = lines.length > 1 && lines.every((line) => /^\s*(?:[-*•]|\d+[.)])\s/.test(line));

        if (isList) {
          return (
            <ul key={blockIndex} className="ml-1 space-y-1.5">
              {lines.map((line, lineIndex) => (
                <li key={lineIndex} className="flex gap-2.5">
                  <span aria-hidden className="mt-[0.6em] size-1 shrink-0 rounded-full bg-cyan/60" />
                  <span>
                    <Inline
                      text={line.replace(/^\s*(?:[-*•]|\d+[.)])\s+/, "")}
                      citations={citations}
                      onOpenEvidence={onOpenEvidence}
                    />
                  </span>
                </li>
              ))}
            </ul>
          );
        }

        return (
          <p key={blockIndex}>
            {lines.map((line, lineIndex) => (
              <Fragment key={lineIndex}>
                {lineIndex > 0 ? <br /> : null}
                <Inline text={line} citations={citations} onOpenEvidence={onOpenEvidence} />
              </Fragment>
            ))}
          </p>
        );
      })}
    </div>
  );
}

function Inline({
  text,
  citations,
  onOpenEvidence,
}: {
  text: string;
  citations: CitationView[];
  onOpenEvidence: (chunkId: string) => void;
}) {
  return (
    <>
      {segmentAnswer(text, citations).map((segment, index) =>
        segment.kind === "citation" ? (
          <CitationChip
            key={index}
            label={segment.lawLabel}
            articleNo={segment.articleNo}
            citation={segment.citation}
            onOpen={onOpenEvidence}
          />
        ) : (
          <Fragment key={index}>{formatInline(segment.value)}</Fragment>
        ),
      )}
    </>
  );
}

/** Apply the supported inline marks, emitting elements rather than markup. */
function formatInline(value: string): ReactNode[] {
  const nodes: ReactNode[] = [];
  let cursor = 0;
  let key = 0;

  const pattern = new RegExp(`${BOLD.source}|${CODE.source}`, "g");
  let match = pattern.exec(value);
  while (match !== null) {
    if (match.index > cursor) nodes.push(value.slice(cursor, match.index));
    const [, boldBody, codeBody] = match;
    if (boldBody !== undefined) {
      nodes.push(
        <strong key={`b${key}`} className="font-semibold text-ink">
          {boldBody}
        </strong>,
      );
    } else if (codeBody !== undefined) {
      nodes.push(
        <code key={`c${key}`} className="rounded bg-surface-2 px-1 py-px font-mono text-[13px] text-cyan">
          {codeBody}
        </code>,
      );
    }
    key += 1;
    cursor = match.index + match[0].length;
    match = pattern.exec(value);
  }
  if (cursor < value.length) nodes.push(value.slice(cursor));
  return nodes;
}
