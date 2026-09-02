# 📱 Lootigo Apps Bot

A fully automated Telegram bot that tracks and posts **paid iOS apps that just went free**.

Same shape as [lootigo-bot](https://github.com/P3R6/lootigo-bot) — GitHub Actions on a
cron, `seen_ids.json` for dedupe, one photo post per app with a **✅ Claim Now** button.
No server, no VPN, nothing to keep running.

---

## Where the data comes from

GamerPower hands out ready-made JSON. The App Store has no equivalent, so this bot
builds one from two public endpoints:

| Step | Source | What it gives |
|---|---|---|
| 1. What is free right now | `appstore-discounts.com/en/free` | app id, title, genre, icon |
| 2. What it used to cost | `appstore-discounts.com/en/app/<id>` | original price, drop time |
| 3. Is that actually true | `itunes.apple.com/lookup?id=<id>` | real genre, **current price**, official icon + link |

Step 3 is the important one. It is Apple's own record, so it catches three things the
listing alone cannot:

- **Games** — dropped, because you already post those in the games bot. Genre comes from
  Apple, not from a keyword guess.
- **Offers that already ended** — if Apple still reports a price, the app is not free any
  more and never gets posted.
- **A clean link and a 512px icon** — straight from the store.

---

## Setup

**1.** Push this folder to a new GitHub repo.

**2.** Add two repository secrets — *Settings → Secrets and variables → Actions*:

| Secret | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | your bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | the channel, e.g. `-1002111575073` or `@LootigoApp` |

**3.** Make the bot an admin of the channel, with permission to post.

**4.** *Actions → Free Apps Bot → Run workflow* to fire it once by hand.

After that it runs itself every 6 hours.

---

## Tuning

All optional, set as `env:` in the workflow:

| Variable | Default | What it does |
|---|---|---|
| `PAGES` | `2` | Listing pages to read (25 apps each) |
| `MAX_POSTS_PER_RUN` | `8` | Ceiling per run, so the channel is never flooded |
| `POST_DELAY` | `30` | Seconds between posts |
| `MAX_AGE_HOURS` | `48` | Ignore drops older than this — **see the note below** |

### The trap in `MAX_AGE_HOURS`

The site shows `Drop detected 6 h ago` for genuinely fresh drops, but for older ones it
prints a date — and in a live check, **42 of 43 apps carried the exact same date
(06/06/2026)**. That is when their database was seeded, not when those apps went free.

So the bot trusts only relative timestamps:

- `6 h ago` → real. Filtered against `MAX_AGE_HOURS`, and shown in the post as
  *"Free since 6 h ago"*.
- `06/06/2026` → meaningless. Never used to reject an app, and never shown to readers —
  the post just says *"Limited time — grab it now"*.

Apps with an unreliable date are still verified free through Apple before posting, and
the per-run cap drains that backlog a few at a time instead of dumping 43 posts at once.

---

## Post format

```
🖼 [app icon]

📱 GLPzy: GLP-1 Shot Tracker

📝 A private GLP-1 shot and dose tracker for Zepbound, Wegovy, Mounjaro…

💻 Platform: iOS, iPadOS
📂 Type: #HealthAndFitness
💸 Price: $79.99 → Free
⏳ Ends: Free since 6 h ago — can end any time

📌 How to Claim
1. Tap the button to open the App Store page.
2. Get it while the price is $0.
3. It stays in your Apple ID forever, even after the price goes back up.

        [ ✅ Claim Now ]
```

Telegram caps photo captions at 1024 characters. When a description is too long, **only
the description is trimmed** — the fields and the claim steps always survive.

The `#Type` hashtag is the app's real App Store genre (`#HealthAndFitness`,
`#Productivity`), not a fixed label.

---

## Tests

```bash
python -m unittest test_main
```

26 tests, no token and no network needed. The listing and detail parsers run against
saved copies of the real pages in `fixtures/`, so if the site changes its HTML the tests
fail before the channel does.

To refresh those copies:

```bash
curl -s "https://appstore-discounts.com/en/free?p=1" -o fixtures/free_page.html
```

---

## Files

| File | Role |
|---|---|
| `main.py` | the whole bot |
| `.github/workflows/bot.yml` | cron, secrets, and the commit of `seen_ids.json` |
| `seen_ids.json` | app ids already posted — committed back by the workflow |
| `test_main.py` | offline tests |
| `fixtures/` | saved real pages the parser tests run against |
