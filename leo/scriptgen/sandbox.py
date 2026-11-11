"""Checking and running generated script code.

A generated script is model-written code that reads email text, so the email can try to steer
what the model writes. The code is held to a small contract and run in a restricted namespace:

- **Contract.** A module that defines `matches(text) -> bool` and `parse(text) -> dict`. `text`
  has LF line ends (`ScriptExtractor` turns CRLF into LF first). `parse` returns the LLM
  extractor's merged JSON shape (`vendor_name`, `terms`, `invoice_facts`, `contacts`), which
  goes through `LLMExtractor.to_extraction`, so a script's output is normalized exactly as
  the LLM's is. `parse` raises `ValueError` when the document doesn't fit the template.
- **`check_code`** walks the AST. It rejects imports, `global`/`nonlocal`, any name or
  attribute starting with `_`, the attributes that reach frames or code objects (`gi_frame`,
  `f_globals`, ...), `format`/`format_map` (a format string can read dunder attributes), bare
  `except` and `except BaseException`, and calls to `exec`, `eval`, `open` and the like.
- **The namespace** holds a short list of builtins and single functions and constants, never
  whole modules: `email.utils` exposes `os` as a plain attribute, and `re.enum` exposes `sys`
  and the builtins.
- **Fail closed.** Any exception from `matches` is False, and one from `parse` is reported as
  an error, so the document falls through to the LLM.

`run_isolated` runs code in a spawned child process with a time limit, because a runaway
regex can't be interrupted in-process. Promotion and the worker both use it: generated code
never runs in the parent process.

This module imports only the standard library at the top, so the spawned child starts fast.
"""

import ast
import builtins
import datetime
import multiprocessing
import re
from collections.abc import Callable
from decimal import Decimal
from email.utils import parseaddr, parsedate_to_datetime
from typing import Any

# --- The check -------------------------------------------------------------------------------

FORBIDDEN_CALLS = frozenset(
    {
        "exec",
        "eval",
        "compile",
        "open",
        "getattr",
        "setattr",
        "delattr",
        "hasattr",
        "globals",
        "locals",
        "vars",
        "dir",
        "input",
        "breakpoint",
        "type",
        "object",
        "memoryview",
        "help",
        "exit",
        "quit",
    }
)
# Attributes that lead from an ordinary object to frames, code or the real globals.
FORBIDDEN_ATTR_PREFIXES = ("_", "gi_", "cr_", "ag_", "f_", "tb_", "co_")
FORBIDDEN_ATTRS = frozenset({"format", "format_map", "mro", "with_traceback"})
REQUIRED = ("matches", "parse")


def check_code(code: str) -> list[str]:
    """Why the code breaks the sandbox rules; empty when it passes. Messages carry line
    numbers but no addresses, so they're the same every run (they go into retry prompts)."""
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return [f"syntax error at line {e.lineno}: {e.msg}"]
    errors: list[str] = []

    def bad(node: ast.AST, why: str) -> None:
        errors.append(f"line {getattr(node, 'lineno', 0)}: {why}")

    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.Assign | ast.AnnAssign):
            continue
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            continue  # a docstring
        bad(node, f"only functions and assignments at the top level, not {type(node).__name__}")

    for node in ast.walk(tree):
        if isinstance(node, ast.Import | ast.ImportFrom):
            bad(node, "imports are not allowed; use the provided names")
        elif isinstance(node, ast.Global | ast.Nonlocal):
            bad(node, f"{type(node).__name__.lower()} is not allowed")
        elif isinstance(node, ast.AsyncFunctionDef | ast.Await | ast.ClassDef):
            bad(node, f"{type(node).__name__} is not allowed")
        elif isinstance(node, ast.Name) and node.id.startswith("_"):
            bad(node, f"name {node.id!r} starts with an underscore")
        elif isinstance(node, ast.Name) and node.id in FORBIDDEN_CALLS:
            bad(node, f"{node.id!r} is not allowed")
        elif isinstance(node, ast.Attribute) and (
            node.attr.startswith(FORBIDDEN_ATTR_PREFIXES) or node.attr in FORBIDDEN_ATTRS
        ):
            bad(node, f"attribute {node.attr!r} is not allowed")
        elif isinstance(node, ast.FunctionDef | ast.arg) and _name_of(node).startswith("_"):
            bad(node, f"name {_name_of(node)!r} starts with an underscore")
        elif isinstance(node, ast.ExceptHandler) and (
            node.type is None
            or (isinstance(node.type, ast.Name) and node.type.id == "BaseException")
        ):
            bad(node, "catch ValueError (or another Exception), not everything")
        elif isinstance(node, ast.ExceptHandler) and node.name and node.name.startswith("_"):
            bad(node, f"name {node.name!r} starts with an underscore")

    defined = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    for name in REQUIRED:
        if name not in defined:
            errors.append(f"missing a top-level function {name}(text)")
    return errors


def _name_of(node: ast.FunctionDef | ast.arg) -> str:
    return node.name if isinstance(node, ast.FunctionDef) else node.arg


# --- The namespace ---------------------------------------------------------------------------

SAFE_BUILTINS: dict[str, Any] = {
    name: getattr(builtins, name)
    for name in (
        "len",
        "int",
        "str",
        "float",
        "bool",
        "list",
        "dict",
        "tuple",
        "set",
        "range",
        "enumerate",
        "zip",
        "min",
        "max",
        "sum",
        "sorted",
        "reversed",
        "any",
        "all",
        "abs",
        "round",
        "isinstance",
        "Exception",
        "ValueError",
        "KeyError",
        "IndexError",
        "ArithmeticError",
    )
}

