"use client";

import { AnimatePresence, motion } from "framer-motion";
import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AnswerText } from "@/components/AnswerText";
import { Composer } from "@/components/Composer";
import { Dropzone } from "@/components/Dropzone";
import { ThemeToggle } from "@/components/ThemeToggle";
import { SpectrumField, type FieldState } from "@/components/SpectrumField";
import { EvidencePanel } from "@/components/EvidencePanel";
import { ProvenanceRail, type RailState, emptyRail } from "@/components/ProvenanceRail";
import { describeKind, getWorkspace, readSession, type Workspace } from "@/lib/workspace";
import { RefusalCard } from "@/components/RefusalCard";
import { ScoreLadder } from "@/components/ScoreLadder";
import {
  askStream,
  type AskScope,
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
  scope: "law" | "workspace";
  streamed: string;
  answer: AnswerView | null;
  rail: RailState;
  error: string | null;
}

const SUGGESTIONS = [
  { kind: "In your words", text: "How much end-of-service money do I get after 6 years?" },
  { kind: "By article", text: "What does Article 30 of the labour law say?" },
  { kind: "Renting", text: "Can my landlord increase the rent when I renew?" },
  { kind: "Not covered", text: "What is the capital gains tax rate in Singapore?" },
];

export default function Page() {
  const [health, setHealth] = useState<HealthView | null>(null);
  const [laws, setLaws] = useState<LawView[]>([]);
  const [draft, setDraft] = useState("");
  const [lawId, setLawId] = useState<string | null>(null);
  const [exchanges, setExchanges] = useState<Exchange[]>([]);
  const [busy, setBusy] = useState(false);
  const [openChunk, setOpenChunk] = useState<ChunkView | null>(null);
  const [mode, setMode] = useState<AskScope>("law");
  const [workspace, setWorkspace] = useState<Workspace | null>(null);
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

  // Fetched when the mode is first opened rather than on page load: it creates a session
  // server-side, and a visitor who only ever reads the law should not get one.
  useEffect(() => {
    if (mode !== "workspace" || workspace) return;
    const controller = new AbortController();
    getWorkspace(controller.signal).then(setWorkspace).catch(() => setWorkspace(null));
    return () => controller.abort();
  }, [mode, workspace]);

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
        { id, question, scope: mode, streamed: "", answer: null, rail, error: null },
      ]);
      setDraft("");
      setBusy(true);

      const controller = new AbortController();
      abortRef.current = controller;

      await askStream({
        question,
        history,
        lawId: mode === "workspace" ? null : lawId,
        rerank: true,
        scope: mode,
        sessionId: readSession(),
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
    [busy, exchanges, lawId, mode, patch],
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

  // /?mode=workspace opens straight into the upload view, so a demo link lands on the
  // thing being demonstrated rather than on the law library.
  useEffect(() => {
    const requested = new URLSearchParams(window.location.search).get("mode");
    if (requested === "workspace" || requested === "law") setMode(requested);
  }, []);

  const fieldState: FieldState = useMemo(() => {
    const last = exchanges[exchanges.length - 1];
    if (!last) return "idle";
    if (last.answer === null && last.error === null) return "searching";
    if (last.answer?.kind === "refusal") return "refused";
    if (last.answer?.kind === "blocked") return "blocked";
    return last.answer ? "answered" : "idle";
  }, [exchanges]);

  const lastRetrieval = exchanges[exchanges.length - 1]?.rail.retrieval;

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
      <Masthead
        health={health}
        mode={mode}
        onModeChange={setMode}
        documentCount={workspace?.documents.length ?? 0}
      />

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
          {mode === "workspace" ? (
            <div className="cut-in space-y-5 pb-8">
              {exchanges.length === 0 && <WorkspaceOpening />}
              <Dropzone workspace={workspace} onChange={setWorkspace} />
            </div>
          ) : exchanges.length === 0 ? (
            <Opening
              laws={laws}
              onPick={(text) => void send(text)}
              chunkCount={health?.chunks_indexed ?? 181}
              fieldState={fieldState}
              lastRetrieval={lastRetrieval}
              verifiedCount={exchanges[exchanges.length - 1]?.rail.verified ?? 0}
            />
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
              showLawFilter={mode === "law"}
              disabled={health?.status === "degraded"}
            />
          </div>
        </div>

        <div className="hidden w-[19rem] shrink-0 lg:block">
          <div className="sticky top-6 space-y-5">
            <ProvenanceRail state={latest?.rail ?? emptyRail()} />
            {mode === "workspace" ? (
              <WorkspaceSummary workspace={workspace} />
            ) : (
              <CorpusIndex laws={laws} health={health} />
            )}
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

function Masthead({
  health,
  mode,
  onModeChange,
  documentCount,
}: {
  health: HealthView | null;
  mode: AskScope;
  onModeChange: (mode: AskScope) => void;
  documentCount: number;
}) {
  const offline = health?.engine === "offline-extractive";
  return (
    <header className="masthead-rule sticky top-0 z-30 -mx-5 px-5 py-3 sm:-mx-8 sm:px-8">
      <div className="flex flex-wrap items-center justify-between gap-x-6 gap-y-3">
        <div className="flex items-center gap-4">
          <span className="display text-[1.45rem] tracking-[-0.01em] text-ink">LEXORA</span>
          {/* The two things you can search. Named for what they hold, not for how they
              are stored. */}
          <div className="segment" role="group" aria-label="What to search">
            <button
              type="button"
              aria-pressed={mode === "law"}
              onClick={() => onModeChange("law")}
            >
              UAE law
            </button>
            <button
              type="button"
              aria-pressed={mode === "workspace"}
              onClick={() => onModeChange("workspace")}
            >
              My documents{documentCount > 0 ? ` · ${documentCount}` : ""}
            </button>
          </div>
        </div>
        <div className="flex items-center gap-2">
          {/* One word about where answers come from. The model names belong on the
              metrics page, not on the first screen someone sees. */}
          {offline ? (
            <span
              className="stamp text-ochre"
              title="No Claude key is configured yet. Everything else runs — the answer quotes the source instead of rewriting it."
            >
              Quoting sources
            </span>
          ) : health ? (
            <span className="stamp text-indigo">Writing answers</span>
          ) : null}
          <Link href="/metrics" className="btn btn-quiet">
            How well it works
          </Link>
          <ThemeToggle />
        </div>
      </div>
    </header>
  );
}

/**
 * The workspace empty state. It says what to do and what happens to the file, because
 * those are the two questions anyone has before dropping a contract into a website.
 */
function WorkspaceOpening() {
  return (
    <section className="cut-in-slow">
      <h1 className="display max-w-[15ch] text-[clamp(2.2rem,6vw,4rem)] text-ink">
        Ask your own documents.
      </h1>
      <div className="threshold draw-rule my-5 w-full" />
      <p className="statute max-w-[56ch] text-ink-soft">
        A contract, a policy, a lease, a photo of a page. Ask in plain English and get the
        answer with the exact wording attached — or a straight “that is not in this
        document”.
      </p>
      <p className="marginal mt-4 max-w-[56ch]">
        Your file is held in memory for this visit only, never written to disk, and dropped
        when you close the tab.
      </p>
    </section>
  );
}

function Opening({
  laws,
  onPick,
  chunkCount,
  fieldState,
  lastRetrieval,
  verifiedCount,
}: {
  laws: LawView[];
  onPick: (text: string) => void;
  chunkCount: number;
  fieldState: FieldState;
  lastRetrieval?: { candidates: number } | null;
  verifiedCount: number;
}) {
  return (
    <section className="cut-in-slow pb-10">
      <h1 className="display max-w-[16ch] text-[clamp(2.4rem,7vw,4.8rem)] text-ink">
        Answers with the clause attached.
      </h1>
      <p className="display mt-2 max-w-[18ch] text-[clamp(1.4rem,3.6vw,2.3rem)] text-ink-soft">
        Or no answer at all.
      </p>
      {/* The corpus, drawn. This is the hero: a question lights the passages the
          retrievers found and pushes the best above the threshold, and a refusal is the
          picture of nothing crossing it. */}
      <SpectrumField
        className="my-8"
        count={chunkCount}
        state={fieldState}
        retrieved={lastRetrieval?.candidates ?? 0}
        kept={verifiedCount}
      />

      <p className="statute mt-7 max-w-[58ch] text-ink-soft">
        Ask about working in the UAE or renting in Dubai. Every answer quotes the article it
        came from, and every quote opens the real text. When the law does not cover your
        question, Lexora says so instead of guessing — and shows you what it considered.
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
          scope={exchange.scope}
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

/**
 * The workspace's own summary panel, in place of the law library.
 *
 * It states the one thing the law corpus can claim and this cannot: these answers have
 * not been measured. The corpus refusal threshold was fitted against 61 labelled
 * questions about the corpus; on a document uploaded a minute ago there is no labelled
 * set, so saying nothing here would let the interface borrow credibility it has not
 * earned. The API sends `calibrated: false` precisely so this cannot be forgotten.
 */
function WorkspaceSummary({ workspace }: { workspace: Workspace | null }) {
  const documents = workspace?.documents ?? [];
  return (
    <section className="sheet overflow-hidden">
      <div className="border-b border-rule px-3 py-2.5">
        <span className="text-[0.82rem] font-medium text-ink">Your documents</span>
      </div>
      {documents.length === 0 ? (
        <p className="px-3 py-3 text-[0.78rem] leading-snug text-ink-faint">
          Nothing added yet. Drop a file or paste a link to start asking.
        </p>
      ) : (
        <ul>
          {documents.map((document) => (
            <li key={document.doc_id} className="border-b border-rule px-3 py-2 last:border-b-0">
              <p className="truncate text-[0.82rem] text-ink">{document.title}</p>
              <p className="marginal mt-0.5">{describeKind(document)}</p>
            </li>
          ))}
        </ul>
      )}
      {workspace && !workspace.calibrated && (
        <p className="border-t border-rule px-3 py-2.5 text-[0.74rem] leading-snug text-ochre">
          Answers here are not measured. The accuracy figures on the metrics page were
          measured against the UAE law library, not against your file.
        </p>
      )}
    </section>
  );
}

function CorpusIndex({ laws, health }: { laws: LawView[]; health: HealthView | null }) {
  if (laws.length === 0) return null;
  return (
    <section className="sheet">
      <p className="marginal border-b border-rule px-3 py-2">The law library</p>
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
        // Model names moved to the metrics page. On the first screen they read as noise
        // to a visitor and told an engineer nothing they could not find in one click.
        <p className="marginal border-t border-rule px-3 py-2 leading-relaxed">
          {health.chunks_indexed} passages, searchable
        </p>
      ) : null}
    </section>
  );
}
