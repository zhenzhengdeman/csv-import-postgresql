# SIDTrace CSV 入库系统部署文档（Rocky 9 + PostgreSQL 17.10 + TimescaleDB 2.28.1）

> 当前版本：详细命令版初稿。本文按测试虚拟机部署场景编写：Rocky 9 x86_64、内网离线环境、使用公司内部 repo 解决依赖、PostgreSQL 17.10、TimescaleDB 2.28.1、数据库和导入程序不同机部署。

## 1. 本次测试环境已确认信息

| 项目 | 本次测试值 |
| --- | --- |
| 操作系统 | Rocky Linux 9 |
| CPU 架构 | x86_64 |
| 网络环境 | 完全离线，但可访问公司内部 repo |
| 数据库版本 | PostgreSQL 17.10 |
| TimescaleDB 版本 | TimescaleDB 2.28.1，对应 PostgreSQL 17 |
| PostgreSQL / TimescaleDB 安装包 | 自己已事先下载好 |
| 其他依赖包 | 使用公司内部 repo |
| 数据库服务器 | 测试虚拟机 |
| CSV 文件位置 | 远端文件服务器 |
| 数据库备份 | 当前没有备份 |
| 导入程序位置 | 与数据库不同机 |
| 导入程序网络要求 | 同时能访问 CSV 文件服务器和 PostgreSQL 数据库 |
| 历史数据量 | 13 亿多行 |
| 查询条件 | 基本固定为 `gcode + line + csv_date` |
| 增量任务 | 已配置 Windows 定时任务 |
| 查询账号 | 无论如何都建议使用只读账号查询 |

## 2. 部署变量

后续命令里会使用下面这些变量。执行前请按现场实际值替换。

### 2.1 数据库服务器变量

```bash
export DB_NAME="TraceData"
export DB_SCHEMA="data"
export DB_PORT="5432"

export PG_MAJOR="17"
export PG_VERSION_EXPECTED="17.10"
export TIMESCALEDB_VERSION_EXPECTED="2.28.1"

# 使用公司 repo 的 RPM 安装时，PostgreSQL 程序目录通常固定为 /usr/pgsql-17。
# 本文所谓自定义安装目录，重点是自定义数据目录、WAL 目录、日志目录、配置备份目录。
export PGHOME="/usr/pgsql-17"
export PGROOT="/data/postgresql/17.10"
export PGDATA="${PGROOT}/data"
export PGWAL="${PGROOT}/pg_wal"
export PGLOG="${PGROOT}/log"
export PGCONFBAK="${PGROOT}/conf-backup"

# 替换为数据库虚拟机内网 IP。
export DB_IP="<数据库服务器IP>"

# 替换为导入程序所在 Windows 服务器 IP。
export IMPORT_SERVER_IP="<导入服务器IP>"

# 替换为允许使用只读账号查询的网段或单机 IP。
# 单台机器写法：192.168.1.50/32
# 一个网段写法：192.168.1.0/24
export QUERY_CLIENT_CIDR="<查询客户端网段或IP>"
```

### 2.2 公司 repo 变量

公司内部 repo 的 ID 需要先通过 `dnf repolist all` 查看。下面只是示例名，现场执行时要替换。

```bash
export COMPANY_BASE_REPO="<公司Rocky9基础repo-id>"
export COMPANY_DEPS_REPO="<公司依赖repo-id>"
export LOCAL_RPM_DIR="/opt/offline-packages/pgsql17-timescaledb-rpms"
```

本次约定：

- PostgreSQL 17.10 和 TimescaleDB 2.28.1 的 RPM 包由自己提前下载，放到数据库服务器本地目录。
- 公司内部 repo 只用于补齐 Rocky 9 系统依赖包。
- 如果公司基础依赖和通用依赖在同一个 repo，后续命令里的 `--enablerepo="${COMPANY_BASE_REPO}" --enablerepo="${COMPANY_DEPS_REPO}"` 可以改成只启用一个 repo。

### 2.3 导入服务器变量（Windows）

```powershell
$DbHost = "<数据库服务器IP>"
$DbPort = 5432
$DbName = "TraceData"
$ImportUser = "sid_import_user"
$ReadonlyUser = "sid_readonly_user"

$CsvRoot = "\\192.168.1.28\TraceDate"
$ProjectDir = "D:\csv-import-postgresql"
$ConfigFile = "D:\csv-import-postgresql\config.json"
```

## 3. 部署架构

```text
远端 CSV 文件服务器
  \\192.168.1.28\TraceDate
        |
        | SMB / Windows 共享访问
        v
Windows 导入服务器
  D:\csv-import-postgresql
  import_sid_csv.py
  Windows 定时任务
        |
        | TCP 5432
        v
Rocky 9 测试数据库虚拟机
  PostgreSQL 17.10
  TimescaleDB 2.28.1
  TraceData.data.did_query
  TraceData.data.import_log
```

关键点：

- CSV 文件不在数据库服务器本机，导入程序必须能访问远端共享目录。
- 导入程序和数据库不同机，所以 PostgreSQL 必须允许导入服务器远程连接。
- 查询统一使用只读账号，不直接使用 `postgres` 或导入账号查询。
- 历史数据 13 亿多行，首次全量导入前建议先不建主查询索引，导完后再建索引。
- 当前没有备份，所以至少要先做配置备份；数据备份方案后续必须补。

## 4. 数据库服务器安装前检查

以下命令在 Rocky 9 数据库虚拟机上执行。

### 4.1 切换 root

```bash
sudo -i
```

如果当前已经是 `root`，可直接继续。

### 4.2 确认系统版本和架构

```bash
cat /etc/rocky-release
cat /etc/os-release
uname -m
hostnamectl
```

期望结果：

```text
Rocky Linux release 9.x
x86_64
```

### 4.3 确认时间、时区和同步状态

```bash
date
timedatectl
```

建议时区：

```bash
timedatectl set-timezone Asia/Shanghai
timedatectl
```

如果内网有 NTP 服务器，补充配置：

```bash
chronyc sources -v
chronyc tracking
```

### 4.4 确认磁盘和挂载点

```bash
lsblk -f
df -hT
mount | column -t
```

建议检查：

- `/data` 是否已经挂载到数据盘。
- `/data` 剩余空间是否足够容纳 13 亿多行数据、索引和后续增长。
- 如果没有 `/data`，需要先让系统管理员挂载数据盘。

创建基础目录前先确认：

```bash
test -d /data && echo "/data exists" || echo "/data not exists"
```

### 4.5 确认 IP 和网络

```bash
ip addr
ip route
ss -lntp
```

从数据库服务器测试是否能访问公司内部 repo：

```bash
dnf repolist all
```

从导入服务器测试能否访问数据库服务器，等 PostgreSQL 启动后再执行：

```powershell
Test-NetConnection <数据库服务器IP> -Port 5432
```

