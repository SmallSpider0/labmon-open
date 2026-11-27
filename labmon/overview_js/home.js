/* ---------- 记录 ---------- */
// 筛选（分类、服务器、人、关键字）都在地址里，刷新、分享照样打开；改筛选只换地址、不记历史。
// 分类是顶栏的选项卡（带条数），搜索边打边筛；按天分组，先取 LOGN 条，往下「加载更早」再取
const LOGN = 200;
let LOG = null, LGQ = null, LGT = null; // LOG：当前筛选取到的记录 {key, rows, cats, more, busy, gen, err, stale}；LGQ：搜索框里的字；LGT：停手再筛的计时
const rowKey = e => `${e.t}|${e.kind}|${e.host || ""}|${e.msg}`;
const logHref = f => { const q = new URLSearchParams(Object.entries(f).filter(([, v]) => v)).toString(); return "#/log" + (q ? "?" + q : ""); };
async function logLoad(f, mode) {      // mode：空是换了筛选，"new" 是采集了新的一轮（新的接在前面），"old" 是加载更早
  const L = LOG, q = {...f, limit: LOGN};
  if (mode === "old") q.before = L.rows.at(-1).t + 1;          // 同一秒的可能跨页：含上这一秒、去重
  else q.cats = 1;
  L.busy = true; L.gen = D.generated;
  try {
    const r = await api("records.json?" + new URLSearchParams(q), {cache: "no-store"}, {retry: true});
    if (!r.ok) throw new Error("HTTP " + r.status);
    const j = await r.json(), seen = new Set(), all = mode === "old" ? [...L.rows, ...j.rows] : mode === "new" ? [...j.rows, ...L.rows] : j.rows;
    L.rows = all.filter(e => !seen.has(rowKey(e)) && seen.add(rowKey(e))).sort((a, b) => b.t - a.t);
    if (mode !== "new") L.more = j.more;
    if (j.cats) L.cats = j.cats;
    L.err = null;
  } catch (e) { L.err = "记录读不了：" + (e.message || e); L.rows ||= []; }
  L.busy = L.stale = false;
  if (LOG !== L) return;
  if (document.activeElement?.id === "lgq") { FFOCUS = "lgq"; render("keep"); }     // 正在搜索框里打字：照样重画，光标放回去
  else render(false);
}
function logSet(ch) {
  if (!("q" in ch) || ch.q !== (LGQ ?? "").trim()) LGQ = null;       // 打字触发的保留原样（含末尾空格），别处改的以地址为准
  if (document.activeElement?.id === "lgq") FFOCUS = "lgq";
  clearTimeout(LGT);
  nav(logHref({...route().f, ...ch}));
}
// 顶栏的分类选项卡：条数是同样筛选（服务器、人、关键字）下各分类的，没有记录的分类不列
function logTabs(f) {
  const c = LOG?.cats, all = c && Object.values(c).reduce((a, b) => a + b, 0);
  const tabs = [["", "全部", all], ...(D.event_cats || []).map(([k, n]) => [k, n, c?.[k] || 0])].filter(([k, , n]) => !k || !c || n || k === f.cat);
  return `<nav class="ftabs lgtabs">${tabs.map(([k, n, ct]) => `<a class="ftab${(f.cat || "") === k ? " on" : ""}" href="${esc(logHref({...f, cat: k}))}">${esc(n)}${c ? `<span class="ct">${ct}</span>` : ""}</a>`).join("")}</nav>`;
}
const dayName = t => { const d = new Date(t * 1000), n = new Date(); return (d.toDateString() === n.toDateString() ? "今天 · " : "") + `${tsMD(t)} 周${"日一二三四五六"[d.getDay()]}`; };
const LOGKEEP = "问题报告、掉线与恢复、平台操作、登录退出、主机清单与人员的改动、租期、保活守护、共享盘告警、看板自身的采集出错保留一年；队列与 worker 的保留 30 天。";
function logPage(f) {
  if (!LIVE) return `<div class="card"><div class="empty">记录要经看板服务（labmon serve）打开才看得到。</div></div>`;
  const key = new URLSearchParams(f).toString();
  if (LOG?.key !== key) { LOG = {key, rows: LOG?.rows || null, cats: LOG?.cats, stale: !!LOG?.rows}; logLoad(f); }    // 换筛选时先淡显旧的，不闪「读取中」
  else if (LOG.gen !== D.generated && !LOG.busy) logLoad(f, "new");
  const L = LOG, F = D.fleet;
  const hosts = [...D.hosts.map(h => [h.id, h.label]), ...(F ? [...F.mine, ...F.servers].map(x => [x.id, x.holder ? `${x.holder} ${x.id}` : x.id]) : [])]
    .filter(([id], k, a) => a.findIndex(x => x[0] === id) === k).sort((a, b) => byName(a[0], b[0]));
  if (f.host && !hosts.some(x => x[0] === f.host)) hosts.push([f.host, f.host]);         // 已不在清单里的服务器
  const people = Object.keys(fops().people || {}).sort(byName);
  if (f.who && !people.includes(f.who)) people.push(f.who);
  const opt = (v, name, on) => `<option value="${esc(v)}"${on ? " selected" : ""}>${esc(name)}</option>`;
  const sel = (k, ico, all, opts) => `<label class="lgsel${f[k] ? " on" : ""}">${icon(ico)}<select data-lg="${k}" aria-label="${all}">${opt("", all)}${opts}</select>${icon("chev")}</label>`;
  const chip = (k, ico, text) => `<button class="lgchip" data-lg="rm" data-k="${k}" aria-label="去掉筛选：${esc(text)}">${icon(ico)}${esc(text)}${icon("x")}</button>`;
  const on = [f.host && chip("host", "server", hosts.find(x => x[0] === f.host)[1]), f.who && chip("who", "users", f.who), f.q && chip("q", "search", `“${f.q}”`)].filter(Boolean);
  const n = L.cats && (f.cat ? L.cats[f.cat] || 0 : Object.values(L.cats).reduce((a, b) => a + b, 0));
  let s = `<div class="lgbar">
    <label class="lgq">${icon("search")}<input id="lgq" type="search" placeholder="搜索记录里的文字" value="${esc(LGQ ?? f.q ?? "")}" aria-label="搜索记录" autocomplete="off"></label>
    ${sel("host", "server", "全部服务器", hosts.map(([id, nm]) => opt(id, nm, f.host === id)).join(""))}
    ${sel("who", "users", "全部的人", people.map(x => opt(x, x, f.who === x)).join(""))}
    <span class="sp"></span><span class="sm mut nw">${n != null && !L.stale ? `共 ${n} 条 ` : ""}<span class="ii" ${tip(LOGKEEP)}>ⓘ</span></span></div>`;
  if (on.length) s += `<div class="lgon"><span class="sm mut">筛选</span>${on.join("")}<button class="lglink" data-lg="clear">全部清除</button></div>`;
  s += `<div class="card rlog${L.stale ? " stale" : ""}">`;
  if (!L.rows) return s + `<div class="empty">读取中…</div></div>`;
  if (!L.rows.length) s += `<div class="empty" style="padding:14px 0">${key ? "没有符合筛选的记录。" : "还没有记录。"}</div>`;
  let day = null, grp = [];
  const flush = () => { if (grp.length) s += `<div class="lgday"><b>${dayName(grp[0].t)}</b><span class="sm mut">${grp.length} 条</span></div>` + recList(grp, {showHost: !f.host, day: true}); };
  for (const e of L.rows) {
    const d = new Date(e.t * 1000).toDateString();
    if (d !== day) { flush(); day = d; grp = []; }
    grp.push(e);
  }
  flush();
  if (L.more) s += `<div class="lgmore"><button class="btn" data-lg="more" ${L.busy ? "disabled" : ""}>${L.busy ? "读取中…" : "加载更早"}</button></div>`;
  if (L.err) s += `<div class="rn-err">${esc(L.err)}</div>`;
  return s + `</div>`;
}

/* ---------- 总览 ---------- */
// 各项目看板的告警（失败、队列进程不在、GPU 空转等）：总览的告警数与列表都算上，每条带项目名。
// 主机级的（磁盘余量、自定义检查）同机几个项目报的是同一句，只列一次、写上都有谁
const projAlerts = () => { const by = new Map(); for (const p of D.projects) for (const m of p.alert_msgs || []) by.set(m, [...(by.get(m) || []), p]);
  return [...by].map(([m, ps]) => ({ps, m})); };
