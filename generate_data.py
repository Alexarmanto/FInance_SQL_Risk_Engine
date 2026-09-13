"""
generate_data.py
=================
Generates all synthetic data for the SQL-first Trade & Risk Analytics project
and materializes it into a fresh SQLite database (data/risk_engine.db).

IMPORTANT — scope of this file
-------------------------------
This script ONLY creates data. It does not compute positions, P&L, VaR,
z-scores, or any other analytical/business metric — that logic lives
exclusively in the SQL files under queries/, executed from analysis.py.
The only "SQL" run from here is:
  - the DDL (table creation) from schema.sql,
  - a single query to compute the *correct* reference position per
    (client, instrument) so that declared_positions can be seeded with a
    realistic baseline before a handful of rows are deliberately perturbed
    to simulate reconciliation breaks. That query reuses the exact same
    recursive-CTE adjustment logic as queries/05 and queries/06 — it is not
    a shortcut around them, it's how the "second source of truth" table is
    supposed to be built.

Design notes on realism
------------------------
- market_data follows a geometric Brownian motion per instrument with
  instrument-specific drift, and a Markov-switching volatility regime
  (calm / normal / turbulent) so realized volatility clusters in time
  instead of being flat noise.
- Corporate actions (SPLIT) are applied as a genuine multiplicative jump in
  the simulated price path on the action date, exactly as a real split
  would show up in a raw (unadjusted) price series.
- Trade generation walks the trading calendar day by day and maintains a
  running holdings dict per (client, instrument) so that SELL trades never
  exceed available holdings. Crucially, a SPLIT event multiplies existing
  holdings automatically (as it would in real life) WITHOUT inserting a
  trade row — this is precisely why the raw running-total position (query 1)
  and the corporate-action-adjusted position (query 6) diverge, which is the
  whole point of queries 5/6.
- Trading activity is deliberately non-uniform: monthly seasonality (e.g. a
  slow August), a mild day-of-week effect, and a handful of random "burst"
  weeks with elevated volume.
"""

import math
import random
import sqlite3
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

import numpy as np

sys.stdout.reconfigure(encoding="utf-8")

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------

RANDOM_SEED = 42

ROOT_DIR = Path(__file__).resolve().parent
SCHEMA_PATH = ROOT_DIR / "schema.sql"
DB_DIR = ROOT_DIR / "data"
DB_PATH = DB_DIR / "risk_engine.db"

START_DATE = date(2024, 8, 5)   # Monday
END_DATE = date(2026, 8, 7)     # ~2 years of history, ending "today"

NUM_INSTRUMENTS = 40
NUM_CLIENTS = 25
TARGET_TRADES = 42_000
NUM_CORPORATE_ACTIONS = 9  # 6 SPLIT (one instrument gets 2) + 3 DIVIDEND

SECTORS = [
    "Technology", "Healthcare", "Financials", "Energy",
    "Consumer Discretionary", "Consumer Staples", "Industrials",
    "Materials", "Utilities", "Telecommunications",
]

ASSET_CLASSES = ["Equity", "ETF", "Bond"]
ASSET_CLASS_WEIGHTS = [0.70, 0.15, 0.15]

CURRENCIES = ["EUR", "USD", "GBP", "CHF"]
CURRENCY_WEIGHTS = [0.50, 0.35, 0.10, 0.05]

CLIENT_TYPES = ["Individual", "Corporate", "Institutional"]
CLIENT_TYPE_WEIGHTS = [0.55, 0.25, 0.20]

NAME_PREFIXES = [
    "Nova", "Quantum", "Cyber", "Pixel", "Vertex", "Helio", "Meridian",
    "Atlas", "Aurora", "Zenith", "Orbital", "Granite", "Cobalt", "Amber",
    "Silverline", "Ironwood", "Northgate", "Bluecrest", "Redstone", "Solace",
    "Cedar", "Falcon", "Harbor", "Ridgeline", "Summit", "Delta", "Pioneer",
    "Wavefront", "Brightside", "Anchor",
]
NAME_SUFFIXES = [
    "Corp", "Inc", "Group", "Holdings", "SA", "PLC", "AG",
    "Technologies", "Industries", "Partners", "Capital", "Materials",
]

