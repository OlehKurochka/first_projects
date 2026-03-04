"""
╔══════════════════════════════════════════════════════════════╗
║        РОЗУМНИЙ СОРТУВАЛЬНИК ОСОБИСТИХ ФІНАНСІВ              ║
║        Використовує Groq API для класифікації витрат         ║
╚══════════════════════════════════════════════════════════════╝
"""

import json
import groq
import pandas as pd
import matplotlib.pyplot as plt
from datetime import datetime
from pathlib import Path

# ─────────────────────────────────────────────
# НАЛАШТУВАННЯ
# ─────────────────────────────────────────────

GROQ_API_KEY = "API ключ"
INPUT_FILE = r"файл виписки.csv"
OUTPUT_CSV = "result_categories.csv"
OUTPUT_CHART = "spending_chart.png"

# ─────────────────────────────────────────────
# КРОК 1: ЗАВАНТАЖЕННЯ ФАЙЛУ
# ─────────────────────────────────────────────

def load_file(filepath: str) -> pd.DataFrame:
    """
    Завантажує банківську виписку з файлу.
    Повертає DataFrame з колонками: date, amount, description
    """
    path = Path(filepath)

    if not path.exists():
        raise FileNotFoundError(f"Файл не знайдено: {filepath}")

    print(f"Завантажую файл: {filepath}")

    if path.suffix.lower() == ".csv":
        return load_csv(filepath)
    else:
        raise ValueError("Підтримуються лише .csv файли")


def load_csv(filepath: str) -> pd.DataFrame:
    """
    Читає CSV виписку.
    """
    for encoding in ["utf-8", "utf-8-sig", "cp1251", "latin-1"]:
        try:
            df = pd.read_csv(filepath, encoding=encoding)
            print(f"CSV прочитано, кодування: {encoding}")
            print(f"Знайдено колонок: {list(df.columns)}")
            break
        except UnicodeDecodeError:
            continue

    df = df.rename(columns={
        "Дата i час операції":          "date",
        "Деталі операції":              "description",
        "Сума в валюті картки (UAH)":   "amount",
    })

    df = df[["date", "amount", "description"]].copy()

    df["amount"] = (
        df["amount"]
        .astype(str)
        .str.replace(" ", "")
        .str.replace(",", ".")
        .astype(float)
    )

    df = df[df["amount"] < 0].copy()
    df["amount"] = df["amount"].abs()

    print(f"Знайдено витрат: {len(df)} транзакцій")
    return df.reset_index(drop=True)

# ─────────────────────────────────────────────
# КРОК 2: КЛАСИФІКАЦІЯ ЧЕРЕЗ LLM
# ─────────────────────────────────────────────

CATEGORIES = ["Їжа", "Розваги", "Транспорт", "Комунальні", "Здоров'я", "Одяг", "Інше"]

SYSTEM_PROMPT = f"""Ти — асистент для класифікації банківських транзакцій.

Твоя задача: визначити категорію витрати на основі опису транзакції.

Доступні категорії:
{chr(10).join(f"- {cat}" for cat in CATEGORIES)}

Правила:
- "Їжа" — ресторани, кафе, супермаркети, доставка їжі (Glovo, Bolt Food)
- "Розваги" — кіно, Netflix, Steam, ігри, концерти, спорт  
- "Транспорт" — Uber, Bolt, метро, АЗС, паркування, авіаквитки
- "Комунальні" — електрика, газ, вода, інтернет, мобільний зв'язок
- "Здоров'я" — аптека, лікарі, клініки, спортзал
- "Одяг" — магазини одягу, взуття, аксесуари
- "Інше" — якщо не підходить жодна категорія

Відповідай ТІЛЬКИ JSON масивом, без пояснень.
Кожен елемент: {{"id": <номер>, "category": "<назва категорії>"}}
"""


def classify_transactions(df: pd.DataFrame, batch_size: int = 20) -> pd.DataFrame:
    """
    Класифікує транзакції через Groq API.
    Відправляємо батчами (по 20 штук) — дешевше і швидше.
    """
    client = groq.Groq(api_key=GROQ_API_KEY)

    categories = []
    total = len(df)

    print(f"\n Класифікую {total} транзакцій через Groq...")

    for batch_start in range(0, total, batch_size):
        batch_end = min(batch_start + batch_size, total)
        batch = df.iloc[batch_start:batch_end]

        print(f"Батч {batch_start // batch_size + 1}: транзакції {batch_start + 1}–{batch_end}")

        transactions_text = "\n".join([
            f'{i}. Сума: {row["amount"]:.2f} грн | Опис: {row["description"]}'
            for i, (_, row) in enumerate(batch.iterrows())
        ])

        user_message = f"Класифікуй ці транзакції:\n\n{transactions_text}"

        # Запит до Groq
        try:
            response = client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                max_tokens=1024,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_message}
                ]
            )

            raw_text = response.choices[0].message.content.strip()

            raw_text = raw_text.replace("```json", "").replace("```", "").strip()

            batch_results = json.loads(raw_text)

            for item in batch_results:
                categories.append(item.get("category", "Інше"))

        except json.JSONDecodeError as e:
            print(f"Помилка парсингу відповіді: {e}")
            categories.extend(["Інше"] * (batch_end - batch_start))

        except groq.APIError as e:
            print(f"    Помилка API: {e}")
            categories.extend(["Інше"] * (batch_end - batch_start))

    df = df.copy()
    df["category"] = categories

    print(f"Класифікацію завершено!")
    return df


# ─────────────────────────────────────────────
# КРОК 3: АГРЕГАЦІЯ ТА АНАЛІЗ
# ─────────────────────────────────────────────

