"""
krx_addon.py
============
Drop this file in the SAME folder as your existing bot.py.
It adds NEW features on top of your existing bot WITHOUT changing any of
your existing code — it uses your existing db_connect()/PK_TYPE so its
data lives in the same database, but in its own new tables, so nothing
you already built (live quizzes, quiz sets, scheduling, etc.) is touched.

WHAT THIS ADDS
--------------
1. Auto-broadcast: every ADDON_BROADCAST_INTERVAL_MIN minutes, sends one
   bilingual (Hindi + English together) quiz poll to every group your bot
   is in — topic and Class 1 / 6-10 / 11-12 / Graduation level are both
   random each time, covering ALL subjects (Science, Maths, GK, History,
   Geography, English, Hindi, Computer, Reasoning, Current Affairs, etc.)
2. Persistent scoring for those auto-broadcast polls: +5 correct / -2
   wrong, stored in the addon's own table. On every answer it reacts with
   a Tenor GIF + an AI-written Hinglish shayari / funny movie-dialogue /
   "alone" one-liner, tagging the user — and that reaction message
   auto-deletes after 5 seconds in groups so it doesn't clutter the chat.
3. General AI chat: `/chat <anything>` in any chat, or simply replying to
   one of the bot's own messages in a DM, gets a normal AI answer on
   whatever the user asks — not limited to quiz/cybersecurity topics.
   (This is intentionally NOT a blind catch-all on every DM message,
   because your existing bot has many multi-step wizards — settitle,
   addapi, add_bulk, etc. — that read plain text as part of their flow.
   A catch-all here would fire an unwanted extra AI reply during those.
   `/chat` and reply-to-bot are the safe way to add free-form AI chat
   without breaking any of that.)
4. Provider auto-detection: `/addonkey <key>` lets the owner add AI keys
   for JUST this addon's own rotation pool (kept separate from your
   existing /addapi key system) — paste any key from any supported
   provider and it detects which one it is from the key's shape, no need
   to say which service it's for.

SETUP
-----
1. Save this file as krx_addon.py next to bot.py.
2. Add these optional env vars if you want them (both have safe defaults):
     ADDON_BROADCAST_INTERVAL_MIN=60
     TENOR_API_KEY=<your free Tenor key>   (GIF reaction skipped if unset)
3. Inside your bot.py's main() function, find where app.job_queue jobs are
   registered (search for "periodic_backup_job") and add these two lines
   right after ApplicationBuilder().build() creates `app`, anywhere before
   app.run_polling(...) is called:

       import krx_addon
       krx_addon.setup(app, db_connect, PK_TYPE, OWNER_ID, logger)

   That's it — nothing else in bot.py needs to change.
4. Add API keys for the addon with /addonkey <key> in a DM (owner only).
"""

import asyncio
import json
import logging
import os
import random
import re

import httpx
from telegram.constants import PollType
from telegram.ext import CommandHandler, MessageHandler, PollAnswerHandler, filters

TENOR_API_KEY = os.environ.get("TENOR_API_KEY")
BROADCAST_INTERVAL_MIN = int(os.environ.get("ADDON_BROADCAST_INTERVAL_MIN", "60"))

CORRECT_POINTS = 5
WRONG_POINTS = -2

QUIZ_LEVELS = ["Class 1-5", "Class 6-10", "Class 11-12", "Graduation"]
QUIZ_TOPICS = [
    "Science (Physics/Chemistry/Biology)", "Mathematics", "General Knowledge",
    "History", "Geography", "Civics & Political Science",
    "English Grammar & Literature", "Hindi Grammar & Literature",
    "Computer Science & IT", "Reasoning & Aptitude", "Environmental Science",
    "Economics", "Arts & Culture", "Sports", "Current Affairs",
    "Cybersecurity & Ethical Hacking",
]
CORRECT_GIF_TERMS = ["funny celebration", "victory dance funny", "winning meme funny"]
WRONG_GIF_TERMS = ["sad alone funny meme", "dramatic sad meme funny", "fail funny meme"]

_addon_logger = None


# ---------------------------------------------------------------- provider auto-detect

def detect_provider(raw_key: str):
    """Returns (provider, cleaned_key, extra) or (None, None, None)."""
    key = raw_key.strip()
    if key.lower().startswith("cf:"):
        parts = key.split(":")
        if len(parts) == 3:
            return "cloudflare", parts[2], parts[1]
        return None, None, None
    if key.startswith("AIza"):
        return "gemini", key, None
    if key.startswith("sk-or-"):
        return "openrouter", key, None
    if key.startswith("gsk_"):
        return "groq", key, None
    if key.startswith("hf_"):
        return "huggingface", key, None
    if key.startswith("csk-"):
        return "cerebras", key, None
    if key.startswith("github_pat_") or key.startswith("ghp_"):
        return "github", key, None
    if re.fullmatch(r"[a-zA-Z0-9]{32}", key):
        return "mistral", key, None
    if key.startswith("sk-"):
        return "openai", key, None
    return None, None, None


