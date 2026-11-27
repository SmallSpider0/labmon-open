/* ---------- 组内服务器的平台操作：与平台核对、开机参数（见 fleetops.py） ---------- */
let OPS = null, OPOLL = null, FCAP = null, FERR = null;   // OPS：每 2 秒取的操作进度；FCAP：正在输验证码 {code, input, t, err, next}
let FSCROLL = null;        // 打开操作面板后，重画完把它滚到眼前（元素 id）
let FFOCUS = null;         // 重画完把光标放进这个输入框（验证码）
let FWIZ = null;           // 换人、换节点、开机向导：{kind: swap | move | open, sid, pool, person, spec, ack, newp, adv: 展开镜像与参数, pv: 预览, err, busy}
let FPED = null;           // 添加组内人员的独立抽屉草稿
const NAME_RE = /^[0-9A-Za-z_~-]{1,32}$/;      // 与 platform.py 一致
async function startOp(act, body, pop = false) {          // 已登录就直接开始，否则先取验证码（pop：验证码出在发起处，true 是按钮旁的弹窗，"renew" 是批量续租抽屉的底部）
  const here = () => pop === "renew" ? RPICK : POP, anchor = POP, from = here(), wiz = FWIZ;   // from：发起的那个弹窗或批量续租抽屉的勾选，回来时已不是它就丢掉验证码
  if (act === "ka" || fops().session?.logged_in) {
    OPS = await fleetPost(act, body);
    if (FWIZ === wiz) FWIZ = null;
    if (anchor && POP === anchor) { FFOCUS = anchor.trigger || FFOCUS; popClose(); }
    pollOps();
  }
  else {
    const j = await fleetPost("captcha");
    if (pop && here() !== from) return;
    FCAP = {code: j.code, tok: j.tok, input: "", t: Date.now(), next: {act, body}, pop}; FFOCUS = "fcapin";   // tok：这张验证码的标识，提交时带回去（别的页面换过就作废）
    if (route().kind === "fleet" && !pop) FSCROLL = "fpanel";
  }
}
async function dismissDone() { if (fops().op?.steps && !fops().op.active) OPS = await fleetPost("dismiss"); }   // 上一次已结束的操作结果先收起
function fops(includeSelf = true) {
  const ops = OPS && (!D.fleet?.ops || OPS.t >= D.fleet.ops.t) ? OPS : D.fleet?.ops || {};
  const me = ME?.fleet_person, people = ops.people || {};
  return includeSelf && me && !people[me] ? {...ops, people: {...people, [me]: {slots: []}}} : ops;
}
async function fleetPost(act, body) {
  const r = await postJSON(`fleet/op/${act}`, body || {});
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || "HTTP " + r.status);
  return j;
}
async function pollOps() {
  if (OPOLL) return;
  OPOLL = setInterval(async () => {
    try { const r = await api("fleet/op.json", {cache: "no-store"}); if (r.ok) OPS = await r.json(); } catch (e) {}     // 每 2 秒取一次：连不上就等下一次
    if (!fops().op?.active) { clearInterval(OPOLL); OPOLL = null; return collectNow(true); }    // 做完了：立即采集，页面换成做完后的样子
    render(false);
  }, 2000);
}
// 向导的预览：要提交的参数、挑出的端口、本地能核对的各项（不连平台；真正开始前后台会连平台再核一次）
let WSEQ = 0;
async function wizPreview() {
  const W = FWIZ;
  if (!W) return;
  const q = new URLSearchParams({kind: W.kind, person: W.person || ""});
  if (W.sid) q.set("sid", W.sid); else q.set("pool", W.pool);
  if (W.spec) q.set("spec", W.spec);
  const n = ++WSEQ;
  W.busy = true; render(false);
  try {
    const r = await api("fleet/plan.json?" + q, {cache: "no-store"}, {retry: true}), j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.error || "HTTP " + r.status);
    if (n === WSEQ) { W.pv = j; W.err = null; }
  } catch (e) { if (n === WSEQ) { W.pv = null; W.err = "预览失败：" + (e.message || e); } }
  if (n === WSEQ) W.busy = false;
  render(false);
}
// 操作面板开在哪个抽屉里（换人、开机）：那个抽屉没开着就打开它
function toPanel() {
  const P = fPanel(), h = P.sid && srvById(P.sid) ? DH.srv(P.sid) : !P.sid && P.pool ? DH.new(P.pool) : null, d = route().drw;
  if (h && !(d && DH[d.k](d.id) === h)) nav(h);
}
// 平台操作：已登录就直接做；否则先取验证码，人输完再做（next 是输完后要做的）
async function fopAct(act, body) {
  FERR = null;
  const onFleet = route().kind === "fleet", panel = async () => {          // 开新面板：上一次已结束的操作结果先收起，免得挡住它
    await dismissDone(); FWIZ = null; popClose(); if (onFleet) FSCROLL = "fpanel";
  };
  try {
    if (act === "newcode") {
      const cap = FCAP, j = await fleetPost("captcha");
      if (FCAP === cap) { Object.assign(cap, {code: j.code, tok: j.tok, t: Date.now(), err: null}); FFOCUS = "fcapin"; }
    }
    else if (act === "close") FCAP = null;
    else if (act === "sync") {
      popToggle("sync", "", {route: location.hash, trigger: "fleet-sync"});
      const pop = POP;
      if (pop) {
        try { await startOp(act, body, true); }
        catch (e) { if (POP === pop) pop.err = e.message || String(e); }
      }
    }
    else if (act === "renewone") popToggle("renew", body.ids[0]);
    else if (act === "renew1") { await dismissDone(); await startOp("renew", {ids: [POP.id], ack: POP.ack ? [POP.id] : []}, true); }
    else if (act === "rpop") popToggle(body.kind, body.ids[0]);          // 主机页的预备续租、取消预备
    else if (act === "guard-edit") {
      popToggle("guard-edit", "", {route: location.hash, trigger: "open-guard-edit", draft: {...fops().guard}});
      FFOCUS = POP ? "gdidle" : "open-guard-edit";
    }
    else if (act === "popx") {
      if (POP?.trigger) { document.activeElement?.blur(); FFOCUS = POP.trigger; }
      popClose();
    }
    else if (act === "renewgo" && RPICK && !RPICK.busy) {        // 批量续租抽屉：验证码出在抽屉底部（FCAP.pop = "renew"），开始后进度出在抽屉顶上
      const R = RPICK, {ids, ack} = rpBody(R);
      R.busy = true;
      try {
        await dismissDone();
        if (RPICK === R) await startOp("renew", {ids, ack}, "renew");
      } finally { R.busy = false; }
    }
    else if (act === "offpick") popToggle("off", body.ids[0]);
    else if (act === "offgo") { await dismissDone(); await startOp("off", {ids: [POP.id], ack: POP.ack ? [POP.id] : []}, true); }
    else if (act === "wiz") {                         // 换人/换节点：沿用这台的池；开机：已关机的服务器缺省还给原来的人；开新的：选了池
      const x = body.ids ? srvById(body.ids[0]) : null, move = body.kind === "move";
      await panel();
      FWIZ = {kind: body.kind, sid: x?.id || null, pool: x?.pool || body.pool,
              person: move ? x?.holder || null : body.name || (body.kind === "open" && x ? x.off?.holder || x.holder : null),
              spec: null, ack: false, newp: "", adv: false};
      wizPreview(); toPanel();          // 从人员抽屉、卡片墙点的：打开那一台（或那个池「开新的」）的抽屉，向导在里面
    } else if (act === "wizcancel") FWIZ = null;
    else if (act === "wizadv") FWIZ.adv = !FWIZ.adv;
    else if (act === "wiznew") {
      const n = FWIZ.newp.trim();
      if (!NAME_RE.test(n)) { FWIZ.err = "名字也是平台作业名：只能用字母、数字、_ ~ -，最多 32 个"; return render(false); }
      FWIZ = {...FWIZ, person: n, newp: "", err: null}; wizPreview();
    } else if (act === "wizgo") {
      const W = FWIZ;
      await startOp(W.kind, {ids: W.sid ? [W.sid] : [], ...(W.kind === "move" ? {} : {person: W.person}), pool: W.pool,
        ...(W.spec ? {spec: W.spec} : {}), ack: W.ack && W.sid ? [W.sid] : []});
    } else if (act === "pgive") {
      if (popIs("pgive", body.name) && POP.place !== body.place) popClose();
      popToggle("pgive", body.name, {route: location.hash, place: body.place, trigger: personTrigger(act, body.name, body.place)});
    }
    else if (act === "padd") { const d = fops().defaults || {}; FPED = {name: "", image: d.image || "", run_cmd: d.run_cmd || "", note: "", isNew: true}; nav(DH.person("+")); }
    else if (act === "pedit" || act === "pdef" || act === "pdel") {
      const n = body?.name || "", p = act === "pdef" ? fops().defaults || {} : fops().people?.[n] || {};
      if (popIs(act, n) && POP.place !== body?.place) popClose();
      popToggle(act, n, {route: location.hash, place: body?.place, trigger: personTrigger(act, n, body?.place),
        draft: {name: n, image: p.image || "", run_cmd: p.run_cmd || "", note: p.note || ""}});
      if (POP && act !== "pdel") FFOCUS = "fpimg";
    }
    else if (act === "dklim") {                      // 共享盘提醒线：在这人的抽屉里改，空着就是不设
      const v = (document.getElementById("dklim")?.value || "").trim(), n = route().drw?.id;
      if (v && !(+v > 0)) throw new Error("提醒线要写一个正数（GiB），不设就空着");
      return diskSet({name: n, limit: v ? +v : null});
    }
    else if (act === "person-pop-save") {
      const pop = POP;
      if (!pop || !["pedit", "pdef", "pdel"].includes(pop.k) || pop.busy) return;
      const {name, image, run_cmd, note} = pop.draft;
      const defaults = pop.k === "pdef", remove = pop.k === "pdel";
      document.activeElement?.blur();
      pop.busy = true; pop.err = null; render(false);
      try {
        OPS = await fleetPost(defaults ? "defaults" : "person", defaults ? {image, run_cmd}
          : remove ? {name, remove} : {name, image, run_cmd, note, create: false});
        if (POP === pop) {
          FFOCUS = pop.trigger; popClose();
          if (remove) closeDrw();
        }
      } catch (e) { if (POP === pop) pop.err = e.message || String(e); }
      pop.busy = false;
    }
    else if (act === "pcancel") { const nw = FPED?.isNew; FPED = null; if (nw) closeDrw(); }
    else if (act === "psave") {                      // 新的人存好后，抽屉换成这人的
      const nw = FPED.isNew, name = FPED.name.trim(), editing = FPED;
      OPS = await fleetPost("person", {name, image: FPED.image, run_cmd: FPED.run_cmd, note: FPED.note, create: nw});
      if (FPED !== editing) return;
      FPED = null;
      if (nw) { nav(DH.person(name)); if (ME?.scopes.includes("admin")) accLoad("users"); }
    }
    else if (act === "ka") await startOp("ka", body);
    else if (BACTS.has(act)) await borrowAct(act, body?.ids?.[0] || POP?.id);
    else if (act === "kamark") { await fleetPost("keepalive", {id: body.ids[0], pid: +body.name}); return probeNow(body.ids[0]); }   // 认作保活，探一次才生效
    else if (act === "go") {
      const cap = FCAP, wiz = FWIZ;
      if (!cap || cap.busy) return;
      const code = (document.getElementById("fcapin")?.value || cap.input || "").trim();
      document.activeElement?.blur();
      if (!code) { cap.err = "先输验证码"; FFOCUS = "fcapin"; return render(false); }
      cap.busy = true;
      try {
        OPS = await fleetPost(cap.next.act, {...cap.next.body, code, tok: cap.tok});
        if (FCAP === cap) {
          if (cap.pop) { FFOCUS = POP?.trigger || FFOCUS; popClose(); }
          else FCAP = null;
        }
        if (FWIZ === wiz) FWIZ = null;
        pollOps();
      } catch (e) { if (FCAP === cap) { cap.err = e.message; FFOCUS = "fcapin"; } }
      cap.busy = false;
    } else if (act === "dismiss" || act === "logout") OPS = await fleetPost(act);
    else await startOp(act, body);
  } catch (e) { const m = e.message || String(e); if (FPED) FPED.err = m; else FERR = m; }
  const ae = document.activeElement, fi = ae && FIN[ae.id];        // 表单已关（回车保存）：输入框失焦，免得重画一直被「正在输入」挡着
  if (fi && !finOf(fi)) ae.blur();
  if (!POP) whenBlur();                                            // 借来用、改约定的弹窗关了：时刻控件同样失焦
  render(false);
}
const fopBtn = (act, text, cls = "", hint = "") => `<button class="btn ${cls}" data-fop="${act}" ${hint ? tip(hint) : ""}>${text}</button>`;
const capBox = () => `<div class="rn-note">登录平台后${esc(opName(FCAP.next.act))}。<span class="sm mut">同一账号只保留一个登录：网页上正在用这个账号的人会被顶下线。</span></div>
  <div class="rn"><span class="cap-code" data-fop="newcode" ${tip("看不清或过期了：点一下换一张")}>${esc(FCAP.code)}</span>
  <input id="fcapin" class="cap-in" autocomplete="off" spellcheck="false" placeholder="输入验证码" value="${esc(FCAP.input || "")}">
  ${fopBtn("go", "登录并开始", "pri")}${FCAP.pop ? popX : fopBtn("close", "取消")}<span id="capage" class="sm mut">${capAge()}</span></div>${FCAP.err ? `<div class="rn-err">${esc(FCAP.err)}</div>` : ""}`;
