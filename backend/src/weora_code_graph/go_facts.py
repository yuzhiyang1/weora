"""解析 Go 声明、模块导入、调用及可确认的路由注册。"""

import re
from dataclasses import dataclass, field
from typing import TypedDict

from tree_sitter import Language, Node, Parser
from tree_sitter_go import language

_GO = Language(language())
_IMPORT = re.compile(r"(?:\./|\.\./)*[a-zA-Z0-9_.~-]+(?:/[a-zA-Z0-9_.~-]+)*")
_MODULE = re.compile(r'^module[ \t]+(?:"([^"\r\n]+)"|([^\s"]+))[ \t]*(?://.*)?$')
_ROUTE = re.compile(r"(?:(GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS) )?(/[a-zA-Z0-9_./{}:-]{0,109})")
_METHODS = {"Get", "Post", "Put", "Delete", "Patch", "Head", "Options"}
_FUNCTIONS = {"function_declaration", "method_declaration", "func_literal"}
_BLOCKS = {
    "block", "if_statement", "for_statement", "expression_switch_statement", "type_switch_statement",
    "select_statement", "expression_case", "type_case", "default_case", "communication_case",
}


class GoImport(TypedDict):
    alias: str
    path: str


class GoDeclaration(TypedDict):
    name: str
    kind: str
    receiver: str
    line: int


class GoCall(TypedDict):
    qualifier: str
    receiver_type: str
    symbol: str
    line: int


class GoFacts(TypedDict):
    package: str
    imports: list[GoImport]
    declarations: list[GoDeclaration]
    calls: list[GoCall]


@dataclass(slots=True)
class Scope:
    parent: "Scope | None" = None
    bindings: dict[str, tuple[int, str]] = field(default_factory=dict)

    def bind(self, names: list[str], start: int, type_name: str = "") -> None:
        for name in names:
            if name and name != "_":
                self.bindings.setdefault(name, (start, type_name))

    def binding(self, name: str, position: int, *, include_package: bool = True) -> tuple[int, str] | None:
        current: Scope | None = self
        while current is not None and (include_package or current.parent is not None):
            if (binding := current.bindings.get(name)) is not None and binding[0] <= position:
                return binding
            current = current.parent
        return None


def _text(node: Node | None) -> str:
    return (node.text or b"").decode() if node is not None else ""


def _field(node: Node, name: str) -> str:
    return _text(node.child_by_field_name(name))


def _literal(node: Node | None) -> str:
    if node is None or node.type not in {"interpreted_string_literal", "raw_string_literal"}:
        return ""
    value = _text(node)[1:-1]
    return value if "\\" not in value and "\n" not in value and "\r" not in value else ""


def module_directive(text: str) -> tuple[str, int]:
    markers = []
    for line, content in enumerate(text.lstrip("\ufeff").splitlines(), 1):
        if (
            (match := _MODULE.fullmatch(content.strip())) and _IMPORT.fullmatch(value := match[1] or match[2])
            and len(value) <= 480 and not any(part in {".", ".."} for part in value.split("/"))
        ):
            markers.append((value, line))
    return markers[0] if len(markers) == 1 else ("", 0)


def _names(node: Node | None) -> list[str]:
    if node is None:
        return []
    if node.type == "identifier":
        return [_text(node)]
    return [_text(child) for child in node.named_children if child.type == "identifier"]


def _named_type(node: Node | None) -> str:
    if node is not None and node.type == "pointer_type" and node.named_children:
        node = node.named_children[0]
    if node is not None and node.type == "generic_type":
        node = node.child_by_field_name("type")
    return _text(node) if node is not None and node.type == "type_identifier" else ""


def _parameters(scope: Scope, node: Node) -> None:
    for role in ("receiver", "parameters", "result", "type_parameters"):
        if group := node.child_by_field_name(role):
            for item in group.named_children:
                if item.type not in {"parameter_declaration", "variadic_parameter_declaration",
                                     "type_parameter_declaration"}:
                    continue
                named = [_text(child) for child in item.children_by_field_name("name")]
                scope.bind(named, node.start_byte, _named_type(item.child_by_field_name("type")))


def _scope(node: Node, scope: Scope) -> Scope:
    kind = node.type
    if kind in _FUNCTIONS:
        child = Scope(scope)
        _parameters(child, node)
        return child
    if kind in _BLOCKS:
        child = Scope(scope)
        # 类型 switch 的变量从各 case 开始生效，值表达式仍使用原有词法绑定。
        if kind in {"type_case", "default_case"} and node.parent is not None:
            child.bind(_names(node.parent.child_by_field_name("alias")), node.start_byte)
        return child
    if (
        kind in {"short_var_declaration", "range_clause", "receive_statement"}
        and any(child.type == ":=" for child in node.children)
    ):
        scope.bind(_names(node.child_by_field_name("left")), node.end_byte)
    elif kind in {"var_spec", "const_spec", "type_spec", "type_alias"}:
        start = node.start_byte if kind in {"type_spec", "type_alias"} else node.end_byte
        scope.bind(
            [_text(child) for child in node.children_by_field_name("name")], start,
            _named_type(node.child_by_field_name("type")) if kind == "var_spec" else "",
        )
    return scope


