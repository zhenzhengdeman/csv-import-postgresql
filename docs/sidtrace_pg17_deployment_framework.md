# SIDTrace CSV 入库部署文档

## 1. 基本信息

本次部署用于测试虚拟机环境。

| 项目 | 值 |
| --- | --- |
| 操作系统 | Rocky Linux 9 |
| 架构 | x86_64 |
| 网络 | 内网离线环境 |
| 依赖来源 | 公司内部 repo |
| PostgreSQL 安装包 | 自己提前下载的 PostgreSQL 17.10 RPM |
| TimescaleDB 安装包 | 自己提前下载的 TimescaleDB 2.28.1 for PostgreSQL 17 RPM |
| 数据库版本 | PostgreSQL 17.10 |
| TimescaleDB 版本 | 2.28.1 |
| 数据库名 | `TraceData` |
| 业务 schema | `data` |
| 业务表 | `data.did_query` |
| 导入日志表 | `data.import_log` |
| CSV 文件位置 | 远端文件服务器 `\\192.168.1.28\TraceDate` |
| 导入程序位置 | Windows 导入服务器，和数据库不同机 |
| 数据规模 | 13 亿多行 |
| 查询条件 | 基本固定为 `gcode + line + csv_date` |
| 增量方式 | Windows 定时任务 |
| 查询账号 | 使用只读账号查询 |

本文示例目录：

```bash
export DB_NAME="TraceData"
export DB_SCHEMA="data"

export COMPANY_BASE_REPO="<公司Rocky9基础repo-id>"
export COMPANY_DEPS_REPO="<公司依赖repo-id>"

export LOCAL_RPM_DIR="/opt/offline-packages/pgsql17-timescaledb-rpms"

export PGROOT="/data/postgresql/17.10"
export PGDATA="${PGROOT}/data"
export PGWAL="${PGROOT}/pg_wal"
export PGLOG="${PGROOT}/log"

export DB_IP="<数据库服务器IP>"
export IMPORT_SERVER_IP="<导入服务器IP>"
export QUERY_CLIENT_CIDR="<查询客户端IP或网段>"
```

说明：

- RPM 安装方式下，PostgreSQL 程序目录通常是 `/usr/pgsql-17`。
- 自定义安装目录主要指数据目录、WAL 目录、日志目录。
- PostgreSQL 和 TimescaleDB 主安装包放在本地目录，依赖从公司内部 repo 补齐。

## 2. 安装 PostgreSQL 和 TimescaleDB

在 Rocky 9 数据库服务器上执行。

### 2.1 禁用系统自带 PostgreSQL 模块

```bash
sudo -i

dnf -qy module disable postgresql
dnf clean all
dnf makecache \
  --disablerepo='*' \
  --enablerepo="${COMPANY_BASE_REPO}" \
  --enablerepo="${COMPANY_DEPS_REPO}"
```

### 2.2 准备本地 RPM 包目录

把自己下载好的 PostgreSQL 17.10 和 TimescaleDB 2.28.1 RPM 包放到：

```bash
mkdir -p "${LOCAL_RPM_DIR}"
cd "${LOCAL_RPM_DIR}"
ls -lh
```

目录中至少应包含类似包：

```text
postgresql17-17.10-*.x86_64.rpm
postgresql17-libs-17.10-*.x86_64.rpm
postgresql17-server-17.10-*.x86_64.rpm
postgresql17-contrib-17.10-*.x86_64.rpm
timescaledb-2-postgresql-17-2.28.1-*.x86_64.rpm
```

检查版本：

```bash
rpm -qp --qf '%{NAME} %{VERSION}-%{RELEASE} %{ARCH}\n' "${LOCAL_RPM_DIR}"/*.rpm | sort
```

确认：

- PostgreSQL 包版本是 `17.10`。
- TimescaleDB 包版本是 `2.28.1`。
- TimescaleDB 包名是 `timescaledb-2-postgresql-17`。
- 架构是 `x86_64`。

### 2.3 安装

```bash
dnf install -y \
  --disablerepo='*' \
  --enablerepo="${COMPANY_BASE_REPO}" \
  --enablerepo="${COMPANY_DEPS_REPO}" \
  "${LOCAL_RPM_DIR}"/*.rpm
```

如果公司基础 repo 和依赖 repo 是同一个，只保留一个 `--enablerepo` 即可。

安装后确认：

```bash
/usr/pgsql-17/bin/postgres --version
/usr/pgsql-17/bin/psql --version
rpm -qa | grep -E 'postgresql17|timescaledb' | sort
```

## 3. 初始化 PostgreSQL

### 3.1 创建数据目录

