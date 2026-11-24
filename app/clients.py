"""Client pages (/client_<name>, 2026-11-22): a client's own copy of the demo page /customerep, showing the client's
name, logo and colours instead of the studio's. The owner: "It would be identical to the demo endpoint, except that,
here, the name and the logo should be used. also make the endpoint webpage's theme similar to their logo color".

Each page has its own login (the owner asked for a separate username and password per client page, allowed in that
page and in no other): that login reaches only its page (security.client_routes) and the jobs it made (Job.client);
the studio login reaches every page; the demo login none. No client's name or login is ever written in this code.
Signing in from a client page happens on a sign-in page in the client's colours and comes back to the page.

Everything about a client lives in the git-ignored folder client_assets/ (the logos and clients.json, with the
login; its README.md gives the format), so this public code holds no client material. The folder is read once at
start-up: restart the app after changing it."""

from __future__ import annotations

import base64
import html
import io
import json
import logging
import re
from dataclasses import dataclass, replace
from pathlib import Path

from PIL import Image

from app.security import ClientLogin

log = logging.getLogger(__name__)

CONFIG = "clients.json"
SLUG_RE = re.compile(r"[a-z0-9]{1,32}")  # the page is /client_<slug>; security.client_routes(slug) allows it
PAGE_RE = re.compile(r"/client_([a-z0-9]{1,32})/?")
FILE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,80}\.(?:png|webp|jpe?g)", re.IGNORECASE)
HEX_RE = re.compile(r"#[0-9A-Fa-f]{6}")
USERNAME_RE = re.compile(r"[A-Za-z0-9._@-]{1,64}")
IMAGE_TYPES = {"PNG": "image/png", "WEBP": "image/webp", "JPEG": "image/jpeg"}
MAX_IMAGE_BYTES = 512 * 1024  # each image is written into the page itself; keep them small (tens of KB)
MAX_IMAGE_SIDE = 4096  # px; a logo needs far less, and a huge one would cost every visitor's browser the memory
TEXT_LIMIT = 80  # characters, for the name and each wordmark part
ROLES = ("primary", "secondary", "tertiary")  # the client's colours; the wordmark's parts name one, or "text"
MARK_HEIGHT = 30  # the header logo's height in CSS px (static/css/app.css .brand-logo)
LOGIN_LOGO_WIDTH = 260

# The app's own surfaces (static/css/app.css), which the client's colours must read well on.
WHITE, BLACK = "#ffffff", "#000000"
DARK_SURFACE, DARK_SURFACE_2 = "#151922", "#1b2029"


# ---------- colours ----------

def _rgb(color: str) -> tuple[int, int, int]:
    return int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)


def _hex(rgb: tuple[float, float, float]) -> str:
    return "#" + "".join(f"{max(0, min(255, round(c))):02x}" for c in rgb)


def mix(a: str, b: str, t: float) -> str:
    """`a` with the share `t` of `b` mixed in."""
    return _hex(tuple(x + (y - x) * t for x, y in zip(_rgb(a), _rgb(b))))


def _over(color: str, alpha: float, background: str) -> str:
    """`color` at opacity `alpha` painted over `background`."""
    return mix(background, color, alpha)


def _luminance(color: str) -> float:
    def channel(c: int) -> float:
        c = c / 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(c) for c in _rgb(color))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: str, b: str) -> float:
    """The WCAG contrast ratio of two colours (1 to 21)."""
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def readable(color: str, backgrounds: tuple[str, ...], toward: str, ratio: float = 4.5) -> str:
    """`color` as it is, or mixed step by step toward `toward` (black or white), until it reads at `ratio` (WCAG AA
    for text) on every one of `backgrounds`."""
    for step in range(0, 101, 2):
        candidate = mix(color, toward, step / 100)
        if all(contrast(candidate, bg) >= ratio for bg in backgrounds):
            return candidate
    return toward


def _rgba(color: str, alpha: float) -> str:
    r, g, b = _rgb(color)
    return f"rgba({r}, {g}, {b}, {alpha})"


