#!/usr/bin/env python3
"""
ICP Perp DCA-Short Backtest
Strategy: $1 margin short at 20x leverage, entry = prev-day high
- Fill only if today's high reaches prev-day high
- No stop loss — liquidation at +5% above entry (1/leverage)
- Liquidated positions lose full margin ($1)
- Positions accumulate
"""
import psycopg2
import pandas as pd

DB = dict(host="localhost", dbname="mount_midas", user="mount_midas",
          password="Xk9pL3mNqRvT8wYcZhJdF2sA")

MARGIN   = 1.0
LEVERAGE = 20
LIQ_MULT = 1 + 1 / LEVERAGE  # 1.05


def main():
    conn = psycopg2.connect(**DB)
    df = pd.read_sql("SELECT date, open, high, low, close FROM ohlcv_daily ORDER BY date", conn)
    conn.close()

    positions = []
    rows = []

    for i in range(1, len(df)):
        prev       = df.iloc[i - 1]
        curr       = df.iloc[i]
        entry      = float(prev["high"])
        liq_price  = entry * LIQ_MULT
        curr_high  = float(curr["high"])
        curr_close = float(curr["close"])

        liq_today = 0

        # New position opens only if price reaches prev-day high
        filled = curr_high >= entry
        if filled:
            pos = dict(entry=entry, liq=liq_price, status="open")
            if curr_high >= liq_price:
                pos["status"] = "liquidated"
                liq_today += 1
            positions.append(pos)

        # Check all previously open positions
        for pos in (positions[:-1] if filled else positions):
            if pos["status"] != "open":
                continue
            if curr_high >= pos["liq"]:
                pos["status"] = "liquidated"
                liq_today += 1

        open_pos  = [p for p in positions if p["status"] == "open"]
        liq_total = sum(1 for p in positions if p["status"] == "liquidated")
        open_pnl  = sum(
            (p["entry"] - curr_close) / p["entry"] * (MARGIN * LEVERAGE)
            for p in open_pos
        )
        capital   = len(positions) * MARGIN
        net_pnl   = open_pnl - liq_total * MARGIN

        rows.append(dict(
            date      = str(curr["date"])[:10],
            close     = round(curr_close, 4),
            filled    = int(filled),
            liq_today = liq_today,
            liq_total = liq_total,
            open_count= len(open_pos),
            capital   = round(capital, 2),
            open_pnl  = round(open_pnl, 2),
            net_pnl   = round(net_pnl, 2),
        ))

    results = pd.DataFrame(rows)
    last    = results.iloc[-1]

    print(f"\n{'='*52}")
    print(f"  ICP DCA-Short Backtest  |  20x / $1 pro Tag")
    print(f"{'='*52}")
    print(f"Zeitraum:               {results['date'].iloc[0]}  →  {results['date'].iloc[-1]}")
    print(f"Handelstage gesamt:     {len(results)}")
    print(f"Positionen eröffnet:    {int(results['filled'].sum())}")
    print(f"  davon liquidiert:     {int(last['liq_total'])}")
    print(f"  davon noch offen:     {int(last['open_count'])}")
    print()
    print(f"Kapital eingesetzt:     ${last['capital']:.2f}")
    print(f"Liquidierungsverlust:   ${last['liq_total'] * MARGIN:.2f}")
    print(f"Unrealisierter PnL:     ${last['open_pnl']:.2f}")
    print(f"Net PnL:                ${last['net_pnl']:.2f}")
    roi = last['net_pnl'] / last['capital'] * 100 if last['capital'] else 0
    print(f"ROI (auf Kapital):      {roi:.1f}%")

    # Monthly breakdown
    results["month"] = pd.to_datetime(results["date"]).dt.to_period("M")
    monthly = results.groupby("month").agg(
        fills    = ("filled",    "sum"),
        liqs     = ("liq_today", "sum"),
        net_pnl  = ("net_pnl",   "last"),
        price    = ("close",     "last"),
    )
    print(f"\n{'='*52}")
    print(f"  Monatliche Übersicht")
    print(f"{'='*52}")
    print(monthly.to_string())

    # Worst drawdown on open_pnl
    results["peak"]     = results["net_pnl"].cummax()
    results["drawdown"] = results["net_pnl"] - results["peak"]
    max_dd = results["drawdown"].min()
    max_dd_date = results.loc[results["drawdown"].idxmin(), "date"]
    print(f"\nMax Drawdown (Net PnL): ${max_dd:.2f}  am {max_dd_date}")


if __name__ == "__main__":
    main()
