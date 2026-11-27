// 存储。共享盘（组里各服务器挂的同一块盘，数据是 D.disk）：容量条（我们各项目、公共目录、我们的其他目录、别人、剩余）＋近 7 天剩余量；
// 按人的明细在组内服务器的「共享盘」。本地盘（续租、关机、换人清空）：开机以来写的构成（各项目缓存、单格临时、labmon 之外按目录，lwBar）＋ needs= 声明的本地依赖。
const COMMON = new Set(["envs", "datasets", "weights", "archive", "labmon"]);
function sharedParts() {
  const K = D.disk;
  if (!K?.df) return null;
  const dk = {total: K.df.total / GIB, free: K.df.free / GIB, warn_gb: K.warn_gb, eta_days: K.eta, path: K.root};
  const rows = ((K.ours || {}).dirs || []).map(r => ({d: r.u, name: r.name, gb: r.b / GIB, p: r.p}));
  const byP = {};
  rows.filter(r => r.p).forEach(r => (byP[r.p] ||= []).push(r));
  const common = rows.filter(r => !r.p && COMMON.has(r.name)), rest = rows.filter(r => !r.p && !COMMON.has(r.name)).sort((a, b) => b.gb - a.gb);
  const sum = rs => rs.reduce((a, r) => a + r.gb, 0), ours = sum(rows), used = dk.total - dk.free;
  return {dk, byP, common, rest, ours, used, others: rows.length && !K.pending ? Math.max(0, used - ours) : null, sum, pending: K.pending};
}
const dirList = rs => rs.slice(0, 6).map(r => `${r.name} ${g1(r.gb)} GiB`).join("\n") + (rs.length > 6 ? `\n… 共 ${rs.length} 个目录` : "");
function sharedBar(x, thin) {
  const P = gb => 100 * gb / x.dk.total;
  let s = Object.entries(x.byP).map(([p, rs]) => seg(P(x.sum(rs)), color(p), `${p}：${g1(x.sum(rs))} GiB\n${dirList(rs)}`, thin ? "" : p)).join("");
  if (x.common.length) s += seg(P(x.sum(x.common)), "var(--faint)", `公共目录 ${g1(x.sum(x.common))} GiB\n${dirList(x.common)}`, thin ? "" : "公共");
  if (x.rest.length) s += seg(P(x.sum(x.rest)), "var(--muted)", `我们的其他目录 ${g1(x.sum(x.rest))} GiB（没登记队列的项目、旧目录）\n${dirList(x.rest)}`, thin ? "" : "其他目录");
  const oc = "color-mix(in srgb, var(--muted) 45%, var(--soft))";          // 别人的用量淡一些，突出我们自己的
  s += x.others == null ? seg(P(Math.max(0, x.used - x.ours)), oc, `其余已用 ${g1(Math.max(0, x.used - x.ours))} GiB（还有 ${x.pending} 个目录没统计完，暂不区分我们的与别人的）`, "", "hatch")
    : seg(P(x.others), oc, `其他人的 ${g1(x.others)} GiB（总已用 − 我们目录下的）；按人看在组内服务器的「共享盘」`, thin ? "" : "其他人", "hatch");
  return `<div class="bar ${thin ? "thin" : "big"}" style="flex:1;overflow:hidden">${s}${x.dk.warn_gb ? `<b style="left:${100 - P(x.dk.warn_gb)}%" ${tip(`剩余低于 ${x.dk.warn_gb} GiB 告警`)}></b>` : ""}</div>`;
}
const G = b => b == null ? "—" : b >= GIB ? g1(b / GIB) + " GiB" : Math.round(b / 2 ** 20) + " MiB";

/* ---------- 共享盘（组内服务器 · 共享盘、人员卡与抽屉）：D.disk 是按人汇总好的；各人的颜色记在看板里，不随排名变 ---------- */
const DKC = n => n === "公共" ? "var(--faint)" : D.disk?.colors?.[n] || "var(--muted)";
const diskOf = n => (D.disk?.owners || []).find(o => o.name === n);
const ago = t => t ? dur(D.generated - t) + "前" : "—";
// 共享盘增长的关注档（#108，按人表、人员卡、抽屉里的各目录共用）：照近 24 小时与近 7 天里快的那个速度，多少天占满剩余；
// HEAT_HI 天内或这人的暴涨告警在（burst:）记「暴涨」，HEAT_MID 天内且每天至少涨 1 GiB 记「在涨」，其余不标。配色用 --heat，不借告警红
const HEAT_HI = 7, HEAT_MID = 30;
function diskHeat(x, burst = false) {
  const free = D.disk?.df?.free, d7 = (x.d7 || 0) / 7, r = Math.max(x.d24 || 0, d7), days = free > 0 && r > 0 ? free / r : null;
  const lv = burst || (days != null && days < HEAT_HI) ? "hi" : days != null && days < HEAT_MID && r >= GIB ? "mid" : null;
  if (!lv) return null;
  const why = [`近 24 小时 ${dGB(x.d24)} GiB，近 7 天 ${dGB(x.d7)} GiB`,
    days != null ? `照${r > (x.d24 || 0) ? "近 7 天" : "近 24 小时"}的速度（每天 ${g1(r / GIB)} GiB）约 ${days < 1 ? "不到 1" : Math.round(days)} 天占满剩余的 ${GB(free)} GiB` : "",
    burst ? "已触发暴涨告警" : "", `${HEAT_HI} 天内会占满或有暴涨告警记「暴涨」，${HEAT_MID} 天内记「在涨」`].filter(Boolean).join("\n");
  return {lv, txt: lv === "hi" ? "暴涨" : "在涨", why};
}
const heatTag = h => h ? ` <span class="heat ${h.lv}" ${tip(h.why)}>${h.ic ?? (h.lv === "hi" ? "⇈" : "↑")}${h.txt}</span>` : "";
const heatNum = (h, v) => `<span class="${h ? "heat-n " + h.lv : ""}">${v}</span>`;
const burstOf = n => (D.disk?.alerts || []).some(a => a.key === "burst:" + n);

