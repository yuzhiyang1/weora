import type { TableColumn } from "../../components/console/data-table";
import {
  Plus as IconPlus,
  Puzzle as IconPuzzle,
  Search as IconSearch,
} from "lucide-react";
import {
  Button,
  Empty,
  Input,
  Select,
  Table,
  Tag,
  Typography,
} from "../../components/console";
import {
  AssetDetailsLink,
  AssetLoadError,
} from "../../components/console/asset-controls";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRef, useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";

import { useAuth } from "../auth/AuthProvider";
import {
  createSkill,
  listSkills,
  SkillsApiError,
  type SkillSummary,
} from "./api";
import { SkillPackageDialog } from "./SkillPackageDialog";
import "./skill-reader.css";

export function SkillsPage() {
  const { principal } = useAuth();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [searchInput, setSearchInput] = useState("");
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState("");
  const [uploadOpen, setUploadOpen] = useState(false);
  const searchRef = useRef<HTMLInputElement>(null);

  if (!principal) throw new Error("Skills 页面缺少当前工作空间");
  const workspaceId = principal.workspace.id;
  const canWrite = ["owner", "admin"].includes(principal.workspace.role);
  const skillsQuery = useQuery({
    queryKey: ["skills", workspaceId, search, status],
    queryFn: () => listSkills(workspaceId, { search, status }),
  });
  const createMutation = useMutation({
    mutationFn: (file: File) => createSkill(workspaceId, file),
    onSuccess: async (skill) => {
      await queryClient.invalidateQueries({
        queryKey: ["skills", workspaceId],
      });
      setUploadOpen(false);
      navigate(`/skills/${skill.id}`);
    },
  });

  function submitSearch(event: FormEvent) {
    event.preventDefault();
    setSearch(searchInput.trim());
  }

  const items = skillsQuery.data?.items ?? [];
  const hasFilters = Boolean(search || status);
  const columns: TableColumn<SkillSummary>[] = [
    {
      title: "Skill",
      dataIndex: "display_name",
      render: (_: unknown, skill: SkillSummary) => (
        <Link className="resource-cell" to={`/skills/${skill.id}`}>
          <SkillLogo />
          <span>
            <strong>{skill.display_name}</strong>
            {skill.display_name !== skill.slug && <code className="skill-list-slug">{skill.slug}</code>}
            <small>{skill.description}</small>
          </span>
        </Link>
      ),
    },
    {
      title: "上传人",
      dataIndex: "uploader_name",
      width: 116,
      render: (value: string | null) => <span className="skill-uploader">{value || "未知"}</span>,
    },
    {
      title: "状态",
      dataIndex: "status",
      width: 120,
      render: (value: SkillSummary["status"]) => <StatusBadge status={value} />,
    },
    {
      title: "最新版本",
      dataIndex: "latest_published_version",
      width: 150,
      render: (value: string | null) => (
        <Tag color={value ? "blue" : "grey"}>{value ?? "仅草稿"}</Tag>
      ),
    },
    {
      title: "更新时间",
      dataIndex: "updated_at",
      width: 160,
      render: (value: string) => (
        <time dateTime={value}>{formatDate(value)}</time>
      ),
    },
    {
      title: "",
      dataIndex: "id",
      width: 106,
      render: (_: unknown, skill: SkillSummary) => (
        <AssetDetailsLink
          to={`/skills/${skill.id}`}
          name={skill.display_name}
        />
      ),
    },
  ];

  return (
    <section className="asset-page asset-mobile-list page-enter">
      <header className="page-header">
        <div>
          <Typography.Title heading={1}>Skills</Typography.Title>
          <Typography.Paragraph>
            上传、校验、发布并分发团队可复用的能力包。
          </Typography.Paragraph>
        </div>
        {canWrite ? (
          <Button
            aria-label="上传 Skill"
            icon={<IconPlus />}
            onClick={() => setUploadOpen(true)}
            size="large"
            theme="solid"
            type="primary"
          >
            上传 Skill
          </Button>
        ) : null}
      </header>

      <section className="asset-surface">
        <div className="asset-toolbar">
          <Select
            aria-label="筛选状态"
            onChange={(value) => setStatus(String(value ?? ""))}
            optionList={[
              { label: "全部状态", value: "" },
              { label: "活跃", value: "active" },
              { label: "已归档", value: "archived" },
            ]}
            value={status}
          />
          <form className="asset-search" onSubmit={submitSearch} role="search">
            <Input
              ref={searchRef}
              aria-label="搜索 Skills"
              onChange={setSearchInput}
              placeholder="搜索 Skill 名称或描述"
              prefix={<IconSearch />}
              showClear
              value={searchInput}
            />
            <Button htmlType="submit">搜索</Button>
          </form>
        </div>

        {skillsQuery.isError ? (
          <AssetLoadError
            description={apiErrorMessage(skillsQuery.error)}
            title="Skills 暂时无法加载"
            onRetry={() => void skillsQuery.refetch()}
          />
        ) : null}

        {!skillsQuery.isPending &&
        !skillsQuery.isError &&
        items.length === 0 ? (
          <div className="asset-empty">
            <Empty
              description={
                hasFilters
                  ? "调整关键词或状态筛选后再试。"
                  : "上传完整目录包，把团队经验变成可版本化资产。"
              }
              image={<IconPuzzle size={32} />}
              title={hasFilters ? "没有匹配的 Skill" : "还没有团队 Skill"}
            />
            {hasFilters && (
              <Button
                onClick={() => {
                  setSearchInput("");
                  setSearch("");
                  setStatus("");
                  searchRef.current?.focus();
                }}
              >
                清除筛选
              </Button>
            )}
            {canWrite && !hasFilters ? (
              <Button
                aria-label="上传第一个 Skill"
                onClick={() => setUploadOpen(true)}
                theme="light"
                type="primary"
              >
                上传第一个 Skill
              </Button>
            ) : null}
          </div>
        ) : null}

        {skillsQuery.isPending || items.length > 0 ? (
          <Table
            className="asset-table asset-stack-table"
            variant="secondary"
            columns={columns}
            dataSource={items}
            loading={skillsQuery.isPending}
            pagination={false}
            rowKey="id"
          />
        ) : null}
      </section>

      <SkillPackageDialog
        onClose={() => setUploadOpen(false)}
        onSubmit={async (file) => {
          await createMutation.mutateAsync(file);
        }}
        open={uploadOpen}
        submitLabel="校验并创建"
        title="上传 Skill 包"
      />
    </section>
  );
}

export function StatusBadge({ status }: { status: "active" | "archived" }) {
  return (
    <Tag color={status === "active" ? "green" : "grey"}>
      {status === "active" ? "活跃" : "已归档"}
    </Tag>
  );
}

export function SkillLogo({ large = false }: { large?: boolean }) {
  return (
    <span
      className={large ? "resource-mark skill large" : "resource-mark skill"}
      aria-hidden="true"
    >
      <IconPuzzle />
    </span>
  );
}

function apiErrorMessage(error: unknown): string {
  return error instanceof SkillsApiError
    ? error.message
    : "请检查网络连接后重试。";
}

export function formatDate(value: string): string {
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}
