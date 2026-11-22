import { useCallback, useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api, fmtTime, type Connection, type PlatformInfo } from "../api";
import { useWid } from "../auth";
import { ErrorNote, PlatformBadge, Spinner, StatusBadge, errText, platformLabel, useToast } from "../components/ui";

const DESCRIPTIONS: Record<string, string> = {
  facebook: "Page comments and Messenger conversations.",
  instagram: "Comments and Direct Messages on your professional account.",
  youtube: "Comments on your channel's videos (YouTube has no DM API).",
  sandbox: "A simulated platform for testing the full pipeline without real credentials.",
};

export default function ConnectionsPage() {
  const wid = useWid();
  const toast = useToast();
  const [params, setParams] = useSearchParams();
  const [platforms, setPlatforms] = useState<PlatformInfo[] | null>(null);
  const [conns, setConns] = useState<Connection[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [p, c] = await Promise.all([api.platforms(wid), api.connections(wid)]);
      setPlatforms(p);
      setConns(c);
      setError(null);
    } catch (e) {
      setError(errText(e));
    }
  }, [wid]);

  useEffect(() => {
    void load();
  }, [load]);

  // Toast from OAuth callback redirect (?connected= / ?error=)
  useEffect(() => {
    const ok = params.get("connected");
    const err = params.get("error");
    if (ok) toast(`${platformLabel(ok)} connected successfully.`, "ok");
    if (err) toast(`Connection failed: ${err.replace(/_/g, " ")}`, "err");
    if (ok || err) setParams({}, { replace: true });
  }, [params, setParams, toast]);

  const connect = async (platform: string) => {
    setBusy(platform);
    try {
      const { authorize_url } = await api.startConnect(wid, platform);
      // Only follow http(s) URLs returned by our backend.
      const u = new URL(authorize_url, window.location.origin);
      if (u.protocol !== "https:" && u.protocol !== "http:") throw new Error("Invalid authorize URL");
      window.location.assign(u.toString());
    } catch (e) {
      toast(errText(e), "err");
      setBusy(null);
    }
  };

  const disconnect = async (c: Connection) => {
    if (!window.confirm(`Disconnect ${c.name || platformLabel(c.platform)}? New messages will stop arriving.`)) return;
    try {
      await api.disconnect(wid, c.id);
      toast("Disconnected.", "ok");
      await load();
    } catch (e) {
      toast(errText(e), "err");
    }
  };

  if (!platforms && !error) return <Spinner />;

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>Connections</h1>
          <p className="muted">Tap Connect, approve access on the platform, and messages start flowing in automatically.</p>
        </div>
      </div>
      <ErrorNote error={error} />

      <div className="platform-grid">
        {(platforms ?? []).map((p) => {
          const count = conns.filter((c) => c.platform === p.platform && c.status !== "disconnected").length;
          return (
            <section key={p.platform} className="card platform-card">
              <div className="platform-card-head">
                <PlatformBadge platform={p.platform} size="lg" />
                <div>
                  <h2>{p.label || platformLabel(p.platform)}</h2>
                  <div className="caps">
                    {p.supports_comments && <span className="chip">Comments</span>}
                    {p.supports_dm && <span className="chip">Messages</span>}
                  </div>
                </div>
              </div>
              <p className="muted small">{DESCRIPTIONS[p.platform] ?? ""}</p>
              {count > 0 && <p className="small">{count} account{count > 1 ? "s" : ""} connected</p>}
              <button
                className="btn btn-primary btn-block btn-lg"
                disabled={!p.configured || busy === p.platform}
                onClick={() => connect(p.platform)}
                aria-describedby={!p.configured ? `nc-${p.platform}` : undefined}
              >
                {busy === p.platform ? "Redirecting…" : count > 0 ? "Connect another" : "Connect"}
              </button>
              {!p.configured && (
                <p id={`nc-${p.platform}`} className="small warn">
                  Not configured — add app credentials
                </p>
              )}
            </section>
          );
        })}
      </div>

      <h2 className="section-title">Connected accounts</h2>
      {conns.length === 0 ? (
        <div className="card empty">No accounts connected yet.</div>
      ) : (
        <div className="card table-card">
          <table className="table">
            <thead>
              <tr>
                <th scope="col">Account</th>
                <th scope="col">Status</th>
                <th scope="col">Last event</th>
                <th scope="col">
                  <span className="sr-only">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {conns.map((c) => (
                <tr key={c.id}>
                  <td>
                    <div className="acct">
                      <PlatformBadge platform={c.platform} />
                      <div>
                        <div className="acct-name">{c.name || c.external_id}</div>
                        <div className="muted small">{platformLabel(c.platform)}</div>
                      </div>
                    </div>
                  </td>
                  <td>
                    <StatusBadge status={c.status} />
                  </td>
                  <td className="small">{fmtTime(c.last_event_at)}</td>
                  <td className="row-actions">
                    {c.status === "needs_reconnect" && (
                      <button className="btn btn-primary btn-sm" onClick={() => connect(c.platform)}>
                        Reconnect
                      </button>
                    )}
                    {c.status !== "disconnected" && (
                      <button className="btn btn-danger-ghost btn-sm" onClick={() => disconnect(c)}>
                        Disconnect
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
