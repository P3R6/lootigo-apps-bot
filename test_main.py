"""Offline tests — no token, no network.

Run:  python -m unittest -v test_main
"""
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
            apps = main.collect(set())
        titles = [a["title"] for a in apps]
        self.assertEqual(titles, ["Good App"])

    def test_price_line_shows_the_drop(self):
        with patch.object(main, "PAGES", 1), \
             patch.object(main.requests, "get", self._fake_get()):
            app = main.collect(set())[0]
        self.assertEqual(app["price"], "$9.99 → Free")
        self.assertIn("apps.apple.com", app["url"])
        self.assertTrue(app["image"].startswith("https://"))

    def test_stale_offers_are_skipped_and_remembered(self):
        """A trustworthy relative timestamp that is too old means skip."""
        seen = set()
        with patch.object(main, "PAGES", 1), \
             patch.object(main.requests, "get", self._fake_get(drop="40 d ago")):
            apps = main.collect(seen)
        self.assertEqual(apps, [])
        self.assertIn("111", seen, "a stale app should not be re-checked every run")

    def test_seed_dates_do_not_reject_a_free_app(self):
        """The site stamps almost every older app with the same made-up date.

        Filtering on it threw away 43 genuinely free apps in the first live run.
        """
        with patch.object(main, "PAGES", 1), \
             patch.object(main.requests, "get", self._fake_get(drop="06/06/2026")):
            apps = main.collect(set())
        self.assertEqual([a["title"] for a in apps], ["Good App"])

    def test_seed_date_is_never_shown_to_readers(self):
        with patch.object(main, "PAGES", 1), \
             patch.object(main.requests, "get", self._fake_get(drop="06/06/2026")):
            app = main.collect(set())[0]
        self.assertNotIn("06/06/2026", app["ends"])
        self.assertIn("Limited time", app["ends"])

    def test_real_drop_time_is_shown(self):
        with patch.object(main, "PAGES", 1), \
             patch.object(main.requests, "get", self._fake_get(drop="6 h ago")):
            app = main.collect(set())[0]
        self.assertIn("Free since 6 h ago", app["ends"])

    def test_seen_apps_are_not_fetched_again(self):
        calls = []
        base = self._fake_get()

        def counting_get(url, **kwargs):
            calls.append(url)
            return base(url, **kwargs)

        with patch.object(main, "PAGES", 1), \
             patch.object(main.requests, "get", counting_get):
            apps = main.collect({"111", "222", "333"})
        self.assertEqual(apps, [])
        self.assertFalse([u for u in calls if "/en/app/" in u],
                         "detail pages must not be fetched for known apps")

    def test_post_cap_is_respected(self):
        with patch.object(main, "PAGES", 1), \
             patch.object(main, "MAX_POSTS_PER_RUN", 0), \
             patch.object(main.requests, "get", self._fake_get()):
            self.assertEqual(main.collect(set()), [])

    def test_listing_failure_does_not_crash(self):
        def boom(url, **kwargs):
            raise RuntimeError("site down")

        with patch.object(main, "PAGES", 1), \
             patch.object(main.requests, "get", boom):
            self.assertEqual(main.collect(set()), [])


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
