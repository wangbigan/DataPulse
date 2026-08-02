# DataPulse MVP

DataPulse MVP 是按 `DataPulse系统设计方案_v1.2.md` 落地的最小可用版本，范围收敛为：

- FastAPI 应用服务。
- DuckDB 本地元数据仓库。
- 面向 SQLite、DuckDB、MySQL 数据源的快照式扫描器。
- 无构建链静态浏览界面，支持 L0-L3 钻取。
- Word 报告导出。
- 样例数据清空，并执行 DuckDB `CHECKPOINT`。

## 快速启动

```powershell
python -m pip install -r requirements.txt
python .\scripts\create_demo_sqlite.py
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

打开：

```text
http://127.0.0.1:8000
```

创建一个 SQLite demo 数据源：

- 名称：`Demo HIS`
- 类型：`SQLite`
- URI：`C:\aiSpace\DataPulse\data\demo_his.sqlite`

然后点击创建快照并开始扫描。快照状态变为 `done` 后，可以在页面中查看库总览、表列表、字段详情，也可以导出 Word 报告。

## 当前规则口径

### 业务域

业务域当前由前端展示层临时推断，不在扫描阶段落库。规则取 `table_name + table_comment` 转小写后按关键字包含匹配，第一条命中规则生效：

| 业务域 | 关键字 |
| --- | --- |
| 患者 | `patient`、`empi`、`master_index`、`患者` |
| 住院 | `inp`、`zy`、`admission`、`discharge`、`encounter`、`住院`、`入院`、`出院` |
| 门诊 | `mz`、`outp`、`register`、`visit`、`门诊`、`挂号`、`就诊` |
| 检验 | `lis`、`lab`、`result`、`检验`、`化验` |
| 检查 | `pacs`、`exam`、`image`、`检查`、`影像` |
| 收费 | `fee`、`charge`、`invoice`、`settle`、`claim`、`收费`、`费用`、`结算`、`医保` |
| 药事 | `drug`、`pharmacy`、`prescription`、`药`、`处方` |
| 字典 | `dict`、`dept`、`staff`、`code`、`字典`、`科室`、`人员` |
| 平台 | `sys`、`log`、`interface`、`系统`、`日志`、`接口` |

未命中时归为 `临床`。

### 有效率

字段级有效率：

```text
valid_rate = (row_count - null_count - empty_count - placeholder_count) / row_count
```

`row_count = 0` 时不计算。`empty_count` 和 `placeholder_count` 只对字符串型字段生效；数值型 `0` 视为合法值。默认占位值清单：

```text
无, 未知, 不详, -, --, /, \, N/A, NULL, null, 0, .
```

表级 `avg_valid_rate` 是该表参与统计字段的 `valid_rate` 简单平均。

### 时间分布

每张表选一个日期字段写入 `table_stat.date_column/min_date/max_date`，前端时间分布直接使用这些字段。候选字段需要满足：

- 字段类型包含 `DATE` 或 `TIME`。
- 字段统计阶段已经得到非空的 `min_value` 和 `max_value`。

排序规则是字段名包含 `date` 或 `日期` 的优先，其次按字段顺序 `ordinal_position`，取第一列。

### 疑似字典字段

当前没有单独的字典绑定或确认标记。仪表盘里的“疑似字典字段”按低基数字段统计：

```text
distinct_count IS NOT NULL AND distinct_count <= low_cardinality_limit
```

默认 `low_cardinality_limit = 50`。值域分布采集也使用同一阈值：只有 distinct 不超过该阈值的字段才会落 `value_dist`。

### 敏感字段

启动时从 `config/sensitive.yaml` 导入敏感字段策略。扫描结构时把 `column_name`、`column_comment`、`table_name` 拼成匹配文本，再用配置中的正则按忽略大小写匹配；第一条命中即标记为敏感字段，并记录对应动作：

- `skip`：不保存字段取值、值域分布和样例。
- `mask`：只保存脱敏后的取值、值域分布和样例。

默认策略覆盖姓名、身份证、电话/手机、地址、病案号/住院号等常见字段。具体规则以 `config/sensitive.yaml` 为准。

## MySQL 测试

MySQL 连接串格式：

```text
mysql://user:password@127.0.0.1:3306/database_name
```

如果要把 demo 表结构和仿真数据写入一个已有 MySQL 数据库：

```powershell
python .\scripts\create_demo_mysql.py "mysql://user:password@127.0.0.1:3306/datapulse_demo"
```

注意：

- `datapulse_demo` 数据库需要提前创建。
- 执行 demo 初始化脚本的账号需要 `CREATE`、`DROP`、`INSERT`、`SELECT` 权限。
- 仅做扫描时，账号需要目标库的 `SELECT` 权限，以及访问 `information_schema` 的权限。

## MVP 范围

已实现：

- 数据源登记和连接测试。
- 创建扫描快照，可指定表范围。
- 扫描任务记录、加权进度、暂停、恢复、删除快照。
- 表元数据、字段元数据、行数、有值率、有效率、重复率、低基数字段值域分布、随机样例。
- 从 `config/sensitive.yaml` 导入敏感字段策略。
- `skip` 字段不保存取值和值域样例。
- `mask` 字段只保存脱敏后的取值。
- 静态 L0/L1/L2/L3 浏览界面。
- `.docx` 报告导出。
- 样例清空接口：删除 `sample_data`，执行 DuckDB `CHECKPOINT`，并写入 `audit_log`。

按设计文档推迟到 V2/V3：

- 元数据编辑 UI/API。
- 逻辑外键确认和关联率指标。
- 机构维度下钻。
- 标准字典对照。
- 快照 diff 和质量综合评分。
- MySQL/Oracle 真实生产库基准测试、方言模板加固、驱动级取消能力验证。

## API 入口

- `GET /api/health`
- `POST /api/sources`
- `POST /api/sources/test`
- `POST /api/scans`
- `POST /api/scans/{snapshot_id}/pause`
- `POST /api/scans/{snapshot_id}/resume`
- `DELETE /api/snapshots/{snapshot_id}`
- `GET /api/snapshots`
- `GET /api/dashboard?snapshot_id=...`
- `GET /api/tables?snapshot_id=...`
- `GET /api/tables/{table_id}?snapshot_id=...`
- `GET /api/columns/{column_id}?snapshot_id=...`
- `POST /api/reports/{snapshot_id}/docx`
- `POST /api/samples/clear`

## 目录说明

- `app/main.py`：FastAPI 入口和 API 路由。
- `app/scanner.py`：快照扫描流程和指标计算。
- `app/storage.py`：DuckDB 元数据仓库 schema 和基础访问。
- `app/datasource.py`：SQLite、DuckDB、MySQL 数据源连接器。
- `app/static/`：无构建链静态页面。
- `config/sensitive.yaml`：敏感字段识别和处理策略。
- `scripts/create_demo_sqlite.py`：生成 SQLite demo 库。
- `scripts/create_demo_mysql.py`：向 MySQL 写入 demo 表和仿真数据。
- `reports/`：导出的 Word 报告目录。
- `data/`：本地 DuckDB 元数据仓库和 demo 数据库目录。
