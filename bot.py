import os
import time
import random
import html
import json
from datetime import datetime, timezone, timedelta
import io

import requests
import telebot
from telebot import types
from PIL import Image, ImageDraw, ImageFont

# =============== CONFIG ===============

TELEGRAM_BOT_TOKEN = "8049402641:AAEZZdPKSNYkn8lPW7qSekU3MkhzaQOKrrQ"

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "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")
# Note: For OpenRouter keys, you may need to change OPENAI_API_URL to OpenRouter's URL if required by the model, but I left it unchanged for now.
OPENAI_API_URL = "https://api.openai.com/v1/chat/completions"
TTS_API_URL = "https://api.openai.com/v1/audio/speech"

WEATHER_API_KEY = os.getenv("WEATHER_API_KEY", "9d762a315572ebb2eee51a587a910ca2")

OWNER_USERNAME = "@your_username_here"          # <- apna @username
START_PHOTO_URL = "https://pin.it/eJbbcSH1S"  # <- image URL ya file_id

if TELEGRAM_BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN_HERE":
    raise ValueError("Please set TELEGRAM_BOT_TOKEN in bot.py")

if not OPENAI_API_KEY or OPENAI_API_KEY == "YOUR_OPENAI_API_KEY_HERE":
    raise ValueError("Please set OPENAI_API_KEY (env or bot.py)")

# =============== BOT INIT ===============

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN, parse_mode=None)

bot_info = bot.get_me()
BOT_ID = bot_info.id
BOT_USERNAME = (bot_info.username or "").lower()
BOT_NAME_WORDS = (bot_info.first_name or "").lower().split()
BOT_DISPLAY_NAME = bot_info.first_name or bot_info.username or "ZeroTwo"
BASE_BOT_NAME = BOT_DISPLAY_NAME

# =============== GLOBAL STATE ===============

# AI chat history
chat_histories = {}  # {chat_id: [ {"role":..., "content":...}, ... ]}
MAX_HISTORY_MESSAGES = 4

# AI cooldown (anti-spam per user)
COOLDOWN_SECONDS = 10
last_user_request = {}  # {user_id: last_ts}

# Filters & AFK
filters_per_chat = {}  # {chat_id: {keyword: payload}}
afk_status = {}        # {user_id: {"reason","since","username","name","sticker_id"}}

# Stickers pool for fun reply
sticker_pool = []      # list of sticker file_ids

# Warn & bans
warns_per_chat = {}    # {chat_id: {user_id: warn_count}}
banlist_per_chat = {}  # {chat_id: {user_id: {...}}}

# Username & name maps
usernames_per_chat = {}  # {chat_id: {username_lower: user_id}}
names_per_chat = {}      # {chat_id: {name_lower: user_id}}

# TTS cooldown
tts_last_use = {}     # {user_id: last_ts}
TTS_COOLDOWN = 20

# Monthly active user tracking (name/description ko ab change nahi karenge)
USERS_FILE = "users.json"
user_last_seen = {}   # {user_id: last_ts}


# =============== HELPER FUNCTIONS ===============

def is_mentioned_by_text(message_text: str) -> bool:
    if not message_text:
        return False
    text = message_text.lower()
    if BOT_USERNAME and f"@{BOT_USERNAME}" in text:
        return True
    for word in BOT_NAME_WORDS:
        if word and word in text:
            return True
    return False


def is_message_for_bot(message) -> bool:
    if is_mentioned_by_text(message.text or ""):
        return True

    if message.entities:
        for ent in message.entities:
            if ent.type == "mention":
                t = message.text[ent.offset:ent.offset + ent.length].lower()
                if BOT_USERNAME and t == f"@{BOT_USERNAME}":
                    return True
            elif ent.type == "text_mention":
                if ent.user and ent.user.id == BOT_ID:
                    return True

    if message.reply_to_message and message.reply_to_message.from_user:
        if message.reply_to_message.from_user.id == BOT_ID:
            return True

    return False


def detect_greeting_type(text: str) -> str:
    if not text:
        return ""
    t = text.lower()
    morning_keywords = [
        "good morning", "gm", "g.m", "suprabhat", "subah bakhair",
        "shubho shokal", "shuvo shokal",
    ]
    night_keywords = [
        "good night", "gn", "g.n", "shubh ratri",
        "shubho raatri", "shuvo ratri",
    ]
    for k in morning_keywords:
        if k in t:
            return "morning"
    for k in night_keywords:
        if k in t:
            return "night"
    return ""


def parse_duration_to_seconds(text: str):
    if not text:
        return None
    text = text.strip().lower()
    num_str = ""
    unit_str = ""
    for ch in text:
        if ch.isdigit():
            num_str += ch
        else:
            unit_str += ch
    if not num_str:
        return None
    amount = int(num_str)
    if amount <= 0:
        return None

    if not unit_str or unit_str in ("s", "sec", "secs", "second", "seconds"):
        return amount
    if unit_str in ("m", "min", "mins", "minute", "minutes"):
        return amount * 60
    if unit_str in ("h", "hr", "hrs", "hour", "hours"):
        return amount * 60 * 60
    if unit_str in ("d", "day", "days"):
        return amount * 60 * 60 * 24
    if unit_str in ("w", "week", "weeks"):
        return amount * 60 * 60 * 24 * 7
    if unit_str in ("y", "yr", "yrs", "year", "years"):
        return amount * 60 * 60 * 24 * 365
    return None


def format_duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds}s" if seconds else f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        s = f"{hours}h"
        if minutes:
            s += f" {minutes}m"
        return s
    days, hours = divmod(hours, 24)
    if days < 365:
        s = f"{days}d"
        if hours:
            s += f" {hours}h"
        return s
    years, days = divmod(days, 365)
    s = f"{years}y"
    if days:
        s += f" {days}d"
    return s


def find_user_by_name(chat_id: int, name_query: str):
    name_query = (name_query or "").strip().lower()
    if not name_query:
        return None, "No name given."

    name_map = names_per_chat.get(chat_id, {})
    if not name_map:
        return None, "I don't have any names cached for this group yet."

    if name_query in name_map:
        return name_map[name_query], None

    matches = [(name, uid) for name, uid in name_map.items() if name_query in name]
    if not matches:
        return None, "No user found in this group matching that name. Try reply, @username or ID."

    if len(matches) > 1:
        return None, "Multiple users match this name. Please use reply/@username/ID or a more specific name."

    return matches[0][1], None


def is_user_admin(chat_id, user_id) -> bool:
    try:
        member = bot.get_chat_member(chat_id, user_id)
        return member.status in ("administrator", "creator")
    except Exception as e:
        print(f"[admin check error] {e}")
        return False


# =============== AI REPLY ===============

