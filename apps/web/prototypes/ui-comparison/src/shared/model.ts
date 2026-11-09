import { useEffect, useState } from "react";

export type TaskStatus = "running" | "waiting" | "done";
export type View = "tasks" | "agents" | "projects" | "settings";
export type Task = {
  id: string;
  title: string;
  agent: string;
  project: string;
  status: TaskStatus;
  time: string;
  description: string;
};
export const statusLabel: Record<TaskStatus, string> = {
  running: "运行中",
  waiting: "等待审批",
  done: "已完成",
};
export const agents = [
  {
    id: "review",
    name: "代码审查助手",
    role: "发现问题，让每一次提交更可靠",
    tag: "研发",
    initials: "CR",
    color: "blue",
    version: "v1.4.0",
    skills: 4,
    runs: 128,
  },
  {
    id: "research",
    name: "产品研究员",
    role: "从资料到洞察，整理有依据的结论",
    tag: "研究",
    initials: "PR",
    color: "violet",
    version: "v1.2.0",
    skills: 3,
    runs: 86,
  },
  {
    id: "release",
    name: "交付协作助手",
    role: "梳理变更和验证证据，准备发布",
    tag: "交付",
    initials: "DL",
    color: "green",
    version: "v0.9.0",
    skills: 5,
    runs: 64,
  },
];
export const projects = [
  {
    name: "Weora 平台",
    description: "连接团队与 Agent 的协作工作台",
    repos: 3,
    members: 6,
    initials: "W",
    color: "blue",
  },
  {
    name: "团队知识库",
    description: "让项目经验成为可复用的团队资产",
    repos: 2,
    members: 4,
    initials: "K",
    color: "violet",
  },
  {
    name: "客户服务",
    description: "持续改进客户问题的响应与交付",
    repos: 2,
    members: 5,
    initials: "C",
    color: "green",
  },
];
const initialTasks: Task[] = [
  {
    id: "WR-1028",
    title: "审查登录与权限模块的最新变更",
    agent: "代码审查助手",
    project: "Weora 平台",
    status: "running",
    time: "刚刚",
    description: "检查认证边界、错误处理和回归风险，整理一份可执行的审查清单。",
  },
  {
    id: "WR-1027",
    title: "准备本周发布分支与变更说明",
    agent: "交付协作助手",
    project: "Weora 平台",
    status: "waiting",
    time: "8 分钟前",
    description:
      "变更与测试证据已整理完成，正在等待你确认是否继续创建演示分支。",
  },
  {
    id: "WR-1026",
    title: "整理企业知识库产品的交互方案",
    agent: "产品研究员",
    project: "团队知识库",
    status: "done",
    time: "32 分钟前",
    description: "对照资料入口、检索和引用流程，整理关键决策与下一步建议。",
  },
  {
    id: "WR-1025",
    title: "检查客户工单接口的异常处理",
    agent: "代码审查助手",
    project: "客户服务",
    status: "done",
    time: "1 小时前",
    description: "检查超时、重复请求和权限失效时的接口响应。",
  },
  {
    id: "WR-1024",
    title: "生成项目新成员入门指南",
    agent: "产品研究员",
    project: "Weora 平台",
    status: "done",
    time: "2 小时前",
    description: "整理项目结构、研发约定与本地启动流程。",
  },
  {
    id: "WR-1023",
    title: "汇总近期客户反馈与改进建议",
    agent: "产品研究员",
    project: "客户服务",
    status: "done",
    time: "今天 09:12",
    description: "归纳重复出现的问题，并按优先级给出改进建议。",
  },
];

/** 所有变化只保存在此页面的内存中，不调用正式业务接口。 */
export function useDemo(variant: string) {
  const [view, setView] = useState<View>("tasks");
  const [tasks, setTasks] = useState(initialTasks);
  const [filter, setFilter] = useState("all");
  const [search, setSearch] = useState("");
  const [project, setProject] = useState("all");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [notice, setNotice] = useState("");
  const [notifications, setNotifications] = useState(true);
  const [approval, setApproval] = useState(true);
  const [compact, setCompact] = useState(false);
  const [mobile, setMobile] = useState(false);
  const [theme, setTheme] = useState(
    new URLSearchParams(location.search).get("theme") === "light"
      ? "light"
      : "dark",
  );

  useEffect(() => {
    function closeNavigation(event: KeyboardEvent) {
      if (event.key === "Escape") setMobile(false);
    }
    window.addEventListener("keydown", closeNavigation);
    return () => window.removeEventListener("keydown", closeNavigation);
  }, []);
  useEffect(() => {
    document.documentElement.classList.toggle("dark", theme === "dark");
    document.documentElement.dataset.theme = theme;
    document.documentElement.style.colorScheme = theme;
    parent.postMessage({ type: "weora-preview-theme", theme }, location.origin);
  }, [theme]);
  useEffect(() => {
    function receive(event: MessageEvent) {
      if (event.origin !== location.origin || event.source !== parent) return;
      if (event.data?.type === "weora-preview-theme")
        setTheme(event.data.theme === "light" ? "light" : "dark");
    }
    window.addEventListener("message", receive);
    return () => window.removeEventListener("message", receive);
  }, []);
  useEffect(() => {
    if (!notice) return;
    const timer = setTimeout(() => setNotice(""), 4200);
    return () => clearTimeout(timer);
  }, [notice]);
  useEffect(() => {
    parent.postMessage(
      { type: "weora-preview-state", variant, count: tasks.length, view },
      location.origin,
    );
  }, [tasks.length, view, variant]);

  const visible = tasks.filter(
    (task) =>
      (filter === "all" || task.status === filter) &&
      (project === "all" || task.project === project) &&
      `${task.title} ${task.agent} ${task.id}`
        .toLowerCase()
        .includes(search.toLowerCase()),
  );
  function create(
    title: string,
    agent: string,
    projectName: string,
    requireApproval: boolean,
  ) {
    const id = `WR-${1029 + tasks.length - initialTasks.length}`;
    setTasks((current) => [
      {
        id,
        title,
        agent,
        project: projectName,
        status: requireApproval ? "waiting" : "running",
        time: "刚刚",
        description: title,
      },
      ...current,
    ]);
    setCreateOpen(false);
    setView("tasks");
    setFilter("all");
    setSearch("");
    setProject("all");
    setNotice("演示任务已创建，点击任务可查看详情。");
  }
  function approve(id: string) {
    setTasks((current) =>
      current.map((task) =>
        task.id === id ? { ...task, status: "running" } : task,
      ),
    );
    setNotice("已确认演示审批，任务状态更新为运行中。");
  }
  function finish(id: string) {
    setTasks((current) =>
      current.map((task) =>
        task.id === id ? { ...task, status: "done" } : task,
      ),
    );
    setNotice("演示任务已完成，结果已更新。");
  }
  function navigate(next: View) {
    setView(next);
    setMobile(false);
  }
  return {
    view,
    navigate,
    tasks,
    visible,
    filter,
    setFilter,
    search,
    setSearch,
    project,
    setProject,
    selected: tasks.find((task) => task.id === selectedId),
    setSelectedId,
    createOpen,
    setCreateOpen,
    create,
    approve,
    finish,
    notice,
    setNotice,
    notifications,
    setNotifications,
    approval,
    setApproval,
    compact,
    setCompact,
    mobile,
    setMobile,
    theme,
    setTheme,
  };
}
export type Demo = ReturnType<typeof useDemo>;
