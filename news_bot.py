import asyncio
import os
import re
import html
import feedparser
import httpx
import pytz
from datetime import time
from bs4 import BeautifulSoup
from thefuzz import fuzz
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

# ─── CONFIGURATION ────────────────────────────────────────────────────────────
# These are read from Railway environment variables at runtime.
# BOT_TOKEN  → your Telegram bot token from @BotFather
# MY_CHAT_ID → your personal Telegram chat ID from @userinfobot
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
MY_CHAT_ID = os.environ.get("MY_CHAT_ID", "")

# Maps each news category to its author slug on dnyuz.com (the "Homelander" mirror site).
# When dnyuz has a matching article, we use that free link instead of archive.is.
SOURCE_MAPPING = {
    "NYT World": "the-new-york-times",
    "NYT Business": "the-new-york-times",
    "NYT Technology": "the-new-york-times",
    "The Atlantic": "the-atlantic",
    "Washington Post": "the-washington-post",
    "Politico": "politico",
    "Wired": "wired",
}

# RSS feed URLs for each news category we want to pull from.
RSS_FEEDS = {
    "NYT World": "https://rss.nytimes.com/services/xml/rss/nyt/World.xml",
    "NYT Business": "https://rss.nytimes.com/services/xml/rss/nyt/Business.xml",
    "NYT Technology": "https://rss.nytimes.com/services/xml/rss/nyt/Technology.xml",
    "The Atlantic": "https://feeds.feedburner.com/TheAtlantic",
    "Washington Post": "https://feeds.washingtonpost.com/rss/world",
    "Politico": "https://rss.politico.com/politics-news.xml",
    "Wired": "https://www.wired.com/feed/rss",
}

# Keeps track of article links we've already sent, so we don't repeat them.
# This is an in-memory set — it resets every time the bot restarts.
processed_links = set()

# ─── HELPERS ──────────────────────────────────────────────────────────────────


def normalize_title(title):
    """Lowercases a title and collapses extra whitespace for cleaner comparison."""
    return re.sub(r"\s+", " ", title.lower()).strip()


def to_archive_link(url):
    """Wraps any URL in an archive.is link so paywalled articles can be read for free."""
    return f"https://archive.is/newest/{url}"


def escape_markdown(text):
    """Escapes characters that break Telegram's Markdown formatting (like * and _)."""
    return text.replace("_", "\\_").replace("*", "\\*")


# ─── DNYUZ / HOMELANDER LOOKUP ────────────────────────────────────────────────


async def find_on_dnyuz(target_title, category=None):
    """
    Tries to find a free version of an article on dnyuz.com by searching their site directly.

    How it works (NEW approach — uses dnyuz search instead of author pages):
    - dnyuz.com has a site-wide search at https://dnyuz.com/?s=<keywords>
    - We send the article title as the search query.
    - dnyuz returns a page of matching articles from ALL authors/publishers.
    - We fuzzy-match the results to pick the best hit.
    - This works for NYT, Atlantic, WashPost, Politico, Wired — any source dnyuz mirrors.

    Returns the dnyuz link if a good match is found, or None if not.
    """
    browserless_token = os.environ.get("BROWSERLESS_TOKEN")
    if not browserless_token:
        print("[DEBUG] BROWSERLESS_TOKEN not set, skipping Homelander lookup")
        return None

    # Build dnyuz search URL. urllib.parse.quote_plus handles spaces and special chars.
    from urllib.parse import quote_plus

    # Keep first 5 words of title as search query — shorter queries match better on dnyuz.
    search_query = " ".join(target_title.split()[:5])
    search_url = f"https://dnyuz.com/?s={quote_plus(search_query)}"

    try:
        print(f"[DEBUG] Browserless: Searching dnyuz for: {target_title[:60]}...")

        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"https://production-sfo.browserless.io/content?token={browserless_token}",
                json={"url": search_url},
                timeout=30.0,
            )

        if response.status_code != 200:
            print(f"[DEBUG] Browserless returned error status: {response.status_code}")
            return None

        soup = BeautifulSoup(response.text, "html.parser")
        # Search results on dnyuz also use h3.wps_post_title, but we also check h2/h3 fallbacks.
        articles = soup.find_all("h3", class_="wps_post_title")
        if not articles:
            # Fallback: some search result layouts use different tags
            articles = soup.select("h2 a, h3 a, article a")

        print(
            f"[DEBUG] Found {len(articles)} search results on dnyuz for '{search_query}'"
        )

        normalized_target = normalize_title(target_title)
        best_match = None
        best_similarity = 0

        for i, article in enumerate(articles[:15]):
            # Handle both h3.wps_post_title (with <a> inside) and direct <a> tags
            link_tag = article.find("a") if article.name in ("h2", "h3") else article
            if not link_tag or not hasattr(link_tag, "get"):
                continue

            found_title = link_tag.get_text().strip()
            found_link = link_tag.get("href")

            if not found_link or not found_title or "dnyuz.com" not in found_link:
                continue

            similarity = fuzz.token_sort_ratio(
                normalized_target, normalize_title(found_title)
            )
            print(
                f"[DEBUG] Result {i}: {similarity}% match (token_sort) for '{found_title[:60]}'"
            )

            if similarity > best_similarity:
                best_similarity = similarity
                best_match = found_link

            # 80%+ is a confident match via search — return immediately.
            if similarity > 80:
                print(f"[DEBUG] ✅ Strong match found! Similarity={similarity}%")
                return found_link

        # 70–85% is a reasonable match — use it but flag it as partial.
        if best_match and best_similarity > 70:
            print(f"[DEBUG] Partial match ({best_similarity}%) — using it.")
            return best_match

        print(f"[DEBUG] No good match found (best was {best_similarity}%)")

    except Exception as e:
        print(f"[DEBUG] Browserless error for '{category}': {e}")

    return None


