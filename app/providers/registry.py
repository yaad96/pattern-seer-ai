"""Builds the configured image providers and brief writers from Settings."""

from __future__ import annotations

from app.config import Settings
from app.providers.base import ImageProvider
from app.providers.text.base import BriefWriter

IMAGE_PROVIDER_ORDER = ("openai", "mock")
BRIEF_WRITER_ORDER = ("claude", "openai")


class ProviderRegistry:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.image_providers: dict[str, ImageProvider] = {}
        self.brief_writers: dict[str, BriefWriter] = {}
        timeout = settings.provider_timeout_s

        if settings.openai_api_key:
            from app.providers.openai_images import OpenAIImageProvider
            from app.providers.text.openai_text import OpenAIBriefWriter

            self.image_providers["openai"] = OpenAIImageProvider(
                api_key=settings.openai_api_key, default_model=settings.openai_image_model, timeout_s=timeout
            )
            self.brief_writers["openai"] = OpenAIBriefWriter(
                api_key=settings.openai_api_key, model=settings.openai_text_model, timeout_s=timeout
            )
        if settings.anthropic_api_key:
            from app.providers.text.claude import ClaudeBriefWriter

            self.brief_writers["claude"] = ClaudeBriefWriter(
                api_key=settings.anthropic_api_key,
                model=settings.claude_model,
                effort=settings.claude_effort,
                timeout_s=timeout,
            )
        if settings.enable_mock_provider or not self.image_providers:
            from app.providers.mock import MockImageProvider

            self.image_providers["mock"] = MockImageProvider()

    # -- image providers -----------------------------------------------------

    def default_image_provider(self) -> str:
        preferred = self.settings.default_image_provider
        if preferred != "auto" and preferred in self.image_providers:
            return preferred
        return next(name for name in IMAGE_PROVIDER_ORDER if name in self.image_providers)

    def get_image_provider(self, name: str | None) -> ImageProvider:
        key = name or self.default_image_provider()
        provider = self.image_providers.get(key)
        if provider is None:
            raise LookupError(f"Image provider '{key}' is not configured. Paste its API key into the key file.")
        return provider

    # -- brief writers -------------------------------------------------------

    def get_brief_writer(self, name: str = "auto") -> BriefWriter | None:
        """The requested writer if configured, else the first available one; None if off or none configured."""
        if self.settings.brief_writer == "off":
            return None
        if name == "auto":
            name = self.settings.brief_writer
        if name in self.brief_writers:
            return self.brief_writers[name]
        return next((self.brief_writers[n] for n in BRIEF_WRITER_ORDER if n in self.brief_writers), None)
