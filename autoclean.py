# -*- coding: utf-8 -*-
"""
🧹 AUTO-CLEAN for groups.

  /autoclean on          -> remove other bots' polls/quizzes AND link / @username / promo messages
  /autoclean polls       -> remove only other bots' polls/quizzes
  /autoclean off         -> stop
  /autoclean status      -> show current mode
  /autoclean log on|off  -> DM group admins a short note for every removal (default on)

Never touched: group admins, the bot owner, anonymous admins and this bot's own messages.
Needs the bot to be a group ADMIN with "Delete messages" permission.
"""
import logging
import re
import time

from telegram import Update
from telegram.ext import CommandHandler, ContextTypes, MessageHandler, filters

logger = logging.getLogger("autoclean")

H = {}              # filled by register()
_cache_mode = {}    # chat_id -> (mode, expires)
_cache_admin = {}   # chat_id -> (set(user_ids), expires)

PROMO_RE = re.compile(
    r"(https?://|www\.|t\.me/|telegram\.(?:me|dog)/|discord\.gg/|bit\.ly/|"
    r"@[A-Za-z][A-Za-z0-9_]{4,}|"
    r"\bjoin\s+(?:now|our|us|karo|kare|kariye)\b|\bpromo\b|\bfree\s+(?:vip|premium|coins)\b|\bsubscribe\b|"
    r"जॉइन|ज्वाइन|प्रोमो)",
    re.I,
)
LINK_ENTITIES = {"url", "text_link", "mention", "text_mention"}


# ------------------------------------------------------------------ settings
def mode_for(chat_id: int) -> str:
    """'off' | 'polls' | 'all'"""
    now = time.time()
    hit = _cache_mode.get(chat_id)
    if hit and hit[1] > now:
        return hit[0]
    val = H["get_setting"](f"autoclean_{chat_id}", "off")
    if val not in ("off", "polls", "all"):
        val = "off"
    _cache_mode[chat_id] = (val, now + 60)
    return val


def set_mode(chat_id: int, val: str):
    H["set_setting"](f"autoclean_{chat_id}", val)
    _cache_mode.pop(chat_id, None)


def log_on(chat_id: int) -> bool:
    return H["get_setting"](f"autoclean_log_{chat_id}", "1") == "1"


async def admin_ids(bot, chat_id: int):
    """Set of human admin ids. Returns None if Telegram did not answer (then we do nothing)."""
    now = time.time()
    hit = _cache_admin.get(chat_id)
    if hit and hit[1] > now:
        return hit[0]
    try:
        ids = {m.user.id for m in await bot.get_chat_administrators(chat_id) if not m.user.is_bot}
    except Exception as e:  # noqa
        logger.info(f"[autoclean] admin list failed in {chat_id}: {e}")
        return None
    _cache_admin[chat_id] = (ids, now + 300)
    return ids


# ------------------------------------------------------------------ detection
def _has_link_or_promo(msg) -> bool:
    text = msg.text or msg.caption or ""
    ents = list(msg.entities or []) + list(msg.caption_entities or [])
    if any(e.type in LINK_ENTITIES for e in ents):
        return True
    return bool(PROMO_RE.search(text))


async def reason_for(msg, bot, chat_id: int):
    """Returns a short reason to delete, or None to leave the message alone."""
    user = msg.from_user
    if user and user.id == bot.id:
        return None                                  # our own quiz/polls
    if user and user.id == H["owner_id"]:
        return None
    if msg.sender_chat and msg.sender_chat.id == chat_id:
        return None                                  # anonymous group admin
    ids = await admin_ids(bot, chat_id)
    if ids is None:
        return None
    if user and user.id in ids:
        return None
    mode = mode_for(chat_id)
    if user and user.is_bot and msg.poll is not None:
        return "doosre bot ka poll / quiz"
    if mode == "all" and _has_link_or_promo(msg):
        return "link / username / promo"
    return None


# ------------------------------------------------------------------ handlers
async def watch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg, chat = update.effective_message, update.effective_chat
    if not msg or not chat or chat.type not in ("group", "supergroup"):
        return
    if mode_for(chat.id) == "off":
        return
    why = await reason_for(msg, context.bot, chat.id)
    if not why:
        return
    try:
        await msg.delete()
    except Exception as e:  # noqa
        logger.info(f"[autoclean] delete failed in {chat.id} (bot admin nahi?): {e}")
        return
    logger.info(f"[autoclean] removed in {chat.id}: {why}")
    if log_on(chat.id):
        if msg.from_user:
            who = msg.from_user.full_name
        elif msg.sender_chat:
            who = msg.sender_chat.title or "channel"
        else:
            who = "?"
        note = f"🧹 Auto-clean\nGroup: {chat.title}\nHataya gaya: {why}\nKisne bheja: {who}"
        for uid in list(await admin_ids(context.bot, chat.id) or [])[:15]:
            try:
                await context.bot.send_message(uid, note)
            except Exception:  # noqa  (admin ne bot ko start nahi kiya)
                pass


async def autoclean_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
    if chat.type not in ("group", "supergroup"):
        await msg.reply_text("ℹ️ Ye command group me chalao.")
        return
    if not (user.id == H["owner_id"] or await H["is_chat_admin"](update, context)):
        await msg.reply_text("🚫 Sirf group admin chala sakta hai.")
        return
    args = [a.lower() for a in context.args]
    sub = args[0] if args else "status"

    if sub == "status":
        m = mode_for(chat.id)
        label = {"off": "OFF ❌", "polls": "Sirf doosre bot ke polls ✅", "all": "Polls + link/promo ✅"}[m]
        await msg.reply_text(
            f"🧹 Auto-clean: {label}\nLog (admin DM): {'ON' if log_on(chat.id) else 'OFF'}\n\n"
            "Bot ko group me admin ke saath 'Delete messages' permission honi chahiye.")
    elif sub == "off":
        set_mode(chat.id, "off")
        await msg.reply_text("⏹ Auto-clean band.")
    elif sub in ("on", "all"):
        set_mode(chat.id, "all")
        await msg.reply_text("✅ Auto-clean ON: doosre bot ke polls/quiz aur link/username/promo wale messages hat jayenge. "
                             "Admins aur owner ke messages nahi hatenge.")
    elif sub == "polls":
        set_mode(chat.id, "polls")
        await msg.reply_text("✅ Auto-clean ON (sirf doosre bot ke polls/quiz).")
    elif sub == "log" and len(args) > 1 and args[1] in ("on", "off"):
        H["set_setting"](f"autoclean_log_{chat.id}", "1" if args[1] == "on" else "0")
        await msg.reply_text(f"📝 Admin log: {args[1].upper()}")
    else:
        await msg.reply_text("Aise likho:\n/autoclean on  |  /autoclean polls  |  /autoclean off\n"
                             "/autoclean status  |  /autoclean log on|off")


def register(app, *, get_setting, set_setting, is_chat_admin, owner_id):
    H.update(get_setting=get_setting, set_setting=set_setting, is_chat_admin=is_chat_admin, owner_id=owner_id)
    app.add_handler(CommandHandler("autoclean", autoclean_command))
    # block=False + its own group: it never stops the quiz/answer handlers
    app.add_handler(MessageHandler(filters.ChatType.GROUPS, watch, block=False), group=8)
    logger.info("[autoclean] registered")