def _target(node: Node) -> tuple[str, str]:
    expression = node.child_by_field_name("function") or node.child_by_field_name("type")
    # 泛型调用和类型转换存在语法歧义。最终只连接确有函数或方法声明的目标。
    while expression is not None:
        if expression.type == "index_expression":
            expression = expression.child_by_field_name("operand")
        elif expression.type == "generic_type":
            expression = expression.child_by_field_name("type")
        elif expression.type == "parenthesized_expression" and expression.named_children:
            expression = expression.named_children[0]
        else:
            break
    if expression is None:
        return "", ""
    if expression.type in {"identifier", "type_identifier"}:
        return "", _text(expression)
    if expression.type == "qualified_type":
        return _field(expression, "package"), _field(expression, "name")
    if expression.type == "selector_expression":
        owner = expression.child_by_field_name("operand")
        if owner is not None and owner.type == "identifier":
            return _text(owner), _field(expression, "field")
    return "", ""


def _declaration(node: Node) -> GoDeclaration | None:
    name = _field(node, "name")
    if not name:
        return None
    receiver = ""
    if node.type == "method_declaration":
        group = node.child_by_field_name("receiver")
        if group is not None and group.named_children:
            receiver = _named_type(group.named_children[0].child_by_field_name("type"))
        if not receiver:
            return None
        kind = "method"
    elif node.type == "function_declaration":
        kind = "function"
    elif node.type in {"type_spec", "type_alias"}:
        type_node = node.child_by_field_name("type")
        kind = "interface" if type_node is not None and type_node.type == "interface_type" else "type"
    else:
        return None
    return {"name": name, "kind": kind, "receiver": receiver, "line": node.start_point.row + 1}


def go_facts(text: str) -> GoFacts:
    root = Parser(_GO).parse(text.encode()).root_node
    package = ""
    imports: list[GoImport] = []
    declarations: list[GoDeclaration] = []
    calls: list[GoCall] = []
    module_scope = Scope()
    # 包级变量在整个包作用域内生效，包含文本上位于声明之前的调用。
    for item in root.named_children:
        if item.type == "package_clause":
            package = next((_text(child) for child in item.named_children if child.type == "package_identifier"), "")
        if item.type in {"var_declaration", "const_declaration", "type_declaration"}:
            specs = list(item.named_children)
            while specs:
                node = specs.pop()
                if node.type.endswith("_list"):
                    specs.extend(node.named_children)
                    continue
                module_scope.bind(
                    [_text(child) for child in node.children_by_field_name("name")], 0,
                    _named_type(node.child_by_field_name("type")) if node.type == "var_spec" else "",
                )
    candidates: list[tuple[Node, Scope]] = []
    pending = [(root, module_scope)]
    while pending:
        node, scope = pending.pop()
        if node.is_error:
            continue
        kind = node.type
        scope = _scope(node, scope)
        if kind == "import_spec" and len(imports) < 120:
            path = _literal(node.child_by_field_name("path"))
            if len(path) <= 480 and _IMPORT.fullmatch(path):
                imports.append({"alias": _field(node, "name"), "path": path})
        if (
            kind in {"method_declaration", "function_declaration", "type_spec", "type_alias"}
            and len(declarations) < 500 and (declaration := _declaration(node))
        ):
            declarations.append(declaration)
        if kind in {"call_expression", "type_conversion_expression"} and not node.has_error and len(candidates) < 450:
            candidates.append((node, scope))
        pending.extend((child, scope) for child in reversed(node.named_children))

    http_aliases = {entry["alias"] or "http" for entry in imports if entry["path"] == "net/http"}
    for node, scope in candidates:
        qualifier, symbol = _target(node)
        if not symbol:
            continue
        position = node.start_byte
        binding = scope.binding(qualifier or symbol, position)
        receiver = binding[1] if qualifier and binding else ""
        if binding is None or (receiver and scope.binding(receiver, position, include_package=False) is None):
            calls.append({
                "qualifier": qualifier, "symbol": symbol, "receiver_type": receiver,
                "line": node.start_point.row + 1,
            })
        if qualifier and (symbol in {"Handle", "HandleFunc"} or (symbol in _METHODS and qualifier not in http_aliases)):
            arguments = node.child_by_field_name("arguments")
            value = _literal(arguments.named_children[0]) if arguments is not None and arguments.named_children else ""
            if (match := _ROUTE.fullmatch(value)) and len(declarations) < 500:
                method, path = match.groups()
                verb = method or (symbol.upper() if symbol in _METHODS else "")
                declarations.append({
                    "kind": "route", "name": f"{verb} {path}".strip(), "line": node.start_point.row + 1, "receiver": "",
                })
    return {"package": package, "imports": imports, "declarations": declarations, "calls": calls[:300]}
