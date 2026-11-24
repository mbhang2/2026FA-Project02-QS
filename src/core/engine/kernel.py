"""Restricted per-event strategy kernels, compiled by Numba for the native event loop.

A kernel is one function:

    def kernel(event, book, state, orders):
        ...
        return number_of_orders

`event` and `book` are read-only copies for this event. `state` is the kernel's own float
buffer. Each row of `orders` is one action. A kernel receives no session array, so it has no
later price to index. Numba does not check bounds by default, so this module turns them on and
treats any failure as a stopped run instead of a silent zero.

The source is checked before compilation. It may use arithmetic, comparisons, `if`, and `for`
over a small literal range. It may not import, call arbitrary functions, read attributes,
loop without a bound, or write to `event` or `book`.
"""

from __future__ import annotations

import ast
import ctypes

CONSTANTS = {
    "E_TS": 0, "E_SESSION": 2, "E_SIZE": 3, "E_POSITION": 4, "E_SIDE": 5,
    "E_ACTIONABLE": 6, "E_N_WORKING": 7, "E_WORKING": 8, "E_KIND": 40,
    "EVENT_TRADE": 0, "EVENT_BOOK": 1,
    "B_PRICE": 0, "B_OK": 1, "B_N_BID": 2, "B_N_ASK": 3, "B_BID_PX": 4, "B_BID_QTY": 9,
    "B_ASK_PX": 14, "B_ASK_QTY": 19,
    "O_OP": 0, "O_KIND": 1, "O_TIF": 2, "O_SIZE": 3, "O_LIMIT": 4, "O_ORDER_ID": 5,
    "OP_BUY": 1, "OP_SELL": 2, "OP_CANCEL": 3, "KIND_MARKET": 0, "KIND_LIMIT": 1,
    "TIF_DAY": 0, "TIF_GTC": 1,
}
ARGUMENTS = ("event", "book", "state", "orders")
ALLOWED_CALLS = {"abs", "min", "max", "int", "float", "range"}
MAX_RANGE = 64
MAX_LOOP_DEPTH = 2
MAX_STATE = 4096
_ALLOWED_NODES = (
    ast.Module, ast.FunctionDef, ast.arguments, ast.arg, ast.Return, ast.Assign, ast.AugAssign,
    ast.For, ast.If, ast.Expr, ast.Pass, ast.Break, ast.Continue, ast.Name, ast.Load, ast.Store,
    ast.Constant, ast.Subscript, ast.Tuple, ast.Slice, ast.BinOp, ast.UnaryOp, ast.BoolOp,
    ast.Compare, ast.IfExp, ast.Call, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod,
    ast.USub, ast.UAdd, ast.Not, ast.And, ast.Or, ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
)


class KernelRejected(ValueError):
    """The source is outside the kernel language."""


def _root(node: ast.AST) -> str | None:
    while isinstance(node, ast.Subscript):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def _bounded_literal(node: ast.AST) -> bool:
    return (isinstance(node, ast.Constant) and type(node.value) is int
            and 0 <= node.value <= MAX_RANGE)


def _check_range(call: ast.Call) -> None:
    if call.keywords or not 1 <= len(call.args) <= 2 or not all(_bounded_literal(a) for a in call.args):
        raise KernelRejected(f"range() needs one or two integer literals up to {MAX_RANGE}")


