import { useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { Button, Card, Empty, Input, TextArea } from "../../components/console";
import { editProject, type Project } from "../projects/api";
import { useAuth } from "../auth/AuthProvider";
import {
  useSettingsDirty,
  useSettingsEnvironment,
} from "./SettingsEnvironment";
import { SettingsLoading, SettingsPage, SettingsResult } from "./SettingsPage";

export function BusinessSpaceSettingsPage() {
  const env = useSettingsEnvironment();
  if (!env) throw new Error("缺少业务空间上下文");
  return (
    <SettingsPage
      title="业务空间"
      description="为一条业务线管理资料、研发约定和多个代码仓库。"
      actions={
        <Button theme="solid" onClick={env.create}>
          新增业务空间
        </Button>
      }
    >
      <SettingsLoading
        loading={env.loading}
        error={env.error}
        retry={env.refresh}
      />
      {env.project && !env.error ? (
        <BusinessSpaceForm
          key={env.project.id}
          project={env.project}
          refresh={env.refresh}
        />
      ) : (
        !env.loading &&
        !env.error && (
          <Card>
            <Empty
              title="还没有业务空间"
              description="创建业务空间后，可添加前端、后端、App 等多个仓库。"
            >
              <Button theme="solid" onClick={env.create}>
                创建业务空间
              </Button>
            </Empty>
          </Card>
        )
      )}
    </SettingsPage>
  );
}
function BusinessSpaceForm({
  project,
  refresh,
}: {
  project: Project;
  refresh: () => Promise<unknown>;
}) {
  const { principal } = useAuth();
  const [values, setValues] = useState({
    name: project.name,
    description: project.description,
    architecture: project.architecture,
    conventions: project.conventions,
  });
  const [savedProject, setSavedProject] = useState(project);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [success, setSuccess] = useState<string | null>(null);
  const [baseline, setBaseline] = useState({
    name: project.name,
    description: project.description,
    architecture: project.architecture,
    conventions: project.conventions,
  });
  const dirty = JSON.stringify(values) !== JSON.stringify(baseline);
  useSettingsDirty(dirty);
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (busy || !principal) return;
    setBusy(true);
    setError(null);
    try {
      const saved = await editProject(
        principal.workspace.id,
        savedProject,
        values,
      );
      const next = {
        name: saved.name,
        description: saved.description,
        architecture: saved.architecture,
        conventions: saved.conventions,
      };
      setValues(next);
      setBaseline(next);
      setSavedProject(saved);
      setSuccess("业务空间资料已保存");
      await refresh();
    } catch (cause) {
      setError(cause);
    } finally {
      setBusy(false);
    }
  }
  return (
    <form className="settings-stack" onSubmit={submit}>
      {!project.can_manage && (
        <p className="form-help">
          你可以查看当前业务空间；只有管理者可以修改资料。
        </p>
      )}
      <Card title="当前业务空间">
        <fieldset
          disabled={!project.can_manage || busy}
          className="settings-stack"
        >
          <label className="form-field settings-fields">
            <span>业务空间名称</span>
            <Input
              required
              maxLength={120}
              value={values.name}
              onChange={(name) => setValues({ ...values, name })}
            />
          </label>
          <label className="form-field">
            <span>介绍</span>
            <TextArea
              rows={3}
              maxLength={6000}
              value={values.description}
              onChange={(description) => setValues({ ...values, description })}
              placeholder="业务目标、核心概念与已知约束"
            />
          </label>
          <label className="form-field">
            <span>整体架构</span>
            <TextArea
              rows={3}
              maxLength={6000}
              value={values.architecture}
              onChange={(architecture) =>
                setValues({ ...values, architecture })
              }
            />
          </label>
          <label className="form-field">
            <span>研发约定</span>
            <TextArea
              rows={3}
              maxLength={4000}
              value={values.conventions}
              onChange={(conventions) => setValues({ ...values, conventions })}
            />
          </label>
        </fieldset>
        <p className="form-help">
          业务空间 ID：<code>{project.id}</code> · 资料版本 v
          {savedProject.version}
          。修改仅影响后续新任务的上下文。
        </p>
        <SettingsResult error={error} success={success} />
        {project.can_manage && (
          <div className="settings-save-row">
            <Button
              theme="solid"
              htmlType="submit"
              loading={busy}
              disabled={!dirty || !values.name.trim()}
            >
              保存修改
            </Button>
            <Link to="/settings/code">管理关联仓库 →</Link>
          </div>
        )}
      </Card>
    </form>
  );
}
