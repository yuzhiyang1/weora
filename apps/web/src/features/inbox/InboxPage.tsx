import { useEffect, useEffectEvent, useRef, useState } from "react";
import { Button, Dropdown } from "@heroui/react";
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Archive,
  ArchiveRestore,
  ArrowLeft,
  ArrowUpRight,
  ChevronDown,
  ClipboardCheck,
  Ellipsis,
  Filter,
  Inbox,
  ShieldCheck,
  Undo2,
  UserRound,
} from "lucide-react";
import { Link } from "react-router-dom";

import { Banner, Spin } from "../../components/console";
import { useAuth } from "../auth/AuthProvider";
import { ConsoleApiError, consoleRequest, consoleWrite, errorMessage } from "../runs/api";
import { DeliveryPanel } from "../runs/DeliveryPanel";
import "../runs/runs.css";
import { TaskDetailContent } from "../tasks/TasksPage";
import "./inbox.css";

type InboxItem = {
  id: string;
  type: "approval" | "review" | "assignment" | "review_returned";
  title: string;
  task_id: string;
  run_id: string | null;
  pending: boolean;
  read: boolean;
  created_at: string;
};

type InboxGroup = {
  taskId: string;
  items: [InboxItem, ...InboxItem[]];
  primary: InboxItem;
  unread: boolean;
};
type InboxUpdate =
  | { kind: "read"; itemId: string; read: boolean }
  | { kind: "group"; taskId: string; archived: boolean }
  | { kind: "bulk"; action: "read_all" | "archive_all" | "archive_read" };
type InboxArchivePage = { items: InboxItem[]; next_cursor: string | null; has_more: boolean };
type MenuControl = { isOpen: boolean; onOpenChange: (open: boolean) => void };

const kinds = {
  approval: { label: "待审批", icon: ShieldCheck },
  review: { label: "待验收", icon: ClipboardCheck },
  assignment: { label: "任务指派", icon: UserRound },
  review_returned: { label: "退回修改", icon: Undo2 },
};

function itemLabel(item: InboxItem) {
  if (!item.pending && item.type === "approval") return "执行审批";
  if (!item.pending && item.type === "review") return "任务验收";
  return kinds[item.type].label;
}

function itemPath(item: InboxItem) {
  return item.run_id ? "/runs/" + item.run_id + "?tab=delivery" : "/tasks/" + item.task_id;
}

function relativeTime(date: string) {
  const minutes = Math.max(0, Math.floor((Date.now() - Date.parse(date)) / 60000));
  if (minutes < 1) return "刚刚";
  if (minutes < 60) return minutes + " 分钟";
  if (minutes < 1440) return Math.floor(minutes / 60) + " 小时";
  return Math.floor(minutes / 1440) + " 天";
}

function groupItems(items: InboxItem[]) {
  const grouped = new Map<string, [InboxItem, ...InboxItem[]]>();
  for (const item of items) {
    const group = grouped.get(item.task_id);
    if (group) group.push(item);
    else grouped.set(item.task_id, [item]);
  }
  return Array.from(grouped, ([taskId, entries]): InboxGroup => ({
    taskId,
    items: entries,
    primary: entries.find((item) => item.pending) ?? entries[0],
    unread: !entries[0].read,
  }));
}

function ItemMenu({ item, unread, archived, busy, onRead, onArchive, control }: {
  item: InboxItem;
  unread: boolean;
  archived: boolean;
  busy: boolean;
  onRead: () => void;
  onArchive: () => void;
  control?: MenuControl;
}) {
  return (
    <Dropdown {...control}>
      <Button isIconOnly size="sm" variant="ghost" aria-label="通知操作" isDisabled={busy}>
        <Ellipsis size={16} />
      </Button>
      <Dropdown.Popover placement="bottom end">
        <Dropdown.Menu aria-label="通知操作">
          <Dropdown.Item id="open" href={itemPath(item)} target="_blank" rel="noopener noreferrer">
            在新标签页打开
          </Dropdown.Item>
          {!archived && (
            <Dropdown.Item id="read" onAction={onRead}>
              {unread ? "标为已读" : "标为未读"}
            </Dropdown.Item>
          )}
          <Dropdown.Item id="archive" onAction={onArchive}>
            {archived ? "取消归档" : "归档"}
            <kbd className="inbox-shortcut" aria-hidden="true">E</kbd>
          </Dropdown.Item>
        </Dropdown.Menu>
      </Dropdown.Popover>
    </Dropdown>
  );
}

