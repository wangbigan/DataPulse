# DataPulse 系统设计方案

> 文档版本：v1.2（MVP 收敛版）　状态：设计评审稿　日期：2026-07-31
> 本版基于 v1.1（对抗性审查修订版）做第二轮对抗性审查后的结构性修订：MVP 收敛为"扫描引擎 + DuckDB + 静态浏览界面 + 报告导出"，React 全套前端整体推迟；修订点映射见下表。

## v1.2 修订记录（映射第二轮对抗性审查 16 项）

| # | 审查项 | 处理 |
|---|---|---|
| 1 | 首扫成本与离场交付模式矛盾 | 新增 3.7 节"分批次扫描计划"；开发计划阶段 0 增加真实库基准测试 |
| 2 | MVP 最小形态 = 引擎 + DuckDB + 报告 + 简易浏览 | 第 8 章整体重写：React 推迟，MVP 用无构建链静态界面 |
| 3 | 快照纯粹性 / 补算无 computed_at | 所有 stat 表加 `computed_at`；补算语义见 6.4.4 |
| 4 | 工具应辅助语义层（不止省 SQL） | 业务域标签自动建议列入 V2 backlog（复用同义词表资产），见 6.3.7 |
| 5 | DATE_SPAN 两阶段重复扫描 | 4.3.4 改单阶段：直接读 column_stat 的日期 min/max，删除 rowcount 阶段日期扫描 |
| 6 | diff 预计算无落点且属 V3 | finalize 删除 diff 预计算，V3 实时 diff |
| 7 | 值域分布逐列扫描违反单趟原则 | 方言适配器加 GROUPING SETS 能力位，支持时一趟出全部低基数列分布（6.2.4） |
| 8 | MySQL 大表精确 distinct 是最贵单点 | MySQL 大表 distinct 默认跳过标"不支持"，用户手动触发（6.2.4） |
| 9 | 质量评分缺项归一未定义、易被挑战 | 评分推迟到 V3，出分项指标 + 问题榜单；V3 启用前必须定义缺项重归一规则（4.7） |
| 10 | 毒任务无限循环 | scan_task 加 `crash_count`，崩溃重置也计次（6.4.4） |
| 11 | 同源库并发扫描无互斥 | 调度器硬约束：每数据源同时最多 1 个 running 快照（6.4.1） |
| 12 | DuckDB 多 worker 并发写 | 写入收敛到单写者线程队列（6.4.3） |
| 13 | value_dist 依赖 column 产出未建模 | 显式表级依赖：column 失败 → 本表 value_dist 级联 skipped；估算 distinct 走保守分支（6.2.9） |
| 14 | 推断 blacklist 误杀稀疏外键 | 子侧不卡 fill_rate，低 fill 改为降分项（6.3.2） |
| 15 | TRIM join 杀索引 / 样例清空可恢复 / 孤儿样例无落点 | TRIM 成本提示 + 预归一化建议（4.4.5）；清空样例后强制执行 VACUUM/CHECKPOINT（3.5）；孤儿样例物化进 relation_stat（5.3.3） |
| 16 | 若干口径/边界小项 | dup_rate 负值钳制；哨兵转义规则；退避统一 30s/120s；MVP 页面范围与分期对齐；值形态敏感检测入 backlog |

---

## 1. 项目背景与目标

### 1.1 业务背景

DataPulse 是一个面向医疗场景的轻量级数据探查（Data Profiling）工具。用户只需提供一个可执行 SQL 的数据库访问接口，工具即可自动扫描数据库的元数据信息，包括：

- 表结构信息、主外键关系、值域字段及对应的值域字典；
- 每张表的数据量、有主外键关系的表之间的关联率；
- 每个字段的有值率、值域分布、重复率、样例数据等。

扫描结果支持人工编辑与补充（逻辑外键、字段注释、敏感字段、维度字段等），并基于这些元数据生成逻辑清晰、层层深入的浏览界面与数据质量报告，帮助用户快速掌握数据总体情况并逐级钻取细节。

**痛点来源：**

- 数据质量评估与报告交付：需要经常帮助客户探查数据情况、评估数据质量并撰写质量报告；
- ETL 生产过程中的反复探查：数据有没有重复、字段填充率如何、值域分布如何、有没有脏数据、一致性如何、表之间能否关联上、A 表 join B 表后某字段的值域分布如何等。

**为什么不用 Atlas / DataHub / OpenMetadata 等重型平台：** 这些通用数据治理平台部署重、学习成本高，且不理解医疗场景的两个关键诉求——按医疗机构的维度下钻和医疗值域字典对照。这个空白真实存在，是 DataPulse 的定位空间。

### 1.2 产品目标

1. 省去 90% 以上的数据探查 SQL 编写工作（不追求 100%，剩余约 10% 的定制化探查通过"自定义探查模板"沉淀为可复用资产）；
2. 快速沉淀一个数据库的元数据资产，扫描一次即可长期分析；
3. **直接产出可交付的数据质量报告初稿**——报告是客户付钱买的交付物，浏览界面是中间态；
4. 支撑电子病历评级中"数据一致性"考察点的自查材料。

### 1.3 核心设计原则

- **引擎优先，界面从简**：MVP 的全部复杂度预算给扫描引擎与口径严谨性；界面是无构建链的静态浏览页，React 全套前端推迟到 V2 之后。
- **扫描结果与源库完全解耦**：所有探查指标算完后落入本地元数据仓库，界面与报告只查元数据仓库，不再碰源库（即席探查除外，见第 2 章）。
- **快照驱动**：每次扫描产生一个快照，结构元数据和统计指标永远不"原地更新"，只新增快照——这是快照对比、趋势分析的基础。
- **扫描层 / 人工层双层分离**：扫描产生的数据重刷即弃；用户编辑补充的数据跨快照存活，永远不被扫描覆盖。
- **自动化的边界停在"准备好证据等一键确认"**：外键自动推断等智能能力只产出"高置信候选 + 证据"，由人工一键确认后生效，绝不全自动写死（详见 6.3 节）。
- **保护源库优先**：源库是医院生产库，扫描调度以"扫出事就是事故"为前提设计限流、熔断与低峰窗口（详见 6.4 节）。

---

## 2. 总体架构

### 2.1 架构分层（v1.2）

```
┌─────────────────────────────────────────────────┐
│        静态浏览界面（无构建链，随容器分发）          │
│   L0 总览 → L1 表列表 → L2 表详情 → L3 字段详情    │
│   原生 ES Module + ECharts（本地化打包，院内离线）   │
├─────────────────────────────────────────────────┤
│              应用服务层（FastAPI）                │
│   元数据查询API（只读JSON）│ 扫描任务管理 │ 报告导出  │
│   （元数据编辑API 随人工层功能在 V2 开放）          │
├─────────────────────────────────────────────────┤
│                探查引擎（核心）                   │
│  扫描调度 │ SQL模板生成 │ 指标计算 │ 方言适配器     │
├──────────────┬──────────────────────────────────┤
│   元数据仓库   │         数据源接入层              │
│ (DuckDB单机/  │   SQL执行接口（用户提供的统一入口） │
│  PG协作版)    │   MySQL/Oracle/SQLServer/PG/Hive  │
└──────────────┴──────────────────────────────────┘
```

**v1.2 关键变化**：展示层从"React + Ant Design 应用"改为"无构建链静态界面"——原生 ES Module + 本地化打包的 ECharts，FastAPI 只出只读 JSON。理由：MVP 的付费交付物是质量报告，浏览界面是中间态；单人开发体量下 React 全套（表列表/关联图谱/维度对比/关系确认/快照对比/字典管理六个复杂页面）是最大的成本黑洞。静态界面零构建、零依赖安装、随 Docker 镜像分发，院内离线环境天然可用。V2 开放人工层编辑时，再评估是否引入 React（编辑交互复杂度才是框架的真正理由）。

### 2.2 关键设计决策：扫描结果与源库完全解耦

所有探查指标算完后落入本地元数据仓库，界面与报告**默认**只查元数据仓库，不再碰源库——标注为"即席探查"的交互（孤儿样例、两表级联分析等，V2 开放）除外，统一走快车道、10s 超时，并强制经过与样例同一套的敏感字段过滤/脱敏渲染层（见 6.4.3 / 8.3 节）。好处有三：

1. **页面秒开**，钻取体验流畅；
2. 客户现场扫一次，数据（脱敏后）带回来慢慢分析、写报告；
3. 天然支持**扫描快照对比**——同一库不同时间扫两次，看数据量增长、有值率变化趋势，这是质量报告里最有说服力的素材。

### 2.3 技术选型

| 层 | 选型 | 理由 |
|---|---|---|
| 后端 | Python + FastAPI | 团队主战场，pandas/pyarrow 处理扫描结果顺手 |
| 数据库连接 | SQLAlchemy + 各库驱动 | 方言抽象现成，支持库广 |
| 元数据仓库 | DuckDB（单机）/ PostgreSQL（协作版） | 单机交付用 DuckDB 零依赖；多人协作版用 PG |
| 前端（MVP） | 无构建链静态界面：原生 ES Module + ECharts 本地化 | 零构建、随镜像分发、院内离线可用；React 推迟 |
| 报告导出 | docxtpl（Jinja2 → docx） | 直接产出 Word 质量报告初稿，MVP 交付物 |
| 部署 | Docker 单容器，院内离线部署 | 医疗数据不出院，交付形态就是一个镜像 |
| 异步任务 | 数据库任务表 + 进程内线程池 | 扫描是长任务；不需要 Celery/RabbitMQ（详见 6.4 节） |

---

## 3. 功能需求与扫描内容模型

本章定义"扫什么"。各项指标只给口径摘要，精确定义（公式、分母、边界、展示规范）以第 4 章为准。

### 3.1 结构元数据（从 information_schema / 数据字典视图获取）

按"库 / 表 / 字段 / 关系"四级组织：

| 层级 | 内容 |
|---|---|
| 库级 | 库名、字符集、表数量、扫描时间 |
| 表级 | 表名、表注释、物理主键、估算行数（数据库统计信息） |
| 字段级 | 字段名、类型、长度、可空、默认值、字段注释 |
| 关系级 | 已定义的主外键约束（物理外键） |

### 3.2 统计元数据（生成探查 SQL 在源库执行）

| 层级 | 指标 | 口径摘要 |
|---|---|---|
| 表级 | 精确行数 | `COUNT(*)`，大表可选估算或采样 |
| 表级 | 时间跨度 | 从字段级日期 min/max 中按规则选定业务日期字段（单阶段，见 4.3.4） |
| 字段级 | **有值率** | 非 NULL 且非空串（字符串型）的行占比；NULL 率、空串率分开列示 |
| 字段级 | **唯一值数 / 重复率** | distinct count（大表用 approx 或跳过）；重复率精确口径见 4.2.3 节 |
| 字段级 | **值域分布** | 低基数字段全量 group by；高基数 TopN + 其余归并"其他" |
| 字段级 | 数值统计 | min/max/mean/中位数/分位数，异常值提示 |
| 字段级 | **样例数据** | N 条随机样例（受敏感字段配置约束；不取首行样例） |
| 关系级 | **关联率** | A.fk → B.pk 命中率；孤儿率；父侧覆盖度；关联基数分布（V2） |

### 3.3 值域字典（医疗特色，差异化能力，V3）

- **自动识别**：低基数字段自动标记为"疑似字典字段"，提取完整值域列表；
- **人工确认与补充**：用户可把字段绑定到一个字典（如性别、科室、血型、婚姻状况）；
- **标准对照**：内置常用医疗标准字典（GB/T 2261.1 性别、CV 代码、ICD-10 等可选导入），扫描时自动比对源库取值与标准字典的**匹配率**——这一项直接就是电子病历评级里"数据一致性"考察点的自查材料，对评级咨询业务是现成的钩子。

### 3.4 元数据编辑与补充（扫描结果可修正，V2 开放编辑界面）

这是本工具区别于纯 Profiling 工具的核心。元数据分两层存储（双层存储原则）：

- **自动层（扫描层）**：扫描产生，重扫会刷新；
- **人工层**：用户编辑补充，**永远不被扫描覆盖**，冲突时页面并显。

可编辑项：

1. **逻辑主外键**：源库没建外键约束时人工补充（HIS/EMR 库几乎必然需要），补充后触发关联率重算；
2. **字段业务含义**：注释缺失时补充中文含义；
3. **值域字典绑定**：字段 ↔ 字典的映射关系维护（V3）；
4. **敏感字段标记**：字段级标记 + 脱敏规则选择（MVP 用配置文件，V2 开界面）；
5. **维度字段设置**：指定哪些字段是机构维度（机构编码/机构名称），支持多字段组合、支持"默认机构"（V2）。

**维度下钻的实现方式**：设置维度字段后，对应指标全部按 `GROUP BY 维度字段` 重算一遍并单独存储（实现模板见 6.2 节）。注意控制成本：维度探查只对**用户圈选的表**执行，不要默认全库跑。页面上做一个全局"机构切换器"（V2），切换后各层页面展示该机构的数据量、有值率、关联率——机构间横向对比（A 院有值率 92% vs B 院 61%）是质量报告里最好用的素材。

> MVP 边界：人工层表结构（dim_config / sensitive_config / column_annotation 等）**在 M1 就建好**，但 MVP 只通过 YAML 配置文件读写敏感字段与维度配置，不做编辑界面——schema 一次到位，界面分期开放，避免 V2 返工。

### 3.5 敏感数据处理（医疗合规红线，默认从严）

- 内置常见敏感字段正则/词库（姓名、身份证、电话、住址、病案号），扫描时**自动预判**；MVP 经配置文件确认，V2 提供确认界面；
- 敏感字段三选一：**不取样例**（skip，默认）／脱敏后取样例（mask，掩码：张\*明、身份证前6后4／哈希）；
- 样例数据只存元数据仓库本地，且提供"清空所有样例"一键操作——离场交付时客户最怕这个；
- **物理清空的完整定义**（v1.2 修订）：删除 `sample_data` 全部行后，必须强制执行存储层空间回收——DuckDB 执行 `CHECKPOINT` 并重建（或导出后删库重导），SQLite 执行 `VACUUM`——否则已删除数据仍残留在数据页中可被恢复，"清空"对客户信息科是假的。清空操作与回收结果写入 audit_log；
- 敏感配置的约束力覆盖**所有"字段取值落盘"的通道**——值域分布、众值 top_value、字典未命中清单、孤儿样例、即席查询、推断证据。规则：`skip` 类敏感字段不做值域分布、不存 top_value、不进入任何样例/清单/证据；`mask` 类脱敏后参与；
- **第二道防线（backlog）**：当前敏感检测基于字段名/注释，拼音缩写字段（`CSZ` 出生地、`HKDZ` 户口地址）会漏网。后续增加"基于样例值的形态检测"（身份证 18 位、手机号正则命中率高 → 提示标记敏感）。

**离场导出审查**：导出包（报告/数据带离现场）生成时，工具先产出内容清单（包含哪些表、指标、是否含样例数据、是否含字典未命中清单），并提供敏感项复核页面逐项列示可能触及敏感数据的条目，客户确认后方可带离。