def get_ai_reply(chat_id: int, user_text: str, user_name: str) -> str:
    """Short, fast AI reply; follows user's language/mix."""
    history = chat_histories.get(chat_id, [])

    text_low = (user_text or "").lower()
    wants_long = any(
        key in text_low
        for key in [
            "detail", "details", "explain", "explanation",
            "batao detail", "batao detail me", "step by step",
            "long answer", "full form", "why", "kaise", "kya hai",
        ]
    )

    max_toks = 40 if not wants_long else 80

    system_instruction = (
        "You are Zero Two, a funny, friendly anime-themed Telegram chat bot.\n"
        "- DEFAULT: Always answer very short and to the point.\n"
        "- Normally reply in at most 1–2 short sentences. No long paragraphs, no big lists.\n"
        "- Only if the user clearly asks for detailed explanation (e.g. 'detail me batao', "
        "'explain', 'step by step', 'why/kaise/kya hai'), then you may write up to 4–5 sentences "
        "or a tiny list (max 3 points).\n"
        "- If the user asks for recommendations (anime, movies, series), give at most 3 titles "
        "with 1 short line each.\n"
        "- Persona: cute, slightly flirty anime girl (ladki), but respectful and PG-13.\n"
        "- Use light jokes/teasing when natural, but stay kind.\n"
        "- When using Hindi/Hinglish about yourself, use feminine forms like 'karti hoon', "
        "'gayi', 'boli', 'meri', not masculine.\n"
        "- If the user says 'roast me' / 'thoda roast kar', give ONE short, harmless roast "
        "about them or their message. No slurs or heavy abuse.\n"
        "- Never insult someone's race, religion, nationality, gender, sexuality or appearance.\n"
        "- In PRIVATE chats you reply to every message from the user.\n"
        "- In GROUP chats you reply only when users mention you or reply to you "
        "(except special greetings and filter replies handled by the code).\n"
        "- Always try to answer in the SAME LANGUAGE or language mix that the user used.\n"
        f"- You are talking to: {user_name or 'a user'}.\n"
    )

    messages = [
        {"role": "system", "content": system_instruction},
        *history,
        {"role": "user", "content": user_text},
    ]

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {OPENAI_API_KEY}",
    }

    data = {
        "model": "gpt-4o-mini",
        "messages": messages,
        "temperature": 0.5,
        "max_tokens": max_toks,
    }

    try:
        resp = requests.post(OPENAI_API_URL, headers=headers, json=data, timeout=20)
        resp.raise_for_status()
        result = resp.json()
        reply = result["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f"[OpenAI error] {e}")
        reply = "Sorry, I'm having trouble replying right now. Please try again later."

    history.append({"role": "user", "content": user_text})
    history.append({"role": "assistant", "content": reply})
    chat_histories[chat_id] = history[-MAX_HISTORY_MESSAGES:]

    return reply


# =============== WARN / BAN HELPERS ===============

def add_ban_record(chat_id: int, target_user, reason: str, by_user):
    name = " ".join(
        p for p in [target_user.first_name, target_user.last_name] if p
    ).strip() or "User"
    username = f"@{target_user.username}" if target_user.username else "(no username)"

    chat_bans = banlist_per_chat.setdefault(chat_id, {})
    chat_bans[target_user.id] = {
        "name": name,
        "username": username,
        "reason": reason,
        "by": by_user.id if by_user else None,
        "time": int(time.time()),
    }


# =============== TRANSLATE ===============

def translate_to_english(text: str) -> str:
    system = (
        "You are a translator. Translate the user's message into natural, simple English.\n"
        "Answer ONLY with the translation text, no extra explanation."
    )
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {OPENAI_API_KEY}",
    }
    data = {
        "model": "gpt-4o-mini",
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": text},
        ],
        "temperature": 0.3,
        "max_tokens": 80,
    }
    try:
        resp = requests.post(OPENAI_API_URL, headers=headers, json=data, timeout=20)
        resp.raise_for_status()
        result = resp.json()
        return result["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f"[translate error] {e}")
        return "Sorry, there was a problem while translating that text."


# =============== QUOTES & COMMAND HELP ===============

QUOTES = [
    "“Dream, dream, dream. Dreams transform into thoughts and thoughts result in action.” — A.P.J. Abdul Kalam",
    "“You have to dream before your dreams can come true.” — A.P.J. Abdul Kalam",
    "“If you want to shine like a sun, first burn like a sun.” — A.P.J. Abdul Kalam",
    "“Don’t read success stories, you will only get a message. Read failure stories, you will get some ideas to get success.” — A.P.J. Abdul Kalam",
    "“Arise, awake, and stop not till the goal is reached.” — Swami Vivekananda",
    "“Take risks in your life. If you win, you can lead; if you lose, you can guide.” — Swami Vivekananda",
    "“Talk to yourself at least once in a day, otherwise you may miss a meeting with an excellent person in this world.” — Swami Vivekananda",
    "“Strength does not come from physical capacity. It comes from an indomitable will.” — Mahatma Gandhi",
    "“The future depends on what you do today.” — Mahatma Gandhi",
    "“Life is like riding a bicycle. To keep your balance, you must keep moving.” — Albert Einstein",
    "“In the middle of difficulty lies opportunity.” — Albert Einstein",
    "“It always seems impossible until it’s done.” — Nelson Mandela",
    "“The best way to predict your future is to create it.” — Peter Drucker",
    "“Your time is limited, so don’t waste it living someone else’s life.” — Steve Jobs",
    "“If you are working on something exciting that you really care about, you don’t have to be pushed. The vision pulls you.” — Steve Jobs",
    "“Hardships often prepare ordinary people for an extraordinary destiny.” — C.S. Lewis",
    "“Believe you can and you’re halfway there.” — Theodore Roosevelt",
    "“Success is the sum of small efforts, repeated day in and day out.” — Robert Collier",
    "“You are never too old to set another goal or to dream a new dream.” — C.S. Lewis",
    "“Stars can’t shine without darkness.” — Unknown",
    "“Success is not final, failure is not fatal: it is the courage to continue that counts.” — Winston Churchill",
]


def get_random_quote() -> str:
    return random.choice(QUOTES)


COMMAND_HELP = {
    "start": "start - Start talking to me in DM and see my introduction.",
    "alive": "alive - Check if I am online and working.",
    "owner": "owner - Shows my owner's name or username.",
    "about": "about - Shows details about me and what I can do.",
    "help": "help - General help menu with a list of my features.",
    "info": "info - Get information about a user (reply to a user or use in DM for yourself).",
    "id": "id - Get user or group ID (reply to a user to get their ID).",
    "filter": "filter - Set an auto-reply filter in a group. Usage: reply to a message + /filter <keyword> OR /filter <keyword> <reply text>.",
    "filters": "filters - Show all active filters in the current group.",
    "stop": "stop - Remove a specific filter. Usage: /stop <keyword> or reply to the filtered message + /stop <keyword>.",
    "stopall": "stopall - Remove all filters in the current group.",
    "mute": "mute - Temporarily or permanently mute a user. Usage: reply + /mute [time] or /mute <user_id|@username|name> [time].",
    "unmute": "unmute - Unmute a user and allow them to speak again.",
    "ban": "ban - Ban a user from the group. Usage: reply + /ban [reason] or /ban <user_id|@username|name> [reason].",
    "unban": "unban - Unban a previously banned user. Usage: /unban <user_id|@username> or reply to an old message.",
    "kick": "kick - Kick a user from the group (ban + unban so they can rejoin).",
    "warn": "warn - Give a warning to a user. After 3 warnings the user is auto-banned from the group.",
    "unwarn": "unwarn - Remove one warning (or all warnings) from a user.",
    "warnlist": "warnlist - Show list of users who currently have warnings in the group.",
    "banlist": "banlist - Show the list of users banned by me in this group (recorded by the bot).",
    "admin": "admin - Promote a user to admin. Usage: reply to a user + /admin (only admins/owner).",
    "unadmin": "unadmin - Demote an admin back to normal user. Usage: reply + /unadmin (only admins/owner).",
    "admins": "admins - Show all admins and the owner of the group.",
    "tr": "tr - Translate any text to English. Usage: reply + /tr or /tr <text>.",
    "quote": "quote - Get a random motivational quote from famous people.",
    "weather": "weather - Get weather info and AQI for a city. Usage: /weather <city>.",
    "imagine": "imagine - Generate a short creative anime-style scene description for your prompt.",
    "time": "time - Show the current time in IST.",
}


def chunk_list(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


# =============== USER TRACKING (NO PROFILE CHANGE) ===============

def load_users():
    global user_last_seen
    if os.path.exists(USERS_FILE):
        try:
            with open(USERS_FILE, "r") as f:
                data = json.load(f)
            if isinstance(data, dict):
                user_last_seen = {int(k): float(v) for k, v in data.items()}
            elif isinstance(data, list):
                user_last_seen = {int(uid): 0.0 for uid in data}
            else:
                user_last_seen = {}
        except Exception as e:
            print(f"[users load error] {e}")
            user_last_seen = {}
    else:
        user_last_seen = {}


def save_users():
    try:
        with open(USERS_FILE, "w") as f:
            json.dump({str(k): v for k, v in user_last_seen.items()}, f)
    except Exception as e:
        print(f"[users save error] {e}")


def update_bot_profile_user_count():
    """Disabled: we don't touch profile name/description automatically."""
    return


def register_user_from_message(message):
    user = getattr(message, "from_user", None)
    chat = getattr(message, "chat", None)
    if not user or not chat:
        return

    user_id = user.id
    now = time.time()

    user_last_seen[user_id] = now

    if user.username:
        uname = user.username.lower()
        usernames_per_chat.setdefault(chat.id, {})[uname] = user_id

    display_name = " ".join(
        p for p in [user.first_name, user.last_name] if p
    ).strip()
    if display_name:
        names_per_chat.setdefault(chat.id, {})[display_name.lower()] = user_id

    try:
        save_users()
        update_bot_profile_user_count()
    except Exception as e:
        print(f"[register_user save/update error] {e}")


def track_user(messages):
    for msg in messages:
        try:
            register_user_from_message(msg)
        except Exception as e:
            print(f"[track_user error] {e}")


# =============== AQI HELPERS (OpenWeather) ===============

PM25_BREAKPOINTS = [
    (0.0, 12.0, 0, 50),
    (12.1, 35.4, 51, 100),
    (35.5, 55.4, 101, 150),
    (55.5, 150.4, 151, 200),
    (150.5, 250.4, 201, 300),
    (250.5, 500.4, 301, 500),
]

PM10_BREAKPOINTS = [
    (0, 54, 0, 50),
    (55, 154, 51, 100),
    (155, 254, 101, 150),
    (255, 354, 151, 200),
    (355, 424, 201, 300),
    (425, 604, 301, 500),
]


def _calc_aqi(conc: float, breakpoints):
    if conc is None:
        return None
    for Clow, Chigh, Ilow, Ihigh in breakpoints:
        if Clow <= conc <= Chigh:
            I = (Ihigh - Ilow) / (Chigh - Clow) * (conc - Clow) + Ilow
            return int(round(I))
    return None


def compute_us_aqi_pm25(conc: float):
    return _calc_aqi(conc, PM25_BREAKPOINTS)


def compute_us_aqi_pm10(conc: float):
    return _calc_aqi(conc, PM10_BREAKPOINTS)


def aqi_category(aqi: int) -> str:
    if aqi is None:
        return "Unknown"
    if aqi <= 50:
        return "Good"
    if aqi <= 100:
        return "Moderate"
    if aqi <= 150:
        return "Unhealthy for Sensitive Groups"
    if aqi <= 200:
        return "Unhealthy"
    if aqi <= 300:
        return "Very Unhealthy"
    return "Hazardous"


def get_us_aqi_from_components(components: dict):
    if not components:
        return None, None

    aqi_candidates = []

    pm25 = components.get("pm2_5")
    pm10 = components.get("pm10")

    if pm25 is not None:
        aqi_pm25 = compute_us_aqi_pm25(pm25)
        if aqi_pm25 is not None:
            aqi_candidates.append((aqi_pm25, "PM2.5"))

    if pm10 is not None:
        aqi_pm10 = compute_us_aqi_pm10(pm10)
        if aqi_pm10 is not None:
            aqi_candidates.append((aqi_pm10, "PM10"))

    if not aqi_candidates:
        return None, None

    best = max(aqi_candidates, key=lambda x: x[0])
    return best  # (aqi_value, pollutant_name)


# =============== MMF (sticker -> meme) HELPERS ===============

def _draw_text_on_image(img: Image.Image, text: str) -> Image.Image:
    img = img.convert("RGBA")
    draw = ImageDraw.Draw(img)

    base_size = max(20, img.height // 12)
    try:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", base_size)
    except Exception:
        font = ImageFont.load_default()

    max_width = int(img.width * 0.85)
    words = text.split()
    lines = []
    line = ""
    for w in words:
        test = (line + " " + w).strip()
        w_test, _ = draw.textsize(test, font=font)
        if w_test <= max_width:
            line = test
        else:
            if line:
                lines.append(line)
            line = w
    if line:
        lines.append(line)

    line_height = draw.textsize("Ay", font=font)[1] + 4
    block_height = line_height * len(lines)
    y = img.height - block_height - int(img.height * 0.05)

    for ln in lines:
        w_ln, _ = draw.textsize(ln, font=font)
        x = img.width // 2 - w_ln // 2
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                if dx == 0 and dy == 0:
                    continue
                draw.text((x + dx, y + dy), ln, font=font, fill="black")
        draw.text((x, y), ln, font=font, fill="white")
        y += line_height

    return img


# =============== AFK HELPER ===============

def send_afk_notice(chat_id: int, target_id: int, info: dict, reply_to_message_id: int = None):
    name = info.get("name") or f"User {target_id}"
    reason = info.get("reason") or ""
    since = info.get("since", time.time())
    sticker_id = info.get("sticker_id")

    elapsed = format_duration(time.time() - since)
    mention = f'<a href="tg://user?id={target_id}">{html.escape(name)}</a>'

    lines = [
        f"{mention} is currently AFK.",
        f"AFK for {elapsed}.",
    ]
    if reason:
        lines.append(f"Reason: {html.escape(reason)}")

    text = "\n".join(lines)

    try:
        bot.send_message(
            chat_id,
            text,
            parse_mode="HTML",
            reply_to_message_id=reply_to_message_id,
        )
    except Exception as e:
        print(f"[AFK notice send error] {e}")

    if sticker_id:
        try:
            bot.send_sticker(
                chat_id,
                sticker_id,
                reply_to_message_id=reply_to_message_id,
            )
        except Exception as e:
            print(f"[AFK sticker send error] {e}")


# =============== WELCOME NEW MEMBERS ===============

@bot.message_handler(content_types=["new_chat_members"])
def welcome_new_members(message):
    chat = message.chat
    chat_title = html.escape(chat.title or "this group")

    if BOT_USERNAME:
        add_url = f"https://t.me/{BOT_USERNAME}?startgroup=true"
    else:
        add_url = "https://t.me/"

    owner_username_clean = (OWNER_USERNAME or "").lstrip("@")
    if owner_username_clean:
        owner_url = f"https://t.me/{owner_username_clean}"
    else:
        owner_url = "https://t.me/"

    for new_member in message.new_chat_members:
        print(f"[welcome] chat={chat.id}, new_member={new_member.id}")

        if new_member.id == BOT_ID:
            try:
                thank_you = (
                    "Konnichiwa~ 💗 Thanks for adding me!\n\n"
                    "I’m Zero Two, your cute & smart anime bot 🧚‍♀️✨\n"
                    "Here to make chats fun, active & adorable.\n\n"
                    "💬 Auto replies ON\n"
                    "✨ Fun interactions ready\n"
                    "⚙ Always active for you\n\n"
                    "Let’s enjoy together, Darling 💗"
                )
                bot.send_message(chat.id, thank_you)
            except Exception as e:
                print(f"[bot-added welcome error] {e}")
            continue

        user_id = new_member.id

        full_name = " ".join(
            part for part in [new_member.first_name, new_member.last_name] if part
        ).strip() or "New user"
        full_name_html = html.escape(full_name)

        if full_name:
            names_per_chat.setdefault(chat.id, {})[full_name.lower()] = user_id

        username_raw = new_member.username
        if username_raw:
            username_display = f"@{username_raw}"
            usernames_per_chat.setdefault(chat.id, {})[username_raw.lower()] = user_id
        else:
            username_display = "(no username)"
        username_display_html = html.escape(username_display)

        try:
            member_info = bot.get_chat_member(chat.id, user_id)
            raw_status = member_info.status
        except Exception as e:
            print(f"[welcome get_chat_member error] {e}")
            raw_status = "member"

        status_map = {
            "creator": "Owner",
            "administrator": "Admin",
            "member": "Member",
            "restricted": "Restricted",
            "left": "Left",
            "kicked": "Banned",
        }
        status_str = status_map.get(raw_status, raw_status.capitalize())
        status_html = html.escape(status_str)

        mention_name = f'<a href="tg://user?id={user_id}">{full_name_html}</a>'
        mention_id = f'<a href="tg://user?id={user_id}">{user_id}</a>'
        mention_username = f'<a href="tg://user?id={user_id}">{username_display_html}</a>'

        caption = (
            "╔═══❖•༻ ♡ W E L C O M E ♡ ༺•❖═══╗\n"
            f"• Group   : {chat_title}\n"
            f"• Name    : {mention_name}\n"
            f"• ID      : {mention_id}\n"
            f"• Username: {mention_username}\n"
            f"• Status  : {status_html}\n"
            "╚══════════════════════════════╝\n"
            "❝ Stay soft, follow the rules & vibe with ZeroTwo 💗 ❞"
        )

        keyboard = types.InlineKeyboardMarkup()
        keyboard.add(
            types.InlineKeyboardButton(
                "➕ Add me to your group",
                url=add_url
            ),
            types.InlineKeyboardButton(
                "👑 Owner",
                url=owner_url
            ),
        )

        try:
            photos = bot.get_user_profile_photos(user_id, limit=1)
            if photos.total_count > 0:
                photo_file_id = photos.photos[0][-1].file_id
                bot.send_photo(
                    chat.id,
                    photo_file_id,
                    caption=caption,
                    parse_mode="HTML",
                    reply_markup=keyboard,
                )
            else:
                bot.send_message(
                    chat.id,
                    caption,
                    parse_mode="HTML",
                    reply_markup=keyboard,
                )
        except Exception as e:
            print(f"[welcome photo error] {e}")
            bot.send_message(
                chat.id,
                caption,
                parse_mode="HTML",
                reply_markup=keyboard,
            )


# =============== /start & HELP MENU ===============

@bot.message_handler(commands=["start"])
def handle_start(message):
    chat_type = message.chat.type

    if chat_type == "private":
        intro_text = (
            "Hello There, I'm Zero Two 💗✨\n"
            "Your AI-Integrated, Anime-Themed Advanced Assistant 🧠🤖\n"
            "\n"
            "I can:\n"
            "• Auto Reply | Smart AI Chat\n"
            "• Cute + Fun Anime Interactions\n"
            "• Group Assistance & Management\n"
            "• Fast Replies & Endless Conversations 💬\n"
            "\n"
            "Make sure to check my About section below\n"
            "to discover all my features! ⚡\n"
            "\n"
            "Type anything to begin 🩷\n"
            "❤️"
        )

        if BOT_USERNAME:
            add_url = f"https://t.me/{BOT_USERNAME}?startgroup=true"
        else:
            add_url = "https://t.me/"

        owner_username_clean = (OWNER_USERNAME or "").lstrip("@")
        if owner_username_clean:
            owner_url = f"https://t.me/{owner_username_clean}"
        else:
            owner_url = "https://t.me/"

        keyboard = types.InlineKeyboardMarkup()
        keyboard.add(
            types.InlineKeyboardButton("🫰 Steal me", url=add_url),
            types.InlineKeyboardButton("👑 Owner", url=owner_url),
        )
        keyboard.add(
            types.InlineKeyboardButton("📖 Help", callback_data="start_help_menu")
        )

        if START_PHOTO_URL:
            try:
                bot.send_photo(
                    message.chat.id,
                    START_PHOTO_URL,
                    caption=intro_text,
                    reply_markup=keyboard,
                )
            except Exception as e:
                print(f"[start photo error] {e}")
                bot.send_message(
                    message.chat.id,
                    intro_text,
                    reply_markup=keyboard,
                )
        else:
            bot.send_message(
                message.chat.id,
                intro_text,
                reply_markup=keyboard,
            )
    else:
        text = (
            f"Hey, I am {BOT_DISPLAY_NAME}.\n"
            f"- Use /help to see what I can do in this group.\n"
            f"- DM me with /start to see my full introduction."
        )
        bot.reply_to(message, text)


@bot.callback_query_handler(func=lambda c: c.data == "start_help_menu")
def handle_start_help_menu(callback_query):
    msg = callback_query.message
    chat_id = msg.chat.id

    if msg.chat.type != "private":
        bot.answer_callback_query(callback_query.id)
        return

    help_caption = (
        "📖 <b>Command help menu</b>\n"
        "Tap any command button below and I will explain how to use it."
    )

    commands_order = [
        "start", "alive", "owner", "about", "help",
        "info", "id",
        "filter", "filters", "stop", "stopall",
        "mute", "unmute",
        "ban", "unban", "kick",
        "warn", "unwarn", "warnlist", "banlist",
        "admin", "unadmin", "admins",
        "tr", "quote", "weather", "imagine", "time",
    ]

    kb = types.InlineKeyboardMarkup()
    for row in chunk_list(commands_order, 3):
        buttons = [
            types.InlineKeyboardButton(f"/{cmd}", callback_data=f"cmdhelp:{cmd}")
            for cmd in row
        ]
        kb.row(*buttons)

    kb.add(types.InlineKeyboardButton("⬅ Back", callback_data="root_start_menu"))

    try:
        if msg.photo:
            bot.edit_message_caption(
                chat_id=chat_id,
                message_id=msg.message_id,
                caption=help_caption,
                reply_markup=kb,
                parse_mode="HTML",
            )
        else:
            bot.edit_message_text(
                help_caption,
                chat_id=chat_id,
                message_id=msg.message_id,
                reply_markup=kb,
                parse_mode="HTML",
            )
    except Exception as e:
        print(f"[start_help_menu edit error] {e}")

    bot.answer_callback_query(callback_query.id)


@bot.callback_query_handler(func=lambda c: c.data == "root_start_menu")
def handle_root_start_menu(callback_query):
    msg = callback_query.message
    chat_id = msg.chat.id

    if msg.chat.type != "private":
        bot.answer_callback_query(callback_query.id)
        return

    intro_text = (
        "Hello There, I'm Zero Two 💗✨\n"
        "Your AI-Integrated, Anime-Themed Advanced Assistant 🧠🤖\n"
        "\n"
        "I can:\n"
        "• Auto Reply | Smart AI Chat\n"
        "• Cute + Fun Anime Interactions\n"
        "• Group Assistance & Management\n"
        "• Fast Replies & Endless Conversations 💬\n"
        "\n"
        "Make sure to check my About section below\n"
        "to discover all my features! ⚡\n"
        "\n"
        "Type anything to begin 🩷\n"
        "❤️"
    )

    if BOT_USERNAME:
        add_url = f"https://t.me/{BOT_USERNAME}?startgroup=true"
    else:
        add_url = "https://t.me/"

    owner_username_clean = (OWNER_USERNAME or "").lstrip("@")
    if owner_username_clean:
        owner_url = f"https://t.me/{owner_username_clean}"
    else:
        owner_url = "https://t.me/"

    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton("🫰 Steal me", url=add_url),
        types.InlineKeyboardButton("👑 Owner", url=owner_url),
    )
    kb.add(
        types.InlineKeyboardButton("📖 Help", callback_data="start_help_menu")
    )

    try:
        if msg.photo:
            bot.edit_message_caption(
                chat_id=chat_id,
                message_id=msg.message_id,
                caption=intro_text,
                reply_markup=kb,
            )
        else:
            bot.edit_message_text(
                intro_text,
                chat_id=chat_id,
                message_id=msg.message_id,
                reply_markup=kb,
            )
    except Exception as e:
        print(f"[root_start_menu edit error] {e}")

    bot.answer_callback_query(callback_query.id)


@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("cmdhelp:"))
def handle_cmd_help(callback_query):
    msg = callback_query.message
    chat_id = msg.chat.id

    if msg.chat.type != "private":
        bot.answer_callback_query(callback_query.id)
        return

    data = callback_query.data.split(":", 1)
    if len(data) != 2:
        bot.answer_callback_query(callback_query.id)
        return

    cmd = data[1]
    desc = COMMAND_HELP.get(cmd, f"/{cmd} command help text is not configured yet.")

    help_text = (
        f"ℹ️ <b>/{cmd}</b>\n"
        f"{desc}\n\n"
        "Use the Back button below to return to the command menu."
    )

    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton("⬅ Back to menu", callback_data="start_help_menu")
    )

    try:
        if msg.photo:
            bot.edit_message_caption(
                chat_id=chat_id,
                message_id=msg.message_id,
                caption=help_text,
                reply_markup=kb,
                parse_mode="HTML",
            )
        else:
            bot.edit_message_text(
                help_text,
                chat_id=chat_id,
                message_id=msg.message_id,
                reply_markup=kb,
                parse_mode="HTML",
            )
    except Exception as e:
        print(f"[cmdhelp edit error] {e}")

    bot.answer_callback_query(callback_query.id)