```bash
mkdir -p "${PGDATA}" "${PGWAL}" "${PGLOG}"
chown -R postgres:postgres "${PGROOT}"
chmod 700 "${PGDATA}" "${PGWAL}"
chmod 750 "${PGROOT}" "${PGLOG}"
```

如果 SELinux 是 `Enforcing`，给自定义目录设置上下文：

```bash
dnf install -y \
  --disablerepo='*' \
  --enablerepo="${COMPANY_BASE_REPO}" \
  --enablerepo="${COMPANY_DEPS_REPO}" \
  policycoreutils-python-utils

semanage fcontext -a -t postgresql_db_t "${PGROOT}(/.*)?" || \
semanage fcontext -m -t postgresql_db_t "${PGROOT}(/.*)?"
restorecon -Rv "${PGROOT}"
```

### 3.2 初始化数据库目录

```bash
sudo -iu postgres /usr/pgsql-17/bin/initdb \
  -D "${PGDATA}" \
  --waldir="${PGWAL}" \
  --encoding=UTF8 \
  --locale=C.UTF-8 \
  --auth-local=peer \
  --auth-host=scram-sha-256
```

如果系统没有 `C.UTF-8`，使用：

```bash
sudo -iu postgres /usr/pgsql-17/bin/initdb \
  -D "${PGDATA}" \
  --waldir="${PGWAL}" \
  --encoding=UTF8 \
  --locale=C \
  --auth-local=peer \
  --auth-host=scram-sha-256
```

### 3.3 配置 systemd 使用自定义数据目录

```bash
mkdir -p /etc/systemd/system/postgresql-17.service.d

cat > /etc/systemd/system/postgresql-17.service.d/override.conf <<EOF
[Service]
Environment=PGDATA=${PGDATA}
EOF

systemctl daemon-reload
```

## 4. 配置 PostgreSQL

### 4.1 修改 `postgresql.conf`

```bash
cat >> "${PGDATA}/postgresql.conf" <<'EOF'

# SIDTrace deployment
listen_addresses = '*'
port = 5432
password_encryption = 'scram-sha-256'

shared_preload_libraries = 'timescaledb,pg_stat_statements'

max_connections = 200
shared_buffers = '4GB'
effective_cache_size = '12GB'
work_mem = '16MB'
maintenance_work_mem = '1GB'

checkpoint_completion_target = 0.9
max_wal_size = '16GB'
min_wal_size = '4GB'
wal_compression = on

logging_collector = on
log_directory = '/data/postgresql/17.10/log'
log_filename = 'postgresql-%Y-%m-%d_%H%M%S.log'
log_rotation_age = '1d'
log_rotation_size = '1GB'
log_min_duration_statement = '5s'
log_line_prefix = '%m [%p] %u@%d %r '

timezone = 'Asia/Shanghai'
log_timezone = 'Asia/Shanghai'
track_io_timing = on
EOF
```

如果测试虚拟机内存较小，把内存参数改小：

```bash
sed -i "s/shared_buffers = '4GB'/shared_buffers = '1GB'/" "${PGDATA}/postgresql.conf"
sed -i "s/effective_cache_size = '12GB'/effective_cache_size = '3GB'/" "${PGDATA}/postgresql.conf"
sed -i "s/maintenance_work_mem = '1GB'/maintenance_work_mem = '512MB'/" "${PGDATA}/postgresql.conf"
```

### 4.2 修改 `pg_hba.conf`

```bash
cat >> "${PGDATA}/pg_hba.conf" <<EOF

# SIDTrace deployment
local   all             postgres                                  peer
local   all             all                                       peer
host    all             all               127.0.0.1/32            scram-sha-256

host    ${DB_NAME}      sid_import_user   ${IMPORT_SERVER_IP}/32  scram-sha-256
host    ${DB_NAME}      sid_readonly_user ${QUERY_CLIENT_CIDR}    scram-sha-256
EOF
```

### 4.3 放通防火墙

```bash
firewall-cmd --permanent \
  --add-rich-rule="rule family='ipv4' source address='${IMPORT_SERVER_IP}/32' port protocol='tcp' port='5432' accept"

firewall-cmd --permanent \
  --add-rich-rule="rule family='ipv4' source address='${QUERY_CLIENT_CIDR}' port protocol='tcp' port='5432' accept"

firewall-cmd --reload
```

如果 `QUERY_CLIENT_CIDR` 是单台 IP，需要写成 `192.168.1.50/32`。

### 4.4 启动数据库

```bash
systemctl enable --now postgresql-17
systemctl status postgresql-17 --no-pager
ss -lntp | grep 5432
```

确认版本和预加载：