### 4.6 确认 SELinux 和防火墙

```bash
getenforce
sestatus
systemctl status firewalld --no-pager
firewall-cmd --state
```

说明：

- 如果 SELinux 为 `Enforcing`，自定义数据目录需要设置 SELinux 文件上下文。
- 如果是测试虚拟机，也可以临时用 `Permissive` 排查问题，但文档默认按不关闭 SELinux 的方式写。

## 5. 公司内部 repo 检查

### 5.1 查看 repo ID

```bash
dnf repolist all
ls -l /etc/yum.repos.d/
```

记录能使用的 repo ID，例如：

```text
company-rocky9-baseos
company-rocky9-appstream
company-pg17-timescaledb
```

然后设置变量：

```bash
export COMPANY_BASE_REPO="<公司Rocky9基础repo-id>"
export COMPANY_DEPS_REPO="<公司依赖repo-id>"
export LOCAL_RPM_DIR="/opt/offline-packages/pgsql17-timescaledb-rpms"
```

### 5.2 禁用 Rocky 自带 PostgreSQL 模块

Rocky 9 AppStream 里可能有发行版自带 PostgreSQL 模块。为了避免装错版本，先禁用系统模块。

```bash
dnf -qy module disable postgresql
dnf module list postgresql
```

### 5.3 刷新公司 repo 缓存

```bash
dnf clean all
dnf makecache \
  --disablerepo='*' \
  --enablerepo="${COMPANY_BASE_REPO}" \
  --enablerepo="${COMPANY_DEPS_REPO}"
```

如果公司 repo 不允许 `--disablerepo='*'` 这种方式，就先用普通方式刷新：

```bash
dnf clean all
dnf makecache
```

### 5.4 检查本地 PostgreSQL 和 TimescaleDB RPM 包

```bash
export LOCAL_RPM_DIR="/opt/offline-packages/pgsql17-timescaledb-rpms"
mkdir -p "${LOCAL_RPM_DIR}"
cd "${LOCAL_RPM_DIR}"
ls -lh
```

如果 RPM 包还没上传到这个目录，先从离线介质复制进去，然后再检查版本：

```bash
rpm -qp --qf '%{NAME} %{VERSION}-%{RELEASE} %{ARCH}\n' "${LOCAL_RPM_DIR}"/*.rpm | sort
```

重点确认：

- `postgresql17-server` 版本是 `17.10`。
- `postgresql17` 版本是 `17.10`。
- 如有 `postgresql17-contrib`，版本也是 `17.10`。
- `timescaledb-2-postgresql-17` 版本是 `2.28.1`。
- TimescaleDB 包名必须对应 PostgreSQL 17，不能是 PostgreSQL 16 或 PostgreSQL 18。

查看这些本地 RPM 需要哪些依赖：

```bash
rpm -qpR "${LOCAL_RPM_DIR}"/*.rpm | sort -u | less
```

依赖不需要手工逐个下载，本次由公司内部 repo 解决。

## 6. 安装 PostgreSQL 17.10 和 TimescaleDB 2.28.1

本次环境明确为：

- PostgreSQL 17.10 RPM 包：自己提前下载。
- TimescaleDB 2.28.1 for PostgreSQL 17 RPM 包：自己提前下载。
- 其他依赖：从公司内部 repo 安装。

所以本章只写“本地 RPM + 公司 repo 依赖”的安装方式。

### 6.1 确认本地 RPM 包目录

```bash
export LOCAL_RPM_DIR="/opt/offline-packages/pgsql17-timescaledb-rpms"
cd "${LOCAL_RPM_DIR}"
ls -lh
```

至少应包含 PostgreSQL 服务端、客户端、库、常用扩展以及 TimescaleDB PG17 包。示例：

```text
postgresql17-17.10-*.x86_64.rpm
postgresql17-libs-17.10-*.x86_64.rpm
postgresql17-server-17.10-*.x86_64.rpm
postgresql17-contrib-17.10-*.x86_64.rpm
timescaledb-2-postgresql-17-2.28.1-*.x86_64.rpm
```

如果还额外下载了 `timescaledb-tools`，也一起放在这个目录。

### 6.2 再次核对 RPM 版本

```bash
rpm -qp --qf '%{NAME} %{VERSION}-%{RELEASE} %{ARCH}\n' "${LOCAL_RPM_DIR}"/*.rpm | sort
```

期望看到类似内容：

```text
postgresql17 17.10-... x86_64
postgresql17-contrib 17.10-... x86_64
postgresql17-libs 17.10-... x86_64
postgresql17-server 17.10-... x86_64
timescaledb-2-postgresql-17 2.28.1-... x86_64
```

如果这里看到的是 PostgreSQL 16、PostgreSQL 18、aarch64 或 TimescaleDB 非 PG17 包，停止安装，重新换包。

### 6.3 用本地 RPM 安装，依赖走公司源

使用 `dnf install ./*.rpm`，这样本地 RPM 是主安装包，缺少的系统依赖由公司内部 repo 补齐。

```bash
dnf install -y \
  --disablerepo='*' \
  --enablerepo="${COMPANY_BASE_REPO}" \
  --enablerepo="${COMPANY_DEPS_REPO}" \
  "${LOCAL_RPM_DIR}"/*.rpm
```

如果公司依赖 repo 和基础 repo 是同一个，就改成：

```bash
dnf install -y \
  --disablerepo='*' \
  --enablerepo="${COMPANY_BASE_REPO}" \
  "${LOCAL_RPM_DIR}"/*.rpm
```

如果 dnf 报某个依赖找不到，先不要用 `rpm -ivh --nodeps` 硬装，应把缺失依赖包补进公司 repo 或本地依赖目录。

### 6.4 安装后验证

```bash
rpm -qa | grep -E 'postgresql17|timescaledb' | sort
/usr/pgsql-17/bin/postgres --version
/usr/pgsql-17/bin/psql --version
```

期望：

```text
postgres (PostgreSQL) 17.10
psql (PostgreSQL) 17.10
```

检查 TimescaleDB 扩展文件：

```bash
find /usr/pgsql-17 -iname '*timescale*' | sort | head -50
rpm -ql timescaledb-2-postgresql-17 | head -80
```

## 7. 创建自定义目录

说明：

- 使用 RPM 安装时，程序目录一般固定为 `/usr/pgsql-17`。
- 自定义目录主要指数据库数据、WAL、日志和配置备份目录。
- 本文使用 `/data/postgresql/17.10` 作为测试目录，可按现场磁盘规划调整。

### 7.1 设置目录变量

```bash
export PGROOT="/data/postgresql/17.10"
export PGDATA="${PGROOT}/data"
export PGWAL="${PGROOT}/pg_wal"
export PGLOG="${PGROOT}/log"
export PGCONFBAK="${PGROOT}/conf-backup"
```

### 7.2 创建目录

