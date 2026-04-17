"""
Family Budget Telegram Bot — Enhanced with AI (OpenRouter)
─────────────────────────────────────────────────────────
Нові функції:
  • /income      — додати дохід
  • /setlimit    — ліміт витрат по категорії (напр. Продукти)
  • /categories  — розбивка витрат по категоріях
  • /ai          — ШІ-аналітика, поради, тижневий звіт (OpenRouter)
  • Авто-категоризація кожної витрати через ШІ

Змінні середовища:
  TELEGRAM_BOT_TOKEN   — токен бота
  OPENROUTER_API_KEY   — ключ OpenRouter (https://openrouter.ai/)
"""

import os
import json
import logging
import asyncio
import httpx
from datetime import datetime, timedelta
from collections import defaultdict

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, ConversationHandler, filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── Conversation states ───────────────────────────────────────────────────────
(
    SET_BUDGET,
    ADD_AMOUNT, ADD_DESC,
    SET_LIMIT_CAT, SET_LIMIT_AMOUNT,
    ADD_INCOME_AMOUNT, ADD_INCOME_DESC,
) = range(7)

# ── Config ────────────────────────────────────────────────────────────────────
DATA_FILE          = "budget_data.json"
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL   = "openai/gpt-4o-mini"   # дешева та швидка модель

# ── Категорії витрат ─────────────────────────────────────────────────────────
CATEGORIES = [
    "🛒 Продукти",
    "🍽 Кафе/Ресторани",
    "🚗 Транспорт",
    "💊 Аптека/Здоров'я",
    "👗 Одяг",
    "🏠 Комунальні",
    "📱 Зв'язок/Інтернет",
    "🎮 Розваги",
    "📚 Освіта",
    "🐾 Тварини",
    "💄 Краса",
    "🔧 Ремонт/Техніка",
    "💰 Інше",
]

# ═══════════════════════════════════════════════════════════════════════════════
# Зберігання даних
# ═══════════════════════════════════════════════════════════════════════════════

def load_data() -> dict:
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_data(data: dict):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_group(data: dict, chat_id: str) -> dict:
    """Повертає запис групи; ініціалізує якщо відсутній."""
    if chat_id not in data:
        data[chat_id] = {
            "weekly_budget": 5000,
            "week_start": get_week_start(),
            "expenses": [],
            "incomes": [],
            "members": {},
            "category_limits": {},   # {"🛒 Продукти": 2000, ...}
        }
    g = data[chat_id]
    # міграція старих даних
    g.setdefault("incomes", [])
    g.setdefault("category_limits", {})
    return g


# ═══════════════════════════════════════════════════════════════════════════════
# Допоміжні функції
# ═══════════════════════════════════════════════════════════════════════════════

def get_week_start() -> str:
    today = datetime.now()
    monday = today - timedelta(days=today.weekday())
    return monday.strftime("%Y-%m-%d")


def is_current_week(date_str: str, week_start: str) -> bool:
    try:
        exp_dt = datetime.strptime(date_str[:10], "%Y-%m-%d")
        wk_dt  = datetime.strptime(week_start, "%Y-%m-%d")
        return wk_dt <= exp_dt < wk_dt + timedelta(days=7)
    except Exception:
        return False


def ensure_week_reset(group: dict):
    if group.get("week_start") != get_week_start():
        group["week_start"] = get_week_start()


def week_expenses(group: dict) -> list:
    ws = group.get("week_start", get_week_start())
    return [e for e in group["expenses"] if is_current_week(e["date"], ws)]


def week_incomes(group: dict) -> list:
    ws = group.get("week_start", get_week_start())
    return [i for i in group.get("incomes", []) if is_current_week(i["date"], ws)]


def total_spent(group: dict) -> float:
    return sum(e["amount"] for e in week_expenses(group))


def total_income(group: dict) -> float:
    return sum(i["amount"] for i in week_incomes(group))


def member_spent(group: dict, user_id: str) -> float:
    return sum(e["amount"] for e in week_expenses(group) if e["user_id"] == user_id)


def remaining(group: dict) -> float:
    return group["weekly_budget"] - total_spent(group)


def category_spent_week(group: dict, category: str) -> float:
    return sum(e["amount"] for e in week_expenses(group) if e.get("category") == category)


