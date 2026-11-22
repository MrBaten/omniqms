// API client for the OmniQMS backend. See PLAN.md "REST API".

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

export type Platform = "facebook" | "instagram" | "youtube" | "sandbox" | string;
export type Kind = "dm" | "comment";
export type ConvStatus = "open" | "pending_human" | "auto_replied" | "resolved";

export interface User { id: number; email: string; name: string }
export interface WorkspaceRef { id: number; name: string; role: string }
export interface Me { user: User; workspaces: WorkspaceRef[] }

export interface PlatformInfo {
  platform: Platform;
  label: string;
  configured: boolean;
  supports_dm: boolean;
  supports_comments: boolean;
}

export interface Connection {
  id: number;
  platform: Platform;
  external_id: string;
  name: string;
  status: "connected" | "needs_reconnect" | "disconnected" | string;
  last_event_at: string | null;
  created_at: string;
}

export interface ConversationSummary {
  id: number;
  platform: Platform;
  kind: Kind;
  status: ConvStatus;
  contact: { display_name: string };
  account_name: string;
  last_message_preview: string;
  last_customer_message_at: string | null;
  updated_at: string;
}

export interface Source { doc_id: number; title: string; snippet?: string }

export interface Message {
  id: number;
  direction: "inbound" | "outbound";
  author: "customer" | "ai" | "agent";
  text: string;
  ai_confidence: number | null;
  ai_sources: Source[] | null;
  delivery_status: "received" | "draft" | "queued" | "sent" | "failed" | string;
  error: string | null;
  created_at: string;
}

export interface ConversationDetail extends ConversationSummary {
  messages: Message[];
  reply_window: { dm_ai_allowed: boolean; dm_human_allowed: boolean; expires_at: string | null };
}

export interface KnowledgeDoc {
  id: number;
  title: string;
  source_type: "text" | "faq" | string;
  content: string;
  created_at: string;
  updated_at: string;
}

export interface TestAnswer {
  answer: string;
  confidence: number;
  needs_human: boolean;
  sources: Source[];
}

export interface Settings {
  auto_reply_enabled: boolean;
  confidence_threshold: number;
  brand_voice: string;
  reply_language: string;
  escalation_keywords: string[];
  business_hours_note: string;
}

export interface Stats {
  open: number;
  pending_human: number;
  resolved: number;
  auto_replied_24h: number;
  by_platform: Record<string, number>;
}

function readCookie(name: string): string | null {
  const parts = document.cookie ? document.cookie.split("; ") : [];
  for (const p of parts) {
    const i = p.indexOf("=");
    if (i > 0 && p.slice(0, i) === name) return decodeURIComponent(p.slice(i + 1));
  }
  return null;
}

const UNSAFE = new Set(["POST", "PUT", "PATCH", "DELETE"]);
const NO_REDIRECT_PATHS = ["/api/auth/login", "/api/auth/register", "/api/auth/me"];

function errorMessage(data: unknown, fallback: string): string {
  if (data && typeof data === "object") {
    const d = data as Record<string, unknown>;
    const detail = d.detail ?? d.error ?? d.message;
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) {
      // FastAPI validation errors: [{loc, msg}]
      return detail
        .map((e) => (e && typeof e === "object" && "msg" in e ? String((e as { msg: unknown }).msg) : String(e)))
        .join("; ");
    }
  }
  return fallback;
}

export async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (UNSAFE.has(method)) {
    const csrf = readCookie("qms_csrf");
    if (csrf) headers["X-CSRF-Token"] = csrf;
  }
  let res: Response;
  try {
    res = await fetch(path, {
      method,
      headers,
      credentials: "include",
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch {
    throw new ApiError(0, "Network error — is the server running?");
  }
  if (res.status === 204) return undefined as T;
  const text = await res.text();
  let data: unknown = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = null;
    }
  }
  if (!res.ok) {
    if (res.status === 401 && !NO_REDIRECT_PATHS.some((p) => path.startsWith(p))) {
      const here = window.location.pathname;
      if (!here.startsWith("/app/login") && !here.startsWith("/app/register")) {
        window.location.assign("/app/login");
      }
    }
    throw new ApiError(res.status, errorMessage(data, `Request failed (${res.status})`));
  }
  return data as T;
}

const w = (wid: number) => `/api/w/${wid}`;
const qs = (params: Record<string, string | number | undefined>) => {
  const u = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== "") u.set(k, String(v));
  const s = u.toString();
  return s ? `?${s}` : "";
};

export const api = {
  me: () => request<Me>("GET", "/api/auth/me"),
  login: (email: string, password: string) => request<Me>("POST", "/api/auth/login", { email, password }),
  register: (b: { email: string; password: string; name: string; workspace_name: string }) =>
    request<Me>("POST", "/api/auth/register", b),
  logout: () => request<void>("POST", "/api/auth/logout"),

  platforms: (wid: number) => request<PlatformInfo[]>("GET", `${w(wid)}/platforms`),
  connections: (wid: number) => request<Connection[]>("GET", `${w(wid)}/connections`),
  startConnect: (wid: number, platform: string) =>
    request<{ authorize_url: string }>("POST", `${w(wid)}/connections/${encodeURIComponent(platform)}/start`),
  disconnect: (wid: number, id: number) => request<void>("DELETE", `${w(wid)}/connections/${id}`),

  conversations: (
    wid: number,
    f: { status?: string; platform?: string; kind?: string; q?: string; limit?: number; offset?: number },
  ) => request<{ items: ConversationSummary[]; total: number }>("GET", `${w(wid)}/conversations${qs(f)}`),
  conversation: (wid: number, cid: number) => request<ConversationDetail>("GET", `${w(wid)}/conversations/${cid}`),
  reply: (wid: number, cid: number, text: string) =>
    request<Message>("POST", `${w(wid)}/conversations/${cid}/reply`, { text }),
  approveDraft: (wid: number, cid: number, mid: number, text?: string) =>
    request<Message>("POST", `${w(wid)}/conversations/${cid}/drafts/${mid}/approve`, text === undefined ? {} : { text }),
  setStatus: (wid: number, cid: number, status: ConvStatus) =>
    request<unknown>("POST", `${w(wid)}/conversations/${cid}/status`, { status }),

  docs: (wid: number) => request<KnowledgeDoc[]>("GET", `${w(wid)}/knowledge`),
  createDoc: (wid: number, d: { title: string; source_type: string; content: string }) =>
    request<KnowledgeDoc>("POST", `${w(wid)}/knowledge`, d),
  updateDoc: (wid: number, id: number, d: { title: string; source_type: string; content: string }) =>
    request<KnowledgeDoc>("PUT", `${w(wid)}/knowledge/${id}`, d),
  deleteDoc: (wid: number, id: number) => request<void>("DELETE", `${w(wid)}/knowledge/${id}`),
  testKb: (wid: number, question: string) => request<TestAnswer>("POST", `${w(wid)}/knowledge/test`, { question }),

  settings: (wid: number) => request<Settings>("GET", `${w(wid)}/settings`),
  saveSettings: (wid: number, s: Settings) => request<Settings>("PUT", `${w(wid)}/settings`, s),
  stats: (wid: number) => request<Stats>("GET", `${w(wid)}/stats`),
  simulate: (wid: number, b: { kind: Kind; text: string; author_name: string }) =>
    request<unknown>("POST", `${w(wid)}/sandbox/simulate`, b),
};

export function fmtTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return "—";
  const diff = (Date.now() - d.getTime()) / 1000;
  if (diff < 60) return "just now";
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}
