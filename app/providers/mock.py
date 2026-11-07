"""Offline demo image provider: deterministic jersey-like graphics drawn locally (no network, no AI).

Test hooks anywhere in the system or user prompt: [[mock:fail]] -> permanent failure, [[mock:safety]] ->
safety block, [[mock:retry-once]] -> the first call for a prompt fails with a retryable error.
"""

from __future__ import annotations

import asyncio
import colorsys
import hashlib
import io
import random
import re
import time

from PIL import Image, ImageDraw, ImageFont, ImageOps

from app.providers.base import ImageGenRequest, ImageGenResult, ModelInfo, ProviderError
from app.schemas import CONCRETE_ASPECT_RATIOS

MODEL_ID = "mock-1"
LATENCY_S = 0.2  # simulated API latency
LONG_EDGE = {"1K": 1024, "2K": 1536, "4K": 2048}  # smaller than real models so demos and tests stay fast
HOOK_FAIL = "[[mock:fail]]"
HOOK_SAFETY = "[[mock:safety]]"
HOOK_RETRY_ONCE = "[[mock:retry-once]]"
_HEX = re.compile(r"#[0-9A-Fa-f]{6}\b")
# Front view of a jersey in unit coordinates: neckline, shoulders, sleeves, body.
_JERSEY = (
    (0.36, 0.06), (0.5, 0.14), (0.64, 0.06), (0.8, 0.1), (1.0, 0.36), (0.88, 0.46), (0.78, 0.38),
    (0.78, 0.98), (0.22, 0.98), (0.22, 0.38), (0.12, 0.46), (0.0, 0.36), (0.2, 0.1),
)


class MockImageProvider:
    name = "mock"
    label = "Offline demo (no AI)"

    def __init__(self) -> None:
        self._retried: set[str] = set()  # prompts whose [[mock:retry-once]] failure was already raised

    def models(self) -> list[ModelInfo]:
        return [
            ModelInfo(
                id=MODEL_ID,
                label="DEMO placeholder - not AI (add an OpenAI key)",
                resolutions=("1K", "2K", "4K"),
                aspect_ratios=CONCRETE_ASPECT_RATIOS,
                max_input_images=8,
                supports_transparent=True,
                supports_seed=True,
                supports_system_prompt=True,
                notes="Draws a placeholder pattern locally for demos and tests; no AI is used.",
            )
        ]

    def default_model(self) -> str:
        return MODEL_ID

    async def generate(self, req: ImageGenRequest) -> ImageGenResult:
        started = time.perf_counter()
        await asyncio.sleep(LATENCY_S)
        self._apply_hooks(req)
        width, height = output_size(req.aspect_ratio, req.resolution)
        data = await asyncio.to_thread(render, req, width, height)
        return ImageGenResult(
            data=data,
            mime_type="image/png",
            provider=self.name,
            model=MODEL_ID,
            latency_s=time.perf_counter() - started,
            request_meta={
                "endpoint": "mock.generate", "model": MODEL_ID, "size": f"{width}x{height}", "background": req.background,
                "seed": req.seed,
            },
        )

    def _apply_hooks(self, req: ImageGenRequest) -> None:
        prompt = f"{req.system_prompt}\n{req.user_prompt}"
        if HOOK_FAIL in prompt:
            raise ProviderError("Mock failure requested by [[mock:fail]].", kind="unavailable", provider=self.name)
        if HOOK_SAFETY in prompt:
            raise ProviderError("Mock safety block requested by [[mock:safety]].", kind="safety", provider=self.name)
        if HOOK_RETRY_ONCE in prompt:
            key = hashlib.sha256(prompt.encode()).hexdigest()
            if key not in self._retried:
                self._retried.add(key)
                raise ProviderError(
                    "Temporary mock failure requested by [[mock:retry-once]].",
                    kind="unavailable",
                    retryable=True,
                    provider=self.name,
                )


def output_size(aspect_ratio: str, resolution: str) -> tuple[int, int]:
    width, height = (int(part) for part in aspect_ratio.split(":"))
    long_edge = LONG_EDGE.get(resolution, LONG_EDGE["2K"])
    if width >= height:
        return long_edge, round(long_edge * height / width)
    return round(long_edge * width / height), long_edge


def render(req: ImageGenRequest, width: int, height: int) -> bytes:
    """Deterministic PNG: the same prompts, seed and input images always give the same bytes.

    Size and background are not part of the fingerprint, so one design scales across resolutions.
    """
    rng = random.Random(_fingerprint(req))
    img = _pattern(width, height, _palette(req.user_prompt, rng), rng)
    if req.images:
        img = Image.blend(_posterized(req.images[0].data, width, height), img, 0.15)
    if req.background == "transparent":
        img = img.convert("RGBA")
        img.putalpha(_jersey_mask(width, height))
    _stamp(img)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _fingerprint(req: ImageGenRequest) -> int:
    digest = hashlib.sha256()
    for part in (req.system_prompt, req.user_prompt, str(req.seed)):
        digest.update(part.encode() + b"\0")
    for image in req.images:
        digest.update(hashlib.sha256(image.data).digest())
    return int.from_bytes(digest.digest()[:8], "big")


