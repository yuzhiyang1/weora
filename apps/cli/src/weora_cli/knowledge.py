"""通过登录设备访问项目知识，离线贡献使用原请求 ID 重试。"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import TypedDict
from uuid import UUID, uuid4

from ruamel.yaml.error import YAMLError
from weora_code_graph.design import GRAPH_TOPIC_DOCUMENTS, assign_code_domain

from weora_cli import knowledge_promotion
from weora_cli.collab_client import Client, CollabError, as_object, config_path, lock_file, paired_client, write_json
from weora_cli.collab_client import items as response_items
from weora_cli.commands import JsonValue
from weora_cli.knowledge_activity import configure as configure_activity
from weora_cli.knowledge_activity import drain as drain_activity
from weora_cli.knowledge_activity import publish as publish_activity
from weora_cli.knowledge_adoption import adoption as score_adoption
from weora_cli.knowledge_api import call as _call
from weora_cli.knowledge_api import lifecycle_query, refresh_review_indexes
from weora_cli.knowledge_benchmark import DEFAULT_SUITE
from weora_cli.knowledge_benchmark import execute as execute_benchmark
from weora_cli.knowledge_feedback import has_pending_feedback, pending_for_recall, submit_feedback, sync_feedback
from weora_cli.knowledge_finished_sessions import configure as configure_archive
from weora_cli.knowledge_finished_sessions import drain as drain_finished_session
from weora_cli.knowledge_finished_sessions import finished as list_finished_sessions
from weora_cli.knowledge_import import review_markdown, select_imports
from weora_cli.knowledge_import_enrichment import ImportDesignDriver, finish_import_design, select_import_design_driver
from weora_cli.knowledge_improve_run import execute as execute_improvement
from weora_cli.knowledge_improvement import plan as plan_improvement
from weora_cli.knowledge_improvement_ci import execute as execute_improvement_ci
from weora_cli.knowledge_improvement_delivery import execute as execute_improvement_delivery
from weora_cli.knowledge_improvement_schedule import execute as execute_improvement_schedule
from weora_cli.knowledge_local_code import local_snapshot
from weora_cli.knowledge_maintenance import maintain
from weora_cli.knowledge_markdown import markdown_date, markdown_domain, parse_metadata, rule_paths
from weora_cli.knowledge_mr_hint import configure as configure_mr_hint
from weora_cli.knowledge_named_scoring import GENERATED_NAME_PREFIXES, score_named
from weora_cli.knowledge_org import list_org
from weora_cli.knowledge_overlap import LOOKBACK, MIN_OVERLAP, keywords, overlap
from weora_cli.knowledge_plan_reminder import configure as configure_plan_reminder
from weora_cli.knowledge_quality import generate as generate_quality
from weora_cli.knowledge_quality import submit as submit_quality
from weora_cli.knowledge_queue_view import queue_matches, queue_view
from weora_cli.knowledge_recall_quality import record_quality
from weora_cli.knowledge_repo_import import (
    RepositoryImportSource,
    import_repositories,
    is_repo_batch,
    pattern,
    positive_option,
    preview_repositories,
    read_repo_list,
    repository_domains,
    repository_plan,
    repository_reference,
    write_whitelist,
)
from weora_cli.knowledge_rule_import import is_local_import, review_local_iwiki_section, run_import
from weora_cli.knowledge_run_mode import configured_mode, task_identifier
from weora_cli.knowledge_score_offer import score_offer
from weora_cli.knowledge_score_work import configure as configure_score_work
from weora_cli.knowledge_score_work import drain as drain_score_work
from weora_cli.knowledge_score_work import stage_drained_session
from weora_cli.knowledge_score_work import start_if_bound as start_score_work
from weora_cli.knowledge_session_scoring import configure as configure_session_scorers
from weora_cli.knowledge_session_scoring import score as score_session
from weora_cli.knowledge_session_share import share_learnings
from weora_cli.knowledge_settings import SessionSettings
from weora_cli.knowledge_transcript import share_session
from weora_cli.knowledge_unattended import drain as drain_unattended
from weora_cli.knowledge_workflow import WORKFLOW_NAMES
from weora_cli.knowledge_workflow import arguments as workflow_arguments
from weora_cli.knowledge_workflow import configure as configure_knowledge_workflow
from weora_cli.knowledge_workflow import execute as execute_knowledge_workflow
from weora_cli.knowledge_workflow_activity import execute as publish_workflow_activity


def _uuid(value: str) -> str:
    try:
        return str(UUID(value))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("请输入有效的 UUID。") from exc


def _entry_or_path(value: str) -> str:
    if "/" in value or "\\" in value:
        path = value.replace("\\", "/")
        if path.startswith("/") or ":" in path or not path.isprintable() or len(path) > 1000:
            raise argparse.ArgumentTypeError("请输入团队内的相对资源路径。")
        return path
    return _uuid(value)


def arguments(parser: argparse.ArgumentParser) -> None:
    actions = parser.add_subparsers(dest="action", required=True)
    search = actions.add_parser("search", aliases=["recall"], help="检索团队知识")
    search.add_argument("query")
    search.add_argument("--check", action="store_true")
    search.add_argument("--depth", choices=("route", "context", "lookup"), help="代码召回深度，召回默认 context")
    search.add_argument("--session")
    search.add_argument("--recall-actor", type=_uuid, help="专用召回子 Agent 的归因标识")
    search.add_argument("--automatic", action="store_true", help="按会话策略记录自动召回")
    search.add_argument("--limit", type=int)
    recall_scope = actions.add_parser("recall-scope", help="设置项目是否继承工作空间共享知识")
    recall_scope.add_argument("mode", choices=("enable", "disable", "status"))
    recall_scope.add_argument("--project", required=True, type=_uuid, help="Weora 项目 ID")
    recall_stats = actions.add_parser("recall-stats", help="查看本人根会话的召回、采纳及关系使用")
    recall_stats.add_argument("--session", help="只查看指定根会话")
    recall_stats.add_argument("--limit", type=int, default=10)
    recall_stats.add_argument("--recent", action="store_true", help="只查看最近 24 小时的召回")
    detail = actions.add_parser("get", help="读取知识正文")
    detail.add_argument("entry", type=_entry_or_path, help="知识条目 UUID 或团队资源路径")
    detail.add_argument("--session")
    detail.add_argument("--actor", type=_uuid, help="实际打开知识的子 Agent 归因标识")
    detail.add_argument("--automatic", action="store_true", help="按会话策略记录自动采纳")
    for action in (search, detail):
        selection = action.add_mutually_exclusive_group()
        selection.add_argument("--project", type=_uuid, help="显式使用指定的 Weora 项目")
        selection.add_argument("--shared", action="store_true", help="显式使用工作空间共享知识")
    gaps = actions.add_parser("gaps", help="查看当前项目已发布的代码知识缺口")
    gaps.add_argument("query", nargs="?", default="", help="按缺口主题、源码路径或标识符筛选")
    gaps.add_argument("--project", required=True, type=_uuid)
    graph = actions.add_parser("graph", help="按主题直接打开已发布的项目图谱文档")
    graph.add_argument("topic", choices=tuple(GRAPH_TOPIC_DOCUMENTS), help="g1 依赖，g2 调用链，g5 场景，g6 多跳关系")
    graph.add_argument("--project", type=_uuid, required=True, help="Weora 项目 ID")
    graph.add_argument("--codebase", type=_uuid, help="项目中有多个图谱时选择代码索引 ID")
    graph.add_argument("--session")
    graph.add_argument("--actor", type=_uuid, help="实际打开知识的子 Agent 归因标识")
    graph.add_argument("--automatic", action="store_true", help="按会话策略记录自动采纳")
    contribute = actions.add_parser("contribute", help="贡献 Markdown 经验或提交审核")
    contribute.add_argument("--file", required=True, type=Path)
    contribute.add_argument("--title")
    contribute.add_argument("--kind", choices=("learning", "rule", "doc", "code", "skill"), default="learning")
    contribute.add_argument(
        "--source", choices=("manual", "session", "run", "pull_request", "file", "wiki", "repository"),
    )
    contribute.add_argument("--source-ref", default="")
    location = contribute.add_mutually_exclusive_group()
    location.add_argument("--resource-path", help="用于项目覆盖和引用的团队相对路径")
    location.add_argument("--namespace", help="发布到已启用的经验目录，传空值使用共享根目录")
    contribute.add_argument("--maps-to", type=_uuid, action="append", help="提交待评审的代码证据关系，可重复指定")
    contribute.add_argument("--promoted-from", type=_uuid)
    contribute.add_argument("--entry", type=_uuid)
    contribute.add_argument("--base-revision", type=int)
    contribute.add_argument("--key", type=_uuid)
    contribute.add_argument("--dry-run", action="store_true", help="预览贡献目标及审核状态")
    sync = actions.add_parser("sync", help="重试本机待发送的贡献和反馈")
    sync.add_argument("--automatic", action="store_true", help="仅在当前目录开启召回时补发同范围的反馈")
    feedback = actions.add_parser("feedback", help="记录一个根会话的采纳或负反馈")
    feedback.add_argument("entry", type=_uuid)
    feedback.add_argument("--session", required=True)
    feedback.add_argument("--dry-run", action="store_true", help="预览反馈，跳过保存和提交")
    direction = feedback.add_mutually_exclusive_group(required=True)
    direction.add_argument("--positive", action="store_true")
    direction.add_argument("--negative", action="store_true")
    adoption = actions.add_parser("adoption", help="预览或提交未打开知识的采纳评分")
    adoption.add_argument("mode", choices=("status", "enable", "disable", "plan", "score", "write"))
    adoption.add_argument("--session", help="实际召回的根会话标识")
    adoption.add_argument("--limit", type=int, default=20)
    adoption.add_argument("--after", type=_uuid, help="从预览返回的游标继续")
    adoption.add_argument("--all-recalled", action="store_true", help="规划时也列出已打开或已判定的召回条目")
    adoption.add_argument("--driver", choices=("codex", "claude"), help="覆盖评分标准偏好的客户端")
    scoring_evidence = adoption.add_mutually_exclusive_group()
    scoring_evidence.add_argument("--evidence", type=Path, help="本次允许交给评分 Agent 的本地会话材料")
    scoring_evidence.add_argument("--archive", type=_uuid, help="使用本人同一根会话已发布的归档")
    adoption.add_argument("--archive-revision", type=int, help="固定评分所使用的归档版本")
    adoption.add_argument("--verdicts", type=Path, help="已核对的采纳判定 JSON")
    adoption.add_argument("--apply", action="store_true", help="评分完成后提交判定")
    pending = actions.add_parser("pending", help="查看待审核版本")
    health = actions.add_parser("health", help="查看知识库覆盖率、维护候选及代码来源状态")
    health.add_argument("--limit", type=int, default=20, help="各明细区块的最大条数")
    activity = actions.add_parser("activity-settings", help="设置本机目录或工作空间的活动上报")
    activity.add_argument("mode", choices=("enable", "disable", "inherit", "status"))
    usage_scope = activity.add_mutually_exclusive_group()
    usage_scope.add_argument("--workspace", action="store_true")
    usage_scope.add_argument("--project", type=_uuid)
    usage_scope.add_argument("--shared", action="store_true")
    actions.add_parser("publish-activity", help="补发当前目录已记录的会话计数")
    workflow_report = actions.add_parser("publish-workflow", help="回读并上报已完成的 GitHub 工作流尝试")
    workflow_source = workflow_report.add_mutually_exclusive_group(required=True)
    workflow_source.add_argument("--event", type=Path, help="GitHub workflow_run 完成事件")
    workflow_source.add_argument("--run", help="需补发的 GitHub 运行编号")
    workflow_report.add_argument("--repository", help="预期 GitHub 仓库 owner/repo")
    workflow_report.add_argument("--attempt", type=int, help="与 --run 同时提供")
    digest = actions.add_parser("digest", help="查看团队活动及知识发布周报")
    digest.add_argument("--failures", "--json", action="store_true", help="查看最近 30 天的评分失败及同组趋势")
    digest.add_argument("--limit", type=int, default=50, help="评分失败明细的最大条数")
    digest.add_argument("--scorer", help="根据指定评分标准和本地检出筛选回归候选，需指定项目")
    digest.add_argument("--suite", help="回归套件路径，默认 benchmarks/regressions")
    benchmark = actions.add_parser("benchmark", help="维护项目回归套件和改进提案")
    benchmark_actions = benchmark.add_subparsers(dest="benchmark_action", required=True)
    suite_add = benchmark_actions.add_parser("suite-add", help="登记套件 YAML")
    suite_add.add_argument("--suite", help="套件路径，默认从 YAML 名称推导")
    suite_add.add_argument("--file", type=Path, required=True)
    task_add = benchmark_actions.add_parser("task-add", help="将评分失败登记为回归任务")
    task_add.add_argument("--suite", default=DEFAULT_SUITE)
    task_add.add_argument("--scorer", required=True)
    task_add.add_argument("--label", type=int, required=True)
    task_add.add_argument("--file", type=Path, required=True)
    task_add.add_argument("--publish", action="store_true", help="直接加入默认套件")
    task_add.add_argument("--key", type=_uuid, help="显式指定可重试的操作 ID")
    tasks = benchmark_actions.add_parser("tasks", help="读取默认套件或指定提案的任务")
    tasks.add_argument("--suite", default=DEFAULT_SUITE)
    tasks.add_argument("--proposal", type=_uuid)
    proposal = benchmark_actions.add_parser("proposal-settle", help="发布或放弃平台提案中的任务")
    proposal.add_argument("--suite", default=DEFAULT_SUITE)
    proposal.add_argument("--proposal", type=_uuid, required=True)
    proposal.add_argument("--status", choices=("merged", "closed"), required=True)
    run_plan = benchmark_actions.add_parser("run-plan", help="固定基准任务和两组资源版本")
    run_benchmark = benchmark_actions.add_parser("run", help="固定基准矩阵并立即执行两侧试验")
    for command in (run_plan, run_benchmark):
        command.add_argument("--suite", default=DEFAULT_SUITE)
        command.add_argument("--proposal", type=_uuid)
        command.add_argument("--resources", type=Path, required=True)
        command.add_argument("--base-ref", required=True)
        command.add_argument("--proposal-ref", required=True)
        command.add_argument("--task", action="append", default=[], help="只纳入指定任务，可重复使用")
        command.add_argument("--key", type=_uuid, required=True, help="同一次运行重试时使用原操作 ID")
    run_results = benchmark_actions.add_parser("run-record", help="登记运行的评判结果")
    run_results.add_argument("--run", type=_uuid, required=True)
    run_results.add_argument("--file", type=Path, required=True)
    run_inputs = benchmark_actions.add_parser("run-inputs", help="读取执行器所需的任务及评判标准")
    run_inputs.add_argument("--run", type=_uuid, required=True)
    run_report = benchmark_actions.add_parser("run-report", help="查看基准进度及两组比较")
    run_report.add_argument("--run", type=_uuid, required=True)
    run_execute = benchmark_actions.add_parser("run-execute", help="在两个独立的 OCI 沙箱执行并评判试验")
    run_execute.add_argument("--run", type=_uuid, required=True)
    run_execute.add_argument("--resources", type=Path, required=True)
    for command in (run_benchmark, run_execute):
        command.add_argument("--checkout", type=Path, required=True)
        command.add_argument("--image", required=True, help="本机已有的 Linux 镜像 ID 或仓库摘要")
        command.add_argument("--network", help="模型网关使用的 Docker 内部网络")
        command.add_argument("--gateway", help="内部模型网关的 HTTP 地址")
        command.add_argument("--judge-tool", choices=("codex", "claude"), default="codex")
        command.add_argument("--judge-model", required=True)
    for command in (run_benchmark, run_execute, run_report):
        command.add_argument("--fail-on-regression", action="store_true", help="检测到回退时进程返回失败")
    improvement = actions.add_parser("self-improvement", help="从重复评分失败生成改进提案")
    improvement_actions = improvement.add_subparsers(dest="improvement_action", required=True)
    improve_run = improvement_actions.add_parser("run", help="生成资源修改并登记每项失败的回归任务")
    improve_run.add_argument("scorer")
    improve_run.add_argument("--suite", default=DEFAULT_SUITE)
    improve_run.add_argument("--checkout", type=Path, required=True)
    improve_run.add_argument("--resources", type=Path, required=True)
    improve_run.add_argument("--base-ref", required=True)
    improve_run.add_argument("--image", required=True)
    improve_run.add_argument("--network")
    improve_run.add_argument("--gateway")
    improve_run.add_argument("--via", choices=("codex", "claude"), default="claude")
    improve_run.add_argument("--model", required=True)
    improve_run.add_argument("--timeout", type=int, default=900)
    improve_run.add_argument("--key", type=_uuid, required=True, help="同一次改进重试时使用原操作 ID")
    improve_action = improve_run.add_mutually_exclusive_group()
    improve_action.add_argument("--preview", action="store_true", help="查看生成草案，暂不登记")
    improve_action.add_argument("--publish", action="store_true", help="生成并交付本操作的 GitHub PR 修订")
    improve_schedule = improvement_actions.add_parser("scheduled", help="由 GitHub Actions 定期生成并交付改进")
    improve_schedule.add_argument("scorer")
    improve_schedule.add_argument("--suite", default=DEFAULT_SUITE)
    improve_schedule.add_argument("--checkout", type=Path, required=True)
    improve_schedule.add_argument("--repository", required=True)
    improve_schedule.add_argument("--base-branch", required=True)
    improve_schedule.add_argument("--workflow-run", help="默认使用 GITHUB_RUN_ID")
    improve_schedule.add_argument("--image", required=True)
    improve_schedule.add_argument("--network")
    improve_schedule.add_argument("--gateway")
    improve_schedule.add_argument("--via", choices=("codex", "claude"), default="claude")
    improve_schedule.add_argument("--model", required=True)
    improve_schedule.add_argument("--timeout", type=int, default=900)
    improve_schedule.set_defaults(preview=False)
    improve_publish = improvement_actions.add_parser("publish", help="创建或更新已登记提案的 GitHub PR")
    improve_publish.add_argument("--resources", type=Path, required=True, help="本地资源仓库检出目录")
    improve_confirm = improvement_actions.add_parser("confirm", help="核对 PR 已合并和基准门禁后发布任务")
    improve_confirm.add_argument("--benchmark", required=True, type=_uuid)
    for command in (improve_publish, improve_confirm):
        command.add_argument("--suite", default=DEFAULT_SUITE)
        command.add_argument("--proposal", required=True, type=_uuid)
    improve_ci = improvement_actions.add_parser("ci", help="从 GitHub PR 事件运行基准或确认合并")
    improve_ci.add_argument("--event", required=True, type=Path)
    improve_ci.add_argument("--workflow-run")
    improve_ci.add_argument("--attempt")
    improve_ci.add_argument("--checkout", type=Path)
    improve_ci.add_argument("--image")
    improve_ci.add_argument("--network")
    improve_ci.add_argument("--gateway")
    improve_ci.add_argument("--judge-tool", choices=("codex", "claude"), default="codex")
    improve_ci.add_argument("--judge-model")
    improve_ci.set_defaults(require_ci_success=True)
    review = actions.add_parser("review", help="通过或驳回指定版本")
    review.add_argument("entry", type=_uuid)
    review.add_argument("decision", choices=("accept", "reject", "preview"))
    review.add_argument("--revision", type=int)
    review.add_argument("--iwiki-section", choices=("business-api", "external-knowledge", "glossary"),
                        help="审核离线导入中的指定汇总章节，此时条目参数是导入运行 ID")
    review.add_argument("--reason", default="")
    maintenance = actions.add_parser("maintenance", help="预览或归档低置信度经验")
    maintenance_action = maintenance.add_mutually_exclusive_group()
    maintenance_action.add_argument("--prune", action="store_true", help="归档低置信度经验")
    maintenance_action.add_argument("--confidence-writeback", action="store_true", help="更新发布版本的置信度快照")
    maintenance.add_argument("--archive", action="store_true", help="归档并保留版本以供恢复")
    maintenance.add_argument("--threshold", type=float, help="低置信度归档阈值，默认 0.15")
    maintenance_action.add_argument("--quality-candidates", action="store_true")
    maintenance_action.add_argument("--update-quality", action="store_true", help="为低采纳条目生成修订草稿")
    maintenance_action.add_argument("--submit-quality", type=Path, help="提交已核对的质量草稿")
    maintenance.add_argument("--after", type=_uuid)
    maintenance.add_argument("--dry-run", action="store_true", help="预览候选，跳过写入")
    maintenance.add_argument("--via", choices=("claude", "codex"), help="生成草稿的 Agent，默认 Claude Code")
    maintenance.add_argument("--output", type=Path, help="质量草稿目录")
    promote = actions.add_parser("promote", help="列出、预演或提交经验晋升")
    knowledge_promotion.arguments(promote)
    for command in (maintenance, promote, health, digest):
        command.add_argument("--project-only", action="store_true", help="只处理指定项目的独立知识范围")
    restore = actions.add_parser("restore", help="恢复已归档的发布版本")
    restore.add_argument("entry", type=_uuid)
    imports = actions.add_parser("import", help="导入本机规则、iWiki、PR/MR、仓库或来源清单")
    sources = imports.add_mutually_exclusive_group(required=True)
    sources.add_argument("--manifest", type=Path, help="已分类的来源文件清单")
    sources.add_argument("--from-tools", "--from-claude", action="store_true", help="发现并分类本机工具的用户规则")
    sources.add_argument("--from-iwiki", help="iWiki 数字空间 ID 或页面链接")
    imports.add_argument("--iwiki-dual", action="store_true", help="同时归纳业务接口、外部知识源和术语表")
    imports.add_argument("--require-review", action="store_true", help="将 iWiki 汇总章节送审")
    sources.add_argument("--from-mr", nargs="+", metavar="URL", help="GitHub PR 链接")
    sources.add_argument("--from-repo", metavar="仓库", help="按登记仓库 ID 或地址建立代码索引")
    sources.add_argument("--dir", type=Path, metavar="目录", help="从当前设备的目录建立项目代码索引")
    sources.add_argument("--from-repo-list", type=Path, metavar="YAML", help="按 YAML 清单批量索引项目仓库")
    sources.add_argument("--from-org", metavar="组织", help="列出托管平台组织仓库并建立导入清单")
    sources.add_argument("--resume", type=_uuid, help="续做中断的来源导入")
    imports.add_argument("--via", choices=("claude", "codex"), help="选择分类或代码设计生成的客户端")
    imports.add_argument("--skip-enrich", action="store_true", help="代码导入跳过 AI 生成")
    imports.add_argument("--domain", help="为 --from-repo 指定仓库业务领域")
    imports.add_argument("--output", type=Path, help="输出本地 Markdown 或离线代码知识库")
    imports.add_argument("--ssh", action="store_true", help="离线仓库读取使用 SSH")
    imports.add_argument("--all", action="store_true", help="确认接受所有团队条目")
    imports.add_argument("--no-discussions", action="store_true", help="PR/MR 导入时跳过评审评论")
    imports.add_argument("--language", choices=("zh", "en"), help="PR/MR 经验语言，默认中文")
    imports.add_argument("--dry-run", action="store_true", help="预览 iWiki、PR/MR 草稿或仓库计划")
    imports.add_argument("--incremental", action="store_true", help="仓库导入时复用未变化文件的提取结果")
    imports.add_argument("--max-files", type=int, help="仓库导入的源码文件上限，默认 200")
    imports.add_argument("--concurrency", type=int, help="仓库批量导入的并发数，默认 3")
    imports.add_argument("--skip-import", action="store_true", help="组织导入只写出仓库清单")
    imports.add_argument("--max-repos", type=int, help="组织列表拉取上限，默认 200")
    imports.add_argument("--include-pattern", help="按仓库全名匹配组织纳入正则")
    imports.add_argument("--exclude-pattern", help="按仓库全名匹配组织排除正则")
    archives = imports.add_mutually_exclusive_group()
    archives.add_argument("--include-archived", action="store_true", help="将托管平台归档仓库纳入清单")
    archives.add_argument("--exclude-archived", action="store_true", help="排除托管平台归档仓库（默认）")
    codebase = actions.add_parser("codebase", help="查询项目或离线代码索引状态及完整性")
    codebase.add_argument("--output", type=Path, help="与 import --output 相同的离线知识库目录")
    codebase.add_argument("--project", help="平台项目 UUID 或离线导入返回的代码库名称")
    source = codebase.add_mutually_exclusive_group()
    source.add_argument("--repository", type=_uuid, help="已登记的仓库 ID")
    source.add_argument("--local", type=_uuid, help="本地目录导入返回的代码索引 ID")
    code_action = codebase.add_mutually_exclusive_group()
    code_action.add_argument("--status", action="store_true", help="查看仓库索引和源提交")
    code_action.add_argument("--lint", action="store_true", help="检查缺失文档、文件清单和采集缺口")
    code_action.add_argument("--deep-enrich", action="store_true", help="生成组件设计、架构总览和场景序列图")
    codebase.add_argument("--via", choices=("claude", "codex"), help="深度生成所用的原生客户端")
    codebase.add_argument("--max-modules", type=int, help="组件设计的数量上限，0 表示全部")
    codebase.add_argument("--concurrency", type=int, help="并行生成的组件数，默认 5")
    code_action.add_argument("--reconcile", action="store_true", help="调和产品文档与代码证据")
    codebase.add_argument("--dry-run", action="store_true", help="预览深度生成计划或调和报告")
    namespaces = actions.add_parser("namespace-settings", help="查看当前目录选择或保存工作空间目录清单")
    namespaces.add_argument("mode", choices=("status", "apply"))
    namespaces.add_argument("--file", type=Path, help="包含基准版本的 JSON 目录清单")
    settings = actions.add_parser("recall-settings", help="配置这个目录的会话召回")
    settings.add_argument("mode", choices=("enable", "disable", "inherit", "status"))
    settings.add_argument("--workspace", action="store_true", help="查看或修改工作空间默认值")
    share_settings = actions.add_parser("share-reminder", help="配置当前目录的会话经验提醒")
    share_settings.add_argument("mode", choices=("enable", "disable", "inherit", "status"))
    share_settings.add_argument("--workspace", action="store_true", help="查看或修改工作空间默认值")
    workflow = actions.add_parser("workflow-definition", help="查看或管理团队知识工作流")
    workflow.add_argument("mode", choices=("status", "apply", "reset"))
    workflow.add_argument("name", choices=WORKFLOW_NAMES)
    workflow.add_argument("--revision", type=int, help="查看定义时返回的基准版本")
    workflow.add_argument("--file", type=Path, help="团队工作流覆盖 YAML")
    workflow_arguments(actions.add_parser("workflow", help="启动、恢复和审批知识工作流"), _uuid)
    share = actions.add_parser("share-learnings", help="预览并确认发布会话经验草稿")
    share.add_argument("--runtime", required=True, choices=("codex", "claude-code"))
    share.add_argument("--session", required=True, type=_uuid)
    share.add_argument(
        "--file", required=True, action="append", type=Path, help="每份经验一个 Markdown 文件，可重复指定",
    )
    share.add_argument("--confirm", metavar="确认码", help="使用预览返回的确认码发布已核对的经验")
    transcript = actions.add_parser("share-session", help="预览脱敏后的原生会话或确认分享")
    transcript.add_argument("--runtime", required=True, choices=("codex", "claude-code"))
    transcript.add_argument("--session", required=True, type=_uuid)
    transcript.add_argument("--transcript", type=Path, help="存在多个本机转录时指定文件")
    transcript.add_argument("--title", help="归档标题，默认取首条用户消息")
    transcript.add_argument("--confirm", metavar="确认码", help="发布先前核对的脱敏快照")
    archives_list = actions.add_parser("list-shared-sessions", help="列出此范围内的会话归档")
    archives_list.add_argument("--limit", type=int, default=20)
    archive_detail = actions.add_parser("get-shared-session", help="读取已分享会话")
    archive_detail.add_argument("archive", type=_uuid)
    archive_detail.add_argument("--revision", type=int)
    scorers = actions.add_parser("session-scorers", help="列出或发布团队会话评分标准")
    scorers.add_argument("mode", choices=("list", "publish", "reset-adoption", "reset-routing"))
    scorers.add_argument("--file", type=Path, help="评分 Markdown 或含 SCORER.md 的目录")
    scorers.add_argument("--base-revision", type=int, help="核对更新所基于的版本，新建时为 0")
    scorers.add_argument("--namespace", help="评分器所在的团队目录，显式空值表示根目录")
    session_score = actions.add_parser("score", help="为本机会话或指定归档评分")
    subject = session_score.add_mutually_exclusive_group()
    subject.add_argument("--archive", type=_uuid)
    subject.add_argument("--session", type=_uuid, help="在本机脱敏并评分，只提交标签")
    session_score.add_argument("--revision", type=int)
    session_score.add_argument("--runtime", choices=("codex", "claude-code"))
    session_score.add_argument("--transcript", type=Path, help="存在多份本机转录时指定原生文件")
    rubric = session_score.add_mutually_exclusive_group(required=True)
    rubric.add_argument("--scorer", action="append", default=[], help="已发布的评分标准名称，可多次指定")
    rubric.add_argument("--file", type=Path, help="试跑未发布的评分 Markdown 或目录")
    session_score.add_argument("--label", help="使用人工判定的标签")
    session_score.add_argument("--driver", choices=("codex", "claude"), help="评分宿主，默认 Codex")
    session_score.add_argument("--dry-run", action="store_true", help="查看判定结果，不写入标签")
    session_score.add_argument("--batch", action="store_true", help="同客户端和模型的标准共用一次调用")
    session_score.add_argument("--operation-id", type=_uuid, help="重试时继续使用同一个请求键")
    offer = actions.add_parser("score-offer", help="预览建议的评分标准，确认后才评分或跳过")
    offered = offer.add_mutually_exclusive_group()
    offered.add_argument("--archive", type=_uuid)
    offered.add_argument("--session", type=_uuid)
    offered.add_argument("--confirm", help="使用预览码执行评分")
    offered.add_argument("--skip", help="跳过这份预览")
    offer.add_argument("--revision", type=int)
    offer.add_argument("--runtime", choices=("codex", "claude-code"))
    offer.add_argument("--transcript", type=Path)
    offer.add_argument("--scorer", action="append", default=[], help="自行选择评分标准，可重复指定")
    offer.add_argument("--run-mode", choices=("attended", "unattended"))
    offer.add_argument("--batch", action="store_true", help="预览并确认合并调用")
    labels = actions.add_parser("session-labels", help="查看已保存的会话评分标签")
    labels.add_argument("archive", type=_uuid)
    labels.add_argument("--revision", required=True, type=int)
    labels.add_argument("--limit", type=int, default=50)
    labels.add_argument("--history", action="store_true", help="显示此前保存的标签")
    receipt = actions.add_parser("score-receipt", help="按调用 ID 查看本人的点名评分收据")
    receipt.add_argument("call_id", type=_uuid)
    local_scores = actions.add_parser("list-local-scores", help="列出已向团队提交标签的本机会话")
    local_scores.add_argument("--limit", type=int, default=50)
    score_settings = actions.add_parser("scoring-settings", help="管理工作空间、评分目录和本机的会话评分设置")
    score_settings.add_argument("decision", choices=("off", "ask", "auto", "inherit", "status", "apply"))
    score_settings.add_argument("--run-mode", choices=("attended", "unattended"), default="attended")
    score_settings.add_argument("--workspace", action="store_true", help="查看或修改工作空间评分策略")
    score_settings.add_argument("--daily-limit", type=int, help="工作空间同一成员每天的评分次数限额")
    score_settings.add_argument("--batch", action=argparse.BooleanOptionalAction, help="启用或关闭团队合并评分")
    score_settings.add_argument("--isolation", choices=("best-effort", "verified"), help="工作空间评分的最低隔离级别")
    score_settings.add_argument(
        "--skill-routing", action=argparse.BooleanOptionalAction, help="启用工作空间 Skill 路由",
    )
    score_settings.add_argument("--namespace", help="查看或导入 scorers 下的评分目录")
    score_settings.add_argument("--file", type=Path, help="导入该目录的 scoring.yaml")
    score_settings.add_argument("--base-revision", type=int, help="目录当前版本，首次导入为 0")
    score_drain = actions.add_parser("score-drain", help="处理当前范围内已结束会话的后台评分")
    score_drain.add_argument("--run-mode", choices=("attended", "unattended"), default=None)
    score_drain.add_argument("--limit", type=int, default=10)
    score_drain.add_argument("--bound", action="store_true", help=argparse.SUPPRESS)
    archive_settings = actions.add_parser("archive-settings", help="配置目录或工作空间的会话归档策略")
    archive_settings.add_argument("mode", choices=("off", "ask", "auto", "inherit", "status"))
    archive_settings.add_argument("--run-mode", choices=("attended", "unattended"), default="attended")
    archive_settings.add_argument("--workspace", action="store_true", help="查看或修改工作空间策略")
    archive_settings.add_argument("--project", type=_uuid, help="将本目录的归档绑定到项目")
    finished = actions.add_parser("finished-sessions", help="预览本机已结束的会话或按策略归档")
    finished.add_argument("--limit", type=int, default=20)
    finished.add_argument("--apply", action="store_true", help="发布符合自动归档策略的候选")
    finished.add_argument("--drain", type=_uuid, metavar="无人值守会话ID", help="启动器在原生会话退出后调用")
    finished.add_argument("--runtime", choices=("codex", "claude-code"))
    finished.add_argument("--transcript", type=Path, help="处理无人值守会话时指定原生转录文件")
    plan_reminder = actions.add_parser("plan-reminder", help="查看或维护工作空间计划后召回提醒")
    plan_reminder.add_argument("mode", choices=("enable", "disable", "status", "configure"))
    plan_reminder.add_argument("--language", choices=("zh", "en", "default"))
    plan_text = plan_reminder.add_mutually_exclusive_group()
    plan_text.add_argument("--message-file", type=Path, help="设置所有语言共享的静态 Markdown 文案")
    plan_text.add_argument("--default-message", action="store_true", help="恢复内置提醒文案")
    hint = actions.add_parser("mr-hint", help="配置近期合并提醒")
    hint.add_argument("mode", choices=("enable", "disable", "inherit", "status"))
    hint.add_argument("--workspace", action="store_true", help="查看或修改工作空间默认值")
    hint_scope = hint.add_mutually_exclusive_group()
    hint_scope.add_argument("--project", type=_uuid, help="在这个目录关联项目知识")
    hint_scope.add_argument("--shared", action="store_true", help="在这个目录使用工作空间共享知识")
    hint.add_argument("--lookback-days", type=int, help="工作空间提醒窗口的天数")
    hint.add_argument("--max-mrs", type=int, help="单次提示包含的最多 PR/MR 数")
    scoped_actions = (
        contribute, sync, feedback, pending, review, maintenance, promote, restore, imports, settings,
        share, health, digest, adoption, recall_stats, transcript, archives_list, archive_detail, workflow_report,
        scorers, session_score, offer, labels, receipt, local_scores, score_settings, score_drain,
        suite_add, task_add, tasks, proposal, run_plan, run_results, run_report, run_inputs, run_execute, run_benchmark,
        improve_run, improve_publish, improve_confirm, improve_ci, improve_schedule, namespaces,
    )
    for action in scoped_actions:
        action.add_argument("--project", type=_uuid, help="Weora 项目 ID，省略时使用工作空间共享范围")


def _queue(client: Client) -> Path:
    # 离线正文绑定原设备凭据，切换成员时不会误提交前一个成员的内容。
    identity = f"{client.account}\0{client.token}"
    return config_path().parent / "knowledge-outbox" / hashlib.sha256(identity.encode()).hexdigest()


def session_recall_hint(
    settings: SessionSettings, native_session: str, *,
    actor: UUID | None = None, recall_agent: bool = False, delegate_to: str | None = None,
) -> str:
    """通过现有宿主 Hook 提供召回指引，正文继续由本人设备按需查询。"""
    state = settings.effective("recall")
    if state.get("enabled") is not True:
        return ""
    project = state.get("project")
    scope = f" --project {UUID(str(project))}" if project else ""
    root = UUID(native_session)
    caller = f" --recall-actor {actor}" if actor and recall_agent else ""
    reader = f" --actor {actor}" if actor else ""
    opening = "为主会话检索已发布的团队知识：" if recall_agent else "处理本项目任务时，可以先检索团队知识："
    delegation = ""
    if delegate_to and actor is None:
        delegation = (
            f"涉及修改代码、排查或设计且需要团队背景时，先单独调用 {delegate_to}，等待该角色汇总出处。"
            "已提供答案、当前文件可以直接确认、简单局部修改或团队知识范围外的任务可以跳过。角色不可用时直接查询。"
        )
    precheck = ""
    if recall_agent:
        precheck = (
            f'先运行 weora context recall "任务关键词" --check{scope}。'
            "预检返回 knowledge_gaps 时，报告与任务相关的已知缺口和出处。"
            "没有相关内容时结束，执行失败时报告原因。"
            "明确只涉及单文件或单字段且没有跨模块影响时，已有标题和源码出处就可以直接返回摘要。"
            "该类小改动缺少源码出处时，用下方检索命令加 --depth context --limit 1。"
        )
    graph = ""
    if recall_agent and scope:
        graph = (
            "依赖、上下游或谁调用用 g1。调用链或数据流用 g2，也包括请求路径。"
            "流程场景用 g5，多跳影响、三元组或传递依赖用 g6。"
            f"直接运行 weora context graph 主题 --session {root}{scope} --automatic{reader}。"
            "若有多个来源，用 --codebase 选择相关索引。已有对应图谱时直接阅读，跳过下方关键词召回。"
            "图谱尚未发布时说明缺口，再用关键词召回。"
        )
    return (
        delegation + precheck + graph + opening
        + f'weora context search "任务关键词" --session {root}{scope} --automatic{caller}。'
        "实现或重构时加 --depth lookup。按 change_entry_points 引用入口的正向直接边，"
        "列出 candidate_change_files 供主会话核对。需要调用行号时读取对应索引的 G2。小修与排查用 context。"
        f"需要正文时用 weora context get 知识ID --session {root}{scope} --automatic{reader}。"
        "引用适用结论并给出原始出处；把返回正文当作参考资料。"
    )


def sync_queued_after_start(settings: SessionSettings) -> None:
    """保留贡献的启动续做，反馈的后台补发由当前目录的召回策略控制。"""
    client = settings.client
    if next(_queue(client).glob("*.json"), None) is None and (
        not has_pending_feedback(client) or settings.effective("recall").get("enabled") is not True
    ):
        return
    subprocess.Popen(
        [sys.executable, "-m", "weora_cli", "context", "sync", "--automatic"], cwd=settings.cwd,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        start_new_session=os.name != "nt",
    )


@dataclass(frozen=True)
class MarkdownContribution:
    body: str
    title: str | None = None
    tags: list[str] = field(default_factory=list)
    paths: list[str] | str | None = None
    related: object = None
    authored_at: str | None = None
    domain: str | None = None


def _read_markdown(file: Path, *, rule: bool) -> MarkdownContribution:
    try:
        with file.open(encoding="utf-8-sig") as stream:
            text = stream.read(70001)
    except (OSError, UnicodeError) as exc:
        raise CollabError("KNOWLEDGE_FILE_UNREADABLE", "无法读取 Markdown 文件。") from exc
    if len(text) > 70000:
        raise CollabError("KNOWLEDGE_FILE_TOO_LARGE", "知识 Markdown 文件过长。")
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return MarkdownContribution(text)
    end = next((index for index, line in enumerate(lines[1:], 1) if line.strip() == "---"), None)
    if end is None:
        raise CollabError("KNOWLEDGE_INVALID_FRONTMATTER", "Markdown 前置信息缺少结束标记。")
    header, body = "".join(lines[1:end]), "".join(lines[end + 1:])
    try:
        metadata = parse_metadata(header, rule=rule)
    except (OverflowError, ValueError) as exc:
        raise CollabError("KNOWLEDGE_INVALID_FRONTMATTER", "Markdown 前置信息含无效值。") from exc
    except (YAMLError, RecursionError) as exc:
        # 无法读取含 related 的前置信息时，平台仍可按贡献类型给出警告或审核失败。
        if re.search(r"^related[ \t]*:", header, re.MULTILINE):
            return MarkdownContribution(body, related=True)
        raise CollabError("KNOWLEDGE_INVALID_FRONTMATTER", "Markdown 前置信息格式无效。") from exc
    if not isinstance(metadata, dict):
        return MarkdownContribution(body)
    raw_title: object = metadata.get("title")
    raw_tags: object = metadata.get("tags")
    tags = [tag for tag in raw_tags if isinstance(tag, str)] if isinstance(raw_tags, list) else []
    return MarkdownContribution(
        body, raw_title if isinstance(raw_title, str) else None, tags,
        rule_paths(metadata) if rule else None, metadata.get("related"), markdown_date(metadata.get("date")),
        markdown_domain(metadata.get("domain")),
    )


def _related(metadata: object) -> tuple[list[tuple[str, str]], JsonValue]:
    """既保留旧 UUID 声明，也把团队路径和无效项交给平台统一校验。"""
    if metadata is None:
        return [], None
    if not isinstance(metadata, dict):
        return [], True
    allowed = {"MAPS_TO", "REFERENCES", "SUPERSEDES", "CONFLICTS_WITH"}
    links: list[tuple[str, str]] = []
    team_paths: dict[str, JsonValue] = {}
    for label, values in metadata.items():
        kind = label.upper() if isinstance(label, str) else ""
        if kind not in allowed:
            team_paths[label if isinstance(label, str) else "无效关系键"] = None
            continue
        if not isinstance(values, (str, list)):
            team_paths[label] = None
            continue
        paths: list[JsonValue] = []
        for identifier in values if isinstance(values, list) else [values]:
            if not isinstance(identifier, str):
                paths.append(None)
                continue
            try:
                links.append((kind, str(UUID(identifier))))
            except ValueError:
                paths.append(identifier)
        if paths:
            team_paths[label] = paths
    return links, team_paths or None


def _body(file: Path) -> tuple[str | None, list[str], str]:
    content = _read_markdown(file, rule=False)
    return content.title, content.tags, content.body


def _send(client: Client, entry: dict[str, JsonValue]) -> dict[str, JsonValue]:
    project, target = entry.get("project"), entry.get("entry")
    suffix = f"/{target}/revisions" if isinstance(target, str) else ""
    return _call(
        client, project if isinstance(project, str) else None, suffix,
        method="POST", payload=as_object(entry.get("payload")),
    )


def _contribution_request(args: argparse.Namespace) -> tuple[str, dict[str, JsonValue]]:
    content = _read_markdown(args.file, rule=args.kind == "rule")
    body = content.body
    operation = args.key or str(uuid4())
    source: dict[str, JsonValue] = {"type": args.source or "manual", "ref": args.source_ref}
    if args.resource_path is not None:
        source["resource_path"] = args.resource_path
    namespace = getattr(args, "namespace", None)
    if namespace is not None and (args.kind != "learning" or args.entry):
        raise CollabError("KNOWLEDGE_NAMESPACE_KIND", "经验目录只在新建经验时指定。")
    if namespace:
        source["namespace"] = namespace
    heading: str = args.title or content.title or args.file.stem
    if not body.strip() or len(body) > 64_000 or "\x00" in body:
        raise CollabError("KNOWLEDGE_CONTRIBUTION_TEXT", "请提供 64,000 字内的非空 Markdown 正文。")
    if not heading.strip() or len(heading) > 160 or "\x00" in heading:
        raise CollabError("KNOWLEDGE_CONTRIBUTION_TITLE", "请提供 160 字内的非空标题。")
    payload: dict[str, JsonValue] = {
        "operation_id": operation, "kind": args.kind, "title": heading,
        "body": body, "tags": list(content.tags), "source": source,
        "promoted_from": args.promoted_from, "base_revision": args.base_revision,
    }
    if content.authored_at is not None:
        payload["authored_at"] = content.authored_at
    if content.domain is not None:
        payload["domain"] = content.domain
    links, team_paths = _related(content.related)
    if team_paths is not None:
        payload["related"] = team_paths
    if args.maps_to:
        if args.kind not in {"doc", "rule"}:
            raise CollabError("KNOWLEDGE_RELATION_KIND", "--maps-to 适用于需要评审的文档或规则。")
        links.extend(("MAPS_TO", value) for value in args.maps_to)
    if links:
        payload["relations"] = [{"kind": kind, "target": target} for kind, target in dict.fromkeys(links)]
    if content.paths is not None:
        payload["rule_paths"] = content.paths if isinstance(content.paths, str) else list(content.paths)
    entry: dict[str, JsonValue] = {
        # 显式传空值与 TeamAI 一样选择共享根目录，省略参数继续沿用 Weora 项目。
        "project": None if namespace == "" else args.project,
        "entry": args.entry,
        "payload": payload,
    }
    return operation, entry


def _preview_contribution(args: argparse.Namespace) -> dict[str, JsonValue]:
    operation, entry = _contribution_request(args)
    payload = as_object(entry["payload"])
    source = as_object(payload["source"])
    body, title = str(payload["body"]), str(payload["title"])
    immediate = (
        payload["kind"] == "learning" and entry["entry"] is None
        and payload["promoted_from"] is None and source["type"] in ("manual", "session", "run")
    )
    location = source.get("resource_path")
    if location is None and immediate:
        directory = source.get("namespace")
        location = f"learnings/{directory}/{operation}" if directory else f"learnings/{operation}"
    return {
        "preview": True, "operation_id": operation, "project_id": entry["project"], "entry_id": entry["entry"],
        "kind": payload["kind"], "title": title.strip(), "resource_path": location,
        "bytes": len(body.encode()), "target_status": "active" if immediate else "pending",
        "base_revision": payload["base_revision"],
        **({"authored_at": payload["authored_at"]} if "authored_at" in payload else {}),
        **({"domain": payload["domain"]} if "domain" in payload else {}),
    }


@contextmanager
def _claim_contribution(path: Path) -> Iterator[None]:
    with ExitStack() as held:
        try:
            held.enter_context(lock_file(path))
        except OSError as error:
            raise CollabError("KNOWLEDGE_CONTRIBUTION_QUEUE_BUSY", "当前请求无法锁定，请在同步结束后重试。") from error
        try:
            yield
        except OSError as error:
            raise CollabError("KNOWLEDGE_CONTRIBUTION_QUEUE_IO", "无法更新本机贡献队列，请检查目录权限。") from error


def _read_contribution(path: Path) -> dict[str, JsonValue] | None:
    try:
        return as_object(json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return None
    except (OSError, ValueError, CollabError) as error:
        raise CollabError("KNOWLEDGE_CONTRIBUTION_QUEUE_INVALID", "无法读取已排队的贡献。") from error


def _submit(client: Client, args: argparse.Namespace) -> dict[str, JsonValue]:
    operation, entry = _contribution_request(args)
    staged = _queue(client) / f"{operation}.json"
    with _claim_contribution(staged):
        previous = _read_contribution(staged)
        if previous is not None and previous != entry:
            raise CollabError("KNOWLEDGE_CONFLICT", "同一离线请求标识已用于不同的贡献。")
        if previous is None:
            write_json(staged, entry)
        try:
            result = _send(client, entry)
        except CollabError as exc:
            if _queue_offline(exc):
                return {"queued": True, "operation_id": operation}
            # 已持久排队的草稿保留，便于权限或基准恢复后按原请求重试。
            if previous is None:
                staged.unlink(missing_ok=True)
            raise
        staged.unlink(missing_ok=True)
        return result


def _sync(client: Client, project: str | None, *, automatic: bool = False) -> dict[str, JsonValue]:
    published: list[JsonValue] = []
    failures: list[JsonValue] = []
    for file in sorted(_queue(client).glob("*.json")):
        try:
            entry = _read_contribution(file)
            if entry is None or (project is not None and entry.get("project") != project):
                continue
            with _claim_contribution(file):
                # 等待期间其他命令可能已送达。拿到锁后再读取实际要发送的请求。
                current = _read_contribution(file)
                if current is None or (project is not None and current.get("project") != project):
                    continue
                receipt = _send(client, current)
                file.unlink(missing_ok=True)
                published.append(receipt)
        except CollabError as exc:
            failures.append({"operation_id": file.stem, "code": exc.code})
    sent: list[JsonValue] = []
    pending: list[JsonValue] = []
    scope = project
    may_send = not automatic
    if automatic and has_pending_feedback(client):
        # 后台进程启动后重新读取策略，处理 hook 返回后用户关闭召回的情况。
        with suppress(CollabError, OSError, ValueError):
            recall = SessionSettings(client, Path.cwd()).effective("recall")
            may_send = recall.get("enabled") is True
            if scope is None and isinstance(bound_project := recall.get("project"), str):
                scope = bound_project
    if may_send:
        sent, pending = sync_feedback(client, scope, exact_scope=automatic)
    report: dict[str, JsonValue] = {"published": published}
    if sent or pending:
        report["feedback"] = {"sent": sent, "failures": pending}
    if failures or pending:
        if failures:
            report["failures"] = failures
        raise CollabError("KNOWLEDGE_SYNC_INCOMPLETE", "部分知识贡献或反馈仍在本机等待重试。", report)
    return report


def _queued_learnings(
    client: Client, project: str | None, *, created_after: float | None = None,
) -> list[dict[str, JsonValue]]:
    items: list[tuple[float, dict[str, JsonValue]]] = []
    for file in _queue(client).glob("*.json"):
        try:
            created = file.stat().st_mtime
            if created_after is not None and created < created_after:
                continue
            row = as_object(json.loads(file.read_text(encoding="utf-8")))
            payload = as_object(row.get("payload"))
            source = as_object(payload.get("source"))
        except (OSError, ValueError, CollabError):
            # 同步进程可能已取走文件。损坏的条目也不影响读取其他队列内容。
            continue
        scope = row.get("project")
        if row.get("entry") is not None or payload.get("promoted_from") is not None:
            continue
        if scope is not None and scope != project:
            continue
        if payload.get("kind") != "learning" or source.get("type") not in ("manual", "session", "run"):
            continue
        items.append((created, payload | {"id": file.stem, "project_id": scope, "status": "queued"}))
    items.sort(key=lambda row: (-row[0], str(row[1]["id"])))
    return [item for _, item in items]


def _review_overlaps(client: Client, project: str | None, body: str) -> tuple[list[JsonValue], str | None]:
    draft = keywords(body)
    if not draft:
        return [], None
    cutoff = (datetime.now(UTC) - LOOKBACK).timestamp()
    local: dict[str, tuple[float, str]] = {}
    for item in _queued_learnings(client, project, created_after=cutoff):
        title, text = item.get("title"), item.get("body")
        if not isinstance(title, str) or not isinstance(text, str):
            continue
        ratio = overlap(draft, keywords(text))
        if ratio >= MIN_OVERLAP:
            local[str(item["id"])] = (ratio, title)
    remote: list[tuple[float, str]] = []
    warning = None
    try:
        result = _call(client, project, "/overlaps", method="POST", payload={"body": body})
        for item in response_items(result):
            score, title = item.get("overlap"), item.get("title")
            if not isinstance(score, (float, int)) or not isinstance(title, str):
                raise CollabError("INVALID_RESPONSE", "知识重叠结果格式无效。")
            remote.append((float(score), title))
            if isinstance(key := item.get("operation_id"), str):
                local.pop(key, None)
    except CollabError as error:
        if error.code != "WEORA_UNREACHABLE" and not error.code.startswith("HTTP_5"):
            return [], error.code
        warning = error.code
    matches = sorted([*local.values(), *remote], key=lambda match: match[0], reverse=True)
    titles: list[JsonValue] = [title for _, title in matches]
    return titles, warning


class SearchOptions(TypedDict, total=False):
    depth: str
    check_details: int


class OpenOptions(TypedDict, total=False):
    recall_mode: int
    path: str


def _search(client: Client, args: argparse.Namespace) -> JsonValue:
    allowed = _automatic_recall_allowed(client, args)
    session = args.session if not args.check and allowed else None
    recall = args.action == "recall" or bool(args.automatic or args.recall_actor)
    depth = args.depth or ("context" if recall else None)
    check_details = args.check and args.action == "recall"
    pending_feedback = [] if args.check or not allowed else sync_feedback(
        client, args.project, for_search=True, exact_scope=args.automatic,
    )[1]
    votes = pending_for_recall(
        client, args.project, exact_scope=args.automatic,
    ) if allowed and args.query.strip() else []
    local_rows = _queued_learnings(client, args.project) if args.query.strip() else []
    if recall and args.project is not None:
        local_rows.sort(key=lambda item: item["project_id"] is None)
    queued = queue_view(local_rows, args.query, recall=recall)
    try:
        if session or votes or queued:
            payload: dict[str, JsonValue] = {
                "query": args.query, "limit": args.limit, "recall_mode": recall,
            }
            if depth:
                payload["depth"] = depth
            if votes:
                payload["pending_feedback"] = votes
            if queued:
                payload["queued_learnings"] = [row.hint() for row in queued]
            if session:
                payload["session"] = session
                if args.recall_actor:
                    payload["recall_actor"] = args.recall_actor
                endpoint = "/recall"
            else:
                payload["check"] = args.check
                if check_details:
                    payload["check_details"] = True
                endpoint = "/recall/preview"
            result = _call(client, args.project, endpoint, method="POST", payload=payload)
        else:
            options: SearchOptions = {"depth": depth} if depth else {}
            if check_details:
                options["check_details"] = 1
            result = _call(
                client, args.project, query=args.query, limit=args.limit,
                check=int(args.check), recall_mode=int(recall), **options,
            )
    except CollabError as exc:
        if not _queue_offline(exc):
            raise
        if recall and args.project is not None:
            # 离线时无法确认服务器上的继承设置，只召回当前项目的本机经验。
            queued = [row for row in queued if row.item["project_id"] == args.project]
        if not queue_matches(queued):
            raise
        result = {"offline": True}
    visible = result.pop("queued_visible", [] if not result.get("offline") else None)
    local = queue_matches(queued, visible=visible)
    if args.check:
        if check_details:
            if local:
                first = next(row for row in queued if row.item["id"] == local[0]["id"])
                response = first.check_details(offline=result.get("offline") is True)
                if gaps := result.get("knowledge_gaps"):
                    response["knowledge_gaps"] = gaps
                return response
            return result
        return {"relevant": bool(local) or result.get("relevant") is True}
    if recall and session:
        record_quality(client, session, result)
    remote = result.get("items")
    rows: list[JsonValue] = list(local)
    if isinstance(remote, list):
        rows.extend(remote)
    # 本机仅保留自己的离线正文。无权限响应继续按原错误返回。
    if pending_feedback:
        result["feedback_pending"] = pending_feedback
    return result | {"items": rows[:args.limit]}


def _queue_offline(error: CollabError) -> bool:
    # 网关的结构化业务码也可能对应短暂的 HTTP 服务故障。
    return error.code == "WEORA_UNREACHABLE" or error.code.startswith("HTTP_5") or (
        error.status is not None and error.status >= 500
    )


def _local_knowledge(client: Client, project: str | None, entry: str, *, recall: bool) -> dict[str, JsonValue] | None:
    queued = _queued_learnings(client, project)
    path = entry if "/" in entry else None
    if path is None:
        queued = [item for item in queued if item["id"] == entry]
        if not recall:
            return next(iter(queued), None)
    candidates = [replace(row, matches=True) for row in queue_view(queued, "", recall=True, path=path)]
    if not candidates:
        return None
    # 路径读取也使用设备范围与已提交请求核对，不发送正文和标题。
    try:
        report = _call(client, project, "/recall/preview", method="POST", payload={
            "query": "learning", "check": True, "limit": 1, "recall_mode": recall,
            "queued_learnings": [row.hint() for row in candidates],
        })
        items = queue_matches(candidates, visible=report.get("queued_visible", []))
    except CollabError as error:
        if not _queue_offline(error):
            raise
        if recall and project is not None:
            candidates = [row for row in candidates if row.item["project_id"] == project]
        items = queue_matches(candidates)
    return next(iter(items), None)


def _automatic_recall_allowed(client: Client, args: argparse.Namespace) -> bool:
    return not args.automatic or SessionSettings(client, Path.cwd()).effective("recall").get("enabled") is True


def _open_published(client: Client, project: str | None, entry: str, args: argparse.Namespace) -> JsonValue:
    """图谱定向读取和按路径打开使用同一授权、版本及根会话归因。"""
    by_path = "/" in entry
    path = "/by-path" if by_path else f"/{entry}"
    recall = bool(args.automatic or args.actor)
    if args.session and _automatic_recall_allowed(client, args):
        payload: dict[str, JsonValue] = {"session": args.session, "recall_mode": recall}
        if args.actor:
            payload["actor"] = args.actor
        if by_path:
            payload["path"] = entry
        return _call(client, project, path + "/open", method="POST", payload=payload)
    options: OpenOptions = {"recall_mode": 1} if recall else {}
    if by_path:
        options["path"] = entry
    return _call(client, project, path, **options)


def _read_graph(client: Client, project: str, args: argparse.Namespace) -> JsonValue:
    suffix = f"/codebases/graph-documents/{args.topic}"
    catalog = (
        _call(client, project, suffix, codebase_id=args.codebase)
        if args.codebase else _call(client, project, suffix)
    )
    pages = response_items(catalog)
    if not pages:
        return catalog | {"status": "missing"}
    if len(pages) > 1 or catalog.get("truncated") is True:
        return catalog | {"status": "choose"}
    entry = pages[0].get("entry_id")
    if not isinstance(entry, str):
        raise CollabError("INVALID_RESPONSE", "图谱文档缺少知识条目 ID。")
    try:
        identifier = UUID(entry)
    except ValueError as error:
        raise CollabError("INVALID_RESPONSE", "图谱文档的知识条目 ID 无效。") from error
    document = _open_published(client, project, str(identifier), args)
    return catalog | {"status": "ready", "document": document}


def execute(args: argparse.Namespace) -> JsonValue:
    if args.action == "import":
        return _import(args)
    if args.action == "contribute" and args.dry_run:
        return _preview_contribution(args)
    if args.action == "review":
        if args.iwiki_section:
            if args.project or args.revision is not None:
                raise CollabError("KNOWLEDGE_IWIKI_REVIEW_SCOPE", "离线章节审核沿用导入范围。")
            return review_local_iwiki_section(UUID(args.entry), args.iwiki_section, args.decision, args.reason)
        if args.revision is None:
            raise CollabError("KNOWLEDGE_REVIEW_REVISION", "平台审核需要指定 --revision。")
    if args.action == "codebase":
        if args.deep_enrich:
            return _enrich_codebase(args)
        if any(option is not None for option in (args.via, args.max_modules, args.concurrency)):
            raise CollabError("KNOWLEDGE_CODEBASE_OPTIONS", "深度生成选项需要 --deep-enrich。")
        if args.dry_run and not args.reconcile:
            raise CollabError("KNOWLEDGE_CODEBASE_OPTIONS", "--dry-run 需要 --deep-enrich 或 --reconcile。")
        if args.reconcile and (args.local or args.repository or (args.output is not None and args.project)):
            raise CollabError("KNOWLEDGE_CODEBASE_SCOPE", "调和覆盖所选项目或离线目录中的全部代码来源。")
        if args.reconcile and args.output is not None:
            from weora_cli.knowledge_offline_code import offline_reconcile

            return offline_reconcile(args.output, preview=args.dry_run)
        if args.output is not None:
            from weora_cli.knowledge_offline_code import offline_codebase

            if args.local or args.repository:
                raise CollabError("KNOWLEDGE_OFFLINE_SCOPE", "--output 使用离线代码库名称选择来源。")
            return offline_codebase(args.output, args.project, lint=args.lint)
    client = paired_client()
    action = args.action
    if action == "workflow-definition":
        return configure_knowledge_workflow(client, args)
    if action == "workflow":
        return execute_knowledge_workflow(client, args, _body)
    if action == "share-learnings":
        if SessionSettings(client, Path.cwd()).effective("recall").get("enabled") is not True:
            raise CollabError("KNOWLEDGE_SHARE_RECALL_DISABLED", "分享会话经验需要先启用召回。")
        return share_learnings(
            client,
            runtime=args.runtime, session=args.session, project=args.project, files=args.file,
            confirmation=args.confirm, read_markdown=_body,
        )
    if action == "share-reminder":
        return SessionSettings(client, Path.cwd()).configure("share-reminder", args.mode, workspace=args.workspace)
    if action == "plan-reminder":
        return configure_plan_reminder(client, args)
    if action == "mr-hint":
        return configure_mr_hint(client, args)
    if action == "activity-settings":
        return configure_activity(
            client, Path.cwd(), args.mode, workspace=args.workspace, project=args.project, shared=args.shared,
        )
    if action == "publish-activity":
        return publish_activity(client, Path.cwd()) or {"status": "off"}
    if action == "publish-workflow":
        return publish_workflow_activity(client, args)
    if action == "archive-settings":
        return configure_archive(
            client, Path.cwd(), args.run_mode, args.mode, workspace=args.workspace, project=args.project,
        )
    if action == "scoring-settings":
        return configure_score_work(client, Path.cwd(), args)
    if action == "score-drain":
        args.run_mode = args.run_mode or configured_mode()
        if args.run_mode == "unattended" and not args.bound:
            return drain_unattended(client, Path.cwd(), args, task=task_identifier())
        return drain_score_work(client, Path.cwd(), args)
    if action == "finished-sessions":
        if args.drain is not None and args.runtime is not None:
            cwd = Path.cwd()
            activity_report = None
            if args.apply:
                with suppress(CollabError, OSError, ValueError):
                    activity_report = drain_activity(client, cwd, args.runtime, args.drain, args.transcript)
                with suppress(CollabError, OSError, ValueError):
                    stage_drained_session(client, cwd, args.runtime, args.drain, args.transcript)
            try:
                result = drain_finished_session(
                    client, cwd, args.runtime, args.drain, args.transcript, apply=args.apply,
                )
                return result | {"activity": activity_report} if activity_report is not None else result
            finally:
                if args.apply:
                    with suppress(CollabError, OSError, ValueError):
                        start_score_work(client, cwd, "unattended")
        if args.drain is not None or args.runtime is not None or args.transcript is not None:
            raise CollabError("KNOWLEDGE_ARCHIVE_DRAIN", "处理无人值守会话时必须同时指定 --drain 和 --runtime。")
        finished = list_finished_sessions(client, Path.cwd(), args.limit, apply=args.apply)
        if args.apply:
            with suppress(CollabError, OSError, ValueError):
                start_score_work(client, Path.cwd(), "attended")
        return finished
    project = args.project
    if action == "share-session":
        return share_session(client, project, args.runtime, args.session, args.transcript, args.title, args.confirm)
    if action == "list-shared-sessions":
        if not 1 <= args.limit <= 100:
            raise CollabError("INVALID_LIMIT", "会话条数必须在 1 至 100 之间。")
        return _call(client, project, "/shared-sessions", limit=args.limit)
    if action == "get-shared-session":
        version = {"revision": args.revision} if args.revision is not None else {}
        return _call(client, project, f"/shared-sessions/{args.archive}", **version)
    if action == "session-scorers":
        return configure_session_scorers(client, args)
    if action == "score":
        if any(name.startswith(GENERATED_NAME_PREFIXES) for name in args.scorer):
            return score_named(client, args)
        return score_session(client, args)
    if action == "score-offer":
        return score_offer(client, args)
    if action == "session-labels":
        return _call(client, project, f"/shared-sessions/{args.archive}/labels",
                     revision=args.revision, limit=args.limit, history=int(args.history))
    if action == "score-receipt":
        return _call(client, project, f"/session-scoring/named-calls/{args.call_id}")
    if action == "list-local-scores":
        return _call(client, project, "/session-scoring/local-subjects", limit=args.limit)
    if action == "codebase":
        if not project:
            raise CollabError("KNOWLEDGE_CODEBASE_SCOPE", "查看仓库索引需要指定 --project。")
        try:
            project = str(UUID(project))
        except ValueError as exc:
            raise CollabError("KNOWLEDGE_CODEBASE_SCOPE", "平台代码索引需要 Weora 项目 UUID。") from exc
        if args.reconcile:
            return _call(client, project, "/codebases/reconcile", method="GET" if args.dry_run else "POST")
        suffix = f"/local/{args.local}" if args.local else f"/{args.repository}" if args.repository else ""
        return _call(client, project, "/codebases" + suffix, lint=int(args.lint))
    if action == "namespace-settings":
        if args.mode == "status":
            return _call(client, project, "/settings/resource-namespaces")
        if project or not args.file:
            raise CollabError("KNOWLEDGE_NAMESPACE_SCOPE", "保存清单需要 --file，使用工作空间范围。")
        try:
            manifest = as_object(json.loads(args.file.read_text(encoding="utf-8-sig")))
        except (OSError, ValueError) as exc:
            raise CollabError("KNOWLEDGE_NAMESPACE_INVALID", "无法读取 JSON 目录清单。") from exc
        return _call(client, None, "/settings/resource-namespaces", method="PUT", payload=manifest)
    if action == "recall-settings":
        return SessionSettings(client, Path.cwd()).configure(
            "recall", args.mode, workspace=args.workspace, project=project,
        )
    if action == "recall-scope":
        endpoint = "/settings/recall-scope"
        if args.mode == "status":
            return _call(client, project, endpoint)
        return _call(client, project, endpoint, method="PUT", payload={"inherit_shared": args.mode == "enable"})
    if action == "sync":
        return _sync(client, project, automatic=args.automatic)
    if action == "contribute":
        return _submit(client, args)
    if action in {"search", "recall"}:
        if args.project is None and not args.shared and (action == "recall" or args.automatic or args.recall_actor):
            args.project = SessionSettings(client, Path.cwd()).recall_project()
        if args.limit is None:
            args.limit = 5 if action == "recall" or args.automatic else 20
        if not 1 <= args.limit <= 100:
            raise CollabError("INVALID_LIMIT", "检索条数必须在 1 至 100 之间。")
        if (args.automatic or args.recall_actor) and not args.session:
            raise CollabError("KNOWLEDGE_SESSION_REQUIRED", "自动召回或子 Agent 召回需要 --session。")
        return _search(client, args)
    if action == "gaps":
        return _call(client, args.project, "/codebases/gaps", query=args.query)
    if action == "get" or action == "graph":
        if (args.automatic or args.actor) and not args.session:
            raise CollabError("KNOWLEDGE_SESSION_REQUIRED", "自动采纳或子 Agent 打开需要 --session。")
        if action == "graph":
            return _read_graph(client, args.project, args)
        recall = bool(args.automatic or args.actor)
        if project is None and not args.shared:
            project = SessionSettings(client, Path.cwd()).recall_project()
        if local := _local_knowledge(client, project, args.entry, recall=recall):
            return local
        return _open_published(client, project, args.entry, args)
    if action == "feedback":
        return submit_feedback(
            client, project, args.entry, args.session, positive=args.positive, preview=args.dry_run,
        )
    if action == "adoption":
        return score_adoption(client, args)
    if action == "recall-stats":
        if not 1 <= args.limit <= 100:
            raise CollabError("INVALID_LIMIT", "会话条数必须在 1 至 100 之间。")
        return _call(client, project, "/recall/stats", method="POST", payload={
            "session": args.session, "limit": args.limit, "recent": args.recent,
        })
    if action == "pending":
        return _call(client, project, "/review")
    if action == "health":
        if not 1 <= args.limit <= 100:
            raise CollabError("INVALID_LIMIT", "报告明细条数必须在 1 至 100 之间。")
        return _call(client, project, "/health", limit=args.limit, **lifecycle_query(project, args.project_only))
    if action == "self-improvement":
        if args.improvement_action == "run":
            return execute_improvement(client, args)
        if args.improvement_action == "ci":
            return execute_improvement_ci(client, args)
        if args.improvement_action == "scheduled":
            return execute_improvement_schedule(client, args)
        return execute_improvement_delivery(client, args)
    if action == "benchmark":
        return execute_benchmark(client, args)
    if action == "digest":
        if args.suite is not None and args.scorer is None:
            raise CollabError("KNOWLEDGE_BENCHMARK_SUITE", "--suite 需要同时指定 --scorer。")
        if args.failures or args.scorer is not None:
            if not 1 <= args.limit <= 100:
                raise CollabError("INVALID_LIMIT", "评分失败明细条数必须在 1 至 100 之间。")
            improvement = (
                plan_improvement(client, project, args.scorer, Path.cwd(), suite=args.suite)
                if args.scorer is not None else None
            )
            digest = _call(client, project, "/digest/failures", limit=args.limit)
            return digest | {"improvement": improvement} if improvement is not None else digest
        return _call(client, project, "/digest", **lifecycle_query(project, args.project_only))
    if action == "review":
        if args.decision == "preview":
            return _call(client, project, f"/{args.entry}/review", revision=args.revision)
        return _call(client, project, f"/{args.entry}/review", method="POST", payload={
            "decision": args.decision, "revision": args.revision, "reason": args.reason,
        })
    if action == "promote":
        return knowledge_promotion.execute(client, project, args)
    if action == "restore":
        return _call(client, project, f"/{args.entry}/restore", method="POST")
    if args.archive and (
        args.confidence_writeback or args.update_quality or args.submit_quality or args.quality_candidates
    ):
        raise CollabError("KNOWLEDGE_MAINTENANCE_OPTIONS", "--archive 只能与归档选项一起使用。")
    if args.threshold is not None and not (args.prune or args.archive):
        raise CollabError("KNOWLEDGE_MAINTENANCE_OPTIONS", "--threshold 需要同时指定 --prune 或 --archive。")
    if args.prune or args.confidence_writeback or (args.archive and (args.dry_run or args.threshold is not None)):
        if args.via or args.output:
            raise CollabError("KNOWLEDGE_MAINTENANCE_OPTIONS", "归档和置信度回写直接使用已记录的反馈。")
        return maintain(
            client, project, confidence=args.confidence_writeback, preview=args.dry_run,
            threshold=args.threshold, after=args.after, project_only=args.project_only,
        )
    if args.update_quality:
        return generate_quality(
            client, project, driver=args.via or "claude", preview=args.dry_run, output=args.output,
            after=args.after, project_only=args.project_only,
        )
    if args.dry_run or args.via or args.output:
        raise CollabError("KNOWLEDGE_QUALITY_OPTIONS", "质量草稿的预览、Agent 和输出选项请配合 --update-quality。")
    if args.submit_quality:
        if args.after:
            raise CollabError("KNOWLEDGE_QUALITY_OPTIONS", "提交草稿不使用分页游标。")
        return submit_quality(client, args.submit_quality, project)
    suffix = "/maintenance/archive" if args.archive else (
        "/maintenance/quality" if args.quality_candidates else "/maintenance"
    )
    params = lifecycle_query(project, args.project_only)
    if args.after:
        params["after"] = args.after
    return _call(client, project, suffix, method="POST" if args.archive else "GET", **params)


def _enrich_codebase(args: argparse.Namespace) -> JsonValue:
    from weora_cli.knowledge_code_enrichment import enrich_offline, enrich_platform

    if not args.project:
        raise CollabError("KNOWLEDGE_CODE_ENRICH_SCOPE", "请选择平台项目或离线代码库。")
    maximum = args.max_modules if args.max_modules is not None else 0
    if not 0 <= maximum <= (1 << 53) - 1:
        raise CollabError("KNOWLEDGE_CODEBASE_OPTIONS", "--max-modules 需要非负安全整数。")
    concurrency = positive_option(args.concurrency, "--concurrency", 5)
    if args.output is not None:
        if args.local or args.repository:
            raise CollabError("KNOWLEDGE_CODE_ENRICH_SCOPE", "离线代码库按导入返回的名称选择。")
        return enrich_offline(
            args.output, args.project, driver=args.via, max_modules=maximum,
            concurrency=concurrency, preview=args.dry_run,
        )
    if not (args.local or args.repository):
        raise CollabError("KNOWLEDGE_CODE_ENRICH_SCOPE", "请选择 --repository 或 --local 的代码索引。")
    try:
        project = str(UUID(args.project))
    except ValueError as exc:
        raise CollabError("KNOWLEDGE_CODE_ENRICH_SCOPE", "平台深度生成需要 Weora 项目 UUID。") from exc
    source = f"local/{args.local}" if args.local else args.repository
    return enrich_platform(
        paired_client(timeout=120), project, f"/codebases/{source}/design",
        driver=args.via, max_modules=maximum, concurrency=concurrency, preview=args.dry_run,
    )


def _import(args: argparse.Namespace) -> JsonValue:
    project = args.project
    if args.domain is not None and args.from_repo is None:
        raise CollabError("KNOWLEDGE_IMPORT_OPTIONS", "--domain 用于 --from-repo，仓库清单使用各条目的 domain。")
    if (args.iwiki_dual and args.from_iwiki is None) or (args.require_review and not args.iwiki_dual):
        raise CollabError("KNOWLEDGE_IMPORT_OPTIONS", "--iwiki-dual 用于 iWiki 导入，--require-review 用于汇总章节。")
    if args.dir is not None:
        return _import_local_codebase(args)
    if any(source is not None for source in (args.from_repo, args.from_repo_list, args.from_org)):
        return _import_codebase(args)
    if args.from_mr is None and (args.no_discussions or args.language):
        raise CollabError("KNOWLEDGE_IMPORT_OPTIONS", "讨论和语言选项在首次 PR/MR 导入时指定。")
    if args.dry_run and args.from_mr is None and args.from_iwiki is None:
        raise CollabError("KNOWLEDGE_IMPORT_OPTIONS", "预览选项用在首次 iWiki 或 PR/MR 导入。")
    if (
        args.incremental or args.max_files is not None or args.concurrency is not None
        or args.ssh or args.skip_enrich or _has_org_options(args)
    ):
        raise CollabError("KNOWLEDGE_IMPORT_OPTIONS", "仓库导入选项只在首次代码索引时指定。")
    if args.dry_run and args.output is not None:
        raise CollabError("KNOWLEDGE_IMPORT_OPTIONS", "预览通过命令返回草稿，本地 Markdown 请使用 --output。")
    if args.manifest is not None:
        if args.via is not None or args.output is not None:
            raise CollabError("KNOWLEDGE_IMPORT_OPTIONS", "分类客户端和本地输出用于直接从来源导入。")
        client = paired_client()
        items = select_imports(args.manifest, accept_all=args.all, project=project)
        if not items:
            return {"items": []}
        return _call(client, project, "/imports", method="POST", payload={"items": items})
    if args.resume and (project or args.all or args.output is not None):
        raise CollabError("KNOWLEDGE_IMPORT_OPTIONS", "续做沿用原来的项目、审阅方式和输出位置。")
    if args.output is not None and project:
        raise CollabError("KNOWLEDGE_IMPORT_OPTIONS", "本地输出不选择平台项目。")
    if args.resume and is_local_import(args.resume, marker="offline_repo_batch"):
        from weora_cli.knowledge_offline_repo import resume_offline_repositories

        if args.via is not None:
            raise CollabError("KNOWLEDGE_IMPORT_OPTIONS", "离线仓库续做沿用原来的来源和输出位置。")
        return resume_offline_repositories(UUID(args.resume))
    local = (
        args.output is not None or (args.resume is not None and is_local_import(args.resume))
        or (args.from_iwiki is not None and args.dry_run and not project)
    )
    device = None if local else paired_client()
    if device and args.resume and is_repo_batch(device, UUID(args.resume)):
        if args.via is not None:
            raise CollabError("KNOWLEDGE_IMPORT_OPTIONS", "仓库续做沿用原来的项目、来源和索引参数。")
        return import_repositories(device, resume=UUID(args.resume))
    if device and project:
        _call(device, project, query="", limit=1)

    def publish(scope: str | None, items: list[JsonValue]) -> dict[str, JsonValue]:
        assert device is not None
        evidence: dict[str, JsonValue] = {}
        contributions: list[JsonValue] = []
        for entry in items:
            item = as_object(entry).copy()
            if terms := item.pop("iwiki_terms", None):
                evidence[str(item["operation_id"])] = terms
            contributions.append(item)
        payload: dict[str, JsonValue] = {"items": contributions, "publish_reviewed": True}
        if evidence:
            payload["iwiki_terms"] = evidence
        return _call(device, scope, "/imports", method="POST", payload=payload)

    def publish_summary(scope: str | None, payload: dict[str, JsonValue]) -> dict[str, JsonValue]:
        assert device is not None
        return _call(device, scope, "/imports/iwiki-sections", method="POST", payload=payload)

    def overlaps(scope: str | None, draft: dict[str, JsonValue]) -> tuple[list[JsonValue], str | None]:
        assert device is not None
        return _review_overlaps(device, scope, review_markdown(draft))

    return run_import(
        device, project=project, driver=args.via, accept_all=args.all, resume=args.resume,
        output=args.output, publish=publish, iwiki_source=args.from_iwiki,
        iwiki_dual=args.iwiki_dual, iwiki_require_review=args.require_review, publish_summary=publish_summary,
        mr_sources=args.from_mr, discussions=not args.no_discussions, language=args.language or "zh",
        overlaps=overlaps if device is not None else None, preview=args.dry_run,
        refresh=partial(refresh_review_indexes, device) if device is not None else None,
    )


def _has_org_options(args: argparse.Namespace) -> bool:
    return args.skip_import or args.include_archived or args.exclude_archived or any(
        option is not None for option in (args.max_repos, args.include_pattern, args.exclude_pattern)
    )


def _import_local_codebase(args: argparse.Namespace) -> JsonValue:
    if (
        args.all or args.no_discussions or args.language
        or args.concurrency is not None or args.ssh or _has_org_options(args)
    ):
        raise CollabError("KNOWLEDGE_IMPORT_OPTIONS", "本地目录导入支持项目、输出目录、预览、增量与文件数选项。")
    limit = args.max_files if args.max_files is not None else 200
    if not 1 <= limit <= 1000:
        raise CollabError("KNOWLEDGE_IMPORT_OPTIONS", "--max-files 必须在 1 至 1000 之间。")
    if args.output is not None and args.project:
        raise CollabError("KNOWLEDGE_IMPORT_OPTIONS", "本地输出不选择平台项目。")
    if args.dry_run:
        return local_snapshot(args.dir, limit=limit, preview=True, incremental=args.incremental)
    if args.output is not None:
        return _import_offline_directory(args, limit)
    if not args.project:
        raise CollabError(
            "KNOWLEDGE_CODEBASE_SCOPE", "本地目录导入需要使用 --project 指定项目，或用 --output 输出至本机。",
        )
    driver = select_import_design_driver(args.via, skip=args.skip_enrich)
    client = paired_client(timeout=140)
    payload = local_snapshot(args.dir, limit=limit, preview=False, incremental=args.incremental)
    result = _call(client, args.project, "/codebases/local", method="POST", payload=payload)
    if driver is False:
        return result
    return _enrich_code_import(client, args.project, "local", result.get("codebase_id"), result, driver)


def _import_offline_directory(args: argparse.Namespace, limit: int) -> dict[str, JsonValue]:
    from weora_cli.knowledge_code_enrichment import enrich_offline_snapshot
    from weora_cli.knowledge_offline_code import export_codebase, offline_code_writer

    driver = select_import_design_driver(args.via, skip=args.skip_enrich)
    payload = local_snapshot(args.dir, limit=limit, preview=False, incremental=args.incremental)
    if driver is False:
        return export_codebase(args.output, payload)
    with offline_code_writer(args.output) as writer:
        result = writer.publish(payload)
        try:
            slug = str(result["project"])
            return finish_import_design(
                result,
                lambda index: enrich_offline_snapshot(
                    writer.root, slug, driver=driver, expected_revision=index["revision"],
                ),
                {"output": str(writer.root.parent), "project": slug, "deep_enrich": True},
            )
        finally:
            writer.refresh()


def _enrich_code_import(
    client: Client, project: str, selector: str, identifier: JsonValue,
    index: dict[str, JsonValue], driver: ImportDesignDriver, *, domain_override: str | None = None,
) -> dict[str, JsonValue]:
    from weora_cli.knowledge_code_enrichment import enrich_platform

    if not isinstance(identifier, str):
        raise CollabError("KNOWLEDGE_CODE_ENRICH_RESPONSE", "平台没有返回可用于深度生成的代码索引。")
    target = f"local/{identifier}" if selector == "local" else identifier
    retry: dict[str, JsonValue] = {"project": project, selector: identifier, "deep_enrich": driver is not False}
    if domain_override is not None:
        retry["domain"] = domain_override
    return finish_import_design(
        index,
        lambda expected: enrich_platform(
            client, project, f"/codebases/{target}/design", driver=driver, max_modules=0,
            concurrency=5, preview=False, expected_index=expected, domain_override=domain_override,
        ),
        retry,
    )


def _import_codebase(args: argparse.Namespace) -> JsonValue:
    if args.all or args.no_discussions or args.language:
        raise CollabError("KNOWLEDGE_IMPORT_OPTIONS", "仓库导入使用仓库索引参数。")
    if args.output is not None and args.project:
        raise CollabError("KNOWLEDGE_IMPORT_OPTIONS", "离线输出不选择平台项目。")
    if args.ssh and args.output is None:
        raise CollabError("KNOWLEDGE_IMPORT_OPTIONS", "平台仓库导入沿用项目登记的连接方式。")
    if args.from_org is None and _has_org_options(args):
        raise CollabError("KNOWLEDGE_IMPORT_OPTIONS", "组织筛选和只生成清单选项用于 --from-org。")
    workers = positive_option(args.concurrency, "--concurrency", 3)
    if args.max_files is not None and not 1 <= args.max_files <= 1000:
        raise CollabError("KNOWLEDGE_IMPORT_OPTIONS", "--max-files 必须在 1 至 1000 之间。")
    if args.output is None and not args.project and not (args.from_org and (args.skip_import or args.dry_run)):
        raise CollabError("KNOWLEDGE_CODEBASE_SCOPE", "仓库导入需要使用 --project 指定 Weora 项目。")
    if args.from_repo is not None and args.concurrency is not None:
        raise CollabError("KNOWLEDGE_IMPORT_OPTIONS", "--concurrency 用于批量仓库导入。")
    profile = assign_code_domain(args.domain) if args.domain is not None else None
    if args.domain is not None and profile is None:
        raise CollabError("KNOWLEDGE_CODE_DOMAIN_BODY", "领域需要为 1 至 80 字的单行文本。")
    domain = profile.domain if profile is not None else None
    driver = False if args.dry_run or args.skip_import else select_import_design_driver(args.via, skip=args.skip_enrich)
    if args.from_repo is not None:
        source = repository_reference(args.from_repo)
        if args.output is not None:
            return _offline_repositories(
                args, [RepositoryImportSource(source, domain=domain)], [], workers, driver=driver,
            )
        client = paired_client(timeout=140)
        if args.dry_run:
            return preview_repositories(
                client, args.project, [source], [], domain_overrides={source: domain} if domain else None,
            )
        payload: dict[str, JsonValue] = {"incremental": args.incremental}
        if args.max_files is not None:
            payload["max_files"] = args.max_files
        try:
            repo = str(UUID(source))
        except ValueError:
            planned = repository_plan(client, args.project, [source])[0]
            if planned.get("status") != "ready":
                raise CollabError(
                    "KNOWLEDGE_CODEBASE_REPOSITORY_NOT_FOUND", "项目中未找到唯一的登记仓库。", planned,
                ) from None
            repo = str(UUID(str(planned["repository_id"])))
            payload["expected_connection_version"] = planned["connection_version"]
        result = _call(client, args.project, f"/codebases/{repo}", method="POST", payload=payload)
        if driver is False and domain is None:
            return result
        return _enrich_code_import(client, args.project, "repository", repo, result, driver, domain_override=domain)
    if args.from_org is not None:
        return _import_org(args, workers, driver)
    sources, skipped = read_repo_list(args.from_repo_list)
    if args.output is not None:
        return _offline_repositories(args, sources, skipped, workers, driver=driver)
    rows = list(dict.fromkeys(item.source for item in sources))
    domains = repository_domains(sources)
    device = paired_client()
    if args.dry_run:
        return preview_repositories(device, args.project, rows, skipped, domain_overrides=domains)
    return import_repositories(
        device, project=args.project, sources=rows, incremental=args.incremental,
        limit=args.max_files, concurrency=workers, skipped=skipped, design_driver=driver, domain_overrides=domains,
    )


def _offline_repositories(
    args: argparse.Namespace, sources: list[RepositoryImportSource], skipped: list[JsonValue],
    workers: int, *, driver: ImportDesignDriver, whitelist: str | None = None,
) -> dict[str, JsonValue]:
    from weora_cli.knowledge_offline_repo import preview_offline_repositories, start_offline_repositories

    limit = args.max_files if args.max_files is not None else 200
    if args.ssh:
        sources = [
            replace(item, auth="ssh") if item.auth != "public" else item for item in sources
        ]
    if args.dry_run:
        return preview_offline_repositories(args.output, sources, skipped, incremental=args.incremental, limit=limit)
    return start_offline_repositories(
        args.output, sources, skipped, incremental=args.incremental, limit=limit,
        concurrency=workers, whitelist=whitelist, design_driver=driver,
    )


def _import_org(args: argparse.Namespace, workers: int, driver: ImportDesignDriver) -> JsonValue:
    maximum = positive_option(args.max_repos, "--max-repos", 200)
    include, exclude = pattern(args.include_pattern), pattern(args.exclude_pattern)
    device = paired_client() if args.project and not args.skip_import else None
    rows = list_org(args.from_org, maximum, include=include, exclude=exclude, include_archived=args.include_archived)
    sources = [str(row["url"]) for row in rows]
    exposed_sources: list[JsonValue] = [source for source in sources]
    path = Path.cwd() / ".weora" / "repo-whitelist.draft.yaml"
    if args.dry_run:
        data: dict[str, JsonValue] = {"preview": True, "whitelist": str(path), "repositories": exposed_sources}
        if device:
            data |= preview_repositories(device, args.project, sources, [])
        return data
    if rows:
        write_whitelist(path, rows)
    if rows and args.output is not None and not args.skip_import:
        return _offline_repositories(
            args, [RepositoryImportSource(source, "token") for source in sources], [], workers,
            driver=driver, whitelist=str(path),
        )
    if not device or not rows:
        return {"whitelist": str(path) if rows else None, "repositories": exposed_sources, "imported": 0}
    return import_repositories(
        device, project=args.project, sources=sources, incremental=args.incremental,
        limit=args.max_files, concurrency=workers, whitelist=str(path), design_driver=driver,
    )