```bash
mkdir -p "${PGDATA}" "${PGWAL}" "${PGLOG}" "${PGCONFBAK}"
chown -R postgres:postgres "${PGROOT}"
chmod 700 "${PGDATA}" "${PGWAL}"
chmod 750 "${PGROOT}" "${PGLOG}" "${PGCONFBAK}"
ls -ld "${PGROOT}" "${PGDATA}" "${PGWAL}" "${PGLOG}" "${PGCONFBAK}"
```

### 7.3 SELinux 文件上下文

如果 `getenforce` 是 `Enforcing`，执行：

```bash
dnf install -y \
  --disablerepo='*' \
  --enablerepo="${COMPANY_BASE_REPO}" \
  policycoreutils-python-utils
```

设置 PostgreSQL 自定义目录上下文：

```bash
semanage fcontext -a -t postgresql_db_t "${PGROOT}(/.*)?"
restorecon -Rv "${PGROOT}"
ls -Zd "${PGROOT}" "${PGDATA}" "${PGWAL}" "${PGLOG}"
```

如果 `semanage fcontext -a` 提示规则已存在，改用：

```bash
semanage fcontext -m -t postgresql_db_t "${PGROOT}(/.*)?"
restorecon -Rv "${PGROOT}"
```

测试环境临时排查 SELinux 问题时可以执行：

```bash
setenforce 0
getenforce
```

但这只是临时排查，不建议作为正式方案。

## 8. 初始化 PostgreSQL 数据目录

### 8.1 确认 locale

```bash
locale -a | grep -Ei 'c\.utf|en_US\.utf|zh_CN\.utf|C$'
```

建议优先使用 `C.UTF-8` 或 `C.utf8`。如果系统没有，可先用 `C`。

```bash
export DB_LOCALE="C.UTF-8"
locale -a | grep -Fx "${DB_LOCALE}" || export DB_LOCALE="C"
echo "${DB_LOCALE}"
```

### 8.2 执行 initdb

```bash
sudo -iu postgres /usr/pgsql-17/bin/initdb \
  -D "${PGDATA}" \
  --waldir="${PGWAL}" \
  --encoding=UTF8 \
  --locale="${DB_LOCALE}" \
  --auth-local=peer \
  --auth-host=scram-sha-256
```

检查初始化结果：

```bash
ls -lah "${PGDATA}" | head
ls -lah "${PGWAL}" | head
test -f "${PGDATA}/postgresql.conf" && echo "postgresql.conf exists"
test -f "${PGDATA}/pg_hba.conf" && echo "pg_hba.conf exists"
```

## 9. 配置 systemd 使用自定义 PGDATA

PGDG RPM 的服务名通常是 `postgresql-17.service`。

### 9.1 查看服务文件

```bash
systemctl cat postgresql-17
```

### 9.2 创建 systemd override

```bash
mkdir -p /etc/systemd/system/postgresql-17.service.d
cat > /etc/systemd/system/postgresql-17.service.d/override.conf <<EOF
[Service]
Environment=PGDATA=${PGDATA}
EOF
```

重新加载 systemd：

```bash
systemctl daemon-reload
systemctl cat postgresql-17
```

确认输出里能看到：

```text
Environment=PGDATA=/data/postgresql/17.10/data
```

## 10. 配置 postgresql.conf

### 10.1 备份原配置

```bash
cp -a "${PGDATA}/postgresql.conf" "${PGCONFBAK}/postgresql.conf.$(date +%Y%m%d_%H%M%S).bak"
ls -lh "${PGCONFBAK}"
```

### 10.2 追加 SIDTrace 测试配置

下面是测试虚拟机的保守起点。正式环境要按 CPU、内存、磁盘重新调整。

```bash
cat >> "${PGDATA}/postgresql.conf" <<'EOF'

# --- SIDTrace test deployment settings ---
listen_addresses = '*'
port = 5432

password_encryption = 'scram-sha-256'

# TimescaleDB 必须预加载；pg_stat_statements 用于后续慢 SQL 分析。
shared_preload_libraries = 'timescaledb,pg_stat_statements'

# 测试虚拟机保守起点，后续按内存调整。
max_connections = 200
shared_buffers = '4GB'
effective_cache_size = '12GB'
work_mem = '16MB'
maintenance_work_mem = '1GB'

# 13 亿多行全量导入期间，WAL 和 checkpoint 不宜太小。
checkpoint_completion_target = 0.9
max_wal_size = '16GB'
min_wal_size = '4GB'
wal_compression = on

# 日志。
logging_collector = on
log_directory = '/data/postgresql/17.10/log'
log_filename = 'postgresql-%Y-%m-%d_%H%M%S.log'
log_rotation_age = '1d'
log_rotation_size = '1GB'
log_min_duration_statement = '5s'
log_line_prefix = '%m [%p] %u@%d %r '

timezone = 'Asia/Shanghai'
log_timezone = 'Asia/Shanghai'

# 后续如发现 autovacuum 跟不上，再单独调大。
autovacuum = on
track_io_timing = on

# --- end SIDTrace settings ---
EOF
```

### 10.3 检查配置关键项

```bash
grep -nE "listen_addresses|port|shared_preload_libraries|shared_buffers|log_directory|max_wal_size|timezone" "${PGDATA}/postgresql.conf"
```

如果测试虚拟机内存不足 16 GB，请先把内存参数调小，例如：

```bash
sed -i "s/shared_buffers = '4GB'/shared_buffers = '1GB'/" "${PGDATA}/postgresql.conf"
sed -i "s/effective_cache_size = '12GB'/effective_cache_size = '3GB'/" "${PGDATA}/postgresql.conf"
sed -i "s/maintenance_work_mem = '1GB'/maintenance_work_mem = '512MB'/" "${PGDATA}/postgresql.conf"
```

查看内存：

```bash
free -h
```

## 11. 配置 pg_hba.conf

### 11.1 备份原配置

```bash
cp -a "${PGDATA}/pg_hba.conf" "${PGCONFBAK}/pg_hba.conf.$(date +%Y%m%d_%H%M%S).bak"
```

### 11.2 追加连接规则

把 `<导入服务器IP>` 和 `<查询客户端网段或IP>` 替换成真实值。

```bash
cat >> "${PGDATA}/pg_hba.conf" <<EOF

# --- SIDTrace test deployment rules ---
# 本机维护连接。
local   all             postgres                                peer
local   all             all                                     peer
host    all             all             127.0.0.1/32            scram-sha-256
host    all             all             ::1/128                 scram-sha-256

# 导入服务器只能用导入账号连接业务库。
host    ${DB_NAME}      sid_import_user  ${IMPORT_SERVER_IP}/32  scram-sha-256

# 查询统一使用只读账号。
host    ${DB_NAME}      sid_readonly_user ${QUERY_CLIENT_CIDR}   scram-sha-256

# 如需要 DBA 远程维护，单独添加 DBA 机器 IP，不建议开放整个网段。
# host  all            postgres          <DBA_IP>/32            scram-sha-256
# --- end SIDTrace rules ---
EOF
```

