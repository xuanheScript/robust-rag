import { useQuery } from "@tanstack/react-query";
import { lazy, Suspense } from "react";
import { NavLink, Navigate, Outlet, Route, Routes } from "react-router-dom";

import { getSystemInfo } from "@/lib/api";
import { DashboardPage } from "@/pages/DashboardPage";
import { DocumentsPage } from "@/pages/DocumentsPage";
import { GraphPage } from "@/pages/GraphPage";
import { JobsPage } from "@/pages/JobsPage";
import { SystemPage } from "@/pages/SystemPage";

const ChatPage = lazy(() => import("@/pages/ChatPage").then((module) => ({ default: module.ChatPage })));

function ChatRoute() {
  return <Suspense fallback={<div className="loading-state" role="status">正在加载问答界面</div>}><ChatPage /></Suspense>;
}

const navigation = [
  { to: "/overview", label: "总览", icon: "⌂" },
  { to: "/documents", label: "文档", icon: "▤" },
  { to: "/jobs", label: "任务", icon: "↻" },
  { to: "/chat", label: "问答", icon: "✦" },
  { to: "/graph", label: "知识图谱", icon: "⌘" },
  { to: "/system", label: "系统状态", icon: "◉" },
];

function AppLayout() {
  const systemInfo = useQuery({
    queryKey: ["system-info"],
    queryFn: ({ signal }) => getSystemInfo(signal),
    retry: 1,
    refetchInterval: 30_000,
  });
  return (
    <div className="app-frame">
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-mark">R</span>
          <div>
            <strong>Robust RAG</strong>
            <small>Knowledge operations</small>
          </div>
        </div>
        <nav aria-label="主导航">
          {navigation.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              className={({ isActive }) => (isActive ? "nav-link active" : "nav-link")}
            >
              <span aria-hidden="true">{item.icon}</span>
              {item.label}
            </NavLink>
          ))}
        </nav>
        <div className="sidebar-status">
          <span className={systemInfo.isSuccess ? "health-dot healthy" : "health-dot"} />
          <div>
            <strong>{systemInfo.isSuccess ? "服务已连接" : "正在检查服务"}</strong>
            <small>{systemInfo.data ? `v${systemInfo.data.version}` : "本机管理端"}</small>
          </div>
        </div>
      </aside>
      <main className="workspace">
        <Outlet />
      </main>
    </div>
  );
}

export default function App() {
  return (
    <Routes>
      <Route element={<AppLayout />}>
        <Route index element={<Navigate to="/overview" replace />} />
        <Route path="/overview" element={<DashboardPage />} />
        <Route path="/documents" element={<DocumentsPage />} />
        <Route path="/jobs" element={<JobsPage />} />
        <Route path="/chat" element={<ChatRoute />} />
        <Route path="/chat/:conversationId" element={<ChatRoute />} />
        <Route path="/graph" element={<GraphPage />} />
        <Route path="/system" element={<SystemPage />} />
        <Route path="*" element={<Navigate to="/overview" replace />} />
      </Route>
    </Routes>
  );
}
