CREATE DATABASE IF NOT EXISTS analytics;

CREATE TABLE IF NOT EXISTS analytics.events
(
    timestamp DateTime,
    shop_id UInt32,
    event_type LowCardinality(String),
    amount Float64,
    event_count UInt32
)
ENGINE = MergeTree
ORDER BY (shop_id, timestamp)
SETTINGS index_granularity = 8192;

ALTER TABLE analytics.events
    ADD PROJECTION IF NOT EXISTS events_by_type
    (
        SELECT
            event_type,
            shop_id,
            sum(event_count),
            sum(amount)
        GROUP BY event_type, shop_id
    );

CREATE TABLE IF NOT EXISTS analytics.events_daily
(
    day Date,
    shop_id UInt32,
    event_count UInt64,
    amount Float64
)
ENGINE = SummingMergeTree
ORDER BY (day, shop_id);

CREATE MATERIALIZED VIEW IF NOT EXISTS analytics.events_daily_mv
TO analytics.events_daily
AS
SELECT
    toDate(timestamp) AS day,
    shop_id,
    sum(event_count) AS event_count,
    sum(amount) AS amount
FROM analytics.events
GROUP BY day, shop_id;
