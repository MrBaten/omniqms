import { useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api } from "../api";
import { useAuth } from "../auth";
import { ErrorNote, errText } from "../components/ui";

export default function AuthPage({ mode }: { mode: "login" | "register" }) {
  const { setMe } = useAuth();
  const nav = useNavigate();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [name, setName] = useState("");
  const [workspaceName, setWorkspaceName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const isReg = mode === "register";

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setError(null);
    if (isReg && password.length < 8) {
      setError("Password must be at least 8 characters.");
      return;
    }
    setBusy(true);
    try {
      const me = isReg
        ? await api.register({ email, password, name, workspace_name: workspaceName })
        : await api.login(email, password);
      setMe(me);
      nav(isReg ? "/connections" : "/inbox", { replace: true });
    } catch (err) {
      setError(errText(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="center-page">
      <form className="card auth-card" onSubmit={submit} noValidate>
        <div className="brand brand-center">
          <span className="brand-mark" aria-hidden="true">Q</span>
          <span>OmniQMS</span>
        </div>
        <h1>{isReg ? "Create your workspace" : "Sign in"}</h1>
        <p className="muted">
          {isReg
            ? "One inbox for Facebook, Instagram and YouTube comments and messages."
            : "Welcome back. Sign in to your workspace."}
        </p>
        {isReg && (
          <>
            <label className="field">
              <span className="field-label">Your name</span>
              <input value={name} onChange={(e) => setName(e.target.value)} autoComplete="name" required />
            </label>
            <label className="field">
              <span className="field-label">Company / workspace name</span>
              <input value={workspaceName} onChange={(e) => setWorkspaceName(e.target.value)} required />
            </label>
          </>
        )}
        <label className="field">
          <span className="field-label">Email</span>
          <input type="email" value={email} onChange={(e) => setEmail(e.target.value)} autoComplete="email" required />
        </label>
        <label className="field">
          <span className="field-label">Password</span>
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete={isReg ? "new-password" : "current-password"}
            minLength={isReg ? 8 : undefined}
            required
          />
        </label>
        <ErrorNote error={error} />
        <button className="btn btn-primary btn-block" disabled={busy} type="submit">
          {busy ? "Please wait…" : isReg ? "Create workspace" : "Sign in"}
        </button>
        <p className="muted center">
          {isReg ? (
            <>
              Already have an account? <Link to="/login">Sign in</Link>
            </>
          ) : (
            <>
              New here? <Link to="/register">Create a workspace</Link>
            </>
          )}
        </p>
      </form>
    </div>
  );
}
