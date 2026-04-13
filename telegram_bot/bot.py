"""
Family Budget Telegram Bot
Tracks weekly expenses per family member with shared budget limit.
"""

import os
import json
import logging
from datetime import datetime, timedelta
from collections import defaultdict
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, ConversationHandler, filters
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ── States for ConversationHandler ──────────────────────────────────────────
SET_BUDGET, ADD_AMOUNT, ADD_DESC, DELETE_CONFIRM = range(4)

# ── Persistent storage (JSON file) ──────────────────────────────────────────
DATA_FILE = "budget_data.json"

def load_data() -> dict:
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_data(data: dict):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def get_group(data: dict, chat_id: str) -> dict:
    """Return (and initialise if needed) the group record."""
    if chat_id not in data:
        data[chat_id] = {
            "weekly_budget": 5000,
            "week_start": get_week_start(),
            "expenses": [],
            "members": {}
        }
    return data[chat_id]

def get_week_start() -> str:
    today = datetime.now()
    monday = today - timedelta(days=today.weekday())
    return monday.strftime("%Y-%m-%d")

def is_current_week(expense_date: str, week_start: str) -> bool:
    try:
        exp_dt = datetime.strptime(expense_date[:10], "%Y-%m-%d")
        wk_dt  = datetime.strptime(week_start, "%Y-%m-%d")
        return wk_dt <= exp_dt < wk_dt + timedelta(days=7)
    except Exception:
        return False

def week_expenses(group: dict) -> list:
    ws = group.get("week_start", get_week_start())
    return [e for e in group["expenses"] if is_current_week(e["date"], ws)]

def total_spent(group: dict) -> float:
    return sum(e["amount"] for e in week_expenses(group))

def member_spent(group: dict, user_id: str) -> float:
    return sum(e["amount"] for e in week_expenses(group) if e["user_id"] == user_id)

def remaining(group: dict) -> float:
    return group["weekly_budget"] - total_spent(group)

def bar(spent: float, budget: float, width: int = 10) -> str:
    """Visual progress bar."""
    pct = min(spent / budget, 1.0) if budget > 0 else 1.0
    filled = round(pct * width)
    bar_str = "█" * filled + "░" * (width - filled)
    emoji = "🟢" if pct < 0.6 else ("🟡" if pct < 0.85 else "🔴")
    return f"{emoji} [{bar_str}] {pct*100:.0f}%"

def fmt_money(amount: float) -> str:
    return f"{amount:,.0f} ₴".replace(",", " ")

def ensure_week_reset(group: dict):
    """Auto-reset if current week is newer than stored week_start."""
    current_ws = get_week_start()
    if group.get("week_start") != current_ws:
        group["week_start"] = current_ws

# ────────────────────────────────────────────────────────────────────────────
# /start
# ────────────────────────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Вітаю! Я бот для відстеження *сімейних витрат* 💰\n\n"
        "📋 *Команди:*\n"
        "➕ /add — додати витрату\n"
        "📊 /stats — статистика тижня\n"
        "👤 /my — мої витрати\n"
        "⚙️ /setbudget — встановити бюджет\n"
        "🗑 /delete — видалити останню витрату\n"
        "📅 /history — історія витрат\n"
        "❓ /help — допомога\n\n"
        "Додайте мене до сімейного чату і встановіть тижневий бюджет командою /setbudget",
        parse_mode="Markdown"
    )

# ────────────────────────────────────────────────────────────────────────────
# /setbudget  (conversation)
# ────────────────────────────────────────────────────────────────────────────
async def setbudget_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    chat_id = str(update.effective_chat.id)
    group = get_group(data, chat_id)
    await update.message.reply_text(
        f"💰 Поточний тижневий бюджет: *{fmt_money(group['weekly_budget'])}*\n\n"
        "Введіть новий бюджет на тиждень (в гривнях):",
        parse_mode="Markdown"
    )
    return SET_BUDGET

async def setbudget_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().replace(",", ".").replace(" ", "")
    try:
        amount = float(text)
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Введіть коректну суму, наприклад: 5000")
        return SET_BUDGET

    data = load_data()
    chat_id = str(update.effective_chat.id)
    group = get_group(data, chat_id)
    group["weekly_budget"] = amount
    save_data(data)

    await update.message.reply_text(
        f"✅ Тижневий бюджет встановлено: *{fmt_money(amount)}*",
        parse_mode="Markdown"
    )
    return ConversationHandler.END

# ────────────────────────────────────────────────────────────────────────────
# /add  (conversation)
# ────────────────────────────────────────────────────────────────────────────
async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "💸 Введіть суму витрати (в гривнях):\n"
        "_Наприклад: 250 або 1 200.50_",
        parse_mode="Markdown"
    )
    return ADD_AMOUNT

