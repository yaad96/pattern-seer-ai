"""Option lists shown in the UI.

`value` strings are inserted verbatim into prompts, so they stay precise English
garment-construction phrases; `label` and `description` are what staff and
clients see (Bangla: common terms in Banglish, full sentences in Bangla).
Before/after pictures for the output types live in static/examples/
(<mode>_before.webp + <mode>_after.webp); a card shows one when both files exist.
"""

from __future__ import annotations

from pathlib import Path

EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "static" / "examples"

SPORTS = [
    {"value": "football (soccer)", "label": "ফুটবল"},
    {"value": "cricket", "label": "ক্রিকেট"},
    {"value": "basketball", "label": "বাস্কেটবল"},
    {"value": "volleyball", "label": "ভলিবল"},
    {"value": "rugby", "label": "রাগবি"},
    {"value": "kabaddi", "label": "কাবাডি"},
    {"value": "handball", "label": "হ্যান্ডবল"},
    {"value": "badminton", "label": "ব্যাডমিন্টন"},
    {"value": "table tennis", "label": "টেবিল টেনিস"},
    {"value": "baseball", "label": "বেসবল"},
    {"value": "ice hockey", "label": "আইস হকি"},
    {"value": "esports (gaming)", "label": "ই-স্পোর্টস / গেমিং"},
    {"value": "cycling", "label": "সাইক্লিং"},
    {"value": "running (athletics)", "label": "রানিং / অ্যাথলেটিক্স"},
]

COLLARS = [
    {"value": "", "label": "ডিজাইনারের পছন্দ"},
    {"value": "crew neck (round neck) with a ribbed neckband", "label": "ক্রু / রাউন্ড নেক"},
    {"value": "V-neck", "label": "ভি-নেক"},
    {"value": "insert V-neck with a contrast panel inside the V", "label": "ইনসার্ট ভি-নেক"},
    {"value": "crossover V-neck with overlapping neckband ends", "label": "ক্রসওভার ভি-নেক"},
    {"value": "polo collar with a two-button placket", "label": "পোলো কলার + বোতাম"},
    {"value": "johnny collar (polo collar over an open V placket, no buttons)", "label": "জনি কলার"},
    {"value": "mandarin (band) collar", "label": "ম্যান্ডারিন / ব্যান্ড কলার"},
    {"value": "henley neck with a short button placket", "label": "হেনলি"},
    {"value": "hooded neck", "label": "হুড"},
]

SLEEVES = [
    {"value": "", "label": "ডিজাইনারের পছন্দ"},
    {"value": "short set-in sleeves", "label": "শর্ট, সেট-ইন"},
    {"value": "short raglan sleeves", "label": "শর্ট, র‍্যাগলান"},
    {"value": "long set-in sleeves with ribbed cuffs", "label": "লং, সেট-ইন"},
    {"value": "long raglan sleeves with ribbed cuffs", "label": "লং, র‍্যাগলান"},
    {"value": "three-quarter raglan sleeves", "label": "থ্রি-কোয়ার্টার র‍্যাগলান"},
    {"value": "sleeveless (singlet / tank cut)", "label": "স্লিভলেস"},
    {"value": "cap sleeves", "label": "ক্যাপ স্লিভ"},
]

RECREATE_MODES = [
    {
        "value": "pattern",
        "label": "শুধু প্যাটার্ন",
        "description": "গার্মেন্ট, মকআপ, ব্র্যান্ড, লোগো বাদ দেওয়া। সাবলিমেশনের জন্য বেস্ট।",
    },
    {
        "value": "flat_jersey",
        "label": "ফ্ল্যাট জার্সি আর্টওয়ার্ক",
        "description": "",
    },
    {
        "value": "replica",
        "label": "এইচডি ভার্সন",
        "description": "একই ছবি, হাই রেজোলিউশনে বানানো।",
    },
    {
        "value": "panels",
        "label": "প্রোডাকশন প্যানেল",
        "description": "৩টি প্রিন্ট-রেডি প্যানেল: ফ্রন্ট, ব্যাক ও স্লিভ। প্রতিটি পুরো আয়তক্ষেত্র জুড়ে ডিজাইন, কোনো সাদা বর্ডার নেই, 3XL প্রিন্ট সাইজে (ফ্রন্ট ও ব্যাক 25 × 34 ইঞ্চি, স্লিভ 24 × 14 ইঞ্চি)। দুই হাতেই একই স্লিভ।",
    },
]