CLIENT_FIRST_NAMES = [
    "Alice", "Baptiste", "Chloe", "David", "Elena", "Farid", "Giulia",
    "Hugo", "Ines", "Julien", "Karim", "Laura", "Mathis", "Nora", "Oscar",
    "Paul", "Sarah", "Thomas", "Valentina", "Wei", "Yasmine", "Zoe",
    "Antoine", "Camille", "Diego",
]
CLIENT_LAST_NAMES = [
    "Martin", "Bernard", "Dubois", "Rossi", "Garcia", "Nguyen", "Muller",
    "Andersson", "Kowalski", "Silva", "Costa", "Weber", "Moreau", "Lefevre",
    "Fischer", "Rodriguez", "Bianchi", "Larsen", "Novak", "Haddad",
]
CORPORATE_NAME_WORDS = [
    "Meridian", "Atlas", "Northbridge", "Silverline", "Vantage", "Cobalt",
    "Harborview", "Ridgeline", "Anchor", "Brightside", "Cedar", "Falcon",
]
CORPORATE_SUFFIXES = ["Capital", "Asset Management", "Holdings", "Fund", "Partners", "Group"]

rng = random.Random(RANDOM_SEED)
np_rng = np.random.default_rng(RANDOM_SEED)


# ----------------------------------------------------------------------------
# Calendar helpers
# ----------------------------------------------------------------------------

def business_days(start: date, end: date) -> list[date]:
    days = []
    d = start
    while d <= end:
        if d.weekday() < 5:  # Mon-Fri
            days.append(d)
        d += timedelta(days=1)
    return days


TRADING_DAYS = business_days(START_DATE, END_DATE)
NUM_DAYS = len(TRADING_DAYS)
DATE_STR = [d.isoformat() for d in TRADING_DAYS]


# ----------------------------------------------------------------------------
# Weighted choice helper (pure python, no extra dependency)
# ----------------------------------------------------------------------------

def weighted_choice(rng_obj: random.Random, items, weights):
    return rng_obj.choices(items, weights=weights, k=1)[0]


# ----------------------------------------------------------------------------
# 1. Instruments
# ----------------------------------------------------------------------------

def generate_instruments():
    used_tickers = set()
    used_names = set()
    instruments = []
    for i in range(1, NUM_INSTRUMENTS + 1):
        sector = SECTORS[(i - 1) % len(SECTORS)]  # even spread across sectors
        asset_class = weighted_choice(rng, ASSET_CLASSES, ASSET_CLASS_WEIGHTS)
        currency = weighted_choice(rng, CURRENCIES, CURRENCY_WEIGHTS)

        while True:
            prefix = rng.choice(NAME_PREFIXES)
            suffix = rng.choice(NAME_SUFFIXES)
            name = f"{prefix} {suffix}"
            if name not in used_names:
                used_names.add(name)
                break

        while True:
            base = (prefix[:3] + suffix[:2]).upper()
            ticker = base + str(rng.randint(0, 9))
            if ticker not in used_tickers:
                used_tickers.add(ticker)
                break

        # Price / drift / vol parameters depend on asset class.
        if asset_class == "Equity":
            start_price = rng.uniform(15, 320)
            mu_annual = rng.uniform(-0.05, 0.20)
            sigma_annual = rng.uniform(0.18, 0.45)
        elif asset_class == "ETF":
            start_price = rng.uniform(25, 400)
            mu_annual = rng.uniform(0.00, 0.12)
            sigma_annual = rng.uniform(0.10, 0.22)
        else:  # Bond
            start_price = rng.uniform(85, 115)
            mu_annual = rng.uniform(-0.01, 0.04)
            sigma_annual = rng.uniform(0.02, 0.08)

        instruments.append({
            "id": i,
            "ticker": ticker,
            "name": name,
            "sector": sector,
            "asset_class": asset_class,
            "currency": currency,
            "start_price": start_price,
            "mu_annual": mu_annual,
            "sigma_annual": sigma_annual,
        })
    return instruments


