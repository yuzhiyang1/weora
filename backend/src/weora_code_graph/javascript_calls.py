"""从 JS/TS 语法树提取具有静态模块绑定的调用。"""

from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field
from itertools import islice
from typing import TypedDict

from tree_sitter import Language, Node, Parser
from tree_sitter_typescript import language_tsx, language_typescript

from weora_code_graph.typescript_facts import Implementation, ImportBinding, Interface, _specifier

_LANGUAGES = {False: Language(language_typescript()), True: Language(language_tsx())}
_FUNCTIONS = {
    "function_declaration", "generator_function_declaration", "function_expression", "generator_function",
    "arrow_function", "method_definition",
}
_CLASSES = {"class", "class_declaration", "abstract_class_declaration"}
_VALUE_DECLARATIONS = {
    "function_declaration", "generator_function_declaration", "function_signature", "enum_declaration",
}
_BLOCKS = {"statement_block", "for_statement", "for_in_statement", "catch_clause", "switch_body"}
_SYMBOL_TYPES = {
    "class": "class", "class_declaration": "class", "abstract_class_declaration": "class",
    "function_declaration": "function", "generator_function_declaration": "function",
    "function_signature": "function", "interface_declaration": "interface",
    "type_alias_declaration": "type", "enum_declaration": "enum",
}
_TYPE_NAMES = _CLASSES - {"class"} | {"interface_declaration", "type_alias_declaration", "enum_declaration",
                                    "internal_module"}


class JavaScriptCall(TypedDict):
    module: str
    symbol: str
    callee: str
    line: int


class JavaScriptForward(TypedDict):
    exported: str
    imported: str
    module: str
    type_only: bool


class JavaScriptBinding(ImportBinding):
    type_only: bool


@dataclass(slots=True)
class JavaScriptExports:
    callables: set[str] = field(default_factory=set)
    names: set[str] = field(default_factory=set)
    types: set[str] = field(default_factory=set)
    interfaces: list[Interface] = field(default_factory=list)
    forwards: list[JavaScriptForward] = field(default_factory=list)


@dataclass(slots=True)
class JavaScriptFacts:
    exports: JavaScriptExports
    symbols: list[tuple[str, str, int]] = field(default_factory=list)
    specifiers: list[str] = field(default_factory=list)
    import_sites: list[tuple[str, int]] = field(default_factory=list)
    bindings: list[ImportBinding] = field(default_factory=list)
    calls: list[JavaScriptCall] = field(default_factory=list)
    implementations: list[Implementation] = field(default_factory=list)


@dataclass(slots=True)
class Scope:
    parent: "Scope | None" = None
    function_boundary: bool = False
    names: set[str] = field(default_factory=set)
    types: dict[str, bool] = field(default_factory=dict)

    def type_binding(self, name: str) -> bool | None:
        current: Scope | None = self
        while current is not None:
            if name in current.types:
                return current.types[name]
            current = current.parent
        return None

    def shadows(self, name: str) -> bool:
        current: Scope | None = self
        while current is not None:
            if name in current.names:
                return True
            current = current.parent
        return False

    def function(self) -> "Scope":
        current = self
        while current.parent is not None and not current.function_boundary:
            current = current.parent
        return current


def _text(node: Node | None) -> str:
    return (node.text or b"").decode() if node is not None else ""


def _field(node: Node, name: str) -> str:
    return _text(node.child_by_field_name(name))


def _patterns(node: Node | None) -> Iterator[str]:
    pending = [node] if node is not None else []
    while pending:
        current = pending.pop()
        if current.type in {"identifier", "shorthand_property_identifier_pattern"}:
            yield _text(current)
        elif current.type in {"object_pattern", "array_pattern", "rest_pattern", "formal_parameters"}:
            pending.extend(current.named_children)
        else:
            child_field = {
                "pair_pattern": "value", "assignment_pattern": "left", "object_assignment_pattern": "left",
                "required_parameter": "pattern", "optional_parameter": "pattern",
            }.get(current.type)
            if child_field and (child := current.child_by_field_name(child_field)):
                pending.append(child)


def _type_only(node: Node) -> bool:
    # 当前语法包把 export type * 中的 type 标成独立错误节点，仍可识别这项静态声明。
    return any(child.type == "type" or (child.is_error and _text(child) == "type") for child in node.children)


