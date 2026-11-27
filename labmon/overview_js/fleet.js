/* ---------- 组内服务器：组里别人在用的服务器，只读监控（见 fleet.py） ---------- */
const FSN = {job: "实验", unknown: "未识别", keepalive: "只有保活", idle: "空闲", down: "掉线"};
const FSB = {job: "b-ok", unknown: "b-warn", keepalive: "b-mute", idle: "b-mute", down: "b-bad"};
const FSC = {job: "var(--accent)", unknown: "var(--warn)", keepalive: "color-mix(in srgb, var(--muted) 55%, transparent)",
             idle: "color-mix(in srgb, var(--muted) 22%, transparent)", down: "var(--bad)"};
const fName = x => `${x.holder}（${x.id}）`;
const fKind = x => x.mine ? "自用" : x.public ? "公用" : "专用";      // 服务器的池类型（抽屉标题、人员抽屉、小签、批量续租都用它）
const fBadge = x => x.off ? `<span class="badge b-mute" ${tip(`${ts(x.off.t)} 在看板上关机（当时的使用人 ${x.off.holder}）`)}>已关机</span>` : x.state ? `<span class="badge ${FSB[x.state]}">${FSN[x.state]}</span>` : `<span class="badge b-mute">还没探到</span>`;
const fShare = x => x.sample?.share && x.state !== "down" ? `<span class="badge b-ok" ${tip("除保活外的实际占用都低于阈值：还能给别人并行用")}>可共享</span>` : "";
const fState = x => fBadge(x) + (fShare(x) ? " " + fShare(x) : "");
const fDot = x => x.off ? "var(--muted)" : x.down ? "var(--bad)" : x.state === "unknown" || leaseSoon(x.lease_end) ? "var(--warn)" : x.state ? "var(--ok)" : "var(--muted)";
// 组员名下服务器自动接入调度 / 仅监控状态与切换（#224/#236，唯一实现）
let MAN = {busy: false, id: null, where: null, msg: null, err: null};
const manageBadge = x => !x || x.mine || !x.manage ? ""
  : x.manage.on ? (x.manage.state === "relaunch_failed"
    ? `<span class="badge b-bad" ${tip(x.manage.why || "自动拉起 worker 失败")}>拉起失败</span>`
    : x.manage.blocked
    ? `<span class="badge b-warn" ${tip(x.manage.why || "接入调度受阻")}>接入受阻</span>`
    : `<span class="badge b-ok" ${tip(x.manage.why || "已自动接入本人调度")}>已接入调度</span>`)
  : x.manage.opt_out ? `<span class="badge b-mute" ${tip(x.manage.why || "已切为仅监控")}>仅监控</span>` : "";
const canToggleManage = x => !!(LIVE && ME && x && !x.mine && x.manage?.can_toggle
  && (ME.admin ? canOps() : ME.write && ME.fleet_person === x.holder));
function managePop(x, where = "drw") {
  if (!canToggleManage(x)) return "";
  const on = !!x.manage.on, needResume = on && x.manage.state === "manual_stopped";
  const mk = (k, nextOn, label, cls, bodyHtml) => {
    const open = popIs(k, x.id);
    const btn = `<button type="button" class="btn mini-btn${cls}" data-fman="pick" data-id="${esc(x.id)}" data-where="${where}" data-k="${k}" ${MAN.busy ? "disabled" : ""}>${label}</button>`;
    const err = open && MAN.err && MAN.id === x.id ? `<div class="rn-err">${esc(MAN.err)}</div>` : "";
    const go = `<button type="button" class="btn pri${nextOn ? "" : " dz"}" data-fman="go" data-id="${esc(x.id)}" data-on="${nextOn ? "1" : "0"}" data-where="${where}" ${MAN.busy ? "disabled" : ""}>${MAN.busy ? "处理中…" : `确认${label}`}</button>`;
    return popConfirm(btn, open, bodyHtml + err, go);
  };
  const resume = needResume ? mk(`fman-${where}-res`, true, "恢复拉起", " pri",
    `<b>清除 ${esc(x.holder)}（${esc(x.id)}）的手动停用标记并恢复拉起？</b><div class="rn-note">将清除远端/共享盘的 <code>stopped</code> 停用标记与拉起冷却，由主服务下一轮采集自动拉起 worker（不推送代码，不动已有实验与用户自开保活）。</div>`) : "";
  const label = on ? "切为仅监控" : "接入调度";
  const body = on
    ? `<b>将 ${esc(x.holder)}（${esc(x.id)}）切为仅监控？</b><div class="rn-note">将停止该台 worker 并撤出受管主机列表，不再放行新任务；容器内正在跑的任务不受影响，看板保留只读监控。可随时重新接入。</div>`
    : `<b>将 ${esc(x.holder)}（${esc(x.id)}）接入本人调度？</b><div class="rn-note">将自动部署并拉起 worker（同时清除旧停用标记），仅开放给持有人本人；绝不动容器内已有实验进程与用户自开保活。</div>`;
  return resume + mk(`fman-${where}`, !on, label, on ? "" : " pri", body);
}
async function manageAct(act, d = {}) {
  const id = d.id || "", where = d.where || "drw";
  if (act === "pick") {
    MAN.err = null;
    popToggle(d.k || `fman-${where}`, id);
    return render(false);
  }
  if (act !== "go" || MAN.busy) return;
  const on = d.on === "1";
  MAN = {busy: true, id, where, msg: null, err: null};
  render(false);
  try {
    const r = await postJSON("api/fleet/manage", {id, on}), j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.error || "HTTP " + r.status);
    const srv = srvById(id);
    if (srv && j.manage) srv.manage = j.manage;
    popClose();
    MAN = {busy: false, id, where, msg: j.msg || (on ? "已接入调度" : "已切为仅监控"), err: null};
    render(false);
    if (ME?.admin) collectNow(true); else poll();
  } catch (e) {
    MAN = {busy: false, id, where, msg: null, err: e.message || String(e)};
    render(false);
  }
}
let FRANGE = ST.get("frange", 24), SER = {}, PROBING = null, PERR = {}, FKEEP = null;   // FKEEP：立即探测的结果，等全局页采集追上前先用它
function fleetAlerts(mine = true) {      // mine=false：首页用，自用主机的掉线、租期那里由主机自己的告警（alertsOf）报，不重复
  const F = D.fleet, out = [], now = D.generated;
  if (F) {
    if (F.error) out.push(F.error);
    const q = F.quota || {};
    for (const x of [...F.servers.filter(x => !x.off), ...mine && F.mine || []]) {    // 组内页、侧栏、抽屉也列自用主机的掉线、租期（守护不管它，不会没开成）
      if (x.down) out.push(`${fName(x)}：掉线 ${dur(now - (x.since || x.last))}，${x.down}`);
      if (leaseSoon(x.lease_end)) out.push(`${fName(x)}：${leaseText(x)}（${ts(x.lease_end)} 到期），需在平台续租`);
      if (x.guard?.status === "fail") out.push(`${fName(x)}：保活守护${guardText(x)}`);
    }
    if (q.public_max != null && q.public > q.public_max) out.push(`公用池开了 ${q.public} 台，超过全组上限 ${q.public_max}：可能被警告甚至强制关机`);
    for (const m of fops().platform?.issues || []) out.push(`与平台核对（${ts(fops().platform.t)}）：${m}`);
  }
  if (F || (LIVE && ME && !ME.admin && D.disk?.df)) {
    for (const a of D.disk?.alerts || []) if (a.notify) out.push(a.msg);     // 共享盘快满、某人暴涨（超过提醒线只在共享盘页标红）
    if (D.disk?.error) out.push(D.disk.error);
  }
  return out;
}
// 组内提醒条；租期快到的那一条带「续租」：打开批量续租的抽屉，只勾这一台
const fleetDue = () => [...D.fleet?.servers || [], ...D.fleet?.mine || []].filter(x => !x.off && leaseSoon(x.lease_end));
// fold：人员、共享盘选项卡上收成一行「⚠ 组内 N 条提醒」，点开看全部（#15：完整的只在「服务器」选项卡顶上，别的选项卡不把正文挤到第一屏以下）；开合记在本机（cardOpen 的 fleetal）
function fleetAlertBox(al, btn = true, fold = false) {      // 详情页上工具栏已有「续租」，提醒条不再放
  if (!al.length) return "";
  const due = fleetDue();
  const rows = al.map(m => { const x = due.find(x => m.startsWith(fName(x)) && m.includes("租期"));
    return `<div>⚠ ${esc(m)}${btn && x && canOps() && !fops().op?.active ? ` <button class="btn mini-btn" data-drw="${esc(DH.renew(x.id))}">续租</button>` : ""}</div>`; }).join("");
  if (!fold) return `<div class="alerts">${rows}</div>`;
  const open = !!ST.get("cardOpen", {}).fleetal;
  const lbl = D.fleet?.scope === "self" ? "资源" : "组内";
  return `<details class="alerts fold fleet-al" data-fold="fleetal" ${open ? "open" : ""}><summary>${icon("chev")}⚠ ${lbl} ${al.length} 条提醒${open ? "" : `<span class="sm mut" style="margin-left:6px">点开看</span>`}</summary>${rows}</details>`;
}
const fcr = c => /^\d{4}-\d\d-\d\d /.test(c || "") ? c.slice(5, 16) : c;     // 平台给的 2026-11-05 10:37:52 → 11-05 10:37，与别处一致
// 分段条：[[值, 颜色, 提示, class]]
const fbar = (parts, total, cls = "thin") => `<div class="bar ${cls}" style="flex:1;overflow:hidden">${total ? parts.filter(p => p[0] > 0).map(p =>
  `<i class="${p[3] || ""}" style="width:${Math.min(100, 100 * p[0] / total)}%;background:${p[1]}" ${tip(p[2])}></i>`).join("") : ""}</div>`;
// 负载里的进程名（"脚本 · 参数"）：同一脚本只是参数不同的合成「脚本 × 个数」，完整列表放悬停提示
function nameSum(names) {
  const by = new Map();
  for (const n of names) { const k = n.split(" · ")[0]; by.set(k, [...(by.get(k) || []), n]); }
  const txt = [...by].map(([k, v]) => v.length > 1 ? `${k} × ${v.length}` : v[0]).join("、");
  return `<div class="mut" ${names.length > by.size ? tip(names.join("\n")) : ""}>${esc(txt)}</div>`;
}
const KA = "color-mix(in srgb, var(--muted) 45%, transparent)";
// 近 7 天逐小时：上排是这一小时占多数的状态，下排绿色深浅是可共享的比例
function hourStrip(x, big) {
  if (!x.strip) return "—";
  const cell = (c, k) => { const t = x.h0 + k * 3600;     // c[2]：这一小时借给了谁（借出的小时不改色，只在悬停里写）
    return `<i style="background:${c ? FSC[c[0]] : "var(--soft)"}" ${big ? tip(`${ts(t)} 起的一小时\n${c ? FSN[c[0]] + (c[1] ? `，可共享 ${Math.round(100 * c[1])}%` : "") + (c[2] ? `\n借给 ${c[2].join("、")}` : "") : "没有数据"}`) : ""}></i>`; };
  const s7 = x.s7 || {}, f = s7.frac || {};
  const sum = s7.n ? `近 7 天（有数据 ${s7.hours} 小时）：` + Object.keys(FSN).filter(k => f[k]).map(k => `${FSN[k]} ${Math.round(100 * f[k])}%`).join("，")
    + `\n可共享 ${Math.round(100 * s7.share)}%` : "近 7 天还没有数据";
  return `<div ${big ? "" : tip(sum + "\n上排：每小时占多数的状态；下排：可共享的比例")}><div class="hs${big ? " big" : ""}">${x.strip.map(cell).join("")}</div>
    <div class="hs sh">${x.strip.map(c => `<i style="background:var(--ok);opacity:${c ? c[1] : 0}"></i>`).join("")}</div></div>`;
}
const fLegend = (extra = "") => `<div class="legend">${Object.keys(FSN).map(k => `<span><span class="dot" style="background:${FSC[k]}"></span>${FSN[k]}</span>`).join("")}${extra}
  <span><span class="dot" style="background:var(--soft);border:1px solid var(--line)"></span>没有数据</span><span><span class="dot" style="background:var(--ok)"></span>下排：可共享（越深越多）</span></div>`;