function InboxRow({ group, selected, archived, busy, onOpen, onRead, onArchive }: {
  group: InboxGroup;
  selected: boolean;
  archived: boolean;
  busy: boolean;
  onOpen: () => void;
  onRead: () => void;
  onArchive: () => void;
}) {
  const [menuOpen, setMenuOpen] = useState(false);
  const { primary } = group;
  const unread = group.unread && !archived;
  const latest = group.items[0];
  const Icon = kinds[latest.type].icon;
  const ArchiveIcon = archived ? ArchiveRestore : Archive;
  return (
    <li
      className="inbox-row"
      data-selected={selected}
      data-unread={unread}
      onContextMenu={(event) => {
        event.preventDefault();
        if (!busy) setMenuOpen(true);
      }}
    >
      <Button
        variant="ghost"
        className="inbox-row-button"
        aria-pressed={selected}
        aria-label={[archived ? "已归档" : unread ? "未读" : "已读", latest.title, itemLabel(latest)].join(" · ")}
        onPress={onOpen}
        onKeyDown={(event) => {
          if (event.key === "ContextMenu" || (event.shiftKey && event.key === "F10")) {
            event.preventDefault();
            if (!busy) setMenuOpen(true);
          }
        }}
      >
        <span className="inbox-row-icon"><Icon size={15} /></span>
        <span className="inbox-row-text">
          <span className="inbox-row-title">
            {unread && <span className="inbox-unread-dot" aria-hidden="true" />}
            <strong>{latest.title}</strong>
          </span>
          <span>{itemLabel(latest)}{group.items.length > 1 && " · " + group.items.length + " 条"}</span>
        </span>
        <time dateTime={latest.created_at} title={new Date(latest.created_at).toLocaleString("zh-CN")}>
          {relativeTime(latest.created_at)}
        </time>
      </Button>
      <span className="inbox-row-actions" data-open={menuOpen}>
        <Button isIconOnly size="sm" variant="ghost" aria-label={archived ? "取消归档" : "归档"} isDisabled={busy} onPress={onArchive}>
          <ArchiveIcon size={14} />
        </Button>
        <ItemMenu item={primary} unread={unread} archived={archived} busy={busy} onRead={onRead} onArchive={onArchive} control={{ isOpen: menuOpen, onOpenChange: setMenuOpen }} />
      </span>
    </li>
  );
}

export function InboxPage() {
  const { principal } = useAuth();
  if (!principal) return null;
  const { workspace, user } = principal;
  return <InboxView key={workspace.id + ":" + user.id} workspaceId={workspace.id} userId={user.id} />;
}