def _imports(root: Node) -> dict[str, list[JavaScriptBinding]]:
    imports: dict[str, list[JavaScriptBinding]] = defaultdict(list)
    for statement in root.named_children:
        if statement.type != "import_statement" or statement.has_error:
            continue
        only_types = _type_only(statement)
        # 读取仅类型绑定供转发解析，运行时调用阶段会过滤。
        spec = _specifier(_field(statement, "source"))
        if spec is None:
            continue
        for clause in statement.named_children:
            if clause.type != "import_clause":
                continue
            for binding in clause.named_children:
                pairs: list[tuple[str, str, bool]] = []
                if binding.type == "identifier":
                    pairs.append((_text(binding), "default", only_types))
                elif binding.type == "namespace_import" and binding.named_children:
                    pairs.append((_text(binding.named_children[0]), "*", only_types))
                elif binding.type == "named_imports":
                    for item in binding.named_children:
                        if item.type == "import_specifier":
                            name = _field(item, "name")
                            pairs.append((_field(item, "alias") or name, name, only_types or _type_only(item)))
                for local, exported, type_only in pairs:
                    if local and exported:
                        imports[local].append({
                            "specifier": spec, "exported": exported, "local": local, "type_only": type_only,
                        })
    return imports


def _declarations(node: Node) -> Iterator[tuple[str, Node]]:
    if node.type in _VALUE_DECLARATIONS | _CLASSES | {"interface_declaration", "type_alias_declaration"}:
        if name := _field(node, "name"):
            yield name, node
    elif node.type in {"lexical_declaration", "variable_declaration"}:
        for child in node.named_children:
            identifier, value = child.child_by_field_name("name"), child.child_by_field_name("value")
            declaration = value if value is not None and identifier and identifier.type == "identifier" else child
            for name in _patterns(identifier):
                yield name, declaration


def _export_members(name: str, value: Node) -> Iterator[str]:
    if value.type in _FUNCTIONS | _CLASSES:
        yield name
    if value.type in _CLASSES and (body := value.child_by_field_name("body")):
        for child in body.named_children:
            if child.type == "method_definition" and any(token.type == "static" for token in child.children):
                method = child.child_by_field_name("name")
                if method is not None and method.type == "property_identifier":
                    yield f"{name}.{_text(method)}"


def _own_export(
    facts: JavaScriptExports, exported: str, value: Node, *, type_only: bool,
) -> None:
    facts.names.add(exported)
    if type_only or value.type in {"interface_declaration", "type_alias_declaration"}:
        facts.types.add(exported)
        if value.type == "interface_declaration":
            facts.interfaces.append({
                "name": _field(value, "name"), "exported": exported, "line": value.start_point.row + 1,
            })
    else:
        facts.callables.update(_export_members(exported, value))


def _forward(
    facts: JavaScriptExports, exported: str, imported: str, module: str, *, type_only: bool,
) -> None:
    if exported and imported and max(len(exported), len(imported)) <= 120 and len(facts.forwards) < 500:
        facts.forwards.append({"exported": exported, "imported": imported, "module": module, "type_only": type_only})


def _exports(root: Node, imports: dict[str, list[JavaScriptBinding]]) -> JavaScriptExports:
    top_level: dict[str, Node] = {}
    for statement in root.named_children:
        top_level.update(_declarations(statement))
        if statement.type == "export_statement" and (declaration := statement.child_by_field_name("declaration")):
            top_level.update(_declarations(declaration))

    facts = JavaScriptExports()
    for statement in root.named_children:
        if statement.type != "export_statement":
            continue
        source_literal = _field(statement, "source")
        source = _specifier(source_literal)
        if source_literal and not source:
            continue
        only_types = _type_only(statement)
        default = any(child.type == "default" for child in statement.children)
        if declaration := statement.child_by_field_name("declaration"):
            for name, value in _declarations(declaration):
                _own_export(facts, "default" if default else name, value, type_only=only_types)
        elif expression := statement.child_by_field_name("value"):
            if default and expression.type == "identifier":
                _local_export(facts, "default", _text(expression), top_level, imports, only_types=only_types)
            elif default:
                _own_export(facts, "default", expression, type_only=only_types)
        elif source and any(child.type == "*" for child in statement.children):
            _forward(facts, "*", "*", source, type_only=only_types)
        else:
            for node in statement.named_children:
                if source and node.type == "namespace_export" and node.named_children:
                    _forward(facts, _text(node.named_children[0]), "*", source, type_only=only_types)
                if node.type != "export_clause":
                    continue
                for specifier in node.named_children:
                    imported = _field(specifier, "name")
                    exported = _field(specifier, "alias") or imported
                    type_only = only_types or _type_only(specifier)
                    if source:
                        _forward(facts, exported, imported, source, type_only=type_only)
                    else:
                        _local_export(facts, exported, imported, top_level, imports, only_types=type_only)
    return facts


