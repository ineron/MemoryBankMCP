"""Tree-sitter based entity/dependency extraction, one module per language.

Only Python is wired up today (`tree-sitter-python`). Adding a language
later means: add its pip package to the `codegraph` extra, add one branch
to `_LANGUAGE_LOADERS` + `parse_file`, and write one `_parse_<lang>`
function following the same shape as `_parse_python` below — nothing else
in this package needs to change.

Importing this module requires the optional `codegraph` extra
(`tree-sitter` + `tree-sitter-python`); the actual `import tree_sitter*`
calls are deferred into the `_py_language()`/`_parse_python()` functions so
that merely importing `memory_mcp.codegraph.parser` doesn't require those
packages until a Python file is actually parsed — server.py's
availability probe imports this module eagerly, so keeping the heavy
imports lazy here would only matter if a future language's bindings were
expensive to import; for tree-sitter itself this is a minor nicety, not a
requirement.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

SUPPORTED_EXTENSIONS: dict[str, str] = {
    ".py": "python",
}


@dataclass
class RawEntity:
    kind: str  # module | class | function | method
    name: str
    full_path: str
    start_line: int
    end_line: int
    is_public: bool
    parent_full_path: str | None
    # True for @decorator-wrapped functions/classes (Flask/FastAPI routes,
    # @pytest.fixture, @click.command, etc.) — these are typically invoked
    # by a framework at runtime, never by a name lookup in-source, so a
    # "zero incoming calls" unused-code heuristic is wrong for them far
    # more often than it's right. See get_issues()'s unused_entities.
    has_decorator: bool = False


@dataclass
class RawDependency:
    source_full_path: str
    target_name: str  # raw textual reference, e.g. "self.baz", "foo.bar", "os"
    dependency_type: str  # calls | imports
    line_number: int


@dataclass
class ParsedFile:
    entities: list[RawEntity] = field(default_factory=list)
    dependencies: list[RawDependency] = field(default_factory=list)


def file_hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def parse_file(relpath: Path, module_full_path: str, content: bytes) -> ParsedFile:
    language = SUPPORTED_EXTENSIONS.get(relpath.suffix)
    if language == "python":
        return _parse_python(module_full_path, content)
    raise ValueError(f"unsupported extension {relpath.suffix!r} for {relpath}")


# ---------------------------------------------------------------------
# Python
# ---------------------------------------------------------------------

_PY_LANGUAGE = None


def _py_language():
    global _PY_LANGUAGE
    if _PY_LANGUAGE is None:
        import tree_sitter_python as tspython
        from tree_sitter import Language

        _PY_LANGUAGE = Language(tspython.language())
    return _PY_LANGUAGE


def _parse_python(module_full_path: str, content: bytes) -> ParsedFile:
    from tree_sitter import Parser

    parser = Parser(_py_language())
    tree = parser.parse(content)

    result = ParsedFile()
    result.entities.append(
        RawEntity(
            kind="module",
            name=module_full_path.rsplit(".", 1)[-1],
            full_path=module_full_path,
            start_line=1,
            end_line=content.count(b"\n") + 1,
            is_public=True,
            parent_full_path=None,
        )
    )
    _walk_python(tree.root_node, module_full_path, module_full_path, "module", result)
    return result


def _handle_definition(node, full_path_prefix: str, parent_kind: str, result: ParsedFile, has_decorator: bool) -> None:
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return
    name = name_node.text.decode()
    full_path = f"{full_path_prefix}.{name}"
    if node.type == "class_definition":
        kind, next_parent_kind = "class", "class"
    else:
        kind = "method" if parent_kind == "class" else "function"
        next_parent_kind = "function"
    result.entities.append(
        RawEntity(
            kind=kind,
            name=name,
            full_path=full_path,
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            is_public=not name.startswith("_"),
            parent_full_path=full_path_prefix,
            has_decorator=has_decorator,
        )
    )
    body = node.child_by_field_name("body")
    if body is not None:
        _walk_python(body, full_path, full_path, next_parent_kind, result)


def _walk_python(node, full_path_prefix: str, owner_full_path: str, parent_kind: str, result: ParsedFile) -> None:
    for child in node.children:
        if child.type in ("class_definition", "function_definition"):
            _handle_definition(child, full_path_prefix, parent_kind, result, has_decorator=False)

        elif child.type == "decorated_definition":
            inner = next(
                (gc for gc in child.children if gc.type in ("class_definition", "function_definition")),
                None,
            )
            if inner is not None:
                _handle_definition(inner, full_path_prefix, parent_kind, result, has_decorator=True)

        elif child.type == "call":
            func_node = child.child_by_field_name("function")
            if func_node is not None:
                result.dependencies.append(
                    RawDependency(
                        source_full_path=owner_full_path,
                        target_name=func_node.text.decode(),
                        dependency_type="calls",
                        line_number=child.start_point[0] + 1,
                    )
                )
            # recurse into arguments etc. — nested calls like foo(bar()) and
            # the callee expression itself (e.g. Foo().bar() has a nested call)
            _walk_python(child, full_path_prefix, owner_full_path, parent_kind, result)

        elif child.type in ("import_statement", "import_from_statement"):
            for target_name in _import_targets(child):
                result.dependencies.append(
                    RawDependency(
                        source_full_path=owner_full_path,
                        target_name=target_name,
                        dependency_type="imports",
                        line_number=child.start_point[0] + 1,
                    )
                )

        else:
            _walk_python(child, full_path_prefix, owner_full_path, parent_kind, result)


def _import_targets(node) -> list[str]:
    """Best-effort dotted names referenced by an import/from-import statement.
    "import os, sys" -> ["os", "sys"]; "import foo.bar as fb" -> ["foo.bar"];
    "from foo.bar import baz as qux" -> ["foo.bar.baz"]. Aliases are ignored
    (target_name is the real path, not the local binding name) since
    resolution matches against real module full_paths."""
    module_node = node.child_by_field_name("module_name")  # None for plain import_statement
    module_name = module_node.text.decode() if module_node is not None else ""
    module_node_id = module_node.id if module_node is not None else None

    targets = []
    for c in node.children:
        # tree-sitter Node objects are re-wrapped on each access (`is`
        # comparison is never true even for the same underlying node) —
        # `.id` identifies the actual node.
        if module_node_id is not None and c.id == module_node_id:
            continue
        if c.type == "dotted_name":
            targets.append(_join_module(module_name, c.text.decode()))
        elif c.type == "aliased_import":
            inner = c.child_by_field_name("name")
            if inner is not None:
                targets.append(_join_module(module_name, inner.text.decode()))
        elif c.type == "wildcard_import":
            targets.append(_join_module(module_name, "*"))
    return targets


def _join_module(module_name: str, name: str) -> str:
    if not module_name:
        return name
    sep = "" if module_name.endswith(".") else "."
    return f"{module_name}{sep}{name}"
