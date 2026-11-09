import { createRoot } from "react-dom/client";
import { useState } from "react";
import {
  Activity,
  ArrowDown,
  ArrowRight,
  ArrowUpRight,
  Bell,
  Bot,
  Check,
  CheckCheck,
  ChevronDown,
  CircleCheck,
  Clock3,
  Code2,
  FileText,
  FolderGit2,
  LayoutDashboard,
  Menu,
  Moon,
  Plus,
  Search,
  Settings2,
  ShieldCheck,
  Sun,
  X,
} from "lucide-react";
import { Button } from "./ui/button";
import { Badge } from "./ui/badge";
import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from "./ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "./ui/dialog";
import { Input } from "./ui/input";
import { Textarea } from "./ui/textarea";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "./ui/tabs";
import { Checkbox } from "./ui/checkbox";
import { Switch } from "./ui/switch";
import { Popover, PopoverContent, PopoverTrigger } from "./ui/popover";
import {
  Command,
  CommandInput,
  CommandList,
  CommandEmpty,
  CommandItem,
} from "./ui/command";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "./ui/table";
import {
  agents,
  projects,
  statusLabel,
  useDemo,
  type Demo,
  type TaskStatus,
  type View,
} from "../shared/model";
import "./style.css";

const nav: { id: View; label: string; icon: typeof Activity }[] = [
  { id: "tasks", label: "工作台", icon: LayoutDashboard },
  { id: "agents", label: "Agent 团队", icon: Bot },
  { id: "projects", label: "项目空间", icon: FolderGit2 },
  { id: "settings", label: "工作空间设置", icon: Settings2 },
];
const filters = [
  { id: "all", label: "全部任务" },
  { id: "running", label: "运行中" },
  { id: "waiting", label: "待审批" },
  { id: "done", label: "已完成" },
];
function Status({ status }: { status: TaskStatus }) {
  return (
    <Badge variant="outline" className={`sh-status ${status}`}>
      <span className="status-dot" />
      {statusLabel[status]}
    </Badge>
  );
}