def theme(colors: dict[str, str]) -> tuple[dict[str, str], dict[str, str]]:
    """The app's colour tokens (static/css/app.css) in the client's colours, for light and dark: the primary colour
    takes the accent (buttons, selections, focus, links), the secondary the step numbers, and all three the header's
    wordmark. Every colour used for text is adjusted until it reads at 4.5:1 where it is shown; the header's band
    (--band-1/2) shows the colours exactly as in the logo."""
    p, s, t = (colors[role] for role in ROLES)
    light_soft = mix(p, WHITE, 0.92)
    light_step = mix(s, WHITE, 0.9)
    light_accent = readable(p, (WHITE,), BLACK)  # white text on the buttons
    light = {
        "--accent": light_accent,
        "--accent-hover": mix(light_accent, BLACK, 0.15),
        "--accent-contrast": WHITE,
        "--accent-soft": light_soft,
        "--accent-border": mix(p, WHITE, 0.68),
        "--accent-text": readable(p, (WHITE, light_soft), BLACK),
        "--accent-ring": _rgba(p, 0.28),
        "--brand-1": readable(p, (WHITE,), BLACK),
        "--brand-2": readable(s, (WHITE,), BLACK),
        "--brand-3": readable(t, (WHITE,), BLACK),
        "--step-bg": light_step,
        "--step-text": readable(s, (light_step,), BLACK),
        "--band-1": p,
        "--band-2": s,
    }
    # Dark: the accent is also the focus ring, so it is the lightest mix toward white that still keeps white button
    # text at 4.6:1 (lighter stands out more on the dark surfaces: about 3.4:1 or more against them, above the 3:1 a
    # focus ring needs); hover darkens it instead, keeping the white text readable. Text colours are lightened until
    # they read on the dark surfaces, including the tinted backgrounds of selections.
    dark_accent = next((mix(p, WHITE, step / 100) for step in range(100, -1, -2)
                        if contrast(mix(p, WHITE, step / 100), WHITE) >= 4.6), light_accent)
    dark_hover = mix(dark_accent, BLACK, 0.12)
    soft_tone = mix(p, WHITE, 0.3)
    step_tone = mix(s, WHITE, 0.3)
    dark_soft_bg = _over(soft_tone, 0.16, DARK_SURFACE)
    dark_step_bg = _over(step_tone, 0.18, DARK_SURFACE)
    dark = {
        "--accent": dark_accent,
        "--accent-hover": dark_hover,
        "--accent-soft": _rgba(soft_tone, 0.16),
        "--accent-border": _rgba(mix(p, WHITE, 0.45), 0.45),
        "--accent-text": readable(p, (DARK_SURFACE, DARK_SURFACE_2, dark_soft_bg), WHITE),
        "--accent-ring": _rgba(mix(p, WHITE, 0.45), 0.4),
        "--brand-1": readable(p, (DARK_SURFACE,), WHITE),
        "--brand-2": readable(s, (DARK_SURFACE,), WHITE),
        "--brand-3": readable(t, (DARK_SURFACE,), WHITE),
        "--step-bg": _rgba(step_tone, 0.18),
        "--step-text": readable(s, (DARK_SURFACE, dark_step_bg), WHITE),
    }
    return light, dark


def _block(selector: str, tokens: dict[str, str]) -> str:
    return selector + " { " + " ".join(f"{name}: {value};" for name, value in tokens.items()) + " }"


# ---------- the client folder ----------

@dataclass(frozen=True)
class ClientBrand:
    slug: str
    name: str
    logo: str  # data: URI of the whole logo (the sign-in page)
    logo_size: tuple[int, int]
    mark: str  # data: URI of the header's logo
    mark_size: tuple[int, int]
    mark_alt: str
    icon: str  # data: URI of the browser tab's icon
    wordmark: tuple[tuple[str, str], ...]  # (text, role) parts shown next to the header's logo
    light: tuple[tuple[str, str], ...]  # colour tokens (theme())
    dark: tuple[tuple[str, str], ...]
    login: ClientLogin | None = None  # the page's own login; without one only the studio login can open the page

    @property
    def path(self) -> str:
        return f"/client_{self.slug}"


