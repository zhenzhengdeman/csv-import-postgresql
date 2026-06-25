-- 仅用于当前尚未导入数据的 data.did_query 空表。
-- 如果表中已有数据，脚本会主动停止，避免误操作。

CREATE EXTENSION IF NOT EXISTS timescaledb;

SET TIME ZONE 'Asia/Shanghai';

DO $$
DECLARE
    current_data_type TEXT;
BEGIN
    IF to_regclass('data.did_query') IS NULL THEN
        RAISE EXCEPTION 'data.did_query 不存在，请先执行 schema.sql';
    END IF;

    IF EXISTS (SELECT 1 FROM data.did_query LIMIT 1) THEN
        RAISE EXCEPTION 'data.did_query 已有数据，本脚本只允许处理空表';
    END IF;

    SELECT data_type
    INTO current_data_type
    FROM information_schema.columns
    WHERE table_schema = 'data'
      AND table_name = 'did_query'
      AND column_name = 'csv_date';

    IF current_data_type IS NULL THEN
        RAISE EXCEPTION 'data.did_query.csv_date 字段不存在';
    END IF;

    IF current_data_type <> 'timestamp with time zone' THEN
        ALTER TABLE data.did_query
            ALTER COLUMN csv_date TYPE TIMESTAMPTZ
            USING to_timestamp(csv_date::TEXT, 'YYYYMMDDHH24');
    END IF;
END
$$;

ALTER TABLE data.did_query
    ALTER COLUMN csv_date SET NOT NULL;

SELECT create_hypertable(
    'data.did_query',
    by_range('csv_date', INTERVAL '1 day'),
    if_not_exists => TRUE
);

DROP INDEX IF EXISTS data.idx_did_query_main_query;

CREATE INDEX idx_did_query_main_query
ON data.did_query (gcode, line, csv_date)
INCLUDE (did);
