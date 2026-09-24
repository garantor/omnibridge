"""
The protocol card: what the system knows about one protocol, as hypotheses.

A card lives at `protocols/<protocol_id>/CARD.md`. Its facts are in the Markdown body,
in the layout from docs/AGENTIC_OMNIBRIDGE.md: fast-path interfaces, a chain cache
table, and learned failure modes. `cards.markdown` parses that body into these types
and renders them back, and the parser is strict: a cell that does not parse to its type
makes the whole card unreadable. That strictness is what lets facts live in hand-editable
Markdown and still be safe to put in front of a model — the agent is only ever shown a
rendering of these parsed values, never the file's text.

Row statuses follow the lifecycle in the plan:

  unverified  passed the automated checks (bytecode, selector, documentation source)
              when it was added, but no approved broadcast has used it yet
  verified    its contract was called by a route that simulated, was approved by a
              human and was broadcast
  failed      a route calling its contract reverted while it was still unverified

A row's status is about its contract addresses. Its destination identifier is marked
`confirmed` separately, when a broadcast delivered *to* that chain using it — the two
are proved by different transactions, and one status cannot honestly cover both.
"""

import re
from enum import Enum
from typing import Dict, List, Optional

from pydantic import BaseModel, Field, field_validator, model_validator
from web3 import Web3

from omnibridge.core import abi_signature

#: Rows in the chain cache. A card is context for a model, and context has a budget.
MAX_ROWS = 64
#: Failure modes kept per card.
MAX_FAILURE_MODES = 32
#: Characters in any free-text field.
MAX_FREE_TEXT = 200
#: Characters in a network name or a label.
MAX_LABEL = 48

_PRINTABLE = re.compile(r"^[\x20-\x7e]*$")
_ID = re.compile(r"^[a-z0-9_]+$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TX_HASH = re.compile(r"^0x[0-9a-fA-F]{64}$")
_DEST_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,32}$")


class RowStatus(str, Enum):
    UNVERIFIED = "unverified"
    VERIFIED = "verified"
    FAILED = "failed"


def text_field(
    value: Optional[str], field: str, limit: int = MAX_FREE_TEXT, allow_code: bool = False
) -> Optional[str]:
    """
    Short, printable, one line, and no Markdown table or code delimiters.

    Refused rather than trimmed or escaped: this text is shown to every later run, and a
    field that can carry a pipe or a backtick can break out of the cell it was put in.
    `allow_code` permits backticks in text that is a bullet rather than a cell or a code
    span, such as a fix ("the domain is `6`, not `8453`").
    """
    if value is None:
        return None
    text = str(value).strip()
    if len(text) > limit:
        raise ValueError(f"{field} is {len(text)} characters; the limit is {limit}.")
    if not _PRINTABLE.match(text):
        raise ValueError(f"{field} may contain printable ASCII only, on one line.")
    if "|" in text or (not allow_code and "`" in text):
        raise ValueError(f"{field} may not contain '|'" + ("" if allow_code else " or '`'") + ".")
    return text


def slug(label: str) -> str:
    """'Token Messenger' -> 'token_messenger', 'CCTP Domain' -> 'cctp_domain'."""
    return re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")


def checksummed(value: str, field: str) -> str:
    try:
        return Web3.to_checksum_address(value)
    except Exception as exc:
        raise ValueError(f"{field} {value!r} is not a valid EVM address.") from exc


class Interface(BaseModel):
    """A function the protocol exposes, and which contract column it is called on."""

    label: str
    signature: str
    #: The address column this is called on, as a slug. None when the card has one.
    target: Optional[str] = None

    @field_validator("label")
    @classmethod
    def _label(cls, value: str) -> str:
        text = text_field(value, "interface label", MAX_LABEL)
        if not text or ":" in text or "*" in text:
            raise ValueError("an interface label may not be empty or contain ':' or '*'.")
        return text

    @field_validator("signature")
    @classmethod
    def _signature(cls, value: str) -> str:
        abi_signature.parse(value)
        return value

    @property
    def name(self) -> str:
        return abi_signature.parse(self.signature).name