def _openai_style(base_url, model, key, prompt):
    url = f"{base_url}/chat/completions"
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    body = {"model": model, "messages": [{"role": "user", "content": prompt}], "temperature": 0.9}

    def parse(data):
        return data["choices"][0]["message"]["content"]

    return url, headers, body, parse


def _build_request(provider, key, extra, prompt):
    if provider == "gemini":
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"gemini-2.0-flash:generateContent?key={key}"
        )
        headers = {"Content-Type": "application/json"}
        body = {"contents": [{"parts": [{"text": prompt}]}]}

        def parse(data):
            return data["candidates"][0]["content"]["parts"][0]["text"]
        return url, headers, body, parse
    if provider == "openrouter":
        return _openai_style("https://openrouter.ai/api/v1", "meta-llama/llama-3.3-70b-instruct:free", key, prompt)
    if provider == "groq":
        return _openai_style("https://api.groq.com/openai/v1", "llama-3.3-70b-versatile", key, prompt)
    if provider == "cerebras":
        return _openai_style("https://api.cerebras.ai/v1", "llama3.3-70b", key, prompt)
    if provider == "github":
        return _openai_style("https://models.inference.ai.azure.com", "gpt-4o-mini", key, prompt)
    if provider == "mistral":
        return _openai_style("https://api.mistral.ai/v1", "open-mistral-7b", key, prompt)
    if provider == "openai":
        return _openai_style("https://api.openai.com/v1", "gpt-4o-mini", key, prompt)
    if provider == "huggingface":
        model = "mistralai/Mistral-7B-Instruct-v0.3"
        url = f"https://api-inference.huggingface.co/models/{model}"
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        body = {"inputs": prompt, "parameters": {"max_new_tokens": 512}}

        def parse(data):
            return data[0]["generated_text"] if isinstance(data, list) else data.get("generated_text", "")
        return url, headers, body, parse
    if provider == "cloudflare":
        model = "@cf/meta/llama-3.1-8b-instruct"
        url = f"https://api.cloudflare.com/client/v4/accounts/{extra}/ai/run/{model}"
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        body = {"messages": [{"role": "user", "content": prompt}]}

        def parse(data):
            return data["result"]["response"]
        return url, headers, body, parse
    raise ValueError(f"Unknown provider: {provider}")


async def _call_provider(provider, key, extra, prompt) -> str:
    url, headers, body, parse = _build_request(provider, key, extra, prompt)
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(url, headers=headers, json=body)
        resp.raise_for_status()
        return parse(resp.json())


# ---------------------------------------------------------------- DB layer (own tables)

