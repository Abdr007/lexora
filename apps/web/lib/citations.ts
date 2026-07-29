import type { CitationView } from "@/lib/api";

/**
 * Split answer text into plain segments and citation segments.
 *
 * The regex mirrors the verifier's on the server so the UI can never render a chip the
 * verifier did not adjudicate: every match is looked up in the verified citation list,
 * and anything unmatched is rendered as plain text rather than silently styled as a
 * confirmed source.
 */
const CITATION_RE = /\[\s*([A-Za-z][A-Za-z .'-]{1,48}?)\s*,\s*(?:Article|Art\.?)\s*\(?\s*(\d{1,3})\s*\)?\s*\]/gi;

export type Segment =
  | { kind: "text"; value: string }
  | { kind: "citation"; value: string; lawLabel: string; articleNo: number; citation: CitationView | null };

export function segmentAnswer(text: string, citations: CitationView[]): Segment[] {
  const byKey = new Map<string, CitationView>();
  for (const citation of citations) {
    byKey.set(`${normalise(citation.law_label)}|${citation.article_no}`, citation);
  }

  const segments: Segment[] = [];
  let cursor = 0;
  CITATION_RE.lastIndex = 0;
  let match = CITATION_RE.exec(text);
  while (match !== null) {
    const [raw, lawLabel = "", articleRaw = "0"] = match;
    if (match.index > cursor) {
      segments.push({ kind: "text", value: text.slice(cursor, match.index) });
    }
    const articleNo = Number.parseInt(articleRaw, 10);
    segments.push({
      kind: "citation",
      value: raw,
      lawLabel: lawLabel.trim(),
      articleNo,
      citation: byKey.get(`${normalise(lawLabel)}|${articleNo}`) ?? null,
    });
    cursor = match.index + raw.length;
    match = CITATION_RE.exec(text);
  }
  if (cursor < text.length) {
    segments.push({ kind: "text", value: text.slice(cursor) });
  }
  return segments;
}

function normalise(label: string): string {
  return label.toLowerCase().replace(/[^a-z0-9]+/g, "");
}

/**
 * Find the span of `passage` that best matches the answer, for highlighting.
 *
 * Deliberately conservative: it highlights the longest run of consecutive sentences
 * whose distinctive words appear in the answer. Highlighting nothing is better than
 * highlighting the wrong clause in a legal document.
 */
export function highlightRanges(passage: string, answer: string): [number, number][] {
  const answerWords = new Set(
    answer.toLowerCase().match(/[a-z0-9]{4,}/g) ?? [],
  );
  if (answerWords.size === 0) return [];

  const ranges: [number, number][] = [];
  const sentenceRe = /[^.;:]+[.;:]?/g;
  let match = sentenceRe.exec(passage);
  while (match !== null) {
    const sentence = match[0];
    const words = sentence.toLowerCase().match(/[a-z0-9]{4,}/g) ?? [];
    if (words.length >= 4) {
      const hits = words.filter((word) => answerWords.has(word)).length;
      if (hits / words.length >= 0.6) {
        ranges.push([match.index, match.index + sentence.length]);
      }
    }
    match = sentenceRe.exec(passage);
  }
  return mergeAdjacent(ranges);
}

function mergeAdjacent(ranges: [number, number][]): [number, number][] {
  if (ranges.length === 0) return [];
  const merged: [number, number][] = [ranges[0]!];
  for (const range of ranges.slice(1)) {
    const last = merged[merged.length - 1]!;
    if (range[0] - last[1] <= 2) last[1] = range[1];
    else merged.push(range);
  }
  return merged;
}
