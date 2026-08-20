import { lazy, Suspense } from "react";
import { NavLink, Navigate, Outlet, Route, Routes, useLocation } from "react-router-dom";

import { DashboardPage } from "@/pages/DashboardPage";
import { DocumentsPage } from "@/pages/DocumentsPage";
import { GraphPage } from "@/pages/GraphPage";
import { JobsPage } from "@/pages/JobsPage";
import { QualityReviewPage } from "@/pages/QualityReviewPage";
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
  const location = useLocation();
  const isChatRoute = location.pathname === "/chat" || location.pathname.startsWith("/chat/");
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
      </aside>
      <main className={isChatRoute ? "workspace workspace-chat" : "workspace"}>
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
        <Route path="/documents/:documentId/quality-review" element={<QualityReviewPage />} />
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