# ----------------------------------------------------------------------------
# 2. Clients
# ----------------------------------------------------------------------------

def generate_clients():
    clients = []
    used_names = set()
    for i in range(1, NUM_CLIENTS + 1):
        client_type = weighted_choice(rng, CLIENT_TYPES, CLIENT_TYPE_WEIGHTS)
        if client_type == "Individual":
            while True:
                name = f"{rng.choice(CLIENT_FIRST_NAMES)} {rng.choice(CLIENT_LAST_NAMES)}"
                if name not in used_names:
                    used_names.add(name)
                    break
        else:
            while True:
                name = f"{rng.choice(CORPORATE_NAME_WORDS)} {rng.choice(CORPORATE_SUFFIXES)}"
                if name not in used_names:
                    used_names.add(name)
                    break

        # Account opening date: somewhere in the 3 years before the
        # simulation start, so every client is already "open" on day 1.
        days_before_start = rng.randint(30, 3 * 365)
        opening_date = (START_DATE - timedelta(days=days_before_start)).isoformat()

        # Trading profile: concentrated on one sector, or diversified.
        is_concentrated = rng.random() < 0.55
        home_sector = rng.choice(SECTORS) if is_concentrated else None

        # Activity weight: institutional clients trade a lot more.
        base_activity = {"Individual": 1.0, "Corporate": 2.5, "Institutional": 5.0}[client_type]
        activity_weight = base_activity * rng.lognormvariate(0, 0.4)

        clients.append({
            "id": i,
            "name": name,
            "client_type": client_type,
            "opening_date": opening_date,
            "is_concentrated": is_concentrated,
            "home_sector": home_sector,
            "activity_weight": activity_weight,
        })
    return clients


# ----------------------------------------------------------------------------
# 3. Corporate actions (dates picked before simulating prices, so the price
#    path can react to them)
# ----------------------------------------------------------------------------

def generate_corporate_actions(instruments):
    equity_ids = [ins["id"] for ins in instruments if ins["asset_class"] == "Equity"]
    rng.shuffle(equity_ids)

    actions = []
    action_id = 1

    # Pick distinct "safe window" dates: avoid the first/last 60 trading days
    # so there's adjustment history both before and after each event.
    safe_days = TRADING_DAYS[60:-60]

    def random_safe_date():
        return rng.choice(safe_days).isoformat()

    split_ratio_choices = [2.0, 2.0, 3.0, 1.5, 0.5]

    # 5 distinct instruments get exactly one SPLIT each.
    split_instruments = equity_ids[:5]
    for inst_id in split_instruments:
        actions.append({
            "id": action_id, "instrument_id": inst_id, "date": random_safe_date(),
            "action_type": "SPLIT", "ratio": rng.choice(split_ratio_choices),
        })
        action_id += 1

    # One of those instruments gets a SECOND split later, to demonstrate the
    # recursive CTE composing two successive factors.
    double_split_instrument = split_instruments[0]
    first_split_date = next(a["date"] for a in actions if a["instrument_id"] == double_split_instrument)
    later_days = [d for d in safe_days if d.isoformat() > first_split_date]
    if later_days:
        actions.append({
            "id": action_id, "instrument_id": double_split_instrument,
            "date": rng.choice(later_days).isoformat(),
            "action_type": "SPLIT", "ratio": rng.choice(split_ratio_choices),
        })
        action_id += 1

    # 3 distinct instruments (not already used for splits) get a DIVIDEND.
    dividend_instruments = equity_ids[5:8]
    for inst_id in dividend_instruments:
        actions.append({
            "id": action_id, "instrument_id": inst_id, "date": random_safe_date(),
            "action_type": "DIVIDEND", "ratio": round(rng.uniform(0.3, 3.5), 2),
        })
        action_id += 1

    actions.sort(key=lambda a: (a["instrument_id"], a["date"]))
    return actions