def _init_tables(db_connect, pk_type):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS addon_api_keys (
            id {pk_type},
            api_key TEXT UNIQUE,
            provider TEXT,
            extra TEXT,
            daily_usage INTEGER DEFAULT 0,
            active INTEGER DEFAULT 1
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS addon_scores (
            user_id BIGINT PRIMARY KEY,
            username TEXT,
            score INTEGER DEFAULT 0
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS addon_polls (
            poll_id TEXT PRIMARY KEY,
            chat_id BIGINT,
            correct_option_id INTEGER
        )
    """)
    conn.commit()
    conn.close()


def _get_usable_keys(db_connect):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("SELECT id, api_key, provider, extra, daily_usage FROM addon_api_keys WHERE active=1 ORDER BY daily_usage ASC")
    rows = cur.fetchall()
    conn.close()
    return rows


def _bump_usage(db_connect, key_id):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("UPDATE addon_api_keys SET daily_usage = daily_usage + 1 WHERE id=?", (key_id,))
    conn.commit()
    conn.close()


async def _generate_with_rotation(db_connect, prompt) -> str:
    rows = _get_usable_keys(db_connect)
    if not rows:
        raise RuntimeError("No addon API keys added yet. Owner: use /addonkey <key>.")
    last_err = None
    for row in rows:
        key_id, api_key, provider, extra, _ = row
        try:
            result = await _call_provider(provider, api_key, extra, prompt)
            _bump_usage(db_connect, key_id)
            return result
        except Exception as e:
            last_err = e
            continue
    raise RuntimeError(f"All addon keys failed: {last_err}")


def _add_score(db_connect, user_id, username, delta):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("SELECT score FROM addon_scores WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    if row:
        cur.execute("UPDATE addon_scores SET score = score + ?, username=? WHERE user_id=?", (delta, username, user_id))
    else:
        cur.execute("INSERT INTO addon_scores (user_id, username, score) VALUES (?, ?, ?)", (user_id, username, delta))
    conn.commit()
    conn.close()


def _save_poll(db_connect, poll_id, chat_id, correct_option_id):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO addon_polls (poll_id, chat_id, correct_option_id) VALUES (?, ?, ?)",
        (poll_id, chat_id, correct_option_id),
    )
    conn.commit()
    conn.close()


def _get_poll(db_connect, poll_id):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("SELECT chat_id, correct_option_id FROM addon_polls WHERE poll_id=?", (poll_id,))
    row = cur.fetchone()
    conn.close()
    return row


def _get_tracked_groups(db_connect):
    """Reuses the host bot's existing 'groups' table (already populated by
    its own track_group_message handler) so we don't need a new one."""
    conn = db_connect()
    cur = conn.cursor()
    try:
        cur.execute("SELECT group_id FROM groups")
        rows = [r[0] for r in cur.fetchall()]
    except Exception:
        rows = []
    conn.close()
    return rows


# ---------------------------------------------------------------- AI helpers

async def _gen_quiz(db_connect):
    topic = random.choice(QUIZ_TOPICS)
    level = random.choice(QUIZ_LEVELS)
    prompt = (
        f"Create ONE multiple-choice quiz question suitable for '{level}' level students "
        f"on the subject '{topic}'. Write the question BILINGUALLY: English sentence, "
        f"then ' / ', then its Hindi translation, e.g. 'What is X? / X kya hai?'. Write "
        f"each option bilingually the same way, e.g. 'Apple / Seb'. Respond with ONLY raw "
        f'JSON: {{"question": "...", "options": ["...", "...", "...", "..."], '
        f'"correct_index": 0}}. No markdown fences, no extra text. Keep the bilingual '
        f"question under 280 characters and each bilingual option under 95 characters."
    )
    raw = await _generate_with_rotation(db_connect, prompt)
    cleaned = re.sub(r"^```(json)?|```$", "", raw.strip()).strip()
    data = json.loads(cleaned)
    return topic, level, data


async def _gen_shayari(db_connect, correct: bool, mention: str) -> str:
    if correct:
        mood = (
            "a witty, funny, celebratory 2-line Hinglish shayari OR a funny "
            "movie-dialogue-style victory line (original wording, not a real quoted "
            "film line), Instagram-caption style"
        )
    else:
        mood = (
            "randomly ONE of: (a) a dramatic mock-'sad and alone' 2-line Hinglish "
            "shayari, (b) an original funny dialogue in a dramatic Bollywood style "
            "(not a real quoted film line), or (c) an exaggerated 'akela/lonely' "
            "comic one-liner — Instagram reels caption style, funny not mean"
        )
    prompt = f"Write {mood} about a quiz answer. Naturally include {mention}. Max 30 words, no hashtags."
    return await _generate_with_rotation(db_connect, prompt)


async def _fetch_gif(term: str):
    if not TENOR_API_KEY:
        return None
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://tenor.googleapis.com/v2/search",
                params={"q": term, "key": TENOR_API_KEY, "limit": 20, "media_filter": "gif"},
            )
            resp.raise_for_status()
            results = resp.json().get("results", [])
            if not results:
                return None
            return random.choice(results)["media_formats"]["gif"]["url"]
    except Exception:
        return None


# ---------------------------------------------------------------- handlers

async def _cmd_addonkey(update, context, db_connect, pk_type, owner_id):
    if update.effective_user.id != owner_id:
        return
    if not context.args:
        await update.message.reply_text("Usage: /addonkey <key>  (cf:<account_id>:<token> for Cloudflare)")
        return
    provider, cleaned, extra = detect_provider(context.args[0])
    if not provider:
        await update.message.reply_text("Couldn't auto-detect this key's provider.")
        return
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("SELECT id FROM addon_api_keys WHERE api_key=?", (cleaned,))
    if not cur.fetchone():
        cur.execute(
            "INSERT INTO addon_api_keys (api_key, provider, extra) VALUES (?, ?, ?)",
            (cleaned, provider, extra),
        )
    conn.commit()
    conn.close()
    await update.message.reply_text(f"✅ Addon key added. Detected provider: {provider}")


