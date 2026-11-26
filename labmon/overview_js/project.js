/* ---------- 项目的「管理」抽屉（#/p/<id>/manage，#71 单元 1）：主机与队列（全局快照的 regs）、调度、项目配置（这三节只读）、归档（#97） ---------- */
const nrm = s => String(s ?? "").replace(/\/+$/, "");          // 目录比较时去掉末尾的 /
const sameQ = (a, root, jobs) => nrm(a.root) === nrm(root) && a.queue === jobs;
// 登记的停法（worker add --stop）：缺省 boundary（等当前任务跑完）不写
const stopText = s => !s || s === "boundary" ? "" : s === "term" ? "暂停时发 SIGTERM" : s.startsWith("file:") ? `暂停时写停止文件 ${s.slice(5)}` : s;
// 一条登记此刻的进度（项目抽屉的行、主机页「队列」、接上卡共用）：ln 对上的车道（项目快照里 主机 + root + 清单），run 在跑几个，left 还有几个没跑（含在跑的），idle 空着；说不出的为 null。
// 在跑与没跑优先取车道；对不上车道、或车道这轮没读到（run、unfinished 为空值）时在用主机取 worker 状态：running 与 todo（这条清单还会跑的，在跑的在内；暂停、drain 的推演里没有，todo 里照样有），
// todo 里没有的只有标了 empty 才算空着，否则说不出；已归还的对不上车道也说不出
function regLeft(x, q, p) {
  const ln = (p?.lanes || []).find(l => l.host === x.host && sameQ(l, q.root, q.jobs));
  const h = x.state === "active" && D.hosts.find(e => e.id === x.host), st = h?.status;
  const td = st && (st.todo || []).find(l => l.root != null && sameQ(l, q.root, q.jobs));
  const run = ln && ln.run != null ? ln.run : st ? (st.running || []).filter(j => sameQ(j, q.root, q.jobs)).length : null;
  const left = ln && ln.unfinished != null ? ln.unfinished : td ? (td.jobs || []).length : (q.empty || q.sleeping) ? 0 : null;
  return {ln, run, left, sleeping: !!q.sleeping, idle: !run && (left === 0 || !!q.empty || !!q.sleeping)};
}
// 状态一格：运行中 N 个（还有 M 个没跑）、还有 N 个没跑、已休眠、空着、—
function regState(L) {
  const {run, left, idle, sleeping} = L;
  return run ? `<span class="badge b-ok">运行中 ${run} 个</span>${left > run ? ` <span class="sm">还有 ${left - run} 个没跑</span>` : ""}`
    : left ? `<span class="badge b-warn">还有 ${left} 个没跑</span>`
    : sleeping ? `<span class="badge b-mute" ${tip("空满 2 天已自动休眠：不占卡也不读状态目录，往清单里加任务自动唤醒")}>已休眠</span>`
    : idle ? `<span class="badge b-mute" ${tip("清单里没有待办：不占卡，往清单里加任务就接着跑")}>空着</span>`
    : `<span class="sm mut" ${tip("这条清单的进度没读到")}>—</span>`;
}
const regGpus = q => q.gpus == null || q.gpus === "" ? "" : [].concat(q.gpus).join(",");
// 一条登记的一行（项目抽屉「展开」里，只读）：车道名、清单、状态，停法、限定卡、登记时刻
function regRow(x, q, p) {
  const L = regLeft(x, q, p), so = stopText(q.stop), gp = regGpus(q);
  const name = L.ln ? `<b>${esc(L.ln.label)}</b>` : `<span class="mut" ${tip("项目页的车道按登记生成：刚登记或刚换了主机，项目看板下一轮采集后出现在这台下面")}>下一轮采集后显示</span>`;
  const ex = [so ? esc(so) : "", gp ? `限定卡 ${esc(gp)}` : "", q.added ? `${esc(q.added)} 登记` : ""].filter(Boolean);
  return `<div class="rg-row"><span class="ln">${name}</span><span class="rq"><code>${esc(nrm(q.root))}/<b>${esc(q.jobs)}</b></code></span><span class="rs">${regState(L)}</span>`
    + (ex.length ? `<span class="sm mut rx">${ex.join(" · ")}</span>` : "") + `</div>`;
}
// 一台主机上这个项目的清单按状态数（每条按 regLeft 归一类）：在跑、在等（还有没跑的）、跑完（空着）、说不出
function regTally(x, qs, p) {
  const n = {run: 0, wait: 0, done: 0, unk: 0};
  for (const q of qs) { const L = regLeft(x, q, p); n[L.run ? "run" : L.left ? "wait" : L.idle ? "done" : "unk"]++; }
  return n;
}
// 项目「管理」抽屉每台主机一行的条与数：按在跑、在等、跑完、没读到分色（颜色与上面的图例同一份 REG_ST）
const REG_ST = [["run", "在跑", "var(--ok)"], ["wait", "在等", "var(--warn)"], ["done", "跑完", "var(--faint)"], ["unk", "没读到进度", "var(--line)"]];
const tallyBar = (n, all) => `<div class="bar thin rg-bar">${REG_ST.map(([k, , c]) => n[k] ? `<i style="width:${100 * n[k] / all}%;background:${c}"></i>` : "").join("")}</div>`;
const tallyHtml = (n, all) => `<span class="rg-n">${all} 条清单：${REG_ST.filter(([k]) => n[k]).map(([k, t, c]) => k === "unk" ? `<span class="mut" ${tip("这几条的进度没读到")}>${n[k]} 条${t}</span>`
  : `<span style="color:${k === "done" ? "var(--muted)" : c}">${n[k]} ${t}</span>`).join(" · ")}</span>`;
// 项目页收起的车道（只在登记里有的、跑完久了没动，collect._quiet；项目快照的 quiet）：抽屉的逐条清单不列
const regQuiet = (p, x, q) => (p?.quiet || []).some(l => l.host === x.host && sameQ(l, q.root, q.jobs));
/* 按主机换、不用一台（#95 起，#121 改成按主机，#133 改名）：「换到其他主机」= 这个项目在这台的清单全在目标主机上登记（worker add 把原来那份删掉），
   「不用这台」= 这些登记逐条 worker rm（已不在用的主机经在用主机代办）；接上卡一次搬勾上的几条（adoptCard）。都经 POST /reg/…，做完立即采集；
   服务端按项目给实验会话一条消息（records.tell）。页面上的悬停与确认不写“登记”“整台”这类内部词 */
