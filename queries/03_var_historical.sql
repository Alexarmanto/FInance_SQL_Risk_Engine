-- ============================================================================
-- Query 3 — Historical VaR (95%) per client, trailing 250-trading-day window
-- ============================================================================
-- Technique demonstrated: event-time "as-of" running SUM to forward-fill a
-- position onto a daily calendar (no correlated subquery), LAG() for daily
-- returns, and a manual percentile via ROW_NUMBER()/COUNT() window functions.
--
-- SQLite has no PERCENTILE_CONT / PERCENTILE_DISC (confirmed: SQLite's SQL
-- dialect does not implement the standard "ordered-set aggregate function"
-- syntax `agg(...) WITHIN GROUP (ORDER BY ...)`). We approximate the 5th
-- percentile of the daily-return distribution — the historical-simulation
-- VaR at a 95% confidence level — by sorting returns with ROW_NUMBER() and
-- picking the row whose rank matches floor(0.05 * N) + 1. This is a
-- standard, well-documented approximation of PERCENTILE_CONT/DISC and is
-- deterministic and index-friendly, unlike a client-side pandas percentile.
--
-- Building the daily portfolio value requires a position *as of any given
-- calendar day*, not just as of a trade. We get this without a correlated
-- subquery by unioning real trades (signed quantity deltas) with one
-- "marker" row per (client, instrument, calendar day), then taking a single
-- running SUM() OVER (... ORDER BY date, is_marker) per partition: trades
-- dated on a given day sort before that day's marker, so the marker row
-- always reads the correct end-of-day position. This is the classic
-- SQL "as-of join via running total" pattern.
--
-- Scope: computed as of the last date in the dataset, using its trailing
-- 250-trading-day window (documented VaR convention: RiskMetrics-style
-- historical simulation). Producing a full day-by-day VaR *time series*
-- would multiply the cost by ~500 dates for no analytical benefit here;
-- removing the "last 250 dates" filter in `calendar_window` turns this into
-- a full-history version if ever needed.
-- ============================================================================

WITH calendar_window AS (
    -- The 250 most recent trading days present in market_data.
    SELECT DISTINCT "timestamp" AS calendar_date
    FROM market_data
    ORDER BY calendar_date DESC
    LIMIT 250
),

client_instrument_pairs AS (
    SELECT DISTINCT client_id, instrument_id FROM trades
),

events AS (
    -- Real trades: signed quantity delta on their execution date.
    SELECT
        client_id, instrument_id, "timestamp" AS event_date,
        CASE WHEN side = 'BUY' THEN quantity ELSE -quantity END AS delta_qty,
        0 AS is_marker
    FROM trades

    UNION ALL

    -- One zero-delta "marker" per (client, instrument, calendar day) in the
    -- VaR window: this is where we'll read the as-of position back out.
    SELECT
        p.client_id, p.instrument_id, cw.calendar_date AS event_date,
        0 AS delta_qty,
        1 AS is_marker
    FROM client_instrument_pairs p
    CROSS JOIN calendar_window cw
),

running_position AS (
    SELECT
        client_id, instrument_id, event_date, is_marker,
        SUM(delta_qty) OVER (
            PARTITION BY client_id, instrument_id
            ORDER BY event_date, is_marker          -- trades (0) settle before the marker (1) on the same day
            ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
        ) AS position_qty
    FROM events
),

position_by_day AS (
    SELECT client_id, instrument_id, event_date AS calendar_date, position_qty
    FROM running_position
    WHERE is_marker = 1
),

portfolio_value AS (
    SELECT
        pd.client_id,
        pd.calendar_date,
        SUM(pd.position_qty * md.close_price) AS total_value
    FROM position_by_day pd
    JOIN market_data md
      ON md.instrument_id = pd.instrument_id AND md."timestamp" = pd.calendar_date
    GROUP BY pd.client_id, pd.calendar_date
),

daily_returns AS (
    SELECT
        client_id,
        calendar_date,
        total_value,
        (total_value - LAG(total_value) OVER (PARTITION BY client_id ORDER BY calendar_date))
            / NULLIF(LAG(total_value) OVER (PARTITION BY client_id ORDER BY calendar_date), 0) AS daily_return
    FROM portfolio_value
),

ranked_returns AS (
    SELECT
        client_id,
        daily_return,
        ROW_NUMBER() OVER (PARTITION BY client_id ORDER BY daily_return ASC) AS ascending_rank,
        COUNT(*)     OVER (PARTITION BY client_id)                          AS num_observations
    FROM daily_returns
    WHERE daily_return IS NOT NULL
)

SELECT
    rr.client_id,
    c.name                                                 AS client_name,
    rr.num_observations,
    ROUND(rr.daily_return, 6)                              AS var_95_daily_return,
    ROUND(-rr.daily_return * 100, 3)                        AS var_95_pct_loss
FROM ranked_returns rr
JOIN clients c ON c.id = rr.client_id
WHERE rr.ascending_rank = CAST(0.05 * rr.num_observations AS INTEGER) + 1
ORDER BY var_95_pct_loss DESC;
