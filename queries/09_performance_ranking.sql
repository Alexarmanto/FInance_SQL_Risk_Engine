-- ============================================================================
-- Query 9 — Client performance ranking
-- ============================================================================
-- Technique demonstrated: RANK() and DENSE_RANK() window functions to rank
-- clients by portfolio return over the full period, computed as total P&L
-- (realized + unrealized, same moving-average-cost method as query 2)
-- divided by total capital deployed (gross sum of BUY trade amounts).
--
-- RANK() leaves gaps after ties (1, 2, 2, 4, ...) — appropriate for a
-- "your rank among N clients" leaderboard. DENSE_RANK() does not (1, 2, 2,
-- 3, ...) — appropriate for "how many distinct performance tiers are above
-- you". Both are shown side by side to make the difference visible.
-- ============================================================================

WITH trade_costing AS (
    SELECT
        t.client_id,
        t.instrument_id,
        t."timestamp",
        t.id,
        t.side,
        t.quantity,
        t.price,
        SUM(CASE WHEN t.side = 'BUY' THEN t.quantity ELSE 0 END)
            OVER (PARTITION BY t.client_id, t.instrument_id
                  ORDER BY t."timestamp", t.id
                  ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)         AS cum_buy_qty,
        SUM(CASE WHEN t.side = 'BUY' THEN t.quantity * t.price ELSE 0 END)
            OVER (PARTITION BY t.client_id, t.instrument_id
                  ORDER BY t."timestamp", t.id
                  ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)         AS cum_buy_cost,
        SUM(CASE WHEN t.side = 'BUY' THEN t.quantity ELSE -t.quantity END)
            OVER (PARTITION BY t.client_id, t.instrument_id
                  ORDER BY t."timestamp", t.id
                  ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)         AS position_after_trade
    FROM trades t
),
average_cost AS (
    SELECT *, cum_buy_cost / NULLIF(cum_buy_qty, 0) AS avg_acquisition_cost
    FROM trade_costing
),
realized_pnl_by_client AS (
    SELECT
        client_id,
        SUM(CASE WHEN side = 'SELL' THEN (price - avg_acquisition_cost) * quantity ELSE 0 END) AS realized_pnl
    FROM average_cost
    GROUP BY client_id
),
last_position AS (
    SELECT client_id, instrument_id, position_after_trade AS open_position, avg_acquisition_cost
    FROM (
        SELECT ac.*,
               ROW_NUMBER() OVER (PARTITION BY client_id, instrument_id
                                   ORDER BY "timestamp" DESC, id DESC) AS rn
        FROM average_cost ac
    )
    WHERE rn = 1
),
current_market_value AS (
    SELECT instrument_id, close_price
    FROM (
        SELECT instrument_id, close_price,
               ROW_NUMBER() OVER (PARTITION BY instrument_id ORDER BY "timestamp" DESC) AS rn
        FROM market_data
    )
    WHERE rn = 1
),
unrealized_pnl_by_client AS (
    SELECT
        lp.client_id,
        SUM((cmv.close_price - lp.avg_acquisition_cost) * lp.open_position) AS unrealized_pnl
    FROM last_position lp
    JOIN current_market_value cmv ON cmv.instrument_id = lp.instrument_id
    WHERE lp.open_position > 0.0001
    GROUP BY lp.client_id
),
capital_deployed AS (
    SELECT client_id, SUM(quantity * price) AS total_invested
    FROM trades
    WHERE side = 'BUY'
    GROUP BY client_id
),
client_performance AS (
    SELECT
        cd.client_id,
        cd.total_invested,
        COALESCE(rp.realized_pnl, 0) + COALESCE(up.unrealized_pnl, 0) AS total_pnl,
        (COALESCE(rp.realized_pnl, 0) + COALESCE(up.unrealized_pnl, 0)) / cd.total_invested AS portfolio_return
    FROM capital_deployed cd
    LEFT JOIN realized_pnl_by_client rp    ON rp.client_id = cd.client_id
    LEFT JOIN unrealized_pnl_by_client up  ON up.client_id = cd.client_id
)

SELECT
    p.client_id,
    c.name              AS client_name,
    c.client_type,
    ROUND(p.total_invested, 2)          AS total_invested,
    ROUND(p.total_pnl, 2)               AS total_pnl,
    ROUND(p.portfolio_return * 100, 3)  AS return_pct,
    RANK()       OVER (ORDER BY p.portfolio_return DESC) AS rank,
    DENSE_RANK() OVER (ORDER BY p.portfolio_return DESC) AS dense_rank
FROM client_performance p
JOIN clients c ON c.id = p.client_id
ORDER BY rank;