let RMSG = null;                                          // 上一次换、不用的结果（一分钟后收起）
const regId = (x, q) => `${x.host}\n${nrm(q.root)}\n${q.jobs}`;
const regHost = id => { const h = D.hosts.find(e => e.id === id); return h ? hostShort(h) : (D.regs || []).find(x => x.host === id)?.label || id; };
const regItem = (x, q) => ({host: x.host, root: q.root, jobs: q.jobs, project: q.project, stop: q.stop || null});
const regMine = (hid, pid) => { const x = (D.regs || []).find(y => y.host === hid); return x ? {x, qs: (x.queues || []).filter(q => q.project === pid)} : null; };
// 一台主机此刻空着几张卡（没有任务占着的 GPU，含旧 gpu_queue.sh 的任务）：「换到」里给人挑主机用，不进准入
const freeCards = h => { const gs = h?.status?.info?.gpus || [], busy = new Set(jobsOf(h).flatMap(j => j.gpus || [])); return gs.length ? `空 ${gs.filter(g => !busy.has(g.uuid)).length}/${gs.length} 张卡` : ""; };
// 能换去的主机：worker 目录相同（同一块共享盘，换过去时才删得掉原来那份）的别的在用主机；借来的只给别的项目用的置灰（worker 也会拒），drain、worker 没在跑的写在后面；
// info 是给人挑的：空着几张卡、租期
function regTo(x, pid) {
  return (D.regs || []).filter(y => y.state === "active" && y.host !== x.host && canUseHost(y.host) && nrm(y.home) === nrm(x.home)
      && !D.hosts.find(e => e.id === y.host)?.borrow?.pool).map(y => {        // 算力池借来的不列：放什么由看板定（#25）
    const h = D.hosts.find(e => e.id === y.host), b = h?.borrow, ov = h?.status?.overrides || [];
    const no = b?.for?.length && !b.for.includes(pid) ? `借来的，只给 ${b.for.join("、")} 用` : "";
    const note = no || [ov.some(o => o.kind === "drain") ? "drain 中，不放行" : "", !h?.worker_up ? "worker 没在跑，拉起后放行" : ""].filter(Boolean).join("，");
    return {id: y.host, name: h ? hostShort(h) : y.label || y.host, no: !!no, note, info: [freeCards(h), h ? leaseText(h) : ""].filter(Boolean).join(" · ")};
  });
}
// 一台主机那一行的两个按钮（项目抽屉「主机与队列」）：「换到其他主机」（只有一台能去时直接写它的名字；一台都不能去时置灰、悬停说原因）、「不用这台」（这项目在这台有任务在跑时置灰，服务端再查一次）
function regHostBtns(x, qs, p) {
  if (!canWrite(p.id)) return "";
  const id = `${x.host}\n${p.id}`, to = regTo(x, p.id), ok = to.filter(t => !t.no), from = esc(regHost(x.host)), who = esc(pname(p.id)), n = qs.length;
  const run = qs.reduce((a, q) => a + (regLeft(x, q, p).run || 0), 0), gp = qs.filter(q => regGpus(q)).length;
  const da = `data-h="${esc(x.host)}" data-p="${esc(p.id)}"`;
  const b = (act, text, hint, dis) => `<button class="btn mini-btn" data-reg="${act}" ${da} ${dis ? "disabled" : ""} ${tip(hint)}>${text}</button>`;
  const go = (act, text) => `<button class="btn pri" data-reg="${act}" ${da} ${POP?.busy ? "disabled" : ""}>${POP?.busy ? "正在做…" : text}</button>`;
  const err = () => POP?.err ? `<div class="rn-err">${esc(POP.err)}</div>` : "";
  const open = popIs("rmv", id), tgt = open && to.find(t => t.id === POP.to), tn = tgt ? `<b>${esc(tgt.name)}</b>` : "选好的那台";
  const why = !to.length ? "没有能换去的在用主机：要有使用权限，并与这台共用 worker 目录（同一块共享盘）" : `能换去的主机都不收它：${to.map(t => `${t.name}（${t.note}）`).join("、")}`;
  const mvBtn = ok.length ? b("mv", ok.length === 1 ? `换到 ${esc(ok[0].name)}` : "换到其他主机", `把这项目在这台的 ${n} 条清单改到别的在用主机上跑`, false) : b("mv", "换到其他主机", why, true);
  const pick = to.length > 1 ? `<div class="rg-tos">${to.map(t => `<label class="rg-to${t.no ? " mut" : ""}"><input type="radio" name="regto" data-regto="${esc(t.id)}"${POP?.to === t.id ? " checked" : ""}${t.no ? " disabled" : ""}>`
    + `<b>${esc(t.name)}</b><span class="sm mut">${esc([t.info, t.note].filter(Boolean).join(" · "))}</span></label>`).join("")}</div>` : tgt?.info || tgt?.note ? `<div class="sm mut">${esc([tgt.info, tgt.note].filter(Boolean).join(" · "))}</div>` : "";
  const mv = popConfirm(mvBtn, open, !open ? "" :
    `<div class="rn-note">把 <b>${who}</b> 在 <b>${from}</b> 上的 ${n} 条清单改到${to.length > 1 ? "哪台跑：" : ` ${tn} 跑`}</div>${pick}
    <div class="sm mut">${[run ? `${from} 上正在跑的 ${run} 个跑完再走，没跑的到 ${tn} 接着跑` : `没跑的到 ${tn} 接着跑`, gp ? `${gp} 条原来限定了 ${from} 的卡号，换过去不限` : "",
      "暂停时怎么停跟着走；实验会话会收到一条消息"].filter(Boolean).join("。")}。</div>${err()}`, go("mvgo", "换过去"), true);
  const pool = x.state === "pool", rmText = pool ? "从算力池撤回" : "不用这台";
  const rm = !canUseHost(x.host) ? "" : popConfirm(b("rm", rmText, run ? `有 ${run} 个任务在跑：先暂停或等跑完` : `${from} 不再跑这项目的 ${n} 条清单（清单文件、结果和日志都不删）`, !!run), popIs("rrm", id), !popIs("rrm", id) ? "" :
    `<div class="rn-note">${pool ? `把 <b>${who}</b> 交给算力池的 ${n} 条清单撤回：不再放到别的服务器上跑` : `<b>${from}</b> 不再跑 <b>${who}</b> 的 ${n} 条清单：worker 不再从这几条清单里取任务${x.state === "active" ? "" : "（经在用主机代办）"}`}。</div>
    <div class="sm mut">清单文件、结果和日志都不删；要再用，在实验会话里 <code>labmon worker add</code>。有任务在跑时会拒绝。实验会话会收到一条消息。</div>${err()}`, go("rmgo", rmText));
  return `<div class="rg-b">${mv}${rm}</div>`;
}
async function regPost(act, body) {
  const r = await postJSON(`reg/${act}`, body);
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || "HTTP " + r.status);
  RMSG = j.msg; setTimeout(() => { RMSG = null; render(false); }, 60000);
}
async function regAct(act, d) {
  if (act === "adopt") return adoptGo(d.h);
  const m = regMine(d.h, d.p);
  if (!m?.qs.length || !canWrite(d.p)) return;
  if ((act === "rm" || act === "rmgo") && !canUseHost(d.h)) return;
  const {x, qs} = m, id = `${x.host}\n${d.p}`, P = POP;
  if (act === "mv") popToggle("rmv", id, {to: regTo(x, d.p).find(t => !t.no)?.id});
  else if (act === "rm") popToggle("rrm", id);
  else {
    if (!P) return;
    if (act === "mvgo" && !regTo(x, d.p).some(t => t.id === P.to && !t.no)) {
      P.err = "选一台现在能换去的主机"; return render(false);
    }
    P.busy = true; P.err = null; render(false);
    try {
      await regPost(act === "mvgo" ? "move" : "rm", act === "mvgo" ? {to: P.to, items: qs.map(q => regItem(x, q))}
        : {host: x.host, home: x.home, project: d.p, items: qs.map(q => ({root: q.root, jobs: q.jobs}))});
      POP = null;
      return collectNow(true);
    } catch (e) { if (POP) { POP.busy = false; POP.err = e.message || String(e); } }
  }
  render(false);
}
const regMsg = () => RMSG ? `<div class="rn-state">${esc(RMSG)}</div>` : "";
// 「接上原来的项目」（主机页，#71 问题 2）：这台在用、worker 状态里一条登记都没有，同一 worker 目录下有已归还（或主机清单里已没有）的主机留着登记时出现。
// 上一台（同一个池里最近归还的自用主机，regs 里这台的 prev，即给自己开回来时沿用端口的那台）排第一，别的折叠；按项目分组，有没跑完的默认勾上，
// 借来的主机只能勾“只给谁用”里的项目。勾选记在 ADOPT（换了主机就重来）
let ADOPT = null;                                         // {host, sel: {登记 id: 勾没勾}, busy, err}
function adoptSrc(h) {
  const me = (D.regs || []).find(x => x.host === h.id);
  if (!canWrite() || !me || me.state !== "active" || me.stale || !Array.isArray(me.queues) || me.queues.length || !h.status) return null;
  const src = D.regs.filter(x => (x.state === "returned" || x.state === "unlisted") && nrm(x.home) === nrm(me.home) && (x.queues || []).some(q => q.project));
  return src.length ? {me, src: [...src.filter(x => x.host === me.prev), ...src.filter(x => x.host !== me.prev)]} : null;
}
function adoptCard(h) {
  const A = adoptSrc(h);
  if (!A) return "";
  if (ADOPT?.host !== h.id) ADOPT = {host: h.id, sel: {}};
  const only = h.borrow?.for?.length ? h.borrow.for : null;
  const row = (x, q) => {
    const id = regId(x, q), L = regLeft(x, q, D.projects.find(p => p.id === q.project)), no = only && !only.includes(q.project);
    if (!(id in ADOPT.sel)) ADOPT.sel[id] = !no && L.left > 0;
    return `<label class="rg-row ad-row${no ? " mut" : ""}" ${no ? tip(`这台是借来的，只给 ${only.join("、")} 用`) : ""}><input type="checkbox" data-adopt="${esc(id)}"${ADOPT.sel[id] && !no ? " checked" : ""}${no ? " disabled" : ""}>
      <span class="rq"><code>${esc(nrm(q.root))}/<b>${esc(q.jobs)}</b></code></span><span class="rs">${regState(L)}</span></label>`;
  };
  const host = x => {
    const by = {};
    for (const q of x.queues || []) if (q.project) (by[q.project] ||= []).push(q);
    return Object.entries(by).map(([pid, qs]) => `<div class="ad-p"><div class="sm">${projName(pid, {always: true})}</div>${qs.map(q => row(x, q)).join("")}</div>`).join("");
  };
  const head = x => `<b>${esc(x.label || x.host)}</b>${x.returned ? `（${esc(ts(x.returned))} 归还）` : x.state === "unlisted" ? "（主机清单里已没有）" : ""}`;
  const [first, ...rest] = A.src, prev = first.host === A.me.prev;
  const top = host(first), more = rest.map(x => `<div class="sm ad-h">${head(x)}</div>${host(x)}`).join("");    // 先画行：没动过的勾选在这里取缺省
  const picked =A.src.flatMap(x => (x.queues || []).filter(q => q.project && ADOPT.sel[regId(x, q)] && !(only && !only.includes(q.project))));
  const nq = xs => xs.reduce((a, x) => a + (x.queues || []).filter(q => q.project).length, 0);
  return `<div class="card adopt"><div class="chd wrap"><b>接上原来的项目</b><span class="sm mut">这台还没有登记队列</span></div>
    <div class="sm">${prev ? `上一台自用主机 ${head(first)} 登记着：` : `已归还的主机 ${head(first)} 上登记着：`}</div>${top}
    ${rest.length ? `<details class="dfold"${rest.some(x => (x.queues || []).some(q => ADOPT.sel[regId(x, q)])) ? " open" : ""}><summary class="sm">别的已归还主机上还有 ${nq(rest)} 条</summary>${more}</details>` : ""}
    ${ADOPT.err ? `<div class="rn-err">${esc(ADOPT.err)}</div>` : ""}${regMsg()}
    <div class="rn"><button class="btn pri" data-reg="adopt" data-h="${esc(h.id)}" ${!picked.length || ADOPT.busy ? "disabled" : ""}>${ADOPT.busy ? "正在搬…" : `搬到这台${picked.length ? `（${picked.length} 条）` : ""}`}</button>
    <span class="sm mut">搬过来后原主机上的登记删掉；${h.worker_up ? "下一轮开始放行" : "worker 拉起后开始放行"}。本地依赖（needs=）在这台上由 worker 放行前重新准备；原来限定的卡号不带。</span></div></div>`;
}
// 平台操作“换给自己”“开机”做完的那句结果后面：这台还没有登记、上一台（regs 的 prev）留着登记时，提示到主机页接上
function adoptHint(hid) {
  const me = (D.regs || []).find(x => x.host === hid), pv = me?.prev && D.regs.find(x => x.host === me.prev);
  const n = (pv?.queues || []).filter(q => q.project).length;
  return me && Array.isArray(me.queues) && !me.queues.length && n ? `。上一台 ${esc(pv.label || pv.host)} 上登记着 ${n} 条队列：<a href="#/host/${encodeURIComponent(hid)}">到主机页接上</a>` : "";
}
async function adoptGo(hid) {
  const h = D.hosts.find(e => e.id === hid), A = h && adoptSrc(h), only = h?.borrow?.for?.length ? h.borrow.for : null;
  if (!A || ADOPT?.host !== hid) return;
  const items = A.src.flatMap(x => (x.queues || []).filter(q => q.project && ADOPT.sel[regId(x, q)] && !(only && !only.includes(q.project))).map(q => regItem(x, q)));
  if (!items.length) return;
  ADOPT.busy = true; ADOPT.err = null; render(false);
  try { await regPost("move", {to: hid, items}); ADOPT = null; return collectNow(true); }
  catch (e) { ADOPT.busy = false; ADOPT.err = e.message || String(e); }
  render(false);
}
// 「给项目用」（主机页，#173）：这台在用的自用主机（不是直接借来的组内服务器）还没有任何登记时，选一个自己有权操作的项目，
// 经 POST /host/assign → records.tell(project, "host", …) 通知该项目的实验会话去 labmon worker add，不改登记、不替会话挑清单。
// 同时看 h.status.queues 与 D.regs：普通用户的 D.regs 只含自己的项目，用 h.status.queues 避免把已有别人登记的主机误判为空。
let AMSG = null;                                          // 上一次通知的结果：{host, msg}（一分钟后收起）
function assignSrc(h) {
  if (!LIVE || !canUseHost(h.id) || (h.borrow && !h.borrow.swap) || !h.status) return null;
  if ((h.status.queues || []).length) return null;
  const me = (D.regs || []).find(x => x.host === h.id);
  if (me && (me.state !== "active" || me.stale || (me.queues || []).length)) return null;
  const ps = (D.projects || []).filter(p => canWrite(p.id));
  return ps.length ? {h, ps} : null;
}
function assignCard(h) {
  const A = assignSrc(h);
  if (!A) return "";
  const open = popIs("assign", h.id), P = open ? POP : null, ps = A.ps, hn = esc(hostShort(h));
  const btn = `<button class="btn mini-btn pri" id="assign-${esc(h.id)}" data-assign="open" data-h="${esc(h.id)}" aria-haspopup="dialog" aria-expanded="${open}" ${tip("通知选定项目的实验会话使用这台主机")}>给项目用</button>`;
  const tgt = open && ps.find(p => p.id === P.to);
  const pick = ps.length > 1 ? `<div class="rg-tos">${ps.map(p => `<label class="rg-to"><input type="radio" name="assignto" data-assignto="${esc(p.id)}"${P?.to === p.id ? " checked" : ""}>`
    + `<b>${esc(p.title || p.id)}</b>${p.title && p.title !== p.id ? `<span class="sm mut">${esc(p.id)}</span>` : ""}</label>`).join("")}</div>` : "";
  const body = !open ? "" : `<div class="rn-note">${ps.length > 1 ? `要把 <b>${hn}</b> 给哪个项目用：` : `通知 <b>${esc(tgt?.title || ps[0].id)}</b> 的实验会话使用 <b>${hn}</b>？`}</div>${pick}
    <div class="sm mut">发送一条消息给该项目的实验会话（下一轮 <code>labmon wait</code> 收到），由会话自行 <code>labmon worker add</code> 登记；看板不改动现有登记。</div>${P.err ? `<div class="rn-err">${esc(P.err)}</div>` : ""}`;
  const go = `<button class="btn pri" data-assign="go" data-h="${esc(h.id)}" ${P?.busy ? "disabled" : ""}>${P?.busy ? "正在通知…" : "通知实验会话"}</button>`;
  const msg = AMSG?.host === h.id ? `<div class="rn-state">${esc(AMSG.msg)}</div>` : "";
  return `<div class="card assign"><div class="chd wrap"><b>给项目用</b><span class="sm mut">这台自用主机还没有登记队列</span></div>
    <div class="row wrap" style="gap:8px;align-items:center"><span class="sm mut">选一个项目，通知它的实验会话来登记清单。</span>${popConfirm(btn, open, body, go, true)}</div>${msg}</div>`;
}
async function assignAct(act, d) {
  const h = D.hosts.find(e => e.id === d.h), A = h && assignSrc(h);
  if (!A) return;
  if (act === "open") {
    popToggle("assign", h.id, {to: A.ps[0].id, route: location.hash, trigger: "assign-" + h.id});
    return render(false);
  }
  if (act !== "go" || !popIs("assign", h.id) || POP.busy) return;
  const P = POP;
  if (!A.ps.some(p => p.id === P.to)) { P.err = "选一个有权操作的项目"; return render(false); }
  P.busy = true; P.err = null; render(false);
  try {
    const r = await postJSON("host/assign", {host: h.id, project: P.to});
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.error || "HTTP " + r.status);
    const cur = AMSG = {host: h.id, msg: j.msg || "已通知实验会话"};
    setTimeout(() => { if (AMSG === cur) { AMSG = null; render(false); } }, 60000);
    if (POP === P) { FFOCUS = P.trigger; popClose(); }
  } catch (e) { if (POP === P) { P.busy = false; P.err = e.message || String(e); } }
  render(false);
}
// 「主机与队列」：这个项目的清单登记在哪几台（在用的在前），每台一行（.rg：左边名字与按状态分色的条、右边「换到其他主机」「不用这台」，照 .strip 的两列网格排，按钮不随名字长短折行）；
// 逐条清单折在「展开 N 条」里只读，项目页收起的（跑完久了没动）不列。monitor.toml 列着、哪台都没登记的车道单列“只显示”；车道所在主机这轮登记没读到（regs 里没有这台，或 queues 为空值）的不算没登记
function projRegSec(p) {
  const R = D.regs, note = `<span class="sm mut">worker 照这些清单放行（labmon worker add / rm）</span>`;
  if (!R) return dSec("主机与队列", `<div class="empty">还没有登记数据：看板服务下一轮采集后再看。</div>`, note);
  const grp = R.map(x => ({x, qs: (x.queues || []).filter(q => q.project === p.id)})).filter(g => g.qs.length);
  const known = l => R.some(x => x.host === l.host && x.queues), reg = l => R.some(x => x.host === l.host && (x.queues || []).some(q => sameQ(l, q.root, q.jobs)));
  const only = (p.lanes || []).filter(l => known(l) && !reg(l)), unk = (p.lanes || []).filter(l => !known(l));
  const hn = id => { const h = D.hosts.find(e => e.id === id); return h ? hostShort(h) : R.find(x => x.host === id)?.label || id; };
  let s = grp.map(({x, qs}) => {
    const pool = x.state === "pool", off = x.state !== "active" && !pool, h = !off && D.hosts.find(e => e.id === x.host), n = regTally(x, qs, p);
    const name = h ? `<a href="#/host/${encodeURIComponent(x.host)}" ${tip(h.label)}>${esc(hostShort(h))}</a>` : `<span ${tip(x.host)}>${esc(x.label || x.host)}</span>`;
    const tags = [pool ? `<span class="badge b-acc" ${tip("交给算力池、还没放到哪台的清单：看板挑一台入池的组内服务器放过去")}>等着放到某台</span>` : x.state === "unlisted" ? `<span class="badge b-mute" ${tip("主机清单里已经没有这一台，它的清单记录还留在共享盘上")}>主机清单里没有</span>`
      : off ? `<span class="badge b-mute">已归还${x.returned ? " " + esc(ts(x.returned)) : ""}</span>` : "",
      x.stale ? `<span class="badge b-warn" ${tip(off ? "代读它的那台在用主机这一轮没连上，或本机不在内网" : "这台这一轮没读到状态")}>这轮没读到，用的是 ${x.t ? esc(ts(x.t)) : "上一轮"} 的</span>` : ""].filter(Boolean);
    const shown = qs.filter(q => !regQuiet(p, x, q)), hid = qs.length - shown.length;
    const sleepN = qs.filter(q => regQuiet(p, x, q) && (q.sleeping || (p?.quiet || []).some(l => l.sleeping && l.host === x.host && sameQ(l, q.root, q.jobs)))).length;   // 空满 2 天已自动休眠（#164）
    const hidNote = hid ? `<div class="sm mut rg-hid" ${tip("项目页已收起这几条车道：空清单、已休眠、已摘登记或已归还无未完成，或最后一次活动早于 hide_done_hours（缺省一天）。往清单里加任务自动唤醒、重新显示"
      + (sleepN ? `\n其中 ${sleepN} 条空满 2 天已自动休眠：worker 只检查清单文件是否变动，不读状态目录；不再用的可用 labmon worker rm 摘掉` : ""))}>另有 ${hid} 条已收起，不列${sleepN ? `（含 ${sleepN} 条已休眠）` : ""}</div>` : "";
    const body = shown.length ? `<details class="dfold rg-q"><summary class="sm">展开 ${shown.length} 条</summary>${shown.map(q => regRow(x, q, p)).join("")}${hidNote}</details>` : hidNote;
    return `<div class="rg${off ? " rg-off" : ""}"><div class="rg-i"><div class="rg-nm">${name} ${tags.join(" ")}</div><div class="rg-tl">${tallyBar(n, qs.length)}${tallyHtml(n, qs.length)}</div></div>`
      + `${regHostBtns(x, qs, p)}${body ? `<div class="rg-x">${body}</div>` : ""}</div>`;
  }).join("") || `<div class="empty">哪台主机上都没有登记这个项目的清单。</div>`;
  const legend = grp.length ? `<div class="rg-lg">${REG_ST.slice(0, 3).map(([, t, c]) => `<span><i style="background:${c}"></i>${t}</span>`).join("")}</div>` : "";
  const lrow = l => `<div class="rg-row"><span class="ln">${esc(l.label)}</span><span class="rq"><code>${esc(nrm(l.root))}/<b>${esc(l.queue)}</b></code></span><span class="sm mut">${esc(hn(l.host))}</span></div>`;
  if (only.length) s += `<details class="dfold rg-more"><summary class="sm" ${tip("项目页照 monitor.toml 的 [[queues]] 显示这几条车道，但哪台主机的 worker 都没收它们：不会放行")}>只显示（monitor.toml 列着、没登记）${only.length} 条</summary>${only.map(lrow).join("")}</details>`;
  if (unk.length) s += `<div class="sm mut" style="margin-top:6px" ${tip(unk.map(l => `${hn(l.host)} ${l.label}`).join("\n"))}>另有 ${unk.length} 条车道所在主机的登记读不到（已归还、又没有与它共用 worker 目录的在用主机）。</div>`;
  s += `<div class="sm mut" style="margin-top:8px">放哪条清单到哪台由实验会话定：要加一台主机，在会话里 <code>labmon worker add &lt;主机&gt; ROOT JOBS --project ${esc(p.id)}</code>。</div>`;
  return dSec("主机与队列", regMsg() + legend + s, note);
}
// 「调度」：这一个项目的已用 / 应得条（与调度卡同一个 shareBar）、权重、优先、暂停（按钮与调度卡同一套：schedW、schedFlag，POST /sched/…）；优先、暂停照调度卡写范围（所有主机，或只在哪几台），悬停写到期与撤销命令
const flagCell = (k, x, on) => x[k] ? `<span ${tip(flagTip(k, x))}>${on}${esc(flagText(k, x))}</span>` : `<span class="mut">没有</span>`;
function projSchedSec(p) {
  const x = D.sched?.projects?.find(o => o.id === p.id);
  if (!x) return dSec("调度", `<div class="empty">还没有调度数据（各主机的 worker 状态里没有份额）。</div>`, `<span class="sm mut">所有主机生效</span>`);
  const flag = (k, t, on) => `<span class="flag pop-l"><span class="k">${t}</span>${flagCell(k, x, on)}${canWrite(p.id) ? schedFlag(x, k) : ""}</span>`;
  return dSec("调度", (SMSG ? `<div class="rn-state">${esc(SMSG)}</div>` : "")
    + `<div>${shareBar(x)}<div class="sm mut" style="margin-top:4px">已用 / 应得（竖线）· 此刻占 ${wtxt(x.cards)} 张卡 · 排队 ${x.queued} 个</div></div>`
    + `<div class="flags">${schedW(x)}${flag("focus", "优先", "优先中")}${flag("paused", "暂停", `<span style="color:var(--warn)">暂停中</span>`)}</div>`,
    `<span class="sm mut">所有主机生效，同总览的调度卡</span>`);
}
/* 「项目配置」（#122，#133 改版）：控件一直显示，能改的那几项经 POST /project/config 写回看板上的 monitor.toml（#161 起配置以登记目录里那份为准，projects.set_config → config.set_project：
   只改相关行、按 load 校验、记配置改动、告诉实验会话；实验会话的 labmon wait 把它拉回项目目录）。
   项目 id、输出目录、主机与队列、计划项的归组规则只读。取值范围、单位、缺省都取 cfg 的 spec（config.cfg_spec），页面不另抄。
   CFE[项目 id]（无原型的对象：项目 id 叫 constructor、__proto__ 也只认自己的，Codex 审查 #136）：还没保存的草稿 {base 第一次输入时的配置（比较基准）, v 草稿, busy, err, field 服务端报错的那一项, show 展开了「看改动」}，第一次输入时才生成（cfgEdit），
   没有改动了就丢掉（cfgCur），页面按草稿画、定时刷新不冲掉；只交草稿相对 base 改过的项（cfgDiff）：表单开着时别处改了 monitor.toml，没动的项不会被改回去（Codex 审查 #128）。
   打字时焦点在输入框里，整页重画被「正在操作」挡着：cfgPatch 只把同一份 HTML 里标了 data-cfv 的展示块与行的 class 就地换上（里面没有输入框），焦点离开后照常重画 */