检查：

```bash
tail -40 "${PGDATA}/pg_hba.conf"
```

## 12. 配置防火墙

### 12.1 放通导入服务器访问 5432

```bash
firewall-cmd --permanent \
  --add-rich-rule="rule family='ipv4' source address='${IMPORT_SERVER_IP}/32' port protocol='tcp' port='5432' accept"
```

### 12.2 放通只读查询客户端

如果是单台查询客户端：

```bash
firewall-cmd --permanent \
  --add-rich-rule="rule family='ipv4' source address='<查询客户端IP>/32' port protocol='tcp' port='5432' accept"
```

如果是一个内网网段：

```bash
firewall-cmd --permanent \
  --add-rich-rule="rule family='ipv4' source address='<查询客户端网段>/24' port protocol='tcp' port='5432' accept"
```

### 12.3 重载并检查

```bash
firewall-cmd --reload
firewall-cmd --list-all
firewall-cmd --list-rich-rules
```

## 13. 启动 PostgreSQL

### 13.1 启动并设置开机自启

```bash
systemctl enable --now postgresql-17
```

### 13.2 查看服务状态

```bash
systemctl status postgresql-17 --no-pager
journalctl -u postgresql-17 -n 100 --no-pager
```

### 13.3 查看端口监听

```bash
ss -lntp | grep 5432
```

期望能看到 `0.0.0.0:5432` 或数据库内网 IP 的 `5432` 监听。

### 13.4 数据库本机连接测试

```bash
sudo -iu postgres /usr/pgsql-17/bin/psql -c "SELECT version();"
sudo -iu postgres /usr/pgsql-17/bin/psql -c "SHOW data_directory;"
sudo -iu postgres /usr/pgsql-17/bin/psql -c "SHOW shared_preload_libraries;"
```

期望：

- `SELECT version();` 包含 PostgreSQL 17.10。
- `SHOW data_directory;` 返回 `/data/postgresql/17.10/data`。
- `SHOW shared_preload_libraries;` 包含 `timescaledb`。

## 14. 创建数据库、账号和扩展

本次按最小账号模型：

| 账号 | 用途 | 原则 |
| --- | --- | --- |
| `postgres` | 初始化和 DBA 维护 | 不给业务日常使用 |
| `sid_import_user` | CSV 导入程序 | 只给业务库写入权限 |
| `sid_readonly_user` | 所有查询 | 只读查询，可使用临时表 |

### 14.1 创建角色和数据库

测试环境可先用明文占位密码。正式环境不要把真实密码写进脚本和聊天记录。

```bash
sudo -iu postgres /usr/pgsql-17/bin/psql <<'SQL'
CREATE ROLE sid_import_user LOGIN PASSWORD '请替换为导入账号强密码';
CREATE ROLE sid_readonly_user LOGIN PASSWORD '请替换为只读账号强密码';

CREATE DATABASE "TraceData"
  WITH ENCODING 'UTF8'
  TEMPLATE template0;
SQL
```

如果角色已存在，使用下面命令重置密码：

```bash
sudo -iu postgres /usr/pgsql-17/bin/psql <<'SQL'
ALTER ROLE sid_import_user LOGIN PASSWORD '请替换为导入账号强密码';
ALTER ROLE sid_readonly_user LOGIN PASSWORD '请替换为只读账号强密码';
SQL
```

### 14.2 创建扩展和 schema

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

### 14.3 验证 TimescaleDB 版本

```bash
sudo -iu postgres /usr/pgsql-17/bin/psql -d "TraceData" <<'SQL'
SELECT name, default_version, installed_version
FROM pg_available_extensions
WHERE name IN ('timescaledb', 'pg_stat_statements');

SELECT extname, extversion
FROM pg_extension
WHERE extname IN ('timescaledb', 'pg_stat_statements');
SQL
```

期望 `timescaledb` 的 `installed_version` 为 `2.28.1`。

## 15. 初始化业务表结构

当前实际脚本对应的业务 schema 是 `data`，核心表是：

- `data.did_query`：存放按 `line`、`csv_date`、`did`、`gcode` 提取后的查询数据。
- `data.import_log`：记录每个 CSV 文件的导入状态，防止重复导入。

### 15.1 首次全量导入前的建表建议

历史数据 13 亿多行，首次全量导入建议分两步：

1. 先建表和 hypertable，不建 `idx_did_query_main_query` 主查询索引。
2. 全量导入完成后再建 `gcode + line + csv_date INCLUDE (did)` 索引。

这样首次导入更快，避免每插入一批数据都维护大索引。

### 15.2 建表但暂不建主查询索引

