"""
The protocol card format. See docs/PROTOCOL_CARDS.md, section 3.

A card is protocol-level context for the discovery agent: contracts per chain, how the
protocol names destinations, its functions and what their arguments mean, and the
reverts it has produced. Every fact is an entry with a stable id and a status, and the
status is the whole point — it says how much a fact has been through, from "somebody
wrote it down" (`lead`) to "a human approved a broadcast that used it" (`verified`).

Nothing here decides a status. Validation (cards.validation) can raise a lead to
`checked`; only run outcomes can make an entry `verified` or `demoted`.

The limits are enforced by the model rather than by callers, so a card that breaks them
cannot be loaded or written by any path: card text is read by every later run, which
makes it a persistent prompt-injection surface in a way a single web page is not.
"""

import hashlib
import re
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator, model_validator
from web3 import Web3

from omnibridge.core import abi_signature

#: Entries per kind. A card is context for a model, and context has a budget.
MAX_ENTRIES_PER_KIND = 64
#: Characters in any free-text field (`meaning`, `resolution`).
MAX_FREE_TEXT = 200
#: Transaction hashes kept as evidence on one entry.
MAX_EVIDENCE = 5
#: Changelog records kept on a card. Older history lives in revisions/.
MAX_CHANGELOG = 10

_PRINTABLE = re.compile(r"^[\x20-\x7e]*$")
_ID_PART = re.compile(r"^[a-z0-9_]+$")


class EntryStatus(str, Enum):
    LEAD = "lead"            # stated by someone, not checked on chain
    CHECKED = "checked"      # passed the on-chain checks for its kind
    VERIFIED = "verified"    # used in a route a human approved and that was broadcast
    DEMOTED = "demoted"      # was checked or verified, then failed


def free_text(value: Optional[str], field: str) -> Optional[str]:
    """Short, printable, single-line. Anything else is refused, never trimmed."""
    if value is None:
        return None
    text = str(value)
    if len(text) > MAX_FREE_TEXT:
        raise ValueError(f"{field} is {len(text)} characters; the limit is {MAX_FREE_TEXT}.")
    if not _PRINTABLE.match(text):
        raise ValueError(f"{field} may contain printable ASCII only, on one line.")
    return text


def checksummed(value: str, field: str) -> str:
    try:
        return Web3.to_checksum_address(value)
    except Exception as exc:
        raise ValueError(f"{field} {value!r} is not a valid EVM address.") from exc


def revert_id(revert: str) -> str:
    """Stable id for a revert string, so the same revert seen twice is one entry."""
    normalised = " ".join(str(revert).lower().split())
    return "r-" + hashlib.sha256(normalised.encode("utf-8")).hexdigest()[:6]


class Provenance(BaseModel):
    """Fields every entry carries. None of them is set by the agent."""

    status: EntryStatus = EntryStatus.LEAD
    sources: List[str] = Field(default_factory=list)
    added: Optional[str] = None
    #: "seed", "human:<name>", or "agent:<model>".
    added_by: Optional[str] = None
    verified_count: int = 0
    last_verified: Optional[str] = None
    #: Transaction hashes of broadcasts that used this entry, newest last.
    evidence: List[str] = Field(default_factory=list)
    consecutive_failures: int = 0
    last_failure: Optional[str] = None

    @field_validator("evidence")
    @classmethod
    def _bounded_evidence(cls, value: List[str]) -> List[str]:
        return list(value)[-MAX_EVIDENCE:]

    @field_validator("last_failure")
    @classmethod
    def _bounded_failure(cls, value: Optional[str]) -> Optional[str]:
        # A node's revert text, not free text from a model, but still shown to later
        # runs — so it is bounded, though not refused, since the chain wrote it.
        return None if value is None else str(value)[:MAX_FREE_TEXT]


class ChainRef(BaseModel):
    scheme: str
    value: str


