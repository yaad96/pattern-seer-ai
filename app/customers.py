"""The Customer EntryPoint Builder (2026-11-24; the owner: "in the supreme dashboard, can we create a new 'Customer
EntryPoint Builder'? Where I will upload customer name, a phone number, suggested entrypoint link (editable), the logo
and a mechanical, automated script will create an entrypoint for them"). A client page (app/clients.py) made from a
form on the dashboards, by the head admin or the partner, at once and without a restart:

- the link /client_<slug>, suggested from the name and editable;
- the logo used as it was uploaded (the owner: "lets leave it at as-uploaded as of now"): only made smaller where it
  has to be (the sign-in page's logo, the header's, the tab's icon); nothing is cut out or cleaned;
- three colours picked from the logo (editable in the form), for the page's buttons and accents;
- the page's own login: the slug as the username and a generated password;
- the header shows the logo and the name (the owner: "the logo plus name is enough").

What the builder makes lives in <output_dir>/clients/ (clients.json and the logo files, in the format of
client_assets/, so app.clients reads both): it is the server's own data, which a deploy never touches. A client
written by hand in client_assets/ keeps working as before; a slug used there can't be made again here."""

from __future__ import annotations

import io
import json
import logging
import os
import re
import secrets
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from PIL import Image, ImageOps

from app.clients import CONFIG, HEX_RE, MAX_IMAGE_BYTES, ROLES, SLUG_RE, TEXT_LIMIT, ClientBrand, read_client
from app.imaging.palette import dominant_colors

log = logging.getLogger(__name__)

FOLDER = "clients"  # under the output folder
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_PIXELS = 25_000_000  # a phone's photo of a logo is about 12 million; more would cost the small server its memory
MAX_CUSTOMERS = 500
LOGO_SIDE = 520  # the sign-in page shows the logo 260 px wide
MARK_HEIGHT = 96  # the header shows it 30 px tall
ICON_SIDE = 64
PHONE_RE = re.compile(r"\+?[0-9][0-9 ()\-]{4,23}")
PASSWORD_LETTERS = "abcdefghjkmnpqrstuvwxyz23456789"  # none that look alike (0 o, 1 l i)
DEFAULT_COLORS = {"primary": "#2f5bea", "secondary": "#454d5a", "tertiary": "#13795b"}  # a logo without colours


class CustomerError(ValueError):
    """What is wrong with a customer's form, in words for the person filling it in."""


@dataclass(frozen=True)
class Logo:
    """An uploaded logo, as the page will use it: three files' bytes with their file endings, and its colours."""
    logo: bytes
    logo_ext: str
    mark: bytes
    mark_ext: str
    icon: bytes
    colors: dict[str, str]


def folder_of(output_dir: Path) -> Path:
    return Path(output_dir) / FOLDER


def clean_name(text: str) -> str:
    name = " ".join((text or "").split())
    if not name or len(name) > TEXT_LIMIT:
        raise CustomerError(f"Enter the customer's name (at most {TEXT_LIMIT} characters).")
    return name


def clean_phone(text: str) -> str:
    """A phone number to reach the customer on, as typed (digits, spaces, + ( ) -), kept whole."""
    phone = " ".join((text or "").split())
    if not PHONE_RE.fullmatch(phone) or not 6 <= sum(ch.isdigit() for ch in phone) <= 15:
        raise CustomerError("Enter the customer's phone number, like 01XXXXXXXXX or +8801XXXXXXXXX.")
    return phone


def suggest_slug(name: str, taken: set[str]) -> str:
    """A link name from the customer's name: its letters and digits in lower case (20 at most), with a number added
    when that is in use; "customer" for a name without Latin letters or digits."""
    plain = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode("ascii").lower()
    base = re.sub(r"[^a-z0-9]", "", plain)[:20] or "customer"
    slug, n = base, 2
    while slug in taken:
        slug = f"{base[:20 - len(str(n))]}{n}"
        n += 1
    return slug


def slug_problem(slug: str, taken: set[str]) -> str | None:
    """Why this link name can't be used, or None. `taken`: the slugs in use and every login's username (the slug is
    also the new login's username), in lower case."""
    if not SLUG_RE.fullmatch(slug or ""):
        return "Use small letters and digits only, 1 to 32, without spaces."
    if slug in taken:
        return "This link name is already in use. Choose another."
    return None


