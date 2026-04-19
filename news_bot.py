import asyncio
import feedparser
import requests
import pytz
import re
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

BOT_TOKEN = "8785893403:AAFxihc1urBoZQ_vizwlvA-ed1mZh23f8tk"
MY_CHAT_ID = "6794301814"

# Mapping RSS categories to Dnyuz Author pages
SOURCE_MAPPING = {
    "NYT World": "new-york-times",
    "NYT Business": "new-york-times",
    "NYT Tech": "new-york-times",
    "NYT India": "new-york-times",
    "NYT Lifestyle": "new-york-times",
    "The Atlantic": "the-atlantic",
    "Washington Post": "washington-post",
    "Politico": "politico",
    "Wired": "wired",
}

RSS_FEEDS = {
    "NYT World": "https://rss.nytimes.com/services/xml/rss/nyt/World.xml",
    "NYT Business": "https://rss.nytimes.com/services/xml/rss/nyt/Business.xml",
    "NYT Tech": "https://rss.nytimes.com/services/xml/rss/nyt/Technology.xml",
    "NYT India": "https://rss.nytimes.com/services/xml/rss/nyt/India.xml",
    "NYT Lifestyle": "https://rss.nytimes.com/services/xml/rss/nyt/PersonalTech.xml",
    "The Atlantic": "https://www.theatlantic.com/feed/all/",
    "Financial Times": "https://www.ft.com/news-feed?format=rss",
    "Washington Post": "https://feeds.washingtonpost.com/rss/world",
    "Politico": "https://www.politico.com/rss/politicopico.xml",
    "Wired": "https://www.wired.com/feed/rss",
}

processed_links = set()


def normalize_title(title):
    """Cleans up titles for better matching (lowercase, remove extra spaces)."""
    return re.sub(r"\s+", " ", title.lower()).strip()


async def find_on_dnyuz(target_title, category):
    """
    Uses Playwright to load dnyuz author pages and fuzzy-match article titles.
    Returns a dnyuz link if a high-confidence match is found, None otherwise.
    """
    if category not in SOURCE_MAPPING:
        return None

    author_slug = SOURCE_MAPPING[category]
    author_url = f"https://dnyuz.com/author/{author_slug}/"

    browser = None
    try:
        print(f"[DEBUG] Playwright: Loading {author_url} for: {target_title[:50]}...")

        # Import playwright here to avoid import errors if not installed
        from playwright.async_api import async_playwright

        async with async_playwright() as p:
            # Launch Chromium browser
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            page.set_default_timeout(10000)  # 10 second timeout

            # Navigate to author page
            await page.goto(author_url, wait_until="networkidle")
            print(f"[DEBUG] Page loaded, searching for articles...")

            # Wait for article titles to load
            await page.wait_for_selector("h3.entry-title a", timeout=8000)

            # Get all article links and titles from the page
            articles_data = await page.evaluate(
                """
                () => {
                    const articles = [];
                    document.querySelectorAll("h3.entry-title a").forEach(link => {
                        articles.push({
                            title: link.textContent.trim(),
                            href: link.href
                        });
                    });
                    return articles;
                }
                """
            )

            print(f"[DEBUG] Found {len(articles_data)} articles on page")

            normalized_target = normalize_title(target_title)
            best_match = None
            best_similarity = 0

            for i, article in enumerate(articles_data[:20]):
                found_title = article["title"]
                found_link = article["href"]

                if not found_link or not found_title:
                    continue

                similarity = fuzz.ratio(normalized_target, normalize_title(found_title))
                print(
                    f"[DEBUG] Article {i}: similarity={similarity}% for '{found_title[:50]}...'"
                )

                if similarity > best_similarity:
                    best_similarity = similarity
                    best_match = found_link

                if similarity > 85:
                    print(f"[DEBUG] ✅ MATCH FOUND! Similarity={similarity}%")
                    return found_link

            if best_match and best_similarity > 70:
                print(f"[DEBUG] ⚠️  PARTIAL MATCH ({best_similarity}%). Using it.")
                return best_match

            print(f"[DEBUG] No match found (best was {best_similarity}%)")

    except Exception as e:
        print(f"[DEBUG] Playwright error: {e}")
        import traceback

        traceback.print_exc()

    finally:
        if browser:
            try:
                await browser.close()
            except:
                pass

    return None