class Deployment(Provenance):
    id: str = ""
    chain_id: int
    chain: Optional[str] = None
    #: What this contract does for the protocol, e.g. "token_messenger".
    role: str
    version: str = "v1"
    address: str

    @field_validator("role", "version")
    @classmethod
    def _id_safe(cls, value: str) -> str:
        if not _ID_PART.match(value):
            raise ValueError(f"{value!r} must be lowercase letters, digits and underscores.")
        return value

    @field_validator("address")
    @classmethod
    def _address(cls, value: str) -> str:
        return checksummed(value, "address")

    @model_validator(mode="after")
    def _stable_id(self) -> "Deployment":
        expected = f"{self.chain_id}/{self.role}/{self.version}"
        if self.id and self.id != expected:
            raise ValueError(f"deployment id {self.id!r} should be {expected!r}.")
        self.id = expected
        return self


class Destination(Provenance):
    id: str = ""
    chain_id: int
    ref: ChainRef

    @model_validator(mode="after")
    def _stable_id(self) -> "Destination":
        expected = str(self.chain_id)
        if self.id and self.id != expected:
            raise ValueError(f"destination id {self.id!r} should be {expected!r}.")
        self.id = expected
        return self


class Token(Provenance):
    id: str = ""
    chain_id: int
    symbol: str
    address: str
    #: Read from the contract by validation. A seed may state it; the chain wins.
    decimals: Optional[int] = None

    @field_validator("address")
    @classmethod
    def _address(cls, value: str) -> str:
        return checksummed(value, "address")

    @model_validator(mode="after")
    def _stable_id(self) -> "Token":
        expected = f"{self.chain_id}/{self.symbol}"
        if self.id and self.id != expected:
            raise ValueError(f"token id {self.id!r} should be {expected!r}.")
        self.id = expected
        return self


class FunctionParam(BaseModel):
    name: str
    type: str
    meaning: Optional[str] = None
    #: "code" when reviewed code computes the value and the agent must not choose it.
    computed_by: Optional[str] = None

    @field_validator("meaning")
    @classmethod
    def _meaning(cls, value: Optional[str]) -> Optional[str]:
        return free_text(value, "meaning")


class SpendSpec(BaseModel):
    """Which argument names the token pulled, and which contract must be approved."""

    #: The name of the parameter holding the token address.
    token: str
    #: "self" for the deployment the function is called on, or another role.
    spender: str = "self"


class Function(Provenance):
    id: str = ""
    #: The deployment role this function is called on.
    role: str
    signature: str
    params: List[FunctionParam] = Field(default_factory=list)
    spend: Optional[SpendSpec] = None
    #: Called with eth_call only, never signed.
    read_only: bool = False

    @field_validator("signature")
    @classmethod
    def _canonical(cls, value: str) -> str:
        abi_signature.parse(value)
        return value

    @model_validator(mode="after")
    def _consistent(self) -> "Function":
        parsed = abi_signature.parse(self.signature)
        if self.params:
            # A struct argument is one ABI type, so a function whose only argument is a
            # tuple describes the tuple's fields. Either way the types must line up.
            expected_types = parsed.types
            only = parsed.types[0] if len(parsed.types) == 1 else ""
            if only.startswith("(") and only.endswith(")"):
                expected_types = abi_signature.split_top_level(only[1:-1])
            declared = [p.type for p in self.params]
            if declared != expected_types:
                raise ValueError(
                    f"params {declared} do not match the signature's types {expected_types}."
                )
        if self.spend is not None and self.params:
            names = {p.name for p in self.params}
            if self.spend.token not in names:
                raise ValueError(f"spend.token {self.spend.token!r} is not a parameter name.")
        expected = f"{self.role}/{parsed.name}"
        if self.id and self.id != expected:
            raise ValueError(f"function id {self.id!r} should be {expected!r}.")
        self.id = expected
        return self


class Parameter(Provenance):
    id: str
    value: Any
    meaning: Optional[str] = None

    @field_validator("meaning")
    @classmethod
    def _meaning(cls, value: Optional[str]) -> Optional[str]:
        return free_text(value, "meaning")


