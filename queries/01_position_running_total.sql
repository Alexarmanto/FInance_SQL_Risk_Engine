-- ============================================================================
-- Query 1 — Running-total position per client / instrument
-- ============================================================================
-- Technique demonstrated: window function running SUM().
--
-- For every trade, we want the client's cumulative holding in that
-- instrument immediately after the trade executes. This is a textbook
-- "running total" and is computed entirely with a window function — no
-- self-join, no correlated subquery, no loop:
--
--   SUM(signed_quantity) OVER (
--       PARTITION BY client_id, instrument_id  -- one running total per book
--       ORDER BY "timestamp"                   -- accumulated in trade order
--       ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
--   )
--
-- BUY trades add to the position, SELL trades subtract from it, so we first
-- turn (quantity, side) into a single signed quantity with a CASE
-- expression, then window-sum it.
--
-- Note: this is the RAW position from trades alone. It does not account for
-- corporate actions (splits) that change share counts without a trade being
-- recorded — that's exactly what queries 5 and 6 correct for.
-- ============================================================================

SELECT
    t.client_id,
    c.name                                                        AS client_name,
    t.instrument_id,
    i.ticker,
    t."timestamp"                                                AS trade_date,
    t.side,
    t.quantity,
    CASE WHEN t.side = 'BUY' THEN t.quantity ELSE -t.quantity END AS signed_quantity,
    SUM(CASE WHEN t.side = 'BUY' THEN t.quantity ELSE -t.quantity END)
        OVER (
            PARTITION BY t.client_id, t.instrument_id
            ORDER BY t."timestamp", t.id
            ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
        )                                                         AS running_position
FROM trades t
JOIN clients c     ON c.id = t.client_id
JOIN instruments i ON i.id = t.instrument_id
ORDER BY t.client_id, t.instrument_id, t."timestamp", t.id;
