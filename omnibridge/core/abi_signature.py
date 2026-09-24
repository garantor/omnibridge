"""
Parsing a function signature into its name and top-level argument types.

A selector is the keccak of the signature text, so the text has to be canonical: no
spaces, no aliases. `transfer(address,uint)` and `transfer(address,uint256)` name the
same Solidity function and hash to different selectors, and only the second is the one
deployed. Splitting on commas is not enough either: a struct argument is a tuple type,
`exactInputSingle((address,address,uint24,...))`, whose commas are not argument
boundaries.

This is a format standard (the ABI grammar), not protocol knowledge.
"""

import re
from dataclasses import dataclass
from typing import List

from eth_abi.grammar import TupleType, normalize, parse as parse_type

_NAME = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")

#: The elementary types in the ABI specification. The grammar parser accepts any word
#: here (`adress` parses), and only this set hashes to a selector anything implements.
_ABI_BASES = {"uint", "int", "address", "bool", "bytes", "string", "fixed", "ufixed", "function"}


def _check_bases(node) -> None:
    if isinstance(node, TupleType):
        for component in node.components:
            _check_bases(component)
        return
    if node.base not in _ABI_BASES:
        raise SignatureError(f"{node.base!r} is not an ABI type.")
    node.validate()


class SignatureError(ValueError):
    """The text is not a canonical ABI function signature."""


@dataclass(frozen=True)
class Signature:
    name: str
    types: List[str]

    @property
    def canonical(self) -> str:
        return f"{self.name}({','.join(self.types)})"


def split_top_level(inner: str) -> List[str]:
    """Split an argument list on the commas that are not inside a tuple."""
    parts: List[str] = []
    depth = 0
    current = ""
    for ch in inner:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth < 0:
                raise SignatureError("Unbalanced parentheses in the argument list.")
        if ch == "," and depth == 0:
            parts.append(current)
            current = ""
        else:
            current += ch
    if depth != 0:
        raise SignatureError("Unbalanced parentheses in the argument list.")
    if current or parts:
        parts.append(current)
    return parts


def parse(signature: str) -> Signature:
    """
    Parse `name(type,...)`, refusing anything whose selector would not be the deployed one.

    Raises SignatureError naming what is wrong.
    """
    text = signature or ""
    if any(ch.isspace() for ch in text):
        raise SignatureError(f"{text!r} contains whitespace; the canonical form has none.")
    if "(" not in text or not text.endswith(")"):
        raise SignatureError(f"{text!r} is not of the form name(type,...).")

    name = text[: text.index("(")]
    if not _NAME.match(name):
        raise SignatureError(f"{name!r} is not a valid function name.")

    types = split_top_level(text[text.index("(") + 1 : -1])
    for t in types:
        if not t:
            raise SignatureError(f"{text!r} has an empty argument type.")
        try:
            node = parse_type(t)
            canonical = normalize(t)
        except Exception as exc:
            raise SignatureError(f"{t!r} is not an ABI type: {exc}") from exc
        # Before validation, so an alias is reported as what it is, with the fix.
        if canonical != t:
            raise SignatureError(
                f"{t!r} is not canonical; the selector is computed from {canonical!r}."
            )
        try:
            _check_bases(node)
        except SignatureError:
            raise
        except Exception as exc:
            raise SignatureError(f"{t!r} is not an ABI type: {exc}") from exc
    return Signature(name=name, types=types)
