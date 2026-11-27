const LANOFF = () => D.lan?.ok === false;          // 本机不在内网（看板连不上平台与任何一台服务器）
const g1 = x => x == null ? "—" : (+x).toFixed(1);
const mb2g = x => x == null ? "—" : (x / 1024).toFixed(1);
const pct = (a, b) => b ? Math.round(100 * a / b) : null;
const color = p => D.colors[p] || "var(--muted)";
const jobsOf = h => h.status ? [...h.status.running.map(j => ({...j, src: "worker"})), ...h.status.external.map(j => ({...j, src: "gpu_queue"}))] : [];

/* ---------- 路由：#/、#/log[?筛选]、#/host/<id>、#/p/<id>、#/fleet[/<选项卡>]、#/me（我的账号）；组内服务器的抽屉 #/fleet/srv/<id>、#/fleet/people/<名字>、#/fleet/new/<池>；
   项目的「管理」抽屉 #/p/<id>/manage；已归档的项目 #/archived（总览上的抽屉） ---------- */
let BGTAB = null;           // 抽屉背后的选项卡：从哪个选项卡打开就留在哪个（直接打开链接时按抽屉的种类）
function route(h = location.hash) {
  const dec = decodeURIComponent, pm = h.match(/^#\/p\/([^/]+)\/manage$/);
  if (pm) return {kind: "p", id: dec(pm[1]), drw: {k: "proj", id: dec(pm[1])}};
  const ha = h.match(/^#\/host\/([^/]+)\/assign$/);
  if (ha) return {kind: "host", id: dec(ha[1]), drw: {k: "assign", id: dec(ha[1])}};
  if (h === "#/archived") return {kind: "home", drw: {k: "arch", id: ""}};
  const ext = h.match(/^#\/ext\/([^/]+)$/);
  if (ext) return {kind: "home", drw: {k: "ext", id: ext[1] === "new" ? "" : dec(ext[1])}};
  if (h === "#/pool") return {kind: "pool"};
  const account = h.match(/^#\/me(?:\/(security|tokens|push))?$/);
  if (account) return {kind: "me", tab: account[1] || "overview"};
  const user = h.match(/^#\/admin\/users(?:\/([^/]+)(?:\/(usage))?)?$/);
  if (user) return {kind: "fleet", tab: "people", oldAccount: user[1] ? dec(user[1]) : "", oldUsage: Boolean(user[2])};
  const m = h.match(/^#\/(host|p)\/(.+)$/);
  if (m) return {kind: m[1], id: dec(m[2])};
  let d = h.match(/^#\/log(?:\?(.*))?$/);
  if (d) return {kind: "log", f: Object.fromEntries(new URLSearchParams(d[1] || ""))};
  if (h === "#/fleet/guard") return {kind: "fleet", tab: BGTAB || "slots", drw: {k: "guard", id: ""}};
  if ((d = h.match(/^#\/fleet\/renew(?:\/(.+))?$/))) return {kind: "fleet", tab: BGTAB || "slots", drw: {k: "renew", id: d[1] ? dec(d[1]) : ""}};
  d = h.match(/^#\/fleet\/srv\/(.+)$/);
  if (d) return {kind: "fleet", tab: BGTAB || "slots", drw: {k: "srv", id: dec(d[1])}};
  if ((d = h.match(/^#\/fleet\/new\/(.+)$/))) return {kind: "fleet", tab: BGTAB || "slots", drw: {k: "new", id: dec(d[1])}};
  if ((d = h.match(/^#\/fleet\/people\/([^/]+)(?:\/(account))?$/))) return {kind: "fleet", tab: BGTAB || "people", drw: {k: "person", id: dec(d[1]), section: d[2]}};
  const f = h.match(/^#\/fleet(?:\/(people|disk))?$/);
  return f ? {kind: "fleet", tab: f[1] || "slots"} : {kind: "home"};
}
// 换地址只走 nav。地址总写全（刷新、发链接照样打开），记不记历史看怎么来的，返回键回到“上一页”：
//   同一页里换视图（选项卡、记录的筛选与搜索、抽屉）替换当前这条；
//   侧栏、底栏、「更多」换栏目先退回第一层再换，历史里最多是「总览 → 这一栏」，点总览就退回总览那一条；
//   正文里点进去（主机行、记录行、链接）记一条，返回时滚回离开时的位置。
// history.state：d 是这条在第几层（打开看板的那条是 0），b 是第 0 层是不是总览，y 是离开时的滚动位置
let RKEY = "", RHASH = null, NAVTO = null;            // RHASH：正显示的地址；NAVTO：退回第一层后要换成的地址
const rkey = r => `${r.kind}:${r.tab || r.id || ""}`;
const samePage = (a, b) => a.kind === b.kind && (a.id || "") === (b.id || "");
function onRoute() {                                  // 只开关抽屉（页面与选项卡不变）时不滚回顶上
  if (location.hash === RHASH) return;
  RHASH = location.hash;
  if (POP?.route && POP.route !== RHASH) popClose();
  const keep = rkey(route()) === RKEY, y = history.state?.y;
  render(keep ? "keep" : true);
  if (!keep && y) document.getElementById("main").scrollTop = y;
}
function nav(href, bar = false) {                     // bar：从侧栏、底栏、「更多」点的
  const st = history.state || {}, d = st.d || 0, a = route(), b = route(href);
  if (samePage(a, b)) { history.replaceState(st, "", href); return onRoute(); }
  const base = d ? st.b : a.kind === "home", lv = base && b.kind !== "home" ? 1 : 0;
  history.replaceState({...st, y: document.getElementById("main").scrollTop}, "");
  if (!bar) history.pushState({d: d + 1, b: base}, "", href);
  else if (d > lv) { NAVTO = href; return history.go(lv - d); }
  else if (d === lv) history.replaceState({d, b: base}, "", href);
  else history.pushState({d: 1, b: true}, "", href);
  onRoute();
}
addEventListener("popstate", () => {
  const st = history.state || {}, h = NAVTO;
  NAVTO = null;
  if (h && h !== location.hash) history.replaceState({d: st.d, b: st.b}, "", h);
  onRoute();
});
addEventListener("hashchange", onRoute);              // 地址栏里改的
function closeDrw() {
  const r = route();
  if (r.drw) nav(r.kind === "p" ? "#/p/" + encodeURIComponent(r.id) : r.kind === "host" ? "#/host/" + encodeURIComponent(r.id) : r.kind === "home" ? "#/" : r.tab === "slots" ? "#/fleet" : "#/fleet/" + r.tab);
}
function stepDrw(dir) {
  const d = route().drw;
  if (!d || d.k === "new" || d.k === "guard" || d.k === "renew" || d.k === "proj" || d.k === "arch" || d.k === "assign" || d.k === "account" || d.k === "ext" || d.id === "+") return;
  const ids = d.k === "srv" ? srvOrder() : peopleOrder(), k = ids.indexOf(d.id);
  if (k >= 0 && ids.length > 1) nav(DH[d.k](ids[(k + dir + ids.length) % ids.length]));
}

function projCell(j) {
  const name = visibleProject(j.link) ? `<a href="#/p/${encodeURIComponent(j.link)}">${esc(j.project)}</a>` : esc(j.project);
  return `<span class="nw"><span class="dot" style="background:${color(j.project)}"></span> ${name}</span>`;
}

// 一台主机的告警（页面上现算，不进 labmon wait）：[{to, msg}]，to 给谁看同项目告警（#142，common.js 的 forUser、forAgent、alertBox）。
// 依赖等不来、因 CPU 被挡是实验会话的事（项目告警 dep:、cpu: 已送到会话），只折在主机页「给实验会话的提示」里；其余要用户看。
// 借来的约定挡住两边都要：这里写给用户的那句（borrow.late_say），会话的那份在项目告警 borrow_cut 里
function alertsOf(h) {
  const out = [], add = (to, msg) => out.push({to, msg});
  if (h.ephemeral) {
    if (h.health?.state === "down" && h.health.why) add("user", `${h.label}（临时主机）：${h.health.why}`);
    return out;
  }
  const hl = h.health || {};
  if (hl.state === "lan") {}                     // 本机不在内网：不是这台的问题，页顶统一说
  else if (hl.state === "down") add("user", `${h.label}：掉线 ${dur(D.generated - hl.since)}。${hl.why}`);
  else if (h.error) add("user", `${h.label}：连不上（${h.error}），持续 3 分钟才算掉线`);
  else if (h.status_error) add("user", `${h.label}：连得上，但 worker 的状态文件读不出（${h.status_error}），下一轮通常就好`);
  else if (!h.status) add("user", `${h.label}：没有 worker 状态（还没部署？labmon worker start ${h.id}）`);
  else if (!h.worker_up) add("user", `${h.label}：worker 已 ${dur(h.age)}没更新状态（容器重启过？labmon worker start ${h.id}）`);
  const m = h.status?.info?.mem;
  if (m && m.limit && m.anon / m.limit >= 0.9) add("user", `${h.label}：收不回的内存 ${g1(m.anon)} / ${g1(m.limit)} GiB，超过 90%${h.status?.gate?.on ? "，已停止放行" : ""}`);
  const dg = h.status?.disk_gate || h.work_disk?.disk_gate, wd = h.work_disk;
  if (dg?.on) add("both", `${h.label}：${dg.why || "本地工作盘空间不足，已暂停放行新任务"}`);
  else if (wd?.low) add("both", `${h.label}：本地工作盘 ${wd.root} 剩 ${GB(wd.free)} GiB（低于预警线 ${wd.warn_gb} GiB）`);
  else if (wd?.quota_warn) add("both", `${h.label}：本地工作盘 ${wd.root} 已用 ${GB(wd.work_b)} GiB，接近软配额 ${wd.quota_gb} GiB`);
  for (const w of missW(h)) add("user", `${h.label}：${w.name} ${w.why}`);
  for (const w of (h.status?.waiting || []).filter(w => w.dep && w.dep !== "wait")) add("agent", `${h.label}：${pname(w.project)} 的 ${w.name} ${w.why}`);
  for (const w of (h.status?.waiting || []).filter(w => w.cpu_wait)) add("agent", `${h.label}：${pname(w.project)} 的 ${w.name} 因 CPU 被挡 ${dur(w.cpu_wait)}：${w.why}`);
  for (const w of h.borrow?.late || []) add("both", `${h.label}：${pname(w.project)} 的 ${laneName(laneOf(h, w), [h])} 还有 ${w.n} 个任务不会在这台放行：${w.why}；${h.borrow.late_say}`);   // borrow.late_jobs、late_say
  const th = h.status?.throttle;
  if (th?.alert && (th.over?.length || th.other >= 1)) add("both", thrText(h));   // CPU 持续被配额限流且有超核任务或外部抢核（worker 判的，#39、#221）
  const kf = h.status?.kafight;            // worker 记的：要卡时停了的保活又被拉起来（近 1 小时）
  if (kf) add("user", `${h.label}：GPU 保活在任务运行中被拉起 ${kf.n} 次（1 小时内，最近 ${tsNear(kf.t)}），已停；${kf.who}`);
  const ks = h.status?.keepalive?.stuck;   // worker 判的：要卡时停保活一直没停成（#102），任务和它同卡分算力
  if (ks) add("user", `${h.label}：GPU 保活停不下来（${ks.why}，${tsNear(ks.t0)} 起 ${ks.n} 次），任务和它同卡分算力、少了它占的显存；${h.borrow ? `请 ${h.borrow.holder || "对方"} 停掉它或查 ka.sh` : "请查 ka.sh"}`);
  if (h.renew?.state === "ready") add("user", `${h.label}：可以续租了（${h.renew.why}），${srvById(h.id) ? "到主机页点「续租」" : "去平台续租（主机清单没有 [fleet] 段，看板不能替你续）"}`);
  else if (leaseSoon(h.lease_end)) add("user", `${h.label}：${leaseText(h)}（${ts(h.lease_end)} 到期）${h.renew ? "，已预备续租：" + h.renew.why : h.platform_kind === "other" ? "，请在外平台手动续租或提前迁移任务" : h.platform ? "，到主机页点「预备续租」" : "，找个任务间隙在平台续租"}`);
  const sBlock = h.stopped_blocks != null ? !!h.stopped_blocks : !!h.stopped;
  if (h.auto && !h.error && !h.status_error && !h.lan_off && h.status && !h.worker_up && !sBlock) add("user", `${h.label}：worker 不在，看板会自动拉起（10 分钟内一次）`);
  else if (h.auto && !h.error && !h.lan_off && !h.worker_up && sBlock) add("user", `${h.label}：worker 已手动停用（${String(h.stopped || "").replace(/^manual:/, "")}），不会自动拉起`);
  return out;
}
const allAlerts = () => D.hosts.flatMap(h => forUser(alertsOf(h)));
// CPU 被配额限流（worker 写的 throttle，#39；门槛只在 worker 的 thr_step、cpu_over，页面只读）：告警的那句、主机表与资源卡上限流比例的颜色与悬停
const thrOver = th => th.over.map(o => `${pname(o.project)} 的 ${o.name} 实测 ${g1(o.cores)} 核（给 ${o.give} 核）`).join("、");
function thrText(h) {
  const th = h.status.throttle;
  return `${h.label}：容器 CPU 被配额限流${th.thr != null ? ` ${g1(th.thr)}%` : ""}（已持续 ${dur(th.for)}），`
    + (th.over?.length ? `超核的任务：${thrOver(th)}，线程数、进程数按 $LABMON_CPUS 设就不再抢核` : `labmon 之外的进程占 ${g1(th.other)} 核`);
}
// 限流比例的小字：限流中琥珀、持续满（告警）标红；悬停写口径与超核的任务
function thrTag(h, cls = "sm") {
  const thr = h.status?.info?.press?.thr, th = h.status?.throttle;
  if (thr == null) return "";
  const t = `近 1 分钟里 CPU 被配额限流的周期比例（cgroup cpu.stat）${th ? `\n${th.alert ? "持续" : "已"}限流 ${dur(th.for)}${th.over.length ? `\n超核：${thrOver(th)}` : ""}` : ""}\n只记录，准入不看`;
  return `<span class="${cls}${th ? "" : " mut"}" style="${th ? `color:var(${th.alert ? "--bad" : "--warn"})` : ""}" ${tip(t)}>限流 ${g1(thr)}%</span>`;
}        // 给用户的（总览红框与告警数）

function standaloneBadge(h) {
  if (!h?.standalone) return "";
  const pk = h.platform_kind === "other" ? "外平台独立主机" : "自有独立主机";
  return ` <span class="badge b-mute" ${tip(`${pk}：不共享集群盘，不可借用、不可入池、不支持跨机搬登记`)}>独立</span>`;
}
function workerBadge(h) {
  if (h.ephemeral) return h.worker_up
    ? `<span class="badge b-ok" ${tip(`临时主机推模式心跳，${Math.round(h.age ?? 0)} 秒前上报`)}>临时在线</span>`
    : `<span class="badge b-mute" ${tip(h.error || "临时主机已离线")}>临时离线</span>`;
  const st = h.status;
  return h.lan_off ? `<span class="badge b-mute" ${tip("本机不在内网，下面是离开前的数据")}>本机不在内网</span>` : h.error ? `<span class="badge b-bad">连不上</span>` : !st ? `<span class="badge b-mute">无 worker</span>`
    : h.worker_up ? `<span class="badge b-ok" ${tip(`PID ${st.pid}，${Math.round(h.age)} 秒前更新`)}>worker 运行中</span>`
    : `<span class="badge b-bad">worker 不在（${dur(h.age)}未更新）</span>`;
}
// 租期（都按页面上的“现在” D.generated 算，页面上别处不再自己算剩余）：leaseLeft 剩余秒数；leaseSoon 一天内到期或已过（告警、标红、组内提醒条都按它）；
// leaseRem 纯文字「剩 X」/「已过」；leaseText 主机的「租期剩 X」（没登记租期给空）；leaseHtml 带配色的唯一出口：数字加粗，leaseSoon 标红，pre 为前缀（如“租期”）
const leaseLeft = end => end - D.generated;
const leaseSoon = end => !!end && leaseLeft(end) < 86400;
const leaseRem = end => leaseLeft(end) > 0 ? `剩 ${dur(leaseLeft(end))}` : "已过";
const leaseText = h => !h.lease_end ? "" : "租期" + leaseRem(h.lease_end);
const leaseHtml = (end, pre = "") => leaseLeft(end) > 0 ? `${pre}剩 <b style="${leaseSoon(end) ? "color:var(--bad)" : ""}">${dur(leaseLeft(end))}</b>` : `<b style="color:var(--bad)">${pre}已过</b>`;
function hostDot(h) {
  if (h.ephemeral) return h.worker_up ? (forUser(alertsOf(h)).length ? "var(--warn)" : "var(--ok)") : h.health?.why ? "var(--bad)" : "var(--faint)";
  const o = renewOp(h.id); return h.lan_off ? "var(--faint)" : o?.error ? "var(--bad)" : o?.active ? "var(--warn)"
  : h.error || !h.status || !h.worker_up ? "var(--bad)" : forUser(alertsOf(h)).length ? "var(--warn)" : "var(--ok)"; }

/* ---------- 左侧导航与顶栏 ---------- */
const ST = mkStore("labmon:");           // 本机存的设置（common.js 的 mkStore）：折叠、展开、时间线视图、小曲线的时段
const nOf = (n, unit = "条") => n ? `${n} ${unit}` : "无";
const short = s => { s = String(s).replace(/（.*?）|\(.*?\)/g, "").trim(); return s.length > 5 ? s.slice(0, 4) + "…" : s; };
// 线条图标（Tabler Icons，MIT），内嵌以便离线显示
const ICONS = {
  dash: '<path d="M5 4h4a1 1 0 0 1 1 1v6a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1M5 16h4a1 1 0 0 1 1 1v2a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1v-2a1 1 0 0 1 1-1M15 12h4a1 1 0 0 1 1 1v6a1 1 0 0 1-1 1h-4a1 1 0 0 1-1-1v-6a1 1 0 0 1 1-1M15 4h4a1 1 0 0 1 1 1v2a1 1 0 0 1-1 1h-4a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1"/>',
  server: '<path d="M6 4h12a3 3 0 0 1 3 3v2a3 3 0 0 1-3 3H6a3 3 0 0 1-3-3V7a3 3 0 0 1 3-3M6 12h12a3 3 0 0 1 3 3v2a3 3 0 0 1-3 3H6a3 3 0 0 1-3-3v-2a3 3 0 0 1 3-3M7 8v.01M7 16v.01"/>',
  chev: '<path d="M6 9l6 6 6-6"/>',
  chevl: '<path d="M15 6l-6 6 6 6"/>',
  chevr: '<path d="M9 6l6 6-6 6"/>',
  collapse: '<path d="M6 4h12a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2M9 4v16M15 10l-2 2 2 2"/>',
  expand: '<path d="M6 4h12a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2M9 4v16M14 10l2 2-2 2"/>',
  clock: '<path d="M3 12a9 9 0 1 0 18 0a9 9 0 0 0-18 0M12 7v5l3 3"/>',
  db: '<path d="M4 6a8 3 0 1 0 16 0a8 3 0 1 0-16 0M4 6v6a8 3 0 0 0 16 0V6M4 12v6a8 3 0 0 0 16 0v-6"/>',
  hourglass: '<path d="M6.5 7h11M6.5 17h11M6 20v-2a6 6 0 1 1 12 0v2a1 1 0 0 1-1 1H7a1 1 0 0 1-1-1zM6 4v2a6 6 0 1 0 12 0V4a1 1 0 0 0-1-1H7a1 1 0 0 0-1 1z"/>',
  renew: '<path d="M20 11A8.1 8.1 0 0 0 4.5 9M4 5v4h4M4 13a8.1 8.1 0 0 0 15.5 2m.5 4v-4h-4"/>',
  x: '<path d="M18 6L6 18M6 6l12 12"/>',
  undo: '<path d="M9 14l-4-4 4-4M5 10h11a4 4 0 1 1 0 8h-1"/>',
  play: '<path d="M7 4v16l13-8z"/>',
  pause: '<path d="M8 5v14M16 5v14"/>',
  stop: '<path d="M5 7a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2z"/>',
  flag: '<path d="M5 5a5 5 0 0 1 7 0a5 5 0 0 0 7 0v9a5 5 0 0 1-7 0a5 5 0 0 0-7 0V5M5 21v-7"/>',
  pencil: '<path d="M4 20h4L18.5 9.5a2.828 2.828 0 1 0-4-4L4 16v4M13.5 6.5l4 4"/>',
  power: '<path d="M7 6a7.75 7.75 0 1 0 10 0M12 4v8"/>',
  zap: '<path d="M13 3v7h6l-8 11v-7H5l8-11"/>',
  users: '<path d="M5 7a4 4 0 1 0 8 0a4 4 0 1 0-8 0M3 21v-2a4 4 0 0 1 4-4h4a4 4 0 0 1 4 4v2M16 3.13a4 4 0 0 1 0 7.75M21 21v-2a4 4 0 0 0-3-3.85"/>',
  gear: '<path d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 0 0 2.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 0 0 1.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 0 0-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 0 0-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 0 0-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 0 0-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 0 0 1.066-2.573c-.94-1.543.826-3.31 2.37-2.37c1 .608 2.296.07 2.572-1.065zM9 12a3 3 0 1 0 6 0a3 3 0 0 0-6 0"/>',
  shield: '<path d="M12 3a12 12 0 0 0 8.5 3a12 12 0 0 1-8.5 15a12 12 0 0 1-8.5-15a12 12 0 0 0 8.5-3"/>',
  archive: '<path d="M4 4h16a1 1 0 0 1 1 1v2a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1M5 8v10a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8M10 12h4"/>',
  shieldck: '<path d="M12 3a12 12 0 0 0 8.5 3a12 12 0 0 1-8.5 15a12 12 0 0 1-8.5-15a12 12 0 0 0 8.5-3M9 12l2 2 4-4"/>',
  user: '<path d="M8 7a4 4 0 1 0 8 0a4 4 0 0 0-8 0M6 21v-2a4 4 0 0 1 4-4h4a4 4 0 0 1 4 4v2"/>',
  dots: '<path d="M4 12a1 1 0 1 0 2 0a1 1 0 1 0-2 0M11 12a1 1 0 1 0 2 0a1 1 0 1 0-2 0M18 12a1 1 0 1 0 2 0a1 1 0 1 0-2 0"/>',
  wifi: '<path d="M12 18h.01M9.17 15.17a4 4 0 0 1 5.66 0M6.34 12.34a8 8 0 0 1 11.32 0M3.52 9.52c4.68-4.69 12.28-4.69 16.96 0"/>',
  list: '<path d="M9 6h11M9 12h11M9 18h11M5 6v.01M5 12v.01M5 18v.01"/>',
  search: '<path d="M3 10a7 7 0 1 0 14 0a7 7 0 1 0-14 0M21 21l-6-6"/>',
  lock: '<path d="M5 13a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2v6a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2zM11 16a1 1 0 1 0 2 0a1 1 0 1 0-2 0M8 11V7a4 4 0 1 1 8 0v4"/>',
  cloud: '<path d="M6.66 18.6C4.1 18.6 2 16.6 2 14.1c0-2.4 2.1-4.4 4.66-4.4.4-1.92 1.86-3.48 3.84-4.1 1.97-.62 4.15-.2 5.7 1.1 1.55 1.3 2.24 3.25 1.8 5.13h1c1.93 0 3.5 1.57 3.5 3.5s-1.57 3.5-3.5 3.5H6.66z"/>',
};
const icon = k => `<svg class="ico" viewBox="0 0 24 24" aria-hidden="true">${ICONS[k]}</svg>`;
// Logo：卡上三个任务占位，右上角的点是还能放
const LOGO = (n = 28) => `<svg width="${n}" height="${n}" viewBox="0 0 32 32" aria-hidden="true"><rect width="32" height="32" rx="7" fill="#378ADD"/><rect x="7" y="15" width="4.5" height="10" rx="1.5" fill="#fff"/><rect x="13.75" y="10" width="4.5" height="15" rx="1.5" fill="#fff"/><rect x="20.5" y="18" width="4.5" height="7" rx="1.5" fill="#fff" opacity=".55"/><circle cx="22.75" cy="10" r="2.6" fill="#5DCAA5"/></svg>`;
// 项目缩写：多段名取各段首字母，否则取前 3 个字符
const abbr = p => { const ps = String(p).split(/[-_\s]+/).filter(Boolean); return (ps.length > 1 ? ps.map(x => x[0]).join("") : ps[0] || "?").slice(0, 3).toUpperCase(); };
// 人名缩写（#15）：末尾带数字的取首字母 + 数字（PERSON3 → P3），多段名取各段首字母，否则取前两个字（PERSON 与 PERSON3 不再都是 PER）
const pabbr = n => { const s = String(n || "?"), m = /^(.*?)[-_\s]*(\d+)$/.exec(s);
  if (m && m[1]) return (m[1][0] + m[2]).slice(0, 3).toUpperCase();
  const ps = s.split(/[-_\s.]+/).filter(Boolean); return (ps.length > 1 ? ps.map(x => x[0]).join("") : s).slice(0, 3 - (ps.length < 2)).toUpperCase(); };
// 人的头像：缩写 + 共享盘上这人的配色（disk.py 经 config.pick_color 定的，与共享盘的图一致；没有的用主色）；extra 是角上的状态点
const pfav = (n, extra = "", cls = "fav") => { const c = D.disk?.colors?.[n];
  return `<span class="${cls}"${c ? ` style="--pc:${esc(c)}"` : ""} aria-hidden="true">${esc(pabbr(n))}${extra}</span>`; };
const inkOn = hex => { const m = /^#(..)(..)(..)$/.exec(hex || ""); if (!m) return "#fff";
  const [r, g, b] = m.slice(1).map(x => parseInt(x, 16) / 255); return 0.299 * r + 0.587 * g + 0.114 * b > 0.62 ? "#1d2330" : "#fff"; };
// 标签页图标：有告警时右上角加红点，切到别的标签页也看得到
let ICON_N = -1;
function setIcon(n) {
  if ((n > 0) === (ICON_N > 0) && ICON_N >= 0) return;
  ICON_N = n;
  const svg = LOGO(32).replace("<svg", '<svg xmlns="http://www.w3.org/2000/svg"').replace(/ aria-hidden="true"/, "")
    .replace("</svg>", n > 0 ? '<circle cx="26" cy="6" r="6" fill="#E24B4A" stroke="#fff" stroke-width="1.5"/></svg>' : "</svg>");
  document.getElementById("favicon").href = "data:image/svg+xml," + encodeURIComponent(svg);
}
// 导航（宽屏的侧栏、窄屏的底栏与「更多」面板同用这一份）：上段总览、记录；分组是主机、项目、组内服务器、管理（管理员）；
// me 是「我的账号」（侧栏底部、「更多」最后一组；file:// 打开时没有）
// 每项 {href, ico, label, on, bad（告警数）, full（悬停全名）, ab（收起时的简写）, sub（「更多」里的一行说明）}
function navModel(r) {
  const n = allAlerts().length + projAlerts().length;
  const hal = h => forUser(alertsOf(h)).length;
  const hostState = h => h.ephemeral ? (h.worker_up ? "临时在线" : "临时离线") : h.lan_off ? "本机不在内网" : h.error ? "连不上" : !h.status || !h.worker_up ? "worker 不在" : hal(h) ? "有告警" : "正常";
  const top = [{k: "home", href: "#/", ico: icon("dash"), label: "总览", on: r.kind === "home", bad: n, full: n ? `总览：${n} 条告警` : "总览"}];
  if (D.pool?.pools?.length || D.pool?.error) {
    const ps = D.pool.pools || [], pbad = (D.pool.error ? 1 : 0) + Object.values(D.pool.lists || {}).filter(x => x.nofit).length;
    const pPend = ps.reduce((a, p) => a + (p.pending || 0), 0), pFree = ps.reduce((a, p) => a + (p.free_gpus || 0), 0), pGpus = ps.reduce((a, p) => a + (p.gpus || 0), 0);
    top.push({k: "pool", href: "#/pool", ico: icon("zap"), label: "算力池", ab: "算力池", on: r.kind === "pool", bad: pbad,
      full: D.pool.error ? `算力池：配置有误` : `共有算力池 ${ps.length} 个：空 ${pFree}/${pGpus} 张卡，待放 ${pPend} 条`,
      sub: D.pool.error ? "配置有误" : `${ps.length} 个池 · 空 ${pFree}/${pGpus} 卡 · 待放 ${pPend}`});
  }
  if (LIVE) top.push({k: "log", href: "#/log", ico: icon("clock"), label: "记录", on: r.kind === "log", full: "记录：事件、平台操作、配置改动", sub: "事件、平台操作、配置改动"});
  const secs = [
    {id: "host", name: "主机", ct: D.hosts.length, empty: "没有在用的主机", items: D.hosts.map(h => ({href: `#/host/${encodeURIComponent(h.id)}`, on: r.kind === "host" && r.id === h.id,
      ico: `<span class="hico">${icon(h.ephemeral ? "cloud" : "server")}<span class="cdot" style="background:${hostDot(h)}"></span></span>`, label: h.label, bad: hal(h), ab: short(h.label),
      full: `${h.label}（${hostState(h)}）：运行 ${jobsOf(h).length}，等待 ${(h.status?.waiting || []).length}${hal(h) ? `，${hal(h)} 条告警` : ""}`,
      sub: `${hostState(h)} · 运行 ${jobsOf(h).length} · 等待 ${(h.status?.waiting || []).length}`}))},
    {id: "proj", name: "项目", ct: D.projects.length, empty: "没有项目", items: D.projects.map(p => ({href: `#/p/${encodeURIComponent(p.id)}`, on: r.kind === "p" && r.id === p.id,
      ico: `<span class="av" style="background:${color(p.id)};color:${inkOn(D.colors[p.id])}">${esc(abbr(p.id))}</span>`, label: p.title || p.id, bad: p.alerts || 0,
      full: `${p.title || p.id}${p.alerts ? `：${p.alerts} 条告警` : ""}${p.no_work_since ? `（${NO_WORK}）` : ""}`,
      sub: p.no_work_since ? NO_WORK : p.generated ? `${ts(p.generated)} 更新` : ""})).concat(archItem(r))},
  ];
  const selfView = LIVE && ME && !ME.admin;
  if (selfView) {               // 普通用户（#211）：按数据有无展示「我的资源」（我的服务器、共享盘），隐藏全组人员与保活守护
    const F = D.fleet && !D.fleet.error ? D.fleet : null, nSrv = F ? F.servers.length + F.mine.length : 0;
    const hasDisk = !!D.disk?.df, items = [];
    if (nSrv) {
      const fa = fleetAlerts().length;
      items.push({k: "fleet", href: "#/fleet", ico: icon("server"), label: "我的服务器", ab: "服务器",
        on: r.kind === "fleet" && r.tab === "slots", bad: fa,
        full: fa ? `我的服务器：${fa} 条提醒` : "我的服务器", sub: `${nSrv} 台`});
    }
    if (hasDisk) {
      const da = (D.disk.alerts || []).filter(a => a.notify).length;
      items.push({k: "disk", href: "#/fleet/disk", ico: icon("db"), label: "共享盘", ab: "共享盘",
        on: r.kind === "fleet" && r.tab === "disk", bad: da,
        full: da ? `共享盘：${da} 条告警` : "本人共享盘用量", sub: `剩 ${GB(D.disk.df.free)} GiB`});
    }
    if (items.length) secs.push({id: "myres", name: "我的资源", ct: nSrv || "", sep: true, items});
  } else {
    if (D.fleet) {                // 各台在概况页里，侧栏不逐台列；「更多」里另列概况页的几个选项卡与保活守护
      const fa = fleetAlerts().length, F = D.fleet;
      secs.push({id: "fleet", name: "组内服务器", ct: F.servers.length + F.mine.length, sep: true, items: [{k: "fleet", href: "#/fleet", ico: icon("users"), label: "概况", ab: "组内", on: r.kind === "fleet", bad: fa,
        full: fa ? `组内服务器：${fa} 条提醒` : "组内服务器", sub: `${F.servers.length + F.mine.length} 台`,
        subs: [["人员", "#/fleet/people", icon("user")], ["共享盘", "#/fleet/disk", icon("db")], ["保活守护", DH.guard(), icon("shield")]]}]});
    }
    if (!D.fleet && LIVE && ME?.admin && ME.scopes.includes("admin")) secs.push({id: "fleet", name: "组内", sep: true, items: [
      {k: "fleet", href: "#/fleet/people", ico: icon("users"), label: "人员", ab: "人员", on: r.kind === "fleet", full: "组内人员"}]});
  }
  const me = LIVE && ME ? {k: "me", href: "#/me", ico: accAv(ME.name, " nv"), label: ME.name, ab: "我", on: r.kind === "me", cls: "me",
    full: `我的账号：${ME.name}（${ME.role_name}，从${ME.entry_name}来）`, sub: `${ME.role_name} · ${ME.entry_name} · 口令、令牌、登录的浏览器`} : null;
  return {top, secs, me};
}
// 项目段末尾的「已归档 N 个」（有归档的项目时才有）：打开已归档的抽屉，在那里恢复（#97）
function archItem(r) {
  const A = D.archived || [];
  return A.length ? [{href: DH.arch(), on: r.drw?.k === "arch", ico: `<span class="hico">${icon("archive")}</span>`, label: `已归档 ${A.length} 个`, ab: "归档",
    full: `已归档：${A.map(e => e.title || e.id).join("、")}`, sub: A.map(e => e.title || e.id).join("、")}] : [];
}
function side(r) {
  const {top, secs, me} = navModel(r), fold = ST.get("fold", {});
  const mini = document.body.classList.contains("mini");
  const sec = (id, name, n, body) => `<div class="nsec ${fold[id] ? "fold" : ""}" data-sec="${id}" role="button" tabindex="0"
    aria-expanded="${!fold[id]}">${icon("chev")}${name}<span class="ct">${n}</span></div><div class="grp">${body}</div>`;
  const item = x => `<a class="nav ${x.on ? "on" : ""}${x.cls ? " " + x.cls : ""}" href="${x.href}" ${mini ? tip(x.full || x.label) : ""}>
    ${x.ico}<span class="lbl">${esc(x.label)}</span>${x.ab ? `<span class="ab">${esc(x.ab)}</span>` : ""}${x.bad ? `<span class="ct bad">${x.bad}</span>` : ""}</a>`;
  let s = `<a class="brand" href="#/" aria-label="labmon 工作台">${LOGO()}<span class="lbl"><b>labmon</b><small>调度与实验工作台</small></span></a>`;
  // 上段是我们自己的实验（总览、主机、项目），分隔线下是组内服务器；总览单独一项，不另起组
  s += `<div class="top1">${top.map(item).join("")}</div>`;
  for (const g of secs) s += (g.sep ? `<div class="zsep"></div>` : "") + sec(g.id, g.name, g.ct,
    g.items.map(item).join("") || `<div class="nav" style="cursor:default;color:var(--side-mut)"><span class="lbl">${g.empty}</span></div>`);
  s += `<div class="sfoot">${me ? item(me) : ""}<button class="nav" data-side ${mini ? tip("展开侧边栏（[）") : ""} aria-label="${mini ? "展开侧边栏" : "收起侧边栏"}">
    ${icon(mini ? "expand" : "collapse")}<span class="lbl">收起侧边栏</span><span class="kbd">[</span></button></div>`;
  return s;
}
// 窄屏：底栏放总览、组内、记录与「更多」；「更多」从底部滑出，列全部导航（同侧栏）。在主机页、项目页时「更多」算当前
let MORE = false;
function tabbar(r) {
  const {top, secs, me} = navModel(r), fsec = secs.find(g => g.id === "fleet" || g.id === "myres"), fleet = fsec?.items[0];
  const rest = top.filter(x => x.k !== "home" && x.k !== "log").concat(secs.filter(g => g !== fsec).flatMap(g => g.items), fsec?.items.slice(1) || []);
  const tab = (x, attr = `href="${x.href}"`) => `<a class="tb${x.on ? " on" : ""}" ${attr} ${x.on ? 'aria-current="page"' : ""}>
    <span class="tbi">${x.ico}${x.bad ? `<span class="tbb">${x.bad > 99 ? "99+" : x.bad}</span>` : x.dot ? `<span class="tbb dot"></span>` : ""}</span><span>${esc(x.label)}</span></a>`;
  const tabs = [top[0], fleet && {...fleet, label: fsec.id === "myres" ? "资源" : "组内"}, top.find(x => x.k === "log")].filter(Boolean)
    .map(x => tab({...x, on: x.on && !MORE}));
  tabs.push(tab({ico: icon("dots"), label: "更多", on: MORE || rest.some(x => x.on) || !!me?.on, dot: rest.some(x => x.bad)}, `href="#" data-more role="button" aria-expanded="${MORE}"`));
  return tabs.join("");
}
function morePanel(r) {
  const {top, secs, me} = navModel(r);
  const row = x => `<a class="mrow${x.on ? " on" : ""}" href="${x.href}"><span class="mic">${x.ico}</span><span class="mt"><b>${esc(x.label)}</b>${x.sub ? `<span class="sm mut">${esc(x.sub)}</span>` : ""}</span>
    ${x.bad ? `<span class="badge b-bad">${x.bad}</span>` : ""}${icon("chevr")}</a>`;
  const grp = (name, rows) => rows.length ? `<div class="mgrp">${name}</div><div class="mcard">${rows.join("")}</div>` : "";
  return `<div class="mhd"><b>全部</b><button class="ib" data-more aria-label="收起">${icon("x")}</button></div>`
    + grp("工作台", top.map(row)) + secs.map(g => grp(g.name, g.items.flatMap(x => [row(x), ...(x.subs || []).map(([label, href, ico]) => row({label, href, ico, on: location.hash === href}))]))).join("")
    + (me ? grp("我", [row(me)]) : "");
}
document.body.classList.toggle("mini", ST.get("mini", false));

function topbar(r) {
  const fs = r.kind === "p" ? frameState() : null, gen = fs?.now || D.generated, off = OFFLINE || fs?.offline;
  const age = Date.now() / 1000 - gen, stale = age > 300;
  let title = "调度总览", crumb = "调度", extra = "";
  if (r.kind === "host") {
    const h = D.hosts.find(x => x.id === r.id);
    title = h ? h.label : r.id; crumb = "主机"; extra = h ? workerBadge(h) : "";
  } else if (r.kind === "pool") {
    title = "算力池"; crumb = "调度";
  } else if (r.kind === "log") {
    title = "记录"; crumb = ""; extra = LIVE ? logTabs(r.f) : "";
  } else if (r.kind === "fleet") {
    const selfView = LIVE && ME && !ME.admin;
    title = selfView ? "我的资源" : "组内服务器";
    crumb = selfView ? "资源" : "组内";
    extra = selfView ? (D.fleet || D.disk?.df ? fleetTabs(r.tab) : "") : (D.fleet ? fleetTabs(r.tab) : "");
  } else if (r.kind === "p") {
    const p = D.projects.find(x => x.id === r.id);
    title = p ? p.title || p.id : r.id; crumb = "项目";
  } else if (r.kind === "me") {
    return `<div class="account-content account-top"><h1>我的账号</h1>${LIVE && ME?.account ? meTabs(r.tab) : ""}
      ${off ? `<div class="account-feedback" role="status">${offBadge(off)}</div>` : ""}</div>`;
  }
  const upd = `<span class="sm mut nw" style="${stale ? "color:var(--bad);font-weight:600" : ""}" ${tip(`${ts(gen)} 更新。每分钟采集一次，页面每 15 秒取一次`)}><span class="dsk">${tsMD(gen)} </span>${tsHM(gen)}<span class="dsk"> 更新</span>${stale ? `（${Math.round(age / 60)} 分钟未更新）` : ""}</span>`;
  if (r.kind === "p") {
    const writable = canWrite(r.id), busy = BUSY || fs?.busy;
    return `<div class="project-title"><span class="crumb">项目 /</span><h1 ${tip(title)}>${esc(title)}</h1></div>
      ${upd}
      <div class="project-actions">${writable ? `<button class="btn" data-now ${busy ? "disabled" : ""} ${tip("立即采集一次")} aria-label="立即采集">${icon("renew")}<span class="dsk">立即采集</span></button>` : ""}
        ${LIVE ? `<button class="btn" data-drw="${esc(DH.proj(r.id))}" aria-label="管理" ${tip("这个项目登记在哪几台主机、调度与配置")}>${icon("gear")}<span class="dsk">管理</span></button>` : ""}</div>
      <div class="project-feedback" role="status">${offBadge(off) || (busy ? '<span class="sm mut">采集中…</span>' : "")}</div>`;
  }
  return `${crumb ? `<span class="crumb">${crumb} /</span>` : ""}<h1>${esc(title)}</h1>${extra}<span class="sp"></span>
    ${offBadge(off)}
    ${upd}
    ${canWrite() ? `<button class="btn" data-now ${BUSY ? "disabled" : ""} ${tip("立即采集一次")} aria-label="立即采集">${icon("renew")}<span class="dsk">${BUSY ? "采集中…" : "立即采集"}</span></button>` : ""}
    `;
}