def check(source: str) -> ast.Module:
    """Parse and validate a kernel, naming the first thing outside the language."""
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise KernelRejected(f"not valid Python: {exc.msg}") from None
    if len(tree.body) != 1 or not isinstance(tree.body[0], ast.FunctionDef) or tree.body[0].name != "kernel":
        raise KernelRejected("the source must be exactly one function named kernel")
    function = tree.body[0]
    args = function.args
    if function.decorator_list or function.returns or args.posonlyargs or args.kwonlyargs or args.vararg \
            or args.kwarg or args.defaults or tuple(a.arg for a in args.args) != ARGUMENTS \
            or any(a.annotation for a in args.args):
        raise KernelRejected("the signature must be kernel(event, book, state, orders)")
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise KernelRejected(f"{type(node).__name__} is not allowed in a kernel")
        if isinstance(node, ast.Name) and node.id.startswith("__"):
            raise KernelRejected("dunder names are not allowed")
        if isinstance(node, ast.Constant) and not (node.value is None or type(node.value) in (bool, int, float)):
            raise KernelRejected("only numeric constants are allowed")
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in ALLOWED_CALLS or node.keywords:
                raise KernelRejected("only abs, min, max, int, float and range may be called")
            if node.func.id == "range":
                _check_range(node)
        if isinstance(node, ast.For) and not (isinstance(node.iter, ast.Call) and isinstance(node.iter.func, ast.Name)
                                              and node.iter.func.id == "range"):
            raise KernelRejected("for loops must iterate over range()")
        if isinstance(node, ast.For) and not isinstance(node.target, ast.Name):
            raise KernelRejected("a loop target must be one name")
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AugAssign) else []
        for target in targets:
            if isinstance(target, ast.Tuple):
                raise KernelRejected("assign one value at a time")
            if isinstance(target, ast.Subscript) and _root(target) not in ("state", "orders"):
                raise KernelRejected("a kernel may write only to state and orders")
            if isinstance(target, ast.Name) and (target.id in ARGUMENTS or target.id in CONSTANTS
                                                 or target.id in ALLOWED_CALLS):
                raise KernelRejected(f"{target.id} cannot be reassigned")

    def depth(node, level=0):
        here = level + isinstance(node, ast.For)
        if here > MAX_LOOP_DEPTH:
            raise KernelRejected(f"loops may nest at most {MAX_LOOP_DEPTH} deep")
        for child in ast.iter_child_nodes(node):
            depth(child, here)

    depth(function)
    assigned = {t.id for n in ast.walk(function) if isinstance(n, (ast.Assign, ast.For, ast.AugAssign))
                for t in (n.targets if isinstance(n, ast.Assign) else [n.target]) if isinstance(t, ast.Name)}
    known = set(ARGUMENTS) | set(CONSTANTS) | ALLOWED_CALLS | assigned
    unknown = sorted({n.id for n in ast.walk(function) if isinstance(n, ast.Name)} - known)
    if unknown:
        raise KernelRejected(f"unknown names: {unknown}")
    return tree


class FcKernelUser(ctypes.Structure):
    _fields_ = [("kernel", ctypes.c_void_p), ("state", ctypes.c_void_p), ("state_len", ctypes.c_int32),
                ("error", ctypes.c_int32)]


class CompiledKernel:
    """A checked kernel, its native entry point, and its own zeroed state."""

    def __init__(self, source: str, *, state_size: int = 64):
        import numba
        import numpy as np
        from numba import carray, cfunc, types

        if not 1 <= state_size <= MAX_STATE:
            raise ValueError(f"state_size must be between 1 and {MAX_STATE}")
        check(source)
        namespace = dict(CONSTANTS)
        exec(compile(source, "<kernel>", "exec"), {"__builtins__": {
            name: __builtins__[name] if isinstance(__builtins__, dict) else getattr(__builtins__, name)
            for name in ALLOWED_CALLS}, **namespace}, namespace)
        user = namespace["kernel"]
        user.__globals__.update({k: v for k, v in CONSTANTS.items()})
        body = numba.njit(boundscheck=True)(user)

        signature = types.int32(types.CPointer(types.int64), types.CPointer(types.float64),
                                types.CPointer(types.float64), types.CPointer(types.float64),
                                types.int32, types.int32)

        @cfunc(signature)
        def entry(event_ptr, book_ptr, state_ptr, order_ptr, state_len, capacity):
            try:
                event = carray(event_ptr, (41,))
                book = carray(book_ptr, (24,))
                state = carray(state_ptr, (state_len,))
                orders = carray(order_ptr, (capacity, 6))
                return int(body(event, book, state, orders))
            except Exception:
                return -1

        self._entry = entry
        self.state = np.zeros(state_size, dtype=np.float64)
        self.user = FcKernelUser(ctypes.cast(entry.address, ctypes.c_void_p), self.state.ctypes.data,
                                 state_size, 0)

    @property
    def error(self) -> int:
        return int(self.user.error)

    def callback(self, lib: ctypes.CDLL) -> ctypes.c_void_p:
        return ctypes.cast(lib.fc_kernel_callback, ctypes.c_void_p)

    def user_pointer(self):
        return ctypes.byref(self.user)
