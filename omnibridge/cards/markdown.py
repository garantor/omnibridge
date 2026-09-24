"""
CARD.md <-> Card.

The file is YAML frontmatter plus three Markdown sections, in the layout of
docs/AGENTIC_OMNIBRIDGE.md:

    ## Fast-Path Interfaces
    - **Deposit For Burn:** `depositForBurn(uint256,uint32,bytes32,address)`

    ## Learned Chain Cache (Agent Proposes, System Verifies)
    | Network | Chain ID | CCTP Domain | Token Messenger Address | Status | Verified Date | Evidence |
    | :--- | ... |
    | Base | 8453 | 6 (confirmed) | `0x1682...` | `unverified` | — | https://developers.circle.com/... |

    ## Learned Failure Modes & Revert Fixes
    - *Revert Reason:* `Invalid destination domain`. (seen 1x)
    - *Fix:* Base CCTP domain is `6`, not `8453`.

Parsing is strict. Anything it does not recognise — an unknown section, a line of prose
inside a section, a truncated address like `0x1234...abcd`, an unknown status — makes the
card unreadable and says where. The alternative, skipping what it cannot read, would let
text into the file that no check ever looked at, and silently drop it on the next write.

`render` is the inverse, and `parse(render(card)) == card` for every valid card.
"""

import re
from typing import Any, Dict, List, Optional, Tuple

import yaml

from omnibridge.cards.models import (
    Card,
    ChainRow,
    FailureMode,
    Interface,
    RowStatus,
    slug,
)

INTERFACES = "Fast-Path Interfaces"
CHAIN_CACHE = "Learned Chain Cache (Agent Proposes, System Verifies)"
FAILURE_MODES = "Learned Failure Modes & Revert Fixes"

_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)
_FRONT_KEYS = {
    "protocol_id", "protocol_name", "docs", "supported_chains", "supported_tokens",
    "card_version", "last_updated",
}
_INTERFACE = re.compile(
    r"^- \*\*(?P<label>[^*:]+):\*\* `(?P<sig>[^`]+)`(?: on (?P<target>[A-Za-z0-9 ]+))?$"
)
_REVERT = re.compile(r"^- \*Revert Reason:\* `(?P<revert>[^`]+)`\.?(?: \(seen (?P<seen>\d+)x\))?$")
_FIX = re.compile(r"^\s*- \*Fix:\* (?P<fix>.+)$")
_FIXED_COLUMNS = ("Network", "Chain ID", "Status", "Verified Date", "Evidence")
_EMPTY = ("", "-", "—")


class CardParseError(ValueError):
    """The file is not a valid card. The message names the line."""


def _cell(text: str) -> str:
    """A cell's value, with one surrounding pair of backticks removed."""
    text = text.strip()
    if len(text) >= 2 and text.startswith("`") and text.endswith("`"):
        text = text[1:-1].strip()
    return text


def _split_row(line: str) -> List[str]:
    line = line.strip()
    if not (line.startswith("|") and line.endswith("|")):
        raise CardParseError(f"not a table row: {line!r}")
    return [c.strip() for c in line[1:-1].split("|")]


def _sections(body: str) -> Tuple[Optional[str], Dict[str, List[Tuple[int, str]]]]:
    title = None
    sections: Dict[str, List[Tuple[int, str]]] = {}
    current: Optional[str] = None
    for n, raw in enumerate(body.splitlines(), start=1):
        line = raw.rstrip()
        if not line.strip():
            continue
        if line.startswith("# ") and title is None and current is None:
            title = line[2:].strip()
            continue
        if line.startswith("## "):
            name = line[3:].strip()
            if name not in (INTERFACES, CHAIN_CACHE, FAILURE_MODES):
                raise CardParseError(f"line {n}: unknown section {name!r}.")
            if name in sections:
                raise CardParseError(f"line {n}: section {name!r} appears twice.")
            current = name
            sections[name] = []
            continue
        if current is None:
            raise CardParseError(f"line {n}: text outside any section: {line!r}")
        sections[current].append((n, line))
    return title, sections


def _parse_interfaces(lines: List[Tuple[int, str]]) -> List[Dict[str, Any]]:
    out = []
    for n, line in lines:
        m = _INTERFACE.match(line)
        if not m:
            raise CardParseError(f"line {n}: not an interface bullet: {line!r}")
        target = m.group("target")
        out.append({
            "label": m.group("label").strip(),
            "signature": m.group("sig").strip(),
            "target": slug(target) if target else None,
        })
    return out


