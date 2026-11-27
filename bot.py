#!/usr/bin/env python3
"""
Quiz Maker Bot  (python-telegram-bot v21+)

Setup
-----
    pip install -r requirements.txt
    export BOT_TOKEN="123456:ABC..."        # from @BotFather
    export DB_PATH="quizzes.db"             # optional (use a persistent disk on Render)
    python quizmaker_bot.py

BotFather: /setjoingroups -> Enable  (so the bot can be added to groups)
"""
import asyncio
import html
import json
import logging
import os
import random
import re
import sqlite3
import string
import time
from urllib.parse import quote

from telegram import BotCommand, InlineKeyboardButton as Btn, InlineKeyboardMarkup as Kb, Poll, Update
from telegram.constants import ChatType, ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes,
    MessageHandler, PollAnswerHandler, filters,
)

TOKEN = os.getenv("BOT_TOKEN", "")
DB_PATH = os.getenv("DB_PATH", "quizzes.db")
MAX_Q = 500
PAGE = 8
HTML = ParseMode.HTML
MEDALS = ["🥇", "🥈", "🥉"]
logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
log = logging.getLogger("quizbot")

# ───────────────────────────── database ─────────────────────────────
_db = sqlite3.connect(DB_PATH, check_same_thread=False)
_db.row_factory = sqlite3.Row
_db.execute("""CREATE TABLE IF NOT EXISTS quizzes(
    code TEXT PRIMARY KEY, owner_id INTEGER, owner_username TEXT DEFAULT '',
    title TEXT DEFAULT '', description TEXT DEFAULT '', creator TEXT DEFAULT '',
    creator_link TEXT DEFAULT '', promo TEXT DEFAULT '',
    timer INTEGER DEFAULT 20, shuffle_q INTEGER DEFAULT 0, shuffle_o INTEGER DEFAULT 0,
    opt_style TEXT DEFAULT 'none', creator_in_q TEXT DEFAULT 'off',
    show_expl INTEGER DEFAULT 1, q_numbers INTEGER DEFAULT 1,
    questions TEXT DEFAULT '[]', saved INTEGER DEFAULT 0, plays INTEGER DEFAULT 0,
    src TEXT DEFAULT '', created REAL)""")
_db.commit()
COLS = {r[1] for r in _db.execute("PRAGMA table_info(quizzes)")}


def _row(r):
    if not r:
        return None
    d = dict(r)
    d["questions"] = json.loads(d["questions"] or "[]")
    return d


def get_quiz(code):
    if not code:
        return None
    return _row(_db.execute("SELECT * FROM quizzes WHERE code=?", (str(code),)).fetchone())


def _new_code():
    while True:
        c = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
        if not _db.execute("SELECT 1 FROM quizzes WHERE code=?", (c,)).fetchone():
            return c


def new_quiz(user, **kw):
    data = dict(
        owner_id=user.id, owner_username=user.username or "", creator=user.full_name,
        creator_link=(f"https://t.me/{user.username}" if user.username else ""), created=time.time(),
    )
    data.update(kw)
    data["code"] = _new_code()
    data["questions"] = json.dumps(data.get("questions", []), ensure_ascii=False)
    assert set(data) <= COLS
    with _db:
        _db.execute(f"INSERT INTO quizzes({', '.join(data)}) VALUES({', '.join('?' * len(data))})", list(data.values()))
    return data["code"]


def update_quiz(code, **kw):
    sets, vals = [], []
    for k, v in kw.items():
        assert k in COLS and k != "code"
        if k == "questions":
            v = json.dumps(v, ensure_ascii=False)
        sets.append(f"{k}=?")
        vals.append(v)
    vals.append(code)
    with _db:
        _db.execute(f"UPDATE quizzes SET {', '.join(sets)} WHERE code=?", vals)


def delete_quiz(code):
    with _db:
        _db.execute("DELETE FROM quizzes WHERE code=?", (code,))


def list_saved(uid, page):
    rows = _db.execute(
        "SELECT code,title,questions FROM quizzes WHERE owner_id=? AND saved=1 ORDER BY created DESC LIMIT ? OFFSET ?",
        (uid, PAGE + 1, page * PAGE)).fetchall()
    return [(r["code"], r["title"], len(json.loads(r["questions"] or "[]"))) for r in rows]


def has_saved(uid, quiz):
    if quiz["owner_id"] == uid:
        return bool(quiz["saved"])
    return _db.execute("SELECT 1 FROM quizzes WHERE owner_id=? AND src=? AND saved=1", (uid, quiz["code"])).fetchone() is not None


def save_quiz(user, quiz):
    """Owner: mark saved. Anyone else: save an editable copy."""
    if quiz["owner_id"] == user.id:
        update_quiz(quiz["code"], saved=1)
        return quiz["code"]
    if has_saved(user.id, quiz):
        return None
    keep = ("title", "description", "creator", "creator_link", "promo", "timer", "shuffle_q", "shuffle_o",
            "opt_style", "creator_in_q", "show_expl", "q_numbers", "questions")
    return new_quiz(user, src=quiz["code"], saved=1, **{k: quiz[k] for k in keep})


# ───────────────────────────── helpers ─────────────────────────────
def esc(s):
    return html.escape(str(s or ""))


def clip(s, n):
    s = (s or "").strip()
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def norm_link(v):
    v = v.strip()
    if v.startswith("@") and len(v) > 1:
        return "https://t.me/" + v[1:]
    if re.match(r"^(t\.me|telegram\.me)/", v):
        return "https://" + v
    if v.startswith(("http://", "https://")):
        return v
    return None


def thread_of(msg):
    return msg.message_thread_id if msg is not None and getattr(msg, "is_topic_message", False) else None


def cmd_arg(update):
    return re.sub(r"^/\w+(@\w+)?\s*", "", update.effective_message.text or "").strip()


HINDI = "कखगघङचछजझञ"
STYLES = {
    "none": ("None", lambda i: ""),
    "upper": ("A) B) C)", lambda i: f"{chr(65 + i)}) "),
    "lower": ("(a) (b) (c)", lambda i: f"({chr(97 + i)}) "),
    "num": ("1. 2. 3.", lambda i: f"{i + 1}. "),
    "hindi": ("क) ख) ग)", lambda i: f"{HINDI[i]}) "),
    "dot": ("• bullets", lambda i: "• "),
}
CIQ = {"off": "Off", "prefix": "Before question", "suffix": "After question"}
SHUF_LABEL = {(0, 0): "Off", (1, 0): "Questions", (0, 1): "Options", (1, 1): "Questions + Options"}
SHUF_ORDER = [(0, 0), (1, 0), (0, 1), (1, 1)]
SHUF_ARGS = {"off": (0, 0), "q": (1, 0), "questions": (1, 0), "o": (0, 1), "options": (0, 1), "both": (1, 1), "on": (1, 1)}


def next_shuf(q):
    return SHUF_ORDER[(SHUF_ORDER.index((q["shuffle_q"], q["shuffle_o"])) + 1) % 4]


