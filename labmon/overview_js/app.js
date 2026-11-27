/* ---------- 渲染 ---------- */
// 本机不在内网：各页顶上一条说明（数据停在离开前，不探测、不报掉线，平台操作做不了）
const lanBar = () => LANOFF() ? `<div class="lanbar"><b>本机不在内网</b><span>${D.lan.since ? ts(D.lan.since) + " 起，" : ""}平台与各服务器都连不上，下面是离开前的数据。`
  + `这期间不探测、不报掉线，统计里算没有数据；续租、换人等平台操作要回到内网再做。回到内网后下一轮采集自动恢复。</span></div>` : "";
// 这台看板服务是备用（服务租约，#163）：各页顶上一条，说主是谁、何时能接管；数据停在当主时的最后一轮，写操作回 409
const standbyBar = () => D.server?.role === "standby" ? `<div class="lanbar"><b>备用</b><span>${esc(D.server.say || "")}</span></div>` : "";
const NONET = new Set(["sync", "renewone", "renewgo", "renew1", "offpick", "offgo", "wiz", "wizgo", "ka", "newcode", "go", "bgo", "bretgo", "bkeep"]);
const noNet = (e, el) => flashTip(e, "本机不在内网：回到内网再操作", el);
// 项目页用 iframe 嵌入：每个项目一个，切走时只隐藏，回来不重新加载、状态不丢。FRAME 是正显示的那个项目
let FRAME = null;
const frameEl = id => [...document.querySelectorAll("#frames iframe")].find(f => f.dataset.p === id);
const frameWin = () => { try { return FRAME ? frameEl(FRAME)?.contentWindow || null : null; } catch (e) { return null; } };
const frameState = () => { try { return frameWin()?.embedState?.() || null; } catch (e) { return null; } };
// 项目页数据变了（它每 15 秒取一次）：顶栏上的更新时间、连不上的提示跟着它
function frameChanged() { if (FRAME) document.getElementById("top").innerHTML = topbar(route()); }
let LAST = "", PENDING = false;
const selecting = () => editing("main", "drw");          // 正在操作：输入框里，或正文、抽屉里选着文字（common.js）
function render(how) {
  // how：true 是换了页（滚回顶上），"keep" 是只开关了抽屉，false 是定时刷新。
  // 定时刷新时：用户正选着文字就先不重画，选区一取消再补；内容没变就不动页面
  if (!how && selecting()) {
    PENDING = true;
    if (popIs("guard-edit", "")) gdSyncSave();
    return;
  }
  PENDING = false;
  srvMoved();
  const r = route(), view = document.getElementById("view"), frames = document.getElementById("frames");
  if (r.oldAccount !== undefined && (!LIVE || ME)) {
    const L = r.oldAccount && r.oldAccount !== "+" ? directoryUsers() : null;
    if (L || !r.oldAccount || r.oldAccount === "+" || !ME?.scopes.includes("admin")) {
      const u = L?.users.find(u => u.name === r.oldAccount);
      const person = directoryPeople(L).has(u?.fleet_person) ? u.fleet_person : null;
      history.replaceState(history.state, "", person ? DH.person(person) + (r.oldUsage ? "" : "/account") : "#/fleet/people");
      return render(how);
    }
  }
  RKEY = rkey(r); BGTAB = r.kind === "fleet" ? r.tab : null;
  accLeave(r);
  document.getElementById("side").innerHTML = side(r);
  drawNav(r);
  document.getElementById("top").innerHTML = topbar(r);
  tipCheck();                                           // 侧边栏、顶栏每次都重画：悬停着的元素没了就收起提示
  document.title = (r.kind === "home" ? "调度总览" : document.querySelector(".bar-top h1")?.textContent) + " · labmon";
  setIcon(allAlerts().length + fleetAlerts().length + D.projects.reduce((k, p) => k + (p.alerts || 0), 0));
  if (r.kind === "p" && !visibleProject(r.id) && !(D.archived || []).some(p => p.id === r.id)) {
    view.hidden = false; frames.hidden = true; FRAME = null;
    view.innerHTML = LAST = '<div class="card empty">看板上没有这个项目，或你的账号无权查看。</div>';
    return drawer(r);
  }
  if (r.kind === "p") {
    let f = frameEl(r.id);
    if (!f) {
      f = document.createElement("iframe");
      f.src = `p/${encodeURIComponent(r.id)}/`; f.title = r.id; f.dataset.p = r.id;
      frames.append(f);
    }
    const back = f.hidden || frames.hidden;
    for (const x of frames.children) x.hidden = x !== f;
    view.hidden = true; frames.hidden = false; FRAME = r.id; LAST = "";
    if (back) try { f.contentWindow?.drawTimeline?.(); } catch (e) {}       // 隐藏期间它的时间线没画
    drawer(r);
    return fitPops();                                   // 项目页的「管理」抽屉里也有弹窗（#143：以前这条路不量，靠右的伸出屏幕）
  }
  FRAME = null; view.hidden = false; frames.hidden = true; TLH = null;
  const html = standbyBar() + lanBar() + (r.kind === "host" ? hostPage(r.id) : r.kind === "fleet" ? fleetPage() : r.kind === "log" ? logPage(r.f)
    : r.kind === "me" ? `<div class="account-content">${mePage(r.tab)}</div>` : home());
  if (how || html !== LAST) {
    const m = document.getElementById("main"), y = m.scrollTop;
    view.innerHTML = LAST = html;
    m.scrollTop = how === true ? 0 : y;
  }
  drawer(r);
  if (FFOCUS) { const el = document.getElementById(FFOCUS); el?.focus({preventScroll: true}); if (el?.type === "search") el.selectionStart = el.selectionEnd = el.value.length; FFOCUS = null; }
  document.documentElement.style.setProperty("--toph", document.getElementById("top").offsetHeight + "px");     // 记录页的日期标题吸在顶栏下面
  if (FSCROLL) { const el = document.getElementById(FSCROLL); FSCROLL = null; el?.scrollIntoView({block: "nearest"}); }      // 不用平滑滚动：预览回来再重画会把它打断
  tipCheck();
  fitPops();
  drawTimeline();
}
// 窄屏的底栏与「更多」面板：开关面板只重画这两处
function drawNav(r = route()) {
  const m = document.getElementById("more"), open = MORE && !m.hidden;
  document.getElementById("tabbar").innerHTML = tabbar(r);
  m.hidden = !MORE;
  document.body.classList.toggle("more-open", MORE);
  if (MORE) { document.getElementById("morep").innerHTML = morePanel(r); if (!open) document.getElementById("morep").focus({preventScroll: true}); }
}
// 抽屉只有一个，开关时加减 class 做滑入滑出；内容按 key 重画，同一个抽屉重画时保留它的滚动位置
// 保活守护（#/fleet/guard）：规则与两个时间、每台的开关、最近的记录。时间写进 [fleet]，开关写进那一台的块（fleet.set_guard）
let GDERR = null, GDPEND = {};           // GDPEND：拨了还没存好的开关（id → 开或关），先照它画并置灰
const gdVal = k => popIs("guard-edit", "") ? POP.draft[k] : fops().guard?.[k];
// 与 fleet.set_guard 的范围一致；探测间隔不是整分钟时，服务端向下取整。
const gdLimits = () => ({idle_min: [Math.floor(D.fleet.interval / 60), 1440], watch_min: [0, 60]});
const GD_IN = {idle_min: ["gdidle", "空闲多久开"], watch_min: ["gdwatch", "开后盯多久"]};
// 草稿的校验（保存按钮、回车与范围提示共用）：bad 是不合法的项，body 是合法且有改动时要提交的
function gdCheck() {
  const body = {}, bad = [], current = fops().guard;
  let changed = false;
  for (const [k, [lo, hi]] of Object.entries(gdLimits())) {
    const raw = gdVal(k), value = Number(raw);
    if (raw == null || String(raw).trim() === "" || !Number.isInteger(value) || value < lo || value > hi) { bad.push(k); continue; }
    body[k] = value;
    changed ||= value !== Number(current?.[k]);
  }
  return {bad, body: !bad.length && changed ? body : null};
}
const gdHint = bad => bad.map(k => `${GD_IN[k][1]}填 ${gdLimits()[k][0]} 到 ${gdLimits()[k][1]} 的整数分钟`).join("；");
const gdPayload = () => popIs("guard-edit", "") && !POP.busy && canOps() ? gdCheck().body : null;
function gdSyncSave() {     // 打字时就地更新保存按钮、范围提示与输入框的 aria-invalid，不整页重画（免得打断输入）
  const {bad} = popIs("guard-edit", "") ? gdCheck() : {bad: []}, hint = document.querySelector("[data-gdhint]");
  document.querySelector("[data-gdsave]")?.toggleAttribute("disabled", !gdPayload());
  if (hint) { hint.textContent = gdHint(bad); hint.hidden = !bad.length; }
  for (const [k, [id]] of Object.entries(GD_IN)) document.getElementById(id)?.setAttribute("aria-invalid", String(bad.includes(k)));
}
function guardDrawer() {
  const F = D.fleet, dis = canOps() ? "" : "disabled", iv = Math.round(F.interval / 60);
  const open = popIs("guard-edit", ""), limits = gdLimits(), bad = open ? gdCheck().bad : [];
  const num = (id, name, k) => {
    const [lo, hi] = limits[k];
    return `<label>${name}<input id="${id}" class="cap-in gd-in" type="number" inputmode="numeric" min="${lo}" max="${hi}" step="1" value="${esc(gdVal(k) ?? "")}" aria-invalid="${bad.includes(k)}" aria-describedby="gdhint"${POP.busy ? " disabled" : ""}></label>`;
  };
  const edit = canOps() ? popConfirm(`<button class="btn" id="open-guard-edit" data-fop="guard-edit" aria-expanded="${open}" aria-haspopup="dialog">修改守护时间</button>`,
    open, open ? `<div class="aform">${num("gdidle", "空闲多久开（分钟）", "idle_min")}${num("gdwatch", "开后盯多久（分钟）", "watch_min")}</div>
      <div class="rn-err" id="gdhint" data-gdhint role="status"${bad.length ? "" : " hidden"}>${esc(gdHint(bad))}</div>
      ${POP.err ? `<div class="rn-err">${esc(POP.err)}</div>` : ""}` : "",
    `<button class="btn pri" data-gdsave ${!gdPayload() ? "disabled" : ""}>保存</button>`, true) : opsNote();
  const flow = `<ol class="gd-flow">
      <li><b>GPU 连着空 ${esc(fops().guard?.idle_min ?? "—")} 分钟</b><span class="sm mut">没有实验也没有保活才算空。约 ${iv} 分钟探一次，有一次没探到就重新计时</span></li>
      <li><b>开保活</b><span class="sm mut">开之前再看一眼 GPU，有别的进程就不开。正在做平台操作的那台不碰</span></li>
      <li><b>在服务器上盯 ${esc(fops().guard?.watch_min ?? "—")} 分钟</b><span class="sm mut">每 20 秒看一次 GPU，出现实验或这人自己开的保活就让出。盯完之后靠每次探测，见到别的负载照样让出</span></li></ol>
    ${GDERR ? `<div class="rn-err">${esc(GDERR)}</div>` : ""}`;
  const row = x => { const pend = x.id in GDPEND, on = pend ? GDPEND[x.id] : !!x.guard?.on;    // 没开守护的只写 GPU 现在的情况：开关已经说了没开
    return `<div class="gd-row">${pfav(x.holder, `<span class="cdot" style="background:${fDot(x)}"></span>`)}
      <div class="fc-t"><b>${esc(x.holder)}</b><span class="sm mut">${esc(x.id)} · ${esc(x.guard?.status === "off" ? gdSit(x) : guardText(x))}</span></div>${gdIcon(x)}
      <label class="sw" ${tip(on ? "关掉：不再自动开（已开的照样让路）" : "打开：GPU 连着空闲就自动开保活")}><input type="checkbox" role="switch" data-gd="${esc(x.id)}" ${on ? "checked" : ""} ${pend ? "disabled" : dis} aria-label="${esc(x.holder)}（${esc(x.id)}）的保活守护"><i></i></label></div>`; };
  const list = srvOrder().map(srvById).filter(x => x && !x.mine), mine = F.mine || [];
  const grp = (t, xs, f = row) => xs.length ? `<div class="gd-h">${t}</div><div class="gd-rows">${xs.map(f).join("")}</div>` : "";
  const own = x => `<div class="gd-row">${pfav(x.holder)}<div class="fc-t"><b>${esc(x.holder)}</b><span class="sm mut">${esc(x.id)} · worker 在管保活，不用守护</span></div></div>`;
  const srvs = grp("公用池 · 默认开", list.filter(x => x.public)) + grp("专用池 · 默认关", list.filter(x => !x.public)) + grp("自用", mine, own);
  return dHead(`<span class="fav">${icon("shield")}</span>`, "保活守护", "GPU 连着空闲就自动开保活，免得服务器因为没保活被平台释放")
    + `<div class="db">${dSec("规则", flow, edit)}
      ${dSec("服务器", srvs, `<span class="sm mut">开着 ${list.filter(x => !x.off && x.guard?.on).length} 台 · 用户保活持续空转且归属核实后，切换为看板保活</span>`)}
      ${dSec("最近记录", recSlice({cat: "keepalive"}, 10, "还没有开保活或让出的记录。"))}</div>`;
}
async function gdSet(body) {
  GDERR = null;
  if (body.id) { GDPEND[body.id] = body.on; render(false); }
  try { await fleetPost("guard", body); await collectNow(true); }
  catch (e) { GDERR = e.message || String(e); }
  delete GDPEND[body.id];
  render(false);
  if (body.id && document.activeElement === document.body) document.querySelector(`[data-gd="${CSS.escape(body.id)}"]`)?.focus({preventScroll: true});   // 重画后光标回到这个开关
}
async function gdSave() {
  const pop = POP, body = gdPayload();
  if (!body) {               // 不合法：提示照显示，焦点留在第一个不合法的框；没改动就什么也不做
    const bad = popIs("guard-edit", "") && !pop.busy ? gdCheck().bad : [];
    if (bad.length) { gdSyncSave(); document.getElementById(GD_IN[bad[0]][0])?.focus({preventScroll: true}); }
    return;
  }
  document.activeElement?.blur();
  pop.busy = true; pop.err = null; render(false);
  try {
    OPS = await fleetPost("guard", body);
    await collectNow(true);
    if (POP === pop) { FFOCUS = pop.trigger; popClose(); }
  } catch (e) { if (POP === pop) pop.err = e.message || String(e); }
  pop.busy = false;
  render(false);
}
function renewLeave() { RPICK = null; if (FCAP?.pop === "renew") FCAP = null; }     // 离开续租抽屉：勾选与抽屉里取的验证码一起作废，在途的回执经 startOp 的实例比较丢掉
function canOpenDrawer(r) {
  if (!r.drw) return false;
  if (r.kind === "p" || r.kind === "home" || r.kind === "host") return true;
  if (r.kind !== "fleet") return false;
  if (!LIVE || ME?.admin) return (D.fleet && !D.fleet.error) || (r.drw.k === "person" && ME?.scopes.includes("admin"));
  if (r.drw.k === "srv") return !!(D.fleet && !D.fleet.error && srvById(r.drw.id));
  return false;
}
function drawer(r) {
  const el = document.getElementById("drw"), pane = document.getElementById("drwp"), d = canOpenDrawer(r) ? r.drw : null;
  if (!d) {
    if (!DKEY) return;
    const k = DKEY, back = pane.contains(document.activeElement) || document.activeElement === document.body;
    if (k.startsWith("person:")) { FPED = null; FDK = {}; }
    if (k.startsWith("guard:")) GDERR = null;
    if (k.startsWith("renew:")) renewLeave();
    if (k.startsWith("assign:")) APICK = null;
    pane.querySelector(".secret")?.remove();
    DKEY = null; DLAST = ""; DBACK = null;
    el.classList.remove("on"); el.setAttribute("aria-hidden", "true"); document.documentElement.classList.remove("drw-on");
    setTimeout(() => { if (!DKEY) pane.innerHTML = ""; }, 320);
    const i = k.indexOf(":"), h = DH[k.slice(0, i)](k.slice(i + 1));
    if (back) document.querySelector(`#view [data-drw="${CSS.escape(h)}"], #top [data-drw="${CSS.escape(h)}"]`)?.focus({preventScroll: true});    // 光标回到点开它的卡片（项目的「管理」在顶栏）
    return;
  }
  const key = `${d.k}:${d.id}`;
  if (DKEY && DKEY !== key && DKEY.startsWith("person:")) { FDK = {}; if (FPED && !FPED.isNew) FPED = null; }
  if (DKEY?.startsWith("renew:") && d.k !== "renew") renewLeave();     // 切到别的抽屉也算离开续租
  if (DKEY?.startsWith("assign:") && d.k !== "assign") APICK = null;
  const html = d.k === "srv" ? srvDrawer(d.id) : d.k === "new" ? newDrawer(d.id) : d.k === "guard" ? guardDrawer() : d.k === "renew" ? renewDrawer(d.id) : d.k === "proj" ? projDrawer(d.id)
    : d.k === "assign" ? assignDrawer(d.id) : d.k === "arch" ? archDrawer() : directoryDrawer(d);
  const section = d.section || "";
  const previous = pane.dataset.section;
  if (html !== DLAST || key !== DKEY) {
    const changedSection = key === DKEY && section !== previous;
    const focus = changedSection && pane.contains(document.activeElement);
    const y = key === DKEY && !changedSection ? pane.querySelector(".db")?.scrollTop || 0 : 0;
    pane.dataset.section = section;
    pane.innerHTML = DLAST = html;
    const db = pane.querySelector(".db"); if (db) db.scrollTop = y;
    if (focus) pane.querySelector('.drawer-tabs [aria-current="page"]')?.focus({preventScroll: true});
  }
  if (!DKEY) {
    void pane.offsetWidth;                                       // 先排一次版，滑入动画才有起点
    el.classList.add("on"); el.setAttribute("aria-hidden", "false"); document.documentElement.classList.add("drw-on");
    pane.focus({preventScroll: true}); hideTip();
  }
  DKEY = key;
}
document.addEventListener("selectionchange", () => { if (PENDING && !selecting()) render(false); });
document.addEventListener("focusout", () => setTimeout(() => { if (PENDING && !selecting()) render(false); }));
document.addEventListener("change", e => {               // 批量续租的勾选、弹窗里的「可以中断」与借用表单、向导里的勾选与镜像、共享盘目录归属、保活守护的开关
  const t = e.target, d = t.dataset || {};
  if (t.classList?.contains("when-in")) return whenInput(t);      // 时刻的下拉：有的浏览器只发 change
  if ("dkown" in d) return diskSet({dir: d.dkown, owner: t.value || null});
  if ("cf" in d) return cfgInput(t, "change");                      // 项目配置表单：开关、取色选定、日期框
  if ("gd" in d) return gdSet({id: d.gd, on: t.checked});
  if (d.lg === "host" || d.lg === "who") return logSet({[d.lg]: t.value});
  if ("pack" in d && POP) { POP.ack = t.checked; return render(false); }
  if ("wack" in d && FWIZ) { FWIZ.ack = t.checked; return render(false); }
  if ("bok" in d && POP) { POP.ok = t.checked; return render(false); }
  if ("baway" in d && POP) { POP.away = t.checked; return borrowSync(); }
  if ("blight" in d && POP) { POP.light = t.checked; return render(false); }
  if ("bfor" in d && POP?.sel) { t.checked ? POP.sel.add(d.bfor) : POP.sel.delete(d.bfor); return; }
  if ("owner" in d && POP?.k === "owner") { POP.owner = t.value; return; }
  if ("achost" in d && POP?.k === "acc-hosts") { t.checked ? POP.sel.add(d.achost) : POP.sel.delete(d.achost); return; }
  if ("pushkind" in d && POP?.k === "acc-push") { POP.kind = t.value; ACC.err = null; t.blur(); return render(false); }
  if ("regto" in d && POP) { POP.to = d.regto; POP.err = null; return render(false); }   // 换到哪台
  if ("assignto" in d && APICK && !APICK.busy) { APICK.to = d.assignto; APICK.err = null; return render(false); }   // 给项目用：抽屉内选项目
  if ("adopt" in d && ADOPT) { ADOPT.sel[d.adopt] = t.checked; return render(false); }    // 接上卡的勾选
  const sel = d.rsel, ack = d.rack;
  if (!RPICK || (!sel && !ack) || FCAP?.pop === "renew" || RPICK.busy) return;      // 输验证码、提交中不改（同 renewDrawer 的 lock）
  const set = sel ? RPICK.sel : RPICK.ack, id = sel || ack;
  t.checked ? set.add(id) : set.delete(id);
  render(false);
});
let FDK = {};              // 人员抽屉里还没保存的共享盘提醒线（limit → 输入框里的字）
const FIN = {fwnew: ["W", "newp"], fpname: ["P", "name"], fpimg: ["P", "image"], fpcmd: ["P", "run_cmd"], fpnote: ["P", "note"],
             gdidle: ["G", "idle_min"], gdwatch: ["G", "watch_min"], dklim: ["K", "limit"]};