### 3.6 自定义探查模板（兜底 10%，V3）

预留"自定义探查模板"能力：用户写带参数的 SQL 模板（如"A 表 join B 表后看某字段分布"），工具执行并把结果纳入元数据仓库展示。这把剩余 10% 的定制化 SQL 也沉淀成可复用资产，下次遇到同类库直接复用。

### 3.7 分批次扫描计划（v1.2 新增，应对首扫成本与离场模式的矛盾）

全库首轮扫描在 500 表规模下可能是 10~50 小时量级（阶段 0 基准测试后校准此数），一次进场窗口未必扫得完，而医院几乎不可能给远程持续接入。因此"分批扫描"是一等公民功能，不是最佳实践建议：

- **进场前圈选**：扫描范围按 schema / 业务域 / 表清单圈选，支持"本次只扫住院域 120 张表"；
- **断点天然支持**：断点续扫（6.4.4）+ 圈选机制组合后，"上次扫了 300 张、这次续扫剩余 200 张"就是常规操作而非特例；
- **多次进场产出增量快照**：每次进场生成新快照，快照对比（V3）顺带回答"这三个月数据治理有没有改善"——分批的代价转化为趋势分析的素材；
- **估算先行**：扫描发起前基于 est_row_count 与代价模型给出总时长估算（见 6.1），让用户在客户现场就能和信息科谈定"这次扫什么"。

---

## 4. 指标口径定义

口径定义是本工具的"法条"——数字将来要写进给客户的数据质量报告，评审专家一句"你这个有值率分母是什么"答不上来就砸招牌。所以每个指标按**定义卡**格式写死：公式、分母、边界处理、展示规范。

### 4.1 总则：口径三要素与通用规则

每个指标必须显式声明三要素：**统计对象**（在什么集合上算）、**分母**（除以什么）、**过滤条件**（排除了什么）。不声明分母的比率都是耍流氓。

**通用规则（全部指标适用）：**

