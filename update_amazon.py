import os
import io
import json
import time
import datetime as dt

import requests
import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Font, Alignment, PatternFill, Border, Side
from openpyxl.utils import get_column_letter

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------
SHEET_ID = "19h1OUyvNQbIVxNIkPEMUsHM8pLNPuw0jMHKkUQiB1l8"
DAILY_IMP_GID = "210013716"
ASIN = "B00CO00Y32"

# Aannames (bewerkbaar in de Excel; dit zijn de startwaarden)
VAT_RATE = 0.20              # net = gross / (1 + BTW)
COMMISSION_ADFEE_PCT = 0.153 # 15,3% van gross RSP
FBA = 2.42                   # GBP per stuk
COGS = 3.96                  # GBP per stuk

OUTPUT = os.path.join("output", f"Amazon_{ASIN}.xlsx")
CSV_URL = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid={DAILY_IMP_GID}"

NEEDED = ["date", "childAsin", "unitsOrdered", "orderedProductSales", "unitSessionPercentage"]

FONT = "Arial"
BLUE = "0000FF"
GREY = "808080"

# ----------------------------------------------------------------------------
# Data inlezen
# ----------------------------------------------------------------------------
def load_daily_imp() -> pd.DataFrame:
    local = os.environ.get("AMAZON_LOCAL_CSV")
    if local:
        df = pd.read_csv(local)
    else:
        # Cache-buster + no-cache headers: dwingt Google een verse export te geven
        # in plaats van een gecachte versie die de nieuwste rij nog mist.
        url = f"{CSV_URL}&_cb={int(time.time())}"
        r = requests.get(url, timeout=60,
                         headers={"Cache-Control": "no-cache", "Pragma": "no-cache"})
        r.raise_for_status()
        df = pd.read_csv(io.StringIO(r.text))

    missing = [c for c in NEEDED if c not in df.columns]
    if missing:
        raise SystemExit(f"Ontbrekende kolommen in Daily IMP: {missing}")

    df = df[NEEDED].copy()
    df["date"] = pd.to_datetime(df["date"]).dt.date
    for c in ["unitsOrdered", "orderedProductSales", "unitSessionPercentage"]:
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)
    df = df[df["childAsin"] == ASIN]
    # Daily IMP bevat per datum dubbele rijen (een blok zonder en een met traffic).
    # units/sales zijn identiek in beide; dedup naar 1 rij per datum via max.
    # Voor CVR pakt max automatisch de rij met traffic (rij zonder traffic = 0).
    df = (df.groupby(["date", "childAsin"], as_index=False)
            .agg({"unitsOrdered": "max",
                  "orderedProductSales": "max",
                  "unitSessionPercentage": "max"}))
    df = df.sort_values("date").reset_index(drop=True)
    return df

