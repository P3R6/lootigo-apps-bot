"""Offline tests — no token, no network.

Run:  python -m unittest -v test_main
"""
import json
import os
import pathlib
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "1:test")
os.environ.setdefault("TELEGRAM_CHAT_ID", "-100123")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import main  # noqa: E402

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"


def load(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


class TestListing(unittest.TestCase):
    def setUp(self):
        self.cards = main.parse_listing(load("free_page.html"))

    def test_finds_apps(self):
        self.assertGreaterEqual(len(self.cards), 10,
                                "no cards — did the site's HTML change?")

    def test_games_are_dropped(self):
        for card in self.cards:
            self.assertNotEqual(card["genre"].lower(), "games")

    def test_fields_are_usable(self):
        for card in self.cards:
            self.assertTrue(card["id"].isdigit())
            self.assertTrue(card["title"])
            self.assertNotIn("<", card["title"])

    def test_garbage_html_is_survivable(self):
        self.assertEqual(main.parse_listing("<<< nope"), [])


class TestDetail(unittest.TestCase):
    def setUp(self):
        self.detail = main.parse_detail(load("detail_page.html"))

    def test_original_price(self):
        self.assertTrue(self.detail["price_old"].startswith("$"))

    def test_drop_time(self):
        self.assertTrue(self.detail["dropped_text"])
        self.assertIsNotNone(self.detail["age_hours"])

    def test_empty_page_is_safe(self):
        empty = main.parse_detail("<html></html>")
        self.assertEqual(empty["price_old"], "")
        self.assertIsNone(empty["age_hours"])
        self.assertFalse(empty["age_is_reliable"])

    def test_relative_time_is_trusted(self):
        detail = main.parse_detail(
            '<p class="drop-relative">Drop detected 6 h ago</p>')
        self.assertTrue(detail["age_is_reliable"])

    def test_absolute_date_is_not_trusted(self):
        """06/06/2026 is the site's seed date, not a real drop date."""
        detail = main.parse_detail(
            '<p class="drop-relative">Drop detected 06/06/2026</p>')
        self.assertFalse(detail["age_is_reliable"])


class TestAge(unittest.TestCase):
    def test_relative(self):
        self.assertEqual(main.age_hours("6 h ago"), 6)
        self.assertEqual(main.age_hours("2 d ago"), 48)
        self.assertEqual(main.age_hours("3 w ago"), 504)

    def test_absolute_date(self):
        """The site prints a date for older drops — without this they slip through."""
        yesterday = datetime.now(timezone.utc) - timedelta(days=1)
        self.assertLess(main.age_hours(yesterday.strftime("%d/%m/%Y")), 48)
        self.assertGreater(main.age_hours("06/06/2020"), 24 * 365 * 5)

    def test_unreadable(self):
        self.assertIsNone(main.age_hours(""))
        self.assertIsNone(main.age_hours("whenever"))


class TestCaption(unittest.TestCase):
    APP = {
        "id": "1", "title": "GLPzy: GLP-1 Shot Tracker",
        "description": "A private shot and dose tracker.",
        "platform": "iOS, iPadOS", "tag": "HealthAndFitness",
        "price": "$79.99 → Free", "ends": "Free since 6 h ago — can end any time",
        "image": "https://x/icon.png", "url": "https://apps.apple.com/us/app/id1",
    }

    def test_has_every_line(self):
        caption = main.build_caption(self.APP)
        self.assertIn("<b>GLPzy: GLP-1 Shot Tracker</b>", caption)
        self.assertIn("<b>Platform:</b> iOS, iPadOS", caption)
        self.assertIn("<b>Type:</b> #HealthAndFitness", caption)
        self.assertIn("<b>Price:</b> $79.99 → Free", caption)
        self.assertIn("<b>Ends:</b> Free since 6 h ago", caption)
        self.assertIn("<b>How to Claim</b>", caption)

    def test_long_description_is_trimmed_not_the_fields(self):
        app = dict(self.APP, description="x" * 4000)
        caption = main.build_caption(app)
        self.assertLessEqual(len(caption), main.MAX_CAPTION)
        self.assertIn("<b>Price:</b>", caption)
        self.assertIn("<b>How to Claim</b>", caption)

    def test_never_exceeds_telegram_limit(self):
        for size in (0, 500, 900, 5000):
            app = dict(self.APP, description="y" * size)
            self.assertLessEqual(len(main.build_caption(app)), main.MAX_CAPTION)


class TestTag(unittest.TestCase):
    def test_shapes(self):
        self.assertEqual(main.tag_from_genre("Health & Fitness"), "HealthAndFitness")
        self.assertEqual(main.tag_from_genre("Photo & Video"), "PhotoAndVideo")
        self.assertEqual(main.tag_from_genre(""), "App")


class TestCollect(unittest.TestCase):
    """The full pipeline with the network faked out."""

    LISTING = "".join(
        f'<article class="app-card">'
        f'<a href="https://appstore-discounts.com/en/app/{aid}">'
        f'<img src="https://img/{aid}.jpg">'
        f'<h3 class="app-card-title">App {aid}</h3>'
        f'<span class="genre">Utilities</span></article>'
        for aid in ("111", "222", "333")
    )
    ITUNES = {
        "111": {"trackName": "Good App", "primaryGenreName": "Productivity",
                "price": 0.0, "description": "Useful.",
                "artworkUrl512": "https://is1/good.jpg",
                "trackViewUrl": "https://apps.apple.com/us/app/good/id111"},
        "222": {"trackName": "Sneaky Game", "primaryGenreName": "Games",
                "price": 0.0, "trackViewUrl": "https://apps.apple.com/us/app/g/id222"},
        "333": {"trackName": "Back To Paid", "primaryGenreName": "Utilities",
                "price": 2.99, "trackViewUrl": "https://apps.apple.com/us/app/p/id333"},
    }

    def _fake_get(self, drop="6 h ago"):
        detail = (f'<p class="drop-relative">Drop detected {drop}</p>'
                  f'<span class="price-old">$9.99</span>')

        class Response:
            def __init__(self, text="", payload=None):
                self.text = text
                self._payload = payload
                self.ok = True

            def raise_for_status(self):
                pass

            def json(self):
                return self._payload

        def get(url, **kwargs):
            if "/en/free" in url:
                page = url.rsplit("p=", 1)[-1]
                return Response(self.LISTING if page == "1" else "")
            if "/en/app/" in url:
                return Response(detail)
            app_id = url.rsplit("id=", 1)[-1]
            found = self.ITUNES.get(app_id)
            return Response(payload={"results": [found] if found else []})

        return get

    def test_keeps_only_fresh_free_non_games(self):
        with patch.object(main, "PAGES", 1), \
             patch.object(main.requests, "get", self._fake_get()):
            apps = main.collect_ios(set())
        titles = [a["title"] for a in apps]
        self.assertEqual(titles, ["Good App"])

    def test_price_line_shows_the_drop(self):
        with patch.object(main, "PAGES", 1), \
             patch.object(main.requests, "get", self._fake_get()):
            app = main.collect_ios(set())[0]
        self.assertEqual(app["price"], "$9.99 → Free")
        self.assertIn("apps.apple.com", app["url"])
        self.assertTrue(app["image"].startswith("https://"))

    def test_stale_offers_are_skipped_and_remembered(self):
        """A trustworthy relative timestamp that is too old means skip."""
        seen = set()
        with patch.object(main, "PAGES", 1), \
             patch.object(main.requests, "get", self._fake_get(drop="40 d ago")):
            apps = main.collect_ios(seen)
        self.assertEqual(apps, [])
        self.assertIn("111", seen, "a stale app should not be re-checked every run")

    def test_seed_dates_do_not_reject_a_free_app(self):
        """The site stamps almost every older app with the same made-up date.

        Filtering on it threw away 43 genuinely free apps in the first live run.
        """
        with patch.object(main, "PAGES", 1), \
             patch.object(main.requests, "get", self._fake_get(drop="06/06/2026")):
            apps = main.collect_ios(set())
        self.assertEqual([a["title"] for a in apps], ["Good App"])

    def test_seed_date_is_never_shown_to_readers(self):
        with patch.object(main, "PAGES", 1), \
             patch.object(main.requests, "get", self._fake_get(drop="06/06/2026")):
            app = main.collect_ios(set())[0]
        self.assertNotIn("06/06/2026", app["ends"])
        self.assertIn("Limited time", app["ends"])

    def test_real_drop_time_is_shown(self):
        with patch.object(main, "PAGES", 1), \
             patch.object(main.requests, "get", self._fake_get(drop="6 h ago")):
            app = main.collect_ios(set())[0]
        self.assertIn("Free since 6 h ago", app["ends"])

    def test_seen_apps_are_not_fetched_again(self):
        calls = []
        base = self._fake_get()

        def counting_get(url, **kwargs):
            calls.append(url)
            return base(url, **kwargs)

        with patch.object(main, "PAGES", 1), \
             patch.object(main.requests, "get", counting_get):
            apps = main.collect_ios({"111", "222", "333"})
        self.assertEqual(apps, [])
        self.assertFalse([u for u in calls if "/en/app/" in u],
                         "detail pages must not be fetched for known apps")

    def test_post_cap_is_respected(self):
        with patch.object(main, "PAGES", 1), \
             patch.object(main, "MAX_POSTS_PER_RUN", 0), \
             patch.object(main.requests, "get", self._fake_get()):
            self.assertEqual(main.collect_ios(set()), [])

    def test_listing_failure_does_not_crash(self):
        def boom(url, **kwargs):
            raise RuntimeError("site down")

        with patch.object(main, "PAGES", 1), \
             patch.object(main.requests, "get", boom):
            self.assertEqual(main.collect_ios(set()), [])


class TestSeenFile(unittest.TestCase):
    def test_round_trip(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "seen.json")
            with patch.object(main, "SEEN_FILE", path):
                self.assertEqual(main.load_seen(), set())
                main.save_seen({"b", "a"})
                self.assertEqual(main.load_seen(), {"a", "b"})
                self.assertEqual(
                    open(path).read().strip()[0], "[", "must stay a JSON list"
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)


# ------------------------------------------------------------- Android side

REDDIT_XML = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>[Game] Samorost 3 ($4.99 -&gt; Free)</title>
    <content type="html">&lt;a href="https://play.google.com/store/apps/details?id=amanita.samorost3"&gt;link&lt;/a&gt;</content>
  </entry>
  <entry>
    <title>Notes Pro (was $3.99, now free)</title>
    <content type="html">&lt;a href="https://play.google.com/store/apps/details?id=com.notes.pro&amp;hl=en"&gt;link&lt;/a&gt;</content>
  </entry>
  <entry>
    <title>Still Paid Utility ($2.99)</title>
    <content type="html">&lt;a href="https://play.google.com/store/apps/details?id=com.still.paid"&gt;link&lt;/a&gt;</content>
  </entry>
  <entry>
    <title>Duplicate of Notes Pro</title>
    <content type="html">&lt;a href="https://play.google.com/store/apps/details?id=com.notes.pro"&gt;link&lt;/a&gt;</content>
  </entry>
  <entry>
    <title>A post with no store link at all</title>
    <content type="html">&lt;p&gt;nothing here&lt;/p&gt;</content>
  </entry>
</feed>"""

PLAY_RECORDS = {
    "amanita.samorost3": {"name": "Samorost 3", "applicationCategory": "GAME_ADVENTURE",
                          "image": "https://play-lh/samorost.png", "description": "Adventure.",
                          "offers": [{"price": "0"}]},
    "com.notes.pro": {"name": "Notes Pro", "applicationCategory": "PRODUCTIVITY",
                      "image": "https://play-lh/notes.png", "description": "Take notes.",
                      "offers": [{"price": "0"}]},
    "com.still.paid": {"name": "Still Paid Utility", "applicationCategory": "TOOLS",
                       "image": "https://play-lh/paid.png", "description": "Tool.",
                       "offers": [{"price": "2.99"}]},
}


def _play_page(pkg):
    payload = dict(PLAY_RECORDS[pkg])
    payload["@type"] = "SoftwareApplication"
    return ('<html><head><script type="application/ld+json">'
            + json.dumps(payload) + "</script></head></html>")


class TestRedditParsing(unittest.TestCase):
    def setUp(self):
        self.found = main.parse_reddit_packages(REDDIT_XML)
        self.pkgs = [f["pkg"] for f in self.found]

    def test_extracts_packages(self):
        self.assertIn("com.notes.pro", self.pkgs)
        self.assertIn("amanita.samorost3", self.pkgs)

    def test_strips_query_noise(self):
        """?id=com.notes.pro&hl=en must not become 'com.notes.pro&hl=en'."""
        self.assertIn("com.notes.pro", self.pkgs)
        self.assertFalse([p for p in self.pkgs if "&" in p or "hl=" in p])

    def test_deduplicates(self):
        self.assertEqual(len(self.pkgs), len(set(self.pkgs)))

    def test_ignores_posts_without_a_link(self):
        self.assertEqual(len(self.pkgs), 3)

    def test_keeps_the_post_title_for_the_price(self):
        notes = [f for f in self.found if f["pkg"] == "com.notes.pro"][0]
        self.assertIn("$3.99", notes["title"])

    def test_broken_feed_is_safe(self):
        self.assertEqual(main.parse_reddit_packages("<<<"), [])


class TestCollectAndroid(unittest.TestCase):
    def _fake_get(self, reddit_fails=False):
        def get(url, **kwargs):
            class Response:
                def __init__(self, text="", ok=True):
                    self.text = text
                    self.ok = ok

                def raise_for_status(self):
                    pass

            if "reddit.com" in url:
                if reddit_fails:
                    raise RuntimeError("429 Too Many Requests")
                return Response(REDDIT_XML)
            pkg = url.split("id=", 1)[1].split("&")[0]
            if pkg not in PLAY_RECORDS:
                raise RuntimeError("404")
            return Response(_play_page(pkg))

        return get

    def test_keeps_only_free_non_games(self):
        with patch.object(main.requests, "get", self._fake_get()):
            apps = main.collect_android(set())
        self.assertEqual([a["title"] for a in apps], ["Notes Pro"])

    def test_game_is_dropped_by_category(self):
        with patch.object(main.requests, "get", self._fake_get()):
            apps = main.collect_android(set())
        self.assertNotIn("Samorost 3", [a["title"] for a in apps])

    def test_android_fields(self):
        with patch.object(main.requests, "get", self._fake_get()):
            app = main.collect_android(set())[0]
        self.assertEqual(app["platform"], "Android")
        self.assertEqual(app["store"], "android")
        self.assertEqual(app["tag"], "Productivity")
        self.assertEqual(app["price"], "$3.99 → Free")
        self.assertIn("play.google.com", app["url"])
        self.assertTrue(app["image"].startswith("https://"))

    def test_android_gets_play_instructions(self):
        with patch.object(main.requests, "get", self._fake_get()):
            app = main.collect_android(set())[0]
        self.assertIn("Google Play page", main.build_caption(app))
        self.assertIn("Google account", main.build_caption(app))

    def test_seen_packages_are_skipped(self):
        with patch.object(main.requests, "get", self._fake_get()):
            apps = main.collect_android({"com.notes.pro", "amanita.samorost3",
                                         "com.still.paid"})
        self.assertEqual(apps, [])

    def test_reddit_outage_is_not_fatal(self):
        """Reddit 429s from GitHub's shared runner IPs — that must not break the run."""
        with patch.object(main.requests, "get", self._fake_get(reddit_fails=True)):
            self.assertEqual(main.collect_android(set()), [])

    def test_lookup_budget_is_respected(self):
        with patch.object(main, "MAX_LOOKUPS_PER_RUN", 1), \
             patch.object(main.requests, "get", self._fake_get()):
            apps = main.collect_android(set())
        self.assertEqual(apps, [], "first candidate is a game, budget stops there")


class TestTagFromBothStores(unittest.TestCase):
    def test_apple_and_google_agree(self):
        self.assertEqual(main.tag_from_genre("Health & Fitness"), "HealthAndFitness")
        self.assertEqual(main.tag_from_genre("HEALTH_AND_FITNESS"), "HealthAndFitness")
        self.assertEqual(main.tag_from_genre("HOUSE_AND_HOME"), "HouseAndHome")
        self.assertEqual(main.tag_from_genre("PRODUCTIVITY"), "Productivity")


class TestMustBeARealDrop(unittest.TestCase):
    """An app that was already free is not a deal.

    The first live Android run posted "Easy Phone Senior Dialer" with
    "Price: Free" — nothing proved it had ever cost anything. Google's page
    only reports today's price, so the Reddit title is the sole evidence.
    No stated original price means no post.
    """

    NO_PRICE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>Easy Phone Senior Dialer - great free app for seniors</title>
    <content type="html">&lt;a href="https://play.google.com/store/apps/details?id=com.always.free"&gt;link&lt;/a&gt;</content>
  </entry>
  <entry>
    <title>Notes Pro (was $3.99, now free)</title>
    <content type="html">&lt;a href="https://play.google.com/store/apps/details?id=com.notes.pro"&gt;link&lt;/a&gt;</content>
  </entry>
</feed>"""

    RECORDS = {
        "com.always.free": {"name": "Easy Phone Senior Dialer",
                            "applicationCategory": "COMMUNICATION",
                            "image": "https://play-lh/dialer.png",
                            "description": "Large buttons.", "offers": [{"price": "0"}]},
        "com.notes.pro": {"name": "Notes Pro", "applicationCategory": "PRODUCTIVITY",
                          "image": "https://play-lh/notes.png",
                          "description": "Take notes.", "offers": [{"price": "0"}]},
    }

    def _get(self, url, **kwargs):
        class Response:
            def __init__(self, text):
                self.text = text
                self.ok = True

            def raise_for_status(self):
                pass

        if "reddit.com" in url:
            return Response(self.NO_PRICE_XML)
        pkg = url.split("id=", 1)[1].split("&")[0]
        payload = dict(self.RECORDS[pkg])
        payload["@type"] = "SoftwareApplication"
        return Response('<script type="application/ld+json">'
                        + json.dumps(payload) + "</script>")

    def test_always_free_app_is_not_posted(self):
        with patch.object(main.requests, "get", self._get):
            apps = main.collect_android(set())
        self.assertEqual([a["title"] for a in apps], ["Notes Pro"])

    def test_it_is_remembered_so_it_is_not_rechecked(self):
        seen = set()
        with patch.object(main.requests, "get", self._get):
            main.collect_android(seen)
        self.assertIn("com.always.free", seen)

    def test_price_line_always_shows_a_drop(self):
        with patch.object(main.requests, "get", self._get):
            app = main.collect_android(set())[0]
        self.assertEqual(app["price"], "$3.99 → Free")
        self.assertNotEqual(app["price"], "Free")


class TestIosMustBeARealDrop(unittest.TestCase):
    """Same rule on the Apple side."""

    LISTING = (
        '<article class="app-card">'
        '<a href="https://appstore-discounts.com/en/app/777">'
        '<img src="https://img/777.jpg">'
        '<h3 class="app-card-title">Always Free App</h3>'
        '<span class="genre">Utilities</span></article>'
    )
    ITUNES = {"777": {"trackName": "Always Free App", "primaryGenreName": "Utilities",
                      "price": 0.0, "description": "Free forever.",
                      "artworkUrl512": "https://is1/x.jpg",
                      "trackViewUrl": "https://apps.apple.com/us/app/x/id777"}}

    def _get(self, url, **kwargs):
        class Response:
            def __init__(self, text="", payload=None):
                self.text = text
                self._payload = payload
                self.ok = True

            def raise_for_status(self):
                pass

            def json(self):
                return self._payload

        if "/en/free" in url:
            page = url.rsplit("p=", 1)[-1]
            return Response(self.LISTING if page == "1" else "")
        if "/en/app/" in url:
            # detail page with a drop time but no price-old element
            return Response('<p class="drop-relative">Drop detected 6 h ago</p>')
        app_id = url.rsplit("id=", 1)[-1]
        return Response(payload={"results": [self.ITUNES[app_id]]})

    def test_no_original_price_means_no_post(self):
        with patch.object(main, "PAGES", 1), \
             patch.object(main.requests, "get", self._get):
            self.assertEqual(main.collect_ios(set()), [])


class TestButtonStyle(unittest.TestCase):
    """Telegram validates the style field: only four values are accepted.

    Probed live against the API — 'red', 'warning', 'secondary', 'attention'
    and the rest are rejected with "Invalid button style specified".
    """

    def test_only_the_four_known_values(self):
        self.assertEqual(main.BUTTON_STYLES,
                         ("success", "danger", "primary", "default"))

    def test_apps_default_to_danger_not_the_games_green(self):
        self.assertEqual(main.BUTTON_STYLE, "danger")

    def test_style_is_attached_to_the_button(self):
        app = {"id": "1", "title": "T", "description": "d", "platform": "Android",
               "tag": "Tools", "price": "$1 → Free", "ends": "soon",
               "image": "https://x/i.png", "url": "https://x", "store": "android"}
        captured = {}

        class Response:
            ok = True
            status_code = 200
            text = ""

        def fake_post(url, data=None, **kwargs):
            captured.update(data or {})
            return Response()

        with patch.object(main, "DRY_RUN", False), \
             patch.object(main.requests, "post", fake_post):
            main.send_photo(app)

        button = json.loads(captured["reply_markup"])["inline_keyboard"][0][0]
        self.assertEqual(button["style"], "danger")
        self.assertEqual(button["url"], "https://x")