def _parse_table(lines: List[Tuple[int, str]]) -> Tuple[Optional[str], List[str], List[Dict[str, Any]]]:
    if not lines:
        return None, [], []
    n0, header_line = lines[0]
    headers = _split_row(header_line)
    for fixed in ("Network", "Chain ID", "Status", "Verified Date"):
        if fixed not in headers:
            raise CardParseError(f"line {n0}: the chain cache table needs a {fixed!r} column.")
    address_labels = [h[: -len(" Address")] for h in headers if h.endswith(" Address")]
    other = [h for h in headers if h not in _FIXED_COLUMNS and not h.endswith(" Address")]
    if len(other) > 1:
        raise CardParseError(f"line {n0}: unrecognised columns {other}.")
    dest_label = other[0] if other else None

    if len(lines) < 2 or not all(set(c) <= set(":- ") for c in _split_row(lines[1][1])):
        raise CardParseError(f"line {n0 + 1}: expected the table's separator row.")

    rows = []
    for n, line in lines[2:]:
        cells = _split_row(line)
        if len(cells) != len(headers):
            raise CardParseError(f"line {n}: {len(cells)} cells, header has {len(headers)}.")
        values = dict(zip(headers, cells))
        try:
            chain_id = int(_cell(values["Chain ID"]))
        except ValueError:
            raise CardParseError(f"line {n}: chain id {values['Chain ID']!r} is not an integer.")

        dest_id, confirmed = None, False
        if dest_label:
            raw = _cell(values[dest_label])
            if raw.endswith("(confirmed)"):
                confirmed, raw = True, raw[: -len("(confirmed)")].strip()
            dest_id = None if raw in _EMPTY else raw

        addresses = {}
        for label in address_labels:
            raw = _cell(values[f"{label} Address"])
            if raw not in _EMPTY:
                addresses[slug(label)] = raw

        status_raw = _cell(values["Status"])
        try:
            status = RowStatus(status_raw)
        except ValueError:
            raise CardParseError(f"line {n}: unknown status {status_raw!r}.")

        date = _cell(values["Verified Date"])
        evidence = _cell(values.get("Evidence", ""))
        rows.append({
            "network": _cell(values["Network"]),
            "chain_id": chain_id,
            "dest_id": dest_id,
            "dest_confirmed": confirmed,
            "addresses": addresses,
            "status": status,
            "verified_date": None if date in _EMPTY else date,
            "evidence": None if evidence in _EMPTY else evidence,
            "_line": n,
        })
    return dest_label, address_labels, rows


def _parse_failures(lines: List[Tuple[int, str]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for n, line in lines:
        m = _REVERT.match(line)
        if m:
            out.append({"revert": m.group("revert").strip(), "seen": int(m.group("seen") or 0)})
            continue
        m = _FIX.match(line)
        if m:
            if not out or out[-1].get("fix"):
                raise CardParseError(f"line {n}: a Fix must follow its Revert Reason.")
            out[-1]["fix"] = m.group("fix").strip()
            continue
        raise CardParseError(f"line {n}: not a failure-mode bullet: {line!r}")
    return out


def parse(text: str) -> Card:
    """Parse a CARD.md, or raise CardParseError naming what is wrong."""
    m = _FRONTMATTER.match(text)
    if not m:
        raise CardParseError("no YAML frontmatter block.")
    front = yaml.safe_load(m.group(1)) or {}
    if not isinstance(front, dict):
        raise CardParseError("frontmatter is not a mapping.")
    unknown = set(front) - _FRONT_KEYS
    if unknown:
        raise CardParseError(f"unknown frontmatter keys {sorted(unknown)}.")

    _title, sections = _sections(m.group(2))
    dest_label, address_labels, rows = _parse_table(sections.get(CHAIN_CACHE, []))

    try:
        row_models = []
        for row in rows:
            line = row.pop("_line")
            try:
                row_models.append(ChainRow(**row))
            except ValueError as exc:
                raise CardParseError(f"line {line}: {exc}") from exc
        return Card(
            **front,
            interfaces=[Interface(**i) for i in _parse_interfaces(sections.get(INTERFACES, []))],
            dest_id_label=dest_label,
            address_labels=address_labels,
            rows=row_models,
            failure_modes=[FailureMode(**f) for f in _parse_failures(sections.get(FAILURE_MODES, []))],
        )
    except CardParseError:
        raise
    except ValueError as exc:
        raise CardParseError(str(exc)) from exc


def render(card: Card) -> str:
    """The CARD.md text for a card. Deterministic, so diffs show only real changes."""
    front = {
        "protocol_id": card.protocol_id,
        "protocol_name": card.protocol_name,
        "docs": list(card.docs),
        "supported_chains": list(card.supported_chains),
        "supported_tokens": list(card.supported_tokens),
        "card_version": card.card_version,
        "last_updated": card.last_updated,
    }
    out = ["---", yaml.dump(front, sort_keys=False).rstrip(), "---", ""]
    out += [f"# Protocol Knowledge: {card.protocol_name}", ""]

    out += [f"## {INTERFACES}"]
    for iface in card.interfaces:
        target = ""
        if iface.target and len(card.roles) > 1:
            label = next(l for l in card.address_labels if slug(l) == iface.target)
            target = f" on {label}"
        out.append(f"- **{iface.label}:** `{iface.signature}`{target}")
    out.append("")

    out += [f"## {CHAIN_CACHE}"]
    headers = ["Network", "Chain ID"]
    if card.dest_id_label:
        headers.append(card.dest_id_label)
    headers += [f"{label} Address" for label in card.address_labels]
    headers += ["Status", "Verified Date", "Evidence"]
    out.append("| " + " | ".join(headers) + " |")
    out.append("| " + " | ".join(":---" for _ in headers) + " |")
    for row in card.rows:
        cells = [row.network, str(row.chain_id)]
        if card.dest_id_label:
            dest = row.dest_id or "—"
            cells.append(f"{dest} (confirmed)" if row.dest_confirmed and row.dest_id else dest)
        for label in card.address_labels:
            addr = row.addresses.get(slug(label))
            cells.append(f"`{addr}`" if addr else "—")
        cells.append(f"`{row.status.value}`")
        cells.append(row.verified_date or "—")
        if row.evidence and row.evidence.startswith("0x"):
            cells.append(f"`{row.evidence}`")
        else:
            cells.append(row.evidence or "—")
        out.append("| " + " | ".join(cells) + " |")
    out.append("")

    out += [f"## {FAILURE_MODES}"]
    for mode in card.failure_modes:
        seen = f" (seen {mode.seen}x)" if mode.seen else ""
        out.append(f"- *Revert Reason:* `{mode.revert}`.{seen}")
        if mode.fix:
            out.append(f"- *Fix:* {mode.fix}")
    out.append("")
    return "\n".join(out)