// 页面上的项目名，后面带一个空格：配色点 + 标题（悬停 id）。几个项目时才写，always 总写（指令的对象）；plain 只要文字（悬停提示、受影响列表）
// 普通用户此刻能用的主机：单独分配给这人的（ME.hosts），加上快照里给这人看的主机（所属共有池或本人项目有车道的，users.access 的 hosts）
const myHosts = () => [...new Set([...(ME?.hosts || []), ...D.hosts.map(h => h.id)])];
// 普通用户是否属于至少一个有主机的共有型算力池（#212）：算作已有可用算力，不再弹无可用主机提示
const hasPoolCompute = () => !!(ME?.pools?.length || (D.pool?.pools || []).some(p => (p.total ?? p.hosts?.length ?? 0) > 0));
const visibleProject = id => !!id && (!LIVE || !!ME?.admin || D.projects.some(p => p.id === id));
const projName = (id, {always = false, plain = false} = {}) => {
  if (!id || !always && D.projects.length < 2 && (!LIVE || ME?.admin)) return "";
  const t = D.projects.find(p => p.id === id)?.title || id;
  return plain ? t + " " : `<span class="nw"><span class="dot" style="background:${color(id)}"></span> <span ${tip(id)}>${esc(t)}</span></span> `;
};
let AL_EXP = false;                // 总览告警超 5 条时默认折叠，点击展开/收起
function home() {
  const noHost = !myHosts().length && !hasPoolCompute();      // 确实没有可用主机且不在任何有主机的共有池才提（#15、#212、#224）
  const fleetSrvs = D.fleet?.servers || [], fleetN = fleetSrvs.length;
  const optOutN = fleetSrvs.filter(s => s.manage?.opt_out).length;
  const offN = fleetSrvs.filter(s => s.off).length;
  const noHostMsg = !noHost ? "" : optOutN
    ? `<p>你名下有 ${optOutN} 台服务器当前处于仅监控状态：可在<a href="#/fleet">我的服务器</a>或<a href="#/me">我的账号</a>一键重新接入本人调度，也可请管理员分配其他主机或加入共有算力池。</p>`
    : fleetN
    ? `<p>你名下有 ${fleetN} 台组内服务器（${offN === fleetN ? "当前均已关机" : "当前暂未接入调度"}，可在<a href="#/fleet">我的服务器</a>查看）：开机并空闲后会自动接入本人调度，也可请管理员分配主机或加入共有算力池。</p>`
    : '<p>你的账号还没有能用的主机：请管理员在「组内服务器 → 人员」里打开你，在「账号与权限」给你分配主机，或将你加入共有算力池。</p>';
  const intro = LIVE && ME && !ME.admin && (!D.projects.length || noHost) ? dSec("开始使用", `
    ${noHostMsg}
    ${D.projects.length ? "" : '<p>从<a href="#/me">我的账号</a>下载命令行，在项目目录里运行 <code>labmon project push</code> 登记项目。</p>'}`) : "";
  if (intro && !D.hosts.length && !D.projects.length && !(D.pool?.pools?.length)) return intro;
  const al = allAlerts(), fal = fleetAlerts(false), pal = projAlerts(), hs = D.hosts.filter(h => h.status?.info);
  const jobs = D.hosts.flatMap(h => jobsOf(h).map(j => ({...j, h})));
  const allw = D.hosts.flatMap(h => (h.status?.waiting || []).map(w => ({...w, h})));
  const waits = allw.filter(w => w.name), blocked = allw.filter(w => !w.name);
  const gu = hs.flatMap(h => (h.status.info.gpus || []).map(g => ({...g, h}))), gUsed = gu.reduce((a, g) => a + g.used, 0), gTot = gu.reduce((a, g) => a + g.total, 0);
  const U = utilText(utilAvg(gu.map(g => gpuBusy(g.h, g.i)), gu.map(g => g.util))), now = D.generated;
  const lease = D.hosts.filter(h => h.lease_end).sort((a, b) => a.lease_end - b.lease_end)[0];
  // 全部跑完：worker 推演里最晚的结束（与任务卡标题、项目表同一来源）
  const fin = D.hosts.flatMap(h => planLanes(h).flatMap(l => l.jobs.map(j => ({p: l.project, t1: j.t1 || 0})))).sort((a, b) => b.t1 - a.t1)[0];
  const last = fin && fin.t1 > now ? fin : null;
  const mk = D.projects.flatMap(p => (p.markers || []).map(m => ({...m, p}))).filter(m => m.ts > now).sort((a, b) => a.ts - b.ts)[0];   // 几个项目时写明是谁的
  const na = al.length + fal.length + pal.length;
  const selfView = LIVE && ME && !ME.admin, fLbl = selfView ? (D.fleet ? "我的服务器" : "共享盘") : "组内", fHref = selfView && !D.fleet ? "#/fleet/disk" : "#/fleet";
  let s = intro + `<div class="kpis k5">
    <div class="kpi"><div class="k">运行 / 等待</div><div class="v">${jobs.length}<small> / ${waits.length} 个任务</small></div><div class="s">${[esc([...new Set(jobs.map(j => j.project))].join("、")), waits.filter(w => w.excl).length ? waits.filter(w => w.excl).length + " 个独占在等" : "", blocked.length ? blocked.length + " 条队列受阻" : ""].filter(Boolean).join("，") || "—"}</div></div>
    <div class="kpi"><div class="k">显存</div><div class="v">${pct(gUsed, gTot) ?? "—"}<small>%</small></div><div class="s">${mb2g(gUsed)} / ${mb2g(gTot)} GiB，${gu.length} 张卡${U.v != null ? `，<span ${tip(U.tip)}>${U.span}利用率 ${U.big}</span>` : ""}</div></div>
    <div class="kpi"><div class="k">最早到期的租期</div><div class="v">${lease ? leaseHtml(lease.lease_end) : "—"}</div><div class="s">${lease ? `${esc(hostShort(lease))} · ${ts(lease.lease_end)} 到期` : "没有登记租期"}</div></div>
    <div class="kpi"><div class="k">预计全部跑完</div><div class="v" style="font-size:19px">${last ? ts(last.t1) : "—"}</div><div class="s" ${mk ? tip(`下一个里程碑：${projName(mk.p.id, {plain: true})}${mk.label} ${ts(mk.ts)}`) : ""}>${last ? `${projName(last.p)}剩 ${dur(last.t1 - now)}` : "没有排队的任务"}${mk ? `，离 ${mk.p.id === last?.p ? "" : projName(mk.p.id)}${esc(mk.label)} ${dur(mk.ts - now)}` : ""}</div></div>
    <div class="kpi"><div class="k">告警</div><div class="v" style="color:${na ? "var(--bad)" : "var(--ok)"}">${na}<small> 条</small></div><div class="s">${na ? (fal.length || pal.length ? "含" + [fal.length ? `${fLbl} ${fal.length} 条` : "", pal.length ? `项目 ${pal.length} 条` : ""].filter(Boolean).join("、") + "，" : "") + "见下方" : `${D.hosts.filter(h => h.worker_up).length} / ${D.hosts.length} 台 worker 在跑`}</div></div></div>`;
  if (na) {
    const items = [...al.map(a => `<div>⚠ ${esc(a)}</div>`),
      ...fal.map(a => `<div>⚠ <a href="${fHref}">${fLbl}</a>：${esc(a)}</div>`),
      ...pal.map(({ps, m}) => `<div>⚠ ${ps.map(p => `<a href="#/p/${encodeURIComponent(p.id)}">${esc(p.title || p.id)}</a>`).join("、")}：${esc(m)}</div>`)];
    const fold = items.length > 5 && !AL_EXP;
    const head = items.slice(0, 5).join("");
    const extra = items.length > 5 ? `<div class="al-extra"${fold ? " hidden" : ""}>${items.slice(5).join("")}</div>`
      + `<div><button class="lnk sm" type="button" data-alexp aria-expanded="${!fold}">${fold ? `展开其余 ${items.length - 5} 条告警 ▾` : "收起告警 ▴"}</button></div>` : "";
    s += `<div class="alerts">${head}${extra}</div>`;
  }
  const addExtBtn = LIVE && ME && (ME.admin ? canOps() : ME.write) ? `<span class="sp"></span><button class="btn mini-btn" data-drw="#/ext/new">+ 添加外部主机</button>` : "";
  s += `<div class="card"><div class="chd wrap"><b>主机</b><span class="sm mut">点一行看详情</span>${addExtBtn}</div><div class="scroll"><table>
    <tr><th>主机</th><th>worker</th><th style="min-width:180px" ${tip("每张卡一行：条是显存，数字是显存占比 · GPU 近 1 小时平均利用率（保活按 0 算）")}>显存 · 利用率</th><th style="min-width:180px">内存（容器）</th><th style="min-width:160px" ${tip("集群主机显示共享盘；独立主机显示本地工作盘（含低水位/配额闸门）；红线是告警/停止线")}>磁盘</th><th class="r">CPU</th><th class="r">运行</th><th class="r">等待</th><th>租期</th></tr>` +
    (D.hosts.map(h => {
      const info = h.status?.info, gs = info?.gpus || [], m = info?.mem;
      const gb = gs.map(g => {
        const isMps = g.backend === "mps", U = utilText(gpuBusy(h, g.i), g.util), up = unacctPaused(h, g), umb = g.unacct_mb || 0;
        const utip = isMps ? `\n${g.name || "Apple Silicon (MPS)"}（统一内存保守估算，默认单卡互斥，不采集利用率）`
          : up ? `\n外部（如宿主机）占卡暂停放行${umb ? `（约 ${mb2g(umb)} GiB）` : ""}${g.unacct_why ? `：${g.unacct_why}` : ""}`
          : umb >= 512 ? `\n外部（如宿主机）未归因显存约 ${mb2g(umb)} GiB（已扣减可放显存）` : "";
        const utag = isMps ? ` <span class="badge b-mute">MPS</span>`
          : up ? ` <span class="badge b-warn">外部占卡</span>` : umb >= 512 ? `<span class="mut"> · 外部 ${mb2g(umb)}G</span>` : "";
        return `<div class="row" style="flex-wrap:nowrap" ${tip(`GPU ${g.i}：${mb2g(g.used)} / ${mb2g(g.total)} GiB\n${isMps ? "Apple Silicon MPS 逻辑卡" : U.tip}${utip}`)}><div class="bar thin" style="flex:1"><i style="width:${g.total ? 100 * g.used / g.total : 0}%;background:${up ? "var(--warn)" : "var(--accent)"}"></i></div><span class="sm num nw">${pct(g.used, g.total)}%${isMps ? "" : `<span class="mut"> · ${U.big}</span>`}${utag}</span></div>`;
      }).join("");
      const sx = (h.ephemeral || h.standalone) ? null : sharedParts(), wd = h.standalone ? h.work_disk : null;
      const ephSto = h.ephemeral ? (h.storage || []).map(x => {
        const low = x.kind === "cloud" && x.avail_gb < 1;
        return `<span class="sm nw" style="${low ? "color:var(--bad);font-weight:600" : ""}" ${tip(`${x.kind === "cloud" ? "云盘（不计入实验室共享盘）" : "实例本地盘（回收清空）"}\n${x.path}：剩 ${g1(x.avail_gb)} / ${g1(x.total_gb)} GiB`)}>${esc(x.label)} 剩 ${g1(x.avail_gb)} GiB</span>`;
      }).join(" · ") || `<span class="sm mut">无共享盘</span>` : null;
      const sb = h.ephemeral ? ephSto
        : h.standalone ? (wd ? `<div class="row" style="flex-wrap:nowrap">${workDiskBar(wd, true)}<span class="sm num nw" style="${wd.disk_gate?.on || h.status?.disk_gate?.on || wd.low ? "color:var(--bad)" : ""}">${wd.disk_gate?.on || h.status?.disk_gate?.on ? "闸门 · " : ""}剩 ${Math.round((wd.free || 0) / GIB)}</span></div>` : `<span class="sm mut" ${tip("独立主机不挂集群共享盘，本地工作盘等待首次扫描")}>—</span>`)
        : sx ? `<div class="row" style="flex-wrap:nowrap">${sharedBar(sx, true)}<span class="sm num nw" style="${sx.dk.free < sx.dk.warn_gb ? "color:var(--bad)" : ""}">剩 ${Math.round(sx.dk.free)}${sx.dk.eta_days != null && sx.dk.eta_days < 7 ? ` · ${sx.dk.eta_days} 天满` : ""}</span></div>` : "—";
      const mb = m ? `<div class="row" style="flex-wrap:nowrap"><div class="bar thin" style="flex:1"><i style="width:${100 * m.used / m.limit}%;background:${m.used / m.limit >= 0.9 ? "var(--bad)" : "var(--accent)"}"></i><b style="left:90%"></b></div><span class="sm num nw">${g1(m.used)} / ${g1(m.limit)}</span></div>` : "—";
      const poolTag = h.pool_label ? ` <span class="badge b-mute" ${tip(`共有算力池：${h.pool_label}（${h.pool_exclusive ? "独占，请经池入口提交" : "开放，允许直投单台"}）`)}>${esc(h.pool_label)}</span>` : "";
      const autoTag = h.member_auto ? ` <span class="badge b-ok" ${tip("本人名下自动接入调度的组内服务器（不动已有实验与用户自开保活）")}>我的服务器</span>` : "";
      return `<tr class="link" tabindex="0" data-go="#/host/${encodeURIComponent(h.id)}"><td><span class="cdot" style="background:${hostDot(h)}"></span> <b>${esc(h.label)}</b>${h.ephemeral ? ` <span class="badge b-mute">临时</span>` : standaloneBadge(h)}${autoTag}${poolTag}</td><td>${workerBadge(h)}</td>
        <td>${gb || "—"}</td><td>${mb}</td><td>${sb}</td><td class="r nw">${info?.cpu_used != null ? g1(info.cpu_used) : "—"} <span class="mut">/ ${info?.cpu_quota ? g1(info.cpu_quota) : info?.nproc ?? "—"}</span>${thrTag(h) ? `<div>${thrTag(h)}</div>` : ""}</td>
        <td class="r">${jobsOf(h).length}</td><td class="r">${(h.status?.waiting || []).length}</td>
        <td class="sm nw" ${!h.ephemeral && info?.started ? tip(`${info.os === "darwin" ? "主机" : "容器"} ${ts(info.started)} 启动，已运行 ${dur(now - info.started)}`) : ""}>${h.ephemeral ? `<span class="mut">临时实例</span>` : h.lease_end ? `${leaseHtml(h.lease_end, "租期")} <span class="mut">${tsMD(h.lease_end)}</span>` : "—"} ${h.ephemeral ? "" : renewChip(h)}</td></tr>`;
    }).join("") || `<tr><td colspan="9" class="empty">主机清单里没有在用的主机。</td></tr>`) + `</table></div></div>`;
  s += poolSummaryCard() + schedCard() + tasksCard(D.hosts, jobs, allw, true);
  const pEnd = id => Math.max(0, ...D.hosts.flatMap(h => planLanes(h).filter(l => l.project === id).flatMap(l => l.jobs.map(j => j.t1 || 0))));   // 这个项目在各主机推演里最晚的结束
  s += `<div class="card"><div class="chd"><b>项目</b></div><table><tr><th>项目</th><th>更新</th><th>预计全部跑完</th><th>下一个里程碑</th><th>告警</th></tr>` +
    (D.projects.map(p => { const m = (p.markers || []).filter(x => x.ts > now).sort((a, b) => a.ts - b.ts)[0];
      return `<tr class="link" tabindex="0" data-go="#/p/${encodeURIComponent(p.id)}"><td><span class="dot" style="background:${color(p.id)}"></span> ${esc(p.title || p.id)}</td>
      <td class="sm">${ts(p.generated)}</td><td class="sm">${p.no_work_since ? noWorkBadge(p.no_work_since) : ts(pEnd(p.id))}${rebalMark(D.rebalance?.[p.id])}</td><td class="sm">${m ? `${esc(m.label)} ${tsMD(m.ts)} <span class="mut">（${dur(m.ts - now)}后）</span>` : "—"}</td>
      <td>${p.alerts ? `<span class="badge b-bad">${p.alerts} 条</span>` : `<span class="badge b-ok">无</span>`}</td></tr>`; }).join("")
      || `<tr><td colspan="5" class="empty">看板服务里没有项目。</td></tr>`) + `</table></div>`;
  s += svcCard();
  const d = D.hosts.flatMap(h => (h.done || []).map(r => ({...r, h}))).sort((a, b) => b.t1 - a.t1).slice(0, 10);
  const ovr = D.hosts.flatMap(h => (h.status?.overrides || []).map(o => ({...o, h})))       // 项目级的（global）每台都有一份，只列一次
    .filter((o, i, a) => !o.global || a.findIndex(x => x.global && x.kind === o.kind && x.target === o.target) === i);
  s += recCard("home", [
    {id: "done", name: "最近跑完", note: d.length ? `${ts(d[0].t1)} ${esc(d[0].name || "")}` : "无", body: () => doneTable(d, true)},
    {id: "events", name: "事件", note: recNote({}, 20), body: () => recSlice({}, 20)},
    {id: "ovr", name: "临时指令", note: ovr.length ? `${ovr.length} 条` : "无", body: () => ovrTable(ovr, true)}]);
  return s;
}

