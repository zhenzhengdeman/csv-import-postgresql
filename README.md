# csv-import-postgresql

将网络共享目录中的 `SIDTrace_00.csv` 到 `SIDTrace_23.csv` 导入 PostgreSQL。

## 文件说明

- `import_sid_csv_to_postgres.py`：只导入 `DID`、`GCODE`，以及解析得到的 `line`、`csv_date`。
- `import_sid_csv_full_to_postgres.py`：导入 CSV 全部字段，第三、第四列使用 `did`、`gcode`，其他列使用临时字段名。
- `schema.sql`：测试字段版数据表和导入日志表。
- `schema_full.sql`：全字段数据表参考结构。
- `alter_import_log_remove_unused_columns.sql`：已有导入日志表的字段调整脚本。

脚本默认不会自动创建数据库或数据表。

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