| 规则 | 内容 |
|---|---|
| 空值两态 | `NULL` 与"空串"分开计数、分开列示，不混为一谈 |
| 空串定义 | 字符串型：`TRIM(值) = ''`（纯空白也算空串）；非字符串型无空串概念 |
| Oracle 特例 | `'' ≡ NULL`，空串计数恒为 0，页面标注"该库空串归入 NULL 统计" |
| 估算标记 | 采样/近似算法得出的指标，存储和展示都带 `is_estimated`，页面加 `~` 前缀 |
| 除零处理 | 分母为 0 时指标值为 `NULL`，页面显示 `—`，**绝不显示 0%**（0% 和"无法计算"是两回事） |
| distinct 口径 | `COUNT(DISTINCT col)` 遵循 SQL 标准，**不含 NULL** |
| 估算钳制 | 近似 distinct 高估导致 `distinct_cnt > non_empty_cnt` 时，dup_rate/uniq_rate 按 0/1 钳制并保留估算标记（v1.2 新增） |
| 哨兵转义 | 值域分布存储用 `'(NULL)'` / `'(空串)'` 哨兵字符串；真实数据中源值与哨兵字面冲突时，源值落库前缀 `\` 转义（如 `\ (NULL)`，展示层反转义）（v1.2 新增） |
| 口径版本 | 所有口径定义存入 `metric_registry` 并带版本号，快照记录所用口径版本（`scan_snapshot.metric_def_version`），报告可复现 |

**口径登记册 `metric_registry`**：工具交付报告时，每个数字都能溯源到一条口径定义。表结构见 5.3.7 节；界面的指标悬浮口径卡内容也来自该表（见 4.8 节）。

### 4.2 字段级指标

#### 4.2.1 有值率 `FILL_RATE`

| 项 | 定义 |
|---|---|
| 公式 | `1 − (null_cnt + empty_cnt) / row_count` |
| 分母 | 该口径范围（全表或某机构）的**总行数** |
| 边界 | 空表（row_count=0）→ `—`；Oracle 源 empty_cnt 恒 0 |

#### 4.2.2 有效率 `VALID_RATE`（有值率的升级版）

有值率有个著名漏洞：HIS 里大量字段填 `'无'`、`'未知'`、`'-'`、`'/'`、`'0'` 占位。所以拆两层：

| 项 | 定义 |
|---|---|
| 公式 | `1 − (null_cnt + empty_cnt + placeholder_cnt) / row_count` |
| 占位值清单 | 默认内置：`{'无','未知','不详','-','--','/','\','N/A','NULL','null','0','.'}`，**用户可按字段自定义扩充**，配置存人工层 |
| 展示 | 有值率、有效率并排显示（如 `94.2% / 71.5%`），落差本身就是最强的质量信号 |

> 注意 `'0'` 是否算占位值有争议（有些字段 0 是合法值）。处理：占位清单默认对**字符串型**生效，数值型的 0 不算占位；单字段可覆盖（见 10.2 节决策点）。

#### 4.2.3 重复率 `DUP_RATE`（字段级）

| 项 | 定义 |
|---|---|
| 公式 | `1 − distinct_cnt / non_empty_cnt`，其中 `non_empty_cnt = row_count − null_cnt − empty_cnt` |
| 分母 | **有值行数**，不是总行数——10 万行表 9 万行 NULL、1 万个不同值，重复率应该是 0% 而不是"看不出来" |
| 边界 | non_empty_cnt = 0 → `—`；distinct 为近似值时整指标标估算，且按 4.1 节"估算钳制"规则防止出现负值 |
| 解读约定 | 该指标只在上下文里有意义：主键字段应 ≈0%，性别字段 ≈99% 是正常的。页面展示时不做红绿色判定，判定留给"主键候选"场景 |

#### 4.2.4 唯一率 `UNIQ_RATE`（重复率的正向表达，主键分析专用）

| 公式 | `distinct_cnt / non_empty_cnt` |
| 用途 | 外键推断的父侧准入（=1 且 non_empty_cnt = row_count 即物理唯一）；主键候选验证 |

#### 4.2.5 值域分布 `VALUE_DIST`

| 项 | 定义 |
|---|---|
| 统计对象 | 全部行，**NULL 作为独立一行参与分布**（NULL 占比是最重要的分布信息之一，不能丢） |
| pct 分母 | 总行数 row_count（含 NULL 行） |
| 排序 | freq 降序，freq 相同按 value 字典序升序（保证两次扫描结果可 diff） |
| 截断 | 低基数字段全量；高基数 TopN（默认 50）+ `__OTHER__` 归并行；`distinct ≥ 90% × row_count` 判定为近似唯一字段，不计算分布 |
| 展示 | NULL 行显示为 `(NULL)`，空串行显示为 `(空串)`，与真实值 `'NULL'` 字符串可区分；**存储即哨兵字符串**——`value` 是主键列不可存 NULL，存储层统一落 `'(NULL)'` / `'(空串)'`，展示层原样渲染；SQL 结果中的 NULL/空串到哨兵的映射发生在**落库渲染层**（不在 SQL 模板内），哨兵冲突按 4.1 节转义规则处理（v1.2 明确） |
| 依赖 | 分支决策需要 distinct_cnt，因此 value_dist 任务**依赖本表 column 任务完成**；column 失败则本表 value_dist 级联 skipped；distinct 为估算值时一律走保守分支（TopN 模板，不做字典全量）（v1.2 明确，见 6.2.9） |

#### 4.2.6 数值统计 `NUM_STATS`

| 指标 | 口径 |
|---|---|
| min/max/mean | 仅非 NULL 值参与 |
| median/p25/p75 | 仅非 NULL 值；连续型插值（PERCENTILE_CONT 语义）；MySQL 降级算法为中间两行均值 |
| 异常值 `ANOMALY` | 规则驱动：`anomaly_cnt / non_null_cnt`。内置规则如"年龄 <0 或 >150"、"体温 <30 或 >45"、"日期 > 扫描日+1天（未来日期）"，规则表可配置、按字段类型/字段名模式绑定 |

#### 4.2.7 样例数据 `SAMPLE`

| 项 | 定义 |
|---|---|
| 抽样方式 | 小表（<100万）`ORDER BY RAND()`；大表谓词采样（5 倍冗余取前 N）；超大表可用块采样并标注 |
| N | 默认 20 条，可配 |
| 敏感字段 | skip → 字段不出现在样例行 JSON 中（不是显示星号，是**根本不采集**）；mask → 采集脱敏后值 |
| 口径声明 | 页面固定文案："样例为随机抽样，仅用于感知数据形态，不构成统计推断" |

### 4.3 表级指标

#### 4.3.1 行数 `ROW_COUNT`

| 项 | 定义 |
|---|---|
| 精确口径 | `COUNT(*)` |
| 估算口径 | 取自数据库统计信息（MySQL `table_rows` / Oracle `num_rows`），标 `is_estimated`，注明"来自数据库统计信息，可能滞后" |
| 边界 | 视图：只算行数，跳过全部字段级指标（可配置强制扫） |

#### 4.3.2 完全重复行 `FULL_DUP`

| 公式 | `row_count − COUNT(DISTINCT 全字段组合)` |
| 默认 | **关闭**，用户按表手动触发；高代价任务受低峰窗口约束（见 6.4 节） |

#### 4.3.3 主键重复 `PK_DUP`

| 公式 | 按声明主键 `GROUP BY pk HAVING COUNT(*)>1` 的冗余行数；`pk_dup_rate = dup_pk_rows / row_count` |
| 边界 | 无声明主键 → 指标不存在（不是 0） |

#### 4.3.4 时间跨度 `DATE_SPAN`（v1.2 改为单阶段，删除重复扫描）

| 项 | 定义 |
|---|---|
| 业务日期字段的选择规则 | **单阶段选择**：column 阶段的单趟批处理已对所有 `std_type=date` 字段算出 min/max（落入 `column_stat.min_date/max_date`）；column 阶段完成后，在该表 `fill_rate ≥ 80%` 的日期字段中，按名称/注释关键词优先级选 1 个：`出院 > 就诊 > 入院 > 登记 > 收费 > 创建`；无命中则取 fill_rate 最高者；选定后 upsert 写入 `table_stat.date_column/min_date/max_date`（属 6.4.4 节允许的同快照追加写），选中字段名公开透明。**v1.2 删除 rowcount 阶段的日期扫描**——v1.1 的"先算后选"两阶段对日期字段扫了两遍全表，违反单趟聚合原则，且"原始结果暂存"在 DDL 中没有落点；column_stat 本身就是暂存层 |
| 指标 | `min_date` ~ `max_date` |
| 异常标记 | `max_date > 扫描时间 + 1天` → 标"存在未来日期"；`max_date < 扫描时间 − 365天` → 标"疑似历史归档库/接口停传"——后者是数据接入排查的高频信号 |

### 4.4 关系级指标（口径争议最大的地方，全部钉死）（V2）

设：子表 C（外键 fk），父表 P（主键 pk）。

#### 4.4.1 关联率 `MATCH_RATE`

| 项 | 定义 |
|---|---|
| 公式 | `matched_rows / (child_rows − fk_null_rows)` |
| 分母 | **外键非空的子表行数**。外键为 NULL 的行既不进入分子也不进入分母 |
| 理由 | 外键没填是"有值率问题"，填了但找不到是"关联问题"——两种病用两个指标诊断，混在一起哪个都看不清 |
| 配套列示 | 页面上永远同时展示 `fk 有值率 = (child_rows − fk_null_rows) / child_rows`，让读者自己还原全貌 |

#### 4.4.2 孤儿率 `ORPHAN_RATE`

| 公式 | `1 − match_rate`（同分母） |
| 孤儿样例 | 随机 20 条（脱敏），附值集层面的未命中值 TopN 及频次——孤儿数据按值聚合比按行展示有用得多。**v1.2：孤儿样例在关联率扫描时物化存入 `relation_stat.orphan_samples`（JSON），页面只读元数据仓库**——v1.1 未定义存储位置，而即席查询车道 10s 超时在大表 anti-join 上根本跑不完 |

#### 4.4.3 父表覆盖度 `PARENT_COVERAGE`

| 公式 | `parent_referenced_rows / parent_rows` |
| 分母 | 父表总行数 |
| 解读 | 患者主索引表覆盖度 60% = 四成患者在医嘱表里从未出现（可能是体检/挂号未就诊患者）——低覆盖不一定是问题，口径说明里必须写"需结合业务解读" |

#### 4.4.4 关联基数 `CARDINALITY`

| 统计对象 | 子表按 fk 分组（fk IS NOT NULL）的每组行数 n |
| 指标 | min / median / p95 / max |
| 关系形态判定 | max = 1 → 1:1；median = 1 且 max > 1 → 1:N（稀疏）；median > 1 → 1:N（常态）。判定结论只是提示，允许人工修正 |

#### 4.4.5 前提条件声明（每条关系指标页面固定展示）

- 父侧唯一性：物理主键 / 扫描验证唯一 / **未验证**（此时关联率标"参考值"，实际用的是安全版 SQL——见 6.2 节关联指标模板，无虚高，但要说明代价口径差异）；
- 联合外键：以上公式中 `c.fk = p.pk` 为全部列的等值 AND，NULL 判定为任一列 NULL 即 NULL；
- 等值比较归一化：等值比较前默认两侧 TRIM，归一化级别为关系级配置（`meta_relation.compare_rule`，见 5.3.2 节）。未归一化导致的假性低关联率（如 `'ZYH001 '` 尾空格）是医疗库高频事件，口径必须随指标展示；
- **TRIM 的性能代价与对策**（v1.2 新增）：`TRIM(c.fk) = TRIM(p.pk)` 使父表索引失效，大表关联只能走全量 hash join（无 hash join 的老版本 MySQL 上是灾难）。对策：① 关联率任务按代价等级受低峰窗口约束；② 大表（父表 > 1000 万行）发起 TRIM 关联前给出成本提示，建议改用 raw 比较先算一版，差异大再启用 TRIM；③ 更彻底的方案是预归一化——生成 `TRIM` 后的临时映射表或函数索引（需写权限，默认不用）。规则：能证明两侧无空格差异（值域分布中无尾空格值）时，适配器自动降级为 raw join 并记录。

### 4.5 维度口径（V2）

| 规则 | 内容 |
|---|---|
| 计算方式 | 全部指标 `GROUP BY {{dim_col}}` 得出各机构口径 |
| 全库口径 | **单独计算一次**（不带 GROUP BY），不等于各机构之和——落库用哨兵值区分：全库口径行 `dim_value='__ALL__'`；维度字段为 NULL 或不在字典内的未归类行归入 `'__UNCLASSIFIED__'` 桶，各机构 + 未归类才等于全库；`dim_value` 一律不存 NULL（见 5.1 节原则 3） |
| 机构对比口径 | 横向对比时用同一快照、同一口径版本的数据，页面注明快照时间；跨快照对比机构数据是常见误用，UI 上不允许直接并排 |
| 机构数上限 | 维度 distinct 值 > 50（可配）拒绝维度扫描，防止维度字段配错 |

### 4.6 字典匹配率（V3）

| 指标 | 公式 | 分母 |
|---|---|---|
| 行级匹配率 `DICT_MATCH_RATE`（主口径） | `matched_rows / total_rows` | 字段非空行数 |
| 取值级覆盖率 `DICT_VALUE_COVERAGE`（辅助） | `matched_distinct / distinct_values` | 非空取值种数 |

| 边界规则 | 内容 |
|---|---|
| 比对前归一化 | 两侧 TRIM；大小写敏感与否为绑定级配置（默认敏感） |
| 两种模式 | `code` 比对 item_code；`name` 比对 item_name（名称模式默认开启同义词归一，如"男/M/男性"） |
| 未命中明细 | 按频次 TopN 50 展示，这是整改清单的直接素材 |

### 4.7 库级聚合指标（L0 总览）

聚合方式最容易含糊，逐个钉死：

| 指标 | 口径 |
|---|---|
| 库平均有值率 | **字段简单平均**：Σ fill_rate / 参与统计的字段总数。不用行数加权（一张 8000 万行的日志表会把全院指标带歪）；加权值可作为辅助指标并列 |
| 库物理外键覆盖率 | 有 ≥1 个物理/确认外键的表数 / 总表数 |
| 库关联健康度 | 全部 active 关系 match_rate 的**按子表行数加权**平均（关联率这里加权合理，因为孤儿行数本身就是影响面）（V2） |
| 质量综合评分 | **推迟至 V3**（v1.2 修订）。V3 启用前必须先定义：① 默认权重（有值率 40% + 关联率 30% + 字典匹配率 20% + 主键唯一率 10%）；② **缺项重归一规则**——无字典绑定/无关联关系时，缺项权重按比例分配给其余项，并在报告中显式声明"本次评分基于 N 项指标"；③ 评分公式和权重在报告导出时强制附带说明。MVP/V2 只出分项指标与问题榜单，不出总分 |

### 4.8 展示规范（前端统一遵守）

| 场景 | 规范 |
|---|---|
| 百分比 | 保留 1 位小数；`≥99.95%` 显示为 `>99.9%`（避免"100%"和"99.95%"无法区分，评级场景这个差异要命） |
| 估算值 | `~` 前缀 + 悬浮说明估算方式 |
| 无法计算 | `—`，悬浮说明原因（分母为 0 / 方言不支持 / 指标被跳过） |
| 大数字 | ≥1 万显示 `12.3万`，≥1 亿显示 `1.2亿`，悬浮显示精确值（千分位） |
| 指标悬浮卡 | 每个指标名悬浮显示口径定义卡（内容来自 `metric_registry`），从页面到报告口径一致 |

---

## 5. 元数据仓库设计

这是整个工具的数据地基。以下 DDL 用通用 ANSI 写法，DuckDB / SQLite / PostgreSQL 都能跑，差异点在 5.5 节说明。

### 5.1 设计原则

1. **快照驱动**：每次扫描产生一个 `snapshot_id`，所有扫描类指标都挂在快照下。结构元数据和统计指标永远不"原地更新"，只新增快照——这是快照对比、趋势分析的基础。
2. **双层分离**：扫描层（自动产生，重刷即弃）与人工层（用户编辑，跨快照存活）物理分表。人工层用 `(source_id, table_name, column_name)` 做业务主键，与快照无关，重扫不丢。
3. **维度复用**：所有统计表带 `dim_value` 列，用哨兵值而非 NULL 区分口径：`'__ALL__'` 表示全表口径，机构编码表示某机构口径，维度字段为 NULL 或不在字典内的未归类行归 `'__UNCLASSIFIED__'`——`dim_value` 一律不存 NULL（避免 NULL 既表"全表"又表"未归类"的二义性，也避开主键列含 NULL 的问题）。不为维度单独建表，一套 schema 两种粒度。
4. **时间可追溯**（v1.2 新增）：所有 stat 表带 `computed_at`（结果实际落库时间）。补算指标（人工确认关系后触发）允许追加进最新快照，页面用 `computed_at > snapshot.started_at` 判定并标注"该指标为补算，计算时间晚于快照时间"——快照本体不可变，但可追加，追加行自带时间戳使这一行为可审计。

### 5.2 ER 概览

```
data_source ──┬── scan_snapshot ──┬── meta_table ──┬── meta_column
              │                   │                ├── table_stat
              │                   │                └── column_stat ── column_value_dist
              │                   ├── meta_relation ── relation_stat
              │                   └── sample_data
              ├── scan_task（扫描任务明细，调度依据）
              ├── dim_config（维度字段设置，人工层，V2）
              ├── sensitive_config（敏感字段，人工层，MVP 走配置文件）
              ├── placeholder_config / anomaly_rule / audit_log（占位值/异常值规则/审计，人工层）
              ├── column_annotation / table_annotation（人工注释，人工层，V2）
              ├── dictionary ── dict_item ── column_dict_binding ── dict_match_stat（V3）
              ├── custom_probe ── custom_probe_result（自定义探查模板，V3）
              └── metric_registry（口径登记册）
```

### 5.3 完整 DDL（按模块分组）

#### 5.3.1 数据源与扫描任务

```sql
-- 数据源：用户提供的"SQL执行接口"
CREATE TABLE data_source (
    source_id       BIGINT PRIMARY KEY,
    source_name     VARCHAR NOT NULL,          -- 如"XX医院HIS库"
    db_type         VARCHAR NOT NULL,          -- mysql/oracle/sqlserver/postgresql/hive...
    conn_config     TEXT,                      -- JSON，连接参数；连接凭据应用层强制加密存储，
                                               -- 密钥不入元数据仓库（环境变量/密钥文件注入）
    access_mode     VARCHAR DEFAULT 'direct',  -- direct=驱动直连 / gateway=HTTP SQL网关
    gateway_url     VARCHAR,                   -- access_mode=gateway 时使用
    remark          VARCHAR,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 扫描快照：一次扫描的完整上下文
CREATE TABLE scan_snapshot (
    snapshot_id     BIGINT PRIMARY KEY,
    source_id       BIGINT NOT NULL REFERENCES data_source(source_id),
    snapshot_no     INTEGER NOT NULL,          -- 该数据源第几次扫描
    status          VARCHAR DEFAULT 'running', -- created/running/done/failed/partial/canceled/paused
    scan_scope      TEXT,                      -- JSON：圈选的schema/业务域/表清单、采样阈值等参数（见 3.7 节）
    dim_enabled     BOOLEAN DEFAULT FALSE,     -- 本次是否执行了维度探查
    metric_def_version VARCHAR,                -- 本次扫描使用的"口径包"整体版本
    started_at      TIMESTAMP,
    finished_at     TIMESTAMP,
    error_log       TEXT,
    UNIQUE (source_id, snapshot_no)
);
```

> 质量综合评分字段已删除（评分推迟 V3，见 4.7 节），V3 启用时再加回 `quality_score`。
> `metric_def_version` 的设计理由：指标口径会随版本演进，快照记录当时所用口径版本，配合 `metric_registry` 使历史报告中的每个数字可复现、可解释。`metric_registry` 主键为 `(metric_code, def_version)`，同一指标多版本口径并存；MVP 阶段只需单版本，版本机制 V3 启用。

```sql
-- 扫描任务明细：断点续扫的依据；最小调度单元由 task_key 表达（见 6.4.1 节）
CREATE TABLE scan_task (
    task_id         BIGINT PRIMARY KEY,
    snapshot_id     BIGINT NOT NULL REFERENCES scan_snapshot(snapshot_id),
    task_key        VARCHAR NOT NULL DEFAULT '', -- 调度粒度键：表级任务=表名；关联任务=relation:<relation_id>；
                                                 -- 字典任务=dict:<binding_id>；自定义探查=probe:<probe_id>
    table_name      VARCHAR NOT NULL,
    phase           VARCHAR NOT NULL,          -- struct/rowcount/column/value_dist/relation/sample/dim
    status          VARCHAR DEFAULT 'pending', -- pending/ready/running/done/failed/skipped/canceled
    priority        INTEGER DEFAULT 5,         -- 调度优先级，对应 P0-P6（见 6.4 节）
    attempt         INTEGER DEFAULT 0,         -- SQL 执行失败已尝试次数，重试上限判断依据
    crash_count     INTEGER DEFAULT 0,         -- v1.2 新增：崩溃重置次数。僵尸任务重置为 ready 时
                                               -- attempt 不增加（不是它的错）但 crash_count+1，超限（默认3）
                                               -- 转 failed——防止"稳定搞挂 worker 的毒任务"无限循环
    worker_id       VARCHAR,                   -- 领取任务的 worker 标识
    heartbeat_at    TIMESTAMP,                 -- running 任务每 5 秒更新，崩溃恢复判僵尸用
    queued_at       TIMESTAMP,                 -- 进入 ready 队列时间
    depends_on      VARCHAR,                   -- JSON：前置依赖（阶段栅栏 或 表级任务依赖，见 6.4.1）
    cost_ms         BIGINT,                    -- 耗时，用于优化扫描调度与成本加权进度
    error_msg       TEXT,
    UNIQUE (snapshot_id, phase, task_key)
);
```

> `priority / attempt / crash_count / worker_id / heartbeat_at / queued_at / depends_on` 字段是扫描调度器（6.4 节）的直接支撑：优先级排序、指数退避重试、毒任务防护、僵尸任务识别、断点续扫都依赖它们，因此任务表在设计时一次到位。

#### 5.3.2 结构元数据（扫描层）

```sql
CREATE TABLE meta_table (
    snapshot_id     BIGINT NOT NULL,
    table_name      VARCHAR NOT NULL,
    table_comment   VARCHAR,
    schema_name     VARCHAR,                   -- v1 一次扫描限定单一 schema，此列仅作记录
    est_row_count   BIGINT,                    -- 数据库统计信息里的估算行数（免费获得）
    pk_columns      VARCHAR,                   -- JSON数组，如 ["patient_id","visit_id"]
    is_partitioned  BOOLEAN DEFAULT FALSE,     -- v1 仅记录，不做分区感知的探查优化
    PRIMARY KEY (snapshot_id, schema_name, table_name)
);

CREATE TABLE meta_column (
    snapshot_id     BIGINT NOT NULL,
    table_name      VARCHAR NOT NULL,
    column_name     VARCHAR NOT NULL,
    ordinal_pos     INTEGER,
    data_type       VARCHAR,                   -- 源库原始类型，如 NUMBER(10,2)
    std_type        VARCHAR,                   -- 归一化类型：string/number/date/bool/clob
    nullable        BOOLEAN,
    default_value   VARCHAR,
    column_comment  VARCHAR,
    PRIMARY KEY (snapshot_id, table_name, column_name)
);

-- 主外键关系：物理+逻辑+推断统一存放，source_type 区分来源（V2 全面启用）
CREATE TABLE meta_relation (
    relation_id     BIGINT PRIMARY KEY,
    snapshot_id     BIGINT,                    -- 物理外键：来自某次扫描；逻辑外键：NULL
    source_id       BIGINT NOT NULL,           -- 逻辑外键挂靠数据源，跨快照存活
    source_type     VARCHAR NOT NULL,          -- scanned=扫描发现 / manual=人工补充 / inferred=系统推断待确认
    child_table     VARCHAR NOT NULL,
    child_columns   VARCHAR NOT NULL,          -- JSON数组，支持联合外键
    parent_table    VARCHAR NOT NULL,
    parent_columns  VARCHAR NOT NULL,
    compare_rule    VARCHAR DEFAULT 'trim',    -- 等值比较归一化（见 4.4.5 节）：trim / trim_casefold / raw
    status          VARCHAR DEFAULT 'active',  -- active / pending（推断待确认）/ disabled（人工否掉）
    infer_evidence  TEXT,                      -- JSON：推断证据（见 6.3 节）
    created_by      VARCHAR,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

> `meta_relation.snapshot_id` 为什么可为 NULL：物理外键来自某次扫描，挂快照；人工补充的逻辑外键与快照无关、按 `source_id` 挂靠，所以重扫之后逻辑外键还在，直接用最新快照重算关联率。
> **多 schema 支持边界（v1）**：一次扫描限定单一 schema——Oracle 多 schema 库按 schema 拆成多次扫描/多个数据源。`meta_table.schema_name` 保留仅作记录；下游表不带 `schema_name` 是有意的 v1 边界。

#### 5.3.3 统计指标（扫描层，全部带 `dim_value` 与 `computed_at`）

```sql
-- 表级指标
CREATE TABLE table_stat (
    snapshot_id     BIGINT NOT NULL,
    table_name      VARCHAR NOT NULL,
    dim_value       VARCHAR NOT NULL,          -- '__ALL__'=全表口径；'0871XXXX'=某机构；'__UNCLASSIFIED__'=维度未归类
    row_count       BIGINT,
    is_estimated    BOOLEAN DEFAULT FALSE,
    min_date        TIMESTAMP,                 -- 选定的业务日期字段范围（选择规则见 4.3.4）
    max_date        TIMESTAMP,
    date_column     VARCHAR,
    dup_row_count   BIGINT,                    -- 完全重复行数（可选计算，贵）
    computed_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,  -- v1.2：落库时间，补算标注依据
    PRIMARY KEY (snapshot_id, table_name, dim_value)
);

-- 字段级指标
CREATE TABLE column_stat (
    snapshot_id     BIGINT NOT NULL,
    table_name      VARCHAR NOT NULL,
    column_name     VARCHAR NOT NULL,
    dim_value       VARCHAR NOT NULL,
    row_count       BIGINT,                    -- 分母（该口径下总行数）
    null_count      BIGINT,
    empty_count     BIGINT,
    fill_rate       DOUBLE,
    placeholder_count BIGINT,
    valid_rate      DOUBLE,
    distinct_count  BIGINT,
    is_distinct_est BOOLEAN DEFAULT FALSE,
    distinct_skipped BOOLEAN DEFAULT FALSE,    -- v1.2 新增：方言不支持且表过大时跳过 distinct（MySQL 大表）
    dup_rate        DOUBLE,
    -- 数值型统计（非数值型为NULL）
    min_val         DOUBLE, max_val DOUBLE,
    mean_val        DOUBLE, median_val       DOUBLE,
    p25_val         DOUBLE, p75_val DOUBLE,
    anomaly_count   BIGINT,
    -- 日期型统计（DATE_SPAN 的选择数据源，见 4.3.4）
    min_date        TIMESTAMP, max_date TIMESTAMP,
    -- 通用
    top_value       VARCHAR,                   -- 众值（敏感 skip 字段不填）
    top_value_pct   DOUBLE,
    is_dict_like    BOOLEAN,
    sample_skipped  BOOLEAN DEFAULT FALSE,
    cost_ms         BIGINT,
    computed_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,  -- v1.2
    PRIMARY KEY (snapshot_id, table_name, column_name, dim_value)
);

-- 值域分布：一个字段N行
CREATE TABLE column_value_dist (
    snapshot_id     BIGINT NOT NULL,
    table_name      VARCHAR NOT NULL,
    column_name     VARCHAR NOT NULL,
    dim_value       VARCHAR NOT NULL,
    value           VARCHAR NOT NULL,          -- 取值（统一转字符串存储）；NULL/空串落哨兵 '(NULL)'/'(空串)'，
                                               -- 冲突转义见 4.1 节
    freq            BIGINT,
    pct             DOUBLE,
    rank_no         INTEGER,                   -- 频次排名，NULL表示"其他"归并行
    is_other        BOOLEAN DEFAULT FALSE,
    computed_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,  -- v1.2
    PRIMARY KEY (snapshot_id, table_name, column_name, dim_value, value)
);

-- 关联率指标（V2）
CREATE TABLE relation_stat (
    snapshot_id     BIGINT NOT NULL,
    relation_id     BIGINT NOT NULL REFERENCES meta_relation(relation_id),
    dim_value       VARCHAR NOT NULL,
    child_rows      BIGINT,
    fk_null_rows    BIGINT,
    matched_rows    BIGINT,
    match_rate      DOUBLE,
    orphan_rows     BIGINT,
    orphan_samples  TEXT,                      -- v1.2 新增：JSON，物化的孤儿样例（随机20条脱敏 + 未命中值
                                               -- TopN及频次），扫描时写入，页面只读仓库（见 4.4.2）
    parent_rows     BIGINT,
    parent_referenced_rows  BIGINT,
    parent_coverage DOUBLE,
    card_min INTEGER, card_median DOUBLE, card_p95 DOUBLE, card_max INTEGER,
    cost_ms         BIGINT,
    computed_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,  -- v1.2：补算关系指标时晚于快照时间，页面标注
    PRIMARY KEY (snapshot_id, relation_id, dim_value)
);

-- 样例数据（敏感内容，独立存放便于一键清除；物理清除定义见 3.5 节）
CREATE TABLE sample_data (
    snapshot_id     BIGINT NOT NULL,
    table_name      VARCHAR NOT NULL,
    dim_value       VARCHAR NOT NULL,
    row_no          INTEGER,
    row_data        TEXT,                      -- JSON，整行；敏感字段已脱敏或已剔除
    sampled_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (snapshot_id, table_name, dim_value, row_no)
);
```

#### 5.3.4 人工层（跨快照存活；MVP 建表，配置走 YAML，界面 V2）

```sql
-- 维度字段设置（V2）
CREATE TABLE dim_config (
    dim_id          BIGINT PRIMARY KEY,
    source_id       BIGINT NOT NULL,
    dim_name        VARCHAR NOT NULL,
    field_rules     TEXT NOT NULL,             -- JSON：哪些表的哪些字段是维度，支持通配 {"*": ["org_code"]}
    default_value   VARCHAR,
    enabled         BOOLEAN DEFAULT TRUE
);

-- 敏感字段配置（MVP：YAML 配置文件导入此表）
CREATE TABLE sensitive_config (
    id              BIGINT PRIMARY KEY,
    source_id       BIGINT NOT NULL,
    table_name      VARCHAR,                   -- NULL + column_pattern 表示全局规则
    column_pattern  VARCHAR NOT NULL,          -- 精确字段名或正则，如 '(?i)id_?card'
    sensitive_type  VARCHAR,                   -- name/idcard/phone/address/mrn/other
    action          VARCHAR DEFAULT 'skip',    -- skip=不取样例 / mask=脱敏后取
    mask_rule       VARCHAR,
    auto_detected   BOOLEAN DEFAULT FALSE,
    confirmed       BOOLEAN DEFAULT FALSE
);

-- 字段人工注释（V2）
CREATE TABLE column_annotation (
    source_id       BIGINT NOT NULL,
    table_name      VARCHAR NOT NULL,
    column_name     VARCHAR NOT NULL,
    biz_name        VARCHAR,
    biz_desc        TEXT,
    biz_domain      VARCHAR,                   -- 业务域标签：门诊/住院/检验/药品...
    updated_by      VARCHAR,
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (source_id, table_name, column_name)
);

-- 表级标签（V2）
CREATE TABLE table_annotation (
    source_id       BIGINT NOT NULL,
    table_name      VARCHAR NOT NULL,
    biz_domain      VARCHAR,
    biz_desc      TEXT,
    PRIMARY KEY (source_id, table_name)
);

-- 占位值配置（见 4.2.2 节）
CREATE TABLE placeholder_config (
    id              BIGINT PRIMARY KEY,
    source_id       BIGINT NOT NULL,
    table_name      VARCHAR,                   -- NULL=全局规则
    column_name     VARCHAR,                   -- NULL=表级规则
    placeholder_value VARCHAR NOT NULL,
    enabled         BOOLEAN DEFAULT TRUE
);

-- 异常值规则（见 4.2.6 节）
CREATE TABLE anomaly_rule (
    rule_id         BIGINT PRIMARY KEY,
    source_id       BIGINT,                    -- NULL=内置全局规则
    rule_name       VARCHAR NOT NULL,
    column_pattern  VARCHAR,
    value_type      VARCHAR,                   -- number/date
    rule_expr       TEXT NOT NULL,             -- JSON 条件表达式，如 {"lt":0,"gt":150}
    builtin         BOOLEAN DEFAULT FALSE,
    enabled         BOOLEAN DEFAULT TRUE
);

-- 审计日志：记录敏感操作，即使单机版无用户体系也必须记录
CREATE TABLE audit_log (
    audit_id        BIGINT PRIMARY KEY,
    source_id       BIGINT,
    action          VARCHAR NOT NULL,          -- 关系确认/否决、样例导出、清空样例（含VACUUM结果）、数据源变更、字典变更等
    target          VARCHAR,
    actor           VARCHAR,
    detail          TEXT,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

#### 5.3.5 值域字典（V3）

```sql
CREATE TABLE dictionary (
    dict_id         BIGINT PRIMARY KEY,
    dict_code       VARCHAR UNIQUE,
    dict_name       VARCHAR NOT NULL,
    dict_type       VARCHAR DEFAULT 'custom',  -- standard=国标/行标 / custom=自建
    source_id       BIGINT,                    -- NULL=全局内置标准字典
    remark          VARCHAR
);

CREATE TABLE dict_item (
    dict_id         BIGINT NOT NULL REFERENCES dictionary(dict_id),
    item_code       VARCHAR NOT NULL,
    item_name       VARCHAR,
    parent_code     VARCHAR,
    PRIMARY KEY (dict_id, item_code)
);

CREATE TABLE column_dict_binding (
    binding_id      BIGINT PRIMARY KEY,
    source_id       BIGINT NOT NULL,
    table_name      VARCHAR NOT NULL,
    column_name     VARCHAR NOT NULL,
    dict_id         BIGINT NOT NULL,
    match_mode      VARCHAR DEFAULT 'code',
    created_by      VARCHAR,
    UNIQUE (source_id, table_name, column_name)
);

CREATE TABLE dict_match_stat (
    snapshot_id     BIGINT NOT NULL,
    binding_id      BIGINT NOT NULL,
    dim_value       VARCHAR NOT NULL,
    total_rows      BIGINT,
    matched_rows    BIGINT,
    match_rate      DOUBLE,
    unmatched_dist  TEXT,                      -- JSON：未匹配的取值及频次 TopN
    computed_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (snapshot_id, binding_id, dim_value)
);
```

#### 5.3.6 自定义探查模板（V3）

```sql
CREATE TABLE custom_probe (
    probe_id        BIGINT PRIMARY KEY,
    source_id       BIGINT,
    probe_name      VARCHAR NOT NULL,
    sql_template    TEXT NOT NULL,             -- 带 {{参数}} 占位
    param_def       TEXT,
    result_type     VARCHAR DEFAULT 'table',
    created_by      VARCHAR,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE custom_probe_result (
    run_id          BIGINT PRIMARY KEY,
    probe_id        BIGINT NOT NULL,
    snapshot_id     BIGINT,
    params          TEXT,
    result_data     TEXT,
    run_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

#### 5.3.7 口径登记册

```sql
CREATE TABLE metric_registry (
    metric_code   VARCHAR NOT NULL,      -- 如 FILL_RATE
    metric_name   VARCHAR NOT NULL,      -- 有值率
    formula       TEXT NOT NULL,         -- 人类可读的公式
    denominator   TEXT NOT NULL,         -- 分母说明
    edge_rules    TEXT,                  -- 边界处理
    def_version   VARCHAR NOT NULL,      -- 口径版本，如 v1.2
    PRIMARY KEY (metric_code, def_version)
);
```

> MVP 阶段只需单版本，版本机制 V3 启用。

### 5.4 关键设计说明

- **指标冗余存储**（如 `fill_rate` 算好存下来，不存公式）——页面查询全是单表直查，不做实时计算，这是"页面秒开"的关键。
- **`column_value_dist.value` 统一存字符串**：日期和数值也转字符串——值域分布本质是展示型数据，不参与计算，统一类型换来 schema 极简。注意数值 `1` 与字符串 `'1'`、浮点 `1.0` 与 `1` 转字符串后可能撞主键：落库时保留源类型标签（freq 合并或按 4.1 转义规则区分），同一字段内类型通常一致，冲突概率低但要有规则。
- **指标幂等 upsert**：所有任务的结果落库必须是 upsert（按主键覆盖写），绝不允许"先查有没有再插"。任务做到一半进程死了，重跑就是再写一遍，无副作用——这是一切断点续扫与崩溃恢复机制的地基（见 6.4.4 节）。
- **DuckDB 单写者约束**（v1.2 新增）：DuckDB 不允许多连接并发写。所有 worker 的 upsert 统一投递到一个**单写者线程的写入队列**（连接释放 → 结果解析 → 投递写队列），写冲突在架构上消除，而不是靠重试。PG 协作版不受此限，但同一写路径在两种后端上行为一致。

### 5.5 查询场景自检

设计完用几个页面真实查询验证 schema 是否够用：

| 页面查询 | SQL 形态 | 是否单表直查 |
|---|---|---|
| L0 总览：平均有值率 | `column_stat` 按 snapshot 实时聚合 | ✅ |
| L1 表列表 + 排序 | `table_stat` JOIN `meta_table`（取注释） | ✅ 两表 |
| L2 表详情字段表格 | `column_stat` 单表 | ✅ |
| 字段值域分布图 | `column_value_dist` 单表 | ✅ |
| 机构切换后全页面刷新（V2） | 所有 stat 表加 `dim_value = ?` 条件 | ✅ 无额外 join |
| 两次快照对比（V3） | 同表按两个 `snapshot_id` 自关联；跨快照对齐必须用业务键（table_name / table_name+column_name / 关系四元组），不能用 relation_id | ✅ |
| 表详情展示人工注释（V2） | `meta_column` LEFT JOIN `column_annotation` | ✅ |
| 补算指标标注 | `computed_at > snapshot.started_at` 判定（v1.2） | ✅ |

唯一较重的查询是 L0 总览的聚合（`column_stat` 约 2.5 万行，DuckDB 实时聚合毫秒级），L0 一律实时聚合，与"逐层显影"一致。

### 5.6 落地注意事项

1. **主键生成**：`relation_id`、`snapshot_id` 等用雪花 ID 或自增均可；DuckDB 用 `SEQUENCE`。
2. **方言差异**：DuckDB/SQLite 的 `BOOLEAN`、PostgreSQL 的 `TEXT` 都兼容上述写法；JSON 列统一用 `TEXT` 存字符串，应用层解析。
3. **清理策略**：快照删除时级联清掉 `meta_*`、`*_stat`、`column_value_dist`、`sample_data`（人工层绝不动）；`sample_data` 单独提供物理清空接口（DELETE + CHECKPOINT/VACUUM，见 3.5 节）。
4. **索引**：除主键外暂不加辅助索引；如果按有值率排序卡顿，再加 `(snapshot_id, fill_rate)`，先别过度设计。
5. **容量预估**：单库 500 表 × 平均 50 字段 ≈ 2.5 万行 `column_stat`；值域分布按每字段平均 30 个取值 ≈ 75 万行——DuckDB/SQLite 完全无压力。维度下钻会把 stat 类表放大 N 倍（N=机构数），所以维度探查务必圈选表执行。
6. **多 schema 边界**：v1 一次扫描限定单一 schema；Oracle 多 schema 库按 schema 拆多次扫描/多个数据源。
7. **同源库单活扫描**（v1.2 新增）：调度器硬约束——每个 data_source 同时最多一个 running/paused 快照（应用层在创建快照时校验并拒绝），防止并发扫描瓜分连接、源库负载翻倍、`snapshot_no` 竞争。DuckDB 不支持部分唯一索引，此约束由应用层 + 创建时事务内检查保证。

---

## 6. 探查引擎设计

### 6.1 方言适配器

用户提供的是"一个能执行 SQL 的接口"，所以引擎内部统一走 **SQL 模板 + 方言渲染**：

- **结构元数据查询**：MySQL 查 `information_schema`，Oracle 查 `all_tab_columns`/`all_constraints`，SQL Server 查 `sys.*`，PostgreSQL 用 `information_schema`，各写一套模板（见 6.2.2 节）；
- **统计探查 SQL**：用标准 SQL 生成，方言差异点由适配器处理，差异片段抽象成方言函数（对照表见 6.2.8 节）；
- **方言能力位**（v1.2 新增）：适配器显式声明能力矩阵——`approx_distinct` / `percentile` / `tablesample` / `grouping_sets` / `cancel_sql`，模板渲染前查能力位决定走精确/近似/合并/跳过哪条路径，不支持的能力在指标上标"该方言不支持"而不是静默降级；
- **两种接入通道**：适配器要同时支持 `direct`（驱动直连数据库）和 `gateway`（HTTP SQL 网关）两种通道的抽象。**v1 仅实现 direct**；gateway 保留字段与适配器抽象，V2 落地时需补降级矩阵（无法 cancel、并发不可控、结果集截断）；
- **单 schema 边界**：一次扫描限定单一 schema；Oracle 多 schema 库按 schema 拆多次扫描/多个数据源；
- **扫描策略原则**：
  - 分阶段扫描：① 结构元数据（秒级）→ ② 表行数（分钟级）→ ③ 字段指标 → ④ 值域分布 → ⑤ 样例 →（V2：关联率、维度），每阶段完成即可在页面上看到对应部分，不用等全扫完；
  - 首扫预算：扫描启动前基于 `est_row_count` 与任务代价模型估算总时长并展示，提示用户调整范围（跳过大表、先只扫结构、按业务域圈选——见 3.7 节）；全库首轮扫描在 500 表规模下预计 10~50 小时量级，**此数在阶段 0 基准测试后校准**，必须让用户在发起前知情；
  - 大表采样：超过阈值的表，指标可切换为采样估算（`TABLESAMPLE` 或主键范围抽样），页面上明确标注"估算值"；
  - 并发与限流：可配并发度、单 SQL 超时、失败重试；只发 SELECT，建议要求只读账号；
  - 断点续扫：每个探查任务落库记录状态，中断后可续扫；
  - 增量扫描：v1 不做——增量需要"结构 hash 变化检测 + 表级变更识别"的完整设计，列入 backlog，避免口号式功能。

### 6.2 探查 SQL 模板库

本节是引擎的"弹药库"。先定模板规范，再按模块给 SQL，最后是方言渲染表和执行策略。

#### 6.2.1 模板规范

- 占位符用 `{{...}}`：`{{table}}`、`{{column}}`、`{{topn}}`、`{{sample_n}}`、`{{dim_col}}` 由引擎渲染；方言相关片段单独抽象成方言函数，见 6.2.8 节。
- 所有指标 SQL 都遵循**单趟聚合**原则：能一次 `SELECT` 扫出来的绝不分两次，源库扫描次数是最贵的资源。**该原则同样约束值域分布**（见 6.2.4 GROUPING SETS）与日期跨度（见 4.3.4，v1.2 已删除重复扫描）。
- 每个模板标注**代价等级**（低/中/高），供扫描调度器（6.4 节）决策：大表只跑低代价指标 + 采样估算。

#### 6.2.2 结构元数据（代价：低）

**MySQL：**

```sql
-- 表
SELECT table_name, table_comment, table_rows AS est_row_count
FROM information_schema.tables
WHERE table_schema = DATABASE() AND table_type = 'BASE TABLE';

-- 字段
SELECT table_name, column_name, ordinal_position, column_type,
       is_nullable, column_default, column_comment, column_key
FROM information_schema.columns
WHERE table_schema = DATABASE()
ORDER BY table_name, ordinal_position;

-- 外键
SELECT kcu.table_name      AS child_table,
       kcu.column_name     AS child_column,
       kcu.referenced_table_name  AS parent_table,
       kcu.referenced_column_name AS parent_column,
       kcu.constraint_name
FROM information_schema.key_column_usage kcu
WHERE kcu.table_schema = DATABASE()
  AND kcu.referenced_table_name IS NOT NULL;
```

**Oracle：**

```sql
SELECT t.table_name, c.comments AS table_comment, t.num_rows AS est_row_count
FROM all_tables t LEFT JOIN all_tab_comments c
  ON t.owner = c.owner AND t.table_name = c.table_name
WHERE t.owner = {{schema}};

SELECT c.table_name, c.column_name, c.column_id, c.data_type,
       c.data_length, c.nullable, cc.comments
FROM all_tab_columns c LEFT JOIN all_col_comments cc
  ON c.owner = cc.owner AND c.table_name = cc.table_name AND c.column_name = cc.column_name
WHERE c.owner = {{schema}};

SELECT a.table_name AS child_table, a.column_name AS child_column,
       c_pk.table_name AS parent_table, b.column_name AS parent_column
FROM all_cons_columns a
JOIN all_constraints c  ON a.constraint_name = c.constraint_name AND a.owner = c.owner
JOIN all_constraints c_pk ON c.r_constraint_name = c_pk.constraint_name AND c.r_owner = c_pk.owner
JOIN all_cons_columns b ON c_pk.constraint_name = b.constraint_name AND c_pk.owner = b.owner
                       AND a.position = b.position
WHERE c.constraint_type = 'R' AND a.owner = {{schema}};
```

SQL Server（`sys.tables`/`sys.columns`/`sys.foreign_key_columns`）和 PostgreSQL（`information_schema` 基本同 MySQL 写法）结构类似，不展开，适配器里各写一份。

#### 6.2.3 表级指标

**行数（代价：低-中）：**

```sql
SELECT COUNT(*) FROM {{table}};
```

决策逻辑：`est_row_count`（结构元数据里白拿的）< 阈值（如 500 万）→ 精确 COUNT；≥ 阈值 → 先用估算值并标 `is_estimated=true`，精确 COUNT 放入"低峰任务"由用户手动触发。

**完全重复行数（代价：高，默认关闭）：**

```sql
SELECT COUNT(*) - COUNT(DISTINCT {{all_cols_concat_or_hash}})
FROM {{table}};
```

全字段 `COUNT(DISTINCT)` 大表基本不可行。替代方案：仅当存在物理主键时算**主键重复**（代价中）：

```sql
SELECT COUNT(*) AS dup_pk_rows FROM (
  SELECT {{pk_cols}} FROM {{table}}
  GROUP BY {{pk_cols}} HAVING COUNT(*) > 1
) t;
```

#### 6.2.4 字段级核心指标（重头戏）

**单趟批处理模板（代价：中）。** 不要把每个字段拆成一条 SQL——500 表 × 50 字段就是 2.5 万次全表扫描。正确姿势是**每表一趟、字段分组聚合**，生成形如：

```sql
SELECT
  COUNT(*) AS row_count,

  -- 字段1：字符串型
  SUM(CASE WHEN {{col1}} IS NULL THEN 1 ELSE 0 END)            AS {{col1}}__null_cnt,
  SUM(CASE WHEN TRIM(CAST({{col1}} AS VARCHAR)) = ''
            THEN 1 ELSE 0 END)                                  AS {{col1}}__empty_cnt,
  SUM(CASE WHEN TRIM(CAST({{col1}} AS VARCHAR)) IN ({{placeholder_list}})
            THEN 1 ELSE 0 END)                                  AS {{col1}}__placeholder_cnt,
  COUNT(DISTINCT {{col1}})                                      AS {{col1}}__distinct_cnt,

  -- 字段2：数值型（有值率 + 数值统计一趟出）
  SUM(CASE WHEN {{col2}} IS NULL THEN 1 ELSE 0 END)            AS {{col2}}__null_cnt,
  COUNT(DISTINCT {{col2}})                                      AS {{col2}}__distinct_cnt,
  MIN({{col2}})                                                 AS {{col2}}__min,
  MAX({{col2}})                                                 AS {{col2}}__max,
  AVG(CAST({{col2}} AS DOUBLE))                                 AS {{col2}}__mean,

  -- 字段3：日期型（min/max 在此产出，供 4.3.4 单阶段选择）
  SUM(CASE WHEN {{col3}} IS NULL THEN 1 ELSE 0 END)            AS {{col3}}__null_cnt,
  MIN({{col3}})                                                 AS {{col3}}__min_date,
  MAX({{col3}})                                                 AS {{col3}}__max_date
FROM {{table}};
```

**生成规则：**

| 字段类型 | 生成的聚合 |
|---|---|
| string | null_cnt + empty_cnt + placeholder_cnt + distinct_cnt |
| number | null_cnt + distinct_cnt + min/max/mean |
| date | null_cnt + min/max |
| 命中 anomaly_rule 的 number/date | 额外生成 `SUM(CASE WHEN <规则条件> THEN 1 ELSE 0 END) AS {{col}}__anomaly_cnt` |
| clob/blob/longtext | **只算 null_cnt**，跳过 distinct，页面上标注"大字段，指标受限" |

占位清单 `{{placeholder_list}}` 来自 `placeholder_config` + 内置默认值（口径见 4.2.2 节）。

**分批策略**：单表字段 > 30 个时拆成多条 SQL（每条 ≤ 30 个字段），避免一条 SQL 几十个 `COUNT(DISTINCT)` 把执行计划和内存打爆（MySQL 多 distinct 同值去重器会互相拖慢，Presto/Spark 会 spill）。30 是经验值，做成可配参数。

**distinct 的精确、近似与跳过（v1.2 修订）：**

```sql
-- 精确
COUNT(DISTINCT {{column}})
-- 近似（方言能力位 approx_distinct=true 且大表时默认用，is_distinct_est=true）
APPROX_COUNT_DISTINCT({{column}})
```

- MySQL 8 无原生 approx distinct：**大表（est_row_count ≥ 阈值）不再降级精确**——30 个精确 `COUNT(DISTINCT)` 是 MySQL 大表上最贵的单点。默认跳过（`distinct_skipped=true`，页面标"方言不支持"），用户在页面/配置中可对单表手动触发精确 distinct（进低峰窗口）。小表照旧精确。

**中位数与分位数（代价：中-高，方言差异最大）：**

```sql
-- Oracle / SQL Server / PG（PG 用 ordered-set 语法）
SELECT PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY {{column}}) AS p25,
       PERCENTILE_CONT(0.5)  WITHIN GROUP (ORDER BY {{column}}) AS median,
       PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY {{column}}) AS p75