# =============== BASIC COMMANDS ===============

@bot.message_handler(commands=["help"])
def handle_help(message):
    chat_type = message.chat.type

    if chat_type == "private":
        text = (
            "Here’s a quick overview of what I can do:\n\n"
            "• Chat with you using smart AI (Hindi / Bengali / English).\n"
            "• Cute & fun anime-style interactions.\n"
            "• Group management: warns, bans, mutes, filters, admin tools.\n"
            "• Utility: translation, quotes, weather, time, and more.\n\n"
            "Useful commands (DM + Group):\n"
            "/start   – Start talking to me\n"
            "/alive   – Check if I am online\n"
            "/owner   – Show my owner\n"
            "/about   – Info about me\n"
            "/help    – This help\n"
            "/quote   – Random motivational quote\n"
            "/tr      – Translate any text to English\n"
            "/tts     – Convert text to voice (if TTS limit allows)\n"
            "/weather – Weather info + AQI\n"
            "/time    – Current time (IST)\n\n"
            "Group-only commands (admins/owner):\n"
            "/admins, /admin, /unadmin\n"
            "/warn, /unwarn, /warnlist\n"
            "/ban, /unban, /banlist, /kick\n"
            "/mute, /unmute\n"
            "/filter, /filters, /stop, /stopall\n"
            "/info, /id\n\n"
            "For detailed usage of each command, open /start and tap the 📖 Help button."
        )
    else:
        text = (
            "Group help:\n"
            f"- Mention me (@{BOT_USERNAME}) or reply to my messages and I will answer.\n"
            "- I welcome new members with a styled welcome card and buttons.\n"
            "- I can also react to 'good morning' and 'good night' messages.\n\n"
            "Basic admin commands:\n"
            "/admins – show admins\n"
            "/warn, /unwarn, /warnlist – warning system\n"
            "/ban, /unban, /banlist – bans\n"
            "/mute, /unmute – mute system\n"
            "/kick – kick user\n"
            "/admin, /unadmin – promote/demote admins\n"
            "/filter, /filters, /stop, /stopall – auto-reply filters\n"
            "/info, /id – user/group info\n"
            "/weather <city> – weather + AQI\n"
        )

    bot.reply_to(message, text)


