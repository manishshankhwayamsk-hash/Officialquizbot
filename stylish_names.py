# -*- coding: utf-8 -*-
"""
✨ STYLISH NAME generator for the quiz bot.

User taps  "✨ Stylish Name"  (or sends /style Rahul) -> sends a normal name -> gets 10 unique decorated names
built by MIXING many Unicode fonts + decoration frames. "🔄 Aur names" gives 10 fresh ones every time.
Every name is shown inside <code> so one tap copies it.
"""
import html
import random
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (ApplicationHandlerStop, CallbackQueryHandler, CommandHandler,
                          ContextTypes, MessageHandler, filters)

UP = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
LO = "abcdefghijklmnopqrstuvwxyz"


def _range_font(up_start, lo_start, exceptions=None):
    m = {}
    for i, ch in enumerate(UP):
        m[ch] = chr(up_start + i) if up_start else ch
    for i, ch in enumerate(LO):
        m[ch] = chr(lo_start + i) if lo_start else ch
    if exceptions:
        m.update(exceptions)
    return m


def _from_strings(upper, lower):
    m = dict(zip(UP, upper))
    m.update(dict(zip(LO, lower)))
    return m


FONTS = {
    "bold": _range_font(0x1D400, 0x1D41A),
    "italic": _range_font(0x1D434, 0x1D44E, {"h": "\u210E"}),
    "bolditalic": _range_font(0x1D468, 0x1D482),
    "script": _range_font(0x1D49C, 0x1D4B6, {"B": "\u212C", "E": "\u2130", "F": "\u2131", "H": "\u210B", "I": "\u2110",
                                              "L": "\u2112", "M": "\u2133", "R": "\u211B", "e": "\u212F", "g": "\u210A", "o": "\u2134"}),
    "boldscript": _range_font(0x1D4D0, 0x1D4EA),
    "fraktur": _range_font(0x1D504, 0x1D51E, {"C": "\u212D", "H": "\u210C", "I": "\u2111", "R": "\u211C", "Z": "\u2128"}),
    "boldfraktur": _range_font(0x1D56C, 0x1D586),
    "double": _range_font(0x1D538, 0x1D552, {"C": "\u2102", "H": "\u210D", "N": "\u2115", "P": "\u2119", "Q": "\u211A",
                                              "R": "\u211D", "Z": "\u2124"}),
    "sans": _range_font(0x1D5A0, 0x1D5BA),
    "sansbold": _range_font(0x1D5D4, 0x1D5EE),
    "sansitalic": _range_font(0x1D608, 0x1D622),
    "sansbolditalic": _range_font(0x1D63C, 0x1D656),
    "mono": _range_font(0x1D670, 0x1D68A),
    "wide": _range_font(0xFF21, 0xFF41),
    "circled": _range_font(0x24B6, 0x24D0),
    "paren": _range_font(0x1F110, 0x249C),
    "smallcaps": _from_strings(UP, "ᴀʙᴄᴅᴇғɢʜɪᴊᴋʟᴍɴᴏᴘǫʀsᴛᴜᴠᴡxʏᴢ"),
    "look": _from_strings("ΛßС∂ЄҒGНΙJКԼМИΘPQЯЅТЦVШХУZ", "αв¢∂єƒgнιנкℓмησρqяѕтυνωχуz"),
    "wide_up": {c: chr(0xFF21 + i) for i, c in enumerate(UP)} | {c: chr(0xFF21 + i) for i, c in enumerate(LO)},
}
# uppercase-only fonts (letters are upper-cased first)
for _name, _start in (("negcircled", 0x1F150), ("squared", 0x1F130), ("negsquared", 0x1F170), ("regional", 0x1F1E6)):
    _m = {c: chr(_start + i) for i, c in enumerate(UP)}
    _m.update({c: _m[c.upper()] for c in LO})
    FONTS[_name] = _m

# fonts that look good for a whole name / mixed together
MAIN_FONTS = ["bold", "italic", "bolditalic", "script", "boldscript", "fraktur", "boldfraktur", "double", "sansbold",
              "sansbolditalic", "mono", "circled", "smallcaps", "look", "wide"]
BIG_FONTS = ["boldscript", "script", "boldfraktur", "bolditalic", "sansbolditalic", "double", "bold"]
SMALL_FONTS = ["smallcaps", "look", "italic", "sansitalic", "mono", "sans"]
BOX_FONTS = ["negcircled", "squared", "negsquared", "regional", "paren"]

