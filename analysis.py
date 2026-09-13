"""
analysis.py
===========
Runs every SQL query under queries/ against data/risk_engine.db, prints the
results in readable tables, runs the optimization demonstration (EXPLAIN
QUERY PLAN + timing, before/after adding indexes), then exports:
  - figures/*.png   -- report-ready charts (300 dpi) built from the SQL results
  - results/*.csv   -- the underlying tables, one CSV per query result

Scope discipline: pandas is used ONLY to format, plot, and export results
that SQL already computed. matplotlib is used ONLY to visualize numbers SQL
already produced (bar heights, line positions, stacked segment widths come
straight from query columns). No P&L, VaR, z-score, position, or ranking
figure is computed or adjusted in Python anywhere in this file.

Run with:  python analysis.py
"""

import re
import sqlite3
import statistics
import sys
import time
from pathlib import Path

import matplotlib
matplotlib.use("Agg")  # headless: this script only ever writes PNG files, never shows a window

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.lines import Line2D
from matplotlib.patches import Patch

# Force UTF-8 stdout regardless of the host console's codepage (Windows
# terminals sometimes default to cp1252, which would otherwise mangle
# non-ASCII characters in printed output).
sys.stdout.reconfigure(encoding="utf-8")

ROOT_DIR = Path(__file__).resolve().parent
DB_PATH = ROOT_DIR / "data" / "risk_engine.db"
SCHEMA_PATH = ROOT_DIR / "schema.sql"
QUERIES_DIR = ROOT_DIR / "queries"
FIGURES_DIR = ROOT_DIR / "figures"
RESULTS_DIR = ROOT_DIR / "results"

pd.set_option("display.width", 140)
pd.set_option("display.max_columns", 20)
pd.set_option("display.float_format", lambda v: f"{v:,.4f}")

# ----------------------------------------------------------------------------
# Chart styling — a small fixed palette used consistently across every figure.
# Categorical hues are assigned in this fixed order (never cycled/reused per
# chart), so e.g. slot 1 always means "raw/before" and slot 2 always means
# "adjusted" wherever that contrast appears.
# ----------------------------------------------------------------------------

CATEGORICAL = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
STATUS_GOOD = "#0ca30c"
STATUS_CRITICAL = "#d03b3b"
INK_PRIMARY = "#0b0b0b"
INK_SECONDARY = "#52514e"
INK_MUTED = "#898781"
GRID_COLOR = "#e1e0d9"
AXIS_COLOR = "#c3c2b7"
SURFACE = "#fcfcfb"

plt.rcParams["font.family"] = "sans-serif"
plt.rcParams["font.sans-serif"] = ["Segoe UI", "DejaVu Sans", "Arial"]


def style_axes(ax, grid_axis="x"):
    ax.set_facecolor(SURFACE)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["bottom"].set_color(AXIS_COLOR)
    ax.spines["left"].set_color(AXIS_COLOR)
    ax.tick_params(colors=INK_MUTED, labelsize=9)
    ax.xaxis.label.set_color(INK_SECONDARY)
    ax.yaxis.label.set_color(INK_SECONDARY)
    ax.grid(axis=grid_axis, color=GRID_COLOR, linewidth=0.8, zorder=0)
    ax.set_axisbelow(True)


def save_figure(fig, filename):
    path = FIGURES_DIR / filename
    fig.tight_layout()
    fig.savefig(path, dpi=300, bbox_inches="tight", facecolor=SURFACE)
    plt.close(fig)
    print(f"  -> figures/{filename}")


def section(title: str):
    print("\n" + "=" * 100)
    print(title)
    print("=" * 100)


def load_query(filename: str) -> str:
    return (QUERIES_DIR / filename).read_text(encoding="utf-8")


def run_df(conn: sqlite3.Connection, sql: str) -> pd.DataFrame:
    return pd.read_sql_query(sql, conn)


# ----------------------------------------------------------------------------
# Part 1 - run all 9 analytical queries, display their results, and return
# the DataFrames so Part 3 can chart/export them without re-querying.
# ----------------------------------------------------------------------------