@bot.message_handler(commands=["ping"])
def handle_ping(message):
    bot.reply_to(message, "Pong, I am alive.")


@bot.message_handler(commands=["alive"])
def handle_alive(message):
    bot.reply_to(message, "I'm alive and working.")


@bot.message_handler(commands=["owner"])
def handle_owner(message):
    bot.reply_to(message, f"My owner is {OWNER_USERNAME}")


@bot.message_handler(commands=["about"])
def handle_about(message):
    bot.reply_to(
        message,
        "I am a funny, flirty, roast-capable anime themed assistant for your groups and DMs.",
    )


# =============== AFK SYSTEM ===============

@bot.message_handler(commands=["afk"])
def handle_afk(message):
    user = message.from_user
    user_id = user.id
    name = user.first_name or user.username or "User"

    parts = message.text.split(maxsplit=1)
    if len(parts) > 1:
        reason = parts[1].strip()
    else:
        reason = ""

    sticker_id = None
    if message.reply_to_message and message.reply_to_message.sticker:
        sticker_id = message.reply_to_message.sticker.file_id

    afk_status[user_id] = {
        "reason": reason,
        "since": time.time(),
        "username": (user.username or "").lower() if user.username else None,
        "name": name,
        "sticker_id": sticker_id,
    }

    if message.chat.type in ("group", "supergroup"):
        try:
            bot.delete_message(message.chat.id, message.message_id)
        except Exception as e:
            print(f"[AFK delete cmd error] {e}")

        mention = f'<a href="tg://user?id={user_id}">{html.escape(name)}</a>'
        text = f"{mention} is now AFK."
        bot.send_message(message.chat.id, text, parse_mode="HTML")
    else:
        if reason:
            bot.reply_to(message, f"You are now AFK with reason: {reason}")
        else:
            bot.reply_to(message, "You are now AFK.")


# =============== FILTERS ===============

def build_filter_payload_from_message(msg):
    if msg.sticker:
        return {"type": "sticker", "file_id": msg.sticker.file_id}
    if msg.animation:
        return {"type": "animation", "file_id": msg.animation.file_id}
    if msg.voice:
        return {"type": "voice", "file_id": msg.voice.file_id}
    if msg.audio:
        return {"type": "audio", "file_id": msg.audio.file_id}
    if msg.video:
        return {"type": "video", "file_id": msg.video.file_id}
    if msg.video_note:
        return {"type": "video_note", "file_id": msg.video_note.file_id}
    if msg.document:
        return {
            "type": "document",
            "file_id": msg.document.file_id,
            "caption": msg.caption or "",
        }
    if msg.photo:
        file_id = msg.photo[-1].file_id
        return {"type": "photo", "file_id": file_id, "caption": msg.caption or ""}
    if msg.text:
        return {"type": "text", "text": msg.text}
    return None


def payload_matches(a, b) -> bool:
    if a.get("type") != b.get("type"):
        return False
    t = a.get("type")
    if t == "text":
        return a.get("text") == b.get("text")
    if t in ["sticker", "animation", "voice", "audio", "video", "video_note", "document", "photo"]:
        return a.get("file_id") == b.get("file_id")
    return False


def send_filter_reply(message, payload):
    chat_id = message.chat.id
    reply_to = message.message_id
    ftype = payload.get("type")

    if ftype == "text":
        bot.reply_to(message, payload.get("text", ""))
    elif ftype == "sticker":
        bot.send_sticker(chat_id, payload["file_id"], reply_to_message_id=reply_to)
    elif ftype == "animation":
        bot.send_animation(chat_id, payload["file_id"], reply_to_message_id=reply_to)
    elif ftype == "voice":
        bot.send_voice(chat_id, payload["file_id"], reply_to_message_id=reply_to)
    elif ftype == "audio":
        bot.send_audio(chat_id, payload["file_id"], reply_to_message_id=reply_to)
    elif ftype == "video":
        bot.send_video(chat_id, payload["file_id"], reply_to_message_id=reply_to)
    elif ftype == "video_note":
        bot.send_video_note(chat_id, payload["file_id"], reply_to_message_id=reply_to)
    elif ftype == "document":
        bot.send_document(
            chat_id,
            payload["file_id"],
            caption=payload.get("caption") or None,
            reply_to_message_id=reply_to,
        )
    elif ftype == "photo":
        bot.send_photo(
            chat_id,
            payload["file_id"],
            caption=payload.get("caption") or None,
            reply_to_message_id=reply_to,
        )
    else:
        bot.reply_to(message, "[Filter error] Unknown filter type.")


@bot.message_handler(commands=["filter"])
def handle_filter(message):
    chat_id = message.chat.id
    text = message.text or ""
    parts = text.split(maxsplit=2)

    if message.reply_to_message:
        if len(parts) < 2:
            bot.reply_to(message, "Usage (as reply): /filter <keyword>")
            return

        keyword = parts[1].lower()
        payload = build_filter_payload_from_message(message.reply_to_message)
        if not payload:
            bot.reply_to(
                message,
                "Sorry, I cannot save this type of message as a filter.",
            )
            return

        chat_filters = filters_per_chat.setdefault(chat_id, {})
        chat_filters[keyword] = payload
        bot.reply_to(message, f"Saved filter for '{keyword}' using replied message.")
        return

    if len(parts) < 3:
        bot.reply_to(
            message,
            "Usage:\n"
            "- Reply to any sticker/gif/voice/text and use: /filter <keyword>\n"
            "- Or: /filter <keyword> <reply text>",
        )
        return

    keyword = parts[1].lower()
    reply_text = parts[2]
    payload = {"type": "text", "text": reply_text}
    chat_filters = filters_per_chat.setdefault(chat_id, {})
    chat_filters[keyword] = payload
    bot.reply_to(message, f"Saved text filter for '{keyword}'.")


@bot.message_handler(commands=["filters"])
def handle_filters(message):
    chat_id = message.chat.id
    chat_filters = filters_per_chat.get(chat_id, {})

    if not chat_filters:
        bot.reply_to(message, "No filters are set in this chat.")
        return

    lines = ["Active filters in this chat:"]
    for keyword, payload in chat_filters.items():
        lines.append(f"- {keyword} ({payload.get('type', 'unknown')})")

    bot.reply_to(message, "\n".join(lines))