# ----------------------------------------------------------------------------
# 4. Market data: GBM with regime-switching volatility + split jumps
# ----------------------------------------------------------------------------

VOL_REGIMES = [0.6, 1.0, 2.0]  # calm / normal / turbulent multipliers
REGIME_PERSISTENCE = 0.97      # probability of staying in the same regime day to day


def simulate_regime_path(n_days):
    regime = 1  # start "normal"
    path = np.empty(n_days)
    for t in range(n_days):
        if rng.random() > REGIME_PERSISTENCE:
            regime = rng.randrange(len(VOL_REGIMES))
        path[t] = VOL_REGIMES[regime]
    return path


def generate_market_data(instruments, corporate_actions):
    splits_by_instrument = defaultdict(list)
    for ca in corporate_actions:
        if ca["action_type"] == "SPLIT":
            splits_by_instrument[ca["instrument_id"]].append((ca["date"], ca["ratio"]))

    rows = []
    price_paths = {}  # instrument_id -> np.array of close prices, aligned to TRADING_DAYS

    for ins in instruments:
        mu_daily = ins["mu_annual"] / 252
        sigma_daily = ins["sigma_annual"] / math.sqrt(252)
        vol_regime = simulate_regime_path(NUM_DAYS)
        z = np_rng.standard_normal(NUM_DAYS)

        split_dates = dict(splits_by_instrument.get(ins["id"], []))

        prices = np.empty(NUM_DAYS)
        price = ins["start_price"]
        base_volume = {"Equity": 50_000, "ETF": 120_000, "Bond": 8_000}[ins["asset_class"]]

        for t in range(NUM_DAYS):
            sigma_t = sigma_daily * vol_regime[t]
            ret = (mu_daily - 0.5 * sigma_t ** 2) + sigma_t * z[t]
            price = price * math.exp(ret)

            today_str = DATE_STR[t]
            if today_str in split_dates:
                price = price / split_dates[today_str]

            prices[t] = price

        price_paths[ins["id"]] = prices

        # Volume: base level scaled by asset class, bumped on high-|return| days.
        daily_returns = np.diff(prices, prepend=prices[0]) / np.concatenate(([prices[0]], prices[:-1]))
        vol_noise = np_rng.lognormal(mean=0.0, sigma=0.35, size=NUM_DAYS)
        volumes = (base_volume * (1 + 4 * np.abs(daily_returns)) * vol_noise).astype(int)

        for t in range(NUM_DAYS):
            rows.append((ins["id"], DATE_STR[t], round(float(prices[t]), 4), int(volumes[t])))

    return rows, price_paths


# ----------------------------------------------------------------------------
# 5. Trades: sequential day-by-day walk with realistic non-uniform activity
# ----------------------------------------------------------------------------

MONTH_SEASONALITY = {
    1: 1.30, 2: 1.10, 3: 1.00, 4: 0.95, 5: 0.90, 6: 0.85,
    7: 0.60, 8: 0.55, 9: 1.10, 10: 1.15, 11: 1.05, 12: 0.90,
}
WEEKDAY_MULT = {0: 0.90, 1: 1.05, 2: 1.05, 3: 1.05, 4: 0.95}  # Mon..Fri


def build_day_weights():
    weights = np.empty(NUM_DAYS)
    for t, d in enumerate(TRADING_DAYS):
        weights[t] = MONTH_SEASONALITY[d.month] * WEEKDAY_MULT[d.weekday()]

    # A handful of random "burst" weeks (earnings season / market stress)
    # with elevated activity.
    num_bursts = 10
    for _ in range(num_bursts):
        start_idx = rng.randrange(0, NUM_DAYS - 10)
        length = rng.randint(4, 9)
        multiplier = rng.uniform(2.0, 4.0)
        weights[start_idx:start_idx + length] *= multiplier

    weights = weights / weights.sum()
    return weights