// 租期：剩多久和到期日
const fpct = v => v == null ? "—" : Math.round(100 * v) + "%";

/* ---------- 卡片墙：按资源池分组，每台一张等高的卡；点卡片从右侧滑出这台的详情（抽屉），卡片本身不展开 ---------- */
const FREE_H = 12;                                    // 连着这么多小时只有保活或空闲：标「闲置」
const fNode = s => String(s || "").split("@").pop();
const srvById = id => D.fleet?.servers.find(s => s.id === id) || D.fleet?.mine.find(s => s.id === id);     // 自用主机也能换人、关机
// 换人（含换给自己、自用主机换给别人）、开机换了 id：旧的从列表去掉、新的登记上（op.conn.sid）。开着旧那台的抽屉跟到新的那台，
// 地址原地换掉、不记历史；不然抽屉停在“已经没有这一台”，要关掉重开（#72）。
// 换地址的唯一例外：它在 render 里调，经 nav 会再走 onRoute → render 重入，所以直接 history.replaceState、改 RHASH
function srvMoved() {
  const d = route().drw, op = fops().op, to = op?.conn?.sid;
  if (d?.k !== "srv" || srvById(d.id) || !(op?.ids || []).includes(d.id) || !to || !srvById(to)) return;
  history.replaceState(history.state, "", DH.srv(to)); RHASH = location.hash;
}
// 池的顺序：专用在前；看板认得的池（核对过、有模板作业）没有服务器也列出来，好在里面开新的
function fPools() {
  const F = D.fleet;
  if (!F) return [];
  const pp = F.quota?.private_pools || [], m = new Map();
  for (const x of [...F.servers, ...F.mine]) if (!m.has(x.pool || "")) m.set(x.pool || "", F.quota ? pp.includes(x.pool) : !x.public);
  for (const [p, v] of Object.entries(fops().pools || {})) if (!m.has(p)) m.set(p, !!v.private);
  return [...m].sort((a, b) => b[1] - a[1] || byName(a[0], b[0]));
}
const byName = (a, b) => String(a).localeCompare(String(b), "en", {sensitivity: "base"});
const poolMembers = p => [...D.fleet?.servers || [], ...D.fleet?.mine || []].filter(x => (x.pool || "") === p)
  .sort((a, b) => !!a.off - !!b.off || byName(a.holder, b.holder) || byName(a.id, b.id));
const srvOrder = () => fPools().flatMap(([p]) => poolMembers(p)).map(x => x.id);       // 卡片墙上的顺序：抽屉里「上一台、下一台」照它
// 人员的顺序：有服务器的在前，再按名字首字母；人员卡与抽屉里的上一个、下一个照它
const peopleOrder = () => { const ppl = fops().people || {}, none = n => !(ppl[n].slots || []).length;
  return Object.keys(ppl).sort((a, b) => none(a) - none(b) || byName(a, b)); };
const myFleetHost = x => x.mine && ME?.fleet_person === x.holder;
const holdCount = n => [...D.fleet?.servers || [], ...D.fleet?.mine || []].filter(x => x.holder === n && !x.off).length;
// GPU 利用率去掉保活（吃满程度：只有保活时按 0 算，#51），返回 utilText 的结果（v 是数）。自用主机的样本（fleet.from_worker）带各卡 worker 的平均值 busy；
// 组内服务器只有探针此刻的样本（5 秒内每 0.5 秒采一次取平均），按样本的状态（与探针同口径；自用主机的 x.state 是平台操作口径，只跑 CPU 任务也算 job）
function fUtil(x) {
  const a = x.sample || {}, gs = a.gpus || [], b = gs.some(g => g.busy) ? utilAvg(gs.map(g => g.busy), gs.map(g => g.util)) : null;
  if (b) return utilText(b);
  return utilText(a.util == null ? null : a.state === "keepalive" ? {now: 0, ka: a.util, how: FLEET_HOW} : {now: a.util_real ?? a.util, how: FLEET_HOW});
}
const FLEET_HOW = "探针此刻的样本：5 秒内每 0.5 秒采一次取平均；只有保活时按 0 算";
// 组内页一张卡的利用率（小方块、抽屉悬停）：自用主机取 worker 的平均值，组内服务器取探针此刻的样本（按整台的状态认只有保活）
const fCardU = (g, st) => utilText(g.busy || (g.util == null ? null : st === "keepalive" ? {now: 0, ka: g.util, how: FLEET_HOW} : {now: g.util, how: FLEET_HOW}));
// 每张卡一排小方块：一块一张 GPU，填充高度是利用率（自用主机是 worker 的近 1 小时平均，组内服务器是探针此刻的样本）；只有保活时是灰的、高度是保活顶出来的
function gpuTiles(gs, st) {
  if (!gs?.length) return "";
  return `<div class="gt${gs.length < 3 ? " one" : ""}">${gs.map((g, k) => { const U = fCardU(g, st), ka = g.busy ? g.busy.ka != null : st === "keepalive";
    const f = Math.min(1, ((ka ? g.busy?.ka ?? g.util : U.v) || 0) / 100);
    return `<i ${tip(`GPU ${g.i ?? g.index ?? k}${g.name ? "：" + g.name : ""}\n利用率 ${U.big}（${U.span}）· 显存 ${mb2g(g.used)} / ${mb2g(g.total)} GiB${ka ? "\n只有保活在跑" : ""}`)}><b style="height:${Math.round(100 * f)}%;background:${ka ? KA : FSC[st] || "var(--muted)"}"></b></i>`; }).join("")}</div>`;
}
// 服务器上的操作按钮：放在这台的抽屉里，这台能做什么就给什么（抽屉的节两边一样，差别只在这里）。
// 组内服务器：立即探测、借来用、开保活、续租、换人、开机、关机；借用中只有立即探测（续租、换人、关机都会停掉自己的 worker，按钮在「借用」里）。
// 自用主机：没有探针（每轮采集读 worker 状态）、保活由 worker 管，只有续租（主机页概况条上的同一组 renewBtns：预备续租 / 取消预备 / 恢复放行 + 续租）、
// 换人、关机；能不能动看 x.state（平台操作口径，与 fleetops._probe_mine 同一个），红框、勾选都在 goRenew、offAsk、wizForm 里。
// 借来用、续租、关机点了先在按钮旁弹出确认；换人、开机步骤多，是抽屉里的向导
const poolSpecs = p => fops().pools?.[p]?.specs || [];
const specName = (p, id) => poolSpecs(p).find(s => s.specificationId === id)?.specificationName || id || "";
function srvBtns(x) {
  if (!canOps()) return opsNote();
  const dis = fops().op?.active ? "disabled" : "", specs = poolSpecs(x.pool);
  const swap = `<button class="btn" data-fop="wiz" data-kind="swap" data-id="${esc(x.id)}" ${dis} ${tip(x.mine ? "停掉这台的作业后立刻开新的：同一个池、沿用端口（可选换规格）；旧自用主机标成已归还\nworker 有任务在跑时不让换，先在主机页暂停队列" : "停掉这台的作业后立刻给别人开（也可以换给自己、可选换规格）：同一个池、沿用端口，空档只有十几秒，这台不会被别人占走")}>${icon("users")}换人</button>`;
  const move = specs.length === 1 ? "" : `<button class="btn" data-fop="wiz" data-kind="move" data-id="${esc(x.id)}" ${dis || !specs.length ? "disabled" : ""} ${tip(!specs.length ? "还没读到这个池的规格列表：先点「与平台核对」" : x.mine ? "同人同池停旧后立刻按新规格（物理节点）开新的，旧自用主机标成已归还\nworker 有任务在跑时不让换，先在主机页暂停队列" : "同人同池停旧后立刻按新规格（物理节点）开新的，空档十几秒；新节点重新登记并给出新连接命令")}>${icon("server")}换节点</button>`;
  const off = `<span class="vsep"></span>` + opPop("off", x, `<button class="btn danger" data-fop="offpick" data-id="${esc(x.id)}" ${dis} ${tip(x.mine ? "在平台上停掉你这台的作业，自用主机标成已归还（看板不再连它）。点了先出确认" : "在平台上停掉这台的作业；之后不探、不提醒租期、不算配额。点了先出确认")}>${icon("power")}关机</button>`,
    () => offAsk(x), () => offGo(x));
  if (x.mine) { const h = D.hosts.find(e => e.id === x.id); return (h ? renewBtns(h) : "") + (x.state === "down" ? "" : swap + move) + off; }
  const probe = x.off ? "" : `<button class="btn" data-probe="${esc(x.id)}" ${PROBING ? "disabled" : ""} ${tip("立即只读探一次（一条 ssh，约 10 秒）")}>${icon("renew")}${PROBING === x.id ? "探测中…" : "立即探测"}</button>`;
  if (borrowOf(x)) return probe;
  return probe + [x.reserve ? borrowPop(x, `<button class="btn" data-fop="bpick" data-id="${esc(x.id)}" ${dis} ${tip(`和 ${x.holder} 说好后借来给自己的项目跑：拉起 worker，不动平台；${x.using ? `${x.holder} 在轻用（${x.using}），给这人留出余量；` : ""}到约定时间不再放新任务，跑完自动还回去、开保活`)}>${icon("users")}借来用</button>`) : "",
    !x.off && (x.state === "idle" || /主机密钥/.test(x.error || "")) ? `<button class="btn" data-fop="ka" data-id="${esc(x.id)}" ${dis} ${tip(`ssh 上去开保活，已在跑就不重开，不碰平台。\n先探一次，在跑实验就不开；开后盯 ${fops().guard?.watch_min ?? 5} 分钟，有实验或这人自己的保活上来就让出。\n主机密钥变了的先清掉旧密钥`)}>${icon("zap")}开保活</button>` : "",
    !x.off && x.state !== "down" ? goRenew(x) : "",
    !x.off && x.state !== "down" ? swap : "",
    !x.off && x.state !== "down" ? move : "",
    x.off ? `<button class="btn pri" data-fop="wiz" data-kind="open" data-id="${esc(x.id)}" ${dis} ${tip("在平台上重开这一台（沿用端口，被占了就另挑），可换人、可选规格")}>开机</button>` : "",
    !x.off ? off : ""].join("");
}
// 卡片上一行「在跑什么」：自用主机列顶层的 work（平台操作口径：worker 的任务，含只用 CPU 的，加 labmon 之外的进程），组内服务器列探到的各组负载
const fWhat = x => { const a = x.sample || {}, work = (x.work || a.work || []).filter(w => w.kind !== "keepalive");
  return x.off ? `<span class="mut">${ts(x.off.t)} 关机（当时 ${esc(x.off.holder)}）</span>` : !x.state ? `<span class="mut">还没探到</span>`
    : x.state === "down" ? `<span style="color:var(--bad)">${esc(x.down)}</span>`
    : work.length ? work.slice(0, 2).map(w => `<span ${tip([w.label, ...(w.names || [])].join("\n"))}>${w.kind === "unknown" ? "？" : ""}${w.owner && w.owner !== x.holder ? `<b>${esc(w.owner)}</b> 的 ` : ""}${esc(w.label)}</span>`).join("、") + (work.length > 2 ? ` 等 ${work.length} 组` : "")
    : !x.sample ? `<span class="mut">${esc(x.error || "还没探到")}</span>`
    : (a.work || []).length ? `<span class="mut">只有${x.guard?.status === "ours" ? esc(x.guard.by || "看板") + "开的" : " GPU "}保活</span>` : `<span class="mut">没有 GPU 进程</span>`; };
