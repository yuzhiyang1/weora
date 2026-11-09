import { useState } from "react";
import { Bot, ClipboardList, Search, UserRound } from "lucide-react";
import { Link } from "react-router-dom";

import {
  Avatar,
  Button,
  Card,
  Input,
  Select,
  Tag,
} from "../../components/console";
import type { TaskSummary } from "./api";
import { taskStatuses } from "./task-status";

const views = [
  { id: "all", label: "全部任务", icon: ClipboardList },
  { id: "mine", label: "分配给我", icon: UserRound },
  { id: "agent", label: "Agent 负责", icon: Bot },
] as const;

const allAssignees = "all";
const unassigned = "unassigned";
type View = (typeof views)[number]["id"];

function assigneeKey(task: TaskSummary) {
  return task.assignment
    ? `${task.assignment.kind}:${task.assignment.id}`
    : unassigned;
}

function TaskBoardCard({ task }: { task: TaskSummary }) {
  const assignee = task.assignment?.name;
  return (
    <Link className="task-board-card-link" to={`/tasks/${task.id}`}>
      <Card>
        <div className="task-board-card-body">
          <span className="task-board-meta">
            #{task.id.slice(0, 8).toUpperCase()}
          </span>
          <strong>{task.title ?? "未命名任务"}</strong>
          <span className="task-board-assignee">
            {assignee && (
              <span aria-hidden="true">
                <Avatar>
                  {task.assignment?.kind === "agent" ? (
                    <Bot size={15} />
                  ) : (
                    assignee.slice(0, 1)
                  )}
                </Avatar>
              </span>
            )}
            <span>{assignee ?? "未分配"}</span>
          </span>
          {task.pending_assignment && (
            <span className="task-board-meta">
              接收人 · {task.pending_assignment.name}
            </span>
          )}
        </div>
      </Card>
    </Link>
  );
}

export function TaskBoard({
  tasks,
  memberId,
}: {
  tasks: TaskSummary[];
  memberId: string;
}) {
  const [view, setView] = useState<View>("all");
  const [search, setSearch] = useState("");
  const [assignee, setAssignee] = useState(allAssignees);
  const keyword = search.trim().toLocaleLowerCase().replace(/^#/, "");
  const people = new Map(
    tasks.map((task) => [assigneeKey(task), task.assignment?.name ?? "未分配"]),
  );
  const options = [
    { value: allAssignees, label: "全部负责人" },
    ...Array.from(people, ([value, label]) => ({ value, label })),
  ];
  const selectedAssignee = people.has(assignee) ? assignee : allAssignees;
  const currentView = views.find((item) => item.id === view) ?? views[0];
  const matching = tasks.filter((task) => {
    if (
      view === "mine" &&
      (task.assignment?.kind !== "member" || task.assignment.id !== memberId)
    )
      return false;
    if (view === "agent" && task.assignment?.kind !== "agent") return false;
    if (
      selectedAssignee !== allAssignees &&
      selectedAssignee !== assigneeKey(task)
    )
      return false;
    return [task.title, task.id, task.assignment?.name].some((value) =>
      value?.toLocaleLowerCase().includes(keyword),
    );
  });

  return (
    <div className="task-board-layout">
      <nav className="task-board-views" aria-label="任务视图">
        <strong className="task-board-work">工作</strong>
        <span className="task-board-views-label task-board-meta">视图</span>
        <div className="task-board-view-actions">
          {views.map(({ id, label, icon: Icon }) => (
            <Button
              key={id}
              size="small"
              theme={view === id ? "light" : "borderless"}
              aria-pressed={view === id}
              icon={<Icon size={16} aria-hidden="true" />}
              onClick={() => {
                setView(id);
                setAssignee(allAssignees);
              }}
            >
              {label}
            </Button>
          ))}
        </div>
      </nav>
      <section className="task-board-main" aria-labelledby="task-board-heading">
        <header className="task-board-toolbar">
          <h1 id="task-board-heading">{currentView.label}</h1>
          <Input
            type="search"
            aria-label="搜索任务"
            placeholder="搜索任务"
            value={search}
            onChange={setSearch}
            prefix={<Search size={15} aria-hidden="true" />}
          />
          <div className="task-board-filter">
            <Select
              aria-label="按负责人筛选"
              optionList={options}
              value={selectedAssignee}
              onChange={(value) => setAssignee(String(value))}
            />
          </div>
        </header>
        {tasks.length === 0 && (
          <p className="task-board-notice">
            暂无协作任务。可以从运行详情发布协作摘要。
          </p>
        )}
        {tasks.length > 0 && matching.length === 0 && (
          <p className="task-board-notice" role="status">
            当前视图没有符合条件的任务。
          </p>
        )}
        {tasks.length >= 100 && (
          <p className="task-board-notice">
            当前看板展示最近加载的 {tasks.length} 项任务。
          </p>
        )}
        <div
          className="task-board-scroll"
          role="region"
          aria-label="任务状态看板"
          tabIndex={0}
        >
          {Object.entries(taskStatuses).map(
            ([id, { label, icon: Icon, color }]) => {
              const items = matching.filter((task) => task.status === id);
              return (
                <section
                  key={id}
                  className="task-board-column"
                  aria-labelledby={`task-board-${id}`}
                >
                  <header className="task-board-column-heading">
                    <Icon size={16} aria-hidden="true" />
                    <h2 id={`task-board-${id}`}>{label}</h2>
                    <Tag color={color} aria-label={`${items.length} 项任务`}>
                      {items.length}
                    </Tag>
                  </header>
                  {items.length ? (
                    <ul className="task-board-cards">
                      {items.map((task) => (
                        <li key={task.id}>
                          <TaskBoardCard task={task} />
                        </li>
                      ))}
                    </ul>
                  ) : (
                    <p className="task-board-column-empty">暂无任务</p>
                  )}
                </section>
              );
            },
          )}
        </div>
      </section>
    </div>
  );
}