FROM {{table}} WHERE {{column}} IS NOT NULL;

-- Presto / Spark / DuckDB
SELECT approx_percentile({{column}}, ARRAY[0.25, 0.5, 0.75]) FROM {{table}};

-- MySQL 8 无原生分位数 → 降级方案：只对行数 < 阈值的表用窗口函数算
SELECT AVG(val) AS median FROM (
  SELECT {{column}} AS val,
         ROW_NUMBER() OVER (ORDER BY {{column}}) AS rn,
         COUNT(*)    OVER ()                     AS cnt
  FROM {{table}} WHERE {{column}} IS NOT NULL
) t
WHERE rn IN (FLOOR((cnt+1)/2), CEIL((cnt+1)/2));
```

策略：min/max/mean 随主趟免费出；**分位数单独一条 SQL，且只对数值型 + 行数 < 阈值的表执行**，MySQL 源大表直接默认跳过，标"该方言不支持"。

**值域分布（v1.2 修订：GROUPING SETS 合并 + TopN 模板）：**

原则：低基数字段的值域分布**按表合并成一趟**——方言能力位 `grouping_sets=true`（Oracle / PG / Presto / Spark / SQL Server）时：

```sql
-- 一趟出同表全部待分布列（每列 GROUPING SETS 一组），NULL 自然成行
SELECT GROUPING({{col1}}) AS g1, GROUPING({{col2}}) AS g2,
       {{col1}}, {{col2}}, COUNT(*) AS freq