def analyze(df: pd.DataFrame) -> pd.DataFrame:
    """
    Підраховує суму та кількість транзакцій по кожній категорії.
    """
    summary = (
        df.groupby("category")
        .agg(
            total_spent=("amount", "sum"),
            num_transactions=("amount", "count"),
            avg_transaction=("amount", "mean")
        )
        .sort_values("total_spent", ascending=False)
        .reset_index()
    )

    total = summary["total_spent"].sum()
    summary["percent"] = (summary["total_spent"] / total * 100).round(1)

    return summary


def print_report(df: pd.DataFrame, summary: pd.DataFrame):
    """
    Виводить звіт у консоль.
    """
    print("\n" + "═" * 55)
    print("ЗВІТ ПО ВИТРАТАХ")
    print("═" * 55)

    for _, row in summary.iterrows():
        bar = "█" * int(row["percent"] / 2)
        print(f"\n  {row['category']:<15} {bar}")
        print(f"  {'':15} {row['total_spent']:.2f} грн  ({row['percent']}%)")
        print(f"  {'':15} {row['num_transactions']} транзакцій  |  ср. {row['avg_transaction']:.2f} грн")

    print("\n" + "─" * 55)
    print(f"  ЗАГАЛОМ ВИТРАЧЕНО: {summary['total_spent'].sum():.2f} грн")
    print("═" * 55)


# ─────────────────────────────────────────────
# КРОК 4: ВІЗУАЛІЗАЦІЯ
# ─────────────────────────────────────────────

COLORS = ["#FF6B6B", "#4ECDC4", "#45B7D1", "#96CEB4", "#FFEAA7", "#DDA0DD", "#98D8C8"]


def build_chart(summary: pd.DataFrame, output_path: str):
    """
    Будує кругову діаграму витрат по категоріях і зберігає у PNG.
    """
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 7))
    fig.patch.set_facecolor("#1a1a2e")

    colors = COLORS[:len(summary)]

    wedges, texts, autotexts = ax1.pie(
        summary["total_spent"],
        labels=summary["category"],
        autopct="%1.1f%%",
        colors=colors,
        startangle=90,
        pctdistance=0.8,
        wedgeprops={"edgecolor": "#1a1a2e", "linewidth": 2}
    )

    for text in texts:
        text.set_color("white")
        text.set_fontsize(11)
    for autotext in autotexts:
        autotext.set_color("white")
        autotext.set_fontweight("bold")

    ax1.set_facecolor("#1a1a2e")
    ax1.set_title("Структура витрат", color="white", fontsize=14, pad=15)

    ax2.set_facecolor("#1a1a2e")
    bars = ax2.barh(
        summary["category"],
        summary["total_spent"],
        color=colors,
        edgecolor="#1a1a2e",
        height=0.6
    )

    for bar, (_, row) in zip(bars, summary.iterrows()):
        ax2.text(
            bar.get_width() + summary["total_spent"].max() * 0.01,
            bar.get_y() + bar.get_height() / 2,
            f'{row["total_spent"]:.0f} грн',
            va="center", color="white", fontsize=10
        )

    ax2.set_xlabel("Сума (грн)", color="white")
    ax2.set_title("Витрати по категоріях", color="white", fontsize=14, pad=15)
    ax2.tick_params(colors="white")
    ax2.spines["top"].set_visible(False)
    ax2.spines["right"].set_visible(False)
    for spine in ["left", "bottom"]:
        ax2.spines[spine].set_color("#444")
    ax2.set_xlim(0, summary["total_spent"].max() * 1.2)

    plt.suptitle(
        f"Аналіз витрат  •  {datetime.now().strftime('%B %Y')}",
        color="white", fontsize=16, fontweight="bold", y=1.02
    )
    plt.tight_layout()
    plt.savefig(output_path, dpi=150, bbox_inches="tight",
                facecolor="#1a1a2e", edgecolor="none")

    print(f"\n Графік збережено: {output_path}")


# ─────────────────────────────────────────────
# КРОК 5: ЗБЕРЕЖЕННЯ РЕЗУЛЬТАТІВ
# ─────────────────────────────────────────────

def save_results(df: pd.DataFrame, summary: pd.DataFrame, output_path: str):
    """
    Зберігає деталізований результат у CSV.
    """
    df.to_csv(output_path, index=False, encoding="utf-8-sig")
    print(f"Результат збережено: {output_path}")

    summary_path = output_path.replace(".csv", "_summary.csv")
    summary.to_csv(summary_path, index=False, encoding="utf-8-sig")
    print(f"Зведений звіт: {summary_path}")


# ─────────────────────────────────────────────
# ГОЛОВНА ФУНКЦІЯ — ТУТ ВСЕ З'ЄДНУЄТЬСЯ
# ─────────────────────────────────────────────

def main():
    print("=" * 55)
    print("РОЗУМНИЙ СОРТУВАЛЬНИК ФІНАНСІВ")
    print("=" * 55)

    # 1. Завантажуємо файл
    df = load_file(INPUT_FILE)

    # 2. Класифікуємо через LLM
    df = classify_transactions(df, batch_size=20)

    # 3. Аналізуємо
    summary = analyze(df)

    # 4. Виводимо звіт
    print_report(df, summary)

    # 5. Будуємо графік
    build_chart(summary, OUTPUT_CHART)

    # 6. Зберігаємо
    save_results(df, summary, OUTPUT_CSV)

    print("\n Готово! Перевір файли:", OUTPUT_CSV, "та", OUTPUT_CHART)


# ─────────────────────────────────────────────
# Запусти
# ─────────────────────────────────────────────

if __name__ == "__main__":

    main()
