#!/usr/bin/env python3
"""
ICP DCA-Short — Signal-gesteuerter Backtest
Aktivierung:    Einzelner Tag mit Return > pump_threshold  (z.B. +15%)
Deaktivierung:  Close < rolling_30d_high * (1 - bottom_drop)  (z.B. -30% vom Hoch)
Setup:          10x Leverage, $1 Margin/Tag, Entry=PrevHigh, TP 300%
"""
import psycopg2
import pandas as pd

DB = dict(host="localhost", port=5434, dbname="mount_midas",
          user="mount_midas", password="8qP4J3NUqXVpUUW51tItL8MqSh04d3B9")

LEVERAGE    = 10
MARGIN      = 1.0
TP_PCT      = 3.0   # 300% auf Margin
BOTTOM_DROP = 0.30  # -30% vom lokalen Hoch
PEAK_WINDOW = 30    # Rollendes Hoch über 30 Tage


def run(df, pump_threshold):
    liq_mult  = 1 + 1 / LEVERAGE
    positions = []
    rows      = []
    active    = False

    for i in range(1, len(df)):
        prev    = df.iloc[i - 1]
        curr    = df.iloc[i]
        c_open  = float(curr["open"])
        c_high  = float(curr["high"])
        c_low   = float(curr["low"])
        c_close = float(curr["close"])
        p_close = float(prev["close"])

        # Pump-Signal: heutiger Return > Threshold
        daily_return = (c_close - p_close) / p_close
        pump_signal  = daily_return >= pump_threshold

        # Bottom-Signal: Close > PEAK_WINDOW-Tage zurück als rollendes Hoch
        window_start = max(0, i - PEAK_WINDOW)
        rolling_high = df["close"].iloc[window_start:i].max()
        bottom_signal = c_close < rolling_high * (1 - BOTTOM_DROP)

        # State-Machine
        if pump_signal and not active:
            active = True
        if bottom_signal and active:
            active = False

        # Neue Position nur wenn aktiv
        liq_today = tp_today = 0
        filled    = False

        if active:
            entry     = float(prev["high"])
            liq_price = entry * liq_mult
            tp_price  = entry * (1 - TP_PCT / LEVERAGE)
            filled    = c_high >= entry
            if filled:
                pos = dict(entry=entry, liq=liq_price, tp=tp_price, status="open")
                if c_high >= liq_price:
                    pos["status"] = "liquidated"
                    liq_today += 1
                elif c_low <= tp_price:
                    pos["status"] = "tp_hit"
                    tp_today += 1
                positions.append(pos)

        # Bestehende Positionen immer weiter managen (auch wenn inaktiv)
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
        net_pnl = open_pnl + tp_total * MARGIN * TP_PCT - liq_total * MARGIN
        capital = len(positions) * MARGIN

        rows.append(dict(
            date        = str(curr["date"])[:10],
            close       = round(c_close, 4),
            active      = int(active),
            pump_signal = int(pump_signal),
            bot_signal  = int(bottom_signal),
            filled      = int(filled),
            liq_today   = liq_today,
            tp_today    = tp_today,
            liq_total   = liq_total,
            tp_total    = tp_total,
            open_count  = len(open_pos),
            capital     = round(capital, 2),
            open_pnl    = round(open_pnl, 2),
            net_pnl     = round(net_pnl, 2),
        ))

    return pd.DataFrame(rows)


def summarize(label, r):
    last   = r.iloc[-1]
    r      = r.copy()
    r["peak"] = r["net_pnl"].cummax()
    fills  = int(r["filled"].sum())
    liq    = int(last["liq_total"])
    tp_h   = int(last["tp_total"])
    cap    = last["capital"]
    net    = last["net_pnl"]
    roi    = net / cap * 100 if cap else 0
    max_dd = (r["net_pnl"] - r["peak"]).min()
    pumps  = int(r["pump_signal"].sum())
    bots   = int(r["bot_signal"].sum())
    active_days = int(r["active"].sum())
    liq_pct = liq / fills * 100 if fills else 0
    return dict(
        label       = label,
        pump_days   = pumps,
        bot_days    = bots,
        active_days = active_days,
        fills       = fills,
        liq         = f"{liq} ({liq_pct:.0f}%)",
        tp_hits     = tp_h,
        capital     = f"${cap:.0f}",
        net_pnl     = f"${net:.2f}",
        roi         = f"{roi:.1f}%",
        max_dd      = f"${max_dd:.2f}",
    )


def main():
    conn = psycopg2.connect(**DB)
    df = pd.read_sql(
        "SELECT date, open, high, low, close FROM ohlcv_daily ORDER BY date", conn
    )
    conn.close()

    print(f"\nOHLCV: {str(df['date'].iloc[0])[:10]}  ->  {str(df['date'].iloc[-1])[:10]}")
    print(f"Setup:  10x Leverage | TP 300% | Bottom -30% vom 30d-Hoch\n")

    thresholds = [0.08, 0.10, 0.15, 0.20, 0.25]
    summaries  = []
    all_results = {}

    for t in thresholds:
        label = f"Pump >{int(t*100)}%"
        r     = run(df, t)
        summaries.append(summarize(label, r))
        all_results[label] = r

    # Auch ungefilterte Baseline zum Vergleich
    baseline_label = "Baseline (kein Filter)"
    r_base = run(df.copy(), pump_threshold=-1.0)  # immer aktiv
    summaries.append(summarize(baseline_label, r_base))
    all_results[baseline_label] = r_base

    sdf = pd.DataFrame(summaries).set_index("label")
    print("=" * 90)
    print("  PUMP-FILTER SWEEP  |  $1/Tag  |  10x  |  TP 300%  |  Stop: -30% vom 30d-Hoch")
    print("=" * 90)
    print(sdf.to_string())

    # Monatliches Net PnL
    print("\n" + "=" * 90)
    print("  MONATLICHES NET PnL")
    print("=" * 90)
    monthly = {}
    for label, r in all_results.items():
        r = r.copy()
        r["month"] = pd.to_datetime(r["date"]).dt.to_period("M")
        short_label = label.replace("Pump >", "P>").replace("Baseline (kein Filter)", "Baseline")
        monthly[short_label] = r.groupby("month")["net_pnl"].last()
    print(pd.DataFrame(monthly).to_string())

    # Aktivierungsperioden anzeigen fuer den besten Threshold
    print("\n" + "=" * 90)
    print("  PUMP-EVENTS & BOTTOM-EVENTS  (Pump >15%)")
    print("=" * 90)
    r15 = all_results["Pump >15%"].copy()
    pump_events  = r15[r15["pump_signal"] == 1][["date", "close"]]
    bot_events   = r15[r15["bot_signal"] == 1][["date", "close"]].drop_duplicates("date")
    print(f"\nPump-Events ({len(pump_events)}):")
    print(pump_events.to_string(index=False))
    print(f"\nBottom-Events ({len(bot_events)} Tage, erste/letzte je Periode):")
    r15["bot_grp"] = (r15["bot_signal"] != r15["bot_signal"].shift()).cumsum()
    bot_periods = r15[r15["bot_signal"] == 1].groupby("bot_grp").agg(
        start=("date", "first"), end=("date", "last"), low=("close", "min")
    )
    print(bot_periods.to_string())


if __name__ == "__main__":
    main()