class KnownRevert(BaseModel):
    """
    A revert this protocol produced. Created by code from the node's own revert text,
    never by the agent; the agent may only propose a `resolution`, which is a lead.
    """

    id: str = ""
    revert: str
    #: The deployment id it was observed on, if it was observed at all.
    observed_on: Optional[str] = None
    first_seen: Optional[str] = None
    #: How many times it has been observed. 0 for a seeded entry nobody has seen here.
    count: int = 0
    resolution: Optional[str] = None
    resolution_status: EntryStatus = EntryStatus.LEAD
    added_by: Optional[str] = None

    @field_validator("revert")
    @classmethod
    def _revert(cls, value: str) -> str:
        return str(value)[:MAX_FREE_TEXT]

    @field_validator("resolution")
    @classmethod
    def _resolution(cls, value: Optional[str]) -> Optional[str]:
        return free_text(value, "resolution")

    @model_validator(mode="after")
    def _stable_id(self) -> "KnownRevert":
        expected = revert_id(self.revert)
        if self.id and self.id != expected:
            raise ValueError(f"revert id {self.id!r} should be {expected!r}.")
        self.id = expected
        return self


class ChangeRecord(BaseModel):
    """One write, summarised for people reading the card. Never shown to a model."""

    at: str
    card_version: int
    cause: str
    changes: List[str] = Field(default_factory=list)


#: The entry lists on a card, in rendering order.
ENTRY_KINDS = ("deployments", "destinations", "tokens", "functions", "parameters", "known_reverts")


class Card(BaseModel):
    id: str
    name: str
    #: "bridge" or "swap". Only bridge cards are offered to discovery today.
    kind: str = "bridge"
    network: str = "mainnet"
    card_version: int = 0
    aliases: List[str] = Field(default_factory=list)
    #: Hosts whose pages count as this protocol's documentation. Must be allowlisted.
    documentation: List[str] = Field(default_factory=list)
    dest_scheme: Optional[str] = None
    assets: List[str] = Field(default_factory=list)
    #: False when the pipeline cannot yet execute anything from this card.
    executable: bool = True
    blocked_on: List[str] = Field(default_factory=list)
    last_updated: Optional[str] = None

    deployments: List[Deployment] = Field(default_factory=list)
    destinations: List[Destination] = Field(default_factory=list)
    tokens: List[Token] = Field(default_factory=list)
    functions: List[Function] = Field(default_factory=list)
    parameters: List[Parameter] = Field(default_factory=list)
    known_reverts: List[KnownRevert] = Field(default_factory=list)

    changelog: List[ChangeRecord] = Field(default_factory=list)

    @field_validator("id")
    @classmethod
    def _id(cls, value: str) -> str:
        if not _ID_PART.match(value):
            raise ValueError(f"card id {value!r} must be lowercase letters, digits and underscores.")
        return value

    @field_validator("kind")
    @classmethod
    def _kind(cls, value: str) -> str:
        if value not in ("bridge", "swap"):
            raise ValueError(f"kind must be 'bridge' or 'swap', not {value!r}.")
        return value

    @field_validator("changelog")
    @classmethod
    def _bounded_changelog(cls, value: List[ChangeRecord]) -> List[ChangeRecord]:
        return list(value)[-MAX_CHANGELOG:]

    @model_validator(mode="after")
    def _limits_and_unique_ids(self) -> "Card":
        for kind in ENTRY_KINDS:
            entries = getattr(self, kind)
            if len(entries) > MAX_ENTRIES_PER_KIND:
                raise ValueError(
                    f"{kind} has {len(entries)} entries; the limit is {MAX_ENTRIES_PER_KIND}."
                )
            seen = set()
            for entry in entries:
                if entry.id in seen:
                    raise ValueError(f"{kind} has two entries with id {entry.id!r}.")
                seen.add(entry.id)
        return self

    def entries(self, kind: str) -> List[Any]:
        if kind not in ENTRY_KINDS:
            raise KeyError(kind)
        return getattr(self, kind)

    def entry(self, kind: str, entry_id: str) -> Optional[Any]:
        return next((e for e in self.entries(kind) if e.id == entry_id), None)

    def statuses(self) -> Dict[str, str]:
        """`kind:id` → status, for diffing two versions of a card."""
        out: Dict[str, str] = {}
        for kind in ENTRY_KINDS:
            for entry in self.entries(kind):
                status = getattr(entry, "status", None) or getattr(entry, "resolution_status")
                out[f"{kind}:{entry.id}"] = status.value
        return out


class CardDocument(BaseModel):
    card: Card
    body: str = ""
    file_path: Optional[str] = None
