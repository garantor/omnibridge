"""
Skill Manager for persistent procedural cross-chain execution modules (SKILL.md).
Adheres to the agentskills.io and Hermes Agent open skill specifications.
"""

import os
from pathlib import Path
import re
from typing import Dict, List, Optional, Any
import yaml
from pydantic import BaseModel, Field


class SkillMetadata(BaseModel):
    name: str
    description: str
    source_chain: str
    source_chain_id: int
    dest_chain: str
    dest_chain_id: int
    token: str
    bridge_protocol: str
    router_address: str
    source_token_address: Optional[str] = None
    dest_token_address: Optional[str] = None
    cctp_destination_domain: Optional[int] = None
    last_verified: Optional[str] = None
    rpc_fallbacks: List[str] = Field(default_factory=list)
    tags: List[str] = Field(default_factory=list)


class Skill(BaseModel):
    metadata: SkillMetadata
    content: str
    file_path: Optional[str] = None


class SkillManager:
    """Manages discovery, loading, verification, and persistence of SKILL.md modules."""

    def __init__(self, repository_path: Optional[str] = None):
        if repository_path:
            self.repo_dir = Path(repository_path)
        else:
            # Default to local package repository and user home fallback
            base_dir = Path(__file__).resolve().parent / "repository"
            self.repo_dir = base_dir

        self.repo_dir.mkdir(parents=True, exist_ok=True)

    def _parse_skill_file(self, file_path: Path) -> Optional[Skill]:
        """Parses a SKILL.md file with YAML frontmatter."""
        try:
            raw_text = file_path.read_text(encoding="utf-8")
            pattern = r"^---\s*\n(.*?)\n---\s*\n(.*)$"
            match = re.search(pattern, raw_text, re.DOTALL)
            if not match:
                return None

            yaml_str = match.group(1)
            markdown_content = match.group(2)

            data = yaml.safe_load(yaml_str)
            if not data or "name" not in data:
                return None

            metadata = SkillMetadata(**data)
            return Skill(metadata=metadata, content=markdown_content, file_path=str(file_path))
        except Exception as e:
            print(f"[SkillManager] Warning: failed to parse {file_path}: {e}")
            return None

    def list_skills(self) -> List[Skill]:
        """Lists all registered skills in the repository."""
        skills = []
        for root, _, files in os.walk(self.repo_dir):
            for file in files:
                if file.lower() == "skill.md":
                    skill_path = Path(root) / file
                    parsed = self._parse_skill_file(skill_path)
                    if parsed:
                        skills.append(parsed)
        return skills

    def find_skill(self, source_chain: str, dest_chain: str, token: str) -> Optional[Skill]:
        """Finds an existing matching skill for the requested chain-pair and token."""
        s_src = source_chain.lower().strip()
        s_dst = dest_chain.lower().strip()
        s_tok = token.upper().strip()

        for skill in self.list_skills():
            m = skill.metadata
            if (
                m.source_chain.lower() == s_src
                and m.dest_chain.lower() == s_dst
                and m.token.upper() == s_tok
            ):
                return skill
        return None

    def save_skill(
        self,
        metadata: SkillMetadata,
        markdown_body: str,
        target_dir: Optional[str] = None,
    ) -> str:
        """Saves or updates a SKILL.md file."""
        dest_dir = Path(target_dir) if target_dir else self.repo_dir / metadata.name
        dest_dir.mkdir(parents=True, exist_ok=True)
        file_path = dest_dir / "SKILL.md"

        yaml_header = yaml.dump(metadata.model_dump(exclude_none=True), sort_keys=False)
        full_content = f"---\n{yaml_header}---\n\n{markdown_body.strip()}\n"

        file_path.write_text(full_content, encoding="utf-8")
        return str(file_path)