async def add_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().replace(",", ".").replace(" ", "")
    try:
        amount = float(text)
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Введіть коректну суму, наприклад: 350")
        return ADD_AMOUNT

    context.user_data["pending_amount"] = amount
    await update.message.reply_text(
        "📝 Вкажіть категорію або опис витрати:\n"
        "_Наприклад: Продукти, Аптека, Кафе, Бензин..._",
        parse_mode="Markdown"
    )
    return ADD_DESC

async def add_desc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    desc = update.message.text.strip()
    amount = context.user_data.get("pending_amount", 0)

    user = update.effective_user
    data = load_data()
    chat_id = str(update.effective_chat.id)
    group = get_group(data, chat_id)
    ensure_week_reset(group)

    # register member
    user_id = str(user.id)
    if user_id not in group["members"]:
        group["members"][user_id] = {"name": user.full_name, "username": user.username or ""}
    else:
        group["members"][user_id]["name"] = user.full_name

    expense = {
        "id": len(group["expenses"]) + 1,
        "user_id": user_id,
        "amount": amount,
        "desc": desc,
        "date": datetime.now().strftime("%Y-%m-%d %H:%M")
    }
    group["expenses"].append(expense)
    save_data(data)

    rem = remaining(group)
    budget = group["weekly_budget"]
    spent_total = total_spent(group)
    my_spent = member_spent(group, user_id)

    over = rem < 0
    rem_text = f"⚠️ *Перевитрата на {fmt_money(abs(rem))}!*" if over else f"Залишок: *{fmt_money(rem)}*"

    await update.message.reply_text(
        f"✅ Записано: *{fmt_money(amount)}* — {desc}\n\n"
        f"{bar(spent_total, budget)}\n"
        f"Витрачено: {fmt_money(spent_total)} / {fmt_money(budget)}\n"
        f"{rem_text}\n\n"
        f"👤 Твої витрати цього тижня: *{fmt_money(my_spent)}*",
        parse_mode="Markdown"
    )
    return ConversationHandler.END

# ────────────────────────────────────────────────────────────────────────────
# /stats
# ────────────────────────────────────────────────────────────────────────────
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    chat_id = str(update.effective_chat.id)
    group = get_group(data, chat_id)
    ensure_week_reset(group)

    budget = group["weekly_budget"]
    spent_total = total_spent(group)
    rem = remaining(group)
    expenses = week_expenses(group)
    ws = group["week_start"]
    we = (datetime.strptime(ws, "%Y-%m-%d") + timedelta(days=6)).strftime("%d.%m")
    ws_fmt = datetime.strptime(ws, "%Y-%m-%d").strftime("%d.%m")

    over = rem < 0
    rem_line = f"❗ Перевитрата: *{fmt_money(abs(rem))}*" if over else f"💚 Залишок: *{fmt_money(rem)}*"

    text = (
        f"📊 *Статистика тижня* ({ws_fmt}–{we})\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 Бюджет: *{fmt_money(budget)}*\n"
        f"💸 Витрачено: *{fmt_money(spent_total)}*\n"
        f"{rem_line}\n"
        f"{bar(spent_total, budget)}\n\n"
    )

    # per-member breakdown
    if group["members"]:
        text += "👥 *По членах родини:*\n"
        member_totals = defaultdict(float)
        for e in expenses:
            member_totals[e["user_id"]] += e["amount"]

        for uid, member in group["members"].items():
            ms = member_totals.get(uid, 0)
            pct = (ms / budget * 100) if budget > 0 else 0
            name = member["name"]
            text += f"  • {name}: *{fmt_money(ms)}* ({pct:.0f}%)\n"

    if not expenses:
        text += "\n_Цього тижня витрат ще немає._"

    await update.message.reply_text(text, parse_mode="Markdown")

# ────────────────────────────────────────────────────────────────────────────
# /my — personal stats
# ────────────────────────────────────────────────────────────────────────────
async def my_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = str(user.id)
    data = load_data()
    chat_id = str(update.effective_chat.id)
    group = get_group(data, chat_id)
    ensure_week_reset(group)

    my_expenses = [e for e in week_expenses(group) if e["user_id"] == user_id]
    my_total = sum(e["amount"] for e in my_expenses)
    budget = group["weekly_budget"]
    pct = (my_total / budget * 100) if budget > 0 else 0

    text = (
        f"👤 *Твої витрати цього тижня*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Всього: *{fmt_money(my_total)}* ({pct:.0f}% бюджету)\n\n"
    )

    if my_expenses:
        text += "📋 *Деталі:*\n"
        for e in sorted(my_expenses, key=lambda x: x["date"], reverse=True):
            date_short = e["date"][5:16]  # MM-DD HH:MM
            text += f"  `{date_short}` — {fmt_money(e['amount'])} — {e['desc']}\n"
    else:
        text += "_Цього тижня витрат ще немає._"

    await update.message.reply_text(text, parse_mode="Markdown")

