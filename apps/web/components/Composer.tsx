"use client";

import { useEffect, useRef } from "react";
import { cn } from "@/lib/cn";
import type { LawView } from "@/lib/api";

const MAX_CHARS = 1000;

export function Composer({
  value,
  onChange,
  onSubmit,
  onStop,
  busy,
  laws,
  lawId,
  onLawChange,
  disabled,
  showLawFilter = true,
}: {
  value: string;
  onChange: (value: string) => void;
  onSubmit: () => void;
  onStop: () => void;
  busy: boolean;
  laws: LawView[];
  lawId: string | null;
  onLawChange: (lawId: string | null) => void;
  disabled: boolean;
  /** Hidden in workspace mode: the filter names laws, and there are none to filter. A
   *  control that cannot affect the result should not be on screen. */
  showLawFilter?: boolean;
}) {
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    const node = textareaRef.current;
    if (!node) return;
    node.style.height = "0px";
    node.style.height = `${Math.min(node.scrollHeight, 168)}px`;
  }, [value]);

  const over = value.length > MAX_CHARS;

  return (
    <div className="sheet border-t-2 border-t-indigo">
      {showLawFilter !== false && (
      <div className="flex flex-wrap items-center gap-x-1.5 gap-y-1 border-b border-rule px-4 py-2">
        <span className="marginal mr-1">Search in</span>
        <ScopeButton active={lawId === null} onClick={() => onLawChange(null)}>
          All
        </ScopeButton>
        {laws.map((law) => (
          <ScopeButton
            key={law.law_id}
            active={lawId === law.law_id}
            onClick={() => onLawChange(law.law_id)}
            title={`${law.title} — ${law.article_count} articles`}
          >
            {law.label}
          </ScopeButton>
        ))}
      </div>
      )}

      <textarea
        ref={textareaRef}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === "Enter" && !event.shiftKey) {
            event.preventDefault();
            if (!busy && !over && value.trim()) onSubmit();
          }
        }}
        rows={1}
        disabled={disabled}
        placeholder="Ask a question in plain English…"
        aria-label="Your question"
        className="statute w-full resize-none bg-transparent px-4 py-3 text-[1.02rem] text-ink placeholder:text-ink-faint focus:outline-none disabled:opacity-50"
      />

      <div className="flex items-center justify-between gap-3 border-t border-rule px-4 py-2">
        <span className="marginal">Enter to send · Shift+Enter for a line break</span>
        <div className="flex items-center gap-3">
          <span className={cn("instrument", over ? "text-oxblood" : "text-ink-faint")}>
            {value.length}/{MAX_CHARS}
          </span>
          {busy ? (
            <button
              type="button"
              onClick={onStop}
              className="marginal border border-rule px-3 py-1 transition-colors hover:border-oxblood hover:text-oxblood"
            >
              Stop
            </button>
          ) : (
            <button
              type="button"
              onClick={onSubmit}
              disabled={disabled || over || !value.trim()}
              className="marginal border border-ink bg-ink px-4 py-1 text-paper transition-opacity hover:opacity-80 disabled:cursor-not-allowed disabled:opacity-30"
            >
              Ask
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

function ScopeButton({
  active,
  onClick,
  children,
  title,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
  title?: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      title={title}
      aria-pressed={active}
      className={cn(
        "marginal border px-2 py-0.5 transition-colors",
        active
          ? "border-ink bg-ink text-paper"
          : "border-rule text-ink-soft hover:border-ink hover:text-ink",
      )}
    >
      {children}
    </button>
  );
}
