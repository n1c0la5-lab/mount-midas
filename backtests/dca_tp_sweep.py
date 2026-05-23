#!/usr/bin/env python3
import psycopg2
import pandas as pd

DB = dict(host="localhost", port=5434, dbname="mount_midas",
          user="mount_midas", password="8qP4J3NUqXVpUUW51tItL8MqSh04d3B9")
LEVERAGE = 10
MARGIN   = 1.0


def run(df, tp_pct):
    liq_mult  = 1 + 1 / LEVERAGE
    positions = []
    rows      = []
    for i in range(1, len(df)):
        prev      = df.iloc[i - 1]
        curr      = df.iloc[i]
        entry     = float(prev["high"])
        liq_price = entry * liq_mult
        tp_price  = entry * (1 - tp_pct / LEVERAGE)
        c_high    = float(curr["high"])
        c_low     = float(curr["low"])
        c_close   = float(curr["close"])
        liq_today = tp_today = 0

        filled = c_high >= entry
        if filled:
            pos = dict(entry=entry, liq=liq_price, tp=tp_price, status="open")
            if c_high >= liq_price:
                pos["status"] = "liquidated"
                liq_today += 1
            elif c_low <= tp_price:
                pos["status"] = "tp_hit"
                tp_today += 1
            positions.append(pos)

        for pos in (positions[:-1] if filled else positions):
            if pos["status"] != "open":
                continue
            if c_high >= pos["liq"]:
                pos["status"] = "liquidated"
                liq_today += 1
            elif c_low <= pos["tp"]:
                pos["status"] = "tp_hit"
                tp_today += 1

        open_pos  = [p for p in positions if p["status"] == "open"]
        liq_total = sum(1 for p in positions if p["status"] == "liquidated")
        tp_total  = sum(1 for p in positions if p["status"] == "tp_hit")
        open_pnl  = sum(
            (p["entry"] - c_close) / p["entry"] * (MARGIN * LEVERAGE)
            for p in open_pos
        )
        net_pnl = open_pnl + tp_total * MARGIN * tp_pct - liq_total * MARGIN
        rows.append(dict(
            date      = str(curr["date"])[:10],
            net_pnl   = round(net_pnl, 2),
            liq_total = liq_total,
            tp_total  = tp_total,
            filled    = int(filled),
            capital   = len(positions) * MARGIN,
        ))
    return pd.DataFrame(rows)


def main():
    conn = psycopg2.connect(**DB)
    df = pd.read_sql(
        "SELECT date, open, high, low, close FROM ohlcv_daily ORDER BY date", conn
    )
    conn.close()

    tp_levels = [1.0, 2.0, 3.0, 4.0, 5.0]

    header = f"{'TP':>6}  {'Fills':>5}  {'Liq':>11}  {'TP-Hits':>7}  {'Kapital':>7}  {'Net PnL':>8}  {'ROI':>6}  {'Max DD':>8}"
    print()
    print("=" * len(header))
    print("  TP-SWEEP  |  10x  |  Entry=PrevHigh  |  $1/Tag")
    print("=" * len(header))
    print(header)
    print("-" * len(header))

    monthly_data = {}
    for tp in tp_levels:
        r         = run(df, tp)
        last      = r.iloc[-1]
        r["peak"] = r["net_pnl"].cummax()
        fills     = int(r["filled"].sum())
        liq       = int(last["liq_total"])
        tp_h      = int(last["tp_total"])
        cap       = last["capital"]
        net       = last["net_pnl"]
        roi       = net / cap * 100
        max_dd    = (r["net_pnl"] - r["peak"]).min()
        label     = f"TP{int(tp * 100)}%"
        print(
            f"{label:>6}  {fills:>5}  {liq:>4} ({liq/fills*100:.0f}%)  "
            f"{tp_h:>7}  ${cap:>6.0f}  ${net:>7.2f}  {roi:>5.1f}%  ${max_dd:>7.2f}"
        )
        r["month"]     = pd.to_datetime(r["date"]).dt.to_period("M")
        monthly_data[label] = r.groupby("month")["net_pnl"].last()

    print()
    print("=" * len(header))
    print("  MONATLICHES NET PnL")
    print("=" * len(header))
    print(pd.DataFrame(monthly_data).to_string())


if __name__ == "__main__":
    main()