def bar(spent: float, budget: float, width: int = 10) -> str:
    pct = min(spent / budget, 1.0) if budget > 0 else 1.0
    filled = round(pct * width)
    b = "█" * filled + "░" * (width - filled)
    emoji = "🟢" if pct < 0.6 else ("🟡" if pct < 0.85 else "🔴")
    return f"{emoji} [{b}] {pct*100:.0f}%"


def fmt(amount: float) -> str:
    return f"{amount:,.0f} ₴".replace(",", "\u202f")


# ═══════════════════════════════════════════════════════════════════════════════
# OpenRouter AI
# ═══════════════════════════════════════════════════════════════════════════════

async def _openrouter(prompt: str, max_tokens: int = 100) -> str:
    """Запит до OpenRouter. Повертає текст відповіді або порожній рядок."""
    if not OPENROUTER_API_KEY:
        return ""
    try:
        async with httpx.AsyncClient(timeout=25) as client:
            r = await client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "https://family-budget-bot",
                },
                json={
                    "model": OPENROUTER_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": max_tokens,
                },
            )
            data = r.json()
            return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        logger.error(f"OpenRouter error: {e}")
        return ""


async def ai_categorize(description: str) -> str:
    """Авто-категоризація витрати через ШІ."""
    if not OPENROUTER_API_KEY:
        return "💰 Інше"

    cats = "\n".join(CATEGORIES)
    prompt = (
        f'Визнач категорію витрати за описом: "{description}"\n\n'
        f"Категорії:\n{cats}\n\n"
        "Відповідай ТІЛЬКИ точною назвою категорії з списку вище, без пояснень."
    )
    result = await _openrouter(prompt, max_tokens=30)

    # знаходимо найближчий збіг
    for cat in CATEGORIES:
        if cat in result or result in cat:
            return cat
    return "💰 Інше"


async def ai_weekly_report(group: dict) -> str:
    """Тижнева аналітика та фінансові поради від ШІ."""
    if not OPENROUTER_API_KEY:
        return (
            "⚠️ *OpenRouter API ключ не налаштовано.*\n\n"
            "Додайте `OPENROUTER_API_KEY` у змінні середовища і перезапустіть бота.\n"
            "Отримати ключ безкоштовно: https://openrouter.ai/"
        )

    expenses = week_expenses(group)
    incomes  = week_incomes(group)
    budget   = group["weekly_budget"]
    spent    = total_spent(group)
    inc      = total_income(group)
    limits   = group.get("category_limits", {})

    cat_totals: dict[str, float] = defaultdict(float)
    for e in expenses:
        cat_totals[e.get("category", "💰 Інше")] += e["amount"]

    cats_str   = "\n".join(f"- {c}: {a:.0f} ₴" for c, a in sorted(cat_totals.items(), key=lambda x: -x[1])) or "немає витрат"
    income_str = "\n".join(f"- {i['desc']}: {i['amount']:.0f} ₴" for i in incomes) or "не вказано"
    limits_str = (
        "\n".join(
            f"- {c}: ліміт {l:.0f} ₴, витрачено {cat_totals.get(c, 0):.0f} ₴ "
            f"({'ПЕРЕВИЩЕНО' if cat_totals.get(c, 0) >= l else f'{cat_totals.get(c, 0)/l*100:.0f}%'})"
            for c, l in limits.items()
        ) or "не встановлено"
    )

    prompt = f"""Ти фінансовий радник сімейного бюджету. Зроби аналіз і дай конкретні поради.

ТИЖНЕВИЙ ЗВІТ:
- Бюджет на тиждень: {budget:.0f} ₴
- Витрачено: {spent:.0f} ₴ ({spent/budget*100:.0f}% бюджету)
- Доходи цього тижня: {inc:.0f} ₴
- Баланс (дохід − витрати): {inc - spent:.0f} ₴

ВИТРАТИ ПО КАТЕГОРІЯХ:
{cats_str}

КАТЕГОРІЙНІ ЛІМІТИ:
{limits_str}

Відповідай строго за цим форматом (без зайвого тексту):
📊 *Резюме*
[2–3 речення про стан бюджету]

⚠️ *Тривожні сигнали*
[bullet-список або «немає»]

💡 *Поради на наступний тиждень*
[3–5 конкретних практичних порад]

🎯 *Ціль тижня*
[одна чітка ціль]

Відповідай українською. Будь конкретним і лаконічним."""

    result = await _openrouter(prompt, max_tokens=900)
    return result if result else "❌ Не вдалося отримати аналіз. Спробуйте пізніше."