FROM {{table}}
GROUP BY GROUPING SETS (({{col1}}), ({{col2}}));
-- 落库渲染层按 grouping 位拆分各列分布，NULL 行映射哨兵（见 4.2.5）
```

一条 SQL 替掉原来按列逐条的 N 次全表扫描。MySQL 8 不支持 GROUPING SETS → 降级为逐列 `GROUP BY`（每列一趟，MySQL 源的固有额外成本，写进首扫估算）。

高基数 / 估算 distinct 走 TopN 模板（每列一条，无法合并）：

```sql
WITH dist AS (
  SELECT {{column}} AS value, COUNT(*) AS freq
  FROM {{table}}
  WHERE {{column}} IS NULL                    -- 先出NULL单独一行
  GROUP BY {{column}}
  UNION ALL
  SELECT {{column}}, COUNT(*) FROM {{table}} WHERE {{column}} IS NOT NULL GROUP BY {{column}}
),
ranked AS (
  SELECT value, freq,
         ROW_NUMBER() OVER (ORDER BY freq DESC) AS rn,
         SUM(freq)   OVER ()                   AS total
  FROM dist
)
SELECT CASE WHEN rn <= {{topn}} THEN CAST(value AS VARCHAR)
            ELSE '__OTHER__' END AS value,
       SUM(freq)                AS freq,
       SUM(freq) * 1.0 / MAX(total) AS pct,
       MIN(rn)                   AS rank_no
FROM ranked
GROUP BY CASE WHEN rn <= {{topn}} THEN CAST(value AS VARCHAR)
              ELSE '__OTHER__' END
ORDER BY freq DESC;
```

分支决策（依赖本表 column 任务产出，见 4.2.5 / 6.2.9）：

- 前置：`skip` 类敏感字段跳过值域分布，不生成此 SQL（见 3.5 节）；
- `distinct_cnt ≤ dict_threshold`（精确 distinct）→ 走 GROUPING SETS 合并全量分布，这就是值域字典的原始材料；
- `distinct_cnt > dict_threshold` 或 **distinct 为估算/被跳过** → 保守分支：TopN 模板，`{{topn}}` 默认 50；
- `distinct_cnt > row_count × 0.9`（疑似主键/流水号）→ **跳过值域分布**，页面上标"近似唯一字段，不做分布"。

**样例数据（代价：低-中）：**

```sql
-- 朴素版（小表）
SELECT {{col_list}} FROM {{table}} ORDER BY {{rand_func}}() {{limit_n}};

-- 大表版：谓词采样，避免全表排序
SELECT {{col_list}} FROM {{table}}
WHERE {{rand_func}}() < {{sample_n}} * 5.0 / {{row_count}}   -- 5倍冗余防不足
{{limit_n}};
```

- `{{col_list}}` 渲染时**剔除 `skip` 类敏感字段**，`mask` 类字段包脱敏表达式；
- 方言注记：SQL Server 谓词采样须用 `ABS(CHECKSUM(NEWID())) % 100 < pct`——`RAND()` 在一条查询内每行同值，不可用于谓词；
- Oracle 可用 `SAMPLE({{pct}})`、SQL Server / PG 用 `TABLESAMPLE` 更高效，但块采样随机性差，医疗数据按时间聚簇严重，建议仅在超大表使用，并在页面注明"块采样"。

#### 6.2.5 关联指标（每对关系 2-3 条 SQL）（V2）

**5.1 正向：外键命中率（代价：中，一趟出全）：**

```sql
SELECT
  COUNT(*)                                              AS child_rows,
  SUM(CASE WHEN c.{{fk}} IS NULL THEN 1 ELSE 0 END)    AS fk_null_rows,
  SUM(CASE WHEN p.{{pk}} IS NOT NULL THEN 1 ELSE 0 END) AS matched_rows
FROM {{child_table}} c
LEFT JOIN {{parent_table}} p ON c.{{fk}} = p.{{pk}};
-- orphan_rows = child_rows - fk_null_rows - matched_rows（应用层算）
-- 渲染时按 meta_relation.compare_rule 包裹两侧 join 表达式（默认两侧 TRIM，性能对策见 4.4.5）
```

⚠️ 前提：`{{pk}}` 在父表唯一（有物理主键约束，或 `column_stat` 里验证过 `dup_rate=0`），否则 LEFT JOIN 会扇出导致 matched_rows 虚高。父侧唯一性为"近似验证"（`is_distinct_est=true`）视同"未验证"——近似算法无法认证零重复，一律走安全版 SQL：

```sql
SELECT COUNT(*) AS child_rows,
       SUM(CASE WHEN fk IS NULL THEN 1 ELSE 0 END) AS fk_null_rows,
       SUM(CASE WHEN pk IS NOT NULL THEN 1 ELSE 0 END) AS matched_rows
FROM {{child_table}} c
LEFT JOIN (SELECT DISTINCT {{pk}} FROM {{parent_table}}) p
  ON c.{{fk}} = p.{{pk}};
```

**5.2 反向：父表被引用覆盖度（代价：中）：**

```sql
SELECT COUNT(*)                                   AS parent_rows,
       COUNT(f.fk)                                AS parent_referenced_rows
FROM {{parent_table}} p
LEFT JOIN (SELECT DISTINCT {{fk}} FROM {{child_table}} WHERE {{fk}} IS NOT NULL) f
  ON p.{{pk}} = f.fk;
```

**5.3 关联基数分布 1:N（代价：中-高）：**

```sql
SELECT MIN(n) AS card_min, MAX(n) AS card_max,
       {{median_func}}(n) AS card_median,
       {{p95_func}}(n)    AS card_p95
