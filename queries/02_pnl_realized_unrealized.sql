-- ============================================================================
-- Query 2 — Realized & unrealized P&L, fully decomposed into CTEs
-- ============================================================================
-- Technique demonstrated: chained CTEs, each responsible for one step of the
-- classic "moving average cost" P&L method, plus window functions
-- (running SUM, ROW_NUMBER) to avoid any procedural/row-by-row logic.
--
-- Method (moving average cost, the standard used by most custodians):
--   - Every BUY updates the average acquisition cost of the position.
--   - A SELL does NOT change the average cost of the remaining shares; it
--     realizes P&L = (sell_price - avg_cost_at_the_time) * quantity_sold.
--   - The position still open today carries a *latent* (unrealized) P&L =
--     (current_market_price - avg_cost) * open_quantity.
--
-- Pipeline:
--   1. trade_costing      -> running buy-quantity / buy-cost per trade
--   2. average_cost        -> average acquisition cost at each trade (CTE #1)
--   3. realized_pnl_*      -> realized P&L per SELL trade, aggregated
--   4. last_position       -> last known open position & avg cost (CTE #2, #3)
--   5. current_market_value -> latest market price per instrument (CTE #2)
--   6. unrealized_pnl       -> unrealized P&L on the still-open position (CTE #3)
--   7. final SELECT        -> realized + unrealized P&L per (client, instrument)
-- ============================================================================

WITH trade_costing AS (
    SELECT
        t.id,
        t.client_id,
        t.instrument_id,
        t."timestamp",
        t.side,
        t.quantity,
        t.price,
        -- Running totals restricted to BUY trades: SELL rows contribute 0,
        -- so the average cost only ever moves on a BUY, which is exactly
        -- the moving-average-cost convention.
        SUM(CASE WHEN t.side = 'BUY' THEN t.quantity ELSE 0 END)
            OVER (PARTITION BY t.client_id, t.instrument_id
                  ORDER BY t."timestamp", t.id
                  ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)         AS cum_buy_qty,
        SUM(CASE WHEN t.side = 'BUY' THEN t.quantity * t.price ELSE 0 END)
            OVER (PARTITION BY t.client_id, t.instrument_id
                  ORDER BY t."timestamp", t.id
                  ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)         AS cum_buy_cost,
        -- Running net position after this trade (same logic as query 1).
        SUM(CASE WHEN t.side = 'BUY' THEN t.quantity ELSE -t.quantity END)
            OVER (PARTITION BY t.client_id, t.instrument_id
                  ORDER BY t."timestamp", t.id
                  ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)         AS position_after_trade
    FROM trades t
),

average_cost AS (
    -- CTE 1: average acquisition cost of the position at each point in time.
    SELECT
        tc.*,
        tc.cum_buy_cost / NULLIF(tc.cum_buy_qty, 0) AS avg_acquisition_cost
    FROM trade_costing tc
),

realized_pnl_per_trade AS (
    -- Realized P&L crystallizes on every SELL trade.
    SELECT
        client_id,
        instrument_id,
        CASE WHEN side = 'SELL'
             THEN (price - avg_acquisition_cost) * quantity
             ELSE 0
        END AS realized_pnl
    FROM average_cost
),

realized_pnl_total AS (
    SELECT client_id, instrument_id, SUM(realized_pnl) AS realized_pnl_total
    FROM realized_pnl_per_trade
    GROUP BY client_id, instrument_id
),

last_position AS (
    -- CTE 2: the most recent trade per (client, instrument) tells us the
    -- currently open quantity and the average cost carried on it.
    SELECT client_id, instrument_id, position_after_trade AS open_position, avg_acquisition_cost
    FROM (
        SELECT
            ac.*,
            ROW_NUMBER() OVER (PARTITION BY client_id, instrument_id
                                ORDER BY "timestamp" DESC, id DESC) AS rn
        FROM average_cost ac
    )
    WHERE rn = 1
),

current_market_value AS (
    -- CTE 3: latest available close price per instrument.
    SELECT instrument_id, close_price AS current_price
    FROM (
        SELECT
            instrument_id,
            close_price,
            ROW_NUMBER() OVER (PARTITION BY instrument_id ORDER BY "timestamp" DESC) AS rn
        FROM market_data
    )
    WHERE rn = 1
),

unrealized_pnl AS (
    SELECT
        lp.client_id,
        lp.instrument_id,
        lp.open_position,
        lp.avg_acquisition_cost,
        cmv.current_price,
        (cmv.current_price - lp.avg_acquisition_cost) * lp.open_position AS unrealized_pnl
    FROM last_position lp
    JOIN current_market_value cmv ON cmv.instrument_id = lp.instrument_id
    WHERE lp.open_position > 0.0001   -- only positions still open
),

all_pairs AS (
    SELECT DISTINCT client_id, instrument_id FROM trades
)

SELECT
    ap.client_id,
    c.name                                        AS client_name,
    ap.instrument_id,
    i.ticker,
    ROUND(COALESCE(up.open_position, 0), 2)       AS open_position,
    ROUND(up.avg_acquisition_cost, 4)             AS avg_acquisition_cost,
    ROUND(up.current_price, 4)                    AS current_price,
    ROUND(COALESCE(up.unrealized_pnl, 0), 2)      AS unrealized_pnl,
    ROUND(COALESCE(rpt.realized_pnl_total, 0), 2) AS realized_pnl,
    ROUND(COALESCE(up.unrealized_pnl, 0) + COALESCE(rpt.realized_pnl_total, 0), 2) AS total_pnl
FROM all_pairs ap
JOIN clients c            ON c.id = ap.client_id
JOIN instruments i        ON i.id = ap.instrument_id
LEFT JOIN unrealized_pnl up        ON up.client_id = ap.client_id AND up.instrument_id = ap.instrument_id
LEFT JOIN realized_pnl_total rpt   ON rpt.client_id = ap.client_id AND rpt.instrument_id = ap.instrument_id
ORDER BY total_pnl DESC;
