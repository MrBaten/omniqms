import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { AuthProvider, useAuth } from "./auth";
import { Spinner, ToastProvider } from "./components/ui";
import Layout from "./components/Layout";
import AuthPage from "./pages/AuthPage";
import InboxPage from "./pages/InboxPage";
import ConnectionsPage from "./pages/ConnectionsPage";
import KnowledgePage from "./pages/KnowledgePage";
import SettingsPage from "./pages/SettingsPage";

function Protected() {
  const { me, loading, workspace } = useAuth();
  if (loading) return <Spinner />;
  if (!me) return <Navigate to="/login" replace />;
  if (!workspace) {
    return (
      <div className="center-page">
        <div className="card">
          <h1>No workspace</h1>
          <p>Your account is not a member of any workspace yet. Ask an owner to invite you.</p>
        </div>
      </div>
    );
  }
  return <Layout />;
}

function PublicOnly({ mode }: { mode: "login" | "register" }) {
  const { me, loading } = useAuth();
  if (loading) return <Spinner />;
  if (me) return <Navigate to="/inbox" replace />;
  return <AuthPage mode={mode} />;
}

export default function App() {
  return (
    <BrowserRouter basename="/app">
      <ToastProvider>
        <AuthProvider>
          <Routes>
            <Route path="/login" element={<PublicOnly mode="login" />} />
            <Route path="/register" element={<PublicOnly mode="register" />} />
            <Route element={<Protected />}>
              <Route path="/inbox" element={<InboxPage />} />
              <Route path="/connections" element={<ConnectionsPage />} />
              <Route path="/knowledge" element={<KnowledgePage />} />
              <Route path="/settings" element={<SettingsPage />} />
            </Route>
            <Route path="*" element={<Navigate to="/inbox" replace />} />
          </Routes>
        </AuthProvider>
      </ToastProvider>
    </BrowserRouter>
  );
}