```bash
sudo -iu postgres /usr/pgsql-17/bin/psql -d "TraceData" <<'SQL'
CREATE SCHEMA IF NOT EXISTS data;

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

CREATE TABLE IF NOT EXISTS data.did_query (
    line VARCHAR(10) NOT NULL,
    csv_date TIMESTAMP(0) WITHOUT TIME ZONE NOT NULL,
    did TEXT,
    gcode TEXT
);

SELECT create_hypertable(
    'data.did_query',
    by_range('csv_date', INTERVAL '1 day'),
    if_not_exists => TRUE
);

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

### 15.3 如果需要直接执行仓库里的 schema.sql

如果导入服务器或 DBA 电脑上有 `D:\csv-import-postgresql\schema.sql`，也可以用 `psql` 执行。

Windows 示例：

```powershell
$env:PGPASSWORD = "postgres管理员密码"
psql -h <数据库服务器IP> -p 5432 -U postgres -d TraceData -f D:\csv-import-postgresql\schema.sql
Remove-Item Env:\PGPASSWORD
```

注意：当前 `schema.sql` 会直接创建主查询索引。13 亿多行首次全量导入时，如果想先导入后建索引，就不要直接执行原始 `schema.sql`，应使用上一节“暂不建主查询索引”的 SQL。

### 15.4 全量导入后创建主查询索引

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

如果不想阻塞查询，可用并发建索引：

```bash
sudo -iu postgres /usr/pgsql-17/bin/psql -d "TraceData" <<'SQL'
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_did_query_main_query
ON data.did_query (gcode, line, csv_date)
INCLUDE (did);
SQL
```

说明：

- `CREATE INDEX CONCURRENTLY` 不能放在显式事务中。
- 导入期间不建议同时建这个大索引。
- 当前查询条件基本固定为 `gcode + line + csv_date`，所以该索引是核心索引。

## 16. TimescaleDB 压缩设置

之前测试结果显示，`did_query` 建成 TimescaleDB 超表后压缩效果明显。压缩建议在历史全量导入和主查询索引验证后再启用。

### 16.1 查看 hypertable

```bash
sudo -iu postgres /usr/pgsql-17/bin/psql -d "TraceData" <<'SQL'
SELECT hypertable_schema, hypertable_name, num_dimensions, num_chunks
FROM timescaledb_information.hypertables
ORDER BY hypertable_schema, hypertable_name;
SQL
```

### 16.2 启用传统 compression 配置

```bash
sudo -iu postgres /usr/pgsql-17/bin/psql -d "TraceData" <<'SQL'
ALTER TABLE data.did_query SET (
  timescaledb.compress,
  timescaledb.compress_segmentby = 'line,gcode',
  timescaledb.compress_orderby = 'csv_date DESC'
);
SQL
```

### 16.3 添加压缩策略

示例：压缩 7 天前的数据。

```bash
sudo -iu postgres /usr/pgsql-17/bin/psql -d "TraceData" <<'SQL'
SELECT add_compression_policy('data.did_query', INTERVAL '7 days', if_not_exists => TRUE);
SQL
```

如果 TimescaleDB 2.28.1 当前包提示使用新版 columnstore / Hypercore API，再按实际提示调整；本文先保留和当前测试方案一致的传统 compression 写法。

### 16.4 手动压缩历史 chunk

先查看 chunk：

```bash
sudo -iu postgres /usr/pgsql-17/bin/psql -d "TraceData" <<'SQL'
SELECT show_chunks('data.did_query', older_than => INTERVAL '7 days') LIMIT 20;
SQL
```

手动压缩所有 7 天前 chunk：

```bash
sudo -iu postgres /usr/pgsql-17/bin/psql -d "TraceData" <<'SQL'
SELECT compress_chunk(chunk_name)
FROM show_chunks('data.did_query', older_than => INTERVAL '7 days') AS chunk_name;
SQL
```

### 16.5 查看压缩效果

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

## 17. 远程连接验证

### 17.1 数据库服务器本机验证

```bash
sudo -iu postgres /usr/pgsql-17/bin/psql -d "TraceData" <<'SQL'
SELECT current_database();
SELECT current_schema();
SELECT extname, extversion FROM pg_extension ORDER BY extname;
SELECT count(*) FROM data.import_log;
SELECT count(*) FROM data.did_query;
SQL
```

### 17.2 从导入服务器验证导入账号

在 Windows 导入服务器执行：

```powershell
Test-NetConnection <数据库服务器IP> -Port 5432
```

如果安装了 PostgreSQL 客户端：

```powershell
$env:PGPASSWORD = "导入账号密码"
psql -h <数据库服务器IP> -p 5432 -U sid_import_user -d TraceData -c "SELECT current_user, current_database();"
psql -h <数据库服务器IP> -p 5432 -U sid_import_user -d TraceData -c "SELECT count(*) FROM data.import_log;"
Remove-Item Env:\PGPASSWORD
```

验证导入账号不能做超级管理操作：

```powershell
$env:PGPASSWORD = "导入账号密码"
psql -h <数据库服务器IP> -p 5432 -U sid_import_user -d TraceData -c "CREATE DATABASE should_fail;"
Remove-Item Env:\PGPASSWORD
```

期望失败。

### 17.3 从查询客户端验证只读账号

```powershell
$env:PGPASSWORD = "只读账号密码"
psql -h <数据库服务器IP> -p 5432 -U sid_readonly_user -d TraceData -c "SELECT count(*) FROM data.did_query;"
psql -h <数据库服务器IP> -p 5432 -U sid_readonly_user -d TraceData -c "INSERT INTO data.did_query(line, csv_date, did, gcode) VALUES ('L01', now(), 'x', 'x');"
Remove-Item Env:\PGPASSWORD
```

期望：

- `SELECT` 成功。
- `INSERT` 失败。

## 18. 导入服务器部署检查（Windows）

导入程序部署在 Windows 服务器，并且 Windows 定时任务已经配置。这里补齐检查命令。

### 18.1 检查项目目录

```powershell
cd /d D:\csv-import-postgresql
dir
```

应能看到：

```text
import_sid_csv.py
config.example.json
schema.sql
requirements.txt
query_examples.sql
```

### 18.2 检查 Python

```powershell
python --version
py --version
```

如果系统有多个 Python，建议固定使用一个版本：

```powershell
py -3 --version
```

### 18.3 创建虚拟环境

```powershell
cd /d D:\csv-import-postgresql
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip --version
```

### 18.4 安装依赖

如果导入服务器可访问公司内部 Python 包源：

```powershell
cd /d D:\csv-import-postgresql
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

如果完全离线，依赖包已放在 `D:\csv-import-postgresql\offline_packages`：

```powershell
cd /d D:\csv-import-postgresql
.\.venv\Scripts\python.exe -m pip install --no-index --find-links .\offline_packages -r requirements.txt
```

### 18.5 语法检查

```powershell
cd /d D:\csv-import-postgresql
.\.venv\Scripts\python.exe -m py_compile .\import_sid_csv.py
```

如果没有输出，说明语法检查通过。

### 18.6 检查 CSV 共享目录

```powershell
Test-Path "\\192.168.1.28\TraceDate"
Get-ChildItem "\\192.168.1.28\TraceDate" | Select-Object -First 20
```

如果需要凭据访问共享目录：

```powershell
net use \\192.168.1.28\TraceDate /user:<域或机器名\用户名> *
Test-Path "\\192.168.1.28\TraceDate"
```

如果要映射盘符：

```powershell
net use Z: \\192.168.1.28\TraceDate /user:<域或机器名\用户名> *
dir Z:\
```

建议配置文件仍使用 UNC 路径 `\\192.168.1.28\TraceDate`，不要依赖交互登录会话里的盘符，因为 Windows 定时任务运行时未必能看到映射盘。

### 18.7 检查导入服务器到数据库网络

```powershell
Test-NetConnection <数据库服务器IP> -Port 5432
```

`TcpTestSucceeded` 应为 `True`。

## 19. 配置导入程序

### 19.1 复制配置文件

```powershell
cd /d D:\csv-import-postgresql
copy .\config.example.json .\config.json
notepad .\config.json
```

### 19.2 配置文件示例

下面按本次部署场景填写。密码请替换，不要照抄占位文字。

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
    ],
    "full": {
      "did_column": 3,
      "gcode_column": 4,
      "other_column_prefix": "col_",
      "other_column_digits": 2
    }
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

### 19.3 配置说明

| 配置项 | 本次建议 | 说明 |
| --- | --- | --- |
| `database.host` | 数据库虚拟机 IP | 导入服务器通过这个 IP 访问 PostgreSQL |
| `database.name` | `TraceData` | 与建库命令一致 |
| `database.user` | `sid_import_user` | 导入程序不要用 `postgres` |
| `database.schema` | `data` | 与 `schema.sql` 一致 |
| `csv.root` | `\\192.168.1.28\TraceDate` | 建议使用 UNC 路径 |
| `columns.mode` | `selected` | 当前只取 DID 和 GCODE |
| `data_batch_size` | `20000` | 已拆分数据批次和日志批次 |
| `log_batch_size` | `5000` | 导入日志批次 |
| `audit_start_date` | `2021-01-01` | 历史全量开始日期 |
| `incremental_lookback_days` | `3` | Windows 定时任务增量扫描近 3 天 |

