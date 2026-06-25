DROP INDEX IF EXISTS idx_import_log_status_time;

ALTER TABLE data.import_log
    DROP COLUMN IF EXISTS file_name,
    DROP COLUMN IF EXISTS file_size,
    DROP COLUMN IF EXISTS modified_at,
    DROP COLUMN IF EXISTS csv_date;

CREATE INDEX IF NOT EXISTS idx_import_log_status
ON data.import_log (status);

CREATE INDEX IF NOT EXISTS idx_import_log_status_imported_at
ON data.import_log (status, imported_at DESC);

CREATE INDEX IF NOT EXISTS idx_import_log_line_status
ON data.import_log (line, status);