@bot.message_handler(commands=["stop"])
def handle_stop_filter(message):
    chat_id = message.chat.id
    text = message.text or ""
    parts = text.split(maxsplit=1)
    keyword = parts[1].lower() if len(parts) > 1 else None

    chat_filters = filters_per_chat.get(chat_id, {})
    if not chat_filters:
        bot.reply_to(message, "No filters set in this chat.")
        return

    if message.reply_to_message:
        payload = build_filter_payload_from_message(message.reply_to_message)
        if payload:
            removed = False
            for k, v in list(chat_filters.items()):
                if keyword and k != keyword:
                    continue
                if payload_matches(v, payload):
                    del chat_filters[k]
                    bot.reply_to(message, f"Removed filter '{k}'.")
                    removed = True
                    break
            if removed:
                return

    if not keyword:
        bot.reply_to(
            message,
            "Usage: reply to original message + /stop <keyword>\n"
            "or just /stop <keyword>.",
        )
        return

    if keyword in chat_filters:
        del chat_filters[keyword]
        bot.reply_to(message, f"Removed filter '{keyword}'.")
    else:
        bot.reply_to(message, f"No filter found for '{keyword}'.")


@bot.message_handler(commands=["stopall"])
def handle_stopall(message):
    chat_id = message.chat.id

    if chat_id in filters_per_chat and filters_per_chat[chat_id]:
        filters_per_chat[chat_id] = {}
        bot.reply_to(message, "Removed all filters in this chat.")
    else:
        bot.reply_to(message, "There were no filters set in this chat.")


# =============== INFO & ID ===============

@bot.message_handler(commands=["info"])
def handle_info(message):
    if message.reply_to_message and message.reply_to_message.from_user:
        user = message.reply_to_message.from_user
    else:
        user = message.from_user

    full_name = " ".join(
        part for part in [user.first_name, user.last_name] if part
    ).strip() or "(no name)"

    username = f"@{user.username}" if user.username else "(no username)"

    text = (
        f"User info:\n"
        f"Name: {full_name}\n"
        f"Username: {username}\n"
        f"User ID: {user.id}"
    )

    bot.reply_to(message, text)


@bot.message_handler(commands=["id"])
def handle_id(message):
    chat_id = message.chat.id

    if message.reply_to_message and message.reply_to_message.from_user:
        user = message.reply_to_message.from_user
        text = (
            f"Chat ID: {chat_id}\n"
            f"User ID: {user.id}"
        )
    else:
        text = f"Chat ID: {chat_id}"

    bot.reply_to(message, text)


# =============== ADMINS LIST ===============

@bot.message_handler(commands=["admins"])
def handle_admins(message):
    if message.chat.type not in ("group", "supergroup"):
        bot.reply_to(message, "This command only works in groups.")
        return

    try:
        admins = bot.get_chat_administrators(message.chat.id)
    except Exception as e:
        print(f"[admins error] {e}")
        bot.reply_to(message, "Unable to fetch admins list. Maybe I lack permissions.")
        return

    if not admins:
        bot.reply_to(message, "I couldn't find any admins in this group.")
        return

    lines = ["<b>Group admins:</b>"]
    for a in admins:
        u = a.user
        name = html.escape(u.first_name or u.username or "User")
        username = f"@{u.username}" if u.username else ""
        role = "Owner" if a.status == "creator" else "Admin"
        mention = f'<a href="tg://user?id={u.id}">{name}</a>'
        if username:
            lines.append(f"• {mention} ({username}) – {role}")
        else:
            lines.append(f"• {mention} – {role}")

    bot.send_message(message.chat.id, "\n".join(lines), parse_mode="HTML")


# =============== WARN / BAN / BANLIST / WARNLIST ===============

@bot.message_handler(commands=["warn"])
def handle_warn(message):
    chat = message.chat

    if chat.type not in ("group", "supergroup"):
        bot.reply_to(message, "Use /warn only in groups.")
        return

    if not is_user_admin(chat.id, message.from_user.id):
        bot.reply_to(message, "Only group admins/owner can use /warn.")
        return

    if not message.reply_to_message or not message.reply_to_message.from_user:
        bot.reply_to(message, "Reply to a user's message and type /warn.")
        return

    target = message.reply_to_message.from_user

    if target.id == BOT_ID:
        bot.reply_to(message, "Warning me won't help, darling~")
        return

    if is_user_admin(chat.id, target.id):
        bot.reply_to(message, "I can't warn another admin.")
        return

    text = message.text or ""
    parts = text.split(maxsplit=1)
    reason = parts[1].strip() if len(parts) > 1 else "No reason"

    chat_warns = warns_per_chat.setdefault(chat.id, {})
    count = chat_warns.get(target.id, 0) + 1
    chat_warns[target.id] = count

    name = target.first_name or target.username or "User"

    if count >= 3:
        try:
            bot.ban_chat_member(chat.id, target.id)
            add_ban_record(chat.id, target, f"Auto-ban after 3 warns: {reason}", message.from_user)
            del chat_warns[target.id]
            bot.reply_to(
                message,
                f"User {name} had 3/3 warnings and is now banned from the group."
            )
        except Exception as e:
            print(f"[warn autoban error] {e}")
            bot.reply_to(
                message,
                f"There was a problem banning {name}. Maybe I don't have ban permission."
            )
    else:
        bot.reply_to(
            message,
            f"User {name} has {count}/3 warnings; be careful!\n"
            f"Reason: {reason}"
        )


@bot.message_handler(commands=["unwarn"])
def handle_unwarn(message):
    chat = message.chat

    if chat.type not in ("group", "supergroup"):
        bot.reply_to(message, "Use /unwarn only in groups.")
        return

    if not is_user_admin(chat.id, message.from_user.id):
        bot.reply_to(message, "Only group admins/owner can use /unwarn.")
        return

    if not message.reply_to_message or not message.reply_to_message.from_user:
        bot.reply_to(message, "Reply to a user's message and type /unwarn.")
        return

    target = message.reply_to_message.from_user
    name = target.first_name or target.username or "User"

    chat_warns = warns_per_chat.get(chat.id, {})
    current = chat_warns.get(target.id, 0)

    if current <= 0:
        bot.reply_to(message, f"User {name} has no active warns.")
        return

    new_count = current - 1
    if new_count <= 0:
        del chat_warns[target.id]
    else:
        chat_warns[target.id] = new_count

    admin_name = message.from_user.first_name or message.from_user.username or "Admin"

    text = f"Admin {admin_name} has removed {name}'s warning."
    bot.reply_to(message, text)


@bot.message_handler(commands=["warnlist"])
def handle_warnlist(message):
    chat = message.chat
    if chat.type not in ("group", "supergroup"):
        bot.reply_to(message, "Use /warnlist only in groups.")
        return
    if not is_user_admin(chat.id, message.from_user.id):
        bot.reply_to(message, "Only group admins/owner can use /warnlist.")
        return

    chat_warns = warns_per_chat.get(chat.id, {})
    if not chat_warns:
        bot.reply_to(message, "Nobody has active warns in this group.")
        return

    lines = ["<b>Current warns:</b>"]
    for uid, cnt in chat_warns.items():
        try:
            u = bot.get_chat_member(chat.id, uid).user
            name = html.escape(u.first_name or u.username or "User")
        except Exception:
            name = f"User {uid}"
        mention = f'<a href="tg://user?id={uid}">{name}</a>'
        lines.append(f"• {mention} – {cnt}/3 warns")

    bot.send_message(chat.id, "\n".join(lines), parse_mode="HTML")


@bot.message_handler(commands=["ban"])
def handle_ban(message):
    chat = message.chat

    if chat.type not in ("group", "supergroup"):
        bot.reply_to(message, "Use /ban only in groups.")
        return

    if not is_user_admin(chat.id, message.from_user.id):
        bot.reply_to(message, "Only group admins/owner can use /ban.")
        return

    text = message.text or ""
    parts = text.split(maxsplit=2)

    target_user = None
    reason = "Manual ban by admin"

    if message.reply_to_message and message.reply_to_message.from_user:
        target_user = message.reply_to_message.from_user
        if len(parts) > 1:
            reason = text.split(maxsplit=1)[1]
    else:
        if len(parts) < 2:
            bot.reply_to(
                message,
                "Usage:\n"
                "- Reply to user message: /ban [reason]\n"
                "- Or: /ban <user_id|@username|name> [reason]",
            )
            return

        target_arg = parts[1]
        reason = parts[2] if len(parts) > 2 else reason

        if target_arg.startswith("@"):
            uname = target_arg[1:].lower()
            uid = usernames_per_chat.get(chat.id, {}).get(uname)
            if uid is None:
                bot.reply_to(
                    message,
                    "I don't know this username in this group yet. "
                    "The user must speak at least once before I can ban by username.",
                )
                return
            try:
                member = bot.get_chat_member(chat.id, uid)
                target_user = member.user
            except Exception as e:
                print(f"[ban get_chat_member username error] {e}")
                bot.reply_to(message, "Could not fetch user info, maybe they left the group.")
                return
        elif target_arg.isdigit():
            uid = int(target_arg)
            try:
                member = bot.get_chat_member(chat.id, uid)
                target_user = member.user
            except Exception as e:
                print(f"[ban get_chat_member id error] {e}")
                bot.reply_to(message, "No user with this ID found in this group.")
                return
        else:
            uid, err = find_user_by_name(chat.id, target_arg)
            if uid is None:
                bot.reply_to(message, err)
                return
            try:
                member = bot.get_chat_member(chat.id, uid)
                target_user = member.user
            except Exception as e:
                print(f"[ban get_chat_member name error] {e}")
                bot.reply_to(message, "Could not fetch user info, maybe they left the group.")
                return

    if not target_user:
        bot.reply_to(message, "Could not identify target user.")
        return

    if target_user.id == BOT_ID:
        bot.reply_to(message, "I can't ban myself.")
        return

    if is_user_admin(chat.id, target_user.id):
        bot.reply_to(message, "I can't ban another admin.")
        return

    try:
        bot.ban_chat_member(chat.id, target_user.id)
        add_ban_record(chat.id, target_user, reason, message.from_user)
    except Exception as e:
        print(f"[ban error] {e}")
        bot.reply_to(message, "Failed to ban user. Maybe I don't have ban permission.")
        return

    username_text = f"@{target_user.username}" if target_user.username else (target_user.first_name or "User")
    username_html = html.escape(username_text)
    mention = f'<a href="tg://user?id={target_user.id}">{username_html}</a>'

    info_text = f"{mention} [{target_user.id}] banned."
    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton(
            "✅ Unban",
            callback_data=f"unbanbtn:{chat.id}:{target_user.id}"
        )
    )

    bot.send_message(
        chat.id,
        info_text,
        parse_mode="HTML",
        reply_markup=kb,
    )