function InboxView({ workspaceId, userId }: { workspaceId: string; userId: string }) {
  const client = useQueryClient();
  const [archived, setArchived] = useState(false);
  const [unreadOnly, setUnreadOnly] = useState(false);
  const [selectedId, setSelectedId] = useState<string>();
  const [selectedSource, setSelectedSource] = useState<string>();
  const [pinnedArchive, setPinnedArchive] = useState<InboxGroup>();
  const [live, setLive] = useState(false);
  const [revoked, setRevoked] = useState(false);
  const listScroll = useRef<HTMLDivElement>(null);
  const loadMoreAnchor = useRef<HTMLDivElement>(null);
  const queryScope = ["inbox", workspaceId, userId] as const;
  const enabled = !!workspaceId && !!userId && !revoked;
  const activeQuery = useQuery({
    queryKey: [...queryScope, "current"],
    queryFn: ({ signal }) => consoleRequest<{ items: InboxItem[] }>(workspaceId, "/inbox", { signal }),
    enabled: enabled && !archived,
    refetchInterval: live ? 30000 : 8000,
  });
  const archiveQuery = useInfiniteQuery({
    queryKey: [...queryScope, "archived", unreadOnly],
    queryFn: ({ pageParam, signal }) => {
      const search = new URLSearchParams({ unread_only: String(unreadOnly) });
      if (pageParam) search.set("cursor", pageParam);
      return consoleRequest<InboxArchivePage>(workspaceId, "/inbox/archived?" + search, { signal });
    },
    initialPageParam: null as string | null,
    getNextPageParam: (page) => page.next_cursor ?? undefined,
    enabled: enabled && archived,
    refetchInterval: live ? 30000 : 8000,
  });
  const query = archived ? archiveQuery : activeQuery;
  const update = useMutation({
    mutationFn: (input: InboxUpdate) => {
      switch (input.kind) {
        case "read":
          return consoleWrite<void>(workspaceId, "/inbox", { item_ids: [input.itemId], read: input.read }, "PATCH");
        case "group":
          return consoleWrite<void>(workspaceId, "/inbox/groups/" + encodeURIComponent(input.taskId), { archived: input.archived }, "PATCH");
        case "bulk":
          return consoleWrite<void>(workspaceId, "/inbox/bulk", { action: input.action }, "POST");
      }
    },
    onSettled: () => client.invalidateQueries({ queryKey: queryScope }),
  });
  const archivedItems = archiveQuery.data?.pages.flatMap((page) => page.items) ?? [];
  const groups = groupItems(archived ? archivedItems : activeQuery.data?.items ?? []);
  if (archived && unreadOnly && pinnedArchive && !groups.some((group) => group.taskId === pinnedArchive.taskId)) {
    groups.unshift(pinnedArchive);
  }
  const visibleGroups = groups.filter((group) => !unreadOnly || group.unread || group.taskId === selectedId);
  const selected = visibleGroups.find((group) => group.taskId === selectedId);
  const unreadCount = groups.filter((group) => group.unread).length;
  const source = selected?.items.find((item) => item.id === selectedSource) ?? selected?.primary;
  const relatedSources = selected
    ? [...new Map([...selected.items].reverse().map((item) => [itemPath(item), item])).values()]
    : [];
  const ArchiveIcon = archived ? ArchiveRestore : Archive;
  const archiveLabel = archived ? "取消归档" : "归档";
  const viewLabel = archived ? "已归档" : "收件箱";

  function markRead(group: InboxGroup, read: boolean) {
    const latest = group.items[0];
    if (latest.read !== read) update.mutate({ kind: "read", itemId: latest.id, read });
  }

  function openGroup(group: InboxGroup) {
    setSelectedId(group.taskId);
    setSelectedSource(undefined);
    setPinnedArchive(archived && unreadOnly && group.unread ? group : undefined);
    // 自动标记只发生在打开时，手动设为未读后可继续查看当前详情。
    markRead(group, true);
  }

  function clearSelection() {
    setSelectedId(undefined);
    setSelectedSource(undefined);
    setPinnedArchive(undefined);
  }

  function switchView(nextArchived: boolean) {
    if (nextArchived === archived) return;
    setArchived(nextArchived);
    clearSelection();
    update.reset();
  }

  function archiveGroup(group: InboxGroup) {
    const index = visibleGroups.indexOf(group);
    const next = visibleGroups[index + 1] ?? visibleGroups[index - 1];
    update.mutate({ kind: "group", taskId: group.taskId, archived: !archived }, {
      onSuccess: () => {
        if (selectedId !== group.taskId) return;
        if (next) openGroup(next);
        else clearSelection();
      },
    });
  }

  function archiveBulk(onlyRead: boolean) {
    const targets = groups.filter((group) => !onlyRead || !group.unread);
    if (!targets.length) return;
    update.mutate({ kind: "bulk", action: onlyRead ? "archive_read" : "archive_all" }, {
      onSuccess: () => {
        if (targets.some((group) => group.taskId === selectedId)) clearSelection();
      },
    });
  }

  useEffect(() => {
    if (!workspaceId || !userId || typeof EventSource === "undefined") return;
    const key = ["inbox", workspaceId, userId];
    const source = new EventSource(
      "/api/v1/workspaces/" + encodeURIComponent(workspaceId) + "/inbox/events/stream",
      { withCredentials: true },
    );
    source.onopen = () => setLive(true);
    source.onerror = () => setLive(false);
    source.addEventListener("inbox_changed", () => {
      // 连接或重连时也回读，补齐断线期间已提交的变化。
      void client.cancelQueries({ queryKey: key }).then(() => client.invalidateQueries({ queryKey: key }));
    });
    source.addEventListener("access_revoked", () => {
      source.close();
      setRevoked(true);
      client.removeQueries({ queryKey: key });
    });
    return () => source.close();
  }, [client, workspaceId, userId]);

  const { fetchNextPage, hasNextPage, isFetching, isFetchNextPageError } = archiveQuery;
  useEffect(() => {
    const root = listScroll.current;
    const anchor = loadMoreAnchor.current;
    if (!archived || !root || !anchor || !hasNextPage || isFetching || isFetchNextPageError ||
      typeof IntersectionObserver === "undefined") return;
    const observer = new IntersectionObserver(([entry]) => {
      if (entry?.isIntersecting) {
        observer.disconnect();
        void fetchNextPage();
      }
    }, { root, rootMargin: "80px" });
    observer.observe(anchor);
    return () => observer.disconnect();
  }, [archived, fetchNextPage, hasNextPage, isFetching, isFetchNextPageError]);

  const archiveShortcut = useEffectEvent((event: KeyboardEvent) => {
    if (!selected || update.isPending || event.defaultPrevented || event.key.toLowerCase() !== "e" ||
      event.altKey || event.ctrlKey || event.metaKey || event.shiftKey || event.repeat) return;
    const target = event.target;
    if (target instanceof HTMLElement && target !== document.body &&
      (!target.closest(".inbox-shell") || target.closest("input, textarea, select, [contenteditable], [role=combobox]"))) return;
    event.preventDefault();
    archiveGroup(selected);
  });
  useEffect(() => {
    // 先接收按键，避免列表按钮或其他控件吞掉归档快捷键。
    const handleKey = (event: KeyboardEvent) => archiveShortcut(event);
    window.addEventListener("keydown", handleKey, true);
    return () => window.removeEventListener("keydown", handleKey, true);
  }, []);

  if (revoked || (query.error instanceof ConsoleApiError && [401, 403, 404].includes(query.error.status))) {
    return <Banner type="danger" title="收件箱访问权限已失效" />;
  }

  return (
    <section
      className="inbox-shell"
      aria-label="待我处理"
      data-has-selection={!!selected}
    >
      <aside className="inbox-list-pane" aria-label="通知列表">
        <header className="inbox-toolbar">
          <h1>
            <Dropdown>
              <Button size="sm" variant="ghost" aria-label={viewLabel + "，切换收件箱视图"} isDisabled={update.isPending}>
                {viewLabel}<ChevronDown size={13} />
              </Button>
              <Dropdown.Popover placement="bottom start">
                <Dropdown.Menu aria-label="收件箱视图" selectionMode="single" selectedKeys={[archived ? "archived" : "current"]}>
                  <Dropdown.Item id="current" onAction={() => switchView(false)}>收件箱</Dropdown.Item>
                  <Dropdown.Item id="archived" onAction={() => switchView(true)}>已归档</Dropdown.Item>
                </Dropdown.Menu>
              </Dropdown.Popover>
            </Dropdown>
          </h1>
          {!archived && unreadCount > 0 && <span className="inbox-unread-count" aria-label={unreadCount + " 条未读"}>{unreadCount}</span>}
          <div className="inbox-toolbar-actions">
            <Dropdown>
              <Button isIconOnly size="sm" variant={unreadOnly ? "secondary" : "ghost"} aria-label="筛选收件箱">
                <Filter size={15} />
              </Button>
              <Dropdown.Popover placement="bottom end">
                <Dropdown.Menu aria-label="通知筛选" selectionMode="single" selectedKeys={[unreadOnly ? "unread" : "all"]}>
                  <Dropdown.Item id="all" onAction={() => setUnreadOnly(false)}>全部通知</Dropdown.Item>
                  <Dropdown.Item id="unread" onAction={() => setUnreadOnly(true)}>仅未读</Dropdown.Item>
                </Dropdown.Menu>
              </Dropdown.Popover>
            </Dropdown>
            {!archived && (
              <Dropdown>
                <Button isIconOnly size="sm" variant="ghost" aria-label="收件箱更多操作" isDisabled={update.isPending}>
                  <Ellipsis size={16} />
                </Button>
                <Dropdown.Popover placement="bottom end">
                  <Dropdown.Menu aria-label="收件箱批量操作">
                    <Dropdown.Item id="read" isDisabled={!unreadCount} onAction={() => update.mutate({ kind: "bulk", action: "read_all" })}>全部标为已读</Dropdown.Item>
                    <Dropdown.Item id="archive-read" isDisabled={!groups.some((group) => !group.unread)} onAction={() => archiveBulk(true)}>归档已读通知</Dropdown.Item>
                    <Dropdown.Item id="archive-all" isDisabled={!groups.length} onAction={() => archiveBulk(false)}>归档全部通知</Dropdown.Item>
                  </Dropdown.Menu>
                </Dropdown.Popover>
              </Dropdown>
            )}
          </div>
        </header>
        <div className="inbox-list-scroll" ref={listScroll}>
          {update.isError && <Banner type="danger" description={errorMessage(update.error)} />}
          {query.isPending ? <Spin tip="正在读取收件箱" /> : query.isError && !query.data ? (
            <Banner type="danger" title="收件箱暂时无法加载" description={errorMessage(query.error)} />
          ) : visibleGroups.length ? (
            <ul aria-label="收件箱事项">
              {visibleGroups.map((group) => (
                <InboxRow
                  key={group.taskId}
                  group={group}
                  selected={group === selected}
                  archived={archived}
                  busy={update.isPending}
                  onOpen={() => { if (!update.isPending) openGroup(group); }}
                  onRead={() => markRead(group, group.unread)}
                  onArchive={() => archiveGroup(group)}
                />
              ))}
            </ul>
          ) : !(archived && hasNextPage) && (
            <p className="inbox-list-empty">{unreadOnly ? "暂无未读通知" : archived ? "暂无已归档通知" : "暂无待处理事项"}</p>
          )}
          {archived && hasNextPage && (
            <div className="inbox-load-more" ref={loadMoreAnchor}>
              <Button size="sm" variant="secondary" isDisabled={isFetching} onPress={() => void fetchNextPage()}>
                {archiveQuery.isFetchingNextPage ? "正在加载…" : isFetchNextPageError ? "重试加载" : "加载更多"}
              </Button>
            </div>
          )}
        </div>
      </aside>
      <section className="inbox-detail-pane" aria-label="通知详情">
        {selected && source ? (
          <>
            <header className="inbox-toolbar inbox-detail-toolbar">
              <Button size="sm" variant="ghost" className="inbox-back" isDisabled={update.isPending} onPress={clearSelection}>
                <ArrowLeft size={16} />{viewLabel}
              </Button>
              <Link className="inbox-source-link" to={itemPath(source)} title={source.title}>
                <span>{source.title}</span><ArrowUpRight size={14} />
              </Link>
              <div className="inbox-toolbar-actions">
                <Button
                  isIconOnly
                  size="sm"
                  variant="ghost"
                  aria-label={archiveLabel}
                  aria-keyshortcuts="E"
                  isDisabled={update.isPending}
                  onPress={() => archiveGroup(selected)}
                >
                  <ArchiveIcon size={16} />
                </Button>
                <ItemMenu item={source} unread={selected.unread} archived={archived} busy={update.isPending} onRead={() => markRead(selected, selected.unread)} onArchive={() => archiveGroup(selected)} />
              </div>
            </header>
            {relatedSources.length > 1 && (
              <nav className="inbox-related" aria-label="同一任务的处理入口">
                {relatedSources.map((item) => (
                  <Button key={item.id} size="sm" variant={itemPath(item) === itemPath(source) ? "secondary" : "ghost"} onPress={() => setSelectedSource(item.id)}>
                    {itemLabel(item)}
                  </Button>
                ))}
              </nav>
            )}
            <div className="inbox-detail-body" key={itemPath(source)}>
              {update.isError && <div className="inbox-mobile-error"><Banner type="danger" description={errorMessage(update.error)} /></div>}
              {source.run_id ? (
                <div className="inbox-approval"><DeliveryPanel workspaceId={workspaceId} userId={userId} runId={source.run_id} /></div>
              ) : (
                <TaskDetailContent key={source.task_id} taskId={source.task_id} embedded />
              )}
            </div>
          </>
        ) : (
          <div className="inbox-detail-empty"><Inbox size={34} /><p>选择一条通知查看详情</p></div>
        )}
      </section>
    </section>
  );
}
