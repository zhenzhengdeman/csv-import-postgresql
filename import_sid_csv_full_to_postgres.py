"""
Import all SID CSV fields into PostgreSQL.

Key rules:
- import_log stores file-level information and status. source_file_path is saved
  from PT-Lxx onward, without the common CSV_ROOT prefix.
- The data table stores line, csv_date, all CSV fields, and keeps the 3rd/4th
  CSV fields named did/gcode. Other CSV fields use temporary names such as
  col_01, col_02, col_05...
- This script does not create the database or tables by default.
- line is shortened from PT-L08 to L08.
- csv_date is YYYYMMDDHH, parsed from folders and SIDTrace_HH.csv.
- Full mode audits expected hourly files from AUDIT_START_DATE and writes
  status=missing when the file does not exist.
- Incremental mode audits only recent hourly files and imports newly found files.

Install dependency:
    pip install psycopg[binary]

Run full import:
    python import_sid_csv_full_to_postgres.py --mode full

Run incremental import:
    python import_sid_csv_full_to_postgres.py --mode incremental
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path, PureWindowsPath
from typing import Iterator, Sequence

# =========================
# 配置区：后续主要改这里
# =========================

# CSV 根目录。
CSV_ROOT = r"\\192.168.1.28\TraceDate"

# 只导入 SIDTrace_00.csv 到 SIDTrace_23.csv 这类文件。
FILE_PATTERN = "SIDTrace_*.csv"

# PostgreSQL 连接信息。也可以改成从环境变量读取。
DB_HOST = "127.0.0.1"
DB_PORT = 5432
DB_NAME = "TraceData"
DB_USER = "postgres"
DB_PASSWORD = "数据库密码"

# 表名。
DB_SCHEMA = "data"
IMPORT_LOG_TABLE = "import_log"
DATA_TABLE = "did_query_full"

# 是否自动创建 import_log 表。默认关闭，要求提前建好表。
AUTO_CREATE_IMPORT_LOG_TABLE = False

# 是否自动创建数据表。默认关闭，要求提前建好表。
AUTO_CREATE_DATA_TABLE = False

# CSV 是否有表头。当前按无表头处理，第一行直接作为数据导入。
CSV_HAS_HEADER = False

# 编码尝试顺序。中文文件常见 GBK，也可能是 UTF-8 with BOM。
ENCODINGS = ["utf-8-sig", "gbk", "utf-8"]

# 每批写入多少行。
BATCH_SIZE = 2000

# 文件最后修改时间距离当前时间小于这个值时跳过，避免导入未写完文件。
FILE_STABLE_MINUTES = 5

# 全量审计从哪一天开始。首次补 import_log 时从这里开始逐天统计。
AUDIT_START_DATE = date(2021, 1, 1)

# 增量模式默认扫描最近几天。设为 1 表示只扫今天，设为 3 表示今天和前两天。
INCREMENTAL_LOOKBACK_DAYS = 3

# 增量模式下是否记录应有但不存在的小时文件。
AUDIT_MISSING_FILES = True

# 需要审计的产线目录。
EXPECTED_LINE_DIRS = [
    "PT-L01",
    "PT-L02",
    "PT-L03",
    "PT-L04",
    "PT-L05",
    "PT-L06",
    "PT-L07",
    "PT-L08",
]


# =========================
# 逻辑区：正常情况下不改
# =========================


@dataclass(frozen=True)
class FileMeta:
    source_file_path: str
    line: str
    csv_date: str
    work_date: date


def quote_ident(name: str) -> str:
    """Quote a PostgreSQL identifier after strict validation."""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise ValueError(f"非法字段名或表名：{name}")
    return f'"{name}"'


def table_ref(table_name: str) -> str:
    """Return a schema-qualified table reference."""
    if DB_SCHEMA:
        return f"{quote_ident(DB_SCHEMA)}.{quote_ident(table_name)}"
    return quote_ident(table_name)


def get_connection() -> psycopg.Connection:
    try:
        import psycopg
    except ImportError as exc:
        raise SystemExit(
            "缺少 PostgreSQL Python 驱动，请先执行：pip install -r requirements.txt"
        ) from exc

    return psycopg.connect(
        host=DB_HOST,
        port=DB_PORT,
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
    )


def ensure_import_log_table(conn: psycopg.Connection) -> None:
    table = table_ref(IMPORT_LOG_TABLE)
    with conn.cursor() as cur:
        if DB_SCHEMA:
            cur.execute(f"CREATE SCHEMA IF NOT EXISTS {quote_ident(DB_SCHEMA)}")
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {table} (
                id bigserial PRIMARY KEY,
                source_file_path text NOT NULL UNIQUE,
                line text,
                status text NOT NULL,
                row_count integer,
                error_msg text,
                started_at timestamp,
                imported_at timestamp
            )
            """
        )
    conn.commit()


