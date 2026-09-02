"""Shared helpers. See tests/golden/README.md for how the fixtures were captured."""

import os

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


# --------------------------------------------------------------- live tests

#: Tests that call a paid API run only when this is set.
LIVE_ENV_VAR = "OMNIBRIDGE_LIVE_TESTS"


def live_tests_enabled() -> bool:
    """
    Whether tests may spend money.

    Deliberately not "is a key present". A .env is loaded automatically on import, so
    keying off credentials meant that merely *having* a configured checkout turned the
    live tests on — the suite would start billing an account because a config file
    existed, which is not a decision a test run should make on the user's behalf.
    """
    return os.environ.get(LIVE_ENV_VAR, "").strip().lower() in ("1", "true", "yes")


def requires_live(*credential_vars):
    """Skip marker for a test that calls a paid API."""
    import pytest

    if not live_tests_enabled():
        return pytest.mark.skip(reason=f"live API test; set {LIVE_ENV_VAR}=1 to run (costs money)")
    missing = [v for v in credential_vars if not os.environ.get(v)]
    if missing:
        return pytest.mark.skip(reason=f"{LIVE_ENV_VAR} is set but {' / '.join(missing)} is not")
    return pytest.mark.usefixtures()