function syncPop(btn) {
  const open = popIs("sync", "");
  return popConfirm(btn, open, !open ? "" : FCAP?.pop ? capBox() : POP.err
    ? `<div class="rn-err">${esc(POP.err)}</div>` : "", FCAP?.pop ? null : "", !!FCAP?.pop);
}
// 操作面板：操作进度与换人、开机向导，同一时间只有一个；x 给了就只看这一台的（详情页）。
// 续租一台、关机的确认（和它们的验证码）在按钮旁的弹窗里，批量续租的验证码在它的抽屉底部（FCAP.pop），都不在面板里。
// sid：组内服务器页把面板放进这一台的卡里；没有 sid 而有 pool：放在这个池「+ 开新的」那一台；都没有就另起一张卡
function fPanel(x) {
  const o = fops(), op = o.op, one = ids => (ids || []).length === 1 ? ids[0] : null;
  let h = "", sid = null, pool = null;
  if (op?.steps && (!x || (op.ids || []).includes(x.id) || op.conn?.sid === x.id)) {
    h = opProgress(op); sid = [op.conn?.sid, one(op.ids)].find(i => i && srvById(i)) || null; pool = sid ? null : op.pool;
  } else if (FCAP && !FCAP.pop && (!x || (FCAP.next.body?.ids || []).includes(x.id))) { h = capBox(); sid = one(FCAP.next.body?.ids); pool = FCAP.next.body?.pool; }
  else if (FWIZ && (!x || FWIZ.sid === x.id)) { h = wizForm(); sid = FWIZ.sid; pool = FWIZ.pool; }
  if (h && FERR && !x) h += `<div class="rn-err">${esc(FERR)}</div>`;
  return {h, sid, pool};
}
// 这一台此刻不能直接动（续租、关机、换人前要勾确认，同后端 fleetops._risk）：在跑、有未识别的 GPU 进程（FBUSY，会中断它），
// 或本地盘开机以来 labmon 之外写了不少（lw.risk，会清空）
const FBUSY = x => ["job", "unknown"].includes(x.state);
const FRISKY = x => FBUSY(x) || !!x.lw?.risk;
// 勾选框的文字（续租一台、批量续租、关机、换人共用）
const ackText = x => { const w = [FBUSY(x) && "可以中断", x.lw?.risk && "本地盘可以清空"].filter(Boolean).join("、");
  return myFleetHost(x) ? `确认${w}` : `已和 ${esc(x.holder)} 说好，${w}`; };
