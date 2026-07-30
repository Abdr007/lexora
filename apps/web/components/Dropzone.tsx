"use client";

import { useCallback, useRef, useState } from "react";

import {
  ACCEPTED_FILE_TYPES,
  addLink,
  coverageWarning,
  describeKind,
  removeDocument,
  uploadFile,
  type Workspace,
} from "@/lib/workspace";

interface Props {
  workspace: Workspace | null;
  onChange: (workspace: Workspace) => void;
}

/**
 * The upload surface: drop, browse, or paste a link.
 *
 * An empty screen is an invitation to act, so when there is nothing here yet this is the
 * largest target on the page and it names what it takes in plain words. Once a document
 * is in, it shrinks to a list and gets out of the way of the question box.
 */
export function Dropzone({ workspace, onChange }: Props) {
  const [dragging, setDragging] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [url, setUrl] = useState("");
  const fileInput = useRef<HTMLInputElement>(null);

  const documents = workspace?.documents ?? [];
  const full = workspace ? documents.length >= workspace.limits.max_documents : false;

  const run = useCallback(
    async (label: string, action: () => Promise<Workspace>) => {
      setBusy(label);
      setError(null);
      try {
        onChange(await action());
      } catch (cause) {
        // The server writes these messages for the person who uploaded the file — a
        // generic "something went wrong" would throw away the only useful part.
        setError(cause instanceof Error ? cause.message : "That file could not be read.");
      } finally {
        setBusy(null);
      }
    },
    [onChange],
  );

  const send = useCallback(
    (files: FileList | null) => {
      const file = files?.[0];
      if (file) void run(`Reading ${file.name}`, () => uploadFile(file));
    },
    [run],
  );

  return (
    <section aria-labelledby="add-heading" className="space-y-3">
      <h2 id="add-heading" className="sr-only">
        Add a document
      </h2>

      <div
        className="dropzone px-5 py-6 text-center"
        data-active={dragging}
        onDragOver={(event) => {
          event.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(event) => {
          event.preventDefault();
          setDragging(false);
          send(event.dataTransfer.files);
        }}
      >
        <input
          ref={fileInput}
          type="file"
          className="sr-only"
          accept={ACCEPTED_FILE_TYPES}
          onChange={(event) => {
            send(event.target.files);
            event.target.value = "";
          }}
        />

        {busy ? (
          <div className="space-y-3">
            <div className="threshold pulse mx-auto w-2/3" />
            <p className="text-sm text-ink-soft">{busy}…</p>
          </div>
        ) : (
          <>
            <p className="text-base font-medium">Drop a document here</p>
            <p className="mt-1 text-sm text-ink-faint">
              PDF, Word, an image or a photo of a page — scans are read automatically
            </p>
            <div className="mt-4 flex flex-wrap items-center justify-center gap-2">
              <button
                type="button"
                className="btn btn-primary"
                disabled={full}
                onClick={() => fileInput.current?.click()}
              >
                Choose a file
              </button>
              <span className="marginal">or</span>
              <form
                className="flex items-center gap-2"
                onSubmit={(event) => {
                  event.preventDefault();
                  const target = url.trim();
                  if (!target) return;
                  void run(`Fetching ${target}`, () => addLink(target)).then(() => setUrl(""));
                }}
              >
                <label htmlFor="ws-url" className="sr-only">
                  Web address
                </label>
                <input
                  id="ws-url"
                  type="url"
                  value={url}
                  onChange={(event) => setUrl(event.target.value)}
                  placeholder="paste a link"
                  disabled={full}
                  className="w-52 rounded-lg border rule bg-raised px-3 py-2 text-sm outline-none focus-visible:border-indigo"
                />
                <button type="submit" className="btn" disabled={full || !url.trim()}>
                  Add
                </button>
              </form>
            </div>
          </>
        )}
      </div>

      {full && (
        <p className="marginal text-ochre">
          That is the maximum of {workspace?.limits.max_documents} documents. Remove one to add
          another.
        </p>
      )}

      {error && (
        <p role="alert" className="sheet border-oxblood/40 px-4 py-3 text-sm text-oxblood">
          {error}
        </p>
      )}

      {documents.length > 0 && (
        <ul className="space-y-2">
          {documents.map((document) => (
            <li
              key={document.doc_id}
              className="sheet flex items-center justify-between gap-3 px-4 py-3"
            >
              <div className="min-w-0">
                <p className="truncate text-sm font-medium">{document.title}</p>
                <p className="marginal mt-0.5 truncate">{describeKind(document)}</p>
                {coverageWarning(document) && (
                  <p className="mt-1 text-[0.72rem] leading-snug text-ochre">
                    {coverageWarning(document)}
                  </p>
                )}
              </div>
              <button
                type="button"
                className="btn btn-quiet"
                onClick={() => void run("Removing", () => removeDocument(document.doc_id))}
              >
                Remove
              </button>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