## 20. 首次导入流程

### 20.1 小范围测试导入

先确认配置能加载：

```powershell
cd /d D:\csv-import-postgresql
.\.venv\Scripts\python.exe .\import_sid_csv.py --config .\config.json --mode incremental
```

如果要先测单天或小范围，需要临时修改 `config.json` 中的 `audit_start_date`，或者临时把 CSV 测试目录指到一个很小的目录。

### 20.2 全量导入

全量导入前确认主查询索引还没建：

```bash
sudo -iu postgres /usr/pgsql-17/bin/psql -d "TraceData" <<'SQL'
SELECT indexname, indexdef
FROM pg_indexes
WHERE schemaname = 'data'
  AND tablename = 'did_query'
ORDER BY indexname;
SQL
```

如果还没有 `idx_did_query_main_query`，开始全量导入。

Windows 导入服务器执行：

```powershell
cd /d D:\csv-import-postgresql
.\.venv\Scripts\python.exe .\import_sid_csv.py --config .\config.json --mode full
```

全量导入期间建议另开窗口观察数据库：

```bash
sudo -iu postgres /usr/pgsql-17/bin/psql -d "TraceData" <<'SQL'
SELECT now(), count(*) AS did_rows FROM data.did_query;
SELECT status, count(*) FROM data.import_log GROUP BY status ORDER BY status;
SQL
```

观察连接：

```bash
sudo -iu postgres /usr/pgsql-17/bin/psql -d "TraceData" <<'SQL'
SELECT pid,
       usename,
       application_name,
       client_addr,
       state,
       wait_event_type,
       wait_event,
       now() - query_start AS running_for,
       left(query, 120) AS query_sample
FROM pg_stat_activity
WHERE datname = 'TraceData'
ORDER BY query_start NULLS LAST;
SQL
```

观察磁盘：

```bash
df -hT /data
du -sh /data/postgresql/17.10/*
```

### 20.3 全量导入后处理

全量导入完成后执行：

```bash
sudo -iu postgres /usr/pgsql-17/bin/psql -d "TraceData" <<'SQL'
SELECT count(*) AS did_rows FROM data.did_query;
SELECT status, count(*) FROM data.import_log GROUP BY status ORDER BY status;
ANALYZE data.did_query;
ANALYZE data.import_log;
SQL
```

然后创建主查询索引：

```bash
sudo -iu postgres /usr/pgsql-17/bin/psql -d "TraceData" <<'SQL'
CREATE INDEX IF NOT EXISTS idx_did_query_main_query
ON data.did_query (gcode, line, csv_date)
INCLUDE (did);
ANALYZE data.did_query;
SQL
```

如果历史数据量很大，建索引期间另开窗口观察：

```bash
sudo -iu postgres /usr/pgsql-17/bin/psql -d "TraceData" <<'SQL'
SELECT pid,
       datname,
       relid::regclass AS table_name,
       phase,
       lockers_total,
       lockers_done,
       blocks_total,
       blocks_done,
       tuples_total,
       tuples_done
FROM pg_stat_progress_create_index;
SQL
```

## 21. 增量导入和 Windows 定时任务

用户已确认 Windows 定时任务已配置。这里写检查和建议命令。

### 21.1 手动执行增量

```powershell
cd /d D:\csv-import-postgresql
.\.venv\Scripts\python.exe .\import_sid_csv.py --config .\config.json --mode incremental
```

### 21.2 查看现有定时任务

```powershell
Get-ScheduledTask | Where-Object {$_.TaskName -like "*SID*" -or $_.TaskName -like "*CSV*" -or $_.TaskName -like "*import*"} | Format-Table TaskName, State, TaskPath
```

查看某个任务详情：

```powershell
Get-ScheduledTask -TaskName "<任务名称>" | Format-List *
Get-ScheduledTaskInfo -TaskName "<任务名称>"
```

### 21.3 建议的定时任务动作

定时任务建议使用完整路径，不依赖当前目录：

```text
程序：
D:\csv-import-postgresql\.venv\Scripts\python.exe

参数：
D:\csv-import-postgresql\import_sid_csv.py --config D:\csv-import-postgresql\config.json --mode incremental

起始于：
D:\csv-import-postgresql
```

### 21.4 用命令创建定时任务示例

如果需要重建任务，可参考：

```powershell
$Action = New-ScheduledTaskAction `
  -Execute "D:\csv-import-postgresql\.venv\Scripts\python.exe" `
  -Argument "D:\csv-import-postgresql\import_sid_csv.py --config D:\csv-import-postgresql\config.json --mode incremental" `
  -WorkingDirectory "D:\csv-import-postgresql"

$Trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).Date.AddHours(1) `
  -RepetitionInterval (New-TimeSpan -Hours 1) `
  -RepetitionDuration (New-TimeSpan -Days 3650)

Register-ScheduledTask `
  -TaskName "SIDTrace CSV Incremental Import" `
  -Action $Action `
  -Trigger $Trigger `
  -Description "每小时增量导入 SIDTrace CSV 到 PostgreSQL" `
  -User "<运行任务的Windows账号>" `
  -RunLevel Highest
```

如果任务需要访问网络共享目录，运行任务的 Windows 账号必须有共享目录权限。

## 22. 查询验证

### 22.1 只读账号连接

```powershell
$env:PGPASSWORD = "只读账号密码"
psql -h <数据库服务器IP> -p 5432 -U sid_readonly_user -d TraceData -c "SELECT current_user, current_database();"
Remove-Item Env:\PGPASSWORD
```

### 22.2 典型查询

把示例条件换成真实数据：

```powershell
$env:PGPASSWORD = "只读账号密码"
psql -h <数据库服务器IP> -p 5432 -U sid_readonly_user -d TraceData -c "SELECT line, csv_date, did, gcode FROM data.did_query WHERE gcode = '<GCODE>' AND line = 'L08' AND csv_date >= '2023-08-02 00:00:00' AND csv_date < '2023-08-03 00:00:00' LIMIT 50;"
Remove-Item Env:\PGPASSWORD
```

### 22.3 查看执行计划

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

期望能看到使用 `idx_did_query_main_query` 或 TimescaleDB chunk 上对应索引。

### 22.4 查看索引

```bash
sudo -iu postgres /usr/pgsql-17/bin/psql -d "TraceData" <<'SQL'
SELECT schemaname,
       tablename,
       indexname,
       indexdef