def ensure_data_table(conn: psycopg.Connection) -> None:
    raise RuntimeError("全字段导入脚本默认不自动建表，请先按目标列数创建数据表。")


def scan_sid_csv_files(root: str, pattern: str) -> Iterator[Path]:
    root_path = Path(root)
    if not root_path.exists():
        raise FileNotFoundError(f"CSV 根目录不存在或无法访问：{root}")

    yield from sorted(
        (file for file in root_path.rglob(pattern) if file.is_file()),
        key=lambda item: str(item).lower(),
    )


def should_process_by_mode(meta: FileMeta, mode: str) -> bool:
    if mode == "full":
        return True

    today = date.today()
    earliest = today - timedelta(days=INCREMENTAL_LOOKBACK_DAYS - 1)
    return meta.work_date >= earliest


def iter_audit_dates(mode: str) -> Iterator[date]:
    today = date.today()
    if mode == "full":
        current = AUDIT_START_DATE
    else:
        current = today - timedelta(days=INCREMENTAL_LOOKBACK_DAYS - 1)

    while current <= today:
        yield current
        current += timedelta(days=1)


def expected_file_path(line_dir: str, work_date: date, hour: int) -> Path:
    return (
        Path(CSV_ROOT)
        / line_dir
        / f"{work_date.year:04d}"
        / f"M{work_date.month:02d}"
        / f"D{work_date.day:02d}"
        / f"SIDTrace_{hour:02d}.csv"
    )


def is_expected_slot_ready(work_date: date, hour: int) -> bool:
    slot_start = datetime(work_date.year, work_date.month, work_date.day, hour)
    slot_end = slot_start + timedelta(hours=1)
    return slot_end <= datetime.now() - timedelta(minutes=FILE_STABLE_MINUTES)


def iter_expected_files(mode: str) -> Iterator[Path]:
    for work_date in iter_audit_dates(mode):
        for line_dir in EXPECTED_LINE_DIRS:
            for hour in range(24):
                if is_expected_slot_ready(work_date, hour):
                    yield expected_file_path(line_dir, work_date, hour)


def is_file_stable(file_path: Path) -> bool:
    age_seconds = time.time() - file_path.stat().st_mtime
    return age_seconds >= FILE_STABLE_MINUTES * 60


def make_log_source_path(source_file_path: str) -> str:
    """Return path from PT-Lxx onward for import_log.source_file_path."""
    parts = PureWindowsPath(source_file_path).parts
    for index, part in enumerate(parts):
        if re.fullmatch(r"PT-L\d{2}", part, re.IGNORECASE):
            return "\\".join(parts[index:])
    return PureWindowsPath(source_file_path).name


