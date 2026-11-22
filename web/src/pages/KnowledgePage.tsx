import { useCallback, useEffect, useState, type FormEvent } from "react";
import { api, fmtTime, type KnowledgeDoc, type TestAnswer } from "../api";
import { useWid } from "../auth";
import { ErrorNote, Spinner, errText, useToast } from "../components/ui";

interface Draft {
  id: number | null;
  title: string;
  source_type: string;
  content: string;
}
const EMPTY: Draft = { id: null, title: "", source_type: "text", content: "" };
const MAX_CONTENT = 200_000;

export default function KnowledgePage() {
  const wid = useWid();
  const toast = useToast();
  const [docs, setDocs] = useState<KnowledgeDoc[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [saving, setSaving] = useState(false);
  const [question, setQuestion] = useState("");
  const [testing, setTesting] = useState(false);
  const [answer, setAnswer] = useState<TestAnswer | null>(null);

  const load = useCallback(async () => {
    try {
      setDocs(await api.docs(wid));
      setError(null);
    } catch (e) {
      setError(errText(e));
    }
  }, [wid]);

  useEffect(() => {
    void load();
  }, [load]);

  const save = async (e: FormEvent) => {
    e.preventDefault();
    if (!draft) return;
    if (!draft.title.trim() || !draft.content.trim()) {
      toast("Title and content are required.", "err");
      return;
    }
    setSaving(true);
    try {
      const body = { title: draft.title.trim(), source_type: draft.source_type, content: draft.content };
      if (draft.id == null) await api.createDoc(wid, body);
      else await api.updateDoc(wid, draft.id, body);
      toast("Saved.", "ok");
      setDraft(null);
      await load();
    } catch (err) {
      toast(errText(err), "err");
    } finally {
      setSaving(false);
    }
  };

  const remove = async (d: KnowledgeDoc) => {
    if (!window.confirm(`Delete "${d.title}"? The AI will stop using it.`)) return;
    try {
      await api.deleteDoc(wid, d.id);
      toast("Deleted.", "ok");
      if (draft?.id === d.id) setDraft(null);
      await load();
    } catch (e) {
      toast(errText(e), "err");
    }
  };

  const test = async (e: FormEvent) => {
    e.preventDefault();
    if (!question.trim()) return;
    setTesting(true);
    setAnswer(null);
    try {
      setAnswer(await api.testKb(wid, question.trim()));
    } catch (err) {
      toast(errText(err), "err");
    } finally {
      setTesting(false);
    }
  };

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>Knowledge Base</h1>
          <p className="muted">Everything the AI is allowed to answer from: products, prices, policies, FAQs.</p>
        </div>
        <button className="btn btn-primary" onClick={() => setDraft({ ...EMPTY })}>
          + Add document
        </button>
      </div>
      <ErrorNote error={error} />

      <div className="kb-layout">
        <div className="kb-main">
          {draft && (
            <form className="card kb-editor" onSubmit={save}>
              <h2>{draft.id == null ? "New document" : "Edit document"}</h2>
              <div className="row">
                <label className="field grow">
                  <span className="field-label">Title</span>
                  <input
                    value={draft.title}
                    onChange={(e) => setDraft({ ...draft, title: e.target.value })}
                    maxLength={300}
                    required
                  />
                </label>
                <label className="field">
                  <span className="field-label">Type</span>
                  <select value={draft.source_type} onChange={(e) => setDraft({ ...draft, source_type: e.target.value })}>
                    <option value="text">Text</option>
                    <option value="faq">FAQ</option>
                  </select>
                </label>
              </div>
              <label className="field">
                <span className="field-label">
                  Content{" "}
                  <span className="muted small">
                    {draft.source_type === "faq" ? "(use Q: … / A: … pairs)" : ""} {draft.content.length.toLocaleString()}/
                    {MAX_CONTENT.toLocaleString()}
                  </span>
                </span>
                <textarea
                  rows={14}
                  value={draft.content}
                  maxLength={MAX_CONTENT}
                  onChange={(e) => setDraft({ ...draft, content: e.target.value })}
                  placeholder={
                    draft.source_type === "faq"
                      ? "Q: What are your delivery charges?\nA: Inside Dhaka 60 BDT, outside Dhaka 120 BDT."
                      : "Paste product details, policies, opening hours…"
                  }
                  required
                />
              </label>
              <div className="actions">
                <button className="btn btn-ghost" type="button" onClick={() => setDraft(null)}>
                  Cancel
                </button>
                <button className="btn btn-primary" type="submit" disabled={saving}>
                  {saving ? "Saving…" : "Save"}
                </button>
              </div>
            </form>
          )}

          {docs === null && !error ? (
            <Spinner />
          ) : docs && docs.length === 0 ? (
            <div className="card empty">No documents yet. Add your first FAQ so the AI has something to answer from.</div>
          ) : (
            <ul className="doc-list">
              {(docs ?? []).map((d) => (
                <li key={d.id} className="card doc-item">
                  <div className="doc-info">
                    <div className="doc-title">
                      {d.title} <span className="chip">{d.source_type.toUpperCase()}</span>
                    </div>
                    <div className="muted small doc-preview">{d.content.slice(0, 180)}</div>
                    <div className="muted small">Updated {fmtTime(d.updated_at)}</div>
                  </div>
                  <div className="row-actions">
                    <button
                      className="btn btn-ghost btn-sm"
                      onClick={() => setDraft({ id: d.id, title: d.title, source_type: d.source_type, content: d.content })}
                    >
                      Edit
                    </button>
                    <button className="btn btn-danger-ghost btn-sm" onClick={() => remove(d)}>
                      Delete
                    </button>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </div>

        <aside className="card kb-test" aria-label="Test the AI">
          <h2>Test the AI</h2>
          <p className="muted small">Ask what a customer might ask and see the answer, confidence and sources.</p>
          <form onSubmit={test}>
            <label className="field">
              <span className="field-label">Customer question</span>
              <textarea rows={3} value={question} onChange={(e) => setQuestion(e.target.value)} maxLength={2000} />
            </label>
            <button className="btn btn-primary btn-block" disabled={testing || !question.trim()}>
              {testing ? "Thinking…" : "Ask"}
            </button>
          </form>
          {answer && (
            <div className="test-result">
              <div className="row between">
                <ConfidenceMeter value={answer.confidence} />
                {answer.needs_human && <span className="badge st-pending_human">Would go to a human</span>}
              </div>
              <p className="answer-text">{answer.answer || <em className="muted">(no answer)</em>}</p>
              {answer.sources.length > 0 && (
                <div>
                  <div className="field-label">Sources</div>
                  <ul className="sources">
                    {answer.sources.map((s, i) => (
                      <li key={`${s.doc_id}-${i}`}>
                        <strong>{s.title}</strong>
                        {s.snippet && <div className="muted small">{s.snippet}</div>}
                      </li>
                    ))}
                  </ul>
                </div>
              )}
            </div>
          )}
        </aside>
      </div>
    </div>
  );
}

export function ConfidenceMeter({ value }: { value: number | null }) {
  if (value == null) return null;
  const pct = Math.round(Math.max(0, Math.min(1, value)) * 100);
  const cls = pct >= 70 ? "hi" : pct >= 45 ? "mid" : "lo";
  return (
    <span className={`conf conf-${cls}`} title="AI confidence">
      <span className="conf-bar" aria-hidden="true">
        <span style={{ width: `${pct}%` }} />
      </span>
      {pct}% confidence
    </span>
  );
}