const opName = k => fops().names?.[k] || k;           // 平台操作的中文名（fleetops.OP_NAMES）
const fBusy = x => { const op = fops().op; return op?.active && (op.ids || []).includes(x.id) ? `<span class="badge b-warn">${esc(opName(op.kind))}中…</span>` : ""; };
// 保活守护（fleet.guard_state）：这一台现在怎样。卡片的盾牌、抽屉、设置页、提醒条的文字都取它
const GDWHY = {job: "在跑实验", keepalive: "这人自己开着保活", unknown: "GPU 上有未识别的进程"};
// GPU 现在的情况（设置页里没开守护的那几行只写这个）
const gdSit = x => GDWHY[x.state] || (x.guard?.since ? `GPU 连着空了 ${dur(D.generated - x.guard.since)}` : "GPU 刚空下来");
// 守护只在探完一台时动手：t 之后（含 t）的第一次探测约在几点。每台按 interval 轮着探，已经过点的算现在
const probeAt = (x, t) => { const iv = D.fleet?.interval || 600, l = x.last || D.generated; return Math.max(D.generated, l + Math.max(1, Math.ceil((t - l) / iv)) * iv); };
function guardText(x) {
  const g = x.guard || {}, now = D.generated;
  if (g.status === "ours") return `${g.by || "看板"}开的保活在跑（${tsHM(g.t)} 开）` + (now < g.until ? `，盯到 ${tsHM(g.until)}：有实验或这人自己的保活上来就让出` : "：探测见到别的负载就让出");
  if (g.status === "wait") return g.act ? `${gdSit(x)}，下次探测（约 ${tsHM(probeAt(x, now))}）就开保活` : `${gdSit(x)}，约 ${tsHM(probeAt(x, g.at))} 开保活`;
  if (g.status === "fail") return g.at ? `没开成：${g.fail.msg}；约 ${tsHM(probeAt(x, g.at))} 再试` : g.fail.msg;
  if (g.status === "busy") return g.why ? `${g.why}，先不动` : x.state === "idle" ? "GPU 刚空下来，开始计时" : `${gdSit(x)}，不用开`;
  if (g.status === "off") return "没开守护";
  if (g.status === "borrowed") return "借来在用：保活由 worker 停启，守护不管；还了以后开保活";
  if (g.status === "worker") return "保活由 worker 停启（要卡时停、空闲时开），守护不管";
  return x.off ? "已关机" : "掉线或还没探到，恢复后再看";
}
// 盾牌：看板开的保活在跑，或守护开着（关机的不画）；计时中按空了多久从下往上填
function gdIcon(x) {
  const g = x.guard;
  if (!g || !(g.status === "ours" || g.on && !x.off && g.status !== "borrowed")) return "";
  const p = g.status === "wait" ? Math.max(.1, Math.min(1, (D.generated - g.since) / (g.at - g.since))) : g.status === "ours" ? 1 : 0;
  return `<span class="gd ${g.status}" ${tip("保活守护：" + guardText(x))}>${icon(g.status === "ours" ? "shieldck" : "shield")}${p ? `<span class="lv" style="--p:${p.toFixed(2)}">${icon("shield")}</span>` : ""}</span>`;
}
function fleetCard(x) {
  const now = D.generated, op = fops().op, d = route().drw, a = x.sample || {}, tags = [];
  const gs = a.gpus || [], vr = [a.vram_real, a.vram_total];
  const selfView = D.fleet?.scope === "self";
  if (x.mine) { tags.push(`<span class="chip" ${tip("worker 管任务与保活；续租、换人、关机在这台的详情里，任务与队列在主机页")}>${myFleetHost(x) ? "我的自用主机" : "自用"}</span>`); }
  else if (borrowOf(x)) { const b = hostBorrow(x);
    if (selfView) {
      const by = b.by ? `借给 ${b.by}` : "已借出", til = b.until ? ` · 到 ${tsNear(b.until)}` : "";
      const tt = `${by}（${ts(b.since)} 起）${b.until ? `，约定 ${tsNear(b.until)} 还` : ""}${b.returning ? "，跑完就还" : ""}`;
      tags.push(`<span class="badge b-warn" ${tip(tt)}>${esc(b.returning ? `${by} · 跑完就还` : by + til)}</span>`);
    } else tags.push(borrowBadge(b, now, "借用中 · ", b.for ? " · " + b.for.join("、") : "")); }
  else {
    const mb = manageBadge(x);
    if (mb) tags.push(mb);
    if (fShare(x)) tags.push(fShare(x));
    if (x.idle_h >= FREE_H) tags.push(`<span class="badge b-ok" ${tip(selfView ? `最近连着 ${x.idle_h} 小时只有保活或空闲` : `最近连着 ${x.idle_h} 小时只有保活或空闲：可以问问 ${x.holder} 还用不用，不用就换给别人`)}>闲置 · ${x.idle_h >= 48 ? Math.floor(x.idle_h / 24) + " 天" : x.idle_h + " 小时"}</span>`);
    if (x.error && !x.down) tags.push(`<span class="sm" style="color:var(--warn)" ${tip(x.error)}>上次没连上</span>`);
    if (!selfView && !x.off && !fops().boot?.[x.id]?.has_clone) tags.push(`<span class="sm" style="color:var(--warn)" ${tip("还没存平台克隆出的整份配置：开机、换人要用它。点「与平台核对」补存")}>开机参数不全</span>`);
  }
  if (!x.off && x.lw?.risk) tags.push(heatTag(lwHeat(x.lw)));
  const lease = x.off || !x.lease_end ? "" : `<span ${tip(`${ts(x.lease_end)} 到期`)}>${leaseHtml(x.lease_end, "租期")}</span>`;
  const U = fUtil(x), nh = holdCount(x.holder), h = DH.srv(x.id);
  const num = x.off ? "" : `<div class="fc-gpu">${gpuTiles(gs, x.state)}
      <div class="fc-num" ${tip(U.tip)}><b>${U.big}</b><span class="sm mut">GPU${U.ka || U.span ? " · " + esc(U.ka || U.span) : ""}</span></div>
      <div class="fc-num"><b>${vr[1] ? `${mb2g(vr[0])}<small> / ${mb2g(vr[1])} GiB</small>` : "—"}</b><span class="sm mut">显存（去掉保活）</span></div></div>`;
  return `<div class="fc${d?.k === "srv" && d.id === x.id ? " sel" : ""}${x.off ? " off" : ""}" id="fc-${esc(x.id)}" data-drw="${esc(h)}" tabindex="0" role="button" aria-label="${esc(x.holder)}（${esc(x.id)}）的详情">
    <div class="fc-hd">${pfav(x.holder, `<span class="cdot" style="background:${fDot(x)}"></span>`)}
      <div class="fc-t"><span class="fc-n"><b>${esc(x.holder)}</b>${gdIcon(x)}</span><span class="sm mut fc-sub"><span class="fc-ad">${esc(fNode(x.ssh))}:${esc(x.port)}</span><span class="fc-more">${x.job && x.job !== x.holder ? ` · 作业 ${esc(x.job)}` : ""}${nh > 1 ? ` · <span ${tip(`${x.holder} 现在共占 ${nh} 台`)}>共 ${nh} 台</span>` : ""}</span></span></div>
      <div class="fc-st">${fBusy(x) || fBadge(x)}</div>${icon("chev")}</div>
    ${num}<div class="fc-what sm">${fWhat(x)}</div>
    ${x.off ? "" : `<div class="fc-hs">${hourStrip(x, false)}</div>`}
    <div class="fc-ft sm">${lease}${tags.join("")}${x.off && canOps() ? `<button class="btn mini-btn" data-fop="wiz" data-kind="open" data-id="${esc(x.id)}" ${op?.active ? "disabled" : ""}>开机</button>` : ""}</div></div>`;
}
function poolGroup([p, priv]) {
  const q = D.fleet.quota || {}, info = fops().pools?.[p] || {}, op = fops().op;
  const n = priv ? q.private : q.public, max = priv ? q.private_max : q.public_max, full = max != null && n >= max;
  const specs = info.specs || [], sp = specs.find(x => x.specificationId === info.spec);
  const spText = specs.length > 1 ? `<span ${tip(`${ts(fops().pools_t)} 与平台核对时读的`)}>各规格空：${specs.map(s => `${esc(s.specificationName || s.specificationId)} ${esc(s.quantityAvailable)}/${esc(s.specificationAmount)}`).join(" · ")}</span>`
    : sp ? `<span ${tip(`${ts(fops().pools_t)} 与平台核对时读的`)}>平台上这种规格空 ${esc(sp.quantityAvailable)} / ${esc(sp.specificationAmount)}</span>` : "";
  const head = [(info.nodes || []).length ? `节点 ${esc(info.nodes.map(fNode).join("、"))}` : "", max != null ? `${priv ? "专用" : "公用"}池已开 ${n} / ${max} 台${priv ? "" : "（全组）"}${full ? `<span class="badge b-mute" style="margin-left:6px" ${tip("到上限了：要给别人用，在一台上点「换人」，或先关掉一台")}>已满</span>` : ""}` : "",
    spText].filter(Boolean).join(" · ");
  const add = !p || full || !canOps() ? "" : `<button class="fc add" data-fop="wiz" data-kind="open" data-pool="${esc(p)}" ${info.ref && !op?.active ? "" : "disabled"} ${info.ref ? "" : tip("这个池还没有模板作业：先与平台核对")}>
        <span class="plus">+</span><b>开新的</b><span class="sm mut">${max == null ? "没写上限" : `还能开 ${max - n} 台`}；端口自动挑</span></button>`;
  return `<div class="pg"><div class="pg-hd"><b>${esc(p || "没写资源池")}</b><span class="chip">${priv ? "专用" : "公用"}</span><span class="sm mut">${head}</span></div>
    <div class="fwall">${poolMembers(p).map(fleetCard).join("")}${add}</div></div>`;
}
// 组内服务器页：指标与提醒条在上，下面分选项卡；当前选项卡写在地址里（#/fleet/people），刷新后不变
const FTABS = [["slots", "服务器"], ["people", "人员"], ["disk", "共享盘"]];
function fleetPage() {
  const F = D.fleet, r = route(), tab = r.tab;
  const it = (k, v, t = "", bad = false) => `<span class="fs" ${t ? tip(t) : ""}><span class="mut">${k}</span><b style="${bad ? "color:var(--bad)" : ""}">${v}</b></span>`;
  if (LIVE && ME && !ME.admin) {
    if (tab === "people") return '<div class="card empty">组内人员与账号管理只有管理员看得到。</div>';
    if (tab === "disk") return (F && !F.error ? fleetAlertBox(fleetAlerts(), false, true) : "") + diskTab();
    if (!F || F.error) return `<div class="card empty">你名下当前没有持有的服务器。${D.disk?.df ? ' <a href="#/fleet/disk">查看共享盘</a>' : ""}</div>`;
    const all = [...F.servers, ...F.mine], al = fleetAlerts(), cnt = k => all.filter(x => x.state === k).length;
    const due = fleetDue(), dueIds = new Set(due.map(x => x.id));
    const lease = all.filter(x => x.lease_end && !x.off && !dueIds.has(x.id)).sort((a, b) => a.lease_end - b.lease_end)[0];
    const other = ["keepalive", "unknown", "idle", "down"].filter(k => cnt(k)).map(k => `${FSN[k]} ${cnt(k)}`).join("，");
    const pools = fPools();
    let s = `<div class="fsum">${[
      it("我的服务器", `${cnt("job")} / ${all.length} 台在跑`, other || "其余没有异常"),
      D.disk?.df ? it("共享盘剩", `${GB(D.disk.df.free)} GiB${D.disk.eta != null ? ` · 约 ${D.disk.eta} 天满` : ""}`,
        `已用 ${GB(D.disk.df.used)} / ${GB(D.disk.df.total)} GiB（${D.disk.root}）${D.disk.eta != null ? `\n按近 24 小时的速度约 ${D.disk.eta} 天满` : ""}`, D.disk.df.free < D.disk.warn_gb * GIB) : "",
      it(due.length ? "下一个到期" : "最早到期", lease ? `${leaseHtml(lease.lease_end)} · ${esc(lease.id)}` : "—", lease ? `${lease.id} ${ts(lease.lease_end)} 到期` : due.length ? "其余都没有租期" : "还没探到容器启动时间")].join("")}</div>` + fleetAlertBox(al, false);
    s += `<div class="card"><div class="chd wrap"><b>我的服务器</b><span class="sm mut">按资源池分组，约 ${Math.round((F.interval || 600) / 60)} 分钟只读探一次；点卡片看详情与曲线</span></div>
      ${pools.map(poolGroup).join("") || `<div class="empty">你名下当前没有持有的服务器。</div>`}${fLegend()}</div>`;
    if (F.unknown?.length) s += `<div class="card"><div class="chd"><b>未识别的 GPU 进程</b><span class="sm mut">显卡上有这些进程，但在这台的容器里找不到</span></div><div class="scroll"><table>
      <tr><th>服务器</th><th>负载</th><th class="r">显存</th><th>目录</th></tr>${F.unknown.map(w => `<tr class="link" tabindex="0" data-drw="${esc(DH.srv(w.id))}"><td class="nw"><b>${esc(w.holder)}</b> <span class="mut sm">${esc(w.id)}</span></td>
        <td class="sm" ${tip((w.chain || []).join("\n"))}>${esc(w.label)}${(w.names || []).length ? nameSum(w.names) : ""}</td><td class="r">${mb2g(w.mb)} GiB</td><td class="sm"><code>${esc(w.cwd || "—")}</code></td></tr>`).join("")}</table></div></div>`;
    return s;
  }
  if ((!F || F.error) && tab === "people") {
    const note = F?.error ? fleetAlertBox(fleetAlerts()) : '<div class="card empty">还没有配置组内服务器。可以管理已登记人员的账号。在看板主机可用 <code>labmon user person &lt;人员名&gt;</code> 登记人员。</div>';
    return note + peopleTab();
  }
  if (!F) return `<div class="card empty">主机清单（~/.config/labmon/hosts.toml）里没有 [fleet] 段。写法见 labmon 仓库 docs/fleet.md。</div>`;
  if (F.error) return fleetAlertBox(fleetAlerts()) + (tab === "disk" ? diskTab()          // 兜底只有 error：共享盘另有数据照样看
    : `<div class="card empty">这一轮组内服务器的数据没读出来（原因见上），下一轮采集通常就好。</div>`);
  const q = F.quota || {}, all = [...F.servers, ...F.mine], al = fleetAlerts();
  const cnt = k => all.filter(x => x.state === k).length;
  const sh = F.servers.filter(x => x.sample?.share && x.state !== "down"), free = F.servers.filter(x => x.idle_h >= FREE_H);
  const due = fleetDue(), dueIds = new Set(due.map(x => x.id));       // 24 小时内到期的已在提醒条里，指标卡给下一个
  const lease = all.filter(x => x.lease_end && !x.off && !dueIds.has(x.id)).sort((a, b) => a.lease_end - b.lease_end)[0];
  const other = ["keepalive", "unknown", "idle", "down"].filter(k => cnt(k)).map(k => `${FSN[k]} ${cnt(k)}`).join("，");
  const pubFull = q.public_max != null && q.public >= q.public_max;
  const o = fops(), P = fPanel(), pools = fPools(), act = o.op?.active ? "disabled" : "";
  // 指标条与完整的提醒条只在「服务器」选项卡（#15）；人员、共享盘只留一行可展开的提醒，共享盘有自己的指标
  let s = tab === "people" || tab === "disk" ? fleetAlertBox(al, true, true) : `<div class="fsum">${[
    it("专用池", `${q.private} / ${q.private_max ?? "—"} 台`, (q.private_pools || []).join("\n")),
    it("公用池", `${q.public} / ${q.public_max ?? "—"} 台${pubFull && q.public <= q.public_max ? " · 已满" : ""}`, q.public_max == null ? "没写上限" : q.public > q.public_max ? "超过全组上限，可能被强制关机" : pubFull ? "到全组上限了，再开会被警告" : `全组还能开 ${q.public_max - q.public} 台`, q.public_max != null && q.public > q.public_max),
    it("在跑实验", `${cnt("job")} / ${all.length} 台`, other || "其余没有异常"),
    it("可共享 · 闲置", `${sh.length} · ${free.length} 台`, `可共享：${sh.map(x => x.holder).join("、") || "—"}\n闲置：${free.map(x => x.holder).join("、") || "—"}\n\n可共享：除保活外显存 < ${100 * F.share.vram}%、利用率 < ${F.share.util}%、CPU < ${100 * F.share.cpu}% 配额、内存 < ${100 * F.share.mem}% 上限\n闲置：最近连着 ${FREE_H} 小时以上只有保活或空闲`),
    D.disk?.df ? it("共享盘剩", `${GB(D.disk.df.free)} GiB${D.disk.eta != null ? ` · 约 ${D.disk.eta} 天满` : ""}`,
      `已用 ${GB(D.disk.df.used)} / ${GB(D.disk.df.total)} GiB（${D.disk.root}）${D.disk.eta != null ? `\n按近 24 小时的速度约 ${D.disk.eta} 天满` : ""}`, D.disk.df.free < D.disk.warn_gb * GIB) : "",
    it(due.length ? "下一个到期" : "最早到期", lease ? `${leaseHtml(lease.lease_end)} · ${esc(lease.holder)}` : "—", lease ? `${lease.holder} ${ts(lease.lease_end)} 到期${due.length ? `\n${due.map(x => x.holder).join("、")} 一天内到期，见提醒条` : ""}` : due.length ? "其余都没有租期" : "还没探到容器启动时间")].join("")}</div>` + fleetAlertBox(al);
  const ph = P.h && (P.sid && srvById(P.sid) ? DH.srv(P.sid) : !P.sid && P.pool ? DH.new(P.pool) : null);   // 面板在哪个抽屉里；批量续租、核对不在抽屉里，放在页面上
  if (ph && !(r.drw && DH[r.drw.k](r.drw.id) === ph)) s += opStrip(P, ph);
  if (P.h && !ph) s += `<div class="card" id="fpanel">${P.h}</div>`;
  if (FERR && !P.h && !r.drw) s += `<div class="rn-err" style="margin:12px 0 0">${esc(FERR)}</div>`;
  if (tab === "people") return s + peopleTab();
  if (tab === "disk") return s + diskTab();
  s += `<div class="card"><div class="chd wrap"><b>服务器</b><span class="sm mut">按资源池分组，约 ${Math.round(F.interval / 60)} 分钟只读探一次；点卡片看详情与操作</span><span class="sp"></span>
    ${syncLine()}
    <button class="btn" data-drw="${DH.guard()}" ${tip("GPU 连着空闲就自动开保活，有实验上来就让出：在这里按台开关、改时间")}>${icon("shield")}保活守护 <span class="mut">${F.servers.filter(x => !x.off && x.guard?.on).length}</span></button>
    ${canOps() ? `${syncPop(`<button class="btn" data-fop="sync" id="fleet-sync" ${act} ${tip("读作业列表，逐台核对作业名、资源池、端口 22→port，更新开机参数，补存克隆配置；再读资源池规格、挂载卷与镜像。只读，不改平台上的作业。对不上的进提醒条，改了的进事件")}>${icon("cloud")}与平台核对</button>`)}
      <button class="btn" data-drw="${DH.renew()}" ${act} ${tip("选几台一起续租：只登录一次，逐台停旧作业、提交新作业、等运行，再 ssh 上去开保活")}>${icon("hourglass")}续租</button>` : opsNote()}</div>
    ${pools.map(poolGroup).join("") || `<div class="empty">[fleet] 里还没有服务器。</div>`}${fLegend()}</div>`;
  if (F.unknown.length) s += `<div class="card"><div class="chd"><b>未识别的 GPU 进程</b><span class="sm mut">显卡上有这些进程，但在这台的容器里找不到（多半是同一宿主机上别的容器在用这张卡）</span></div><div class="scroll"><table>
    <tr><th>服务器</th><th>负载</th><th class="r">显存</th><th>目录</th></tr>${F.unknown.map(w => `<tr class="link" tabindex="0" data-drw="${esc(DH.srv(w.id))}"><td class="nw"><b>${esc(w.holder)}</b> <span class="mut sm">${esc(w.id)}</span></td>
      <td class="sm" ${tip((w.chain || []).join("\n"))}>${esc(w.label)}${(w.names || []).length ? nameSum(w.names) : ""}</td><td class="r">${mb2g(w.mb)} GiB</td><td class="sm"><code>${esc(w.cwd || "—")}</code></td></tr>`).join("")}</table></div></div>`;
  return s;
}
// 选项卡放在顶栏里（顶栏吸顶，滚下去也能切）；数字：台数、人数、核对对不上的、最近 24 小时的事件
function fleetTabs(tab) {
  const F = D.fleet, o = fops(), K = D.disk;
  const diskCt = K?.df ? `<span class="ct${(K.alerts || []).some(a => a.notify) ? " bad" : ""}" ${tip(`已用 ${GB(K.df.used)} / ${GB(K.df.total)} GiB`)}>${Math.round(100 * K.df.used / K.df.total)}%</span>` : "";
  if (LIVE && ME && !ME.admin) {
    const tabs = [];
    if (F && !F.error) tabs.push(["slots", "我的服务器", `<span class="ct">${F.servers.length + F.mine.length}</span>`]);
    if (K?.df) tabs.push(["disk", "共享盘", diskCt]);
    return tabs.length ? `<nav class="ftabs">${tabs.map(([k, n, c]) => `<a class="ftab${k === tab ? " on" : ""}" href="#/fleet${k === "slots" ? "" : "/" + k}">${n}${c}</a>`).join("")}</nav>` : "";
  }
  const ct = {slots: `<span class="ct">${F.servers.length + F.mine.length}</span>`, people: `<span class="ct">${Object.keys(o.people || {}).length}</span>`, disk: diskCt};
  return `<nav class="ftabs">${FTABS.map(([k, n]) => `<a class="ftab${k === tab ? " on" : ""}" href="#/fleet${k === "slots" ? "" : "/" + k}">${n}${ct[k]}</a>`).join("")}</nav>`;
}
// 操作面板在某个抽屉里而那个抽屉没开着：页面顶上缩成一条，点「查看」打开它
function opStrip(P, h) {
  const op = fops().op, x = P.sid ? srvById(P.sid) : null, st = op?.steps || [];
  const what = st.length ? `${opName(op.kind)}${op.active ? `进行中：第 ${Math.min(st.length, st.filter(v => v.state === "ok").length + 1)} / ${st.length} 步` : op.error ? "停下了" : "完成"}`
    : FCAP ? "等你输验证码" : FWIZ ? `${FWIZ.kind === "swap" ? "换人" : FWIZ.kind === "move" ? "换节点" : "开机"}向导开着` : "续租确认开着";
  return `<div class="fstrip${op?.error ? " bad" : ""}"><span>${esc(what)}${x ? `<span class="mut"> · ${esc(fName(x))}</span>` : P.pool ? `<span class="mut"> · ${esc(P.pool)}</span>` : ""}</span><a href="${esc(h)}" data-drw="${esc(h)}">查看</a></div>`;
}
async function loadSer(id) {
  SER[id] = {...(SER[id] || {}), busy: true};
  try {
    const r = await api(`fleet/${encodeURIComponent(id)}.json`, {cache: "no-store"}, {retry: true});
    if (!r.ok) throw new Error("HTTP " + r.status);
    SER[id] = {rows: (await r.json()).series, at: Date.now()};
  } catch (e) { SER[id] = {rows: SER[id]?.rows || [], at: Date.now(), err: e.message || String(e)}; }
  render(false);
}
async function probeNow(id) {
  PROBING = id; delete PERR[id]; render(false);
  try {
    const {ok, status, body: j} = await postJSON(`fleet/probe/${encodeURIComponent(id)}`, {}, {retry: true, json: true});
    if (!ok) throw new Error(j.error || "HTTP " + status);
    D.fleet = j; FKEEP = {t: Date.now() / 1000, fleet: j}; delete SER[id];
  } catch (e) {
    PERR[id] = "探测失败：" + (e.message || e);
    if (e.responseLost) poll();
  }
  PROBING = null; render(false);
}