// 看板服务小卡（#166）：看板服务跑在 WSL 里时，体检所在的 Windows 机与 WSL（快照的 hostcheck，hostcheck.view）；不是 WSL 时没有这张卡。
// 一格一项，提醒成立（V.warn 的 key）的那格染色，下面逐条列提醒原文（与推到手机的同一句）；门槛取 V.lim，不另写一份；计划维护中（V.maint，#165）格子上面一行说明
function svcCard() {
  const V = D.hostcheck;
  if (!V) return "";
  const now = D.generated, L = V.lim || {}, W = V.warn || {}, E = V.err || {}, nw = Object.keys(W).length;
  const dot = c => `<span class="cdot" style="background:var(--${c})"></span>`;
  const tile = (k, v, sub, keys, tp, cls = "") => {
    const hit = keys.some(x => W[x]);
    return `<div class="svc-t${hit ? " hit" : ""}${cls ? " " + cls : ""}" ${tp ? tip(tp) : ""}><div class="k">${dot(hit ? "warn" : cls === "off" ? "faint" : "ok")}${k}</div>`
      + `<div class="v">${v}</div><div class="s">${sub || "&nbsp;"}</div></div>`;
  };
  const gib = mb => mb == null ? "—" : `${mb2g(mb)}<small>GiB</small>`;
  if (!V.t) return `<div class="card"><div class="chd"><b>看板服务</b><span class="sm mut">还没体检过（看板服务起来后几分钟内有）</span></div></div>`;
  const u = V.updates || {}, ul = u.list, sec = (ul || []).filter(x => x.sec).length, au = V.au, tn = V.tunnel || {}, wm = V.win_mem || {}, sm = V.wsl_mem || {};
  const src = u.src === "task" ? "计划任务" : "离线搜索";
  const pause = V.pause_until && V.pause_until > now ? V.pause_until : null;
  const tiles = [
    tile("开机", V.boot ? dur(now - V.boot) : "—", V.boot ? `${ts(V.boot)} 起` : "", ["uptime"], `开机超过 ${L.uptime_d} 天提醒装更新、重启一次`),
    tile("待重启", V.reboot ? "要重启" : V.boot ? "不用" : "—", V.reboot ? `等了 ${dur(now - V.reboot)}` : "没有挂着的", ["reboot"], `装完更新等重启超过 ${dur(L.reboot_s)} 提醒`),
    tile("待装更新", ul == null ? "—" : ul.length ? `${ul.length}<small>个</small>` : "无", u.err && ul == null ? "读不了" : ul && ul.length ? (sec ? `安全更新 ${sec} 个` : "没有安全更新") : u.at ? `${src} ${tsNear(u.at)}` : "",
      ["updates"], [`${src}${u.at ? ` ${ts(u.at)}` : ""}；安全更新挂了 ${dur(L.sec_s)}、其余挂了 ${dur(L.any_s)} 以上提醒`, ...(ul || []).map(x => `${x.sec ? "[安全] " : ""}${x.title}${x.since ? `（已挂 ${dur(now - x.since)}）` : ""}`),
        u.err ? `这次没读出：${u.err}` : ""].filter(Boolean).join("\n")),
    tile("更新方式", au ? (au.manual ? "手动装" : "自动装") : "—", pause ? `暂停到 ${tsMD(pause)}` : au ? esc(au.text) : "", ["pause"],
      [au ? `${au.text}${au.manual ? "" : "：会自动装更新并在夜里重启，按 #157 定的改成通知后手动装"}` : "", pause ? `暂停更新到 ${ts(pause)}，${dur(L.pause_s)} 内到期提醒` : ""].filter(Boolean).join("\n"), au && !au.manual ? "cau subhit" : "subhit"),
    tile("Windows 内存", gib(wm.free), wm.total ? `可用 · 共 ${mb2g(wm.total)} GiB` : "", ["win_mem"], `可用低于 ${mb2g(L.win_free_mb)} GiB 持续 ${dur(L.low_s)} 提醒`),
    tile("WSL 内存", gib(sm.avail), sm.total ? `可用 · 上限 ${mb2g(sm.total)} GiB${V.oom?.n ? ` · OOM ${V.oom.n} 次` : ""}` : "", ["wsl_mem", "oom"],
      `可用低于上限的 ${Math.round(L.wsl_frac * 100)}% 持续 ${dur(L.low_s)} 提醒；近 ${dur(L.oom_s)} 有 OOM kill 也提醒` + (V.oom?.last ? `\n最近一次 OOM kill：${ts(V.oom.last)}` : "")),
    tile("服务进程", V.rss == null ? "—" : V.rss < 1024 ? `${Math.round(V.rss)}<small>MiB</small>` : gib(V.rss), "labmon serve 常驻内存", ["rss"], `超过 ${mb2g(L.rss_mb)} GiB 像在泄漏，提醒`),
    tile("隧道", {active: "在连", down: "断开", none: "没装", unknown: "—"}[tn.state] || "—",
      tn.state === "down" && tn.since ? `已断 ${dur(now - tn.since)}` : tn.state === "none" ? "公网入口还没装" : tn.state === "active" ? "到公网入口" : "",
      ["tunnel"], `${tn.unit || ""}：到 TX 公网入口的反向隧道，断了超过 ${dur(L.tunnel_s)} 提醒`, tn.state === "none" ? "off" : "")];
  const how = `每 ${dur(V.tick)}看一次所在的 Windows 机与 WSL，待装更新每 ${dur(V.upd_every)}查一次。格子染色表示提醒成立；`
    + `成立的推到手机，只在白天 ${V.day[0]}–${V.day[1]} 点推、每件事一天一次。悬停各格看门槛。`;
  const old = now - V.t > 5 * V.tick;
  const errs = Object.entries(E).map(([k, e]) => `<div class="mut">读不了${k === "win" ? " Windows" : " WSL"}：${esc(e)}</div>`);
  return `<div class="card"><div class="chd wrap"><b>看板服务</b><span class="sm mut">${esc(V.name || "")}${V.name ? " · " : ""}Windows + WSL · `
    + `${old ? `<span style="color:var(--warn)">体检停在 ${tsNear(V.t)}</span>` : `体检于 ${tsNear(V.t)}`} <span class="ii" ${tip(how)}>ⓘ</span></span>`
    + `${V.role ? `<span class="badge b-mute">${V.role === "primary" ? "主" : "备用"}</span>` : ""}<span class="sp"></span>`
    + `${nw ? `<span class="badge b-warn">${nw} 项要处理</span>` : `<span class="badge b-ok">正常</span>`}</div>`
    + (V.maint && V.maint > now ? `<div class="svc-m sm">计划维护到 ${tsNear(V.maint)}：期间不推体检提醒，TX 的看门狗也不报（<code>labmon server maint off</code> 提前结束）</div>` : "")
    + `<div class="svc">${tiles.join("")}</div>`
    + (nw || errs.length ? `<div class="svc-w">${Object.values(W).map(m => `<div class="w">⚠ ${esc(m)}</div>`).join("")}${errs.join("")}</div>` : "") + `</div>`;
}
// worker 推演（status.plan）：各车道的任务何时开始结束。planEnd 是这台最晚的结束；stuckWhy 是等待中的任务排不上的原因（它那条车道的 stuck：队列按目录与清单认，单元按项目）
const planLanes = h => h.status?.plan?.lanes || [], planEnd = h => Math.max(0, ...planLanes(h).flatMap(l => l.jobs.map(j => j.t1 || 0)));
const stuckWhy = x => planLanes(x.h).find(l => l.kind === "slot" ? x.kind === "slot" && l.project === x.project : l.root === x.root && l.queue === x.queue)?.stuck;
// worker 状态里的一条队列（推演的车道、等待项、borrow.late：有 root、queue）对应快照里 projects[].lanes 的那一条（有 label，交给 laneName）；
// 对不上的（没接入看板的项目）用队列名
const laneOf = (h, l) => D.projects.flatMap(p => p.lanes || []).find(x => x.host === h.id && x.root === l.root && x.queue === l.queue) || {host: h.id, label: l.queue};
// 任务卡（总览与主机页共用）：占用时间线 + 运行中 + 等待中（没有在等的就只在标题里写 0）
function tasksCard(hs, jobs, allw, showHost) {
  // 队列只数有待办的；清单跑空了的（项目暂时没实验）不算，免得别人以为有人在抢卡
  const qs = hs.flatMap(h => h.status?.queues || []), qn = qs.filter(q => !q.empty).length, qe = qs.length - qn;
  const waits = allw.filter(w => w.name);
  // 全部跑完：推演里最晚的结束；排到租期之后的主机标红，推演里排不上的车道单独写
  const end = Math.max(0, ...hs.map(planEnd)), late = hs.filter(h => h.lease_end && planEnd(h) > h.lease_end);
  const stuck = hs.flatMap(h => planLanes(h).filter(l => l.stuck).map(l => `${projName(l.project, {always: true, plain: true})}${laneName(laneOf(h, l), hs)}：${l.stuck}`));
  const head = [`运行 ${jobs.length}`, `等待 ${waits.length}`, `<span ${qe ? tip(`另有 ${qe} 条登记着但清单里没有待办（空着，不占卡）`) : ""}>队列 ${qn} 条${qe ? `（另 ${qe} 条空着）` : ""}</span>`,
    end > D.generated ? `<span style="${late.length ? "color:var(--bad)" : ""}" ${late.length ? tip(`${late.map(hostShort).join("、")} 的排队按推演排到租期之后：续租前跑不完`) : ""}>预计 ${ts(end)} 全部跑完${late.length ? "（晚于租期）" : ""}</span>` : "",
    stuck.length ? `<span style="color:var(--warn)" ${tip(stuck.join("\n"))}>${stuck.length} 条车道推演里排不上</span>` : ""].filter(Boolean).join(" · ");
  TLH = hs.some(h => h.status?.info?.gpus?.length || (D.occ?.[h.id] || []).some(isCpuOcc)) ? {hs, showHost} : null;
  return `<div class="card"><div class="chd wrap"><b>任务</b><span class="sm mut">${head}</span>
      <span class="ii" ${tip("点一行展开命令与日志。显存、内存为“当前 / 峰值”；内存按会话内全部进程的 PSS 计，fork 出来的子进程不会重复计。\n时间线：每张卡一行，有纯 CPU 任务的主机在卡行下方加一行 CPU，按项目配色；往后的是 worker 按同一套放行规则推演的，同机各项目一起排。")}>ⓘ</span>
      ${TLH ? `<span id="tlbar"></span>` : ""}</div>
    ${TLH ? `<div class="sm" id="tllegend"></div><div id="tlbox"></div><div id="tlkey" class="tlkey${TL.key ? " open" : ""}"></div>` : ""}${jobsTable(jobs, showHost)}
    ${allw.length ? `<div class="sub">等待</div>${waitingTable(allw, showHost)}` : ""}</div>`;
}

