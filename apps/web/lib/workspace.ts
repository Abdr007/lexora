/**
 * Client for the bring-your-own-document endpoints.
 *
 * The session id is a credential — it is the only thing separating one person's uploaded
 * contract from another's. The server issues it on the `X-Lexora-Session` response header
 * and we send it back on every subsequent request. It is kept in `sessionStorage` rather
 * than `localStorage` deliberately: closing the tab should end the session, which matches
 * what the server does with the documents themselves.
 */

import { API_URL } from "./api";

const SESSION_KEY = "lexora-workspace-session";
const SESSION_HEADER = "X-Lexora-Session";

export interface WorkspaceDocument {
  doc_id: string;
  title: string;
  source: string;
  kind: "pdf" | "docx" | "image" | "html" | "text";
  pages: number;
  chunks: number;
  ocr_engine: string | null;
  /** The document was longer than the page limit and was cut. */
  truncated: boolean;
  /** Fraction of its words that reached the index. 1.0 means nothing was lost. */
  coverage: number;
}

export interface Workspace {
  session_id: string;
  documents: WorkspaceDocument[];
  total_chunks: number;
  /** Always false. The refusal floor was fitted against the law corpus and does not
   *  transfer to a document uploaded a moment ago, so the UI must say so. */
  calibrated: boolean;
  limits: {
    max_documents: number;
    max_bytes: number;
    max_pages: number;
    session_ttl_s: number;
  };
}

export function readSession(): string | null {
  if (typeof window === "undefined") return null;
  try {
    return window.sessionStorage.getItem(SESSION_KEY);
  } catch {
    return null;
  }
}

function writeSession(id: string | null): void {
  if (typeof window === "undefined" || !id) return;
  try {
    window.sessionStorage.setItem(SESSION_KEY, id);
  } catch {
    /* private browsing; the session simply will not survive a reload */
  }
}

function sessionHeaders(extra: HeadersInit = {}): HeadersInit {
  const id = readSession();
  return id ? { ...extra, [SESSION_HEADER]: id } : extra;
}

/** The server's message is written for the person who uploaded the file — show it. */
async function unwrap(response: Response): Promise<Workspace> {
  writeSession(response.headers.get(SESSION_HEADER));
  if (!response.ok) {
    let detail = `Upload failed (${response.status}).`;
    try {
      const body = (await response.json()) as { detail?: string };
      if (body.detail) detail = body.detail;
    } catch {
      /* a non-JSON error body is still a failure; keep the status message */
    }
    throw new Error(detail);
  }
  return (await response.json()) as Workspace;
}

export async function getWorkspace(signal?: AbortSignal): Promise<Workspace> {
  return unwrap(
    await fetch(`${API_URL}/api/workspace`, { headers: sessionHeaders(), signal }),
  );
}

export async function uploadFile(file: File, signal?: AbortSignal): Promise<Workspace> {
  const form = new FormData();
  form.append("file", file);
  // No Content-Type header: the browser must set the multipart boundary itself.
  return unwrap(
    await fetch(`${API_URL}/api/workspace/upload`, {
      method: "POST",
      headers: sessionHeaders(),
      body: form,
      signal,
    }),
  );
}

export async function addLink(url: string, signal?: AbortSignal): Promise<Workspace> {
  return unwrap(
    await fetch(`${API_URL}/api/workspace/link`, {
      method: "POST",
      headers: sessionHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ url }),
      signal,
    }),
  );
}

export async function removeDocument(docId: string): Promise<Workspace> {
  return unwrap(
    await fetch(`${API_URL}/api/workspace/${encodeURIComponent(docId)}`, {
      method: "DELETE",
      headers: sessionHeaders(),
    }),
  );
}

export const ACCEPTED_FILE_TYPES =
  ".pdf,.docx,.txt,.md,.markdown,.rst,.csv,.log,.html,.htm,.png,.jpg,.jpeg,.webp,.tif,.tiff,.bmp";

/** Plain words for a file kind. The interface should not make anyone learn a MIME type. */
export function describeKind(document: WorkspaceDocument): string {
  const pages = document.pages === 1 ? "1 page" : `${document.pages} pages`;
  const base: Record<WorkspaceDocument["kind"], string> = {
    pdf: "PDF",
    docx: "Word document",
    image: "Image",
    html: "Web page",
    text: "Text",
  };
  const read = document.ocr_engine
    ? ` · read by ${document.ocr_engine === "claude-vision" ? "Claude vision" : "OCR"}`
    : "";
  return `${base[document.kind]} · ${pages}${read}`;
}

/**
 * A warning only when there is something to warn about, so it stays meaningful.
 * A partially read document answers as confidently as a complete one, and the reader has
 * no way to tell the difference unless the interface says it.
 */
export function coverageWarning(document: WorkspaceDocument): string | null {
  if (document.truncated) {
    return "Only the first part of this document was read — it is longer than the page limit.";
  }
  if (document.coverage < 0.999) {
    return `About ${Math.round((1 - document.coverage) * 100)}% of this document could not be read.`;
  }
  return null;
}