def allocate_trades_per_day(total_trades):
    weights = build_day_weights()
    raw = weights * total_trades
    counts = np.floor(raw).astype(int)
    remainder = total_trades - counts.sum()
    # Distribute the rounding remainder to the highest-weight days.
    top_idx = np.argsort(-raw)[:remainder]
    counts[top_idx] += 1
    return counts


def sample_lot_size(client_type: str) -> int:
    base_mean = {"Individual": 20, "Corporate": 100, "Institutional": 350}[client_type]
    qty = rng.lognormvariate(math.log(base_mean), 0.7)
    return max(1, min(5000, round(qty)))


def build_client_instrument_weights(clients, instruments):
    """Per client, a weight vector over instruments driven by its trading
    profile (concentrated on a home sector, or roughly diversified)."""
    weights_by_client = {}
    for c in clients:
        w = np.ones(len(instruments))
        for idx, ins in enumerate(instruments):
            if c["is_concentrated"] and ins["sector"] == c["home_sector"]:
                w[idx] = 8.0
            else:
                w[idx] = 1.0
        w = w / w.sum()
        weights_by_client[c["id"]] = w
    return weights_by_client


def generate_trades(clients, instruments, price_paths, corporate_actions):
    instrument_ids = [ins["id"] for ins in instruments]
    client_by_id = {c["id"]: c for c in clients}

    client_ids = [c["id"] for c in clients]
    client_weights = [c["activity_weight"] for c in clients]

    ci_weights = build_client_instrument_weights(clients, instruments)

    splits_by_date = defaultdict(list)  # date_str -> [(instrument_id, ratio)]
    for ca in corporate_actions:
        if ca["action_type"] == "SPLIT":
            splits_by_date[ca["date"]].append((ca["instrument_id"], ca["ratio"]))

    trades_per_day = allocate_trades_per_day(TARGET_TRADES)

    holdings = defaultdict(float)  # (client_id, instrument_id) -> economic qty held (auto-adjusted by splits)
    rows = []
    trade_id = 1

    for t, day_date in enumerate(TRADING_DAYS):
        today_str = DATE_STR[t]

        # Apply any split happening today: real holdings jump automatically,
        # with no corresponding trade row (this is what makes the raw
        # running-total position diverge from the true economic position).
        for inst_id, ratio in splits_by_date.get(today_str, []):
            for (c_id, i_id) in list(holdings.keys()):
                if i_id == inst_id and holdings[(c_id, i_id)] > 0:
                    holdings[(c_id, i_id)] *= ratio

        n_today = int(trades_per_day[t])
        if n_today == 0:
            continue

        today_clients = rng.choices(client_ids, weights=client_weights, k=n_today)

        for c_id in today_clients:
            client = client_by_id[c_id]
            inst_idx = np_rng.choice(len(instrument_ids), p=ci_weights[c_id])
            inst_id = instrument_ids[inst_idx]

            price_today = float(price_paths[inst_id][t])
            key = (c_id, inst_id)
            cur_qty = holdings[key]

            if cur_qty > 0 and rng.random() < 0.35:
                side = "SELL"
                qty = min(cur_qty, sample_lot_size(client["client_type"]))
                qty = max(1, round(qty))
                qty = min(qty, cur_qty)
            else:
                side = "BUY"
                qty = sample_lot_size(client["client_type"])

            exec_price = price_today * (1 + np_rng.normal(0, 0.003))
            exec_price = max(0.01, exec_price)

            rows.append((trade_id, c_id, inst_id, today_str, float(qty), round(float(exec_price), 4), side))
            trade_id += 1

            holdings[key] = cur_qty + qty if side == "BUY" else cur_qty - qty

    return rows


# ----------------------------------------------------------------------------
# 6. declared_positions: second source of truth with injected discrepancies
# ----------------------------------------------------------------------------