def run_all_queries(conn: sqlite3.Connection) -> dict:
    dfs = {}

    section("QUERY 1 - Running-total position per client / instrument")
    df = run_df(conn, load_query("01_position_running_total.sql"))
    dfs["positions_running_total"] = df
    print(f"{len(df):,} rows (one per trade). Sample for a single client/instrument book:")
    sample_key = df.iloc[0][["client_id", "instrument_id"]]
    print(df[(df.client_id == sample_key.client_id) & (df.instrument_id == sample_key.instrument_id)]
          .head(8).to_string(index=False))

    section("QUERY 2 - Realized & unrealized P&L (CTE pipeline)")
    df = run_df(conn, load_query("02_pnl_realized_unrealized.sql"))
    dfs["pnl_realized_unrealized"] = df
    print(f"{len(df):,} (client, instrument) positions valued. Top 10 by total P&L:")
    print(df.head(10).to_string(index=False))
    print(f"\nPortfolio-wide totals -> realized: {df.realized_pnl.sum():,.2f}   "
          f"unrealized: {df.unrealized_pnl.sum():,.2f}   total: {df.total_pnl.sum():,.2f}")

    section("QUERY 3 - Historical VaR (95%), trailing 250-trading-day window")
    df = run_df(conn, load_query("03_var_historical.sql"))
    dfs["var_by_client"] = df
    print(df.to_string(index=False))

    section("QUERY 4 - Price anomaly detection via rolling z-score")
    df = run_df(conn, load_query("04_zscore_anomaly_detection.sql"))
    dfs["detected_anomalies"] = df
    print(f"{len(df):,} trades flagged with |z-score| > 3 (out of 42,000 total trades).")
    print("Most extreme anomalies:")
    print(df.head(10).to_string(index=False))

    section("QUERY 5 - Cumulative corporate-action adjustment factor (recursive CTE)")
    df = run_df(conn, load_query("05_corporate_actions_recursive_factor.sql"))
    dfs["corporate_action_factors"] = df
    print(df.to_string(index=False))

    section("QUERY 6 - Trade quantities adjusted for corporate actions")
    df = run_df(conn, load_query("06_position_adjusted_corporate_actions.sql"))
    dfs["positions_adjusted"] = df
    adjusted = df[df.adjustment_factor != 1.0]
    print(f"{len(df):,} trade rows adjusted. {len(adjusted):,} trades carry a non-trivial "
          f"adjustment factor (i.e. predate at least one later split).")
    print("Sample of adjusted trades:")
    print(adjusted.head(8).to_string(index=False))

    section("QUERY 7 - Position reconciliation vs. declared positions")
    df = run_df(conn, load_query("07_reconciliation.sql"))
    dfs["position_reconciliation"] = df
    print(f"{len(df):,} discrepancies detected:")
    print(df["discrepancy_type"].value_counts().to_string())
    print()
    print(df.to_string(index=False))

    section("QUERY 8 - Sector exposure and concentration flagging")
    df = run_df(conn, load_query("08_sector_concentration.sql"))
    dfs["sector_concentration"] = df
    flagged = df[df.concentration_flag == 1]
    print(f"{flagged.client_id.nunique()} client(s) flagged with >40% exposure to a single sector:")
    print(flagged[["client_id", "client_name", "sector", "pct_of_portfolio"]].to_string(index=False))

    section("QUERY 9 - Client performance ranking")
    df = run_df(conn, load_query("09_performance_ranking.sql"))
    dfs["performance_ranking"] = df
    print(df.to_string(index=False))

    return dfs


# ----------------------------------------------------------------------------
# Part 2 - optimization: EXPLAIN QUERY PLAN + timing, before vs after indexes
# ----------------------------------------------------------------------------

HEAVY_QUERIES = {
    "Query 1 - running-total position (full trades scan, window function)": "01_position_running_total.sql",
    "Query 4 - z-score anomaly detection (full trades scan, window function)": "04_zscore_anomaly_detection.sql",
    "Query 3 - historical VaR (trades + market_data join across a 250-day calendar)": "03_var_historical.sql",
}


