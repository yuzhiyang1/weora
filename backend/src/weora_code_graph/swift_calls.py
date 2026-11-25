"""用 Swift 语法树提取同模块调用的声明和词法证据。"""

import re
from dataclasses import dataclass, field
from typing import TypedDict

from tree_sitter import Language, Node, Parser
from tree_sitter_swift import language

_SWIFT = Language(language())
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z_0-9]{0,119}")
_TYPES = {"class_declaration", "protocol_declaration"}
_TYPE_BODIES = {"class_body", "enum_class_body", "protocol_body"}
_FUNCTION_SCOPES = {
    "function_declaration", "init_declaration", "deinit_declaration",
    "subscript_declaration", "lambda_literal",
}


class SwiftDeclaration(TypedDict):
    name: str
    kind: str
    visible: bool


class SwiftMethod(TypedDict):
    owner: str
    name: str
    visible: bool


class SwiftCall(TypedDict):
    receiver: str
    symbol: str
    line: int
    enclosed_by_type: bool


class SwiftCalls(TypedDict):
    declarations: list[SwiftDeclaration]
    methods: list[SwiftMethod]
    members: list[str]
    calls: list[SwiftCall]


@dataclass(slots=True)
class Scope:
    parent: "Scope | None" = None
    enclosed_by_type: bool = False
    bindings: dict[str, int] = field(default_factory=dict)

    def child(self, *, inside_type: bool = False) -> "Scope":
        return Scope(self, self.enclosed_by_type or inside_type)

    def bind(self, names: list[str], start: int) -> None:
        for name in names:
            self.bindings[name] = min(self.bindings.get(name, start), start)

    def shadows(self, name: str, position: int) -> bool:
        current: Scope | None = self
        while current is not None:
            start = current.bindings.get(name)
            if start is not None and start <= position:
                return True
            current = current.parent
        return False


def _text(node: Node | None) -> str:
    return (node.text or b"").decode() if node is not None else ""


def _identifier(node: Node | None) -> str:
    value = _text(node).strip("\x60")
    return value if _IDENTIFIER.fullmatch(value) and value != "_" else ""


def _names(nodes: list[Node]) -> list[str]:
    # Swift 的参数类型和值名可能共用 name 字段。只把声明的标识符加入词法作用域。
    return [
        name for child in nodes if child.type in {"simple_identifier", "type_identifier"}
        and (name := _identifier(child))
    ]


def _pattern_names(node: Node | None, *, explicit: bool = False) -> list[str]:
    if node is None:
        return []
    labels = []
    pending = [(node, not explicit)]
    while pending:
        part, bind_plain = pending.pop()
        children = part.named_children
        declared = any(child.type == "value_binding_pattern" for child in children)
        bind_plain |= declared
        labels.extend(_names(part.children_by_field_name("bound_identifier")))
        # 枚举构造器的标识不在绑定列表内。
        include_direct = bind_plain and (len(children) == 1 or (part.type == "pattern" and declared))
        include_direct &= not any(child.type == "." for child in part.children)
        for child in children:
            if child.type in {"pattern", "switch_pattern"}:
                pending.append((child, bind_plain))
            elif include_direct and child.type == "simple_identifier" and (name := _identifier(child)):
                labels.append(name)
    return list(dict.fromkeys(labels))


def _modifiers(node: Node) -> set[str]:
    group = next((child for child in node.named_children if child.type == "modifiers"), None)
    if group is None:
        return set()
    return {
        _text(child).strip() for child in group.named_children
        if child.type in {"visibility_modifier", "property_modifier"}
    }


def _type_name(node: Node) -> str:
    name = node.child_by_field_name("name")
    if name is not None and name.type == "user_type":
        parts = name.named_children
        if len(parts) == 1 and parts[0].type == "type_identifier":
            return _identifier(parts[0])
        return ""
    return _identifier(name)


def _frame_bindings(node: Node) -> list[str]:
    names = []
    for child in node.named_children:
        if child.type == "parameter":
            names.extend(_names(child.children_by_field_name("name")))
        elif child.type == "capture_list":
            for item in child.named_children:
                names.extend(_names(item.children_by_field_name("name")))
        elif child.type == "lambda_function_type":
            for group in child.named_children:
                if group.type == "lambda_function_type_parameters":
                    for item in group.named_children:
                        names.extend(_names(item.children_by_field_name("name")))
        elif child.type == "type_parameters":
            for item in child.named_children:
                names.extend(_names(item.named_children[:1]))
    return names


def _declaration_names(node: Node) -> list[str]:
    if node.type == "import_declaration":
        identifier = next((child for child in node.named_children if child.type == "identifier"), None)
        if identifier is None:
            return []
        parts = identifier.named_children
        selective = any(
            child.type in {"class", "struct", "enum", "protocol", "typealias", "func", "var", "let"}
            for child in node.children
        )
        return _names(parts[-1:] if selective else parts[:1])
    if node.type in _TYPES:
        # 扩展没有引入新的类型声明。
        return [] if any(child.type == "extension" for child in node.children) else _names(
            node.children_by_field_name("name"),
        )
    if node.type == "property_declaration":
        return [name for part in node.children_by_field_name("name") for name in _pattern_names(part)]
    if node.type in {"function_declaration", "protocol_function_declaration", "typealias_declaration", "enum_entry"}:
        return _names(node.children_by_field_name("name")[:1])
    return []