/* ---------- 详情抽屉：桌面从右侧滑出、窄屏从底部升起，背后的卡片墙不动。地址写在 hash 里
   （#/fleet/srv/<id>、#/fleet/people/<名字>、#/fleet/new/<池>，不记历史），Esc、点空白处、右上角 × 都能关 ---------- */
const DH = {srv: id => "#/fleet/srv/" + encodeURIComponent(id), person: n => "#/fleet/people/" + encodeURIComponent(n), new: p => "#/fleet/new/" + encodeURIComponent(p), guard: () => "#/fleet/guard",
  renew: (id = "") => "#/fleet/renew" + (id ? "/" + encodeURIComponent(id) : ""),
            proj: id => `#/p/${encodeURIComponent(id)}/manage`, arch: () => "#/archived", assign: id => `#/host/${encodeURIComponent(id)}/assign`,
            ext: (id = "") => id ? "#/ext/" + encodeURIComponent(id) : "#/ext/new",
            pool: (id = "") => id ? "#/pool/edit/" + encodeURIComponent(id) : "#/pool/new"};
let DKEY = null, DLAST = "";
const dHead = (av, title, sub, right = "", mark = "", status = "") => { const b = dBack();
  return `<header class="dh">${b ? `<button class="ib" data-dback aria-label="返回${esc(b)}" ${tip(`返回「${b}」`)}>${icon("chevl")}</button>` : ""}${av}
  <div class="fc-t"><span class="fc-n"><b>${title}</b>${mark}</span>${sub ? `<span class="sm mut">${sub}</span>` : ""}${status}</div>${right}
  <button class="ib" data-dclose aria-label="关闭" ${tip("关闭（Esc）")}>${icon("x")}</button></header>`; };
