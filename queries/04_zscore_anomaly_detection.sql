-- ============================================================================
-- Query 4 — Price anomaly detection via rolling z-score (pure SQL)
-- ============================================================================
-- Technique demonstrated: rolling AVG() OVER a bounded frame, and a manual
-- rolling standard deviation built from the identity
--     Var(X) = E[X^2] - E[X]^2
-- i.e. the rolling average of the squared prices minus the square of the
-- rolling average. SQLite has no STDEV / STDEV_POP / STDEV_SAMP window
-- function, so this "sum-of-squares" decomposition is the standard manual
-- substitute — it only needs AVG(), which SQLite does support as a window
-- function, applied twice (once to price, once to price^2).
--
-- For each trade, we compute the mean and standard deviation of that
-- instrument's execution price over the trailing 20 trades (its own rolling
-- history, in trade order — not calendar days, since several trades can
-- happen on the same day), then flag the trade if its price sits more than
-- 3 standard deviations away from that rolling mean.
--
-- MAX(variance, 0) guards against a tiny negative value from floating-point
-- rounding when the true variance is ~0 (e.g. a flat run of identical
-- prices), which would otherwise make SQRT() return NULL.
-- ============================================================================

WITH rolling_stats AS (
    SELECT
        t.id                AS trade_id,
        t.client_id,
        t.instrument_id,
        i.ticker,
        t."timestamp"       AS trade_date,
        t.price,
        COUNT(*) OVER w      AS window_observations,
        AVG(t.price) OVER w  AS rolling_mean,
        AVG(t.price * t.price) OVER w AS rolling_mean_of_squares
    FROM trades t
    JOIN instruments i ON i.id = t.instrument_id
    WINDOW w AS (
        PARTITION BY t.instrument_id
        ORDER BY t."timestamp", t.id
        ROWS BETWEEN 19 PRECEDING AND CURRENT ROW   -- rolling window of 20 trades
    )
),

zscores AS (
    SELECT
        *,
        SQRT(MAX(rolling_mean_of_squares - rolling_mean * rolling_mean, 0.0)) AS rolling_stddev
    FROM rolling_stats
),

zscores_final AS (
    SELECT
        *,
        (price - rolling_mean) / NULLIF(rolling_stddev, 0) AS z_score
    FROM zscores
    WHERE window_observations >= 10   -- require enough history before judging a trade "abnormal"
)

SELECT
    trade_id,
    client_id,
    instrument_id,
    ticker,
    trade_date,
    ROUND(price, 4)              AS price,
    ROUND(rolling_mean, 4)       AS rolling_mean_20t,
    ROUND(rolling_stddev, 4)     AS rolling_stddev_20t,
    ROUND(z_score, 3)            AS z_score,
    CASE WHEN ABS(z_score) > 3 THEN 1 ELSE 0 END AS is_anomaly
FROM zscores_final
WHERE ABS(z_score) > 3
ORDER BY ABS(z_score) DESC;