RENDER_STYLES = [
    {"value": "flat", "label": "ফ্ল্যাট আর্টওয়ার্ক", "description": "2D প্রোডাকশন আর্টওয়ার্ক (টেকনিক্যাল ফ্ল্যাট), সলিড রং, কোনো শেডিং নেই।"},
    {"value": "mockup", "label": "ফটো মকআপ", "description": "অদৃশ্য ম্যানিকিনে পরানো বাস্তবসম্মত স্টুডিও ছবি, ক্লায়েন্টের অনুমোদনের জন্য।"},
    {"value": "pattern", "label": "শুধু প্যাটার্ন", "description": "শুধু প্রিন্টের প্যাটার্ন, পুরো ক্যানভাস জুড়ে, সাবলিমেশনের জন্য।"},
]

VIEWS = [
    {"value": "front_back", "label": "ফ্রন্ট + ব্যাক"},
    {"value": "front", "label": "ফ্রন্ট"},
    {"value": "back", "label": "ব্যাক"},
]

ASPECT_RATIOS = [
    {"value": "auto", "label": "অটো"},
    {"value": "1:1", "label": "স্কয়ার 1:1"},
    {"value": "4:5", "label": "পোর্ট্রেট 4:5"},
    {"value": "3:4", "label": "পোর্ট্রেট 3:4"},
    {"value": "2:3", "label": "পোর্ট্রেট 2:3"},
    {"value": "9:16", "label": "লম্বা 9:16"},
    {"value": "5:4", "label": "ল্যান্ডস্কেপ 5:4"},
    {"value": "4:3", "label": "ল্যান্ডস্কেপ 4:3"},
    {"value": "3:2", "label": "ল্যান্ডস্কেপ 3:2"},
    {"value": "16:9", "label": "ওয়াইড 16:9"},
    {"value": "21:9", "label": "আল্ট্রা-ওয়াইড 21:9"},
]

RESOLUTIONS = [
    {"value": "1K", "label": "1K - কুইক প্রিভিউ"},
    {"value": "2K", "label": "2K - ফুল এইচডি"},
    {"value": "4K", "label": "4K - ম্যাক্সিমাম ডিটেইল"},
]

PRINT_SIZES = [
    {"value": 0, "label": "জেনারেট যেমন হয়েছে"},
    {"value": 4096, "label": "4096 px (300 dpi-তে ~35 cm)"},
    {"value": 6144, "label": "6144 px (300 dpi-তে ~52 cm)"},
    {"value": 8192, "label": "8192 px (300 dpi-তে ~69 cm)"},
]

VECTOR_PRESETS = [
    {"value": "flat", "label": "ভেক্টর - ফ্ল্যাট (8 কালার)", "description": "সবচেয়ে মসৃণ আকার ও সবচেয়ে ছোট ফাইল। পরিষ্কার গ্রাফিক্সের জন্য সেরা।"},
    {"value": "standard", "label": "ভেক্টর - স্ট্যান্ডার্ড (16 কালার)", "description": "বেশিরভাগ জার্সি ডিজাইনের জন্য ভালো ভারসাম্য।"},
    {"value": "detailed", "label": "ভেক্টর - ডিটেইলড (32 কালার)", "description": "সূক্ষ্ম টেক্সচার ও গ্রেডিয়েন্ট রাখে; ফাইল বড় হয়।"},
    {"value": "raster", "label": "র‍্যাস্টার EPS (হুবহু পিক্সেল)", "description": "এইচডি ছবিটি EPS-এর ভেতরে রাখা হয়। ফটো বা জটিল গ্রেডিয়েন্টের জন্য ব্যবহার করুন।"},
]


def _with_examples(modes: list[dict]) -> list[dict]:
    out = []
    for mode in modes:
        item = dict(mode)
        before = EXAMPLES_DIR / f"{mode['value']}_before.webp"
        after = EXAMPLES_DIR / f"{mode['value']}_after.webp"
        if before.is_file() and after.is_file():
            item["example"] = {"before": f"/static/examples/{before.name}", "after": f"/static/examples/{after.name}"}
        out.append(item)
    return out


def as_dict() -> dict:
    return {
        "sports": SPORTS,
        "collars": COLLARS,
        "sleeves": SLEEVES,
        "recreate_modes": _with_examples(RECREATE_MODES),
        "render_styles": RENDER_STYLES,
        "views": VIEWS,
        "aspect_ratios": ASPECT_RATIOS,
        "resolutions": RESOLUTIONS,
        "print_sizes": PRINT_SIZES,
        "vector_presets": VECTOR_PRESETS,
    }
