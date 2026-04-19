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
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options

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


def find_on_dnyuz(target_title, category):
    """
    Uses Selenium to load Dnyuz author page and find matching articles.
    Handles JavaScript-rendered content properly.
    """
    author_slug = SOURCE_MAPPING.get(category)
    if not author_slug:
        print(f"[DEBUG] No dnyuz mapping for {category}")
        return None

    driver = None
    try:
        author_url = f"https://dnyuz.com/author/{author_slug}/"
        print(f"[DEBUG] Selenium: Loading {author_url} for: {target_title[:50]}...")

        # Set up Chrome options for headless mode (no GUI)
        chrome_options = Options()
        chrome_options.add_argument("--headless")
        chrome_options.add_argument("--no-sandbox")
        chrome_options.add_argument("--disable-dev-shm-usage")
        chrome_options.add_argument("--disable-gpu")
        chrome_options.add_argument("user-agent=Mozilla/5.0")

        driver = webdriver.Chrome(options=chrome_options)
        driver.set_page_load_timeout(10)
        driver.get(author_url)

        # Wait for articles to load (h3 with entry-title class)
        WebDriverWait(driver, 8).until(
            EC.presence_of_all_elements_located((By.CSS_SELECTOR, "h3.entry-title a"))
        )
        print(f"[DEBUG] Page loaded, searching for articles...")

        # Parse the rendered HTML with BeautifulSoup
        soup = BeautifulSoup(driver.page_source, "html.parser")
        articles = soup.find_all("h3", class_="entry-title")
        print(f"[DEBUG] Found {len(articles)} articles on page")

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
        print(f"[DEBUG] Selenium error: {e}")
        import traceback

        traceback.print_exc()

    finally:
        if driver:
            driver.quit()

    return None

    try:
        author_url = f"https://dnyuz.com/author/{author_slug}/"
        print(f"[DEBUG] Checking {author_url} for: {target_title[:50]}...")
        headers = {"User-Agent": "Mozilla/5.0"}
        response = requests.get(author_url, headers=headers, timeout=10)

        if response.status_code != 200:
            print(f"[DEBUG] Bad status code: {response.status_code}")
            return None

        soup = BeautifulSoup(response.text, "html.parser")

        # Try multiple selectors to find articles
        articles = []
        # Try h3 with entry-title class
        articles = soup.find_all("h3", class_="entry-title")
        print(f"[DEBUG] Found {len(articles)} articles with h3.entry-title")

        # If no h3, try h2
        if not articles:
            articles = soup.find_all("h2", class_="entry-title")
            print(
                f"[DEBUG] Fallback: Found {len(articles)} articles with h2.entry-title"
            )

        # If still nothing, try any article container with title
        if not articles:
            articles = soup.find_all(class_="post-title")
            print(
                f"[DEBUG] Fallback 2: Found {len(articles)} articles with class post-title"
            )

        normalized_target = normalize_title(target_title)
        best_match = None
        best_similarity = 0

        for i, article in enumerate(articles[:20]):  # Check first 20 articles
            link_tag = article.find("a")
            if not link_tag:
                # Try to get the link from the parent
                parent = article.find_parent()
                if parent:
                    link_tag = parent.find("a")
                if not link_tag:
                    continue

            found_title = link_tag.get_text().strip()
            found_link = link_tag.get("href")

            if not found_link or not found_title:
                continue

            # Fuzzy match check
            similarity = fuzz.ratio(normalized_target, normalize_title(found_title))
            print(
                f"[DEBUG] Article {i}: similarity={similarity}% for '{found_title[:50]}...'"
            )

            if similarity > best_similarity:
                best_similarity = similarity
                best_match = found_link

            if similarity > 85:
                print(f"[DEBUG] ✅ MATCH FOUND! Returning: {found_link}")
                return found_link

        if best_match and best_similarity > 70:
            print(
                f"[DEBUG] ⚠️  PARTIAL MATCH ({best_similarity}%). Returning best match: {best_match}"
            )
            return best_match

        print(f"[DEBUG] No match found (best was {best_similarity}%)")

    except Exception as e:
        print(f"[DEBUG] Error peeking at dnyuz author page: {e}")
        import traceback

        traceback.print_exc()

    return None

    try:
        author_url = f"https://dnyuz.com/author/{author_slug}/"
        print(f"[DEBUG] Checking {author_url} for: {target_title[:50]}...")
        headers = {"User-Agent": "Mozilla/5.0"}
        response = requests.get(author_url, headers=headers, timeout=10)

        if response.status_code != 200:
            print(f"[DEBUG] Bad status code: {response.status_code}")
            return None

        soup = BeautifulSoup(response.text, "html.parser")
        # Author pages list articles in h3 tags with class entry-title
        articles = soup.find_all("h3", class_="entry-title")
        print(f"[DEBUG] Found {len(articles)} articles on dnyuz author page")

        normalized_target = normalize_title(target_title)

        for i, article in enumerate(articles):
            link_tag = article.find("a")
            if not link_tag:
                continue

            found_title = link_tag.get_text()
            found_link = link_tag.get("href")

            # Fuzzy match check
            similarity = fuzz.ratio(normalized_target, normalize_title(found_title))
            print(
                f"[DEBUG] Article {i}: similarity={similarity}% for '{found_title[:50]}...'"
            )
            if similarity > 85:
                print(f"[DEBUG] ✅ MATCH FOUND! Returning: {found_link}")
                return found_link

        print(f"[DEBUG] No match found (best was <85%)")

    except Exception as e:
        print(f"[DEBUG] Error peeking at dnyuz author page: {e}")

    return None


def to_archive_link(url):
    return f"https://archive.is/newest/{url}"


def fetch_news():
    news = []
    for category, feed_url in RSS_FEEDS.items():
        print(f"Fetching {category}...")
        feed = feedparser.parse(feed_url)
        # Get top 2 from each feed
        for entry in feed.entries[:2]:
            if entry.link not in processed_links:
                # Logic: Use Source-Aware Peeking for Homelander links
                dnyuz_link = find_on_dnyuz(entry.title, category)

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


def search_news(query):
    """Looks for a specific word in all news categories."""
    query = query.lower()
    results = []
    for category, feed_url in RSS_FEEDS.items():
        feed = feedparser.parse(feed_url)
        for entry in feed.entries:
            if query in entry.title.lower():
                dnyuz_link = find_on_dnyuz(entry.title, category)
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
    news_items = fetch_news()
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
    search_results = search_news(text)

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
    news_items = fetch_news()
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
