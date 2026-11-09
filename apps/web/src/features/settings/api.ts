import { consoleRequest, consoleWrite } from "../runs/api";

export type HarnessId = "pi" | "oh-my-pi";
export type HarnessSettings = {
  harness: HarnessId;
  revision: number;
  omp_available: boolean;
};
export const getHarnessSettings = (workspace: string) =>
  consoleRequest<HarnessSettings>(workspace, "/settings/harness");
export const saveHarnessSettings = (
  workspace: string,
  values: Pick<HarnessSettings, "harness" | "revision">,
) => consoleWrite<HarnessSettings>(workspace, "/settings/harness", values, "PUT");

export type GitSettings = {
  revision: number;
  name_mode: "actor_agent" | "actor" | "environment" | "manual";
  email_mode: "actor" | "environment" | "manual";
  manual_name: string;
  manual_email: string;
  co_authored_by: boolean;
};
export const getGitSettings = (workspace: string) =>
  consoleRequest<GitSettings>(workspace, "/settings/git");
export const saveGitSettings = (workspace: string, values: GitSettings) =>
  consoleWrite<GitSettings>(workspace, "/settings/git", values, "PUT");
export type ModelConnection = {
  id: string;
  name: string;
  provider: string;
  model_id: string;
  has_key: boolean;
  key_source: "deployment" | "saved" | "missing";
};
export type ModelBudget = {
  max_tokens: number | null;
  timeout_seconds: number | null;
  execution_timeout_seconds: number | null;
};
export type ModelProvider = {
  id: string;
  label: string;
  models: { id: string; name: string }[];
};
export type ModelCatalog = {
  revision: number;
  primary_id: string;
  items: ModelConnection[];
  budget: ModelBudget;
  effective_budget: Record<keyof ModelBudget, number>;
  providers: ModelProvider[];
  runtime_enabled: boolean;
};
export type ModelConnectionInput = {
  revision: number;
  name: string;
  provider: string;
  model_id: string;
  api_key?: string;
  make_primary: boolean;
};
export const getModelCatalog = (workspace: string) =>
  consoleRequest<ModelCatalog>(workspace, "/settings/model-connections");
export const saveModelConnection = (
  workspace: string,
  input: ModelConnectionInput,
  id?: string,
) =>
  consoleWrite<ModelCatalog>(
    workspace,
    `/settings/model-connections${id ? `/${encodeURIComponent(id)}` : ""}`,
    input,
    id ? "PUT" : "POST",
  );
export const modelConnectionAction = (
  workspace: string,
  id: string,
  revision: number,
  action: "primary" | "remove",
) =>
  consoleWrite<ModelCatalog>(
    workspace,
    `/settings/model-connections/${encodeURIComponent(id)}/${action}`,
    { revision },
  );
export const checkModelConnection = (
  workspace: string,
  id: string,
  revision: number,
) =>
  consoleWrite<{ status: string }>(
    workspace,
    `/settings/model-connections/${encodeURIComponent(id)}/check`,
    { revision },
  );
export const saveModelBudget = (
  workspace: string,
  revision: number,
  budget: ModelBudget,
) =>
  consoleWrite<ModelCatalog>(
    workspace,
    "/settings/model-budget",
    { revision, ...budget },
    "PUT",
  );