class ClientError(ValueError):
    pass


def _image(folder: Path, name: object, what: str) -> tuple[str, tuple[int, int]]:
    """A logo file from the client folder as a data: URI, with its size. Only PNG, WebP and JPEG (no SVG: it could
    carry scripts), only plain file names and only files really in the folder (a link to elsewhere is refused), at
    most MAX_IMAGE_BYTES and MAX_IMAGE_SIDE. Any problem with the file is a ClientError, so it leaves the client out
    and never stops the app."""
    if not isinstance(name, str) or not FILE_RE.fullmatch(name):
        raise ClientError(f"{what}: a PNG, WebP or JPEG file name in {folder.name}/ is needed, not {name!r}")
    path = folder / name
    try:
        if not path.is_file() or path.resolve().parent != folder.resolve():
            raise ClientError(f"{what}: {folder.name}/{name} is missing (or is a link to a file elsewhere)")
        if path.stat().st_size > MAX_IMAGE_BYTES:
            raise ClientError(f"{what}: {name} is {path.stat().st_size // 1024} KB; at most "
                              f"{MAX_IMAGE_BYTES // 1024} KB")
        data = path.read_bytes()
        with Image.open(io.BytesIO(data)) as im:  # Pillow also refuses decompression bombs here
            fmt, size = im.format, im.size
            im.verify()
    except ClientError:
        raise
    except Exception as exc:  # unreadable, not an image, corrupt, too many pixels (a decompression bomb) …
        raise ClientError(f"{what}: {name} can't be used ({type(exc).__name__}: {exc})") from None
    if fmt not in IMAGE_TYPES:
        raise ClientError(f"{what}: {name} is {fmt}; use PNG, WebP or JPEG")
    if max(size) > MAX_IMAGE_SIDE:
        raise ClientError(f"{what}: {name} is {size[0]} × {size[1]} px; at most {MAX_IMAGE_SIDE} px on a side")
    return f"data:{IMAGE_TYPES[fmt]};base64,{base64.b64encode(data).decode('ascii')}", size