```bash
sudo -iu postgres /usr/pgsql-17/bin/psql -c "SELECT version();"
sudo -iu postgres /usr/pgsql-17/bin/psql -c "SHOW data_directory;"
sudo -iu postgres /usr/pgsql-17/bin/psql -c "SHOW shared_preload_libraries;"
```

## 5. 建库、账号、schema

### 5.1 创建账号和数据库

```bash
sudo -iu postgres /usr/pgsql-17/bin/psql <<'SQL'
CREATE ROLE sid_import_user LOGIN PASSWORD '请替换为导入账号密码';
CREATE ROLE sid_readonly_user LOGIN PASSWORD '请替换为只读账号密码';

CREATE DATABASE "TraceData"
  WITH ENCODING 'UTF8'
  TEMPLATE template0;
SQL
```

### 5.2 创建扩展和 schema

```bash
sudo -iu postgres /usr/pgsql-17/bin/psql -d "TraceData" <<'SQL'
CREATE EXTENSION IF NOT EXISTS timescaledb;
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;

CREATE SCHEMA IF NOT EXISTS data;
CREATE SCHEMA IF NOT EXISTS sid_readonly AUTHORIZATION sid_readonly_user;

REVOKE CREATE ON SCHEMA public FROM PUBLIC;

GRANT CONNECT ON DATABASE "TraceData" TO sid_import_user;
GRANT CONNECT ON DATABASE "TraceData" TO sid_readonly_user;
GRANT TEMP ON DATABASE "TraceData" TO sid_readonly_user;

GRANT USAGE ON SCHEMA data TO sid_import_user;
GRANT USAGE ON SCHEMA data TO sid_readonly_user;
GRANT USAGE, CREATE ON SCHEMA sid_readonly TO sid_readonly_user;

ALTER ROLE sid_import_user IN DATABASE "TraceData" SET search_path = data, public;
ALTER ROLE sid_readonly_user IN DATABASE "TraceData" SET search_path = data, sid_readonly, public;
SQL
```

确认 TimescaleDB 版本：

```bash
sudo -iu postgres /usr/pgsql-17/bin/psql -d "TraceData" <<'SQL'
SELECT extname, extversion
FROM pg_extension
WHERE extname IN ('timescaledb', 'pg_stat_statements')
ORDER BY extname;
SQL
```

## 6. 建表和超表

历史数据有 13 亿多行，首次全量导入建议先不建主查询索引，导入完成后再建。

### 6.1 创建导入日志表

```bash
sudo -iu postgres /usr/pgsql-17/bin/psql -d "TraceData" <<'SQL'
CREATE TABLE IF NOT EXISTS data.import_log (
    id BIGSERIAL PRIMARY KEY,
    source_file_path TEXT NOT NULL UNIQUE,
    line VARCHAR(10),
    status VARCHAR(20) NOT NULL,
    row_count INTEGER,
    error_msg TEXT,
    started_at TIMESTAMP(0),
    imported_at TIMESTAMP(0)
);

CREATE INDEX IF NOT EXISTS idx_import_log_status
ON data.import_log (status);

CREATE INDEX IF NOT EXISTS idx_import_log_status_imported_at
ON data.import_log (status, imported_at DESC);

CREATE INDEX IF NOT EXISTS idx_import_log_line_status
ON data.import_log (line, status);
SQL
```

### 6.2 创建业务表

```bash
sudo -iu postgres /usr/pgsql-17/bin/psql -d "TraceData" <<'SQL'
CREATE TABLE IF NOT EXISTS data.did_query (
    line VARCHAR(10) NOT NULL,
    csv_date TIMESTAMP(0) WITHOUT TIME ZONE NOT NULL,
    did TEXT,
    gcode TEXT
);
SQL
```

字段含义：

| 字段 | 说明 |
| --- | --- |
| `line` | 线别，例如 `L08` |
| `csv_date` | 从目录和文件名解析出的小时级时间 |
| `did` | CSV 第 3 列 |
| `gcode` | CSV 第 4 列 |

### 6.3 转成 TimescaleDB 超表

```bash
sudo -iu postgres /usr/pgsql-17/bin/psql -d "TraceData" <<'SQL'
SELECT create_hypertable(
    'data.did_query',
    by_range('csv_date', INTERVAL '1 day'),
    if_not_exists => TRUE
);
SQL
```

确认超表：

```bash
sudo -iu postgres /usr/pgsql-17/bin/psql -d "TraceData" <<'SQL'
SELECT hypertable_schema, hypertable_name, num_dimensions, num_chunks
FROM timescaledb_information.hypertables
WHERE hypertable_schema = 'data'
  AND hypertable_name = 'did_query';
SQL
```

