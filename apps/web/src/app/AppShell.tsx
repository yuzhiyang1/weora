import {
  Activity,
  ArrowLeft,
  KeyRound,
  MessageSquare,
  UserRound,
  ArrowUpRight,
  BookOpen,
  Bot,
  ChevronDown,
  ChevronsLeft,
  ChevronsRight,
  FolderGit2,
  Inbox,
  GitBranch,
  LayoutDashboard,
  LogOut,
  Menu,
  Moon,
  Plus,
  Puzzle,
  Search,
  Settings2,
  Sun,
  Users,
  X,
  type LucideIcon,
} from "lucide-react";
import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import {
  Autocomplete,
  Avatar,
  Input,
  ListBox,
  Popover,
  SearchField,
} from "@heroui/react";
import { buttonVariants } from "@heroui/styles";
import {
  Link,
  NavLink,
  Outlet,
  useLocation,
  useNavigate,
} from "react-router-dom";

import { Button, Modal } from "../components/console";
import { useAuth } from "../features/auth/AuthProvider";
import { HealthBadge } from "../features/system/HealthBadge";
import { BrandWordmark } from "../components/brand/BrandWordmark";
import {
  BusinessSpacePicker,
  useSettingsEnvironment,
} from "../features/settings/SettingsEnvironment";
import "../features/settings/settings.css";
import { SettingsMobileNavigation } from "../features/settings/SettingsMobileNavigation";

type NavigationItem = {
  label: string;
  mobileLabel?: string;
  path: string;
  icon: LucideIcon;
  planned?: boolean;
};
const sections: { label: string; items: NavigationItem[] }[] = [
  {
    label: "工作台",
    items: [
      { label: "概览", mobileLabel: "工作台", path: "/", icon: LayoutDashboard },
      { label: "运行", mobileLabel: "任务列表", path: "/runs", icon: Activity },
      { label: "协作任务", path: "/tasks", icon: Users },
      { label: "待我处理", path: "/inbox", icon: Inbox },
      { label: "协调中心", path: "/coordination", icon: Bot },
      { label: "项目", path: "/projects", icon: FolderGit2 },
    ],
  },
  {
    label: "团队能力",
    items: [
      { label: "Agent", path: "/agents", icon: Bot },
      { label: "Skills", path: "/skills", icon: Puzzle },
      { label: "飞书上下文", path: "/context", icon: Settings2 },
    ],
  },
  {
    label: "探索",
    items: [
      { label: "工作流", path: "/workflows", icon: GitBranch, planned: true },
      { label: "知识库", path: "/knowledge", icon: BookOpen, planned: true },
      { label: "协作室", path: "/rooms", icon: Users, planned: true },
    ],
  },
  {
    label: "连接与设置",
    items: [{ label: "设置", path: "/settings/profile", icon: Settings2 }],
  },
];
const navigation = sections.flatMap((section) => section.items);
const settingsSections: typeof sections = [
  {
    label: "设置",
    items: [
      { label: "个人资料", path: "/settings/profile", icon: UserRound },
      { label: "代码", path: "/settings/code", icon: GitBranch },
      { label: "业务空间", path: "/settings/business-space", icon: FolderGit2 },
    ],
  },
  {
    label: "团队配置 · 影响所有业务空间",
    items: [
      { label: "模型与密钥", path: "/settings/models", icon: KeyRound },
      { label: "Agent 执行内核", path: "/settings/harness", icon: Settings2 },
      { label: "飞书连接", path: "/settings/feishu", icon: MessageSquare },
      { label: "Agent 协调", path: "/settings/coordination", icon: Bot },
    ],
  },
];
const settingsNavigation = settingsSections.flatMap((section) => section.items);

function navigationLabel(
  item: Pick<NavigationItem, "label" | "mobileLabel">,
  isMobile: boolean,
) {
  return (isMobile && item.mobileLabel) || item.label;
}

const mobileQuery = "(max-width: 760px)";
function subscribeViewport(listener: () => void) {
  const query = window.matchMedia?.(mobileQuery);
  query?.addEventListener("change", listener);
  return () => query?.removeEventListener("change", listener);
}
function isNarrowViewport() {
  return window.matchMedia?.(mobileQuery).matches ?? false;
}

