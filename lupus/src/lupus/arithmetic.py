"""Bounded exact integer arithmetic for read-only requests. No eval, I/O or model call.

Only a complete arithmetic request is eligible. Non-integer division, unsupported syntax,
undefined operations and excessive work return None so the ordinary answer path can explain.
"""
from __future__ import annotations

import ast
import re

MAX_CHARS, MAX_NODES, MAX_BITS, MAX_EXPONENT = 256, 64, 4096, 1024
_REPLY = re.compile(r'\s*(?:reply|return|answer)\s+(?:with\s+)?only\s+(?:the\s+)?integer\.?$', re.I)
_PREFIX = re.compile(r'^(?:what is|calculate|compute|evaluate)\s+', re.I)
_SUFFIX = re.compile(r'\s*(?:계산해(?:줘|주세요)?|계산|(?:은|는)\s*얼마(?:야|인가요|입니까))[.?!]?\s*$')
_EXPRESSION = re.compile(r'(?=.*\d)[\d\s()+*/%×÷−-]+\Z', re.ASCII)


def expression(request: str) -> str | None:
    text = _REPLY.sub('', request.strip()).strip()
    prefixed = bool(_PREFIX.match(text))
    text = _PREFIX.sub('', text)
    text = _SUFFIX.sub('', text).strip()
    if text.endswith('?') or (prefixed and text.endswith('.')):
        text = text[:-1].strip()
    if text.endswith('=') and not text.endswith('=='):
        text = text[:-1].strip()
    if not text or len(text) > MAX_CHARS or not _EXPRESSION.fullmatch(text):
        return None
    return text.replace('×', '*').replace('÷', '/').replace('−', '-')


def calculate(request: str) -> str | None:
    text = expression(request)
    if text is None:
        return None

    def bounded(value: int) -> int:
        if value.bit_length() > MAX_BITS:
            raise ValueError('result limit')
        return value

    def visit(node: ast.AST) -> int:
        if isinstance(node, ast.Constant) and type(node.value) is int:
            return bounded(node.value)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            value = visit(node.operand)
            return value if isinstance(node.op, ast.UAdd) else -value
        if not isinstance(node, ast.BinOp):
            raise ValueError('unsupported expression')
        left, right = visit(node.left), visit(node.right)
        if isinstance(node.op, ast.Add):
            return bounded(left + right)
        if isinstance(node.op, ast.Sub):
            return bounded(left - right)
        if isinstance(node.op, ast.Mult):
            return bounded(left * right)
        if isinstance(node.op, ast.Div):
            if right == 0 or left % right:
                raise ValueError('not exact integer division')
            return bounded(left // right)
        if isinstance(node.op, ast.FloorDiv):
            return bounded(left // right)
        if isinstance(node.op, ast.Mod):
            return bounded(left % right)
        if isinstance(node.op, ast.Pow):
            if not 0 <= right <= MAX_EXPONENT or max(1, left.bit_length()) * right > MAX_BITS:
                raise ValueError('exponent limit')
            return bounded(left ** right)
        raise ValueError('unsupported operator')

    try:
        tree = ast.parse(text, mode='eval')
        if sum(1 for _ in ast.walk(tree)) > MAX_NODES:
            return None
        return str(visit(tree.body))
    except (SyntaxError, ValueError, ArithmeticError, RecursionError):
        return None