class ChainRow(BaseModel):
    network: str
    chain_id: int
    #: The protocol's identifier for this chain as a destination, e.g. a CCTP domain.
    dest_id: Optional[str] = None
    #: A broadcast delivered to this chain using `dest_id`.
    dest_confirmed: bool = False
    #: Address column slug -> address.
    addresses: Dict[str, str] = Field(default_factory=dict)
    status: RowStatus = RowStatus.UNVERIFIED
    verified_date: Optional[str] = None
    #: A transaction hash (the broadcast that verified it) or an allowlisted URL (the
    #: documentation it was checked against when it was added).
    evidence: Optional[str] = None

    @field_validator("network")
    @classmethod
    def _network(cls, value: str) -> str:
        text = text_field(value, "network", MAX_LABEL)
        if not text:
            raise ValueError("network may not be empty.")
        return text

    @field_validator("dest_id")
    @classmethod
    def _dest_id(cls, value: Optional[str]) -> Optional[str]:
        if value is None or value == "":
            return None
        if not _DEST_ID.match(value):
            raise ValueError(f"destination identifier {value!r} is not a plain identifier.")
        return value

    @field_validator("addresses")
    @classmethod
    def _addresses(cls, value: Dict[str, str]) -> Dict[str, str]:
        return {role: checksummed(addr, f"{role} address") for role, addr in value.items()}

    @field_validator("verified_date")
    @classmethod
    def _date(cls, value: Optional[str]) -> Optional[str]:
        if value and not _DATE.match(value):
            raise ValueError(f"verified date {value!r} is not YYYY-MM-DD.")
        return value or None

    @field_validator("evidence")
    @classmethod
    def _evidence(cls, value: Optional[str]) -> Optional[str]:
        if not value:
            return None
        if _TX_HASH.match(value) or value.startswith("https://"):
            return text_field(value, "evidence", 300)
        raise ValueError(f"evidence {value!r} must be a transaction hash or an https URL.")

    @model_validator(mode="after")
    def _verified_has_proof(self) -> "ChainRow":
        if self.status is RowStatus.VERIFIED and not (self.evidence and _TX_HASH.match(self.evidence)):
            raise ValueError(
                f"{self.network} is verified but its evidence is not a transaction hash. "
                "Only an approved broadcast verifies a row, so it must name one."
            )
        return self


class FailureMode(BaseModel):
    """
    A revert, and what fixed it. The revert text is recorded by code from what the node
    said; a fix is proposed by the agent or written by a person.
    """

    revert: str
    fix: Optional[str] = None
    seen: int = 0

    @field_validator("revert")
    @classmethod
    def _revert(cls, value: str) -> str:
        text = text_field(value, "revert reason")
        if not text:
            raise ValueError("revert reason may not be empty.")
        return text

    @field_validator("fix")
    @classmethod
    def _fix(cls, value: Optional[str]) -> Optional[str]:
        return text_field(value, "fix", allow_code=True)

    @property
    def key(self) -> str:
        return normalise_revert(self.revert)


def normalise_revert(text: str) -> str:
    """The same revert seen twice is one failure mode."""
    return " ".join(str(text).lower().split()).rstrip(".")


def clean_revert(raw: str) -> str:
    """
    A node's revert message, reduced to something a card can hold.

    Nodes wrap the reason in their own framing ("execution reverted: ...", a Python
    exception repr), and some include characters a card refuses. Keep the reason.
    """
    text = str(raw or "")
    match = re.search(r"execution reverted:?\s*([^'\"}]*)", text)
    if match and match.group(1).strip():
        text = match.group(1)
    text = re.sub(r"[^\x20-\x7e]", " ", text).replace("|", "/").replace("`", "'")
    return " ".join(text.split())[:MAX_FREE_TEXT].strip() or "unspecified revert"


