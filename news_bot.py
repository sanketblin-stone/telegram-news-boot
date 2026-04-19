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


async def find_on_dnyuz(target_title, category):
    """
    Tries to find a free version of an article on dnyuz.com.

    How it works:
    - dnyuz.com mirrors articles from major publishers under author slugs.
    - We use Browserless.io (a cloud browser service) to load the author's page,
      because dnyuz uses JavaScript to render content — a regular HTTP request
      won't see the articles.
    - We then compare article titles using fuzzy matching (like spell-check similarity)
      to find the best match.

    Returns the dnyuz link if a good match is found, or None if not.
    """
    if category not in SOURCE_MAPPING:
        return None

    browserless_token = os.environ.get("BROWSERLESS_TOKEN")
    if not browserless_token:
        print("[DEBUG] BROWSERLESS_TOKEN not set, skipping Homelander lookup")
        return None

    author_slug = SOURCE_MAPPING[category]
    author_url = f"https://dnyuz.com/author/{author_slug}/"

    try:
        print(f"[DEBUG] Browserless: Loading {author_url} for: {target_title[:50]}...")

        # httpx is an async HTTP library — it lets us make web requests without
        # blocking the rest of the bot while waiting for a response.
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"https://production-sfo.browserless.io/content?token={browserless_token}",
                json={"url": author_url},
                timeout=30.0,
            )

        if response.status_code != 200:
            print(f"[DEBUG] Browserless returned error status: {response.status_code}")
            return None

        # BeautifulSoup parses the HTML so we can search it like a document.
        soup = BeautifulSoup(response.text, "html.parser")
        # dnyuz currently wraps article titles in <h3 class="wps_post_title">
        articles = soup.find_all("h3", class_="wps_post_title")
        print(
            f"[DEBUG] Found {len(articles)} articles on dnyuz for category: {category}"
        )

        normalized_target = normalize_title(target_title)
        best_match = None
        best_similarity = 0

        for i, article in enumerate(articles[:20]):
            link_tag = article.find("a")
            if not link_tag:
                continue

            found_title = link_tag.get_text().strip()
            found_link = link_tag.get("href")

            if not found_link or not found_title:
                continue

            # fuzz.ratio gives a 0–100 score for how similar two strings are.
            similarity = fuzz.ratio(normalized_target, normalize_title(found_title))
            print(f"[DEBUG] Article {i}: {similarity}% match for '{found_title[:50]}'")

            if similarity > best_similarity:
                best_similarity = similarity
                best_match = found_link

            # 85%+ is a very confident match — return immediately.
            if similarity > 85:
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
    """Searches all RSS feeds for articles whose titles contain the given keyword."""
    query = query.lower()
    results = []

    for category, feed_url in RSS_FEEDS.items():
        feed = feedparser.parse(feed_url)
        for entry in feed.entries:
            if query in entry.title.lower():
                dnyuz_link = await find_on_dnyuz(entry.title, category)
                final_link = dnyuz_link if dnyuz_link else to_archive_link(entry.link)
                results.append(
                    {
                        "title": entry.title,
                        "link": final_link,
                        "category": category,
                    }
                )

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
    await update.message.reply_text(f"Searching for news about '{text}'...")
    search_results = await search_news(text)

    if not search_results:
        await update.message.reply_text(f"No recent articles found about '{text}'.")
    else:
        message = f"🔍 *Search: '{escape_markdown(text)}'*\n\n"
        for item in search_results[:5]:
            safe_title = escape_markdown(item["title"])
            message += f"*{item['category']}:* {safe_title}\n🔗 {item['link']}\n\n"
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
    app = Application.builder().token(BOT_TOKEN).build()

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
