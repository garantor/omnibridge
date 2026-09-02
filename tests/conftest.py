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


# ------------------------------------------------------- hermetic environment

#: Everything that steers provider selection, model choice or credentials.
_CONFIG_PREFIXES = ("OMNIBRIDGE_",)
_CREDENTIAL_VARS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_WORKSPACE_ID",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
)


@pytest.fixture(autouse=True)
def hermetic_env(monkeypatch, request):
    """
    Run every test against an empty configuration.

    A .env is loaded on import, so without this the suite inherits whatever the
    developer happens to have configured — and the assertions that matter here are
    about what the code does with a *given* configuration. Three tests started failing
    the moment a real .env existed, not because the code changed but because the
    machine did. A test that passes only on an unconfigured checkout is not a test.

    Live tests opt out: they need the real credentials, and they only run when
    OMNIBRIDGE_LIVE_TESTS says so.
    """
    if live_tests_enabled():
        yield
        return

    for name in list(os.environ):
        if name in _CREDENTIAL_VARS or name.startswith(_CONFIG_PREFIXES):
            monkeypatch.delenv(name, raising=False)
    # Belt and braces: nothing under test should re-read the file mid-run either.
    monkeypatch.setenv("OMNIBRIDGE_NO_DOTENV", "1")
    yield
