import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useState,
  type PropsWithChildren,
} from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useBlocker } from "react-router-dom";
import { ChevronDown, FolderGit2 } from "lucide-react";
import { Banner, Button, Modal, Select } from "../../components/console";
import { useAuth } from "../auth/AuthProvider";
import {
  createProject,
  listProjects,
  projectKey,
  type Project,
} from "../projects/api";
import { ProjectEditor } from "../projects/ProjectEditors";
import { errorMessage } from "../runs/api";
import { SettingsDraftsContext } from "./SettingsDrafts";
export { useSettingsDirty } from "./SettingsDrafts";

type Environment = {
  project: Project | undefined;
  projects: Project[];
  loading: boolean;
  error: unknown;
  refresh: () => Promise<unknown>;
  choose: (id: string) => void;
  create: () => void;
  attempt: (action: () => void) => void;
  draftResetVersion: number;
};
const Context = createContext<Environment | null>(null);
export const useSettingsEnvironment = () => useContext(Context);

/** 业务空间复用 Project；不能把它当作租户身份覆盖 workspace_id。 */
export function SettingsEnvironment({ children }: PropsWithChildren) {
  const { principal } = useAuth();
  if (!principal) throw new Error("设置缺少认证上下文");
  const { workspace, user } = principal;
  const storageKey = `weora-business-space:${workspace.id}:${user.id}`;
  const queryClient = useQueryClient();
  const query = useQuery({
    queryKey: projectKey(workspace.id, user.id),
    queryFn: () => listProjects(workspace.id),
  });
  const projects = (query.data?.items ?? []).filter(
    (item) => item.status === "active",
  );
  const [selected, setSelected] = useState(
    () => localStorage.getItem(storageKey) ?? "",
  );
  const project = projects.find((item) => item.id === selected) ?? projects[0];
  const [draftResetVersion, setDraftResetVersion] = useState(0);
  const [creating, setCreating] = useState(false);
  const [dirtyKeys, setDirtyKeys] = useState<Set<symbol>>(new Set());
  const [pending, setPending] = useState<{
    run: () => void;
    resetPage: boolean;
  } | null>(null);
  const blocker = useBlocker(
    ({ currentLocation, nextLocation }) =>
      dirtyKeys.size > 0 &&
      (currentLocation.pathname !== nextLocation.pathname ||
        currentLocation.search !== nextLocation.search),
  );
  const register = useCallback(
    (key: symbol, dirty: boolean) =>
      setDirtyKeys((previous) => {
        if (previous.has(key) === dirty) return previous;
        const next = new Set(previous);
        if (dirty) next.add(key);
        else next.delete(key);
        return next;
      }),
    [],
  );
  useEffect(() => {
    if (!dirtyKeys.size) return;
    const guard = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", guard);
    return () => window.removeEventListener("beforeunload", guard);
  }, [dirtyKeys]);
  const attempt = (action: () => void, resetPage = false) => {
    if (dirtyKeys.size) setPending({ run: action, resetPage });
    else action();
  };
  const choose = (id: string) => {
    if (id === project?.id || !projects.some((item) => item.id === id)) return;
    attempt(() => {
      setSelected(id);
      setDraftResetVersion((version) => version + 1);
      localStorage.setItem(storageKey, id);
    });
  };
  const refresh = () =>
    queryClient.invalidateQueries({
      queryKey: projectKey(workspace.id, user.id),
    });
  const closeConfirm = () => {
    setPending(null);
    if (blocker.state === "blocked") blocker.reset();
  };
  return (
    <SettingsDraftsContext.Provider value={register}>
      <Context.Provider
        value={{
          project,
          projects,
          loading: query.isPending,
          error: query.error,
          refresh,
          choose,
          create: () => attempt(() => setCreating(true), true),
          attempt,
          draftResetVersion,
        }}
      >
        {children}
        {creating && (
          <ProjectEditor
            terminology="业务空间"
            onClose={() => setCreating(false)}
            onSave={async (values) => {
              const created = await createProject(workspace.id, values);
              await refresh();
              setSelected(created.id);
              localStorage.setItem(storageKey, created.id);
            }}
          />
        )}
        <Modal
          visible={!!pending || blocker.state === "blocked"}
          title="离开前保存修改？"
          onCancel={closeConfirm}
          footer={null}
          width={480}
        >
          <p className="form-help">当前内容尚未保存，离开后将丢失本页修改。</p>
          <div className="modal-actions">
            <Button onClick={closeConfirm}>继续编辑</Button>
            <Button
              type="danger"
              onClick={() => {
                setDirtyKeys(new Set());
                const action = pending;
                setPending(null);
                if (blocker.state === "blocked") blocker.proceed();
                else {
                  if (action?.resetPage)
                    setDraftResetVersion((version) => version + 1);
                  action?.run();
                }
              }}
            >
              放弃修改并离开
            </Button>
          </div>
        </Modal>
      </Context.Provider>
    </SettingsDraftsContext.Provider>
  );
}

export function BusinessSpacePicker({
  onOpen,
  onDismiss,
}: {
  onOpen?: () => void;
  onDismiss?: () => void;
} = {}) {
  const { principal } = useAuth();
  const env = useSettingsEnvironment();
  const [open, setOpen] = useState(false);
  if (!env) return null;
  return (
    <>
      <Button
        theme="light"
        className="workspace-switch"
        onClick={() => {
          onOpen?.();
          setOpen(true);
        }}
        aria-label="切换业务空间"
      >
        <FolderGit2 size={20} className="workspace-glyph" aria-hidden="true" />
        <span className="sidebar-text">
          <strong>
            {env.project?.name ?? (env.loading ? "正在加载…" : "选择业务空间")}
          </strong>
          <small>{principal?.workspace.name ?? "当前业务空间"}</small>
        </span>
        <ChevronDown size={14} aria-hidden="true" />
      </Button>
      <Modal
        title="切换业务空间"
        visible={open}
        onCancel={() => {
          setOpen(false);
          onDismiss?.();
        }}
        footer={null}
        width={480}
      >
        <div className="agent-form">
          {!!env.error && (
            <Banner type="danger" description={errorMessage(env.error)} />
          )}
          <p className="form-help">
            业务空间区分业务项目。团队配置对本团队所有业务空间生效。
          </p>
          <Select
            aria-label="当前业务空间"
            value={env.project?.id ?? ""}
            placeholder="暂无业务空间"
            optionList={env.projects.map((item) => ({
              value: item.id,
              label: item.name,
            }))}
            onChange={(value) => {
              env.choose(String(value));
              setOpen(false);
            }}
          />
          <Button
            theme="solid"
            onClick={() => {
              setOpen(false);
              env.create();
            }}
          >
            新增业务空间
          </Button>
        </div>
      </Modal>
    </>
  );
}
