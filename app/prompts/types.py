"""Prompt container shared by the builders, the pipeline and the providers."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PromptPair:
    system: str  # persistent role + hard rules (Claude/OpenAI system message; prepended for GPT Image)
    user: str  # the per-request task

    def combined(self) -> str:
        """Single prompt for APIs without a system field (e.g. the OpenAI Images API)."""
        return f"{self.system.strip()}\n\n---\n\n{self.user.strip()}"
