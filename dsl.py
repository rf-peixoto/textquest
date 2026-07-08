"""
textquest.dsl
=============
A *safe* expression evaluator used for choice conditions and `set` effects.

Game authors write plain expressions such as:

    "health > 5 and has('torch')"
    "score + 10"
    "visited('cellar') or chance(0.25)"

We parse them with `ast` and only allow a whitelist of node types, so game
files can never execute arbitrary code on the player's machine.
"""

from __future__ import annotations

import ast
import operator
import random
import re

_DICE_RE = re.compile(r"\b(\d*)[dD](\d+)\b")


def roll_dice(n: int, sides: int) -> list[int]:
    """Roll <n> dice with <sides> faces, return individual results."""
    n = max(1, int(n))
    sides = max(2, int(sides))
    return [random.randint(1, sides) for _ in range(n)]


def roll_spec(spec: str) -> int:
    """Silently roll a dice spec like '2d6+3' or 'd20' and return the total.
    Any non-dice arithmetic in the spec is evaluated too ('1d4*2+1')."""
    detail = roll_spec_detailed(spec)
    return detail["total"]


def roll_spec_detailed(spec: str) -> dict:
    """Roll a spec and return {'total': int, 'display': str, 'faces': [...]}.
    The display string shows each die's faces: '2d6[4,1] + 3 = 8'."""
    all_faces: list[int] = []
    display_parts: list[str] = []
    expr_parts: list[str] = []
    pos = 0
    for m in _DICE_RE.finditer(spec):
        display_parts.append(spec[pos:m.start()])
        expr_parts.append(spec[pos:m.start()])
        pos = m.end()
        n = int(m.group(1) or 1)
        sides = int(m.group(2))
        faces = roll_dice(n, sides)
        all_faces.extend(faces)
        display_parts.append(
            f"{n if n > 1 else ''}d{sides}[{','.join(map(str, faces))}]")
        expr_parts.append(str(sum(faces)))
    display_parts.append(spec[pos:])
    expr_parts.append(spec[pos:])
    expr = "".join(expr_parts).strip()
    total = sum(all_faces)
    if _is_arith(expr):
        try:
            total = int(eval(compile(ast.parse(expr, mode="eval"),
                                     "<dice>", "eval"),
                             {"__builtins__": {}}, {}))
        except Exception:
            pass
    return {"total": total, "display": "".join(display_parts).strip(),
            "faces": all_faces, "expr": expr}


def _is_arith(expr: str) -> bool:
    """True if expr is pure arithmetic (digits/operators/spaces only)."""
    return bool(expr) and bool(re.fullmatch(r"[0-9+\-*/() .%]+", expr))

_ALLOWED_NODES = (
    ast.Expression, ast.BoolOp, ast.BinOp, ast.UnaryOp, ast.Compare,
    ast.Name, ast.Load, ast.Constant, ast.Call, ast.And, ast.Or, ast.Not,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow,
    ast.USub, ast.UAdd, ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
    ast.In, ast.NotIn, ast.IfExp, ast.List, ast.Tuple, ast.Subscript,
    ast.Index if hasattr(ast, "Index") else ast.Expression,  # py<3.9 compat
)

_BIN_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod, ast.Pow: operator.pow,
}
_CMP_OPS = {
    ast.Eq: operator.eq, ast.NotEq: operator.ne, ast.Lt: operator.lt,
    ast.LtE: operator.le, ast.Gt: operator.gt, ast.GtE: operator.ge,
    ast.In: lambda a, b: a in b, ast.NotIn: lambda a, b: a not in b,
}


KNOWN_FUNCTIONS = {
    "chance", "randint", "min", "max", "abs", "int", "len", "roll",
    "has", "count", "visited", "visits", "turn", "equipped",
    "d4", "d6", "d8", "d10", "d12", "d20", "d100",
}
_LITERAL_NAMES = {"true", "True", "false", "False", "none", "None", "null"}