def _palette(prompt: str, rng: random.Random) -> list[str]:
    """Hex colours named in the prompt (the first is the base colour), topped up with seeded random colours."""
    colors = list(dict.fromkeys(h.upper() for h in _HEX.findall(prompt)))[:6]
    while len(colors) < 3:
        r, g, b = colorsys.hsv_to_rgb(rng.random(), rng.uniform(0.5, 0.95), rng.uniform(0.35, 0.95))
        colors.append(f"#{round(r * 255):02X}{round(g * 255):02X}{round(b * 255):02X}")
    return colors


def _pattern(width: int, height: int, colors: list[str], rng: random.Random) -> Image.Image:
    img = Image.new("RGB", (width, height), colors[0])
    draw_style = rng.choice((_stripes, _chevrons, _shards))
    draw_style(ImageDraw.Draw(img), width, height, colors[1:], rng)
    return img


def _stripes(draw: ImageDraw.ImageDraw, w: int, h: int, accents: list[str], rng: random.Random) -> None:
    """Diagonal sublimation stripes of varying width."""
    slant = h * rng.uniform(0.3, 0.9) * rng.choice((-1, 1))
    x, i = -abs(slant), 0
    while x < w + abs(slant):
        band = w * rng.uniform(0.03, 0.09)
        draw.polygon([(x, 0), (x + band, 0), (x + band + slant, h), (x + slant, h)], fill=accents[i % len(accents)])
        x += band * rng.uniform(1.8, 3.2)
        i += 1


def _chevrons(draw: ImageDraw.ImageDraw, w: int, h: int, accents: list[str], rng: random.Random) -> None:
    """Horizontal chevron bands pointing down the chest."""
    depth = h * rng.uniform(0.08, 0.2)
    y, i = -depth, 0
    while y < h:
        band = h * rng.uniform(0.03, 0.08)
        points = [(0, y), (w / 2, y + depth), (w, y), (w, y + band), (w / 2, y + depth + band), (0, y + band)]
        draw.polygon(points, fill=accents[i % len(accents)])
        y += band * rng.uniform(1.8, 3.5)
        i += 1


def _shards(draw: ImageDraw.ImageDraw, w: int, h: int, accents: list[str], rng: random.Random) -> None:
    """Angular 'shattered glass' triangles."""
    for i in range(rng.randint(12, 24)):
        cx, cy, size = rng.random() * w, rng.random() * h, max(w, h) * rng.uniform(0.08, 0.3)
        points = [(cx + rng.uniform(-1, 1) * size, cy + rng.uniform(-1, 1) * size) for _ in range(3)]
        draw.polygon(points, fill=accents[i % len(accents)])


def _posterized(data: bytes, width: int, height: int) -> Image.Image:
    with Image.open(io.BytesIO(data)) as src:
        ref = ImageOps.fit(src.convert("RGB"), (width, height), Image.Resampling.LANCZOS)
    return ImageOps.posterize(ref, 3)


def _jersey_mask(width: int, height: int) -> Image.Image:
    """Alpha mask: an opaque jersey silhouette centred on a transparent surround."""
    mask = Image.new("L", (width, height), 0)
    side = 0.9 * min(width, height)
    x0, y0 = (width - side) / 2, (height - side) / 2
    ImageDraw.Draw(mask).polygon([(x0 + x * side, y0 + y * side) for x, y in _JERSEY], fill=255)
    return mask


DEMO_LINES = (
    "DEMO - NOT AI",
    "No OpenAI key in .openai_api_key",
    "Paste it there and restart the app",
)


def _stamp(img: Image.Image) -> None:
    """Large centred banner so a demo placeholder can never be mistaken for an AI result."""
    draw = ImageDraw.Draw(img)
    title = ImageFont.load_default(size=max(18, img.width // 11))
    body = ImageFont.load_default(size=max(10, img.width // 34))
    lines = [(DEMO_LINES[0], title)] + [(text, body) for text in DEMO_LINES[1:]]
    boxes = [draw.textbbox((0, 0), text, font=font) for text, font in lines]
    gap = max(6, img.width // 80)
    pad = max(10, img.width // 30)
    block_w = max(b[2] - b[0] for b in boxes) + 2 * pad
    block_h = sum(b[3] - b[1] for b in boxes) + gap * (len(lines) - 1) + 2 * pad
    x0, y0 = (img.width - block_w) // 2, (img.height - block_h) // 2
    draw.rectangle((x0, y0, x0 + block_w, y0 + block_h), fill="#B00020", outline="#FFFFFF", width=max(2, pad // 4))
    y = y0 + pad
    for (text, font), (left, top, right, bottom) in zip(lines, boxes):
        draw.text(((img.width - (right - left)) // 2 - left, y - top), text, font=font, fill="#FFFFFF")
        y += bottom - top + gap