FROM pg_indexes
WHERE schemaname = 'data'
ORDER BY tablename, indexname;
SQL
```

### 22.5 查看表大小

```bash
sudo -iu postgres /usr/pgsql-17/bin/psql -d "TraceData" <<'SQL'
SELECT pg_size_pretty(pg_total_relation_size('data.did_query')) AS did_query_total_size;
SELECT pg_size_pretty(pg_total_relation_size('data.import_log')) AS import_log_total_size;
SQL
```

TimescaleDB 详细大小：

```bash
sudo -iu postgres /usr/pgsql-17/bin/psql -d "TraceData" <<'SQL'
SELECT *
FROM hypertable_detailed_size('data.did_query');
SQL
```

## 23. 日常运维命令

### 23.1 PostgreSQL 服务

```bash
systemctl status postgresql-17 --no-pager
systemctl restart postgresql-17
systemctl reload postgresql-17
journalctl -u postgresql-17 -n 200 --no-pager
```

### 23.2 日志

```bash
ls -lh /data/postgresql/17.10/log
tail -200 /data/postgresql/17.10/log/postgresql-*.log
```

### 23.3 当前连接

```bash
sudo -iu postgres /usr/pgsql-17/bin/psql -d "TraceData" <<'SQL'
SELECT pid,
       usename,
       client_addr,
       state,
       wait_event_type,
       wait_event,
       now() - state_change AS state_for,
       left(query, 120) AS query_sample
FROM pg_stat_activity
WHERE datname = 'TraceData'
ORDER BY state_change;
SQL
```

### 23.4 锁等待

```bash
sudo -iu postgres /usr/pgsql-17/bin/psql -d "TraceData" <<'SQL'
SELECT blocked.pid AS blocked_pid,
       blocked.usename AS blocked_user,
       blocking.pid AS blocking_pid,
       blocking.usename AS blocking_user,
       blocked.query AS blocked_query,
       blocking.query AS blocking_query
FROM pg_stat_activity blocked
JOIN pg_locks blocked_locks
  ON blocked_locks.pid = blocked.pid
JOIN pg_locks blocking_locks
  ON blocking_locks.locktype = blocked_locks.locktype
 AND blocking_locks.database IS NOT DISTINCT FROM blocked_locks.database
 AND blocking_locks.relation IS NOT DISTINCT FROM blocked_locks.relation
 AND blocking_locks.page IS NOT DISTINCT FROM blocked_locks.page
 AND blocking_locks.tuple IS NOT DISTINCT FROM blocked_locks.tuple
 AND blocking_locks.virtualxid IS NOT DISTINCT FROM blocked_locks.virtualxid
 AND blocking_locks.transactionid IS NOT DISTINCT FROM blocked_locks.transactionid
 AND blocking_locks.classid IS NOT DISTINCT FROM blocked_locks.classid
 AND blocking_locks.objid IS NOT DISTINCT FROM blocked_locks.objid
 AND blocking_locks.objsubid IS NOT DISTINCT FROM blocked_locks.objsubid
 AND blocking_locks.pid != blocked_locks.pid
JOIN pg_stat_activity blocking
  ON blocking.pid = blocking_locks.pid
WHERE NOT blocked_locks.granted
  AND blocking_locks.granted;
SQL
```

### 23.5 导入状态

```bash
sudo -iu postgres /usr/pgsql-17/bin/psql -d "TraceData" <<'SQL'
SELECT status, count(*)
FROM data.import_log
GROUP BY status
ORDER BY status;

SELECT *
FROM data.import_log
WHERE status <> 'success'
ORDER BY imported_at DESC NULLS LAST
LIMIT 100;
SQL
```

### 23.6 最近导入情况

```bash
sudo -iu postgres /usr/pgsql-17/bin/psql -d "TraceData" <<'SQL'
SELECT max(imported_at) AS last_import_time
FROM data.import_log
WHERE status = 'success';

SELECT date_trunc('hour', imported_at) AS hour,
       status,
       count(*) AS file_count,
       sum(row_count) AS row_count
FROM data.import_log
WHERE imported_at >= now() - interval '24 hours'
GROUP BY 1, 2
ORDER BY 1 DESC, 2;
SQL
```

### 23.7 慢 SQL 统计

```bash
sudo -iu postgres /usr/pgsql-17/bin/psql -d "TraceData" <<'SQL'
SELECT calls,
       round(total_exec_time::numeric, 2) AS total_ms,
       round(mean_exec_time::numeric, 2) AS mean_ms,
       rows,
       left(query, 200) AS query_sample
FROM pg_stat_statements
ORDER BY total_exec_time DESC
LIMIT 20;
SQL
```

如果提示 `pg_stat_statements` 不存在，检查：

```bash
sudo -iu postgres /usr/pgsql-17/bin/psql -d "TraceData" -c "CREATE EXTENSION IF NOT EXISTS pg_stat_statements;"
sudo -iu postgres /usr/pgsql-17/bin/psql -d "TraceData" -c "SHOW shared_preload_libraries;"
```

## 24. 备份现状和最低限度备份建议

用户已确认当前没有备份。这个风险要在文档里明确记录。

### 24.1 至少备份配置文件

```bash
mkdir -p /data/postgresql/17.10/config-backup-final
cp -a /data/postgresql/17.10/data/postgresql.conf /data/postgresql/17.10/config-backup-final/
cp -a /data/postgresql/17.10/data/pg_hba.conf /data/postgresql/17.10/config-backup-final/
cp -a /etc/systemd/system/postgresql-17.service.d /data/postgresql/17.10/config-backup-final/ 2>/dev/null || true
tar -czf /data/postgresql/17.10/config-backup-final-$(date +%Y%m%d_%H%M%S).tar.gz -C /data/postgresql/17.10 config-backup-final
ls -lh /data/postgresql/17.10/config-backup-final-*.tar.gz
```

### 24.2 逻辑备份示例

13 亿多行数据较大，`pg_dump` 可能耗时很长。测试环境如果磁盘允许，可先备份 schema：

```bash
sudo -iu postgres /usr/pgsql-17/bin/pg_dump \
  -d "TraceData" \
  --schema-only \
  -f /data/postgresql/17.10/TraceData_schema_$(date +%Y%m%d_%H%M%S).sql
```

备份整库示例：

```bash
sudo -iu postgres /usr/pgsql-17/bin/pg_dump \
  -d "TraceData" \
  -Fc \
  -f /data/postgresql/17.10/TraceData_full_$(date +%Y%m%d_%H%M%S).dump