def check_expression(expr: str, known_vars: set[str]) -> str | None:
    """Statically validate an expression without executing it.
    Returns an error message, or None if the expression looks sound."""
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as e:
        return f"syntax error: {e.msg}"
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            return f"disallowed syntax: {type(node).__name__}"
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name):
                return "only simple function calls are allowed"
            if node.func.id not in KNOWN_FUNCTIONS:
                return f"unknown function '{node.func.id}()'"
        elif isinstance(node, ast.Name):
            name = node.id
            if (name not in known_vars and name not in _LITERAL_NAMES
                    and name not in KNOWN_FUNCTIONS):
                return f"unknown variable '{name}'"
    return None


class ExpressionError(Exception):
    pass


class Evaluator:
    """Evaluates expressions against a variable dict + helper functions."""

    def __init__(self, variables: dict, functions: dict):
        self.variables = variables
        self.functions = dict(functions)
        # always-available helpers
        self.functions.setdefault("chance", lambda p: random.random() < p)
        self.functions.setdefault("randint", random.randint)
        self.functions.setdefault("min", min)
        self.functions.setdefault("max", max)
        self.functions.setdefault("abs", abs)
        self.functions.setdefault("int", int)
        self.functions.setdefault("len", len)
        # dice: d20() rolls one die; roll('2d6+3') rolls a full spec silently
        for sides in (4, 6, 8, 10, 12, 20, 100):
            self.functions.setdefault(
                f"d{sides}", lambda s=sides: random.randint(1, s))
        self.functions.setdefault("roll", roll_spec)

    def eval(self, expr: str):
        try:
            tree = ast.parse(expr, mode="eval")
        except SyntaxError as e:
            raise ExpressionError(f"Bad expression {expr!r}: {e}") from e
        for node in ast.walk(tree):
            if not isinstance(node, _ALLOWED_NODES):
                raise ExpressionError(
                    f"Disallowed syntax {type(node).__name__!r} in {expr!r}")
        return self._eval(tree.body)

    # ------------------------------------------------------------------ #
    def _eval(self, node):
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            if node.id in self.variables:
                return self.variables[node.id]
            if node.id in ("true", "True"):
                return True
            if node.id in ("false", "False"):
                return False
            if node.id in ("none", "None", "null"):
                return None
            raise ExpressionError(f"Unknown variable {node.id!r}")
        if isinstance(node, ast.BoolOp):
            if isinstance(node.op, ast.And):
                result = True
                for v in node.values:
                    result = self._eval(v)
                    if not result:
                        return result
                return result
            else:  # Or
                for v in node.values:
                    result = self._eval(v)
                    if result:
                        return result
                return result
        if isinstance(node, ast.UnaryOp):
            val = self._eval(node.operand)
            if isinstance(node.op, ast.Not):
                return not val
            if isinstance(node.op, ast.USub):
                return -val
            return +val
        if isinstance(node, ast.BinOp):
            return _BIN_OPS[type(node.op)](self._eval(node.left),
                                           self._eval(node.right))
        if isinstance(node, ast.Compare):
            left = self._eval(node.left)
            for op, comparator in zip(node.ops, node.comparators):
                right = self._eval(comparator)
                if not _CMP_OPS[type(op)](left, right):
                    return False
                left = right
            return True
        if isinstance(node, ast.IfExp):
            return self._eval(node.body) if self._eval(node.test) \
                else self._eval(node.orelse)
        if isinstance(node, (ast.List, ast.Tuple)):
            return [self._eval(e) for e in node.elts]
        if isinstance(node, ast.Subscript):
            container = self._eval(node.value)
            key = self._eval(node.slice)
            return container[key]
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name):
                raise ExpressionError("Only simple function calls are allowed")
            fname = node.func.id
            if fname not in self.functions:
                raise ExpressionError(f"Unknown function {fname!r}")
            args = [self._eval(a) for a in node.args]
            return self.functions[fname](*args)
        raise ExpressionError(f"Unsupported node {type(node).__name__!r}")
