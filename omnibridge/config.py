"""
Environment loading from a .env file.

Two rules, both about not silently doing the wrong thing:

  - A real environment variable always wins. A value exported in the shell is a
    deliberate act for this run; a .env file is a default. Overriding the first with
    the second means `ANTHROPIC_API_KEY=other-key python server.py` quietly uses the
    file's key instead, which is the sort of surprise that ends with a request billed
    to the wrong account.

  - A line that sets nothing says so. A .env holding a bare secret with no `NAME=`
    prefix parses to zero variables, and the usual behaviour is to skip it in silence —
    leaving a file that looks configured, an app that is not, and an error message
    about missing credentials that appears to contradict the file sitting right there.
"""

import os
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ENV_FILENAME = ".env"

#: Anything that looks like a credential is never echoed, only named.
_SECRET_MARKERS = ("key", "token", "secret", "password", "credential")


def _is_secret(name: str) -> bool:
    lowered = name.lower()
    return any(marker in lowered for marker in _SECRET_MARKERS)


def find_env_file(start: Optional[Path] = None) -> Optional[Path]:
    """Nearest .env at or above `start`, so the server can be run from a subdirectory."""
    here = (start or Path.cwd()).resolve()
    for directory in (here, *here.parents):
        candidate = directory / ENV_FILENAME
        if candidate.is_file():
            return candidate
    return None


def parse_env_file(path: Path) -> Tuple[Dict[str, str], List[str]]:
    """
    Parse a .env into (values, complaints).

    Deliberately small: `NAME=value`, optional `export ` prefix, `#` comments, and
    matched surrounding quotes stripped. Anything else is reported rather than guessed
    at — a config file is not a place to infer intent.
    """
    values: Dict[str, str] = {}
    complaints: List[str] = []

    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()

        if "=" not in line:
            # The case worth naming: a bare secret pasted in on its own. It sets
            # nothing, and silence here reads as "configured" when it is not.
            looks_like_secret = len(line) > 20 and " " not in line
            complaints.append(
                f"{path}:{lineno}: no '=' on this line, so it sets nothing"
                + (
                    " — a bare value needs a name, e.g. ANTHROPIC_API_KEY=<value>"
                    if looks_like_secret
                    else ""
                )
            )
            continue

        name, _, value = line.partition("=")
        name = name.strip()
        if not name:
            complaints.append(f"{path}:{lineno}: empty variable name")
            continue

        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        values[name] = value

    return values, complaints


def load_env(
    path: Optional[Path] = None,
    override: bool = False,
    quiet: bool = False,
) -> Dict[str, str]:
    """
    Load a .env into os.environ and return what it applied.

    Existing variables are left alone unless `override` is set. Set
    OMNIBRIDGE_NO_DOTENV=1 to skip the file entirely.
    """
    if os.environ.get("OMNIBRIDGE_NO_DOTENV", "").strip().lower() in ("1", "true", "yes"):
        return {}

    env_path = path or find_env_file()
    if env_path is None:
        return {}

    try:
        values, complaints = parse_env_file(env_path)
    except OSError as exc:
        if not quiet:
            print(f"[omnibridge] could not read {env_path}: {exc}")
        return {}

    applied: Dict[str, str] = {}
    skipped: List[str] = []
    for name, value in values.items():
        if name in os.environ and not override:
            skipped.append(name)
            continue
        os.environ[name] = value
        applied[name] = value

    if not quiet:
        _report(env_path, applied, skipped, complaints, bool(values))
    return applied


def _report(
    env_path: Path,
    applied: Dict[str, str],
    skipped: List[str],
    complaints: List[str],
    had_values: bool,
) -> None:
    """Say what happened, naming variables but never printing their values."""
    for complaint in complaints:
        print(f"[omnibridge] warning: {complaint}")

    if not had_values:
        print(
            f"[omnibridge] warning: {env_path} set no variables. The app will behave as "
            "though it is not there."
        )
        return

    if applied:
        names = ", ".join(
            f"{n} (set)" if _is_secret(n) else f"{n}={applied[n]}" for n in sorted(applied)
        )
        print(f"[omnibridge] loaded {env_path}: {names}")
    if skipped:
        print(
            f"[omnibridge] {env_path}: kept the existing environment for "
            f"{', '.join(sorted(skipped))}"
        )
