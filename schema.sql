CREATE EXTENSION IF NOT EXISTS timescaledb;

CREATE SCHEMA IF NOT EXISTS data;

CREATE TABLE IF NOT EXISTS data.import_log (
    id BIGSERIAL PRIMARY KEY,
    source_file_path TEXT NOT NULL UNIQUE,
    line VARCHAR(10),
    status VARCHAR(20) NOT NULL,
    row_count INTEGER,
    error_msg TEXT,
    started_at TIMESTAMP,
    imported_at TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_import_log_status
ON data.import_log (status);

CREATE INDEX IF NOT EXISTS idx_import_log_status_imported_at
ON data.import_log (status, imported_at DESC);

CREATE INDEX IF NOT EXISTS idx_import_log_line_status
ON data.import_log (line, status);

CREATE TABLE data.did_query (
    line VARCHAR(10) NOT NULL,
    csv_date TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    did TEXT,
    gcode TEXT
);

SELECT create_hypertable(
    'data.did_query',
    by_range('csv_date', INTERVAL '1 day')
);

CREATE INDEX idx_did_query_main_query
ON data.did_query (gcode, line, csv_date)
INCLUDE (did);
