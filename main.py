"""Free iOS apps bot — same shape as lootigo-bot, different source.

GamerPower hands out ready-made JSON. The App Store has no such feed, so this
bot builds one:

    appstore-discounts.com/en/free   ->  which apps are free right now
    itunes.apple.com/lookup          ->  is it really free, is it a game, icon

Everything else (GitHub Actions cron, seen_ids.json, the caption layout, the
Claim Now button) works exactly like the games bot.
"""

import html
import json
import os
import re
import time
from datetime import datetime, timezone

import requests

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
SEEN_FILE = "seen_ids.json"

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

LIST_URL = "https://appstore-discounts.com/en/free?p={page}"
DETAIL_URL = "https://appstore-discounts.com/en/app/{app_id}"
ITUNES_URL = "https://itunes.apple.com/lookup?id={app_id}"

# How many listing pages to read (25 apps per page)
PAGES = int(os.environ.get("PAGES", "2"))
# Only post offers that went free within this many hours.
# The site shows "6 h ago" for genuinely fresh drops and its own seed date
# (06/06/2026) for everything older, so a small window means "real drops only".
MAX_AGE_HOURS = int(os.environ.get("MAX_AGE_HOURS", "48"))
# Never flood the channel, however many are waiting
MAX_POSTS_PER_RUN = int(os.environ.get("MAX_POSTS_PER_RUN", "8"))
# Gap between posts, same as the games bot
POST_DELAY = int(os.environ.get("POST_DELAY", "30"))

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
}


def pe(emoji_id, fallback):
    return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'


EMOJI_APP = "📱"
EMOJI_DESC = pe("5334882760735598374", "📝")
EMOJI_PLATFORM = pe("5431376038628171216", "💻")
EMOJI_TYPE = pe("5431721976769027887", "📂")
EMOJI_PRICE = pe("5375296873982604963", "💸")
EMOJI_DATE = pe("5451732530048802485", "⏳")
EMOJI_HOW = pe("5318986077455795572", "📌")

MAX_CAPTION = 1024

HOW_TO_CLAIM = (
    "1. Tap the button to open the App Store page.\n"
    "2. Get it while the price is $0.\n"
    "3. It stays in your Apple ID forever, even after the price goes back up."
)

# ----------------------------------------------------------------- scraping

CARD_RE = re.compile(r'<article class="app-card">(.*?)</article>', re.S)
CARD_ID_RE = re.compile(r"/en/app/(\d+)")
CARD_TITLE_RE = re.compile(r'class="app-card-title">(.*?)</h3>', re.S)
CARD_GENRE_RE = re.compile(r'class="genre">(.*?)</span>', re.S)
CARD_ART_RE = re.compile(r'<img src="(https://[^"]+)"')

OLD_PRICE_RE = re.compile(r'class="price-old">\s*([^<]+?)\s*</span>', re.I)
DROPPED_RE = re.compile(
    r'class="drop-relative">\s*Drop detected\s*([^<]+?)\s*</p>', re.I
)
AGE_RE = re.compile(r"(\d+)\s*(min|h|d|w|mo|y)", re.I)
DATE_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b")
AGE_HOURS = {"min": 1 / 60, "h": 1, "d": 24, "w": 168, "mo": 730, "y": 8760}


def clean(raw, limit=300):
    text = re.sub(r"<[^>]+>", " ", raw or "")
    return " ".join(html.unescape(text).split())[:limit].strip()


def age_hours(text):
    """How long ago did this go free? None when it cannot be read."""
    text = (text or "").strip()
    if not text:
        return None

    absolute = DATE_RE.search(text)
    if absolute:
        first, second, year = (int(x) for x in absolute.groups())
        for day, month in ((first, second), (second, first)):
            try:
                moment = datetime(year, month, day, tzinfo=timezone.utc)
            except ValueError:
                continue
            delta = datetime.now(timezone.utc) - moment
            return max(0.0, delta.total_seconds() / 3600)
        return None

    match = AGE_RE.search(text)
    if not match:
        return None
    return int(match.group(1)) * AGE_HOURS.get(match.group(2).lower(), 1)