### 6.4 授权

```bash
sudo -iu postgres /usr/pgsql-17/bin/psql -d "TraceData" <<'SQL'
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA data TO sid_import_user;
GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA data TO sid_import_user;

GRANT SELECT ON ALL TABLES IN SCHEMA data TO sid_readonly_user;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA data TO sid_readonly_user;

ALTER DEFAULT PRIVILEGES IN SCHEMA data
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO sid_import_user;

ALTER DEFAULT PRIVILEGES IN SCHEMA data
GRANT SELECT ON TABLES TO sid_readonly_user;
SQL
```

### 6.5 全量导入后建主查询索引

全量导入完成后再执行：

```bash
sudo -iu postgres /usr/pgsql-17/bin/psql -d "TraceData" <<'SQL'
CREATE INDEX IF NOT EXISTS idx_did_query_main_query
ON data.did_query (gcode, line, csv_date)
INCLUDE (did);

ANALYZE data.did_query;
ANALYZE data.import_log;
SQL
```

如果导入完成后仍有查询在跑，可以用并发建索引：

```bash
sudo -iu postgres /usr/pgsql-17/bin/psql -d "TraceData" <<'SQL'
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_did_query_main_query
ON data.did_query (gcode, line, csv_date)
INCLUDE (did);
SQL
```

## 7. 部署导入程序

导入程序在 Windows 服务器上执行，需要同时访问：

- CSV 共享目录：`\\192.168.1.28\TraceDate`
- 数据库服务器：`<数据库服务器IP>:5432`

### 7.1 准备 Python 环境

```powershell
cd /d D:\csv-import-postgresql

py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

如果 Python 依赖也离线安装：

```powershell
cd /d D:\csv-import-postgresql

py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --no-index --find-links .\offline_packages -r requirements.txt
```

检查脚本语法：

```powershell
.\.venv\Scripts\python.exe -m py_compile .\import_sid_csv.py
```

### 7.2 配置 `config.json`

```powershell
cd /d D:\csv-import-postgresql
copy .\config.example.json .\config.json
notepad .\config.json
```

配置示例：

```json
{
  "database": {
    "type": "postgresql",
    "host": "<数据库服务器IP>",
    "port": 5432,
    "name": "TraceData",
    "user": "sid_import_user",
    "password": "请替换为导入账号密码",
    "connect_timeout_seconds": 10,
    "schema": "data",
    "import_log_table": "import_log",
    "data_table": "did_query"
  },
  "csv": {
    "root": "\\\\192.168.1.28\\TraceDate",
    "has_header": false,
    "encodings": ["utf-8-sig", "gbk", "utf-8"],
    "expected_line_dirs": [
      "PT-L01",
      "PT-L02",
      "PT-L03",
      "PT-L04",
      "PT-L05",
      "PT-L06",
      "PT-L07",
      "PT-L08"
    ]
  },
  "columns": {
    "mode": "selected",
    "selected": [
      {
        "target": "did",
        "source_column": 3
      },
      {
        "target": "gcode",
        "source_column": 4
      }
    ]
  },
  "import": {
    "write_method": "auto",
    "data_batch_size": 20000,
    "log_batch_size": 5000,
    "scan_progress_every": 1000,
    "success_progress_every": 1,
    "file_stable_minutes": 5,
    "audit_start_date": "2021-01-01",
    "incremental_lookback_days": 3,
    "audit_missing_files": true
  }
}
```

### 7.3 验证网络访问

```powershell
Test-Path "\\192.168.1.28\TraceDate"
Test-NetConnection <数据库服务器IP> -Port 5432
```

如果共享目录需要账号：

```powershell
net use \\192.168.1.28\TraceDate /user:<域或机器名\用户名> *
```

定时任务建议使用 UNC 路径，不建议依赖映射盘符。

## 8. 导入数据

### 8.1 小范围测试

先跑一次增量，确认连接、权限、配置都正常：

```powershell
cd /d D:\csv-import-postgresql
.\.venv\Scripts\python.exe .\import_sid_csv.py --config .\config.json --mode incremental
```

数据库侧检查：

```bash
sudo -iu postgres /usr/pgsql-17/bin/psql -d "TraceData" <<'SQL'
SELECT status, count(*)
FROM data.import_log
GROUP BY status
ORDER BY status;

