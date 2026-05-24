"""AST-based pre-commit helper enforcing REST route conventions.

Encodes the API patterns documented in CLAUDE.md so they apply uniformly
across `app/routes/`. Designed to be wired into `.pre-commit-config.yaml`
with ``files: ^app/routes/.*[.]py$`` so it only scans changed route files,
which makes gradual cleanup of existing violations possible.

Rules enforced (each violation emits `path:line: [CODE] message`):

  R1  Every `@router.<method>(...)` decorator must declare a response
      shape: either `response_model=` (JSON endpoints) or `response_class=`
      (HTML/PlainText/etc. endpoints). Catches accidentally-untyped APIs.

  R2  POST/PUT/PATCH/DELETE handlers that return JSON (i.e., have NO
      `response_class=` override) must pass an explicit `status_code=`.
      Form-style handlers returning HTMLResponse are exempt because their
      implicit 200 is correct.

  R3  Any handler parameter annotated as `AsyncSession` must default to
      `Depends(get_session)`. Catches missing DI and sessions constructed
      inline.

  R4  Every `TemplateResponse(...)` call must include `request` in its
      context. We accept either the second positional dict argument or a
      `context=` kwarg, and require a `"request"` string key.

Usage::

    python scripts/check_route_conventions.py app/routes/foo.py [...]

Returns nonzero exit code if any violations were found.
"""

from __future__ import annotations

import ast
import sys
from dataclasses import dataclass
from pathlib import Path


_HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}
_WRITE_METHODS = {"post", "put", "patch", "delete"}


@dataclass(frozen=True)
class Violation:
    path: Path
    lineno: int
    code: str
    message: str

    def format(self) -> str:
        return f"{self.path}:{self.lineno}: [{self.code}] {self.message}"


def _router_method(decorator: ast.expr) -> str | None:
    """Return the HTTP method name if this decorator is `@router.<method>(...)`."""
    if not isinstance(decorator, ast.Call):
        return None
    func = decorator.func
    if not isinstance(func, ast.Attribute):
        return None
    if func.attr not in _HTTP_METHODS:
        return None
    value = func.value
    if not isinstance(value, ast.Name) or value.id != "router":
        return None
    return func.attr


def _kwarg(call: ast.Call, name: str) -> ast.keyword | None:
    for kw in call.keywords:
        if kw.arg == name:
            return kw
    return None


def _annotation_name(annotation: ast.expr | None) -> str | None:
    """Reduce a parameter annotation to its base type name."""
    if annotation is None:
        return None
    if isinstance(annotation, ast.Name):
        return annotation.id
    if isinstance(annotation, ast.Attribute):
        return annotation.attr
    if isinstance(annotation, ast.Subscript):
        return _annotation_name(annotation.value)
    return None


def _is_depends_get_session(default: ast.expr | None) -> bool:
    """True if the default value is `Depends(get_session)` (or an equivalent)."""
    if not isinstance(default, ast.Call):
        return False
    func = default.func
    name = (
        func.attr
        if isinstance(func, ast.Attribute)
        else (func.id if isinstance(func, ast.Name) else None)
    )
    if name != "Depends":
        return False
    if not default.args:
        return False
    arg = default.args[0]
    arg_name = (
        arg.attr
        if isinstance(arg, ast.Attribute)
        else (arg.id if isinstance(arg, ast.Name) else None)
    )
    return arg_name == "get_session"


def _context_has_request_key(node: ast.expr) -> bool:
    """True if the given expression is a dict literal that contains a `"request"` key."""
    if not isinstance(node, ast.Dict):
        # If it's not a literal dict (e.g., a variable), we can't statically
        # verify. Skip rather than false-positive.
        return True
    for key in node.keys:
        if isinstance(key, ast.Constant) and key.value == "request":
            return True
    return False


