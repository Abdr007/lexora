"use client";

import { AnimatePresence, motion } from "framer-motion";
import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AnswerText } from "@/components/AnswerText";
import { Composer } from "@/components/Composer";
import { EvidencePanel } from "@/components/EvidencePanel";
import { ProvenanceRail, type RailState, emptyRail } from "@/components/ProvenanceRail";
import { RefusalCard } from "@/components/RefusalCard";
import { ScoreLadder } from "@/components/ScoreLadder";
import {
  askStream,
  getHealth,
  getLaws,
  type AnswerView,
  type ChunkView,
  type HealthView,
  type LawView,
  type Turn,
} from "@/lib/api";
import { cn } from "@/lib/cn";

interface Exchange {
  id: string;
  question: string;
  streamed: string;
  answer: AnswerView | null;
  rail: RailState;
  error: string | null;
}

const SUGGESTIONS = [
  { kind: "Paraphrase", text: "How much end-of-service money do I get after 6 years?" },
  { kind: "Exact term", text: "What does Article 30 of the labour law say?" },
  { kind: "Tenancy", text: "Can my landlord increase the rent when I renew?" },
  { kind: "Refusal", text: "What is the capital gains tax rate in Singapore?" },
];

export default function Page() {
  const [health, setHealth] = useState<HealthView | null>(null);
  const [laws, setLaws] = useState<LawView[]>([]);
  const [draft, setDraft] = useState("");
  const [lawId, setLawId] = useState<string | null>(null);
  const [exchanges, setExchanges] = useState<Exchange[]>([]);
  const [busy, setBusy] = useState(false);
  const [openChunk, setOpenChunk] = useState<ChunkView | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const controller = new AbortController();
    getHealth(controller.signal).then(setHealth).catch(() => setHealth(null));
    getLaws(controller.signal).then(setLaws).catch(() => setLaws([]));
    return () => controller.abort();
  }, []);


  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [exchanges]);

  const chunkIndex = useMemo(() => {
    const map = new Map<string, ChunkView>();
    for (const exchange of exchanges) {
      for (const chunk of exchange.answer?.evidence ?? []) map.set(chunk.chunk_id, chunk);
      for (const chunk of exchange.answer?.near_misses ?? []) map.set(chunk.chunk_id, chunk);
    }
    return map;
  }, [exchanges]);

  const openEvidence = useCallback(
    (chunkId: string) => {
      const chunk = chunkIndex.get(chunkId);
      if (chunk) setOpenChunk(chunk);
    },
    [chunkIndex],
  );

  const patch = useCallback((id: string, update: (current: Exchange) => Exchange) => {
    setExchanges((current) =>
      current.map((exchange) => (exchange.id === id ? update(exchange) : exchange)),
    );
  }, []);

  const send = useCallback(
    async (questionText: string) => {
      const question = questionText.trim();
      if (!question || busy) return;

      const history: Turn[] = exchanges
        .filter((exchange) => exchange.answer !== null)
        .flatMap((exchange) => [
          { role: "user" as const, content: exchange.question },
          { role: "assistant" as const, content: exchange.answer?.text ?? "" },
        ])
        .slice(-6);

      const id = `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
      const rail = emptyRail();
      rail.active = "gate";
      setExchanges((current) => [
        ...current,
        { id, question, streamed: "", answer: null, rail, error: null },
      ]);
      setDraft("");
      setBusy(true);

      const controller = new AbortController();
      abortRef.current = controller;

      await askStream({
        question,
        history,
        lawId,
        rerank: true,
        signal: controller.signal,
        onEvent: (event) => {
          patch(id, (exchange) => {
            const next: Exchange = { ...exchange, rail: { ...exchange.rail } };
            const done = new Set(next.rail.done);

            switch (event.type) {
              case "gate": {
                done.add("gate");
                next.rail = {
                  ...next.rail,
                  done,
                  active: "fuse",
                  gate: { ...event.data, reason: "", latency_ms: 0 },
                  blocked: event.data.decision === "block",
                };
                break;
              }
              case "retrieval": {
                done.add("fuse");
                done.add("rerank");
                next.rail = {
                  ...next.rail,
                  done,
                  active: "ground",
                  retrieval: event.data,
                  refused: !event.data.covered,
                };
                break;
              }
              case "token": {
                next.streamed = next.streamed + event.data.text;
                break;
              }
              case "final": {
                done.add("ground");
                done.add("verify");
                next.answer = event.data;
                next.streamed = event.data.text;
                next.rail = {
                  ...next.rail,
                  done,
                  active: null,
                  timings: event.data.timings,
                  verified: event.data.verification?.verified_count ?? 0,
                  unsupported: event.data.verification?.unsupported_count ?? 0,
                  refused: event.data.kind === "refusal",
                  blocked: event.data.kind === "blocked",
                };
                break;
              }
              case "error": {
                next.error = event.data.detail;
                next.rail = { ...next.rail, active: null };
                break;
              }
            }
            return next;
          });
        },
      });

      abortRef.current = null;
      setBusy(false);
    },
    [busy, exchanges, lawId, patch],
  );

  const sendRef = useRef<((text: string) => Promise<void>) | null>(null);
  useEffect(() => {
    sendRef.current = send;
  }, [send]);

  // Deep link: /?q=... asks the question on load, so a demo can be handed to someone as
  // a URL rather than a list of instructions. Declared AFTER the ref assignment above:
  // effects run in declaration order, so placing it first would read a null ref and
  // silently never fire. The ref guard survives React 19 strict-mode double mounting.
  const autoRan = useRef(false);
  useEffect(() => {
    if (autoRan.current || !sendRef.current) return;
    const params = new URLSearchParams(window.location.search);
    const preset = params.get("q");
    if (!preset) return;
    autoRan.current = true;
    const scope = params.get("law");
    if (scope) setLawId(scope);
    void sendRef.current(preset);
  }, [send]);

  const stop = useCallback(() => {
    abortRef.current?.abort();
    abortRef.current = null;
    setBusy(false);
  }, []);

  const latest = exchanges[exchanges.length - 1];
  // `??` does not fall through an EMPTY array, and a refusal has evidence: [] with the
  // near misses populated — so the margin index would silently stay blank on exactly the
  // state it is most interesting for.
  const marginNumbers = (
    latest?.answer?.evidence?.length
      ? latest.answer.evidence
      : (latest?.answer?.near_misses ?? [])
  ).slice(0, 6);

  return (
    <div className="mx-auto max-w-[78rem] px-5 sm:px-8">
      <Masthead health={health} laws={laws} />

      <main className="flex gap-8 pt-8 pb-10">
        {/* The statutory margin: the article numbers currently in evidence, set outside
            the text block exactly as a printed statute sets its marginal notes. It is a
            real index rather than an ornament — each number opens its own clause. */}
        <nav
          aria-label="Articles in evidence"
          className="hidden w-12 shrink-0 border-r border-rule pr-3 pt-1 md:block"
        >
          {marginNumbers.map((chunk) => (
            <button
              key={chunk.chunk_id}
              type="button"
              onClick={() => openEvidence(chunk.chunk_id)}
              title={chunk.heading}
              className="marginal block w-full py-1 text-right transition-colors hover:text-indigo"
            >
              {chunk.article_no}
            </button>
          ))}
        </nav>

        <div className="min-w-0 flex-1">
          {exchanges.length === 0 ? (
            <Opening laws={laws} onPick={(text) => void send(text)} />
          ) : null}

          <div className="space-y-10">
            {exchanges.map((exchange) => (
              <ExchangeView
                key={exchange.id}
                exchange={exchange}
                onOpenEvidence={openEvidence}
              />
            ))}
          </div>
          <div ref={endRef} />

          <div className="sticky bottom-0 mt-8 bg-paper pt-3">
            <Composer
              value={draft}
              onChange={setDraft}
              onSubmit={() => void send(draft)}
              onStop={stop}
              busy={busy}
              laws={laws}
              lawId={lawId}
              onLawChange={setLawId}
              disabled={health?.status === "degraded"}
            />
          </div>
        </div>

        <div className="hidden w-[19rem] shrink-0 lg:block">
          <div className="sticky top-6 space-y-5">
            <ProvenanceRail state={latest?.rail ?? emptyRail()} />
            <CorpusIndex laws={laws} health={health} />
          </div>
        </div>
      </main>

      <EvidencePanel
        chunk={openChunk}
        answerText={latest?.streamed ?? ""}
        onClose={() => setOpenChunk(null)}
      />
    </div>
  );
}

function Masthead({ health, laws }: { health: HealthView | null; laws: LawView[] }) {
  const articles = laws.reduce((total, law) => total + law.article_count, 0);
  const offline = health?.engine === "offline-extractive";
  return (
    <header className="masthead-rule pt-3">
      <div className="flex flex-wrap items-baseline justify-between gap-x-6 gap-y-2 border-b border-rule pb-2.5">
        <div className="flex items-baseline gap-4">
          <span className="display text-[1.6rem] tracking-[0.02em] text-ink">LEXORA</span>
          <span className="marginal hidden sm:inline">
            Statute Instrument{articles > 0 ? ` · ${articles} articles indexed` : ""}
          </span>
        </div>
        <div className="flex items-center gap-3">
          {offline ? (
            <span
              className="marginal border border-ochre px-2 py-0.5 text-ochre"
              title="No Claude API key configured. Retrieval, reranking, the refusal gate and citation verification all run; answers are quoted from the corpus rather than written."
            >
              Offline · extractive
            </span>
          ) : health ? (
            <span className="marginal border border-indigo px-2 py-0.5 text-indigo">
              Claude · grounded
            </span>
          ) : null}
          <Link href="/metrics" className="marginal underline-offset-4 hover:underline">
            Metrics
          </Link>
        </div>
      </div>
    </header>
  );
}

function Opening({ laws, onPick }: { laws: LawView[]; onPick: (text: string) => void }) {
  return (
    <section className="cut-in-slow pb-10">
      <h1 className="display max-w-[16ch] text-[clamp(2.6rem,7.5vw,5.2rem)] text-ink">
        Answers with the clause attached.
      </h1>
      <div className="draw-rule my-5 h-px w-full bg-ink" />
      <p className="display max-w-[18ch] text-[clamp(1.5rem,4vw,2.6rem)] italic text-ink-soft">
        Or no answer at all.
      </p>

      <p className="statute mt-7 max-w-[58ch] text-ink-soft">
        Every claim carries the article it came from, and every citation opens the exact
        text. Where the indexed law does not cover a question, Lexora says so — and shows
        the passages it rejected, with the scores that rejected them.
      </p>

      <ul className="mt-8 divide-y divide-rule border-y border-rule">
        {SUGGESTIONS.map((suggestion) => (
          <li key={suggestion.text}>
            <button
              type="button"
              onClick={() => onPick(suggestion.text)}
              className="group flex w-full items-baseline gap-5 py-3 text-left"
            >
              <span className="marginal w-[6.5rem] shrink-0 group-hover:text-indigo">
                {suggestion.kind}
              </span>
              <span className="statute text-ink group-hover:text-indigo">
                {suggestion.text}
              </span>
            </button>
          </li>
        ))}
      </ul>

      {laws.length > 0 ? (
        <p className="marginal mt-4">{laws.map((law) => law.label).join(" · ")}</p>
      ) : null}
    </section>
  );
}

function ExchangeView({
  exchange,
  onOpenEvidence,
}: {
  exchange: Exchange;
  onOpenEvidence: (chunkId: string) => void;
}) {
  const { answer, streamed, error } = exchange;
  const streaming = answer === null && error === null;

  return (
    <article className="cut-in">
      <div className="mb-4 border-l-2 border-ink pl-4">
        <p className="marginal mb-1">Question</p>
        <p className="display text-[1.35rem] leading-tight text-ink">{exchange.question}</p>
      </div>

      {error ? (
        <div className="border-l-2 border-oxblood bg-oxblood-wash/60 py-3 pl-5 pr-4">
          <p className="marginal mb-1 text-oxblood">Request failed</p>
          <p className="statute text-ink">{error}</p>
        </div>
      ) : answer?.kind === "refusal" ? (
        <RefusalCard
          text={answer.text}
          nearMisses={answer.near_misses}
          floor={exchange.rail.retrieval?.floor ?? null}
          bestScore={exchange.rail.retrieval?.best_score ?? null}
          onOpen={onOpenEvidence}
        />
      ) : answer?.kind === "blocked" ? (
        <div className="border-l-2 border-oxblood bg-oxblood-wash/60 py-3 pl-5 pr-4">
          <p className="marginal mb-1 text-oxblood">Blocked by the input gate</p>
          <p className="statute max-w-[62ch] text-ink">{answer.text}</p>
          {answer.gate.signals.length > 0 ? (
            <p className="instrument mt-2 text-oxblood">{answer.gate.signals.join(" · ")}</p>
          ) : null}
        </div>
      ) : !streamed && streaming ? (
        <p className="marginal">
          <span className="stamp inline-block">searching the corpus</span>
        </p>
      ) : (
        <div className={cn(streaming && "caret")}>
          <AnswerText
            text={streamed}
            citations={answer?.citations ?? []}
            onOpenEvidence={onOpenEvidence}
          />
        </div>
      )}

      {answer && answer.kind === "answer" ? (
        <Footprint answer={answer} onOpenEvidence={onOpenEvidence} />
      ) : null}
    </article>
  );
}

function Footprint({
  answer,
  onOpenEvidence,
}: {
  answer: AnswerView;
  onOpenEvidence: (chunkId: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const verification = answer.verification;
  const clean = verification?.passed ?? true;

  return (
    <div className="mt-5 border-t border-rule">
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        aria-expanded={open}
        className="flex w-full flex-wrap items-center gap-x-5 gap-y-1 py-2 text-left"
      >
        <span className={cn("instrument", clean ? "text-indigo" : "text-oxblood")}>
          {clean
            ? `${verification?.verified_count ?? 0} citations verified`
            : `${verification?.unsupported_count ?? 0} unsupported`}
        </span>
        <span className="instrument text-ink-faint">
          {answer.evidence.length} passages of {answer.retrieval.candidates_considered}
        </span>
        <span className="instrument text-ink-faint">
          {Math.round(answer.timings.total_ms)} ms
        </span>
        {answer.usage.usd > 0 ? (
          <span className="instrument text-ink-faint">${answer.usage.usd.toFixed(5)}</span>
        ) : null}
        <span className="marginal ml-auto underline-offset-4">
          {open ? "hide evidence" : "show evidence"}
        </span>
      </button>

      <AnimatePresence initial={false}>
        {open ? (
          <motion.ul
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: "auto", opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.18 }}
            className="divide-y divide-rule overflow-hidden border-t border-rule"
          >
            {answer.evidence.map((chunk) => (
              <li key={chunk.chunk_id}>
                <button
                  type="button"
                  onClick={() => onOpenEvidence(chunk.chunk_id)}
                  className="w-full py-2.5 text-left transition-colors hover:bg-indigo-wash/40"
                >
                  <div className="flex items-baseline gap-3">
                    <span className="marginal w-6 shrink-0">{chunk.final_rank}</span>
                    <span className="truncate font-medium text-[0.92rem] text-ink">
                      {chunk.heading}
                    </span>
                  </div>
                  <ScoreLadder chunk={chunk} className="mt-1 pl-9" />
                </button>
              </li>
            ))}
          </motion.ul>
        ) : null}
      </AnimatePresence>
    </div>
  );
}

function CorpusIndex({ laws, health }: { laws: LawView[]; health: HealthView | null }) {
  if (laws.length === 0) return null;
  return (
    <section className="sheet">
      <p className="marginal border-b border-rule px-3 py-2">Corpus</p>
      <ul className="divide-y divide-rule">
        {laws.map((law) => (
          <li key={law.law_id}>
            <a
              href={law.official_url}
              target="_blank"
              rel="noopener noreferrer"
              className="group block px-3 py-2"
              title={law.title}
            >
              <div className="flex items-baseline justify-between gap-2">
                <span className="truncate text-[0.86rem] font-medium text-ink group-hover:text-indigo">
                  {law.label}
                </span>
                <span className="instrument shrink-0 text-ink-faint">{law.article_count}</span>
              </div>
              <span className="marginal block truncate normal-case tracking-normal">
                {law.publisher}
              </span>
            </a>
          </li>
        ))}
      </ul>
      {health ? (
        <p className="instrument border-t border-rule px-3 py-2 leading-relaxed text-ink-faint">
          {health.chunks_indexed} chunks
          <br />
          {health.embedding_model.split("/").pop()}
          <br />
          {health.reranker.split("/").pop()}
        </p>
      ) : null}
    </section>
  );
}
