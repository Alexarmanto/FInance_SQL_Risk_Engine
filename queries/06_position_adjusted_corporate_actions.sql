-- ============================================================================
-- Query 6 — Trade quantities adjusted for corporate actions, via a join
-- ============================================================================
-- Technique demonstrated: reuses the recursive CTE from query 5, then joins
-- it back onto `trades` (conditional join logic) to convert every historical
-- trade quantity into today's share-count terms, and finally re-runs the
-- running-total window function from query 1 on the ADJUSTED quantities.
--
-- Why a trade needs adjusting at all: a SPLIT changes the share count for
-- shares already held WITHOUT a new trade being booked. A client who bought
-- 100 shares before a 2-for-1 split silently owns 200 afterwards. So a trade
-- executed *before* a split must be scaled up by every split ratio that
-- happened *after* it to be comparable to today's share count; a trade
-- executed *after* the last split needs no adjustment at all (factor = 1).
--
-- For a trade at time t, the correct adjustment factor is:
--     adjustment_factor(t) = total_cumulative_factor(instrument)
--                              / cumulative_factor_as_of(instrument, t)
-- where cumulative_factor_as_of(t) is the cumulative factor as of the last
-- corporate action on/before t (or 1.0 if there is none yet). Dividing by
-- that "already-applied" portion is what correctly composes two successive
-- splits straddling a trade date (e.g. a trade between a 2-for-1 and a
-- later 3-for-1 split gets adjusted by 3.0 only, not 6.0 — the 2-for-1
-- effect is already embedded in the shares that existed at trade time).
-- corporate_actions is tiny (a handful of rows per instrument), so the two
-- correlated subqueries below are cheap index lookups, not a performance
-- concern.
-- ============================================================================

WITH RECURSIVE ca_ordered AS (
    SELECT
        instrument_id,
        date,
        CASE WHEN action_type = 'SPLIT' THEN ratio ELSE 1.0 END AS event_factor,
        ROW_NUMBER() OVER (PARTITION BY instrument_id ORDER BY date) AS rn
    FROM corporate_actions
),

cumulative_factor AS (
    SELECT instrument_id, date, event_factor, rn, event_factor AS cumulative_factor
    FROM ca_ordered
    WHERE rn = 1

    UNION ALL

    SELECT o.instrument_id, o.date, o.event_factor, o.rn,
           c.cumulative_factor * o.event_factor
    FROM ca_ordered o
    JOIN cumulative_factor c ON o.instrument_id = c.instrument_id AND o.rn = c.rn + 1
),

factor_total AS (
    -- Final cumulative factor per instrument = product of ALL its corporate actions.
    SELECT cf.instrument_id, cf.cumulative_factor AS total_factor
    FROM cumulative_factor cf
    JOIN (SELECT instrument_id, MAX(rn) AS rn_max FROM ca_ordered GROUP BY instrument_id) last_evt
      ON last_evt.instrument_id = cf.instrument_id AND last_evt.rn_max = cf.rn
),

adjusted_trades AS (
    SELECT
        t.id                AS trade_id,
        t.client_id,
        t.instrument_id,
        t."timestamp"       AS trade_date,
        t.side,
        t.quantity           AS raw_quantity,
        -- Conditional join: instruments with no corporate action at all get
        -- a neutral factor of 1.0 via COALESCE, instead of being dropped.
        COALESCE(ft.total_factor, 1.0) / COALESCE(
            (SELECT cf2.cumulative_factor
             FROM cumulative_factor cf2
             WHERE cf2.instrument_id = t.instrument_id AND cf2.date <= t."timestamp"
             ORDER BY cf2.date DESC
             LIMIT 1),
            1.0
        ) AS adjustment_factor
    FROM trades t
    LEFT JOIN factor_total ft ON ft.instrument_id = t.instrument_id
)

SELECT
    trade_id,
    client_id,
    instrument_id,
    trade_date,
    side,
    raw_quantity,
    ROUND(adjustment_factor, 6)                                            AS adjustment_factor,
    ROUND(raw_quantity * adjustment_factor, 4)                             AS adjusted_quantity,
    ROUND(
        SUM(CASE WHEN side = 'BUY' THEN raw_quantity * adjustment_factor
                 ELSE -raw_quantity * adjustment_factor END)
        OVER (PARTITION BY client_id, instrument_id ORDER BY trade_date, trade_id
              ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW),
        4
    ) AS running_adjusted_position
FROM adjusted_trades
ORDER BY client_id, instrument_id, trade_date, trade_id;
