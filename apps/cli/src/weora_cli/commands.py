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
    Command("auth", "在浏览器登录、验证身份或退出", side_effects=True),
    Command("login", "在浏览器登录 Weora CLI", side_effects=True),
    Command("logout", "注销当前 CLI 设备凭据", side_effects=True),
)