/* ---------- 本地盘开机以来的新写入（#149）：L 是 fleet.local_writes 算好的（GiB；自用与借来的主机 h.lw，组内服务器与组内页的自用主机 x.lw）。
   续租、关机、换人会清空它。画法与共享盘同一套：分段条 seg、斜纹是 labmon 之外的，关注档用 heatTag / heatNum 与 --heat ---------- */
const lwOut = L => (L?.user || 0) + (L?.shm || 0);          // labmon 之外的新写入加容器私有的 /dev/shm：门槛（risk）与提醒说的都是它
const lwPath = p => p.endsWith("/.") ? (p.slice(0, -2) || "/") + "（本层）" : p;    // 一、二级目录本层的文件记成 <目录>/.
// 过门槛（risk）时的一句话（同 fleet.lw_text，口径相同）：写了多少、大头在哪、verb 会清空
function lwText(L, verb = "续租") {
  if (!L?.risk) return "";
  const top = (L.scan?.top || []).slice(0, 3).map(x => lwPath(x[0])).join("、");
  return `本地盘开机以来 labmon 之外写了 ${g1(lwOut(L))} GiB${top ? `（${top}）` : ""}，${verb}会清空，要留的先挪到共享盘`;
}
// 关注档：labmon 之外写的过门槛记 hi（组内卡片、主机页、抽屉的小标与数字）
const lwHeat = L => L?.risk ? {lv: "hi", ic: "", txt: `本地盘 ${g1(lwOut(L))} GiB`, why: lwText(L, "续租、关机、换人")} : null;
// labmon 之外的各段：扫出的目录一段一段（过门槛用 --heat，否则灰），其余几处、没分到目录的淡一些；P 把 GiB 换成条上的百分比；shm 带上 /dev/shm 一段
function lwSegs(L, P, shm = false) {
  const sc = L.scan, hot = L.risk ? "var(--heat)" : "var(--muted)", pale = `color-mix(in srgb, ${hot} 45%, var(--soft))`;
  let s = (sc?.top || []).map(([p, g, m]) => seg(P(g), hot, `${lwPath(p)}\n${G(g * GIB)}${m ? `，最近写入 ${ago(m)}` : ""}`, lwPath(p).split("/").filter(Boolean).pop() || "/", "hatch")).join("");
  if (sc?.more?.[1] > 0) s += seg(P(sc.more[1]), pale, `其余 ${sc.more[0]} 处 ${G(sc.more[1] * GIB)}`, "", "hatch");
  const rest = L.user - (sc?.b || 0);
  if (rest > 0.05) s += seg(P(rest), pale, sc ? `没分到目录的 ${G(rest * GIB)}：扫过以后新写的、还没扫到的，或起算以前写的` : `labmon 之外 ${G(rest * GIB)}：还没扫路径`, "", "hatch");
  if (shm && L.shm) s += seg(P(L.shm), hot, `/dev/shm（容器私有的内存盘）${G(L.shm * GIB)}：续租同样清空`, "/dev/shm", "hatch");
  return s;
}
// 总量条：labmon 的缓存与单格临时用项目色（loc：worker 的 storage.local，字节，主机才有），labmon 之外的见 lwSegs（斜纹，过门槛变 --heat）；
// 测不准的（不是有配额的 overlay）其余已用一段灰。资源行（主机页资源卡、组内服务器抽屉）按根盘总量（容器配额）画，与别的资源行一样满了就是满了；
// whole：明细（lwBlock）里按开机以来写的总量（加 /dev/shm）画，看得出各部分的比例
function lwBar(L, loc = {}, whole = false) {
  const P = g => 100 * g / (whole ? Math.max(L.used + (L.shm || 0), 0.001) : L.total || 1);
  let s = "", m = 0;
  for (const [p, v] of Object.entries(loc || {})) {
    if (v.cache) s += seg(P(v.cache / GIB), color(p), `${p} 缓存 $LABMON_CACHE：${G(v.cache)}`, p);
    if (v.scratch) s += seg(P(v.scratch / GIB), color(p), `${p} 单格临时 $LABMON_SCRATCH：${G(v.scratch)}`, "", "resv");
    m += (v.cache + v.scratch) / GIB;
  }
  if ((L.own || 0) - m > 0.05) s += seg(P(L.own - m), "var(--faint)", `labmon 的其他文件 ${G((L.own - m) * GIB)}（扫描索引等）`);
  s += L.ok ? lwSegs(L, P, whole) : seg(P(L.user), "var(--line)", `其余已用 ${G(L.user * GIB)}：根盘不是有配额的 overlay，含镜像自带的，测不准开机以来写了多少`);
  return `<div class="bar big" style="${whole ? "margin:6px 0" : "flex:1"};overflow:hidden">${s}</div>`;
}
// 资源行右边的数字：已用 / 总量，labmon 之外的（过门槛用 --heat）
const lwVal = L => `${g1(L.used)} / ${Math.round(L.total)} GiB${L.ok ? `<span class="mut">labmon 之外 ${heatNum(lwHeat(L), g1(L.user))}</span>` : `<span class="mut">测不准</span>`}`;
const LW_TIP = "容器根盘（overlay，平台给的配额）：已用就是开机以来写进来的，续租、关机、换人都会清空；条按配额画\n不是有配额的 overlay 时已用含镜像自带的，测不准";
// 明细（组内服务器抽屉的「本地盘」、主机页的「本地盘」选项卡共用）：按写的总量画的总量条（lwBar whole，宽窄看得出大头）＋ 目录表（大小、最近写入）、
// 扫描进度与没扫的原因。o.started：容器启动时刻（扫的起点晚于它的是借来的主机，只数借来以后的）；o.why：还没扫时的原因；o.loc：worker 的 storage.local
function lwBlock(L, o = {}) {
  if (!L) return `<div class="empty">还没读到根盘。</div>`;
  if (!L.ok) return `<div class="empty">根盘不是有配额的 overlay：已用 ${g1(L.used)} / ${Math.round(L.total)} GiB 含镜像自带的，测不准开机以来写了多少，不扫路径。</div>`;
  const sc = L.scan, hh = lwHeat(L);
  const since = sc?.since && o.started && sc.since > o.started + 60 ? `${ts(sc.since)} 以后` : "开机以来";
  const head = `<div class="sm">开机以来写了 <b>${g1(L.used)} GiB</b>（labmon 之外 ${heatNum(hh, g1(L.user) + " GiB")}${L.own != null ? `，labmon 的缓存与临时 ${g1(L.own)} GiB` : ""}）${L.shm ? `，/dev/shm ${g1(L.shm)} GiB` : ""}；续租、关机、换人都会清空${heatTag(hh)}</div>`;
  const dot = (c, t, k = "") => `<span><span class="dot ${k}" style="background:${c}"></span>${t}</span>`, loc = Object.entries(o.loc || {});
  const bar = L.used + (L.shm || 0) > 0.001 ? lwBar(L, o.loc, true) + `<div class="legend">${loc.filter(([, v]) => v.cache).map(([p]) => dot(color(p), `${esc(p)} 的缓存`)).join("")}
    ${loc.some(([, v]) => v.scratch) ? dot("var(--muted);opacity:.4", "淡色：单格临时") : ""}${dot(L.risk ? "var(--heat)" : "var(--muted)", `斜纹：labmon 之外，按目录${L.risk ? "（过 1 GiB 变紫）" : ""}`, "hatch")}<span>条上悬停看明细</span></div>` : "";
  const rows = (sc?.top || []).map(([p, g, m]) => `<tr><td class="sm"><code>${esc(lwPath(p)).replaceAll("/", "/<wbr>")}</code></td><td class="r">${G(g * GIB)}</td><td class="r sm">${ago(m)}</td></tr>`).join("")
    + (sc?.more?.[0] ? `<tr><td class="sm mut">其余 ${sc.more[0]} 处</td><td class="r">${G(sc.more[1] * GIB)}</td><td></td></tr>` : "");
  const st = !sc ? o.why || "还没扫路径"
    : [sc.err === "nofind" ? "这台没有 GNU find，扫不了路径，只有总量" : "",
       sc.partial ? "上次没扫完（限时），数字只算扫到的；间隔翻倍再扫" : "",
       sc.pending ? `还有 ${sc.pending} 个目录没扫，数字只算扫过的` : "",
       sc.t ? `扫于 ${ago(sc.t)}：只看文件的大小与时间，不读内容；变了才重扫` : ""].filter(Boolean).join("；");
  return head + bar + (rows ? `<div class="scroll"><table class="dku lwt"><tr><th>${esc(since)}新写的目录</th><th class="r">大小</th><th class="r">最近写入</th></tr>${rows}</table></div>` : "")
    + `<div class="sm mut" style="margin-top:4px">${esc(st)}</div>`;
}
// 主机页上的（资源行、「本地盘」选项卡）：借来的主机续租是对方的事，不标关注档
const hostLw = h => h.borrow && h.lw ? {...h.lw, risk: false} : h.lw;
// 借来的主机归还前（跑完就还、现在停的确认）：借用期间 labmon 目录之外新写的（worker 只数借来以后的）。归还只删 /tmp/labmon，
// 这些留在对方那边、我们拿不到（同 borrow.wrote_note 的口径，不到 1 MiB 不说）；对方原有的不在这里列（续租对方的服务器时列）
function lwLeft(h, who) {
  const sc = h?.lw?.scan;
  if (!sc || sc.b * GIB < 2 ** 20) return "";
  const top = sc.top.slice(0, 3).map(x => lwPath(x[0])).join("、");
  return `<div class="pop-w">借用期间本地盘 labmon 目录之外新写了 ${G(sc.b * GIB)}（多半是我们的任务写的${top ? "：" + esc(top) : ""}）。归还只删 /tmp/labmon，这些留在 ${esc(who)} 那边、我们拿不到；要留的先挪到共享盘。</div>`;
}
// 统计进度（主机页、共享盘页的说明）
const diskProg = K => (K.pending ? `各目录由 ${K.scanner || "—"} 的 worker 限速轮流统计：还有 ${K.pending} / ${K.units} 个没扫${K.now ? `，正在扫 ${K.now.slice((K.root || "").length + 1)}` : ""}`
  : `各目录由 ${K.scanner || "—"} 的 worker 限速轮流统计，变了的每小时、没变的最长一周再扫；索引更新于 ${ts(K.at)}`)
  + (K.yield ? `\n此刻${K.yield}：先不开始新的扫描，同一件最多推迟 30 分钟` : "");
