#!/usr/bin/env python3
"""
ICP DCA-Short — Varianten-Vergleich
Baseline:   20x, Entry=PrevHigh, kein TP
Variante A: 10x, Entry=PrevHigh, kein TP
Variante B:  5x, Entry=PrevHigh, kein TP
Variante C: 10x, Entry=PrevClose (market-ähnlich), kein TP
Variante D: 10x, Entry=PrevHigh, TP bei +100% auf Margin (pos zahlt sich zurück)
"""
import psycopg2
import pandas as pd

DB = dict(host="localhost", port=5434, dbname="mount_midas",
          user="mount_midas", password="8qP4J3NUqXVpUUW51tItL8MqSh04d3B9")


def run(df, leverage, entry_col="high", take_profit_pct=None):
    """
    entry_col:       'high' = PrevHigh, 'close' = PrevClose
    take_profit_pct: None = kein TP, 1.0 = 100% auf Margin (= $1 Profit bei $1 Margin)
    """
    liq_mult = 1 + 1 / leverage
    positions = []
    rows = []

    for i in range(1, len(df)):
        prev       = df.iloc[i - 1]
        curr       = df.iloc[i]
        entry      = float(prev[entry_col])
        liq_price  = entry * liq_mult
        curr_high  = float(curr["high"])
        curr_low   = float(curr["low"])
        curr_close = float(curr["close"])

        liq_today = 0
        tp_today  = 0

        # TP-Zielpreis: entry - (margin * tp_pct / notional) * entry
        # Vereinfacht: entry * (1 - tp_pct / leverage)
        tp_price = entry * (1 - take_profit_pct / leverage) if take_profit_pct else None

        # Neues Short öffnen (Fill wenn Preis entry erreicht)
        # Bei PrevClose als Entry: wir nehmen an, immer gefüllt (Open kann drüber/drunter sein)
        filled = (entry_col == "close") or (curr_high >= entry)
        if filled:
            pos = dict(entry=entry, liq=liq_price, tp=tp_price, status="open")
            # Sofort-Liquidation wenn Preis entry UND liq_price am gleichen Tag kreuzt
            if curr_high >= liq_price:
                pos["status"] = "liquidated"
                liq_today += 1
            elif tp_price and curr_low <= tp_price:
                # TP wurde getroffen (Preis fiel auf tp_price)
                pos["status"] = "tp_hit"
                pos["tp_hit_price"] = tp_price
                tp_today += 1
            positions.append(pos)

        # Bestehende offene Positionen prüfen (alle außer der gerade eröffneten)
        check_range = positions[:-1] if filled else positions
        for pos in check_range:
            if pos["status"] != "open":
                continue
            if curr_high >= pos["liq"]:
                pos["status"] = "liquidated"
                liq_today += 1
            elif pos["tp"] and curr_low <= pos["tp"]:
                pos["status"] = "tp_hit"
                pos["tp_hit_price"] = pos["tp"]
                tp_today += 1

        open_pos  = [p for p in positions if p["status"] == "open"]
        liq_total = sum(1 for p in positions if p["status"] == "liquidated")
        tp_total  = sum(1 for p in positions if p["status"] == "tp_hit")

        # PnL offener Positionen
        open_pnl = sum(
            (p["entry"] - curr_close) / p["entry"] * (1.0 * leverage)
            for p in open_pos
        )
        # Realisierter PnL aus TP-Positionen (jede hat margin * tp_pct verdient)
        tp_pnl = tp_total * (1.0 * take_profit_pct) if take_profit_pct else 0.0

        capital  = len(positions) * 1.0
        net_pnl  = open_pnl + tp_pnl - liq_total * 1.0

        rows.append(dict(
            date       = str(curr["date"])[:10],
            close      = round(curr_close, 4),
            filled     = int(filled),
            liq_today  = liq_today,
            tp_today   = tp_today,
            liq_total  = liq_total,
            tp_total   = tp_total,
            open_count = len(open_pos),
            capital    = round(capital, 2),
            open_pnl   = round(open_pnl, 2),
            tp_pnl     = round(tp_pnl, 2),
            net_pnl    = round(net_pnl, 2),
        ))

    return pd.DataFrame(rows)


def summary(label, results):
    last = results.iloc[-1]
    fills     = int(results["filled"].sum())
    liq_total = int(last["liq_total"])
    tp_total  = int(last["tp_total"])
    capital   = last["capital"]
    net_pnl   = last["net_pnl"]
    roi       = net_pnl / capital * 100 if capital else 0
    results["peak"]     = results["net_pnl"].cummax()
    results["drawdown"] = results["net_pnl"] - results["peak"]
    max_dd    = results["drawdown"].min()
    liq_pct   = liq_total / fills * 100 if fills else 0
    return dict(
        label    = label,
        fills    = fills,
        liq      = f"{liq_total} ({liq_pct:.0f}%)",
        tp       = tp_total,
        capital  = f"${capital:.0f}",
        net_pnl  = f"${net_pnl:.2f}",
        roi      = f"{roi:.1f}%",
        max_dd   = f"${max_dd:.2f}",
    )


def main():
    conn = psycopg2.connect(**DB)
    df = pd.read_sql(
        "SELECT date, open, high, low, close FROM ohlcv_daily ORDER BY date", conn
    )
    conn.close()

    print(f"\nOHLCV: {str(df['date'].iloc[0])[:10]}  →  {str(df['date'].iloc[-1])[:10]}  ({len(df)} Tage)\n")

    variants = [
        ("Baseline  20x PrevHigh    kein TP", dict(leverage=20, entry_col="high")),
        ("Var A     10x PrevHigh    kein TP", dict(leverage=10, entry_col="high")),
        ("Var B      5x PrevHigh    kein TP", dict(leverage=5,  entry_col="high")),
        ("Var C     10x PrevClose   kein TP", dict(leverage=10, entry_col="close")),
        ("Var D     10x PrevHigh    TP 100%", dict(leverage=10, entry_col="high", take_profit_pct=1.0)),
    ]

    summaries = []
    all_results = {}
    for label, kwargs in variants:
        r = run(df, **kwargs)
        summaries.append(summary(label, r))
        all_results[label] = r

    # Übersichtstabelle
    summary_df = pd.DataFrame(summaries).set_index("label")
    print("=" * 80)
    print("  VARIANTEN-VERGLEICH  |  $1 Margin pro Tag  |  Entry = Prev-Day-Preis")
    print("=" * 80)
    print(summary_df.to_string())

    # Monatliches Net PnL aller Varianten nebeneinander
    print("\n" + "=" * 80)
    print("  MONATLICHES NET PnL")
    print("=" * 80)
    monthly = {}
    for label, r in all_results.items():
        r["month"] = pd.to_datetime(r["date"]).dt.to_period("M")
        monthly[label[:8].strip()] = r.groupby("month")["net_pnl"].last()

    monthly_df = pd.DataFrame(monthly)
    print(monthly_df.to_string())


if __name__ == "__main__":
    main()
