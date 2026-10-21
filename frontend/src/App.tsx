import { createContext, useCallback, useContext, useEffect, useState } from "react";
import { Link, Navigate, Route, Routes, useLocation } from "react-router-dom";
import { api } from "./lib/api";
import type { Protocol, User } from "./lib/types";
import LoginPage from "./pages/LoginPage";
import CasesPage from "./pages/CasesPage";
import WorkspacePage from "./pages/WorkspacePage";
import ProjectPage from "./pages/ProjectPage";

interface Ctx {
  user: User;
  protocol: Protocol;
  reloadProtocol: () => Promise<void>;
  logout: () => Promise<void>;
}

const AppCtx = createContext<Ctx | null>(null);

export function useApp(): Ctx {
  const c = useContext(AppCtx);
  if (!c) throw new Error("AppCtx missing");
  return c;
}

export default function App() {
  const [user, setUser] = useState<User | null | undefined>(undefined);
  const [protocol, setProtocol] = useState<Protocol | null>(null);
  const loc = useLocation();

  const reloadProtocol = useCallback(async () => {
    setProtocol(await api.get<Protocol>("/api/protocol"));
  }, []);

  useEffect(() => {
    api
      .get<User>("/api/auth/me")
      .then(setUser)
      .catch(() => setUser(null));
    const onUnauth = () => setUser(null);
    window.addEventListener("ssi-unauthorized", onUnauth);
    return () => window.removeEventListener("ssi-unauthorized", onUnauth);
  }, []);

  useEffect(() => {
    if (user) reloadProtocol();
  }, [user, reloadProtocol]);

  const logout = useCallback(async () => {
    await api.post("/api/auth/logout");
    setUser(null);
  }, []);

  if (user === undefined) return <div className="center muted">加载中…</div>;
  if (user === null) return <LoginPage onLogin={setUser} />;
  if (!protocol) return <div className="center muted">加载标注协议…</div>;

  const inWorkspace = loc.pathname.startsWith("/a/");
  const isReviewer = user.role === "admin" || user.role === "reviewer";

  return (
    <AppCtx.Provider value={{ user, protocol, reloadProtocol, logout }}>
      <div className={inWorkspace ? "app app-workspace" : "app"}>
        <header className="topbar">
          <Link to="/" className="brand">
            UBE SSI 标注平台 <span className="muted small">Protocol {protocol.version}</span>
          </Link>
          <nav>
            <Link to="/">病例</Link>
            {isReviewer && <Link to="/project">项目与质控</Link>}
          </nav>
          <div className="spacer" />
          <span className="muted small">
            {user.display_name}（{user.username} · {roleName(user.role)}）
          </span>
          <button className="btn ghost" onClick={logout}>
            退出
          </button>
        </header>
        <main>
          <Routes>
            <Route path="/" element={<CasesPage />} />
            <Route path="/a/:aid" element={<WorkspacePage key={loc.pathname} />} />
            <Route path="/project" element={isReviewer ? <ProjectPage /> : <Navigate to="/" />} />
            <Route path="*" element={<Navigate to="/" />} />
          </Routes>
        </main>
      </div>
    </AppCtx.Provider>
  );
}

export function roleName(r: string) {
  return (
    { admin: "管理员", reviewer: "复核专家", annotator: "标注者", primary: "主标注", secondary: "重复标注", pilot: "Pilot", consensus: "共识/裁决" } as Record<string, string>
  )[r] ?? r;
}

export function statusName(s: string) {
  return (
    { in_progress: "标注中", submitted: "已提交", returned: "已退回", approved: "复核通过", open: "进行中", locked: "已锁定", processing: "视频处理中", ready: "就绪", error: "出错" } as Record<string, string>
  )[s] ?? s;
}