def parse_optimized_indexes():
    text = SCHEMA_PATH.read_text(encoding="utf-8")
    idx_section = text.split("-- 2. OPTIMIZED INDEXES")[1]
    statements = re.findall(r"CREATE INDEX[^;]+;", idx_section, flags=re.IGNORECASE | re.DOTALL)
    names = re.findall(r"CREATE INDEX IF NOT EXISTS\s+(\w+)", idx_section, flags=re.IGNORECASE)
    return statements, names


def explain(conn: sqlite3.Connection, sql: str) -> pd.DataFrame:
    return pd.read_sql_query("EXPLAIN QUERY PLAN " + sql, conn)


def bench(conn: sqlite3.Connection, sql: str, repeats: int = 7):
    times = []
    for _ in range(repeats):
        t0 = time.perf_counter()
        conn.execute(sql).fetchall()
        times.append(time.perf_counter() - t0)
    return times


def run_optimization_section(conn: sqlite3.Connection) -> pd.DataFrame:
    section("OPTIMIZATION - EXPLAIN QUERY PLAN + timing, before vs. after indexes")
    print(
        "The 3 heaviest queries all run a window function over the FULL trades table,\n"
        "partitioned by (client_id[, instrument_id]) and ordered by timestamp. Without a\n"
        "matching index, SQLite must materialize the partition/order key into a temporary\n"
        "B-tree to sort it before it can stream rows into the window function -- that's the\n"
        "'USE TEMP B-TREE FOR ORDER BY' line in EXPLAIN QUERY PLAN below. A composite index\n"
        "on the same (partition columns..., order column) lets SQLite scan the table\n"
        "already in the required order, and that plan line disappears.\n"
    )

    index_statements, index_names = parse_optimized_indexes()

    # Ensure a clean "before" state regardless of prior runs of this script.
    for name in index_names:
        conn.execute(f"DROP INDEX IF EXISTS {name}")
    conn.commit()

    results = {}
    for label, filename in HEAVY_QUERIES.items():
        sql = load_query(filename)
        print("\n" + "-" * 100)
        print(f"{label}\n")
        print(">>> EXPLAIN QUERY PLAN (BEFORE indexes)")
        print(explain(conn, sql).to_string(index=False))
        times_before = bench(conn, sql)
        results[label] = {"before": times_before}
        print(f"\nWall-clock timing, {len(times_before)} runs (seconds): "
              f"{[round(t, 4) for t in times_before]}")
        print(f"Median before: {statistics.median(times_before):.4f}s")

    print("\n" + "-" * 100)
    print("Applying optimized indexes from schema.sql (section 2)...")
    for stmt in index_statements:
        conn.execute(stmt)
    conn.commit()
    print("Indexes created:", ", ".join(index_names))

    for label, filename in HEAVY_QUERIES.items():
        sql = load_query(filename)
        print("\n" + "-" * 100)
        print(f"{label}\n")
        print(">>> EXPLAIN QUERY PLAN (AFTER indexes)")
        print(explain(conn, sql).to_string(index=False))
        times_after = bench(conn, sql)
        results[label]["after"] = times_after
        print(f"\nWall-clock timing, {len(times_after)} runs (seconds): "
              f"{[round(t, 4) for t in times_after]}")
        print(f"Median after: {statistics.median(times_after):.4f}s")

    section("OPTIMIZATION SUMMARY")
    summary_rows = []
    for label, r in results.items():
        before_med = statistics.median(r["before"])
        after_med = statistics.median(r["after"])
        gain_pct = (before_med - after_med) / before_med * 100
        summary_rows.append({
            "query": label,
            "median_before_s": round(before_med, 4),
            "median_after_s": round(after_med, 4),
            "gain_pct": round(gain_pct, 1),
        })
    summary_df = pd.DataFrame(summary_rows)
    print(summary_df.to_string(index=False))
    print(
        "\nNote on scale: with ~42k trades / ~21k market_data rows, SQLite keeps the whole\n"
        "working set in OS page cache, so absolute wall-clock gains are modest even though\n"
        "the query plan is structurally cheaper (sort eliminated). The gain would widen at\n"
        "larger row counts, where the eliminated O(n log n) temp B-tree sort dominates more."
    )
    return summary_df