async def _cmd_chat(update, context, db_connect):
    query = " ".join(context.args) if context.args else None
    if not query and update.message.reply_to_message:
        query = update.message.text
    if not query:
        await update.message.reply_text("Usage: /chat <your question>")
        return
    try:
        reply = await _generate_with_rotation(
            db_connect,
            "You are a helpful, friendly assistant. Answer naturally in the same "
            f"language style as this message:\n\n{query}",
        )
    except Exception as e:
        await update.message.reply_text(f"⚠️ AI unavailable right now ({e}).")
        return
    await update.message.reply_text(reply)


async def _on_reply_to_bot(update, context, db_connect):
    msg = update.message
    if not msg or not msg.text or msg.chat.type != "private":
        return
    if not (msg.reply_to_message and msg.reply_to_message.from_user
            and msg.reply_to_message.from_user.id == context.bot.id):
        return
    try:
        reply = await _generate_with_rotation(
            db_connect,
            "You are a helpful, friendly assistant. Answer naturally in the same "
            f"language style as this message:\n\n{msg.text}",
        )
    except Exception:
        return
    await msg.reply_text(reply)


async def _broadcast_job(app, db_connect):
    group_ids = _get_tracked_groups(db_connect)
    for chat_id in group_ids:
        try:
            topic, level, quiz = await _gen_quiz(db_connect)
            question = quiz["question"][:280]
            options = [str(o)[:95] for o in quiz["options"]][:10]
            correct_index = int(quiz["correct_index"])
            message = await app.bot.send_poll(
                chat_id=chat_id,
                question=f"🧠 [{topic} | {level}] {question}",
                options=options,
                type=PollType.QUIZ,
                correct_option_id=correct_index,
                is_anonymous=False,
            )
            _save_poll(db_connect, message.poll.id, chat_id, correct_index)
        except Exception:
            if _addon_logger:
                _addon_logger.exception("krx_addon broadcast failed for chat_id=%s", chat_id)
        await asyncio.sleep(1.5)


async def _on_poll_answer(update, context, db_connect):
    ans = update.poll_answer
    if not ans.option_ids:
        return
    poll_row = _get_poll(db_connect, ans.poll_id)
    if not poll_row:
        return  # not one of this addon's polls — ignore, let the host bot's own handler deal with it
    chat_id, correct_option_id = poll_row
    correct = ans.option_ids[0] == correct_option_id
    delta = CORRECT_POINTS if correct else WRONG_POINTS
    user = ans.user
    mention = f"@{user.username}" if user.username else user.first_name
    _add_score(db_connect, user.id, user.username, delta)

    try:
        line = await _gen_shayari(db_connect, correct, mention)
    except Exception:
        line = f"{'✅' if correct else '❌'} {mention}!"

    sign = "+5" if correct else "-2"
    caption = f"{line}\n\n({mention} {sign} pts)"
    gif_url = await _fetch_gif(random.choice(CORRECT_GIF_TERMS if correct else WRONG_GIF_TERMS))

    try:
        if gif_url:
            sent = await context.bot.send_animation(chat_id, gif_url, caption=caption)
        else:
            sent = await context.bot.send_message(chat_id, caption)
    except Exception:
        return

    chat = await context.bot.get_chat(chat_id)
    if chat.type in ("group", "supergroup"):
        asyncio.create_task(_delete_after(context.bot, chat_id, sent.message_id, 5))


async def _delete_after(bot, chat_id, message_id, seconds):
    await asyncio.sleep(seconds)
    try:
        await bot.delete_message(chat_id, message_id)
    except Exception:
        pass


# ---------------------------------------------------------------- public entry point

def setup(app, db_connect, pk_type, owner_id, logger=None):
    global _addon_logger
    _addon_logger = logger or logging.getLogger("krx_addon")

    _init_tables(db_connect, pk_type)

    app.add_handler(CommandHandler(
        "addonkey", lambda u, c: _cmd_addonkey(u, c, db_connect, pk_type, owner_id)
    ))
    app.add_handler(CommandHandler("chat", lambda u, c: _cmd_chat(u, c, db_connect)))
    app.add_handler(MessageHandler(
        filters.TEXT & filters.ChatType.PRIVATE & filters.REPLY & ~filters.COMMAND,
        lambda u, c: _on_reply_to_bot(u, c, db_connect),
    ))
    app.add_handler(PollAnswerHandler(lambda u, c: _on_poll_answer(u, c, db_connect)))

    if app.job_queue:
        app.job_queue.run_repeating(
            lambda ctx: asyncio.create_task(_broadcast_job(app, db_connect)),
            interval=BROADCAST_INTERVAL_MIN * 60,
            first=30,
            name="krx_addon_broadcast",
        )

    _addon_logger.info("krx_addon loaded — broadcasting every %s min.", BROADCAST_INTERVAL_MIN)