/** 外壳只管理导航与外观；认证、任务和工作空间权限继续由各业务模块负责。 */
export function AppShell() {
  const { principal, logout } = useAuth();
  const location = useLocation();
  const businessSpace = useSettingsEnvironment();
  const settingsOpen = location.pathname.startsWith("/settings");
  const navigate = useNavigate();
  const [searchOpen, setSearchOpen] = useState(false);
  const [mobileOpen, setMobileOpen] = useState(false);
  const [compact, setCompact] = useState(false);
  const [dark, setDark] = useState(
    () => localStorage.getItem("weora-appearance") !== "light",
  );
  const [loggingOut, setLoggingOut] = useState(false);
  const [logoutError, setLogoutError] = useState<string | null>(null);
  const sidebar = useRef<HTMLElement>(null);
  const mobileMenu = useRef<HTMLButtonElement>(null);
  const searchFromMobileMenu = useRef(false);
  const isMobile = useSyncExternalStore(
    subscribeViewport,
    isNarrowViewport,
    () => false,
  );
  useEffect(() => {
    document.documentElement.classList.toggle("dark", dark);
    document.documentElement.dataset.theme = dark ? "dark" : "light";
    localStorage.setItem("weora-appearance", dark ? "dark" : "light");
  }, [dark]);
  useEffect(() => {
    function shortcut(event: KeyboardEvent) {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setMobileOpen(false);
        setSearchOpen((open) => !open);
      }
      if (event.key === "Escape") setMobileOpen(false);
    }
    window.addEventListener("keydown", shortcut);
    return () => window.removeEventListener("keydown", shortcut);
  }, []);
  useEffect(() => {
    if (!isMobile || !mobileOpen) return;
    const previous = document.activeElement;
    function focusable() {
      return Array.from(
        sidebar.current?.querySelectorAll<HTMLElement>(
          "a[href], button:not(:disabled)",
        ) ?? [],
      ).filter((element) => element.offsetParent !== null);
    }
    focusable()[0]?.focus();
    function trapFocus(event: KeyboardEvent) {
      if (event.key !== "Tab") return;
      const controls = focusable();
      const first = controls[0];
      const last = controls[controls.length - 1];
      if (event.shiftKey && document.activeElement === first && last) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last && first) {
        event.preventDefault();
        first.focus();
      }
    }
    document.addEventListener("keydown", trapFocus);
    return () => {
      document.removeEventListener("keydown", trapFocus);
      if (previous instanceof HTMLElement && previous.isConnected)
        previous.focus();
    };
  }, [isMobile, mobileOpen]);
  useEffect(() => {
    if (searchOpen || !searchFromMobileMenu.current) return;

    // 弹窗原来的触发按钮在已关闭的侧栏里，归还焦点到可见的菜单入口。
    const frame = window.requestAnimationFrame(() => {
      searchFromMobileMenu.current = false;
      mobileMenu.current?.focus();
    });
    return () => window.cancelAnimationFrame(frame);
  }, [searchOpen]);
  if (!principal) throw new Error("认证外壳缺少当前用户");
  const current =
    [...settingsNavigation, ...navigation].find(
      (item) => item.path !== "/" && location.pathname.startsWith(item.path),
    ) ?? navigation[0]!;
  const runDetailOpen = location.pathname.startsWith("/runs/");
  let surfaceTitle: string | undefined;
  if (location.pathname === "/") surfaceTitle = "工作台";
  else if (location.pathname === "/runs") surfaceTitle = "任务列表";
  else if (runDetailOpen) surfaceTitle = "任务详情";
  else if (["/projects", "/agents", "/skills"].includes(current.path)) {
    const detailTitle =
      current.path === "/projects"
        ? "项目详情"
        : current.path === "/skills"
          ? "Skill 详情"
          : "Agent 详情";
    surfaceTitle =
      location.pathname === current.path ? current.label : detailTitle;
  }
  const mobileTitle = isMobile && (settingsOpen ? "设置" : surfaceTitle);
  function visit(path: string) {
    setSearchOpen(false);
    setMobileOpen(false);
    navigate(path);
  }
  async function handleLogout() {
    setLoggingOut(true);
    setLogoutError(null);
    try {
      await logout();
    } catch {
      setLogoutError("退出失败，请稍后重试");
      setLoggingOut(false);
    }
  }
  return (
    <div
      className="app-shell"
      data-compact={compact && !isMobile}
      data-mobile-open={mobileOpen}
      data-settings={settingsOpen}
      data-mobile-surface={!!surfaceTitle}
    >
      <a className="skip-link" href="#main-content">
        跳到主要内容
      </a>
      {mobileOpen && (
        <button
          className="sidebar-scrim"
          aria-label="关闭导航"
          tabIndex={-1}
          onClick={() => setMobileOpen(false)}
        />
      )}
      <aside
        ref={sidebar}
        id="workspace-navigation"
        className="app-sidebar"
        aria-label="工作空间导航"
        aria-hidden={isMobile && !mobileOpen}
        inert={isMobile && !mobileOpen}
      >
        <div className="sidebar-brand-row">
          <Link
            className="app-brand"
            to="/"
            aria-label="Weora 首页"
            onClick={() => setMobileOpen(false)}
          >
            <BrandWordmark />
          </Link>
          <Button
            icon={mobileOpen ? <X size={16} /> : <ChevronsLeft size={15} />}
            theme="borderless"
            aria-label={
              mobileOpen ? "关闭导航菜单" : compact ? "展开侧栏" : "收起侧栏"
            }
            onClick={() =>
              mobileOpen ? setMobileOpen(false) : setCompact((value) => !value)
            }
          />
        </div>
        {businessSpace ? (
          <BusinessSpacePicker
            onOpen={() => setMobileOpen(false)}
            onDismiss={() => {
              if (isMobile) {
                window.requestAnimationFrame(() => mobileMenu.current?.focus());
              }
            }}
          />
        ) : (
          <Popover>
            <Button theme="borderless" className="workspace-switch">
              <span className="workspace-avatar">
                {Array.from(principal.workspace.name).slice(0, 1)}
              </span>
              <span className="sidebar-text">
                <strong>{principal.workspace.name}</strong>
                <small>团队工作空间</small>
              </span>
              <ChevronDown size={14} />
            </Button>
            <Popover.Content placement="bottom start">
              <Popover.Dialog
                className="workspace-popover"
                aria-label="当前工作空间"
              >
                <strong>{principal.workspace.name}</strong>
                <p>
                  当前身份：
                  {principal.workspace.role === "owner"
                    ? "所有者"
                    : principal.workspace.role === "admin"
                      ? "管理员"
                      : "成员"}
                </p>
                <p>所有资产与任务均在当前工作空间中访问。</p>
              </Popover.Dialog>
            </Popover.Content>
          </Popover>
        )}
        {!settingsOpen && (
          <Button
            theme="borderless"
            className="sidebar-search"
            onClick={() => {
              // 交给查找弹窗接管焦点，避免两个导航层同时打开。
              searchFromMobileMenu.current = isMobile && mobileOpen;
              setMobileOpen(false);
              setSearchOpen(true);
            }}
          >
            <Search size={15} />
            <span className="sidebar-text">快速查找</span>
            <kbd>⌘ K</kbd>
          </Button>
        )}
        {settingsOpen && (
          <Link
            to="/"
            className={`${buttonVariants({ variant: "ghost" })} app-navigation-item settings-back`}
            onClick={() => setMobileOpen(false)}
          >
            <ArrowLeft size={16} />
            <span className="sidebar-text">返回工作台</span>
          </Link>
        )}
        {!settingsOpen && (
          <Button
            className="sidebar-create"
            aria-label="快速发起任务"
            icon={<Plus size={16} />}
            theme="light"
            onClick={() => visit("/runs?new=1")}
          >
            发起任务
          </Button>
        )}
        <nav className="app-navigation" aria-label="主导航">
          {(settingsOpen ? settingsSections : sections).map((section) => (
            <div className="navigation-group" key={section.label}>
              <p className="navigation-label">{section.label}</p>
              {section.items.map(({ icon: Icon, ...item }) => (
                <NavLink
                  key={item.path}
                  to={item.path}
                  end={item.path === "/"}
                  title={navigationLabel(item, isMobile)}
                  aria-label={navigationLabel(item, isMobile)}
                  className={({ isActive }) =>
                    `${buttonVariants({ variant: isActive ? "secondary" : "ghost" })} app-navigation-item${isActive ? " active" : ""}`
                  }
                  onClick={() => setMobileOpen(false)}
                >
                  <Icon size={17} />
                  <span className="sidebar-text">
                    {navigationLabel(item, isMobile)}
                  </span>
                  {item.planned && <span className="nav-planned">规划</span>}
                </NavLink>
              ))}
            </div>
          ))}
        </nav>
        <div className="sidebar-bottom">
          <HealthBadge />
          <div className="account-row">
            <Avatar size="sm">
              {principal.user.avatar_url && (
                <Avatar.Image src={principal.user.avatar_url} alt="个人头像" />
              )}
              <Avatar.Fallback>
                {Array.from(principal.user.display_name).slice(0, 1)}
              </Avatar.Fallback>
            </Avatar>
            <span className="sidebar-text">
              <strong>{principal.user.display_name}</strong>
              <small>{principal.user.email}</small>
            </span>
            <Link
              to="/settings/profile"
              aria-label="账户设置"
              onClick={() => setMobileOpen(false)}
              className={buttonVariants({
                variant: "ghost",
                size: "sm",
                isIconOnly: true,
              })}
            >
              <Settings2 size={15} />
            </Link>
            <Button
              aria-label="退出登录"
              icon={<LogOut size={15} />}
              theme="borderless"
              loading={loggingOut}
              onClick={() =>
                businessSpace
                  ? businessSpace.attempt(() => void handleLogout())
                  : void handleLogout()
              }
            />
          </div>
        </div>
      </aside>
      <div className="workspace-main" inert={isMobile && mobileOpen}>
        <header className="workspace-topbar">
          <div className="workspace-breadcrumb">
            <Button
              ref={mobileMenu}
              {...(!mobileTitle && { theme: "borderless" as const })}
              size={mobileTitle ? "small" : "default"}
              className="mobile-menu"
              aria-label="打开导航"
              aria-controls="workspace-navigation"
              aria-expanded={mobileOpen}
              onClick={() => {
                setCompact(false);
                setMobileOpen(true);
              }}
            >
              {mobileTitle ? "菜单" : <Menu size={18} />}
            </Button>
            {compact && (
              <Button
                theme="borderless"
                className="expand-sidebar"
                aria-label="展开侧栏"
                onClick={() => setCompact(false)}
              >
                <ChevronsRight size={17} />
              </Button>
            )}
            {mobileTitle ? (
              <div className="mobile-page-context">
                <strong>{mobileTitle}</strong>
                {!settingsOpen && (
                  <span className="breadcrumb-workspace">
                    {businessSpace?.project?.name ?? principal.workspace.name}
                  </span>
                )}
              </div>
            ) : (
              <>
                <span className="breadcrumb-workspace">
                  {businessSpace?.project?.name ?? principal.workspace.name}
                </span>
                <span className="breadcrumb-divider">/</span>
                {settingsOpen && (
                  <>
                    <span>设置</span>
                    <span className="breadcrumb-divider">/</span>
                  </>
                )}
                <current.icon size={15} />
                <strong>{current.label}</strong>
              </>
            )}
            {!mobileTitle &&
              location.pathname.split("/").filter(Boolean).length > 1 &&
              !location.pathname.startsWith("/settings/") && (
                <>
                  <span className="breadcrumb-divider">/</span>
                  <span>详情</span>
                </>
              )}
          </div>
          <div className="topbar-actions">
            <span className="private-workspace">
              <span />
              私有工作空间
            </span>
            <Button
              theme="borderless"
              icon={dark ? <Sun size={16} /> : <Moon size={16} />}
              aria-label={dark ? "切换浅色外观" : "切换深色外观"}
              onClick={() => setDark((value) => !value)}
            />
          </div>
        </header>
        {logoutError && (
          <div role="alert" className="global-notice">
            {logoutError}
          </div>
        )}
        <main className="app-main" id="main-content">
          {settingsOpen && isMobile && (
            <SettingsMobileNavigation items={settingsNavigation} />
          )}
          <Outlet
            key={businessSpace?.draftResetVersion ?? 0}
            context={{ dark, setDark }}
          />
        </main>
        <footer className="workspace-footer">
          <span>Weora · Human & Agent</span>
          <span>
            每次协作，都有迹可循 <ArrowUpRight size={12} />
          </span>
        </footer>
      </div>
      <Modal
        title="快速查找"
        visible={searchOpen}
        onCancel={() => setSearchOpen(false)}
        width={540}
      >
        <Autocomplete.Filter
          filter={(text, input) =>
            text.toLocaleLowerCase().includes(input.toLocaleLowerCase())
          }
        >
          <SearchField aria-label="搜索页面或操作" autoFocus>
            <Input placeholder="搜索页面或操作…" aria-label="搜索页面或操作" />
          </SearchField>
          <ListBox
            aria-label="页面与操作"
            onAction={(key) => visit(String(key))}
            renderEmptyState={() => (
              <p className="picker-empty">没有匹配结果</p>
            )}
          >
            <ListBox.Item id="/runs?new=1" textValue="发起任务">
              <Plus />
              发起任务
            </ListBox.Item>
            {[...navigation, ...settingsNavigation]
              .filter(
                (item, index, items) =>
                  items.findIndex((other) => other.path === item.path) ===
                  index,
              )
              .map(({ icon: Icon, ...item }) => (
                <ListBox.Item
                  key={item.path}
                  id={item.path}
                textValue={navigationLabel(item, isMobile)}
                >
                  <Icon size={16} />
                {navigationLabel(item, isMobile)}
                </ListBox.Item>
              ))}
          </ListBox>
        </Autocomplete.Filter>
      </Modal>
    </div>
  );
}