def parse_path_context(source_file_path: str) -> tuple[str, str, date]:
    win_path = PureWindowsPath(source_file_path)
    parts = win_path.parts

    full_line_code = next(
        (part for part in parts if re.fullmatch(r"PT-L\d{2}", part, re.IGNORECASE)),
        None,
    )
    if full_line_code is None:
        raise ValueError(f"无法从路径解析线别：{source_file_path}")
    full_line_code = full_line_code.upper()
    line = full_line_code.replace("PT-", "")

    year = next((part for part in parts if re.fullmatch(r"\d{4}", part)), None)
    month = next((part for part in parts if re.fullmatch(r"M\d{2}", part, re.I)), None)
    day = next((part for part in parts if re.fullmatch(r"D\d{2}", part, re.I)), None)
    if not (year and month and day):
        raise ValueError(f"无法从路径解析年月日：{source_file_path}")

    hour_match = re.fullmatch(r"SIDTrace_(\d{2})\.csv", win_path.name, re.IGNORECASE)
    if hour_match is None:
        raise ValueError(f"文件名不符合 SIDTrace_HH.csv 规则：{win_path.name}")
    hour = hour_match.group(1)
    if not 0 <= int(hour) <= 23:
        raise ValueError(f"小时不在 00-23 范围：{win_path.name}")

    csv_date = f"{year}{month[1:]}{day[1:]}{hour}"
    work_date = date(int(year), int(month[1:]), int(day[1:]))

    return line, csv_date, work_date


def parse_meta(file_path: Path) -> FileMeta:
    actual_file_path = str(file_path)
    line, csv_date, work_date = parse_path_context(actual_file_path)

    return FileMeta(
        source_file_path=make_log_source_path(actual_file_path),
        line=line,
        csv_date=csv_date,
        work_date=work_date,
    )


def make_expected_meta(file_path: Path) -> FileMeta:
    actual_file_path = str(file_path)
    line, csv_date, work_date = parse_path_context(actual_file_path)
    return FileMeta(
        source_file_path=make_log_source_path(actual_file_path),
        line=line,
        csv_date=csv_date,
        work_date=work_date,
    )


def make_failed_meta(file_path: Path) -> FileMeta:
    """Build minimal file metadata when path parsing fails."""
    actual_file_path = str(file_path)
    return FileMeta(
        source_file_path=make_log_source_path(actual_file_path),
        line="",
        csv_date="",
        work_date=date.min,
    )


def detect_encoding(file_path: Path) -> str:
    for encoding in ENCODINGS:
        try:
            with file_path.open("r", encoding=encoding, newline="") as handle:
                handle.read(8192)
            return encoding
        except UnicodeDecodeError:
            continue

    raise UnicodeDecodeError(
        "unknown",
        b"",
        0,
        1,
        f"无法用这些编码读取文件：{', '.join(ENCODINGS)}",
    )


def already_imported_success(conn: psycopg.Connection, source_file_path: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT status
            FROM {table_ref(IMPORT_LOG_TABLE)}
            WHERE source_file_path = %s
            """,
            (source_file_path,),
        )
        row = cur.fetchone()
    return bool(row and row[0] == "success")


def mark_importing(conn: psycopg.Connection, meta: FileMeta) -> None:
    now = datetime.now()
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {table_ref(IMPORT_LOG_TABLE)} (
                source_file_path,
                line,
                status,
                row_count,
                error_msg,
                started_at,
                imported_at
            )
            VALUES (%s, %s, 'importing', NULL, NULL, %s, NULL)
            ON CONFLICT (source_file_path)
            DO UPDATE SET
                line = EXCLUDED.line,
                status = 'importing',
                row_count = NULL,
                error_msg = NULL,
                started_at = EXCLUDED.started_at,
                imported_at = NULL
            """,
            (
                meta.source_file_path,
                meta.line,
                now,
            ),
        )
    conn.commit()