# The recursive CTE + adjustment join used here is identical in spirit to
# queries/05_corporate_actions_recursive_factor.sql and
# queries/06_position_adjusted_corporate_actions.sql. It is executed once, in
# SQL, purely to seed a realistic baseline for the "declared positions" table
# before a handful of rows are deliberately corrupted below.
REFERENCE_POSITION_SQL = """
WITH RECURSIVE ca_ordered AS (
    SELECT
        instrument_id,
        date,
        CASE WHEN action_type = 'SPLIT' THEN ratio ELSE 1.0 END AS split_ratio,
        ROW_NUMBER() OVER (PARTITION BY instrument_id ORDER BY date) AS rn
    FROM corporate_actions
),
cumulative_factor AS (
    SELECT instrument_id, date, split_ratio, rn, split_ratio AS cum_factor
    FROM ca_ordered
    WHERE rn = 1

    UNION ALL

    SELECT o.instrument_id, o.date, o.split_ratio, o.rn, c.cum_factor * o.split_ratio
    FROM ca_ordered o
    JOIN cumulative_factor c
      ON o.instrument_id = c.instrument_id AND o.rn = c.rn + 1
),
factor_total AS (
    -- Final cumulative factor per instrument = product of ALL its corporate actions.
    SELECT cf.instrument_id, cf.cum_factor AS cum_factor_total
    FROM cumulative_factor cf
    JOIN (SELECT instrument_id, MAX(rn) AS rn_max FROM ca_ordered GROUP BY instrument_id) last_evt
      ON last_evt.instrument_id = cf.instrument_id AND last_evt.rn_max = cf.rn
),
trade_adjustment AS (
    -- adjustment_factor(t) = total cumulative factor / factor already
    -- "baked in" as of the trade date (see queries/06 for the full
    -- explanation of why this correctly composes successive splits).
    SELECT
        t.id AS trade_id,
        t.client_id,
        t.instrument_id,
        t.quantity,
        t.side,
        COALESCE(ft.cum_factor_total, 1.0) / COALESCE(
            (SELECT cf2.cum_factor
             FROM cumulative_factor cf2
             WHERE cf2.instrument_id = t.instrument_id AND cf2.date <= t."timestamp"
             ORDER BY cf2.date DESC
             LIMIT 1),
            1.0
        ) AS adjustment_factor
    FROM trades t
    LEFT JOIN factor_total ft ON ft.instrument_id = t.instrument_id
),
adjusted_positions AS (
    SELECT
        client_id,
        instrument_id,
        SUM(CASE WHEN side = 'BUY' THEN quantity ELSE -quantity END * adjustment_factor) AS adjusted_position
    FROM trade_adjustment
    GROUP BY client_id, instrument_id
)
SELECT client_id, instrument_id, adjusted_position
FROM adjusted_positions
WHERE adjusted_position > 0.0001
"""


