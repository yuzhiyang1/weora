"""帮助与 Schema 共用命令声明，未实现的业务能力不进入发现列表。"""

from dataclasses import dataclass

type JsonValue = str | int | float | bool | None | list[JsonValue] | dict[str, JsonValue]


@dataclass(frozen=True)
class Argument:
    """当前仅需要一个可选的命令路径数组，业务参数随真实能力扩展。"""

    name: str
    description: str


@dataclass(frozen=True)
class Command:
    name: str
    description: str
    arguments: tuple[Argument, ...] = ()
    side_effects: bool = False

    def describe(self) -> dict[str, JsonValue]:
        properties: dict[str, JsonValue] = {
            argument.name: {
                "type": "array",
                "items": {"type": "string"},
                "description": argument.description,
                "default": [],
            }
            for argument in self.arguments
        }
        return {
            "name": self.name,
            "description": self.description,
            "kind": "business" if self.side_effects else "utility",
            "side_effects": self.side_effects,
            "input_schema": {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "properties": properties,
                "additionalProperties": False,
            },
        }


COMMANDS = (
    Command("version", "查看 CLI 版本与输出协议版本"),
    Command("commands", "列出当前已实现的命令"),
    Command("schema", "查看全部命令或指定命令的输入 Schema", (Argument("command", "命令路径，省略时返回全部命令"),)),
    Command("collab", "配对本机、查找会话、发送和领取协作消息", side_effects=True),
    Command("skill", "上传、绑定、同步和统计团队 Skill", side_effects=True),
    Command("context", "检索、贡献和维护项目知识", side_effects=True),
    Command("ci", "在持续集成中从代码评审提炼团队知识", side_effects=True),
    Command("auth", "在浏览器登录、验证身份或退出", side_effects=True),
    Command("login", "在浏览器登录 Weora CLI", side_effects=True),
    Command("logout", "注销当前 CLI 设备凭据", side_effects=True),
    Command("init", "将当前检出或 Codex 用户环境接入团队资源", side_effects=True),
    Command("projects", "查看或选择分发项目", side_effects=True),
    Command("roles", "查看或选择知识分发角色", side_effects=True),
    Command("tags", "查看或更新规则标签订阅", side_effects=True),
    Command("pull", "同步团队规则和本地 Codex 指令", side_effects=True),
    Command("push", "提交团队源变更请求", side_effects=True),
    Command("list", "列出团队源的规则"),
    Command("status", "查看本地规则与使用范围"),
    Command("doctor", "检查本地规则投递"),
    Command("recall", "检索当前作用域已同步的规则"),
    Command("import", "分类和导入本机工具的规则", side_effects=True),
    Command("remove", "提交团队规则移除请求", side_effects=True),
    Command("review", "查看或合并候选分支", side_effects=True),
    Command("uninstall", "解除团队规则安装并清理已管理的内容", side_effects=True),
)
