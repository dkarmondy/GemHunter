"""Import personal comps into the GemHunter db (and/or a portable CSV).

Sources:
  --xlsx  E:\\WATCHES\\Watch History.xlsx  -> my-purchase / my-sale rows
  --pdf   an eBay "Bid History" PDF        -> bid-history row (final price = top bid)
  --from-csv data/comps.csv                -> import a CSV produced earlier (for the Pi)

Usage (Windows, build + local db + csv):
  python scripts/import_comps.py --xlsx "E:\\WATCHES\\Watch History.xlsx" \
      --pdf "E:\\WATCHES\\COLLECTION\\IWCFliegerChronograph3706\\eBay Item Bid History.pdf" \
      --db gemhunter.db --to-csv data/comps.csv

Usage (Pi, after copying comps.csv over):
  .venv/bin/python scripts/import_comps.py --from-csv data/comps.csv \
      --db /mnt/ssd/gemhunter/gemhunter.db
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gemhunter.storage import Storage  # noqa: E402

COLS = ["source", "brand", "model", "reference", "caliber", "title", "condition",
        "sale_date", "price", "currency", "bid_count", "seller", "url", "notes"]


def _iso(v) -> str:
    if v is None:
        return ""
    if isinstance(v, datetime):
        return v.date().isoformat()
    return str(v).strip()


def _num(v) -> float | None:
    if v is None:
        return None
    try:
        return float(str(v).replace("$", "").replace(",", "").strip())
    except ValueError:
        return None


def from_xlsx(path: str) -> list[dict]:
    import openpyxl
    ws = openpyxl.load_workbook(path, data_only=True)["Watch History"]
    rows, comps = list(ws.iter_rows(values_only=True)), []
    for row in rows[2:]:                      # row 0 = title, row 1 = headers
        row = list(row) + [None] * (41 - len(row))
        brand, name, ref = row[0], row[1], row[2]
        cond, year, cal = row[6], row[7], row[13]
        event, cost, offer = row[31], _num(row[33]), _num(row[34])
        seller, bought = row[35], _iso(row[29])
        sold_date, sell_price, sold_to = _iso(row[38]), _num(row[39]), row[40]
        if not brand or not str(brand).strip():
            continue
        title = " ".join(str(x).strip() for x in (brand, name, ref) if x)
        price = offer or cost                 # offer = hammer price; cost includes fees
        if price:
            comps.append(dict(
                source="my-purchase", brand=str(brand).strip(),
                model=str(name or "").strip(), reference=str(ref or "").strip(),
                caliber=str(cal or "").strip(), title=title,
                condition=" / ".join(str(x) for x in (cond, event) if x),
                sale_date=bought, price=price, currency="USD", bid_count=None,
                seller=str(seller or "").strip(), url="",
                notes=f"total cost {cost}" if cost and offer else ""))
        if sell_price:
            comps.append(dict(
                source="my-sale", brand=str(brand).strip(),
                model=str(name or "").strip(), reference=str(ref or "").strip(),
                caliber=str(cal or "").strip(), title=title,
                condition="as sold by me", sale_date=sold_date, price=sell_price,
                currency="USD", bid_count=None, seller="me", url="",
                notes=f"sold to {sold_to}" if sold_to else ""))
    return comps


def from_pdf(path: str) -> list[dict]:
    from pypdf import PdfReader
    text = "\n".join(p.extract_text() or "" for p in PdfReader(path).pages)
    amounts = [float(m.replace(",", ""))
               for m in re.findall(r"US \$([\d,]+\.\d{2})", text)]
    if not amounts:
        amounts = [float(m.replace(",", ""))
                   for m in re.findall(r"\$([\d,]+\.\d{2})", text)]
    if not amounts:
        print(f"[!] no bid amounts found in {path}")
        return []
    title_m = re.search(r"Bid history\s*\n?(.{10,90})", text)
    date_m = re.search(r"\((\d{1,2}\s+\w{3},?\s+\d{4})", text) or \
        re.search(r"(\w{3}\s+\d{1,2},\s+\d{4})", text)
    return [dict(source="bid-history",
                 brand="", model="", reference="", caliber="",
                 title=(title_m.group(1).strip() if title_m else Path(path).parent.name),
                 condition="", sale_date=date_m.group(1) if date_m else "",
                 price=max(amounts), currency="USD", bid_count=len(amounts),
                 seller="", url="", notes=f"parsed from {Path(path).name}")]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--xlsx")
    ap.add_argument("--pdf", action="append", default=[])
    ap.add_argument("--from-csv")
    ap.add_argument("--db")
    ap.add_argument("--to-csv")
    args = ap.parse_args()

    comps: list[dict] = []
    if args.xlsx:
        comps += from_xlsx(args.xlsx)
    for p in args.pdf:
        comps += from_pdf(p)
    if args.from_csv:
        with open(args.from_csv, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                row["price"] = _num(row.get("price"))
                row["bid_count"] = int(row["bid_count"]) if row.get("bid_count") else None
                comps.append(row)

    print(f"collected {len(comps)} comp rows")
    if args.to_csv:
        Path(args.to_csv).parent.mkdir(parents=True, exist_ok=True)
        with open(args.to_csv, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=COLS)
            w.writeheader()
            for c in comps:
                w.writerow({k: c.get(k) for k in COLS})
        print(f"wrote {args.to_csv}")
    if args.db:
        s = Storage(args.db)
        added = sum(1 for c in comps if s.add_comp(**c))
        s.close()
        print(f"imported {added} new comps into {args.db} "
              f"({len(comps) - added} duplicates skipped)")


if __name__ == "__main__":
    main()
