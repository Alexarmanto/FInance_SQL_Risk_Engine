-- ============================================================================
-- Query 7 — Position reconciliation vs. a second source of truth
-- ============================================================================
-- Technique demonstrated: emulating a FULL OUTER JOIN with two LEFT JOINs
-- combined by UNION (portable pattern, works on any SQL engine), to catch
-- discrepancies on *both* sides of the comparison:
--   - positions we calculated internally but the "custodian" never declared
--   - positions the "custodian" declared that our own trade history
--     doesn't support (phantom positions)
--   - positions present on both sides but with a mismatched quantity
--
-- `declared_positions` simulates a second, independent source of truth
-- (e.g. a custodian statement). It was seeded from the SAME corporate-
-- action-adjusted position logic as query 6, then had a handful of rows
-- deliberately corrupted (dropped, phantom-added, or nudged off) at data
-- generation time — see generate_data.py, function generate_declared_positions.
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
    -- Our internal, corporate-action-adjusted position per (client, instrument) — same method as query 6.
    SELECT
        t.client_id,
        t.instrument_id,
        SUM(
            (CASE WHEN t.side = 'BUY' THEN t.quantity ELSE -t.quantity END)
            * COALESCE(ft.total_factor, 1.0) / COALESCE(
                (SELECT cf2.cumulative_factor
                 FROM cumulative_factor cf2
                 WHERE cf2.instrument_id = t.instrument_id AND cf2.date <= t."timestamp"
                 ORDER BY cf2.date DESC
                 LIMIT 1),
                1.0
            )
        ) AS calculated_position
    FROM trades t
    LEFT JOIN factor_total ft ON ft.instrument_id = t.instrument_id
    GROUP BY t.client_id, t.instrument_id
    HAVING ABS(calculated_position) > 0.0001
),
last_reference_date AS (
    SELECT MAX(reference_date) AS reference_date FROM declared_positions
),
discrepancies AS (
    -- Pass 1: everything we calculated, matched (or not) to the declared side.
    SELECT
        cp.client_id,
        cp.instrument_id,
        cp.calculated_position,
        dp.declared_quantity
    FROM calculated_position cp
    LEFT JOIN declared_positions dp
      ON dp.client_id = cp.client_id AND dp.instrument_id = cp.instrument_id
     AND dp.reference_date = (SELECT reference_date FROM last_reference_date)

    UNION

    -- Pass 2: everything declared, matched (or not) to what we calculated.
    -- Combined with Pass 1 via UNION, this reproduces a FULL OUTER JOIN.
    SELECT
        dp.client_id,
        dp.instrument_id,
        cp.calculated_position,
        dp.declared_quantity
    FROM declared_positions dp
    LEFT JOIN calculated_position cp
      ON cp.client_id = dp.client_id AND cp.instrument_id = dp.instrument_id
    WHERE dp.reference_date = (SELECT reference_date FROM last_reference_date)
)

SELECT
    d.client_id,
    c.name                                                     AS client_name,
    d.instrument_id,
    i.ticker,
    ROUND(d.calculated_position, 4)                            AS calculated_position,
    d.declared_quantity,
    ROUND(COALESCE(d.calculated_position, 0) - COALESCE(d.declared_quantity, 0), 4) AS discrepancy,
    CASE
        WHEN d.calculated_position IS NULL THEN 'PHANTOM_AT_CUSTODIAN'    -- declared but not ours
        WHEN d.declared_quantity IS NULL THEN 'MISSING_AT_CUSTODIAN'      -- ours but not declared
        ELSE 'QUANTITY_MISMATCH'                                          -- both exist, amounts differ
    END AS discrepancy_type
FROM discrepancies d
JOIN clients c     ON c.id = d.client_id
JOIN instruments i ON i.id = d.instrument_id
WHERE d.calculated_position IS NULL
   OR d.declared_quantity IS NULL
   OR ABS(d.calculated_position - d.declared_quantity) > 0.01   -- tolerance for float rounding
ORDER BY discrepancy_type, ABS(COALESCE(d.calculated_position, 0) - COALESCE(d.declared_quantity, 0)) DESC;
