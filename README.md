# csv-import-postgresql

将网络共享目录中的 `SIDTrace_00.csv` 到 `SIDTrace_23.csv` 导入 PostgreSQL。

## 文件说明

- `import_sid_csv_to_postgres.py`：只导入 `DID`、`GCODE`，以及解析得到的 `line`、`csv_date`。
- `import_sid_csv_full_to_postgres.py`：导入 CSV 全部字段，第三、第四列使用 `did`、`gcode`，其他列使用临时字段名。
- `schema.sql`：测试字段版数据表和导入日志表。
- `schema_full.sql`：全字段数据表参考结构。
- `migrate_empty_did_query_to_timescaledb.sql`：将当前空的 `did_query` 表升级为 TimescaleDB hypertable。
- `alter_import_log_remove_unused_columns.sql`：已有导入日志表的字段调整脚本。

脚本默认不会自动创建数据库或数据表。

`csv_date` 使用 `TIMESTAMPTZ`，脚本根据目录和文件名生成中国标准时间。例如：

```text
PT-L08\2023\M08\D02\SIDTrace_01.csv
→ 2023-08-02 01:00:00+08
```

查询时可以显示为小时格式：

```sql
SELECT to_char(
    csv_date AT TIME ZONE 'Asia/Shanghai',
    'YYYY-MM-DD HH24'
) AS csv_date
FROM data.did_query;
```

如果 `data.did_query` 已经创建但仍为空，请在 DBeaver 中执行：

```text
migrate_empty_did_query_to_timescaledb.sql
```

如果数据库中还没有这些表，则直接执行 `schema.sql`。

## 安装依赖

```powershell
python -m pip install -r requirements.txt
```

离线安装：

```powershell
python -m pip install --no-index --find-links .\offline_packages -r requirements.txt
```

## 运行

首次全量导入：

```powershell
python .\import_sid_csv_to_postgres.py --mode full
```

后续增量导入：

```powershell
python .\import_sid_csv_to_postgres.py --mode incremental
```

运行前需要修改脚本配置区中的 CSV 根目录和 PostgreSQL 连接信息。
