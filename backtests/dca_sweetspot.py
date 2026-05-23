#!/usr/bin/env python3
"""
ICP DCA-Short — Sweet Spot Analyse (2D Grid)
Signal-Logik:
  Aktivierung:   Einzelner Tag mit Return >= pump_threshold
  Trailing High: pump_high = max(pump_high, close) solange aktiv
  Deaktivierung: close < pump_high * (1 - bottom_drop)
Setup: 10x Leverage, $1/Tag, Entry=PrevHigh, TP 300%
"""
import psycopg2
import pandas as pd

DB = dict(host="localhost", port=5434, dbname="mount_midas",
          user="mount_midas", password="8qP4J3NUqXVpUUW51tItL8MqSh04d3B9")

LEVERAGE = 10
MARGIN   = 1.0
TP_PCT   = 3.0  # 300%


def run(df, pump_threshold, bottom_drop):
    liq_mult  = 1 + 1 / LEVERAGE
    positions = []
    rows      = []
    active    = False
    pump_high = None

    for i in range(1, len(df)):
        prev    = df.iloc[i - 1]
        curr    = df.iloc[i]
        c_high  = float(curr["high"])
        c_low   = float(curr["low"])
        c_close = float(curr["close"])
        p_close = float(prev["close"])

        daily_return = (c_close - p_close) / p_close

        # Aktivierung
        if not active and daily_return >= pump_threshold:
            active    = True
            pump_high = c_close

        # Trailing High + Deaktivierung
        if active:
            pump_high = max(pump_high, c_close)
            if c_close < pump_high * (1 - bottom_drop):
                active    = False
                pump_high = None

        # Neue Position
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
                    pos["status"] = "liquidated"; liq_today += 1
                elif c_low <= tp_price:
                    pos["status"] = "tp_hit";     tp_today  += 1
                positions.append(pos)

        # Bestehende Positionen immer managen
        for pos in (positions[:-1] if filled else positions):
            if pos["status"] != "open":
                continue
            if c_high >= pos["liq"]:
                pos["status"] = "liquidated"; liq_today += 1
            elif c_low <= pos["tp"]:
                pos["status"] = "tp_hit";     tp_today  += 1

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
            date      = str(curr["date"])[:10],
            close     = round(c_close, 4),
            active    = int(active),
            filled    = int(filled),
            liq_total = liq_total,
            tp_total  = tp_total,
            open_count= len(open_pos),
            capital   = round(capital, 2),
            net_pnl   = round(net_pnl, 2),
        ))

    return pd.DataFrame(rows)


def metrics(r):
    last   = r.iloc[-1]
    r      = r.copy()
    r["peak"] = r["net_pnl"].cummax()
    cap    = last["capital"]
    net    = last["net_pnl"]
    roi    = net / cap * 100 if cap else 0.0
    max_dd = (r["net_pnl"] - r["peak"]).min()
    fills  = int(r["filled"].sum())
    liq    = int(last["liq_total"])
    tp_h   = int(last["tp_total"])
    active = int(r["active"].sum())
    return dict(net=net, roi=roi, max_dd=max_dd, fills=fills,
                liq=liq, tp=tp_h, capital=cap, active_days=active)


def print_grid(title, grid_data, fmt):
    pumps   = sorted(grid_data.keys())
    bottoms = sorted(next(iter(grid_data.values())).keys())
    col_w   = 10
    header  = f"{'Pump\\Bot':>10}" + "".join(f"{f'Bot-{int(b*100)}%':>{col_w}}" for b in bottoms)
    print(f"\n{title}")
    print("-" * len(header))
    print(header)
    print("-" * len(header))
    for p in pumps:
        row = f"Pump>{int(p*100)}%".rjust(10)
        for b in bottoms:
            val = grid_data[p][b]
            row += fmt(val).rjust(col_w)
        print(row)
    print("-" * len(header))


def main():
    conn = psycopg2.connect(**DB)
    df = pd.read_sql(
        "SELECT date, open, high, low, close FROM ohlcv_daily ORDER BY date", conn
    )
    conn.close()

    pump_thresholds = [0.06, 0.08, 0.10, 0.12, 0.15, 0.20]
    bottom_drops    = [0.15, 0.20, 0.25, 0.30, 0.40]

    print(f"\nOHLCV: {str(df['date'].iloc[0])[:10]}  ->  {str(df['date'].iloc[-1])[:10]}")
    print(f"Setup:  10x | TP 300% | Trailing High ab Pump-Event\n")
    print("Berechne Grid", end="", flush=True)

    grid = {}
    for p in pump_thresholds:
        grid[p] = {}
        for b in bottom_drops:
            r = run(df, p, b)
            grid[p][b] = metrics(r)
            print(".", end="", flush=True)
    print(" fertig\n")

    # Referenz: pure baseline (kein Filter, TP 300%)
    r_base = run(df, pump_threshold=-1.0, bottom_drop=0.0)
    base   = metrics(r_base)
    print(f"Referenz (kein Filter, TP 300%): Net=${base['net']:.2f}  ROI={base['roi']:.1f}%  MaxDD=${base['max_dd']:.2f}  Fills={base['fills']}")

    # Grid-Tabellen
    print_grid(
        "NET PnL ($)",
        grid,
        lambda m: f"${m['net']:.1f}"
    )
    print_grid(
        "ROI (%)",
        grid,
        lambda m: f"{m['roi']:.1f}%"
    )
    print_grid(
        "MAX DRAWDOWN ($)",
        grid,
        lambda m: f"${m['max_dd']:.1f}"
    )
    print_grid(
        "FILLS (Anzahl Positionen)",
        grid,
        lambda m: str(m['fills'])
    )
    print_grid(
        "LIQ-RATE (%)",
        grid,
        lambda m: f"{m['liq']/m['fills']*100:.0f}%" if m['fills'] else "n/a"
    )

    # Top 5 Kombinationen nach Net PnL
    print("\n=== TOP 10 nach Net PnL ===")
    ranked = []
    for p in pump_thresholds:
        for b in bottom_drops:
            m = grid[p][b]
            ranked.append(dict(
                combo   = f"Pump>{int(p*100)}% / Bot-{int(b*100)}%",
                fills   = m["fills"],
                liq_pct = f"{m['liq']/m['fills']*100:.0f}%" if m['fills'] else "n/a",
                capital = f"${m['capital']:.0f}",
                net_pnl = m["net"],
                roi     = f"{m['roi']:.1f}%",
                max_dd  = f"${m['max_dd']:.2f}",
                active  = m["active_days"],
            ))
    top = sorted(ranked, key=lambda x: x["net_pnl"], reverse=True)[:10]
    print(pd.DataFrame(top).set_index("combo").to_string())


if __name__ == "__main__":
    main()