const workOf = x => (x.sample?.work || x.work || []).filter(v => v.kind !== "keepalive").map(v => v.label).slice(0, 2).join("、");
// 会中断什么（FBUSY 时）：批量续租的注意、确认里要勾的风险共用；verb：续租、关机、换人
const busyText = (x, verb) => { if (!FBUSY(x)) return ""; const w = workOf(x);
  return `${x.state === "job" ? `在跑${x.mine ? "任务" : "实验"}` : x.mine ? "有 labmon 之外的 GPU 进程" : "有未识别的 GPU 进程"}${w ? `（${w}）` : ""}，${verb}会中断它`; };
// 平台操作（续租一台、关机、换人）确认的分层（#14）：一句结论 riskSay → 要勾才动的风险 riskNeed（同 FRISKY，后端 fleetops._risk）→ 其余折进 riskMore。
// 结论只说会中断、清空、丢掉什么（lose：调用处另加的，如自用主机补不了的本地文件），具体的进程与路径在 riskNeed 里
function riskSay(x, lose = []) {
  const hit = [FBUSY(x) && (x.state === "job" ? `中断在跑的${x.mine ? "任务" : "实验"}` : x.mine ? "中断 labmon 之外的 GPU 进程" : "中断未识别的 GPU 进程"),
    x.lw?.risk && `清空本地盘上 labmon 之外写的 ${g1(lwOut(x.lw))} GiB`, ...lose].filter(Boolean);
  return hit.length ? `会${hit.join("，")}。` : x.state === "down" ? "现在连不上，看不到在跑什么。" : !x.state ? "还没探到在跑什么。"
    : `现在没有${x.mine ? "任务" : "实验"}在跑，本地盘也没写多少。`;
}
// 要勾才动的风险：琥珀框里逐条写会中断的进程、会清空的本地盘（路径能断行），下面是勾选框（attr：弹窗 data-pack 记进 POP.ack，换人向导 data-wack 记进 FWIZ.ack）
const riskNeed = (x, verb, attr, on) => !FRISKY(x) ? "" : `<div class="rk-need"><ul>${[busyText(x, verb), lwText(x.lw, verb)].filter(Boolean).map(t => `<li>${esc(t)}</li>`).join("")}</ul>
  <label class="pop-ck"><input type="checkbox" ${attr} ${on ? "checked" : ""}> ${ackText(x)}</label></div>`;