# ─── NEWS FETCHING ────────────────────────────────────────────────────────────


async def process_entry(entry, category):
    """
    Processes a single RSS entry:
    1. Tries to find a free dnyuz link first (Homelander).
    2. Falls back to an archive.is link if dnyuz doesn't have it.
    Returns a dict with the article info.
    """
    dnyuz_link = await find_on_dnyuz(entry.title, category)

    if dnyuz_link:
        final_link = dnyuz_link
        link_label = "✅ (Homelander)"
    else:
        final_link = to_archive_link(entry.link)
        link_label = (
            "🔗 (Archive Fallback)" if category in SOURCE_MAPPING else "🔗 (Archive)"
        )

    return {
        "title": entry.title,
        "link": final_link,
        "category": category,
        "original_link": entry.link,
        "link_type": link_label,
    }


async def fetch_news():
    """
    Pulls the top 2 articles from each RSS feed and processes them in parallel.
    Parallel = all feeds are fetched at the same time, not one by one.
    This is much faster and avoids Railway's timeout / conflict issues.
    """
    tasks = []

    for category, feed_url in RSS_FEEDS.items():
        print(f"[INFO] Fetching RSS: {category}...")
        feed = feedparser.parse(feed_url)
        for entry in feed.entries[:2]:
            if entry.link not in processed_links:
                tasks.append(process_entry(entry, category))

    news = []
    if tasks:
        # asyncio.gather runs all tasks at the same time and waits for all to finish.
        results = await asyncio.gather(*tasks)
        for res in results:
            if res:
                news.append(res)
                processed_links.add(res["original_link"])

    return news


async def search_news(query):
    """
    Searches all RSS feeds for articles whose titles contain the given keyword.

    Two-step approach for speed:
    1. First, scan all feeds quickly to find matching titles (no network calls).
    2. Then, look up free dnyuz links for the top 5 matches in parallel.

    This avoids timing out Telegram when the search has many matches.
    """
    query = query.lower()
    matches = []

    # Step 1: Collect all matching articles across all feeds (fast — text only).
    for category, feed_url in RSS_FEEDS.items():
        feed = feedparser.parse(feed_url)
        for entry in feed.entries:
            if query in entry.title.lower():
                matches.append({"entry": entry, "category": category})

    # Cap at top 5 — handle_message only displays 5 anyway, so no point looking up more.
    matches = matches[:5]

    if not matches:
        return []

    # Step 2: Run all dnyuz lookups in parallel with a tiny staggered delay to avoid 429 errors.
    async def process_match(match_dict, index):
        # Stagger the start of each task by 1 second to avoid overwhelming Browserless
        await asyncio.sleep(index * 1.0)
        entry = match_dict["entry"]
        category = match_dict["category"]
        dnyuz_link = await find_on_dnyuz(entry.title, category)
        final_link = dnyuz_link if dnyuz_link else to_archive_link(entry.link)
        return {
            "title": entry.title,
            "link": final_link,
            "category": category,
        }

    results = await asyncio.gather(
        *[process_match(m, i) for i, m in enumerate(matches)]
    )
    print(f"[INFO] Search complete for query: {query}")
    return results


# ─── TELEGRAM COMMAND HANDLERS ────────────────────────────────────────────────


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Your News Bot is running.\n\n"
        "I check NYT, The Atlantic, WashPost, Politico, and Wired.\n"
        "Say 'news' or 'whats the news for today' to get the latest headlines.\n"
        "You can also send me any paywall URL and I'll generate an archive link."
    )


async def ping_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("I'm awake and running, Sanket!")