# Library code the provided names run may import these at call time (`datetime.strptime`
# imports `_strptime`). Generated code can't call `__import__` itself: no import statements
# and no names starting with an underscore.
INTERNAL_IMPORTS = frozenset({"_strptime"})


def _guarded_import(name, globals=None, locals=None, fromlist=(), level=0):  # noqa: A002
    if level == 0 and name in INTERNAL_IMPORTS:
        return builtins.__import__(name, globals, locals, fromlist, level)
    raise ImportError(f"import of {name!r} is not allowed")


# Single functions and constants only. Never hand in a module (see the module docstring).
PROVIDED: dict[str, Any] = {
    "re_search": re.search,
    "re_match": re.match,
    "re_fullmatch": re.fullmatch,
    "re_findall": re.findall,
    "re_finditer": re.finditer,
    "re_sub": re.sub,
    "re_split": re.split,
    "re_escape": re.escape,
    "MULTILINE": re.MULTILINE,
    "IGNORECASE": re.IGNORECASE,
    "DOTALL": re.DOTALL,
    "date": datetime.date,
    "datetime": datetime.datetime,
    "Decimal": Decimal,
    "parseaddr": parseaddr,
    "parsedate_to_datetime": parsedate_to_datetime,
}

CONTRACT = """\
The code is a Python module with no imports. It defines two top-level functions:

    def matches(text: str) -> bool
        True when the document is in this format. Decide on the sender address and fixed
        template markers (labels, headings), never on values that change between documents
        (amounts, dates, invoice numbers).

    def parse(text: str) -> dict
        The extraction, in exactly the JSON shape of the examples' expected output. Raise
        ValueError when any expected line is missing or the numbers don't add up, so the
        document goes to the LLM instead of being stored wrong.

`text` is the whole raw email (headers, a blank line, then the body) with LF line ends.

Available names (nothing else; no imports):
- builtins: {builtins}
- re_search, re_match, re_fullmatch, re_findall, re_finditer, re_sub, re_split, re_escape,
  MULTILINE, IGNORECASE, DOTALL (the functions and flags of Python's `re` module)
- date, datetime (the classes from Python's `datetime` module), Decimal
- parseaddr, parsedate_to_datetime (from `email.utils`)

Rules the code is checked against before it runs:
- no import, global, nonlocal, class or async code
- no name or attribute starting with an underscore, and no .format() or .format_map()
  (use f-strings or concatenation)
- no exec, eval, compile, open, getattr, setattr, hasattr, globals, locals, vars, dir, type
- no bare `except:` and no `except BaseException`
- only functions and assignments at the top level

Output rules:
- Every quote is one whole line of the text, copied verbatim (a substring of `text`).
- Dates are ISO strings ("2026-09-01"); amounts are numbers or decimal strings without "$"
  or commas; a billing period is "YYYY-MM-DD/YYYY-MM-DD".
- Return the same keys, in the same structure, as the expected outputs.
""".format(builtins=", ".join(sorted(SAFE_BUILTINS)))


class SandboxError(ValueError):
    """The code breaks the sandbox rules, or fails to load."""


def load_functions(code: str) -> tuple[Callable[[str], Any], Callable[[str], Any]]:
    """Check the code, run it in the restricted namespace, and return (matches, parse)."""
    if errors := check_code(code):
        raise SandboxError("; ".join(errors))
    namespace: dict[str, Any] = {
        "__builtins__": {**SAFE_BUILTINS, "__import__": _guarded_import},
        **PROVIDED,
    }
    try:
        exec(compile(code, "<generated script>", "exec"), namespace)  # noqa: S102 - checked above
    except Exception as e:
        raise SandboxError(f"loading failed: {type(e).__name__}: {e}") from e
    matches, parse = namespace.get("matches"), namespace.get("parse")
    if not callable(matches) or not callable(parse):
        raise SandboxError("matches and parse must be functions")
    return matches, parse


def safe_matches(matches: Callable[[str], Any], text: str) -> bool:
    """Fail closed: any exception means "not mine"."""
    try:
        return bool(matches(text))
    except Exception:
        return False


# --- Isolated runs (promotion and the worker) -------------------------------------------------

TIMED_OUT = "timed out"


def _child(code: str, texts: list[str], conn) -> None:  # pragma: no cover - runs in a child
    results: list[dict[str, Any]] = []
    try:
        matches, parse = load_functions(code)
    except SandboxError as e:
        conn.send({"error": str(e), "results": []})
        return
    for text in texts:
        r: dict[str, Any] = {"matches": safe_matches(matches, text), "output": None, "error": None}
        if r["matches"]:
            try:
                out = parse(text)
                if not isinstance(out, dict):
                    raise ValueError(f"parse returned {type(out).__name__}, not dict")
                r["output"] = out
            except Exception as e:
                r["error"] = f"{type(e).__name__}: {e}"
        results.append(r)
    conn.send({"error": None, "results": results})


def run_isolated(code: str, texts: list[str], timeout: float = 10.0) -> dict[str, Any]:
    """Run `matches` (and `parse` where it matches) on each text in a spawned child process.

    Returns `{"error": str | None, "results": [{"matches", "output", "error"}, ...]}`, one
    result per text, in order. On a sandbox failure or a timeout, `error` says so and
    `results` is empty. The output is whatever `parse` returned; the caller converts it."""
    ctx = multiprocessing.get_context("spawn")
    parent, child = ctx.Pipe(duplex=False)
    proc = ctx.Process(target=_child, args=(code, texts, child), daemon=True)
    proc.start()
    child.close()
    try:
        if not parent.poll(timeout):
            return {"error": TIMED_OUT, "results": []}
        return parent.recv()
    except EOFError:
        return {"error": "the script process died", "results": []}
    finally:
        if proc.is_alive():
            proc.kill()
        proc.join(5)
        parent.close()