# ----------------------------------------------------------------------------
# Part 3 - figures/*.png (report-ready, 300 dpi) built from the DataFrames above
# ----------------------------------------------------------------------------

def plot_var_by_client(var_df: pd.DataFrame):
    d = var_df.sort_values("var_95_pct_loss", ascending=True)  # ascending so the riskiest client ends up on top
    fig, ax = plt.subplots(figsize=(10, 9))
    ax.barh(d.client_name, d.var_95_pct_loss, color=CATEGORICAL[0], height=0.65, zorder=3)

    mean_val = d.var_95_pct_loss.mean()
    ax.axvline(mean_val, color=INK_SECONDARY, linestyle="--", linewidth=1.5, zorder=4,
               label=f"Portfolio average ({mean_val:.2f}%)")

    ax.set_title("Historical VaR (95%) by Client — Trailing 250-Trading-Day Window",
                 fontsize=14, fontweight="bold", color=INK_PRIMARY, pad=14)
    ax.set_xlabel("1-day VaR at 95% confidence (% of portfolio value)")
    ax.set_ylabel("Client")
    ax.legend(loc="lower right", frameon=False, fontsize=9)
    style_axes(ax, grid_axis="x")
    save_figure(fig, "historical_var_by_client.png")


def pick_split_example(q1_df: pd.DataFrame, q6_df: pd.DataFrame, q5_df: pd.DataFrame):
    """Pick the (client, instrument) pair with the most trades that actually
    straddles a split date, so the raw-vs-adjusted divergence is visible."""
    split_instruments = set(q5_df.loc[q5_df.action_type == "SPLIT", "instrument_id"])
    candidates = q6_df[q6_df.instrument_id.isin(split_instruments)]
    stats = (candidates.groupby(["client_id", "instrument_id"])
             .agg(n=("trade_id", "count"), n_factors=("adjustment_factor", "nunique"))
             .reset_index())
    straddling = stats[stats.n_factors > 1]
    pool = straddling if not straddling.empty else stats
    best = pool.sort_values("n", ascending=False).iloc[0]
    return int(best.client_id), int(best.instrument_id)


def plot_position_adjustment(q1_df: pd.DataFrame, q6_df: pd.DataFrame, q5_df: pd.DataFrame):
    client_id, instrument_id = pick_split_example(q1_df, q6_df, q5_df)

    raw = q1_df[(q1_df.client_id == client_id) & (q1_df.instrument_id == instrument_id)].sort_values("trade_date")
    adj = q6_df[(q6_df.client_id == client_id) & (q6_df.instrument_id == instrument_id)].sort_values("trade_date")
    splits = q5_df[(q5_df.instrument_id == instrument_id) & (q5_df.action_type == "SPLIT")]

    ticker = raw.ticker.iloc[0]
    client_name = raw.client_name.iloc[0]
    dates_raw = pd.to_datetime(raw.trade_date)
    dates_adj = pd.to_datetime(adj.trade_date)

    fig, ax = plt.subplots(figsize=(11, 6))
    ax.step(dates_raw, raw.running_position, where="post", color=CATEGORICAL[0],
             linewidth=2, label="Raw position (unadjusted)", zorder=3)
    ax.step(dates_adj, adj.running_adjusted_position, where="post", color=CATEGORICAL[1],
             linewidth=2, linestyle="--", label="Adjusted position (corporate actions applied)", zorder=3)

    for _, row in splits.iterrows():
        split_date = pd.to_datetime(row.date)
        ax.axvline(split_date, color=INK_MUTED, linestyle=":", linewidth=1.3, zorder=2)
        ax.text(split_date, 1.01, f"{row.event_factor:g}:1 split", transform=ax.get_xaxis_transform(),
                fontsize=8, color=INK_SECONDARY, ha="center", va="bottom")

    ax.set_title(f"Raw vs. Corporate-Action-Adjusted Position — {client_name} / {ticker}",
                 fontsize=14, fontweight="bold", color=INK_PRIMARY, pad=14)
    ax.set_xlabel("Date")
    ax.set_ylabel("Position (shares)")
    ax.legend(loc="upper left", frameon=False, fontsize=9)
    style_axes(ax, grid_axis="y")
    fig.autofmt_xdate()
    save_figure(fig, "positions_before_after_adjustment.png")