const finOf = f => ({W: FWIZ, P: ["pdef", "pedit"].includes(POP?.k) ? POP.draft : FPED, G: popIs("guard-edit", "") ? POP.draft : null, K: FDK})[f[0]];     // 输入框的值记在哪个状态里
document.addEventListener("input", e => {               // 输入框的值先记下：重画后照样填回去
  const id = e.target.id, f = FIN[id], o = f && finOf(f);
  if (id === "fcapin" && FCAP) FCAP.input = e.target.value;
  if (id === "lgq") { LGQ = e.target.value; clearTimeout(LGT); LGT = setTimeout(() => logSet({q: LGQ.trim()}), 300); }    // 边打边筛：停手 0.3 秒再取
  if (o) o[f[1]] = e.target.value;
  if (f?.[0] === "G") gdSyncSave();
  if (e.target.classList?.contains("when-in")) whenInput(e.target);
  if (e.target.dataset?.cf && e.target.type !== "checkbox") cfgInput(e.target, "input");
  if (e.target.dataset?.popField && POP?.draft) POP.draft[e.target.dataset.popField] = e.target.value;
});

// 悬停提示的外壳在 common.js（tipBind）；这里只管小曲线的十字线（XH：正画着十字线的组）与嵌着的项目页
const frameTip = () => { try { frameWin()?.hideTip?.(); } catch (e) {} };   // 项目页同源：替它收起留下的提示
let XH = null;
const xhHide = () => { if (XH) { document.querySelectorAll("svg.spark .xh").forEach(l => l.setAttribute("visibility", "hidden")); XH = null; } };
tipBind({
  hover(e) {
    if (FRAME) frameTip();                         // 光标在本页：项目页（iframe）里留下的提示一定过时了
    const sv = e.target.closest?.("svg.spark"), g = sv && SPKG[sv.dataset.grp];
    if (XH !== sv?.dataset.grp) xhHide();
    if (!g) return;
    const r = sv.getBoundingClientRect(), f = Math.max(0, Math.min(1, (e.clientX - r.left) / r.width));     // 小曲线：同组一起画十字线
    for (const l of document.querySelectorAll(`svg.spark[data-grp="${CSS.escape(sv.dataset.grp)}"] .xh`)) {
      l.setAttribute("x1", 200 * f); l.setAttribute("x2", 200 * f); l.setAttribute("visibility", "visible");
    }
    XH = sv.dataset.grp;
    return [g.at(g.t0 + f * (g.t1 - g.t0)), sv];
  },
  hide: xhHide,
});
document.addEventListener("toggle", e => {
  if (e.target.matches?.("details.agal")) ST.set("agal", e.target.open);        // 主机页「给实验会话的提示」的开合（common.js 的 alertBox）
  if (e.target.matches?.("details.rk-more") && POP) POP.more = e.target.open;          // 弹窗里「细节」的开合（riskMore）
  if (e.target.matches?.("details.fold")) {
    const f = ST.get("cardOpen", {}), k = e.target.dataset.fold;
    if (!!f[k] === e.target.open) return;
    f[k] = e.target.open; ST.set("cardOpen", f);
    if (k.startsWith("rec:")) render(false);           // 记录卡只渲染打开的那一项
  }
}, true);
document.addEventListener("keydown", e => {
  if (e.key === "[" && !e.metaKey && !e.ctrlKey && !e.altKey && !e.target.closest?.("input,textarea,select,[contenteditable]")) {
    e.preventDefault(); return document.querySelector("[data-side]")?.click();
  }
  if (e.key === "Enter" && e.target.id === "fcapin" && FCAP) { e.preventDefault(); return fopAct("go"); }
  if (e.key === "Enter" && !e.isComposing && FIN[e.target.id]?.[0] === "G") { e.preventDefault(); return gdSave(); }
  if (e.key === "Enter" && ["schu", "schp"].includes(e.target.id) && POP) { e.preventDefault(); return schedAct({schu: "fgo", schp: "pgo"}[e.target.id], POP.id); }
  if (e.key === "Enter" && e.target.id === "dklim") { e.preventDefault(); return fopAct("dklim"); }
  if (e.key === "Escape" && document.querySelector(".pop")) { e.preventDefault(); closePop(); return; }     // 先关弹窗（有输入的也关），不连带关抽屉
  if (e.key === "Escape" && MORE) { MORE = false; return drawNav(); }
  if (e.key === "Escape" && e.target.id === "fcapin" && FCAP) { e.target.blur(); return fopAct("close"); }
  if (e.key === "Enter" && e.target.id === "lgq") { e.preventDefault(); return logSet({q: e.target.value.trim()}); }
  if (e.key === "Escape" && e.target.id === "lgq" && e.target.value) { e.preventDefault(); LGQ = ""; return logSet({q: ""}); }
  if (e.key === "Enter" && e.target.id === "fwnew" && FWIZ) { e.preventDefault(); return fopAct("wiznew"); }
  if (e.key === "Enter" && FIN[e.target.id]?.[0] === "P" && finOf(FIN[e.target.id])) {
    e.preventDefault(); return fopAct(["pedit", "pdef"].includes(POP?.k) ? "person-pop-save" : "psave");
  }
  if (DKEY && !e.metaKey && !e.ctrlKey && !e.altKey) {         // 抽屉开着：Esc 关（在输入框里先退出输入框），← → 上一个、下一个
    const inp = e.target.closest?.("input,textarea,select,[contenteditable]");
    if (e.key === "Escape") { e.preventDefault(); return inp ? e.target.blur() : closeDrw(); }
    if (!inp && (e.key === "ArrowLeft" || e.key === "ArrowRight")) { e.preventDefault(); return stepDrw(e.key === "ArrowLeft" ? -1 : 1); }
  }
  if ((e.key === "Enter" || e.key === " ") && e.target.matches?.("[tabindex][data-go],[tabindex][data-job],[tabindex][data-drw],[data-sec]")) { e.preventDefault(); e.target.click(); }
});
document.addEventListener("click", e => {
  if (!e.target.closest?.(".popw")) closePop(false);     // 点弹窗外面：等于取消（里面有输入的不关）
  if (e.target.closest?.("[data-now]")) return collectNow();
  const tz = e.target.closest?.("#tlbox [data-tz], #tlbar [data-tl], #tlbar [data-tz], #tlkey [data-tlkey]");
  if (tz) return tlClick(tz);
  if (e.target.closest?.("[data-more]")) { e.preventDefault(); MORE = !MORE; return drawNav(); }      // 窄屏底栏的「更多」
  if (MORE && e.target.closest?.("#tabbar a, #more a")) { MORE = false; drawNav(); }                  // 点了一项：收起面板（同一页不触发换页，这里先收）
  if (e.target.closest?.("[data-dback]")) { const h = DBACK; DBACK = null; return nav(h); }
  if (e.target.closest?.("[data-dclose]")) return closeDrw();
  const wq = e.target.closest?.("[data-when]");
  if (wq) { if (POP) { whenTap(wq.dataset); render(false); } return; }     // 时刻的小签
  const lg = e.target.closest?.("button[data-lg]");
  if (lg) {                                      // 记录页：去掉一个筛选、全部清除、加载更早
    if (lg.disabled) return;
    const k = lg.dataset.lg;
    if (k === "rm") return logSet({[lg.dataset.k]: ""});
    if (k === "clear") { LGQ = null; return nav("#/log"); }
    if (k === "more" && LOG?.rows?.length && !LOG.busy) { lg.disabled = true; return logLoad(route().f, "old"); }
    return;
  }
  const rx = e.target.closest?.("[data-rexp]");
  if (rx) { const k = rx.dataset.rexp; REXP.has(k) ? REXP.delete(k) : REXP.add(k); return render("keep"); }     // 合并的记录：展开、收起
  const rf = e.target.closest?.("[data-rf]");
  if (rf) return route().kind === "log" ? logSet({host: rf.dataset.rf}) : nav(logHref({host: rf.dataset.rf}));   // 记录里的服务器小签：只看它的
  const dn = e.target.closest?.("[data-dnav]");
  if (dn) return stepDrw(+dn.dataset.dnav);
  const rt = e.target.closest?.("[data-rtab]");
  if (rt) {                                      // 记录卡标题里的一项：展开它；已展开的再点收起
    e.preventDefault();
    const k = rt.dataset.rkey, f = ST.get("cardOpen", {}), tabs = ST.get("recTab", {}), on = rt.classList.contains("on");
    f["rec:" + k] = !on; tabs[k] = rt.dataset.rtab; ST.set("cardOpen", f); ST.set("recTab", tabs);
    return render(false);
  }
  const rb = e.target.closest?.("[data-renew]");
  if (rb && LANOFF()) return noNet(e, rb);
  if (rb) { if (rb.disabled) return; rb.disabled = true; POP = null; return renewAct(rb.dataset.renew, rb.dataset.host); }
  if (e.target.closest?.("[data-side]")) {
    document.body.classList.toggle("mini"); ST.set("mini", document.body.classList.contains("mini")); hideTip();
    render(false); return;
  }
  const sec = e.target.closest?.("[data-sec]");
  if (sec && !document.body.classList.contains("mini")) {
    const f = ST.get("fold", {}); f[sec.dataset.sec] = !f[sec.dataset.sec]; ST.set("fold", f); sec.classList.toggle("fold"); sec.setAttribute("aria-expanded", !f[sec.dataset.sec]); return;
  }
  if (e.target.closest?.("[data-gdsave]")) return gdSave();
  const cp = e.target.closest?.("[data-copy]");
  if (cp) {                                      // 复制连接命令
    navigator.clipboard?.writeText(cp.dataset.copy).then(() => { cp.textContent = "已复制"; setTimeout(() => { cp.textContent = "复制"; }, 1500); }).catch(() => {});
    return;
  }
  const wp = e.target.closest?.("[data-wp]");
  if (wp && FWIZ) { FWIZ = {...FWIZ, person: wp.dataset.wp, err: null}; wizPreview(); return; }
  const ws = e.target.closest?.("[data-ws]");
  if (ws && FWIZ && !ws.disabled) { FWIZ = {...FWIZ, spec: ws.dataset.ws, err: null}; wizPreview(); return; }
  const ac = e.target.closest?.("[data-acc]");
  if (ac) { if (!ac.disabled && !ACC.busy) accAct(ac.dataset.acc, ac.dataset); return; }
  const sc = e.target.closest?.("[data-sch]");
  if (sc) { if (!sc.disabled) schedAct(sc.dataset.sch, sc.dataset.p); return; }
  const pj = e.target.closest?.("[data-pj]");
  if (pj) { if (!pj.disabled) pjAct(pj.dataset.pj, pj.dataset.p); return; }
  const cfb = e.target.closest?.("[data-cfb]");
  if (cfb) { if (!cfb.disabled) cfgAct(cfb.dataset.cfb, cfb.dataset.p); return; }
  const rgb = e.target.closest?.("[data-reg]");
  if (rgb && LANOFF() && rgb.dataset.reg !== "mv" && rgb.dataset.reg !== "rm") return noNet(e, rgb);
  if (rgb) { if (!rgb.disabled) regAct(rgb.dataset.reg, rgb.dataset); return; }
  const asg = e.target.closest?.("[data-assign]");
  if (asg) { if (!asg.disabled) assignAct(asg.dataset.assign, asg.dataset); return; }
  const fo = e.target.closest?.("[data-fop]");
  if (fo && LANOFF() && NONET.has(fo.dataset.fop)) return noNet(e, fo);
  if (fo) {
    if (fo.disabled) return;
    const d = fo.dataset, body = d.id || d.kind || d.pool || d.name || d.place ? {ids: d.id ? [d.id] : undefined, kind: d.kind, pool: d.pool, name: d.name, place: d.place} : undefined;
    fo.disabled = true; fopAct(d.fop, body); return;
  }
  const pb = e.target.closest?.("[data-probe]");
  if (pb && LANOFF()) return noNet(e, pb);
  if (pb) { if (!pb.disabled && !PROBING) probeNow(pb.dataset.probe); return; }
  const dw = e.target.closest?.("[data-drw]");
  if (dw && !e.target.closest?.(".pop")) {       // 卡片、服务器行、人名：打开详情抽屉（卡片里的按钮与别的链接照常）
    const inner = e.target.closest("button,a,input,label,summary");
    if ((!inner || inner === dw || !dw.contains(inner)) && !selecting()) {
      e.preventDefault();
      DBACK = DKEY && dw.closest("#drwp") ? location.hash : null;
      return nav(dw.dataset.drw);
    }
  }
  const fr = e.target.closest?.("[data-frange]");
  if (fr) { FRANGE = +fr.dataset.frange; ST.set("frange", FRANGE); render(false); return; }
  const rg = e.target.closest?.("[data-range]");
  if (rg) { RANGE = +rg.dataset.range; ST.set("range", RANGE); render(false); return; }
  const jr = e.target.closest?.("tr[data-job]");
  if (jr && !e.target.closest("a") && !selecting()) {
    const k = jr.dataset.job;
    OPEN.has(k) ? OPEN.delete(k) : OPEN.add(k);
    ST.set("open", [...OPEN].slice(-20)); render(false); return;
  }
  const go = e.target.closest?.("tr[data-go], .rr[data-go]");
  if (go && !e.target.closest("a, button")) return nav(go.dataset.go);
  const a = e.target.closest?.('a[href^="#/"]');          // 页内链接都经 nav；⌘、Ctrl、Shift 点照常在新标签页、新窗口打开
  if (a && !e.defaultPrevented && !e.metaKey && !e.ctrlKey && !e.shiftKey && !e.altKey) { e.preventDefault(); nav(a.getAttribute("href"), !!a.closest("#side, #tabbar, #more")); }
});
document.addEventListener("submit", e => {                // 账号表单（回车也能提交；浏览器的口令管理认得出）
  const f = e.target.closest?.("[data-accf]");
  if (!f) return;
  e.preventDefault();
  if (!ACC.busy) accAct(f.dataset.accf);
});