// 从一个抽屉里点开另一个（服务器、人、保活守护）：记下来路，只记一级，返回键回去；关掉抽屉或从页面上点开的就清掉
let DBACK = null;
const dBack = () => { const d = DBACK && route(DBACK).drw;
  return !d || DBACK === location.hash ? "" : d.k === "guard" ? "保活守护" : d.k === "renew" ? "续租" : d.k === "proj" ? "管理" : d.k === "arch" ? "已归档" : d.k === "ext" ? (d.id || "外部主机") : d.k === "pool" ? (d.id ? `编辑 ${d.id}` : "新建算力池") : d.k === "srv" ? srvById(d.id)?.holder || d.id : d.k === "new" ? "开新的" : d.id === "+" ? "新的人" : d.id; };
const dNav = (ids, id) => ids.length > 1 && ids.includes(id) ? `<span class="dnav"><button class="ib" data-dnav="-1" aria-label="上一个" ${tip("上一个（←）")}>${icon("chevl")}</button><button class="ib" data-dnav="1" aria-label="下一个" ${tip("下一个（→）")}>${icon("chevr")}</button></span>` : "";
const dSec = (title, body, extra = "", id = "") => `<section class="card"${id ? ` id="${id}"` : ""}><div class="chd wrap"><b>${title}</b>${extra}</div>${body}</section>`;
const kvHtml = kv => `<div class="kv">${kv.map(([k, v]) => `<span>${k}</span><span>${v}</span>`).join("")}</div>`;
const fSum = v => v?.n ? [...["job", "keepalive", "idle", "unknown", "down", "lent"].filter(k => v.frac[k] >= 0.01).map(k => `${PSN[k]} ${fpct(v.frac[k])}`), `可共享 ${fpct(v.share)}`,
  v.util_job != null ? `实验时利用率 ${Math.round(v.util_job)}%` : ""].filter(Boolean).join(" · ") + ` <span class="mut">（有数据 ${v.hours} 小时）</span>` : `<span class="mut">还没有数据</span>`;
const fErr = () => FERR ? `<div class="rn-err">${esc(FERR)}</div>` : "";
/* ---------- 借用（borrow.py）：借来的组内服务器、换给自己借来的自用主机 ---------- */
let BMSG = null;   // 借用刚做完的说明 {id, msg}
const borrowOf = x => x?.borrow?.since && !x.borrow.returned ? x.borrow : null;    // 组内服务器上记的借用（还没还的；since、until、returned 都是 epoch 秒）
const hostBorrow = x => D.hosts.find(e => e.id === x.id)?.borrow || {...borrowOf(x), holder: x.holder};   // borrow.check 算好的：截止、要还了、对方在用
// 借卡弹窗里给对方留多少的那句：勾了说好不用（要有约定时间）换成那一句，不再接“按近 7 天推荐、超出的部分不放”
function borrowSay(x, B) {
  const aw = B.away && whenT(B.until);
  return aw ? `${esc(keepText({...x.reserve, away: aw}))}。正在跑的照常跑完；真撞上容器内存上限时先杀我们的，被挤掉的重新排队。`
    : `${esc(keepText({...x.reserve, hold: !!x.using}))}：按 ${esc(x.holder)} 近 7 天空着或轻用时的用量推荐（这些时候最多用到显存 ${x.reserve.peak.vram} GiB、内存 ${x.reserve.peak.ram} GiB、CPU ${x.reserve.peak.cpu} 核）。
      对方超出的部分不放新任务；正在跑的照常跑完，快撞上限时提醒你和这人商量；真撞上容器内存上限时先杀我们的，被挤掉的重新排队。`;
}
// 借卡弹窗里跟着约定时间与勾选就地变的（焦点还在控件上，render(false) 会被 selecting 跳过）：说明句、「说好不用」能不能勾
function borrowSync() {
  const x = srvById(POP.id), say = document.getElementById("bfsay"), ck = document.getElementById("bfaway");
  if (say && x?.reserve) say.innerHTML = borrowSay(x, POP);
  if (ck) { ck.disabled = !POP.until; document.getElementById("bfawayno").hidden = !!POP.until; }
}
// 借来用、改约定的表单（在按钮旁的弹窗里，输入记在 POP：sel 给哪些项目、until 输入框里的时间、ok、busy、err）
function borrowForm(x) {
  const B = POP, edit = !!borrowOf(x), ps = D.projects || [];
  const rule = `约定时间前跑不完的任务不放行；到点不停正在跑的，跑完自动还回去、开保活。${x.lease_end ? `${esc(x.holder)} 的租期 ${ts(x.lease_end)} 到期，最晚截止到它前 1 小时。` : ""}`;
  return `<div class="rn-note">${edit ? `<b>改约定</b>：${rule}` : `<b>借 ${esc(x.holder)} 的这台</b>给自己的项目跑：在上面拉起 worker（环境与数据在共享盘上），平台作业还是 ${esc(x.holder)} 的。
      <span class="sm mut">${rule}</span>`}</div>
    <div class="rn"><span class="sm">给哪些项目用</span>${ps.map(p => `<label class="sm"><input type="checkbox" data-bfor="${esc(p.id)}" ${B.sel.has(p.id) ? "checked" : ""}> ${esc(p.title || p.id)}</label>`).join("")}
      <span class="sm mut">都不勾就是自己的项目都能用</span></div>
    ${whenPick("fbuntil", "约定还的时间", B.until, "不约定", `不约定：只按对方租期截止${x.lease_cut ? `（${tsNear(x.lease_cut)}）` : ""}`, {fresh: B.wnow, cap: x.lease_cut})}
    ${edit || !x.reserve ? "" : `<div class="sm mut" id="bfsay">${borrowSay(x, B)}</div>`}
    <label class="sm pop-ck"><input type="checkbox" id="bfaway" data-baway ${B.away && B.until ? "checked" : ""} ${B.until ? "" : "disabled"}> ${esc(x.holder)} 说好约定时间前不用：不给这人留余量，空着的都能用<span class="mut" id="bfawayno" ${B.until ? "hidden" : ""}>（说好不用要有个时限：先选约定时间）</span></label>
    ${edit ? "" : `<label class="sm pop-ck"><input type="checkbox" data-bok ${B.ok ? "checked" : ""}> 已和 ${esc(x.holder)} 说好</label>`}
    ${edit || !x.using ? "" : `<label class="sm pop-ck"><input type="checkbox" data-blight ${B.light ? "checked" : ""}> ${esc(x.holder)} 在轻用（${esc(x.using)}），也借</label>`}
    ${B.err ? `<div class="rn-err">${esc(B.err)}</div>` : ""}`;
}
const borrowGo = x => { const B = POP, edit = !!borrowOf(x);
  return `<button class="btn pri" data-fop="bgo" data-id="${esc(x.id)}" ${(edit || (B.ok && (!x.using || B.light))) && !B.busy && !fops().op?.active ? "" : "disabled"} ${edit ? "" : tip("在后台做，进度显示在这台的抽屉里：先探一次（对方空着，或轻用且勾了「也借」），查共享盘与 Python，写进主机清单、下指令（带给对方留的量），再拉起 worker")}>${B.busy ? (edit ? "正在保存…" : "正在开始…") : edit ? "保存" : "借来用"}</button>`; };