// 任务卡里的占用时间线（common.js 的时间线引擎）：每台主机每张卡一行，有纯 CPU 任务的主机在卡行下方加一行 CPU，按项目配色。已跑的是工作台存的用量记录，往后是 worker 推演（overview.occupancy）。
// TLH 是 tasksCard 定的这次画哪些主机；render 之后调
const isCpuOcc = x => !x.g || !x.g.length;
let TLH = null;
tlInit({get: (k, d) => ST.get("ov" + k, d), set: (k, v) => ST.set("ov" + k, v), now: () => D.generated, draw: () => drawTimeline(),
  all: () => { const hs = TLH?.hs || [], xs = hs.flatMap(h => D.occ?.[h.id] || []);     // 「全部」也包住租期到期，看排队跑不跑得完
    return tlSpan(xs.map(x => x.t0), [...xs.map(x => x.t1), ...hs.map(h => h.lease_end).filter(Boolean)]); },
  near: () => (TLH?.hs || []).flatMap(h => D.occ?.[h.id] || []).map(x => [x.t0, x.t1, x.st === "run" || x.st === "plan"])});
function drawTimeline() {
  const box = document.getElementById("tlbox"); if (!box?.clientWidth || !TLH) return;
  tlBind(box); tlFrame();
  const {t0, t1} = tlRange(), rows = [], used = new Set();
  const hostRows = TLH.hs.map(h => {
    const oc = D.occ?.[h.id] || [], gs = h.status?.info?.gpus || [];
    const cpu = oc.filter(isCpuOcc).map(x => ({...x}));     // 纯 CPU 任务（#207）：有才动态追加 CPU 行，无则不画
    return {h, oc, gs, cpu, n: gs.length + (cpu.length ? 1 : 0)};
  });
  const sep = TLH.hs.length > 1 && hostRows.some(r => r.n > 1);     // 有主机不止一行（多卡或含 CPU 行）时，每台的第一行上面画分隔线
  for (const {h, oc, gs, cpu} of hostRows) {
    for (const [k, g] of gs.entries()) {
      const xs = oc.filter(x => x.g?.includes(g.i)).map(x => ({...x}));     // 多卡任务每张卡各画一条，各自分道
      xs.forEach(x => { if (x.t1 > t0 && x.t0 < t1) used.add(x.p); });
      const lay = tlRowLayout(tlPack(xs), OCC_CARD_LAY);
      rows.push({key: `card:${h.id}:${g.i}`, h: lay.h, sep: sep && !k, draw: (y, hh, c) => cardRow(h, g, xs, lay, y, hh, c)});
    }
    if (cpu.length) {
      cpu.forEach(x => { if (x.t1 > t0 && x.t0 < t1) used.add(x.p); });
      const lay = tlRowLayout(tlPack(cpu), OCC_CARD_LAY);
      rows.push({key: `cpu:${h.id}`, h: lay.h, sep: sep && !gs.length, draw: (y, hh, c) => cardRow(h, null, cpu, lay, y, hh, c)});
    }
  }
  const marks = [...new Map(D.projects.flatMap(p => p.markers || []).map(m => [m.label + m.ts, m])).values()];
  tlShow(box, rows, {markers: marks, items: () => [...occItems(color([...used][0])),
    ["lease", "租期到期", tlSw(`<line x1="6" x2="6" y1="0" y2="12" stroke="var(--bad)" stroke-dasharray="3 2" stroke-width="1.5"/>`, 12)]]});
  const lg = document.getElementById("tllegend");
  if (lg) lg.innerHTML = [...used].map(p => projName(p, {always: true})).join(" &nbsp;");     // 只列这段时间图上有的
}
function cardRow(h, g, xs, lay, y, hh, c) {
  const info = h.status?.info, q = info?.cpu_quota || info?.nproc;
  const rowTip = g ? `${h.label} GPU ${g.i}：现在 ${mb2g(g.used)} / ${mb2g(g.total)} GiB`
    : `${h.label} 纯 CPU 任务${info?.cpu_used != null && q ? `：现在 CPU ${g1(info.cpu_used)} / ${g1(q)} 核` : ""}`;
  const lab = (TLH.showHost ? hostShort(h) + " " : "") + (g ? "卡 " + g.i : "CPU");
  let s = `<text x="0" y="${y + hh / 2 + 4}" ${tip(rowTip)}>${esc(fit(lab, c.L - 6))}</text>`;
  s += `<rect x="${c.L}" y="${y + hh / 2 - .5}" width="${c.PW}" height="1" fill="var(--line)"/>`;
  s += occBars(xs, y, hh, c, {layout: lay, color, label: x => x.n,
    text: x => `${projName(x.p, {always: true, plain: true})}${x.lane ? x.lane + " · " : ""}${x.n}\n${occState(x)} · ${ts(x.t0)} → ${ts(x.t1)}（${dur(x.t1 - x.t0)}）· ${x.g?.length ? "卡 " + x.g.join(",") : "纯 CPU"}`
      + (x.after ? `\n等 ${afterText(x.after)} 跑完才轮到` : "")});
  if (h.lease_end >= c.t0 && h.lease_end <= c.t1) {
    c.seen.add("lease");
    s += `<line x1="${c.X(h.lease_end)}" x2="${c.X(h.lease_end)}" y1="${y}" y2="${y + hh}" stroke="var(--bad)" stroke-dasharray="3 2" stroke-width="1.5"/>
      <rect x="${c.X(h.lease_end) - 4}" y="${y}" width="8" height="${hh}" fill="transparent" ${tip(`${hostShort(h)} 租期到 ${ts(h.lease_end)}`)}/>`;
  }
  return s;
}