def parse_listing(page_html):
    """Cards on the 'free right now' page, games already dropped."""
    cards = []
    for block in CARD_RE.findall(page_html):
        app_id = CARD_ID_RE.search(block)
        title = CARD_TITLE_RE.search(block)
        if not app_id or not title:
            continue
        genre = CARD_GENRE_RE.search(block)
        genre_name = clean(genre.group(1), 40) if genre else ""
        if genre_name.lower() == "games":
            continue
        art = CARD_ART_RE.search(block)
        cards.append({
            "id": app_id.group(1),
            "title": clean(title.group(1), 120),
            "genre": genre_name,
            "image": art.group(1) if art else "",
        })
    return cards


def parse_detail(page_html):
    """Original price and how long ago it dropped.

    Only a *relative* time ("6 h ago") is trustworthy. For older entries the
    site prints an absolute date, and that date is the same for almost every
    app (06/06/2026) — it is when their database was seeded, not when the app
    went free. So absolute dates are read but never trusted: they must not be
    used to reject an app, and they must not be shown in the post either.
    """
    old = OLD_PRICE_RE.search(page_html)
    dropped = DROPPED_RE.search(page_html)
    dropped_text = html.unescape(dropped.group(1)).strip() if dropped else ""
    reliable = bool(dropped_text) and not DATE_RE.search(dropped_text)
    return {
        "price_old": html.unescape(old.group(1)).strip() if old else "",
        "dropped_text": dropped_text,
        "age_hours": age_hours(dropped_text),
        "age_is_reliable": reliable,
    }


def itunes_lookup(app_id):
    """Apple's own record: genre, current price, icon, official link."""
    try:
        response = requests.get(
            ITUNES_URL.format(app_id=app_id), headers=HEADERS, timeout=15
        )
        results = response.json().get("results") or []
    except Exception as exc:
        print(f"    itunes error: {exc}")
        return None
    return results[0] if results else None


def tag_from_genre(genre):
    return re.sub(r"[^A-Za-z0-9]", "", (genre or "").replace("&", "And")) or "App"


# ------------------------------------------------------------------ posting

def truncate(text, max_len):
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


def build_caption(app):
    description = app["description"]
    caption = (
        f"{EMOJI_APP} <b>{app['title']}</b>\n\n"
        f"{EMOJI_DESC} {description}\n\n"
        f"{EMOJI_PLATFORM} <b>Platform:</b> {app['platform']}\n"
        f"{EMOJI_TYPE} <b>Type:</b> #{app['tag']}\n"
        f"{EMOJI_PRICE} <b>Price:</b> {app['price']}\n"
        f"{EMOJI_DATE} <b>Ends:</b> {app['ends']}\n\n"
        f"{EMOJI_HOW} <b>How to Claim</b>\n{HOW_TO_CLAIM}"
    )

    # Only the description gives way when the caption is too long — the
    # fields and the claim steps always survive.
    if len(caption) > MAX_CAPTION:
        room = MAX_CAPTION - (len(caption) - len(description))
        description = truncate(description, max(room, 40))
        caption = (
            f"{EMOJI_APP} <b>{app['title']}</b>\n\n"
            f"{EMOJI_DESC} {description}\n\n"
            f"{EMOJI_PLATFORM} <b>Platform:</b> {app['platform']}\n"
            f"{EMOJI_TYPE} <b>Type:</b> #{app['tag']}\n"
            f"{EMOJI_PRICE} <b>Price:</b> {app['price']}\n"
            f"{EMOJI_DATE} <b>Ends:</b> {app['ends']}\n\n"
            f"{EMOJI_HOW} <b>How to Claim</b>\n{HOW_TO_CLAIM}"
        )

    return caption[:MAX_CAPTION]