let CFE = Object.create(null), CFMSG = null;
const CF_PRESET = {interval: [30, 60, 120, 300, 600], refresh: [10, 30, 60, 120, 300], hide_done_hours: [6, 12, 24, 72, 168]};   // 常用档（秒；车道收起是小时）
const cfDur = (k, x) => dur(k === "hide_done_hours" ? x * 3600 : +x).replace(".0 ", " ");      // 档位与「原 X」的文字：6 小时、3 天
// 里程碑的日期框与时刻框的值：文件里的写法（不带年份的、写了秒的、原生日期）由 config.load 经 parse_time 解析成 ts，这里只把 ts 换成控件的格式
// （当天 23:59:59 的当作只写了日期，与 load 补的一致）；没有 ts 的照原文（Codex 审查 #136：不另写一套接受范围不同的解析）
const cfDate = m => { if (m.ts == null) return String(m.date ?? ""); const d = new Date(1000 * m.ts), day = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  return d.getHours() === 23 && d.getMinutes() === 59 && d.getSeconds() === 59 ? day : `${day} ${pad(d.getHours())}:${pad(d.getMinutes())}`; };
// 草稿里里程碑的时刻（epoch 秒）：草稿的日期只来自上面的 cfDate 与日期框、时刻框，形如 YYYY-MM-DD[ HH:MM]，只写日期的取当天结束；没选日期的为 null
const cfTs = s => { const m = /^(\d{4})-(\d\d)-(\d\d)(?: (\d\d):(\d\d))?$/.exec(s || ""); return m ? new Date(+m[1], m[2] - 1, +m[3], m[4] ? +m[4] : 23, m[4] ? +m[5] : 59, m[4] ? 0 : 59).getTime() / 1000 : null; };
const cfHex = c => /^#[0-9a-f]{6}$/i.test(c || "") ? c : "";
// 计划项：写明的（cfg.groups）在前，项目快照里自动归出的（p.groups 的 auto，名字去掉 *，带此刻的颜色）接在后面，一起能排顺序（#150）
const cfgDraft = (c, p) => ({title: c.title ?? "", interval: String(c.interval ?? ""), refresh: String(c.refresh ?? ""), hide_done_hours: String(c.hide_done_hours ?? ""),
  auto_groups: !!c.auto_groups, alerts: Object.fromEntries(Object.keys(c.alerts || {}).map(k => [k, String(c.alerts[k] ?? "")])),
  groups: [...(c.groups || []).map(g => ({id: g.id, label: g.label ?? g.id, color: g.color ?? ""})),
    ...(p?.groups || []).filter(g => g.auto && !(c.groups || []).some(x => x.id === g.id)).map(g => ({id: g.id, label: String(g.label ?? g.id).replace(/\*$/, ""), color: g.color ?? "", auto: true}))],
  markers: (c.markers || []).map(m => ({date: cfDate(m), label: m.label ?? ""}))});
