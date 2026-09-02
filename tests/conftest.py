"""Shared helpers. See tests/golden/README.md for how the fixtures were captured."""

import tokenize

import pytest


def code_only(path):
    """
    Source with comments and string literals stripped.

    Absence assertions need this. The comments that record *what* was deleted quote the
    deleted strings verbatim — that is the point of them — so a substring check over the
    raw file fails on its own documentation.
    """
    kept = []
    with open(path, "rb") as fh:
        for tok in tokenize.tokenize(fh.readline):
            if tok.type in (tokenize.COMMENT, tokenize.STRING):
                continue
            kept.append(tok.string)
    return " ".join(kept)


@pytest.fixture(name="code_only")
def code_only_fixture():
    return code_only