def send_photo(app):
    payload = {
        "chat_id": CHAT_ID,
        "photo": app["image"],
        "caption": build_caption(app),
        "parse_mode": "HTML",
        "reply_markup": json.dumps({
            "inline_keyboard": [[
                {"text": "✅ Claim Now", "url": app["url"]}
            ]]
        }),
    }
    response = requests.post(f"{TELEGRAM_API}/sendPhoto", data=payload, timeout=20)
    if not response.ok:
        print(f"  Telegram error: {response.status_code} - {response.text[:300]}")
        return False
    print(f"  Sent: {app['title']}")
    return True


# --------------------------------------------------------------------- state

def load_seen():
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE) as f:
            return set(json.load(f))
    return set()


def save_seen(seen):
    with open(SEEN_FILE, "w") as f:
        json.dump(sorted(seen), f, indent=2)


# ---------------------------------------------------------------------- main

def collect(seen):
    """Free apps worth posting, newest drops first."""
    cards = []
    for page in range(1, PAGES + 1):
        try:
            response = requests.get(
                LIST_URL.format(page=page), headers=HEADERS, timeout=20
            )
            response.raise_for_status()
        except Exception as exc:
            print(f"  Listing page {page} failed: {exc}")
            continue
        cards.extend(parse_listing(response.text))

    print(f"  Listed: {len(cards)} non-game apps")

    fresh = [c for c in cards if c["id"] not in seen]
    print(f"  New: {len(fresh)} | Already seen: {len(cards) - len(fresh)}")

    apps = []
    for card in fresh:
        if len(apps) >= MAX_POSTS_PER_RUN:
            break

        # The detail page is the only place with the original price
        try:
            response = requests.get(
                DETAIL_URL.format(app_id=card["id"]), headers=HEADERS, timeout=20
            )
            response.raise_for_status()
            detail = parse_detail(response.text)
        except Exception as exc:
            print(f"    {card['title']}: detail failed ({exc})")
            detail = {"price_old": "", "dropped_text": "",
                      "age_hours": None, "age_is_reliable": False}

        # Reject only on a timestamp we can believe. Everything else is still
        # a genuinely free app (Apple confirms the price below), so it goes out
        # — the per-run cap drains the backlog a few at a time.
        age = detail["age_hours"]
        if detail["age_is_reliable"] and age is not None and age > MAX_AGE_HOURS:
            print(f"    {card['title']}: stale ({detail['dropped_text']})")
            seen.add(card["id"])  # judged once, do not re-check every run
            continue

        info = itunes_lookup(card["id"])
        if info is None:
            print(f"    {card['title']}: not on the store any more")
            continue
        if (info.get("primaryGenreName") or "").lower() == "games":
            print(f"    {card['title']}: it is a game")
            seen.add(card["id"])
            continue
        if float(info.get("price") or 0) != 0:
            print(f"    {card['title']}: no longer free")
            continue

        genre = info.get("primaryGenreName") or card["genre"]
        price = detail["price_old"]
        apps.append({
            "id": card["id"],
            "title": info.get("trackName") or card["title"],
            "description": clean(info.get("description") or "", 600),
            "platform": "iOS, iPadOS",
            "tag": tag_from_genre(genre),
            "price": f"{price} → Free" if price else "Free",
            # Never print the site's seed date as if it were the drop date
            "ends": (
                f"Free since {detail['dropped_text']} — can end any time"
                if detail["age_is_reliable"] else "Limited time — grab it now"
            ),
            "image": (info.get("artworkUrl512") or info.get("artworkUrl100")
                      or card["image"]),
            "url": info.get("trackViewUrl")
                   or f"https://apps.apple.com/us/app/id{card['id']}",
        })

    return apps


def main():
    seen = load_seen()
    print(f"Loaded {len(seen)} seen IDs")

    print("\nProcessing free iOS apps...")
    apps = collect(seen)
    print(f"\nReady to post: {len(apps)}")

    for index, app in enumerate(apps):
        if send_photo(app):
            seen.add(app["id"])
        if index < len(apps) - 1:
            time.sleep(POST_DELAY)

    save_seen(seen)
    print(f"\nSaved {len(seen)} seen IDs - Done.")


if __name__ == "__main__":
    main()
