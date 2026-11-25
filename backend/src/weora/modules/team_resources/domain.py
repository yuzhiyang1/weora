"""团队资源保留 TeamAI 文件树中的逻辑路径，工具格式由客户端生成。"""

from base64 import b64decode, b64encode
from binascii import Error as Base64Error
from dataclasses import asdict, dataclass
from pathlib import PurePosixPath
from re import fullmatch
from typing import TypedDict, cast

from yaml import SafeLoader, YAMLError, parse, safe_load
from yaml.events import AliasEvent


class BinaryFile(TypedDict):
    base64: str


type TreeFile = str | BinaryFile
type FileTree = dict[str, TreeFile]
MAX_TREE_BYTES = 2 * 1024 * 1024


class ResourceError(Exception):
    """资源错误保留可向 CLI 和控制台呈现的原因。"""

    def __init__(self, code: str, message: str, status: int = 422) -> None:
        super().__init__(message)
        self.code, self.status = code, status


def mapping(text: str, path: str) -> dict[str, object]:
    try:
        # 先拒绝别名，再构造对象，避免配置通过重复引用无限放大。
        if any(isinstance(event, AliasEvent) for event in parse(text, Loader=SafeLoader)):
            raise YAMLError("aliases are unsupported in team resources")
        value: object = safe_load(text)
    except (YAMLError, RecursionError) as exc:
        raise ResourceError("INVALID_RESOURCE_CONFIG", f"无法解析 {path}。") from exc
    if value is None:
        return {}
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return cast(dict[str, object], value)
    raise ResourceError("INVALID_RESOURCE_CONFIG", f"{path} 必须是 YAML 映射。")


def resource_mapping(value: object, path: str) -> dict[str, object]:
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return cast(dict[str, object], value)
    raise ResourceError("INVALID_RESOURCE_CONFIG", f"{path} 的配置结构无效。")


def strings(value: object, path: str) -> list[str]:
    if isinstance(value, list) and all(isinstance(item, str) and item for item in value):
        return cast(list[str], value)
    raise ResourceError("INVALID_RESOURCE_CONFIG", f"{path} 必须是非空字符串的列表。")


_CONFIG_PATHS = {"teamai.yaml", "manifest/projects.yaml", "manifest/roles.yaml", "manifest/members.yaml", "tags.yaml"}
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def validate_path(path: str) -> None:
    segments = path.split("/")
    if (
        len(path) > 320
        or (path not in _CONFIG_PATHS and not path.startswith(("rules/", "docs/", "learnings/")))
        or any(
            not part
            or part in {".", ".."}
            or part[-1] in {".", " "}
            or part.split(".")[0].upper() in _RESERVED
            or any(ord(character) < 32 or character in '\\:*?"<>|' for character in part)
            for part in segments
        )
    ):
        raise ResourceError("INVALID_RESOURCE_PATH", "资源路径无效，请使用团队源内的相对路径。")


def frontmatter(text: str, path: str) -> tuple[dict[str, object], str]:
    lines = text.removeprefix("\ufeff").splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return {}, text.strip()
    end = next((i for i, line in enumerate(lines[1:], 1) if line.strip() == "---"), None)
    if end is None:
        raise ResourceError("INVALID_RESOURCE_CONFIG", f"{path} 的开头配置缺少结束标记。")
    header = "".join(lines[1:end])
    # TeamAI 允许 paths 使用未加引号的 glob，YAML 会把它误识别成别名。
    normalized = []
    for line in header.splitlines():
        key, separator, value = line.partition(":")
        if separator and key in {"paths", "globs"} and value.strip().startswith("*"):
            line = f"{key}: '{value.strip().replace(chr(39), chr(39) * 2)}'"
        normalized.append(line)
    return mapping("\n".join(normalized), path), "".join(lines[end + 1 :]).strip()


def text_file(content: TreeFile, path: str) -> str:
    if isinstance(content, str):
        return content
    raise ResourceError("RESOURCE_TEXT_REQUIRED", f"{path} 必须是 UTF-8 文本。")