# ───────────────────────────── text-quiz parser ─────────────────────────────
HEAD_KEYS = {"title": "title", "quiz title": "title", "name": "title", "description": "description",
             "desc": "description", "creator": "creator", "author": "creator", "by": "creator",
             "promo": "promo", "promotion": "promo", "link": "promo", "timer": "timer", "time": "timer"}
HEAD_RE = re.compile(r"^\s*(" + "|".join(sorted(HEAD_KEYS, key=len, reverse=True)) + r")\s*[:=\-]\s*(.+)$", re.I)
QNUM_RE = re.compile(r"^\s*(?:Q(?:uestion)?\s*\.?\s*\d*|\d+)\s*[\.\)\:\-]\s+", re.I)
OPT_RE = re.compile(r"^\s*(\*?)\s*[\(\[]?\s*([A-Ja-jकखगघङ]|\d{1,2})\s*[\)\]\.\:\-]\s+(.+)$")
ANS_RE = re.compile(r"^\s*(?:ans(?:wer)?|correct(?:\s+answer)?|right(?:\s+answer)?|उत्तर)\s*[:\-=\.]\s*(.+)$", re.I)
EXP_RE = re.compile(r"^\s*(?:exp(?:lanation)?|sol(?:ution)?|reason|व्याख्या)\s*[:\-=]\s*(.*)$", re.I)
MARK_RE = re.compile(r"(✅|✔️|✔|☑️|☑)")


def resolve_answer(raw, opts):
    r = raw.strip().strip("()[]").strip().rstrip(".)").strip()
    low = [o.strip().lower() for o in opts]
    if r.lower() in low:
        return low.index(r.lower())
    if len(r) == 1 and r.lower() in "abcdefghij":
        i = "abcdefghij".index(r.lower())
    elif r.isdigit():
        i = int(r) - 1
    elif len(r) == 1 and r in "कखगघङ":
        i = "कखगघङ".index(r)
    else:
        m = re.match(r"^\(?([A-Ja-j])[\)\.\:]", raw.strip())
        if not m:
            return None
        i = "abcdefghij".index(m.group(1).lower())
    return i if 0 <= i < len(opts) else None


def _finish_q(cur, n, qs, errs):
    opts, label = cur["options"], clip(cur["q"], 30)
    if len(opts) < 2:
        errs.append(f"#{n} “{label}”: needs at least 2 options")
        return
    if len(opts) > 10:
        errs.append(f"#{n} “{label}”: max 10 options")
        return
    idx = cur["correct"]
    if cur["ans_raw"]:
        r = resolve_answer(cur["ans_raw"], opts)
        if r is not None:
            idx = r
        elif idx is None:
            errs.append(f"#{n} “{label}”: can't read answer “{cur['ans_raw'][:20]}”")
            return
    if idx is None:
        errs.append(f"#{n} “{label}”: no answer (add ✅ or 'Answer: B')")
        return
    qs.append({"q": clip(cur["q"], 1000), "options": [clip(o, 100) for o in opts],
               "correct": idx, "expl": clip(cur["expl"], 200)})


def parse_quiz_text(text):
    """Returns (header_dict, questions, errors)."""
    hdr, qs, errs = {}, [], []
    cur, n, blank, mode = None, 0, False, None

    def close():
        nonlocal cur
        if cur is not None:
            _finish_q(cur, n, qs, errs)
        cur = None

    for raw in text.replace("\r", "").split("\n"):
        line = raw.strip()
        if not line:
            blank = True
            continue
        was_blank, blank = blank, False
        if cur is None and n == 0:
            m = HEAD_RE.match(line)
            if m:
                hdr[HEAD_KEYS[m.group(1).lower()]] = m.group(2).strip()
                continue
        m = ANS_RE.match(line)
        if cur and cur["options"] and m:
            cur["ans_raw"], mode = m.group(1).strip(), "ans"
            continue
        m = EXP_RE.match(line)
        if cur and m:
            cur["expl"], mode = m.group(1).strip(), "expl"
            continue
        m = OPT_RE.match(line)
        if (cur and m and mode != "ans"
                and not (was_blank and len(cur["options"]) >= 2 and (cur["ans_raw"] or cur["correct"] is not None))):
            star, _lab, otext = m.groups()
            mark = bool(star) or bool(MARK_RE.search(otext))
            otext = MARK_RE.sub("", otext).strip() or "—"
            if mark and cur["correct"] is None:
                cur["correct"] = len(cur["options"])
            cur["options"].append(otext)
            mode = "opt"
            continue
        qstart = bool(QNUM_RE.match(line))
        if cur is None or mode == "ans":
            start = True
        elif mode == "expl":
            start = was_blank or qstart
        elif cur["options"]:
            start = was_blank or qstart or bool(cur["ans_raw"]) or cur["correct"] is not None
        else:
            start = qstart
        if start:
            close()
            n += 1
            cur = {"q": QNUM_RE.sub("", line, count=1).strip() or line, "options": [],
                   "correct": None, "ans_raw": "", "expl": ""}
            mode = "q"
        elif mode == "expl":
            cur["expl"] += " " + line
        elif cur["options"]:
            cur["options"][-1] += " " + line
        else:
            cur["q"] += "\n" + line
    close()
    return hdr, qs, errs


# ───────────────────────────── field validation ─────────────────────────────
PROMPTS = {
    "title": "✏️ Send the <b>quiz title</b>:",
    "description": "📝 Send the <b>description</b> (/skip to leave empty, <code>-</code> to clear):",
    "creator": "👤 Send the <b>creator name</b>.\nOptional button link: <code>Name | @username</code>\n(/skip to use your own name)",
    "promo": "🔗 Send the <b>promotion link</b> (<code>https://…</code> or <code>@channel</code>). Send <code>-</code> to remove:",
    "timer": "⏱ Send the <b>timer in seconds</b> for every question (5–600):",
}


def field_changes(field, raw, quiz):
    raw = raw.strip()
    clear = raw == "-"
    if field == "title":
        if clear or not raw:
            return None, "Title can't be empty."
        return {"title": clip(raw, 100)}, None
    if field == "description":
        return {"description": "" if clear else clip(raw, 400)}, None
    if field == "creator":
        name, _, link = raw.partition("|")
        name = clip(name, 60)
        if clear or not name:
            return None, "Creator name can't be empty."
        ch = {"creator": name}
        if link.strip():
            lk = norm_link(link)
            if lk is None:
                return None, "Invalid link. Use https://… or @username."
            ch["creator_link"] = lk
        return ch, None
    if field == "promo":
        if clear:
            return {"promo": ""}, None
        lk = norm_link(raw)
        if lk is None:
            return None, "Send a link starting with https:// or @channel (or - to remove)."
        return {"promo": lk}, None
    if field == "timer":
        m = re.match(r"^\d+", raw)
        if not m:
            return None, "Send the seconds as a number, e.g. 20."
        t = int(m.group())
        if not 5 <= t <= 600:
            return None, "Timer must be between 5 and 600 seconds."
        return {"timer": t}, None
    return None, "Unknown field."


