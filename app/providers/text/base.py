"""Contract for the 'AI designer' text models: design briefs and reference checks (structured outputs)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from app.providers.base import InputImage
from app.schemas import BriefResponse, ReferenceAnalysis


@dataclass
class BriefRequest:
    system_prompt: str
    user_prompt: str
    # current design and/or client inspiration images; for a reference check, the client's reference
    images: list[InputImage] = field(default_factory=list)
    # filled by the writer for the job's API call log: model (the one that answered), endpoint, usage (token
    # counts as returned), request_id, stop_reason
    meta: dict = field(default_factory=dict)


class BriefWriter(Protocol):
    name: str  # "claude" | "openai"
    label: str  # UI label
    model: str  # model id actually used

    async def write(self, req: BriefRequest) -> BriefResponse:
        """Return a validated BriefResponse or raise app.providers.base.ProviderError."""
        ...

    async def analyze_reference(self, req: BriefRequest) -> ReferenceAnalysis:
        """Return which garment parts the reference shows (validated) or raise ProviderError."""
        ...
