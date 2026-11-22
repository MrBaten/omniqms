import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";
import { api, type Me, type WorkspaceRef } from "./api";

interface AuthState {
  me: Me | null;
  loading: boolean;
  workspace: WorkspaceRef | null;
  setWorkspaceId: (id: number) => void;
  refresh: () => Promise<void>;
  setMe: (me: Me | null) => void;
  logout: () => Promise<void>;
}

const Ctx = createContext<AuthState | null>(null);
const WS_KEY = "omniqms.workspace";

function storedWs(): number | null {
  try {
    const v = localStorage.getItem(WS_KEY);
    return v ? Number(v) : null;
  } catch {
    return null;
  }
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [me, setMe] = useState<Me | null>(null);
  const [loading, setLoading] = useState(true);
  const [wsId, setWsId] = useState<number | null>(storedWs());

  const refresh = useCallback(async () => {
    try {
      setMe(await api.me());
    } catch {
      setMe(null);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const setWorkspaceId = (id: number) => {
    setWsId(id);
    try {
      localStorage.setItem(WS_KEY, String(id));
    } catch {
      /* storage unavailable */
    }
  };

  const workspaces = me?.workspaces ?? [];
  const workspace = workspaces.find((w) => w.id === wsId) ?? workspaces[0] ?? null;

  const logout = async () => {
    try {
      await api.logout();
    } catch {
      /* ignore */
    }
    setMe(null);
  };

  return (
    <Ctx.Provider value={{ me, loading, workspace, setWorkspaceId, refresh, setMe, logout }}>
      {children}
    </Ctx.Provider>
  );
}

export function useAuth(): AuthState {
  const v = useContext(Ctx);
  if (!v) throw new Error("useAuth outside provider");
  return v;
}

/** Current workspace id; only use inside authenticated layout. */
export function useWid(): number {
  const { workspace } = useAuth();
  if (!workspace) throw new Error("no workspace");
  return workspace.id;
}
