import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { lazy, Suspense, useEffect, useState } from "react";
import {
  createBrowserRouter,
  createRoutesFromElements,
  Navigate,
  Outlet,
  Route,
  RouterProvider,
  useLocation,
} from "react-router-dom";
import { SettingsEnvironment } from "../features/settings/SettingsEnvironment";

import { AuthProvider } from "../features/auth/AuthProvider";
import { GuestOnly, RequireAuth } from "../features/auth/AuthRoutes";

const AuthPage = lazy(() =>
  import("../features/auth/AuthPage").then((module) => ({
    default: module.AuthPage,
  })),
);
const AgentsPage = lazy(() =>
  import("../features/agents/AgentsPage").then((module) => ({
    default: module.AgentsPage,
  })),
);
const AgentDetailPage = lazy(() =>
  import("../features/agents/AgentDetailPage").then((module) => ({
    default: module.AgentDetailPage,
  })),
);
const OverviewPage = lazy(() =>
  import("../features/overview/OverviewPage").then((module) => ({
    default: module.OverviewPage,
  })),
);
const PlaceholderPage = lazy(() =>
  import("../features/placeholder/PlaceholderPage").then((module) => ({
    default: module.PlaceholderPage,
  })),
);
const SkillDetailPage = lazy(() =>
  import("../features/skills/SkillDetailPage").then((module) => ({
    default: module.SkillDetailPage,
  })),
);
const SkillsPage = lazy(() =>
  import("../features/skills/SkillsPage").then((module) => ({
    default: module.SkillsPage,
  })),
);
const AppShell = lazy(() =>
  import("./AppShell").then((module) => ({ default: module.AppShell })),
);
const RunsPage = lazy(() =>
  import("../features/runs/RunsPage").then((module) => ({
    default: module.RunsPage,
  })),
);
const RunDetailPage = lazy(() =>
  import("../features/runs/RunDetailPage").then((module) => ({
    default: module.RunDetailPage,
  })),
);
const ConnectionsPage = lazy(() =>
  import("../features/context/ConnectionsPage").then((module) => ({
    default: module.ConnectionsPage,
  })),
);
const CoordinationPage = lazy(() =>
  import("../features/coordination/CoordinationPage").then((module) => ({
    default: module.CoordinationPage,
  })),
);
const ProjectsPage = lazy(() =>
  import("../features/projects/ProjectsPage").then((module) => ({
    default: module.ProjectsPage,
  })),
);
const CodeSettingsPage = lazy(() =>
  import("../features/settings/CodeSettingsPage").then((module) => ({
    default: module.CodeSettingsPage,
  })),
);
const ProjectDetailPage = lazy(() =>
  import("../features/projects/ProjectsPage").then((module) => ({
    default: module.ProjectDetailPage,
  })),
);

const ProfileSettingsPage = lazy(() =>
  import("../features/settings/ProfileSettingsPage").then((module) => ({
    default: module.ProfileSettingsPage,
  })),
);
const BusinessSpaceSettingsPage = lazy(() =>
  import("../features/settings/BusinessSpaceSettingsPage").then((module) => ({
    default: module.BusinessSpaceSettingsPage,
  })),
);
const HarnessSettingsPage = lazy(() =>
  import("../features/settings/HarnessSettingsPage").then((module) => ({
    default: module.HarnessSettingsPage,
  })),
);
const ModelSettingsPage = lazy(() =>
  import("../features/settings/ModelSettingsPage").then((module) => ({
    default: module.ModelSettingsPage,
  })),
);
const FeishuSettingsPage = lazy(() =>
  import("../features/settings/FeishuSettingsPage").then((module) => ({
    default: module.FeishuSettingsPage,
  })),
);
const CoordinationSettingsPage = lazy(() =>
  import("../features/settings/CoordinationSettingsPage").then((module) => ({
    default: module.CoordinationSettingsPage,
  })),
);

function ScrollToTop() {
  const { pathname } = useLocation();

  useEffect(() => {
    // 详情页比列表页更长；切换资产时从页面顶部开始，避免继承上一页的滚动位置。
    document.documentElement.scrollTop = 0;
    document.body.scrollTop = 0;
  }, [pathname]);

  return null;
}

export function App() {
  // 每个应用实例持有独立 QueryClient，避免测试和服务端渲染时共享缓存。
  const [queryClient] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            retry: 1,
            staleTime: 30_000,
          },
        },
      }),
  );

  const [router] = useState(() =>
    createBrowserRouter(
      createRoutesFromElements(
        <Route
          element={
            <>
              <ScrollToTop />
              <Outlet />
            </>
          }
        >
          <Route
            path="login"
            element={
              <GuestOnly>
                <AuthPage mode="login" />
              </GuestOnly>
            }
          />
          <Route
            path="register"
            element={
              <GuestOnly>
                <AuthPage mode="register" />
              </GuestOnly>
            }
          />
          <Route element={<RequireAuth />}>
            <Route
              element={
                <SettingsEnvironment>
                  <AppShell />
                </SettingsEnvironment>
              }
            >
              <Route index element={<OverviewPage />} />
              <Route path="agents" element={<AgentsPage />} />
              <Route path="agents/:agentId" element={<AgentDetailPage />} />
              <Route path="skills" element={<SkillsPage />} />
              <Route path="skills/:skillId" element={<SkillDetailPage />} />
              <Route
                path="workflows"
                element={
                  <PlaceholderPage
                    eyebrow="Orchestration"
                    title="工作流"
                    description="编排模型、工具、知识库和人工审批节点。"
                  />
                }
              />
              <Route
                path="knowledge"
                element={
                  <PlaceholderPage
                    eyebrow="Knowledge"
                    title="知识库"
                    description="管理文档、切片、索引、检索策略和引用来源。"
                  />
                }
              />
              <Route
                path="rooms"
                element={
                  <PlaceholderPage
                    eyebrow="Collaboration"
                    title="协作室"
                    description="让人类和多个 Agent 围绕任务、审批和交付物协作。"
                  />
                }
              />
              <Route path="runs" element={<RunsPage />} />
              <Route path="runs/:runId" element={<RunDetailPage />} />
              <Route path="context" element={<ConnectionsPage />} />
              <Route path="coordination" element={<CoordinationPage />} />
              <Route
                path="settings"
                element={<Navigate replace to="/settings/profile" />}
              />
              <Route
                path="settings/profile"
                element={<ProfileSettingsPage />}
              />
              <Route path="settings/code" element={<CodeSettingsPage />} />
              <Route
                path="settings/business-space"
                element={<BusinessSpaceSettingsPage />}
              />
              <Route path="settings/models" element={<ModelSettingsPage />} />
              <Route path="settings/harness" element={<HarnessSettingsPage />} />
              <Route path="settings/feishu" element={<FeishuSettingsPage />} />
              <Route
                path="settings/coordination"
                element={<CoordinationSettingsPage />}
              />
              <Route path="projects" element={<ProjectsPage />} />
              <Route
                path="projects/:projectId"
                element={<ProjectDetailPage />}
              />
            </Route>
          </Route>
        </Route>,
      ),
      { future: { v7_relativeSplatPath: true } },
    ),
  );
  return (
    <QueryClientProvider client={queryClient}>
      <AuthProvider>
        <Suspense fallback={<RouteLoading />}>
          <RouterProvider
            router={router}
            future={{ v7_startTransition: true }}
          />
        </Suspense>
      </AuthProvider>
    </QueryClientProvider>
  );
}

function RouteLoading() {
  return (
    <main aria-label="正在加载页面" className="route-loading">
      <span />
      <strong>正在加载</strong>
    </main>
  );
}
