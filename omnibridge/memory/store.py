"""
Versioned Markdown documents with YAML frontmatter: the storage under skills and cards.

Both are procedural memory written by a running system, read back by later runs, and
reviewed by people as diffs. They need the same four things, so they share them here
rather than each carrying a copy:

  - frontmatter parsing and rendering;
  - every overwrite retains what it replaced, without depending on git;
  - writes are atomic, so a reader never sees half a file;
  - read-modify-write holds a lock, because `server.py` can run two graphs at once and
    a card is shared by every route of its protocol.
"""

import contextlib
import os
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, Optional, Tuple

import yaml

try:  # POSIX only. Without it, writes are still atomic, just not serialised.
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]


_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)

#: Retained history lives beside the document and is never a candidate for loading.
REVISIONS_DIR = "revisions"


def utc_today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


class VersionedStore:
    """Base for a directory of `<name>/<DOCUMENT>` files with retained revisions."""

    #: The filename every document in this store has, e.g. "SKILL.md".
    document_name: str = ""

    @staticmethod
    def parse_document(file_path: Path) -> Optional[Tuple[Dict[str, Any], str]]:
        """Frontmatter and body, or None when the file has no frontmatter block."""
        raw_text = Path(file_path).read_text(encoding="utf-8")
        match = _FRONTMATTER.search(raw_text)
        if not match:
            return None
        data = yaml.safe_load(match.group(1))
        if not isinstance(data, dict):
            return None
        return data, match.group(2)

    @staticmethod
    def render_document(frontmatter: Dict[str, Any], body: str) -> str:
        header = yaml.dump(frontmatter, sort_keys=False, allow_unicode=False)
        return f"---\n{header}---\n\n{body.strip()}\n"

    def iter_documents(self, directories: Iterable[Path]) -> Iterator[Path]:
        """Every document under these directories, skipping retained revisions."""
        wanted = self.document_name.lower()
        for directory in directories:
            if not directory.exists():
                continue
            for root, dirs, files in os.walk(directory):
                dirs[:] = [d for d in dirs if d != REVISIONS_DIR]
                for name in files:
                    if name.lower() == wanted:
                        yield Path(root) / name

    def retain_revision(self, file_path: Path) -> Optional[str]:
        """
        Copy the current document aside before it is replaced.

        Human approval is a good filter, not an infallible one, so every overwrite stays
        reversible without depending on the repository being under git. Names are unique
        even within one second: a card can be written several times in a single run, and
        a second copy with the same stamp would silently replace the first.
        """
        file_path = Path(file_path)
        if not file_path.exists():
            return None
        revisions_dir = file_path.parent / REVISIONS_DIR
        revisions_dir.mkdir(parents=True, exist_ok=True)

        stem, suffix = os.path.splitext(self.document_name or file_path.name)
        stamp = utc_stamp()
        archived = revisions_dir / f"{stem}.{stamp}{suffix}"
        n = 2
        while archived.exists():
            archived = revisions_dir / f"{stem}.{stamp}-{n}{suffix}"
            n += 1
        shutil.copy2(file_path, archived)
        return str(archived.relative_to(file_path.parent))

    @staticmethod
    def write_atomic(file_path: Path, text: str) -> None:
        """Replace the file in one step, so a concurrent reader sees old or new, never half."""
        file_path = Path(file_path)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=str(file_path.parent), prefix=f".{file_path.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, file_path)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)
            raise

    @staticmethod
    @contextlib.contextmanager
    def locked(file_path: Path) -> Iterator[None]:
        """
        Hold an exclusive lock on this document for a read-modify-write.

        The lock is on a sibling file rather than the document, because the document is
        replaced by rename and a lock on the old inode would guard nothing.
        """
        file_path = Path(file_path)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = file_path.parent / f".{file_path.name}.lock"
        with open(lock_path, "a+") as handle:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
