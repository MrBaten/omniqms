import { useState } from "react";
import { NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";
import { useAuth } from "../auth";

const NAV = [
  { to: "/inbox", label: "Inbox" },
  { to: "/connections", label: "Connections" },
  { to: "/knowledge", label: "Knowledge Base" },
  { to: "/settings", label: "Settings" },
];

export default function Layout() {
  const { me, workspace, setWorkspaceId, logout } = useAuth();
  const [open, setOpen] = useState(false);
  const nav = useNavigate();
  const loc = useLocation();
  const workspaces = me?.workspaces ?? [];
  const current = NAV.find((n) => loc.pathname.startsWith(n.to))?.label ?? "";

  return (
    <div className={`shell ${open ? "nav-open" : ""}`}>
      <header className="topbar">
        <button
          className="icon-btn menu-btn"
          aria-label={open ? "Close menu" : "Open menu"}
          aria-expanded={open}
          onClick={() => setOpen((o) => !o)}
        >
          <span aria-hidden="true">☰</span>
        </button>
        <span className="topbar-title">{current}</span>
      </header>

      <aside className="sidebar" aria-label="Main navigation">
        <div className="brand">
          <span className="brand-mark" aria-hidden="true">Q</span>
          <span>OmniQMS</span>
        </div>

        {workspaces.length > 1 ? (
          <label className="ws-switch">
            <span className="field-label">Workspace</span>
            <select
              value={workspace?.id}
              onChange={(e) => {
                setWorkspaceId(Number(e.target.value));
                setOpen(false);
              }}
            >
              {workspaces.map((w) => (
                <option key={w.id} value={w.id}>
                  {w.name}
                </option>
              ))}
            </select>
          </label>
        ) : (
          <div className="ws-name" title="Workspace">
            {workspace?.name}
          </div>
        )}

        <nav>
          {NAV.map((n) => (
            <NavLink key={n.to} to={n.to} className="nav-link" onClick={() => setOpen(false)}>
              {n.label}
            </NavLink>
          ))}
        </nav>

        <div className="sidebar-foot">
          <div className="me">
            <div className="me-name">{me?.user.name || me?.user.email}</div>
            <div className="me-email">{me?.user.email}</div>
          </div>
          <button
            className="btn btn-ghost btn-block"
            onClick={async () => {
              await logout();
              nav("/login", { replace: true });
            }}
          >
            Log out
          </button>
        </div>
      </aside>
      {open && <div className="scrim" onClick={() => setOpen(false)} aria-hidden="true" />}

      <main className="content" key={workspace?.id}>
        <Outlet />
      </main>
    </div>
  );
}