def _local_export(
    facts: JavaScriptExports, exported: str, name: str, local: dict[str, Node],
    imports: dict[str, list[JavaScriptBinding]], *, only_types: bool,
) -> None:
    if target := local.get(name):
        _own_export(facts, exported, target, type_only=only_types)
        return
    bindings = imports.get(name, [])
    if len(bindings) == 1:
        binding = bindings[0]
        _forward(facts, exported, binding["exported"], binding["specifier"],
                 type_only=only_types or binding["type_only"])
    elif exported:
        facts.names.add(exported)
        if only_types:
            facts.types.add(exported)


def _scope_for(node: Node, scope: Scope) -> Scope:
    kind = node.type
    if kind in _VALUE_DECLARATIONS or kind in {"class_declaration", "abstract_class_declaration"}:
        scope.names.add(_field(node, "name"))
    if kind in _FUNCTIONS or kind == "class_static_block":
        current = Scope(scope, function_boundary=True)
        current.names.update(_patterns(node.child_by_field_name("parameters") or node.child_by_field_name("parameter")))
        if kind != "method_definition":
            current.names.add(_field(node, "name"))
        return current
    if kind == "variable_declarator":
        owner = scope.function() if node.parent is not None and node.parent.type == "variable_declaration" else scope
        owner.names.update(_patterns(node.child_by_field_name("name")))
    if kind in _CLASSES | _BLOCKS:
        current = Scope(scope)
        if kind == "for_in_statement":
            declaration = _field(node, "kind")
            if declaration in {"var", "let", "const"}:
                owner = scope.function() if declaration == "var" else current
                owner.names.update(_patterns(node.child_by_field_name("left")))
        elif kind == "catch_clause":
            current.names.update(_patterns(node.child_by_field_name("parameter")))
        elif kind in _CLASSES:
            current.names.add(_field(node, "name"))
        return current
    return scope


def _callee(node: Node) -> tuple[str, str]:
    callee = node.child_by_field_name("function") or node.child_by_field_name("constructor")
    if callee is None:
        return "", ""
    if callee.type == "identifier":
        return _text(callee), ""
    if callee.type == "member_expression":
        receiver = callee.child_by_field_name("object")
        member = callee.child_by_field_name("property")
        if (
            receiver is not None and receiver.type == "identifier"
            and member is not None and member.type == "property_identifier"
        ):
            return _text(receiver), _text(member)
    return "", ""


def _type_name(node: Node | None) -> str:
    suffix: list[str] = []
    while node is not None and len(suffix) < 120:
        if node.type in {"type_identifier", "identifier", "property_identifier"}:
            return ".".join([_text(node), *reversed(suffix)])
        if node.type == "generic_type":
            node = node.child_by_field_name("name")
            continue
        if node.type == "nested_type_identifier":
            owner, member = "module", "name"
        elif node.type == "nested_identifier":
            owner, member = "object", "property"
        else:
            break
        if not (segment := _field(node, member)):
            break
        suffix.append(segment)
        node = node.child_by_field_name(owner)
    return ""


def _implementation_names(node: Node) -> Iterator[str]:
    heritage = next((item for item in node.named_children if item.type == "class_heritage"), None)
    if heritage is None:
        return
    for clause in heritage.named_children:
        if clause.type == "implements_clause" and not clause.has_error:
            for candidate in clause.named_children:
                if len(name := _type_name(candidate)) in range(1, 241):
                    yield name


