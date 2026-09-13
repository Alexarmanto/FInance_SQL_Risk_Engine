-- ============================================================================
-- Query 5 — Cumulative corporate-action adjustment factor (recursive CTE)
-- ============================================================================
-- Technique demonstrated: WITH RECURSIVE to compose an ordered sequence of
-- multiplicative events into a running cumulative product.
--
-- Each corporate action carries an event-level factor:
--   - SPLIT    -> its ratio (e.g. 2.0 for a 2-for-1 split)
--   - DIVIDEND -> 1.0 (a cash dividend does not change the share count)
--
-- If an instrument has two successive splits (e.g. 2-for-1 then 3-for-1),
-- the factor that converts a *pre-both-splits* share count into today's
-- share count is the PRODUCT of the two ratios (2.0 * 3.0 = 6.0), not just
-- the latest one. A plain window SUM/PRODUCT can't express "multiply
-- everything up to and including this row" cleanly across an arbitrary
-- number of prior rows without a recursive step, so we walk the ordered
-- events one at a time:
--   anchor member    -> the first corporate action per instrument
--   recursive member -> join the next action (rn = previous rn + 1) and
--                       multiply its factor into the running product
-- ============================================================================

WITH RECURSIVE ca_ordered AS (
    SELECT
        instrument_id,
        date,
        action_type,
        CASE WHEN action_type = 'SPLIT' THEN ratio ELSE 1.0 END AS event_factor,
        ROW_NUMBER() OVER (PARTITION BY instrument_id ORDER BY date) AS rn
    FROM corporate_actions
),

cumulative_factor AS (
    -- Anchor: first corporate action of each instrument.
    SELECT instrument_id, date, action_type, event_factor, rn,
           event_factor AS cumulative_factor
    FROM ca_ordered
    WHERE rn = 1

    UNION ALL

    -- Recursive step: chain to the next action for the same instrument,
    -- multiplying its factor into the running cumulative product.
    SELECT
        o.instrument_id, o.date, o.action_type, o.event_factor, o.rn,
        c.cumulative_factor * o.event_factor AS cumulative_factor
    FROM ca_ordered o
    JOIN cumulative_factor c
      ON o.instrument_id = c.instrument_id AND o.rn = c.rn + 1
)

SELECT
    cf.instrument_id,
    i.ticker,
    cf.date,
    cf.action_type,
    cf.event_factor,
    cf.cumulative_factor AS cumulative_factor_as_of_date
FROM cumulative_factor cf
JOIN instruments i ON i.id = cf.instrument_id
ORDER BY cf.instrument_id, cf.date;
