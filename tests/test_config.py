"""
.env loading.

Two properties matter, both about a config file not quietly doing the wrong thing: a
real environment variable beats the file, and a file that sets nothing says so.
"""

import pytest

from omnibridge.config import find_env_file, load_env, parse_env_file


def _write(tmp_path, text):
    path = tmp_path / ".env"
    path.write_text(text, encoding="utf-8")
    return path


# ------------------------------------------------------------------- parsing

def test_parses_the_ordinary_forms(tmp_path):
    path = _write(tmp_path, """
# a comment

export ANTHROPIC_API_KEY="sk-ant-quoted"
OMNIBRIDGE_PROVIDER=anthropic
OMNIBRIDGE_MAX_RETRIES = 5
EMPTY=
""")
    values, complaints = parse_env_file(path)
    assert values["ANTHROPIC_API_KEY"] == "sk-ant-quoted"   # quotes stripped
    assert values["OMNIBRIDGE_PROVIDER"] == "anthropic"
    assert values["OMNIBRIDGE_MAX_RETRIES"] == "5"          # whitespace trimmed
    assert values["EMPTY"] == ""
    assert not complaints


def test_a_bare_secret_is_reported_rather_than_skipped(tmp_path):
    """
    The real case this was written for: a key pasted into .env on its own, with no
    NAME=. It sets nothing. Skipping it silently leaves a file that looks configured,
    an app that is not, and a "missing credentials" error that appears to contradict
    the file sitting right there.
    """
    path = _write(tmp_path, "sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAA\n")
    values, complaints = parse_env_file(path)

    assert values == {}
    assert len(complaints) == 1
    assert "sets nothing" in complaints[0]
    assert "needs a name" in complaints[0]


def test_a_malformed_file_warns_that_it_configured_nothing(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("OMNIBRIDGE_NO_DOTENV", raising=False)
    path = _write(tmp_path, "sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAA\n")

    assert load_env(path) == {}
    out = capsys.readouterr().out
    assert "set no variables" in out
    assert "as though it is not there" in out


# --------------------------------------------------------------- precedence

def test_a_real_environment_variable_wins(tmp_path, monkeypatch):
    """
    A value exported in the shell is a deliberate act for this run; the file is a
    default. `ANTHROPIC_API_KEY=other python server.py` must use `other`, or a request
    silently bills the wrong account.
    """
    monkeypatch.delenv("OMNIBRIDGE_NO_DOTENV", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-shell")
    path = _write(tmp_path, "ANTHROPIC_API_KEY=from-file\nOTHER=from-file\n")

    applied = load_env(path, quiet=True)

    import os
    assert os.environ["ANTHROPIC_API_KEY"] == "from-shell"
    assert "ANTHROPIC_API_KEY" not in applied
    assert os.environ["OTHER"] == "from-file"


def test_override_is_available_but_not_the_default(tmp_path, monkeypatch):
    monkeypatch.delenv("OMNIBRIDGE_NO_DOTENV", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-shell")
    path = _write(tmp_path, "ANTHROPIC_API_KEY=from-file\n")

    load_env(path, override=True, quiet=True)

    import os
    assert os.environ["ANTHROPIC_API_KEY"] == "from-file"


def test_the_kill_switch_skips_the_file(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIBRIDGE_NO_DOTENV", "1")
    path = _write(tmp_path, "SOMETHING=value\n")
    assert load_env(path) == {}


# ----------------------------------------------------------------- discovery

def test_finds_a_dotenv_in_a_parent_directory(tmp_path, monkeypatch):
    """The server should be runnable from a subdirectory."""
    (tmp_path / ".env").write_text("A=1\n", encoding="utf-8")
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)

    assert find_env_file() == tmp_path / ".env"


def test_no_dotenv_is_not_an_error(tmp_path, monkeypatch):
    monkeypatch.delenv("OMNIBRIDGE_NO_DOTENV", raising=False)
    monkeypatch.chdir(tmp_path)
    assert find_env_file() is None
    assert load_env() == {}


# -------------------------------------------------------------------- safety

def test_a_secret_value_is_never_printed(tmp_path, capsys, monkeypatch):
    """
    Loading is reported so the user knows what took effect, but a credential is named,
    not echoed — the report goes to stdout, which lands in logs and screenshares.
    """
    monkeypatch.delenv("OMNIBRIDGE_NO_DOTENV", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    path = _write(tmp_path, "ANTHROPIC_API_KEY=sk-ant-supersecret\nOMNIBRIDGE_PROVIDER=anthropic\n")

    load_env(path)
    out = capsys.readouterr().out

    assert "sk-ant-supersecret" not in out
    assert "ANTHROPIC_API_KEY (set)" in out
    # A non-secret is shown, because knowing which provider was selected is the point.
    assert "OMNIBRIDGE_PROVIDER=anthropic" in out


def test_dotenv_is_gitignored():
    """
    The reason this file exists. An API key was committed because .env was untracked
    and absent from .gitignore, and `git add -A` does not distinguish the two.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    ignored = (root / ".gitignore").read_text().splitlines()
    assert ".env" in [line.strip() for line in ignored]