// 弹窗里折起来的细节；开合记在 POP.more（document 上的 toggle 监听记），定时重画不收回去
const riskMore = body => body ? `<details class="rk-more" ${POP?.more ? "open" : ""}><summary>细节</summary>${body}</details>` : "";
// 关机的确认（弹窗里，分层见 riskSay）：做什么、会不会中断；在跑别人实验的要勾「已说好」，自用主机 worker 有任务就不让关
function offAsk(x) {
  const down = x.state === "down", busy = x.mine && x.state === "job";
  const what = x.mine ? `在平台上停掉 ${esc(x.holder)} 的作业 ${esc(x.job)}，主机清单里这台标成已归还（看板不再连它，也不再拉起 worker）。之后要用可以在这个池点「开新的」。`
    : `在平台上停掉它的作业（作业名、资源池、端口 22→${esc(x.port)} 都对上才停；开始前再探一次）。之后这台不再探、不提醒租期、不算配额；要用时点「开机」。`;
  const head = `<b>关机${x.mine ? (myFleetHost(x) ? "（我的自用主机）" : "（自用主机）") : " " + esc(x.holder)}</b>：${x.mine ? "停掉作业，这台标成已归还" : "停掉作业，之后不再探、不提醒租期"}。`;
  return `<div class="rk-say">${head}${busy ? "" : esc(riskSay(x))}</div>`
    + (busy ? `<div class="rn-err">worker 还有任务在跑（${esc(x.work.map(w => w.label).join("、"))}）：先在<a href="#/host/${encodeURIComponent(x.id)}">主机页</a>暂停队列。</div>`
      : riskNeed(x, "关机", "data-pack", POP.ack))
    + riskMore(`<div>${what}</div>${x.mine ? "" : `<div class="mut">要接着给别人用，点「换人」更好：停旧后立刻开新的，这台不会被别人占走。</div>`}`
      + (down && !x.mine ? `<div class="pop-w">现在连不上：平台上要是已经没有它运行中的作业，就只在看板上标为已关机。</div>` : ""));
}
const offGo = x => `<button class="btn pri dz" data-fop="offgo" data-id="${esc(x.id)}" ${x.mine && x.state === "job" || FRISKY(x) && !POP.ack || fops().op?.active ? "disabled" : ""}>关机</button>`;
// 换人、换节点、开机向导：选人（换节点同人免选）与规格 → 一句话核对 → 一个按钮。镜像与参数收在「高级」里
const PGN = {me: "我", team: "工作台"};
function wizForm() {
  const W = FWIZ, o = fops(), x = W.sid ? srvById(W.sid) : null, pv = W.pv || {}, ppl = o.people || {};
  const swap = W.kind === "swap", move = W.kind === "move", stopping = swap || move;
  if (!move && ME?.fleet_person_error) return `<div class="rn-err">${esc(ME.fleet_person_error)}</div><div class="rn">${fopBtn("wizcancel", "关掉")}</div>`;
  if (W.sid && !x) return `<div class="rn-note">组内服务器里已经没有 ${esc(W.sid)}。</div><div class="rn">${fopBtn("wizcancel", "关掉")}</div>`;
  const q = move ? `换到哪个节点（规格）？<span class="sm mut">同一个人（${esc(x.holder)}）、同一个池：停掉旧作业后立刻按新规格开，空档十几秒</span>`
    : swap ? `把这台换给谁？<span class="sm mut">停掉 ${esc(x.holder)} 的作业后立刻开新的，沿用端口，空档十几秒</span>`
    : x ? `重开这一台给谁用？` : `在这个池开一台给谁用？<span class="sm mut">端口自动挑</span>`;
  let ppHtml = "";
  if (!move) {
    const names = Object.keys(ppl).sort(byName).filter(n => !(swap && n === x.holder));     // 选人只按首字母：要分给的多半是还没服务器的
    if (W.person && !ppl[W.person]) names.push(W.person);                     // 刚输的新名字
    const chip = n => { const p = ppl[n], sl = p?.slots || [];
      const sub = !p ? "新的人" : n === ME?.fleet_person ? (sl.length ? `我 · 占 ${sl.length} 台` : "我") : sl.length ? `占 ${sl.length} 台` : "没占服务器";
      return `<button class="pchip${W.person === n ? " on" : ""}" data-wp="${esc(n)}" ${tip(p ? [p.image ? `镜像 ${p.image}（${SRCN[p.src] || ""}）` : "还没读到镜像", p.run_cmd ? "启动命令 " + p.run_cmd : "", p.note || "", sl.length ? "占着 " + sl.join("、") : ""].filter(Boolean).join("\n") : "新的人：按新的人默认的镜像与启动命令开")}>
        ${pfav(n, "", "fav sm")}<span class="pc-t"><b>${esc(n)}</b><span>${esc(sub)}</span></span></button>`; };
    const grp = g => names.filter(n => (n === ME?.fleet_person ? "me" : "team") === g);
    const rows = ["me", "team"].map(g => grp(g).length ? `<div class="pg-row"><span class="k">${PGN[g]}</span><div class="pchips">${grp(g).map(chip).join("")}</div></div>` : "").join("");
    ppHtml = `<div class="wz-pp">${rows}<div class="pg-row"><span class="k">新的人</span><span class="pnew"><input id="fwnew" class="cap-in" placeholder="名字（也是平台作业名）" value="${esc(W.newp)}" autocomplete="off" spellcheck="false">${fopBtn("wiznew", "用这个名字")}</span></div></div>`;
  }
  const specs = (pv.specs?.length ? pv.specs : poolSpecs(W.pool)) || [];
  const origSpec = pv.orig_spec || (x && o.boot?.[x.id]?.spec) || o.pools?.[W.pool]?.spec || null;
  const curSpec = W.spec || (move ? null : origSpec);
  const spChip = s => {
    const id = s.specificationId, nm = s.specificationName || id, avail = s.quantityAvailable, tot = s.specificationAmount;
    const isOrig = !!origSpec && id === origSpec, on = curSpec === id, dis = move && isOrig ? "disabled" : "";
    const zero = avail != null && Number(avail) < 1 && !(swap && isOrig);
    const sub = [isOrig ? (move ? "当前" : "原规格") : "", avail != null ? `空 ${avail}/${tot ?? "—"}` : ""].filter(Boolean).join(" · ");
    const h = isOrig && move ? "已在当前规格上：原地重启请用「续租」"
      : `${nm}${avail != null ? `\n上次核对时空 ${avail} / ${tot ?? "—"}` : ""}${String(s.isSchedule) === "0" ? "\n不可排队" : ""}\n提交前会连平台再核对一次`;
    return `<button class="pchip spchip${on ? " on" : ""}${zero ? " zero" : ""}" data-ws="${esc(id)}" ${dis} ${tip(h)}><span class="pc-t"><b>${esc(nm)}</b><span>${esc(sub || id)}</span></span></button>`;
  };
  const spHtml = specs.length || move ? `<div class="wz-pp"><div class="pg-row"><span class="k" ${tip(o.pools_t ? `上次核对：${ts(o.pools_t)}；提交前会连平台再核对一次` : "提交前会连平台再核对一次")}>${move ? "目标规格" : "规格"}</span><div class="pchips">${specs.length ? specs.map(spChip).join("") : `<span class="sm mut">还没有这个池的规格列表：先点「与平台核对」</span>`}</div></div></div>` : "";
  const picked = move ? !!W.spec : !!W.person;
  const items = (pv.items || []).filter(i => (W.person || i.item !== "作业名") && (!move || W.spec || i.item !== "规格"));
  const bad = items.filter(i => i.ok === false), unk = items.filter(i => i.ok === null);
  const busyMine = stopping && x.mine && x.state === "job", risky = stopping && FRISKY(x) && !busyMine;   // 自用主机上 labmon 之外的 GPU 进程、本地盘写得多也要勾
  const ready = picked && items.length && !bad.length && !W.busy && !(risky && !W.ack) && !busyMine && !o.op?.active;
  const it = k => items.find(i => i.item === k)?.note || "";
  const base = s => String(s || "—").split("/").pop();     // 镜像只写最后一段
  const who = move ? x.holder : W.person;
  let chk = "";
  if (picked) {
    if (W.busy && !items.length) chk = `<div class="sm mut">核对中…</div>`;
    else if (bad.length) chk = `<div class="steps">${bad.map(i => `<span class="i err">✕</span><span>${esc(i.item)}</span><span class="sm err">${esc(i.note)}</span>`).join("")}</div>`;
    else if (items.length) chk = `<div class="wz-ok${unk.length ? " warn" : ""}"><span class="i ok">${unk.length ? "？" : "✓"}</span><span>${[`作业名 <b>${esc(pv.job || who)}</b>${pv.job && pv.job !== who ? `<span class="mut">（${esc(who)} 已在用）</span>` : ""}`,
        pv.spec_name ? `规格 <b>${esc(pv.spec_name)}</b>${pv.orig_spec && pv.spec !== pv.orig_spec ? '<span class="mut">（新规格）</span>' : ""}` : "",
        `端口 ${esc(it("端口"))}`, `镜像 <code>${esc(base(pv.image))}</code> <span class="mut">${esc(pv.image_src || "")}</span>`, esc(it("配额"))].filter(Boolean).join(" · ")}
        ${unk.length ? `<div class="sm" style="color:var(--warn)">${esc(unk.map(i => i.note.startsWith("还没") ? i.note : `${i.item}没核对上（${pv.checked_at ? `按 ${ts(pv.checked_at)} 读的` : "还没读过资源池与镜像"}），开始前连平台再核一次`).join("；"))}</div>` : ""}</span></div>`;
  }
  const nodes = pv.nodes || [];
  const kv = picked && pv.name ? [["作业名", esc(pv.name)], ...(pv.spec_name || pv.spec ? [["规格", esc(pv.spec_name || pv.spec)]] : []),
    ["镜像", `<code>${esc(pv.image || "—")}</code> <span class="sm mut">${esc(pv.image_src || "")}</span>`],
    ["连接", pv.port ? nodes.length === 1 && pv.spec === pv.orig_spec ? `<code>ssh -p ${esc(pv.port)} ${esc(nodes[0])}</code>` : `端口 ${esc(pv.port)}，节点 ${esc(nodes.map(fNode).join("、") || "—")} 之一（开好后看平台分到哪台）` : "—"],
    ["启动命令", `<code>${esc(pv.run_cmd || "—")}</code>`], ...((pv.volumes || []).length ? [["挂载", pv.volumes.map(esc).join("<br>")]] : [])] : [];
  const adv = W.adv ? `<div class="wz-adv">${kv.length ? `<div class="kv">${kv.map(([k, v]) => `<span>${k}</span><span>${v}</span>`).join("")}</div>` : ""}
      <div class="sm mut">${ppl[who] ? `镜像与启动命令按这人的设置；要换就到 <a href="${esc(DH.person(who))}" data-drw="${esc(DH.person(who))}">${esc(who)} 的设置</a>里改。` : "新的人按新的人默认的镜像与启动命令开；要换先在「人员」里加上这人。"}</div></div>` : "";
  const go = move ? (W.spec ? `停 ${x.holder}（${x.id}），换到 ${specName(W.pool, W.spec)}` : `停 ${x.holder}，换节点`)
    : swap ? `停 ${x.holder}，换给 ${W.person || "？"}` : `给 ${W.person || "？"} 开机`;
  const why = move && !W.spec ? "先选一个目标规格" : !move && !W.person ? "先点一个人" : busyMine ? "" : bad.length ? `${bad.map(i => i.item).join("、")}不对，改好才能开始` : risky && !W.ack ? "先勾“已说好”" : "";
  return `<div class="wz-q">${q}</div>${ppHtml}${spHtml}${chk}
    ${picked && pv.after ? `<div class="rn-state">${esc(pv.after)}</div>` : ""}
    ${busyMine ? `<div class="rn-state bad">这台 worker 还有任务在跑（${esc(x.work.map(w => w.label).join("、"))}）：先在<a href="#/host/${encodeURIComponent(x.id)}">主机页</a>暂停队列。</div>` : ""}
    ${risky ? riskNeed(x, move ? "换节点" : "换人", "data-wack", W.ack) : ""}
    ${picked ? `<button class="lnk sm wz-tg" data-fop="wizadv">${W.adv ? "收起镜像与参数" : "镜像与参数"}</button>${adv}` : ""}
    <div class="rn"><button class="btn pri" data-fop="wizgo" ${ready ? "" : "disabled"} ${tip("开始前连平台再核一次，有一项不对就什么都不动；出错停在那一步，不自动重试")}>${esc(go)}</button>${fopBtn("wizcancel", "取消")}
      ${why ? `<span class="sm" style="color:${!picked ? "var(--muted)" : "var(--warn)"}">${esc(why)}</span>` : ""}</div>
    ${W.err ? `<div class="rn-err">${esc(W.err)}</div>` : ""}`;
}
function opProgress(op) {
  const what = opName(op.kind) + (op.person && ["swap", "open"].includes(op.kind) ? `（${op.kind === "swap" ? "→ " : ""}${op.person}）`
    : op.kind === "move" && (op.spec_name || op.spec) ? `（→ ${op.spec_name || op.spec}）` : "");
  const head = op.active ? `${what}进行中，可以关掉页面，服务在后台做完` : op.error ? `${what}停下了` : `${what}完成`;
  return `<div class="rn-note">${esc(head)}<span class="sm mut">（${ts(op.t0)} 开始）</span></div>
    <div class="steps">${op.steps.map(x => `<span class="i ${x.state}">${RICON[x.state]}</span><span class="${x.state === "err" ? "err" : ""}">${esc(x.name)}</span><span class="sm mut">${esc(x.note || "")}</span>`).join("")}</div>`
    + (op.server ? `<div class="rn-state${op.error ? " bad" : ""}">${esc(op.server)}</div>` : "")
    + (op.conn && !op.active && !op.error ? op.conn.mine ? `<div class="rn-state">已接成 ${esc(op.conn.holder)} 的自用主机：<code>${esc(op.conn.cmd)}</code> <button class="lnk" data-copy="${esc(op.conn.cmd)}">复制</button> · <a href="#/host/${encodeURIComponent(op.conn.sid)}">主机页</a>（下一轮采集拉起 worker）${adoptHint(op.conn.sid)}</div>`
      : `<div class="rn-state">发给 ${esc(op.conn.holder)}：<code>${esc(op.conn.cmd)}</code> <button class="lnk" data-copy="${esc(op.conn.cmd)}">复制</button></div>` : "")
    + (op.active ? "" : `<div class="rn">${fopBtn("dismiss", "知道了")}</div>`);
}
// 续租这一台要注意的（批量续租的抽屉与续租一台的弹窗共用）；local：带上本地文件会丢的一句（弹窗里自用主机改用 lossNote 的明细）；
// risk：带上要勾的两件（会中断、会清空本地盘；弹窗里它们在 riskNeed 里）
function renewWarn(x, {local = true, risk = true} = {}) {
  const w = [], fatal = x.mine && local ? (D.hosts.find(e => e.id === x.id)?.status?.local?.jobs || []).filter(r => !r.heal) : [];
  if (risk && FBUSY(x)) w.push(busyText(x, "续租"));
  if (x.state === "down") w.push(x.down || "连不上");          // x.down 本身就是一句（“连不上：timeout”）
  if (!x.last && !x.mine) w.push("还没探过");
  if (fatal.length) w.push(`${fatal.length} 格要用的本地文件会随容器重启清空，续租完先不恢复放行`);
  if (risk && x.lw?.risk) w.push(lwText(x.lw));
  if (x.lease_end && leaseLeft(x.lease_end) > 2 * 86400) w.push(`租期还${leaseRem(x.lease_end)}，不急`);
  return w;
}
// 批量续租的抽屉（#14；#/fleet/renew，从提醒条点的 #/fleet/renew/<id> 只勾那一台）：一台一行，勾选、状态、租期，下一行要注意的；
// 勾上了又有风险的（FRISKY）在本行勾「已说好」。勾选记在 RPICK（按抽屉地址建、关抽屉清掉）；没登录时验证码出在底部（FCAP.pop = "renew"），开始后进度在顶上；
// 输验证码、提交中勾选锁住（lock）：要提交的已记进 FCAP.next，改了也不会跟着变
let RPICK = null;          // {key: 抽屉地址, sel: 勾了要续的, ack: 勾了「已说好」的, busy}
const rpList = () => [...D.fleet.servers, ...D.fleet.mine];
const rpBorrowed = x => !x.mine && borrowOf(x);
const rpBody = R => { const ids = rpList().filter(x => R.sel.has(x.id) && !x.off && !rpBorrowed(x)).map(x => x.id); return {ids, ack: ids.filter(i => R.ack.has(i))}; };
function renewDrawer(id) {
  const key = DH.renew(id), op = fops().op, sv = rpList(), ops = canOps();
  if (RPICK?.key !== key) {                         // 换了地址就是另一份勾选：缺省勾上 24 小时内到期、没在跑实验且没借用中的；上一份的验证码作废（要提交的记在 FCAP.next 里，不跟着变）
    RPICK = {key, sel: new Set(id ? [id] : fleetDue().filter(x => !FRISKY(x) && !rpBorrowed(x)).map(x => x.id)), ack: new Set()};
    if (FCAP?.pop === "renew") FCAP = null;
  }
  const R = RPICK, lock = !ops || FCAP?.pop === "renew" || R.busy, {ids} = rpBody(R), need = sv.filter(x => ids.includes(x.id) && FRISKY(x) && !R.ack.has(x.id));
  const row = x => { const br = rpBorrowed(x), dis = x.off || !!br, on = R.sel.has(x.id) && !dis;
    const w = x.off ? ["已关机：要用就开机"]
      : br ? ["正借来在用：先在它的「借用」里还掉（若已被平台停掉连不上，先点「与平台核对」，确认作业不在运行后会自动归还）再续租"]
      : renewWarn(x), nm = x.mine ? x.label : x.holder;
    return `<div class="rp-row"><input type="checkbox" data-rsel="${esc(x.id)}" ${on ? "checked" : ""} ${dis || lock ? "disabled" : ""} aria-label="续租 ${esc(nm)}">
      <div class="rp-m"><div class="rp-h"><b>${esc(nm)}</b><span class="sm mut">${esc(x.id)} · ${fKind(x)}</span>${fState(x)}<span class="sp"></span><span class="sm nw">${x.lease_end ? leaseHtml(x.lease_end) : "—"}</span></div>
      ${w.length ? `<div class="sm rp-w">${esc(w.join("；"))}</div>` : ""}
      ${on && FRISKY(x) ? `<label class="sm rp-ack"><input type="checkbox" data-rack="${esc(x.id)}" ${R.ack.has(x.id) ? "checked" : ""} ${lock ? "disabled" : ""}> ${ackText(x)}</label>` : ""}</div></div>`; };
  const why = !ids.length ? "先勾要续租的服务器" : need.length ? `${need.map(x => x.mine ? x.label : x.holder).join("、")} 在跑实验或本地盘写得多：先勾选确认` : "";
  const foot = !ops ? `<div class="rn">${opsNote()}</div>` : FCAP?.pop === "renew" ? capBox()
    : `<div class="rn"><button class="btn pri" data-fop="renewgo" ${!ids.length || need.length || R.busy || op?.active ? "disabled" : ""}>续租所选（${ids.length} 台）</button>
      <button class="btn" data-dclose>取消</button>${why ? `<span class="sm" style="color:${ids.length ? "var(--warn)" : "var(--muted)"}">${esc(why)}</span>` : ""}</div>`;
  return dHead(`<span class="fav">${icon("hourglass")}</span>`, "续租", "只登录一次，逐台：停旧作业 → 提交新作业、等运行 → ssh 上去开保活（自用主机是拉起 worker）；一台续完再动下一台")
    + `<div class="db">${op?.steps && op.kind === "renew" ? `<div class="card">${opProgress(op)}</div>` : ""}
      ${dSec("服务器", sv.map(row).join("") || `<div class="empty">没有服务器。</div>`, `<span class="sm mut">开始前会再探一次各台，在跑实验或本地盘写得多而这里没确认的就停，哪台都不动。状态是 ${D.fleet.interval / 60 | 0} 分钟内探的</span>`)}</div>
    <footer class="dft">${foot}${fErr()}</footer>`;
}
// 人员：名单里一人一张小卡（看板账号、占着的服务器、近 30 天用量）；点卡片打开这人的详情抽屉，分配一台、改设置都在抽屉里（#15：卡上不放没文字的图标）
const SRCN = {job: "从现有作业读的", default: "新的人默认", manual: "手动设置", roster: "只登记了姓名"};
const imgTag = img => { const last = String(img || "").split("/").pop(); return last.split(":")[1] || last; };
function peopleTab() {
  const L = directoryUsers(), people = directoryPeople(L);
  const o = fops(), ppl = o.people || {}, hs = Object.fromEntries((D.fleet?.holders || []).map(h => [h.holder, h])), d = o.defaults || {};
  const current = [...new Set([...peopleOrder(), ...(L?.people || [])])];
  const gone = Object.keys(hs).filter(n => !current.includes(n)).sort(byName);
  const missing = L?.users.filter(u => !people.has(u.fleet_person)) || [];
  const err = ACC.loadError.users || L?.people_error;
  const feedback = accErr(err);
  const repair = missing.length ? `<p class="sm mut">账号 ${esc(missing.map(u => u.fleet_person_error || u.name).join("、"))} 暂无可确认的组内人员。请在看板主机核对人员名单，用 <code>labmon user bind</code> 补关联。</p>` : "";
  const add = D.fleet && !D.fleet.error && canOps() ? `<button class="fc add" data-fop="padd"><span class="plus">+</span><b>添加组内人员</b><span class="sm mut">登记服务器分配所需资料</span></button>` : "";
  const def = `<div class="pdef sm"><span class="mut">新的人默认</span><span ${tip(d.image || "")}>镜像 <b>${esc(imgTag(d.image) || "—")}</b></span><span>启动命令 <code>${esc(d.run_cmd || "—")}</code></span>
      ${canOps() ? personSettingsPop("pdef", "", "默认", "修改默认设置", "btn mini-btn") : ""}</div>`;
  return `<div class="card"><div class="chd wrap"><b>人员</b><span class="sm mut">换人、开新的时按这人的设置提交。名字也是平台作业名，同名作业在跑时自动加后缀（如 ALICE-2）</span></div>
    ${D.fleet && !D.fleet.error ? def : ""}${feedback}${repair}<div class="fwall people-grid">${current.map(n => personCard(n, ppl[n], hs[n], directoryAccount(n, L))).join("")}${add}</div>
    ${gone.length ? `<div class="pmore sm"><span class="mut">近 30 天还用过</span>${gone.map(n => `<a class="slc" href="${esc(DH.person(n))}" data-drw="${esc(DH.person(n))}" ${tip(`用过 ${hs[n].servers.join("、")}，${hs[n].hours} 小时有数据`)}>${esc(n)}</a>`).join("")}</div>` : ""}</div>`;
}
// 人员统计里的状态：服务器的五种，加上主人那边借给别人的时段“借出”（斜纹，算进时间占比，不算进利用率）
const PKS = ["job", "unknown", "keepalive", "idle", "down", "lent"], PSN = {...FSN, lent: "借出"};
const PSC = {...FSC, lent: "color-mix(in srgb, var(--accent) 40%, var(--muted))"};
// 借来、借出的明细（悬停用）：借 DANA 的 15151 58 小时 / 借给 ALICE 58 小时（15151）
const portOf = id => { const x = srvById(id); return x ? (x.mine ? x.id : x.port) : id; };
const lendLines = h => [...(h.borrowed || []).map(b => `借 ${b.from} 的 ${portOf(b.id)} ${b.h} 小时`), ...(h.lent || []).map(b => `借给 ${b.to} ${b.h} 小时（${portOf(b.id)}）`)];
// 人员卡的近 30 天占比条与实验占比
function useBar(h) {
  const f = h.frac, ks = PKS.filter(k => k in f);
  return `<div class="usage-track" ${tip(`近 30 天（有数据 ${h.hours} 小时）：\n` + ks.filter(k => f[k]).map(k => `${PSN[k]} ${h.h[k]} 小时（${fpct(f[k])}）`).join("\n") + `\n可共享 ${h.share_h} 小时` + lendLines(h).map(l => "\n" + l).join(""))}>
    ${fbar(ks.map(k => [f[k], PSC[k], `${PSN[k]} ${h.h[k]} 小时（${fpct(f[k])}）`, k === "lent" ? "hatch" : ""]), 1)}</div>
    <div class="sm mut usage-caption">实验 ${fpct(f.job ?? 0)}</div>`;
}
const slotChip = id => { const x = srvById(id), h = DH.srv(id);
  return x ? `<a class="slc" href="${esc(h)}" data-drw="${esc(h)}" ${tip(`${fName(x)}：${x.off ? "已关机" : x.state ? FSN[x.state] : "还没探到"}\n${x.pool || ""}`)}>
    <span class="cdot" style="background:${fDot(x)}"></span><span class="server-caption">${esc(x.mine ? x.id : x.port)} · ${fKind(x)}</span></a>` : `<span class="slc"><span class="server-caption">${esc(id)}</span></span>`; };