def to_archive_link(url):
    return f"https://archive.is/newest/{url}"


async def fetch_news():
    news = []
    for category, feed_url in RSS_FEEDS.items():
        print(f"Fetching {category}...")
        feed = feedparser.parse(feed_url)
        # Get top 2 from each feed
        for entry in feed.entries[:2]:
            if entry.link not in processed_links:
                # Logic: Use Source-Aware Peeking for Homelander links
                dnyuz_link = await find_on_dnyuz(entry.title, category)

                if dnyuz_link:
                    final_link = dnyuz_link
                    link_label = "✅ (Homelander)"
                else:
                    final_link = to_archive_link(entry.link)
                    if category in SOURCE_MAPPING:
                        link_label = "🔗 (Archive Fallback)"
                    else:
                        link_label = "🔗 (Archive)"

                news.append(
                    {
                        "title": entry.title,
                        "link": final_link,
                        "category": category,
                        "original_link": entry.link,
                        "link_type": link_label,
                    }
                )
                processed_links.add(entry.link)
    return news


async def search_news(query):
    """Looks for a specific word in all news categories."""
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


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "I'm your Phase 2.5 News Bot! Now with Source-Aware matching for NYT, Atlantic, WashPo, Politico, and Wired.\n\n"
        "I peek at specific publisher pages for cleaner 'Homelander' links first."
    )


async def ping_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("I'm awake and vibe coding, Sankey!")


async def news_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Fetching latest news from all sources (this may take a moment)..."
    )
    news_items = await fetch_news()
    if not news_items:
        await update.message.reply_text("No new news found in any of the feeds!")
        return

    message = "📰 *Today's Smart News Digest*\n\n"
    for item in news_items:
        message += f"*{item['category']}:* {item['title']}\n{item['link_type']} {item['link']}\n\n"

    if len(message) > 4000:
        for i in range(0, len(message), 4000):
            await update.message.reply_text(
                message[i : i + 4000], parse_mode="Markdown"
            )
    else:
        await update.message.reply_text(message, parse_mode="Markdown")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    text_lower = text.lower()

    if (
        "whats the news for today" in text_lower
        or "what's the news for today" in text_lower
        or text_lower == "news"
    ):
        await news_command(update, context)
        return

    if any(
        domain in text_lower
        for domain in [
            "nytimes.com",
            "theatlantic.com",
            "ft.com",
            "bloomberg.com",
            "washingtonpost.com",
            "politico.com",
            "wired.com",
        ]
    ):
        links = re.findall(r"(https?://[^\s]+)", text)
        for link in links:
            await update.message.reply_text("Generating archive link...")
            archive_link = to_archive_link(link)
            await update.message.reply_text(
                f"🔗 Here is your archive link:\n{archive_link}"
            )
        return

    await update.message.reply_text(f"Searching for news about '{text}'...")
    search_results = await search_news(text)

    if not search_results:
        await update.message.reply_text(
            f"Sorry, I couldn't find any recent articles about '{text}'."
        )
    else:
        message = f"🔍 *Search Results for '{text}'*\n\n"
        for item in search_results[:5]:
            message += f"*{item['category']}:* {item['title']}\n🔗 {item['link']}\n\n"
        await update.message.reply_text(message, parse_mode="Markdown")


async def daily_digest(context: ContextTypes.DEFAULT_TYPE):
    news_items = await fetch_news()
    if not news_items:
        return

    message = "📰 *Your Phase 2.5 Daily Digest*\n\n"
    for item in news_items:
        message += f"*{item['category']}:* {item['title']}\n{item['link_type']} {item['link']}\n\n"

    if len(message) > 4000:
        for i in range(0, len(message), 4000):
            await context.bot.send_message(
                chat_id=MY_CHAT_ID, text=message[i : i + 4000], parse_mode="Markdown"
            )
    else:
        await context.bot.send_message(
            chat_id=MY_CHAT_ID, text=message, parse_mode="Markdown"
        )


def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("ping", ping_command))
    app.add_handler(CommandHandler("news", news_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    job_queue = app.job_queue
    cet_tz = pytz.timezone("CET")
    job_queue.run_daily(daily_digest, time=time(21, 0, 0, tzinfo=cet_tz))

    print("Bot is running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