async def news_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Fetching latest news from all sources...")
    news_items = await fetch_news()

    if not news_items:
        await update.message.reply_text("No new articles found across all feeds.")
        return

    message = "📰 *Today's News Digest*\n\n"
    for item in news_items:
        safe_title = escape_markdown(item["title"])
        message += f"*{item['category']}:* {safe_title}\n{item['link_type']} {item['link']}\n\n"

    # Telegram has a 4096-character message limit. Split if needed.
    if len(message) > 4000:
        for i in range(0, len(message), 4000):
            await update.message.reply_text(
                message[i : i + 4000], parse_mode="Markdown"
            )
    else:
        await update.message.reply_text(message, parse_mode="Markdown")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles any plain text message (not a /command)."""
    text = update.message.text
    text_lower = text.lower()

    # Trigger the news digest if the user says something like "news" or "whats the news"
    if (
        "whats the news for today" in text_lower
        or "what's the news for today" in text_lower
        or text_lower.strip() == "news"
    ):
        await news_command(update, context)
        return

    # If the user pastes a paywall URL, generate an archive link for it
    paywall_domains = [
        "nytimes.com",
        "theatlantic.com",
        "ft.com",
        "bloomberg.com",
        "washingtonpost.com",
        "politico.com",
        "wired.com",
    ]
    if any(domain in text_lower for domain in paywall_domains):
        links = re.findall(r"(https?://[^\s]+)", text)
        for link in links:
            archive_link = to_archive_link(link)
            await update.message.reply_text(f"🔗 Archive link:\n{archive_link}")
        return

    # Otherwise treat the message as a search query

    # Step 1: Send a status message immediately so Telegram knows we're alive.
    # We'll edit this message every 10 seconds to show elapsed time.
    # This prevents Telegram from timing out and re-triggering the handler.
    status_msg = await update.message.reply_text(
        f"Searching for news about '{text}'... (0s)"
    )

    # Step 2: Start a background task that updates the status message every 10 seconds.
    # Think of it like a "still cooking..." notification while the kitchen is busy.
    search_done = asyncio.Event()  # A flag — we set it to True when search finishes.

    async def update_status():
        elapsed = 0
        while not search_done.is_set():
            await asyncio.sleep(10)
            elapsed += 10
            if not search_done.is_set():
                try:
                    await status_msg.edit_text(
                        f"Searching for news about '{text}'... ({elapsed}s)"
                    )
                except Exception:
                    pass  # If editing fails (e.g. message deleted), just ignore it.

    # Fire off the status updater in the background — it runs independently.
    status_task = asyncio.create_task(update_status())

    # Step 3: Run the full search (including ALL browserless lookups) and wait for it to finish.
    search_results = await search_news(text)

    # Step 4: Signal that the search is done — this stops the status updater.
    search_done.set()
    status_task.cancel()

    # Step 5: Edit the status message to show we're done, then send final results.
    await status_msg.edit_text(f"Search complete for '{text}'. Here are the results:")

    if not search_results:
        await update.message.reply_text(f"No recent articles found about '{text}'.")
    else:
        message = f"🔍 *Search: '{escape_markdown(text)}'*\n\n"
        for item in search_results[:5]:
            safe_title = escape_markdown(item["title"])
            link_label = "✅" if "dnyuz.com" in item["link"] else "🔗"
            message += (
                f"*{item['category']}:* {safe_title}\n{link_label} {item['link']}\n\n"
            )
        await update.message.reply_text(message, parse_mode="Markdown")


async def daily_digest(context: ContextTypes.DEFAULT_TYPE):
    """Runs automatically at 9 PM CET every day to send the digest to your chat."""
    news_items = await fetch_news()
    if not news_items:
        return

    message = "📰 *Your Daily News Digest*\n\n"
    for item in news_items:
        safe_title = escape_markdown(item["title"])
        message += f"*{item['category']}:* {safe_title}\n{item['link_type']} {item['link']}\n\n"

    if len(message) > 4000:
        for i in range(0, len(message), 4000):
            await context.bot.send_message(
                chat_id=MY_CHAT_ID, text=message[i : i + 4000], parse_mode="Markdown"
            )
    else:
        await context.bot.send_message(
            chat_id=MY_CHAT_ID, text=message, parse_mode="Markdown"
        )


# ─── MAIN ─────────────────────────────────────────────────────────────────────


def main():
    # Build the application with increased timeouts (60s) to handle slow Browserless searches
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .read_timeout(60)
        .write_timeout(60)
        .build()
    )

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("ping", ping_command))
    app.add_handler(CommandHandler("news", news_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Schedule the daily digest at 9:00 PM Central European Time
    cet_tz = pytz.timezone("CET")
    app.job_queue.run_daily(daily_digest, time=time(21, 0, 0, tzinfo=cet_tz))

    print("Bot is running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