FROM (
  SELECT {{fk}}, COUNT(*) AS n
  FROM {{child_table}}
  WHERE {{fk}} IS NOT NULL
  GROUP BY {{fk}}
) t;
```

**5.4 孤儿数据样例（扫描时物化，见 4.4.2）：**

```sql
SELECT c.{{fk}}, c.{{col_list_safe}}
FROM {{child_table}} c
LEFT JOIN {{parent_table}} p ON c.{{fk}} = p.{{parent_key}}
WHERE c.{{fk}} IS NOT NULL AND p.{{parent_key}} IS NULL
{{limit_20}};
```

结果（含未命中值 TopN 及频次）物化进 `relation_stat.orphan_samples`，页面不触发即席 anti-join。`{{parent_key}}` = 父侧被引用列；JOIN 与 WHERE 两处必须渲染为同一列。

**5.5 级联分析：A join B 后按维度看分布（V2 即席查询）：**

```sql
SELECT b.{{group_col}} AS group_val, a.{{target_col}} AS target_val, COUNT(*) AS freq
FROM {{table_a}} a
JOIN {{table_b}} b ON a.{{fk}} = b.{{pk}}
GROUP BY b.{{group_col}}, a.{{target_col}}
ORDER BY group_val, freq DESC
{{limit_500}};
```

#### 6.2.6 字典匹配率（V3，代价：低-中）

先按取值聚合再比对，行级/取值级两个口径一趟出：

```sql
WITH dist AS (
  SELECT {{column}} AS value, COUNT(*) AS freq
  FROM {{table}} WHERE {{column}} IS NOT NULL
  GROUP BY {{column}}
)
SELECT SUM(freq)                                            AS total_rows,
       SUM(CASE WHEN d.item_code IS NOT NULL THEN freq ELSE 0 END) AS matched_rows,
       COUNT(*)                                             AS distinct_values,
       SUM(CASE WHEN d.item_code IS NULL THEN 1 ELSE 0 END) AS unmatched_values
FROM dist v
LEFT JOIN dict_item d
  ON CAST(v.value AS VARCHAR) = d.item_code AND d.dict_id = {{dict_id}};
```

`match_mode='name'` 时把 join 条件换成 `d.item_name`。

#### 6.2.7 维度版：全部加一刀 GROUP BY（V2）

维度探查 = 上述模板统一改造：SELECT 加 `{{dim_col}} AS dim_value`，GROUP BY 加 `{{dim_col}}`：

```sql
SELECT {{dim_col}} AS dim_value,
       COUNT(*) AS row_count,
       SUM(CASE WHEN {{col1}} IS NULL THEN 1 ELSE 0 END) AS {{col1}}__null_cnt,
       ...
FROM {{table}}
GROUP BY {{dim_col}};
```

- 维度值本身先探查一遍：`SELECT {{dim_col}}, COUNT(*) FROM {{table}} GROUP BY {{dim_col}}`，维度基数 > 50（可配）时拒绝执行维度扫描并提示；
- 落库映射：渲染/落库时将维度 NULL 组映射为 `'__UNCLASSIFIED__'`，全库口径行写 `'__ALL__'`，`dim_value` 不存 NULL；
- 维度版 SQL 的代价 = 单趟版 × 1（GROUP BY 一把出所有机构，不是每机构跑一遍），绝不能用"按机构循环执行"的实现。

#### 6.2.8 方言渲染对照表（v1.2 增加能力位列）

| 片段 | MySQL | Oracle | SQL Server | PostgreSQL | Presto/Spark |
|---|---|---|---|---|---|
| `{{limit_n}}` | `LIMIT n` | `FETCH FIRST n ROWS ONLY`（12c+）/ `ROWNUM<=n` | `TOP n` | `LIMIT n` | `LIMIT n` |
| `{{rand_func}}` | `RAND` | `DBMS_RANDOM.VALUE` | `RAND`（谓词采样须用 `ABS(CHECKSUM(NEWID())) % 100 < pct`，排序抽样用 `NEWID()`） | `RANDOM` | `RAND` |
| 能力位 `approx_distinct` | ❌（大表跳过 distinct） | ✅ `APPROX_COUNT_DISTINCT` | ✅（2016+） | ❌（用精确） | ✅ `APPROX_DISTINCT` |
| 能力位 `percentile` | ❌（窗口降级，限小表） | ✅ `PERCENTILE_CONT WITHIN GROUP` | ✅ 同 Oracle | ✅ `percentile_cont WITHIN GROUP` | ✅ `approx_percentile` |
| 能力位 `tablesample` | ❌（谓词采样） | ✅ `SAMPLE(pct)` | ✅ `TABLESAMPLE (n PERCENT)` | ✅ `TABLESAMPLE BERNOULLI(pct)` | ✅ `TABLESAMPLE` |
| 能力位 `grouping_sets` | ❌（值域分布逐列降级） | ✅ | ✅ | ✅ | ✅ |
| 空串判断 | `TRIM(x)=''` | **`''` 即 NULL，空串判断恒为 0，直接跳过** | `LTRIM(RTRIM(x))=''` | `TRIM(x)=''` | `TRIM(x)=''` |

> **⚠️ Oracle 空串警告**：Oracle 里 `''` 等价于 `NULL`，`empty_cnt` 恒为 0。适配器必须知道这个语义差异，否则有值率口径在 Oracle 源上会出现"看起来更好"的假象——这种坑写进适配器注释里，是工具的护城河（对应 4.1 节"Oracle 特例"）。

#### 6.2.9 扫描执行的决策树（把模板串起来）（v1.2 修订）

```
每张表:
  ① est_row_count < 500万?
     ├─ 是 → 精确 COUNT；6.2.4 批处理全字段（含精确 distinct）
     └─ 否 → 用估算行数；6.2.4 照旧；
             MySQL 源 distinct 默认跳过（distinct_skipped）；其余方言大表用 approx
  ② 字段类型过滤: clob/blob 只算 null_cnt
  ③ column 任务 done 后（表级依赖，column 失败 → 本表 value_dist 级联 skipped）:
     ├─ 精确 distinct ≤ dict_threshold → 标 is_dict_like，GROUPING SETS 合并全量分布
     ├─ 精确 distinct > 90% 唯一 → 跳过分布
     └─ 其余（含估算/跳过的 distinct）→ TopN 模板（保守分支）
每对关系（V2）:
  ① 父表 pk 唯一性已验证? → 是：5.1 快速版 / 否：5.1 安全版(DISTINCT子查询)
  ② 5.2 覆盖度、5.3 基数默认对行数 Top50 的表执行，其余页面点击触发
  ③ TRIM 比较成本检查（见 4.4.5）：两侧值域无尾空格证据 → 自动降级 raw join
全库:
  ④ 维度配置存在（V2）→ 圈选表重跑维度版模板
```

> **三个阈值别混淆**：`dict_threshold`（默认 100，判定字典字段）、TopN（默认 50，分布展示截断）、维度基数上限（默认 50，防维度配错）——三者语义独立、分别可配，不要共用一个参数。

### 6.3 外键自动推断算法（V2）

这一块设计要克制——目标不是"自动推断出外键"，而是**"高置信候选 + 证据 + 一键确认"**。医疗库里脏数据多、孤儿数据是常态，全自动写死关系必然翻车；推断器只负责把人工大海捞针变成从 20 个候选里勾掉 3 个错的。

#### 6.3.1 总体流程：两阶段漏斗

```
全量字段对 (N表×M字段，组合爆炸)
   │
   ▼  阶段A：元数据级过滤（零成本，不碰源库，全部用已扫描结果）
   │    类型兼容 → 父侧唯一性 → 命名相似度 → 反模式黑名单
   │    剩 5~50 个候选对
   ▼  阶段B：值集验证（每候选对 1 条 SQL，值集包含率）
   │
   ▼  评分分级 → 写入 meta_relation(source_type='inferred', status='pending')
   │
   ▼  人工确认(active) / 否决(disabled)
```

关键前提：推断器**运行在扫描完成后**，阶段A需要的所有输入（distinct_cnt、dup_rate、fill_rate、data_type、字段注释）`column_stat` 里都有，一行 SQL 都不用发。

#### 6.3.2 阶段A：候选对生成（元数据过滤）（v1.2 修订子侧准入）

**A.1 父侧候选准入（谁是"主键"）。** 一个字段能当父侧（被引用方），必须满足其一：

| 条件 | 数据来源 |
|---|---|
| 物理主键约束 | `meta_table.pk_columns` |
| `dup_rate = 0` 且 `distinct_cnt = row_count`（经验证唯一；`is_distinct_est=true` 或 `distinct_skipped=true` 不得作为唯一性证据） | `column_stat` |
| 命名命中主键模式（`%_id`、`%ID`、`%_NO`、`编号`结尾）且 `dup_rate < 0.1%`（同样仅接受精确 distinct） | 命名规则 + `column_stat` |

**父侧硬性排除**（推断误报的重灾区）：

- `is_dict_like = true` 的字段：性别、状态标志——它们和谁都"关联得上"；
- 维度字段（`org_code` 等）：全院每张表都有，会炸出海量假关系，维度关系由 `dim_config` 单独表达；
- `fill_rate < 50%` 的字段：半空字段做主键没有意义；
- 日期型、数值型中的金额/数量字段：值域连续，不可能被引用。

**A.2 子侧候选准入（v1.2 修订）：**

- 类型与某父侧候选**兼容**（见 A.3）；
- 排除维度字段；**不再按 fill_rate 排除子侧**——医疗库里 fill_rate 30% 的真外键遍地都是（会诊医生 ID、上级医师 ID），v1.1 的统一 blacklist 会系统性漏报稀疏外键。低 fill 改为评分降权（见 6.3.4，子侧有值率权重项天然承担此职能）；
- 子侧允许 `is_dict_like` 字段参与，但如果匹配到的"父表"是系统内置字典而非业务表，**改道走字典绑定建议**（`column_dict_binding`），不产生外键候选。

**A.3 类型兼容矩阵：**

| 父侧类型 | 兼容子侧类型 | 备注 |
|---|---|---|
| VARCHAR(n) | VARCHAR/CHAR(n±容忍) | 长度不一致容忍，HIS 常见 |
| INT/BIGINT | INT/BIGINT/DECIMAL(整型) | 跨整型兼容 |
| NUMBER | NUMBER/VARCHAR(纯数字值) | 仅 Oracle 常见，需值验证兜底 |
| 其他 | 仅同类型 | 日期型不做推断 |

**A.4 命名相似度（核心排序依据，非过滤依据）。** 命名不相似但值集包含率高的关系是真实存在的（HIS 里 `PATIENT_ID` ↔ `BRID`、`住院号` ↔ `ZYH` 遍地都是），所以命名分只用于**排序和加分**，不做硬过滤。但对"命名强匹配"的候选优先进入阶段B。

归一化流程：

```
原始名 → 小写化 → 去表名前缀(如 PATIENT_ 表里的 PATIENT_ID → ID)
       → 去通用后缀(_id/_no/_code/_num/编号/代码/号)
       → 同义词映射(内置词表) → 比对
```

内置同义词表（医疗场景定制，这是工具的领域壁垒之一，且允许用户扩充）：

```
患者: patient, pat, br, hz, person, mrn, empi
住院: zy, inpatient, visit, admit, 住院号, zyh, bah, 病案
门诊: mz, outpatient, clinic, op
医嘱: order, ord, yz, advice
科室: dept, ks, ward, bq(病区)
人员/医生: doctor, doc, ys, staff, emp
药品: drug, yp, med, medicine
检验: lis, jy, lab
检查: pacs, jc, exam
```

相似度打分：

| 情形 | 得分 |
|---|---|
| 归一化后完全相等 | 1.0 |
| 同义词命中（`BRID` ↔ `PATIENT_ID`） | 0.8 |
| 编辑距离相似（`pat_id` ↔ `pat_id2`） | 0.5 |
| 字段**注释**语义相近 | 0.4 |
| 无任何命名关联 | 0.0（仍可进入阶段B，排在最后） |

**A.5 产出：** 候选对清单，按 `命名分` 降序，**封顶 K 对进入阶段B**（K 默认 50，可配）。

#### 6.3.3 阶段B：值集包含率验证

**B.1 方向判定。** 规则：**唯一性高的一方为父**。两侧都唯一 → 1:1 关系，按表语义词典猜方向，猜不出就都算，标注"1:1 待人工定方向"。

**B.2 包含率 SQL（子侧 distinct 值集 → 父侧查找）：**

```sql
WITH fks AS (
  SELECT DISTINCT {{fk}} AS v
  FROM {{child_table}}
  WHERE {{fk}} IS NOT NULL
)
SELECT COUNT(*)                                          AS distinct_fk_cnt,
       SUM(CASE WHEN p.{{pk}} IS NOT NULL
                THEN 1 ELSE 0 END)                       AS matched_cnt
FROM fks c
LEFT JOIN (SELECT DISTINCT {{pk}} FROM {{parent_table}}) p ON c.v = p.{{pk}};
-- inclusion_rate = matched_cnt / distinct_fk_cnt
```

- 用 `DISTINCT` 子查询而非全表 LEFT JOIN：验证阶段只关心**值集包含关系**，代价比关联率全量版便宜一个量级；
- 子表 > 5000 万行时，`fks` 改为 `TABLESAMPLE`/谓词采样后的 distinct，结果标注"采样验证"；
- 精确关联率（含行级权重、孤儿数）在人工确认后由正常的关联率扫描补齐，推断阶段不算。

**B.3 判定阈值：**

| 包含率 | 判定 | 置信度贡献 |
|---|---|---|
| ≥ 98% | 强包含 | 1.0（允许 2% 孤儿，医疗库历史脏数据常态，要求 100% 会误杀真关系） |
| 80% ~ 98% | 弱包含 | 0.6 |
| 30% ~ 80% | 可疑 | 0.2（可能是同值域巧合） |
| < 30% | 否 | 0，直接丢弃 |

反向校验防"巧合包含"：`distinct_fk_cnt < 20` 且父侧 `distinct_cnt > 1000×distinct_fk_cnt` 时，置信度强制降为 0.2 以下。

#### 6.3.4 综合评分与分级

```
score = 0.45 × 包含率得分
      + 0.30 × 命名相似度
      + 0.10 × 类型精确匹配(1/0.5/0)
      + 0.15 × 子侧有值率(≥95%得1，线性)
```

| score | 分级 | 处理方式 |
|---|---|---|
| ≥ 0.85 | 高置信 | 写入 `meta_relation`，status=`pending`，"关系确认"列表置顶，**仍不自动生效** |
| 0.6 ~ 0.85 | 中置信 | 写入，pending，列表正常展示 |
| 0.4 ~ 0.6 | 低置信 | 只进候选池（折叠区"更多候选"），不打扰主流程 |
| < 0.4 | 丢弃 | 不落库 |

**为什么高置信也不自动 active**：推断错的外键比没有外键更糟——它会污染关联率指标，用户基于错误的"关联率 23%"写进质量报告就是事故。自动化的边界停在"准备好证据等一键确认"。

#### 6.3.5 证据留存与人工闭环

推断证据存入 `meta_relation.infer_evidence`（JSON）：

```json
{"inclusion_rate":0.973, "distinct_fk_cnt":15230, "matched_cnt":14811,
 "naming_score":0.8, "type_score":1.0, "score":0.88, "verified_sampled":false,
 "sample_orphans":["ZYH000123","ZYH000457"]}