def plot_sector_concentration(sector_df: pd.DataFrame):
    # The >40% rule is tested against a single number per client: their
    # largest single-sector exposure. A stacked full-breakdown bar would put
    # every segment but the first at some arbitrary offset from zero, making
    # a fixed 40% reference line meaningless for all but that first segment
    # — so we plot the one figure the business rule actually applies to.
    top = sector_df.loc[sector_df.groupby("client_id")["pct_of_portfolio"].idxmax()].copy()
    top = top.sort_values("pct_of_portfolio", ascending=True)  # most concentrated client ends up on top

    # The categorical palette has 8 CVD-safe hues; with 10 possible sectors,
    # fold anything past the 7 largest (by total value) into "Other" rather
    # than wrapping the palette around and reusing a hue for two sectors.
    sector_totals = sector_df.groupby("sector")["sector_value"].sum().sort_values(ascending=False)
    top_sectors = list(sector_totals.index[:7])
    top["sector_grouped"] = top["sector"].where(top["sector"].isin(top_sectors), "Other")

    color_slots = top_sectors + (["Other"] if (top["sector_grouped"] == "Other").any() else [])
    color_map = {s: CATEGORICAL[i] for i, s in enumerate(color_slots)}
    bar_colors = top["sector_grouped"].map(color_map)
    edge_colors = [STATUS_CRITICAL if flag == 1 else "none" for flag in top["concentration_flag"]]

    fig, ax = plt.subplots(figsize=(10, 9))
    ax.barh(top.client_name, top.pct_of_portfolio, color=bar_colors, edgecolor=edge_colors,
             linewidth=2.2, height=0.65, zorder=3)
    ax.axvline(40, color=STATUS_CRITICAL, linestyle="--", linewidth=1.5, zorder=4)

    present_sectors = [s for s in color_slots if s in top["sector_grouped"].unique()]
    handles = [Patch(facecolor=color_map[s], label=s) for s in present_sectors]
    handles.append(Line2D([0], [0], color=STATUS_CRITICAL, linestyle="--", linewidth=1.5,
                           label="40% concentration threshold"))
    handles.append(Patch(facecolor="white", edgecolor=STATUS_CRITICAL, linewidth=2.2, label="Flagged (>40%)"))
    ax.legend(handles=handles, loc="lower right", frameon=False, fontsize=8, title="Largest sector")

    ax.set_title("Largest Single-Sector Exposure by Client — Concentration Risk",
                 fontsize=14, fontweight="bold", color=INK_PRIMARY, pad=14)
    ax.set_xlabel("% of portfolio in largest sector")
    ax.set_ylabel("Client")
    style_axes(ax, grid_axis="x")
    save_figure(fig, "sector_concentration.png")


def plot_performance_ranking(ranking_df: pd.DataFrame):
    d = ranking_df.sort_values("return_pct", ascending=True)  # ascending so rank #1 ends up on top
    color_map = {"Individual": CATEGORICAL[0], "Corporate": CATEGORICAL[1], "Institutional": CATEGORICAL[2]}
    colors = d.client_type.map(color_map)

    fig, ax = plt.subplots(figsize=(10, 9))
    ax.barh(d.client_name, d.return_pct, color=colors, height=0.65, zorder=3)
    ax.axvline(0, color=AXIS_COLOR, linewidth=1, zorder=2)

    handles = [Patch(facecolor=color_map[t], label=t) for t in color_map]
    ax.legend(handles=handles, title="Client type", loc="lower right", frameon=False, fontsize=9)

    ax.set_title("Client Performance Ranking — Portfolio Return Over the Period",
                 fontsize=14, fontweight="bold", color=INK_PRIMARY, pad=14)
    ax.set_xlabel("Portfolio return (%)")
    ax.set_ylabel("Client (ranked)")
    style_axes(ax, grid_axis="x")
    save_figure(fig, "performance_ranking.png")