def _condition_scope(node: Node, outer: Scope) -> Scope:
    guarded = outer.child()
    children = node.children
    for index, child in enumerate(children):
        names = _names([child]) if node.field_name_for_child(index) == "bound_identifier" else []
        if child.type == "pattern" and node.field_name_for_child(index) == "condition":
            names.extend(_pattern_names(child))
        if not names:
            continue
        # 绑定在初始化表达式后生效，后续逗号分隔的条件可以看到此名称。
        pending = (later for later in children[index + 1:] if later.type in {",", "{", "else"})
        boundary = next(pending, node)
        guarded.bind(names, boundary.start_byte)
        if node.type == "guard_statement":
            outer.bind(names, node.end_byte)
    return guarded


def _children(node: Node, scope: Scope) -> list[tuple[Node, Scope]]:
    if node.type in _FUNCTION_SCOPES:
        frame = scope.child()
        body = next(
            (child for child in node.named_children if child.type in {"statements", "function_body"}),
            node,
        )
        frame.bind(_frame_bindings(node), body.start_byte)
        return [(child, frame) for child in node.named_children]
    if node.type in _TYPES:
        inner = scope.child(inside_type=True)
        inner.bind(_frame_bindings(node), node.start_byte)
        return [(child, inner) for child in node.named_children]
    if node.type in {"if_statement", "guard_statement", "while_statement"}:
        guarded = _condition_scope(node, scope)
        children = []
        passed_else = False
        for child in node.named_children:
            passed_else |= child.type == "else"
            children.append((child, scope if passed_else else guarded))
        return children
    if node.type in {"for_statement", "switch_entry", "catch_block"}:
        names = _pattern_names(node.child_by_field_name("item"))
        if node.type == "catch_block":
            error = node.child_by_field_name("error")
            names.extend(_pattern_names(error, explicit=True) if error else ["error"])
        if node.type == "switch_entry":
            for part in node.named_children:
                if part.type == "switch_pattern":
                    names.extend(_pattern_names(part, explicit=True))
        inner = scope.child()
        inner.bind(names, node.start_byte)
        return [
            (child, inner if child.type in {"statements", "where_clause"} else scope)
            for child in node.named_children
        ]
    return [(child, scope) for child in node.named_children]


def _callee(node: Node) -> tuple[str, str]:
    names = node.named_children
    if not names:
        return "", ""
    target = names[0]
    if target.type == "simple_identifier":
        return "", _identifier(target)
    if target.type == "navigation_expression":
        owner = target.child_by_field_name("target")
        member = target.child_by_field_name("suffix")
        if owner is not None and owner.type == "simple_identifier" and member is not None:
            return _identifier(owner), _identifier(member.child_by_field_name("suffix"))
    if node.type == "constructor_expression":
        owner = node.child_by_field_name("constructed_type")
        if owner is not None and owner.type == "user_type" and owner.named_children:
            # Swift 的泛型调用可能被语法树归为构造器。索引阶段核对真实声明类型。
            parts = [child for child in owner.named_children if child.type == "type_identifier"]
            if len(parts) == 1:
                return "", _identifier(parts[0])
    return "", ""


def swift_calls(text: str) -> SwiftCalls:
    root = Parser(_SWIFT).parse(text.encode()).root_node
    declarations: list[SwiftDeclaration] = []
    methods: list[SwiftMethod] = []
    members: set[str] = set()
    candidates: list[tuple[Node, Scope]] = []
    pending = [(root, Scope())]
    while pending:
        node, scope = pending.pop()
        if node.is_error:
            continue
        if node.type == "statements":
            scope = scope.child()
        parent = node.parent
        names = _declaration_names(node)
        if names and parent is not None:
            top_level = parent.type == "source_file"
            type_member = parent.type in _TYPE_BODIES
            if not type_member:
                activation = node.end_byte if node.type == "property_declaration" and not top_level else 0
                scope.bind(names, activation)
            visible = not (_modifiers(node) & {"private", "fileprivate"})
            if top_level and node.type != "import_declaration" and len(declarations) < 500:
                kind = {
                    "class_declaration": "type", "function_declaration": "function",
                }.get(node.type, "value")
                declarations.extend({"name": name, "kind": kind, "visible": visible} for name in names)
            if type_member:
                members.update(names)
                owner = parent.parent
                if (
                    owner is not None and owner.parent is not None and owner.parent.type == "source_file"
                    and (type_name := _type_name(owner)) and _modifiers(node) & {"static", "class"}
                    and len(methods) < 500
                ):
                    public_owner = not (_modifiers(owner) & {"private", "fileprivate"})
                    methods.extend(
                        {"owner": type_name, "name": name, "visible": visible and public_owner} for name in names
                    )
        if node.type in {"call_expression", "constructor_expression"} and not node.has_error and len(candidates) < 500:
            candidates.append((node, scope))
        pending.extend(reversed(_children(node, scope)))

    calls: list[SwiftCall] = []
    for node, scope in candidates:
        receiver, symbol = _callee(node)
        if symbol and not scope.shadows(receiver or symbol, node.start_byte):
            calls.append({
                "receiver": receiver, "symbol": symbol, "line": node.start_point.row + 1,
                "enclosed_by_type": scope.enclosed_by_type,
            })
    return {
        "declarations": declarations[:500], "methods": methods[:500],
        "members": sorted(members)[:500], "calls": calls[:300],
    }
