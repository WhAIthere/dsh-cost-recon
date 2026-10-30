
const DATA = __DATA__;
DATA.msgs = DATA.mrows.map(r => { const o = {}; DATA.mcols.forEach((c,i) => o[c] = r[i]); return o; });

const DIM_LABEL = {day:"天", hour:"小时", week:"周", workspace:"工作区", session:"会话",
                   turn:"轮次", step:"步", model:"模型", peak:"峰谷", depth:"主/子"};
// 显示名（来自 DSH 的映射表；缺了就回落显示 id，不影响任何统计）
const LBL = (DATA.labels && DATA.labels.workspace) ? DATA.labels : {workspace:{}, session:{}, turn:{}};
function wsName(w){ return LBL.workspace[w] || null; }
function sessName(sid){ return LBL.session[sid] || null; }
function turnName(sid, turn){ return LBL.turn[sid + "|" + turn] || null; }
const DIMS = DATA.dim_order;
const C = {cache:"var(--c1)", in:"var(--c2)", out:"var(--c3)", book:"var(--c4)",
           lvl:["#3b5bdb","#f59f00","#2f9e44"]};

// ---------------- 小工具
const $ = id => document.getElementById(id);
const fmtInt = n => (n||0).toLocaleString("en-US");
const fmtMoney = n => (n||0).toFixed(3);
const esc = s => String(s == null ? "" : s).replace(/[&<>"]/g,
    c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const pct = (a,b) => b ? (a/b*100) : 0;
// 「具体 token 数」那三列：整棵子树都**没有一点 usage**（中断空壳）时显示横杠，
// 别用 0 假装"这次调用真的只花了 0 token" —— 两者是两回事。
const rawCols = nd => {
  const none = ((nd.cache || 0) + (nd.in || 0) + (nd.out || 0)) === 0;
  const cell = v => `<td class="rawcol">${none ? "—" : fmtInt(v)}</td>`;
  return cell(nd.cache) + cell(nd.in) + cell(nd.out);
};

function isoWeek(ts){
  const d = new Date(ts), t = new Date(Date.UTC(d.getFullYear(), d.getMonth(), d.getDate()));
  const day = t.getUTCDay() || 7; t.setUTCDate(t.getUTCDate() + 4 - day);
  const y0 = new Date(Date.UTC(t.getUTCFullYear(),0,1));
  return t.getUTCFullYear() + "-W" + String(Math.ceil(((t - y0)/86400000 + 1)/7)).padStart(2,"0");
}
function dimKey(d, row){
  switch(d){
    case "day": return row.day;
    case "hour": return String(row.hour);
    case "week": return isoWeek(row.ts);
    case "workspace": return row.workspace;
    case "session": return row.session_id;
    case "turn": return row.turn_no == null ? "-" : String(row.turn_no);
    case "step": return row.turn_no == null ? "-"
      : (String(row.turn_no) + "." + String(row.step_no == null ? 1 : row.step_no));
    case "model": return row.model;
    case "peak": return row.peak ? "高峰" : "非高峰";
    case "depth": return row.depth === 0 ? "主会话" : "子代理";
  }
  return "";
}
// 排序用的键：时间维度用「结束时间」（= 最后一次活动），不是起点。
// 用起点排会让"很久以前开始、但你刚刚还在用"的会话一直沉在底部，那是错的。
function sortKey(d, row){
  if (d === "day" || d === "week" || d === "hour") return row.ts;
  if (d === "step") return String(row.turn_no == null ? 0 : row.turn_no).padStart(6,"0")
                        + "." + String(row.step_no == null ? 1 : row.step_no).padStart(4,"0");
  return dimKey(d, row);
}
function prettyDim(d, v, ctx){
  v = (v == null) ? "" : v;
  if (d === "workspace"){
    const n = wsName(v);
    if (n) return n;
    return String(v).replace(/^-+|-+$/g,"").split("-").pop() || "(空)";   // 没映射就退回目录名末段
  }
  if (d === "session"){
    const n = sessName(v);
    if (n) return n.length > 24 ? n.slice(0,24) + "…" : n;
    return String(v).replace("session-","").slice(0,8) || "(空)";
  }
  if (d === "step"){
    const parts = String(v).split(".");
    const sn = (ctx && ctx.snippet) ? String(ctx.snippet) : "";
    const head = parts.length > 1 ? ("第" + parts[0] + "轮·第" + parts[1] + "步") : ("第" + v + "步");
    return sn ? (head + " " + (sn.length > 36 ? sn.slice(0,36) + "…" : sn)) : head;
  }
  if (d === "turn"){
    const sid = ctx && ctx.session_id;
    const t = sid ? turnName(sid, v) : null;
    // 小数轮次键（29.5）是"压缩摘要"这类额外节点：它的标签已经写全了（"第 29 轮中 · 压缩摘要"），
    // 别再拼一层"第29.5轮"，否则会显示成「第29.5轮 第 29 轮中 · 压缩摘要」。
    if (String(v).indexOf(".") >= 0) return t || ("第" + v + "轮");
    return t ? ("第" + v + "轮 " + t) : ("第" + v + "轮");
  }
  return String(v) || "(空)";
}
// 鼠标悬停给完整信息（会话名过长会被截断，这里给全名 + id）
function cellTitle(d, v, ctx){
  v = (v == null) ? "" : v;
  if (d === "session"){
    const n = sessName(v);
    return n ? (n + "  (" + v + ")") : v;
  }
  if (d === "workspace"){
    const n = wsName(v);
    return n ? (n + "  (" + v + ")") : v;
  }
  if (d === "turn"){
    const t = ctx && ctx.session_id ? turnName(ctx.session_id, v) : null;
    // 小数键（29.5）= 压缩摘要这类额外节点，标签已经写全，别再套"第 29.5 轮："
    if (String(v).indexOf(".") >= 0) return t || ("第 " + v + " 轮");
    return "第 " + v + " 轮" + (t ? "：" + t : "");
  }
  return "";
}
// 给某个节点（值为 v 的维度 d）找上下文：会话级维度要 session_id，轮次级还要 turn
function nodeCtx(node){
  const it = (node && node.leafItems && node.leafItems.length) ? node.leafItems[0]
           : (node && node.items && node.items.length) ? node.items[0] : null;
  return it || {};
}

// ---------------- 状态
const SORT_MODES = ["ts","ts_desc","cost","tokens","calls"];   // 时间升/降 + 三个指标降序
let SORTS_READY = false;
const S = {
  dims: ["day","workspace","session"],
  metric: "cost",
  sortOf: ["ts","ts","ts"],  // 第 1/2/3 层各自的排序模式
  mainOnly: false,
  withReplay: false,
  withAnomaly: false,        // 含异常：把中断空壳（及其估算金额/token）一并计入统计
  from: DATA.range[0], to: DATA.range[1],
  fromTime: "00:00", toTime: "23:59",   // 时间的分钟级细分（默认整天：00:00 ~ 23:59）
  workspaces: null,          // null = 全选
  kw: "",
  hotCost: 0.1,               // 标红阈值：单行花费超过它 → 标红（只上色，不过滤）
  hotPeak: 50,                // 标红阈值：单行峰值占比超过它 → 标红（单位 %）
  hotLeafOnly: false,         // 只标红"当前可见的最下层"：没有下级、或下级被折叠的行
  collapsed: new Set(),      // 折叠的节点路径（用路径而不是数字 id：数字 id 每次重排都会变，
  selected: null,            // 换排序就丢失折叠状态；路径跟内容绑定，重排后依然有效）
  view: "tree",
  showRaw: false,            // 是否显示"具体 token 数"三列
  dark: false,               // 深色模式（只换中性色；标红/标黄的告警色不变）
  showRb: true,              // 是否显示「重建参考」列
  tree: null,                // 构建好的树
};

// ---------------- 预设查看模式（纯前端的一键设置，只改下面列出的键）
// 真正生效的清单来自工具根目录的 `presets.txt`（生成看板时读进来、放在 DATA.presets），
// 下面这份只是**兜底**：文件缺失/没读懂时用它，自测也用它。
// 点一下＝套用一次；下拉本身**不反映界面状态**（选完就弹回占位符），它只是一组按钮。
// 每档只写它要覆盖的键，没提到的选项一律不动；日期只由 range 明确指定时才改。
const PRESET_COLS = "名字 | 层级 | 日期(today/full/空) | 排序(可写一个管三层) | 花费标红(元) | 峰值标红(%) | 只标最下层(yes/no)";
const BUILTIN_PRESETS = [
  {name: "今天 · 工作区›会话›轮次（花费>0.5 元，不标峰值）",
   dims: ["workspace", "session", "turn"], range: "today",
   sortOf: ["ts_desc", "ts_desc", "ts_desc"],
   hotCost: 0.5, hotPeak: 0, hotLeafOnly: true},
  {name: "天›工作区›会话（花费>1 元 / 峰值>50%）",
   dims: ["day", "workspace", "session"], range: "full",
   sortOf: ["cost", "cost", "cost"],
   hotCost: 1, hotPeak: 50, hotLeafOnly: true},
  {name: "工作区›会话›天（花费>2 元 / 峰值>50%）",
   dims: ["workspace", "session", "day"], range: "full",
   sortOf: ["cost", "cost", "cost"],
   hotCost: 2, hotPeak: 50, hotLeafOnly: true},
];
// 0 档永远是"回到刚打开时"，由代码加，不进配置文件
const SNAPSHOT_PRESET = {id: 0, name: "上次状态（刚打开时）", snapshot: true};
const PRESETS = [SNAPSHOT_PRESET].concat(
  ((DATA.presets && DATA.presets.length) ? DATA.presets : BUILTIN_PRESETS)
    .map((p, i) => Object.assign({}, p, {id: i + 1})));
const PRESET_WARN = (DATA.preset_warn && DATA.preset_warn.length)
  ? "预设文件有 " + DATA.preset_warn.length + " 行没读懂（已跳过）" : "";

// 日期档位：预设里写 today / full 就行，省得每档都写具体日期
const RANGES = {
  today: () => { const d = latestDay(); return [d, d]; },
  full:  () => [DATA.range[0], latestDay()],
};

// 会被记住的状态（下次打开自动套用）。**不含**日期、关键字、折叠、选中行——
// 日期每次默认 DATA.range[0] ~ 最新一天；其它三个是"当下操作"，记了反而困惑。
const PERSIST_KEYS = ["dims", "sortOf", "hotCost", "hotPeak", "hotLeafOnly",
                      "mainOnly", "withReplay", "withAnomaly", "view", "showRaw", "dark", "showRb",
                      "workspaces"];
const STORE_KEY = "dshTokenStats.view";     // localStorage 键（同一个 html 文件复用）
let P0_SNAPSHOT = null;                     // 打开那一刻的状态＝预设 0 的内容

// ---------------- 过滤
function activeDims(){ return S.dims.filter(d => d && d !== "none"); }
// 「日期 + 分钟」→ 毫秒边界。**结束那一分钟整分钟都算进去**（填 23:59 = 一直算到
// 23:59:59.999，等价于 24:00），这样默认值就是"整天"，不用让用户填 24:00。
function rangeBounds(){
  const t0 = Date.parse(S.from + "T" + (S.fromTime || "00:00") + ":00");
  let t1 = Date.parse(S.to + "T" + (S.toTime || "23:59") + ":00");
  if (isFinite(t1)) t1 += 59999;
  return [isFinite(t0) ? t0 : -Infinity, isFinite(t1) ? t1 : Infinity];
}
function currentRows(){
  const kw = S.kw.trim().toLowerCase();
  const kws = kw ? kw.split(",").map(s => s.trim()).filter(Boolean) : [];
  const [t0, t1] = rangeBounds();
  return DATA.msgs.filter(m => {
    if (m.ts < t0 || m.ts > t1) return false;
    if (S.mainOnly && m.depth !== 0) return false;
    // 重放行看「含重放」，中断空壳看「含异常」——两者都是 keep=0，但来源和用途不同
    if (m.replay && !(m.interrupted ? S.withAnomaly : S.withReplay)) return false;
    if (S.workspaces && !S.workspaces.has(m.workspace)) return false;
    if (kws.length){
      const hay = (m.session_id + " " + m.model).toLowerCase();
      if (!kws.some(k => hay.includes(k))) return false;
    }
    return true;
  });
}

// ---------------- 建树
function blankNode(key, label, dim, depth){
  return {key, label, dim, depth, cost:0, tokens:0, in:0, out:0, cache:0, calls:0,
          peak_cost:0, cost_cache:0, cost_in:0, cost_out:0, replay_calls:0, anomaly:0,
          unknown:0, est_cost:0, est_tokens:0,
          rb:0, rbLvl:0, rbN:0,            // 疑似缓存重建（只作参考）：累计代价 / 最高档 / 次数
          srch:0, srchN:0,                 // 原生搜索开销（只作参考）：累计金额 / 次数
          tools:0,                         // 工具调用次数（纯展示，不参与金额）
          minTs:Infinity, maxTs:-Infinity, children:[], items:[], parent:null, path:"",
          sortBy: "", sortLabel: "", leafItems:[]};
}
function addToNode(nd, m){
  // 中断空壳：token/cost 本来是 0，勾了「含异常」才把估算值算进来（口径开关只影响显示）
  const est = (S.withAnomaly && m.interrupted) ? 1 : 0;
  // 未知价目（价目表里没这一档）：cost_est 是 -1 —— **钱一律不计**，只计 token 与次数
  const unk = (m.cost_est < 0) ? 1 : 0;
  const cost = (unk ? 0 : (m.cost_est || 0)) + (est ? (m.est_cost || 0) : 0);
  const toks = (m.total_tokens || 0) + (est ? (m.est_tokens || 0) : 0);
  nd.cost += cost; nd.tokens += toks; nd.in += m.in_tokens;
  nd.out += m.out_tokens; nd.cache += m.cache_read; nd.calls += 1;
  nd.cost_cache += m.cost_cache; nd.cost_in += m.cost_in; nd.cost_out += m.cost_out;
  if (m.peak && !unk) nd.peak_cost += m.cost_est;
  if (unk) nd.unknown += 1;
  // 疑似缓存重建：钱已经含在 cost_est 里，这里只累加"多花了多少"给那一列看（不影响任何数字）
  if (m.rb > 0){ nd.rb += m.rb; nd.rbN += 1; nd.rbLvl = Math.max(nd.rbLvl, m.rbLvl || 0); }
  // 原生搜索：钱已经含在 cost_est 里，这里只把"其中搜索那部分"拆出来给列看
  if (m.srchN){ nd.srch += (m.srch || 0); nd.srchN += m.srchN; }
  if (m.tn) nd.tools += m.tn;
  if (m.replay) nd.replay_calls += 1;
  if (m.interrupted){                       // 异常往上传播：折叠着也能在祖先行看到 ⚠
    if (m.replayed){ nd.anomaly_copy = (nd.anomaly_copy || 0) + 1; }
    else {
      nd.anomaly += 1;
      nd.est_cost += m.est_cost || 0;
      nd.est_tokens += m.est_tokens || 0;
      if (m.est_note && !nd.est_note) nd.est_note = m.est_note;
    }
  }
  if (m.ts < nd.minTs) nd.minTs = m.ts;
  if (m.ts > nd.maxTs) nd.maxTs = m.ts;
  nd.items.push(m);
}
function buildTree(){
  const dims = activeDims();
  const rows = currentRows();
  const root = blankNode([], "全部", null, -1);
  root.parent = null;
  const map = new Map();            // path -> node
  map.set("", root);
  for (const m of rows){
    let path = "", node = root;
    addToNode(root, m);
    for (let i = 0; i < dims.length; i++){
      const d = dims[i];
      const kv = dimKey(d, m), ks = sortKey(d, m);
      const np = path ? (path + "\u0001" + kv) : kv;   // 首段不带分隔符，避免 path 以空段开头
      let child = map.get(np);
      if (!child){
        child = blankNode(kv, prettyDim(d, kv, m), d, i);
        child.parent = node; child.path = np;
        child.sortBy = ks;                 // 原始键（时间维度的原始键就是 ts）
        child.sortLabel = child.label;     // 显示名，用于并列时排序
        node.children.push(child); map.set(np, child);
      }
      addToNode(child, m);
      node = child; path = np;
    }
    if (node !== root) node.leafItems.push(m);
  }
  // 递归排序：每一层（按节点自己的 dim）用自己那一档排序模式
  const val = (nd, mode) => {
    switch (mode){
      case "cost": return nd.cost;
      case "tokens": return nd.tokens;
      case "calls": return nd.calls;
      default: return 0;
    }
  };
  const cmpAt = (mode) => {
    const byTime = (mode === "ts" || mode === "ts_desc");
    const sign = (mode === "ts_desc" || mode === "cost" || mode === "tokens" || mode === "calls")
                 ? -1 : 1;
    // 时间一律按「结束时间」= 最后一次活动来比，见上面的说明
    const timeOf = nd => (+nd.maxTs || 0);
    // 并列时按「显示出来的名字」排，而不是原始 id ——
    // 否则界面上会出现"名字看着乱序"（比如 Bounded… 排在 doc2docx 前面，因为 id 是 2f…/4d…）
    const nameOf = nd => String(nd.sortLabel || nd.key || "");
    const byName = (a,b) => nameOf(a) < nameOf(b) ? -1 : nameOf(a) > nameOf(b) ? 1 : 0;
    return (a,b) => {
      if (byTime) return sign * (timeOf(a) - timeOf(b)) || byName(a,b);
      return sign * (val(a, mode) - val(b, mode)) || byName(a,b) || (timeOf(a) - timeOf(b));
    };
  };
  (function walk(nd){
    if (!nd.children.length) return;
    // 关键：要排的是 nd.children，用的是【子节点】的层号 = nd.depth + 1。
    // 之前误用 nd.depth（父节点的层号），导致每个下拉都排到了下一层：
    // 第 1 个下拉排了第 2 层、第 2 个排了第 3 层，第 3 个永远轮不到。
    const lvl = Math.min(Math.max(nd.depth + 1, 0), 2);
    nd.children.sort(cmpAt(S.sortOf[lvl] || "ts"));
    nd.children.forEach(walk);
  })(root);
  return root;
}
// 建树有成本（几千行），仅在筛选/层级/排序变化时重算
let TREE_CACHE = {key:"", root:null};
function treeKey(){
  const s = S.sortOf || [];
  // 注意：**凡是会改变"哪些行进树 / 行里的数字"的开关都必须在这里**，
  // 否则 getTree() 会命中缓存、开关点了没反应（withAnomaly 就这么漏过一次）
  return [activeDims().join(","), S.from, S.to, S.fromTime, S.toTime,
          S.mainOnly, S.withReplay, S.withAnomaly,
          S.workspaces ? [...S.workspaces].sort().join("|") : "*", S.kw,
          s[0], s[1], s[2]].join("~~");
}
function getTree(){
  const k = treeKey();
  if (TREE_CACHE.key !== k){ TREE_CACHE = {key:k, root: buildTree()}; }
  return TREE_CACHE.root;
}
function nodesOf(root){
  const out = [];
  (function walk(nd){ if (nd !== root){ out.push(nd); } nd.children.forEach(walk); })(root);
  return out;
}
// 按路径查找节点（路径与排序无关，重排后依然指向同一个内容）
function findNode(root, path){
  if (!path) return root;
  let hit = null;
  (function walk(nd){ if (hit) return; if (nd.path === path) { hit = nd; return; } nd.children.forEach(walk); })(root);
  return hit;
}

// ---------------- 树的缩进渲染
// 占比条：把百分比写进色块里，太窄的块自动不显示数字（靠 span.empty 占位）
function pctBar(parts, title){
  const total = parts.reduce((s,p) => s + Math.max(p.value, 0), 0);
  if (!(total > 0)) return `<span class="pbar"><span class="empty" style="width:100%"></span></span>`;
  const cells = parts.map(p => {
    const w = Math.max(p.value, 0) / total * 100;
    const show = w >= 9 ? (w.toFixed(1) + "%") : "";
    return `<span class="${p.cls}" style="width:${w.toFixed(3)}%" title="${esc(p.name + "：" + w.toFixed(1) + "%")}">${show}</span>`;
  }).join("");
  return `<span class="pbar" ${title ? `title="${esc(title)}"` : ""}>${cells}</span>`;
}
function tokenBar(node){
  return pctBar([
    {name:"缓存命中 " + fmtInt(node.cache), value:node.cache, cls:"c1"},
    {name:"未命中输入 " + fmtInt(node.in), value:node.in, cls:"c2"},
    {name:"输出 " + fmtInt(node.out), value:node.out, cls:"c3"},
  ], "按 token 数量：" + fmtInt(node.cache + node.in + node.out));
}
function costBar(node){
  return pctBar([
    {name:"缓存命中 " + fmtMoney(node.cost_cache) + " 元", value:node.cost_cache, cls:"c1"},
    {name:"未命中输入 " + fmtMoney(node.cost_in) + " 元", value:node.cost_in, cls:"c2"},
    {name:"输出 " + fmtMoney(node.cost_out) + " 元", value:node.cost_out, cls:"c3"},
  ], "按贡献的钱：" + fmtMoney(node.cost) + " 元");
}
function visibleRows(root){
  const out = [];
  (function walk(nd, depth){
    for (const ch of nd.children){
      out.push({node: ch, depth});
      if (!S.collapsed.has(ch.path)) walk(ch, depth + 1);
    }
  })(root, 0);
  return out;
}
function nodeColor(depth){
  return C.lvl[Math.min(depth, C.lvl.length - 1)];
}

// ---------------- 图表
function donut(parts, centerTitle, centerValue, size){
  const R = 52, SW = 20, CIRC = 2*Math.PI*R;
  size = size || 145;
  const total = parts.reduce((s,p) => s + p.value, 0) || 1;
  let off = 0, segs = "";
  for (const p of parts){
    const len = p.value / total * CIRC;
    segs += `<circle r="${R}" cx="72" cy="72" fill="none" stroke="${p.color}" stroke-width="${SW}"
             stroke-dasharray="${len} ${CIRC - len}" stroke-dashoffset="${-off}"
             transform="rotate(-90 72 72)"></circle>`;
    off += len;
  }
  const lg = parts.map(p => `<div><em style="display:inline-block;width:9px;height:9px;
      background:${p.color};margin-right:5px"></em>${p.name}
      <b>${fmtMoney(p.value)}</b> 元 · ${(p.value/total*100).toFixed(1)}%</div>`).join("");
  return `<svg width="${size}" height="${size}" viewBox="0 0 144 144">
      <circle r="${R}" cx="72" cy="72" fill="none" class="track" stroke-width="${SW}"></circle>
      ${segs}
      <text x="72" y="68" text-anchor="middle" font-size="10" class="lbl">${centerTitle}</text>
      <text x="72" y="84" text-anchor="middle" font-size="13" font-weight="700">${centerValue}</text>
    </svg><div style="font-size:11.5px;line-height:1.8">${lg}</div>`;
}
function fmtTs(ts){ const d = new Date(ts); return d.getFullYear()+"-"+String(d.getMonth()+1).padStart(2,"0")
    +"-"+String(d.getDate()).padStart(2,"0")+" "+String(d.getHours()).padStart(2,"0")+":"+String(d.getMinutes()).padStart(2,"0"); }
function fmtShort(ts){ const d = new Date(ts); return String(d.getMonth()+1).padStart(2,"0")+"-"
    +String(d.getDate()).padStart(2,"0")+" "+String(d.getHours()).padStart(2,"0")+":"+String(d.getMinutes()).padStart(2,"0"); }
// 起止时间：起点=该节点里最早那条消息的"用户提问时间"，终点=最晚一条 API 回复时间
function spanText(node){
  if (!node) return "-";
  const items = node.items || [];
  if (!items.length) return "-";
  let s = Infinity, e = -Infinity;
  for (const m of items){
    const a = m.start_ts || m.ts;
    if (a && a < s) s = a;
    if (m.ts && m.ts > e) e = m.ts;
  }
  if (!isFinite(s) || !isFinite(e)) return "-";
  return fmtShort(s) + " → " + fmtShort(e);
}

function dailyBars(){
  const days = DATA.daily.filter(d => d.day >= S.from && d.day <= S.to);
  if (!days.length) return "<div class='sub'>区间内没有数据</div>";
  const W = Math.max(520, days.length * 62), H = 185, pad = 30;
  const max = Math.max(...days.map(d => Math.max(d.cost, bookOf(d.day) || 0))) || 1;
  const bw = Math.min(34, (W - pad*2) / days.length * 0.55);
  let out = `<svg width="100%" viewBox="0 0 ${W} ${H}" style="max-width:100%">`;
  out += `<line x1="${pad}" y1="${H-pad}" x2="${W-6}" y2="${H-pad}" class="axis"></line>`;
  days.forEach((d,i) => {
    const x = pad + (i + 0.5) * ((W - pad*2) / days.length);
    const h = (d.cost / max) * (H - pad*2);
    out += `<rect x="${x-bw/2}" y="${H-pad-h}" width="${bw}" height="${h}" fill="${C.cache}"
             opacity="0.75"></rect>`;
    const bk = bookOf(d.day);
    if (bk != null){
      const y = H - pad - (bk / max) * (H - pad*2);
      out += `<path d="M${x-6},${y+5} L${x},${y-5} L${x+6},${y+5} Z" fill="${C.book}"></path>`;
    }
    const pc = d.cost > 0 ? (d.peak_cost/d.cost*100).toFixed(0) : "0";
    out += `<text x="${x}" y="${H-pad-h-5}" text-anchor="middle" font-size="10">${fmtMoney(d.cost)}</text>`;
    out += `<text x="${x}" y="${H-pad+13}" text-anchor="middle" font-size="10" class="lbl">${d.day.slice(5)}</text>`;
    out += `<text x="${x}" y="${H-pad+25}" text-anchor="middle" font-size="9" class="lbl">peak ${pc}%</text>`;
  });
  return out + "</svg>";
}
function bookOf(day){
  const r = DATA.recon.find(x => x.day === day);
  return r && r.book_cost != null ? r.book_cost : null;
}
function sideTokens(day){ return DATA.side_calls.filter(s => s.day === day).reduce((a,s) => a + s.tokens, 0); }

// 选中节点的时间线（按小时聚合；跨天则按天）
function timeline(node){
  const items = node ? node.items : currentRows();
  if (!items.length) return "<div class='sub'>无数据</div>";
  const byDay = new Set(items.map(m => m.day)).size > 1;
  const m = new Map();
  for (const x of items){
    const k = byDay ? x.day : String(x.hour).padStart(2,"0") + "时";
    const o = m.get(k) || {k, cost:0, tokens:0, calls:0};
    o.cost += (x.cost_est < 0 ? 0 : x.cost_est); o.tokens += x.total_tokens; o.calls += 1;
    m.set(k, o);
  }
  const arr = [...m.values()].sort((a,b) => (a.k > b.k ? 1 : a.k < b.k ? -1 : 0));
  const key = S.metric === "tokens" ? "tokens" : (S.metric === "calls" ? "calls" : "cost");
  const W = Math.max(330, arr.length * 34), H = 165, pad = 26;
  const max = Math.max(...arr.map(d => d[key])) || 1;
  const bw = Math.min(26, (W - pad*2) / arr.length * 0.6);
  let out = `<svg width="100%" viewBox="0 0 ${W} ${H}" style="max-width:100%">`;
  out += `<line x1="${pad}" y1="${H-pad}" x2="${W-4}" y2="${H-pad}" class="axis"></line>`;
  arr.forEach((d,i) => {
    const v = d[key];
    const x = pad + (i + 0.5) * ((W - pad*2) / arr.length);
    const h = (v / max) * (H - pad*2);
    out += `<rect x="${x-bw/2}" y="${H-pad-h}" width="${bw}" height="${h}"
             fill="${C.cache}" opacity="0.85"></rect>`;
    if (bw > 12){
      out += `<text x="${x}" y="${H-pad-h-4}" text-anchor="middle" font-size="9"
               class="val">${key === "cost" ? fmtMoney(v) : fmtInt(v)}</text>`;
    }
    out += `<text x="${x}" y="${H-pad+12}" text-anchor="middle" font-size="9"
             class="lbl">${esc(d.k)}</text>`;
  });
  return out + "</svg>";
}

// ---------------- treemap：只画叶子（面积=指标），父级用留边+标题条表示层次
function treemap(root, W, H){
  const out = [], heads = [];
  const val = n => Math.max(metricOf(n), 1e-9);
  const TITLE = 13;                       // 有子节点时，顶部留一条标题带
  function fill(nodes, x, y, w, h, depth){
    if (!nodes.length || w <= 1 || h <= 1) return;
    const total = nodes.reduce((s,n) => s + val(n), 0);
    if (total <= 0) return;
    // 先按"最长边二分"把父级铺开
    let i = 0, acc = 0;
    if (nodes.length > 1){
      const horizontal = w >= h;
      for (; i < nodes.length - 1; i++){ acc += val(nodes[i]); if (acc >= total / 2) break; }
      i = Math.min(Math.max(i, 1), nodes.length - 1);
      const g1 = nodes.slice(0, i), g2 = nodes.slice(i);
      const t1 = g1.reduce((s,n) => s + val(n), 0);
      const frac = total > 0 ? t1 / total : 0.5;
      if (horizontal){
        const w1 = w * frac;
        fill(g1, x, y, w1, h, depth); fill(g2, x + w1, y, w - w1, h, depth);
      } else {
        const h1 = h * frac;
        fill(g1, x, y, w, h1, depth); fill(g2, x, y + h1, w, h - h1, depth);
      }
      return;
    }
    // 只剩一个节点：有子节点就留标题带再往里铺，否则就是叶子
    const n = nodes[0];
    if (n.children && n.children.length && depth < 3){
      const band = Math.min(TITLE, Math.max(0, h - 4));
      if (w > 46 && band > 8){
        heads.push({x, y, w, h: band, node: n, depth});
        fill(n.children, x + 1, y + band, Math.max(0, w - 2), Math.max(0, h - band - 1), depth + 1);
      } else {
        fill(n.children, x + 1, y + 1, Math.max(0, w - 2), Math.max(0, h - 2), depth + 1);
      }
      return;
    }
    out.push({x, y, w, h, node: n, depth});
  }
  fill(root.children, 0, 0, W, H, 0);
  return {cells: out, heads};
}
function metricOf(n){
  switch (S.metric){
    case "tokens": return n.tokens;
    case "calls": return n.calls;
    case "cache": return n.cache;
    case "in": return n.in;
    case "out": return n.out;
    default: return n.cost;
  }
}
function renderMap(root){
  const W = 1000, H = 470;
  const {cells, heads} = treemap(root, W, H);
  let svg = `<svg width="100%" viewBox="0 0 ${W} ${H}" style="max-width:100%">`;
  svg += `<rect x="0" y="0" width="${W}" height="${H}" class="tilebg"></rect>`;
  const many = cells.length > 200;
  for (const c of cells){
    const col = nodeColor(c.depth);
    svg += `<rect class="tm" data-path="${esc(c.node.path||"")}" x="${c.x+0.5}" y="${c.y+0.5}"
             width="${Math.max(0,c.w-1)}" height="${Math.max(0,c.h-1)}" fill="${col}"
             fill-opacity="${c.depth === 0 ? 0.55 : (c.depth === 1 ? 0.75 : 0.92)}"
             stroke="#fff" stroke-width="0.8"></rect>`;
    const lbl = String(prettyDim(c.node.dim, c.node.key, nodeCtx(c.node)));
    if (c.w > 74 && c.h > 26 && (!many || c.w > 120)){
      const cut = Math.max(4, Math.floor(c.w / 7));
      svg += `<text x="${c.x+4}" y="${c.y+12}" font-size="9.5" fill="#fff"
              style="paint-order:stroke;stroke:rgba(0,0,0,.4);stroke-width:2.4px"
              pointer-events="none">${esc(lbl.slice(0, cut))}</text>`;
      svg += `<text x="${c.x+4}" y="${c.y+24}" font-size="9" fill="#fff" fill-opacity="0.95"
              style="paint-order:stroke;stroke:rgba(0,0,0,.35);stroke-width:2.2px"
              pointer-events="none">${fmtMoney(c.node.cost)}</text>`;
    }
  }
  for (const hd of heads){
    const col = nodeColor(hd.depth);
    svg += `<rect x="${hd.x}" y="${hd.y}" width="${hd.w}" height="${hd.h}"
             fill="${col}" fill-opacity="0.95" class="tm" data-path="${esc(hd.node.path||"")}"></rect>`;
    if (hd.w > 40){
      const cut = Math.max(4, Math.floor(hd.w / 7.5));
      svg += `<text x="${hd.x+4}" y="${hd.y+10.5}" font-size="10" fill="#fff" font-weight="600"
              pointer-events="none">${esc(String(prettyDim(hd.node.dim, hd.node.key, nodeCtx(hd.node))).slice(0, cut))}</text>`;
    }
  }
  svg += "</svg>";
  $("mapBox").innerHTML = svg;
  $("mapLegend").innerHTML = "颜色深浅=层级：" + C.lvl.map((c,i) =>
      `<span><em style="display:inline-block;width:9px;height:9px;border-radius:2px;background:${c};margin-right:4px"></em>第${i+1}级</span>`
      ).join("") + `<span class="sub">面积 = ${S.metric}</span>`;
  $("mapBox").querySelectorAll("rect.tm").forEach(r => {
    r.onclick = () => { const pth = r.getAttribute("data-path"); if (pth) selectNodePath(pth); };
  });
}

// ---------------- sunburst
function renderSun(root){
  const W = 620, H = 620, cx = W/2, cy = H/2;
  const rings = [[0, 90], [90, 150], [150, 210], [210, 270]];
  let svg = `<svg width="100%" viewBox="0 0 ${W} ${H}" style="max-width:620px">`;
  function arc(cx, cy, r0, r1, a0, a1){
    const p = (r,a) => [cx + r*Math.cos(a), cy + r*Math.sin(a)];
    const big = (a1 - a0) > Math.PI ? 1 : 0;
    const [x0,y0] = p(r1,a0), [x1,y1] = p(r1,a1), [x2,y2] = p(r0,a1), [x3,y3] = p(r0,a0);
    return `M${x0},${y0} A${r1},${r1} 0 ${big} 1 ${x1},${y1} L${x2},${y2} A${r0},${r0} 0 ${big} 0 ${x3},${y3} Z`;
  }
  function draw(nodes, depth, a0, a1){
    if (depth >= rings.length) return;
    const total = nodes.reduce((s,n) => s + Math.max(metricOf(n),1e-9), 0) || 1;
    let a = a0;
    for (const n of nodes){
      const span = (a1 - a0) * Math.max(metricOf(n),1e-9) / total;
      const [r0, r1] = rings[depth];
      const col = nodeColor(depth);
      svg += `<path class="sb" data-path="${esc(n.path||"")}" d="${arc(cx,cy,r0,r1,a,a+span)}"
               fill="${col}" fill-opacity="${Math.max(0.3, 0.8 - depth*0.18)}"
               stroke="#fff" stroke-width="0.7"></path>`;
      const mid = (a + a + span) / 2, rr = (r0 + r1) / 2;
      if (span > 0.14 && (r1 - r0) >= 50){          // 标签只放在够宽的环上，免得挤成一团
        svg += `<text x="${cx + rr*Math.cos(mid)}" y="${cy + rr*Math.sin(mid)}"
                 font-size="9" fill="#fff" text-anchor="middle" pointer-events="none"
                 transform="rotate(${(mid*180/Math.PI+90).toFixed(1)} ${cx + rr*Math.cos(mid)} ${cy + rr*Math.sin(mid)})"
                 >${esc(prettyDim(n.dim, n.key, nodeCtx(n)).slice(0,10))}</text>`;
      }
      if (n.children && n.children.length) draw(n.children, depth + 1, a, a + span);
      a += span;
    }
  }
  draw(root.children, 0, 0, Math.PI * 2);
  svg += `<text x="${cx}" y="${cy-4}" text-anchor="middle" font-size="11" class="lbl">合计</text>
          <text x="${cx}" y="${cy+14}" text-anchor="middle" font-size="14" font-weight="700">${fmtMoney(root.cost)}</text>`;
  svg += "</svg>";
  $("sunBox").innerHTML = svg;
  $("sunBox").querySelectorAll("path.sb").forEach(p => {
    p.onclick = () => { const pth = p.getAttribute("data-path"); if (pth) selectNodePath(pth); };
  });
}

// ---------------- 主渲染
function nodeChain(nd){
  const out = [];
  for (let c = nd; c && c.dim; c = c.parent) out.unshift(prettyDim(c.dim, c.key, nodeCtx(c)));
  return out;
}
function selectNodePath(path){
  S.selected = path;
  render();
}
// 工具调用：只显示次数，明细（哪个工具×几次）放 title；叶子行才有明细
function toolCell(node){
  if (!node.tools) return '<td class="toolcell none">—</td>';
  const leaf = (node.leafItems && node.leafItems.length) ? node.leafItems[0] : null;
  const detail = leaf && leaf.tnm ? leaf.tnm : "";
  const tip = "本行" + (leaf ? "" : "（含下级）") + "共 " + node.tools + " 次工具调用"
            + (detail ? "：" + detail : (leaf ? "" : "（明细看叶子行）"))
            + "。工具本身不花钱，但工具返回的内容会进下一次调用的输入";
  return '<td class="toolcell" title="' + esc(tip) + '">' + node.tools + '</td>';
}
function render(){
  const dims = activeDims();
  const root = getTree();
  S.tree = root;
  const rows = currentRows();
  const sel = S.selected ? findNode(root, S.selected) : null;
  if (S.selected && !sel) S.selected = null;

  // 头部
  $("head").innerHTML = `区间 <b>${S.from} ${S.fromTime} ~ ${S.to} ${S.toTime}</b> · 生成于 ${new Date(DATA.generated_at*1000).toLocaleString()}`
    + ` · 上次扫描 ${DATA.last_scan ? new Date(DATA.last_scan*1000).toLocaleString() : "-"}`
    + (DATA.balance != null ? ` · 余额 <b>${DATA.balance}</b> 元（${DATA.balance_date}）` : "");
  const tot = {cost:0,tokens:0,in:0,out:0,cache:0,calls:0,peak_cost:0,unknown:0,
               search:0,searchCost:0};
  for (const m of rows){
    const est = (S.withAnomaly && m.interrupted) ? 1 : 0;
    const unk = (m.cost_est < 0) ? 1 : 0;      // 未知价目：钱不计
    tot.cost += (unk ? 0 : (m.cost_est || 0)) + (est ? (m.est_cost || 0) : 0);
    tot.tokens += (m.total_tokens || 0) + (est ? (m.est_tokens || 0) : 0);
    tot.in += m.in_tokens; tot.out += m.out_tokens; tot.cache += m.cache_read;
    tot.unknown += unk;
    // 原生搜索（旁路账）：默认**不区分来源**，已经并进上面所有数字；这里只是顺手记个数给 KPI
    if (m.kind === "search"){ tot.search += 1; if ((m.cost_est || 0) >= 0) tot.searchCost += m.cost_est; }
    tot.calls += 1; if (m.peak && !unk) tot.peak_cost += m.cost_est; }
  const kpiRows = [
    ["花费（折算）", fmtMoney(tot.cost) + " 元"],
    ["总 token", fmtInt(tot.tokens)],
    ["调用次数", fmtInt(tot.calls)],
    ["缓存命中占比", pct(tot.cache, tot.tokens).toFixed(1) + "%"],
    ["峰值时段花费", fmtMoney(tot.peak_cost) + " 元（" + pct(tot.peak_cost, tot.cost).toFixed(1) + "%）"],
    ["天数 / 会话", new Set(rows.map(m=>m.day)).size + " 天 / " + new Set(rows.map(m=>m.session_id)).size + " 会话"],
  ];
  if (tot.unknown){
    kpiRows.push(["未知价目", tot.unknown + " 次调用 · 未计价（补齐 prices.txt 即可计入）"]);
  }
  if (tot.search){
    kpiRows.push(["其中原生搜索", tot.search + " 次 · " + fmtMoney(tot.searchCost)
      + " 元（已计入上面的合计，不单列）"]);
  }
  const AN = DATA.anomaly || {n:0, cost:0, tokens:0, unmatched:0};
  const OFF = DATA.official || {has:false};
  if (OFF.has){
    kpiRows.push(["官方实付（" + esc(OFF.key) + "）", fmtMoney(OFF.cost) + " 元"
      + (OFF.unmatched ? "（另有 " + fmtMoney(OFF.unmatched) + " 元对不上中断）" : "")]);
  }
  if (AN.n){
    kpiRows.push(["中断空壳", AN.n + " 条 · 已恢复 " + fmtMoney(AN.cost) + " 元"
      + (S.withAnomaly ? "（已计入）" : "（未计入）")]);
  }
  $("kpi").innerHTML = kpiRows.map(([k,v]) => `<div><span>${k}</span><b>${v}</b></div>`).join("");
  $("filterInfo").textContent = `筛后 ${fmtInt(rows.length)} / ${fmtInt(DATA.msgs.length)} 次调用`
    + (S.workspaces ? `，工作区限于 ${S.workspaces.size} 个` : "")
    + (AN.n ? `，中断空壳 ${AN.n} 条（已恢复 ${AN.n - (AN.unrecovered||0)} 条）` : "")
    + (tot.unknown ? `，未知价目 ${tot.unknown} 次（钱未计，补 prices.txt 即可）` : "")
    + (AN.unmatched ? `；另有 ${fmtMoney(AN.unmatched)} 元官方开销对不上任何中断（未分摊）` : "");
  $("crumb").textContent = dims.length ? "层级：" + dims.map(d => DIM_LABEL[d]).join(" › ") : "未选层级";

  // 树表（分页渲染，见 paintRows）
  const vis = visibleRows(root);
  S.vis = vis; S.visTot = tot; S.visSel = sel;
function rowHtml(node, depth, tot, sel){
    const hasKids = node.children.length > 0;
    const collapsed = S.collapsed.has(node.path);
    const ind = "&nbsp;".repeat(depth * 4);
    const tw = hasKids ? `<span class="tw">${collapsed ? "▶" : "▼"}</span> ` : `<span class="tw">·</span> `;
    const isSel = sel && sel.path === node.path;
    const ctx = nodeCtx(node);
    const lbl = prettyDim(node.dim, node.key, ctx);
    const tip = cellTitle(node.dim, node.key, ctx);
    // 标红判定（纯显示层：只决定 class，绝不参与任何计算）
    // 「最下层」= 当前屏幕上最深的那一层：它没有下级，或下级被折叠起来了（折叠后它就成了最下层）
    const isBottom = (node.children.length === 0) || collapsed;
    const hotEligible = !S.hotLeafOnly || isBottom;
    const peakPct = node.cost ? pct(node.peak_cost, node.cost) : 0;
    const hotCost = hotEligible && S.hotCost > 0 && node.cost > S.hotCost;
    const hotPeak = hotEligible && S.hotPeak > 0 && peakPct > S.hotPeak;
    const hotCls = (hotCost || hotPeak) ? " hot" : "";
    // 中断空壳：黄标 + ⚠（走琥珀色这条通道，和标红的红色互不干扰）
    const anom = (node.anomaly > 0) || (node.anomaly_copy > 0);
    const anomCls = anom ? " anom" : "";
    const anomTip = anom
      ? "；含 " + (node.anomaly || 0) + " 次中断" + (node.anomaly_copy
            ? "（另有 " + node.anomaly_copy + " 条 fork 副本，不重复计算）" : "")
        + (node.est_cost
          ? "（已恢复 " + fmtMoney(node.est_cost) + " 元"
            + (node.est_note ? " · " + node.est_note : "")
            + " · " + (S.withAnomaly ? "已计入" : "未计入") + "）"
          : (node.anomaly ? "（未恢复：那一小时对不上官方差额）" : ""))
      : "";
    const anomMark = anom ? `<span class="anom-mark" title="含中断空壳">⚠</span> ` : "";
    // 未知价目：钱没算进去（只在金额后面点一个蓝色问号，不改任何数字）
    const unkMark = node.unknown
      ? `<span class="unk-mark" title="含 ${node.unknown} 次未知价目：金额未计入（在 prices.txt 里补上对应档位即可）">?</span>`
      : "";
    // 重建参考（纯展示）：没嫌疑画横杠；有嫌疑写"估算多花的钱"。
    // 三档：严判 = 红字；只过宽判 = 黄字；都没过 = 横杠。只给这一格的字上色、不动整行。
    const rbTip = node.rb > 0
      ? "疑似缓存重建" + (node.rbLvl === 2 ? "（严判）" : "（宽判）")
        + (node.rbN > 1 ? " · 本行含 " + node.rbN + " 次" : "")
        + "：那一轮的第一条调用整段上下文没命中缓存，估算因此多花 " + fmtMoney(node.rb)
        + " 元（钱已含在花费里，只是拆出来看；判据见 MAINTAIN §2.10）"
      : "";
    const rbCell = `<td class="rbcell${node.rbLvl === 2 ? " strict" : (node.rb > 0 ? " loose" : " none")}"`
      + (rbTip ? ` title="${esc(rbTip)}"` : "") + ">"
      + (node.rb > 0 ? fmtMoney(node.rb) : "—") + "</td>";
    // 搜索开销（纯展示）：这一行（含下级）里**原生搜索**花了多少。
    // 固定费 ¥0.005/次 实测对不上账（见 MAINTAIN §2.9），所以只显示 token 那部分。
    const srchTip = node.srchN
      ? "原生搜索 " + node.srchN + " 次：token 开销 " + fmtMoney(node.srch)
        + " 元（已含在「花费」里，只是拆出来看）。"
        + "另有传闻每次 ¥0.005 的固定费 —— 实测对不上账（19 次搜索那小时残差仅 0.0000），按 0 计"
      : "";
    const srchCell = `<td class="rbcell srccell${node.srchN ? "" : " none"}"`
      + (srchTip ? ` title="${esc(srchTip)}"` : "") + ">"
      + (node.srchN ? fmtMoney(node.srch) : "—") + "</td>";
    return `<tr data-path="${esc(node.path)}" data-depth="${depth}" data-dim="${esc(node.dim||"")}"
      data-key="${esc(node.key)}" data-cost="${node.cost.toFixed(6)}" data-tokens="${node.tokens}"
      data-calls="${node.calls}" class="d${depth} lvl${Math.min(depth,3)} ${isSel?"sel":""}${hotCls}${anomCls}"
      data-anom="${node.anomaly}" data-rb="${node.rbLvl}"
      ${(tip || anom) ? `title="${esc(tip + anomTip)}"` : ""}>
      <td><span class="lblwrap" title="${esc(tip || lbl)}">${ind}${tw}${anomMark}${esc(lbl)}</span></td>
      <td class="${hotCost?"hotcost":""}">${fmtMoney(node.cost)}${unkMark}</td>
      <td>${pct(node.cost, node.parent ? node.parent.cost : node.cost).toFixed(1)}%</td>
      <td>${pct(node.cost, tot.cost).toFixed(1)}%</td>
      <td>${fmtInt(node.tokens)}</td>
      <td>${tokenBar(node)}</td>
      <td>${costBar(node)}</td>
      <td class="${hotPeak?"hotpeak":""}">${node.cost ? pct(node.peak_cost, node.cost).toFixed(0)+"%" : "-"}</td>
      <td>${fmtInt(node.calls)}</td>
      ${toolCell(node)}
      <td class="sub"><span class="lblwrap twrap" title="${esc(spanText(node))}">${esc(spanText(node))}</span></td>
      ${srchCell}
      ${rbCell}
      ${rawCols(node)}</tr>`;
}

// 分页渲染：一次只画 PAGE 行（DOM 行数是卡顿的根源），滚到底再追加
const PAGE = 400;
function paintRows(keep){
  const box = $("treeRows"), vis = S.vis || [], tot = S.visTot, sel = S.visSel;
  const to = Math.min(vis.length, Math.min(4000, Math.max(PAGE, keep || 0)));
  let html = "";
  for (let i = 0; i < to; i++){ const o = vis[i]; html += rowHtml(o.node, o.depth, tot, sel); }
  html += (to >= vis.length)
    ? '<tr class="more"><td colspan="16">共 ' + fmtInt(vis.length) + ' 行（已全部显示）</td></tr>'
    : '<tr class="more" id="moreRow"><td colspan="16">已显示 ' + fmtInt(to) + ' / '
      + fmtInt(vis.length) + ' 行 · 继续向下滚动加载（或按 End）</td></tr>';
  box.innerHTML = html;
  S.shown = to;
}
function moreRows(){
  if (!S.vis || S.shown >= S.vis.length) return;
  const el = document.getElementById("moreRow");
  if (el && el.getBoundingClientRect().top < window.innerHeight + 400) paintRows(S.shown + PAGE);
}
window.addEventListener("scroll", () => {
  if (S.moreTick) return;
  S.moreTick = requestAnimationFrame(() => { S.moreTick = 0; moreRows(); });
}, {passive:true});

  paintRows(S.shown || 0);
  $("treeTot").innerHTML = `<td>合计（${vis.length} 行 / 共 ${nodesOf(root).length} 节点）</td>
    <td>${fmtMoney(tot.cost)}</td><td>100%</td><td>100%</td>
    <td>${fmtInt(tot.tokens)}</td>
    <td>${tokenBar(root)}</td><td>${costBar(root)}</td>
    <td>${pct(tot.peak_cost, tot.cost).toFixed(0)}%</td>
    <td>${fmtInt(tot.calls)}</td><td class="toolcell">${root.tools || "—"}</td><td class="sub">${esc(spanText(root))}</td>
    <td class="rbcell srccell${root.srchN ? "" : " none"}"
      title="本筛选项下原生搜索的 token 开销之和（已含在总花费里）">${root.srchN ? fmtMoney(root.srch) : "—"}</td>
    <td class="rbcell${root.rbLvl === 2 ? " strict" : (root.rb > 0 ? " loose" : " none")}"
      title="本筛选项下所有疑似缓存重建的估算多花金额之和">${root.rb > 0 ? fmtMoney(root.rb) : "—"}</td>
    ${rawCols(tot)}`;
  // 事件委托：只挂一个监听（上万行时逐行 onclick 本身就慢）
  $("treeRows").onclick = (e) => {
    const tr = e.target.closest ? e.target.closest("tr[data-path]") : null;
    if (!tr) return;
    const path = tr.getAttribute("data-path");
    if (e.target.classList.contains("tw")){          // 点三角：只折叠/展开，不选中
      if (S.collapsed.has(path)) S.collapsed.delete(path); else S.collapsed.add(path);
      S.shown = 0;                                    // 折叠变化后从头上计 400 行
      render(); return;
    }
    S.selected = path; render();
  };

  // 视图切换
  $("viewTree").classList.toggle("hidden", S.view !== "tree");
  $("viewMap").classList.toggle("hidden", S.view !== "map");
  $("viewSun").classList.toggle("hidden", S.view !== "sun");
  $("tabTree").classList.toggle("on", S.view === "tree");
  $("tabMap").classList.toggle("on", S.view === "map");
  $("tabSun").classList.toggle("on", S.view === "sun");
  // 「显示具体 token 数」：**必须在这儿应用**（render 是所有改状态的入口）——
  // 之前只在 refreshControls() 里切这个类，而点复选框走的是 render()，于是点了没反应。
  const tbl = document.querySelector("table.t");
  if (tbl) tbl.classList.toggle("rawcol-off", !S.showRaw);
  // 深色模式：同样必须落在 render() 里（它是所有改状态的入口）
  document.body.classList.toggle("dark", !!S.dark);
  if (tbl) tbl.classList.toggle("rbcol-off", !S.showRb);
  if (S.view === "map") renderMap(root);
  if (S.view === "sun") renderSun(root);

  // 选中节点面板
  const node = sel || root;
  const title = sel ? nodeChain(sel).join(" › ") : "全部（未选中，显示总体）";
  $("panelTitle").textContent = "选中节点明细：" + title;
  $("panelHint").textContent = sel
    ? `${node.calls} 次调用 · ${fmtInt(node.tokens)} token · ${fmtInt(node.children.length)} 个子节点`
      + ` · 时间 ${isFinite(node.minTs) ? fmtTs(node.minTs) : "-"} ~ ${isFinite(node.maxTs) ? fmtTs(node.maxTs) : "-"}`
    : "点表格行 / treemap 小格 / 旭日扇区可选中节点（再点三角可折叠）";
  $("donutCost").innerHTML = donut([
    {name:"缓存命中", value:node.cost_cache, color:C.cache},
    {name:"未命中输入", value:node.cost_in, color:C.in},
    {name:"输出", value:node.cost_out, color:C.out}], "折算合计", fmtMoney(node.cost));
  $("donutToken").innerHTML = donut([
    {name:"缓存命中", value:node.cache, color:C.cache},
    {name:"未命中输入", value:node.in, color:C.in},
    {name:"输出", value:node.out, color:C.out}], "token 合计", fmtInt(node.tokens));
  $("tlTitle").textContent = "时间分布（" + (new Set((node.items||[]).map(m=>m.day)).size > 1 ? "按天" : "按小时") + "）";
  $("timeline").innerHTML = timeline(node);

  $("dailyChart").innerHTML = dailyBars();
  $("recon").innerHTML = DATA.recon.filter(r => r.day >= S.from && r.day <= S.to).map(r =>
    `<tr><td>${r.day}</td>
      <td>${r.official != null ? '<b>' + r.official.toFixed(3) + '</b>' : "-"}</td>
      <td>${r.book_cost != null ? r.book_cost.toFixed(2) : "-"}</td>
      <td>${r.events_cost != null ? r.events_cost.toFixed(3) : "-"}</td>
      <td>${r.ledger_cost != null ? r.ledger_cost.toFixed(3) : "-"}</td>
      <td>${r.scan_cost != null ? r.scan_cost.toFixed(3) : "-"}</td>
      <td>${(r.official && r.scan_cost) ? (r.scan_cost / r.official * 100).toFixed(1) + "%"
            : "-"}</td>
      <td>${sideTokens(r.day) ? fmtInt(sideTokens(r.day)) : "-"}</td>
      <td>${esc(r.note || "")}</td></tr>`).join("");

  saveState();          // 每次重画都顺手记一下（纯 UI 状态，与数字无关）
}

// ---------------- 记住上次的状态 / 预设 0
function _numOr(v, def){
  const n = typeof v === "number" ? v : parseFloat(v);
  return isFinite(n) ? n : def;
}
function validDims(ds){
  const out = [];
  for (const d of (Array.isArray(ds) ? ds : [])){
    if (DIMS.indexOf(d) >= 0 && out.indexOf(d) < 0) out.push(d);
  }
  while (out.length < 3) out.push("none");
  return out.slice(0, 3);
}
function snapshot(){
  const o = {};
  for (const k of PERSIST_KEYS){
    o[k] = (k === "workspaces") ? (S.workspaces ? Array.from(S.workspaces) : null) : S[k];
  }
  return o;
}
// 把一份状态按"逐项校验"套回去：坏值/旧版本留下的键直接丢掉，绝不让脏数据把页面搞崩
function applyState(o){
  if (!o || typeof o !== "object") return false;
  if (o.dims){
    const d = validDims(o.dims);
    if (d.join("\u0001") !== S.dims.join("\u0001")){ S.selected = null; S.collapsed = new Set(); }
    S.dims = d;
  }
  if (Array.isArray(o.sortOf)){
    const so = S.sortOf.slice();
    for (let i = 0; i < 3; i++) if (SORT_MODES.indexOf(o.sortOf[i]) >= 0) so[i] = o.sortOf[i];
    S.sortOf = so;
  }
  for (const k of ["hotCost", "hotPeak"]) if (k in o) S[k] = Math.max(0, _numOr(o[k], S[k]));
  for (const k of ["hotLeafOnly", "mainOnly", "withReplay", "withAnomaly", "showRaw", "dark", "showRb"]) if (k in o) S[k] = !!o[k];
  if (typeof o.view === "string" && ["tree", "map", "sun"].indexOf(o.view) >= 0) S.view = o.view;
  if ("workspaces" in o){
    S.workspaces = Array.isArray(o.workspaces) ? new Set(o.workspaces) : null;
  }
  return true;
}
function saveState(){
  const j = JSON.stringify(snapshot());
  let how = "";
  try {
    if (window.localStorage){ window.localStorage.setItem(STORE_KEY, j); how = "已记住"; }
  } catch (e) { /* file:// 被禁等 → 往下走 hash 兜底 */ }
  if (!how){
    try {
      // 兜底：写进地址栏（用 replaceState 免得刷一堆历史记录）
      if (typeof history !== "undefined" && history.replaceState){
        history.replaceState(null, "", "#state=" + encodeURIComponent(j));
        how = "已记住（地址栏）";
      }
    } catch (e) { /* 两条路都不通就当没记住，功能照常 */ }
  }
  const el = $("persistNote");
  if (el) el.textContent = "（" + (how || "本浏览器不让记住，本次仍生效")
    + (PRESET_WARN ? " · " + PRESET_WARN : "") + "）";
}
function loadState(){
  try {
    if (window.localStorage){
      const s = window.localStorage.getItem(STORE_KEY);
      if (s) return JSON.parse(s);
    }
  } catch (e) { /* 忽略 */ }
  try {
    const m = (typeof location !== "undefined" && location.hash || "").match(/state=([^&;]+)/);
    if (m) return JSON.parse(decodeURIComponent(m[1]));
  } catch (e) { /* 忽略 */ }
  return null;
}
// 把状态推回各控件（改状态后必须调：否则"值变了但输入框还显示旧数字"）
function refreshControls(){
  fillDims();
  fillSorts();
  fillWorkspaces();
  $("from").value = S.from;
  $("to").value = S.to;
  $("fromTime").value = S.fromTime;
  $("toTime").value = S.toTime;
  $("hotCost").value = S.hotCost;
  $("hotPeak").value = S.hotPeak;
  $("hotLeafOnly").checked = !!S.hotLeafOnly;
  $("mainOnly").checked = !!S.mainOnly;
  $("withReplay").checked = !!S.withReplay;
  $("withAnomaly").checked = !!S.withAnomaly;
  $("showRaw").checked = !!S.showRaw;
  $("showRb").checked = !!S.showRb;
  $("dark").checked = !!S.dark;
  document.querySelector("table.t").classList.toggle("rawcol-off", !S.showRaw);
}
function fillPresets(){
  const sel = $("preset");
  if (!sel) return;
  sel.innerHTML = '<option value="">选择预设…</option>' + PRESETS.map(p =>
    `<option value="${p.id}">${p.id}. ${esc(p.name)}</option>`).join("");
  sel.value = "";
  sel.onchange = e => {
    const id = parseInt(e.target.value, 10);
    e.target.value = "";                    // 纯动作菜单：选完弹回占位符
    if (isNaN(id)) return;
    const p = PRESETS.filter(x => x.id === id)[0];
    if (p) applyPreset(p);
  };
}
// 套用一档预设：只动它列出的键（0 档＝把"刚打开时"的状态整体套回来，日期同样不动）
function applyPreset(p){
  if (!p) return;
  if (p.snapshot){
    if (P0_SNAPSHOT) applyState(P0_SNAPSHOT);
  } else {
    if (p.dims){
      S.dims = validDims(p.dims);
      S.selected = null; S.collapsed = new Set();
    }
    if (p.range){
      const r = RANGES[p.range];
      if (r){ const d = r(); S.from = d[0]; S.to = d[1]; }
    }
    if (Array.isArray(p.sortOf)){
      // 写一个值＝三层都用它（"排序都选时间降序"这种就不用写三遍）
      const want = p.sortOf.filter(m => SORT_MODES.indexOf(m) >= 0);
      if (want.length){
        const so = S.sortOf.slice();
        for (let i = 0; i < 3; i++){
          const m = want.length === 1 ? want[0] : want[i];
          if (m) so[i] = m;
        }
        S.sortOf = so;
      }
    }
    if ("hotCost" in p) S.hotCost = Math.max(0, _numOr(p.hotCost, S.hotCost));
    if ("hotPeak" in p) S.hotPeak = Math.max(0, _numOr(p.hotPeak, S.hotPeak));
    for (const k of ["hotLeafOnly", "mainOnly", "withReplay"]) if (k in p) S[k] = !!p[k];
  }
  S.selected = null;
  refreshControls();
  render();
}

// ---------------- 控件初始化
function fillDims(){
  for (const [i,id] of ["d0","d1","d2"].entries()){
    const sel = $(id);
    sel.innerHTML = DIMS.map(d => `<option value="${d}">${DIM_LABEL[d]}</option>`).join("")
      + `<option value="none">（不分）</option>`;
    sel.value = S.dims[i] || "none";
    sel.onchange = () => {
      const v = [$("d0").value, $("d1").value, $("d2").value];
      S.dims = v.map((x,j) => v.indexOf(x) === j ? x : "none");
      S.selected = null; S.collapsed = new Set(); fillDims(); render();
    };
  }
  updateSortHint();
}
// 层级下拉也变了 → 排序下拉上的"第N层·维度"要跟着更新
function updateSortHint(){
  fillSortLabels();
}
const SORT_LABEL = {ts: "时间 ↑", ts_desc: "时间 ↓", cost: "花费 ↓",
                    tokens: "token ↓", calls: "调用数 ↓"};
function fillSorts(){
  for (const [i,id] of ["sort0","sort1","sort2"].entries()){
    const sel = $(id);
    sel.innerHTML = SORT_MODES.map(m => `<option value="${m}">${SORT_LABEL[m]}</option>`).join("");
    sel.value = S.sortOf[i] || "ts";
    sel.onchange = () => {
      S.sortOf = [$("sort0").value, $("sort1").value, $("sort2").value];
      render();
    };
  }
  SORTS_READY = true;
  fillSortLabels();
}
// 让下拉上的文字直接写明它管哪一层，避免"改一个看起来全都动了"的困惑
function fillSortLabels(){
  if (!SORTS_READY) return;
  const dims = activeDims();
  for (let i = 0; i < 3; i++){
    const nm = dims[i] ? DIM_LABEL[dims[i]] : null;
    const el = $("sort" + i);
    if (el) el.title = nm ? ("第 " + (i + 1) + " 层（" + nm + "）的排序") : ("第 " + (i + 1) + " 层（未启用）");
    const lab = $("sortLab" + i);
    if (lab) lab.textContent = "第" + (i + 1) + "层" + (nm ? "·" + nm : "");
  }
}
function fillWorkspaces(){
  const list = [...new Set(DATA.msgs.map(m => m.workspace))].sort();
  const box = $("wsChips");
  box.innerHTML = list.map(w => {
    const on = !S.workspaces || S.workspaces.has(w);
    return `<span class="chip ${on?"on":""}" data-ws="${esc(w)}">${esc(prettyDim("workspace", w))}</span>`;
  }).join("");
  box.querySelectorAll(".chip").forEach(c => {
    c.onclick = () => {
      const w = c.getAttribute("data-ws");
      if (!S.workspaces) S.workspaces = new Set(list);
      if (S.workspaces.has(w)) S.workspaces.delete(w); else S.workspaces.add(w);
      if (S.workspaces.size === list.length) S.workspaces = null;
      S.selected = null; fillWorkspaces(); render();
    };
  });
}
function toggleAllWs(all){
  S.workspaces = all ? null : new Set();
  S.selected = null; fillWorkspaces(); render();
}
function latestDay(){
  const days = DATA.daily.map(d => d.day);
  return days[days.length-1] || DATA.range[1];
}
// 改日期/时间都走这儿。keepTimes=true 时**不改**分钟（改日期不该把用户填的分钟冲掉）；
// 点「全部 / 近 7 天 / 最新一天」和套用预设时会给 keepTimes=undefined → 分钟回到 00:00 / 23:59。
function setRange(from, to, keepTimes){
  S.from = from; S.to = to;
  if (!keepTimes){ S.fromTime = "00:00"; S.toTime = "23:59"; }
  $("from").value = from; $("to").value = to;
  $("fromTime").value = S.fromTime; $("toTime").value = S.toTime;
  S.selected = null; render();
}
// 本地日期 → 'YYYY-MM-DD'（**不要**用 toISOString：那是 UTC，UTC+8 会整体前移一天）
function ymd(d){
  const p = n => String(n).padStart(2, "0");
  return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate());
}
function init(){
  $("from").value = DATA.range[0];
  $("to").value = DATA.range[1];
  $("fromTime").value = S.fromTime;
  $("toTime").value = S.toTime;
  $("from").onchange = e => setRange(e.target.value, S.to, true);
  $("to").onchange = e => setRange(S.from, e.target.value, true);
  $("fromTime").onchange = e => { S.fromTime = e.target.value; setRange(S.from, S.to, true); };
  $("toTime").onchange = e => { S.toTime = e.target.value; setRange(S.from, S.to, true); };
  $("btnAll").onclick = () => setRange(DATA.range[0], DATA.range[1]);
  $("btn7").onclick = () => {
    const to = latestDay();
    const t = new Date(to + "T00:00:00"); t.setDate(t.getDate() - 6);
    setRange(ymd(t), to);
  };
  $("btnToday").onclick = () => { const d = latestDay(); setRange(d, d); };
  $("metric").onchange = e => { S.metric = e.target.value; render(); };
  $("mainOnly").onchange = e => { S.mainOnly = e.target.checked; S.selected = null; render(); };
  $("withReplay").onchange = e => { S.withReplay = e.target.checked; S.selected = null; render(); };
  $("withAnomaly").onchange = e => { S.withAnomaly = e.target.checked; S.selected = null; render(); };
  $("kw").oninput = e => { S.kw = e.target.value; S.selected = null; render(); };
  // 标红阈值：改了就重画（只影响颜色，不影响 filterInfo / KPI / 任何数字）
  for (const [id, key] of [["hotCost","hotCost"], ["hotPeak","hotPeak"]]){
    const el = $(id);
    if (!el) continue;
    el.value = S[key];
    el.oninput = () => {
      const v = parseFloat(el.value);
      S[key] = (isFinite(v) && v > 0) ? v : 0;      // 空/0/负数 = 关闭该项标红
      render();
    };
  }
  // 「只标红最下层」：折叠/展开会改变谁是"最下层"，所以跟着 render() 走
  $("hotLeafOnly").onchange = e => { S.hotLeafOnly = e.target.checked; render(); };
  $("showRaw").onchange = e => { S.showRaw = e.target.checked; render(); };
  $("showRb").onchange = e => { S.showRb = e.target.checked; render(); };
  $("dark").onchange = e => { S.dark = e.target.checked; render(); };
  $("wsAll").onclick = () => toggleAllWs(true);
  $("wsNone").onclick = () => toggleAllWs(false);
  $("expAll").onclick = () => { S.collapsed = new Set(); render(); };
  $("expNone").onclick = () => {
    S.collapsed = new Set(nodesOf(getTree()).filter(n => n.children.length).map(n => n.path));
    render();
  };
  $("tabTree").onclick = () => { S.view = "tree"; render(); };
  $("tabMap").onclick = () => { S.view = "map"; render(); };
  $("tabSun").onclick = () => { S.view = "sun"; render(); };
  // 1) 先套上次记住的状态（日期不在其中，永远是 DATA.range[0] ~ 最新一天）
  applyState(loadState());
  // 2) 再用 URL hash 里的显式参数覆盖（#dims,sort,metric,view,range 便于分享/截图）
  parseHash();
  // 3) 事件都挂好、控件同步完，才谈"打开时的状态"——它就是预设 0 的内容
  fillPresets();
  refreshControls();
  P0_SNAPSHOT = snapshot();
  // 会话太多时默认把最细一级折起来，先看到"天 › 工作区"的骨架
  const rows = nodesOf(getTree());
  if (rows.filter(n => n.children.length > 0).length > 260){
    S.collapsed = new Set(rows.filter(n => n.dim === "session").map(n => n.path));
  }
  render();
}
function parseHash(){
  const h = (typeof location !== "undefined" && location.hash) ? location.hash.slice(1) : "";
  if (!h) return;
  const parts = h.split(";");
  for (const p of parts){
    const [k,v] = p.split("=");
    if (!v) continue;
    if (k === "dims"){
      const ds = v.split(",").filter(Boolean).slice(0,3);
      while (ds.length < 3) ds.push("none");
      S.dims = ds;
    } else if (k === "sort"){
      // 每层一个：sort=ts|cost|tokens，逗号分隔（截图与分享用）
      const ms = v.split(",").map(x => x.trim()).filter(x => SORT_MODES.indexOf(x) >= 0);
      for (let i = 0; i < 3; i++) if (ms[i]) S.sortOf[i] = ms[i];
    } else if (k === "metric") S.metric = v;
    else if (k === "view") S.view = v;
    else if (k === "range" && v.includes("..")){
      const [a,b] = v.split("..");
      S.from = a; S.to = b; S.fromTime = "00:00"; S.toTime = "23:59";
    }
  }
}
function boot(){
  try {
    init();
  } catch (e) {
    const box = document.getElementById("head");
    const msg = "页面初始化失败：" + (e && (e.stack || e.message) || e);
    if (box) box.innerHTML = '<span style="color:var(--hot-fg);white-space:pre-wrap">' + esc(msg) + "</span>";
    try { console.error(e); } catch (_) {}
  }
}
window.addEventListener("error", function(ev){
  const box = document.getElementById("head");
  if (box && !box.textContent.trim()){
    box.innerHTML = '<span style="color:var(--hot-fg)">脚本错误：' + esc(String(ev.message)) + "</span>";
  }
});
boot();