def file_size(content: TreeFile, path: str) -> int:
    if isinstance(content, str):
        try:
            return len(content.encode("utf-8"))
        except UnicodeEncodeError as exc:
            raise ResourceError("RESOURCE_TEXT_REQUIRED", f"{path} 必须是 UTF-8 文本。") from exc
    encoded = content["base64"]
    if len(encoded) > 4 * ((MAX_TREE_BYTES + 2) // 3):
        raise ResourceError("RESOURCE_TREE_LIMIT", "团队源不能超过 512 个文件或 2 MiB。", 413)
    try:
        data = b64decode(encoded, validate=True)
    except (ValueError, Base64Error) as exc:
        raise ResourceError("RESOURCE_BINARY_INVALID", f"{path} 的二进制内容无效。") from exc
    # 同一段字节只允许一种表示，修订比较和冲突判断可直接使用文件树。
    if b64encode(data).decode("ascii") != encoded:
        raise ResourceError("RESOURCE_BINARY_INVALID", f"{path} 的二进制内容无效。")
    return len(data)


@dataclass(frozen=True, slots=True)
class TeamRule:
    name: str
    path: str
    namespace: str | None
    description: str
    body: str
    paths: list[str]
    tags: list[str]
    assets: FileTree
    cursor_paths: list[str] | None


def rule_paths(value: object) -> list[str]:
    if isinstance(value, list):
        return [pattern for item in value if (pattern := str(item).strip())]
    if not isinstance(value, str):
        return []
    # 逗号分隔形式只切最外层，保留 src/{main,test}/** 一类 glob。
    parts = []
    depth, start = 0, 0
    for idx, char in enumerate(value):
        if char == "{":
            depth += 1
        elif char == "}":
            depth = max(0, depth - 1)
        elif char == "," and depth == 0:
            parts.append(value[start:idx].strip())
            start = idx + 1
    parts.append(value[start:].strip())
    return [part for part in parts if part]


def manifests(files: FileTree, kind: str) -> list[dict[str, object]]:
    manifest_path = f"manifest/{kind}.yaml"
    values = mapping(text_file(files.get(manifest_path, ""), manifest_path), manifest_path).get(kind, [])
    if not isinstance(values, list):
        raise ResourceError("INVALID_RESOURCE_CONFIG", f"{manifest_path} 的 {kind} 必须是列表。")
    results: list[dict[str, object]] = []
    ids: set[str] = set()
    for value in values:
        entry = resource_mapping(value, manifest_path)
        key = entry.get("id")
        if not isinstance(key, str) or not fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", key) or key.casefold() in ids:
            raise ResourceError("INVALID_RESOURCE_CONFIG", f"{manifest_path} 包含无效或重复的标识。")
        ids.add(key.casefold())
        resources = resource_mapping(entry.get("resources", {}), manifest_path)
        results.append(
            {
                "id": key,
                "name": str(entry.get("name") or key),
                "knowledge": strings(resources.get("knowledge", []), manifest_path),
            }
        )
    return results


def member_roles(files: FileTree, user_id: str, email: str) -> list[str]:
    path = "manifest/members.yaml"
    configuration = mapping(text_file(files.get(path, ""), path), path)
    assignments = resource_mapping(configuration.get("members", {}), path)
    default = strings(configuration.get("default", []), path)
    for value in assignments.values():
        strings(value, path)
    # Weora 用不可变成员 ID 或其账号邮箱承接 TeamAI 的 Git 用户标识。
    normalized = {key.casefold(): value for key, value in assignments.items()}
    if len(normalized) != len(assignments):
        raise ResourceError("INVALID_RESOURCE_CONFIG", f"{path} 包含重复的成员标识。")
    return strings(normalized.get(user_id.casefold(), normalized.get(email.casefold(), default)), path)


def catalog(files: FileTree) -> dict[str, object]:
    language = mapping(text_file(files.get("teamai.yaml", ""), "teamai.yaml"), "teamai.yaml").get("language")
    tagged = resource_mapping(
        mapping(text_file(files.get("tags.yaml", ""), "tags.yaml"), "tags.yaml").get("rules", {}), "tags.yaml"
    )
    folders = sorted(
        (path.removesuffix("RULE.md") for path in files if path.startswith("rules/") and path.endswith("/RULE.md")),
        key=lambda folder: (len(folder), folder),
    )
    rules: dict[str, TeamRule] = {}
    for path, content in sorted(files.items()):
        if not path.startswith("rules/") or not path.endswith(".md") or path.endswith(".draft.md"):
            continue
        parent = next((folder for folder in folders if path.startswith(folder)), None)
        if parent is not None and path != parent + "RULE.md":
            continue
        name = parent[6:-1] if parent else path[6:-3]
        if not name or name.casefold() in rules:
            raise ResourceError("DUPLICATE_RULE", f"规则 {name} 同时以文件和目录定义。")
        header, body = frontmatter(text_file(content, path), path)
        if (
            parent
            and isinstance(language, str)
            and (translation := files.get(f"{parent}RULE.{language}.md")) is not None
        ):
            _, body = frontmatter(text_file(translation, path), path)
        paths = rule_paths(header.get("paths"))
        cursor_paths = rule_paths(header.get("globs")) if header.get("paths") is None and "globs" in header else None
        namespace = name.split("/", 1)[0] if "/" in name else None
        assets = {p[len(parent) :]: data for p, data in files.items() if parent and p.startswith(parent)}
        rules[name.casefold()] = TeamRule(
            name,
            path,
            namespace,
            str(header.get("description") or ""),
            body,
            paths,
            strings(tagged.get(name, []), "tags.yaml"),
            assets,
            cursor_paths,
        )
    return {
        "rules": [asdict(rule) for rule in rules.values()],
        "projects": manifests(files, "projects"),
        "roles": manifests(files, "roles"),
        "members_configured": "manifest/members.yaml" in files,
    }


def validate_tree(files: FileTree) -> None:
    if len(files) > 512 or sum(file_size(content, path) for path, content in files.items()) > MAX_TREE_BYTES:
        raise ResourceError("RESOURCE_TREE_LIMIT", "团队源不能超过 512 个文件或 2 MiB。", 413)
    seen: set[str] = set()
    rule_folders = {
        path.removesuffix("RULE.md") for path in files if path.startswith("rules/") and path.endswith("/RULE.md")
    }
    for path, content in files.items():
        validate_path(path)
        if not isinstance(content, str) and (
            not any(path.startswith(folder) for folder in rule_folders)
            or PurePosixPath(path).name.startswith("RULE.")
            and path.endswith(".md")
        ):
            text_file(content, path)
        key = path.casefold()
        if key in seen:
            raise ResourceError("RESOURCE_PATH_CONFLICT", "资源路径存在大小写冲突。")
        seen.add(key)
    if any(str(parent) in seen for path in seen for parent in PurePosixPath(path).parents):
        raise ResourceError("RESOURCE_PATH_CONFLICT", "同一路径不能同时是文件和目录。")
    member_roles(files, "", "")
    catalog(files)
