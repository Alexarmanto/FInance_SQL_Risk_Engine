-- ============================================================================
-- Query 8 — Sector exposure and concentration flagging
-- ============================================================================
-- Technique demonstrated: multi-level CTE aggregation (per client+sector,
-- then per client) to compute a share-of-total percentage without ever
-- materializing an intermediate result in Python — the "total portfolio
-- value" denominator is itself just another grouped SUM() joined back in.
--
-- Positions are valued at the latest available close price and use the
-- corporate-action-adjusted quantity (same recursive-CTE logic as query 6),
-- since a stale, unadjusted quantity would misstate exposure for any client
-- holding a stock that has since split.
--
-- A client is flagged as "concentrated" when a single sector exceeds 40% of
-- their total portfolio value — the classic single-name/single-sector
-- concentration risk check.
-- ============================================================================

WITH RECURSIVE ca_ordered AS (
    SELECT
        instrument_id, date,
        CASE WHEN action_type = 'SPLIT' THEN ratio ELSE 1.0 END AS event_factor,
        ROW_NUMBER() OVER (PARTITION BY instrument_id ORDER BY date) AS rn
    FROM corporate_actions
),
cumulative_factor AS (
    SELECT instrument_id, date, event_factor, rn, event_factor AS cumulative_factor
    FROM ca_ordered WHERE rn = 1
    UNION ALL
    SELECT o.instrument_id, o.date, o.event_factor, o.rn, c.cumulative_factor * o.event_factor
    FROM ca_ordered o
    JOIN cumulative_factor c ON o.instrument_id = c.instrument_id AND o.rn = c.rn + 1
),
factor_total AS (
    SELECT cf.instrument_id, cf.cumulative_factor AS total_factor
    FROM cumulative_factor cf
    JOIN (SELECT instrument_id, MAX(rn) AS rn_max FROM ca_ordered GROUP BY instrument_id) last_evt
      ON last_evt.instrument_id = cf.instrument_id AND last_evt.rn_max = cf.rn
),
calculated_position AS (
    SELECT
        t.client_id,
        t.instrument_id,
        SUM(
            (CASE WHEN t.side = 'BUY' THEN t.quantity ELSE -t.quantity END)
            * COALESCE(ft.total_factor, 1.0) / COALESCE(
                (SELECT cf2.cumulative_factor FROM cumulative_factor cf2
                 WHERE cf2.instrument_id = t.instrument_id AND cf2.date <= t."timestamp"
                 ORDER BY cf2.date DESC LIMIT 1),
                1.0
            )
        ) AS calculated_position
    FROM trades t
    LEFT JOIN factor_total ft ON ft.instrument_id = t.instrument_id
    GROUP BY t.client_id, t.instrument_id
    HAVING calculated_position > 0.0001   -- only long, still-open positions count towards exposure
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
valued_position AS (
    SELECT
        cp.client_id,
        i.sector,
        cp.calculated_position * cmv.close_price AS position_value
    FROM calculated_position cp
    JOIN instruments i           ON i.id = cp.instrument_id
    JOIN current_market_value cmv ON cmv.instrument_id = cp.instrument_id
),
sector_exposure AS (
    SELECT client_id, sector, SUM(position_value) AS sector_value
    FROM valued_position
    GROUP BY client_id, sector
),
client_totals AS (
    SELECT client_id, SUM(sector_value) AS total_portfolio_value
    FROM sector_exposure
    GROUP BY client_id
)

SELECT
    se.client_id,
    c.name                                                      AS client_name,
    c.client_type,
    se.sector,
    ROUND(se.sector_value, 2)                                   AS sector_value,
    ROUND(ct.total_portfolio_value, 2)                          AS total_portfolio_value,
    ROUND(100.0 * se.sector_value / ct.total_portfolio_value, 2) AS pct_of_portfolio,
    CASE WHEN se.sector_value / ct.total_portfolio_value > 0.40
         THEN 1 ELSE 0 END                                      AS concentration_flag
FROM sector_exposure se
JOIN client_totals ct ON ct.client_id = se.client_id
JOIN clients c         ON c.id = se.client_id
ORDER BY se.client_id, pct_of_portfolio DESC;