# ───────────────────────────── views ─────────────────────────────
def home_kb():
    return Kb([[Btn("➕ Create Quiz", callback_data="m:new")],
               [Btn("📚 My Quizzes", callback_data="m:my:0"), Btn("❓ Help", callback_data="m:help")]])


HOME_TEXT = ("👋 <b>Quiz Maker Bot</b>\n\nCreate quizzes from text, bulk files or forwarded polls, "
             "then play them here or in any group.\n\n💡 Tip: forward quiz polls or paste questions — I'll build the quiz.")

FORMAT_TEXT = (
    "📄 <b>Official format</b> (blank line between questions)\n\n<pre>"
    "Title: Physics Test\nDescription: Chapter 1 - Motion\nCreator: Mannu | @username\n"
    "Promo: @mychannel\nTimer: 20\n\n"
    "Q1. SI unit of force?\nA) Joule\nB) Newton ✅\nC) Watt\nD) Pascal\nExplanation: F = m × a\n\n"
    "Q2. Speed of light?\nA) 3×10^8 m/s\nB) 3×10^6 m/s\nAnswer: A</pre>\n"
    "• Mark the right option with ✅ or add an <code>Answer:</code> line (letter, number or text)\n"
    "• Header lines are optional\n• 2–10 options per question\n• You can also upload a .txt file"
)

COLLECT_HELP = ("📥 <b>Send questions now</b>\n\n• Paste text in the official format (bulk OK)\n"
                "• Upload a .txt file\n• Forward / send <b>quiz-type polls</b>\n\nTap ✅ Done when finished.")

KB_COLLECT = Kb([[Btn("✅ Done", callback_data="done"), Btn("↩️ Undo", callback_data="undo")],
                 [Btn("📄 Format", callback_data="c:fmt"), Btn("❌ Cancel", callback_data="cancel")]])

HELP = (
    "<b>📖 Commands</b>\n\n"
    "<b>Create</b>\n/new – create a quiz\n/done – finish adding questions\n/undo – remove last question\n/cancel – cancel\n\n"
    "<b>Edit current quiz</b>\n/title · /description · /creator · /promo · /timer\n/shuffle · /qshuffle · /oshuffle\n\n"
    "<b>Play</b>\n/myquizzes – saved quizzes\n/play <code>code</code> – start a quiz in this chat\n"
    "/pause · /resume · /stop\n\n"
    "Pause takes effect after the current question."
)


def create_kb():
    return Kb([[Btn("✍️ Text / Bulk (official format)", callback_data="c:text")],
               [Btn("📤 Forward Polls", callback_data="c:poll")],
               [Btn("🧾 Step-by-step Form", callback_data="c:form")],
               [Btn("📄 See Official Format", callback_data="c:fmt")],
               [Btn("🏠 Menu", callback_data="m:home")]])


def link_buttons(quiz):
    row = []
    if quiz["creator"]:
        label = f"👤 {clip(quiz['creator'], 28)}"
        row.append(Btn(label, url=quiz["creator_link"]) if quiz["creator_link"] else Btn(label, callback_data="noop"))
    if quiz["promo"]:
        row.append(Btn("📢 Join / Promo", url=quiz["promo"]))
    return row


def card_text(q):
    n = len(q["questions"])
    t = f"📝 <b>{esc(q['title'] or 'Untitled Quiz')}</b>\n"
    if q["description"]:
        t += f"{esc(q['description'])}\n"
    t += (f"\n👤 {esc(q['creator'])}\n❓ {n} question{'s' if n != 1 else ''} · ⏱ {q['timer']}s each\n"
          f"🔀 Shuffle: {SHUF_LABEL[(q['shuffle_q'], q['shuffle_o'])]}\n"
          f"🆔 <code>{q['code']}</code> · ▶️ {q['plays']} plays")
    return t


def card_kb(q, viewer_id, bot_username):
    c = q["code"]
    link = f"https://t.me/{bot_username}?start=q_{c}"
    rows = [
        [Btn("▶️ Start Here", callback_data=f"play:{c}"),
         Btn("👥 Start in Group", url=f"https://t.me/{bot_username}?startgroup=q_{c}")],
        [Btn("✅ Saved" if has_saved(viewer_id, q) else "💾 Save to My Quizzes", callback_data=f"savec:{c}"),
         Btn("📤 Share", url=f"https://t.me/share/url?url={quote(link)}&text={quote(q['title'] or 'Quiz')}")],
    ]
    lr = link_buttons(q)
    if lr:
        rows.append(lr)
    if q["owner_id"] == viewer_id:
        rows.append([Btn("✏️ Edit", callback_data=f"e:{c}"), Btn("👁 Preview", callback_data=f"pv:{c}")])
    rows.append([Btn("📚 My Quizzes", callback_data="m:my:0"), Btn("🏠 Menu", callback_data="m:home")])
    return Kb(rows)


def panel_text(q):
    n = len(q["questions"])
    return (f"✏️ <b>Edit quiz</b> · <code>{q['code']}</code>\n\n📝 <b>{esc(q['title'] or 'Untitled')}</b>\n"
            f"{esc(q['description']) or '<i>no description</i>'}\n\n👤 {esc(q['creator'])} · ❓ {n} questions\n"
            f"🔗 Promo: {('<code>' + esc(q['promo']) + '</code>') if q['promo'] else '—'}")


def panel_kb(q):
    c = q["code"]
    onoff = lambda v: "On" if v else "Off"  # noqa: E731
    return Kb([
        [Btn("✏️ Title", callback_data=f"ef:{c}:title"), Btn("📝 Description", callback_data=f"ef:{c}:description")],
        [Btn("👤 Creator name", callback_data=f"ef:{c}:creator"), Btn("🔗 Promo link", callback_data=f"ef:{c}:promo")],
        [Btn(f"⏱ Timer: {q['timer']}s", callback_data=f"ef:{c}:timer"),
         Btn(f"🔀 Shuffle: {SHUF_LABEL[(q['shuffle_q'], q['shuffle_o'])]}", callback_data=f"ef:{c}:shuf")],
        [Btn(f"🔠 Options: {STYLES.get(q['opt_style'], STYLES['none'])[0]}", callback_data=f"ef:{c}:ostyle")],
        [Btn(f"🏷 Creator in question: {CIQ.get(q['creator_in_q'], 'Off')}", callback_data=f"ef:{c}:cinq")],
        [Btn(f"💡 Explanation: {onoff(q['show_expl'])}", callback_data=f"ef:{c}:expl"),
         Btn(f"#️⃣ Numbers: {onoff(q['q_numbers'])}", callback_data=f"ef:{c}:qnum")],
        [Btn("➕ Add questions", callback_data=f"ef:{c}:addq"), Btn("↩️ Undo last", callback_data=f"ef:{c}:undo")],
        [Btn("👁 Preview", callback_data=f"pv:{c}"), Btn("🗑 Delete", callback_data=f"del:{c}")],
        [Btn("⬅️ Back to quiz", callback_data=f"q:{c}")],
    ])