def mark_success(conn: psycopg.Connection, source_file_path: str, row_count: int) -> None:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE {table_ref(IMPORT_LOG_TABLE)}
            SET status = 'success',
                row_count = %s,
                error_msg = NULL,
                imported_at = %s
            WHERE source_file_path = %s
            """,
            (row_count, datetime.now(), source_file_path),
        )


def mark_failed(conn: psycopg.Connection, meta: FileMeta, error: Exception) -> None:
    message = str(error)
    if len(message) > 4000:
        message = message[:4000]

    table = table_ref(IMPORT_LOG_TABLE)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {table} (
                source_file_path,
                line,
                status,
                row_count,
                error_msg,
                started_at,
                imported_at
            )
            VALUES (%s, %s, 'failed', NULL, %s, %s, %s)
            ON CONFLICT (source_file_path)
            DO UPDATE SET
                line = EXCLUDED.line,
                status = 'failed',
                row_count = NULL,
                error_msg = EXCLUDED.error_msg,
                started_at = COALESCE({table}.started_at, EXCLUDED.started_at),
                imported_at = EXCLUDED.imported_at
            """,
            (
                meta.source_file_path,
                meta.line,
                message,
                datetime.now(),
                datetime.now(),
            ),
        )
    conn.commit()


def mark_missing_batch(conn: psycopg.Connection, metas: Sequence[FileMeta]) -> None:
    if not metas:
        return

    now = datetime.now()
    table = table_ref(IMPORT_LOG_TABLE)
    rows = [
        (
            meta.source_file_path,
            meta.line,
            "expected file not found",
            now,
            now,
        )
        for meta in metas
    ]

    with conn.cursor() as cur:
        cur.executemany(
            f"""
            INSERT INTO {table} (
                source_file_path,
                line,
                status,
                row_count,
                error_msg,
                started_at,
                imported_at
            )
            VALUES (%s, %s, 'missing', NULL, %s, %s, %s)
            ON CONFLICT (source_file_path)
            DO UPDATE SET
                line = EXCLUDED.line,
                status = 'missing',
                row_count = NULL,
                error_msg = EXCLUDED.error_msg,
                imported_at = EXCLUDED.imported_at
            WHERE {table}.status <> 'success'
            """,
            rows,
        )
    conn.commit()


def mark_missing(conn: psycopg.Connection, meta: FileMeta) -> None:
    mark_missing_batch(conn, [meta])


def iter_csv_rows(file_path: Path, encoding: str) -> tuple[list[str] | None, Iterator[list[str]]]:
    handle = file_path.open("r", encoding=encoding, newline="")
    reader = csv.reader(handle)

    header: list[str] | None = None
    if CSV_HAS_HEADER:
        try:
            header = next(reader)
        except StopIteration:
            handle.close()
            return None, iter(())

    def row_iterator() -> Iterator[list[str]]:
        try:
            for row in reader:
                if not row or all(cell == "" for cell in row):
                    continue
                yield row
        finally:
            handle.close()

    return header, row_iterator()


def make_full_import_columns(csv_column_count: int) -> list[str]:
    if csv_column_count < 4:
        raise ValueError(f"CSV 列数不足 4 列，无法生成 did/gcode 字段：{csv_column_count}")

    columns: list[str] = ["line", "csv_date"]
    for index in range(1, csv_column_count + 1):
        if index == 3:
            columns.append("did")
        elif index == 4:
            columns.append("gcode")
        else:
            columns.append(f"col_{index:02d}")
    return columns


def insert_batch(
    conn: psycopg.Connection,
    columns: Sequence[str],
    rows: Sequence[Sequence[str]],
) -> None:
    quoted_columns = ", ".join(quote_ident(column) for column in columns)
    placeholders = ", ".join(["%s"] * len(columns))
    sql = f"""
        INSERT INTO {table_ref(DATA_TABLE)} ({quoted_columns})
        VALUES ({placeholders})
    """

    with conn.cursor() as cur:
        cur.executemany(sql, rows)