LEFT = ["•⏤•", "⎯꯭🌿", "𓆩", "🦋", "🍃", "꯭❀꯭", "⏤•⧉⃞🦋", "●⏤", "❥", "𓄂꯭", "°🩵", "🌸꯭༏", "✰", "♥⃝", "🔥",
        "⟶", "◓ •", "𖤓", "⛦⃕", "𐏓", "•⏤꯭⛦⃕≛⃝🍃", "⎯꯭🍧", "ꭘ🔥ꗝ", "✿⃟🍭", "⋆", "✦", "☘", "🪽", "💫", "🌙",
        "👑", "🍷", "🥀", "🕊", "⟬", "꯭𓄂꯭⎯⃞⃚", "🍒", "💗", "⚘⃝", "𓋥", "♡", "★彡", "🧿", "🌹", "❤️‍🔥", "🥂"]
RIGHT = ["❥⍣⃟💗࿐", "𓆪", "🦋", "🍃", "꯭❀꯭", "♥️", "⎯꯭", "⛧", "༎ࠫ", "🔥♥️", "➤⧉⃞🔥♥️", "💗", "✿", "✦",
         "⚘⃝", "𓆪🌿", "♡", "★", "🍒", "💫", "🌙", "👑", "🥀", "🪽", "⟭", "⛦⃕", "𝄞", "꯭─꯭,", "࿐", "🌿", "꯭🍧", "💎", "🧿", "🌹"]
BRACKETS = [("『", "』"), ("【", "】"), ("「", "」"), ("〖", "〗"), ("《", "》"), ("❰", "❱"), ("⟦", "⟧"), ("〔", "〕"),
            ("⸨", "⸩"), ("❨", "❩"), ("꧁", "꧂"), ("╰", "╯"), ("[", "]"), ("〘", "〙")]
SEPS = ["•", "꯭", "·", "ღ", "⸼", "✦", "♡", "⋆", "꯭❀"]
MARKS = ["\u20dd", "\u20de", "\u20df", "\u0336", "\u035c", "\u0353", "\u20e3"]
GLUE = [" ", " ", " ", "꯭ ", " ⸼ ", " • ", " ♡ "]


def _apply(font, text):
    m = FONTS[font]
    return "".join(m.get(ch, ch) for ch in text)


def _has_latin(text):
    return any(ch in UP or ch in LO for ch in text)


def _body(name, rnd):
    """Returns one mixed-font rendering of the name."""
    words = name.split()
    pattern = rnd.choice(["single", "single", "firstbig", "firstbig", "alt", "random", "look", "spaced",
                          "marks", "box", "wordsdiff"])

    if pattern == "single":
        f = rnd.choice(MAIN_FONTS)
        out = _apply(f, name.upper() if f in ("wide",) and rnd.random() < .3 else name)
    elif pattern == "firstbig":
        big, small = rnd.choice(BIG_FONTS), rnd.choice(SMALL_FONTS)
        out = " ".join(_apply(big, w[0]) + _apply(small, w[1:].lower()) for w in words)
    elif pattern == "alt":
        a, b = rnd.sample(MAIN_FONTS, 2)
        out, i = [], 0
        for ch in name:
            if ch.isalpha():
                out.append(_apply(a if i % 2 == 0 else b, ch))
                i += 1
            else:
                out.append(ch)
        out = "".join(out)
    elif pattern == "random":
        pool = rnd.sample(MAIN_FONTS, 4)
        out = "".join(_apply(rnd.choice(pool), ch) if ch.isalpha() else ch for ch in name)
    elif pattern == "look":
        out = " ".join(w[0].upper() + _apply("look", w[1:].lower()) for w in words)
        if rnd.random() < .5:
            out = _apply("look", name.lower())
    elif pattern == "spaced":
        f = rnd.choice(MAIN_FONTS)
        sep = rnd.choice(SEPS)
        out = "  ".join(sep.join(_apply(f, ch) for ch in w) for w in words)
    elif pattern == "marks":
        f = rnd.choice(MAIN_FONTS)
        mk = rnd.choice(MARKS)
        first_only = rnd.random() < .5
        parts = []
        for w in words:
            letters = list(w)
            parts.append("".join(_apply(f, ch) + (mk if ch.isalpha() and (not first_only or idx == 0) else "")
                                 for idx, ch in enumerate(letters)))
        out = " ".join(parts)
    elif pattern == "box":
        f = rnd.choice(BOX_FONTS)
        sep = rnd.choice(["", "", "꯭", " "])
        out = "  ".join(sep.join(_apply(f, ch) for ch in w) for w in words)
    else:  # wordsdiff: each word in its own font
        out = " ".join(_apply(rnd.choice(MAIN_FONTS), w) for w in words)
    return out