# ═══════════════════════════════════════════════════════════════════════════════
# /start
# ═══════════════════════════════════════════════════════════════════════════════

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Вітаю! Я бот для *сімейного бюджету* з ШІ-аналізом 💰🤖\n\n"
        "📋 *Команди:*\n"
        "➕ /add — додати витрату _(ШІ визначить категорію)_\n"
        "💵 /income — додати дохід\n"
        "📊 /stats — статистика тижня\n"
        "📂 /categories — витрати по категоріях\n"
        "👤 /my — мої витрати\n"
        "📅 /history — всі витрати тижня\n"
        "⚙️ /setbudget — тижневий бюджет\n"
        "🔒 /setlimit — ліміт на категорію (напр. Продукти)\n"
        "🗑 /delete — видалити останню витрату\n"
        "🤖 /ai — ШІ-аналітика та фінансові поради\n"
        "❓ /help — довідка\n",
        parse_mode="Markdown",
    )


# ═══════════════════════════════════════════════════════════════════════════════
# /setbudget
# ═══════════════════════════════════════════════════════════════════════════════

async def setbudget_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    group = get_group(data, str(update.effective_chat.id))
    await update.message.reply_text(
        f"💰 Поточний тижневий бюджет: *{fmt(group['weekly_budget'])}*\n\n"
        "Введіть новий бюджет (₴):",
        parse_mode="Markdown",
    )
    return SET_BUDGET


async def setbudget_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = float(update.message.text.strip().replace(",", ".").replace(" ", ""))
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Введіть коректну суму, наприклад: 7000")
        return SET_BUDGET

    data = load_data()
    group = get_group(data, str(update.effective_chat.id))
    group["weekly_budget"] = amount
    save_data(data)
    await update.message.reply_text(
        f"✅ Тижневий бюджет встановлено: *{fmt(amount)}*", parse_mode="Markdown"
    )
    return ConversationHandler.END


# ═══════════════════════════════════════════════════════════════════════════════
# /setlimit — ліміт на категорію
# ═══════════════════════════════════════════════════════════════════════════════