def timer_kb(code):
    vals = [10, 15, 20, 30, 45, 60, 90, 120]
    rows = [[Btn(f"{v}s", callback_data=f"et:{code}:{v}") for v in vals[i:i + 4]] for i in (0, 4)]
    rows.append([Btn("✍️ Custom", callback_data=f"ef:{code}:timer_custom"), Btn("⬅️ Back", callback_data=f"e:{code}")])
    return Kb(rows)


def my_view(uid, page):
    items = list_saved(uid, page)
    more = len(items) > PAGE
    items = items[:PAGE]
    if not items and page == 0:
        return "📚 <b>My Quizzes</b>\n\nNothing saved yet. Create a quiz and tap 💾 Save to My Quizzes.", \
            Kb([[Btn("➕ Create Quiz", callback_data="m:new")], [Btn("🏠 Menu", callback_data="m:home")]])
    rows = [[Btn(f"📝 {clip(t or 'Untitled', 32)} · {n}Q", callback_data=f"q:{c}")] for c, t, n in items]
    nav = []
    if page > 0:
        nav.append(Btn("⬅️ Prev", callback_data=f"m:my:{page - 1}"))
    if more:
        nav.append(Btn("Next ➡️", callback_data=f"m:my:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([Btn("➕ New", callback_data="m:new"), Btn("🏠 Menu", callback_data="m:home")])
    return f"📚 <b>My Quizzes</b> (page {page + 1})\nTap a quiz to open it.", Kb(rows)


def build_qtext(quiz, i, n, q):
    pre = f"[{i}/{n}] " if quiz["q_numbers"] else ""
    name, mode = quiz["creator"], quiz["creator_in_q"]
    if mode == "prefix" and name:
        pre = f"{name} • {pre}"
    txt = pre + q
    if mode == "suffix" and name:
        txt += f"\n— {name}"
    return txt


def prepare_questions(quiz):
    qs = [dict(q) for q in quiz["questions"]]
    if quiz["shuffle_q"]:
        random.shuffle(qs)
    label = STYLES.get(quiz["opt_style"], STYLES["none"])[1]
    out = []
    for q in qs:
        order = list(range(len(q["options"])))
        if quiz["shuffle_o"]:
            random.shuffle(order)
        texts, seen = [], set()
        for pos, oi in enumerate(order):
            t = clip(label(pos) + q["options"][oi], 100)
            while t in seen:  # Telegram rejects duplicate options
                t = clip(t, 99) + "\u200b"
            seen.add(t)
            texts.append(t)
        out.append({"q": q["q"], "options": texts, "correct": order.index(q["correct"]), "expl": q.get("expl", "")})
    return out


def preview_text(quiz):
    if not quiz["questions"]:
        return "No questions yet."
    p = prepare_questions(dict(quiz, shuffle_q=0, shuffle_o=0, questions=quiz["questions"][:1]))[0]
    t = f"👁 <b>Preview (question 1)</b>\n\n{esc(build_qtext(quiz, 1, len(quiz['questions']), p['q']))}\n\n"
    t += "\n".join(("🟢 " if i == p["correct"] else "⚪ ") + esc(o) for i, o in enumerate(p["options"]))
    if quiz["show_expl"] and p["expl"]:
        t += f"\n\n💡 {esc(p['expl'])}"
    t += f"\n\n⏱ {quiz['timer']}s"
    return t


async def show(q, text, kb=None):
    """Edit the message a callback came from (fallback: send a new one)."""
    try:
        await q.edit_message_text(text, parse_mode=HTML, reply_markup=kb)
    except BadRequest as e:
        if "not modified" in str(e).lower():
            return
        await q.message.chat.send_message(text, parse_mode=HTML, reply_markup=kb)


async def show_card(chat, context, quiz, viewer_id, q=None):
    text, kb = card_text(quiz), card_kb(quiz, viewer_id, context.bot.username)
    if q:
        await show(q, text, kb)
    else:
        await chat.send_message(text, parse_mode=HTML, reply_markup=kb)


# ───────────────────────────── quiz session (playing) ─────────────────────────────
class Session:
    def __init__(self, chat_id, thread_id, quiz, starter_id, private):
        self.chat_id, self.quiz, self.starter_id, self.private = chat_id, quiz, starter_id, private
        self.kw = {"message_thread_id": thread_id} if thread_id else {}
        self.questions = prepare_questions(quiz)
        self.timer = quiz["timer"]
        self.idx = 0
        self.asked = 0
        self.paused = False
        self.stopped = False
        self.cur_msg = None
        self.scores = {}
        self.early = asyncio.Event()
        self.task = None


SESS = {}   # chat_id -> Session
POLLS = {}  # poll_id -> {"s","correct","t0","answered"}


async def launch(bot, chat_id, thread_id, quiz, starter_id, private):
    if chat_id in SESS:
        return False, "⚠️ A quiz is already running here. Use /stop first."
    if not quiz["questions"]:
        return False, "⚠️ This quiz has no questions."
    s = Session(chat_id, thread_id, quiz, starter_id, private)
    SESS[chat_id] = s
    update_quiz(quiz["code"], plays=quiz["plays"] + 1)
    s.task = asyncio.create_task(run(s, bot))
    return True, ""


def ctl_kb(quiz):
    rows = [[Btn("⏸ Pause", callback_data="ctl:pause"), Btn("▶️ Resume", callback_data="ctl:resume"),
             Btn("⏹ Stop", callback_data="ctl:stop")]]
    lr = link_buttons(quiz)
    if lr:
        rows.insert(0, lr)
    return Kb(rows)


async def run(s, bot):
    try:
        q = s.quiz
        t = f"📝 <b>{esc(q['title'] or 'Quiz')}</b>\n"
        if q["description"]:
            t += esc(q["description"]) + "\n"
        t += (f"\n👤 {esc(q['creator'])}\n❓ {len(s.questions)} questions · ⏱ {s.timer}s each\n"
              f"🔀 {SHUF_LABEL[(q['shuffle_q'], q['shuffle_o'])]}\n\n⏳ Starting in 4 seconds…")
        await bot.send_message(s.chat_id, t, parse_mode=HTML, reply_markup=ctl_kb(q), **s.kw)
        await asyncio.sleep(4)
        while s.idx < len(s.questions) and not s.stopped:
            if s.paused:
                await bot.send_message(s.chat_id, "⏸ <b>Quiz paused.</b> Tap ▶️ Resume or send /resume.", parse_mode=HTML, **s.kw)
                while s.paused and not s.stopped:
                    await asyncio.sleep(0.5)
                if s.stopped:
                    break
                await bot.send_message(s.chat_id, "▶️ Resuming…", **s.kw)
                await asyncio.sleep(2)
            await ask_one(s, bot)
            s.idx += 1
        await finish(s, bot)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("quiz runner crashed")
        try:
            await bot.send_message(s.chat_id, "⚠️ The quiz stopped because of an error.", **s.kw)
        except TelegramError:
            pass
    finally:
        if SESS.get(s.chat_id) is s:
            del SESS[s.chat_id]
        for pid in [p for p, v in POLLS.items() if v["s"] is s]:
            POLLS.pop(pid, None)


async def ask_one(s, bot):
    q, n, quiz = s.questions[s.idx], len(s.questions), s.quiz
    full = build_qtext(quiz, s.idx + 1, n, q["q"])
    try:
        if len(full) > 300:
            await bot.send_message(s.chat_id, esc(full[:3900]), parse_mode=HTML, **s.kw)
            full = (f"[{s.idx + 1}/{n}] " if quiz["q_numbers"] else "") + "⬆️ Choose the correct answer"
        expl = q["expl"] if quiz["show_expl"] and q["expl"] else None
        msg = await bot.send_poll(
            s.chat_id, question=full, options=q["options"], type=Poll.QUIZ, correct_option_id=q["correct"],
            is_anonymous=False, open_period=s.timer, explanation=expl, **s.kw)
    except TelegramError as e:
        log.warning("send_poll failed: %s", e)
        await bot.send_message(s.chat_id, f"⚠️ Skipped question {s.idx + 1}: {esc(str(e))[:120]}", parse_mode=HTML, **s.kw)
        return
    s.cur_msg = msg.message_id
    s.asked += 1
    s.early.clear()
    POLLS[msg.poll.id] = {"s": s, "correct": q["correct"], "t0": time.monotonic(), "answered": set()}
    end = time.monotonic() + s.timer + 1.5
    while time.monotonic() < end and not s.stopped:
        if s.private and s.early.is_set():  # solo play: move on right after the answer
            await asyncio.sleep(2.5 if expl else 1.2)
            break
        await asyncio.sleep(0.3)
    await asyncio.sleep(0.5)
    POLLS.pop(msg.poll.id, None)


async def close_poll(bot, s):
    if s.cur_msg:
        try:
            await bot.stop_poll(s.chat_id, s.cur_msg)
        except TelegramError:
            pass


async def finish(s, bot):
    quiz = s.quiz
    board = sorted(s.scores.values(), key=lambda r: (-r["correct"], r["time"]))
    t = ("🛑 <b>Quiz stopped</b>" if s.stopped else "🏁 <b>Quiz finished!</b>")
    t += f"\n📝 {esc(quiz['title'])}\n❓ Questions asked: {s.asked}\n\n"
    if not board:
        t += "Nobody answered 😴"
    elif s.private:
        r = board[0]
        t += f"🎯 Your score: <b>{r['correct']}/{s.asked}</b>  ({r['wrong']}❌)\n⏱ Time on correct answers: {r['time']:.1f}s"
    else:
        t += "🏆 <b>Leaderboard</b>\n"
        for i, r in enumerate(board[:25]):
            medal = MEDALS[i] if i < 3 else f"{i + 1}."
            t += f"{medal} {esc(clip(r['name'], 30))} — {r['correct']}✅ {r['wrong']}❌ · {r['time']:.1f}s\n"
        if len(board) > 25:
            t += f"…and {len(board) - 25} more"
    rows = []
    lr = link_buttons(quiz)
    if lr:
        rows.append(lr)
    rows.append([Btn("🔁 Play again", callback_data=f"again:{quiz['code']}"),
                 Btn("💾 Save to My Quizzes", callback_data=f"save:{quiz['code']}")])
    rows.append([Btn("➕ Add bot to your group", url=f"https://t.me/{bot.username}?startgroup=q_{quiz['code']}")])
    await bot.send_message(s.chat_id, t, parse_mode=HTML, reply_markup=Kb(rows), **s.kw)


async def on_poll_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    a = update.poll_answer
    info = POLLS.get(a.poll_id)
    if not info or not a.option_ids or a.user is None:
        return
    uid = a.user.id
    if uid in info["answered"]:
        return
    info["answered"].add(uid)
    s = info["s"]
    row = s.scores.setdefault(uid, {"name": a.user.full_name, "correct": 0, "wrong": 0, "time": 0.0})
    if a.option_ids[0] == info["correct"]:
        row["correct"] += 1
        row["time"] += time.monotonic() - info["t0"]
    else:
        row["wrong"] += 1
    if s.private:
        s.early.set()


async def is_ctrl(update, context, s=None):
    chat, user, msg = update.effective_chat, update.effective_user, update.effective_message
    if chat.type == ChatType.PRIVATE:
        return True
    if msg and msg.sender_chat and msg.sender_chat.id == chat.id:  # anonymous admin
        return True
    if user and s and user.id == s.starter_id:
        return True
    try:
        m = await context.bot.get_chat_member(chat.id, user.id)
        return m.status in ("administrator", "creator")
    except TelegramError:
        return False


def apply_ctl(s, act):
    if act == "pause":
        if s.paused:
            return "Already paused."
        s.paused = True
        return "⏸ Quiz will pause after the current question. /resume to continue."
    if act == "resume":
        if not s.paused:
            return "The quiz isn't paused."
        s.paused = False
        return "▶️ Resuming…"
    s.stopped, s.paused = True, False
    return "⏹ Stopping quiz…"


def ctl_cmd(act):
    async def h(update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat = update.effective_chat
        s = SESS.get(chat.id)
        if not s:
            await chat.send_message("No quiz is running here.")
            return
        if not await is_ctrl(update, context, s):
            await chat.send_message("🔒 Only group admins or the person who started the quiz can do that.")
            return
        await chat.send_message(apply_ctl(s, act))
        if act == "stop":
            await close_poll(context.bot, s)
    return h


async def start_here(update, context, quiz):
    chat, user = update.effective_chat, update.effective_user
    if not await is_ctrl(update, context):
        await chat.send_message("🔒 Only group admins can start a quiz.")
        return
    ok, err = await launch(context.bot, chat.id, thread_of(update.effective_message), quiz, user.id,
                           chat.type == ChatType.PRIVATE)
    if not ok:
        await chat.send_message(err)


# ───────────────────────────── creation flow ─────────────────────────────
def ensure_collect(ud, user):
    quiz = get_quiz(ud.get("cur")) if ud.get("state") == "collect" else None
    if quiz and quiz["owner_id"] == user.id:
        return quiz
    code = new_quiz(user)
    ud.update(cur=code, state="collect", after="card", queue=[])
    return get_quiz(code)


def begin_quiz(ud, user):
    old = get_quiz(ud.get("cur"))
    if old and old["owner_id"] == user.id and not old["saved"] and not old["questions"]:
        delete_quiz(old["code"])
    code = new_quiz(user)
    ud.update(cur=code, state=None, after="card", queue=[])
    return code


async def bulk_import(update, context, text):
    ud, user, chat = context.user_data, update.effective_user, update.effective_chat
    hdr, qs, errs = parse_quiz_text(text)
    if not qs and not hdr:
        await chat.send_message("❌ I couldn't find any question.\n" + "\n".join(errs[:5]) +
                                "\n\nSee the official format with /new → 📄", reply_markup=KB_COLLECT)
        return
    quiz = ensure_collect(ud, user)
    changes, notes = {}, []
    for k, v in hdr.items():
        ch, err = field_changes(k, v, quiz)
        if err:
            notes.append(f"{k}: {err}")
        else:
            changes.update(ch)
    allq = quiz["questions"] + qs
    if len(allq) > MAX_Q:
        allq = allq[:MAX_Q]
        notes.append(f"limit is {MAX_Q} questions per quiz")
    changes["questions"] = allq
    update_quiz(quiz["code"], **changes)
    t = f"✅ Added <b>{len(qs)}</b> question(s) — total <b>{len(allq)}</b>."
    if hdr and not notes:
        t += "\n🏷 Header details applied."
    bad = errs[:6] + notes
    if bad:
        t += "\n\n⚠️ Notes:\n" + "\n".join("• " + esc(e) for e in bad)
    t += "\n\nSend more, or tap Done."
    await chat.send_message(t, parse_mode=HTML, reply_markup=KB_COLLECT)


async def _status(bot, chat_id, ud):
    try:
        await asyncio.sleep(1.5)
    except asyncio.CancelledError:
        return
    quiz = get_quiz(ud.get("cur"))
    n = len(quiz["questions"]) if quiz else 0
    await bot.send_message(chat_id, f"✅ Questions in this quiz: <b>{n}</b>\nKeep sending, or tap Done.",
                           parse_mode=HTML, reply_markup=KB_COLLECT)


async def on_poll(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg, chat, ud = update.effective_message, update.effective_chat, context.user_data
    poll = msg.poll
    if poll.type != Poll.QUIZ:
        await chat.send_message("⚠️ Only <b>quiz-type</b> polls can be imported (they have a correct answer).", parse_mode=HTML)
        return
    if poll.correct_option_id is None:
        await chat.send_message("⚠️ Telegram didn't share the correct answer for this poll. "
                                "Forward it to me directly in this chat.")
        return
    quiz = ensure_collect(ud, update.effective_user)
    if len(quiz["questions"]) >= MAX_Q:
        await chat.send_message(f"⚠️ Limit is {MAX_Q} questions per quiz.")
        return
    qs = quiz["questions"] + [{
        "q": clip(poll.question, 1000), "options": [clip(o.text, 100) for o in poll.options],
        "correct": poll.correct_option_id, "expl": clip(poll.explanation or "", 200)}]
    update_quiz(quiz["code"], questions=qs)
    t = ud.get("status_task")
    if t and not t.done():
        t.cancel()
    ud["status_task"] = asyncio.create_task(_status(context.bot, chat.id, ud))


async def on_doc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc, chat = update.effective_message.document, update.effective_chat
    if not (doc.file_name or "").lower().endswith((".txt", ".md")):
        await chat.send_message("📄 Please send a .txt file in the official format.")
        return
    if doc.file_size and doc.file_size > 1_000_000:
        await chat.send_message("⚠️ File too large (max 1 MB).")
        return
    f = await doc.get_file()
    data = await f.download_as_bytearray()
    await bulk_import(update, context, bytes(data).decode("utf-8-sig", errors="replace"))


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ud, text = context.user_data, update.effective_message.text or ""
    st = ud.get("state") or ""
    if st.startswith("ask:"):
        await on_value(update, context, st[4:], text)
    elif st == "collect":
        await bulk_import(update, context, text)
    elif parse_quiz_text(text)[1]:
        await bulk_import(update, context, text)  # pasted quiz → auto-create
    else:
        await update.effective_chat.send_message("Use the menu 👇 — or paste questions in the official format.",
                                                 reply_markup=home_kb())


async def on_value(update, context, field, text):
    ud, chat, user = context.user_data, update.effective_chat, update.effective_user
    quiz = get_quiz(ud.get("cur"))
    if not quiz or quiz["owner_id"] != user.id:
        ud["state"] = None
        await chat.send_message("Session expired. Use /myquizzes or /new.")
        return
    ch, err = field_changes(field, text, quiz)
    if err:
        await chat.send_message(f"⚠️ {err}\nTry again or /cancel.")
        return
    update_quiz(quiz["code"], **ch)
    await next_step(update, context)


async def next_step(update, context):
    ud, chat = context.user_data, update.effective_chat
    queue = ud.get("queue") or []
    if queue:
        nxt = queue.pop(0)
        ud["state"] = "ask:" + nxt
        await chat.send_message(PROMPTS[nxt], parse_mode=HTML)
        return
    quiz = get_quiz(ud.get("cur"))
    after = ud.get("after", "panel")
    if after == "collect":
        ud["state"], ud["after"] = "collect", "card"
        await chat.send_message(COLLECT_HELP, parse_mode=HTML, reply_markup=KB_COLLECT)
    elif after == "card":
        ud["state"] = None
        await show_card(chat, context, quiz, update.effective_user.id)
    else:
        ud["state"] = None
        await chat.send_message(panel_text(quiz), parse_mode=HTML, reply_markup=panel_kb(quiz))


async def finalize(update, context):
    ud, chat, user = context.user_data, update.effective_chat, update.effective_user
    quiz = get_quiz(ud.get("cur"))
    if not quiz or quiz["owner_id"] != user.id or ud.get("state") != "collect":
        await chat.send_message("Nothing to finish. Start with /new.")
        return
    if not quiz["questions"]:
        await chat.send_message("Add at least one question first.")
        return
    ud["after"], ud["queue"] = "card", []
    if not quiz["title"]:
        ud["state"] = "ask:title"
        await chat.send_message(PROMPTS["title"] + "\n(or /skip)", parse_mode=HTML)
        return
    ud["state"] = None
    await show_card(chat, context, quiz, user.id)


async def cancel_flow(update, context):
    ud, chat, user = context.user_data, update.effective_chat, update.effective_user
    quiz = get_quiz(ud.get("cur"))
    ud.update(state=None, queue=[], after="panel")
    if quiz and quiz["owner_id"] == user.id and quiz["questions"]:
        await chat.send_message("❌ Cancelled. Your quiz so far:")
        await show_card(chat, context, quiz, user.id)
        return
    if quiz and quiz["owner_id"] == user.id and not quiz["saved"]:
        delete_quiz(quiz["code"])
    await chat.send_message("❌ Cancelled.", reply_markup=home_kb())


async def undo_flow(update, context):
    chat = update.effective_chat
    quiz = await cur_quiz(update, context)
    if not quiz:
        return
    qs = quiz["questions"]
    if not qs:
        await chat.send_message("No questions to undo.")
        return
    last = qs.pop()
    update_quiz(quiz["code"], questions=qs)
    await chat.send_message(f"↩️ Removed: <i>{esc(clip(last['q'], 80))}</i>\nQuestions left: <b>{len(qs)}</b>",
                            parse_mode=HTML, reply_markup=KB_COLLECT if context.user_data.get("state") == "collect" else None)


async def cur_quiz(update, context):
    chat, user = update.effective_chat, update.effective_user
    if chat.type != ChatType.PRIVATE:
        await chat.send_message("⚙️ Edit commands work in my private chat.")
        return None
    quiz = get_quiz(context.user_data.get("cur"))
    if not quiz or quiz["owner_id"] != user.id:
        await chat.send_message("No quiz selected. Use /new, or open one from /myquizzes → ✏️ Edit.")
        return None
    return quiz


# ───────────────────────────── commands ─────────────────────────────
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat, msg = update.effective_chat, update.effective_message
    args = context.args
    if args and args[0].startswith("q_"):
        quiz = get_quiz(args[0][2:])
        if not quiz:
            await msg.reply_text("❌ Quiz not found (it may have been deleted).")
        elif chat.type == ChatType.PRIVATE:
            await show_card(chat, context, quiz, update.effective_user.id)
        else:
            await start_here(update, context, quiz)
        return
    if chat.type != ChatType.PRIVATE:
        await msg.reply_text("👋 I'm a quiz bot! Admins can start a quiz here with /play <code>.\n"
                             "Create quizzes in my private chat.",
                             reply_markup=Kb([[Btn("📩 Open DM", url=f"https://t.me/{context.bot.username}")]]))
        return
    await msg.reply_text(HOME_TEXT, parse_mode=HTML, reply_markup=home_kb())


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_chat.send_message(HELP, parse_mode=HTML)


async def new_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != ChatType.PRIVATE:
        await update.effective_chat.send_message("Create quizzes in my private chat 👇",
                                                 reply_markup=Kb([[Btn("📩 Open DM", url=f"https://t.me/{context.bot.username}")]]))
        return
    await update.effective_chat.send_message("➕ <b>Create a quiz</b> — choose a method:", parse_mode=HTML, reply_markup=create_kb())


async def myquizzes_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text, kb = my_view(update.effective_user.id, 0)
    await update.effective_chat.send_message(text, parse_mode=HTML, reply_markup=kb)


async def play_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    code = (context.args[0] if context.args else "").strip().lower().removeprefix("q_")
    if not code:
        if chat.type == ChatType.PRIVATE:
            await myquizzes_cmd(update, context)
        else:
            await chat.send_message("Usage: /play <quiz code>")
        return
    quiz = get_quiz(code)
    if not quiz:
        await chat.send_message("❌ Quiz not found.")
        return
    await start_here(update, context, quiz)


async def done_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await finalize(update, context)


async def undo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await undo_flow(update, context)


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cancel_flow(update, context)


async def skip_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ud = context.user_data
    st = ud.get("state") or ""
    if not st.startswith("ask:"):
        await update.effective_chat.send_message("Nothing to skip.")
        return
    if st == "ask:title":
        quiz = get_quiz(ud.get("cur"))
        if quiz and not quiz["title"]:
            update_quiz(quiz["code"], title="Untitled Quiz")
    await next_step(update, context)


def field_cmd(field):
    async def h(update: Update, context: ContextTypes.DEFAULT_TYPE):
        quiz = await cur_quiz(update, context)
        if not quiz:
            return
        chat, arg, ud = update.effective_chat, cmd_arg(update), context.user_data
        if field == "timer" and not arg:
            await chat.send_message("⏱ Pick a timer for all questions:", reply_markup=timer_kb(quiz["code"]))
            return
        if arg:
            ch, err = field_changes(field, arg, quiz)
            if err:
                await chat.send_message("⚠️ " + err)
                return
            update_quiz(quiz["code"], **ch)
            quiz = get_quiz(quiz["code"])
            await chat.send_message(panel_text(quiz), parse_mode=HTML, reply_markup=panel_kb(quiz))
        else:
            ud.update(state="ask:" + field, after="panel", queue=[])
            await chat.send_message(PROMPTS[field], parse_mode=HTML)
    return h


def shuffle_cmd(kind):
    async def h(update: Update, context: ContextTypes.DEFAULT_TYPE):
        quiz = await cur_quiz(update, context)
        if not quiz:
            return
        a, b = quiz["shuffle_q"], quiz["shuffle_o"]
        arg = cmd_arg(update).lower()
        if kind == "q":
            a = 1 - a
        elif kind == "o":
            b = 1 - b
        else:
            a, b = SHUF_ARGS.get(arg) or next_shuf(quiz)
        update_quiz(quiz["code"], shuffle_q=a, shuffle_o=b)
        await update.effective_chat.send_message(f"🔀 Shuffle: <b>{SHUF_LABEL[(a, b)]}</b>", parse_mode=HTML)
    return h


# ───────────────────────────── callbacks ─────────────────────────────
def owned(code, uid):
    q = get_quiz(code)
    return q if q and q["owner_id"] == uid else None


async def on_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    d, user, ud = q.data or "", q.from_user, context.user_data
    chat = update.effective_chat
    uid = user.id

    if d == "noop":
        await q.answer("This creator has no public link.")
        return

    # ---- session controls
    if d.startswith("ctl:"):
        s = SESS.get(chat.id)
        if not s:
            await q.answer("No quiz is running.", show_alert=True)
            return
        if not await is_ctrl(update, context, s):
            await q.answer("Only admins / the starter can do that.", show_alert=True)
            return
        act = d[4:]
        await q.answer(apply_ctl(s, act)[:190], show_alert=False)
        if act == "stop":
            await close_poll(context.bot, s)
        return

    await q.answer()

    # ---- menus
    if d == "m:home":
        await show(q, HOME_TEXT, home_kb())
    elif d == "m:help":
        await show(q, HELP, Kb([[Btn("🏠 Menu", callback_data="m:home")]]))
    elif d == "m:new":
        await show(q, "➕ <b>Create a quiz</b> — choose a method:", create_kb())
    elif d.startswith("m:my:"):
        text, kb = my_view(uid, int(d.split(":")[2]))
        await show(q, text, kb)

    # ---- creation entry points
    elif d == "c:fmt":
        await chat.send_message(FORMAT_TEXT, parse_mode=HTML)
    elif d in ("c:text", "c:poll"):
        code = begin_quiz(ud, user)
        ud.update(state="collect", after="card", queue=[])
        hint = ("✍️ <b>Text / Bulk</b>\nPaste your questions in the official format (see /new → 📄), "
                "or upload a .txt file.\n\n" if d == "c:text" else
                "📤 <b>Forward Polls</b>\nForward or send <b>quiz-type polls</b> to me — I'll collect them all into a new quiz.\n\n")
        await chat.send_message(hint + "Tap ✅ Done when finished.", parse_mode=HTML, reply_markup=KB_COLLECT)
    elif d == "c:form":
        begin_quiz(ud, user)
        ud.update(state="ask:title", after="collect", queue=["description", "creator"])
        await chat.send_message("🧾 <b>Step-by-step form</b>\n\n" + PROMPTS["title"], parse_mode=HTML)
    elif d == "done":
        await finalize(update, context)
    elif d == "cancel":
        await cancel_flow(update, context)
    elif d == "undo":
        await undo_flow(update, context)

    # ---- quiz card / play / save
    elif d.startswith("q:"):
        quiz = get_quiz(d[2:])
        if not quiz:
            await show(q, "❌ Quiz not found.", Kb([[Btn("🏠 Menu", callback_data="m:home")]]))
            return
        if quiz["owner_id"] == uid:
            ud["cur"] = quiz["code"]
        await show_card(chat, context, quiz, uid, q)
    elif d.startswith("play:") or d.startswith("again:"):
        quiz = get_quiz(d.split(":")[1])
        if not quiz:
            await chat.send_message("❌ Quiz not found.")
        else:
            await start_here(update, context, quiz)
    elif d.startswith("save:") or d.startswith("savec:"):
        quiz = get_quiz(d.split(":")[1])
        if not quiz:
            await chat.send_message("❌ Quiz not found.")
            return
        was = has_saved(uid, quiz)
        save_quiz(user, quiz)
        if d.startswith("savec:"):
            await show_card(chat, context, get_quiz(quiz["code"]), uid, q)
        if not was:
            await chat.send_message("💾 Saved to <b>My Quizzes</b>! Open it with /myquizzes.", parse_mode=HTML)

    # ---- owner-only: edit panel
    elif d.startswith(("e:", "ef:", "et:", "pv:", "del:", "delok:")):
        parts = d.split(":")
        quiz = owned(parts[1], uid)
        if not quiz:
            await chat.send_message("🔒 Only the quiz owner can edit it. Tap 💾 Save to get your own copy.")
            return
        ud["cur"] = quiz["code"]
        code = quiz["code"]
        if parts[0] == "e":
            await show(q, panel_text(quiz), panel_kb(quiz))
        elif parts[0] == "pv":
            await chat.send_message(preview_text(quiz), parse_mode=HTML)
        elif parts[0] == "del":
            await show(q, f"🗑 Delete <b>{esc(quiz['title'])}</b> permanently?",
                       Kb([[Btn("Yes, delete", callback_data=f"delok:{code}"), Btn("Cancel", callback_data=f"e:{code}")]]))
        elif parts[0] == "delok":
            delete_quiz(code)
            ud["cur"] = None
            await show(q, "🗑 Quiz deleted.", Kb([[Btn("📚 My Quizzes", callback_data="m:my:0"), Btn("🏠 Menu", callback_data="m:home")]]))
        elif parts[0] == "et":
            update_quiz(code, timer=int(parts[2]))
            quiz = get_quiz(code)
            await show(q, panel_text(quiz), panel_kb(quiz))
        else:  # ef
            await edit_field(update, context, q, quiz, parts[2])


async def edit_field(update, context, q, quiz, field):
    ud, code, chat = context.user_data, quiz["code"], update.effective_chat
    if field in ("title", "description", "creator", "promo", "timer_custom"):
        f = "timer" if field == "timer_custom" else field
        ud.update(state="ask:" + f, after="panel", queue=[])
        await chat.send_message(PROMPTS[f], parse_mode=HTML)
        return
    if field == "timer":
        await show(q, "⏱ Pick a timer for <b>all questions</b>:", timer_kb(code))
        return
    if field == "addq":
        ud.update(state="collect", after="card", queue=[])
        await chat.send_message(COLLECT_HELP, parse_mode=HTML, reply_markup=KB_COLLECT)
        return
    if field == "shuf":
        a, b = next_shuf(quiz)
        update_quiz(code, shuffle_q=a, shuffle_o=b)
    elif field == "ostyle":
        keys = list(STYLES)
        cur = quiz["opt_style"] if quiz["opt_style"] in keys else "none"
        update_quiz(code, opt_style=keys[(keys.index(cur) + 1) % len(keys)])
    elif field == "cinq":
        keys = list(CIQ)
        cur = quiz["creator_in_q"] if quiz["creator_in_q"] in keys else "off"
        update_quiz(code, creator_in_q=keys[(keys.index(cur) + 1) % len(keys)])
    elif field == "expl":
        update_quiz(code, show_expl=1 - quiz["show_expl"])
    elif field == "qnum":
        update_quiz(code, q_numbers=1 - quiz["q_numbers"])
    elif field == "undo":
        qs = quiz["questions"]
        if qs:
            qs.pop()
            update_quiz(code, questions=qs)
    quiz = get_quiz(code)
    await show(q, panel_text(quiz), panel_kb(quiz))


async def on_error(update, context: ContextTypes.DEFAULT_TYPE):
    log.error("Update caused error", exc_info=context.error)


# ───────────────────────────── main ─────────────────────────────
CMDS = [("start", "Open menu"), ("new", "Create a quiz"), ("myquizzes", "My saved quizzes"),
        ("play", "Start a quiz by code"), ("done", "Finish creating"), ("undo", "Remove last question"),
        ("title", "Set quiz title"), ("description", "Set description"), ("creator", "Set creator name"),
        ("promo", "Set promotion link"), ("timer", "Set timer for all questions"), ("shuffle", "Change shuffle type"),
        ("qshuffle", "Toggle question shuffle"), ("oshuffle", "Toggle option shuffle"),
        ("pause", "Pause running quiz"), ("resume", "Resume quiz"), ("stop", "Stop quiz"),
        ("cancel", "Cancel"), ("help", "All commands")]


async def post_init(app: Application):
    await app.bot.set_my_commands([BotCommand(c, d) for c, d in CMDS])
    with _db:  # drop abandoned empty drafts
        _db.execute("DELETE FROM quizzes WHERE saved=0 AND questions='[]' AND created<?", (time.time() - 86400,))
    log.info("Bot ready as @%s", app.bot.username)


def main():
    if not TOKEN:
        raise SystemExit("Set the BOT_TOKEN environment variable first.")
    app = Application.builder().token(TOKEN).post_init(post_init).build()
    for name, fn in [("start", start_cmd), ("help", help_cmd), ("new", new_cmd), ("myquizzes", myquizzes_cmd),
                     ("play", play_cmd), ("done", done_cmd), ("undo", undo_cmd), ("cancel", cancel_cmd),
                     ("skip", skip_cmd)]:
        app.add_handler(CommandHandler(name, fn))
    app.add_handler(CommandHandler("title", field_cmd("title")))
    app.add_handler(CommandHandler(["description", "desc"], field_cmd("description")))
    app.add_handler(CommandHandler("creator", field_cmd("creator")))
    app.add_handler(CommandHandler("promo", field_cmd("promo")))
    app.add_handler(CommandHandler("timer", field_cmd("timer")))
    app.add_handler(CommandHandler("shuffle", shuffle_cmd("both")))
    app.add_handler(CommandHandler("qshuffle", shuffle_cmd("q")))
    app.add_handler(CommandHandler("oshuffle", shuffle_cmd("o")))
    app.add_handler(CommandHandler("pause", ctl_cmd("pause")))
    app.add_handler(CommandHandler("resume", ctl_cmd("resume")))
    app.add_handler(CommandHandler("stop", ctl_cmd("stop")))
    app.add_handler(CallbackQueryHandler(on_cb))
    app.add_handler(PollAnswerHandler(on_poll_answer))
    private = filters.ChatType.PRIVATE
    app.add_handler(MessageHandler(private & filters.POLL, on_poll))
    app.add_handler(MessageHandler(private & filters.Document.ALL, on_doc))
    app.add_handler(MessageHandler(private & filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
