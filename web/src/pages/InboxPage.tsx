import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import {
  api,
  fmtTime,
  type Connection,
  type ConvStatus,
  type ConversationDetail,
  type ConversationSummary,
  type Kind,
  type Message,
  type Stats,
} from "../api";
import { useWid } from "../auth";
import { ErrorNote, KindIcon, PlatformBadge, Spinner, StatusBadge, errText, platformLabel, useToast } from "../components/ui";
import { ConfidenceMeter } from "./KnowledgePage";

const TABS: { key: string; label: string }[] = [
  { key: "pending_human", label: "Needs human" },
  { key: "open", label: "Open" },
  { key: "auto_replied", label: "Auto-replied" },
  { key: "resolved", label: "Resolved" },
  { key: "", label: "All" },
];
const POLL_MS = 5000;

function usePolling(fn: () => void, ms: number, deps: unknown[]) {
  const ref = useRef(fn);
  ref.current = fn;
  useEffect(() => {
    ref.current();
    const t = setInterval(() => {
      if (document.visibilityState === "visible") ref.current();
    }, ms);
    return () => clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
}

export default function InboxPage() {
  const wid = useWid();
  const [status, setStatus] = useState("pending_human");
  const [platform, setPlatform] = useState("");
  const [kind, setKind] = useState("");
  const [q, setQ] = useState("");
  const [qDebounced, setQDebounced] = useState("");
  const [items, setItems] = useState<ConversationSummary[] | null>(null);
  const [total, setTotal] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<number | null>(null);
  const [stats, setStats] = useState<Stats | null>(null);
  const [conns, setConns] = useState<Connection[]>([]);

  useEffect(() => {
    const t = setTimeout(() => setQDebounced(q.trim()), 350);
    return () => clearTimeout(t);
  }, [q]);

  const loadList = useCallback(async () => {
    try {
      const r = await api.conversations(wid, { status, platform, kind, q: qDebounced, limit: 50 });
      setItems(r.items);
      setTotal(r.total);
      setError(null);
    } catch (e) {
      setError(errText(e));
    }
  }, [wid, status, platform, kind, qDebounced]);

  const loadStats = useCallback(async () => {
    try {
      setStats(await api.stats(wid));
    } catch {
      /* non-critical */
    }
  }, [wid]);

  usePolling(() => void loadList(), POLL_MS, [loadList]);
  usePolling(() => void loadStats(), POLL_MS * 2, [loadStats]);

  useEffect(() => {
    api.connections(wid).then(setConns).catch(() => setConns([]));
  }, [wid]);

  const sandboxConnected = conns.some((c) => c.platform === "sandbox" && c.status === "connected");
  const platformsInUse = Array.from(new Set(conns.map((c) => c.platform)));

  return (
    <div className="page inbox-page">
      <div className="page-head">
        <h1>Inbox</h1>
      </div>

      <div className="stats" aria-label="Inbox statistics">
        <StatCard label="Needs human" value={stats?.pending_human} tone="warn" />
        <StatCard label="Open" value={stats?.open} />
        <StatCard label="Resolved" value={stats?.resolved} tone="ok" />
        <StatCard label="Auto-replied (24h)" value={stats?.auto_replied_24h} tone="ai" />
      </div>

      {sandboxConnected && <SandboxPanel wid={wid} onSent={() => void loadList()} />}

      <div className="tabs" role="tablist" aria-label="Conversation status">
        {TABS.map((t) => (
          <button
            key={t.key || "all"}
            role="tab"
            aria-selected={status === t.key}
            className={`tab ${status === t.key ? "active" : ""}`}
            onClick={() => {
              setStatus(t.key);
              setSelected(null);
            }}
          >
            {t.label}
            {t.key === "pending_human" && stats && stats.pending_human > 0 && (
              <span className="count">{stats.pending_human}</span>
            )}
          </button>
        ))}
      </div>

      <div className="filters">
        <label className="field inline">
          <span className="sr-only">Platform</span>
          <select value={platform} onChange={(e) => setPlatform(e.target.value)} aria-label="Platform filter">
            <option value="">All platforms</option>
            {(platformsInUse.length ? platformsInUse : ["facebook", "instagram", "youtube"]).map((p) => (
              <option key={p} value={p}>
                {platformLabel(p)}
              </option>
            ))}
          </select>
        </label>
        <label className="field inline">
          <span className="sr-only">Type</span>
          <select value={kind} onChange={(e) => setKind(e.target.value)} aria-label="Type filter">
            <option value="">Comments &amp; messages</option>
            <option value="dm">Messages only</option>
            <option value="comment">Comments only</option>
          </select>
        </label>
        <label className="field inline grow">
          <span className="sr-only">Search</span>
          <input type="search" placeholder="Search name or text…" value={q} onChange={(e) => setQ(e.target.value)} />
        </label>
      </div>

      <ErrorNote error={error} />

      <div className={`inbox ${selected != null ? "has-selection" : ""}`}>
        <div className="conv-list card" aria-label="Conversations">
          {items === null ? (
            <Spinner />
          ) : items.length === 0 ? (
            <div className="empty">Nothing here. 🎉</div>
          ) : (
            <ul>
              {items.map((c) => (
                <li key={c.id}>
                  <button
                    className={`conv-item ${selected === c.id ? "selected" : ""}`}
                    onClick={() => setSelected(c.id)}
                    aria-current={selected === c.id}
                  >
                    <PlatformBadge platform={c.platform} />
                    <div className="conv-body">
                      <div className="conv-top">
                        <span className="conv-name">{c.contact?.display_name || "Unknown"}</span>
                        <span className="muted small">{fmtTime(c.updated_at)}</span>
                      </div>
                      <div className="conv-preview">
                        <KindIcon kind={c.kind} />
                        <span>{c.last_message_preview}</span>
                      </div>
                      <div className="conv-meta">
                        <StatusBadge status={c.status} />
                        <span className="muted small">{c.account_name}</span>
                      </div>
                    </div>
                  </button>
                </li>
              ))}
            </ul>
          )}
          {items && total > items.length && <div className="muted small center pad">Showing {items.length} of {total}</div>}
        </div>

        <div className="conv-detail card">
          {selected == null ? (
            <div className="empty">Select a conversation to view it.</div>
          ) : (
            <ConversationView
              key={selected}
              wid={wid}
              cid={selected}
              onBack={() => setSelected(null)}
              onChanged={() => {
                void loadList();
                void loadStats();
              }}
            />
          )}
        </div>
      </div>
    </div>
  );
}

function StatCard({ label, value, tone = "" }: { label: string; value: number | undefined; tone?: string }) {
  return (
    <div className={`stat card stat-${tone}`}>
      <div className="stat-value">{value ?? "–"}</div>
      <div className="stat-label">{label}</div>
    </div>
  );
}

function ConversationView({
  wid,
  cid,
  onBack,
  onChanged,
}: {
  wid: number;
  cid: number;
  onBack: () => void;
  onChanged: () => void;
}) {
  const toast = useToast();
  const [conv, setConv] = useState<ConversationDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [text, setText] = useState("");
  const [sending, setSending] = useState(false);
  const endRef = useRef<HTMLDivElement>(null);
  const lastCount = useRef(0);

  const load = useCallback(async () => {
    try {
      setConv(await api.conversation(wid, cid));
      setError(null);
    } catch (e) {
      setError(errText(e));
    }
  }, [wid, cid]);

  usePolling(() => void load(), POLL_MS, [load]);

  useEffect(() => {
    const n = conv?.messages.length ?? 0;
    if (n !== lastCount.current) {
      lastCount.current = n;
      endRef.current?.scrollIntoView({ block: "end" });
    }
  }, [conv]);

  if (error && !conv) return <ErrorNote error={error} />;
  if (!conv) return <Spinner />;

  const isDm = conv.kind === "dm";
  const rw = conv.reply_window;
  const canReply = !isDm || rw?.dm_human_allowed !== false;
  const drafts = conv.messages.filter((m) => m.delivery_status === "draft");
  const thread = conv.messages.filter((m) => m.delivery_status !== "draft");

  const send = async (e: FormEvent) => {
    e.preventDefault();
    if (!text.trim()) return;
    setSending(true);
    try {
      await api.reply(wid, cid, text.trim());
      setText("");
      toast("Reply queued.", "ok");
      await load();
      onChanged();
    } catch (err) {
      toast(errText(err), "err");
    } finally {
      setSending(false);
    }
  };

  const changeStatus = async (s: ConvStatus) => {
    try {
      await api.setStatus(wid, cid, s);
      await load();
      onChanged();
    } catch (err) {
      toast(errText(err), "err");
    }
  };

  return (
    <div className="conv-view">
      <div className="conv-head">
        <button className="btn btn-ghost btn-sm back-btn" onClick={onBack} aria-label="Back to list">
          ← Back
        </button>
        <PlatformBadge platform={conv.platform} />
        <div className="grow">
          <div className="conv-name">{conv.contact?.display_name || "Unknown"}</div>
          <div className="muted small">
            {isDm ? "Direct message" : "Comment thread"} · {conv.account_name}
          </div>
        </div>
        <StatusBadge status={conv.status} />
      </div>

      <div className="conv-actions">
        {conv.status !== "resolved" ? (
          <button className="btn btn-ghost btn-sm" onClick={() => changeStatus("resolved")}>
            ✓ Resolve
          </button>
        ) : (
          <button className="btn btn-ghost btn-sm" onClick={() => changeStatus("open")}>
            Reopen
          </button>
        )}
        {conv.status !== "pending_human" && (
          <button className="btn btn-ghost btn-sm" onClick={() => changeStatus("pending_human")}>
            Mark needs human
          </button>
        )}
      </div>

      <div className="thread" aria-live="polite">
        {thread.map((m) => (
          <Bubble key={m.id} m={m} />
        ))}
        <div ref={endRef} />
      </div>

      {drafts.map((d) => (
        <DraftCard key={d.id} wid={wid} cid={cid} draft={d} disabled={!canReply} onDone={() => { void load(); onChanged(); }} />
      ))}

      {isDm && rw && (
        <div className={`window-note ${rw.dm_ai_allowed ? "ok" : rw.dm_human_allowed ? "warn" : "closed"}`}>
          {rw.dm_ai_allowed
            ? `Within the 24-hour window${rw.expires_at ? ` (until ${new Date(rw.expires_at).toLocaleString()})` : ""}.`
            : rw.dm_human_allowed
              ? "24-hour window closed — only a human agent can reply (up to 7 days after the customer's last message)."
              : "Messaging window closed (more than 7 days). The platform doesn't allow replies until the customer writes again."}
        </div>
      )}

      <form className="reply-box" onSubmit={send}>
        <label className="sr-only" htmlFor={`reply-${cid}`}>
          Reply
        </label>
        <textarea
          id={`reply-${cid}`}
          rows={3}
          value={text}
          maxLength={2000}
          disabled={!canReply}
          placeholder={canReply ? (isDm ? "Write a message…" : "Write a public reply…") : "Replies are not allowed outside the messaging window"}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
              e.preventDefault();
              (e.currentTarget.form as HTMLFormElement | null)?.requestSubmit();
            }
          }}
        />
        <button className="btn btn-primary" disabled={!canReply || sending || !text.trim()}>
          {sending ? "Sending…" : "Send"}
        </button>
      </form>
    </div>
  );
}

