#!/usr/bin/env python3
"""Inventory every production process-execution callsite and enforce its phase.

The scan is deliberately broader than a list of Git wrappers: every Python
process primitive is discovered from the current AST.  A new module containing
one must be added to exactly one complete phase set or the check fails.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
from pathlib import Path

PRE_SNAPSHOT_MODULES = frozenset(
    {
        "core_authority_client.py",
        "core_authority_service.py",
        "custodian.py",
        "custodian_bootstrap.py",
        "custodian_server.py",
        "gitless_repository.py",
        "prelaunch_authority.py",
        "qualified_campaign.py",
    }
)
POST_SNAPSHOT_MODULES = frozenset(
    {
        "candidate_export.py",
        "direct_historical_replay.py",
        "codex_models.py",
        "doctor.py",
        "git_evidence.py",
        "offline_evaluation_package.py",
        "offline_replay_evaluator.py",
        "physics_auditor_evidence.py",
        "physics_auditor_projection.py",
        "physics_benchmark_blindness.py",
        "physics_oracle_workspace.py",
        "safe_git.py",
        "workflow_models.py",
    }
)
QUALIFIED_SANDBOX_MODULES = frozenset(
    {
        "codex_adapter.py",
        "live_shadow_engine.py",
        "live_shadow_isolation.py",
        "physics_oracle_execution.py",
        "replay_campaign_engine.py",
        "test_runner.py",
    }
)
PROCESS_ATTRIBUTES = frozenset(
    {
        "Popen",
        "getoutput",
        "getstatusoutput",
        "popen",
        "run",
        "call",
        "check_call",
        "check_output",
        "create_subprocess_exec",
        "create_subprocess_shell",
        "execv",
        "execve",
        "execvp",
        "execvpe",
        "posix_spawn",
        "posix_spawnp",
        "spawnl",
        "spawnle",
        "spawnlp",
        "spawnlpe",
        "spawnv",
        "spawnve",
        "spawnvp",
        "spawnvpe",
        "system",
    }
)
EXPECTED_INVENTORY_SHA256 = (
    "dc2ded7e1c14774af428538bd9a9e3d7157b578166f2505cf91c9f3a325f445e"
)
CATEGORY_OVERRIDES = {
    ("custodian.py", "_launch"): "POST-SNAPSHOT",
    ("custodian.py", "_run_json"): "POST-SNAPSHOT",
    ("custodian_server.py", "_open_repository"): "POST-SNAPSHOT",
}


def main() -> int:
    repository = Path(__file__).resolve().parents[1]
    source_root = repository / "src/research_automation_supervisor"
    callsites: list[dict[str, object]] = []
    non_python_git_callsites: list[dict[str, object]] = []
    errors: list[str] = []
    project = (repository / "pyproject.toml").read_text(encoding="utf-8")
    if (
        'research-supervisor = "research_automation_supervisor.secure_cli:main"'
        not in project
    ):
        errors.append("installed legacy CLI snapshot gate changed")
    classified_modules = (
        PRE_SNAPSHOT_MODULES | POST_SNAPSHOT_MODULES | QUALIFIED_SANDBOX_MODULES
    )
    if (
        PRE_SNAPSHOT_MODULES & POST_SNAPSHOT_MODULES
        or PRE_SNAPSHOT_MODULES & QUALIFIED_SANDBOX_MODULES
        or POST_SNAPSHOT_MODULES & QUALIFIED_SANDBOX_MODULES
    ):
        errors.append("process module phase sets overlap")

    for path in sorted(source_root.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        discovered = list(_process_calls(tree))
        git_capable_scopes = _git_capable_scopes(tree)
        if discovered and path.name not in classified_modules:
            errors.append(f"unclassified process module: {path.name}")
        for node, scope in discovered:
            category = CATEGORY_OVERRIDES.get(
                (path.name, scope), _category(path.name)
            )
            expression = ast.get_source_segment(source, node) or ast.unparse(node)
            git_likely = _git_likely(
                node, scope, path.name, git_capable_scopes=git_capable_scopes
            )
            callsites.append(
                {
                    "path": path.relative_to(repository).as_posix(),
                    "line": node.lineno,
                    "scope": scope,
                    "category": category,
                    "git_likely": git_likely,
                    "expression_sha256": hashlib.sha256(expression.encode()).hexdigest(),
                }
            )
            if category == "UNCLASSIFIED":
                errors.append(f"unclassified callsite: {path.name}:{node.lineno}")
            if category == "PRE-SNAPSHOT" and git_likely:
                errors.append(f"pre-snapshot Git execution path: {path.name}:{node.lineno}")

    gitless = (source_root / "gitless_repository.py").read_text(encoding="utf-8")
    core = (source_root / "prelaunch_authority.py").read_text(encoding="utf-8")
    for label, source in (("gitless_repository.py", gitless), ("prelaunch_authority.py", core)):
        if re.search(r"\b(import|from)\s+subprocess\b|\bsubprocess\.", source):
            errors.append(f"{label} imports a process executor")
    client = (source_root / "core_authority_client.py").read_text(encoding="utf-8")
    if "safe_git" in client or "create_repository_bundle_for_core" in client:
        errors.append("Custodian client retains the retired Git bundle path")
    command_pattern = re.compile(
        r"(?:^|[;&|]\s*)(?:exec\s+)?(?:/usr/bin/|/bin/)?git(?:\s|$)",
        re.IGNORECASE,
    )
    for path in sorted((repository / "scripts").iterdir()):
        if not path.is_file() or path.suffix not in {
            ".sh",
            ".service",
            ".ps1",
            ".cmd",
            ".bat",
            ".vbs",
        }:
            continue
        for line_number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if command_pattern.search(line.strip()):
                item = {
                    "path": path.relative_to(repository).as_posix(),
                    "line": line_number,
                    "expression_sha256": hashlib.sha256(line.strip().encode()).hexdigest(),
                }
                non_python_git_callsites.append(item)
                errors.append(
                    f"unclassified non-Python Git execution: {item['path']}:{line_number}"
                )

    inventory_authority = [
        {key: value for key, value in item.items() if key != "line"}
        for item in callsites
    ]
    serialized = json.dumps(
        inventory_authority, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    )
    inventory_sha256 = hashlib.sha256(serialized.encode()).hexdigest()
    if inventory_sha256 != EXPECTED_INVENTORY_SHA256:
        errors.append(
            "production process inventory changed: review every callsite and update the "
            "expected inventory digest"
        )
    report = {
        "schema_version": 1,
        "callsite_count": len(callsites),
        "inventory_sha256": inventory_sha256,
        "categories": {
            category: sum(1 for item in callsites if item["category"] == category)
            for category in (
                "PRE-SNAPSHOT",
                "POST-SNAPSHOT",
                "QUALIFIED-SANDBOX-INTERNAL",
                "UNCLASSIFIED",
            )
        },
        "git_likely_callsites": [item for item in callsites if item["git_likely"]],
        "non_python_git_callsites": non_python_git_callsites,
        "all_callsites": callsites,
        "errors": errors,
        "ok": not errors,
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if not errors else 1


def _category(module: str) -> str:
    if module in PRE_SNAPSHOT_MODULES:
        return "PRE-SNAPSHOT"
    if module in POST_SNAPSHOT_MODULES:
        return "POST-SNAPSHOT"
    if module in QUALIFIED_SANDBOX_MODULES:
        return "QUALIFIED-SANDBOX-INTERNAL"
    return "UNCLASSIFIED"


def _process_calls(tree: ast.AST) -> list[tuple[ast.Call, str]]:
    found: list[tuple[ast.Call, str]] = []
    module_aliases = {"subprocess", "asyncio", "os"}
    direct_aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for imported in node.names:
                if imported.name in {"subprocess", "asyncio", "os"}:
                    module_aliases.add(imported.asname or imported.name)
        elif isinstance(node, ast.ImportFrom) and node.module in {
            "subprocess",
            "asyncio",
            "os",
        }:
            for imported in node.names:
                if imported.name in PROCESS_ATTRIBUTES:
                    direct_aliases.add(imported.asname or imported.name)
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            value = node.value
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            aliases = [target.id for target in targets if isinstance(target, ast.Name)]
            if (isinstance(value, ast.Name) and value.id in direct_aliases) or (
                isinstance(value, ast.Attribute)
                and isinstance(value.value, ast.Name)
                and value.value.id in module_aliases
                and value.attr in PROCESS_ATTRIBUTES
            ):
                additions = set(aliases) - direct_aliases
            else:
                additions = set()
            if additions:
                direct_aliases.update(additions)
                changed = True

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.scopes: list[str] = []

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self.scopes.append(node.name)
            self.generic_visit(node)
            self.scopes.pop()

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self.visit_FunctionDef(node)  # type: ignore[arg-type]

        def visit_Call(self, node: ast.Call) -> None:
            attribute = node.func.attr if isinstance(node.func, ast.Attribute) else ""
            direct = isinstance(node.func, ast.Name) and node.func.id in direct_aliases
            qualified = (
                attribute in PROCESS_ATTRIBUTES
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in module_aliases
            )
            if direct or qualified:
                found.append((node, self.scopes[-1] if self.scopes else "<module>"))
            self.generic_visit(node)

    Visitor().visit(tree)
    return found


def _git_capable_scopes(tree: ast.AST) -> set[str]:
    """Propagate exact Git executable literals through local helper calls."""
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    functions = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    edges: dict[str, set[str]] = {name: set() for name in functions}
    capable: set[str] = set()
    for name, function in functions.items():
        ancestor = parents.get(function)
        while ancestor is not None:
            if isinstance(ancestor, (ast.FunctionDef, ast.AsyncFunctionDef)):
                edges[ancestor.name].add(name)
                break
            ancestor = parents.get(ancestor)
        for default in (*function.args.defaults, *function.args.kw_defaults):
            if isinstance(default, ast.Name) and default.id in functions:
                edges[name].add(default.id)
        for node in ast.walk(function):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                normalized = node.value.strip().casefold()
                if normalized == "git" or normalized.endswith("/git"):
                    capable.add(name)
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id in functions:
                    edges[name].add(node.func.id)
    pending = list(capable)
    while pending:
        caller = pending.pop()
        for callee in edges[caller]:
            if callee not in capable:
                capable.add(callee)
                pending.append(callee)
    return capable


def _git_likely(
    node: ast.Call,
    scope: str,
    module: str,
    *,
    git_capable_scopes: set[str],
) -> bool:
    argument = node.args[0] if node.args else None
    if isinstance(argument, (ast.List, ast.Tuple)) and argument.elts:
        argument = argument.elts[0]
    executable = ast.unparse(argument) if argument is not None else ""
    literal = _literal_string(argument)
    if literal is not None:
        executable = f"{executable} {literal}"
    value = f"{module} {scope} {executable}".casefold()
    return scope in git_capable_scopes or bool(
        re.search(r"(^|[^a-z])git([^a-z]|$)", value)
    )


def _literal_string(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _literal_string(node.left)
        right = _literal_string(node.right)
        if left is not None and right is not None:
            return left + right
    return None


if __name__ == "__main__":
    raise SystemExit(main())
