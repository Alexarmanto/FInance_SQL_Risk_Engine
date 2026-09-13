-- ============================================================================
-- SQL-first Trade & Risk Analytics -- Database Schema (SQLite)
-- ============================================================================
-- This file is the single source of truth for the schema. It is split in two
-- parts on purpose:
--
--   1. TABLES        -- the baseline schema, created by generate_data.py
--                        with *no* secondary indexes (only the implicit
--                        indexes SQLite creates for PRIMARY KEY columns).
--                        This is the "before optimization" state used to
--                        measure query performance in analysis.py.
--
--   2. OPTIMIZED INDEXES -- added after profiling the heaviest analytical
--                        queries with EXPLAIN QUERY PLAN + wall-clock timing.
--                        analysis.py applies these statements live, so the
--                        before/after comparison in the "Optimization"
--                        section is reproducible end to end. See README.md
--                        for the measured results.
--
-- generate_data.py only ever executes the TABLES section when it builds the
-- database from scratch; the OPTIMIZED INDEXES section is applied later by
-- analysis.py as part of the optimization walkthrough.
-- ============================================================================


-- ============================================================================
-- 1. TABLES (baseline schema)
-- ============================================================================

-- Reference data: tradable instruments.
CREATE TABLE IF NOT EXISTS instruments (
    id            INTEGER PRIMARY KEY,
    ticker        TEXT    NOT NULL UNIQUE,
    name          TEXT    NOT NULL,
    sector        TEXT    NOT NULL,
    asset_class   TEXT    NOT NULL CHECK (asset_class IN ('Equity', 'Bond', 'ETF')),
    currency      TEXT    NOT NULL CHECK (currency IN ('EUR', 'USD', 'GBP', 'CHF'))
);

-- Reference data: clients.
CREATE TABLE IF NOT EXISTS clients (
    id              INTEGER PRIMARY KEY,
    name            TEXT    NOT NULL,
    client_type     TEXT    NOT NULL CHECK (client_type IN ('Individual', 'Corporate', 'Institutional')),
    opening_date    TEXT    NOT NULL      -- ISO date 'YYYY-MM-DD'
);

-- Fact table: every executed trade. This is the largest and most heavily
-- queried table in the project (tens of thousands of rows).
CREATE TABLE IF NOT EXISTS trades (
    id                INTEGER PRIMARY KEY,
    client_id         INTEGER NOT NULL REFERENCES clients(id),
    instrument_id     INTEGER NOT NULL REFERENCES instruments(id),
    "timestamp"       TEXT    NOT NULL,   -- ISO date 'YYYY-MM-DD' (one trading day granularity)
    quantity          REAL    NOT NULL CHECK (quantity > 0),
    price             REAL    NOT NULL CHECK (price > 0),
    side              TEXT    NOT NULL CHECK (side IN ('BUY', 'SELL'))
);

-- Fact table: daily close price & volume per instrument, 2+ years of history.
CREATE TABLE IF NOT EXISTS market_data (
    instrument_id  INTEGER NOT NULL REFERENCES instruments(id),
    "timestamp"    TEXT    NOT NULL,       -- ISO date 'YYYY-MM-DD'
    close_price    REAL    NOT NULL CHECK (close_price > 0),
    volume         INTEGER NOT NULL CHECK (volume >= 0),
    PRIMARY KEY (instrument_id, "timestamp")
);

-- Corporate actions (splits / dividends) affecting a subset of instruments.
-- `ratio` is interpreted conditionally on `action_type`:
--   SPLIT     -> multiplicative share-count factor (2.0 = 2-for-1 split,
--                0.5 = 1-for-2 reverse split). Price divides by this factor.
--   DIVIDEND  -> cash dividend per share, in the instrument's currency.
--                Does NOT change the share count (factor of 1.0).
CREATE TABLE IF NOT EXISTS corporate_actions (
    id             INTEGER PRIMARY KEY,
    instrument_id  INTEGER NOT NULL REFERENCES instruments(id),
    date           TEXT    NOT NULL,      -- ISO date 'YYYY-MM-DD'
    action_type    TEXT    NOT NULL CHECK (action_type IN ('SPLIT', 'DIVIDEND')),
    ratio          REAL    NOT NULL
);

-- Second "source of truth" simulating a custodian / back-office position
-- feed, used purely for the reconciliation query. A handful of rows are
-- deliberately perturbed at generation time (see generate_data.py).
CREATE TABLE IF NOT EXISTS declared_positions (
    client_id          INTEGER NOT NULL REFERENCES clients(id),
    instrument_id      INTEGER NOT NULL REFERENCES instruments(id),
    reference_date     TEXT    NOT NULL,   -- ISO date 'YYYY-MM-DD'
    declared_quantity  REAL    NOT NULL,
    PRIMARY KEY (client_id, instrument_id, reference_date)
);


-- ============================================================================
-- 2. OPTIMIZED INDEXES (applied by analysis.py during the optimization demo)
-- ============================================================================
-- Rationale for each index is documented in README.md ("Optimization"
-- section) together with the measured EXPLAIN QUERY PLAN and timing
-- before/after. Short version:
--
--   idx_trades_client_instrument_ts
--       Query 1 (running-total position) and Query 2 (P&L) both partition
--       trades BY (client_id, instrument_id) ORDER BY timestamp. Without an
--       index, SQLite performs a full table scan + temp B-tree sort for the
--       window function on every run. This composite index lets SQLite feed
--       the window function already-sorted per partition.
--
--   idx_trades_instrument_ts
--       Query 4 (z-score anomaly detection) partitions trades BY
--       instrument_id ORDER BY timestamp -- same problem, different
--       partition key.
--
--   idx_corporate_actions_instrument_date
--       Query 5 (recursive CTE) and Query 6 (adjusted positions) repeatedly
--       look up corporate actions for a given instrument in date order.
--
--   idx_market_data_instrument_ts
--       Redundant with the market_data PRIMARY KEY in theory, but declared
--       explicitly for clarity and to guarantee an index-only scan path in
--       the VaR query's calendar/price joins regardless of how SQLite
--       chooses to materialize the PK.
-- ============================================================================

CREATE INDEX IF NOT EXISTS idx_trades_client_instrument_ts
    ON trades (client_id, instrument_id, "timestamp");

CREATE INDEX IF NOT EXISTS idx_trades_instrument_ts
    ON trades (instrument_id, "timestamp");

CREATE INDEX IF NOT EXISTS idx_corporate_actions_instrument_date
    ON corporate_actions (instrument_id, date);

CREATE INDEX IF NOT EXISTS idx_market_data_instrument_ts
    ON market_data (instrument_id, "timestamp");