SELECT count(*) FROM data.did_query;
SQL
```

### 8.2 全量导入

确认 `config.json` 中：

```json
"audit_start_date": "2021-01-01"
```

执行全量：

```powershell
cd /d D:\csv-import-postgresql
.\.venv\Scripts\python.exe .\import_sid_csv.py --config .\config.json --mode full
```

导入过程中可在数据库服务器查看进度：

```bash
sudo -iu postgres /usr/pgsql-17/bin/psql -d "TraceData" <<'SQL'
SELECT now(), count(*) AS did_rows FROM data.did_query;

SELECT status, count(*)
FROM data.import_log
GROUP BY status
ORDER BY status;
SQL
```

全量完成后建索引：

```bash
sudo -iu postgres /usr/pgsql-17/bin/psql -d "TraceData" <<'SQL'
CREATE INDEX IF NOT EXISTS idx_did_query_main_query
ON data.did_query (gcode, line, csv_date)
INCLUDE (did);

ANALYZE data.did_query;
ANALYZE data.import_log;
SQL
```

### 8.3 增量导入

手动执行：

```powershell
cd /d D:\csv-import-postgresql
.\.venv\Scripts\python.exe .\import_sid_csv.py --config .\config.json --mode incremental
```

Windows 定时任务动作建议：

```text
程序：
D:\csv-import-postgresql\.venv\Scripts\python.exe

参数：
D:\csv-import-postgresql\import_sid_csv.py --config D:\csv-import-postgresql\config.json --mode incremental

起始于：
D:\csv-import-postgresql
```

查看任务：

```powershell
Get-ScheduledTask | Where-Object {$_.TaskName -like "*SID*" -or $_.TaskName -like "*CSV*" -or $_.TaskName -like "*import*"} | Format-Table TaskName, State, TaskPath
Get-ScheduledTaskInfo -TaskName "<任务名称>"
```

## 9. 压缩数据

建议在全量导入、索引创建、查询验证完成后再启用压缩。

### 9.1 启用压缩配置

```bash
sudo -iu postgres /usr/pgsql-17/bin/psql -d "TraceData" <<'SQL'
ALTER TABLE data.did_query SET (
  timescaledb.compress,
  timescaledb.compress_segmentby = 'line,gcode',
  timescaledb.compress_orderby = 'csv_date DESC'
);
SQL
```

### 9.2 添加自动压缩策略

示例：压缩 7 天前的数据。

```bash
sudo -iu postgres /usr/pgsql-17/bin/psql -d "TraceData" <<'SQL'
SELECT add_compression_policy('data.did_query', INTERVAL '7 days', if_not_exists => TRUE);
SQL
```

### 9.3 手动压缩历史 chunk

```bash
sudo -iu postgres /usr/pgsql-17/bin/psql -d "TraceData" <<'SQL'
SELECT compress_chunk(chunk_name)
FROM show_chunks('data.did_query', older_than => INTERVAL '7 days') AS chunk_name;
SQL
```

### 9.4 查看压缩状态和空间

```bash
sudo -iu postgres /usr/pgsql-17/bin/psql -d "TraceData" <<'SQL'
SELECT *
FROM timescaledb_information.compressed_chunks
WHERE hypertable_name = 'did_query'
ORDER BY chunk_name
LIMIT 50;

SELECT hypertable_schema,
       hypertable_name,
       pg_size_pretty(total_bytes) AS total,
       pg_size_pretty(index_bytes) AS indexes,
       pg_size_pretty(toast_bytes) AS toast
FROM hypertable_detailed_size('data.did_query');
SQL
```

如果当前 TimescaleDB 2.28.1 包提示使用 columnstore / Hypercore 新接口，再按实际版本提示调整压缩 API。

## 10. 查询验证

只读账号连接：

```powershell
$env:PGPASSWORD = "只读账号密码"
psql -h <数据库服务器IP> -p 5432 -U sid_readonly_user -d TraceData -c "SELECT current_user, current_database();"
Remove-Item Env:\PGPASSWORD
```

典型查询：

```sql
SELECT line, csv_date, did, gcode
FROM data.did_query
WHERE gcode = '<GCODE>'
  AND line = 'L08'
  AND csv_date >= '2023-08-02 00:00:00'
  AND csv_date <  '2023-08-03 00:00:00'
LIMIT 50;
```

查看执行计划：

```bash
sudo -iu postgres /usr/pgsql-17/bin/psql -d "TraceData" <<'SQL'
EXPLAIN (ANALYZE, BUFFERS)
SELECT line, csv_date, did, gcode
FROM data.did_query
WHERE gcode = '<GCODE>'
  AND line = 'L08'
  AND csv_date >= '2023-08-02 00:00:00'
  AND csv_date <  '2023-08-03 00:00:00'
LIMIT 50;
SQL
```

重点看是否使用 `idx_did_query_main_query` 或 TimescaleDB chunk 上的对应索引。