async def setlimit_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data  = load_data()
    group = get_group(data, str(update.effective_chat.id))
    limits = group.get("category_limits", {})

    # будуємо клавіатуру 2 стовпці
    keyboard, row = [], []
    for cat in CATEGORIES:
        label = f"{cat} ✓" if cat in limits else cat
        row.append(InlineKeyboardButton(label, callback_data=f"lcat_{cat}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)

    await update.message.reply_text(
        "🔒 Виберіть категорію для встановлення тижневого ліміту:\n"
        "_(✓ — ліміт вже встановлено)_",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return SET_LIMIT_CAT


async def setlimit_cat_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    cat = query.data.removeprefix("lcat_")
    context.user_data["limit_category"] = cat

    data  = load_data()
    group = get_group(data, str(update.effective_chat.id))
    cur   = group["category_limits"].get(cat)
    cur_text = f" (зараз: *{fmt(cur)}*)" if cur else ""

    await query.edit_message_text(
        f"🔒 Категорія: *{cat}*{cur_text}\n\n"
        "Введіть ліміт витрат на тиждень (₴):\n"
        "_Введіть 0, щоб скасувати ліміт_",
        parse_mode="Markdown",
    )
    return SET_LIMIT_AMOUNT


async def setlimit_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = float(update.message.text.strip().replace(",", ".").replace(" ", ""))
        if amount < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Введіть коректну суму, наприклад: 2000")
        return SET_LIMIT_AMOUNT

    cat   = context.user_data.get("limit_category", "💰 Інше")
    data  = load_data()
    group = get_group(data, str(update.effective_chat.id))

    if amount == 0:
        group["category_limits"].pop(cat, None)
        await update.message.reply_text(f"✅ Ліміт для *{cat}* скасовано.", parse_mode="Markdown")
    else:
        group["category_limits"][cat] = amount
        await update.message.reply_text(
            f"✅ Ліміт для *{cat}* — *{fmt(amount)}* на тиждень 🔒",
            parse_mode="Markdown",
        )
    save_data(data)
    return ConversationHandler.END


# ═══════════════════════════════════════════════════════════════════════════════
# /add — додати витрату з ШІ-категоризацією
# ═══════════════════════════════════════════════════════════════════════════════

async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "💸 Введіть суму витрати (₴):\n_Наприклад: 250 або 1 200.50_",
        parse_mode="Markdown",
    )
    return ADD_AMOUNT


async def add_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = float(update.message.text.strip().replace(",", ".").replace(" ", ""))
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Введіть коректну суму, наприклад: 350")
        return ADD_AMOUNT

    context.user_data["pending_amount"] = amount
    await update.message.reply_text(
        "📝 Вкажіть опис витрати:\n"
        "_Наприклад: Продукти АТБ, Кава, Бензин, Ліки, Netflix..._",
        parse_mode="Markdown",
    )
    return ADD_DESC


async def add_desc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    desc   = update.message.text.strip()
    amount = context.user_data.get("pending_amount", 0)
    user   = update.effective_user
    chat_id = str(update.effective_chat.id)
    user_id = str(user.id)

    data  = load_data()
    group = get_group(data, chat_id)
    ensure_week_reset(group)

    # реєструємо учасника
    group["members"].setdefault(user_id, {})
    group["members"][user_id]["name"] = user.full_name
    group["members"][user_id].setdefault("username", user.username or "")

    # ШІ-категоризація
    wait = await update.message.reply_text("🤖 Визначаю категорію...")
    category = await ai_categorize(desc)
    await wait.delete()

    expense = {
        "id":       len(group["expenses"]) + 1,
        "user_id":  user_id,
        "amount":   amount,
        "desc":     desc,
        "category": category,
        "date":     datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    group["expenses"].append(expense)
    save_data(data)

    # лічимо залишок
    budget      = group["weekly_budget"]
    spent_total = total_spent(group)
    rem         = remaining(group)
    my_spent    = member_spent(group, user_id)

    rem_text = (
        f"⚠️ *Перевитрата на {fmt(abs(rem))}!*"
        if rem < 0
        else f"💚 Залишок: *{fmt(rem)}*"
    )

    # перевірка ліміту категорії
    limit_warn = ""
    cat_limit  = group["category_limits"].get(category)
    if cat_limit:
        cat_total = category_spent_week(group, category)
        if cat_total >= cat_limit:
            limit_warn = f"\n🚫 *Ліміт {category} перевищено!* ({fmt(cat_total)} / {fmt(cat_limit)})"
        elif cat_total >= cat_limit * 0.8:
            limit_warn = f"\n⚠️ *{category}:* {fmt(cat_total)} / {fmt(cat_limit)} (понад 80%)"

    await update.message.reply_text(
        f"✅ Записано: *{fmt(amount)}* — {desc}\n"
        f"📂 Категорія: {category}"
        f"{limit_warn}\n\n"
        f"{bar(spent_total, budget)}\n"
        f"Витрачено: {fmt(spent_total)} / {fmt(budget)}\n"
        f"{rem_text}\n\n"
        f"👤 Твої витрати: *{fmt(my_spent)}*",
        parse_mode="Markdown",
    )
    return ConversationHandler.END


# ═══════════════════════════════════════════════════════════════════════════════
# /income — додати дохід
# ═══════════════════════════════════════════════════════════════════════════════

async def income_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "💵 Введіть суму доходу (₴):\n_Наприклад: 15000 або 500_",
        parse_mode="Markdown",
    )
    return ADD_INCOME_AMOUNT


async def income_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = float(update.message.text.strip().replace(",", ".").replace(" ", ""))
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Введіть коректну суму, наприклад: 15000")
        return ADD_INCOME_AMOUNT

    context.user_data["pending_income"] = amount
    await update.message.reply_text(
        "📝 Вкажіть джерело доходу:\n"
        "_Наприклад: Зарплата, Фріланс, Підробіток, Аліменти, Допомога..._",
        parse_mode="Markdown",
    )
    return ADD_INCOME_DESC


async def income_desc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    desc    = update.message.text.strip()
    amount  = context.user_data.get("pending_income", 0)
    user    = update.effective_user
    chat_id = str(update.effective_chat.id)
    user_id = str(user.id)

    data  = load_data()
    group = get_group(data, chat_id)
    ensure_week_reset(group)

    group["members"].setdefault(user_id, {"name": user.full_name, "username": user.username or ""})

    income_record = {
        "id":      len(group["incomes"]) + 1,
        "user_id": user_id,
        "amount":  amount,
        "desc":    desc,
        "date":    datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    group["incomes"].append(income_record)
    save_data(data)

    inc_total = total_income(group)
    spent     = total_spent(group)
    balance   = inc_total - spent

    await update.message.reply_text(
        f"✅ Дохід записано: *{fmt(amount)}* — {desc}\n\n"
        f"💵 Всього доходів цього тижня: *{fmt(inc_total)}*\n"
        f"💸 Витрачено: *{fmt(spent)}*\n"
        f"{'💚' if balance >= 0 else '🔴'} Баланс: *{fmt(balance)}*",
        parse_mode="Markdown",
    )
    return ConversationHandler.END


# ═══════════════════════════════════════════════════════════════════════════════
# /stats
# ═══════════════════════════════════════════════════════════════════════════════

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data  = load_data()
    chat_id = str(update.effective_chat.id)
    group = get_group(data, chat_id)
    ensure_week_reset(group)

    budget      = group["weekly_budget"]
    spent_total = total_spent(group)
    inc_total   = total_income(group)
    rem         = remaining(group)
    expenses    = week_expenses(group)

    ws = group["week_start"]
    ws_fmt = datetime.strptime(ws, "%Y-%m-%d").strftime("%d.%m")
    we_fmt = (datetime.strptime(ws, "%Y-%m-%d") + timedelta(days=6)).strftime("%d.%m")

    rem_line = (
        f"❗ Перевитрата: *{fmt(abs(rem))}*"
        if rem < 0
        else f"💚 Залишок: *{fmt(rem)}*"
    )
    balance  = inc_total - spent_total

    text = (
        f"📊 *Статистика тижня* ({ws_fmt}–{we_fmt})\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 Бюджет: *{fmt(budget)}*\n"
        f"💵 Доходи: *{fmt(inc_total)}*\n"
        f"💸 Витрачено: *{fmt(spent_total)}*\n"
        f"{rem_line}\n"
        f"{'💚' if balance >= 0 else '🔴'} Баланс: *{fmt(balance)}*\n"
        f"{bar(spent_total, budget)}\n\n"
    )

    if group["members"]:
        member_totals: dict[str, float] = defaultdict(float)
        for e in expenses:
            member_totals[e["user_id"]] += e["amount"]

        active = {uid: v for uid, v in member_totals.items() if v > 0}
        if active:
            text += "👥 *По членах родини:*\n"
            for uid, ms in sorted(active.items(), key=lambda x: -x[1]):
                member = group["members"].get(uid, {})
                name   = member.get("name", "Невідомий")
                pct    = ms / budget * 100 if budget > 0 else 0
                text  += f"  • {name}: *{fmt(ms)}* ({pct:.0f}%)\n"

    if not expenses:
        text += "\n_Цього тижня витрат ще немає._"
    else:
        text += "\n📂 /categories — по категоріях  |  🤖 /ai — ШІ-поради"

    await update.message.reply_text(text, parse_mode="Markdown")


# ═══════════════════════════════════════════════════════════════════════════════
# /categories — розбивка по категоріях
# ═══════════════════════════════════════════════════════════════════════════════

async def categories_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data  = load_data()
    group = get_group(data, str(update.effective_chat.id))
    ensure_week_reset(group)

    expenses = week_expenses(group)
    if not expenses:
        await update.message.reply_text("📂 Цього тижня витрат ще немає.")
        return

    cat_totals: dict[str, float] = defaultdict(float)
    for e in expenses:
        cat_totals[e.get("category", "💰 Інше")] += e["amount"]

    total  = sum(cat_totals.values())
    limits = group.get("category_limits", {})

    text = "📂 *Витрати по категоріях*\n━━━━━━━━━━━━━━━━━━━━\n"
    for cat, amt in sorted(cat_totals.items(), key=lambda x: -x[1]):
        pct   = amt / total * 100 if total > 0 else 0
        limit = limits.get(cat)
        if limit:
            usage = amt / limit * 100
            if amt >= limit:
                linfo = f"  🚫 *ПЕРЕВИЩЕНО* ({fmt(amt)}/{fmt(limit)})"
            elif usage >= 80:
                linfo = f"  ⚠️ {usage:.0f}% від ліміту {fmt(limit)}"
            else:
                linfo = f"  ✅ {usage:.0f}% від ліміту {fmt(limit)}"
        else:
            linfo = ""
        text += f"{cat}: *{fmt(amt)}* ({pct:.0f}%){linfo}\n"

    if limits:
        text += "\n_🔒 Встановлені ліміти виділені_"
    else:
        text += "\n_Встановіть ліміти командою /setlimit_"

    await update.message.reply_text(text, parse_mode="Markdown")


# ═══════════════════════════════════════════════════════════════════════════════
# /my
# ═══════════════════════════════════════════════════════════════════════════════

async def my_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    data    = load_data()
    group   = get_group(data, str(update.effective_chat.id))
    ensure_week_reset(group)

    my_exp   = [e for e in week_expenses(group) if e["user_id"] == user_id]
    my_total = sum(e["amount"] for e in my_exp)
    budget   = group["weekly_budget"]
    pct      = my_total / budget * 100 if budget > 0 else 0

    text = (
        f"👤 *Твої витрати цього тижня*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Всього: *{fmt(my_total)}* ({pct:.0f}% бюджету)\n\n"
    )

    if my_exp:
        text += "📋 *Деталі:*\n"
        for e in sorted(my_exp, key=lambda x: x["date"], reverse=True):
            date_s = e["date"][5:16]
            cat    = e.get("category", "")
            text  += f"  `{date_s}` {cat} — {fmt(e['amount'])} — {e['desc']}\n"
    else:
        text += "_Цього тижня витрат ще немає._"

    await update.message.reply_text(text, parse_mode="Markdown")


# ═══════════════════════════════════════════════════════════════════════════════
# /delete
# ═══════════════════════════════════════════════════════════════════════════════

async def delete_last(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    data    = load_data()
    group   = get_group(data, str(update.effective_chat.id))

    my_exp = [e for e in group["expenses"] if e["user_id"] == user_id]
    if not my_exp:
        await update.message.reply_text("❌ У тебе немає витрат для видалення.")
        return

    last = my_exp[-1]
    kb   = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Так, видалити", callback_data=f"del_{last['id']}"),
        InlineKeyboardButton("❌ Скасувати",     callback_data="del_cancel"),
    ]])
    await update.message.reply_text(
        f"🗑 Видалити останню витрату?\n\n"
        f"*{fmt(last['amount'])}* — {last['desc']}\n"
        f"📂 {last.get('category', '')}  _{last['date']}_",
        parse_mode="Markdown",
        reply_markup=kb,
    )


async def delete_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "del_cancel":
        await query.edit_message_text("❌ Видалення скасовано.")
        return

    expense_id = int(query.data.removeprefix("del_"))
    data       = load_data()
    group      = get_group(data, str(update.effective_chat.id))
    before     = len(group["expenses"])
    group["expenses"] = [e for e in group["expenses"] if e["id"] != expense_id]

    if len(group["expenses"]) == before:
        await query.edit_message_text("❌ Витрату не знайдено.")
        return

    save_data(data)
    await query.edit_message_text(
        f"✅ Витрату видалено.\n\n💚 Новий залишок: *{fmt(remaining(group))}*",
        parse_mode="Markdown",
    )


# ═══════════════════════════════════════════════════════════════════════════════
# /history
# ═══════════════════════════════════════════════════════════════════════════════

async def history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data  = load_data()
    group = get_group(data, str(update.effective_chat.id))
    ensure_week_reset(group)

    expenses = week_expenses(group)
    if not expenses:
        await update.message.reply_text("📋 Цього тижня витрат ще немає.")
        return

    text = "📋 *Всі витрати цього тижня:*\n━━━━━━━━━━━━━━━━━━━━\n"
    for e in sorted(expenses, key=lambda x: x["date"], reverse=True):
        member = group["members"].get(e["user_id"], {})
        fname  = (member.get("name", "?") or "?").split()[0]
        cat    = e.get("category", "")
        text  += f"`{e['date'][5:16]}` *{fname}*: {fmt(e['amount'])} — {e['desc']} {cat}\n"

    await update.message.reply_text(text, parse_mode="Markdown")


# ═══════════════════════════════════════════════════════════════════════════════
# /ai — ШІ-аналітика
# ═══════════════════════════════════════════════════════════════════════════════

async def ai_analysis(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data  = load_data()
    group = get_group(data, str(update.effective_chat.id))
    ensure_week_reset(group)

    msg = await update.message.reply_text("🤖 Аналізую бюджет... Зачекайте ⏳")
    report = await ai_weekly_report(group)

    await msg.edit_text(
        f"🤖 *ШІ-Аналіз тижневого бюджету*\n━━━━━━━━━━━━━━━━━━━━\n\n{report}",
        parse_mode="Markdown",
    )


# ═══════════════════════════════════════════════════════════════════════════════
# /help
# ═══════════════════════════════════════════════════════════════════════════════

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "❓ *Довідка — Сімейний Бюджет Бот*\n\n"
        "🤖 Відстежує бюджет родини з ШІ-категоризацією та аналізом.\n\n"
        "📋 *Команди:*\n"
        "• /add — записати витрату _(ШІ автоматично визначить категорію)_\n"
        "• /income — записати дохід\n"
        "• /stats — загальна статистика тижня\n"
        "• /categories — витрати по категоріях з лімітами\n"
        "• /my — твої особисті витрати\n"
        "• /history — всі витрати тижня\n"
        "• /setbudget — встановити тижневий бюджет\n"
        "• /setlimit — ліміт витрат на категорію\n"
        "• /delete — видалити останню свою витрату\n"
        "• /ai — ШІ-аналітика, розподіл категорій, фінансові поради\n\n"
        "⚙️ *Налаштування .env:*\n"
        "`TELEGRAM_BOT_TOKEN` — токен бота\n"
        "`OPENROUTER_API_KEY` — ключ ШІ (openrouter.ai)\n\n"
        "💡 *Підказки:*\n"
        "• Встановіть ліміт на Продукти через /setlimit\n"
        "• Бот попередить, якщо 80%+ ліміту витрачено\n"
        "• Тиждень скидається автоматично щопонеділка\n",
        parse_mode="Markdown",
    )


# ═══════════════════════════════════════════════════════════════════════════════
# cancel
# ═══════════════════════════════════════════════════════════════════════════════

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Скасовано.")
    return ConversationHandler.END


# ═══════════════════════════════════════════════════════════════════════════════
# main
# ═══════════════════════════════════════════════════════════════════════════════

async def main():
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN не встановлено!")
    if not OPENROUTER_API_KEY:
        logger.warning("OPENROUTER_API_KEY не встановлено — ШІ-функції вимкнено!")

    app = Application.builder().token(token).build()

    # ── Conversations ──────────────────────────────────────────────────────────
    budget_conv = ConversationHandler(
        entry_points=[CommandHandler("setbudget", setbudget_start)],
        states={
            SET_BUDGET: [MessageHandler(filters.TEXT & ~filters.COMMAND, setbudget_receive)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    limit_conv = ConversationHandler(
        entry_points=[CommandHandler("setlimit", setlimit_start)],
        states={
            SET_LIMIT_CAT:    [CallbackQueryHandler(setlimit_cat_cb, pattern=r"^lcat_")],
            SET_LIMIT_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, setlimit_amount)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    add_conv = ConversationHandler(
        entry_points=[CommandHandler("add", add_start)],
        states={
            ADD_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_amount)],
            ADD_DESC:   [MessageHandler(filters.TEXT & ~filters.COMMAND, add_desc)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    income_conv = ConversationHandler(
        entry_points=[CommandHandler("income", income_start)],
        states={
            ADD_INCOME_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, income_amount)],
            ADD_INCOME_DESC:   [MessageHandler(filters.TEXT & ~filters.COMMAND, income_desc)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    # ── Handlers ───────────────────────────────────────────────────────────────
    app.add_handler(CommandHandler("start",      start))
    app.add_handler(CommandHandler("help",       help_cmd))
    app.add_handler(CommandHandler("stats",      stats))
    app.add_handler(CommandHandler("categories", categories_stats))
    app.add_handler(CommandHandler("my",         my_stats))
    app.add_handler(CommandHandler("history",    history))
    app.add_handler(CommandHandler("delete",     delete_last))
    app.add_handler(CommandHandler("ai",         ai_analysis))
    app.add_handler(CallbackQueryHandler(delete_callback, pattern=r"^del_"))
    app.add_handler(budget_conv)
    app.add_handler(limit_conv)
    app.add_handler(add_conv)
    app.add_handler(income_conv)

    logger.info("🤖 Бот запущено!")
    async with app:
        await app.start()
        await app.updater.start_polling()
        logger.info("✅ Polling активний. Ctrl+C для зупинки.")
        await asyncio.Event().wait()
        await app.updater.stop()
        await app.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Бот зупинено.")