def _text(value: object, what: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ClientError(f"{what}: a text of 1 to {limit} characters is needed")
    return value.strip()


def _client(folder: Path, entry: object) -> ClientBrand:
    if not isinstance(entry, dict):
        raise ClientError("each client is an object")
    slug = entry.get("slug")
    if not isinstance(slug, str) or not SLUG_RE.fullmatch(slug):
        raise ClientError(f"slug: lower-case letters and digits only (1 to 32), not {slug!r}")
    name = _text(entry.get("name"), "name", TEXT_LIMIT)
    colors = entry.get("colors")
    if not isinstance(colors, dict) or not all(isinstance(colors.get(r), str) and HEX_RE.fullmatch(colors[r])
                                               for r in ROLES):
        raise ClientError("colors: primary, secondary and tertiary, each as #RRGGBB")
    logo, logo_size = _image(folder, entry.get("logo"), "logo")
    mark, mark_size = _image(folder, entry.get("mark", entry.get("logo")), "mark")
    icon, _ = _image(folder, entry.get("icon", entry.get("mark", entry.get("logo"))), "icon")
    raw_parts = entry.get("wordmark", [[name, "text"]])
    if not isinstance(raw_parts, list) or not 1 <= len(raw_parts) <= 6:
        raise ClientError("wordmark: a list of 1 to 6 [text, colour] parts")
    parts = []
    for part in raw_parts:
        if not (isinstance(part, list) and len(part) == 2 and part[1] in (*ROLES, "text")):
            raise ClientError(f"wordmark: each part is [text, one of {', '.join(ROLES)} or text], not {part!r}")
        parts.append((_text(part[0], "wordmark text", TEXT_LIMIT), part[1]))
    login = None
    if "login" in entry:
        raw = entry["login"]
        username = raw.get("username") if isinstance(raw, dict) else None
        password = raw.get("password") if isinstance(raw, dict) else None
        if not isinstance(username, str) or not USERNAME_RE.fullmatch(username):
            raise ClientError('login: {"username": 1 to 64 letters, digits or . _ @ -, "password": …} is needed')
        if not isinstance(password, str) or not 1 <= len(password) <= 200:
            raise ClientError("login: the password must be 1 to 200 characters")
        login = ClientLogin(slug=slug, username=username, password=password)
    light, dark = theme({r: colors[r].lower() for r in ROLES})
    return ClientBrand(
        slug=slug, name=name, logo=logo, logo_size=logo_size, mark=mark, mark_size=mark_size,
        mark_alt=str(entry.get("mark_alt") or "").strip()[:TEXT_LIMIT], icon=icon, wordmark=tuple(parts),
        light=tuple(light.items()), dark=tuple(dark.items()), login=login,
    )


def load_clients(folder: Path, reserved_usernames: tuple[str, ...] = ()) -> dict[str, ClientBrand]:
    """The client pages configured in `folder`/clients.json, by slug. A missing folder or file means none; a client
    with a mistake is left out and the mistake logged, so the app always starts. A client login whose username is
    one of `reserved_usernames` (the studio's and the demo's) or another client's is turned off, also logged: one
    username must mean one login."""
    config = folder / CONFIG
    try:
        if not config.is_file():
            return {}
        entries = json.loads(config.read_text(encoding="utf-8-sig")).get("clients", [])
        if not isinstance(entries, list):
            raise ClientError('"clients" must be a list')
    except Exception as exc:  # unreadable, not JSON, too deeply nested …: no client pages, but the app starts
        log.error("client pages are off: %s can't be used (%s: %s)", config, type(exc).__name__, exc)
        return {}
    clients: dict[str, ClientBrand] = {}
    taken = {name.casefold() for name in reserved_usernames if name}
    for number, entry in enumerate(entries, 1):
        try:
            brand = _client(folder, entry)
        except ClientError as exc:
            log.error("client %d in %s is left out: %s", number, config, exc)
            continue
        except Exception:  # anything unforeseen leaves this client out, not the app down
            log.exception("client %d in %s is left out", number, config)
            continue
        if brand.slug in clients:
            log.error("client %d in %s is left out: the slug %r is used twice", number, config, brand.slug)
            continue
        if brand.login is not None:
            if brand.login.username.casefold() in taken:
                log.error("client %d in %s: its login is off, the username %r is already in use",
                          number, config, brand.login.username)
                brand = replace(brand, login=None)
            else:
                taken.add(brand.login.username.casefold())
        clients[brand.slug] = brand
    if clients:
        log.info("client pages: %s", ", ".join(
            b.path + (" (own login)" if b.login else " (head admin login only)") for b in clients.values()))
    return clients


def brand_for_path(clients: dict[str, ClientBrand], target: str) -> ClientBrand | None:
    """The client whose page `target` (a path, maybe with a query or #fragment) is exactly, if any. Matched on the
    text itself, not through a URL parser (which would drop tabs and newlines, and read //host as a host)."""
    match = PAGE_RE.fullmatch((target or "").split("#", 1)[0].split("?", 1)[0])
    return clients.get(match.group(1)) if match else None


# ---------- pages ----------

def _attr(text: str) -> str:
    return html.escape(text, quote=True)


def _sub(pattern: str, replacement: str, page: str, what: str, required: bool = True) -> str:
    """One replacement in index.html. The patterns follow the markup, not its wording. A required one (anything
    that would otherwise show the studio's brand) that finds nothing raises ValueError, so a page that can't be
    branded is never served; the others are skipped."""
    out, count = re.subn(pattern, lambda m: (m.group(1) if m.re.groups else "") + replacement, page, count=1,
                         flags=re.DOTALL)
    if not count and required:
        raise ValueError(f"static/index.html has no {what}")
    return out


def brand_page(page: str, brand: ClientBrand) -> str:
    """static/index.html as the client's page: marked as the client demo page (data-app="customer", so it behaves
    exactly like /customerep) and data-client=<slug> (its own saved state, its colours), with the client's name in the
    title, the header and the tab icon, and the studio's name taken out of the loading and no-script texts. The full-
    size viewer and the toasts, dark in light mode too, get the dark-mode accent: a readable focus ring on them."""
    name = _attr(brand.name)
    width = round(brand.mark_size[0] * MARK_HEIGHT / brand.mark_size[1])
    words = " ".join(f'<span class="wm-{"text" if role == "text" else ROLES.index(role) + 1}">{html.escape(text)}</span>'
                     for text, role in brand.wordmark)
    header = (f'<span class="brand-logo"><img src="{brand.mark}" alt="{_attr(brand.mark_alt)}" width="{width}" '
              f'height="{MARK_HEIGHT}"></span><span class="brand-wordmark" data-no-translate>{words}</span>')
    root = f':root[data-client="{brand.slug}"]'
    dark = dict(brand.dark)
    style = ("<style>\n" + _block(root, dict(brand.light)) + "\n"
             + _block(f"{root} .lightbox, {root} .toast", {"--accent": dark["--accent"]}) + "\n"
             + "@media (prefers-color-scheme: dark) {\n  " + _block(root, dark) + "\n}\n</style>\n")
    page = _sub(r'<html lang="bn">', f'<html lang="bn" data-app="customer" data-client="{brand.slug}">', page,
                "<html> tag")
    page = _sub(r"<title>[^<]*</title>", f"<title>{name}</title>", page, "<title>")
    page = _sub(r'<link rel="icon"[^>]*>', f'<link rel="icon" href="{brand.icon}">', page, "tab icon")
    page = _sub(r"</head>", style + "</head>", page, "</head>")
    page = _sub(r"<!--brand-->.*?<!--/brand-->", header, page, "<!--brand--> … <!--/brand--> markers")
    page = _sub(r'(<div [^>]*\bid="boot"[^>]*>(?:\s*<span [^>]*></span>)?)[^<]*', "Loading…", page, "loading text")
    page = _sub(r'<meta name="description"[^>]*>', f'<meta name="description" content="{name}">', page,
                "description", required=False)
    return _sub(r'(<p class="noscript">)[^<]*', "This page needs JavaScript. Please enable it and reload the page.",
                page, "no-script text", required=False)


def login_branding(brand: ClientBrand) -> tuple[str, str, str]:
    """The sign-in page's title, extra style and heading in the client's name, logo and colours (main.render_login)."""
    light, dark = dict(brand.light), dict(brand.dark)
    height = round(brand.logo_size[1] * LOGIN_LOGO_WIDTH / brand.logo_size[0])
    style = (f":root{{--accent:{light['--accent']};--b1:{light['--band-1']};--b2:{light['--band-2']}}}"
             f"@media (prefers-color-scheme:dark){{:root{{--accent:{dark['--accent']}}}}}"
             "form{overflow:hidden}"  # the card's round corners clip the band
             "form::before{content:\"\";display:block;height:4px;margin:-28px -28px 22px;"
             "background:linear-gradient(90deg,var(--b1) 50%,var(--b2) 50%)}"
             "h1.logo{margin:0 0 14px;padding:12px;border-radius:10px;background:#fff;display:flex;justify-content:center}"
             f"h1.logo img{{display:block;width:100%;max-width:{LOGIN_LOGO_WIDTH}px;height:auto}}")
    heading = (f'<h1 class="logo"><img src="{brand.logo}" alt="{_attr(brand.name)}" width="{LOGIN_LOGO_WIDTH}" '
               f'height="{height}"></h1>')
    return f"সাইন ইন - {_attr(brand.name)}", style, heading