def new_password() -> str:
    """A password to read out or type on a phone: three groups of four letters and digits."""
    return "-".join("".join(secrets.choice(PASSWORD_LETTERS) for _ in range(4)) for _ in range(3))


def clean_colors(raw: object) -> dict[str, str] | None:
    """The three colours the form sent ({"primary": "#RRGGBB", …}), or None when it sent none (they are picked)."""
    if not raw:
        return None
    if not isinstance(raw, dict) or not all(isinstance(raw.get(r), str) and HEX_RE.fullmatch(raw[r]) for r in ROLES):
        raise CustomerError("The colours must be three colours like #1F4FD8.")
    return {r: raw[r].lower() for r in ROLES}


# ---------- the logo ----------

def _encode(im: Image.Image, limit: int) -> tuple[bytes, str, Image.Image]:
    """`im` as a file of at most `limit` bytes: a PNG, else (without transparency) a JPEG, else made smaller step by
    step. The page carries its logos inside itself, so they have to be small."""
    for scale in (1.0, 0.8, 0.65, 0.5, 0.35):
        cur = im if scale == 1.0 else im.resize((max(1, round(im.width * scale)), max(1, round(im.height * scale))), Image.LANCZOS)
        buf = io.BytesIO()
        cur.save(buf, "PNG", optimize=True)
        if buf.tell() <= limit:
            return buf.getvalue(), "png", cur
        if cur.mode == "RGB":
            buf = io.BytesIO()
            cur.save(buf, "JPEG", quality=88, optimize=True)
            if buf.tell() <= limit:
                return buf.getvalue(), "jpg", cur
    raise CustomerError("This logo picture can't be made small enough. Try a simpler or smaller picture.")


def _fit(im: Image.Image, width: int, height: int) -> Image.Image:
    """`im` made smaller to fit in width × height (never larger: a small logo stays as it is)."""
    scale = min(1.0, width / im.width, height / im.height)
    return im if scale >= 1.0 else im.resize((max(1, round(im.width * scale)), max(1, round(im.height * scale))), Image.LANCZOS)


def _hsl(color: str) -> tuple[float, float]:
    """A #RRGGBB colour's saturation and lightness (0 to 1)."""
    r, g, b = (int(color[i:i + 2], 16) / 255 for i in (1, 3, 5))
    hi, lo = max(r, g, b), min(r, g, b)
    light = (hi + lo) / 2
    sat = 0.0 if hi == lo else (hi - lo) / (1 - abs(2 * light - 1))
    return sat, light


def _far(a: str, b: str) -> bool:
    return sum((int(a[i:i + 2], 16) - int(b[i:i + 2], 16)) ** 2 for i in (1, 3, 5)) ** 0.5 >= 60


def pick_colors(im: Image.Image) -> dict[str, str]:
    """Three colours for the page from the logo: its most common ones that are colours (not white, black or grey)
    and differ from each other. A logo with one colour gets a darker shade of it as the second; one with none gets
    the app's own."""
    flat = Image.new("RGB", im.size, "#ffffff")
    flat.paste(im, mask=im.getchannel("A") if im.mode == "RGBA" else None)
    found: list[str] = []
    for entry in dominant_colors(flat, k=12):
        color = entry.hex.lower()
        sat, light = _hsl(color)
        if sat >= 0.25 and 0.12 <= light <= 0.88 and all(_far(color, other) for other in found):
            found.append(color)
    if not found:
        return dict(DEFAULT_COLORS)
    primary = found[0]
    if len(found) == 1:  # one colour: a darker shade beside it
        r, g, b = (int(primary[i:i + 2], 16) for i in (1, 3, 5))
        found.append("#" + "".join(f"{round(c * 0.6):02x}" for c in (r, g, b)))
    return {"primary": primary, "secondary": found[1], "tertiary": found[2] if len(found) > 2 else found[1]}