```

确认界面每条候选直接展示这些证据：包含率、样本孤儿值（脱敏后）、命名依据。用户决策成本 ≈ 看一眼。

闭环规则：

- 用户点**确认** → `status='active'`，触发该关系的关联率正式计算（补算指标以新行追加进最新快照，`computed_at` 晚于快照时间，页面据此标注——见 5.1 节原则 4）；
- 用户点**否决** → `status='disabled'`，**该 (child, fk, parent, pk) 四元组写入否决集，后续重扫永不再次推断**；
- 人工手动添加的关系（`manual`）与推断体系完全隔离，不受否决集影响；
- 重扫时结构未变的 `active` 推断关系直接继承，只重算指标不重新推断。

#### 6.3.6 伪代码（v1.2 修订子侧过滤）

```python
def infer_foreign_keys(source_id, snapshot_id):
    stats = load_column_stats(snapshot_id)
    parent_blacklist = dim_fields(source_id) | dict_like(stats) | low_fill(stats, 0.5)
    child_blacklist  = dim_fields(source_id)          # v1.2：子侧只排除维度字段

    parents = [c for c in stats
               if (is_declared_pk(c) or (c.dup_rate == 0 and c.distinct_exact))
               and c not in parent_blacklist]
    candidates = []
    for child_col in stats:
        if child_col in child_blacklist or not child_col.type_compatible: continue
        for p in parents:
            if p.table == child_col.table: continue               # 表内自引用不做
            if not types_compatible(p, child_col): continue
            ns = naming_score(child_col, p, synonym_dict)
            candidates.append(Candidate(child_col, p, ns))

    candidates.sort(key=lambda c: -c.naming_score)
    for c in candidates[:K]:                                       # K=50 预算
        c.inclusion, c.evidence = value_set_inclusion(c, sampled=child_col.row_count > 5e7)
        if c.distinct_fk_cnt < 20 and p.distinct_cnt > 1000 * c.distinct_fk_cnt:
            c.inclusion_score = min(c.inclusion_score, 0.2)        # 防巧合包含
        c.score = weigh(c)                                         # 低fill在评分中降权
        if c.score >= 0.4:
            persist(c, source_type='inferred', status='pending',
                    evidence=c.evidence, excluded=veto_set(source_id))
```

#### 6.3.7 成本与边界

1. **总成本**：阶段A 零 SQL；阶段B ≤ K 条轻量 SQL，全库推断增量耗时通常 < 5 分钟，作为扫描的收尾阶段。
2. **明确不做**：联合外键推断（留给 V3 且只在人工指定疑似联合键时验证）；表内自引用；跨库/跨 schema 推断。
3. **可选增强一**：同义词表和命中记录按"厂商 HIS 模板"沉淀——做过一次东华/卫宁/智业的库，下次同厂商库的推断几乎直接满分。这是长期复利的资产，建议在 `dictionary` 体系里给同义词表一张独立的可导入导出的表。
4. **可选增强二（v1.2 新增，对应语义层诉求）**：同一套同义词表 + 字段注释 + 值域分布，可自动生成**业务域标签建议**（这张表像检验域、那个字段像药品编码）——把"填注释"这个纯人工动作也变成确认制。列入 V2 backlog。

### 6.4 扫描调度器

先定调：这个调度器**不需要 Celery/RabbitMQ 这类分布式队列**——单机交付、单租户、一个数据源接一个数据源地扫，用"数据库任务表 + 进程内线程池"就够了。调度器的真正难点不在并发框架，而在三件事：**保护源库**、**断点续扫**、**交互查询不被扫描饿死**（V2 起有即席查询）。

#### 6.4.1 三层状态模型（v1.2 修订依赖表达）

```
快照层  scan_snapshot   created → running → done / partial / failed / canceled / paused
   │
阶段层  phase barrier   struct → rowcount → column → value_dist → sample → (V2: relation → dim) → finalize
   │
任务层  scan_task       pending → ready → running → done / failed / skipped / canceled
```

- **快照层**：用户可见的整体状态；**硬约束：每个 data_source 同时最多一个 running/paused 快照**（创建时校验，见 5.6 节）；
- **阶段层**：阶段间有依赖栅栏（barrier），不是严格串行——`sample` 与 `value_dist` 无依赖可并行；`value_dist` 除阶段栅栏外还有**表级依赖**：依赖本表 `column` 任务 done（需要 distinct_cnt 做分支决策），本表 column failed → 本表 value_dist 级联 skipped（v1.2 显式化）；`relation` 必须等 `column` 完成（父侧唯一性结论来自 `column_stat`）；`dim` 只依赖 `relation`；`finalize` 永远最后。外键推断（V2）不在阶段链中，统一在 finalize 阶段触发；
- **任务层**：最小调度单元由 `scan_task.task_key` 表达：表级任务 = 一张表 × 一个阶段；关联任务 = `relation:<relation_id>`（V2）；字典任务 = `dict:<binding_id>`（V3）；自定义探查 = `probe:<probe_id>`（V3）。`depends_on` 支持两种值：阶段栅栏（`["column"]`）与表级任务（`["task:12345"]`）。

#### 6.4.2 任务层状态机（核心）

```
                 ┌─────────┐
                 │ pending │  依赖未满足
                 └────┬────┘
            依赖满足    ▼
                 ┌─────────┐   被 worker 领取    ┌─────────┐
        ┌────────│  ready  │────────────────────→│ running │
        │        └─────────┘                     └────┬────┘
        │                                             │
        │              ┌──────────────┬───────────────┼──────────────┐
        │              ▼              ▼               ▼              ▼
        │           成功           可重试错误      不可重试错误     用户暂停/取消
        │              │              │               │              │
        │              ▼              ▼               ▼              ▼
        │           [done]      attempt++        [failed]       [canceled]
        │                       超限? ──是──→ [failed]               │
        │                          │否                             │
        └──────────────────────────┘         (skipped: 前置失败级联跳过)
```

**状态迁移规则表（v1.2 统一退避口径）：**

| 当前态 | 事件 | 下一态 | 附加动作 |
|---|---|---|---|
| pending | 前置阶段全 done 且表级依赖 done | ready | 入就绪队列 |
| ready | worker 领取 | running | 记录 worker_id、心跳 |
| running | SQL 成功 + 结果落库 | done | 触发依赖检查、更新进度 |
| running | 超时/连接断/死锁 | ready（attempt+1） | 退避 30s / 120s（第1/2次重试，固定两档） |
| running | attempt ≥ 2 仍失败 | failed | 写 error_msg |
| running | 权限错误/语法错误/表不存在 | failed（不重试） | 永久错误不重试，方言渲染 bug 要报警而不是静默 |
| running | 心跳丢失（进程崩溃恢复时发现） | ready（crash_count+1） | 见 6.4.4 节；crash_count ≥ 3 → failed（毒任务防护，v1.2） |
| 任意 | 前置任务 failed | skipped | 如 column 失败的表，value_dist 级联 skip |
| ready/running | 用户暂停快照 | 保持原态冻结 | running 任务允许跑完当前 SQL |

**错误分类**是重试策略的关键，适配器要把各数据库的错误码翻译成两类：

- **瞬时类**（重试）：连接超时、连接重置、锁等待超时、Oracle `ORA-01013`、SQL Server 死锁牺牲者 1205；
- **永久类**（直接 failed）：`ORA-00942` 表不存在、权限不足（`ORA-01031` / MySQL 1142）、SQL 语法错误。

#### 6.4.3 执行器架构：快慢双车道 + 单写者队列（v1.2 修订）

```
                    ┌──────────────────────────────┐
   扫描任务队列 ────→│  慢车道 worker 池（默认2个）   │──→ 源库连接A/B（扫描专用）
   (ready 按优先级)  │  串行领任务，带成本节流        │
                    └──────────┬───────────────────┘
                    ┌──────────┴───────────────────┐
   页面即席查询 ────→│  快车道 worker（1个，独占）    │──→ 源库连接C（交互专用，V2）
   (用户点击触发)    │  独立连接，永远空闲待命        │
                    └──────────────────────────────┘
                    ┌──────────────────────────────┐
   全部 worker 结果 ─→│  单写者线程 + 写入队列        │──→ 元数据仓库（DuckDB 单写者约束）
                    └──────────────────────────────┘
```

- 双车道：V2 开放即席查询后，用户点击不能被后台大表聚合堵住。**交互查询走独立连接 + 独立超时（10 秒）**。MVP 无即席查询，快车道可暂缓实现，但连接管理器的车道抽象 M1 就留好；
- **单写者队列**（v1.2 新增）：worker 执行完 SQL 先释放源库连接，在 worker 线程内解析结果，然后把"解析好的行集 + upsert 语句"投递到写入队列，由唯一的写者线程顺序落库。DuckDB 单写者约束在架构上消除，同时天然限流了落库峰值。

**优先级排序（ready 队列内部）：**

```
P0  struct        —— 秒级，先让用户看到表清单
P1  rowcount      —— 分钟级，L0 总览能出数据量
P2  column(非高代价)  —— 有值率/重复率，核心价值
P3  值域分布、样例
P4  relation 指标（V2）
P5  dim 维度版（V2）
P6  高代价任务（完全重复行、大表精确COUNT、分位数、MySQL 大表手动 distinct）
```

页面价值的兑现顺序 = 优先级顺序：用户发起扫描后 10 秒内页面有结构、1 分钟内有数据量、随后字段指标陆续填充。

**成本节流规则：**

- 每源库**最大并发连接数**（默认 2，可配 1-8）——保护生产库的硬闸门；
- 每条 SQL **statement timeout**（默认 120s，大表任务 600s），超时即取消走重试；
- 高代价任务（P6）**同时只允许 1 个在跑**，且仅在"业务低峰窗口"内执行；低峰窗口**无默认值，部署时必须与信息科确认后填写**——医院夜间恰是 HIS 跑批高峰；非窗口期 P6 任务保持 ready 不调度；
- worker 领任务前检查源库连接健康度，连接异常先重连再领。

#### 6.4.4 断点续扫与崩溃恢复（v1.2 修订毒任务防护）

**幂等写入（一切恢复机制的地基）。** 所有任务的结果落库必须是 **upsert**（按主键覆盖写），绝不允许"先查有没有再插"。任务做到一半进程死了，重跑就是再写一遍，无副作用。

**恢复流程（v1.2）：**

```
进程启动 → 扫描 scan_task WHERE status='running'
         → 这批任务是"僵尸"（worker 已死，心跳停在崩溃时刻）
         → 全部重置为 ready：attempt 不增加（不是它们的错），但 crash_count+1
         → crash_count ≥ 3 → 转 failed（毒任务：同一条任务反复搞挂进程，
           多半是超大结果集 OOM 或驱动崩溃，无脑重置会无限循环）
         → scan_snapshot status='running' 的自动续跑
```

**续扫 ≠ 重扫。** 用户手动点"继续扫描"一个 partial 快照时：done 的任务直接跳过，failed 的允许一键"重置失败任务"（→ ready，attempt 与 crash_count 清零）再续。"仅重扫选中表"**生成新快照**。

**快照可变性的精确口径（v1.2 对齐 5.1 原则 4）**：已落库的指标行不可变（只增不改，覆盖写仅发生在同任务重跑）；人工确认推断关系或修改逻辑外键后触发的补算指标，允许以新行追加进最新快照，`computed_at` 如实记录落库时间，页面以 `computed_at > snapshot.started_at` 判定并标注"补算"；快照对比（V3）只对两快照均存在的指标做 diff。

#### 6.4.5 快照层状态机与级联规则

```
created ──启动──→ running ──全部任务done──→ done
                    │──部分任务failed(>阈值)──→ partial   ← 大多数快照的真实终态
                    │──熔断触发──────────→ failed
                    │──用户暂停──────────→ paused ──恢复──→ running
                    └──用户取消──────────→ canceled（不可恢复）
```

- **partial 是正常终态**，不是失败：500 张表里 3 张视图权限不够，质量报告照样出。页面上要呈现为"完成（497/500，3 张失败可查看原因）"；
- **熔断器**：同一源库连续 10 个任务瞬时错误（连续计数在任何任务成功时清零）→ 判定源库/网络/账号出问题 → 快照转 failed，立即停止发 SQL。宁可误熔断让用户手动重启，不可在生产库上持续撞墙；
- **paused 的语义**：冻结调度，running 任务跑完当前 SQL 后不再领新任务。不做"杀 SQL 立即暂停"——主动 kill 生产库会话在某些库上本身就有风险。

#### 6.4.6 finalize 阶段（快照的收尾仪式）（v1.2 修订）

全部业务阶段结束后，单个 finalize 任务在**元数据仓库本地**执行（不碰源库）：

1. **DATE_SPAN 选定**（v1.2 从 column 阶段后移至此统一执行，见 4.3.4）：按选择规则从 `column_stat` 选定各表业务日期字段，upsert 写入 `table_stat`；
2. 触发外键自动推断（V2，算法见 6.3 节，纯本地 + 少量验证 SQL）；
3. 字典匹配率计算（V3，绑定存在的字段）；
4. 快照 status → done/partial，记录 finished_at。

> v1.1 中的"与上一快照做 diff 预计算"已删除——diff 结果没有存储表，且快照对比是 V3 功能，届时对 2.5 万行级数据实时 diff 毫无压力。质量综合评分计算同步删除（推迟 V3，见 4.7 节）。

#### 6.4.7 调度器主循环伪代码（v1.2 修订）

```python
class Scheduler:
    def __init__(self):
        self.scan_pool  = ThreadPoolExecutor(max_workers=2)   # 慢车道
        self.ui_pool    = ThreadPoolExecutor(max_workers=1)   # 快车道（V2 启用）
        self.conn_mgr   = PerSourceConnectionManager(max_per_source=2)
        self.write_queue = SingleWriterQueue()                # v1.2：DuckDB 单写者

    def main_loop(self):                    # 单线程调度循环，2秒一拍
        while True:
            self.recover_zombies()          # 心跳丢失的 running → ready（crash_count+1）
            self.check_circuit_breakers()   # 连续失败熔断
            for source in active_sources():
                if source.has_running_snapshot() and not self.is_owner(source):
                    continue                # v1.2：同源库单活快照硬约束
                if not self.in_low_peak_window(source) and not only_light_tasks(source):
                    continue                # 高峰期限流
                while self.conn_mgr.has_idle(source):
                    task = self.next_ready_task(source, priority_order=True)
                    if not task: break
                    self.scan_pool.submit(self.execute, task)
            sleep(2)

    def execute(self, task):
        conn = self.conn_mgr.acquire(task.source, lane='scan')
        try:
            task.to_running(worker_id=self.id)
            sql  = render(task.template, dialect=conn.dialect)   # 6.2 节的模板库 + 能力位
            rows = conn.execute_with_timeout(sql, task.timeout)
        except TransientError as e:
            task.retry(backoff=[30, 120][min(task.attempt, 1)])  # v1.2：固定两档 30s/120s
            return
        except PermanentError as e:
            task.to_failed(e); self.skip_descendants(task)
            return
        finally:
            self.conn_mgr.release(conn)          # 先释放连接再解析

        parsed = parse_rows(task, rows)                        # worker 线程内解析
        self.write_queue.submit(upsert_stmt(task, parsed))     # 投递单写者队列
        task.to_done()
        self.promote_dependents(task)                          # pending → ready
