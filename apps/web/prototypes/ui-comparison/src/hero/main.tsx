import { createRoot } from "react-dom/client";
import { useState } from "react";
import {
  Button,
  Card,
  Chip,
  Modal,
  Tabs,
  TextField,
  Label,
  Input,
  TextArea,
  Select,
  ListBox,
  Switch,
  Checkbox,
} from "@heroui/react";
import {
  Activity,
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
  Sparkles,
  Sun,
  Workflow,
  X,
} from "lucide-react";
import {
  agents,
  projects,
  statusLabel,
  useDemo,
  type Demo,
  type Task,
  type TaskStatus,
  type View,
} from "../shared/model";
import "./style.css";

const filters = [
  { id: "all", label: "全部任务" },
  { id: "running", label: "运行中" },
  { id: "waiting", label: "待审批" },
  { id: "done", label: "已完成" },
];
const nav: { id: View; label: string; icon: typeof Activity }[] = [
  { id: "tasks", label: "工作台", icon: LayoutDashboard },
  { id: "agents", label: "Agent 团队", icon: Bot },
  { id: "projects", label: "项目空间", icon: FolderGit2 },
  { id: "settings", label: "工作空间设置", icon: Settings2 },
];
const tone: Record<TaskStatus, "accent" | "warning" | "success"> = {
  running: "accent",
  waiting: "warning",
  done: "success",
};
function Status({ status }: { status: TaskStatus }) {
  return (
    <Chip size="sm" color={tone[status]} variant="soft">
      <span className="status-dot" />
      {statusLabel[status]}
    </Chip>
  );
}

