"""Free apps bot — same shape as lootigo-bot, two stores.

GamerPower hands out ready-made JSON. Neither app store does, so this bot
builds its own feed for each:

  iOS      appstore-discounts.com/en/free  ->  what is free right now
           itunes.apple.com/lookup         ->  genre, current price, icon

  Android  r/googleplaydeals (RSS)         ->  which packages people found
           play.google.com ld+json         ->  genre, current price, icon

The second step in each pair is the one that matters: it is the store's own
record, so games and already-expired offers never reach the channel.

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

# --- iOS
LIST_URL = "https://appstore-discounts.com/en/free?p={page}"
DETAIL_URL = "https://appstore-discounts.com/en/app/{app_id}"
ITUNES_URL = "https://itunes.apple.com/lookup?id={app_id}"

# --- Android
REDDIT_URL = "https://www.reddit.com/r/googleplaydeals/new/.rss"
PLAY_URL = "https://play.google.com/store/apps/details?id={pkg}&hl=en&gl=US"

# Which stores to collect from
IOS_ENABLED = os.environ.get("IOS_ENABLED", "1") != "0"
ANDROID_ENABLED = os.environ.get("ANDROID_ENABLED", "1") != "0"

# Print what would be posted instead of posting it
DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"

PAGES = int(os.environ.get("PAGES", "2"))
MAX_AGE_HOURS = int(os.environ.get("MAX_AGE_HOURS", "48"))
MAX_POSTS_PER_RUN = int(os.environ.get("MAX_POSTS_PER_RUN", "8"))
# r/googleplaydeals is roughly 90% games, so most lookups end in a rejection.
# The budget has to cover the whole candidate list or the few real apps
# never get reached. Each lookup is one ~1MB page, which is nothing on a runner.
MAX_LOOKUPS_PER_RUN = int(os.environ.get("MAX_LOOKUPS_PER_RUN", "30"))
POST_DELAY = int(os.environ.get("POST_DELAY", "30"))

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
}
# Reddit rejects generic browser agents and rate-limits hard; identify honestly
REDDIT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; LootigoAppsBot/1.0 by /u/P3R6)",
    "Accept": "application/atom+xml,text/xml",
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

HOW_TO_CLAIM = {
    "ios": (
        "1. Tap the button to open the App Store page.\n"
        "2. Get it while the price is $0.\n"
        "3. It stays in your Apple ID forever, even after the price goes back up."
    ),
    "android": (
        "1. Tap the button to open the Google Play page.\n"
        "2. Install it while the price is $0.\n"
        "3. It stays in your Google account forever, even after the price goes back up."
    ),
}

# ----------------------------------------------------------------- shared

def clean(raw, limit=300):
    text = re.sub(r"<[^>]+>", " ", raw or "")
    return " ".join(html.unescape(text).split())[:limit].strip()


def tag_from_genre(genre):
    """'Health & Fitness' and 'HEALTH_AND_FITNESS' both -> HealthAndFitness"""
    words = re.split(r"[^A-Za-z0-9]+", (genre or "").replace("&", " And "))
    return "".join(w.capitalize() for w in words if w) or "App"


PRICE_RE = re.compile(r"\$\s?\d[\d,]*(?:\.\d{2})?")


def truncate(text, max_len):
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


# --------------------------------------------------------------- iOS side

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


def collect_ios(seen):
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

        age = detail["age_hours"]
        if detail["age_is_reliable"] and age is not None and age > MAX_AGE_HOURS:
            print(f"    {card['title']}: stale ({detail['dropped_text']})")
            seen.add(card["id"])
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
        if not price:
            # Same rule as Android: no original price, no proof it is a drop.
            print(f"    {card['title']}: no original price, not a drop")
            seen.add(card["id"])
            continue

        apps.append({
            "id": card["id"],
            "store": "ios",
            "title": info.get("trackName") or card["title"],
            "description": clean(info.get("description") or "", 600),
            "platform": "iOS, iPadOS",
            "tag": tag_from_genre(genre),
            "price": f"{price} → Free",
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


# ----------------------------------------------------------- Android side

PLAY_PKG_RE = re.compile(
    r"play\.google\.com/store/apps/details\?[^\"'<>\s]*?\bid=([a-zA-Z0-9_.]+)"
)
ENTRY_RE = re.compile(r"<entry>(.*?)</entry>", re.S)
ENTRY_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.S)
LD_JSON_RE = re.compile(
    r'<script type="application/ld\+json"[^>]*>(.*?)</script>', re.S
)


def parse_reddit_packages(xml_text):
    """Package ids people posted, newest first, de-duplicated."""
    found = []
    for entry in ENTRY_RE.findall(xml_text):
        title = ENTRY_TITLE_RE.search(entry)
        label = clean(title.group(1), 120) if title else ""
        for pkg in PLAY_PKG_RE.findall(html.unescape(entry)):
            if pkg not in [f["pkg"] for f in found]:
                found.append({"pkg": pkg, "title": label})
    return found


def play_lookup(pkg):
    """Google has no public API, but every app page carries an ld+json block
    with the same four things the iTunes endpoint gives."""
    try:
        response = requests.get(
            PLAY_URL.format(pkg=pkg), headers=HEADERS, timeout=25
        )
        response.raise_for_status()
        body = response.text
    except Exception as exc:
        print(f"    play error for {pkg}: {exc}")
        return None

    for block in LD_JSON_RE.findall(body):
        try:
            data = json.loads(block)
        except (ValueError, TypeError):
            continue
        if not isinstance(data, dict):
            continue
        if data.get("@type") not in ("SoftwareApplication", "MobileApplication"):
            continue

        offers = data.get("offers") or []
        if isinstance(offers, dict):
            offers = [offers]
        price = 0.0
        for offer in offers:
            try:
                price = max(price, float(offer.get("price") or 0))
            except (TypeError, ValueError):
                pass

        return {
            "name": data.get("name") or "",
            "category": (data.get("applicationCategory") or "").upper(),
            "image": data.get("image") or "",
            "description": clean(data.get("description") or "", 600),
            "price": price,
        }
    return None


def collect_android(seen):
    try:
        response = requests.get(REDDIT_URL, headers=REDDIT_HEADERS, timeout=25)
        response.raise_for_status()
        candidates = parse_reddit_packages(response.text)
    except Exception as exc:
        # Reddit rate-limits GitHub's shared runner IPs; a miss here is normal
        print(f"  Reddit unavailable: {exc}")
        return []

    print(f"  Found: {len(candidates)} packages")
    fresh = [c for c in candidates if c["pkg"] not in seen]
    print(f"  New: {len(fresh)} | Already seen: {len(candidates) - len(fresh)}")

    apps = []
    looked_up = 0
    for card in fresh:
        if len(apps) >= MAX_POSTS_PER_RUN or looked_up >= MAX_LOOKUPS_PER_RUN:
            break
        looked_up += 1

        info = play_lookup(card["pkg"])
        if info is None:
            print(f"    {card['pkg']}: no store record")
            continue
        if info["category"].startswith("GAME"):
            print(f"    {info['name'] or card['pkg']}: it is a game")
            seen.add(card["pkg"])
            continue
        if info["price"] != 0:
            print(f"    {info['name'] or card['pkg']}: no longer free")
            continue

        # The old price only ever appears in the Reddit title:
        # "(was $4.99)" / "$4.99 -> Free". Google's page shows today's price
        # only, so without that number there is no evidence this was ever paid.
        old_price = ""
        money = PRICE_RE.search(card["title"])
        if money:
            old_price = money.group(0).replace(" ", "")

        if not old_price:
            # An app that was already free is not a deal — do not post it.
            print(f"    {info['name'] or card['pkg']}: no original price, not a drop")
            seen.add(card["pkg"])
            continue

        apps.append({
            "id": card["pkg"],
            "store": "android",
            "title": info["name"] or card["title"] or card["pkg"],
            "description": info["description"],
            "platform": "Android",
            "tag": tag_from_genre(info["category"]),
            "price": f"{old_price} → Free",
            "ends": "Limited time — grab it now",
            "image": info["image"],
            "url": f"https://play.google.com/store/apps/details?id={card['pkg']}",
        })

    return apps


# ------------------------------------------------------------------ posting

def build_caption(app):
    description = app["description"]
    how = HOW_TO_CLAIM.get(app.get("store", "ios"), HOW_TO_CLAIM["ios"])

    def render(desc):
        return (
            f"{EMOJI_APP} <b>{app['title']}</b>\n\n"
            f"{EMOJI_DESC} {desc}\n\n"
            f"{EMOJI_PLATFORM} <b>Platform:</b> {app['platform']}\n"
            f"{EMOJI_TYPE} <b>Type:</b> #{app['tag']}\n"
            f"{EMOJI_PRICE} <b>Price:</b> {app['price']}\n"
            f"{EMOJI_DATE} <b>Ends:</b> {app['ends']}\n\n"
            f"{EMOJI_HOW} <b>How to Claim</b>\n{how}"
        )

    caption = render(description)
    # Only the description gives way when the caption is too long — the
    # fields and the claim steps always survive.
    if len(caption) > MAX_CAPTION:
        room = MAX_CAPTION - (len(caption) - len(description))
        caption = render(truncate(description, max(room, 40)))
    return caption[:MAX_CAPTION]


def send_photo(app):
    if DRY_RUN:
        print(f"  [dry-run] would post: {app['title']}")
        return True

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

def main():
    seen = load_seen()
    print(f"Loaded {len(seen)} seen IDs")

    apps = []
    if IOS_ENABLED:
        print("\nProcessing free iOS apps...")
        apps += collect_ios(seen)
    if ANDROID_ENABLED:
        print("\nProcessing free Android apps...")
        apps += collect_android(seen)

    apps = apps[:MAX_POSTS_PER_RUN]
    print(f"\nReady to post: {len(apps)}")

    for index, app in enumerate(apps):
        if send_photo(app):
            seen.add(app["id"])
        if index < len(apps) - 1 and not DRY_RUN:
            time.sleep(POST_DELAY)

    if DRY_RUN:
        print("\nDry run — nothing posted, seen_ids not saved.")
        for app in apps:
            print("\n" + "-" * 60)
            print(build_caption(app))
            print(f"[ ✅ Claim Now ] -> {app['url']}")
        return

    save_seen(seen)
    print(f"\nSaved {len(seen)} seen IDs - Done.")


if __name__ == "__main__":
    main()
