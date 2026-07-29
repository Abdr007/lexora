"use client";

import type { CitationView } from "@/lib/api";

/**
 * An inline citation, set as a legal reference rather than a button.
 *
 * Two states, and the difference matters more than any other styling decision here:
 *  - verified    the cited article was among the passages the model was shown. Indigo,
 *                hairline box, opens the exact clause.
 *  - unsupported the model cited something it was never given. Oxblood, marked, and NOT
 *                clickable — there is nothing truthful to open. It is shown rather than
 *                deleted on purpose: a silently redacted answer hides a failure the
 *                reader has every right to see.
 */
export function CitationChip({
  label,
  articleNo,
  citation,
  onOpen,
}: {
  label: string;
  articleNo: number;
  citation: CitationView | null;
  onOpen: (chunkId: string) => void;
}) {
  const unsupported = citation === null || citation.status === "unsupported";

  if (unsupported) {
    return (
      <span
        className="chip-broken mx-[0.15em] inline-flex items-baseline gap-1 align-baseline"
        title="This citation does not resolve to any passage the model was given. It is flagged, not hidden."
      >
        <span aria-hidden>†</span>
        {label} {articleNo}
      </span>
    );
  }

  return (
    <button
      type="button"
      onClick={() => citation.chunk_id && onOpen(citation.chunk_id)}
      className="chip mx-[0.15em] inline-flex items-baseline align-baseline"
      title="Open this clause"
    >
      {label} {articleNo}
    </button>
  );
}