```

#### 6.4.8 几个容易踩的坑（提前说死）

1. **不要在持有源库连接时做结果解析与落库**。先释放连接再解析，解析完投递单写者队列——源库连接占用时间最小化，元数据仓库写并发归零。
2. **Oracle 超时取消**：`ALTER SYSTEM CANCEL SQL` 需要 DBA 权限，只读账号不可用。主路径用驱动级取消（python-oracledb `cancel`），其对 OCI 发送 break 通常能及时中断；要承认驱动级取消在服务端不可中断阶段可能滞后，兜底为丢弃并关闭该物理连接——否则超时机制是假的。
3. **进度百分比别用"完成任务数/总任务数"**：struct 任务 1 秒、大表 column 任务 10 分钟，按数量算的进度条会卡在 99%。用成本加权进度（预估 cost_ms 做权重；首扫无历史 cost_ms 时，用 `est_row_count` × 阶段代价系数作先验权重）。
4. **扫描中途用户改了逻辑外键**（V2）：只对还没调度的 relation 任务生效，已 done 的不追溯；用户要新关系的指标，走"单关系重算"入口，不重启快照。补算指标以新行追加进最新快照，`computed_at` 晚于快照时间，页面标注（见 6.4.4 节）。
5. **心跳线程保活假象**：心跳由异步线程每 5 秒更新，若主线程 hang 死（如解析超大结果集）而心跳线程仍存活，僵尸检测会失效。对策：心跳内容带"当前执行进度序号"，长时间序号不变且 SQL 未返回视为卡死，走崩溃处理路径。

---

## 7. 应用服务层（FastAPI）

v1.2 单独成章，因为 MVP 的 FastAPI 职责被刻意收窄：

| 能力 | MVP | V2 | 说明 |
|---|---|---|---|
| 元数据查询 API（只读 JSON） | ✅ | ✅ | L0-L3 页面全部数据来自这些接口 |
| 扫描任务管理 | ✅ | ✅ | 创建快照（圈选范围）、暂停/恢复/取消、重置失败任务、进度查询 |
| 数据源管理 | ✅ | ✅ | 连接测试、数据源 CRUD（凭据加密） |
| 敏感配置导入 | ✅（YAML） | ✅（界面） | MVP 用 YAML 配置文件，启动时导入 sensitive_config/dim_config |
| 报告导出 | ✅ | ✅ | 见第 9 章 |
| 样例清空（物理） | ✅ | ✅ | DELETE + CHECKPOINT/VACUUM + audit_log（见 3.5 节） |
| 离场导出审查 | ✅ | ✅ | 导出包内容清单 + 敏感项复核清单（见 3.5 节） |
| 元数据编辑 API | ❌ | ✅ | 逻辑外键、注释、敏感标记、维度字段 |
| 关系确认 API | ❌ | ✅ | 推断候选确认/否决 |
| 即席探查转发 | ❌ | ✅ | 快车道独立连接，10s 超时 |

设计要点：**MVP 的 API 只有"读"和"任务控制"，没有任何元数据写接口**——人工层表 MVP 只通过 YAML 配置文件填充。这使 MVP 的攻击面和测试面大幅收窄，也把"编辑"这个天然需要交互框架的复杂度整体推迟。

---

## 8. 静态浏览界面（v1.2 整体重写）

### 8.1 形态决策

| 项 | 决策 | 理由 |
|---|---|---|
| 技术栈 | 原生 ES Module + ECharts（本地化打包进容器） | 零构建链、零 npm、随 Docker 镜像分发；院内离线环境无外网 CDN |
| 数据获取 | fetch FastAPI 只读 JSON | 与 V2 引入框架时的 API 层完全复用，不返工 |
| 组件 | 手写轻量表格 + 详情抽屉（< 500 行 JS） | MVP 页面全是"表格 + 图表 + 悬浮卡"，组件库是杀鸡用牛刀 |
| 升级路径 | V2 开放编辑时再评估 React/Vue | 编辑交互（关系确认、字典绑定、维度配置）才是框架的真正理由 |

### 8.2 页面结构（MVP 范围）

```
L0  库总览 Dashboard
 └─ L1  表列表页（可搜索/排序/筛选）
      └─ L2  表详情页
           └─ L3  字段详情抽屉
全局：快照管理页（列表/进度/暂停恢复）│ 报告导出页 │ 扫描发起页（圈选范围+时长估算）
```

> v1.1 的 L2 关联图谱、L3 历史快照趋势、维度对比页、字典管理页、快照对比页、关系确认页**全部随对应功能推迟**（V2/V3）——MVP 页面范围与分期严格对齐，不写"页面上有但点不动"的壳。

**L0 库总览**：表数量、总行数、字段总数、扫描完成度（成本加权进度）；质量概览卡片（平均有值率、有值率/有效率落差最大的字段榜、物理外键覆盖率、疑似字典字段数、敏感字段数）；数据量 Top 表、有值率最低字段榜（"问题榜单"永远比平均分更有冲击力）。L0 数据全部由 `table_stat` / `column_stat` 实时聚合，扫描进行中即可逐层显影。**MVP 不出质量综合评分**（见 4.7 节）。

**L1 表列表**：每行展示行数、字段数、平均有值率、注释；按有值率升序排就是天然的"问题表清单"。

**L2 表详情**：上方表级信息卡（行数、主键、时间跨度及选中的日期字段、注释）；中部字段指标表格（字段名/类型/注释/有值率/有效率/重复率/值域分布迷你条形图/样例入口），点击行展开 L3。

**L3 字段详情抽屉**：完整值域分布图、数值统计、样例数据（敏感字段显示"已按敏感配置跳过/脱敏"）、**指标口径悬浮卡**（内容来自 `metric_registry`，每个指标名悬浮可见——这是从 MVP 第一天就建立的"口径信任"）。

### 8.3 交互红线（MVP 适用的子集）

- **扫描分阶段可见**：按 P0→P6 优先级顺序，页面随扫描进度逐层显影，不用等全扫完；
- **指标口径可见**：每个指标名悬浮显示口径定义卡（内容来自 `metric_registry`）；
- **估算与缺失可见**：估算值 `~` 前缀、无法计算 `—` 及原因、被跳过的指标（distinct_skipped、近似唯一字段不做分布）全部如实标注——宁可标"没算"，不可给假象；
- **补算标注**：`computed_at` 晚于快照时间的指标带标记（V2 起有实际场景，渲染逻辑 MVP 就位）。

---

## 9. 报告导出（MVP 核心交付物，单独成章）

v1.2 把报告导出从"全局页之一"提升为独立章节：它是 MVP 的付费交付物，优先级等同于扫描引擎。

- **产出**：基于元数据仓库一键生成 Word 数据质量报告初稿（docxtpl / Jinja2 → docx；PDF 经 pandoc/LibreOffice 转换）。
- **章节结构**：封面与口径声明 → 库概览 → 问题榜单（有值率最低字段/表、落差榜、异常值命中）→ 表级明细附录 → **指标口径附录**（从 `metric_registry` 自动生成，每个数字可溯源）。
- **口径强制随行**：报告每个指标数字对应口径附录条目；MVP 不出综合评分，只出分项指标与榜单（见 4.7 节）。
- **快照信息**：报告头注明快照时间、扫描范围、口径版本、估算项清单——评审专家挑战时的第一手弹药。
- **离场审查联动**：导出触发 3.5 节的离场导出审查流程（内容清单 + 敏感项复核）。

---

## 10. 实施路线与开发计划

### 10.1 分期总览（v1.2 修订）

| 阶段 | 范围 | 目标 | 周期估算（单人） |
|---|---|---|---|
| **阶段 0** | 技术基准验证（spike） | 用真实/仿真库校准关键假设 | 1 周 |
| **M1** | 扫描引擎核心 | 一个库扫得完、断得了、续得上 | 3~4 周 |
| **M2** | 静态浏览界面 | 扫完能看懂 | 2 周 |
| **M3** | 报告导出 + 离场合规 | 扫完能交付 | 1~2 周 |
| **V2** | 逻辑外键 + 关联率 + 维度下钻 + 人工层编辑界面 + 外键推断 | 覆盖全部核心诉求 | 4~6 周 |
| **V3** | 标准字典对照 + 快照对比 + 自定义探查 + 质量评分 + gateway | 直接服务交付与评级咨询业务 | 4~6 周 |

**MVP = 阶段 0 + M1 + M2 + M3 ≈ 7~9 周（单人）**，里程碑验收标准："在一个真实 HIS 库上，进场 2 天内完成圈选扫描，离场时交出 docx 报告初稿 + 通过离场审查"。

### 10.2 阶段 0：技术基准验证（1 周）

> 目的：在写引擎之前，用最小代码验证三个承量假设，失败的假设当场调整设计。

| # | 验证项 | 方法 | 通过标准 |
|---|---|---|---|
| 1 | **首扫耗时基准**（审查项 #16） | 搭仿真库（或争取一个真实测试库）：100 表 × 混合行量（万级~千万级），手写 6.2.4 单趟批处理 SQL 实测 | 得出"表数 × 行量 → 耗时"经验公式，校准 10~50h 估算与分批策略必要性 |
| 2 | **方言适配可行性** | MySQL + Oracle 各跑通：结构三查询、单趟批处理、值域分布（Oracle 验证 GROUPING SETS）、样例、驱动级 cancel | 两类库全部模板渲染执行成功；cancel 真实生效 |
| 3 | **DuckDB 元数据仓库** | 建好全部 DDL，灌入仿真数据（2.5 万 column_stat + 75 万 value_dist），测 L0 聚合与 L2 查询延迟 | 全部页面查询 < 200ms |

### 10.3 M1：扫描引擎核心（3~4 周）

| 周 | 内容 | 验收 |
|---|---|---|
| W1 | 数据源接入（direct，MySQL+Oracle）、连接管理器（车道抽象）、struct/rowcount 阶段模板与方言适配器（含能力位） | 两类库结构元数据入库，L0 能看到表清单雏形 |
| W2 | column 阶段单趟批处理生成器（分批 ≤30 字段、占位值、anomaly 规则）、估算/跳过策略 | 字段级指标入库，估算标记正确 |
| W3 | scan_task 调度器：状态机、优先级、退避（30/120 两档）、熔断、暂停/恢复；幂等 upsert + 单写者队列 | 杀进程重启后续扫不丢不重；连续失败熔断生效 |
| W4 | value_dist（GROUPING SETS + TopN 分支 + 表级依赖）、sample（敏感 skip/mask）、finalize（DATE_SPAN 选定）、低峰窗口 | 完整快照跑通 partial/done 终态；敏感字段零落盘 |

### 10.4 M2：静态浏览界面（2 周）

| 周 | 内容 | 验收 |
|---|---|---|
| W5 | FastAPI 只读 JSON API 全套 + L0/L1 页面（ECharts 本地化） | 总览与表列表秒开，问题榜单可排序 |
| W6 | L2/L3 + 口径悬浮卡 + 快照管理页 + 扫描发起页（圈选 + 时长估算） | 钻取链路完整；估算/跳过/无法计算全部按 4.8 节规范渲染 |

### 10.5 M3：报告导出 + 离场合规（1~2 周）

| 周 | 内容 | 验收 |
|---|---|---|
| W7 | docxtpl 报告模板（章节结构见第 9 章）+ 口径附录自动生成 | 一份真实扫描产出可交付 docx 初稿 |
| W8 | 离场导出审查（内容清单 + 敏感项复核）+ 样例物理清空（DELETE + CHECKPOINT/VACUUM + audit_log） | 清空后 DuckDB 文件内样例不可恢复（用十六进制抽查验证） |

### 10.6 V2 / V3 要点（范围锚定，详细计划待 MVP 实战后排）

- **V2**：meta_relation 全面启用（逻辑外键编辑 + 关联率三模板 + 孤儿样例物化）、关系确认页与外键推断（6.3 节）、维度字段与机构切换器、敏感/注释/维度编辑界面（此时评估引入 React）、快车道即席查询、业务域标签建议（6.3.7 增强二）。
- **V3**：标准字典导入与对照、快照对比页（实时 diff）、自定义探查模板、质量综合评分（先定缺项重归一规则再启用，见 4.7 节）、gateway 通道 + 降级矩阵、增量扫描评估。

### 10.7 待拍板决策点

1. **优先支持哪几种数据库**——建议 MySQL + Oracle（按实际项目源库频率）。SQL Server/PG 适配器骨架 M1 留出但不测。
2. **单机工具 vs 多人协作平台**——初期单机版，一个项目一个实例，交付边界干净。
3. **关联率全量算还是按需算**（V2 拍）——建议默认只算用户确认的逻辑外键 + 行数 Top 表，其余"点击计算"。
4. **占位值清单里 `'0'` 的处理**——默认只对字符串型生效，数值型 0 视为合法值；老 HIS 普遍用 0 当"未填写"时可按字段覆盖。
5. **关联率分母排除 NULL 外键**——坚持外键非空行数为分母，页面必须并列展示外键有值率（见 4.4.1）。
6. **首扫时长基准数**——阶段 0 实测后回填 6.1 节估算，并据此决定分批扫描（3.7 节）在 MVP 的呈现强度。

---

## 附：v1.2 相对 v1.1 的结构性变化摘要

1. **展示层降级**：React + AntD 全套 → 无构建链静态界面（第 8 章重写）；FastAPI 收窄为"读 + 任务控制"（新增第 7 章）；报告导出升格为独立章节（第 9 章）。
2. **MVP 重排**：阶段 0 基准验证前置；人工层编辑、关联率、维度、字典、评分全部按 V2/V3 锚定；MVP 里程碑改为"进场 2 天、离场交报告"。
3. **引擎去重**：DATE_SPAN 单阶段化（删 rowcount 日期扫描）；值域分布 GROUPING SETS 合并；finalize 删除 diff 预计算与评分计算。
4. **调度加固**：毒任务 crash_count、同源库单活快照、DuckDB 单写者队列、value_dist 表级依赖、退避口径统一、心跳保活假象对策。
5. **合规补全**：样例物理清空含 VACUUM/CHECKPOINT；孤儿样例物化；离场审查闭环。
6. **口径修补**：dup_rate 估算钳制、哨兵转义、distinct 跳过标记、评分缺项重归一前置约束、TRIM join 性能对策。