@bot.message_handler(commands=["unban"])
def handle_unban(message):
    chat = message.chat

    if chat.type not in ("group", "supergroup"):
        bot.reply_to(message, "Use /unban only in groups.")
        return

    if not is_user_admin(chat.id, message.from_user.id):
        bot.reply_to(message, "Only group admins/owner can use /unban.")
        return

    text = message.text or ""
    parts = text.split(maxsplit=1)

    target_id = None
    target_name = "User"

    if message.reply_to_message and message.reply_to_message.from_user:
        t = message.reply_to_message.from_user
        target_id = t.id
        target_name = t.first_name or t.username or "User"
    elif len(parts) > 1:
        arg = parts[1].split()[0]

        if arg.startswith("@"):
            uname = arg[1:].lower()
            uid = usernames_per_chat.get(chat.id, {}).get(uname)
            if uid is None:
                bot.reply_to(
                    message,
                    "I don't know this username in this group yet.",
                )
                return
            target_id = uid
            try:
                member = bot.get_chat_member(chat.id, uid)
                u = member.user
                target_name = u.first_name or u.username or "User"
            except Exception:
                target_name = f"User {uid}"
        elif arg.isdigit():
            uid = int(arg)
            target_id = uid
            try:
                member = bot.get_chat_member(chat.id, uid)
                u = member.user
                target_name = u.first_name or u.username or "User"
            except Exception:
                target_name = f"User {uid}"
        else:
            bot.reply_to(
                message,
                "Usage: reply + /unban, or /unban <user_id>, or /unban @username.",
            )
            return
    else:
        bot.reply_to(
            message,
            "Usage: reply + /unban, or /unban <user_id>, or /unban @username."
        )
        return

    try:
        bot.unban_chat_member(chat.id, target_id, only_if_banned=False)
        bot.reply_to(
            message,
            f"User {target_name} [{target_id}] has been unbanned."
        )
    except Exception as e:
        print(f"[unban error] {e}")
        bot.reply_to(message, "Failed to unban user. Maybe I don't have rights.")


@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("unbanbtn:"))
def handle_unban_button(callback_query):
    try:
        data = callback_query.data.split(":", 2)
        if len(data) != 3:
            return
        _, chat_id_str, user_id_str = data
        chat_id = int(chat_id_str)
        target_id = int(user_id_str)
    except Exception as e:
        print(f"[unbanbtn parse error] {e}")
        return

    actor = callback_query.from_user

    if not is_user_admin(chat_id, actor.id):
        bot.answer_callback_query(
            callback_query.id,
            "Only group admins/owner can use this button.",
            show_alert=True,
        )
        return

    try:
        bot.unban_chat_member(chat_id, target_id, only_if_banned=False)
    except Exception as e:
        print(f"[unbanbtn unban error] {e}")
        bot.answer_callback_query(
            callback_query.id,
            "Failed to unban user. Maybe I don't have enough rights.",
            show_alert=True,
        )
        return

    try:
        member = bot.get_chat_member(chat_id, target_id)
        u = member.user
        target_name = u.first_name or u.username or "User"
    except Exception:
        target_name = f"User {target_id}"

    text = f"{target_name} [{target_id}] unbanned."
    try:
        bot.send_message(chat_id, text)
    except Exception as e:
        print(f"[unbanbtn send msg error] {e}")

    bot.answer_callback_query(callback_query.id, "User unbanned.")


@bot.message_handler(commands=["banlist"])
def handle_banlist(message):
    chat = message.chat

    if chat.type not in ("group", "supergroup"):
        bot.reply_to(message, "Use /banlist only in groups.")
        return

    if not is_user_admin(chat.id, message.from_user.id):
        bot.reply_to(message, "Only group admins/owner can use /banlist.")
        return

    chat_bans = banlist_per_chat.get(chat.id, {})
    if not chat_bans:
        bot.reply_to(message, "My ban record for this group is empty.")
        return

    lines = ["<b>Ban list (recorded by bot):</b>"]
    for uid, info in chat_bans.items():
        name = html.escape(info.get("name") or "User")
        username = info.get("username") or ""
        reason = html.escape(info.get("reason") or "")
        mention = f'<a href="tg://user?id={uid}">{name}</a>'
        line = f"• {mention}"
        if username and username != "(no username)":
            line += f" ({username})"
        if reason:
            line += f" – {reason}"
        lines.append(line)

    bot.send_message(message.chat.id, "\n".join(lines), parse_mode="HTML")


# =============== MUTE / UNMUTE + BUTTON ===============

@bot.message_handler(commands=["mute"])
def handle_mute(message):
    chat = message.chat

    if chat.type not in ("group", "supergroup"):
        bot.reply_to(message, "Use /mute only in groups.")
        return

    if not is_user_admin(chat.id, message.from_user.id):
        bot.reply_to(message, "Only group admins/owner can use /mute.")
        return

    text = message.text or ""
    parts = text.split()
    args = parts[1:]

    target_user = None
    duration_sec = None
    duration_text = None

    if message.reply_to_message and message.reply_to_message.from_user:
        target_user = message.reply_to_message.from_user

        if args:
            duration_sec = parse_duration_to_seconds(args[0])
            if duration_sec is None:
                bot.reply_to(
                    message,
                    "Wrong time format. Use like 30s, 10m, 2h, 1d, 1y or no time for permanent mute.",
                )
                return
            duration_text = args[0]
    else:
        if not args:
            bot.reply_to(
                message,
                "Usage:\n"
                "- Reply to a user's message: /mute [time]\n"
                "- Or: /mute <user_id|@username|name> [time]",
            )
            return

        maybe_time = args[-1]
        possible_dur = parse_duration_to_seconds(maybe_time)
        if possible_dur is not None:
            duration_sec = possible_dur
            duration_text = maybe_time
            target_tokens = args[:-1]
        else:
            target_tokens = args

        if not target_tokens:
            bot.reply_to(
                message,
                "Please specify which user to mute. Example:\n"
                "/mute @user 10m\n"
                "/mute SomeName",
            )
            return

        target_str = " ".join(target_tokens).strip()

        if target_str.startswith("@"):
            uname = target_str[1:].lower()
            uid = usernames_per_chat.get(chat.id, {}).get(uname)
            if uid is None:
                bot.reply_to(
                    message,
                    "I don't know this username in this group yet. "
                    "The user must speak at least once before I can mute by username.",
                )
                return
            try:
                member = bot.get_chat_member(chat.id, uid)
                target_user = member.user
            except Exception as e:
                print(f"[mute get_chat_member username error] {e}")
                bot.reply_to(message, "Could not fetch user info, maybe they left the group.")
                return
        elif target_str.isdigit():
            uid = int(target_str)
            try:
                member = bot.get_chat_member(chat.id, uid)
                target_user = member.user
            except Exception as e:
                print(f"[mute get_chat_member id error] {e}")
                bot.reply_to(message, "No user with this ID found in this group.")
                return
        else:
            uid, err = find_user_by_name(chat.id, target_str)
            if uid is None:
                bot.reply_to(message, err)
                return
            try:
                member = bot.get_chat_member(chat.id, uid)
                target_user = member.user
            except Exception as e:
                print(f"[mute get_chat_member name error] {e}")
                bot.reply_to(message, "Could not fetch user info, maybe they left the group.")
                return

    if not target_user:
        bot.reply_to(message, "Could not identify target user.")
        return

    if target_user.id == BOT_ID:
        bot.reply_to(message, "I can't mute myself.")
        return

    if is_user_admin(chat.id, target_user.id):
        bot.reply_to(message, "I can't mute another admin.")
        return

    if duration_sec is None:
        duration_sec = 10 * 365 * 24 * 60 * 60
        duration_text = "permanently"

    until = int(time.time()) + duration_sec

    perms = types.ChatPermissions(
        can_send_messages=False,
        can_send_media_messages=False,
        can_send_polls=False,
        can_send_other_messages=False,
        can_add_web_page_previews=False,
    )

    try:
        bot.restrict_chat_member(
            chat.id,
            target_user.id,
            permissions=perms,
            until_date=until,
        )
    except Exception as e:
        print(f"[mute error] {e}")
        bot.reply_to(message, "Failed to mute user. Maybe I don't have enough rights.")
        return

    username_text = f"@{target_user.username}" if target_user.username else (target_user.first_name or "User")
    username_html = html.escape(username_text)
    mention = f'<a href="tg://user?id={target_user.id}">{username_html}</a>'

    if duration_text == "permanently":
        info_text = f"{mention} [{target_user.id}] has been muted permanently."
    else:
        info_text = f"{mention} [{target_user.id}] has been muted for <b>{duration_text}</b>."

    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton(
            "✅ Unmute",
            callback_data=f"unmutebtn:{chat.id}:{target_user.id}"
        )
    )

    bot.send_message(
        chat.id,
        info_text,
        parse_mode="HTML",
        reply_markup=kb,
    )


@bot.message_handler(commands=["unmute"])
def handle_unmute(message):
    chat = message.chat

    if chat.type not in ("group", "supergroup"):
        bot.reply_to(message, "Use /unmute only in groups.")
        return

    if not is_user_admin(chat.id, message.from_user.id):
        bot.reply_to(message, "Only group admins/owner can use /unmute.")
        return

    text = message.text or ""
    parts = text.split()
    args = parts[1:]

    target_id = None
    target_name = "User"

    if message.reply_to_message and message.reply_to_message.from_user:
        u = message.reply_to_message.from_user
        target_id = u.id
        target_name = u.first_name or u.username or "User"
    elif args:
        target_str = " ".join(args).strip()

        if target_str.startswith("@"):
            uname = target_str[1:].lower()
            uid = usernames_per_chat.get(chat.id, {}).get(uname)
            if uid is None:
                bot.reply_to(
                    message,
                    "I don't know this username in this group yet. "
                    "The user must speak at least once before I can unmute by username.",
                )
                return
            target_id = uid
            try:
                member = bot.get_chat_member(chat.id, uid)
                u = member.user
                target_name = u.first_name or u.username or "User"
            except Exception:
                target_name = f"User {uid}"
        elif target_str.isdigit():
            uid = int(target_str)
            target_id = uid
            try:
                member = bot.get_chat_member(chat.id, uid)
                u = member.user
                target_name = u.first_name or u.username or "User"
            except Exception:
                target_name = f"User {uid}"
        else:
            uid, err = find_user_by_name(chat.id, target_str)
            if uid is None:
                bot.reply_to(message, err)
                return
            target_id = uid
            try:
                member = bot.get_chat_member(chat.id, uid)
                u = member.user
                target_name = u.first_name or u.username or "User"
            except Exception:
                target_name = f"User {uid}"
    else:
        bot.reply_to(
            message,
            "Usage: reply + /unmute, or /unmute <user_id>, or /unmute @username, or /unmute <name>.",
        )
        return

    perms = types.ChatPermissions(
        can_send_messages=True,
        can_send_media_messages=True,
        can_send_polls=True,
        can_send_other_messages=True,
        can_add_web_page_previews=True,
        can_invite_users=True,
    )

    try:
        bot.restrict_chat_member(
            chat.id,
            target_id,
            permissions=perms,
            until_date=0,
        )
    except Exception as e:
        print(f"[unmute error] {e}")
        bot.reply_to(message, "Failed to unmute user. Maybe I don't have enough rights.")
        return

    username_html = html.escape(target_name)
    mention = f'<a href="tg://user?id={target_id}">{username_html}</a>'
    info_text = f"{mention} [{target_id}] has been unmuted."

    bot.send_message(chat.id, info_text, parse_mode="HTML")


