import pandas as pd
from pathlib import Path

TTK_FILE = "TTK.xlsx"
TTK_SHEET = "TTK"
PRICES_FILE = "Prices.xlsx"
PRICES_SHEET = "Prices"
DEFAULT_PRICE = 933


def norm(s):
    return str(s or "").strip()


def load_dishes():
    try:
        df = pd.read_excel(TTK_FILE, sheet_name=TTK_SHEET)
    except Exception:
        df = pd.read_excel(TTK_FILE)
    df.columns = df.columns.str.strip()
    dish_col = next((c for c in df.columns if "блюдо" in str(c).lower()),
                    df.columns[0])
    return (df[dish_col].dropna().astype(str).map(norm).replace(
        "", pd.NA).dropna().drop_duplicates().sort_values().tolist())


def main():
    dishes = load_dishes()
    # если Prices.xlsx уже есть — читаем, иначе пустую
    if Path(PRICES_FILE).exists():
        try:
            dfp = pd.read_excel(PRICES_FILE, sheet_name=PRICES_SHEET)
        except Exception:
            dfp = pd.read_excel(PRICES_FILE)
        dfp.columns = dfp.columns.str.strip()
    else:
        dfp = pd.DataFrame(columns=["Блюдо", "Цена продажи"])

    # гарантируем колонки
    for col in ["Блюдо", "Цена продажи"]:
        if col not in dfp.columns:
            dfp[col] = pd.NA

    # объединяем с блюдами из ТТК
    have = set(dfp["Блюдо"].dropna().map(norm))
    to_add = [d for d in dishes if d not in have]
    if to_add:
        dfp = pd.concat([dfp, pd.DataFrame({"Блюдо": to_add})],
                        ignore_index=True)

    # всем поставить 933
    dfp["Блюдо"] = dfp["Блюдо"].astype(str).map(norm)
    dfp.loc[dfp["Блюдо"].notna(), "Цена продажи"] = DEFAULT_PRICE

    # сохранить
    dfp = (dfp.dropna(subset=["Блюдо"]).drop_duplicates(
        subset=["Блюдо"]).sort_values("Блюдо")[["Блюдо", "Цена продажи"
                                                ]].reset_index(drop=True))
    dfp.to_excel(PRICES_FILE, sheet_name=PRICES_SHEET, index=False)
    print(f"✅ {PRICES_FILE}: всем блюдам установлена цена {DEFAULT_PRICE} ₽.")


if __name__ == "__main__":
    main()