class Card(BaseModel):
    protocol_id: str
    protocol_name: str
    docs: List[str] = Field(default_factory=list)
    supported_chains: List[str] = Field(default_factory=list)
    supported_tokens: List[str] = Field(default_factory=list)
    card_version: int = 0
    last_updated: Optional[str] = None

    interfaces: List[Interface] = Field(default_factory=list)
    #: Column label for the destination identifier, e.g. "CCTP Domain". None if the
    #: protocol addresses destinations by EIP-155 chain id.
    dest_id_label: Optional[str] = None
    #: Contract column labels without the trailing " Address", e.g. ["Token Messenger"].
    address_labels: List[str] = Field(default_factory=list)
    rows: List[ChainRow] = Field(default_factory=list)
    failure_modes: List[FailureMode] = Field(default_factory=list)

    @field_validator("protocol_id")
    @classmethod
    def _id(cls, value: str) -> str:
        if not _ID.match(value):
            raise ValueError(f"protocol_id {value!r} must be lowercase letters, digits and underscores.")
        return value

    @field_validator("protocol_name")
    @classmethod
    def _name(cls, value: str) -> str:
        return text_field(value, "protocol_name", MAX_LABEL) or ""

    @field_validator("docs")
    @classmethod
    def _docs(cls, value: List[str]) -> List[str]:
        for url in value:
            if not str(url).startswith("https://"):
                raise ValueError(f"docs entry {url!r} is not an https URL.")
        return value

    @field_validator("supported_chains", "supported_tokens")
    @classmethod
    def _labels(cls, value: List[str]) -> List[str]:
        return [text_field(v, "supported entry", MAX_LABEL) or "" for v in value]

    @field_validator("address_labels")
    @classmethod
    def _address_labels(cls, value: List[str]) -> List[str]:
        return [text_field(v, "address column", MAX_LABEL) or "" for v in value]

    @model_validator(mode="after")
    def _consistent(self) -> "Card":
        if len(self.rows) > MAX_ROWS:
            raise ValueError(f"{len(self.rows)} chain rows; the limit is {MAX_ROWS}.")
        if len(self.failure_modes) > MAX_FAILURE_MODES:
            raise ValueError(f"{len(self.failure_modes)} failure modes; the limit is {MAX_FAILURE_MODES}.")

        roles = [slug(label) for label in self.address_labels]
        if len(set(roles)) != len(roles):
            raise ValueError("two address columns share a name.")
        for iface in self.interfaces:
            if iface.target is None and len(roles) > 1:
                raise ValueError(
                    f"interface {iface.label!r} does not say which contract it is called on, "
                    f"and the card has {len(roles)}."
                )
            if iface.target is not None and iface.target not in roles:
                raise ValueError(f"interface {iface.label!r} targets unknown column {iface.target!r}.")

        seen_ids = set()
        for row in self.rows:
            if row.chain_id in seen_ids:
                raise ValueError(f"two rows for chain id {row.chain_id}.")
            seen_ids.add(row.chain_id)
            unknown = set(row.addresses) - set(roles)
            if unknown:
                raise ValueError(f"{row.network} has addresses for unknown columns {sorted(unknown)}.")
            if row.dest_id and not self.dest_id_label:
                raise ValueError(f"{row.network} has a destination identifier but the card has no column for it.")

        keys = [f.key for f in self.failure_modes]
        if len(set(keys)) != len(keys):
            raise ValueError("the same revert is listed twice.")
        return self

    # --------------------------------------------------------------- lookups

    @property
    def roles(self) -> List[str]:
        return [slug(label) for label in self.address_labels]

    @property
    def dest_scheme(self) -> Optional[str]:
        return slug(self.dest_id_label) if self.dest_id_label else None

    def row(self, chain_id: int) -> Optional[ChainRow]:
        return next((r for r in self.rows if r.chain_id == int(chain_id)), None)

    def interfaces_for(self, role: str) -> List[Interface]:
        return [i for i in self.interfaces if (i.target or (self.roles[0] if self.roles else None)) == role]

    def failure_mode(self, revert: str) -> Optional[FailureMode]:
        key = normalise_revert(revert)
        return next((f for f in self.failure_modes if f.key == key), None)


class CardDocument(BaseModel):
    card: Card
    file_path: Optional[str] = None
