const state = {
  snapshotId: null,
  snapshots: [],
  sources: [],
  dashboard: null,
  tables: [],
  metrics: {},
  poll: null,
  charts: [],
  tableCache: new Map(),
  columnCache: new Map(),
};

const l1State = {
  q: "",
  schema: "全部",
  sort: "avg_fill_rate",
  dir: 1,
};

const $ = (id) => document.getElementById(id);

function esc(value) {
  return String(value ?? "").replace(/[&<>"]/g, (c) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    "\"": "&quot;",
  })[c]);
}

function toast(message) {
  const el = $("toast");
  el.textContent = message;
  el.classList.remove("hidden");
  setTimeout(() => el.classList.add("hidden"), 3200);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!response.ok) {
    let detail = await response.text();
    try {
      detail = JSON.parse(detail).detail || detail;
    } catch {}
    throw new Error(detail);
  }
  return response.json();
}

function fmt(value) {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "number") return value.toLocaleString("zh-CN");
  return String(value);
}

function fmtNum(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "—";
  const n = Number(value);
  if (Math.abs(n) >= 1e8) return `${(n / 1e8).toFixed(1)}亿`;
  if (Math.abs(n) >= 1e4) return `${(n / 1e4).toFixed(1)}万`;
  return Math.round(n).toLocaleString("zh-CN");
}

function fmtPct(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "—";
  const n = Number(value);
  return n >= 0.9995 ? ">99.9%" : `${(n * 100).toFixed(1)}%`;
}

function fmtProgressPct(value, status) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "—";
  const n = Math.max(0, Math.min(1, Number(value)));
  if (status === "done" || n >= 1) return "100%";
  return `${(n * 100).toFixed(1)}%`;
}

function fmtDateTime(value) {
  if (value === null || value === undefined || value === "") return "—";
  const text = String(value);
  const match = text.match(/^(\d{4}-\d{2}-\d{2})[T\s](\d{2}:\d{2}:\d{2})/);
  if (match) return `${match[1]} ${match[2]}`;
  return text.replace("T", " ").replace(/\.\d+$/, "");
}

function pctClass(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "na";
  const n = Number(value);
  if (n >= 0.95) return "good";
  if (n >= 0.8) return "mid";
  if (n >= 0.6) return "warn";
  return "bad";
}

function shortId(id) {
  return id ? String(id).slice(0, 8) : "—";
}

function statusText(status) {
  return ({
    created: "待扫描",
    running: "扫描中",
    paused: "已暂停",
    done: "已完成",
    skipped: "已跳过",
    failed: "失败",
  })[status] || status || "未知";
}

function fmtDuration(ms) {
  if (ms === null || ms === undefined || Number.isNaN(Number(ms))) return "-";
  const n = Number(ms);
  if (n < 1000) return `${Math.max(1, Math.round(n))}ms`;
  if (n < 60000) return `${(n / 1000).toFixed(1)}s`;
  return `${Math.floor(n / 60000)}m ${Math.round((n % 60000) / 1000)}s`;
}

function taskTableName(task) {
  if (!task?.table_name) return "";
  return task.table_schema && task.table_schema !== "main"
    ? `${task.table_schema}.${task.table_name}`
    : task.table_name;
}

function taskTypeLabel(task) {
  const text = String(task?.task_type || task || "");
  const tableName = taskTableName(task);
  const suffix = tableName ? ` · ${tableName}` : "";
  if (text === "struct") return "结构扫描";
  if (text === "relation") return "关系扫描";
  if (text === "finalize") return "收尾汇总";
  if (text.startsWith("rowcount:")) return `行数统计${suffix}`;
  if (text.startsWith("column:")) return `字段指标${suffix}`;
  if (text.startsWith("value_dist_sample:")) return `值域/样例${suffix}`;
  return text || "-";
}

function taskTypeMeta(task) {
  const tableName = taskTableName(task);
  if (tableName) return `数据表 ${tableName}`;
  return "";
}

function taskStatusClass(status) {
  if (status === "done" || status === "skipped") return "good-text";
  if (status === "failed") return "bad-text";
  if (status === "running") return "warn-text";
  return "na-text";
}

function currentSnapshot() {
  return state.snapshots.find((s) => s.snapshot_id === state.snapshotId) || null;
}

function metricCode(code) {
  return code === "SAMPLE" ? "SAMPLE_DATA" : code;
}

function showMetricTip(el) {
  const code = metricCode(el.dataset.metric);
  const m = state.metrics[code];
  if (!m) return;
  const tip = $("metric-tip");
  tip.innerHTML = `
    <div class="tip-title">${esc(m.name)} <span class="tip-code">${esc(code)}</span></div>
    <div class="tip-row"><b>定义</b>${esc(m.definition)}</div>
    <div class="tip-row"><b>公式</b>${esc(m.formula)}</div>
    <div class="tip-row"><b>分母</b>${esc(m.denominator)}</div>
    <div class="tip-row"><b>边界</b>${esc(m.boundary)}</div>`;
  tip.style.display = "block";
  const r = el.getBoundingClientRect();
  tip.style.left = `${Math.min(r.left, window.innerWidth - 350)}px`;
  tip.style.top = `${r.bottom + 8}px`;
}

function hideMetricTip() {
  $("metric-tip").style.display = "none";
}

function disposeCharts() {
  state.charts.forEach((chart) => chart.dispose());
  state.charts = [];
}

function initChart(id, option) {
  const el = $(id);
  if (!el || !window.echarts) return null;
  const chart = echarts.init(el);
  chart.setOption(option);
  state.charts.push(chart);
  return chart;
}