def import_one_file(conn: psycopg.Connection, file_path: Path, meta: FileMeta) -> int:
    encoding = detect_encoding(file_path)
    header, csv_rows = iter_csv_rows(file_path, encoding)

    row_count = 0
    batch: list[list[str]] = []
    insert_columns: list[str] | None = None
    expected_csv_columns: int | None = None

    if AUTO_CREATE_DATA_TABLE:
        ensure_data_table(conn)

    for csv_row in csv_rows:
        if insert_columns is None:
            expected_csv_columns = len(csv_row)
            insert_columns = make_full_import_columns(expected_csv_columns)

        assert expected_csv_columns is not None
        assert insert_columns is not None

        if len(csv_row) != expected_csv_columns:
            raise ValueError(
                f"CSV 列数不一致：期望 {expected_csv_columns} 列，实际 {len(csv_row)} 列，文件 {file_path}"
            )

        batch.append([meta.line, meta.csv_date, *csv_row])
        row_count += 1

        if len(batch) >= BATCH_SIZE:
            insert_batch(conn, insert_columns, batch)
            batch.clear()

    if batch:
        assert insert_columns is not None
        insert_batch(conn, insert_columns, batch)

    mark_success(conn, meta.source_file_path, row_count)
    return row_count


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import SID CSV files into PostgreSQL.")
    parser.add_argument(
        "--mode",
        choices=["full", "incremental"],
        default="incremental",
        help="full 从 AUDIT_START_DATE 开始补全日志并扫描全部历史目录；incremental 只处理最近几天。",
    )
    return parser.parse_args()


def run() -> int:
    args = parse_args()
    started = datetime.now(timezone.utc)
    imported_files = 0
    skipped_files = 0
    failed_files = 0
    missing_files = 0

    if not AUTO_CREATE_IMPORT_LOG_TABLE and not AUTO_CREATE_DATA_TABLE:
        print("提示：脚本不会自动建库建表，请确认数据库、import_log 和数据表已提前创建。")

    if not Path(CSV_ROOT).exists():
        raise FileNotFoundError(f"CSV 根目录不存在或无法访问：{CSV_ROOT}")

    with get_connection() as conn:
        if AUTO_CREATE_IMPORT_LOG_TABLE:
            ensure_import_log_table(conn)

        if AUDIT_MISSING_FILES:
            missing_batch: list[FileMeta] = []
            for expected_path in iter_expected_files(args.mode):
                if expected_path.exists():
                    continue

                expected_meta = make_expected_meta(expected_path)
                missing_batch.append(expected_meta)
                missing_files += 1

                if len(missing_batch) >= BATCH_SIZE:
                    mark_missing_batch(conn, missing_batch)
                    missing_batch.clear()

            if missing_batch:
                mark_missing_batch(conn, missing_batch)

        for file_path in scan_sid_csv_files(CSV_ROOT, FILE_PATTERN):
            try:
                if not is_file_stable(file_path):
                    skipped_files += 1
                    continue

                try:
                    meta = parse_meta(file_path)
                except Exception as exc:
                    failed_meta = make_failed_meta(file_path)
                    mark_failed(conn, failed_meta, exc)
                    failed_files += 1
                    print(f"[failed] {file_path} error={exc}", file=sys.stderr)
                    continue

                if not should_process_by_mode(meta, args.mode):
                    skipped_files += 1
                    continue

                if already_imported_success(conn, meta.source_file_path):
                    skipped_files += 1
                    continue

                mark_importing(conn, meta)

                try:
                    row_count = import_one_file(conn, file_path, meta)
                    conn.commit()
                    imported_files += 1
                    print(f"[success] {meta.source_file_path} rows={row_count}")
                except Exception as exc:
                    conn.rollback()
                    mark_failed(conn, meta, exc)
                    failed_files += 1
                    print(f"[failed] {meta.source_file_path} error={exc}", file=sys.stderr)

            except Exception as exc:
                failed_files += 1
                print(f"[failed] {file_path} error={exc}", file=sys.stderr)

    elapsed = datetime.now(timezone.utc) - started
    print(
        f"完成：导入文件 {imported_files} 个，跳过 {skipped_files} 个，缺失 {missing_files} 个，失败 {failed_files} 个，用时 {elapsed}"
    )
    return 0 if failed_files == 0 else 1


if __name__ == "__main__":
    raise SystemExit(run())