// 记录卡：最近跑完、事件、临时指令等平时不看的，合成一张，默认收成一行；点哪项展开哪项，再点收起
function recCard(key, tabs) {
  const open = !!ST.get("cardOpen", {})["rec:" + key], cur = tabs.find(t => t.id === ST.get("recTab", {})[key]) || tabs[0];
  return `<details class="card fold" data-fold="rec:${key}" ${open ? "open" : ""}><summary class="chd" style="flex-wrap:wrap">${icon("chev")}<b>记录</b>
    ${tabs.map(t => `<span class="rtab ${open && t === cur ? "on" : ""}" data-rtab="${t.id}" data-rkey="${key}" role="button">${t.name}<span class="mut">${t.note}</span></span>`).join("")}</summary>${open ? cur.body() : ""}</details>`;
}

/* ---------- 表格（总览与主机页共用） ---------- */
const OPEN = new Set(ST.get("open", []));           // 展开了详情的任务
const jobKey = j => [j.h?.id, j.root, j.queue, j.name].join("|");

function etaOf(j) {
  // 先用项目看板按日志算的进度；没有就用 worker 推演里这条在跑的（plan 的 run 条）；再按 worker 给这条清单行的预计时长（状态 queues[].dur，
  // 同类历史、实际/估时比值，#140）；都没有才按清单估时
  const p = j.prog || {}, t0 = j.t0, at = (ts, src) => ({ts, rem: ts - D.generated, src});
  if (p.eta_ts) return at(p.eta_ts, p.eta_source || "项目看板");
  const r = planLanes(j.h || {}).flatMap(l => l.jobs).find(x => x.run && x.name === j.name);
  if (r?.t1) return at(r.t1, "worker 推演");
  const d = ((j.h?.status?.queues || []).find(q => q.root === j.root && q.jobs === j.queue)?.dur || {})[j.name];
  if (d?.min && t0) return at(t0 + d.min * 60, d.src || "worker 预计时长");
  if (j.est_min && t0) return at(t0 + j.est_min * 60, "清单估时");
  return null;
}

function jobsTable(rows, showHost) {
  if (!rows.length) return `<div class="empty">没有任务在跑。</div>`;
  const n = showHost ? 10 : 9;
  return `<div class="scroll"><table><tr><th>项目</th><th>任务</th>${showHost ? "<th>主机</th>" : ""}<th>进度</th><th>预计结束</th><th class="r">已运行</th><th>GPU</th>
    <th class="r">显存 GiB</th><th class="r">内存 GiB</th><th class="r">核</th></tr>` + rows.map(j => {
    const h = j.h, idx = Object.fromEntries((h?.status?.info?.gpus || []).map(g => [g.uuid, g.i]));
    const u = j.use || {}, t0 = j.t0, wg = u.with_gpu || [], key = jobKey(j), open = OPEN.has(key);
    const ex = j.excl ? (wg.length ? `<span class="badge b-bad" ${tip("独占任务运行期间同卡出现过：" + wg.join("、"))}>独占被打扰</span>`
                                   : `<span class="badge b-ok">独占${j.excl === "host" ? "整机" : "卡"}</span>`) : "";
    const old = j.kind === "ephemeral" ? ""
      : j.src !== "worker" ? `<span class="badge b-mute" ${tip("旧 gpu_queue.sh 在跑：只统计用量，不归 worker 管")}>旧队列</span>`
      : j.kind === "slot" ? `<span class="badge b-mute" ${tip("项目自己的监督进程申请的单元（labmon_worker.py slot）")}>单元</span>` : "";
    const pr = j.prog?.progress, e = etaOf(j);
    const prog = pr ? `<div class="prog"><div class="bar"><i style="width:${100 * pr[0] / pr[1]}%;background:${color(j.project)}"></i></div><span class="sm num nw">${pr[0]}/${pr[1]}</span></div>`
      : e && t0 ? `<div class="prog" ${tip("没有进度行，按估时画")}><div class="bar"><i class="hatch" style="width:${Math.min(100, 100 * (D.generated - t0) / (e.ts - t0))}%;background:${color(j.project)}"></i></div><span class="sm mut nw">估</span></div>` : `<span class="mut">—</span>`;
    const eta = e ? `<span class="nw" ${tip("依据：" + e.src)}>${e.rem < 0 ? `<span style="color:var(--warn)">超时 ${dur(-e.rem)}</span>` : `剩 ${dur(e.rem)} · ${tsHM(e.ts)}`}</span>` : `<span class="mut">—</span>`;
    let tr = `<tr class="link job ${open ? "open" : ""}" tabindex="0" aria-expanded="${open}" data-job="${esc(key)}"><td>${projCell(j)}</td><td><span class="nw">${esc(j.name)}</span> ${old}${ex}</td>
      ${showHost ? `<td class="nw"><a href="#/host/${encodeURIComponent(h.id)}">${esc(hostShort(h))}</a></td>` : ""}
      <td>${prog}</td><td class="sm">${eta}</td><td class="r">${t0 ? dur(D.generated - t0) : "—"}</td><td class="sm">${(j.gpus || []).map(x => idx[x] ?? "?").join(",") || "—"}</td>
      <td class="r">${mb2g(u.vram)} <span class="mut">/ ${mb2g(u.vram_peak)}</span></td>
      <td class="r">${g1(u.ram)} <span class="mut">/ ${g1(u.ram_peak)}</span></td><td class="r">${g1(u.cores)}</td></tr>`;
    if (open) tr += `<tr class="detail"><td colspan="${n}">${jobDetail(j)}</td></tr>`;
    return tr;
  }).join("") + `</table></div>`;
}

// 任务的 CPU 与读写（运行中的 use、跑完的用量行同一套字段）：峰值是 1 分钟窗口的；读写是 read/write 的量，共享盘与命中页缓存的都算
const ioText = x => [x.cores_peak != null ? `CPU 峰值 ${g1(x.cores_peak)} 核` : null,
  x.rd_b != null ? `读 ${G(x.rd_b)}${x.rd_peak != null ? `（峰值 ${g1(x.rd_peak)} MiB/s）` : ""}、写 ${G(x.wr_b)}${x.wr_peak != null ? `（峰值 ${g1(x.wr_peak)} MiB/s）` : ""}` : null]
  .filter(Boolean).join("；");
// 清单的 cpus=、io= 提示（运行中、等待中的任务带着）
const hintText = x => [x.cpus != null ? `cpus=${x.cpus}` : null, x.io ? `io=${x.io}` : null].filter(Boolean).join(" ");
const HINT_NOTE = "cpus= 进 CPU 记账，io= 只记录";
// 内存估计的来源（#61 起 src 只指显存）。worker 接管的运行中任务沿用放行时的 est，#61 之前放行的没有 ram_src（那时内存与显存同一来源），
// 跑完就没了：11-23 后可删回退，直接取 e.ram_src
const ramSrc = e => e.ram_src || e.src;
// 任务的资源估计一句（等待项与运行中任务的 est；旧 worker 没有 cpus）：内存与显存来源相同时只写一次
const estText = e => `${ramSrc(e) === e.src ? `内存 ${g1(e.ram)} GiB、每卡显存 ${g1(e.vram)} GiB（${e.src}）` : `内存 ${g1(e.ram)} GiB（${ramSrc(e)}）、每卡显存 ${g1(e.vram)} GiB（${e.src}）`}${e.cpus != null ? `、CPU ${e.cpus} 核（${e.cpu_src}）` : ""}`;

function jobDetail(j) {
  const u = j.use || {}, p = j.prog || {}, wg = u.with_gpu || [], wh = u.with_host || [], e = etaOf(j);
  const kv = [
    ["命令", j.cmd ? `<code>${esc(j.cmd)}</code>` : `<span class="mut">（没有记录）</span>`],
    j.kind === "ephemeral" ? ["来源", `${esc(j.project)} 的临时主机上报`] : j.kind === "slot" ? ["来源", `${esc(j.project)} 的单元申请`] : ["队列", `<code>${esc(j.root)}/${esc(j.queue)}</code>${j.src === "worker" ? "" : `<span class="mut">（旧 gpu_queue.sh）</span>`}`],
    ...(j.artifact_dir ? [["产物目录", `<code>${esc(j.artifact_dir)}</code>`]] : []),
    ["日志", j.log ? `<code>${j.root ? esc(j.root) + "/" : ""}${esc(j.log)}</code>` : "—"],
    j.kind === "ephemeral" ? ["开始", ts(j.t0)] : ["进程", `PID ${esc(j.pid)}，${u.nproc ?? "?"} 个进程，开始于 ${ts(j.t0)}`],
    // 有进度行、还算不出速度（进度刚开始、刚换段、这段不打进度）写“暂无速度”，没有进度行才写“没有进度行”（#17 维护者定）
    ["速度", p.rate_per_min ? `${g1(p.rate_per_min)} 步/分（预计结束依据：${esc(p.eta_source || "—")}）`
      : e ? `${p.progress ? `暂无速度（进度 ${esc(p.progress[0])}/${esc(p.progress[1])}）` : "没有进度行"}，预计结束依据：${esc(e.src)}` : "—"],
    ["同卡", wg.length ? esc(wg.join("、")) : "无"], ["同机", wh.length ? esc(wh.join("、")) : "无"],
  ];
  if (j.est) kv.push(["记账", `内存 ${g1(j.acct?.ram)} GiB、显存 ${mb2g(j.acct?.vram)} GiB${j.acct?.cpu != null ? `、CPU ${g1(j.acct.cpu)} 核` : ""}（内存、显存 = max(估计值, 实测)；CPU 开跑满 10 分钟后按近 10 分钟窗口核数回收；估计 ${esc(estText(j.est))}）`]);
  if (j.tag || j.stop) kv.push(["tag / 停法", `${esc(j.tag || "—")} / ${esc(j.stop || "—")}`]);
  if (u.rd != null || u.cores_peak != null) kv.push(["CPU 与读写", [`此刻 ${g1(u.cores)} 核、读 ${g1(u.rd)} / 写 ${g1(u.wr)} MiB/s`, ioText(u)].filter(Boolean).join("；")]);
  if (hintText(j)) kv.push(["资源提示", `${esc(hintText(j))} <span class="mut">（${HINT_NOTE}）</span>`]);
  return `<div class="kv">${kv.map(([k, v]) => `<span>${k}</span><span>${v}</span>`).join("")}</div>`
    + (j.tail?.length ? `<pre class="tail">${esc(j.tail.join("\n"))}</pre>` : `<div class="empty">没有日志末尾。</div>`)
    + (visibleProject(j.link) ? `<div class="sm" style="margin-top:6px"><a href="#/p/${encodeURIComponent(j.link)}">在项目页看 →</a></div>` : "");
}

