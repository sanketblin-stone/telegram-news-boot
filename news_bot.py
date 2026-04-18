#!/usr/bin/env python3
import asyncio
import feedparser
import requests
from datetime import time
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

RSS_FEEDS = {
    "World": "https://rss.nytimes.com/services/xml/rss/nyt/World.xml",
    "Business": "https://rss.nytimes.com/services/xml/rss/nyt/Business.xml",
    "Technology": "https://rss.nytimes.com/services/xml/rss/nyt/Technology.xml",
}

processed_links = set()


def to_archive_link(url):
    return f"https://archive.ph/{url}"


def fetch_news():
    news = []
    for category, feed_url in RSS_FEEDS.items():
        feed = feedparser.parse(feed_url)
        for entry in feed.entries[:5]:
            if entry.link not in processed_links:
                news.append(
                    {
                        "title": entry.title,
                        "link": to_archive_link(entry.link),
                        "category": category,
                        "original_link": entry.link,
                    }
                )
                processed_links.add(entry.link)
    return news


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "I'm your news bot! Send /ping to test, or ask 'whats the news for today'"
    )


async def ping_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("I'm awake, Sankey!")


async def news_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Fetching latest news...")
    news_items = fetch_news()
    if not news_items:
        await update.message.reply_text("No new news found!")
        return

    message = "📰 *Today's NYT News*\n\n"
    for item in news_items:
        message += f"*{item['category']}:* {item['title']}\n🔗 {item['link']}\n\n"

    await update.message.reply_text(message, parse_mode="Markdown")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.lower()
    if "whats the news for today" in text or "what's the news for today" in text:
        await news_command(update, context)
    elif "archive" in text and "nyt" in text:
        await update.message.reply_text(
            "Send me an NYT article link and I'll give you an archive.ph link!"
        )


async def daily_digest(context: ContextTypes.DEFAULT_TYPE):
    news_items = fetch_news()
    if not news_items:
        return

    message = "📰 *Your Daily NYT Digest*\n\n"
    for item in news_items:
        message += f"*{item['category']}:* {item['title']}\n🔗 {item['link']}\n\n"

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
    job_queue.run_daily(daily_digest, time=time(21, 0, 0))

    print("Bot is running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