function HeroApp() {
  const demo = useDemo("heroui");
  const [selectedAgent, setSelectedAgent] = useState(agents[0].name);
  const counts = [
    {
      title: "全部任务",
      value: demo.tasks.length,
      icon: Workflow,
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
    <div className="preview-shell hero-page" data-mobile={demo.mobile}>
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
            isIconOnly
            variant="tertiary"
            aria-label="关闭导航"
            onPress={() => demo.setMobile(false)}
          >
            <X size={17} />
          </Button>
        </div>
        <Card className="workspace-label" variant="transparent">
          <span className="workspace-glyph">W</span>
          <span>
            <strong>Weora 研发团队</strong>
            <small>团队工作空间</small>
          </span>
          <ChevronDown size={13} className="ml-auto opacity-50" />
        </Card>
        <Button variant="primary" onPress={() => start()}>
          <Plus size={16} />
          发起任务
        </Button>
        <p className="nav-label">工作空间</p>
        <nav className="preview-nav" aria-label="主导航">
          {nav.map(({ id, label, icon: Icon }) => (
            <Button
              key={id}
              variant={demo.view === id ? "secondary" : "ghost"}
              onPress={() => demo.navigate(id)}
            >
              <Icon size={17} />
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
              onPress={() => {
                demo.setProject(item.name);
                demo.navigate("tasks");
              }}
            >
              <span className={`project-dot ${item.color}`} />
              {item.name}
            </Button>
          ))}
        </nav>
        <div className="sidebar-tip">
          <Sparkles size={17} />
          <strong>团队经验，随时可用</strong>
          <p>让 Agent 帮你把想法变成下一步。</p>
          <Button
            size="sm"
            variant="secondary"
            onPress={() => demo.navigate("agents")}
          >
            探索团队能力 <ArrowRight size={13} />
          </Button>
        </div>
        <div className="preview-account">
          <span className="initial-avatar">予</span>
          <span>
            <strong>周予安</strong>
            <small>工作空间所有者</small>
          </span>
          <ShieldCheck size={15} className="ml-auto opacity-40" />
        </div>
      </aside>
      <section className="preview-stage" inert={demo.mobile || undefined}>
        <header className="preview-topbar">
          <div className="crumbs">
            <Button
              className="mobile-trigger"
              isIconOnly
              variant="tertiary"
              aria-label="打开导航"
              onPress={() => demo.setMobile(true)}
            >
              <Menu size={18} />
            </Button>
            <span>Weora 研发团队</span>
            <span>/</span>
            <strong>{nav.find((item) => item.id === demo.view)?.label}</strong>
          </div>
          <div className="top-actions">
            <Chip size="sm" variant="soft">
              HeroUI 独立预览
            </Chip>
            <Button
              isIconOnly
              size="sm"
              variant="tertiary"
              aria-label={
                demo.theme === "dark" ? "切换浅色主题" : "切换深色主题"
              }
              onPress={() =>
                demo.setTheme(demo.theme === "dark" ? "light" : "dark")
              }
            >
              {demo.theme === "dark" ? <Sun size={17} /> : <Moon size={17} />}
            </Button>
            <Button
              isIconOnly
              size="sm"
              variant="tertiary"
              aria-label="查看通知"
              onPress={() =>
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
              <div className="eyebrow">YOUR TEAM, IN SYNC</div>
              <h1>
                {demo.view === "tasks"
                  ? "让协作，自然向前。"
                  : demo.view === "agents"
                    ? "你的 Agent 团队"
                    : demo.view === "projects"
                      ? "让上下文，有处安放。"
                      : "工作空间设置"}
              </h1>
              <p>
                {demo.view === "tasks"
                  ? "早上好，予安。团队正在推进目标，这里是今天的进展。"
                  : demo.view === "agents"
                    ? "专业的能力，统一的协作方式。选择一位伙伴，开始新的任务。"
                    : demo.view === "projects"
                      ? "连接项目、资料与团队，给每个任务完整的业务背景。"
                      : "调整通知与审批习惯，让工作台更适合你。"}
              </p>
            </div>
            {demo.view !== "settings" && (
              <Button onPress={() => start()}>
                <Plus size={16} />
                创建任务
              </Button>
            )}
          </div>
          {demo.view === "tasks" && (
            <>
              <div className="stats-grid">
                {counts.map(({ icon: Icon, ...item }) => (
                  <Card className="hero-stat" key={item.title}>
                    <Card.Content>
                      <span className="stat-label">
                        {item.title}
                        <Icon size={17} />
                      </span>
                      <strong className="stat-value">{item.value}</strong>
                      <span className="stat-foot">
                        {item.title === "全部任务" && (
                          <ArrowUpRight size={12} />
                        )}
                        {item.caption}
                      </span>
                    </Card.Content>
                  </Card>
                ))}
              </div>
              <div className="section-line">
                <div>
                  <h2>协作任务</h2>
                  <p>从目标到交付，每一步都看得见。</p>
                </div>
                <Select
                  aria-label="筛选项目"
                  value={demo.project}
                  onChange={(value) => demo.setProject(String(value))}
                  className="project-select"
                >
                  <Select.Trigger>
                    <Select.Value />
                    <Select.Indicator />
                  </Select.Trigger>
                  <Select.Popover>
                    <ListBox>
                      <ListBox.Item id="all" textValue="全部项目">
                        全部项目
                        <ListBox.ItemIndicator />
                      </ListBox.Item>
                      {projects.map((item) => (
                        <ListBox.Item
                          key={item.name}
                          id={item.name}
                          textValue={item.name}
                        >
                          {item.name}
                          <ListBox.ItemIndicator />
                        </ListBox.Item>
                      ))}
                    </ListBox>
                  </Select.Popover>
                </Select>
              </div>
              <Card className="hero-tasks">
                <Card.Content>
                  <Tabs
                    selectedKey={demo.filter}
                    onSelectionChange={(key) => demo.setFilter(String(key))}
                  >
                    <div className="task-controls">
                      <Tabs.ListContainer>
                        <Tabs.List aria-label="任务状态">
                          {filters.map((item) => (
                            <Tabs.Tab id={item.id} key={item.id}>
                              {item.label}
                              <Tabs.Indicator />
                            </Tabs.Tab>
                          ))}
                        </Tabs.List>
                      </Tabs.ListContainer>
                      <TextField
                        aria-label="搜索任务"
                        value={demo.search}
                        onChange={demo.setSearch}
                        className="task-search"
                      >
                        <Input placeholder="搜索任务、Agent…" />
                      </TextField>
                    </div>
                    {filters.map((item) => (
                      <Tabs.Panel id={item.id} key={item.id}>
                        <HeroTasks demo={demo} />
                      </Tabs.Panel>
                    ))}
                  </Tabs>
                </Card.Content>
              </Card>
              <div className="section-line">
                <div>
                  <h2>与你一起工作的 Agent</h2>
                  <p>复用专业能力，把精力留给关键决策。</p>
                </div>
                <Button
                  size="sm"
                  variant="tertiary"
                  onPress={() => demo.navigate("agents")}
                >
                  全部 Agent <ArrowUpRight size={14} />
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
                  <Card.Header>
                    <div className={`agent-icon ${item.color}`}>
                      <FolderGit2 size={23} />
                    </div>
                    <Card.Title className="mt-4">{item.name}</Card.Title>
                    <Card.Description>{item.description}</Card.Description>
                  </Card.Header>
                  <Card.Footer className="justify-between">
                    <span className="task-meta">
                      {item.repos} 个仓库 · {item.members} 位成员
                    </span>
                    <Button
                      size="sm"
                      variant="secondary"
                      onPress={() => {
                        demo.setProject(item.name);
                        demo.navigate("tasks");
                      }}
                    >
                      查看项目任务 <ArrowRight size={13} />
                    </Button>
                  </Card.Footer>
                </Card>
              ))}
            </div>
          )}
          {demo.view === "settings" && (
            <Card className="settings-list">
              <Card.Header>
                <Card.Title>偏好与协作</Card.Title>
                <Card.Description>设置仅用于这次交互预览。</Card.Description>
              </Card.Header>
              <Card.Content>
                <div className="setting-row">
                  <div>
                    <strong>任务完成通知</strong>
                    <p>Agent 完成任务时，在工作台提醒我。</p>
                  </div>
                  <Switch
                    aria-label="任务完成通知"
                    isSelected={demo.notifications}
                    onChange={demo.setNotifications}
                  >
                    <Switch.Content aria-label="任务完成通知">
                      <Switch.Control>
                        <Switch.Thumb />
                      </Switch.Control>
                    </Switch.Content>
                  </Switch>
                </div>
                <div className="setting-row">
                  <div>
                    <strong>重要操作需要审批</strong>
                    <p>在创建分支或更新外部资源前，先由我确认。</p>
                  </div>
                  <Switch
                    aria-label="重要操作需要审批"
                    isSelected={demo.approval}
                    onChange={demo.setApproval}
                  >
                    <Switch.Content aria-label="重要操作需要审批">
                      <Switch.Control>
                        <Switch.Thumb />
                      </Switch.Control>
                    </Switch.Content>
                  </Switch>
                </div>
              </Card.Content>
              <Card.Footer>
                <Button
                  onPress={() =>
                    demo.setNotice("演示偏好已保存；刷新页面会重置。")
                  }
                >
                  保存偏好
                </Button>
              </Card.Footer>
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

function HeroTasks({ demo }: { demo: Demo }) {
  return (
    <div className="hero-task-list">
      {demo.visible.length ? (
        demo.visible.map((task) => (
          <div className="hero-task-row" key={task.id}>
            <div className={`task-leading ${task.status}`}>
              <Bot size={18} />
            </div>
            <button
              className="hero-task-title"
              onClick={() => demo.setSelectedId(task.id)}
            >
              <span className="task-title">{task.title}</span>
              <span className="task-meta">
                {task.agent} <span className="meta-separator">·</span>{" "}
                {task.project}
              </span>
            </button>
            <Status status={task.status} />
            <span className="task-time">{task.time}</span>
            <Button
              isIconOnly
              size="sm"
              variant="tertiary"
              aria-label={`查看任务 ${task.id}`}
              onPress={() => demo.setSelectedId(task.id)}
            >
              <ArrowUpRight size={16} />
            </Button>
          </div>
        ))
      ) : (
        <div className="empty-state">
          <Search size={24} className="mx-auto mb-3" />
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
        <Card key={agent.id} className="hero-agent">
          <Card.Content>
            <div className="agent-top">
              <div className={`agent-icon ${agent.color}`}>
                <Bot size={23} />
              </div>
              <Chip size="sm" variant="soft">
                {agent.tag}
              </Chip>
            </div>
            <h3 className="agent-name">{agent.name}</h3>
            <p className="agent-description">{agent.role}</p>
            <div className="agent-footer">
              <span>
                {agent.skills} 项 Skills · {agent.version}
              </span>
              <Button
                isIconOnly
                size="sm"
                variant="secondary"
                aria-label={`使用${agent.name}`}
                onPress={() => start(agent.name)}
              >
                <ArrowRight size={15} />
              </Button>
            </div>
          </Card.Content>
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
    <Modal.Backdrop isOpen={demo.createOpen} onOpenChange={demo.setCreateOpen}>
      <Modal.Container size="lg">
        <Modal.Dialog aria-label="开始一次新的协作">
          <Modal.CloseTrigger aria-label="关闭创建任务" />
          <Modal.Header>
            <Modal.Icon>
              <Sparkles size={21} />
            </Modal.Icon>
            <Modal.Heading>开始一次新的协作</Modal.Heading>
            <p className="form-note">
              告诉 Agent 你的目标，其余的我们一起推进。
            </p>
          </Modal.Header>
          <Modal.Body>
            <form
              id="hero-create"
              className="form-stack"
              onSubmit={(event) => {
                event.preventDefault();
                if (title.trim())
                  demo.create(title.trim(), agent, project, approval);
              }}
            >
              <TextField isRequired value={title} onChange={setTitle}>
                <Label>任务目标</Label>
                <TextArea
                  placeholder="例如：审查最新的权限模块，列出潜在风险和验证建议…"
                  rows={4}
                />
              </TextField>
              <Select
                value={agent}
                onChange={(value) => setAgent(String(value))}
              >
                <Label>协作 Agent</Label>
                <Select.Trigger>
                  <Select.Value />
                  <Select.Indicator />
                </Select.Trigger>
                <Select.Popover>
                  <ListBox>
                    {agents.map((item) => (
                      <ListBox.Item
                        key={item.name}
                        id={item.name}
                        textValue={item.name}
                      >
                        <Label>{item.name}</Label>
                        <ListBox.ItemIndicator />
                      </ListBox.Item>
                    ))}
                  </ListBox>
                </Select.Popover>
              </Select>
              <Select
                value={project}
                onChange={(value) => setProject(String(value))}
              >
                <Label>所属项目</Label>
                <Select.Trigger>
                  <Select.Value />
                  <Select.Indicator />
                </Select.Trigger>
                <Select.Popover>
                  <ListBox>
                    {projects.map((item) => (
                      <ListBox.Item
                        key={item.name}
                        id={item.name}
                        textValue={item.name}
                      >
                        {item.name}
                        <ListBox.ItemIndicator />
                      </ListBox.Item>
                    ))}
                  </ListBox>
                </Select.Popover>
              </Select>
              <Checkbox isSelected={approval} onChange={setApproval}>
                <Checkbox.Content>
                  <Checkbox.Control>
                    <Checkbox.Indicator />
                  </Checkbox.Control>
                  <Label>重要操作先由我确认</Label>
                </Checkbox.Content>
              </Checkbox>
            </form>
          </Modal.Body>
          <Modal.Footer>
            <Button
              variant="tertiary"
              onPress={() => demo.setCreateOpen(false)}
            >
              取消
            </Button>
            <Button type="submit" form="hero-create" isDisabled={!title.trim()}>
              创建演示任务 <ArrowRight size={15} />
            </Button>
          </Modal.Footer>
        </Modal.Dialog>
      </Modal.Container>
    </Modal.Backdrop>
  );
}
function TaskDetail({ demo }: { demo: Demo }) {
  const task = demo.selected;
  return (
    <Modal.Backdrop
      isOpen={!!task}
      onOpenChange={(open) => {
        if (!open) demo.setSelectedId(null);
      }}
    >
      <Modal.Container size="lg">
        <Modal.Dialog aria-label={task?.title ?? "任务详情"}>
          <Modal.CloseTrigger aria-label="关闭任务详情" />
          <Modal.Header>
            <span className="task-meta">{task?.id}</span>
            <Modal.Heading>{task?.title}</Modal.Heading>
          </Modal.Header>
          <Modal.Body>
            {task && (
              <>
                <Status status={task.status} />
                <div className="detail-info">
                  <div>
                    <small>协作 Agent</small>
                    <strong>
                      {task.agent} ·{" "}
                      {
                        agents.find((agent) => agent.name === task.agent)
                          ?.version
                      }
                    </strong>
                  </div>
                  <div>
                    <small>所属项目</small>
                    <strong>{task.project}</strong>
                  </div>
                </div>
                <Tabs defaultSelectedKey="overview">
                  <Tabs.ListContainer>
                    <Tabs.List aria-label="任务详情页签">
                      <Tabs.Tab id="overview">
                        任务概览
                        <Tabs.Indicator />
                      </Tabs.Tab>
                      <Tabs.Tab id="events">
                        执行过程
                        <Tabs.Indicator />
                      </Tabs.Tab>
                    </Tabs.List>
                  </Tabs.ListContainer>
                  <Tabs.Panel id="overview">
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
                  </Tabs.Panel>
                  <Tabs.Panel id="events">
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
                        正在分析变更
                        <small>这是一段用于比较界面的演示过程</small>
                      </div>
                    </div>
                  </Tabs.Panel>
                </Tabs>
              </>
            )}
          </Modal.Body>
          <Modal.Footer>
            <Button variant="tertiary" onPress={() => demo.setSelectedId(null)}>
              关闭
            </Button>
            {task?.status === "waiting" && (
              <Button onPress={() => demo.approve(task.id)}>
                <ShieldCheck size={16} />
                确认并继续
              </Button>
            )}
            {task?.status === "running" && (
              <Button onPress={() => demo.finish(task.id)}>
                <CheckCheck size={16} />
                模拟完成
              </Button>
            )}
          </Modal.Footer>
        </Modal.Dialog>
      </Modal.Container>
    </Modal.Backdrop>
  );
}
const root = createRoot(document.getElementById("root")!);
root.render(<HeroApp />);
// 热更新替换入口前释放旧根节点，避免重复注册 React 容器。
import.meta.hot?.dispose(() => root.unmount());