function waitingTable(w, showHost) {
  // 两种：任务在等资源（有任务名），整条队列受阻（被旧 gpu_queue 占着、暂停、目录不在）
  // 同一台主机按 worker 的排位（admit）排，被临时指令挡住的（没有排位）放后面
  const hi = x => D.hosts.findIndex(h => h.id === x.h?.id), jobs = w.filter(x => x.name).sort((a, b) => hi(a) - hi(b) || (a.pos?.n ?? 1e9) - (b.pos?.n ?? 1e9));
  const qs = w.filter(x => !x.name);
  if (!w.length) return `<div class="empty">没有在等的任务。</div>`;
  let s = jobs.length ? `<div class="scroll"><table><tr><th ${tip("worker 这一轮放行时排的顺序（同一台主机上），悬停看为什么")}>排位</th><th>项目</th><th>任务</th>${showHost ? "<th>主机</th>" : ""}<th>在等什么</th><th class="r">已等</th><th ${tip("worker 推演（同机各项目按同一套规则一起排）里预计何时开始")}>预计开始</th></tr>` + jobs.map(x => `<tr>
    <td class="sm nw" ${tip(rankTip(x, jobs))}>${x.pos ? `第 ${x.pos.n}${x.pos.barrier ? ` <span class="badge b-warn">屏障</span>` : ""}` : `<span class="mut">—</span>`}</td><td>${projCell(x)}</td>
    <td ${x.est ? tip(`估计：${estText(x.est)}${hintText(x) ? `\n提示：${hintText(x)}（${HINT_NOTE}）` : ""}`) : ""}>${esc(x.name)}${x.kind === "slot" ? ` <span class="badge b-mute">单元</span>` : ""}${x.excl ? ` <span class="badge b-warn">独占</span>` : ""}</td>${showHost ? `<td class="nw">${esc(hostShort(x.h))}</td>` : ""}
    <td class="sm mut">${esc(x.why)}</td><td class="r sm">${x.since ? dur(D.generated - x.since) : "—"}</td>
    <td class="sm nw" ${x.start ? "" : tip(x.pos || x.dep ? `推演里排不上${stuckWhy(x) ? "：" + stuckWhy(x) : ""}` : "被临时指令挡住的不推演")}>${x.start ? (x.start <= D.generated + 60 ? "马上" : ts(x.start)) : `<span class="mut">—</span>`}</td></tr>`).join("") + `</table></div>`
    : `<div class="empty">没有任务在等资源。</div>`;
  if (qs.length) s += `<div class="sub">队列受阻</div><div class="scroll"><table><tr><th>项目</th><th>队列</th>${showHost ? "<th>主机</th>" : ""}<th>原因</th></tr>` + qs.map(x => `<tr><td>${projCell(x)}</td>
    <td class="sm">${esc(x.queue.replace(/^jobs\//, ""))}</td>${showHost ? `<td class="nw">${esc(hostShort(x.h))}</td>` : ""}<td class="sm mut">${esc(x.why)}</td></tr>`).join("") + `</table></div>`;
  return s;
}

function doneTable(d, showHost) {
  if (!d.length) return `<div class="empty">还没有记录（worker 升级后跑完的任务才有）。</div>`;
  return `<div class="scroll"><table><tr><th>结束</th><th>项目</th><th>任务</th>${showHost ? "<th>主机</th>" : ""}<th class="r">用时</th><th>结果</th>
    <th class="r">显存峰值</th><th class="r">内存峰值</th><th class="r">平均核</th><th>同卡 / 同机</th></tr>` + d.map(r => {
    const wg = r.with_gpu || [], wh = r.with_host || [];
    const alone = !wg.length && !wh.length ? `<span class="badge b-ok">独自运行</span>`
      : `<span class="sm" ${tip("同卡：" + (wg.join("、") || "无") + "\n同机：" + (wh.join("、") || "无"))}>${wg.length ? "同卡 " + wg.length : "卡上独自"} · 同机 ${wh.length}</span>`;
    return `<tr><td class="sm nw">${ts(r.t1)}</td><td>${projCell(r)}</td><td>${esc(r.name)}${r.excl ? ` <span class="badge b-mute">独占</span>` : ""}</td>
      ${showHost ? `<td class="nw">${esc(hostShort(r.h))}</td>` : ""}<td class="r">${r.min} 分钟</td>
      <td>${r.kind === "slot" ? `<span class="badge b-mute">单元结束</span>` : r.rc === "0" ? `<span class="badge b-ok">完成</span>` : `<span class="badge b-bad">失败 ${esc(r.rc ?? "")}</span>`}</td>
      <td class="r">${mb2g(r.vram_peak)}</td><td class="r">${g1(r.ram_peak)}</td><td class="r" ${ioText(r) ? tip(ioText(r)) : ""}>${g1(r.cores_mean)}</td><td>${alone}</td></tr>`;
  }).join("") + `</table></div>`;
}

/* ---------- 主机页 ---------- */
const seg = (w, bg, t, label = "", cls = "") => `<i class="${cls}" style="width:${w}%;background:${bg}" ${tip(t)}>${label && w >= 12 ? `<span>${esc(label)}</span>` : ""}</i>`;
// 该卡此刻是否因外部（如宿主机）活跃占卡暂停放新任务（仅读 worker 写进 room.unacct 的判定，页面不另写门槛）
const unacctPaused = (h, g) => (h.status?.room?.unacct || []).includes(g.uuid);

function gpuSegs(h, g) {
  // 各任务实测（够宽就写任务名）＋记账还没占上的预留（斜纹淡色）＋labmon 之外的进程＋不可见的外部占卡显存差额
  const procs = h.status.gpu_procs || [], jobs = jobsOf(h), mine = procs.filter(p => p.uuid === g.uuid);
  let s = "";
  for (const j of jobs) {
    const ps = mine.filter(p => p.owner === "job" && p.job === j.name), mb = ps.reduce((a, p) => a + p.mb, 0);
    if (!ps.length && !(j.gpus || []).includes(g.uuid)) continue;
    if (mb) s += seg(100 * mb / g.total, color(j.project), `${j.project} · ${j.name}\n${mb2g(mb)} GiB（PID ${ps.map(p => p.pid).join(",")}，宿主机 ${ps.map(p => p.hpid).join(",")}）`, j.name);
    const resv = j.acct ? j.acct.vram / Math.max(1, (j.gpus || []).length) - mb : 0;
    if (resv > 256) s += seg(100 * resv / g.total, color(j.project), `${j.name}：记账还没占上的 ${mb2g(resv)} GiB\n（按估计值预留：${j.est?.src || ""}）`, "", "resv");
  }
  for (const p of mine.filter(p => p.owner !== "job")) {
    const lab = p.owner === "keepalive" ? "GPU 保活" : `labmon 之外的进程 ${p.comm || ""}（宿主机 PID ${p.hpid}）`;
    s += seg(100 * p.mb / g.total, p.owner === "keepalive" ? "var(--faint)" : "var(--muted)", `${lab}\n${mb2g(p.mb)} GiB`, p.owner === "keepalive" ? "保活" : p.comm || "", "hatch");
  }
  const rest = g.used - mine.reduce((a, p) => a + p.mb, 0);
  const up = unacctPaused(h, g), umb = g.unacct_mb || 0;
  if (up || umb >= 512 || g.unacct_busy) {
    const show = umb > 0 ? umb : Math.max(0, rest);
    if (show > 0) {
      const desc = up
        ? `外部（如宿主机）占卡暂停放行（约 ${mb2g(show)} GiB，本环境不可见其进程）${g.unacct_why ? `\n${g.unacct_why}` : ""}\n默认整卡暂停放新任务且不杀已跑任务，释放后自动恢复`
        : `外部（如宿主机）未归因显存约 ${mb2g(show)} GiB（本环境不可见其进程）\n已从可放显存中扣减${g.unacct_busy ? "（已开启允许与外部共享 GPU）" : ""}`;
      s += seg(100 * show / g.total, "var(--warn)", desc, "外部占卡", "hatch");
    }
  } else if (rest > 256) {
    const restTip = g.backend === "mps"
      ? `系统与其他进程统一内存占用约 ${mb2g(rest)} GiB（按统一内存保守估算）`
      : `没有对应进程的显存 ${mb2g(rest)} GiB（驱动与上下文开销等）`;
    s += seg(100 * rest / g.total, "var(--line)", restTip);
  }
  return s;
}

function memSegs(h) {
  const m = h.status.info.mem, jobs = jobsOf(h).filter(j => j.use && (j.use.ram || j.acct));
  let s = "";
  for (const j of jobs) {
    const r = j.use.ram || 0;
    if (r) s += seg(100 * r / m.limit, color(j.project), `${j.project} · ${j.name}\n${g1(r)} GiB（峰值 ${g1(j.use.ram_peak)}，${j.use.nproc} 个进程，按 PSS 计）`, j.name);
    const resv = j.acct ? j.acct.ram - r : 0;
    if (resv > 0.2) s += seg(100 * resv / m.limit, color(j.project), `${j.name}：记账还没占上的 ${g1(resv)} GiB\n（按估计值预留：${j.est ? ramSrc(j.est) : ""}）`, "", "resv");
  }
  const anon = m.anon ?? m.used, rest = anon - jobs.reduce((a, j) => a + (j.use.ram || 0), 0);
  if (rest > 0.2) s += seg(100 * rest / m.limit, "var(--muted)", `其他进程与内核占用 ${g1(rest)} GiB`, "", "hatch");
  if (m.used - anon > 0.2) s += seg(100 * (m.used - anon) / m.limit, "var(--line)", `页缓存 ${g1(m.used - anon)} GiB\n读写文件留下的，内核要内存时先回收；放行与内存闸门不算它`);
  return s;
}