async function poll() {
  if (TL.d) return;                                       // 正拖着时间线：这次不刷新
  const gen = D.generated, was = OFFLINE, me = ME, archived = JSON.stringify(D.archived);
  try {
    const r = await api("overview.json", {cache: "no-store"});
    if (!r.ok) throw new Error("HTTP " + r.status);
    D = await r.json(); OFFLINE = pollNet(null);
    const a = D.access;                                 // 普通用户的授权与概览由同一次请求取得
    if (ME && (ME.admin ? !!a : !a)) return location.reload();   // 角色变了，重新载入完整的身份与入口（以 D.access 严格区分管理员与普通用户快照）
    if (ME && a) {
      if (JSON.stringify(ME.own) !== JSON.stringify(a.own) || JSON.stringify(ME.hosts) !== JSON.stringify(a.hosts)) {
        ME = {...ME, own: a.own, hosts: a.hosts};
        if (ACC.me) ACC.me = {...ACC.me, own: a.own, hosts: a.hosts};
        for (const f of [...document.getElementById("frames").children]) {
          if (f.dataset.p && !a.own.includes(f.dataset.p)) f.remove();
        }
        const cur = route();
        if (cur.kind === "p" && !a.own.includes(cur.id)) return nav("#/");
      }
    }
    if (FKEEP) { if (D.generated < FKEEP.t) D.fleet = FKEEP.fleet; else FKEEP = null; }
  } catch (e) { OFFLINE = pollNet(e, poll); }
  if (D.generated !== gen || JSON.stringify(D.archived) !== archived || OFFLINE !== was || ME !== me || PENDING) render(false);     // PENDING：上一次被挡住没画（选区放久了也照画，见 editing）
}
// fresh：刚改了配置或状态（保活守护、共享盘归属、预备续租），跳过两次立即采集的最短间隔，免得拿回改之前的快照
async function collectNow(fresh = false) {
  if (!canWrite()) {
    if (canWrite(FRAME)) await frameWin()?.collectNow?.();
    if (fresh) await poll();
    return;
  }
  BUSY = true; render(false);
  try {                                                   // 在项目页上：项目那边也采一次
    const wait = () => { OFFLINE = netText("poll"); render(false); };           // 立即采集做两遍也没事：连不上自动重试，等的时候顶栏说一句
    const [r] = await Promise.all([postJSON(fresh ? "collect?fresh=1" : "collect", {}, {retry: true, onWait: wait}), frameWin()?.collectNow?.()]);
    if (!r.ok) throw new Error("HTTP " + r.status);
    D = await r.json(); OFFLINE = null;
  } catch (e) { OFFLINE = "立即采集失败：" + (e.message || e); }
  BUSY = false; render(false);
}
RHASH = location.hash; render(true);
if (LIVE && fops().op?.active) pollOps();
if (LIVE && D.fleet?.ops?.op?.active) pollOps();                                  // 平台操作途中刷新了页面：接着看进度
if (LIVE) setInterval(poll, 15000);
else setTimeout(() => location.reload(), 60000);
let rzT; window.addEventListener("resize", () => { clearTimeout(rzT); rzT = setTimeout(drawTimeline, 150); });
let vvF = 0; const vvFit = () => { if (!vvF) vvF = requestAnimationFrame(() => { vvF = 0; fitPops(); }); };    // 软键盘弹出、收起：贴底的弹窗跟着让（scroll 很密，一帧量一次）
window.visualViewport?.addEventListener("resize", vvFit);
window.visualViewport?.addEventListener("scroll", vvFit);
