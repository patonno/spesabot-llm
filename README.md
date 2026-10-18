# 🛒 Spesabot

**Your Italian grocery-list assistant on Telegram — just talk, it handles the rest.**

Send a voice note in Italian and Spesabot figures out what to add, remove, or mark as bought. No typing required.

---

## ✨ What it does

### 🎙️ Voice notes, understood
Send a voice message like *"aggiungi latte, togli le uova e prendi 2 kg di mele"* and the bot will update your list automatically. It uses Google Gemini to understand natural Italian speech.

### 📋 Smart grocery list
- **Add items** with optional quantities (e.g. "2 litri di latte")
- **Remove items** — by name or by amount (subtracts from existing quantity)
- **Mark everything as bought** — with optional exceptions ("bought everything except bread")
- **Clear the list** entirely with a confirmation step so you don't do it by accident

### 🤝 Shared lists for groups
Add the bot to a Telegram group and everyone in the group shares the same list. Each chat (or group) has its own separate list.

### ⚡ Handles edge cases gracefully
- **Duplicate items**: if you add something already on the list, the bot asks how to handle it — replace, sum, or ignore
- **Ambiguous removals**: if it's unclear how much to remove (e.g. mixed units), the bot asks before changing anything
- **Exactly-once processing**: voice notes are never applied twice, even if Telegram redelivers them

### 🔒 Private & secure
- Voice audio is processed entirely in memory and **never saved to disk**
- Only explicitly allowed chats can use the bot
- Unauthorized users are silently ignored (no error messages that could leak info)

---

## 💬 Commands

| Command | What it does |
|---------|-------------|
| `/start` | Welcome message and quick intro |
| `/lista` | Show the current grocery list |
| `/aiuto` | Help and usage tips |
| `/svuota` | Clear the entire list (asks for confirmation) |

Or just **send a voice note** — that's the main way to use it!

---

## 🚀 Getting started

1. Create a bot with [BotFather](https://t.me/BotFather) on Telegram and copy the token
2. Get a [Google Gemini API key](https://aistudio.google.com/)
3. Find your Telegram chat ID (the bot will need to know who's allowed to use it)

Then run:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
# Edit .env and fill in your TELEGRAM_BOT_TOKEN, ALLOWED_CHAT_IDS, and GEMINI_API_KEY
.\.venv\Scripts\python.exe bot.py
```

The bot runs in the background with no public server or domain needed. Stop it anytime with **Ctrl+C**.

---

## ⚙️ Configuration (`.env`)

| Variable | Description |
|----------|-------------|
| `TELEGRAM_BOT_TOKEN` | Your bot token from BotFather |
| `ALLOWED_CHAT_IDS` | Comma-separated list of chat IDs allowed to use the bot |
| `GEMINI_API_KEY` | Your Google Gemini API key |
| `GEMINI_MODEL` | *(optional)* Gemini model to use (default: `gemini-2.0-flash`) |
| `DATABASE_PATH` | *(optional)* Path to the SQLite database file (default: `spesabot.sqlite3`) |

Group chat IDs are negative numbers. Members of an allowed group automatically share the same list.