// 小曲线：每种资源一条；鼠标移上去，同组（同一台主机）的小曲线一起显示十字线，提示里给那一刻的全部数值
const SPKG = {};
let RANGE = ST.get("range", 24);                  // 小曲线看近 6 / 24 小时
function spark(grp, t0, t1, pts, {lo = 0, hi = 100, ref = null, gap = 600} = {}) {
  const W = 200, H = 22, X = t => (W * (t - t0) / Math.max(1, t1 - t0)).toFixed(1);
  const Y = v => (H - 1.5 - (H - 3) * (Math.max(lo, Math.min(hi, v)) - lo) / Math.max(1e-9, hi - lo)).toFixed(1);
  let d = "", prev = null;
  for (const [t, v] of pts) {                     // 值缺失、或相邻两点隔太久（本机睡眠、看板停了）就断开
    if (v == null) { prev = null; continue; }
    d += `${prev != null && t - prev <= gap ? "L" : "M"}${X(t)},${Y(v)}`;
    prev = t;
  }
  const ns = `vector-effect="non-scaling-stroke"`;
  return `<svg class="spark" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" data-grp="${esc(grp)}" aria-hidden="true">
    <line x1="0" x2="${W}" y1="${H - 0.5}" y2="${H - 0.5}" stroke="var(--line)" ${ns}/>
    ${ref != null ? `<line x1="0" x2="${W}" y1="${Y(ref)}" y2="${Y(ref)}" stroke="var(--bad)" stroke-dasharray="3 3" opacity=".55" ${ns}/>` : ""}
    <path d="${d}" fill="none" stroke="var(--accent)" stroke-width="1.5" stroke-linejoin="round" ${ns}/>
    <line class="xh" x1="0" x2="0" y1="0" y2="${H}" stroke="var(--muted)" ${ns} visibility="hidden"/></svg>`;
}
const nearest = (rows, t, k = r => r.t) => rows.reduce((b, r) => !b || Math.abs(k(r) - t) < Math.abs(k(b) - t) ? r : b, null);

