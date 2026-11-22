import { createContext, useCallback, useContext, useState, type ReactNode } from "react";

/* ---------- Toasts ---------- */
type ToastKind = "ok" | "err" | "info";
interface Toast { id: number; kind: ToastKind; text: string }
const ToastCtx = createContext<(text: string, kind?: ToastKind) => void>(() => {});

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const push = useCallback((text: string, kind: ToastKind = "info") => {
    const id = Date.now() + Math.random();
    setToasts((t) => [...t, { id, kind, text }]);
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), 4500);
  }, []);
  return (
    <ToastCtx.Provider value={push}>
      {children}
      <div className="toasts" role="status" aria-live="polite">
        {toasts.map((t) => (
          <div key={t.id} className={`toast toast-${t.kind}`}>
            {t.text}
          </div>
        ))}
      </div>
    </ToastCtx.Provider>
  );
}

export const useToast = () => useContext(ToastCtx);

/* ---------- Platform badge (text badges, no brand logos) ---------- */
const PLATFORM_META: Record<string, { short: string; label: string; cls: string }> = {
  facebook: { short: "FB", label: "Facebook", cls: "pf-facebook" },
  instagram: { short: "IG", label: "Instagram", cls: "pf-instagram" },
  youtube: { short: "YT", label: "YouTube", cls: "pf-youtube" },
  sandbox: { short: "SB", label: "Sandbox", cls: "pf-sandbox" },
};

export function platformLabel(p: string): string {
  return PLATFORM_META[p]?.label ?? p;
}

export function PlatformBadge({ platform, size = "sm" }: { platform: string; size?: "sm" | "lg" }) {
  const m = PLATFORM_META[platform] ?? { short: platform.slice(0, 2).toUpperCase(), label: platform, cls: "pf-other" };
  return (
    <span className={`pf-badge ${m.cls} pf-${size}`} title={m.label} aria-label={m.label}>
      {m.short}
    </span>
  );
}

export function KindIcon({ kind }: { kind: string }) {
  return kind === "dm" ? (
    <svg className="kind-icon" viewBox="0 0 16 16" aria-label="Direct message" role="img">
      <path d="M2 3h12v8H6l-3 3v-3H2z" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinejoin="round" />
    </svg>
  ) : (
    <svg className="kind-icon" viewBox="0 0 16 16" aria-label="Comment" role="img">
      <circle cx="8" cy="7.5" r="5.5" fill="none" stroke="currentColor" strokeWidth="1.4" />
      <path d="M5 7.5h6M5 5.5h6M5 9.5h3" stroke="currentColor" strokeWidth="1.2" />
    </svg>
  );
}

/* ---------- Status badge ---------- */
const STATUS_LABEL: Record<string, string> = {
  open: "Open",
  pending_human: "Needs human",
  auto_replied: "Auto-replied",
  resolved: "Resolved",
  connected: "Connected",
  needs_reconnect: "Needs reconnect",
  disconnected: "Disconnected",
};

export function StatusBadge({ status }: { status: string }) {
  return <span className={`badge st-${status}`}>{STATUS_LABEL[status] ?? status}</span>;
}

export function Spinner({ label = "Loading" }: { label?: string }) {
  return (
    <div className="spinner-wrap" role="status">
      <span className="spinner" aria-hidden="true" />
      <span className="sr-only">{label}</span>
    </div>
  );
}

export function ErrorNote({ error }: { error: string | null }) {
  if (!error) return null;
  return (
    <div className="error-note" role="alert">
      {error}
    </div>
  );
}

export function errText(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}