// 借来用（组内服务器抽屉）、改约定（借用的按钮）：点了在按钮旁弹出表单，缺省是现在的约定
const borrowPop = (x, btn) => { const open = popIs("bor", x.id); return popConfirm(btn, open, open ? borrowForm(x) : "", open ? borrowGo(x) : "", true); };
function borrowSec(x) {
  const b = borrowOf(x);
  if (!b) return BMSG?.id === x.id ? dSec("借用", `<div class="rn-state">${esc(BMSG.msg)}</div>`) : "";
  if (D.fleet?.scope === "self") {
    const say = `${b.by ? `已借给 ${b.by}` : "已借出"}${b.returning ? "（不再放新任务，在跑的跑完就还）" : ""}`;
    const kv = [["借出", ts(b.since)], ...(b.by ? [["借用人", esc(b.by)]] : []),
      ["约定还", b.until ? tsNear(b.until) : "没约定"]];
    return dSec("借用", `<div class="rn-state">${esc(say)}</div>` + kvHtml(kv));
  }
  const hb = hostBorrow(x), run = hostRun(x), wd = D.hosts.find(e => e.id === x.id)?.status?.watchdogs || {};
  const wdt = ws => ws.map(w => `PID ${w.pid}（${w.cmd}：${w.why}）`).join("、");   // worker 找到的保活看门狗
  const kv = [["借来", ts(b.since)], ...(b.by ? [["借用人", esc(b.by)]] : []), ["约定", b.until ? tsNear(b.until) : "没约定"], ["截止", hb.cutoff ? `${tsNear(hb.cutoff)} <span class="mut">${esc(hb.why || "")}</span>` : "—"],
    ["给", b.for ? b.for.map(esc).join("、") : "自己的项目"], ["在跑", run.length ? run.map(j => esc(j.name)).join("、") : `<span class="mut">没有任务</span>`],
    ...hb.keep ? [[hb.keep.away ? "说好不用" : "给对方留", esc(keepText(hb.keep, true)) + (hb.other ? ` <span class="mut">对方此刻：显存 ${mb2g(Object.values(hb.other.vram || {}).reduce((a, v) => a + v, 0))} GiB、内存 ${hb.other.ram ?? "—"} GiB、CPU ${hb.other.cpu ?? "—"} 核</span>` : "")]] : [],
    ...wd.frozen?.length || wd.seen?.length ? [["看门狗", [wd.frozen?.length ? `暂停着（还卡时恢复）：${esc(wdt(wd.frozen))}` : "",
      wd.seen?.length ? `<span class="mut">找到、没动：${esc(wdt(wd.seen))}</span>` : ""].filter(Boolean).join("<br>")]] : []];
  const btns = !LIVE ? "" : `<div class="rn"><a class="btn" href="#/host/${encodeURIComponent(x.id)}">主机页</a>${borrowBtns(x)}</div>`;
  const downTip = x.state === "down" ? `<div class="rn-state bad">这台已掉线，ssh 连不上时无法直接归还或续租：若怀疑作业已被平台停掉，先在组内服务器页点「与平台核对」，确认作业不在运行后会自动按已停归还，之后即可续租。</div>` : "";
  return dSec("借用", `<div class="rn-state${hb.back?.length || hb.overdue ? " bad" : ""}">${esc(borrowText(hb, D.generated))}</div>`
    + downTip + (BMSG?.id === x.id ? `<div class="sm mut">${esc(BMSG.msg)}</div>` : "") + kvHtml(kv) + btns);
}
// 按钮旁的小弹窗：点按钮在它下面弹出说明与「确认 / 取消」，同一时间只开一个（POP 记着是哪个）。点外面或按 Esc 等于取消（closePop）；
// 里面有输入的（表单、验证码：keep）点外面不关，只认 Esc 与「取消」。btn：触发按钮；open：开着；body：说明或表单；go：确认按钮。
// 宽屏：弹窗超出所在区域（抽屉、主内容区，再与视口取交）左右边时 fitPops 往回挪。窄屏（≤860）弹窗贴在屏幕底部（CSS 定 position: fixed），fitPops 只量软键盘
// 占掉的高度（--kb）与看得见的高度（--vvh），弹窗随之上移、变矮，确认键不被键盘挡住
let POP = null;    // 开着的弹窗 {k: 哪种, id: 哪台, ack: 勾了「可以中断」}；借用表单的输入也记在这里
const popIs = (k, id) => POP?.k === k && POP.id === id;
function popClose() { POP = null; if (FCAP?.pop) FCAP = null; }      // 弹窗里取的验证码随弹窗作废（批量续租抽屉里验证码的「取消」同样经它）
function popToggle(k, id, extra = {}) { const same = popIs(k, id); popClose(); if (!same) POP = {k, id, ack: false, ...extra}; }   // 再点一下收起
const popX = `<button type="button" class="btn" data-fop="popx" data-pop-x>取消</button>`;
const popConfirm = (btn, open, body, go, keep = false) => `<div class="popw">${btn}${open ? `<div class="pop${keep ? " keep" : ""}" role="dialog">${body}${go == null ? "" : `<div class="pop-b">${go}${popX}</div>`}</div>` : ""}</div>`;
const closePop = (all = true) => document.querySelector(`.pop${all ? "" : ":not(.keep)"} [data-pop-x]`)?.click();
// 弹窗里选一个将来的时刻（借卡的约定、改约定、优先的到期都用它）：日期框（type=date）+ 时、分两个下拉（分按 5 分钟一档；已有的值不在档上时另加它那一分），
// 下面一排小签：「现在」、往后加的相对量（在当前的值上累加，可连点）、清空（不约定、无期限）；再一句“到几点、还有多久”（tsNear、dur）。
// 不用 datetime-local：有的浏览器只弹日期、时分要手打（Issue #26）。
// 值记在 POP.until：完整的是 dtLocal 的写法（YYYY-MM-DDTHH:MM），空串是不约定，没填完的是缺了几段的同一写法（如 "T15:25"）；提交时经 whenT 换成 epoch 秒。
// 刚打开或点了「现在」时值是下一个 5 分钟档（whenNow），POP.wnow 为真：说明行写“请选一个将来的时刻”（不标红），点确认时照样拦下；手改或点 + 就清掉。
// o.fresh：上面的 POP.wnow；o.cap：最晚截止（借用是对方租期截止），晚于它的说明行写按它截止。
// 过去的、没填完的（日期只填一半时 value 为空、validity.badInput 为真，不能当成不约定）控件与说明行标红，点确认时 whenStop 拦下
const dtLocal = t => { if (!t) return ""; const d = new Date(t * 1000); return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`; };   // epoch 秒 → 控件的值
const whenT = v => { const t = v ? new Date(v).getTime() / 1000 : NaN; return t > 0 ? t : null; };
const whenHalf = v => !!v && !/^\d{4}-\d\d-\d\dT\d\d:\d\d$/.test(v);   // 没填完：日期、时、分缺了几段
const whenPast = v => !!v && !(whenT(v) > Date.now() / 1000);       // 填了、但没填完或已经过去
function whenNow() {                                                 // 现在（取整到分）的下一个 5 分钟档：控件的缺省值，也是「现在」小签
  const d = new Date(), m = d.getMinutes() + (d.getSeconds() || d.getMilliseconds() ? 1 : 0);
  d.setMinutes(Math.ceil(m / 5) * 5, 0, 0);
  return d.getTime() / 1000;
}
const whenInit = t => t ? {until: dtLocal(t), wnow: false} : {until: dtLocal(whenNow()), wnow: true};   // 打开弹窗时：已有的约定照旧，没有的从现在起
const WHEN_ADD = [["+1 小时", 1], ["+3 小时", 3], ["+1 天", 24], ["+3 天", 72]];   // 往后加的小签（小时）
function whenAdd(v, h) {                                             // 在当前的值上往后加 h 小时；空着、没填完、已经过去的从现在（下一个 5 分钟档）加
  const d = new Date(1000 * (whenPast(v) || !v ? whenNow() : whenT(v)));
  d.setHours(d.getHours() + h);
  return dtLocal(d.getTime() / 1000);
}
const whenBlur = () => { const a = document.activeElement; if (a?.closest?.(".when")) a.blur(); };   // 弹窗经回车或异步操作关掉后时刻控件失焦（点击的由 common.js 统一失焦）：焦点留在里面时 render(false) 被「正在操作」（editing）挡住
function whenTap(k) {                                               // 点了小签（data-wq / data-add）：「现在」回到下一个 5 分钟档，+ 在当前的值上往后加（可连点），清空的是不约定 / 无期限（时刻控件已由 common.js 的点击处理失焦，好重画）
  POP.until = k.add ? whenAdd(POP.until, +k.add) : k.wq === "now" ? dtLocal(whenNow()) : "";
  POP.wnow = k.wq === "now"; POP.err = null;
}
function whenSay(v, none, o = {}) {
  const t = whenT(v), now = Date.now() / 1000;
  return whenHalf(v) ? `<span class="bad">没填完：日期和时分都要选，或点「${esc(o.empty || "")}」</span>` : !v ? `<span class="mut">${esc(none)}</span>`
    : o.fresh ? `<span class="${o.stop ? "bad" : "mut"}">请选一个将来的时刻：点上面的 +1 小时、+1 天往后加（可连点），或改日期和时分</span>`
    : t <= now ? `<span class="bad">${tsNear(t)} 已经过去了，换一个更晚的时刻</span>`
    : `到 <b>${tsNear(t)}</b>，还有 ${dur(t - now)}` + (o.cap && t > o.cap ? `<span class="mut">；晚于 ${tsNear(o.cap)}（租期截止），按它截止</span>` : "");
}
const whenBad = (v, o) => whenHalf(v) || (o.fresh ? !!o.stop : whenPast(v));    // 控件标红：没填完、已经过去；停在「现在」的只在点确认被拦下时标红
function whenPick(id, label, v, empty, none, o = {}) {
  const [, d = "", h = "", m = ""] = /^([\d-]*)T(\d*):(\d*)$/.exec(v || "") || [];
  const mins = Array.from({length: 12}, (_, i) => pad(i * 5)).concat(m && +m % 5 ? [m] : []).sort();
  const sel = (sfx, vals, cur, name) => `<select id="${id}-${sfx}" class="when-in when-t" aria-label="${name}">${cur ? "" : `<option value="" selected>--</option>`}${
    vals.map(x => `<option${x === cur ? " selected" : ""}>${x}</option>`).join("")}</select>`;
  const chip = (text, attr, on, cls = "") => `<button type="button" class="wq${cls}${on ? " on" : ""}" data-when="${id}" ${attr}>${esc(text)}</button>`;
  return `<div class="when${whenBad(v, o) ? " bad" : ""}"><label class="sm" for="${id}">${label}</label>
    <div class="when-row"><input type="date" id="${id}" class="when-in when-d" min="${dtLocal(Date.now() / 1000).slice(0, 10)}" value="${esc(d)}">
      ${sel("h", Array.from({length: 24}, (_, i) => pad(i)), h, "时")}<span class="mut">:</span>${sel("m", mins, m, "分")}</div>
    <div class="wqs">${chip("现在", `data-wq="now" ${tip("回到现在（下一个 5 分钟），再点 + 往后加")}`, o.fresh)}${WHEN_ADD.map(([text, n]) => chip(text, `data-add="${n}"`, false)).join("")}${chip(empty, `data-wq="clr"`, !v, " clr")}</div>
    <div class="when-say" data-none="${esc(none)}" data-empty="${esc(empty)}" data-cap="${o.cap || ""}">${whenSay(v, none, o)}</div></div>`;
}
function whenRead(box) {                                             // 从控件拼出值；日期只填了一半（badInput）的那段当作没填
  const [d, h, m] = box.querySelectorAll(".when-in");
  return !d.value && !h.value && !m.value && !d.validity?.badInput ? "" : `${d.validity?.badInput ? "" : d.value}T${h.value}:${m.value}`;
}
function whenInput(el) {                                             // 改了日期、时或分：空着的另外几段从现在（下一个 5 分钟档）补上，值记进 POP，说明行就地跟上，不重画
  const box = el.closest(".when"), parts = [...box.querySelectorAll(".when-in")];
  if (el.value && parts.some(p => !p.value)) {
    const [, d, h, m] = /^(.*)T(\d\d):(\d\d)$/.exec(dtLocal(whenNow()));
    parts.forEach((p, i) => { if (!p.value && !p.validity?.badInput) p.value = [d, h, m][i]; });
  }
  if (POP) { POP.until = whenRead(box); POP.wnow = false; POP.err = null; }
  whenSync(box);
  if (POP?.k === "bor") borrowSync();
}
function whenSync(box, o = {}) {                                     // 说明行、红框与选中的小签就地跟上（o.fresh、o.stop 同 whenSay）
  const v = whenRead(box), say = box.querySelector(".when-say");
  say.innerHTML = whenSay(v, say.dataset.none, {...o, empty: say.dataset.empty, cap: +say.dataset.cap || null});
  box.classList.toggle("bad", whenBad(v, o));
  for (const c of box.querySelectorAll(".wq")) c.classList.toggle("on", c.dataset.wq === "now" ? !!o.fresh : c.dataset.wq === "clr" && !v);
}
function whenStop(id, v, fresh) {                                    // 提交前：没填完、已经过去、还停在「现在」的拦下（返回 true），就地标红并聚焦；说明行就是提示，不另写红字
  const el = document.getElementById(id);
  if (!el?.validity?.badInput && !whenHalf(v) && !fresh && !whenPast(v)) return false;
  const box = el?.closest?.(".when");
  if (box) { whenSync(box, {fresh, stop: true}); el.focus?.(); }
  return true;
}
// 平台操作（续租一台、关机）的弹窗：先说明与确认；没登录时点确认后弹窗里换成输验证码（FCAP.pop），输完开始，进度在操作面板
function opPop(k, x, btn, ask, go) {
  const open = popIs(k, x.id), cap = open && FCAP?.pop;
  return popConfirm(btn, open, !open ? "" : cap ? capBox() : ask(), cap ? null : open ? go() : "", cap);
}
function fitPops() {
  const vv = window.visualViewport, root = document.documentElement.style;
  if (vv) { root.setProperty("--kb", `${Math.max(0, Math.round(innerHeight - vv.height - vv.offsetTop))}px`); root.setProperty("--vvh", `${Math.round(vv.height)}px`); }
  for (const p of document.querySelectorAll(".pop")) {
    p.style.left = p.style.right = p.style.top = p.style.bottom = "";                               // 每次从没挪过的位置量：宽屏挪过又转到窄屏的，不留着盖过贴底的样式
    if (getComputedStyle(p).position === "fixed") continue;          // 窄屏贴底的不挪
    // 所在区域（抽屉、主内容区）与视口取交，左右各留 8px：超出右边往左挪，超出左边往右挪（两边都超出时贴左）。
    // 统一改 left（right: auto）：向左开（right: 0）与向右开（.pop-l，left: 0）的都挪得动（#143）
    const a = (p.closest(".db") || document.getElementById("main")).getBoundingClientRect(), r = p.getBoundingClientRect();
    const lo = Math.max(a.left, 0) + 8, hi = Math.min(a.right, document.documentElement.clientWidth || innerWidth) - 8;
    const dx = Math.max(lo - r.left, Math.min(0, hi - r.right));
    const anchor = p.parentElement?.getBoundingClientRect(), bottom = window.visualViewport?.height || innerHeight;
    if (anchor && r.bottom > bottom - 8 && anchor.top > bottom - anchor.bottom) {
      p.style.top = "auto"; p.style.bottom = "calc(100% + 6px)";
    }
    if (dx) { p.style.left = `${p.offsetLeft + dx}px`; p.style.right = "auto"; }
  }
}
// 这一台上 worker 在跑的任务（去掉占位）：借用节、自用主机的 worker 节、借用按钮的确认共用
const hostRun = x => (D.hosts.find(e => e.id === x.id)?.status?.running || []).filter(j => !j.hold);
// 借用的操作按钮（组内服务器抽屉与主机页共用）：改约定（弹出表单）、跑完就还 ↔ 撤销还（原地换，还掉之前能撤）、现在停（不能撤）。
// 跑完就还、现在停先在按钮旁确认；过了约定时间的撤不了（下一轮又会要还），要先改约定。正在停下的不出按钮
function borrowBtns(x) {
  if (!canOps()) return opsNote();
  const b = hostBorrow(x), dis = fops().op?.active ? "disabled" : "", who = esc(x.holder);
  const run = hostRun(x), names = run.map(j => projName(j.project) + esc(j.name)).join("、"), past = b.cutoff && D.generated >= b.cutoff;
  const hh = D.hosts.find(e => e.id === x.id), aff = affects(hh), left = lwLeft(hh, x.holder);
  const btn = (act, cls, ic, text, hint) => `<button class="btn ${cls}" data-fop="${act}" data-id="${esc(x.id)}" ${dis} ${tip(hint)}>${icon(ic)}${text}</button>`;
  const go = (now, text) => `<button class="btn pri${now ? " dz" : ""}" data-fop="bretgo" data-id="${esc(x.id)}" ${dis}>${text}</button>`;
  if (b.now) return "";
  const ret = !b.returning ? popConfirm(btn("bret", "", "flag", "跑完就还", "不再放新任务，正在跑的跑完后自动还回去、开保活；还掉之前可以撤销"), popIs("bret", x.id),
      `不再放新任务，${run.length ? `${names} 跑完后` : "现在没有任务在跑，下一轮采集就"}还给 ${who}、开保活。还掉之前可以撤销。` + aff + left, go(false, "跑完就还"))
    : btn("bkeep", "undo", "undo", "撤销还", past ? "已过约定时间：先改约定到更晚，再撤销" : "照常放新任务，不还了");
  const now = popConfirm(btn("bnow", "danger", "stop", "现在停", "正在跑的按各自的停法停下，停完就还；不能撤销"), popIs("bnow", x.id),
    `${run.length ? `${names} 按各自的停法停下，这格白跑` : "现在没有任务在跑"}，停完就还给 ${who}。不能撤销。` + aff + left, go(true, "停下并还"));
  return borrowPop(x, btn("bedit", "", "pencil", "改约定", "改约定还的时间、给哪些项目用")) + ret + now;
}
const BACTS = new Set(["bpick", "bedit", "bgo", "bret", "bnow", "bretgo", "bkeep"]);
async function borrowAct(act, id) {
  const x = srvById(id);
  if (act === "bpick" || act === "bedit") {
    const b = borrowOf(x);
    return popToggle("bor", id, {sel: new Set(b?.for || []), ...b && !b.until ? {until: ""} : whenInit(b?.until), ok: false, light: true, away: !!b?.away});   // 改没约定的借用：缺省照旧不约定
  }
  if (act === "bret" || act === "bnow") return popToggle(act, id);
  const B = POP;
  try {
    if (act === "bgo") {
      if (whenStop("fbuntil", B.until, B.wnow)) return;
      B.busy = true; B.err = null; render(false);
      const body = {id, until: whenT(B.until), for: [...B.sel], ok: B.ok, light: B.light, away: !!(B.away && whenT(B.until))};
      if (!borrowOf(x)) { OPS = await fleetPost("borrow", body); POP = null; pollOps(); return; }    // 借来用在后台跑：进度和别的组内操作一样显示在抽屉顶上
      BMSG = {id, msg: (await fleetPost("borrow", body)).msg};
    } else BMSG = {id, msg: (await fleetPost("borrow_return", act === "bkeep" ? {id, cancel: true} : {id, now: B?.k === "bnow"})).msg};
    POP = null;
    await collectNow(true);
  } catch (e) { if (act === "bgo" && POP) { POP.busy = false; POP.err = e.message || String(e); } else { POP = null; FERR = e.message || String(e); } }
}
function srvDrawer(id) {
  const x = srvById(id), op = fops().op;
  if (!x) {                                            // 换人落到别的端口、或已从清单删掉：进度还在就接着给看
    const pr = op?.steps && (op.ids || []).includes(id) ? `<div class="card" id="fpanel">${opProgress(op)}</div>` : "";
    return dHead(`<span class="fav">?</span>`, esc(id), "组内服务器里已经没有这一台") + `<div class="db">${pr || `<div class="card empty">可能换人时换到了别的端口，或已从主机清单删掉。</div>`}</div>`;
  }
  const now = D.generated, a = x.sample || {}, F = D.fleet, selfView = LIVE && ME && !ME.admin, P = selfView ? {} : fPanel(x), nh = holdCount(x.holder), cmd = `ssh -p ${x.port} ${x.ssh}`, ph = DH.person(x.holder);
  const head = dHead(`${pfav(x.holder, `<span class="cdot" style="background:${fDot(x)}"></span>`)}`, esc(x.holder),
    `${esc(fNode(x.ssh))}:${esc(x.port)} · ${fKind(x)}`, `<span class="fc-st">${fBusy(x) || fBadge(x)}</span>${dNav(srvOrder(), id)}`, gdIcon(x));
  let s = fleetAlertBox(fleetAlerts().filter(m => m.startsWith(fName(x))), false) + (P.h ? `<div class="card" id="fpanel">${P.h}</div>` : "") + fErr();
  const holderVal = selfView ? esc(x.holder) : `<a href="${esc(ph)}" data-drw="${esc(ph)}">${esc(x.holder)}</a>`;
  const kv = [["使用人", `${holderVal}${nh > 1 ? ` <span class="mut">共占 ${nh} 台</span>` : ""}${x.job && x.job !== x.holder ? ` <span class="mut">· 作业 ${esc(x.job)}</span>` : ""}`],
    ["连接", `<code>${esc(cmd)}</code> <button class="lnk" data-copy="${esc(cmd)}">复制</button>`], ["资源池", esc(x.pool || "—")]];
  if (LIVE && !x.off && (!SER[id] || (!SER[id].busy && Date.now() - SER[id].at > 120000))) setTimeout(() => loadSer(id));
  const e = selfView ? {} : fops().boot?.[id] || {}, wk = x.guard?.status === "worker";
  if (x.off) kv.push(["关机", `${ts(x.off.t)}（当时的使用人 ${esc(x.off.holder)}）`]);
  else kv.push(["租期", x.lease_end ? `${leaseHtml(x.lease_end)} <span class="mut">${ts(x.lease_end)} 到期</span>` : "—"],
    ["容器", x.started ? `已运行 ${dur(now - x.started)} <span class="mut">${ts(x.started)} 启动${e.created ? ` · 作业 ${esc(fcr(e.created))} 创建` : ""}</span>` : "—"],
    ["上次探测", x.last ? `<span ${tip(x.mine ? "worker 最近一次写状态的时刻（自用主机不另外探，每轮采集读 worker 状态）" : "最近一次只读探测")}>${ts(x.last)}</span>${x.error ? ` <span style="color:var(--bad)">${esc(x.error)}</span>` : ""}` : "还没探过"],
    ["保活守护", `${esc(guardText(x))}${wk || selfView ? "" : ` <a href="${DH.guard()}" data-drw="${DH.guard()}">设置</a>`}`]);
  if (x.mine) kv.push(["调度接入", "已在主机清单 [[hosts]] 单独配置"]);
  else if (x.manage) kv.push(["调度接入", `<span class="row wrap" style="gap:6px;align-items:center"><span>${esc(x.manage.why)}</span>${managePop(x, "drw")}</span>`]);
  const manNote = MAN.id === id && MAN.where === "drw" ? (MAN.msg ? `<div class="rn-state">${esc(MAN.msg)}</div>` : MAN.err && !popIs("fman-drw", id) ? `<div class="rn-err">${esc(MAN.err)}</div>` : "") : "";
  const btns = LIVE && !selfView ? `<div class="rn">${srvBtns(x)}</div>${PERR[id] ? `<div class="rn-err">${esc(PERR[id])}</div>` : ""}${RERR[id] ? `<div class="rn-err">${esc(RERR[id])}</div>` : ""}` : "";
  s += dSec("概况", kvHtml(kv) + manNote + btns) + borrowSec(x) + workerSec(x);
  if (!x.off) {
    const rows = (SER[id]?.rows || []).filter(r => r.t >= now - FRANGE * 3600), grp = "f:" + id, t0 = now - FRANGE * 3600, gap = (F.interval || 600) * 2.5;
    SPKG[grp] = {t0, t1: now, at: t => { const r = nearest(rows, t); return r ? `${ts(r.t)} ${FSN[r.st] || ""}${r.sh ? "，可共享" : ""}` + (r.vt != null ? `\n显存 实际 ${mb2g(r.vr)} / ${mb2g(r.vt)} GiB（含保活 ${mb2g(r.v)}）` : "")
      + (r.u != null ? `\n利用率 ${Math.round(r.u)}%（实际 ${Math.round(r.ur)}%）` : "") + (r.cr != null ? `\nCPU 实际 ${g1(r.cr)} / ${g1(r.cq)} 核` : "") + (r.mr != null ? `\n内存 实际 ${g1(r.mr)} / ${g1(r.ml)} GiB` : "") : ""; }};
    const sp = (f, opt = {}) => rows.length > 1 ? spark(grp, t0, now, rows.map(r => [r.t, f(r)]), {gap, ...opt}) : `<span class="sm mut">${SER[id]?.err ? "曲线取不到" : "数据还不够"}</span>`;
    const row = (l, bar, v, spk, lt = "") => `<span class="l" ${lt ? tip(lt) : ""}>${l}</span>${bar}<span class="v">${v}</span><div class="spk">${spk}</div>`;
    let r = "";
    if (a.vram_total) {
      const U = fUtil(x), kaB = a.state === "keepalive" ? a.util : 0;
      r += row("利用率", fbar([[U.v, "var(--accent)", "实际"], [kaB, KA, "保活", "hatch"]], 100, "big"),
        `${U.big}${U.sub ? `<span class="mut">${esc(U.sub)}</span>` : ""}`, sp(r => r.ur, {ref: F.share.util}), U.tip + "\n小曲线是每次探测的实际利用率");
      r += row("显存", fbar([[a.vram_real, "var(--accent)", `实际 ${mb2g(a.vram_real)} GiB`], [a.vram_ka, KA, `保活 ${mb2g(a.vram_ka)} GiB`, "hatch"], [a.vram - a.vram_real - a.vram_ka, "var(--muted)", "其他（容器里找不到的进程）", "hatch"]], a.vram_total, "big"),
        `${mb2g(a.vram_real)} / ${mb2g(a.vram_total)} GiB<span class="mut">含保活 ${mb2g(a.vram)}</span>`, sp(r => r.vt ? 100 * r.vr / r.vt : null, {ref: 100 * F.share.vram}),
        (a.gpus || []).map(g => `GPU ${g.i}：${g.name}，${mb2g(g.used)} / ${mb2g(g.total)} GiB，利用率 ${fCardU(g, a.state).big}`).join("\n") + `\n曲线是实际显存；红虚线是可共享阈值`);
    }
    if (a.cpu_quota && a.cpu != null) r += row("CPU", fbar([[a.cpu_real, "var(--accent)", `实际 ${g1(a.cpu_real)} 核`], [a.cpu - a.cpu_real, KA, "保活", "hatch"]], a.cpu_quota, "big"),
      `${g1(a.cpu_real)} / ${g1(a.cpu_quota)} 核`, sp(r => r.cq ? 100 * r.cr / r.cq : null, {ref: 100 * F.share.cpu}), `容器配额 ${g1(a.cpu_quota)} 核（nproc 显示的 ${a.nproc} 是宿主机的）`);
    if (a.mem) r += row("内存", fbar([[a.mem_real, "var(--accent)", `实际 ${g1(a.mem_real)} GiB`], [a.mem.used - a.mem_real, KA, "保活", "hatch"]], a.mem.limit, "big"),
      `${g1(a.mem_real)} / ${g1(a.mem.limit)} GiB`, sp(r => r.ml ? 100 * r.mr / r.ml : null, {ref: 100 * F.share.mem}),
      "工作集 = 用量 − 不活跃页缓存" + (a.mem_ka == null ? "；含保活（worker 状态里没有保活进程树单独的量），CPU 也一样" : "，再减去保活进程树的 PSS"));
    const L = x.lw, hm = x.mine && D.hosts.find(e => e.id === x.id);
    if (L) r += row("本地盘", lwBar(L, hm?.status?.storage?.local), lwVal(L), heatTag(lwHeat(L)) || "续租会清空", LW_TIP);
    s += dSec("现在", r ? `<div class="rs">${r}</div>` : `<div class="empty">${x.error ? esc(x.error) : "还没探到。"}</div>`, `<span class="sm mut">深色是实际用量（去掉保活），斜纹是保活；本地盘的斜纹是 labmon 之外写的</span><span class="sp"></span>
      <span class="seg">${[[24, "24 小时"], [168, "7 天"]].map(([v, l]) => `<button data-frange="${v}" class="${v === FRANGE ? "on" : ""}">${l}</button>`).join("")}</span>`);
    const ws = a.work || [];
    s += dSec("在跑什么", (ws.length ? `<div class="dws">${ws.map(w => `<div class="dw"><div class="row"><span class="badge ${FSB[w.kind]}">${FSN[w.kind]}</span><b>${esc(w.label)}</b>${w.rule ? `<span class="sm mut" ${tip("命中规则 " + w.rule)}>规则</span>` : ""}
        ${w.owner ? `<span class="sm mut" ${tip("按工作目录与命令行里的共享盘路径归人（与「共享盘」同一张归属表）；对不上就算这台的使用人")}>${esc(w.owner)} 的</span>` : ""}
        ${canOps() && w.kind === "job" && w.pid ? `<span class="sp"></span><button class="btn mini-btn" data-fop="kamark" data-id="${esc(x.id)}" data-name="${w.pid}" ${tip("其实是保活：把它跑的脚本加进主机清单的 [fleet] keepalive（以后命令行含它的 GPU 进程都算保活），再探一次")}>这是保活</button>` : ""}</div>
        ${(w.names || []).filter(n => n !== w.label).length ? nameSum(w.names.filter(n => n !== w.label)) : ""}
        <div class="sm mut">显存 ${mb2g(w.mb)} GiB · ${w.n} 个 GPU 进程${w.etime != null ? ` · 已运行 ${dur(w.etime)}` : ""}</div>
        ${w.cwd ? `<div class="sm"><code>${esc(w.cwd)}</code></div>` : ""}${(w.chain || []).length ? `<details><summary class="sm">父进程链</summary><pre>${esc(w.chain.join("\n"))}</pre></details>` : ""}</div>`).join("")}</div>`
      : `<div class="empty">${a.work ? "GPU 上没有进程。" : x.error ? esc(x.error) : "还没探到。"}</div>`)
      + (a.vscode || a.terms ? `<div class="sm mut" style="margin-top:6px">${[a.vscode ? "有 VS Code 远程连着" : "", a.terms ? `${a.terms} 个 ssh 终端` : ""].filter(Boolean).join("，")}（说明有人在用，但不算 GPU 负载）</div>` : ""));
    if (L?.ok) s += dSec("本地盘", lwBlock(L, {started: x.started, loc: hm?.status?.storage?.local, why: x.mine ? "worker 还没扫到：限速轮流扫，开机后头一轮要一会儿"
      : borrowOf(x) ? "借用中不扫：扫出来的混着我们的" : L.used < 0.0625 ? "开机以来几乎没写" : "下一次探测时扫"}),
      `<span class="sm mut">续租、关机、换人、换节点前要挪的</span>`);
  }
  s += weekSec(x);
  if (!selfView) {
    s += dSec("开机参数", Object.keys(e).length ? `<details class="dfold"><summary class="sm">${esc((e.image || "").split("/").pop() || "—")} · ${esc(e.src || "")} ${ts(e.t)}</summary>${kvHtml([["镜像", `<code>${esc(e.image || "—")}</code>`],
      ...(e.spec ? [["规格", esc(specName(x.pool, e.spec))]] : []),
      ["端口", esc(Object.entries(e.ports || {}).map(([p, c]) => `${p}→${c}`).join(" ") || "—")], ["配置", esc(`${e.cpu ?? "—"} 核 · ${e.memory ?? "—"} GB 内存 · ${e.disk ?? "—"} GB 硬盘 · ${e.gpu ?? "—"} 卡`)],
      ["IP", esc(e.ip || "—")], ["作业", `${esc(e.job || "—")}${e.job_id ? ` <span class="mut">id ${esc(e.job_id)}</span>` : ""}`], ["启动命令", `<code>${esc(e.run_cmd || "—（与平台核对时补存）")}</code>`],
      ...((e.volumes || []).length ? [["挂载", e.volumes.map(esc).join("<br>")]] : [])])}</details>` : `<div class="empty">在「服务器」选项卡点「与平台核对」，或 labmon fleet import ${esc(id)} &lt;Json 文件&gt;。</div>`);
    s += dSec("事件", recSlice({host: id}, 15));
  } else if (D.hosts.some(h => h.id === id)) {
    s += dSec("事件", recSlice({host: id}, 15));
  }
  return head + `<div class="db">${s}</div>`;
}
// 服务器抽屉的「近 7 天」：逐小时条与 7 / 30 天统计（组内服务器与自用主机共用）
function weekSec(x) {
  const ax = [];
  for (let k = 0; k < 168; k++) { const t = x.h0 + k * 3600; if (new Date(t * 1000).getHours() === 0) ax.push(`<span style="left:${100 * k / 168}%">${tsMD(t)}</span>`); }
  return dSec("近 7 天", `${hourStrip(x, true)}<div class="hax">${ax.join("")}</div>${fLegend()}${kvHtml([["近 7 天", fSum(x.s7)], ["近 30 天", fSum(x.s30)]])}`, `<span class="sm mut">每小块一小时，悬停看那一小时</span>`);
}
// 自用主机与已自动接入调度的组员服务器的「worker」节（借来的组内服务器的 worker 在「借用」节）：worker 在不在、在跑与排队、预备续租、主机页上的告警，链到主机页
function workerSec(x) {
  const h = (x.mine || x.manage?.on) && D.hosts.find(e => e.id === x.id);
  if (!h) return "";
  const run = hostRun(x), wait = h.status?.waiting || [], al = forUser(alertsOf(h)), rc = renewChip(h), hp = `#/host/${encodeURIComponent(x.id)}`;
  const kv = [["状态", x.error || !h.worker_up ? `<span style="color:var(--warn)">${esc(x.error || "worker 不在")}</span>` : `在写状态 <span class="mut">${dur(h.age || 0)} 前</span>`],
    ["在跑", run.length ? run.map(j => projName(j.project) + esc(j.name)).join("、") : `<span class="mut">没有任务</span>`],
    ["排队", wait.length ? `${wait.length} 个` : `<span class="mut">没有</span>`], ...rc ? [["续租", rc]] : [],
    ...al.length ? [["告警", `<a href="${hp}" style="color:var(--bad)" ${tip(al.join("\n"))}>${al.length} 条</a>`]] : []];
  return dSec("worker", kvHtml(kv) + `<div class="rn"><a class="btn" href="${hp}">主机页</a></div>`, `<span class="sm mut">任务、队列、资源曲线的细节在主机页</span>`);
}
// 在一个池开新的：向导与进度放在这个抽屉里
function newDrawer(pool) {
  const o = fops(), op = o.op, P = fPanel(), info = o.pools?.[pool] || {};
  const h = P.h && !P.sid && P.pool === pool ? P.h : op?.steps && op.kind === "open" && op.pool === pool && !(op.ids || []).length ? opProgress(op) : "";
  return dHead(`<span class="fav">+</span>`, "开新的", esc(pool)) + `<div class="db">${h ? `<div class="card" id="fpanel">${h}</div>${fErr()}`
    : `<div class="card"><div class="rn-note">在这个池开一台：选人，端口自动挑；提交前连平台再核一次。</div>
      <div class="rn">${canOps() ? `<button class="btn pri" data-fop="wiz" data-kind="open" data-pool="${esc(pool)}" ${info.ref && !op?.active ? "" : "disabled"}>选人</button>` : opsNote()}</div>${fErr()}</div>`}</div>`;
}