# ────────────────────────────────────────────────────────────────────────────
# /delete — remove last personal expense
# ────────────────────────────────────────────────────────────────────────────
async def delete_last(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    data = load_data()
    chat_id = str(update.effective_chat.id)
    group = get_group(data, chat_id)

    my_expenses = [e for e in group["expenses"] if e["user_id"] == user_id]
    if not my_expenses:
        await update.message.reply_text("❌ У тебе немає витрат для видалення.")
        return

    last = my_expenses[-1]
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Так, видалити", callback_data=f"del_{last['id']}"),
            InlineKeyboardButton("❌ Скасувати", callback_data="del_cancel")
        ]
    ])
    await update.message.reply_text(
        f"🗑 Видалити останню витрату?\n\n"
        f"*{fmt_money(last['amount'])}* — {last['desc']}\n"
        f"_{last['date']}_",
        parse_mode="Markdown",
        reply_markup=keyboard
    )

async def delete_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "del_cancel":
        await query.edit_message_text("❌ Видалення скасовано.")
        return

    expense_id = int(query.data.replace("del_", ""))
    data = load_data()
    chat_id = str(update.effective_chat.id)
    group = get_group(data, chat_id)

    before = len(group["expenses"])
    group["expenses"] = [e for e in group["expenses"] if e["id"] != expense_id]
    after = len(group["expenses"])

    if before == after:
        await query.edit_message_text("❌ Витрату не знайдено.")
        return

    save_data(data)
    rem = remaining(group)
    await query.edit_message_text(
        f"✅ Витрату видалено.\n\n"
        f"💚 Новий залишок: *{fmt_money(rem)}*",
        parse_mode="Markdown"
    )

# ────────────────────────────────────────────────────────────────────────────
# /history
# ────────────────────────────────────────────────────────────────────────────
async def history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    chat_id = str(update.effective_chat.id)
    group = get_group(data, chat_id)
    ensure_week_reset(group)

    expenses = week_expenses(group)
    if not expenses:
        await update.message.reply_text("📋 Цього тижня витрат ще немає.")
        return

    text = "📋 *Всі витрати цього тижня:*\n━━━━━━━━━━━━━━━━━━━━\n"
    for e in sorted(expenses, key=lambda x: x["date"], reverse=True):
        member = group["members"].get(e["user_id"], {})
        name = member.get("name", "Невідомий")
        first_name = name.split()[0] if name else "?"
        date_short = e["date"][5:16]
        text += f"`{date_short}` *{first_name}*: {fmt_money(e['amount'])} — {e['desc']}\n"

    await update.message.reply_text(text, parse_mode="Markdown")

# ────────────────────────────────────────────────────────────────────────────
# /help
# ────────────────────────────────────────────────────────────────────────────
async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "❓ *Довідка — Сімейний Бюджет Бот*\n\n"
        "🤖 Цей бот допомагає відстежувати спільний тижневий бюджет родини.\n\n"
        "📋 *Команди:*\n"
        "• /add — записати нову витрату\n"
        "• /stats — загальна статистика тижня\n"
        "• /my — твої особисті витрати\n"
        "• /history — всі витрати тижня з деталями\n"
        "• /setbudget — змінити тижневий ліміт\n"
        "• /delete — видалити останню свою витрату\n\n"
        "💡 *Поради:*\n"
        "• Додайте бота до групового чату родини\n"
        "• Встановіть спільний тижневий бюджет\n"
        "• Кожен член родини записує свої витрати\n"
        "• Тиждень скидається автоматично щопонеділка\n",
        parse_mode="Markdown"
    )

# ────────────────────────────────────────────────────────────────────────────
# cancel
# ────────────────────────────────────────────────────────────────────────────
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Скасовано.")
    return ConversationHandler.END

# ────────────────────────────────────────────────────────────────────────────
# Main
# ────────────────────────────────────────────────────────────────────────────
async def main():
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN environment variable not set!")

    app = Application.builder().token(token).build()

    # setbudget conversation
    budget_conv = ConversationHandler(
        entry_points=[CommandHandler("setbudget", setbudget_start)],
        states={SET_BUDGET: [MessageHandler(filters.TEXT & ~filters.COMMAND, setbudget_receive)]},
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    # add expense conversation
    add_conv = ConversationHandler(
        entry_points=[CommandHandler("add", add_start)],
        states={
            ADD_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_amount)],
            ADD_DESC:   [MessageHandler(filters.TEXT & ~filters.COMMAND, add_desc)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help",  help_cmd))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("my",    my_stats))
    app.add_handler(CommandHandler("history", history))
    app.add_handler(CommandHandler("delete", delete_last))
    app.add_handler(CallbackQueryHandler(delete_callback, pattern=r"^del_"))
    app.add_handler(budget_conv)
    app.add_handler(add_conv)

    logger.info("🤖 Bot started!")
    async with app:
        await app.start()
        await app.updater.start_polling()
        logger.info("✅ Polling started. Press Ctrl+C to stop.")
        await asyncio.Event().wait()
        await app.updater.stop()
        await app.stop()

if __name__ == "__main__":
    import asyncio
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped.")