def _check_route_decorator(
    path: Path,
    func: ast.AsyncFunctionDef | ast.FunctionDef,
    decorator: ast.Call,
    method: str,
) -> list[Violation]:
    out: list[Violation] = []
    response_model = _kwarg(decorator, "response_model")
    response_class = _kwarg(decorator, "response_class")
    status_code = _kwarg(decorator, "status_code")

    if response_model is None and response_class is None:
        out.append(
            Violation(
                path,
                decorator.lineno,
                "R1",
                f"@router.{method} handler `{func.name}` is missing `response_model=` or `response_class=`.",
            )
        )

    if method in _WRITE_METHODS and response_class is None and status_code is None:
        out.append(
            Violation(
                path,
                decorator.lineno,
                "R2",
                f"@router.{method} handler `{func.name}` must declare an explicit `status_code=` "
                "(e.g., 201 for create, 204 for delete).",
            )
        )

    return out


def _check_handler_signature(
    path: Path,
    func: ast.AsyncFunctionDef | ast.FunctionDef,
) -> list[Violation]:
    out: list[Violation] = []
    args = func.args
    # Align defaults to params: defaults apply to the trailing positional args.
    positional = args.args
    defaults = args.defaults
    paired: list[tuple[ast.arg, ast.expr | None]] = []
    if defaults:
        offset = len(positional) - len(defaults)
        for i, arg in enumerate(positional):
            paired.append((arg, defaults[i - offset] if i >= offset else None))
    else:
        paired = [(arg, None) for arg in positional]
    for arg, default in paired + [
        (a, d) for a, d in zip(args.kwonlyargs, args.kw_defaults)
    ]:
        if _annotation_name(arg.annotation) != "AsyncSession":
            continue
        if not _is_depends_get_session(default):
            out.append(
                Violation(
                    path,
                    arg.lineno,
                    "R3",
                    f"`{func.name}` parameter `{arg.arg}: AsyncSession` must use "
                    "`Depends(get_session)` as its default.",
                )
            )
    return out


def _check_template_response_calls(path: Path, tree: ast.AST) -> list[Violation]:
    out: list[Violation] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        callee = (
            func.attr
            if isinstance(func, ast.Attribute)
            else (func.id if isinstance(func, ast.Name) else None)
        )
        if callee != "TemplateResponse":
            continue
        # FastAPI/Starlette signature: TemplateResponse(name, context, ...)
        context_node: ast.expr | None = None
        if len(node.args) >= 2:
            context_node = node.args[1]
        ctx_kw = _kwarg(node, "context")
        if ctx_kw is not None:
            context_node = ctx_kw.value
        if context_node is None:
            out.append(
                Violation(
                    path,
                    node.lineno,
                    "R4",
                    "TemplateResponse(...) is missing a context dict containing `request`.",
                )
            )
            continue
        if not _context_has_request_key(context_node):
            out.append(
                Violation(
                    path,
                    node.lineno,
                    "R4",
                    'TemplateResponse(...) context dict must include a `"request"` key.',
                )
            )
    return out


def _check_file(path: Path) -> list[Violation]:
    try:
        source = path.read_text(encoding="utf-8")
    except OSError as exc:
        sys.stderr.write(f"warning: could not read {path}: {exc}\n")
        return []
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        sys.stderr.write(f"warning: syntax error in {path}: {exc}\n")
        return []

    violations: list[Violation] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        for decorator in node.decorator_list:
            method = _router_method(decorator)
            if method is None:
                continue
            assert isinstance(decorator, ast.Call)  # narrowed by _router_method
            violations.extend(_check_route_decorator(path, node, decorator, method))
            violations.extend(_check_handler_signature(path, node))
    violations.extend(_check_template_response_calls(path, tree))
    return violations


def main(argv: list[str]) -> int:
    paths = [Path(arg) for arg in argv[1:] if arg.endswith(".py")]
    if not paths:
        return 0

    all_violations: list[Violation] = []
    for path in paths:
        all_violations.extend(_check_file(path))

    if not all_violations:
        return 0

    all_violations.sort(key=lambda v: (str(v.path), v.lineno, v.code))
    sys.stderr.write(
        "Route convention violations (see scripts/check_route_conventions.py docstring "
        "for rule descriptions):\n\n"
    )
    for v in all_violations:
        sys.stderr.write(v.format() + "\n")
    sys.stderr.write("\n")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