@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("unmutebtn:"))
def handle_unmute_button(callback_query):
    try:
        data = callback_query.data.split(":", 2)
        if len(data) != 3:
            return
        _, chat_id_str, user_id_str = data
        chat_id = int(chat_id_str)
        target_id = int(user_id_str)
    except Exception as e:
        print(f"[unmutebtn parse error] {e}")
        return

    actor = callback_query.from_user

    if not is_user_admin(chat_id, actor.id):
        bot.answer_callback_query(
            callback_query.id,
            "Only group admins/owner can use this button.",
            show_alert=True,
        )
        return

    perms = types.ChatPermissions(
        can_send_messages=True,
        can_send_media_messages=True,
        can_send_polls=True,
        can_send_other_messages=True,
        can_add_web_page_previews=True,
        can_invite_users=True,
    )

    try:
        bot.restrict_chat_member(
            chat_id,
            target_id,
            permissions=perms,
            until_date=0,
        )
    except Exception as e:
        print(f"[unmutebtn unmute error] {e}")
        bot.answer_callback_query(
            callback_query.id,
            "Failed to unmute user. Maybe I don't have enough rights.",
            show_alert=True,
        )
        return

    try:
        member = bot.get_chat_member(chat_id, target_id)
        u = member.user
        target_name = u.first_name or u.username or "User"
    except Exception:
        target_name = f"User {target_id}"

    username_html = html.escape(target_name)
    mention = f'<a href="tg://user?id={target_id}">{username_html}</a>'
    text = f"{mention} [{target_id}] has been unmuted."

    try:
        bot.send_message(chat_id, text, parse_mode="HTML")
    except Exception as e:
        print(f"[unmutebtn send msg error] {e}")

    bot.answer_callback_query(callback_query.id, "User unmuted.")


# =============== ADMIN PROMOTE / DEMOTE ===============

@bot.message_handler(commands=["admin"])
def handle_promote_admin(message):
    chat = message.chat

    if chat.type not in ("group", "supergroup"):
        bot.reply_to(message, "Use /admin only in groups.")
        return
    if not is_user_admin(chat.id, message.from_user.id):
        bot.reply_to(message, "Only group admins/owner can use /admin.")
        return
    if not message.reply_to_message or not message.reply_to_message.from_user:
        bot.reply_to(message, "Reply to a user’s message and type /admin.")
        return

    target = message.reply_to_message.from_user
    if target.id == BOT_ID:
        pass
    if is_user_admin(chat.id, target.id):
        bot.reply_to(message, "This user is already an admin.")
        return

    try:
        bot.promote_chat_member(
            chat.id,
            target.id,
            can_manage_chat=True,
            can_delete_messages=True,
            can_manage_topics=True,
            can_restrict_members=True,
            can_promote_members=False,
            can_change_info=True,
            can_invite_users=True,
            can_pin_messages=True,
        )
        bot.reply_to(
            message,
            f"{target.first_name or target.username or 'User'} is now an admin."
        )
    except Exception as e:
        print(f"[admin promote error] {e}")
        bot.reply_to(
            message,
            "Could not promote user to admin. Make sure I have permission to add admins."
        )


@bot.message_handler(commands=["unadmin"])
def handle_unadmin(message):
    chat = message.chat

    if chat.type not in ("group", "supergroup"):
        bot.reply_to(message, "Use /unadmin only in groups.")
        return
    if not is_user_admin(chat.id, message.from_user.id):
        bot.reply_to(message, "Only group admins/owner can use /unadmin.")
        return
    if not message.reply_to_message or not message.reply_to_message.from_user:
        bot.reply_to(message, "Reply to an admin’s message and type /unadmin.")
        return

    target = message.reply_to_message.from_user

    try:
        member = bot.get_chat_member(chat.id, target.id)
        status = member.status
    except Exception as e:
        print(f"[unadmin get_chat_member error] {e}")
        bot.reply_to(message, f"Could not get user info: {e}")
        return

    if status == "creator":
        bot.reply_to(message, "The group owner cannot be demoted.")
        return

    if status != "administrator":
        bot.reply_to(message, "This user is not an admin.")
        return

    try:
        bot.promote_chat_member(
            chat.id,
            target.id,
            can_manage_chat=False,
            can_delete_messages=False,
            can_manage_topics=False,
            can_restrict_members=False,
            can_promote_members=False,
            can_change_info=False,
            can_invite_users=False,
            can_pin_messages=False,
        )
        bot.reply_to(
            message,
            f"{target.first_name or target.username or 'User'} is now a normal member."
        )
    except Exception as e:
        print(f"[unadmin error] {e}")
        bot.reply_to(
            message,
            "Could not demote this admin. Make sure I have permission to manage admins."
        )


# =============== /tr ===============

@bot.message_handler(commands=["tr"])
def handle_translate(message):
    text = message.text or ""
    parts = text.split(maxsplit=1)

    if message.reply_to_message and not (len(parts) > 1):
        src = message.reply_to_message.text or ""
    else:
        if len(parts) < 2:
            bot.reply_to(
                message,
                "Usage:\n"
                "- Reply to a message and type /tr\n"
                "- Or: /tr <sentence>",
            )
            return
        src = parts[1]

    if not src.strip():
        bot.reply_to(message, "Please provide some text to translate.")
        return

    translated = translate_to_english(src)
    bot.reply_to(message, translated)


# =============== /quote ===============

@bot.message_handler(commands=["quote"])
def handle_quote(message):
    q = get_random_quote()
    bot.reply_to(message, f"💬 {q}")


# =============== /weather (with AQI) ===============

@bot.message_handler(commands=["weather"])
def handle_weather(message):
    if not WEATHER_API_KEY or WEATHER_API_KEY == "YOUR_WEATHER_API_KEY_HERE":
        bot.reply_to(
            message,
            "Weather feature is not configured. Set WEATHER_API_KEY first.",
        )
        return

    text = message.text or ""
    parts = text.split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(message, "Usage: /weather <city name>\nExample: /weather Delhi")
        return

    city = parts[1].strip()
    try:
        resp = requests.get(
            "https://api.openweathermap.org/data/2.5/weather",
            params={"q": city, "appid": WEATHER_API_KEY, "units": "metric"},
            timeout=10,
        )
        if resp.status_code != 200:
            bot.reply_to(message, "City not found. Please check the spelling and try again.")
            return

        data = resp.json()
        name = data.get("name", city)
        sys_info = data.get("sys", {})
        country = sys_info.get("country", "")
        main = data.get("main", {})
        weather_list = data.get("weather", [])
        desc = weather_list[0]["description"] if weather_list else "unknown"
        temp = main.get("temp")
        feels = main.get("feels_like")
        hum = main.get("humidity")
        wind = data.get("wind", {}).get("speed")

        coord = data.get("coord", {})
        lat = coord.get("lat")
        lon = coord.get("lon")

        aqi_line = "AQI: N/A"
        if lat is not None and lon is not None:
            try:
                aqi_resp = requests.get(
                    "https://api.openweathermap.org/data/2.5/air_pollution",
                    params={"lat": lat, "lon": lon, "appid": WEATHER_API_KEY},
                    timeout=10,
                )
                aqi_json = aqi_resp.json()
                comp = (
                    aqi_json.get("list", [{}])[0]
                    .get("components", {})
                )
                aqi_value, pollutant = get_us_aqi_from_components(comp)
                if aqi_value is not None:
                    cat = aqi_category(aqi_value)
                    aqi_line = (
                        f"AQI: {aqi_value} – {cat}\n"
                        f"Primary pollutant: {pollutant}"
                    )
            except Exception as e:
                print(f"[weather aqi error] {e}")

        reply = (
            f"🌦 Weather in {name}, {country}\n"
            f"Condition: {desc}\n"
            f"Temp: {temp}°C (feels like {feels}°C)\n"
            f"Humidity: {hum}%\n"
            f"Wind: {wind} m/s\n"
            f"🌫 {aqi_line}"
        )
        bot.reply_to(message, reply)

    except Exception as e:
        print(f"[weather error] {e}")
        bot.reply_to(message, "There was an error while fetching the weather data.")


# =============== /imagine ===============

@bot.message_handler(commands=["imagine"])
def handle_imagine(message):
    text = message.text or ""
    parts = text.split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(
            message,
            "Usage: /imagine <prompt>\nExample: /imagine zero two under sakura tree",
        )
        return

    prompt = parts[1].strip()
    ask = (
        "Create a very short (1–2 sentences) vivid description of an anime-style scene "
        "for this prompt, as if you are painting it: " + prompt
    )
    desc = get_ai_reply(message.chat.id, ask, message.from_user.first_name or "")
    bot.reply_to(
        message,
        f"🎨 Imagine this:\n{desc}\n\n(Real image generation feature may come in the future.)"
    )


# =============== /time ===============

@bot.message_handler(commands=["time"])
def handle_time(message):
    ist = timezone(timedelta(hours=5, minutes=30))
    now = datetime.now(ist)
    text = now.strftime("🕒 Current time (IST): %d-%m-%Y %H:%M:%S")
    bot.reply_to(message, text)


# =============== /mmf : meme text on ANY sticker ===============