// 这人正借着的服务器：虚线框小签「借 DANA · 15151」，点开那台的抽屉
const borrowChips = n => (D.fleet?.servers || []).filter(x => borrowOf(x) && x.borrow.by === n).map(x => { const h = DH.srv(x.id);
  return `<a class="slc lent" href="${esc(h)}" data-drw="${esc(h)}" ${tip(`${fName(x)}：${n} 借来用${x.borrow.until ? `，约定 ${ts(x.borrow.until)} 还` : ""}`)}>
    <span class="cdot" style="background:${fDot(x)}"></span><span class="server-caption">借 ${esc(x.holder)} · ${esc(x.port)}</span></a>`; });
function personCard(n, p, h, u) {
  const d = route().drw, name = n || u.name;
  const isMe = u ? u.name === ME?.name : n === ME?.fleet_person;
  const href = DH.person(n);
  const selected = d?.k === "person" && d.id === n;
  const account = accountCardStatus(u, ACC.users?.roles, !!ACC.users);
  const slots = n ? [...(p?.slots || []).map(slotChip), ...borrowChips(n)] : [];
  const servers = '<span class="pk sm">服务器</span>' + (slots[0] || '<span class="sm mut">暂无</span>');
  const more = slots.length > 1 ? `<span class="sm mut nw">+${slots.length - 1} 台</span>` : "";
  const usage = h?.n ? useBar(h) : '<span class="sm mut usage-caption">暂无用量记录</span>';
  return `<div class="fc pc people-card${selected ? " sel" : ""}" data-drw="${esc(href)}" tabindex="0" role="button" aria-label="${esc(name)} 的详情">
    <div class="fc-hd">${pfav(name)}
      <div class="fc-t"><span class="fc-n"><b ${tip(u && n && u.name !== n ? `${name} · 账号 ${u.name}` : name)}>${esc(name)}</b>${isMe ? '<span class="chip">我</span>' : ""}</span></div>${icon("chev")}</div>${account}
    <div class="slcs">${servers}${more}</div><div class="people-overview">${usage}${n ? diskLine(n) : ""}</div></div>`;
}
let PER = {};               // 人员抽屉的近 30 天明细（打开时取，2 分钟内不重取）
async function loadPer(n) {
  PER[n] = {...(PER[n] || {}), busy: true};
  try {
    const r = await api(`fleet/person/${encodeURIComponent(n)}.json`, {cache: "no-store"}, {retry: true});
    if (!r.ok) throw new Error("HTTP " + r.status);
    PER[n] = {d: await r.json(), at: Date.now()};
  } catch (e) { PER[n] = {d: PER[n]?.d, at: Date.now(), err: e.message || String(e)}; }
  render(false);
}
// 近 30 天逐日：每天一根柱，按状态叠起来（一人多台时一天可超过 24 小时）；下面是按服务器的统计
function perHist(pd) {
  if (!pd?.d) return `<div class="empty">${pd?.err ? "取不到：" + esc(pd.err) : "读取中…"}</div>`;
  const d = pd.d, ks = PKS, tot = v => ks.reduce((s, k) => s + (v.h[k] || 0), 0), mx = Math.max(24, ...d.days.map(tot));
  const cols = d.days.map(v => `<i ${tip(`${tsMD(v.t)}${tot(v) ? "\n" + ks.filter(k => v.h[k]).map(k => `${PSN[k]} ${v.h[k]} 小时`).join("\n") + (v.util_job != null ? `\n实验时利用率 ${Math.round(v.util_job)}%` : "") : "：没有数据"}`)}>${ks.filter(k => v.h[k]).map(k => `<b style="height:${100 * v.h[k] / mx}%;background:${PSC[k]}"></b>`).join("")}</i>`).join("");
  const ax = d.days.map((v, k) => k % 7 === 0 ? `<span style="left:${100 * k / d.days.length}%">${tsMD(v.t)}</span>` : "").join("");
  const rows = d.servers.map(v => { const x = srvById(v.id), cur = x && (v.from ? borrowOf(x) && x.borrow.by === d.name : x.holder === d.name);
    const lend = v.from ? `借 ${esc(v.from)} 的` : v.lent ? `<span ${tip((v.lent_to || []).map(b => `借给 ${b.to} ${b.h} 小时`).join("\n"))}>借出 ${v.lent} 小时</span>` : "";
    return `<tr${x ? ` class="link" tabindex="0" data-drw="${esc(DH.srv(v.id))}"` : ""}><td class="nw"><b>${esc(v.id)}</b>${cur ? "" : ` <span class="sm mut">${!x ? "已不在清单里" : v.from ? "已还" : "已换人"}</span>`}</td>
      <td class="sm nw">${tsMD(v.first)} – ${tsMD(v.last)}</td><td class="sm nw">${lend}</td><td class="r">${v.hours} 小时</td><td class="r">${fpct(v.frac?.job)}</td><td class="r">${fpct(v.frac?.keepalive)}</td><td class="r">${v.util_job != null ? Math.round(v.util_job) + "%" : "—"}</td></tr>`; }).join("");
  const lg = d.total?.frac?.lent ? `<span><span class="dot hatch" style="background:${PSC.lent}"></span>借出</span>` : "";
  return `<div class="sm" style="margin-bottom:8px">${fSum(d.total)}${(d.total?.n && lendLines(d.total).length) ? ` · ${esc(lendLines(d.total).join("、"))}` : ""}</div><div class="dbars" style="--mx:${mx}">${cols}</div><div class="hax">${ax}</div>${fLegend(lg)}
    ${rows ? `<div class="scroll" style="margin-top:8px"><table><tr><th>服务器</th><th>用的日子</th><th>借来 / 借出</th><th class="r">有数据</th><th class="r">实验</th><th class="r">只有保活</th><th class="r">实验时利用率</th></tr>${rows}</table></div>` : ""}`;
}
function directoryDrawer(d) {
  const n = d.id, L = directoryUsers(), people = directoryPeople(L);
  if (n === "+") return D.fleet && !D.fleet.error && canOps() ? newPersonDrawer() : "";
  const u = people.has(n) ? directoryAccount(n, L) : null;
  const usage = !u || d.section !== "account";
  const p = fops().people?.[n] || (L?.people?.includes(n) ? {slots: []} : null);
  const info = u ? `看板账号 ${u.name}` : "";       // 账号的事在标题下单独一行写明是看板账号（#15）
  const status = u
    ? `<div class="directory-status sm"><span class="mut">${esc([info, L.roles[u.role] || u.role].filter(Boolean).join(" · "))}</span>${accStatus(u)}</div>`
    : "";
  const paths = [DH.person(n) + "/account", DH.person(n)];
  const tabs = u && n ? `<nav class="ftabs drawer-tabs" aria-label="详情分区">${["账号与权限", "服务器与用量"].map((label, i) =>
    `<a class="ftab${i === Number(Boolean(usage)) ? " on" : ""}" href="${esc(paths[i])}"${i === Number(Boolean(usage)) ? ' aria-current="page"' : ""}>${label}</a>`).join("")}</nav>` : "";
  if (d.section === "account" && !u && !L && ACC.load.users) {
    return dHead(pfav(n), esc(n), "") + '<div class="db"><div class="empty">读取账号中…</div></div>';
  }
  const error = ME?.scopes.includes("admin") && ACC.loadError.users;
  const linkError = accErr(error);
  const right = dNav(peopleOrder(), n);
  const isMe = u ? u.name === ME?.name : n === ME?.fleet_person;
  // 开通后账号刷新可能已经合卡，设置码仍留在发起的人员抽屉。
  const setup = n && ACC.show?.subject === n ? accShow("code", "onboard") : "";
  const openAccount = setup || (L?.person_choices?.includes(n) && !u
    ? `<div class="row wrap"><span class="sm mut">未开通</span>${onboardPop(n)}</div>` : "");
  const onboard = openAccount ? dSec("看板账号", openAccount) : "";
  return dHead(pfav(n), esc(n), "", right,
    isMe ? '<span class="chip">我</span>' : "", status) + tabs + `<div class="db">${linkError}${!usage && u.fleet_person_error ? `<p class="rn-err">${esc(u.fleet_person_error)}</p>` : ""}${onboard}${usage ? (D.fleet && !D.fleet.error ? personSections(n, p, !!u) : dSec("服务器与用量", '<p class="sm mut">暂未读到组内服务器数据。</p>')) : accountSections(u, L)}</div>`;
}
function personSections(n, p, hasAccount) {
  const pd = PER[n];
  if (LIVE && (!pd || (!pd.busy && Date.now() - pd.at > 120000))) setTimeout(() => loadPer(n));
  let s = fErr();
  const rows = (p?.slots || []).map(id => { const x = srvById(id), hh = DH.srv(id);
    if (!x) return `<div class="drow"><b>${esc(id)}</b></div>`;
    const U = fUtil(x), a = x.sample || {}, vr = [a.vram_real, a.vram_total];
    return `<div class="drow" data-drw="${esc(hh)}" tabindex="0" role="button"><span class="cdot" style="background:${fDot(x)}"></span><b>${esc(x.id)}</b>
      <span class="sm mut">${fKind(x)}</span>${fBusy(x) || fBadge(x)}<span class="sp"></span>
      <span class="sm num">${x.off ? "" : `GPU ${U.big} · ${vr[1] ? `${mb2g(vr[0])} / ${mb2g(vr[1])} GiB` : "—"}`}</span>${icon("chev")}</div>`; });
  const borrowed = borrowChips(n);
  const give = p && canOps() ? `<span class="sp"></span>${givePop(n, "drawer")}` : "";
  s += dSec("服务器", (rows.length ? `<div class="drows">${rows.join("")}</div>` : "")
    + (borrowed.length ? `<div class="slcs">${borrowed.join("")}</div>` : "")
    + (!rows.length && !borrowed.length ? '<div class="empty">现在没有服务器。</div>' : ""), give);
  s += dSec("近 30 天", perHist(pd));
  if (diskOf(n) || p) s += dSec("共享盘", diskOwner(n, true), diskOf(n) ? `<span class="sm mut">${esc(diskOf(n).dirs.map(d => d + "/").join("、"))}</span>` : "", "pdisk");
  s += dSec("事件", recSlice({who: n}, 15, `还没有和 ${n} 有关的记录。`));
  if (p) s += dSec("设置", kvHtml([
      ["镜像", p.image ? `<code>${esc(p.image)}</code> <span class="sm mut">${SRCN[p.src] || ""}</span>` : `<span style="color:var(--warn)">还没读到现有作业的镜像：「与平台核对」后补上</span>`],
      ["启动命令", p.run_cmd ? `<code>${esc(p.run_cmd)}</code>` : "—"], ["备注", esc(p.note || "—")]])
    + (canOps() ? `<div class="rn">${personSettingsPop("pedit", n, "drawer", "修改人员设置")}${p.slots.length || hasAccount ? "" : personSettingsPop("pdel", n, "drawer", "删除组内人员")}</div>` : ""),
    `<span class="sm mut">换人、开新的时按这里提交</span>`, "pset");
  return s;
}
function newPersonDrawer() {
  if (!FPED?.isNew) { const d = fops().defaults || {}; FPED = {name: "", image: d.image || "", run_cmd: d.run_cmd || "", note: "", isNew: true}; }
  return dHead(`<span class="fav">+</span>`, "添加组内人员", "用于分配服务器，镜像与启动命令按默认预填") + `<div class="db">${dSec("设置", personForm(), `<span class="sm mut">换人、开新的时按这里提交</span>`, "pset")}</div>`;
}
// 添加和修改人员共用字段；独立添加抽屉与按钮旁浮窗各自管理草稿。
function personFields(draft, fresh, defaults = false) {
  const inp = (id, k, ph) => `<input id="${id}" class="cap-in wide" value="${esc(draft[k])}" placeholder="${ph}" autocomplete="off" spellcheck="false">`;
  return `<div class="pform">${fresh ? `<label for="fpname">名字</label>${inp("fpname", "name", "也是平台作业名：字母、数字、_ ~ -")}` : ""}
    <label for="fpimg">镜像</label>${inp("fpimg", "image", "写全：仓库地址/项目/名字:标签")}
    <label for="fpcmd">启动命令</label>${inp("fpcmd", "run_cmd", "比如 /etc/init.d/ssh restart && sleep infinity")}
    ${defaults ? "" : `<label for="fpnote">备注</label>${inp("fpnote", "note", "可不填")}`}</div>`;
}
function personForm() {
  return personFields(FPED, true) + `<div class="rn">${fopBtn("psave", "保存", "pri")}${fopBtn("pcancel", "取消")}</div>
    ${FPED.err ? `<div class="rn-err">${esc(FPED.err)}</div>` : ""}`;
}
const personTrigger = (act, name, place) => `${act}-${place}-${name}`;
function personSettingsPop(act, n, place, label, cls = act === "pdel" ? "btn danger" : "btn") {
  const open = popIs(act, n) && POP.place === place, remove = act === "pdel";
  const btn = `<button class="${cls}" id="${esc(personTrigger(act, n, place))}"
    data-fop="${act}" data-name="${esc(n)}" data-place="${place}" aria-haspopup="dialog" aria-expanded="${open}">${esc(label)}</button>`;
  if (!open) return popConfirm(btn, false);
  const body = (remove ? `<p>确定删除组内人员 ${esc(n)}？</p>` : personFields(POP.draft, false, act === "pdef"))
    + (POP.err ? `<div class="rn-err">${esc(POP.err)}</div>` : "");
  const go = `<button class="btn pri${remove ? " dz" : ""}" data-fop="person-pop-save" ${POP.busy ? "disabled" : ""}>${remove ? "删除组内人员" : "保存"}</button>`;
  return popConfirm(btn, true, body, go, !remove);
}
function givePop(n, place) {
  const open = popIs("pgive", n) && POP.place === place;
  const btn = `<button class="btn" id="${esc(personTrigger("pgive", n, place))}"
    data-fop="pgive" data-name="${esc(n)}" data-place="${place}" aria-haspopup="dialog" aria-expanded="${open}"
    ${fops().op?.active ? "disabled" : ""}>${icon("server")}分配一台</button>`;
  return popConfirm(btn, open, open ? givePicker(n) : "", "", true);
}
// 分配一台：先挑一台（在用的换人、关着的开机、池里还有空就开新的），点了打开那台的抽屉，向导里人已选好
function givePicker(n) {
  const F = D.fleet, q = F.quota || {}, dis = fops().op?.active ? "disabled" : "";
  const b = (attrs, av, t, sub, dot) => `<button class="pchip" data-fop="wiz" data-name="${esc(n)}" ${attrs} ${dis}><span class="fav sm">${av}${dot ? `<span class="cdot" style="background:${dot}"></span>` : ""}</span><span class="pc-t"><b>${t}</b><span>${sub}</span></span></button>`;
  const rows = fPools().map(([p, priv]) => {
    const bs = poolMembers(p).map(x => x.off ? b(`data-kind="open" data-id="${esc(x.id)}"`, "关", "开机", `${esc(x.port)} · 已关机`)
      : x.holder === n || x.state === "down" ? "" : b(`data-kind="swap" data-id="${esc(x.id)}"`, esc(pabbr(x.holder)), esc(x.holder), `${esc(x.mine ? "自用 " + x.id : x.port)} · ${x.state ? FSN[x.state] : "还没探到"}`, fDot(x)));
    const k = priv ? q.private : q.public, max = priv ? q.private_max : q.public_max;
    if (p && fops().pools?.[p]?.ref && (max == null || k < max)) bs.push(b(`data-kind="open" data-pool="${esc(p)}"`, "+", "开新的", "端口自动挑"));
    return bs.join("") ? `<div class="pg-row"><span class="k" ${tip(p || "没写资源池")}>${priv ? "专用池" : "公用池"}</span><div class="pchips">${bs.join("")}</div></div>` : "";
  }).join("");
  return `<div class="wz-q">把哪一台分配给 ${esc(n)}？<span class="sm mut">点一台打开它的向导，${esc(n)} 已经选好；在跑实验的在向导里还要再确认</span></div>
    <div class="wz-pp">${rows || `<div class="sm mut">没有能分配的服务器。</div>`}</div>`;
}
// 服务器卡标题行上的一段：上次与平台核对的时间与结果、平台登录状态（可退出）
function syncLine() {
  const o = fops(), pf = o.platform, ss = o.session || {}, bad = (pf?.issues || []).length;
  return `<span class="sm mut">${pf ? `<span ${tip(o.pools_t ? `资源池与镜像 ${ts(o.pools_t)} 读的` : "")}>${ts(pf.t)} 核对</span>${bad ? ` · <span style="color:var(--bad)">${bad} 处对不上</span>` : " · 都对得上"}` : "还没与平台核对过"}${canOps()
    ? ` · ${ss.logged_in ? `已登录平台 <button class="lnk" data-fop="logout" ${tip("忘掉看板手里的平台令牌（钥匙串里存的也删掉）")}>退出</button>` : "没登录平台"}` : ""}</span>`;
}