# ----------------------------------------------------------------------------
# Excel bouwen
# ----------------------------------------------------------------------------
def build(df: pd.DataFrame):
    wb = Workbook()

    # --- Data-tab (ruwe bron, SUMIFS leest hieruit) ---
    data = wb.active
    data.title = "Data"
    data.append(NEEDED)
    for _, row in df.iterrows():
        data.append([row["date"], row["childAsin"],
                     float(row["unitsOrdered"]), float(row["orderedProductSales"]),
                     float(row["unitSessionPercentage"])])
    for r in range(2, data.max_row + 1):
        data.cell(r, 1).number_format = "yyyy-mm-dd"
    for c in range(1, 6):
        data.cell(1, c).font = Font(name=FONT, bold=True)
    data.sheet_state = "hidden"

    # --- Rapport-tab ---
    rep = wb.create_sheet("Rapport")
    dates = sorted(df["date"].unique())

    labels = [
        (3, "Sales #", "Column D"),
        (4, "Sales \u00a3", "Column F"),
        (5, "Avg. RSP (gross)", ""),
        (6, "Avg. RSP (net)", ""),
        (7, "Commission + AdFee", ""),
        (8, "FBA", ""),
        (9, "COGS", ""),
        (10, "Margin (%)", ""),
        (11, "Margin (abs)", ""),
        (12, "Margin Tot. (abs)", ""),
        (13, "Margin Net. (abs)", ""),
        (14, "CVR", "Column AF"),
    ]

    rep["C1"] = "Datum (Column A)"
    rep["A2"] = "ASin"
    rep["B2"] = ASIN
    rep["A3"] = "C40"
    for r, name, src in labels:
        rep.cell(r, 2, name)
        if src:
            rep.cell(r, 3, src)

    # Aannames (bewerkbaar)
    rep["B16"] = "Aannames"
    assum = [
        (17, "BTW", VAT_RATE, "0.0%"),
        (18, "Commission + AdFee %", COMMISSION_ADFEE_PCT, "0.0%"),
        (19, "FBA (\u00a3/stuk)", FBA, "\u00a3#,##0.00"),
        (20, "COGS (\u00a3/stuk)", COGS, "\u00a3#,##0.00"),
    ]
    for r, name, val, fmt in assum:
        rep.cell(r, 2, name)
        c = rep.cell(r, 3, val)
        c.number_format = fmt
        c.font = Font(name=FONT, color=BLUE)  # blauw = bewerkbare input

    # Datakolommen per dag
    first_col = 4  # kolom D
    for i, d in enumerate(dates):
        ci = first_col + i
        L = get_column_letter(ci)
        dcell = rep.cell(1, ci, d)
        dcell.number_format = "yyyy-mm-dd"
        dcell.font = Font(name=FONT, bold=True)
        dcell.alignment = Alignment(horizontal="center")

        f = {
            3:  f"=SUMIFS(Data!$C:$C,Data!$A:$A,{L}$1,Data!$B:$B,$B$2)",
            4:  f"=SUMIFS(Data!$D:$D,Data!$A:$A,{L}$1,Data!$B:$B,$B$2)",
            5:  f"=IF({L}3=0,0,{L}4/{L}3)",
            6:  f"={L}5/(1+$C$17)",
            7:  f"=$C$18*{L}5",
            8:  "=$C$19",
            9:  "=$C$20",
            10: f"=IF({L}6=0,0,{L}11/{L}6)",
            11: f"={L}6-{L}7-{L}8-{L}9",
            12: f"={L}11*{L}3",
            13: None,  # Margin Net. (abs) bewust leeg
            14: f"=SUMIFS(Data!$E:$E,Data!$A:$A,{L}$1,Data!$B:$B,$B$2)",
        }
        for r, formula in f.items():
            if formula is None:
                continue
            rep.cell(r, ci, formula)

    # Opmaak per rij
    money = "\u00a3#,##0.00"
    fmt_by_row = {3: "#,##0", 4: money, 5: money, 6: money, 7: money,
                  8: money, 9: money, 10: "0.0%", 11: money, 12: money,
                  13: money, 14: '0.0"%"'}
    last_col = first_col + len(dates) - 1
    for r, fmt in fmt_by_row.items():
        for ci in range(first_col, last_col + 1):
            rep.cell(r, ci).number_format = fmt
            rep.cell(r, ci).font = Font(name=FONT)

    # Labels/headers opmaak
    rep["A2"].font = Font(name=FONT, bold=True)
    rep["B2"].font = Font(name=FONT, bold=True)
    rep["A3"].font = Font(name=FONT, color=GREY)
    rep["C1"].font = Font(name=FONT, italic=True, color=GREY)
    rep["B16"].font = Font(name=FONT, bold=True)
    for r, *_ in labels:
        rep.cell(r, 2).font = Font(name=FONT, bold=(r in (3, 4)))
        rep.cell(r, 3).font = Font(name=FONT, italic=True, color=GREY)

    # Kolombreedtes
    rep.column_dimensions["A"].width = 6
    rep.column_dimensions["B"].width = 20
    rep.column_dimensions["C"].width = 14
    for ci in range(first_col, last_col + 1):
        rep.column_dimensions[get_column_letter(ci)].width = 12

    rep.freeze_panes = "D2"

    # Forceer Excel om bij openen alle formules te herberekenen (anders blijven de
    # cellen leeg omdat openpyxl geen voorberekende waarden meeschrijft).
    wb.calculation.fullCalcOnLoad = True

    os.makedirs(os.path.dirname(OUTPUT), exist_ok=True)
    wb.save(OUTPUT)
    return len(dates)

# ----------------------------------------------------------------------------
# Cijfers berekenen (identiek aan de Excel-formules) voor data.json / dashboard
# ----------------------------------------------------------------------------
def compute_rows(df: pd.DataFrame):
    rows = []
    for _, r in df.iterrows():
        units = float(r["unitsOrdered"])
        sales = float(r["orderedProductSales"])
        gross = sales / units if units else 0.0
        net = gross / (1 + VAT_RATE)
        commission = COMMISSION_ADFEE_PCT * gross
        margin_abs = net - commission - FBA - COGS
        margin_pct = (margin_abs / net) if net else 0.0
        rows.append({
            "date": str(r["date"]),
            "units": units,
            "sales": sales,
            "grossRsp": gross,
            "netRsp": net,
            "commission": commission,
            "fba": FBA,
            "cogs": COGS,
            "marginPct": margin_pct,
            "marginAbs": margin_abs,
            "marginTot": margin_abs * units,
            "cvr": float(r["unitSessionPercentage"]),
        })
    return rows


# ----------------------------------------------------------------------------
def main():
    df = load_daily_imp()
    if df.empty:
        raise SystemExit(f"Geen rijen gevonden voor ASIN {ASIN}.")
    n = build(df)
    generated = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    meta = {
        "asin": ASIN,
        "file": f"Amazon_{ASIN}.xlsx",
        "generated_utc": generated,
        "days": n,
        "rows": int(len(df)),
        "last_date": str(max(df["date"])),
    }
    with open(os.path.join("output", "meta.json"), "w", encoding="utf-8") as fh:
        json.dump(meta, fh, ensure_ascii=False, indent=2)

    data = {
        "asin": ASIN,
        "generated_utc": generated,
        "assumptions": {
            "vat": VAT_RATE,
            "commissionPct": COMMISSION_ADFEE_PCT,
            "fba": FBA,
            "cogs": COGS,
        },
        "rows": compute_rows(df),
    }
    with open(os.path.join("output", "data.json"), "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)

    print(f"OK: {OUTPUT} ({n} dagkolommen, {len(df)} bronrijen voor {ASIN})")

if __name__ == "__main__":
    main()
