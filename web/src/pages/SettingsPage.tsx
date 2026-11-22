import { useEffect, useState, type FormEvent } from "react";
import { api, type Settings } from "../api";
import { useWid } from "../auth";
import { ErrorNote, Spinner, errText, useToast } from "../components/ui";

export default function SettingsPage() {
  const wid = useWid();
  const toast = useToast();
  const [s, setS] = useState<Settings | null>(null);
  const [keywords, setKeywords] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    api
      .settings(wid)
      .then((v) => {
        setS(v);
        setKeywords((v.escalation_keywords ?? []).join(", "));
      })
      .catch((e) => setError(errText(e)));
  }, [wid]);

  const save = async (e: FormEvent) => {
    e.preventDefault();
    if (!s) return;
    setSaving(true);
    try {
      const body: Settings = {
        ...s,
        escalation_keywords: keywords
          .split(",")
          .map((k) => k.trim())
          .filter(Boolean),
      };
      const saved = await api.saveSettings(wid, body);
      setS(saved);
      setKeywords((saved.escalation_keywords ?? []).join(", "));
      toast("Settings saved.", "ok");
    } catch (err) {
      toast(errText(err), "err");
    } finally {
      setSaving(false);
    }
  };

  if (error) return <div className="page"><ErrorNote error={error} /></div>;
  if (!s) return <Spinner />;

  return (
    <div className="page narrow">
      <h1>Settings</h1>
      <form className="card settings" onSubmit={save}>
        <div className="setting-row">
          <div>
            <div className="setting-title" id="ar-label">Auto-reply</div>
            <p className="muted small">When on, the AI replies on its own if it is confident enough. Otherwise it drafts for a human.</p>
          </div>
          <label className="switch">
            <input
              type="checkbox"
              role="switch"
              aria-labelledby="ar-label"
              checked={s.auto_reply_enabled}
              onChange={(e) => setS({ ...s, auto_reply_enabled: e.target.checked })}
            />
            <span className="slider" aria-hidden="true" />
          </label>
        </div>

        <label className="field">
          <span className="field-label">
            Confidence threshold: <strong>{Math.round(s.confidence_threshold * 100)}%</strong>
          </span>
          <input
            type="range"
            min={0}
            max={1}
            step={0.05}
            value={s.confidence_threshold}
            onChange={(e) => setS({ ...s, confidence_threshold: Number(e.target.value) })}
          />
          <span className="muted small">Below this, replies go to the "Needs human" queue as drafts.</span>
        </label>

        <label className="field">
          <span className="field-label">Brand voice</span>
          <textarea
            rows={3}
            maxLength={1000}
            value={s.brand_voice}
            onChange={(e) => setS({ ...s, brand_voice: e.target.value })}
          />
        </label>

        <label className="field">
          <span className="field-label">Reply language</span>
          <select value={s.reply_language} onChange={(e) => setS({ ...s, reply_language: e.target.value })}>
            <option value="auto">Auto (same as customer)</option>
            <option value="en">English</option>
            <option value="bn">Bangla</option>
          </select>
        </label>

        <label className="field">
          <span className="field-label">Escalation keywords</span>
          <input value={keywords} onChange={(e) => setKeywords(e.target.value)} placeholder="refund, complaint, manager" />
          <span className="muted small">Comma-separated. Messages containing any of these always go to a human.</span>
        </label>

        <label className="field">
          <span className="field-label">Business hours note</span>
          <input
            value={s.business_hours_note}
            maxLength={500}
            onChange={(e) => setS({ ...s, business_hours_note: e.target.value })}
            placeholder="We reply 10am–8pm (Sat–Thu)"
          />
        </label>

        <div className="actions">
          <button className="btn btn-primary" disabled={saving}>
            {saving ? "Saving…" : "Save settings"}
          </button>
        </div>
      </form>
    </div>
  );
}
