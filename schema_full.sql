CREATE EXTENSION IF NOT EXISTS timescaledb;

CREATE SCHEMA IF NOT EXISTS data;

CREATE TABLE data.did_query_full (
    line VARCHAR(10) NOT NULL,
    csv_date TIMESTAMPTZ NOT NULL,
    col_01 TEXT,
    col_02 TEXT,
    did TEXT,
    gcode TEXT,
    col_05 TEXT,
    col_06 TEXT,
    col_07 TEXT,
    col_08 TEXT,
    col_09 TEXT,
    col_10 TEXT,
    col_11 TEXT,
    col_12 TEXT,
    col_13 TEXT,
    col_14 TEXT,
    col_15 TEXT,
    col_16 TEXT
);

SELECT create_hypertable(
    'data.did_query_full',
    by_range('csv_date', INTERVAL '1 day')
);

CREATE INDEX idx_did_query_full_main_query
ON data.did_query_full (gcode, line, csv_date)
INCLUDE (did);