const AUTHOR_LABEL: Record<string, string> = { customer: "Customer", ai: "AI", agent: "Agent" };

function Bubble({ m }: { m: Message }) {
  const side = m.direction === "inbound" ? "in" : "out";
  return (
    <div className={`bubble-row ${side}`}>
      <div className={`bubble author-${m.author}`}>
        <div className="bubble-meta">
          <span className="who">{AUTHOR_LABEL[m.author] ?? m.author}</span>
          <span className="muted">{fmtTime(m.created_at)}</span>
        </div>
        <div className="bubble-text">{m.text}</div>
        {m.author === "ai" && (
          <div className="bubble-ai">
            <ConfidenceMeter value={m.ai_confidence} />
            {m.ai_sources && m.ai_sources.length > 0 && (
              <span className="muted small">Sources: {m.ai_sources.map((s) => s.title).join(", ")}</span>
            )}
          </div>
        )}
        {m.direction === "outbound" && (
          <div className={`delivery d-${m.delivery_status}`}>
            {m.delivery_status === "failed" ? `Failed${m.error ? `: ${m.error}` : ""}` : m.delivery_status}
          </div>
        )}
      </div>
    </div>
  );
}

function DraftCard({
  wid,
  cid,
  draft,
  disabled,
  onDone,
}: {
  wid: number;
  cid: number;
  draft: Message;
  disabled: boolean;
  onDone: () => void;
}) {
  const toast = useToast();
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState(draft.text);
  const [busy, setBusy] = useState(false);

  const approve = async (edited?: string) => {
    setBusy(true);
    try {
      await api.approveDraft(wid, cid, draft.id, edited);
      toast("Draft sent.", "ok");
      onDone();
    } catch (e) {
      toast(errText(e), "err");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="draft card">
      <div className="row between">
        <strong>AI draft</strong>
        <ConfidenceMeter value={draft.ai_confidence} />
      </div>
      {editing ? (
        <textarea rows={4} value={text} maxLength={2000} onChange={(e) => setText(e.target.value)} aria-label="Edit draft" />
      ) : (
        <p className="bubble-text">{draft.text}</p>
      )}
      {draft.ai_sources && draft.ai_sources.length > 0 && (
        <div className="muted small">Sources: {draft.ai_sources.map((s) => s.title).join(", ")}</div>
      )}
      <div className="actions">
        {editing ? (
          <>
            <button className="btn btn-ghost btn-sm" onClick={() => { setEditing(false); setText(draft.text); }}>
              Cancel
            </button>
            <button className="btn btn-primary btn-sm" disabled={busy || disabled || !text.trim()} onClick={() => approve(text.trim())}>
              Send edited
            </button>
          </>
        ) : (
          <>
            <button className="btn btn-ghost btn-sm" disabled={disabled} onClick={() => setEditing(true)}>
              Edit &amp; send
            </button>
            <button className="btn btn-primary btn-sm" disabled={busy || disabled} onClick={() => approve()}>
              Approve
            </button>
          </>
        )}
      </div>
    </div>
  );
}

function SandboxPanel({ wid, onSent }: { wid: number; onSent: () => void }) {
  const toast = useToast();
  const [open, setOpen] = useState(false);
  const [kind, setKind] = useState<Kind>("dm");
  const [author, setAuthor] = useState("Test Customer");
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!text.trim()) return;
    setBusy(true);
    try {
      await api.simulate(wid, { kind, text: text.trim(), author_name: author.trim() || "Test Customer" });
      toast("Simulated message sent. The AI will process it in a moment.", "ok");
      setText("");
      setTimeout(onSent, 1200);
    } catch (err) {
      toast(errText(err), "err");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="card sandbox">
      <button className="sandbox-toggle" aria-expanded={open} onClick={() => setOpen((o) => !o)}>
        <PlatformBadge platform="sandbox" /> Sandbox — simulate an incoming message {open ? "▲" : "▼"}
      </button>
      {open && (
        <form className="sandbox-form" onSubmit={submit}>
          <label className="field">
            <span className="field-label">Type</span>
            <select value={kind} onChange={(e) => setKind(e.target.value as Kind)}>
              <option value="dm">Direct message</option>
              <option value="comment">Comment</option>
            </select>
          </label>
          <label className="field">
            <span className="field-label">Customer name</span>
            <input value={author} onChange={(e) => setAuthor(e.target.value)} maxLength={100} />
          </label>
          <label className="field grow">
            <span className="field-label">Message</span>
            <input value={text} onChange={(e) => setText(e.target.value)} maxLength={2000} placeholder="What is your delivery charge?" />
          </label>
          <button className="btn btn-primary" disabled={busy || !text.trim()}>
            Simulate
          </button>
        </form>
      )}
    </div>
  );
}