```

说明：

- 当前没有备份时，不建议贸然做破坏性结构调整。
- 正式环境应优先设计物理备份、WAL 归档或快照策略。
- TimescaleDB 数据恢复目标环境必须安装兼容版本 TimescaleDB。

## 25. 常见故障定位

| 现象 | 常见原因 | 检查命令 |
| --- | --- | --- |
| `connection refused` | PostgreSQL 没启动或没监听外部地址 | `systemctl status postgresql-17`、`ss -lntp | grep 5432` |
| `timeout` | 防火墙或网络不通 | `firewall-cmd --list-rich-rules`、`Test-NetConnection` |
| `no pg_hba.conf entry` | `pg_hba.conf` 未放通导入服务器或查询客户端 | `tail -40 ${PGDATA}/pg_hba.conf` |
| `password authentication failed` | 密码错误或账号不对 | `ALTER ROLE ... PASSWORD ...` |
| `extension "timescaledb" is not available` | TimescaleDB 包没装或版本不对应 PG17 | `rpm -qa | grep timescaledb` |
| `must be preloaded` | 没配置 `shared_preload_libraries` 或没重启 | `SHOW shared_preload_libraries;` |
| 导入很慢 | 网络共享目录慢、索引提前创建、批次太小 | 检查共享目录、索引、`data_batch_size` |
| Windows 定时任务访问不到共享目录 | 定时任务运行账号没有共享权限或依赖映射盘 | 用任务账号测试 UNC 路径 |
| 只读账号不能查询新表 | 新表缺少授权或默认权限没覆盖 | `GRANT SELECT ON ALL TABLES IN SCHEMA data TO sid_readonly_user;` |

## 26. 最终验收清单

### 26.1 系统和安装验收

```bash
cat /etc/rocky-release
uname -m
/usr/pgsql-17/bin/postgres --version
/usr/pgsql-17/bin/psql --version
rpm -qa | grep -E 'postgresql17|timescaledb' | sort
```

验收标准：

- Rocky 9。
- x86_64。
- PostgreSQL 17.10。
- TimescaleDB 2.28.1 且包名对应 PostgreSQL 17。

### 26.2 服务验收

```bash
systemctl is-enabled postgresql-17
systemctl is-active postgresql-17
ss -lntp | grep 5432
```

验收标准：

- PostgreSQL 开机自启。
- 当前服务运行中。
- 5432 端口监听正常。

### 26.3 数据库配置验收

```bash
sudo -iu postgres /usr/pgsql-17/bin/psql -d "TraceData" <<'SQL'
SHOW data_directory;
SHOW listen_addresses;
SHOW port;
SHOW shared_preload_libraries;
SHOW timezone;
SQL
```

验收标准：

- 数据目录是自定义目录 `/data/postgresql/17.10/data`。
- `shared_preload_libraries` 包含 `timescaledb`。
- 时区为 `Asia/Shanghai`。

### 26.4 扩展验收

```bash
sudo -iu postgres /usr/pgsql-17/bin/psql -d "TraceData" <<'SQL'
SELECT extname, extversion
FROM pg_extension
WHERE extname IN ('timescaledb', 'pg_stat_statements')
ORDER BY extname;
SQL
```

验收标准：

- `timescaledb` 已安装在 `TraceData`。
- `timescaledb` 版本为 `2.28.1`。

### 26.5 表结构验收

```bash
sudo -iu postgres /usr/pgsql-17/bin/psql -d "TraceData" <<'SQL'
\dt data.*
\d+ data.did_query
\d+ data.import_log
SELECT hypertable_schema, hypertable_name
FROM timescaledb_information.hypertables
WHERE hypertable_schema = 'data'
  AND hypertable_name = 'did_query';
SQL
```

验收标准：

- `data.did_query` 存在。
- `data.import_log` 存在。
- `data.did_query` 是 TimescaleDB hypertable。

### 26.6 权限验收

导入账号：

```powershell
$env:PGPASSWORD = "导入账号密码"
psql -h <数据库服务器IP> -p 5432 -U sid_import_user -d TraceData -c "SELECT count(*) FROM data.import_log;"
Remove-Item Env:\PGPASSWORD
```

只读账号：

```powershell
$env:PGPASSWORD = "只读账号密码"
psql -h <数据库服务器IP> -p 5432 -U sid_readonly_user -d TraceData -c "SELECT count(*) FROM data.did_query;"
psql -h <数据库服务器IP> -p 5432 -U sid_readonly_user -d TraceData -c "DELETE FROM data.did_query WHERE false;"
Remove-Item Env:\PGPASSWORD
```

验收标准：

- 导入账号能访问导入表。
- 只读账号能查询。
- 只读账号不能写入或删除。

### 26.7 导入程序验收

```powershell
cd /d D:\csv-import-postgresql
.\.venv\Scripts\python.exe -m py_compile .\import_sid_csv.py
Test-Path "\\192.168.1.28\TraceDate"
Test-NetConnection <数据库服务器IP> -Port 5432
.\.venv\Scripts\python.exe .\import_sid_csv.py --config .\config.json --mode incremental
```

验收标准：

- Python 脚本语法通过。
- 导入服务器能访问 CSV 共享目录。
- 导入服务器能访问数据库 5432。
- 增量导入能正常运行。

### 26.8 查询性能验收

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

验收标准：

- 使用 `gcode + line + csv_date` 相关索引。
- 查询耗时符合测试目标。

## 27. 已确认问题答复

| 序号 | 问题 | 已确认答复 |
| --- | --- | --- |
| 1 | 目标服务器操作系统版本是什么？ | Rocky 9 |
| 2 | 部署环境是否完全离线？ | 完全离线，但有公司内部 repo |
| 3 | PostgreSQL 17.10 安装包来源是否已经确定？ | PostgreSQL 17.10 和 TimescaleDB 2.28.1 包已自己事先下载好；依赖包走公司内部 repo |
| 4 | 是否必须安装 TimescaleDB？ | 必须 |
| 5 | 数据库、CSV 文件、备份分别放在哪个磁盘？ | 数据库在测试虚拟机；CSV 文件在远端服务器；当前没有备份 |
| 6 | 导入程序和数据库是否同机部署？ | 不同机；导入程序需能同时访问 CSV 文件服务器和数据库 |
| 7 | 初始全量数据量大约多少？ | 13 亿多行 |
| 8 | 业务查询主要条件是否固定？ | 基本固定 |
| 9 | 是否需要定时增量导入？ | 已配置 Windows 定时任务 |
| 10 | 是否需要给业务部门开放只读账号？ | 无论怎么样，最好都用只读账号查询 |

## 28. 仍需现场补齐的信息

下面这些不是方案问题，而是执行命令时必须填的现场值：

- 数据库测试虚拟机 IP。
- 导入服务器 IP。
- 查询客户端 IP 或查询网段。
- 公司内部依赖 repo ID。
- 自己下载的 PostgreSQL 和 TimescaleDB RPM 实际文件名。
- `/data` 实际挂载磁盘和可用容量。
- `sid_import_user` 密码。
- `sid_readonly_user` 密码。
- Windows 定时任务运行账号。
- 远端 CSV 共享目录访问账号。
- 全量导入开始日期是否固定为 `2021-01-01`。
- 是否需要在测试完成后补正式备份方案。