function resCard(h) {
  const st = h.status, info = st.info, now = D.generated, t0 = now - RANGE * 3600;
  const rows = (h.series || []).filter(r => r.t >= t0), grp = "h:" + h.id, gs = info.gpus || [];
  SPKG[grp] = {t0, t1: now, at: t => { const r = nearest(rows, t); return r ? `${ts(r.t)}\n内存 ${g1(r.mem)} / ${g1(r.lim)} GiB${r.anon != null ? `（收不回的 ${g1(r.anon)}）` : ""}`
    + (r.gpu || []).map((g, k) => `\nGPU ${gs[k]?.i ?? k} 显存 ${mb2g(g[0])} / ${mb2g(g[1])} GiB · 近 10 分钟利用率 ${pctText(g[3])}`).join("")
    + (r.cpu != null ? `\nCPU ${g1(r.cpu)} 核${r.thr ? `，限流 ${g1(r.thr)}%` : ""}` : "")
    + (r.nfs != null ? `\n共享盘 ${g1(r.nfs)} ms/次` : "") + (r.busy != null ? `\n本地盘忙 ${Math.round(r.busy)}%` : "") : ""; }};
  const sp = (f, opt) => rows.length > 1 ? spark(grp, t0, now, rows.map(r => [r.t, f(r)]), opt) : `<span>数据还不够</span>`;
  const row = (l, bar, v, spk, lt = "") => `<span class="l" ${lt ? tip(lt) : ""}>${l}</span>${bar}<span class="v">${v}</span><div class="spk">${spk}</div>`;
  const big = s => `<div class="bar big" style="overflow:hidden">${s}</div>`;
  let s = "";
  gs.forEach((g, k) => {
    const isMps = g.backend === "mps", room = st.room?.gpus?.[g.uuid], nm = gs.length > 1 ? `GPU ${g.i} ` : "GPU ";
    const up = unacctPaused(h, g), umb = g.unacct_mb || 0;
    const uNote = isMps
      ? ` <span class="badge b-mute" ${tip("Apple Silicon MPS 逻辑卡：按统一内存保守估算容量，默认单卡互斥")}>MPS</span>`
        + (room != null ? `<span class="mut" ${tip("按统一内存与记账还能放")}>可放 ${mb2g(room)}</span>` : "")
      : up
      ? ` <span class="badge b-warn" ${tip(`外部（如宿主机）占卡暂停放行${umb ? `（约 ${mb2g(umb)} GiB）` : ""}${g.unacct_why ? `：${g.unacct_why}` : ""}\n不报异常、不杀已跑任务，释放后自动恢复`)}>外部占卡暂停放行</span>`
      : (room != null ? `<span class="mut" ${tip("按记账还能放（空闲 − 运行中任务还没占上的预留）")}>可放 ${mb2g(room)}</span>` : "")
        + (umb >= 512 ? `<span class="mut" ${tip(`外部（如宿主机）未归因显存约 ${mb2g(umb)} GiB，已从可放显存扣减${g.unacct_busy ? "（已开启允许与外部共享 GPU）" : ""}`)}>外部占 ${mb2g(umb)} GiB</span>` : "");
    s += row(nm + (isMps ? "显存（MPS）" : "显存"), big(gpuSegs(h, g)), `${mb2g(g.used)} / ${mb2g(g.total)} GiB${uNote}`,
      sp(r => r.gpu?.[k] ? 100 * r.gpu[k][0] / r.gpu[k][1] : null), `GPU ${g.i}：${g.name || ""}${isMps ? "\nApple Silicon 统一内存保守估算，默认单卡互斥" : ""}`);
    const U = utilText(gpuBusy(h, g.i), g.util);      // 吃满程度（只有保活时按 0，#51）：条与数是近 1 小时平均，小曲线是近 10 分钟平均（series 卡的第 4 项）
    if (isMps) {
      s += row(nm + "利用率", big(""), `—<span class="mut">MPS 不采集实时利用率</span>`, `<span class="sm mut">—</span>`,
        `${g.name || "Apple Silicon (MPS)"}\n不依赖 Metal 实时利用率采集，不误报 GPU 空转`);
    } else {
      s += row(nm + "利用率", big(`<i style="width:${U.v ?? 0}%;background:var(--accent);opacity:.6"></i>`), `${U.big}${U.sub ? `<span class="mut">${esc(U.sub)}</span>` : ""}`,
        sp(r => r.gpu?.[k]?.[3]), `${U.span}平均\n${U.tip}\n小曲线是近 10 分钟平均`);
    }
  });
  const m = info.mem;
  if (m) {
    const extra = [m.peak ? `启动以来峰值 ${g1(m.peak)} GiB` : null, m.failcnt ? `撞上限 ${m.failcnt} 次` : null, m.oom_kill ? `OOM 杀进程 ${m.oom_kill} 次` : null].filter(Boolean);
    const mLbl = m.src === "cgroup" ? "内存（容器）" : m.src === "vm_stat" ? "内存（macOS）" : "内存";
    s += row(mLbl, `<div class="bar big" style="overflow:hidden">${memSegs(h)}<b style="left:90%" ${tip("90%：内存闸门，收不回的内存超过就停止放行新任务")}></b></div>`,
      `${g1(m.used)} / ${g1(m.limit)} GiB${m.anon != null ? `<span class="mut" ${tip("收不回的内存 = 用量 − 页缓存：匿名页、共享内存（/dev/shm、tmpfs）与内核占用；放行与内存闸门看它")}>收不回 ${g1(m.anon)}</span>` : ""}${st.room?.ram != null ? `<span class="mut" ${tip("可放 = 上限 − labmon 之外收不回的内存 − 任务记账")}>可放 ${g1(st.room.ram)}</span>` : ""}${extra.filter(e => !e.startsWith("启动")).length ? ` <span style="color:var(--bad)">${esc(extra.filter(e => !e.startsWith("启动")).join("，"))}</span>` : ""}`,
      sp(r => r.lim ? 100 * (r.anon ?? r.mem) / r.lim : null, {ref: 90}), [m.src === "vm_stat" ? "macOS 统一内存（vm_stat + sysctl hw.memsize）" : "工作集 = 用量 − 不活跃页缓存（与 cgroup 判定 OOM 的口径一致）", "曲线是收不回的内存（旧记录没有时是工作集），红线 90% 是内存闸门", ...extra].join("\n"));
  }
  const pr = info.press || {}, PSI = {cpu: "CPU", io: "读写", memory: "内存"};
  const psiT = k => pr.psi?.[k] != null ? `\nPSI：近 60 秒有任务在等${PSI[k]}的时间占 ${g1(pr.psi[k])}%` : "";
  const q = info.cpu_quota || info.nproc;
  if (info.cpu_used != null && q) {
    const js = jobsOf(h), acct = js.reduce((a, j) => a + (j.acct?.cpu || 0), 0), rc = st.room?.cpu;
    const ov = Object.fromEntries((st.throttle?.over || []).map(o => [o.project + "|" + o.name, o]));   // 限流中超核的任务（#39）
    const cs = js.map(j => (j.use?.cores ? seg(100 * j.use.cores / q, color(j.project), `${j.project} · ${j.name}：${g1(j.use.cores)} 核${ov[j.project + "|" + j.name] ? `\n超核：给 ${ov[j.project + "|" + j.name].give} 核，1 分钟窗口实测 ${g1(ov[j.project + "|" + j.name].cores)} 核` : ""}`, j.name) : "")
      + (j.acct?.cpu - (j.use?.cores || 0) > 0.05 ? seg(100 * (j.acct.cpu - (j.use?.cores || 0)) / q, color(j.project), `${j.name}：记账还没占上的 ${g1(j.acct.cpu - (j.use?.cores || 0))} 核\n（开跑 10 分钟内按估计值预留：${j.est?.cpu_src || ""}；满 10 分钟后按近 10 分钟窗口核数）`, "", "resv") : "")).join("");
    const cpuHeadTip = info.os === "darwin"
      ? `macOS 本机共 ${info.nproc ?? "—"} 核；当前配额 ${info.cpu_quota ? g1(info.cpu_quota) + " 核" : "未设"}${info.cpu_quota_src === "macos_default" ? "（默认留 2 核给桌面，可在主机配置中调整 cpu_quota）" : ""}`
      : `容器里 nproc 显示 ${info.nproc ?? "—"} 个逻辑核，那是宿主机的；配额${info.cpu_quota ? " " + g1(info.cpu_quota) + " 核" : "读不到，不记 CPU 账"}`;
    s += row("CPU", big(cs + seg(Math.max(0, 100 * (info.cpu_used - js.reduce((a, j) => a + (j.use?.cores || 0), 0)) / q), "var(--muted)", "其他进程", "", "hatch")),
      `${g1(info.cpu_used)} / ${g1(q)} 核${st.room?.cpu_cap != null ? `<span class="mut" ${tip("记账 = 各任务记的核数合计：开跑后 10 分钟内取 max(估计核数（封顶到上限）, 实测核数)，满 10 分钟后估计那份换成近 10 分钟窗口核数的最大值（不超过开跑时给的核数、不低于 1）再与实测取大；上限是配额向下取整")}>记账 ${g1(acct)} / ${st.room.cpu_cap}</span>` : ""}${rc != null ? `<span class="mut" ${tip("可放 = 配额 − labmon 之外的（含旧 gpu_queue.sh 的任务）− 任务记账")}>可放 ${g1(Math.max(0, rc))}</span>` : ""}${thrTag(h, "mut")}`,
      sp(r => r.cpu != null ? 100 * r.cpu / q : null),
      `${cpuHeadTip}\n放行按 CPU 记账：任务要的核数取清单的 cpus=，否则同类近 5 次峰值 ×1.1，都没有按 1 核；放不下的等着，原因写「CPU 不够」\n任务里的 $LABMON_CPUS 是开跑时给它记的核数（估计封顶到上限），不随之后的回收变；没设的 OMP/MKL/OPENBLAS 线程数缺省同它${psiT("cpu")}`);
  }
  const x = (h.ephemeral || h.standalone) ? null : sharedParts();
  if (h.ephemeral) {
    for (const d of h.storage || []) {
      const used = Math.max(0, (d.total_gb || 0) - (d.avail_gb || 0));
      const pctUsed = d.total_gb > 0 ? Math.min(100, 100 * used / d.total_gb) : 0;
      const low = d.kind === "cloud" && d.avail_gb < 1;
      const kindTag = d.kind === "cloud" ? "云盘" : "本地盘";
      s += row(`${esc(d.label)} <span class="mut sm">[${kindTag}]</span>`,
        big(`<i style="width:${pctUsed}%;background:var(${low ? "--bad" : "--accent"});opacity:.65"></i>`),
        `<span style="${low ? "color:var(--bad);font-weight:600" : ""}">剩 ${g1(d.avail_gb)} / ${g1(d.total_gb)} GiB</span>`,
        `<span class="sm mut">${d.kind === "cloud" ? "不计入实验室共享盘" : "实例回收清空"}</span>`,
        `${d.path}\n${d.kind === "cloud" ? "云盘存储（不计入实验室共享盘；剩余低于 1 GiB 标红告警）" : "实例本地高速盘（回收后清空）"}`);
    }
  } else if (h.standalone && h.work_disk) {
    const wd = h.work_disk, recN = (wd.reclaimable || []).length, gOn = !!(wd.disk_gate?.on || h.status?.disk_gate?.on);
    const badge = gOn ? ` <span class="badge b-bad">已暂停放新任务</span>` : recN ? ` <span class="badge b-warn">${recN} 处可回收</span>` : "";
    s += row("本地工作盘", workDiskBar(wd, false),
      `<span style="${wd.low || gOn ? "color:var(--bad)" : ""}">剩 ${Math.round((wd.free || 0) / GIB)} / ${Math.round((wd.total || 0) / GIB)} GiB</span><span class="mut">工作区 ${wd.work_b != null ? GB(wd.work_b) + " GiB" : "待扫描"}${wd.quota_gb ? ` / 配额 ${wd.quota_gb}` : ""}</span>${badge}`,
      `<span class="sm mut">持久保存</span>`,
      `${wd.root}：独立主机本地工作盘（持久保存，不会随续租清空）；低于 ${wd.warn_gb} GiB 预警，低于 ${wd.stop_gb} GiB 暂停放新任务\n目录明细与可回收列表见下方「记录 · 本地工作盘」`);
  } else if (x) {
    const pts = (D.disk.hist || []).filter(r => r[0] >= now - 7 * 86400).map(r => [r[0], x.dk.total - r[1] / GIB]), dg = "d:" + h.id;
    SPKG[dg] = {t0: now - 7 * 86400, t1: now, at: t => { const r = nearest(pts, t, r => r[0]); return r ? `${ts(r[0])}：剩 ${g1(r[1])} GiB` : ""; }};
    const lo = Math.min(x.dk.warn_gb || 0, ...pts.map(r => r[1])) * 0.9, hi = Math.max(1, ...pts.map(r => r[1])) * 1.05;
    const eta = x.dk.eta_days;
    s += row(`<a href="#/fleet/disk">共享盘</a>`, sharedBar(x, false), `<span style="${x.dk.free < x.dk.warn_gb ? "color:var(--bad)" : ""}">剩 ${Math.round(x.dk.free)} / ${Math.round(x.dk.total)} GiB</span>${eta != null ? `<span class="mut" style="${eta < 3 ? "color:var(--bad)" : ""}">约 ${eta} 天满</span>` : ""}`,
      pts.length > 1 ? spark(dg, now - 7 * 86400, now, pts, {lo, hi, ref: x.dk.warn_gb, gap: 3 * 3600}) + "7 天" : "7 天曲线攒数据中",
      `${x.dk.path}：组里各服务器挂的同一块盘，总量与剩余由 df 直接读出\n${diskProg(D.disk)}\n红线：剩余低于 ${x.dk.warn_gb} GiB 告警\n点「共享盘」看${LIVE && ME && !ME.admin ? "本人用量" : "各人用了多少"}`);
  }
  const nf = h.standalone ? null : pr.nfs;
  if (nf) {                                       // 共享盘读写延迟：条的一半是近 14 天基线（竖线），超过基线变红
    const hi = 2 * nf.base, w = nf.ms == null ? 0 : Math.min(100, 100 * nf.ms / hi);
    s += row("共享盘读写", `<div class="bar big" style="overflow:hidden"><i style="width:${w}%;background:var(${nf.slow ? "--bad" : "--accent"});opacity:.6"></i><b style="left:50%" ${tip(`近 14 天基线 ${g1(nf.base)} ms`)}></b></div>`,
      `${nf.ms == null ? `<span class="mut">此刻没有读写</span>` : `${g1(nf.ms)} ms/次`}<span class="mut">基线 ${g1(nf.base)}</span>`,
      sp(r => r.nfs, {lo: 0, hi: Math.max(hi, ...rows.map(r => r.nfs || 0)), ref: nf.base}),
      `${nf.path}（NFS，客户端 ${nf.addr || "—"}）：READ、WRITE 每次的平均执行时间（含排队），近 1 分钟 ${g1(nf.ops)} 次/秒、${g1(nf.mib)} MiB/s\n`
      + `同一物理机的几个容器共用这份计数，组里别人读写重时也会变慢\n`
      + `近 14 天基线：每秒不少于 ${nf.min_ops} 次读写的 5 分钟样本的 95 分位，不低于 ${nf.floor} ms；${nf.n < nf.need ? `样本 ${nf.n} / ${nf.need} 个，还不够，先取 ${nf.floor} ms` : `${nf.n} 个样本`}\n`
      + `超过基线且每秒不少于 ${nf.min_ops} 次算慢（条变红）：准入不看，共享盘扫描与拷本地依赖先让路，同一件最多推迟 30 分钟${nf.slow ? "\n此刻：慢" : ""}${psiT("io")}`);
  }
  const sg = st.storage || {}, L = hostLw(h);
  if (L) {
    const ht = heatTag(lwHeat(L));
    s += row("本地盘", lwBar(L, sg.local), lwVal(L) + (ht ? " " + ht : ""), h.borrow ? "对方续租时清空" : "续租会清空",
      `${LW_TIP}\n$LABMON_CACHE（项目跨格缓存）与 $LABMON_SCRATCH（单格临时，成功后删）在 /tmp/labmon，也在根盘上\n按写的总量画的构成与各目录在下方「记录 · 本地盘」`);
  }
  const dk = pr.disk;
  if (dk) s += row("本地盘读写", big(`<i style="width:${dk.busy ?? 0}%;background:var(--accent);opacity:.6"></i>`),
    dk.busy == null ? "—" : `忙 ${Math.round(dk.busy)}%${dk.await != null ? `<span class="mut">${g1(dk.await)} ms/次</span>` : ""}`, sp(r => r.busy),
    `${dk.dev}：容器 /tmp 背后的盘，近 1 分钟有读写在做的时间比例与每次平均用时\n容器里看到的是宿主机的计数，同一物理机的几个容器共用这块盘\n只记录，准入不看${psiT("io")}`);
  const ps = [...new Set([...jobsOf(h).map(j => j.project), ...Object.keys(x?.byP || {}), ...Object.keys(sg.local || {})])];
  const any = k => (st.gpu_procs || []).some(p => p.owner === k);
  const lg = `<div class="legend">${ps.map(p => `<span><span class="dot" style="background:${color(p)}"></span>${esc(p)}</span>`).join("")}
    <span><span class="dot" style="background:var(--muted);opacity:.4"></span>淡色：记账预留、单格临时</span>
    ${any("keepalive") ? `<span><span class="dot" style="background:var(--faint)"></span>GPU 保活</span>` : ""}
    ${x?.common.length ? `<span><span class="dot" style="background:var(--faint)"></span>公共目录</span>` : ""}
    ${x?.rest.length ? `<span><span class="dot" style="background:var(--muted)"></span>我们的其他目录</span>` : ""}
    <span><span class="dot hatch" style="background:var(--muted)"></span>斜纹：labmon 之外、其他人</span>
    ${L?.risk ? `<span><span class="dot hatch" style="background:var(--heat)"></span>本地盘 labmon 之外写得多：续租前要挪的</span>` : ""}<span>条上悬停看明细</span></div>`;
  return `<div class="card"><div class="chd"><b>资源</b><span class="sm mut">右列是近 ${RANGE} 小时，移上去看那一刻的数值</span>
    <span class="seg">${[6, 24].map(r => `<button data-range="${r}" class="${r === RANGE ? "on" : ""}">${r} 小时</button>`).join("")}</span></div>
    <div class="rs">${s}</div>${lg}</div>`;
}