@bot.message_handler(commands=["mmf"])
def handle_mmf(message):
    if not message.reply_to_message or not message.reply_to_message.sticker:
        bot.reply_to(
            message,
            "Reply to any <b>sticker</b> (static/animated/video) with /mmf <text>.",
            parse_mode="HTML",
        )
        return

    st = message.reply_to_message.sticker

    text = message.text or ""
    parts = text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        bot.reply_to(message, "Use /mmf <text> after replying to a sticker.")
        return

    caption_text = parts[1].strip()

    try:
        if (getattr(st, "is_animated", False) or getattr(st, "is_video", False)) and st.thumb:
            source_file_id = st.thumb.file_id
        else:
            source_file_id = st.file_id

        file_info = bot.get_file(source_file_id)
        file_bytes = bot.download_file(file_info.file_path)

        img = Image.open(io.BytesIO(file_bytes))
        img = _draw_text_on_image(img, caption_text)

        out = io.BytesIO()
        out.name = "mmf.png"
        img.save(out, format="PNG")
        out.seek(0)

        bot.send_photo(
            message.chat.id,
            out,
            reply_to_message_id=message.reply_to_message.message_id,
        )
    except Exception as e:
        print(f"[mmf error] {e}")
        bot.reply_to(
            message,
            "There was an error while writing text on the sticker.\n"
            "Remember: the result will be a static image (not animated)."
        )


# =============== /tts : text to speech ===============

@bot.message_handler(commands=["tts"])
def handle_tts(message):
    user_id = message.from_user.id
    now = time.time()
    last = tts_last_use.get(user_id, 0)
    if now - last < TTS_COOLDOWN:
        wait = int(TTS_COOLDOWN - (now - last))
        bot.reply_to(
            message,
            f"Please slow down 😅 You can use /tts only once every {TTS_COOLDOWN} seconds.\n"
            f"Try again in {wait} seconds."
        )
        return
    tts_last_use[user_id] = now

    text = message.text or ""
    parts = text.split(maxsplit=1)

    if message.reply_to_message and not (len(parts) > 1):
        src = message.reply_to_message.text or ""
    else:
        if len(parts) < 2:
            bot.reply_to(
                message,
                "Usage:\n"
                "- Reply to a text message and type /tts\n"
                "- Or: /tts <text>",
            )
            return
        src = parts[1]

    src = src.strip()
    if not src:
        bot.reply_to(message, "Please provide some text to convert to voice.")
        return

    if len(src) > 400:
        src = src[:400] + "..."

    chat_id = message.chat.id

    try:
        bot.send_chat_action(chat_id, "record_voice")
    except Exception:
        pass

    tmp_path = f"tts_{chat_id}_{message.message_id}.ogg"

    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }
    json_data = {
        "model": "gpt-4o-mini-tts",
        "input": src,
        "voice": "alloy",
        "format": "opus",
    }

    try:
        resp = requests.post(TTS_API_URL, headers=headers, json=json_data, timeout=60)
        try:
            resp.raise_for_status()
        except requests.HTTPError:
            if resp.status_code == 429:
                print("[tts error] 429 Too Many Requests")
                bot.reply_to(
                    message,
                    "My text-to-speech limit is currently exhausted (Too Many Requests).\n"
                    "Please wait a bit and try /tts again, or check your OpenAI usage."
                )
                return
            raise
        with open(tmp_path, "wb") as f:
            f.write(resp.content)
    except Exception as e:
        print(f"[tts error] {e}")
        bot.reply_to(message, "There was a problem generating the TTS (voice) message.")
        return

    try:
        with open(tmp_path, "rb") as f:
            bot.send_voice(
                chat_id,
                f,
                reply_to_message_id=message.message_id,
            )
    except Exception as e:
        print(f"[tts send_voice error] {e}")
        bot.reply_to(message, "There was a problem sending the voice message.")
    finally:
        try:
            os.remove(tmp_path)
        except Exception:
            pass


# =============== MAIN TEXT HANDLER (AI + AFK) ===============

@bot.message_handler(content_types=["text"])
def handle_text(message):
    chat_type = message.chat.type
    text = message.text or ""
    user = message.from_user
    user_id = user.id
    user_name = user.first_name or ""

    if user_id in afk_status and not text.startswith("/afk"):
        info = afk_status.pop(user_id)
        reason = info.get("reason", "")
        since = info.get("since", time.time())
        elapsed = format_duration(time.time() - since)
        name = info.get("name") or user_name or "User"

        if chat_type in ("group", "supergroup"):
            mention = f'<a href="tg://user?id={user_id}">{html.escape(name)}</a>'
            back_msg = f"{mention}, welcome back! You were AFK for {elapsed}."
            if reason:
                back_msg += f" (Reason: {html.escape(reason)})"
            bot.send_message(
                message.chat.id,
                back_msg,
                parse_mode="HTML",
                reply_to_message_id=message.message_id,
            )
        else:
            back_msg = f"Welcome back, {name}! You were AFK for {elapsed}."
            if reason:
                back_msg += f" (Reason: {reason})"
            bot.reply_to(message, back_msg)

    if text.startswith("/"):
        return

    greeting_type = detect_greeting_type(text)
    if greeting_type == "morning":
        bot.reply_to(message, f"Good morning, {user_name}. Have a great day.")
        return
    elif greeting_type == "night":
        bot.reply_to(message, f"Good night, {user_name}. Sleep well.")
        return

    if chat_type in ("group", "supergroup"):
        notified_ids = set()

        if message.reply_to_message and message.reply_to_message.from_user:
            target = message.reply_to_message.from_user
            if target.id in afk_status:
                info = afk_status[target.id]
                send_afk_notice(message.chat.id, target.id, info, reply_to_message_id=message.message_id)
                notified_ids.add(target.id)

        if message.entities:
            for ent in message.entities:
                if ent.type == "text_mention" and ent.user:
                    uid = ent.user.id
                    if uid in afk_status and uid not in notified_ids:
                        info = afk_status[uid]
                        send_afk_notice(message.chat.id, uid, info, reply_to_message_id=message.message_id)
                        notified_ids.add(uid)

        if message.entities:
            for ent in message.entities:
                if ent.type == "mention":
                    mention_text = message.text[ent.offset:ent.offset + ent.length].lower()
                    for uid, info in afk_status.items():
                        un = info.get("username")
                        if not un or uid in notified_ids:
                            continue
                        if mention_text == f"@{un}":
                            send_afk_notice(message.chat.id, uid, info, reply_to_message_id=message.message_id)
                            notified_ids.add(uid)

    chat_id = message.chat.id
    filters_for_chat = filters_per_chat.get(chat_id, {})
    lowered = text.lower()
    for keyword, payload in filters_for_chat.items():
        if keyword in lowered:
            send_filter_reply(message, payload)
            return

    if chat_type in ("group", "supergroup"):
        if not is_message_for_bot(message):
            return
    elif chat_type == "channel":
        return

    now = time.time()
    last_time = last_user_request.get(user_id, 0)
    if now - last_time < COOLDOWN_SECONDS:
        return
    last_user_request[user_id] = now

    print(f"[{chat_id}] ({chat_type}) @{BOT_USERNAME} used by {user_name}: {text}")

    try:
        bot.send_chat_action(chat_id, "typing")
    except Exception:
        pass

    reply_text = get_ai_reply(chat_id, text, user_name)
    bot.reply_to(message, reply_text)


# =============== STICKER HANDLER (AFK + RANDOM REPLY) ===============

@bot.message_handler(content_types=["sticker"])
def handle_sticker(message):
    chat_type = message.chat.type
    chat_id = message.chat.id

    user = message.from_user
    user_id = user.id
    user_name = user.first_name or user.username or "User"

    if user_id in afk_status:
        info = afk_status.pop(user_id)
        reason = info.get("reason", "")
        since = info.get("since", time.time())
        elapsed = format_duration(time.time() - since)
        name = info.get("name") or user_name

        if chat_type in ("group", "supergroup"):
            mention = f'<a href="tg://user?id={user_id}">{html.escape(name)}</a>'
            back_msg = f"{mention}, welcome back! You were AFK for {elapsed}."
            if reason:
                back_msg += f" (Reason: {html.escape(reason)})"
            try:
                bot.send_message(
                    chat_id,
                    back_msg,
                    parse_mode="HTML",
                    reply_to_message_id=message.message_id,
                )
            except Exception as e:
                print(f"[AFK back (sticker) msg error] {e}")
        else:
            back_msg = f"Welcome back, {name}! You were AFK for {elapsed}."
            if reason:
                back_msg += f" (Reason: {reason})"
            try:
                bot.send_message(
                    chat_id,
                    back_msg,
                    reply_to_message_id=message.message_id,
                )
            except Exception as e:
                print(f"[AFK back (sticker) DM error] {e}")

    file_id = message.sticker.file_id
    if file_id not in sticker_pool:
        sticker_pool.append(file_id)

    if chat_type == "channel":
        return

    if (
        message.reply_to_message
        and message.reply_to_message.from_user
        and message.reply_to_message.from_user.id == BOT_ID
    ):
        try:
            if sticker_pool:
                chosen = random.choice(sticker_pool)
            else:
                chosen = file_id

            bot.send_sticker(
                chat_id,
                chosen,
                reply_to_message_id=message.message_id,
            )
        except Exception as e:
            print(f"[sticker reply error] {e}")


# =============== USER TRACKING ===============

def load_users():
    global user_last_seen
    if os.path.exists(USERS_FILE):
        try:
            with open(USERS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                user_last_seen = {int(k): float(v) for k, v in data.items()}
        except Exception as e:
            print(f"Error loading users: {e}")

def save_users():
    try:
        with open(USERS_FILE, "w", encoding="utf-8") as f:
            json.dump(user_last_seen, f)
    except Exception as e:
        print(f"Error saving users: {e}")

def track_user(messages):
    changed = False
    now = time.time()
    for m in messages:
        if m.from_user:
            uid = m.from_user.id
            last = user_last_seen.get(uid, 0)
            if now - last > 300:
                user_last_seen[uid] = now
                changed = True
    if changed:
        save_users()

def update_bot_profile_user_count():
    pass

# =============== STARTUP ===============

load_users()
update_bot_profile_user_count()
bot.set_update_listener(track_user)

print(f"Bot @{BOT_USERNAME} is running...")
bot.infinity_polling(skip_pending=True, timeout=60, long_polling_timeout=60)