def _frame(body, rnd):
    r = rnd.random()
    left, right = rnd.choice(LEFT), rnd.choice(RIGHT)
    if r < .18:
        a, b = rnd.choice(BRACKETS)
        return f"{left}{a}{body}{b}{right}"
    if r < .30:
        return f"{left} {body}"
    if r < .42:
        return f"{body} {right}"
    return f"{left}{rnd.choice(GLUE)}{body}{rnd.choice(GLUE)}{right}"


def generate(name: str, count: int = 10, seen: set | None = None) -> list:
    """Unique stylish variants of `name` (never repeats anything already in `seen`)."""
    name = " ".join(name.split())[:28]
    rnd = random.Random(time.time_ns())
    seen = seen if seen is not None else set()
    latin = _has_latin(name)
    out, tries = [], 0
    while len(out) < count and tries < count * 40:
        tries += 1
        body = _body(name, rnd) if latin else name
        s = _frame(body, rnd)
        if s not in seen:
            seen.add(s)
            out.append(s)
    if len(seen) > 400:          # keep memory small
        seen.clear()
    return out


# ------------------------------------------------------------------ Telegram handlers
def _markup():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Aur names", callback_data="sn_more"),
         InlineKeyboardButton("✏️ Naya naam", callback_data="sn_new")],
        [InlineKeyboardButton("🔙 Main Menu", callback_data="menu_main")],
    ])


def _result_text(name, names):
    lines = [f"✨ <b>Stylish names</b> for <b>{html.escape(name)}</b>\n"]
    for i, n in enumerate(names, 1):
        lines.append(f"{i}. <code>{html.escape(n)}</code>")
    lines.append("\n👆 Kisi bhi name par tap karo, copy ho jayega.")
    if not _has_latin(name):
        lines.append("ℹ️ Fonts ke liye naam English (Latin) letters me bhejo, jaise Rahul. Abhi sirf decorations lage hain.")
    return "\n".join(lines)


async def _send_names(message, context, name):
    seen = context.user_data.setdefault("sn_seen", set())
    context.user_data["sn_name"] = name
    names = generate(name, 10, seen)
    await message.reply_text(_result_text(name, names), parse_mode="HTML", reply_markup=_markup())


async def sn_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    context.user_data["sn_wait"] = True
    await q.message.reply_text("✍️ Apna naam bhejo (jaise: Rahul ya Aisha Khan).\nMain uske bahut saare unique stylish names bana dungi.")


async def sn_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "sn_more" and context.user_data.get("sn_name"):
        await _send_names(q.message, context, context.user_data["sn_name"])
    else:
        context.user_data["sn_wait"] = True
        await q.message.reply_text("✍️ Naya naam bhejo:")


async def sn_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("sn_wait"):
        return
    name = " ".join((update.effective_message.text or "").split())
    if not name:
        return
    context.user_data["sn_wait"] = False
    context.user_data["sn_seen"] = set()
    await _send_names(update.effective_message, context, name[:28])
    raise ApplicationHandlerStop        # this text was a name, not for any other handler


async def style_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = " ".join(context.args).strip()
    if not name:
        context.user_data["sn_wait"] = True
        await update.effective_message.reply_text("✍️ Apna naam bhejo (ya aise likho: /style Rahul)")
        return
    context.user_data["sn_seen"] = set()
    await _send_names(update.effective_message, context, name[:28])


def register(app):
    app.add_handler(CommandHandler("style", style_command))
    app.add_handler(CallbackQueryHandler(sn_start, pattern="^sn_start$"))
    app.add_handler(CallbackQueryHandler(sn_callback, pattern="^sn_(more|new)$"))
    # runs before the normal text handlers, but only acts when the user pressed the Stylish Name button
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, sn_text), group=-3)
