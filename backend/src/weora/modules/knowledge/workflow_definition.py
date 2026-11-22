"""团队可覆盖知识工作流的提示、步骤依赖和审批方式。"""

from graphlib import CycleError, TopologicalSorter
from itertools import count
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

WorkflowName = Literal["teamai-share-learnings", "teamai-share-session"]
Invocation = Literal["allow", "ask", "deny"]
Action = Literal["weora.contribute-learnings", "weora.share-session"]


class WorkflowApproval(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    message: str = Field(min_length=1, max_length=2000)
    required: bool = False


class WorkflowStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,39}$")
    needs: list[str] | None = Field(default=None, max_length=16)
    prompt: str | None = Field(default=None, min_length=1, max_length=8000)
    approve: WorkflowApproval | str | None = None
    uses: Action | None = None

    @model_validator(mode="after")
    def single_action(self) -> Self:
        if sum(field is not None for field in (self.prompt, self.approve, self.uses)) != 1:
            raise ValueError("每个工作流步骤应是草拟、审批或一项 Weora 操作。")
        if self.prompt is not None and not self.prompt.strip():
            raise ValueError("草拟提示不能为空。")
        if isinstance(self.approve, str):
            self.approve = WorkflowApproval(message=self.approve)
        return self


class KnowledgeWorkflowDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, str_strip_whitespace=True)

    schema_version: Literal["v1alpha1"] = Field(alias="schemaVersion")
    name: WorkflowName
    description: str = Field(min_length=1, max_length=2000)
    alias: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9-]{0,63}$")
    invoke: Invocation = "deny"
    steps: list[WorkflowStep] = Field(min_length=1, max_length=16)

    def dependencies(self) -> dict[str, list[str]]:
        """省略 needs 时依赖上一步，空列表允许独立运行。"""
        previous: list[str] = []
        dependencies: dict[str, list[str]] = {}
        for step in self.steps:
            dependencies[step.id] = previous if step.needs is None else step.needs
            previous = [step.id]
        return dependencies

    @model_validator(mode="after")
    def validate_steps(self) -> Self:
        dependencies = self.dependencies()
        if len(dependencies) != len(self.steps):
            raise ValueError("工作流步骤标识必须唯一。")
        if any(set(needs) - dependencies.keys() or len(needs) != len(set(needs)) for needs in dependencies.values()):
            raise ValueError("步骤依赖需要指向不同的已定义步骤。")
        try:
            order = tuple(TopologicalSorter(dependencies).static_order())
        except CycleError as exc:
            raise ValueError("工作流步骤依赖不能成环。") from exc
        drafts = {step.id for step in self.steps if step.prompt is not None}
        actions = {step.uses: step.id for step in self.steps if step.uses is not None}
        if len(drafts) > 1 or len(actions) != sum(step.uses is not None for step in self.steps):
            raise ValueError("草拟和每种发布操作至多登记一次。")
        if self.name == "teamai-share-session" and (drafts or "weora.contribute-learnings" in actions):
            raise ValueError("会话归档工作流只处理脱敏会话和评分。")
        ancestors: dict[str, set[str]] = {}
        for step_id in order:
            needs = dependencies[step_id]
            ancestors[step_id] = set(needs).union(*(ancestors[dependency] for dependency in needs))
        if (contribution := actions.get("weora.contribute-learnings")) and not drafts.intersection(
            ancestors[contribution],
        ):
            raise ValueError("发布经验步骤必须依赖草拟步骤。")
        return self


def default_workflow(name: WorkflowName) -> KnowledgeWorkflowDefinition:
    if name == "teamai-share-session":
        return KnowledgeWorkflowDefinition(
            schemaVersion="v1alpha1", name=name, invoke="ask",
            description="按本人选择分享脱敏会话并运行团队评分。",
            steps=[WorkflowStep(id="share-session", uses="weora.share-session")],
        )
    return KnowledgeWorkflowDefinition(
        schemaVersion="v1alpha1", name=name, invoke="ask",
        description="提炼值得复用的会话经验，确认后发布，并按需归档和评分。",
        steps=[
            WorkflowStep(id="learning", prompt="和发起人一起整理会话经验，逐条确认正文、标题和目标范围。"),
            WorkflowStep(id="approve", approve=WorkflowApproval(message="将已预览的经验分享给团队？")),
            WorkflowStep(id="contribute", needs=["learning", "approve"], uses="weora.contribute-learnings"),
            WorkflowStep(id="share-session", needs=[], uses="weora.share-session"),
        ],
    )


def embed_session_workflow(
    parent: KnowledgeWorkflowDefinition, child: KnowledgeWorkflowDefinition,
) -> KnowledgeWorkflowDefinition:
    """把会话子流程展开为可恢复的依赖关系，父级等待子流程的终点。"""
    stage = next((item for item in parent.steps if item.uses == "weora.share-session"), None)
    if stage is None:
        return parent
    parent_dependencies = parent.dependencies()
    child_dependencies = child.dependencies()
    available = (f"session-{index}" for index in count(1) if f"session-{index}" not in parent_dependencies)
    names = {step.id: next(available) for step in child.steps}
    parents = {dependency for needs in child_dependencies.values() for dependency in needs}
    terminals = [names[step.id] for step in child.steps if step.id not in parents]
    embedded: list[WorkflowStep] = []
    for step in parent.steps:
        if step.id == stage.id:
            for child_step in child.steps:
                needs = [names[dependency] for dependency in child_dependencies[child_step.id]]
                embedded.append(child_step.model_copy(update={
                    "id": names[child_step.id], "needs": needs or parent_dependencies[stage.id],
                }))
        else:
            needs = [
                target
                for dependency in parent_dependencies[step.id]
                for target in (terminals if dependency == stage.id else [dependency])
            ]
            embedded.append(step.model_copy(update={"needs": list(dict.fromkeys(needs))}))
    return KnowledgeWorkflowDefinition.model_validate(parent.model_dump() | {"steps": embedded})