function countUp(el, target, formatter) {
  if (target === null || target === undefined || Number.isNaN(Number(target))) {
    el.textContent = "—";
    return;
  }
  const start = performance.now();
  const duration = 700;
  const end = Number(target);
  const tick = (now) => {
    const p = Math.min((now - start) / duration, 1);
    const eased = 1 - Math.pow(1 - p, 3);
    el.textContent = formatter(end * eased);
    if (p < 1) requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
}

function renderTopbar() {
  const snap = currentSnapshot();
  const progress = state.dashboard?.progress;
  const status = progress?.snapshot?.status || snap?.status;
  $("topbar").innerHTML = `
    <div class="brand">
      <svg viewBox="0 0 32 32" width="26" height="26" aria-hidden="true">
        <path d="M2 16h6l3-9 5 18 4-12 3 3h7" fill="none" stroke="#2dd4bf" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/>
      </svg>
      <span>DataPulse</span><span class="brand-sub">数据探查</span>
    </div>
    <div class="snapshot">
      <span class="snap-name">${esc(snap?.source_name || "等待快照")}</span>
      <span class="chip">${esc(snap?.dialect || "数据源未选")}</span>
      <span class="chip">快照 #${shortId(state.snapshotId)}</span>
      <span class="chip ${["done"].includes(status) ? "chip-ok" : ["failed"].includes(status) ? "chip-bad" : "chip-warn"}">${statusText(status)}</span>
      <span class="chip chip-dim">进度 ${fmtProgressPct(progress?.progress ?? snap?.progress, status)}</span>
      <span class="chip chip-dim">口径 ${esc(progress?.snapshot?.metric_def_version || snap?.metric_def_version || "v1.2")}</span>
    </div>
    <nav>
      <a href="#/" data-nav="l0">总览</a>
      <a href="#/tables" data-nav="l1">表列表</a>
    </nav>
    <button id="toggleConsoleBtn" class="top-button" type="button">扫描控制</button>
    <button id="refreshBtn" class="top-button" type="button">刷新</button>`;
  updateNavState();
}

function renderConsole() {
  const snap = currentSnapshot();
  const progress = state.dashboard?.progress;
  const status = progress?.snapshot?.status || snap?.status;
  const progressValue = Number(progress?.progress || 0);
  const progressWidth = Math.max(0, Math.min(100, Math.round(progressValue * 100)));
  const total = Number(progress?.total_tasks || 0);
  const done = Number(progress?.done_tasks || 0);
  const failed = Number(progress?.failed_tasks || 0);
  $("scanConsole").innerHTML = `
    <div class="console-grid">
      <section class="console-card">
        <h2>数据源</h2>
        <label>名称<input id="sourceName" placeholder="HIS 测试库" /></label>
        <label>类型
          <select id="sourceDialect">
            <option value="sqlite">SQLite</option>
            <option value="duckdb">DuckDB</option>
            <option value="mysql">MySQL</option>
            <option value="oracle">Oracle</option>
            <option value="postgresql">PostgreSQL</option>
          </select>
        </label>
        <label>连接地址<input id="sourceUri" placeholder="C:\\data\\his.db" /></label>
        <div class="button-row">
          <button id="testSourceBtn" type="button">测试连接</button>
          <button id="saveSourceBtn" class="primary" type="button">保存数据源</button>
        </div>
      </section>
      <section class="console-card">
        <h2>启动扫描</h2>
        <label>选择数据源<select id="sourceSelect">${state.sources.map((s) => `<option value="${esc(s.source_id)}">${esc(s.name)} (${esc(s.dialect)})</option>`).join("")}</select></label>
        <label>表范围<input id="scanTables" placeholder="留空为全库；用逗号分隔" /></label>
        <div class="button-row">
          <button id="startScanBtn" class="primary" type="button">创建快照</button>
          <button id="reportBtn" type="button">导出 Word</button>
          <button id="deleteSourceBtn" class="danger" type="button">删除数据源</button>
          <button id="clearSamplesBtn" class="danger" type="button">清空样例</button>
        </div>
        <p id="exportReview" class="note"></p>
      </section>
      <section class="console-card snapshot-card">
        <h2>快照</h2>
        <div class="progress-line">
          <strong id="progressText">${fmtProgressPct(progress?.progress || 0, status)}</strong>
          <span id="progressMeta">${total ? `${fmt(done)} / ${fmt(total)} 任务，失败 ${fmt(failed)}` : "无扫描任务"}</span>
        </div>
        <div class="progress"><span id="progressBar" style="width:${progressWidth}%"></span></div>
        <div class="snapshot-list">${renderSnapshotList()}</div>
      </section>
    </div>`;
  if (snap) renderExportReview().catch(() => {});
}

function renderSnapshotList() {
  if (!state.snapshots.length) return `<div class="empty-inline">暂无快照</div>`;
  return state.snapshots.map((s) => `
    <div class="snapshot-item ${s.snapshot_id === state.snapshotId ? "active" : ""}">
      <button data-select-snapshot="${esc(s.snapshot_id)}" type="button">
        <b>${esc(s.source_name)} · ${shortId(s.snapshot_id)}</b>
        <i>${statusText(s.status)} · ${fmtProgressPct(s.progress, s.status)} · ${fmtDateTime(s.started_at || s.finished_at)}</i>
      </button>
      <span class="snapshot-actions">
        <button data-task-log="${esc(s.snapshot_id)}" type="button">日志</button>
        <button data-delete-snapshot="${esc(s.snapshot_id)}" class="danger" type="button">删除</button>
      </span>
    </div>`).join("");
}

function updateNavState() {
  const hash = location.hash || "#/";
  document.querySelectorAll("[data-nav]").forEach((a) => {
    const isL1 = hash.startsWith("#/tables") || hash.startsWith("#/table/");
    a.classList.toggle("on", a.dataset.nav === (isL1 ? "l1" : "l0"));
  });
}

async function loadSources() {
  state.sources = await api("/api/sources");
}

async function loadSnapshots() {
  state.snapshots = await api("/api/snapshots");
  const selectedExists = state.snapshots.some((s) => s.snapshot_id === state.snapshotId);
  if (!selectedExists) {
    state.snapshotId = state.snapshots[0]?.snapshot_id || null;
  }
}

async function loadDashboard() {
  if (!state.snapshotId) {
    state.dashboard = null;
    return;
  }
  state.dashboard = await api(`/api/dashboard?snapshot_id=${encodeURIComponent(state.snapshotId)}`);
}

async function loadMetrics() {
  if (!state.snapshotId) return;
  const rows = await api(`/api/metrics?snapshot_id=${encodeURIComponent(state.snapshotId)}`);
  state.metrics = Object.fromEntries(rows.map((m) => [m.metric_code, m]));
  state.metrics.ROW_COUNT ||= {
    name: "行数",
    definition: "当前表或扫描范围内的记录数。",
    formula: "COUNT(*)",
    denominator: "—",
    boundary: "大表可由数据源统计信息估算，具体以扫描器返回值为准。",
  };
  state.metrics.NULL_RATE ||= {
    name: "NULL 率",
    definition: "NULL 行数占当前字段总行数的比例。",
    formula: "null_cnt / row_count",
    denominator: "当前字段总行数。",
    boundary: "row_count = 0 时显示为无法计算。",
  };
  state.metrics.DATE_SPAN ||= {
    name: "时间跨度",
    definition: "表或扫描范围内识别到的业务日期最小值到最大值。",
    formula: "min(date_column) ~ max(date_column)",
    denominator: "—",
    boundary: "未识别日期字段时显示为无法计算。",
  };
  state.metrics.PK_DUP ||= {
    name: "主键重复率",
    definition: "声明主键或逻辑主键出现重复的行占比。",
    formula: "dup_pk_rows / row_count",
    denominator: "表总行数。",
    boundary: "当前 MVP 未采集该指标，页面按模板保留位置。",
  };
  state.metrics.ATTR_DUP ||= {
    name: "数据重复率",
    definition: "按配置的属性标识字段组合计算的重复冗余行占比。",
    formula: "data_duplicate_rows / row_count",
    denominator: "表总行数。",
    boundary: "未配置属性标识字段时指标不存在；配置字段为空的行不参与重复组合计算；空表分母为 0 时重复率为空。",
  };
  state.metrics.MATCH_RATE ||= {
    name: "关联率",
    definition: "子表外键在父表主键中可匹配的占比。",
    formula: "matched_rows / child_fk_non_empty_rows",
    denominator: "外键非空的子表行数。",
    boundary: "当前 MVP 未采集表间关系，页面按模板保留位置。",
  };
}

async function loadTablesData() {
  if (!state.snapshotId) {
    state.tables = [];
    return;
  }
  const data = await api(`/api/tables?snapshot_id=${encodeURIComponent(state.snapshotId)}`);
  state.tables = data.tables;
}

async function refreshAll({ reroute = true } = {}) {
  await loadSources();
  await loadSnapshots();
  await loadDashboard();
  await Promise.all([loadMetrics(), loadTablesData()]);
  renderTopbar();
  renderConsole();
  if (reroute) await route();
  const status = state.dashboard?.progress?.snapshot?.status;
  if (["created", "running"].includes(status)) startPolling();
}

function startPolling() {
  if (state.poll) clearInterval(state.poll);
  state.poll = setInterval(async () => {
    if (!state.snapshotId) return;
    const progress = await api(`/api/scans/${encodeURIComponent(state.snapshotId)}/progress`);
    state.dashboard = { ...(state.dashboard || {}), progress };
    renderTopbar();
    renderConsole();
    if (!["created", "running"].includes(progress.snapshot?.status)) {
      clearInterval(state.poll);
      state.poll = null;
      await refreshAll();
    }
  }, 2000);
}

function emptyState() {
  $("app").innerHTML = `
    <div class="empty-state">
      <h1>等待扫描结果</h1>
      <p>先在上方登记数据源并创建快照。扫描完成后，这里会显示库总览、问题榜单、表列表和字段详情。</p>
    </div>`;
}

function renderStat(label, value, formatter, metric, delay = 0, sub = "&nbsp;") {
  return `
    <div class="card stat fade" style="--d:${delay}">
      <div class="stat-label">${metric ? `<span data-metric="${metric}" class="m-name">${label}</span>` : label}</div>
      <div class="stat-value" data-countup="${Number(value ?? 0)}" data-format="${formatter}">0</div>
      <div class="stat-sub">${sub}</div>
    </div>`;
}

function inferDomain(table) {
  const text = `${table.table_name || ""} ${table.table_comment || ""}`.toLowerCase();
  const rules = [
    ["患者", ["patient", "empi", "master_index", "患者"]],
    ["住院", ["inp", "zy", "admission", "discharge", "encounter", "住院", "入院", "出院"]],
    ["门诊", ["mz", "outp", "register", "visit", "门诊", "挂号", "就诊"]],
    ["检验", ["lis", "lab", "result", "检验", "化验"]],
    ["检查", ["pacs", "exam", "image", "检查", "影像"]],
    ["收费", ["fee", "charge", "invoice", "settle", "claim", "收费", "费用", "结算", "医保"]],
    ["药事", ["drug", "pharmacy", "prescription", "药", "处方"]],
    ["字典", ["dict", "dept", "staff", "code", "字典", "科室", "人员"]],
    ["平台", ["sys", "log", "interface", "系统", "日志", "接口"]],
  ];
  return rules.find(([, keys]) => keys.some((key) => text.includes(key)))?.[0] || "临床";
}

function domainRows() {
  return state.tables.reduce((acc, t) => {
    const key = inferDomain(t);
    acc[key] ||= { name: key, value: 0, tables: 0 };
    acc[key].value += Number(t.row_count || 0);
    acc[key].tables += 1;
    return acc;
  }, {});
}

function timeSummary() {
  const ranges = state.tables
    .filter((t) => t.min_date && t.max_date)
    .map((t) => ({ min: String(t.min_date).slice(0, 10), max: String(t.max_date).slice(0, 10), rows: Number(t.row_count || 0) }));
  if (!ranges.length) return null;
  const minDate = ranges.map((r) => r.min).sort()[0];
  const maxDate = ranges.map((r) => r.max).sort().at(-1);
  const years = {};
  ranges.forEach((r) => {
    const y = Number(r.max.slice(0, 4));
    if (Number.isFinite(y)) years[y] = (years[y] || 0) + r.rows;
  });
  return { minDate, maxDate, years: Object.keys(years).map(Number).sort((a, b) => a - b), rowsByYear: years };
}

function primaryKeyLabel(table) {
  const pk = table.primary_key;
  if (!pk) return "—";
  try {
    const parsed = JSON.parse(pk);
    if (Array.isArray(parsed)) return parsed.join(", ") || "—";
  } catch {}
  return String(pk);
}

function jsonList(value) {
  if (!value) return [];
  try {
    const parsed = JSON.parse(value);
    return Array.isArray(parsed) ? parsed.map((item) => String(item)) : [];
  } catch {
    return [];
  }
}

function duplicateReasonText(reason) {
  if (!reason) return "";
  if (reason === "no_declared_primary_key") return "无声明主键";
  if (reason === "no_attribute_key_config") return "未配置属性标识字段";
  if (reason.startsWith("attribute_key_column_missing:")) return "配置字段缺失";
  if (reason.startsWith("data_duplicate_failed:")) return "计算失败";
  if (reason.startsWith("pk_duplicate_failed:")) return "计算失败";
  return reason;
}

function duplicateRateCell(rate, reason, metric, columns = null) {
  if (rate === null || rate === undefined || Number.isNaN(Number(rate))) {
    const text = duplicateReasonText(reason) || "—";
    return `<span class="na" data-metric="${metric}">${esc(text)}</span>`;
  }
  const n = Number(rate);
  const columnText = columns?.length ? `<div class="td-sub mono">${esc(columns.join(", "))}</div>` : "";
  return `
    <div class="dup-cell">
      <b class="${n > 0 ? "bad" : "good"}-text" data-metric="${metric}">${fmtPct(n)}</b>
      ${columnText}
    </div>`;
}

function relationLabel(row) {
  const childColumns = jsonList(row.child_columns_json).join(", ");
  const parentColumns = jsonList(row.parent_columns_json).join(", ");
  return `${row.child_table}.${childColumns} -> ${row.parent_table}.${parentColumns}`;
}

function renderTimeCard(summary) {
  if (!summary) {
    return `
      <div class="card stat timecard fade" style="--d:240">
        <div class="stat-label"><span data-metric="DATE_SPAN" class="m-name">时间分布</span></div>
        <div class="stat-value" style="font-size:21px">—</div>
        <div class="stat-sub">当前快照未识别业务日期字段</div>
      </div>`;
  }
  return `
    <div class="card stat timecard fade" style="--d:240">
      <div class="stat-label"><span data-metric="DATE_SPAN" class="m-name">时间分布</span></div>
      <div class="stat-value" style="font-size:21px">${summary.minDate.slice(0, 4)} ~ ${summary.maxDate.slice(0, 4)}</div>
      <div class="stat-sub">${summary.years.length} 个年度 · 悬浮查看年度分布</div>
      <div class="time-pop">
        <div class="time-pop-title">全库数据年度分布（按表日期跨度归属）</div>
        <div id="chart-year" style="height:190px"></div>
      </div>
    </div>`;
}

function renderL0() {
  const root = $("app");
  const data = state.dashboard;
  if (!state.snapshotId || !data) return emptyState();
  disposeCharts();

  const o = data.overview || {};
  const time = timeSummary();
  root.innerHTML = `
    <section class="fade" style="--d:0">
      <div class="cards">
        ${renderStat("表数量", o.table_count, "int", "ROW_COUNT", 0)}
        ${renderStat("总行数", o.total_rows, "num", "ROW_COUNT", 60)}
        ${renderStat("字段总数", o.column_count, "int", null, 120)}
        ${renderStat("平均有值率", o.avg_fill_rate, "pct", "FILL_RATE", 180, `平均有效率 <b class="${pctClass(o.avg_valid_rate)}-text">${fmtPct(o.avg_valid_rate)}</b>`)}
        ${renderTimeCard(time)}
      </div>
      <div class="cards cards-3">
        <div class="card stat small">
          <div class="stat-label"><span data-metric="PHYSICAL_FK" class="m-name">物理外键覆盖率</span></div>
          <div class="stat-value ${pctClass(o.physical_fk_coverage_rate)}-text">${fmtPct(o.physical_fk_coverage_rate)}</div>
          <div class="stat-sub">${fmtNum(o.physical_foreign_keys)} 条物理外键 · ${fmtNum(o.physical_fk_tables)} 张子表</div>
        </div>
        <div class="card stat small">
          <div class="stat-label">疑似字典字段</div>
          <div class="stat-value">${fmtNum(o.dictionary_candidates)}</div>
          <div class="stat-sub">distinct ≤ 阈值，值域字典的原始材料</div>
        </div>
        <div class="card stat small">
          <div class="stat-label">敏感字段</div>
          <div class="stat-value">${fmtNum(o.sensitive_columns)}</div>
          <div class="stat-sub">skip / mask 按敏感配置处理，样例受控落盘</div>
        </div>
      </div>
    </section>

    <section class="grid-2 fade" style="--d:150">
      <div class="card">
        <h3>业务域分布 <span class="h3-sub">按表数量 / 数据量</span></h3>
        <div id="chart-domain" class="chart"></div>
      </div>
      <div class="card">
        <h3>数据量 Top 10</h3>
        <div id="chart-top" class="chart"></div>
      </div>
    </section>

    <section class="grid-2 fade" style="--d:220">
      <div class="card">
        <h3>表平均有值率排名 <span class="h3-sub" data-metric="FILL_RATE">按表聚合升序 · Top 10</span><a class="h3-link" href="#/tables">查看全部</a></h3>
        ${renderTableFillBoard()}
      </div>
      <div class="card">
        <h3>数据重复榜
          <span class="dup-toggle">
            <button class="on" type="button"><span data-metric="PK_DUP">主键重复率</span></button>
            <button type="button"><span data-metric="ATTR_DUP">数据重复率</span></button>
          </span>
          <a class="h3-link" href="#/tables">查看全部</a>
        </h3>
        ${renderDupBoard()}
      </div>
    </section>

    <section class="fade" style="--d:300">
      <div class="card">
        <h3>表间关联图谱
          <span class="h3-sub" data-metric="MATCH_RATE">节点=表（大小=数据量）· 边=关联关系（粗细=子表外键量 · 颜色=关联率健康度）</span>
          <span class="rel-health">库关联健康度 <b class="${pctClass(o.relation_health_rate)}-text">${fmtPct(o.relation_health_rate)}</b><i>按子表行数加权 · ${fmtNum(data.relations?.length || 0)} 条 active 关系</i></span>
        </h3>
        ${renderRelationPlaceholder()}
      </div>
    </section>`;

  root.querySelectorAll("[data-countup]").forEach((el) => {
    const format = el.dataset.format;
    countUp(el, Number(el.dataset.countup), format === "pct" ? fmtPct : format === "num" ? fmtNum : (x) => Math.round(x).toLocaleString("zh-CN"));
  });
  renderOverviewCharts();
}

function renderOverviewCharts() {
  const top = [...(state.dashboard?.top_tables || [])].slice(0, 10);
  initChart("chart-top", {
    grid: { left: 8, right: 60, top: 8, bottom: 8, containLabel: true },
    tooltip: { trigger: "axis", axisPointer: { type: "shadow" }, valueFormatter: (v) => `${fmtNum(v)} 行` },
    xAxis: { type: "value", splitLine: { lineStyle: { color: "#eef2f6" } }, axisLabel: { formatter: fmtNum } },
    yAxis: { type: "category", inverse: true, data: top.map((t) => t.table_name), axisLabel: { fontFamily: "monospace", fontSize: 11 } },
    series: [{
      type: "bar",
      barWidth: 14,
      data: top.map((t) => ({ value: Number(t.row_count || 0), itemStyle: { color: "#14b8a6" } })),
      itemStyle: { borderRadius: [0, 4, 4, 0] },
      label: { show: true, position: "right", formatter: (p) => fmtNum(p.value), fontSize: 11, color: "#64748b" },
    }],
  });

  const rows = Object.values(domainRows()).sort((a, b) => b.value - a.value);
  initChart("chart-domain", {
    tooltip: { trigger: "item", formatter: (p) => `${esc(p.name)}<br/>数据量 ${fmtNum(p.value)} 行 · ${p.data.tables} 张表 (${p.percent}%)` },
    legend: { bottom: 0, itemWidth: 10, itemHeight: 10, textStyle: { fontSize: 11 } },
    series: [{
      type: "pie",
      radius: ["42%", "68%"],
      center: ["50%", "44%"],
      itemStyle: { borderRadius: 5, borderColor: "#fff", borderWidth: 2 },
      label: { fontSize: 11, formatter: "{b}\n{d}%" },
      data: rows.length ? rows : [{ name: "暂无", value: 1 }],
      color: ["#14b8a6", "#0ea5e9", "#f59e0b", "#8b5cf6", "#ef4444", "#84cc16", "#ec4899", "#64748b"],
    }],
  });

  const summary = timeSummary();
  const yearEl = $("chart-year");
  if (summary && yearEl) {
    let yearChart = null;
    yearEl.closest(".timecard")?.addEventListener("mouseenter", () => {
      if (yearChart) return;
      yearChart = echarts.init(yearEl);
      state.charts.push(yearChart);
      yearChart.setOption({
        grid: { left: 4, right: 8, top: 10, bottom: 4, containLabel: true },
        tooltip: { trigger: "axis", formatter: (p) => `${p[0].name} 年<br/>${fmtNum(p[0].value)} 行` },
        xAxis: { type: "category", data: summary.years, axisLabel: { fontSize: 9, interval: 0 } },
        yAxis: { type: "value", splitLine: { lineStyle: { color: "#eef2f6" } }, axisLabel: { fontSize: 9, formatter: fmtNum } },
        series: [{
          type: "bar",
          data: summary.years.map((year) => ({ value: summary.rowsByYear[year], itemStyle: { color: "#14b8a6", borderRadius: [3, 3, 0, 0] } })),
          barWidth: "62%",
        }],
      });
    });
  }
  renderRelationGraph();
}

function relHealthColor(value) {
  const cls = pctClass(value);
  return ({
    good: "#10b981",
    mid: "#f59e0b",
    warn: "#f97316",
    bad: "#ef4444",
    na: "#94a3b8",
  })[cls] || "#94a3b8";
}

function relationColumnPair(row) {
  const childColumns = jsonList(row.child_columns_json).join(", ") || "FK";
  const parentColumns = jsonList(row.parent_columns_json).join(", ") || "PK";
  return `${childColumns} -> ${parentColumns}`;
}

function relationFkFillRate(row) {
  const table = state.tables.find((t) => t.table_id === row.child_table_id);
  const rows = Number(table?.row_count || 0);
  if (!rows) return null;
  return Number(row.child_fk_non_empty_rows || 0) / rows;
}

function relationIssue(row) {
  if (row.skipped_reason) return row.skipped_reason;
  if (row.match_rate == null) return "关联统计未计算";
  if (Number(row.orphan_rows || 0) > 0) return `${fmtNum(row.orphan_rows)} 个孤儿行`;
  if (row.compare_rule && row.compare_rule !== "raw") return `compare_rule=${row.compare_rule}`;
  return "匹配稳定";
}

function renderRelationGraph() {
  const el = $("chart-relations");
  const relations = state.dashboard?.relations || [];
  if (!el || !relations.length || !window.echarts) return;

  const tableById = new Map(state.tables.map((t) => [t.table_id, t]));
  const nodes = new Map();
  const degree = new Map();
  relations.forEach((r) => {
    [r.child_table_id, r.parent_table_id].forEach((id) => degree.set(id, (degree.get(id) || 0) + 1));
  });
  relations.forEach((r) => {
    [
      [r.child_table_id, r.child_table],
      [r.parent_table_id, r.parent_table],
    ].forEach(([id, name]) => {
      if (!nodes.has(id)) {
        const table = tableById.get(id);
        const rows = Number(table?.row_count || 0);
        nodes.set(id, {
          name: id,
          tableName: name,
          value: rows,
          symbolSize: Math.max(26, Math.min(78, 26 + Math.log10(rows + 1) * 8 + (degree.get(id) || 0) * 2)),
          itemStyle: {
            color: rows ? "#0ea5e9" : "#cbd5e1",
            borderColor: "#fff",
            borderWidth: 3,
            shadowBlur: 18,
            shadowColor: "rgba(15,34,51,.16)",
          },
          label: { color: rows ? "#0f2233" : "#94a3b8" },
        });
      }
    });
  });
  const nodeList = [...nodes.values()].sort((a, b) => {
    const degreeDiff = (degree.get(b.name) || 0) - (degree.get(a.name) || 0);
    return degreeDiff || Number(b.value || 0) - Number(a.value || 0);
  });
  const bounds = el.getBoundingClientRect();
  const graphWidth = Math.max(320, bounds.width || 760);
  const graphHeight = Math.max(320, bounds.height || 470);
  const centerX = graphWidth * 0.5;
  const centerY = graphHeight * 0.52;
  const innerRadius = Math.min(graphWidth * 0.27, graphHeight * 0.38);
  const outerRadius = Math.min(graphWidth * 0.39, graphHeight * 0.5);
  nodeList.forEach((node, index) => {
    if (index === 0) {
      node.x = centerX;
      node.y = centerY;
      return;
    }
    const orbitIndex = index - 1;
    const innerCount = Math.min(8, Math.max(1, nodeList.length - 1));
    const inOuterRing = orbitIndex >= innerCount;
    const ringIndex = inOuterRing ? orbitIndex - innerCount : orbitIndex;
    const ringCount = inOuterRing ? Math.max(1, nodeList.length - innerCount - 1) : innerCount;
    const radius = inOuterRing ? outerRadius : innerRadius;
    const angle = -Math.PI / 2 + (ringIndex / ringCount) * Math.PI * 2 + (inOuterRing ? Math.PI / ringCount : 0);
    node.x = centerX + Math.cos(angle) * radius;
    node.y = centerY + Math.sin(angle) * radius * 0.86;
  });

  const links = relations.map((r) => {
    const rows = Number(r.child_fk_non_empty_rows || 0);
    return {
      source: r.child_table_id,
      target: r.parent_table_id,
      relation: r,
      value: rows,
      lineStyle: {
        color: relHealthColor(r.match_rate),
        width: Math.max(2, Math.min(9, 2 + Math.log10(rows + 1) * 1.2)),
        opacity: Number(r.match_rate ?? 0) >= 0.95 ? 0.34 : 0.82,
        curveness: 0.18,
      },
    };
  });

  const chart = initChart("chart-relations", {
    animationDuration: 700,
    animationEasingUpdate: "cubicOut",
    tooltip: {
      borderColor: "#f59e0b",
      borderWidth: 1,
      backgroundColor: "rgba(255,255,255,.96)",
      padding: 14,
      textStyle: { color: "#0f2233", fontSize: 12 },
      extraCssText: "box-shadow:0 18px 48px rgba(15,34,51,.18);border-radius:8px;",
      formatter: (p) => {
        if (p.dataType === "edge") {
          const r = p.data.relation;
          const child = tableById.get(r.child_table_id);
          const parent = tableById.get(r.parent_table_id);
          return `
            <b style="font-size:14px">${esc(r.child_table)} -> ${esc(r.parent_table)}</b><br/>
            <span style="color:#64748b">${esc(relationColumnPair(r))}</span><br/>
            关联率 <b>${fmtPct(r.match_rate)}</b> · fk 有值率 <b>${fmtPct(relationFkFillRate(r))}</b><br/>
            孤儿 ${fmtNum(r.orphan_rows)} 行 | 子表 ${fmtNum(child?.row_count)} 行 | 父表 ${fmtNum(parent?.row_count)} 行<br/>
            <span style="color:${relHealthColor(r.match_rate)}">${esc(relationIssue(r))}</span>`;
        }
        return `
          <b style="font-size:14px">${esc(p.data.tableName)}</b><br/>
          数据量 ${fmtNum(p.data.value)} 行<br/>
          连接关系 ${fmtNum(degree.get(p.data.name) || 0)} 条`;
      },
    },
    series: [{
      type: "graph",
      layout: "none",
      roam: true,
      draggable: true,
      edgeSymbol: ["none", "arrow"],
      edgeSymbolSize: [0, 8],
      data: nodeList,
      links,
      label: {
        show: true,
        formatter: (p) => p.data.tableName,
        fontFamily: "SF Mono, Consolas, monospace",
        fontSize: 12,
        position: "right",
      },
      emphasis: {
        focus: "adjacency",
        lineStyle: { opacity: 1 },
        label: { fontWeight: 700 },
      },
      lineStyle: { cap: "round" },
    }],
  });
  chart?.on("click", (params) => {
    if (params.dataType === "node" && params.data?.name) {
      location.hash = `#/table/${encodeURIComponent(params.data.name)}`;
    }
    if (params.dataType === "edge" && params.data?.relation?.child_table_id) {
      location.hash = `#/table/${encodeURIComponent(params.data.relation.child_table_id)}`;
    }
  });
}

function renderTableFillBoard() {
  const rows = [...state.tables].sort((a, b) => Number(a.avg_fill_rate ?? 2) - Number(b.avg_fill_rate ?? 2)).slice(0, 10);
  if (!rows.length) return `<div class="na block-empty">暂无表指标</div>`;
  return rows.map((t, i) => `
    <button class="lb-row fade" style="--d:${200 + i * 40}" data-table-route="${esc(t.table_id)}" type="button">
      <span class="lb-rank">${i + 1}</span>
      <span class="lb-name"><b>${esc(t.table_name)}</b><i>${esc(t.table_comment || t.schema_name || "")} · ${fmtNum(t.row_count)} 行</i></span>
      <span class="lb-bar"><span class="lb-fill ${pctClass(t.avg_fill_rate)}" style="width:${Math.max(2, Number(t.avg_fill_rate || 0) * 100)}%"></span></span>
      <span class="lb-val ${pctClass(t.avg_fill_rate)}-text">${fmtPct(t.avg_fill_rate)}</span>
    </button>`).join("");
}

function renderGapBoard() {
  const rows = state.dashboard?.gap_columns || [];
  if (!rows.length) return `<div class="na block-empty">暂无有效率落差数据</div>`;
  const max = Math.max(0.01, ...rows.map((r) => Number(r.gap || 0)));
  return rows.slice(0, 8).map((r, i) => `
    <div class="lb-row gap-row fade" style="--d:${200 + i * 40}">
      <span class="lb-name"><b>${esc(r.table_name)}.${esc(r.column_name)}</b><i>有值 ${fmtPct(r.fill_rate)} · 有效 ${fmtPct(r.valid_rate)}</i></span>
      <span class="lb-bar"><span class="lb-fill ${Number(r.gap) > 0.2 ? "bad" : "warn"}" style="width:${Math.max(2, Number(r.gap || 0) / max * 100)}%"></span></span>
      <span class="lb-val ${Number(r.gap) > 0.2 ? "bad" : "warn"}-text">${(Number(r.gap || 0) * 100).toFixed(1)}pt</span>
    </div>`).join("");
}

function renderDupBoard() {
  const rows = (state.dashboard?.duplicate_tables || state.dashboard?.pk_duplicates || []).slice(0, 10);
  if (!rows.length) {
    return `
      <div class="placeholder-panel">
        <div class="placeholder-title">当前快照没有可展示的重复率指标</div>
        <p>主键重复率依赖数据库声明主键；数据重复率依赖属性标识字段配置。未配置时指标为空，可在表列表查看原因。</p>
      </div>`;
  }
  const max = Math.max(0.01, ...rows.map((t) => Math.max(Number(t.data_duplicate_rate || 0), Number(t.pk_duplicate_rate || 0))));
  return rows.map((t, i) => `
    <button class="lb-row pkdup-row fade" style="--d:${200 + i * 40}" data-table-route="${esc(t.table_id)}" type="button">
      <span class="lb-rank">${i + 1}</span>
      <span class="lb-name"><b>${esc(t.table_name)}</b><i>数据 ${fmtPct(t.data_duplicate_rate)} · 主键 ${fmtPct(t.pk_duplicate_rate)} · ${fmtNum(t.row_count)} 行</i></span>
      <span class="lb-bar"><span class="lb-fill ${Math.max(Number(t.data_duplicate_rate || 0), Number(t.pk_duplicate_rate || 0)) > 0 ? "bad" : "good"}" style="width:${Math.max(2, Math.max(Number(t.data_duplicate_rate || 0), Number(t.pk_duplicate_rate || 0)) / max * 100)}%"></span></span>
      <span class="lb-val ${Math.max(Number(t.data_duplicate_rate || 0), Number(t.pk_duplicate_rate || 0)) > 0 ? "bad" : "good"}-text">${fmtPct(Math.max(Number(t.data_duplicate_rate || 0), Number(t.pk_duplicate_rate || 0)))}</span>
    </button>`).join("");
}

function renderRelationPlaceholder() {
  const relations = state.dashboard?.relations || [];
  if (!relations.length) {
    return `
      <div class="placeholder-panel">
        <div class="placeholder-title">当前快照未发现物理外键</div>
        <p>关联率只对扫描范围内父表和子表都存在的物理 FOREIGN KEY 计算。源库未声明外键时这里保持为空。</p>
      </div>`;
  }
  return `
    <div class="rel-wrap">
      <div class="rel-graph">
        <div id="chart-relations" class="rel-chart"></div>
        <div class="rel-legend">
          <span><b class="rel-dot rel-dot-node"></b>节点大小=数据量</span>
          <span><b class="rel-line rel-line-thick"></b>边宽=子表外键量</span>
          <span><b class="rel-line rel-line-warn"></b>边色=关联健康度</span>
        </div>
      </div>
      <div class="rel-side">
        <div class="rel-side-title">关联率最低的关系</div>
        <div class="rel-list">
          ${relations.slice(0, 8).map((r) => `
            <button class="rel-card" data-table-route="${esc(r.child_table_id)}" type="button">
              <span class="rel-card-main">
                <b>${esc(r.child_table)} -> ${esc(r.parent_table)}</b>
                <i>${esc(relationColumnPair(r))}</i>
                <em>${esc(relationIssue(r))}</em>
                <span class="rel-bars">
                  <span><u style="width:${Math.max(2, Number(r.match_rate || 0) * 100)}%;background:${relHealthColor(r.match_rate)}"></u></span>
                  <span><u style="width:${Math.max(2, Number(relationFkFillRate(r) || 0) * 100)}%"></u></span>
                </span>
              </span>
              <span class="rel-card-metrics">
                <strong class="${pctClass(r.match_rate)}-text">${fmtPct(r.match_rate)}</strong>
                <i>fk ${fmtPct(relationFkFillRate(r))}</i>
              </span>
            </button>`).join("")}
        </div>
      </div>
    </div>`;
}

function renderLegacyRelationPlaceholder() {
  return `
    <div class="placeholder-panel">
      <div class="placeholder-title">当前快照未采集重复榜指标</div>
      <p>模板中的“主键重复率 / 属性组合重复率”需要声明主键、逻辑主键或唯一性字段组合配置。当前 MVP 只采集字段级重复率，因此这里保留模板模块，但不把字段重复率伪装成表级重复榜。</p>
      <div class="placeholder-grid">
        <span>主键重复率：未采集</span>
        <span>属性组合重复率：未采集</span>
      </div>
    </div>`;
}

function renderLegacyRelationGraphPlaceholder() {
  const domains = Object.values(domainRows()).sort((a, b) => b.value - a.value);
  const positions = [
    [16, 18], [42, 14], [68, 22], [26, 48], [56, 52], [76, 62], [38, 74], [12, 68],
  ];
  return `
    <div class="rel-wrap">
      <div class="rel-placeholder">
        ${domains.map((d, i) => {
          const [left, top] = positions[i % positions.length];
          return `<span class="rel-node" style="left:${left}%;top:${top}%">${esc(d.name)}<b>${fmtNum(d.tables)} 表</b></span>`;
        }).join("") || '<span class="na">暂无表</span>'}
      </div>
      <div class="rel-side">
        <div class="rel-side-title">关联率最低的关系</div>
        <div class="placeholder-panel compact">
          当前 MVP 尚未采集物理外键、人工确认关系、外键有值率和匹配率。关系扫描接入后，这里会按模板展示 child → parent、fk → pk、关联率与孤儿行。
        </div>
      </div>
    </div>`;
}

function renderLowColumnBoard() {
  const rows = state.dashboard?.low_columns || [];
  if (!rows.length) return `<div class="na block-empty">暂无字段指标</div>`;
  return rows.slice(0, 12).map((c, i) => `
    <button class="dense-item fade" style="--d:${i * 25}" data-column="${esc(c.column_id)}" type="button">
      <span><b>${esc(c.table_name)}.${esc(c.column_name)}</b><i>${esc(c.data_type)}${c.skipped_reason ? ` · ${esc(c.skipped_reason)}` : ""}</i></span>
      <strong class="${pctClass(c.fill_rate)}-text">${fmtPct(c.fill_rate)}</strong>
    </button>`).join("");
}

function renderL1() {
  const root = $("app");
  if (!state.snapshotId) return emptyState();
  disposeCharts();
  const domains = ["全部", ...new Set(state.tables.map((t) => inferDomain(t)))];
  root.innerHTML = `
    <div class="toolbar fade" style="--d:0">
      <input id="l1-q" class="search" placeholder="搜索表名 / 注释..." value="${esc(l1State.q)}" />
      <div class="chips">${domains.map((domain) => `<button class="fchip ${l1State.schema === domain ? "on" : ""}" data-schema="${esc(domain)}" type="button">${esc(domain)}</button>`).join("")}</div>
    </div>
    <div class="card fade" style="--d:80">
      <table class="tbl" id="l1-tbl">
        <thead><tr>
          <th data-sort="table_name">表名</th>
          <th>业务域</th>
          <th data-sort="row_count" class="num">行数</th>
          <th data-sort="column_count" class="num">字段数</th>
          <th data-sort="avg_fill_rate">平均有值率 <span data-metric="FILL_RATE" class="m-name">?</span></th>
          <th data-sort="pk_duplicate_rate" class="num">主键重复率 <span data-metric="PK_DUP" class="m-name">?</span></th>
          <th data-sort="data_duplicate_rate" class="num">数据重复率 <span data-metric="ATTR_DUP" class="m-name">?</span></th>
          <th>主键</th>
          <th>时间跨度</th>
        </tr></thead>
        <tbody>${renderTableRows()}</tbody>
      </table>
    </div>`;
}

function filteredTables() {
  const q = l1State.q.toLowerCase();
  const rows = state.tables.filter((t) => {
    const schemaOk = l1State.schema === "全部" || inferDomain(t) === l1State.schema;
    const qOk = !q || String(t.table_name || "").toLowerCase().includes(q) || String(t.table_comment || "").toLowerCase().includes(q);
    return schemaOk && qOk;
  });
  rows.sort((a, b) => {
    const k = l1State.sort;
    const av = a[k] ?? "";
    const bv = b[k] ?? "";
    const diff = typeof av === "number" || typeof bv === "number"
      ? Number(av || 0) - Number(bv || 0)
      : String(av).localeCompare(String(bv));
    return diff * l1State.dir;
  });
  return rows;
}

function renderTableRows() {
  const rows = filteredTables();
  if (!rows.length) return `<tr><td colspan="9" class="na" style="text-align:center;padding:32px">无匹配表</td></tr>`;
  return rows.map((t, i) => `
    <tr class="fade" style="--d:${Math.min(i, 20) * 20}" data-table-route="${esc(t.table_id)}">
      <td><b class="mono">${esc(t.table_name)}</b><div class="td-sub">${esc(t.table_comment || "")}</div></td>
      <td><span class="tag">${esc(inferDomain(t))}</span></td>
      <td class="num">${fmtNum(t.row_count)}</td>
      <td class="num">${fmt(t.column_count)}</td>
      <td><div class="cellbar"><span class="cellbar-fill ${pctClass(t.avg_fill_rate)}" style="width:${Math.max(2, Number(t.avg_fill_rate || 0) * 100)}%"></span><em class="${pctClass(t.avg_fill_rate)}-text">${fmtPct(t.avg_fill_rate)}</em></div></td>
      <td class="num">${duplicateRateCell(t.pk_duplicate_rate, t.pk_duplicate_skipped_reason, "PK_DUP")}</td>
      <td class="num">${duplicateRateCell(t.data_duplicate_rate, t.data_duplicate_skipped_reason, "ATTR_DUP", jsonList(t.data_duplicate_columns))}</td>
      <td>${primaryKeyLabel(t) === "—" ? '<span class="na">—</span>' : `<span class="pk mono">${esc(primaryKeyLabel(t))}</span>`}</td>
      <td class="td-sub">${t.date_column ? `${esc(t.min_date)} ~ ${esc(t.max_date)}<div>via ${esc(t.date_column)}</div>` : '<span class="na">—</span>'}</td>
    </tr>`).join("");
}

function drawL1Rows() {
  const tbody = $("l1-tbl")?.querySelector("tbody");
  if (tbody) tbody.innerHTML = renderTableRows();
}

function miniDistForColumn(c) {
  if (c.skipped_reason) return `<span class="na">${esc(c.skipped_reason)}</span>`;
  if (c.distinct_count == null) return `<span class="na">—</span>`;
  if (Number(c.duplicate_rate || 0) < 0.01 || Number(c.distinct_count || 0) > 1000) return `<span class="na">近似唯一字段</span>`;
  const dup = Math.max(0, Math.min(1, Number(c.duplicate_rate || 0)));
  const main = Math.max(4, (1 - dup) * 100);
  return `<span class="mini" title="点击字段查看值域明细"><span class="mini-seg seg-0" style="width:${main}%"></span><span class="mini-seg seg-1" style="width:${Math.max(2, dup * 100)}%"></span></span>`;
}

function sampleState(c) {
  if (c.is_sensitive && c.sensitive_action === "skip") return `<span class="sens">敏感·不取样例</span>`;
  if (c.is_sensitive) return `<span class="sens sens-mask">敏感·脱敏</span>`;
  return `<span class="ok-text">点击查看</span>`;
}

async function getTableDetail(tableId) {
  if (state.tableCache.has(tableId)) return state.tableCache.get(tableId);
  const data = await api(`/api/tables/${encodeURIComponent(tableId)}?snapshot_id=${encodeURIComponent(state.snapshotId)}`);
  state.tableCache.set(tableId, data);
  return data;
}

async function renderL2(tableId) {
  const root = $("app");
  if (!state.snapshotId) return emptyState();
  disposeCharts();
  root.innerHTML = `<div class="card na" style="padding:40px;text-align:center">加载表详情...</div>`;
  const data = await getTableDetail(tableId);
  const t = data.table;
  const cols = data.columns || [];
  root.innerHTML = `
    <div class="fade" style="--d:0">
      <a href="#/tables" class="back">← 表列表</a>
      <div class="card tbl-head">
        <div>
          <h2 class="mono">${esc(t.table_name)}</h2>
          <div class="td-sub">${esc(t.table_comment || "")} · <span class="tag">${esc(inferDomain(t))}</span> <span class="tag tag-na">${esc(t.schema_name || "default")}</span></div>
        </div>
        <div class="tiles">
          <div class="tile"><i data-metric="ROW_COUNT" class="m-name">行数</i><b>${fmtNum(t.row_count)}</b></div>
          <div class="tile"><i>字段数</i><b>${fmt(t.column_count)}</b></div>
          <div class="tile"><i data-metric="FILL_RATE" class="m-name">平均有值率</i><b class="${pctClass(t.avg_fill_rate)}-text">${fmtPct(t.avg_fill_rate)}</b></div>
          <div class="tile"><i>主键</i><b class="mono" style="font-size:13px">${esc(primaryKeyLabel(t))}</b></div>
          <div class="tile"><i data-metric="PK_DUP" class="m-name">主键重复率</i><b class="${Number(t.pk_duplicate_rate || 0) > 0 ? "bad" : "good"}-text">${t.pk_duplicate_skipped_reason ? "—" : fmtPct(t.pk_duplicate_rate || 0)}</b></div>
          <div class="tile"><i data-metric="ATTR_DUP" class="m-name">数据重复率</i><b class="${Number(t.data_duplicate_rate || 0) > 0 ? "bad" : "good"}-text">${t.data_duplicate_skipped_reason ? "—" : fmtPct(t.data_duplicate_rate || 0)}</b></div>
          <div class="tile"><i>时间跨度</i><b class="tile-date">${t.date_column ? `${esc(t.min_date)} ~ ${esc(t.max_date)}<span>via ${esc(t.date_column)}</span>` : "—"}</b></div>
        </div>
      </div>
      <div class="card" style="margin-top:14px">
        <table class="tbl">
          <thead><tr>
            <th>字段</th><th>类型</th>
            <th class="num">有值率 / 有效率 <span data-metric="VALID_RATE" class="m-name">?</span></th>
            <th class="num">重复率 <span data-metric="DUP_RATE" class="m-name">?</span></th>
            <th>值域分布 <span data-metric="VALUE_DIST" class="m-name">?</span></th>
            <th>样例 <span data-metric="SAMPLE_DATA" class="m-name">?</span></th>
          </tr></thead>
          <tbody>${cols.map((c, i) => `
            <tr class="fade" style="--d:${i * 24}" data-column="${esc(c.column_id)}">
              <td><b class="mono">${esc(c.column_name)}</b>${primaryKeyLabel(t).split(",").map((x) => x.trim()).includes(c.column_name) ? '<span class="pk">PK</span>' : ""}<div class="td-sub">${esc(c.column_comment || "")}</div></td>
              <td class="mono td-sub">${esc(c.data_type)}</td>
              <td class="num"><span class="${pctClass(c.fill_rate)}-text">${fmtPct(c.fill_rate)}</span> <span class="td-sub">/ ${fmtPct(c.valid_rate)}</span></td>
              <td class="num">${fmtPct(c.duplicate_rate)}</td>
              <td>${miniDistForColumn(c)}</td>
              <td>${sampleState(c)}</td>
            </tr>`).join("")}</tbody>
        </table>
      </div>
    </div>`;
}

async function openColumn(columnId) {
  let data = state.columnCache.get(columnId);
  if (!data) {
    data = await api(`/api/columns/${encodeURIComponent(columnId)}?snapshot_id=${encodeURIComponent(state.snapshotId)}`);
    state.columnCache.set(columnId, data);
  }
  const c = data.column;
  const dist = data.value_dist || [];
  const samples = data.samples || [];
  const gap = c.fill_rate != null && c.valid_rate != null ? Number(c.fill_rate) - Number(c.valid_rate) : null;
  const nullRate = c.row_count ? Number(c.null_count || 0) / Number(c.row_count) : null;
  $("drawer").innerHTML = `
    <div class="dr-head">
      <div>
        <div class="mono dr-title">${esc(c.column_name)}</div>
        <div class="td-sub">${esc(c.table_name)} · ${esc(c.column_comment || "")}</div>
      </div>
      <button class="dr-close" data-close-drawer type="button">✕</button>
    </div>
    <div class="dr-badges">
      <span class="tag">${esc(c.data_type)}</span>
      ${c.is_sensitive ? `<span class="sens">敏感字段 · ${esc(c.sensitive_action || "")}</span>` : ""}
      ${c.skipped_reason ? `<span class="tag tag-na">${esc(c.skipped_reason)}</span>` : ""}
    </div>
    <div class="dr-stats">
      <div class="ds"><i data-metric="FILL_RATE" class="m-name">有值率</i><b class="${pctClass(c.fill_rate)}-text">${fmtPct(c.fill_rate)}</b></div>
      <div class="ds"><i data-metric="VALID_RATE" class="m-name">有效率</i><b class="${pctClass(c.valid_rate)}-text">${fmtPct(c.valid_rate)}</b>${gap > 0.15 ? `<em class="ds-gap">落差 ${(gap * 100).toFixed(1)}pt</em>` : ""}</div>
      <div class="ds"><i data-metric="NULL_RATE" class="m-name">NULL 率</i><b>${fmtPct(nullRate)}</b></div>
      <div class="ds"><i data-metric="DUP_RATE" class="m-name">重复率</i><b>${fmtPct(c.duplicate_rate)}</b></div>
      <div class="ds"><i>distinct</i><b>${fmtNum(c.distinct_count)}</b></div>
    </div>
    <div class="dr-badges muted-row">
      <span>NULL ${fmtNum(c.null_count)}</span>
      <span>空串 ${fmtNum(c.empty_count)}</span>
      <span>占位 ${fmtNum(c.placeholder_count)}</span>
    </div>
    ${dist.length ? `<h4 data-metric="VALUE_DIST" class="m-name">值域分布</h4><div id="dr-chart" style="height:${Math.min(dist.length, 10) * 34 + 42}px"></div>` : `<h4 data-metric="VALUE_DIST" class="m-name">值域分布</h4><div class="sample-skip">无值域分布，可能是高基数字段、敏感 skip 字段或尚未采集。</div>`}
    <h4 data-metric="SAMPLE_DATA" class="m-name">样例数据</h4>
    ${samples.length
      ? `<div class="samples">${samples.map((s) => `<span class="sample ${s.is_masked ? "sample-na" : ""}">${esc(s.sample_value)}</span>`).join("")}</div><div class="dr-foot">样例仅用于感知数据形态，不构成统计推断${c.is_sensitive ? " · 已按敏感策略处理" : ""}</div>`
      : `<div class="sample-skip">${c.is_sensitive && c.sensitive_action === "skip" ? "该字段按敏感配置 skip，样例不落盘。" : "暂无样例数据。"}</div>`}
    <h4>指标口径</h4>
    ${(data.metrics || []).map((m) => `
      <div class="metric-card">
        <strong>${esc(m.name)} · ${esc(m.metric_code)}</strong>
        <p>${esc(m.definition)}</p>
        <p class="td-sub">公式：${esc(m.formula)}</p>
        <p class="td-sub">边界：${esc(m.boundary)}</p>
      </div>`).join("")}
    <div class="dr-foot">快照 #${shortId(state.snapshotId)} · ${fmtDateTime(c.computed_at)}</div>`;
  $("drawer").classList.add("open");
  $("overlay").classList.add("open");

  if (dist.length) {
    const chart = echarts.init($("dr-chart"));
    chart.setOption({
      grid: { left: 8, right: 64, top: 10, bottom: 8, containLabel: true },
      tooltip: { trigger: "axis", axisPointer: { type: "shadow" }, formatter: (p) => `${esc(p[0].name)}<br/>${fmtNum(p[0].data.count)} 行 · ${fmtPct(p[0].value)}` },
      xAxis: { type: "value", max: 1, splitLine: { lineStyle: { color: "#eef2f6" } }, axisLabel: { formatter: (v) => `${(v * 100).toFixed(0)}%` } },
      yAxis: { type: "category", inverse: true, data: dist.map((d) => d.value_label), axisLabel: { fontSize: 11 } },
      series: [{
        type: "bar",
        barWidth: 16,
        data: dist.map((d) => ({ value: Number(d.ratio || 0), count: d.value_count, itemStyle: { color: d.is_masked ? "#cbd5e1" : "#14b8a6", borderRadius: [0, 4, 4, 0] } })),
        label: { show: true, position: "right", formatter: (p) => fmtPct(p.value), fontSize: 11, color: "#64748b" },
      }],
    });
  }
}

async function openTaskLog(snapshotId) {
  const data = await api(`/api/snapshots/${encodeURIComponent(snapshotId)}/tasks`);
  const snapshot = data.snapshot || {};
  const progress = data.progress || {};
  const tasks = data.tasks || [];
  const failed = tasks.filter((task) => task.status === "failed");
  const running = tasks.filter((task) => task.status === "running");
  const canPause = ["created", "running"].includes(snapshot.status);
  const canResume = snapshot.status === "paused";
  $("drawer").innerHTML = `
    <div class="dr-head">
      <div>
        <div class="mono dr-title">快照任务日志</div>
        <div class="td-sub">${esc(snapshot.source_name || "-")} · #${shortId(snapshot.snapshot_id)}</div>
      </div>
      <div class="dr-actions">
        <button data-pause="${esc(snapshot.snapshot_id)}" type="button" ${canPause ? "" : "disabled"}>暂停</button>
        <button data-resume="${esc(snapshot.snapshot_id)}" type="button" ${canResume ? "" : "disabled"}>恢复</button>
        <button class="dr-close" data-close-drawer type="button">×</button>
      </div>
    </div>
    <div class="dr-stats">
      <div class="ds"><i>状态</i><b class="${taskStatusClass(snapshot.status)}">${statusText(snapshot.status)}</b></div>
      <div class="ds"><i>进度</i><b>${fmtProgressPct(progress.progress, snapshot.status)}</b></div>
      <div class="ds"><i>任务</i><b>${fmt(progress.done_tasks)} / ${fmt(progress.total_tasks)}</b></div>
      <div class="ds"><i>失败</i><b class="${failed.length ? "bad-text" : "good-text"}">${fmt(failed.length)}</b></div>
      <div class="ds"><i>运行中</i><b class="${running.length ? "warn-text" : ""}">${fmt(running.length)}</b></div>
    </div>
    ${snapshot.error_message ? `<div class="task-error"><b>快照错误</b><pre>${esc(snapshot.error_message)}</pre></div>` : ""}
    <div class="task-log-list">
      ${tasks.length ? tasks.map((task) => `
        <div class="task-log-item ${task.status === "failed" ? "task-log-failed" : ""}">
          <div class="task-log-main">
            <b>${esc(taskTypeLabel(task))}</b>
            ${taskTypeMeta(task) ? `<i>${esc(taskTypeMeta(task))}</i>` : ""}
          </div>
          <div class="task-log-meta">
            <strong class="${taskStatusClass(task.status)}">${statusText(task.status)}</strong>
            <span>${fmtDuration(task.duration_ms)}</span>
            <span>attempt ${fmt(task.attempt)} · crash ${fmt(task.crash_count)}</span>
          </div>
          <div class="task-log-time">
            <span>开始 ${fmtDateTime(task.started_at)}</span>
            <span>结束 ${fmtDateTime(task.finished_at)}</span>
          </div>
          ${task.error_message ? `<pre class="task-log-error">${esc(task.error_message)}</pre>` : ""}
        </div>`).join("") : `<div class="empty-inline">暂无任务记录</div>`}
    </div>
    <div class="dr-foot">失败任务会保留异常信息和短堆栈，用于定位连接、SQL、权限或数据类型问题。</div>`;
  $("drawer").classList.add("open");
  $("overlay").classList.add("open");
}

function closeDrawer() {
  $("drawer").classList.remove("open");
  $("overlay").classList.remove("open");
}

async function route() {
  updateNavState();
  closeDrawer();
  const hash = location.hash || "#/";
  const tableMatch = hash.match(/^#\/table\/(.+)$/);
  if (tableMatch) {
    await renderL2(decodeURIComponent(tableMatch[1]));
  } else if (hash.startsWith("#/tables")) {
    renderL1();
  } else {
    renderL0();
  }
  window.scrollTo(0, 0);
}

function sourcePayload() {
  return {
    name: $("sourceName").value.trim(),
    dialect: $("sourceDialect").value,
    conn_uri: $("sourceUri").value.trim(),
    options: {},
  };
}

async function renderExportReview() {
  if (!state.snapshotId || !$("exportReview")) return;
  const data = await api(`/api/export-review/${encodeURIComponent(state.snapshotId)}`);
  $("exportReview").textContent = `导出清单：${fmt(data.content.tables)} 表，${fmt(data.content.columns)} 字段，样例 ${fmt(data.content.samples)} 条，敏感字段 ${fmt(data.content.sensitive_columns)} 个。`;
}

async function downloadReport() {
  if (!state.snapshotId) return toast("没有可导出的快照");
  const response = await fetch(`/api/reports/${encodeURIComponent(state.snapshotId)}/docx`, { method: "POST" });
  if (!response.ok) return toast(await response.text());
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `DataPulse_report_${shortId(state.snapshotId)}.docx`;
  a.click();
  URL.revokeObjectURL(url);
}

document.addEventListener("mouseover", (event) => {
  const el = event.target.closest("[data-metric]");
  if (el) showMetricTip(el);
});

document.addEventListener("mouseout", (event) => {
  if (event.target.closest("[data-metric]")) hideMetricTip();
});

document.addEventListener("input", (event) => {
  if (event.target.id === "l1-q") {
    l1State.q = event.target.value.trim();
    drawL1Rows();
  }
});

document.addEventListener("click", async (event) => {
  const target = event.target.closest("button, tr");
  if (!target) return;
  try {
    if (target.id === "toggleConsoleBtn") $("scanConsole").classList.toggle("collapsed");
    if (target.id === "refreshBtn") await refreshAll();
    if (target.id === "testSourceBtn") {
      const data = await api("/api/sources/test", { method: "POST", body: JSON.stringify(sourcePayload()) });
      toast(`连接成功：发现 ${data.table_count} 张表`);
    }
    if (target.id === "saveSourceBtn") {
      await api("/api/sources", { method: "POST", body: JSON.stringify(sourcePayload()) });
      toast("数据源已保存");
      await refreshAll();
    }
    if (target.id === "deleteSourceBtn") {
      const select = $("sourceSelect");
      const sourceId = select?.value;
      if (!sourceId) return toast("没有可删除的数据源");
      const sourceName = select.selectedOptions[0]?.textContent || "当前数据源";
      if (!confirm(`删除 ${sourceName}？`)) return;
      await api(`/api/sources/${encodeURIComponent(sourceId)}`, { method: "DELETE" });
      toast("数据源已删除");
      await refreshAll();
    }
    if (target.id === "startScanBtn") {
      const sourceId = $("sourceSelect").value;
      const tables = $("scanTables").value.split(",").map((x) => x.trim()).filter(Boolean);
      const data = await api("/api/scans", { method: "POST", body: JSON.stringify({ source_id: sourceId, tables }) });
      state.snapshotId = data.snapshot_id;
      state.tableCache.clear();
      state.columnCache.clear();
      toast("扫描已启动");
      startPolling();
      await refreshAll();
    }
    if (target.id === "reportBtn") await downloadReport();
    if (target.id === "clearSamplesBtn") {
      const data = await api("/api/samples/clear", { method: "POST" });
      state.columnCache.clear();
      toast(`已清空 ${data.deleted_rows} 条样例，并执行 checkpoint`);
      await refreshAll();
    }
    if (target.dataset.selectSnapshot) {
      state.snapshotId = target.dataset.selectSnapshot;
      state.tableCache.clear();
      state.columnCache.clear();
      await refreshAll();
    }
    if (target.dataset.taskLog) await openTaskLog(target.dataset.taskLog);
    if (target.dataset.pause) {
      await api(`/api/scans/${encodeURIComponent(target.dataset.pause)}/pause`, { method: "POST" });
      await refreshAll({ reroute: false });
      if ($("drawer").classList.contains("open")) await openTaskLog(target.dataset.pause);
    }
    if (target.dataset.resume) {
      await api(`/api/scans/${encodeURIComponent(target.dataset.resume)}/resume`, { method: "POST" });
      await refreshAll({ reroute: false });
      if ($("drawer").classList.contains("open")) await openTaskLog(target.dataset.resume);
    }
    if (target.dataset.deleteSnapshot) {
      await api(`/api/snapshots/${encodeURIComponent(target.dataset.deleteSnapshot)}`, { method: "DELETE" });
      if (state.snapshotId === target.dataset.deleteSnapshot) state.snapshotId = null;
      state.tableCache.clear();
      state.columnCache.clear();
      toast("快照已删除");
    }
    if (target.dataset.deleteSnapshot) await refreshAll({ reroute: false });
    if (target.dataset.tableRoute) location.hash = `#/table/${encodeURIComponent(target.dataset.tableRoute)}`;
    if (target.dataset.column) await openColumn(target.dataset.column);
    if (target.dataset.schema) {
      l1State.schema = target.dataset.schema;
      renderL1();
    }
    if (target.dataset.sort) {
      if (l1State.sort === target.dataset.sort) l1State.dir *= -1;
      else {
        l1State.sort = target.dataset.sort;
        l1State.dir = ["table_name"].includes(target.dataset.sort) ? 1 : -1;
      }
      drawL1Rows();
    }
    if (target.dataset.closeDrawer !== undefined) closeDrawer();
  } catch (error) {
    toast(error.message);
  }
});

$("overlay").addEventListener("click", closeDrawer);
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") closeDrawer();
});
window.addEventListener("hashchange", () => route().catch((error) => toast(error.message)));
window.addEventListener("resize", () => state.charts.forEach((chart) => chart.resize()));

renderTopbar();
renderConsole();
refreshAll().catch((error) => {
  renderTopbar();
  renderConsole();
  emptyState();
  toast(error.message);
});