// 30 天小曲线（各人每天的占用）
function mini(pts, c) {
  const v = pts.filter(p => p[1] != null);
  if (v.length < 2) return `<span class="sm mut nw">攒数据中</span>`;
  const lo = Math.min(...v.map(p => p[1])), hi = Math.max(...v.map(p => p[1])), t0 = v[0][0], t1 = v[v.length - 1][0];
  const xy = v.map(([t, b]) => `${(70 * (t - t0) / (t1 - t0 || 1)).toFixed(1)},${(14 - 12 * (b - lo) / (hi - lo || 1)).toFixed(1)}`).join(" ");
  return `<svg width="70" height="16" viewBox="0 0 70 16" ${tip(`${ts(t0)} ${GB(v[0][1])} GiB → ${ts(t1)} ${GB(v[v.length - 1][1])} GiB`)}><polyline fill="none" stroke="${c}" stroke-width="1.5" points="${xy}"/></svg>`;
}
function diskBar(K) {
  const T = K.df.total, os = K.owners.filter(o => o.b > 0), main = os.slice(0, 7), rest = os.slice(7), rb = rest.reduce((a, o) => a + o.b, 0);
  let s = main.map(o => seg(100 * o.b / T, DKC(o.name), `${o.name}：${GB(o.b)} GiB（${o.dirs.map(d => d + "/").join("、")}）`, o.name)).join("");
  if (rb) s += seg(100 * rb / T, "var(--muted)", `其他 ${rest.length} 个：${GB(rb)} GiB\n` + rest.map(o => `${o.name} ${GB(o.b)}`).join("\n"), "其他");
  if (K.unscanned > GIB) s += seg(100 * K.unscanned / T, "var(--muted)", `未统计 ${GB(K.unscanned)} GiB：df 已用 − 已统计的各目录${K.pending ? `（还有 ${K.pending} 个目录没扫）` : "（统计以后的变化）"}`, "", "hatch");
  const lg = [...main.map(o => [DKC(o.name), `${o.name} ${GB(o.b)}`, ""]), ...(rb ? [["var(--muted)", `其他 ${GB(rb)}`, ""]] : []),
    ...(K.unscanned > GIB ? [["var(--muted)", `未统计 ${GB(K.unscanned)}`, "hatch"]] : []), ["var(--bad)", `剩 ${K.warn_gb} GiB 告警线`, ""]];
  return `<div class="bar big" style="overflow:hidden">${s}<b style="left:${100 - 100 * K.warn_gb * GIB / T}%" ${tip(`剩余低于 ${K.warn_gb} GiB 告警`)}></b></div>
    <div class="legend">${lg.map(([c, t, cl]) => `<span><i class="dot ${cl}" style="background:${c}"></i>${esc(t)}</span>`).join("")}</div>`;
}
function diskBarSelf(K, me) {
  const T = K.df.total, myB = me?.b || 0, other = Math.max(0, K.df.used - myB);
  let s = myB > 0 ? seg(100 * myB / T, DKC(me.name), `${me.name}：${GB(myB)} GiB（${(me.dirs || []).map(d => d + "/").join("、")}）`, me.name) : "";
  if (other > 0) s += seg(100 * other / T, "var(--muted)", `其他已用：${GB(other)} GiB（总已用 − 本人已统计目录）`, "其他已用", "hatch");
  const lg = [...(myB > 0 ? [[DKC(me.name), `${me.name} ${GB(myB)} GiB`, ""]] : []),
    ...(other > 0 ? [["var(--muted)", `其他已用 ${GB(other)} GiB`, "hatch"]] : []), ["var(--bad)", `剩 ${K.warn_gb} GiB 告警线`, ""]];
  return `<div class="bar big" style="overflow:hidden">${s}<b style="left:${100 - 100 * K.warn_gb * GIB / T}%" ${tip(`剩余低于 ${K.warn_gb} GiB 告警`)}></b></div>
    <div class="legend">${lg.map(([c, t, cl]) => `<span><i class="dot ${cl}" style="background:${c}"></i>${esc(t)}</span>`).join("")}</div>`;
}
function diskTab() {
  const K = D.disk;
  if (!K) return `<div class="card empty">还没有共享盘数据：自用主机的 worker 更新以后（labmon worker start &lt;主机&gt;）开始统计。</div>`;
  if (!K.df) return `<div class="card rn-err">${esc(K.error || "共享盘数据读不了")}</div>`;
  const df = K.df, eta = K.eta ?? K.eta7;
  const kpi = (k, v, sub, bad = false) => `<div class="kpi"><div class="k">${k}</div><div class="v" style="${bad ? "color:var(--bad)" : ""}">${v}</div><div class="s">${sub}</div></div>`;
  if (LIVE && ME && !ME.admin) {
    const me = (K.owners || [])[0] || null, al = K.alerts || [];
    const s = `<div class="card"><div class="chd wrap"><b>共享盘</b><span class="sm mut">${esc(K.root)} · 组里各服务器挂的同一块盘；以下为总盘余量与本人目录用量</span></div>
      <div class="kpis">${kpi("总盘已用", `${GB(df.used)}<small> / ${GB(df.total)} GiB</small>`, `剩 ${GB(df.free)} GiB${eta != null ? `，按近 ${K.eta != null ? "24 小时" : "7 天"}的速度约 ${eta} 天满` : "，最近没在涨"}`, df.free < K.warn_gb * GIB || (K.eta != null && K.eta < K.soon_days))}
        ${kpi("本人占用", me ? (me.t0 == null ? "待扫描" : `${GB(me.b)}<small> GiB</small>`) : "—", me ? (me.t0 == null ? `还有 ${me.pending} 个目录排着` : `占全盘 ${Math.round(100 * me.b / df.total)}%${me.limit ? ` · 提醒线 ${me.limit} GiB` : ""}`) : "名下暂无目录", !!me?.over)}
        ${kpi("本人近 24 小时", me && me.t0 != null ? `${dGB(me.d24)}<small> GiB</small>` : "—", me && me.t0 != null ? `近 7 天 ${dGB(me.d7)} GiB` : "—", !!(me && burstOf(me.name)))}
        ${kpi("本人 90 天没改过", me && me.t0 != null ? `${GB(me.cold)}<small> GiB</small>` : "—", me && me.t0 != null ? `${me.n.toLocaleString()} 个文件` : "—")}</div>
      ${diskBarSelf(K, me)}<div class="sm mut" style="margin-top:6px">${esc(diskProg(K))}</div>
      ${al.length ? `<div class="alerts" style="margin-top:8px">${al.map(a => `<div>⚠ ${esc(a.msg)}</div>`).join("")}</div>` : ""}</div>`;
    return s + (me ? `<div class="card"><div class="chd wrap"><b>本人目录</b><span class="sm mut">按归属汇总的本人共享盘目录（只读）；悬停目录名看里面最大的几项</span></div>${diskOwner(me.name, false)}</div>`
      : `<div class="card empty">账号尚未关联组内人员或名下暂无目录。</div>`);
  }
  const al = (K.alerts || []).filter(a => !a.notify);
  const top = (k, n = 2) => K.owners.filter(o => o[k] > GIB / 20).sort((a, b) => b[k] - a[k]).slice(0, n).map(o => `${esc(o.name)} ${dGB(o[k])}`).join(" · ") || "没有明显增长";
  let s = `<div class="card"><div class="chd wrap"><b>共享盘</b><span class="sm mut">${esc(K.root)} · 组里各服务器挂的同一块盘；总量与剩余每分钟读 df，按人的用量见下表</span></div>
    <div class="kpis">${kpi("已用", `${GB(df.used)}<small> / ${GB(df.total)} GiB</small>`, `剩 ${GB(df.free)} GiB${eta != null ? `，按近 ${K.eta != null ? "24 小时" : "7 天"}的速度约 ${eta} 天满` : "，最近没在涨"}`, df.free < K.warn_gb * GIB || (K.eta != null && K.eta < K.soon_days))}
      ${kpi("近 24 小时", `${dGB(K.d24)}<small> GiB</small>`, top("d24"), (K.alerts || []).some(a => a.key.startsWith("burst:")))}
      ${kpi("近 7 天", `${dGB(K.d7)}<small> GiB</small>`, top("d7"))}
      ${kpi("90 天没改过", `${GB(K.cold)}<small> GiB</small>`, K.owners.filter(o => o.cold > GIB).sort((a, b) => b.cold - a.cold).slice(0, 2).map(o => `${esc(o.name)} ${GB(o.cold)}`).join(" · ") || "—")}</div>
    ${diskBar(K)}<div class="sm mut" style="margin-top:6px">${esc(diskProg(K))}</div>
    ${al.length ? `<div class="alerts" style="margin-top:8px">${al.map(a => `<div>⚠ ${esc(a.msg)}</div>`).join("")}</div>` : ""}</div>`;
  const rows = K.owners.filter(o => o.t0 != null || o.pending > 0).map(o => { const h = o.t0 == null ? null : diskHeat(o, burstOf(o.name));
    return o.t0 == null ? `<tr class="link" tabindex="0" data-drw="${esc(DH.person(o.name))}">
      <td><i class="dot" style="background:${DKC(o.name)}"></i> <b>${esc(o.name)}</b>${o.person || o.public ? "" : ` <span class="sm mut nw">目录</span>`}</td>
      <td colspan="8" class="sm mut">还没扫（${o.pending} 个目录排着）</td></tr>` : `<tr class="link${h ? " heat-" + h.lv : ""}" tabindex="0" data-drw="${esc(DH.person(o.name))}">
      <td><i class="dot" style="background:${DKC(o.name)}"></i> <b>${esc(o.name)}</b>${o.person || o.public ? "" : ` <span class="sm mut nw" ${tip("不在人员名单里：按目录名列出；点开可以归给某人或「公共」")}>目录</span>`}${o.over ? ` <span class="badge b-bad nw" ${tip(`超过提醒线 ${o.limit} GiB`)}>超线</span>` : ""}${heatTag(h)}</td>
      <td class="dkb"><div class="row" style="flex-wrap:nowrap"><div class="bar thin" style="flex:1"><i style="width:${100 * o.b / (K.owners[0]?.b || 1)}%;background:${DKC(o.name)}"></i></div><span class="sm num nw">${GB(o.b)}</span></div></td>
      <td class="r">${heatNum(h, dGB(o.d24))}</td><td class="r hide-sm">${heatNum(h, dGB(o.d7))}</td><td class="hide-sm">${mini(o.spark, DKC(o.name))}</td>
      <td class="r" ${tip("最后修改在 90 天以前的文件")}>${GB(o.cold)}</td><td class="r hide-sm">${o.n.toLocaleString()}</td><td class="r sm hide-sm">${ago(o.m)}</td>
      <td class="r sm mut" ${tip(o.pending ? `还有 ${o.pending} 个目录没扫` : "这人各目录里最久没重扫的那个")}>${o.pending ? `还差 ${o.pending} 个` : ago(o.t0)}</td></tr>`; }).join("");
  s += `<div class="card"><div class="chd wrap"><b>按人</b><span class="sm mut">共享盘根下的一级目录按名字归人（不分大小写），对不上的按目录名列出；点一行看这人的各目录、改归属与提醒线。数字的单位是 GiB；「⇈暴涨」「↑在涨」：照最近的速度 ${HEAT_HI} 天、${HEAT_MID} 天内会占满剩余（悬停看怎么算的）</span></div>
    <div class="scroll"><table class="dkp"><tr><th>人</th><th>占用</th><th class="r">24 小时</th><th class="r hide-sm">7 天</th><th class="hide-sm">近 30 天</th><th class="r">90 天没改</th><th class="r hide-sm">文件数</th><th class="r hide-sm">最近修改</th><th class="r">统计于</th></tr>${rows}</table></div></div>`;
  return s;
}
// 人员卡上的一行
function diskLine(n) {
  const o = diskOf(n);
  if (!o || (o.t0 == null && !(o.pending > 0))) return "";
  const total = o.t0 == null ? "待扫描" : `${GB(o.b)} GiB`;
  const title = `共享盘：${o.dirs.map(d => d + "/").join("、")}${o.pending ? `\n还有 ${o.pending} 个目录没扫` : ""}`;
  const alert = o.over ? '<span style="color:var(--bad)">超线</span>' : heatTag(diskHeat(o, burstOf(n)));
  return `<div class="sm mut people-disk" ${tip(title)}>${alert ? alert + " · " : ""}共享盘 ${total}</div>`;
}
// 人员抽屉里的「共享盘」：各目录（占用、增量、最近修改、多久重扫），一级目录归给谁，提醒线
function diskOwner(n, manage = true) {
  const K = D.disk, o = diskOf(n), edit = manage && canOps();
  if (!K?.df) return `<div class="empty">还没有共享盘数据。</div>`;
  const lim = edit ? `<div class="row" style="gap:6px;margin-top:8px"><span class="sm mut">提醒线</span><input id="dklim" class="cap-in" style="width:80px" value="${esc(FDK.limit ?? o?.limit ?? "")}" placeholder="不设" inputmode="decimal" autocomplete="off">
      <span class="sm mut">GiB</span>${fopBtn("dklim", "保存")}<span class="sm mut">超过只在页面上标红、记事件，不弹通知</span></div>` : o?.limit ? `<div class="sm mut">提醒线 ${o.limit} GiB</div>` : "";
  if (!o) return `<div class="empty">共享盘上没有归到 ${esc(n)} 的目录。</div>${lim}`;
  const names = [...new Set([...(K.names || []), "公共"])];
  const own = d => edit ? `<select data-dkown="${esc(d)}" aria-label="${esc(d)}/ 归给谁"><option value="">按名字（缺省）</option>${names.map(m => `<option value="${esc(m)}"${K.owners_set?.[d] === m ? " selected" : ""}>${esc(m)}</option>`).join("")}</select>` : "";
  const units = o.units.map(u => { const h = u.b == null ? null : diskHeat(u);
    return `<tr${h ? ` class="heat-${h.lv}"` : ""}><td class="sm" ${tip([u.u, ...(u.top || []).map(([k, b]) => `${k}  ${GB(b)} GiB`)].join("\n"))}><code>${esc(u.u).replaceAll("/", "/<wbr>")}</code>${u.err ? ` <span style="color:var(--bad)" ${tip(u.err)}>出错</span>` : ""}${heatTag(h)}</td>
      ${u.b == null ? `<td class="r mut">还没扫</td><td></td><td></td><td></td>` : `<td class="r">${GB(u.b)}</td><td class="r">${heatNum(h, dGB(u.d24))}</td><td class="r">${GB(u.cold)}</td><td class="r sm">${ago(u.m)}</td>`}
      <td class="r sm mut" ${u.s != null ? tip(`扫了 ${u.s} 秒；下次 ${ts(u.due)}`) : ""}>${u.t ? ago(u.t) : "—"}</td></tr>`; }).join("");
  return `<div class="sm" style="margin-bottom:8px">占 <b>${GB(o.b)} GiB</b>（全盘的 ${Math.round(100 * o.b / K.df.total)}%）· 24 小时 ${dGB(o.d24)} · 7 天 ${dGB(o.d7)} · 90 天没改过 ${GB(o.cold)} GiB · ${o.n.toLocaleString()} 个文件${o.pending ? `<span class="mut">（还有 ${o.pending} / ${o.units.length} 个目录没扫，数字只算扫过的）</span>` : ""}${heatTag(diskHeat(o, burstOf(n)))}</div>
    ${o.dirs.map(d => `<div class="row sm" style="gap:6px;margin-bottom:4px"><code>${esc(d === "." ? "（根目录下的文件）" : d + "/")}</code>${d === "." || !edit ? "" : `<span class="mut">归给</span>${own(d)}`}</div>`).join("")}
    <div class="scroll" style="margin-top:6px"><table class="dku"><tr><th>目录</th><th class="r">占用</th><th class="r">24 小时</th><th class="r">90 天没改</th><th class="r">最近修改</th><th class="r">统计于</th></tr>${units}</table></div>
    <div class="sm mut" style="margin-top:4px">单位 GiB；悬停目录名看全名与它里面最大的几项，悬停「暴涨」「在涨」看怎么算的</div>${lim}${fErr()}`;
}
async function diskSet(body) {
  try { await fleetPost("disk", body); FDK = {}; await collectNow(true); } catch (e) { FERR = e.message || String(e); render(false); }
}
// 概况条：租期、容器、本地依赖（续租会清空本地盘），空闲时续租按钮也在这里
function hostStrip(h) {
  const info = h.status?.info || {}, now = D.generated;
  const it = (ic, html, t = "") => `<span class="it" ${t ? tip(t) : ""}>${icon(ic)}<span>${html}</span></span>`;
  if (h.ephemeral) {
    let es = it("cloud", `推模式临时主机${h.instance_id ? ` <span class="mut">实例 ${esc(h.instance_id)}</span>` : ""}${h.project && visibleProject(h.project) ? ` · <a href="#/p/${encodeURIComponent(h.project)}">${esc(h.project)}</a>` : ""}`,
      "仅监控上报，不接入队列调度、平台操作与实验室共享盘");
    if (info.started) es += it("clock", `实例已上报 ${dur(now - info.started)} <span class="mut">${ts(info.started)} 起</span>`);
    return `<div class="card strip"><div class="info"><div class="line">${es}</div></div></div>`;
  }
  const deps = h.status?.local?.jobs || [], bad = deps.filter(r => !r.heal), miss = missW(h);
  let s = "";
  if (h.standalone) s += it("server", `${h.platform_kind === "other" ? "外平台独立主机" : "独立主机"} <span class="mut">不共享集群盘</span>`,
    "不共享集群 worker_home：仅自用，不支持借用、算力池与跨主机搬登记");
  if (h.borrow) s += it("users", `${esc(borrowText(h.borrow, D.generated))}${!h.borrow.swap && LIVE && srvById(h.id) ? ` <a href="${DH.srv(h.id)}">借用详情</a>` : ""}`
      + (BMSG?.id === h.id ? `<span class="mut">（${esc(BMSG.msg)}）</span>` : ""),
    h.borrow.swap ? "改约定：labmon borrow " + h.id + " --until …（none 去掉）" : "");
  if (h.lease_end) s += it("hourglass", `${leaseHtml(h.lease_end, "租期")} <span class="mut">${ts(h.lease_end)} 到期${h.platform_kind === "other" ? "（外平台手动续租）" : ""}</span> ${renewChip(h)}`);
  if (info.started) s += it("clock", `容器已运行 ${dur(now - info.started)} <span class="mut">${ts(info.started)} 启动</span>`);
  const L = h.lw;
  if (L?.risk && !h.borrow) s += it("db", `${heatNum(lwHeat(L), `本地盘 labmon 之外写了 ${g1(lwOut(L))} GiB`)}，续租会清空`, lwText(L) + "\n各目录在下方「记录 · 本地盘」");
  if (deps.length || miss.length) s += it("db", (bad.length ? `<b style="color:var(--bad)">本地依赖 ${deps.length} 格，${bad.length} 格续租后会失败</b>` : `本地依赖 ${deps.length} 格，续租后都能自愈`)
      + (miss.length ? `，<b style="color:var(--bad)">${miss.length} 格缺文件</b>` : ""),
    deps.map(r => `${r.project} ${r.job}：${r.files.length} 个文件 ${G(r.bytes)}，${healText([r])}`).join("\n") + "\n（明细在下方「记录 · 本地依赖」）");
  const lent = h.borrow && !h.borrow.swap && LIVE && srvById(h.id);     // 借来的组内服务器：改约定、还（续租是对方的事）
  const scm = h.platform && (!h.platform_kind || h.platform_kind === "scm");
  const b = scm ? renewBtns(h) : lent ? borrowBtns(lent) : "", btn = b ? `<div class="rn">${b}</div>` : "";
  if (!s && !btn) return "";
  return `<div class="card strip"><div class="info"><div class="line">${s}</div></div>${btn}${RERR[h.id] ? `<div class="rn-err">${esc(RERR[h.id])}</div>` : b ? fErr() : ""}</div>`;
}
function depsTable(h) {
  const deps = h.status?.local?.jobs || [], miss = missW(h);
  if (!deps.length && !miss.length) return `<div class="empty">没有待跑任务声明本地依赖（清单行尾 needs=）。</div>`;
  return `<div class="scroll"><table><tr><th>任务</th><th class="r">文件</th><th class="r">大小</th><th>续租后</th></tr>
    ${miss.map(w => `<tr><td>${projCell(w)} <b>${esc(w.name)}</b></td><td colspan="2" class="sm mut">缺文件，挡在队首</td><td><span class="badge b-bad">缺文件</span></td></tr>`).join("")}
    ${deps.map(j => `<tr><td>${esc(j.project)} <b>${esc(j.job)}</b></td><td class="r" ${tip(j.files.join("\n"))}>${j.files.length}</td><td class="r num">${G(j.bytes)}</td>
      <td>${j.heal ? `<span class="badge b-mute" ${tip(j.how === "redo" ? "命令会先 [ -f … ] || 重新生成" : "needs= 写了共享盘主副本：放行前先拷到本地盘" + (j.how === "copy" ? "" : "，没有主副本的重新生成"))}>${esc(healText([j]))}</span>`
        : `<span class="badge b-bad" ${tip("续租完先不恢复放行，等你处理")}>会失败</span>`}</td></tr>`).join("")}</table></div>`;
}
// 主机页「队列」的一行（只读）：项目、清单（限定卡）、状态；按项目换到别的主机、不用这台在项目的「管理」抽屉里（#121、#133）
function queueRow(h, q) {
  const x = (D.regs || []).find(y => y.host === h.id), L = x ? regLeft(x, q, D.projects.find(p => p.id === q.project)) : null, gp = regGpus(q);
  return `<tr><td>${projCell(q)}</td><td class="sm">${esc(q.root)}/<b>${esc(q.jobs)}</b>${gp ? ` <span class="chip">卡 ${esc(gp)}</span>` : ""}</td>
    <td class="sm nw">${L ? regState(L) : q.empty ? regState({idle: true}) : ""}</td></tr>`;
}
function hostPage(id) {
  const h = D.hosts.find(x => x.id === id);
  if (!h) return `<div class="card empty">没有这台主机（可能已归还）。<a href="#/">回总览</a></div>`;
  const st = h.status;
  let s = alertBox(alertsOf(h), ST.get("agal", false)) + hostStrip(h) + (h.ephemeral ? "" : renewCard(h) + poolCard(h));
  const evTab = {id: "events", name: "事件", note: recNote({host: h.id}, 40), body: () => recSlice({host: h.id}, 40)};
  if (!st) return s + `<div class="card empty">${esc(h.error || h.status_error || "这台主机上还没有 worker 状态。")}</div>` + recCard("host", [evTab]);
  s += (h.ephemeral ? "" : adoptCard(h) + assignCard(h)) + resCard(h);
  s += tasksCard([h], jobsOf(h).map(j => ({...j, h})), (st.waiting || []).map(w => ({...w, h})), false);
  const done = (h.done || []).slice(-15).reverse(), ho = st.overrides || [], qs = st.queues || [], tags = Object.entries(st.limits?.tags || {});
  const deps = st.local?.jobs || [];
  const tabs = [{id: "done", name: "最近跑完", note: done.length ? `${ts(done[0].t1)} ${esc(done[0].name || "")}` : "无", body: () => doneTable(done.map(r => ({...r, h})), false)}, evTab];
  if (!h.ephemeral) tabs.push(
    {id: "ovr", name: "临时指令", note: [ho.length ? `${ho.length} 条` : "无", ...tags.map(([k, v]) => `${esc(k)} ≤ ${v}`)].join("，"), body: () => ovrTable(ho.map(o => ({...o, h})), false)},
    {id: "queues", name: "队列", note: nOf(qs.length), body: () => (qs.length ? `<table>${qs.map(q => queueRow(h, q)).join("")}</table>` : `<div class="empty">没有登记队列。</div>`)
      + (qs.length && LIVE ? `<div class="sm mut" style="margin-top:6px">按项目换到别的主机、不用这台，在项目页的「管理」里。</div>` : "")},
    {id: "deps", name: "本地依赖", note: deps.length ? `${deps.length} 格` : "无", body: () => depsTable(h)},
    {id: "lw", name: "本地盘", note: h.lw?.ok ? `labmon 之外 ${g1(h.lw.user)} GiB` : h.lw ? "测不准" : "无",
      body: () => lwBlock(hostLw(h), {started: st.info?.started, loc: st.storage?.local, why: "worker 还没扫到：限速轮流扫，开机或借来后头一轮要一会儿"})},
    {id: "log", name: "worker 日志", note: h.log.length ? `${Math.min(40, h.log.length)} 行` : "无", body: () => h.log.length ? `<pre>${esc(h.log.slice(-40).join("\n"))}</pre>` : `<div class="empty">没有日志。</div>`});
  s += recCard("host", tabs);
  return s;
}