/** 选择器只组合本地 shadcn 原语，不借用 HeroUI 或生产 console 的样式。 */
function Picker({
  value,
  options,
  onChange,
  label,
}: {
  value: string;
  options: string[];
  onChange: (value: string) => void;
  label: string;
}) {
  const [open, setOpen] = useState(false);
  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <Button
          variant="outline"
          role="combobox"
          aria-label={label}
          aria-expanded={open}
          className="justify-between w-full font-normal"
        >
          {value}
          <ChevronDown size={14} className="opacity-50" />
        </Button>
      </PopoverTrigger>
      <PopoverContent className="p-0 w-[280px]" align="start">
        <Command label={`搜索${label}`}>
          <CommandInput placeholder="搜索选项…" />
          <CommandList>
            <CommandEmpty>没有匹配选项</CommandEmpty>
            {options.map((option) => (
              <CommandItem
                key={option}
                value={option}
                onSelect={() => {
                  onChange(option);
                  setOpen(false);
                }}
              >
                <Check
                  size={14}
                  className={value === option ? "" : "invisible"}
                />
                {option}
              </CommandItem>
            ))}
          </CommandList>
        </Command>
      </PopoverContent>
    </Popover>
  );
}
function ShadcnApp() {
  const demo = useDemo("shadcn");
  const [selectedAgent, setSelectedAgent] = useState(agents[0].name);
  const counts = [
    {
      title: "全部任务",
      value: demo.tasks.length,
      icon: FileText,
      caption: "工作空间内的协作任务",
    },
    {
      title: "正在运行",
      value: demo.tasks.filter((t) => t.status === "running").length,
      icon: Activity,
      caption: "Agent 正在推进目标",
    },
    {
      title: "需要你处理",
      value: demo.tasks.filter((t) => t.status === "waiting").length,
      icon: Clock3,
      caption: "等待你的确认与决策",
    },
    {
      title: "可用 Agent",
      value: agents.length,
      icon: Bot,
      caption: "随时开启新的协作",
    },
  ];
  function start(agent = agents[0].name) {
    setSelectedAgent(agent);
    demo.setCreateOpen(true);
  }
  return (
    <div className="preview-shell shadcn-page" data-mobile={demo.mobile}>
      {demo.mobile && (
        <button
          className="preview-scrim"
          aria-label="关闭导航遮罩"
          onClick={() => demo.setMobile(false)}
        />
      )}
      <aside className="preview-sidebar">
        <div className="preview-brand">
          <span className="brand-mark">w</span>weora
          <Button
            className="mobile-trigger ml-auto"
            size="icon"
            variant="ghost"
            aria-label="关闭导航"
            onClick={() => demo.setMobile(false)}
          >
            <X size={17} />
          </Button>
        </div>
        <div className="workspace-label">
          <span className="workspace-glyph">W</span>
          <span>
            <strong>Weora 研发团队</strong>
            <small>团队工作空间</small>
          </span>
          <ChevronDown size={13} className="ml-auto opacity-50" />
        </div>
        <Button onClick={() => start()}>
          <Plus size={16} />
          发起任务
        </Button>
        <p className="nav-label">工作空间</p>
        <nav className="preview-nav" aria-label="主导航">
          {nav.map(({ id, label, icon: Icon }) => (
            <Button
              key={id}
              variant={demo.view === id ? "secondary" : "ghost"}
              onClick={() => demo.navigate(id)}
            >
              <Icon size={16} />
              {label}
              {id === "tasks" && (
                <span className="nav-count">{demo.tasks.length}</span>
              )}
            </Button>
          ))}
        </nav>
        <p className="nav-label">最近项目</p>
        <nav className="preview-nav" aria-label="最近项目">
          {projects.slice(0, 2).map((item) => (
            <Button
              variant="ghost"
              key={item.name}
              onClick={() => {
                demo.setProject(item.name);
                demo.navigate("tasks");
              }}
            >
              <FolderGit2 size={15} />
              {item.name}
            </Button>
          ))}
        </nav>
        <div className="sh-sidebar-note">
          <ShieldCheck size={16} />
          <span>
            你的团队，统一协作。<small>每一次运行，都有迹可循。</small>
          </span>
        </div>
        <div className="preview-account">
          <span className="initial-avatar">予</span>
          <span>
            <strong>周予安</strong>
            <small>工作空间所有者</small>
          </span>
          <Settings2 size={15} className="ml-auto opacity-40" />
        </div>
      </aside>
      <section className="preview-stage" inert={demo.mobile || undefined}>
        <header className="preview-topbar">
          <div className="crumbs">
            <Button
              className="mobile-trigger"
              size="icon"
              variant="ghost"
              aria-label="打开导航"
              onClick={() => demo.setMobile(true)}
            >
              <Menu size={17} />
            </Button>
            <span>Weora 研发团队</span>
            <span>/</span>
            <strong>{nav.find((item) => item.id === demo.view)?.label}</strong>
          </div>
          <div className="top-actions">
            <Badge variant="outline">shadcn/ui 独立预览</Badge>
            <Button
              size="icon-sm"
              variant="ghost"
              aria-label={
                demo.theme === "dark" ? "切换浅色主题" : "切换深色主题"
              }
              onClick={() =>
                demo.setTheme(demo.theme === "dark" ? "light" : "dark")
              }
            >
              {demo.theme === "dark" ? <Sun size={17} /> : <Moon size={17} />}
            </Button>
            <Button
              size="icon-sm"
              variant="ghost"
              aria-label="查看通知"
              onClick={() =>
                demo.setNotice(
                  `你有 ${demo.tasks.filter((task) => task.status === "waiting").length} 项演示任务等待审批，可在“待审批”中查看。`,
                )
              }
            >
              <Bell size={17} />
            </Button>
          </div>
        </header>
        <main className="preview-content">
          <div className="section-heading">
            <div>
              <div className="eyebrow">WORKSPACE OVERVIEW</div>
              <h1>
                {demo.view === "tasks"
                  ? "工作台"
                  : demo.view === "agents"
                    ? "Agent 团队"
                    : demo.view === "projects"
                      ? "项目空间"
                      : "工作空间设置"}
              </h1>
              <p>
                {demo.view === "tasks"
                  ? "早上好，予安。管理团队任务，查看每一次协作的进展。"
                  : demo.view === "agents"
                    ? "管理已发布的团队能力，选择一个 Agent 开始协作。"
                    : demo.view === "projects"
                      ? "集中管理项目背景、代码仓库和团队协作。"
                      : "调整通知与审批习惯，让工作台更适合你。"}
              </p>
            </div>
            {demo.view !== "settings" && (
              <Button onClick={() => start()}>
                <Plus size={16} />
                创建任务
              </Button>
            )}
          </div>
          {demo.view === "tasks" && (
            <>
              <div className="stats-grid">
                {counts.map(({ icon: Icon, ...item }) => (
                  <Card key={item.title} className="sh-stat">
                    <CardContent>
                      <span className="stat-label">
                        {item.title}
                        <Icon size={16} />
                      </span>
                      <strong className="stat-value">{item.value}</strong>
                      <span className="stat-foot">{item.caption}</span>
                    </CardContent>
                  </Card>
                ))}
              </div>
              <div className="section-line">
                <div>
                  <h2>协作任务</h2>
                  <p>查看运行状态、审批请求和交付结果。</p>
                </div>
                <div className="w-[150px]">
                  <Picker
                    label="筛选项目"
                    value={demo.project === "all" ? "全部项目" : demo.project}
                    options={["全部项目", ...projects.map((p) => p.name)]}
                    onChange={(value) =>
                      demo.setProject(value === "全部项目" ? "all" : value)
                    }
                  />
                </div>
              </div>
              <Tabs value={demo.filter} onValueChange={demo.setFilter}>
                <div className="task-controls">
                  <TabsList>
                    {filters.map((item) => (
                      <TabsTrigger key={item.id} value={item.id}>
                        {item.label}
                      </TabsTrigger>
                    ))}
                  </TabsList>
                  <div className="sh-search task-search">
                    <Search size={14} />
                    <Input
                      aria-label="搜索任务"
                      value={demo.search}
                      onChange={(event) => demo.setSearch(event.target.value)}
                      placeholder="搜索任务、Agent…"
                    />
                  </div>
                </div>
                {filters.map((item) => (
                  <TabsContent key={item.id} value={item.id}>
                    <ShadcnTasks demo={demo} />
                  </TabsContent>
                ))}
              </Tabs>
              <div className="section-line">
                <div>
                  <h2>团队 Agent</h2>
                  <p>可复用、可追溯的专业能力。</p>
                </div>
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() => demo.navigate("agents")}
                >
                  查看全部 <ArrowUpRight size={14} />
                </Button>
              </div>
              <AgentCards start={start} />
            </>
          )}
          {demo.view === "agents" && <AgentCards start={start} />}
          {demo.view === "projects" && (
            <div className="agent-grid">
              {projects.map((item) => (
                <Card key={item.name}>
                  <CardHeader>
                    <FolderGit2 size={24} className="mb-3" />
                    <CardTitle>{item.name}</CardTitle>
                    <CardDescription>{item.description}</CardDescription>
                  </CardHeader>
                  <CardFooter className="justify-between">
                    <span className="task-meta">
                      {item.repos} 个仓库 · {item.members} 位成员
                    </span>
                    <Button
                      size="sm"
                      variant="outline"
                      onClick={() => {
                        demo.setProject(item.name);
                        demo.navigate("tasks");
                      }}
                    >
                      查看任务 <ArrowRight size={13} />
                    </Button>
                  </CardFooter>
                </Card>
              ))}
            </div>
          )}
          {demo.view === "settings" && (
            <Card className="settings-list">
              <CardHeader>
                <CardTitle>偏好与协作</CardTitle>
                <CardDescription>设置仅用于这次交互预览。</CardDescription>
              </CardHeader>
              <CardContent>
                <div className="setting-row">
                  <div>
                    <strong>任务完成通知</strong>
                    <p>Agent 完成任务时，在工作台提醒我。</p>
                  </div>
                  <Switch
                    aria-label="任务完成通知"
                    checked={demo.notifications}
                    onCheckedChange={demo.setNotifications}
                  />
                </div>
                <div className="setting-row">
                  <div>
                    <strong>重要操作需要审批</strong>
                    <p>在创建分支或更新外部资源前，先由我确认。</p>
                  </div>
                  <Switch
                    aria-label="重要操作需要审批"
                    checked={demo.approval}
                    onCheckedChange={demo.setApproval}
                  />
                </div>
              </CardContent>
              <CardFooter>
                <Button
                  onClick={() =>
                    demo.setNotice("演示偏好已保存；刷新页面会重置。")
                  }
                >
                  保存偏好
                </Button>
              </CardFooter>
            </Card>
          )}
          <footer className="prototype-foot">
            <span>示例数据 · 操作仅用于 UI 体验</span>
            <span>Human + Agent, better together.</span>
          </footer>
        </main>
      </section>
      <CreateTask
        demo={demo}
        initialAgent={selectedAgent}
        key={`${demo.createOpen}-${selectedAgent}`}
      />
      <TaskDetail demo={demo} />
      {demo.notice && (
        <div className="demo-toast" role="status">
          <CircleCheck size={15} className="inline mr-2" />
          {demo.notice}
        </div>
      )}
    </div>
  );
}
function ShadcnTasks({ demo }: { demo: Demo }) {
  return (
    <div className="sh-task-table">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>任务</TableHead>
            <TableHead>状态</TableHead>
            <TableHead>协作 Agent</TableHead>
            <TableHead>
              创建时间 <ArrowDown size={11} className="inline" />
            </TableHead>
            <TableHead>
              <span className="sr-only">操作</span>
            </TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {demo.visible.map((task) => (
            <TableRow key={task.id}>
              <TableCell>
                <button
                  onClick={() => demo.setSelectedId(task.id)}
                  className="text-left cursor-pointer"
                >
                  <strong className="task-title">{task.title}</strong>
                  <span className="task-meta">
                    {task.id} · {task.project}
                  </span>
                </button>
              </TableCell>
              <TableCell>
                <Status status={task.status} />
              </TableCell>
              <TableCell className="agent-table-name">
                <Bot size={14} className="inline mr-2 opacity-50" />
                {task.agent}
              </TableCell>
              <TableCell className="task-meta whitespace-nowrap">
                {task.time}
              </TableCell>
              <TableCell>
                <Button
                  size="icon-sm"
                  variant="ghost"
                  aria-label={`查看任务 ${task.id}`}
                  onClick={() => demo.setSelectedId(task.id)}
                >
                  <ArrowUpRight size={15} />
                </Button>
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
      {!demo.visible.length && (
        <div className="empty-state">
          <Search size={23} className="mx-auto mb-3" />
          没有匹配的任务，试试其他条件。
        </div>
      )}
    </div>
  );
}
function AgentCards({ start }: { start: (agent: string) => void }) {
  return (
    <div className="agent-grid">
      {agents.map((agent) => (
        <Card key={agent.id} className="sh-agent">
          <CardHeader>
            <div className="agent-top">
              <span className="sh-agent-icon">
                <Bot size={20} />
              </span>
              <Badge variant="secondary">{agent.tag}</Badge>
            </div>
            <CardTitle className="mt-4 text-sm">{agent.name}</CardTitle>
            <CardDescription className="text-xs">{agent.role}</CardDescription>
          </CardHeader>
          <CardFooter className="justify-between">
            <span className="task-meta">
              {agent.skills} 项 Skills · {agent.version}
            </span>
            <Button
              size="icon-sm"
              variant="outline"
              aria-label={`使用${agent.name}`}
              onClick={() => start(agent.name)}
            >
              <ArrowRight size={14} />
            </Button>
          </CardFooter>
        </Card>
      ))}
    </div>
  );
}
function CreateTask({
  demo,
  initialAgent,
}: {
  demo: Demo;
  initialAgent: string;
}) {
  const [title, setTitle] = useState("");
  const [agent, setAgent] = useState(initialAgent);
  const [project, setProject] = useState(projects[0].name);
  const [approval, setApproval] = useState(demo.approval);
  return (
    <Dialog open={demo.createOpen} onOpenChange={demo.setCreateOpen}>
      <DialogContent aria-label="创建任务" className="sm:max-w-[540px]">
        <DialogHeader>
          <DialogTitle>创建任务</DialogTitle>
          <DialogDescription>
            描述目标，选择协作 Agent 与所属项目。
          </DialogDescription>
        </DialogHeader>
        <form
          className="form-stack mt-3"
          onSubmit={(event) => {
            event.preventDefault();
            if (title.trim())
              demo.create(title.trim(), agent, project, approval);
          }}
        >
          <label>
            <span className="field-label">任务目标</span>
            <Textarea
              required
              value={title}
              onChange={(event) => setTitle(event.target.value)}
              placeholder="例如：审查最新的权限模块，列出潜在风险和验证建议…"
              rows={4}
            />
          </label>
          <div>
            <span className="field-label" id="sh-agent-label">
              协作 Agent
            </span>
            <Picker
              label="协作 Agent"
              value={agent}
              options={agents.map((item) => item.name)}
              onChange={setAgent}
            />
          </div>
          <div>
            <span className="field-label">所属项目</span>
            <Picker
              label="所属项目"
              value={project}
              options={projects.map((item) => item.name)}
              onChange={setProject}
            />
          </div>
          <label className="flex items-center gap-3 text-xs">
            <Checkbox
              checked={approval}
              onCheckedChange={(value) => setApproval(value === true)}
            />
            重要操作先由我确认
          </label>
          <DialogFooter className="mt-3">
            <Button
              variant="outline"
              type="button"
              onClick={() => demo.setCreateOpen(false)}
            >
              取消
            </Button>
            <Button type="submit" disabled={!title.trim()}>
              创建演示任务 <ArrowRight size={15} />
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
function TaskDetail({ demo }: { demo: Demo }) {
  const task = demo.selected;
  return (
    <Dialog
      open={!!task}
      onOpenChange={(open) => {
        if (!open) demo.setSelectedId(null);
      }}
    >
      <DialogContent
        aria-label={task?.title ?? "任务详情"}
        className="sm:max-w-[610px]"
      >
        <DialogHeader>
          <DialogTitle>{task?.title}</DialogTitle>
          <DialogDescription>
            {task?.id} · 仅发起人可见 · 演示任务
          </DialogDescription>
        </DialogHeader>
        {task && (
          <>
            <Status status={task.status} />
            <div className="detail-info">
              <div>
                <small>协作 Agent</small>
                <strong>
                  {task.agent} ·{" "}
                  {agents.find((agent) => agent.name === task.agent)?.version}
                </strong>
              </div>
              <div>
                <small>所属项目</small>
                <strong>{task.project}</strong>
              </div>
            </div>
            <Tabs defaultValue="overview">
              <TabsList>
                <TabsTrigger value="overview">任务概览</TabsTrigger>
                <TabsTrigger value="events">执行过程</TabsTrigger>
              </TabsList>
              <TabsContent value="overview">
                <div className="detail-copy">
                  <h3>任务目标</h3>
                  <p>{task.description}</p>
                  <h3>
                    {task.status === "waiting"
                      ? "需要你的确认"
                      : task.status === "done"
                        ? "协作结果"
                        : "当前进展"}
                  </h3>
                  <p>
                    {task.status === "waiting"
                      ? "Agent 已完成前置检查，准备创建演示分支。你可以确认继续，也可以先关闭详情。"
                      : task.status === "done"
                        ? "已完成目标拆解与检查，发现 3 个需要关注的边界问题，建议补充权限失效、重复提交和异常恢复的验证。"
                        : "正在读取项目约定、梳理相关变更，并组织可验证的结论。"}
                  </p>
                </div>
              </TabsContent>
              <TabsContent value="events">
                <div className="event-line">
                  <Check size={16} />
                  <div>
                    任务已创建<small>目标与 Agent 版本已固定</small>
                  </div>
                </div>
                <div className="event-line">
                  <FileText size={16} />
                  <div>
                    上下文已就绪<small>读取项目说明与开发约定</small>
                  </div>
                </div>
                <div className="event-line">
                  <Code2 size={16} />
                  <div>
                    正在分析变更<small>这是一段用于比较界面的演示过程</small>
                  </div>
                </div>
              </TabsContent>
            </Tabs>
            <DialogFooter className="mt-4">
              <Button
                variant="outline"
                onClick={() => demo.setSelectedId(null)}
              >
                关闭
              </Button>
              {task.status === "waiting" && (
                <Button onClick={() => demo.approve(task.id)}>
                  <ShieldCheck size={16} />
                  确认并继续
                </Button>
              )}
              {task.status === "running" && (
                <Button onClick={() => demo.finish(task.id)}>
                  <CheckCheck size={16} />
                  模拟完成
                </Button>
              )}
            </DialogFooter>
          </>
        )}
      </DialogContent>
    </Dialog>
  );
}
const root = createRoot(document.getElementById("root")!);
root.render(<ShadcnApp />);
// 两个预览各自管理入口，热更新时释放当前文档的 React 根节点。
import.meta.hot?.dispose(() => root.unmount());