def plot_query_optimization(summary_df: pd.DataFrame):
    labels = [q.split(" - ")[0] for q in summary_df["query"]]
    x = np.arange(len(labels))
    width = 0.35

    fig, ax = plt.subplots(figsize=(9, 6))
    ax.bar(x - width / 2, summary_df.median_before_s, width, color=CATEGORICAL[0],
           label="Before indexes", zorder=3)
    ax.bar(x + width / 2, summary_df.median_after_s, width, color=STATUS_GOOD,
           label="After indexes (optimized)", zorder=3)

    for xi, (b, a, g) in enumerate(zip(summary_df.median_before_s, summary_df.median_after_s, summary_df.gain_pct)):
        ax.text(xi, max(b, a) * 1.02, f"-{g:.0f}%", ha="center", color=INK_SECONDARY,
                fontsize=9, fontweight="bold")

    ax.set_xticks(x)
    ax.set_xticklabels(labels)
    ax.set_title("Query Execution Time — Before vs. After Adding Indexes",
                 fontsize=14, fontweight="bold", color=INK_PRIMARY, pad=14)
    ax.set_xlabel("Query")
    ax.set_ylabel("Median execution time, 7 runs (seconds)")
    ax.legend(frameon=False, fontsize=9)
    style_axes(ax, grid_axis="y")
    save_figure(fig, "query_execution_time_comparison.png")


def export_figures(dfs: dict, summary_df: pd.DataFrame):
    section("EXPORTING FIGURES (figures/*.png, 300 dpi)")
    plot_var_by_client(dfs["var_by_client"])
    plot_position_adjustment(dfs["positions_running_total"], dfs["positions_adjusted"], dfs["corporate_action_factors"])
    plot_sector_concentration(dfs["sector_concentration"])
    plot_performance_ranking(dfs["performance_ranking"])
    plot_query_optimization(summary_df)


# ----------------------------------------------------------------------------
# Part 4 - results/*.csv — one file per query result, for reuse outside the notebook
# ----------------------------------------------------------------------------

def export_csv_results(dfs: dict, summary_df: pd.DataFrame):
    section("EXPORTING RESULT TABLES (results/*.csv)")
    csv_map = {
        "positions_running_total.csv": dfs["positions_running_total"],
        "pnl_realized_unrealized.csv": dfs["pnl_realized_unrealized"],
        "var_by_client.csv": dfs["var_by_client"],
        "detected_anomalies.csv": dfs["detected_anomalies"],
        "corporate_action_factors.csv": dfs["corporate_action_factors"],
        "positions_adjusted.csv": dfs["positions_adjusted"],
        "position_reconciliation.csv": dfs["position_reconciliation"],
        "sector_concentration.csv": dfs["sector_concentration"],
        "performance_ranking.csv": dfs["performance_ranking"],
        "optimization_comparison.csv": summary_df,
    }
    for filename, df in csv_map.items():
        df.to_csv(RESULTS_DIR / filename, index=False)
        print(f"  -> results/{filename}  ({len(df):,} rows)")


def main():
    if not DB_PATH.exists():
        raise SystemExit(f"Database not found at {DB_PATH}. Run generate_data.py first.")

    FIGURES_DIR.mkdir(exist_ok=True)
    RESULTS_DIR.mkdir(exist_ok=True)

    conn = sqlite3.connect(DB_PATH)

    dfs = run_all_queries(conn)
    summary_df = run_optimization_section(conn)

    conn.close()

    export_figures(dfs, summary_df)
    export_csv_results(dfs, summary_df)

    print("\nDone. Indexes from schema.sql are now applied to data/risk_engine.db.")
    print(f"Figures written to: {FIGURES_DIR}")
    print(f"Result tables written to: {RESULTS_DIR}")


if __name__ == "__main__":
    main()
