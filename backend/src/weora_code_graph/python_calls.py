"""从 Python 语法树提取具有静态导入绑定的调用，过程不加载仓库模块。"""

import ast
import symtable
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TypedDict

_SCOPE_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)


class PythonCall(TypedDict):
    module: str
    symbol: str
    callee: str
    line: int


@dataclass(frozen=True, slots=True)
class Binding:
    module: str
    member: str = ""
    # `import package.submodule` 绑定的是 package，调用必须显式经过 submodule。
    prefix: tuple[str, ...] = ()


def _join_module(module: str, member: str) -> str:
    return module + ("" if module.endswith(".") else ".") + member


def _imports(body: Sequence[ast.stmt]) -> dict[str, Binding]:
    direct = set(body)
    counts: Counter[str] = Counter()
    bindings: dict[str, Binding] = {}
    remaining: list[ast.AST] = list(body)
    while remaining:
        node = remaining.pop()
        if isinstance(node, _SCOPE_NODES):
            continue
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                first, *rest = alias.name.split(".")
                local = alias.asname or first
                counts[local] += 1
                if node not in direct:
                    continue
                if isinstance(node, ast.ImportFrom):
                    bindings[local] = Binding("." * node.level + (node.module or ""), alias.name)
                else:
                    bindings[local] = Binding(alias.name, prefix=tuple(rest) if alias.asname is None else ())
        else:
            remaining.extend(ast.iter_child_nodes(node))
    # 分支导入、重复绑定和星号导入的目标可能随运行路径变化。
    if "*" in counts:
        return {}
    return {name: binding for name, binding in bindings.items() if counts[name] == 1}


class _CallVisitor(ast.NodeVisitor):
    def __init__(self, module: symtable.SymbolTable, body: list[ast.stmt]) -> None:
        self.module_bindings = _imports(body)
        self.bindings = self.module_bindings
        self.scope = module
        self.calls: list[PythonCall] = []
        self.dynamic_globals = self._globally_rebound_names(module)

    @staticmethod
    def _globally_rebound_names(module: symtable.SymbolTable) -> set[str]:
        rebound = {
            symbol.get_name() for symbol in module.get_symbols() if symbol.is_assigned() or symbol.is_namespace()
        }
        remaining = module.get_children()
        while remaining:
            scope = remaining.pop()
            rebound.update(
                symbol.get_name()
                for symbol in scope.get_symbols()
                if symbol.is_declared_global()
                and (symbol.is_assigned() or symbol.is_imported() or symbol.is_namespace())
            )
            remaining.extend(scope.get_children())
        return rebound

    def _visit_body(self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> None:
        scope_type = "class" if isinstance(node, ast.ClassDef) else "function"
        matches = [
            scope
            for scope in self.scope.get_children()
            if scope.get_type() == scope_type and scope.get_name() == node.name and scope.get_lineno() == node.lineno
        ]
        if len(matches) != 1:
            return
        # 默认值和装饰器在外层求值，不把这些位置当作函数体。
        old_scope, old_bindings = self.scope, self.bindings
        self.scope, self.bindings = matches[0], _imports(node.body)
        for statement in node.body:
            self.visit(statement)
        self.scope, self.bindings = old_scope, old_bindings

    visit_FunctionDef = _visit_body
    visit_AsyncFunctionDef = _visit_body
    visit_ClassDef = _visit_body

    def visit_Lambda(self, node: ast.Lambda) -> None:
        """匿名作用域独立绑定参数，留给更完整的静态解析处理。"""

    def visit_ListComp(self, node: ast.ListComp | ast.DictComp | ast.SetComp | ast.GeneratorExp) -> None:
        """推导式绑定独立的迭代变量，避免把其中的同名调用归到外部导入。"""

    visit_DictComp = visit_ListComp
    visit_SetComp = visit_ListComp
    visit_GeneratorExp = visit_ListComp

    def _binding(self, name: str) -> Binding | None:
        try:
            symbol = self.scope.lookup(name)
        except KeyError:
            return None
        if symbol.is_assigned() or symbol.is_namespace() or symbol.is_parameter():
            return None
        if not symbol.is_global():
            return self.bindings.get(name)
        if name in self.dynamic_globals:
            return None
        return self.module_bindings.get(name)

    def visit_Call(self, node: ast.Call) -> None:
        callee = node.func
        attrs: list[str] = []
        while isinstance(callee, ast.Attribute):
            attrs.append(callee.attr)
            callee = callee.value
        if isinstance(callee, ast.Name) and (binding := self._binding(callee.id)):
            attrs.reverse()
            self._record_call(callee.id, attrs, binding, node.lineno)
        self.generic_visit(node)

    def _record_call(self, name: str, attrs: list[str], binding: Binding, line: int) -> None:
        module = binding.module
        if binding.member and not attrs:
            symbol = binding.member
        else:
            if tuple(attrs[: len(binding.prefix)]) != binding.prefix or len(attrs) <= len(binding.prefix):
                return
            remaining = attrs[len(binding.prefix) :]
            if binding.member:
                module = _join_module(module, binding.member)
            for part in remaining[:-1]:
                module = _join_module(module, part)
            symbol = remaining[-1]
        callee = ".".join([name, *attrs])
        if max(len(module), len(callee)) <= 480:
            self.calls.append({"module": module, "symbol": symbol, "callee": callee, "line": line})


def call_facts(path: str, text: str, tree: ast.Module) -> list[PythonCall]:
    """以 Python 自己的符号表判断调用名称是否被参数、局部变量或全局写入遮蔽。"""
    try:
        visitor = _CallVisitor(symtable.symtable(text, path, "exec"), tree.body)
        visitor.visit(tree)
    except (SyntaxError, RecursionError):
        # 语法树可保留声明证据，无法建立作用域时停止生成调用关系。
        return []
    calls = sorted(visitor.calls, key=lambda call: call["line"])
    return calls[:200]