// 要写进 monitor.toml 的计划项有几个：草稿末尾那段只有自动组、且还是原来先后的留给自动归组（保存后它们照旧排在写明的后面），前面的都写，
// 其中的自动组写成新的 [[groups]]（#150）；o 是 base 的计划项
function cfgKeep(o, v) {
  const at = Object.fromEntries(o.map((g, i) => [g.id, i]));
  let n = v.length;
  while (n > 0 && v[n - 1].auto && (n === v.length || at[v[n - 1].id] < at[v[n].id])) n--;
  return n;
}
// 草稿和 base 比，只交改了的项（计划项、里程碑整组交；计划项的颜色只交换了的；要写的自动组带 auto 与颜色，留给自动归组的不交：cfgKeep）
function cfgDiff(o, v) {
  const set = {}, al = {};
  for (const k of ["title", "interval", "refresh", "hide_done_hours", "auto_groups"]) if (String(v[k]).trim() !== String(o[k])) set[k] = typeof v[k] === "string" ? v[k].trim() : v[k];
  for (const k in v.alerts) if (String(v.alerts[k]).trim() !== o.alerts[k]) al[k] = v.alerts[k].trim();
  if (Object.keys(al).length) set.alerts = al;
  const gs = g => g.map(x => [x.id, x.label.trim(), x.color].join("\u0000")).join("\u0001"), oc = Object.fromEntries(o.groups.map(x => [x.id, x.color]));
  if (gs(v.groups) !== gs(o.groups)) set.groups = v.groups.slice(0, cfgKeep(o.groups, v.groups)).map(x => x.auto ? {id: x.id, label: x.label.trim(), color: x.color, auto: true}
    : {id: x.id, label: x.label.trim(), ...(x.color !== oc[x.id] ? {color: x.color} : {})});
  const ms = m => m.map(x => [x.date.trim(), x.label.trim()].join("\u0000")).join("\u0001");
  if (ms(v.markers) !== ms(o.markers)) set.markers = v.markers.map(x => ({date: x.date.trim(), label: x.label.trim()}));
  return set;
}
// 当场校验改了的项（没改的不查：文件里原样写着的不拦保存），说法与 config._proj_val 相同；返回 {行: 原因}（计划项 g:N，里程碑 m:N）
function cfgErrs(S, set) {
  const out = {}, num = (k, x) => {
    const s = S[k] || {}, n = String(x).trim(), name = s.name || k, f = +n;
    if (n === "" || !isFinite(f)) return `${name}要填数`;
    if (s.lo != null && (f < s.lo || f > s.hi)) return `${name}要在 ${s.lo}–${s.hi} ${s.unit || ""}之间`;
    return s.int && f !== Math.round(f) ? `${name}要填整数` : null;
  };
  const tmax = S.title?.max ?? 60, lmax = S.label?.max ?? 40;
  if ("title" in set && (!set.title || set.title.length > tmax)) out.title = `标题不能空，最多 ${tmax} 个字`;
  for (const k of ["interval", "refresh", "hide_done_hours"]) if (k in set) { const e = num(k, set[k]); if (e) out[k] = e; }
  for (const k in set.alerts || {}) { const e = num(k, set.alerts[k]); if (e) out[k] = e; }
  (set.groups || []).forEach((g, i) => { if (!g.label || g.label.length > lmax) out["g:" + i] = `名字不能空，最多 ${lmax} 个字`; });
  (set.markers || []).forEach((m, i) => {
    const e = cfTs(m.date) == null ? "要选日期" : m.label.length > lmax ? `名字最多 ${lmax} 个字` : null;
    if (e) out["m:" + i] = e;
  });
  return out;
}
function cfgCur(p) {                       // 这个项目还没保存的草稿；没有改动了（又改回去了）就丢掉，按快照画
  const E = CFE[p.id];
  if (E && !E.busy && !E.err && !E.show && !Object.keys(cfgDiff(E.base, E.v)).length) delete CFE[p.id];
  return CFE[p.id] || null;
}
const cfgEdit = p => CFE[p.id] ||= {base: cfgDraft(p.cfg, p), v: cfgDraft(p.cfg, p), busy: false, err: null, field: null, show: false};
// 画一节要的东西：base o、草稿 v、改了的项 dirty、出错的项 errs（当场查的，加服务端按 field 报的）
function cfgCtx(p) {
  const c = p.cfg, E = cfgCur(p), o = E ? E.base : cfgDraft(c, p), v = E ? E.v : o, S = c.spec || {}, set = E ? cfgDiff(o, v) : {};
  const dirty = new Set([...Object.keys(set).filter(k => k !== "alerts"), ...Object.keys(set.alerts || {})]), errs = cfgErrs(S, set);
  if (E?.err && E.field && !errs[E.field]) errs[E.field] = E.err;
  return {p, c, E, o, v, S, set, dirty, errs, write: canWrite(p.id), dis: canWrite(p.id) ? "" : " disabled", da: `data-p="${esc(p.id)}"`};
}
const cfB = (X, act, text, attrs = "") => `<button ${attrs.includes("class=") ? "" : `class="btn mini-btn" `}data-cfb="${act}" ${X.da}${X.dis} ${attrs}>${text}</button>`;
// 一项设置的外壳：改了的左边蓝线，出错的标红、原因写在下面；ks 是这一行管的键（「原 X ↺」一次还原它们）
function cfRow(X, f, ks, name, ctl, {hint = "", was = "", cls = ""} = {}) {
  const d = ks.some(k => X.dirty.has(k)), e = ks.map(k => X.errs[k]).find(Boolean);
  return `<div class="fr${cls ? " " + cls : ""}${d ? " dirty" : ""}${e ? " err" : ""}" data-f="${f}">${name ? `<span class="l">${name}</span>` : ""}<div class="c">${ctl}`
    + `<span data-cfv="w:${f}">${d && was && X.write ? cfB(X, "undo:" + ks.join(","), `${was.startsWith("还原") ? "" : "原 "}${was} ↺`, `class="lnk cf-was" ${tip("还原成保存过的值")}`) : ""}</span>`
    + `<div class="cf-msg" data-cfv="e:${f}">${e ? `<span class="cf-err">${esc(e)}</span>` : hint}</div></div></div>`;
}
// 时长一项：常用档小签（缺省那档标出）+ 自定义框；off：启动参数定死了，整排置灰
function cfDurRow(X, k, unit, hint, extra = "", off = false) {
  const s = X.S[k] || {}, val = X.v[k], dis = off ? " disabled" : X.dis;
  const chips = (CF_PRESET[k] || []).map(x => `<button class="cf-chip${+val === x && String(val).trim() !== "" ? " on" : ""}" data-cfb="set:${k}:${x}" ${X.da}${dis}>${cfDur(k, x)}${x === s.dft ? `<span class="dft">缺省</span>` : ""}</button>`).join("");
  return cfRow(X, k, [k], esc(s.name || k), `<div class="cf-ln"><span class="chips" data-cfv="ch:${k}">${chips}</span><span class="cf-own"><span class="sm mut">自定义</span>`
    + `<input class="cf-num" type="number" inputmode="decimal" data-cf="${k}" ${X.da} value="${esc(val)}"${s.lo != null ? ` min="${s.lo}" max="${s.hi}"` : ""} step="${s.int ? 1 : "any"}" aria-label="${esc(s.name || k)}（${unit}）"${dis}><span class="sm mut">${unit}</span></span></div>${extra}`,
    {hint, was: cfDur(k, X.o[k]), cls: off ? "off" : ""});
}
// 采集与刷新的节奏：看 10 分钟（采集间隔长时放宽到 4 个间隔），刻度用重复的渐变画
function cfRhythm(X) {
  const ok = k => !X.errs[k] && +X.v[k] > 0 ? +X.v[k] : +X.o[k] || 60, iv = X.c.interval_fixed ? +X.c.interval || 60 : ok("interval"), rf = ok("refresh"), W = Math.max(600, 4 * iv);
  const tk = (st, cls) => `<div class="tk${cls}" style="--st:${(100 * st / W).toFixed(3)}%"></div>`;
  return `<div class="rhy" data-cfv="rhy" ${tip(`采集：每 ${dur(iv)} 读一次各主机与日志；刷新：打开的项目页每 ${dur(rf)} 重画一次`)}><span>采集</span>${tk(iv, "")}<span>刷新</span>${tk(rf, " r")}`
    + `<div class="ax"><span>0</span><span>${cfDur("", W / 2)}</span><span>${cfDur("", W)}</span></div></div>`;
}
// 里程碑：小时间轴（已过变灰、现在一根线）+ 每行日期、可选的时刻、名字、还有多久、×
function cfMarkers(X) {
  const now = D.generated, ms = X.v.markers.map(m => ({...m, t: cfTs(m.date)})).filter(m => m.t != null);
  let track = `<div class="sm mut cf-none">没有里程碑：项目页的时间线上不画竖线。</div>`;
  if (ms.length) {
    const xs = ms.map(m => m.t).concat(now), lo = Math.min(...xs) - 2 * 86400, hi = Math.max(...xs) + 2 * 86400, P = t => (100 * (t - lo) / (hi - lo)).toFixed(2);
    let last = -99;
    track = `<div class="mtl"><div class="ln"></div><div class="past" style="width:${P(now)}%"></div>` + ms.sort((a, b) => a.t - b.t).map(m => {
      const x = +P(m.t), show = x - last >= 16; if (show) last = x;      // 挨得太近的名字不写在轴上（悬停看）
      return `<div class="mk${m.t < now ? " gone" : ""}" style="left:${x}%" ${tip(`${m.label || m.date}：${m.date}`)}>${show ? `<span>${esc(m.label || m.date)}</span>` : ""}<i></i><em>${esc(m.date.slice(5, 10))}</em></div>`;
    }).join("") + `<div class="now" style="left:${P(now)}%"><span>现在</span></div></div>`;
  }
  const lmax = X.S.label?.max ?? 40;
  const rows = X.v.markers.map((m, i) => {
    const t = cfTs(m.date), [d, hm = ""] = m.date.split(" "), e = X.errs["m:" + i];
    return `<div class="mk-row${t != null && t < now ? " gone" : ""}${e ? " err" : ""}" data-f="m:${i}"><input class="cf-dt" type="date" data-cf="md:${i}" ${X.da} value="${esc(d)}" aria-label="日期"${X.dis}>`
      + `<input class="cf-dt cf-tm" type="time" data-cf="mt:${i}" ${X.da} value="${esc(hm)}" step="300" aria-label="时刻（可不填：当天结束）"${X.dis}>`
      + `<input class="cf-txt" data-cf="ml:${i}" ${X.da} value="${esc(m.label)}" maxlength="${lmax}" placeholder="名字" aria-label="名字"${X.dis}>`
      + `<span class="mk-w" data-cfv="mw:${i}">${t == null ? "" : t < now ? "已过" : `还有 ${cfDur("", t - now)}`}</span>${cfB(X, `mdel:${i}`, "×", `class="cf-x" aria-label="删掉这个里程碑"`)}`
      + `<div class="cf-msg" data-cfv="me:${i}">${e ? `<span class="cf-err">${esc(e)}</span>` : ""}</div></div>`;
  }).join("");
  return cfRow(X, "markers", ["markers"], "", `<div data-cfv="mtl">${track}</div>${rows}${X.write ? cfB(X, "madd", "＋ 加里程碑", `class="cf-add"`) : ""}`, {was: "还原里程碑", cls: "wide"});
}
// 计划项：写明的（[[groups]]，能改名、换色）与自动归出来的（只读名字与颜色，标「自动」）在同一个列表里，都能按住把手拖动或 ↑↓ 排顺序（拖动见 sortBind）；
// 挪到要写的位置的自动组标「保存后写明」（cfgKeep，#150）。每行这组几个任务、跑完几个（项目快照的 groups），没归组的另写一句
function cfGroups(X) {
  const st = Object.fromEntries((X.p.groups || []).map(g => [g.id, g])), n = X.v.groups.length, lmax = X.S.label?.max ?? 40, keep = X.set.groups ? X.set.groups.length : 0;
  const cnt = g => !g ? `<span class="mut">还没有任务</span>` : `${g.total} 个 · 跑完 ${g.done}${g.failed ? ` · <span style="color:var(--bad)">失败 ${g.failed}</span>` : ""}${g.running ? ` · 在跑 ${g.running}` : ""}`;
  const own = X.v.groups.map((g, i) => {
    const hex = cfHex(g.color), e = X.errs["g:" + i], w = g.auto && i < keep;
    const name = g.auto ? `<span class="gp-c"><i style="background:${esc(g.color || "var(--muted)")}"></i></span><span class="gp-l">${esc(g.label)}</span>`
      + (w ? `<span class="badge b-acc" ${tip(`保存时写成 monitor.toml 里的 [[groups]]（prefix = ${g.id}，钉住现在的颜色），之后能改名、换色`)}>保存后写明</span>`
        : `<span class="badge b-mute" ${tip("按任务名前缀自动归出来的：拖到写明的计划项之间（或排到别的自动组前面）保存，就写成 monitor.toml 里的 [[groups]]，之后能改名、换色")}>自动</span>`)
      : `<span class="gp-c" ${tip("换颜色")}><span data-cfv="gc:${i}"><i style="background:${esc(hex || g.color || "var(--muted)")}"></i></span><input type="color" data-cf="gc:${i}" ${X.da} value="${hex || "#888888"}" aria-label="计划项 ${esc(g.id)} 的颜色"${X.dis}></span>`
      + `<input class="cf-txt" data-cf="g:${i}" ${X.da} value="${esc(g.label)}" maxlength="${lmax}" aria-label="计划项 ${esc(g.id)} 的名字"${X.dis}>`;
    return `<div class="gp-row${g.auto ? " auto" : ""}${w ? " new" : ""}${e ? " err" : ""}" data-f="g:${i}" data-gi="${i}" ${X.da}><span class="gp-hd"${X.write && n > 1 ? ` data-sort ${tip("按住拖动排顺序")}` : ""} aria-hidden="true">⋮⋮</span>`
      + name + `<code class="sm gp-id" ${tip(g.auto ? "自动组的 id（名字前缀）" : "计划项 id（monitor.toml 里的 id）")}>${esc(g.id)}</code>`
      + `<span class="sm gp-n">${cnt(st[g.id])}</span><span class="gp-ud">${cfB(X, `up:${i}`, "↑", `class="cf-x" aria-label="上移"${i ? "" : " disabled"}`)}${cfB(X, `down:${i}`, "↓", `class="cf-x" aria-label="下移"${i < n - 1 ? "" : " disabled"}`)}</span>`
      + `<div class="cf-msg" data-cfv="ge:${i}">${e ? `<span class="cf-err">${esc(e)}</span>` : ""}</div></div>`;
  }).join("");
  // 草稿生成之后才归出来的自动组：不在草稿里，只读列在后面（保存、撤销后进下一份草稿）
  const late = (X.p.groups || []).filter(g => g.auto && !X.v.groups.some(x => x.id === g.id)).map(g => `<div class="gp-row auto"><span class="gp-hd"></span><span class="gp-c"><i style="background:${esc(g.color || "var(--muted)")}"></i></span>`
    + `<span class="gp-l">${esc(g.label)}</span><span class="badge b-mute" ${tip("改着计划项时新归出来的：保存或撤销之后才能排顺序")}>自动</span><span class="sm gp-n">${cnt(g)}</span></div>`).join("");
  const un = X.p.ungrouped ? `<div class="sm cf-un">另有 ${X.p.ungrouped} 个任务没归进任何组${X.c.auto_groups ? "（名字里没有 _ 或 - 前缀）" : "（自动归组关着）"}</div>` : "";
  const none = !own && !late ? `<div class="sm mut cf-none">${X.p.groups ? "还没有计划项：monitor.toml 里没写 [[groups]]，也还没有能自动归组的任务。" : "这一轮还没读到项目的任务。"}</div>` : "";
  const sw = `<label class="swr"><span class="sw"><input type="checkbox" data-cf="auto_groups" ${X.da} ${X.v.auto_groups ? "checked" : ""}${X.dis}><i></i></span>其余任务按名字前缀自动归组</label>`;
  return cfRow(X, "groups", ["groups", "auto_groups"], "", (own ? `<div class="gp-list">${own}</div>` : "") + late + none + un + sw + `<div class="sm mut cf-ft">归组规则（prefix、match、done_when）在 monitor.toml 里改。</div>`, {was: "还原计划项", cls: "wide"});
}
// 告警：一条一句话，数字嵌在句子里，右边一张小图告诉人这个数大概什么意思
function cfAlerts(X) {
  const a = X.v.alerts, S = X.S, n = (k, w = 58) => `<input class="cf-num" type="number" inputmode="decimal" data-cf="a:${k}" ${X.da} value="${esc(a[k])}"${S[k]?.lo != null ? ` min="${S[k].lo}" max="${S[k].hi}"` : ""} step="${S[k]?.int ? 1 : "any"}" style="width:${w}px" aria-label="${esc(S[k]?.name || k)}"${X.dis}>`;
  const ok = k => !X.errs[k] && isFinite(+a[k]) && String(a[k]).trim() !== "" ? +a[k] : +X.o.alerts[k] || 0;
  const iv = X.c.interval_fixed ? +X.c.interval || 60 : !X.errs.interval && +X.v.interval > 0 ? +X.v.interval : +X.o.interval || 60;
  const row = (f, ks, ic, s, vis) => ks.every(k => k in a) ? cfRow(X, f, ks, "", `<div class="al"><span class="ai">${icon(ic)}</span><div class="s">${s}</div><div class="v" data-cfv="v:${f}">${vis}</div></div>`,
    {was: ks.map(k => esc(X.o.alerts[k])).join(" / "), cls: "wide"}) : "";
  const u = Math.max(0, Math.min(100, ok("gpu_idle_util"))), ls = ok("log_silent_min"), on = +a.log_silent_min > 0;
  // 数据过期的实际门槛（config.stale_limit，与 labmon wait、项目页标红同一个）：没改草稿取后端算好的 s；改了采集间隔或这项时拿后端给的系数当场套（#141）
  const SL = X.c.stale, st = Math.max(0, ok("stale_min")), siv = X.c.interval_fixed ? +SL.iv || iv : iv, floor = SL.runs * siv;
  const lim = X.dirty.has("interval") || X.dirty.has("stale_min") ? Math.max(floor, st * 60) + SL.pad : +SL.s, span = lim * 1.3;
  return row("al-gpu", ["gpu_idle_util", "gpu_idle_min"], "zap", `GPU 利用率（近 10 分钟平均，保活按 0 算）低于 ${n("gpu_idle_util", 52)} % 持续 ${n("gpu_idle_min")} 分钟，算空闲、告警`,
      `<div class="bar thin"><i style="width:${u}%;background:color-mix(in srgb, var(--warn) 55%, transparent)"></i><b style="left:${u}%"></b></div><span>0 · 低于 ${+u.toFixed(1)}% 算空闲 · 100%</span>`)
    + row("al-stale", ["stale_min"], "clock", `数据 ${n("stale_min")} 分钟没更新，算过期`,
      `<div class="bar thin tks" style="--st:${(100 * siv / span).toFixed(3)}%"><b style="left:${(100 * lim / span).toFixed(2)}%"></b></div>`
      + `<span ${tip(`labmon wait 的 collector_stale 与项目页顶栏“N 分钟未更新”都按这个门槛：${SL.runs} 次采集与填的值取大，再加 ${cfDur("", SL.pad)}`)}>实际按 ${cfDur("", lim)}：`
      + (st * 60 < floor ? `至少等 ${SL.runs} 次采集（每 ${cfDur("", siv)}）加 ${cfDur("", SL.pad)}，填的 ${+st} 分钟不起作用` : `填的 ${+st} 分钟加 ${cfDur("", SL.pad)}`) + `</span>`)
    + row("al-log", ["log_silent_min"], "list", `<label class="sw cf-sw"><input type="checkbox" data-cf="logon" ${X.da} ${on ? "checked" : ""} aria-label="日志静默告警"${X.dis}><i></i></label>`
      + ` 运行中任务的日志 ${on ? n("log_silent_min") : `<input class="cf-num" type="number" value="" placeholder="—" style="width:58px" aria-label="日志多久没更新告警（关着）" disabled>`} 分钟没更新就告警`,
      on ? `<span>日志停 ${cfDur("", ls * 60)} 就报</span>` : `<span>关着：不报</span>`);
}
// monitor.toml 前后对照（「看改动」）：顶层键与 [alerts] 一行一改，计划项逐块写改了什么（自动组写成的新块整段列出），里程碑整组写新的
function cfgDiffText(X) {
  const {set, o} = X, L = [], tv = x => typeof x === "boolean" ? String(x) : String(x).trim() !== "" && isFinite(+x) ? String(+x) : JSON.stringify(String(x));
  const pm = (k, a, b) => L.push(`<span class="d">- ${esc(k)} = ${esc(tv(a))}</span>`, `<span class="a">+ ${esc(k)} = ${esc(tv(b))}</span>`);
  for (const k of ["title", "interval", "refresh", "hide_done_hours", "auto_groups"]) if (k in set) pm(k, o[k], set[k]);
  if (set.alerts) { L.push("[alerts]"); for (const k in set.alerts) pm(k, o.alerts[k], set.alerts[k]); }
  if (set.groups) {
    const ob = Object.fromEntries(o.groups.map(g => [g.id, g])), was = o.groups.filter(g => !g.auto).map(g => g.id), add = set.groups.filter(g => g.auto);
    for (const g of add) L.push(`<span class="a">+ [[groups]]</span>`, ...[["id", g.id], ["label", g.label], ["prefix", g.id], ["color", g.color]].map(([k, x]) => `<span class="a">+ ${k} = ${esc(JSON.stringify(x))}</span>`));
    for (const g of set.groups) if (!g.auto && (g.label !== ob[g.id].label || "color" in g)) {
      L.push(`[[groups]] id = ${esc(JSON.stringify(g.id))}`);
      if (g.label !== ob[g.id].label) pm("label", ob[g.id].label, g.label);
      if ("color" in g) pm("color", ob[g.id].color, g.color);
    }
    if (set.groups.map(g => g.id).join("\u0000") !== [...was, ...add.map(g => g.id)].join("\u0000")) L.push(`[[groups]] 顺序改成 <span class="a">${set.groups.map(g => esc(g.label)).join("、")}</span>`);
  }
  if (set.markers) L.push(`[[markers]] 整组换成 <span class="a">${set.markers.map(m => esc(m.label && m.label !== m.date ? `${m.date} ${m.label}` : m.date)).join("、") || "没有"}</span>`);
  return `<pre class="cf-diff">${esc(X.c.path)}\n${L.join("\n")}</pre>`;
}
// 改了没保存：贴在这一节底部的保存条（改了几项、看改动、撤销、保存）；没改动时为空
function cfgSaveBar(X) {
  const {E, set, errs} = X;
  if (!X.write || !E || (!Object.keys(set).length && !E.err)) return "";
  const S = X.S, names = [...X.dirty].map(k => k === "groups" ? "计划项" : k === "markers" ? "里程碑" : S[k]?.name || (k === "auto_groups" ? "自动归组" : k)), nb = Object.keys(errs).length;
  return `<span class="what">改了 <b>${names.length}</b> 项：${esc(names.join("、"))}</span>${cfB(X, "diff", E.show ? "收起改动" : "看改动", `class="lnk"`)}`
    + `${cfB(X, "reset", "撤销", `class="btn"${E.busy ? " disabled" : ""}`)}<button class="btn pri" data-cfb="save" ${X.da} ${E.busy || nb ? "disabled" : ""} ${nb ? tip(`有 ${nb} 项填得不对：先改好标红的那几行`) : ""}>${E.busy ? "正在保存…" : "保存到 monitor.toml"}</button>`
    + (E.err && !E.field ? `<div class="rn-err">${esc(E.err)}</div>` : "") + (E.show && Object.keys(set).length ? cfgDiffText(X) : "")
    + `<div class="sm mut cf-tell">写进看板上的 monitor.toml（只动改了的行、注释留着），记一条配置改动，并告诉实验会话：它的 labmon wait 会把改动拉回项目目录（项目仓库里会多出没提交的改动）。</div>`;
}
function projCfgSec(p) {
  const c = p.cfg;
  if (!c) return dSec("项目配置", `<div class="empty">还没有配置摘要：看板服务下一轮采集后再看。</div>`);
  const X = cfgCtx(p), v = X.v, tmax = X.S.title?.max ?? 60;
  const ids = `<div class="idg"><div class="idt"><div class="k">${icon("lock")}看板上的配置${LIVE ? `<button class="lnk sm" data-copy="${esc(c.path)}">复制</button>` : ""}</div><code ${tip(c.path)}>${esc(c.path)}</code></div>`
    + `<div class="idt"><div class="k">${icon("lock")}项目 id</div><code>${esc(p.id)}</code></div><div class="idt"><div class="k">${icon("lock")}输出目录</div><code ${tip(c.out_dir)}>${esc(c.out_dir)}</code></div></div>`
    + `<div class="sm mut cf-idn">配置以看板上这份为准，项目目录里的 monitor.toml 是工作副本：实验会话的 labmon wait 每轮同步（本地改了推上来、这里改了拉回去）。项目 id 改不了，输出目录固定在登记目录里。</div>`;
  const title = cfRow(X, "title", ["title"], "", `<input class="ttl" data-cf="title" ${X.da} value="${esc(v.title)}" maxlength="${tmax}" aria-label="标题"${X.dis}>`, {was: `「${esc(X.o.title)}」`, cls: "wide"});
  const fixed = c.interval_fixed ? `<div class="sm cf-fix">看板服务启动时用 --interval 定了间隔（每 ${cfDur("", +c.interval || 0)} 一次），monitor.toml 里的不生效；要在这里改，去掉这个启动参数再重启看板服务。</div>` : "";
  const h = (ic, t, s = "") => `<div class="cf-h">${icon(ic)}${t}<span class="sp"></span>${s ? `<span class="sm">${s}</span>` : ""}</div>`;
  const body = (CFMSG ? `<div class="rn-state">${esc(CFMSG)}</div>` : "") + ids + title
    + `<div class="cf-sec">${h("clock", "采集与显示")}${cfDurRow(X, "interval", "秒", "", fixed + cfRhythm(X), !!c.interval_fixed)}${cfDurRow(X, "refresh", "秒", "打开的项目页多久重画一次")}`
    + `${cfDurRow(X, "hide_done_hours", "小时", "跑完这么久没动的车道收起；只管不是 monitor.toml 写明的车道，[[queues]] 里单条写了的按它的")}</div>`
    + `<div class="cf-sec">${h("flag", "里程碑", "项目页时间线上的竖线")}${cfMarkers(X)}</div>`
    + `<div class="cf-sec">${h("list", "计划项", "项目页「计划项」按这些组数完成数" + (X.write && v.groups.length ? "；按住 ⋮⋮ 拖动或 ↑↓ 排顺序（自动组排到写明的之间就写进 monitor.toml），点名字直接改" : ""))}${cfGroups(X)}</div>`
    + `<div class="cf-sec">${h("zap", "告警阈值")}${cfAlerts(X)}</div>`
    + (X.write ? "" : `<div class="sm mut cf-ft">${LIVE ? "只读：你的账号没有项目的写权限，要改找管理员。" : "在项目目录的 monitor.toml 里改，labmon wait 或 labmon project push 推上来后下一轮自动重读。"}</div>`)
    + `<div class="cf-save" data-cfv="save">${cfgSaveBar(X)}</div>`;
  return dSec("项目配置", body, `<span class="sm mut">${X.write ? "改了就写回看板上的 monitor.toml，只动改了的行、注释留着" : "只读"}</span>`, "cfgsec");
}
// 打字时就地换上展示块（data-cfv）与行的 class（data-f），输入框不动：焦点在输入框里时整页重画被挡着（editing）
function cfgPatch(pid) {
  const sec = document.getElementById("cfgsec"), p = D.projects.find(x => x.id === pid);
  if (!sec?.querySelectorAll || !p || DKEY !== "proj:" + pid) return;
  const t = document.createElement("div");
  t.innerHTML = projCfgSec(p);
  for (const e of sec.querySelectorAll("[data-cfv]")) { const n = t.querySelector(`[data-cfv="${CSS.escape(e.dataset.cfv)}"]`); if (n && n.innerHTML !== e.innerHTML) e.innerHTML = n.innerHTML; }
  for (const e of sec.querySelectorAll("[data-f]")) { const n = t.querySelector(`[data-f="${CSS.escape(e.dataset.f)}"]`); if (n && n.className !== e.className) e.className = n.className; }
}
// 项目归属写组内人员名，账号放悬停（#15：与人员页、人员卡同一口径）；人员名取账号库的 fleet_person（只有管理员读得到，读不到的写账号名）
const acctPerson = (a, L = directoryUsers()) => { const u = L?.users.find(x => x.name === a); return u?.fleet_person || a; };
function projOwnerSec(p) {
  const open = popIs("owner", p.id), who = p.owner ? acctPerson(p.owner) : "";
  const label = p.owner ? `<b ${tip("看板账号 " + p.owner)}>${esc(who)}</b>${p.owner === ME?.name ? ' <span class="chip">我</span>' : ""}` : "<b>归管理员</b>";
  const b = `<button class="btn mini-btn" data-pj="owner" data-p="${esc(p.id)}">更改项目归属</button>`;
  const form = !open ? "" : `<label>归属给谁 <select class="ain" data-owner ${ACC.load.users ? "disabled" : ""}>
    <option value="">归管理员</option>${(ACC.users?.users || []).map(u => `<option value="${esc(u.name)}"${u.name === POP.owner ? " selected" : ""}>${esc(u.fleet_person && u.fleet_person !== u.name ? `${u.fleet_person}（账号 ${u.name}）` : u.name)}</option>`).join("")}
    </select></label>${accErr(ACC.loadError.users, false)}${accLine("load")}${pjErr()}`;
  const go = ACC.load.users || !ACC.users ? "" : pjGo("ownergo", p.id, "保存");
  const edit = canWrite() && ME.account ? popConfirm(b, open, form, go, true) : "";
  return dSec("项目归属", `${permissionRow("归属", label, edit)}${PJMSG && PJMSG.pid === p.id ? `<div class="rn-state">${esc(PJMSG.msg)}</div>` : ""}`);
}
function projDrawer(id) {
  const p = D.projects.find(x => x.id === id);
  if (!p) return dHead(`<span class="fav">?</span>`, esc(id), "看板服务里没有这个项目") + `<div class="db"><div class="card empty">${(D.archived || []).some(e => e.id === id)
    ? `已经归档了：在<a href="${DH.arch()}" data-drw="${DH.arch()}">已归档</a>里恢复。` : "可能已从看板服务里摘掉。"}</div></div>`;
  return dHead(`<span class="fav">${esc((p.title || id).slice(0, 1))}<span class="cdot" style="background:${color(id)}"></span></span>`, esc(p.title || id), `${esc(id)} · 管理`)
    + `<div class="db">${projOwnerSec(p)}${projRegSec(p)}${projSchedSec(p)}${projCfgSec(p)}${projArchSec(p)}</div>`;
}
// 「归档」与「已归档」抽屉（#97，projects.archive / restore）：做完的项目收起、要时加回。能不能归档由看板算好（项目的 archive_block：null 能，否则一句原因），
// 页面只照它置灰与写原因。POST /project/archive|restore|delete，回执写在已归档的抽屉顶上（PJMSG），一分钟后收起
let PJMSG = null;
const ARCH_DOES = `移除这个项目在各主机上的登记（已归还的主机上留着的也移除），撤掉它的权重与暂停、优先，看板不再采集它。`;
const ARCH_KEEPS = `不删：看板上的配置与输出目录、共享盘上的文件、用量记录都留着；在侧栏的「已归档」里能恢复（登记要重新 labmon worker add）。`;
const pjErr = () => POP?.err ? `<div class="rn-err">${esc(POP.err)}</div>` : "";
const pjGo = (act, id, text, danger = false) => `<button class="btn ${danger ? " danger" : "pri"}" data-pj="${act}" data-p="${esc(id)}" ${POP?.busy ? "disabled" : ""}>${POP?.busy ? "正在处理…" : text}</button>`;
// 抽屉最下面的危险区（#133）：一句话说清归档做什么、留什么，按钮红框；确认弹窗里写全（ARCH_DOES、ARCH_KEEPS）
function projArchSec(p) {
  if (LIVE && !canWrite(p.id)) return "";
  const blk = p.archive_block, known = "archive_block" in p;
  const say = `<div class="sm mut">移除各主机上的登记、撤掉调度设置、看板不再采集；数据与配置都留着，能恢复。</div>`;
  const why = !LIVE ? `<div class="sm mut">在本机的看板服务上归档。</div>`
    : !known ? `<div class="sm mut">还不知道能不能归档：看板服务下一轮采集后再看</div>` : blk ? `<div class="sm" style="color:var(--warn)">现在不能归档：${esc(blk)}</div>` : "";
  const regs = (D.regs || []).flatMap(x => (x.queues || []).filter(q => q.project === p.id).map(() => x.host)), nh = new Set(regs).size;
  const btn = `<button class="btn danger" data-pj="a" data-p="${esc(p.id)}" ${canWrite(p.id) && known && blk == null ? "" : "disabled"}>${icon("archive")}归档项目</button>`;
  const pop = !canWrite(p.id) ? "" : popConfirm(btn, popIs("pa", p.id), !popIs("pa", p.id) ? "" : `<div class="rn-note"><b>归档 ${esc(p.title || p.id)}</b>：${ARCH_DOES}
      ${regs.length ? `这一轮读到 ${nh} 台主机上有它的 ${regs.length} 条登记。` : "这一轮没读到它的登记。"}<br>${ARCH_KEEPS}</div>${pjErr()}`, pjGo("ago", p.id, "归档"));
  return `<section class="card dz"><div class="dz-r"><div><b>归档这个项目</b>${say}${why}</div>${pop ? `<span class="pop-l">${pop}</span>` : ""}</div></section>`;
}
const canDeleteProject = () => LIVE && ME?.admin && ME.scopes?.includes("admin");
function archDrawer() {
  const A = D.archived || [];
  const card = e => {
    const rg = e.regs || [], sk = e.skipped || [];
    const kv = [["项目 id", `<code>${esc(e.id)}</code>`], ["归档于", esc(ts(e.t))],
      ["移除的登记", rg.length ? `<details class="dfold"><summary class="sm">${rg.length} 条</summary>${rg.map(x => `<div class="sm"><code>${esc(nrm(x.root))}/<b>${esc(x.jobs)}</b></code> <span class="mut">${esc(x.host)}</span></div>`).join("")}</details>`
        : `<span class="mut">没有</span>`]];
    if (sk.length) kv.push(["没动", sk.map(x => `<div class="sm">${esc(x)}</div>`).join("")]);
    const btn = `<button class="btn" data-pj="r" data-p="${esc(e.id)}">${icon("undo")}恢复</button>`;
    const pop = !canWrite(e.id) ? "" : popConfirm(btn, popIs("pr", e.id), !popIs("pr", e.id) ? "" : `<div class="rn-note"><b>恢复 ${esc(e.title || e.id)}</b>：看板接着采集它，项目页回到侧栏。
        登记不自动恢复：要跑先 <code>labmon worker add … --project ${esc(e.id)}</code>。</div>${pjErr()}`, pjGo("rgo", e.id, "恢复"));
    const deleteBtn = `<button class="btn danger" data-pj="d" data-p="${esc(e.id)}">永久删除</button>`;
    const remove = !canDeleteProject() ? "" : popConfirm(deleteBtn, popIs("pd", e.id),
      !popIs("pd", e.id) ? "" : `<div class="rn-note"><b>永久删除 ${esc(e.title || e.id)}</b>
        <div>将删除看板配置、项目缓存和归档记录，删除后无法恢复。共享实验文件、用量和审计记录会保留。</div></div>${pjErr()}`,
      pjGo("dgo", e.id, "永久删除", true));
    return dSec(esc(e.title || e.id), kvHtml(kv), [pop, remove].filter(Boolean).map(x => `<span class="pop-l">${x}</span>`).join(""));
  };
  return dHead(`<span class="fav">${icon("archive")}</span>`, "已归档的项目", "恢复后看板接着采集")
    + `<div class="db">${typeof PJMSG === "string" ? `<div class="rn-state">${esc(PJMSG)}</div>` : ""}${A.map(card).join("") || `<div class="card empty">没有归档的项目。</div>`}</div>`;
}
async function pjAct(act, pid) {
  if (!canWrite(pid)) return;
  if (act === "owner" && canWrite()) {
    const p = D.projects.find(x => x.id === pid);
    popToggle("owner", pid, {owner: p?.owner || ""});
    if (POP) accLoad("users");
    return render(false);
  }
  if (act === "d") {
    if (!canDeleteProject()) return;
    const e = (D.archived || []).find(e => e.id === pid);
    if (!e) return;
    popToggle("pd", pid, {revision: String(e.revision || e.t)});
    render(false);
    return document.querySelector("#drwp .pop")?.scrollIntoView({block: "nearest"});
  }

  if (act === "a" || act === "r") {                  // 「归档」在抽屉底部：弹窗打开后滚进视野
    popToggle(act === "a" ? "pa" : "pr", pid); render(false);
    return document.querySelector("#drwp .pop")?.scrollIntoView({block: "nearest"});
  } else {
    const P = POP, action = {ago: "archive", rgo: "restore", ownergo: "owner", dgo: "delete"}[act];
    const popup = {archive: "pa", restore: "pr", owner: "owner", delete: "pd"}[action];
    if (!action || !popIs(popup, pid) || P.busy ||
        (action === "owner" && !canWrite()) || (action === "delete" && !canDeleteProject())) return;
    P.busy = true; P.err = null; render(false);
    try {
      const r = await postJSON(`project/${action}`, {project: pid, ...(action === "owner" ? {owner: P.owner} : {}),
        ...(action === "delete" ? {revision: P.revision} : {})});
      const j = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(j.error || "HTTP " + r.status);
      if (POP === P) popClose();
      PJMSG = action === "owner" ? {pid, msg: j.msg} : j.msg;
      const message = PJMSG;
      setTimeout(() => { if (PJMSG === message) { PJMSG = null; render(false); } }, 60000);
      if (action === "delete") {
        D.archived = (D.archived || []).filter(e => e.id !== pid || String(e.revision || e.t) !== P.revision);
        render(false);
        return poll();
      }
      if (action === "owner") {
        const p = D.projects.find(x => x.id === pid);
        if (p) p.owner = P.owner;
      }
      if (act === "ago") { frameEl(pid)?.remove(); nav(DH.arch()); }       // 项目页没了：回到总览，打开已归档的抽屉看回执
      return collectNow(true);
    } catch (e) { if (POP === P) { P.busy = false; P.err = e.message || String(e); } }
  }
  render(false);
}
// 项目配置的按钮（#122，#133）：档位 set:键:值、单项还原 undo:键[,键]、计划项上移下移、里程碑删加、看改动、撤销、保存。头一下就生成草稿（cfgEdit）
async function cfgAct(act, pid) {
  const p = D.projects.find(x => x.id === pid);
  if (!p?.cfg || !canWrite(p.id)) return;
  const E = cfgEdit(p), v = E.v, [a, k, x] = act.split(":"), i = +k;
  if (a !== "diff" && a !== "save") { E.err = E.field = null; CFMSG = null; }
  if (a === "set") v[k] = x;
  else if (a === "undo") for (const f of k.split(",")) {
    if (f in v.alerts) v.alerts[f] = E.base.alerts[f];
    else v[f] = structuredClone(E.base[f]);
  }
  else if (a === "up" || a === "down") { const j = a === "up" ? i - 1 : i + 1; if (v.groups[j]) [v.groups[i], v.groups[j]] = [v.groups[j], v.groups[i]]; }
  else if (a === "mdel") v.markers.splice(i, 1);
  else if (a === "madd") { const d = new Date(1000 * (D.generated + 7 * 86400)); v.markers.push({date: `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`, label: ""}); }
  else if (a === "diff") E.show = !E.show;
  else if (a === "reset") delete CFE[pid];
  else if (a === "save") {
    const set = cfgDiff(E.base, v);
    if (!Object.keys(set).length || Object.keys(cfgErrs(p.cfg.spec || {}, set)).length) return render(false);
    E.busy = true; E.err = E.field = null; render(false);
    try {
      const r = await postJSON("project/config", {project: pid, set});
      const j = await r.json().catch(() => ({}));
      if (!r.ok) throw Object.assign(new Error(j.error || "HTTP " + r.status), {field: j.field});
      delete CFE[pid]; CFMSG = j.msg; setTimeout(() => { CFMSG = null; render(false); }, 60000);
      return collectNow(true);
    } catch (e) { if (CFE[pid]) Object.assign(CFE[pid], {busy: false, err: e.message || String(e), field: e.field || null}); }
  }
  render(false);
}
// 控件的输入只记进草稿（data-cf：字段名；g:N / gc:N 计划项的名字与颜色，md:N / mt:N / ml:N 里程碑的日期、时刻、名字，a:键 告警阈值，logon 日志静默的开关）。
// 能打字的控件（与取色框拖着时）只就地换展示块（cfgPatch），焦点离开后整页重画（PENDING）；开关、取色选定直接重画
function cfgInput(t, how) {
  const p = D.projects.find(x => x.id === t.dataset.p);
  if (!p?.cfg || !canWrite(p.id)) return;
  const E = cfgEdit(p), v = E.v, k = t.dataset.cf, [a, n] = k.split(":"), i = +n;
  E.err = E.field = null; CFMSG = null;
  if (k === "auto_groups") v.auto_groups = t.checked;
  else if (k === "logon") v.alerts.log_silent_min = t.checked ? String(+E.base.alerts.log_silent_min > 0 ? E.base.alerts.log_silent_min : 30) : "0";
  else if (a === "g") v.groups[i].label = t.value;
  else if (a === "gc") v.groups[i].color = t.value;
  else if (a === "md" || a === "mt") {
    const m = v.markers[i], [d, hm = ""] = m.date.split(" "), nd = a === "md" ? t.value : d, nh = a === "mt" ? t.value : hm;
    m.date = nd + (nh ? " " + nh : "");
  } else if (a === "ml") v.markers[i].label = t.value;
  else if (a === "a") v.alerts[n] = t.value;
  else v[k] = t.value;
  if (typingEl(t) || (t.type === "color" && how === "input")) { PENDING = true; cfgPatch(p.id); }
  else render(false);
}
// 计划项拖动排顺序（common.js 的 sortBind；键盘用 ↑↓ 按钮）：松手落在哪就插到哪，只改草稿；拖着时挡住的重画（PENDING）松手后补
sortBind({handle: ".gp-hd[data-sort]", row: ".gp-row[data-gi]", cover: ".cf-save", drop(row, from, to) {
  const p = D.projects.find(x => x.id === row.dataset.p);
  if (from !== to && p?.cfg && canWrite(p.id)) { const gs = cfgEdit(p).v.groups, [x] = gs.splice(from, 1); gs.splice(to, 0, x); }
  if (from !== to || PENDING) render(false);
}});