def prepare_logo(data: bytes, colors: dict[str, str] | None = None) -> Logo:
    """The uploaded picture as the page's three logo files (worker thread). It is used as uploaded: turned upright
    if a phone stored it sideways and made smaller where needed, nothing else. Only PNG, JPEG and WebP (no SVG: it
    could carry scripts)."""
    if not data:
        raise CustomerError("Choose the customer's logo picture.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise CustomerError(f"The logo picture is over {MAX_UPLOAD_BYTES // (1024 * 1024)} MB. Choose a smaller one.")
    try:
        with Image.open(io.BytesIO(data)) as probe:
            fmt, (width, height) = probe.format, probe.size
        if fmt not in ("PNG", "JPEG", "WEBP"):
            raise CustomerError("Use a PNG, JPEG or WebP picture for the logo.")
        if width * height > MAX_PIXELS:
            raise CustomerError("The logo picture has too many pixels. Choose a smaller one.")
        with Image.open(io.BytesIO(data)) as raw:
            im = ImageOps.exif_transpose(raw)
            alpha = im.mode in ("RGBA", "LA", "PA") or (im.mode == "P" and "transparency" in im.info)
            im = im.convert("RGBA" if alpha else "RGB")
    except CustomerError:
        raise
    except Exception as exc:  # not a picture, cut short, a decompression bomb …
        raise CustomerError("This file can't be read as a picture. Use a PNG, JPEG or WebP.") from exc
    logo, logo_ext, shown = _encode(_fit(im, LOGO_SIDE, LOGO_SIDE), MAX_IMAGE_BYTES)
    mark, mark_ext, _ = _encode(_fit(shown, 10 * MARK_HEIGHT, MARK_HEIGHT), MAX_IMAGE_BYTES)
    tile = Image.new("RGB", (ICON_SIDE, ICON_SIDE), "#ffffff")  # the tab's icon: the logo on a white square
    small = _fit(shown, ICON_SIDE - 8, ICON_SIDE - 8)
    tile.paste(small, ((ICON_SIDE - small.width) // 2, (ICON_SIDE - small.height) // 2),
               mask=small.getchannel("A") if small.mode == "RGBA" else None)
    icon = io.BytesIO()
    tile.save(icon, "PNG", optimize=True)
    return Logo(logo=logo, logo_ext=logo_ext, mark=mark, mark_ext=mark_ext, icon=icon.getvalue(),
                colors=colors or pick_colors(shown))


# ---------- the folder ----------

def read_entries(folder: Path) -> list[dict]:
    """The clients the builder has made (clients.json's list), [] when there are none or the file can't be read."""
    try:
        entries = json.loads((folder / CONFIG).read_text(encoding="utf-8")).get("clients", [])
    except FileNotFoundError:
        return []
    except (OSError, ValueError, AttributeError):
        log.exception("customers: %s can't be read", folder / CONFIG)
        raise CustomerError("The customers' file can't be read on the server, so no customer can be added now.") from None
    return entries if isinstance(entries, list) else []


def _write(path: Path, data: bytes, mode: int = 0o600) -> None:
    tmp = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode), "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def save(folder: Path, *, slug: str, name: str, phone: str, logo: Logo, password: str, by: str, at: str,
         check: Callable[[ClientBrand], object] | None = None) -> ClientBrand:
    """Write a new customer's logo files and its entry (worker thread; the caller holds the builder's lock), and
    return the page it makes. The entry goes in last, so a half-written customer is never read; if the page can't be
    made from what was written (`check`, the app's own making of the page, raises), the entry isn't added and the
    files are removed."""
    entries = read_entries(folder)
    if len(entries) >= MAX_CUSTOMERS:
        raise CustomerError(f"There are {MAX_CUSTOMERS} customers already; ask for the limit to be raised.")
    folder.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(folder, 0o700)  # the file with the customers' passwords is in it
    except OSError:
        pass
    files = {f"{slug}_logo.{logo.logo_ext}": logo.logo, f"{slug}_mark.{logo.mark_ext}": logo.mark, f"{slug}_icon.png": logo.icon}
    entry = {
        "slug": slug, "name": name, "phone": phone, "logo": f"{slug}_logo.{logo.logo_ext}",
        "mark": f"{slug}_mark.{logo.mark_ext}", "icon": f"{slug}_icon.png", "colors": dict(logo.colors),
        "login": {"username": slug, "password": password}, "made_at": at, "made_by": by,
    }
    try:
        for filename, data in files.items():
            _write(folder / filename, data, 0o644)
        brand = read_client(folder, entry)  # the same reading as at start-up: what is written is what will load
        if check is not None:
            check(brand)
        _write(folder / CONFIG, json.dumps({"clients": [*entries, entry]}, ensure_ascii=False, indent=2).encode("utf-8"))
    except Exception:
        for filename in files:
            (folder / filename).unlink(missing_ok=True)
        raise
    return brand