def generate_declared_positions(conn, clients, instruments):
    cur = conn.cursor()
    cur.execute(REFERENCE_POSITION_SQL)
    true_positions = cur.fetchall()  # [(client_id, instrument_id, qty), ...]

    reference_date = TRADING_DAYS[-1].isoformat()

    perturb_rng = random.Random(RANDOM_SEED + 1)
    n_rows = len(true_positions)
    idx_pool = list(range(n_rows))
    perturb_rng.shuffle(idx_pool)

    n_mismatch = min(6, n_rows // 20 + 1)
    n_missing = min(4, n_rows // 25 + 1)
    mismatch_idx = set(idx_pool[:n_mismatch])
    missing_idx = set(idx_pool[n_mismatch:n_mismatch + n_missing])

    rows = []
    for i, (client_id, instrument_id, qty) in enumerate(true_positions):
        if i in missing_idx:
            continue  # simulate a position missing at the custodian
        if i in mismatch_idx:
            factor = perturb_rng.choice([0.85, 0.90, 1.10, 1.15, 0.5])
            qty = round(qty * factor, 2)
        else:
            qty = round(qty, 2)
        rows.append((client_id, instrument_id, reference_date, qty))

    # Phantom positions: a handful of (client, instrument) pairs the client
    # never actually traded, but that appear at the custodian.
    real_pairs = {(c, i) for c, i, _ in true_positions}
    client_ids = [c["id"] for c in clients]
    instrument_ids = [ins["id"] for ins in instruments]
    phantoms_added = 0
    while phantoms_added < 4:
        c_id = perturb_rng.choice(client_ids)
        i_id = perturb_rng.choice(instrument_ids)
        if (c_id, i_id) in real_pairs:
            continue
        rows.append((c_id, i_id, reference_date, round(perturb_rng.uniform(5, 200), 2)))
        real_pairs.add((c_id, i_id))
        phantoms_added += 1

    cur.executemany(
        "INSERT INTO declared_positions (client_id, instrument_id, reference_date, declared_quantity) "
        "VALUES (?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    return len(rows), n_mismatch, n_missing, phantoms_added


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

def main():
    DB_DIR.mkdir(exist_ok=True)
    if DB_PATH.exists():
        DB_PATH.unlink()

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = OFF")  # bulk-load first, integrity guaranteed by generator logic

    schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")
    tables_only_sql = schema_sql.split("-- 2. OPTIMIZED INDEXES")[0]
    conn.executescript(tables_only_sql)

    print(f"Trading calendar: {TRADING_DAYS[0]} -> {TRADING_DAYS[-1]}  ({NUM_DAYS} business days)")

    print("Generating instruments...")
    instruments = generate_instruments()
    conn.executemany(
        "INSERT INTO instruments (id, ticker, name, sector, asset_class, currency) VALUES (?, ?, ?, ?, ?, ?)",
        [(i["id"], i["ticker"], i["name"], i["sector"], i["asset_class"], i["currency"]) for i in instruments],
    )

    print("Generating clients...")
    clients = generate_clients()
    conn.executemany(
        "INSERT INTO clients (id, name, client_type, opening_date) VALUES (?, ?, ?, ?)",
        [(c["id"], c["name"], c["client_type"], c["opening_date"]) for c in clients],
    )
    conn.commit()

    print("Generating corporate actions...")
    corporate_actions = generate_corporate_actions(instruments)
    conn.executemany(
        "INSERT INTO corporate_actions (id, instrument_id, date, action_type, ratio) VALUES (?, ?, ?, ?, ?)",
        [(a["id"], a["instrument_id"], a["date"], a["action_type"], a["ratio"]) for a in corporate_actions],
    )
    conn.commit()

    print("Simulating market data (GBM + regime-switching volatility + split jumps)...")
    market_rows, price_paths = generate_market_data(instruments, corporate_actions)
    conn.executemany(
        "INSERT INTO market_data (instrument_id, \"timestamp\", close_price, volume) VALUES (?, ?, ?, ?)",
        market_rows,
    )
    conn.commit()
    print(f"  -> {len(market_rows):,} market_data rows")

    print("Generating trades (sequential day-by-day walk with holdings tracking)...")
    trade_rows = generate_trades(clients, instruments, price_paths, corporate_actions)
    conn.executemany(
        "INSERT INTO trades (id, client_id, instrument_id, \"timestamp\", quantity, price, side) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        trade_rows,
    )
    conn.commit()
    print(f"  -> {len(trade_rows):,} trade rows")

    print("Seeding declared_positions with a handful of deliberate discrepancies...")
    n_rows, n_mismatch, n_missing, n_phantom = generate_declared_positions(conn, clients, instruments)
    print(f"  -> {n_rows} declared positions ({n_mismatch} mismatched, {n_missing} missing, {n_phantom} phantom)")

    conn.close()
    print(f"\nDatabase created at: {DB_PATH}")
    print("Note: secondary indexes are NOT yet created - see schema.sql section 2 "
          "and analysis.py's optimization section for the before/after demonstration.")


if __name__ == "__main__":
    main()