def _module_reference(node: Node) -> str | None:
    if node.type in {"import_statement", "export_statement"}:
        if node.parent is None or node.parent.type != "program":
            return None
        if literal := _field(node, "source"):
            return _specifier(literal)
        for child in node.named_children:
            if child.type == "import_require_clause":
                return _specifier(_field(child, "source"))
        return None
    if node.type != "call_expression" or node.has_error:
        return None
    function = node.child_by_field_name("function")
    arguments = node.child_by_field_name("arguments")
    if function is None or arguments is None:
        return None
    values = [child for child in arguments.named_children if not child.is_extra]
    single_require = function.type == "identifier" and _text(function) == "require" and len(values) == 1
    if values and values[0].type == "string" and (function.type == "import" or single_require):
        return _specifier(_text(values[0]))
    return None


def javascript_facts(text: str, *, jsx: bool, typescript: bool) -> JavaScriptFacts:
    root = Parser(_LANGUAGES[jsx]).parse(text.encode()).root_node
    imports = _imports(root)
    bindings = {name: values[0] for name, values in imports.items() if len(values) == 1 and not values[0]["type_only"]}
    facts = JavaScriptFacts(_exports(root, imports))
    raw_bindings = (item for rows in imports.values() for item in rows)
    facts.bindings = [
        {"local": row["local"], "exported": row["exported"], "specifier": row["specifier"]}
        for row in islice(raw_bindings, 300)
    ]
    calls: list[tuple[Node, Scope, str, str]] = []
    sites: list[tuple[Scope, str, str, int]] = []
    specifiers: dict[str, None] = {}
    pending = [(root, Scope(function_boundary=True))]
    while pending:
        node, outer = pending.pop()
        if node.is_error:
            continue
        name = _field(node, "name") if node.type in _SYMBOL_TYPES or node.type in _TYPE_NAMES else ""
        if name and (kind := _SYMBOL_TYPES.get(node.type)) and len(facts.symbols) < 200:
            facts.symbols.append((kind, name[:120], node.start_point.row + 1))
        if typescript and name and node.type in _TYPE_NAMES:
            outer.types[name] = node.type == "interface_declaration" or outer.types.get(name, False)
        scope = _scope_for(node, outer)
        if typescript and scope is not outer and (parameters := node.child_by_field_name("type_parameters")):
            # 类型参数只会遮蔽其声明内部的同名接口，和运行时值的绑定独立。
            for parameter in parameters.named_children:
                if bound := _field(parameter, "name"):
                    scope.types[bound] = False
        if typescript and name and node.type in _CLASSES and len(sites) < 400:
            if node.type == "class":
                scope.types[name] = False
            for interface in islice(_implementation_names(node), 400 - len(sites)):
                sites.append((scope, name[:120], interface, node.start_point.row + 1))
        if (specifier := _module_reference(node)) is not None and (len(specifiers) < 120 or specifier in specifiers):
            specifiers.setdefault(specifier, None)
            if len(facts.import_sites) < 300:
                facts.import_sites.append((specifier, node.start_point.row + 1))
        if node.type in {"call_expression", "new_expression"} and bindings and not node.has_error:
            receiver, member = _callee(node)
            if receiver in bindings:
                calls.append((node, scope, receiver, member))
        if node.type != "import_statement":
            pending.extend((child, scope) for child in reversed(node.named_children))

    facts.specifiers = list(specifiers)
    for scope, name, interface, line in sites:
        root_name, _, member = interface.partition(".")
        visible = scope.type_binding(root_name)
        # 同名类型或不可见的局部接口都不能替代实际导入的接口。
        if visible is None or (visible and not member):
            facts.implementations.append({"name": name, "interface": interface, "line": line, "local": visible is True})
        if len(facts.implementations) == 200:
            break

    for node, scope, receiver, member in calls:
        # 所有声明都已收集。处理早于 let/const 或函数声明的调用时也能识别遮蔽。
        if scope.shadows(receiver):
            continue
        binding = bindings[receiver]
        exported = binding["exported"]
        if not member and exported == "*":
            continue
        symbol = member if exported == "*" else f"{exported}.{member}" if member else exported
        callee = f"{receiver}.{member}" if member else receiver
        if len(callee) > 240 or len(symbol) > 240:
            continue
        facts.calls.append({
            "module": binding["specifier"], "symbol": symbol, "callee": callee, "line": node.start_point.row + 1,
        })
        if len(facts.calls) == 300:
            break
    return facts
