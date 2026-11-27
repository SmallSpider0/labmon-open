/* ---------- 临时指令、调度设置与事件 ---------- */
const OVR = {pause: "暂停", focus: "优先", drain: "drain 主机", deadline: "续租截止", guest: "借来的主机"};
const UNDO = {pause: "resume", focus: "unfocus", drain: "undrain"};
// 指令的目标：暂停、优先写项目（配色点 + 标题，悬停 id），借来的主机写只给哪些项目用
function ovrTarget(o) {
  return o.kind === "pause" || o.kind === "focus" || o.kind === "guest"
    ? o.target.split(",").filter(Boolean).map(id => `<b>${projName(id, {always: true}).trim()}</b>`).join("、") : `<b>${esc(o.target)}</b>`;
}
// 临时指令表（总览各主机合起来、主机页这一台）：项目级的暂停、优先（global，主机清单 [sched.*]，所有主机）主机写“全部”、撤销不带 --host；
// 只给这台下的撤销命令带 --host
function ovrTable(rows, showHost) {
  if (!rows.length) return `<div class="empty">没有生效中的临时指令（labmon pause|focus|drain …）。</div>`;
  const undo = o => o.global ? `<code class="sm">labmon ${UNDO[o.kind]} ${esc(o.target)}</code> <span class="sm mut">或调度卡</span>`
    : `<code class="sm">labmon ${UNDO[o.kind]} ${esc(o.target || o.h.id)}${o.target ? ` --host ${esc(o.h.id)}` : ""}</code>`;
  return `<div class="scroll"><table><tr><th>指令</th>${showHost ? "<th>主机</th>" : ""}<th>到期</th><th>撤销</th></tr>` + rows.map(o => `<tr>
    <td>${o.kind === "deadline" && o.target === "borrow" ? "约定归还" : `${OVR[o.kind] || esc(o.kind)} ${o.target ? ovrTarget(o) : ""}`}${o.now ? ` <span class="badge b-warn">立即停</span>` : ""}${o.keep ? ` <span class="sm mut">${esc(keepText(o.keep))}</span>` : ""}</td>
    ${showHost ? `<td class="nw">${o.global ? "全部" : esc(hostShort(o.h))}</td>` : ""}<td class="sm">${o.until ? tsNear(o.until) : "无期限"}${o.global && !showHost ? ` <span class="mut">· 所有主机</span>` : ""}</td>
    <td>${o.kind === "guest" || o.target === "borrow" ? `<span class="sm">还了就撤（看板按借用的约定下）</span>${o.at ? ` <span class="sm mut">截止 ${tsNear(o.at)}</span>` : ""}`
      : o.kind === "deadline" ? `<span class="sm">主机页“取消预备”</span>${o.at ? ` <span class="sm mut">截止 ${tsNear(o.at)}</span>` : ""}` : undo(o)}</td></tr>`).join("") + `</table></div>`;
}

// 调度卡（总览）：放行规则一句话，各项目的份额（权重占比，按近期用了卡或此刻有任务的项目算）与近期用量占比、此刻占几张卡、排队与在等几个、
// 近 1 天平均等多久（D.sched，overview.sched_view）。权重在胶囊上点 −、+ 直接改（schedW）；加撤优先与暂停在按钮旁的弹窗里，输入记在 POP。都经 POST /sched/…（schedPost）。
// 优先、暂停（p.focus、p.paused）为 null 或 {until, hosts}：hosts 为 null 是项目级的（主机清单 [sched.focus]、[sched.pause]，所有主机），按钮撤的是它；
// 只在几台单独下的（labmon pause … --host）列出主机，徽标悬停写撤销命令
let SMSG = null;                   // 调度卡刚做完的说明
const wtxt = w => String(+(+w).toFixed(2));
const pname = id => projName(id, {always: true, plain: true}).trim();
const SFLAG = {focus: ["优先", "unfocus"], paused: ["暂停", "resume"]};
const flagHosts = f => f.hosts.map(h => hostShort(D.hosts.find(x => x.id === h) || {id: h})).join("、");
// 项目级暂停是不是看页面的人自己下的（快照 paused.by，#16）：普通用户只能恢复、改自己下的；没记是谁的当管理员下的
const pauseMine = f => !!f?.by && String(f.by).toLowerCase() === String(ME?.name || "").toLowerCase();
// 优先、暂停的说明：到期，或只在哪几台（单独下的）与撤销命令；暂停另写谁下的，普通用户恢复不了的写找管理员（项目级 #16；
// 按主机的 paused.by 是 {主机: 谁下的}，#27：不是他下的那几台不给命令）
function flagTip(k, p) {
  const f = p[k], [what, undo] = SFLAG[k];
  const theirs = k === "paused" && f.hosts && f.by && canWrite(p.id) && !canWeight() ? f.hosts.filter(h => h in f.by && !pauseMine({by: f.by[h]})) : [];
  return f.hosts ? `只在 ${flagHosts(f)} 上单独下的${what}${f.until ? `，到 ${tsNear(f.until)}` : ""}：`
      + f.hosts.filter(h => !theirs.includes(h)).map(h => `labmon ${undo} ${p.id} --host ${h}`).concat(theirs.length ? [`${flagHosts({hosts: theirs})} 上的不是你下的，撤销找管理员`] : []).join("；")
    : (f.until ? `到 ${tsNear(f.until)}` : "无期限") + "，所有主机（含之后加入的）"
      + (k !== "paused" ? "" : (f.by ? `；${f.by} 下的` : canWeight() ? "；没记是谁下的" : "") + (canWrite(p.id) && !canWeight() && !pauseMine(f) ? "：不是你下的，恢复找管理员" : ""));
}
const flagText = (k, p) => p[k].hosts ? `（只在 ${flagHosts(p[k])}）` : p[k].until ? `（到 ${tsNear(p[k].until)}）` : "";
const schSb = (p, act, cls, ic, text, hint) => `<button class="btn mini-btn ${cls}" data-sch="${act}" data-p="${esc(p.id)}" ${tip(hint)}>${ic ? icon(ic) : ""}${text}</button>`;
const schGo = (p, act, text) => `<button class="btn pri" data-sch="${act}" data-p="${esc(p.id)}" ${POP?.busy ? "disabled" : ""}>${POP?.busy ? "正在保存…" : text}</button>`;
const schErr = () => POP?.err ? `<div class="rn-err">${esc(POP.err)}</div>` : "";
// 一个项目的权重胶囊「权重 − ×1 +」（调度卡的权重格、项目「管理」抽屉的调度节同一个；#143 维护者定：不弹浮窗，点一下就改）。
// −、+ 在 W_STEPS 的相邻档间走（set_weight 认 0 到 1000），现值不在档上的往最近的那一档走；到头的那个键禁用。
// 点了先显示新值（SWB：项目 id → {w, post 正在写回, t}），写回期间这个项目的键禁用；写回后立即采集。
// 快照里的权重取 worker 状态的 share.weight（overview.sched_view），worker 读到新的 sched.json 要再过一轮，所以立即采集回来的多半还是旧值：
// 写成功的值留到快照里的权重等于它（swCur 收掉），最多留 W_HOLD_MS（Codex 审查 #147）
const W_HOLD_MS = 5 * 60000;
const W_STEPS = [0.1, 0.25, 0.5, 0.75, 1, 1.5, 2, 3, 4, 5, 6, 8, 10, 15, 20, 30, 50, 100, 200, 500, 1000];
const wStep = (w, d) => d < 0 ? W_STEPS.filter(s => s < w - 1e-9).pop() : W_STEPS.find(s => s > w + 1e-9);
const SWB = new Map();
function swCur(p) {
  const e = SWB.get(p.id);
  if (e && !e.post && (Math.abs(p.w - e.w) < 1e-9 || Date.now() - e.t > W_HOLD_MS)) { SWB.delete(p.id); return null; }
  return e;
}
function schedW(p) {
  const e = swCur(p), w = e ? e.w : p.w;
  const b = (act, d, ch, what) => { const to = wStep(w, d);
    return `<button class="btn wb" data-sch="${act}" data-p="${esc(p.id)}" aria-label="权重${what}" ${to == null || e?.post ? "disabled" : ""} ${tip(to == null ? `已是最${d < 0 ? "低" : "高"}的一档` : `${what}到 ×${wtxt(to)}`)}>${ch}</button>`; };
  return `<span class="flag wcap"><span class="k" ${tip("同一人的几个项目之间：近期用量除以它再比，小的先放，所以权重大的能在这人的份额里多用（不改这人和别人之间的份额）。没写的项目为 1；写进主机清单的 [sched.weight]，各 worker 约 1 分钟内生效")}>权重</span>`
    + (canWeight() ? b("wd", -1, "−", "调低") : "") + `<b class="num">×${wtxt(w)}</b>` + (canWeight() ? b("wu", 1, "+", "调高") : "") + `</span>`;
}
// 一个项目的优先或暂停按钮（k：focus / paused）与弹窗（调度卡每行末尾、项目「管理」抽屉的调度节）：项目级的有了就换成撤销、恢复；暂停、恢复会给实验会话一条消息。
// 优先、撤销优先只给管理员（与权重同一个 canWeight）；项目级暂停有了时，「恢复」只给管理员与下暂停的本人（pauseMine，#16）
function schedFlag(p, k) {
  const sb = (...a) => schSb(p, ...a), go = (...a) => schGo(p, ...a), err = schErr, glob = p[k] && !p[k].hosts;
  if (k === "focus" && !canWeight() || k === "paused" && glob && !canWeight() && !pauseMine(p[k])) return "";
  if (k === "focus") return glob ? popConfirm(sb("uf", "undo", "undo", "撤销优先", "各主机下一轮起按份额排"), popIs("su", p.id),
      `撤销 ${esc(pname(p.id))} 的优先：各主机下一轮起在同一人的项目之间按份额排。${err()}`, go("ufgo", "撤销优先"))
    : popConfirm(sb("f", "", "flag", "优先", "排到同一人不优先的项目前面（所有主机，含之后加入的），如截稿前；不越过别人的项目"), popIs("sf", p.id), !popIs("sf", p.id) ? "" :
      `<div class="rn-note"><b>${esc(pname(p.id))} 优先</b>：在所有主机上（含之后加入的）排到同一人不优先的项目前面（同 <code>labmon focus</code>）；份额按人算，不越过别人的项目。不停正在跑的，只影响之后放谁。</div>
      ${whenPick("schu", "到期", POP.until, "无期限", "无期限：用完点「撤销优先」", {fresh: POP.wnow})}${err()}`, go("fgo", "优先"), true);
  return glob ? popConfirm(sb("ur", "undo", "play", "恢复", "各主机下一轮起照常放行它的任务"), popIs("sr", p.id),
      `恢复 ${esc(pname(p.id))}：各主机下一轮起照常放行它的任务。${err()}`, go("urgo", "恢复"))
    : popConfirm(sb("p", "", "pause", "暂停", "不再放行它的新任务（所有主机，含之后加入的）；正在跑的跑完"), popIs("sp", p.id), !popIs("sp", p.id) ? "" :
      `<div class="rn-note"><b>暂停 ${esc(pname(p.id))}</b>：所有主机（含之后加入的）不再放行它的新任务，正在跑的自然跑完（同 <code>labmon pause</code>）。要马上按停法停下正在跑的，用命令行 <code>labmon pause ${esc(p.id)} --now</code>。</div>
      ${whenPick("schp", "到期", POP.until, "无期限", "无期限：用完点「恢复」", {fresh: POP.wnow})}${err()}`, go("pgo", "暂停"), true);
}
const schedBtns = p => schedFlag(p, "focus") + " " + schedFlag(p, "paused");
// 份额（调度卡与项目「管理」抽屉共用；#23 按人）：近期在用的项目 on（用过卡、占着卡、排着队）；su 是所有项目的用量和（use_sum，含已归档、卡上不列的）；同一人（pk，看板上没有的项目 pk 为 null、合起来算一份）
// 的几个项目合起来算一份，应得是 1 ÷ 近期在用的人数 n，再按权重分给这人的项目（sw(pk) 是这人在用项目的权重和）；who 是这人的名字（普通用户看别人的为 null）
const pkOf = p => p.pk ?? null;
const who = p => p.person || (p.pk ? "其他人" : "看板上没有的项目");
function schedOn() {
  const on = (D.sched?.projects || []).filter(p => p.use > 0.005 || p.cards || p.queued || p.waiting);
  return {on, su: D.sched?.use_sum || 0, n: new Set(on.map(pkOf)).size,
          sw: pk => on.filter(p => pkOf(p) === pk).reduce((a, p) => a + p.w, 0)};
}
// 几个人在用卡（调度卡按人分组、排位解释写不写人都看它）：近期在用的人超过一个
const schedPeople = () => schedOn().n > 1;
function shareBar(p) {
  const {on, su, n, sw} = schedOn(), u = su ? 100 * p.use / su : 0, ws = sw(pkOf(p)), d = on.some(x => x.id === p.id) && ws ? 100 / n * p.w / ws : null;
  const split = n > 1 ? `${who(p)} 应得 1 ÷ ${n} 人，在这人近期在用的项目之间按权重分（${wtxt(p.w)} ÷ ${wtxt(ws)}）` : `${wtxt(p.w)} ÷ ${wtxt(ws)}`;
  return d == null ? `<span class="sm mut">近期没用卡</span>` : `<div class="row" style="flex-wrap:nowrap"><div class="bar thin" style="flex:1;min-width:90px" ${tip(
    `近期用量 ${g1(p.use)} 卡·时，占 ${Math.round(u)}%；权重 ${wtxt(p.w)}，应得 ${Math.round(d)}%（${split}）\n先比人：这人几个项目的用量合起来比别人小的先放；同一人的项目之间比用量 ÷ 权重（${g1(p.use / p.w)}），小的先\n竖线是应得的位置`)}><i style="width:${u}%;background:${color(p.id)}"></i><b class="due" style="left:${d}%"></b></div>
    <span class="sm num nw">${Math.round(u)}% <span class="mut">/ ${Math.round(d)}%</span></span></div>`;
}
// 一个人的「已用 / 应得」（调度卡按人分组时人那一行）：颜色条是这人名下各项目合起来的用量占比（puse：看板按完整的用量算，含已归档、卡上不列的项目，
// 与 worker 先比人时用的同一个数），竖线是 1 ÷ 近期在用的人数
function personBar(ps) {
  const {on, su, n} = schedOn(), use = ps[0].puse, u = su ? 100 * use / su : 0, d = ps.some(p => on.includes(p)) ? 100 / n : null;
  return d == null ? `<span class="sm mut">近期没用卡</span>` : `<div class="row" style="flex-wrap:nowrap"><div class="bar thin" style="flex:1;min-width:90px" ${tip(
    `${who(ps[0])} 名下近期用量合计 ${g1(use)} 卡·时（含已归档的项目），占 ${Math.round(u)}%；份额按人算，近期在用的 ${n} 人各应得 ${Math.round(d)}%\n竖线是应得的位置`)}><i style="width:${u}%;background:var(--muted)"></i><b class="due" style="left:${d}%"></b></div>
    <span class="sm num nw">${Math.round(u)}% <span class="mut">/ ${Math.round(d)}%</span></span></div>`;
}
// 共有型算力池：总览紧凑卡片（poolSummaryCard）与独立页（poolPage，#/pool）。数据来自全局快照的 D.pool（pool.tick / pool.summary）；
// 非池成员的 D.pool 为 null（不可见），普通池成员仅见所属池聚合与本人待放清单（不含 members 列表）。
function poolSummaryCard() {
  const P = D.pool;
  if (!P) return "";
  if (P.error) return `<div class="alerts"><div>⚠ 共有算力池配置错误：${esc(P.error)}</div></div>`;
  const ps = P.pools || [];
  if (!ps.length) return "";
  const rows = ps.map(p => {
    const nofit = Object.values(P.lists || {}).filter(v => v.pool === p.id && !v.done && v.nofit).length;
    return `<tr class="link" tabindex="0" data-go="#/pool">
      <td><b>${esc(p.label)}</b> <code class="sm">${esc(p.host_id)}</code> ${p.exclusive ? `<span class="badge b-mute" ${tip("独占模式：禁止对池内单台主机直接 worker add，请经池入口提交")}>独占</span>` : `<span class="badge b-ok" ${tip("开放模式：既可经池入口提交也可直投单台")}>开放</span>`}</td>
      <td class="r">${p.online} / ${p.total}</td>
      <td class="r">${p.free_gpus} / ${p.gpus}</td>
      <td class="r">${p.running} / ${p.queued}</td>
      <td class="r">${p.placed}</td>
      <td class="r">${p.pending}${nofit ? ` <span class="badge b-bad">${nofit} 放不下</span>` : ""}</td></tr>`;
  }).join("");
  return `<div class="card"><div class="chd wrap"><b>共有算力池</b><span class="sm mut">统一经 <code>labmon worker add pool</code>（或 <code>pool:&lt;id&gt;</code>）提交，按人公平份额即时落机与空闲窃取</span><span class="sp"></span><a class="sm" href="#/pool">详情 →</a></div>
    <div class="scroll"><table><tr><th>算力池</th><th class="r">在线主机</th><th class="r">空闲 / 总 GPU</th><th class="r">运行 / 排队</th><th class="r">已落机清单</th><th class="r">待放清单</th></tr>${rows}</table></div></div>`;
}

function poolPage() {
  const P = D.pool;
  if (!P) return `<div class="card empty">当前没有可用的共有型算力池（或你的账号不在任何共有池成员列表中）。</div>`;
  if (P.error) return `<div class="alerts"><div>⚠ 共有算力池配置错误：${esc(P.error)}</div></div>`;
  const ps = P.pools || [], lists = Object.values(P.lists || {}).filter(v => !v.done), now = D.generated;
  if (!ps.length) return `<div class="card empty">hosts.toml 中尚未配置共有型算力池（<code>[pools.&lt;id&gt;]</code>）。</div>`;
  const totOnline = ps.reduce((a, p) => a + (p.online || 0), 0), totHosts = ps.reduce((a, p) => a + (p.total || 0), 0);
  const totFree = ps.reduce((a, p) => a + (p.free_gpus || 0), 0), totGpus = ps.reduce((a, p) => a + (p.gpus || 0), 0);
  const totRun = ps.reduce((a, p) => a + (p.running || 0), 0), totQ = ps.reduce((a, p) => a + (p.queued || 0), 0);
  const totPend = lists.length, nofitN = lists.filter(v => v.nofit).length;
  let s = `<div class="kpis">
    <div class="kpi"><div class="k">共有算力池</div><div class="v">${ps.length}<small> 个</small></div><div class="s">${ps.map(p => esc(p.host_id)).join(" · ")}</div></div>
    <div class="kpi"><div class="k">在线主机</div><div class="v">${totOnline}<small> / ${totHosts} 台</small></div><div class="s">运行 ${totRun} · 排队 ${totQ}</div></div>
    <div class="kpi"><div class="k">空闲 GPU</div><div class="v">${totFree}<small> / ${totGpus} 张</small></div><div class="s">空闲主机自动触发空闲窃取</div></div>
    <div class="kpi"><div class="k">待放清单</div><div class="v" style="${nofitN ? "color:var(--bad)" : ""}">${totPend}<small> 条</small></div><div class="s">${nofitN ? `其中 ${nofitN} 条全池放不下` : "新提交清单下一轮即时落机"}</div></div></div>`;
  for (const p of ps) {
    const pLists = lists.filter(v => v.pool === p.id).sort((a, b) => (a.since || 0) - (b.since || 0));
    const memTag = Array.isArray(p.members)
      ? `<span class="sm mut">成员：${p.members.includes("*") ? "全部用户（*）" : esc(p.members.join("、") || "无")}</span>` : "";
    const modeTag = p.exclusive
      ? `<span class="badge b-mute" ${tip("独占模式：禁止对池内单台主机直接 worker add，请经池入口提交")}>独占 · 仅经 ${esc(p.host_id)}</span>`
      : `<span class="badge b-ok" ${tip("开放模式：既可经池入口提交也可直投单台")}>开放 · 允许直投</span>`;
    const hRows = (p.hosts || []).map(h => {
      const stBadge = h.returned ? `<span class="badge b-mute">已归还</span>`
        : !h.online ? `<span class="badge b-bad">离线</span>`
        : h.drain ? `<span class="badge b-warn">drain</span>`
        : h.deadline ? `<span class="badge b-warn">预备续租</span>`
        : `<span class="badge b-ok">在线</span>`;
      const go = D.hosts.some(x => x.id === h.id) ? ` class="link" tabindex="0" data-go="#/host/${encodeURIComponent(h.id)}"` : "";
      return `<tr${go}><td><b>${esc(h.label || h.id)}</b> <span class="sm mut">${esc(h.id)}</span></td>
        <td>${stBadge}</td><td class="r">${h.online ? `${h.free_gpus} / ${h.gpus}` : "—"}</td>
        <td class="r">${h.online ? h.running : "—"}</td><td class="r">${h.online ? h.queued : "—"}</td>
        <td class="sm nw">${h.lease_end ? leaseHtml(h.lease_end, "租期") : "—"}</td></tr>`;
    }).join("") || `<tr><td colspan="6" class="empty">这个池里还没有配置主机。</td></tr>`;
    const lRows = pLists.map(v => {
      const link = D.projects.some(x => x.id === v.project) ? v.project : null;
      const st = v.nofit ? `<span class="badge b-bad">全池放不下</span> ${esc(v.why || "")}` : esc(v.why || "等待下一轮落机");
      return `<tr><td>${projCell({project: v.project, link})}</td><td class="sm"><code>${esc(v.root)}/<b>${esc(v.jobs)}</b></code></td>
        <td class="sm">${esc(v.head || "—")}</td><td class="r sm">${v.n || "—"}</td>
        <td class="r sm nw">${v.since ? dur(now - v.since) : "—"}</td><td class="sm mut">${st}</td></tr>`;
    }).join("");
    s += `<div class="card"><div class="chd wrap"><b>${esc(p.label)}</b><code class="sm">${esc(p.host_id)}</code>${modeTag}${memTag}<span class="sp"></span>
      <span class="sm mut">在线 ${p.online}/${p.total} 台 · 空 ${p.free_gpus}/${p.gpus} 卡 · 已落机 ${p.placed} 条 · 待放 ${p.pending} 条</span></div>
      <div class="scroll"><table><tr><th>主机</th><th>状态</th><th class="r">空闲 / 总 GPU</th><th class="r">运行</th><th class="r">排队</th><th>租期</th></tr>${hRows}</table></div>
      ${pLists.length ? `<div class="sub">待放清单（${pLists.length} 条）</div><div class="scroll"><table><tr><th>项目</th><th>清单</th><th>队首</th><th class="r">待跑</th><th class="r">已等</th><th>状态 / 原因</th></tr>${lRows}</table></div>`
        : `<div class="sm mut" style="margin-top:8px">当前没有待放清单。提交命令：<code>labmon worker add ${esc(p.host_id)} ROOT JOBS --project &lt;项目&gt;</code></div>`}</div>`;
  }
  return s + schedCard();
}

function schedCard() {
  const S = D.sched;
  if (!S) return "";
  const ps = S.projects, {on} = schedOn(), fo = ps.filter(p => p.focus), pa = ps.filter(p => p.paused), many = schedPeople();
  // 按人分组（#23）：pk 相同的排在一起，组按这人近期用量合计从小到大（与放行的先后一致），组内照原顺序
  const groups = [];
  for (const p of ps) { const g = groups.find(g => pkOf(g[0]) === pkOf(p)); g ? g.push(p) : groups.push([p]); }
  const gUse = g => g[0].puse;
  if (many) groups.sort((a, b) => gUse(a) - gUse(b));
  const list = g => (g.some(p => on.includes(p)) ? g.filter(p => on.includes(p)) : g).map(p => `${esc(pname(p.id))} ×${wtxt(p.w)}`).join("、");
  // 标题行只留短句（谁优先、按份额轮流、谁暂停），算法细节在旁边的 ⓘ 里
  const rule = [fo.length ? `${fo.map(p => esc(pname(p.id) + flagText("focus", p))).join("、")} 优先${many ? "（在各自主人的项目里）" : ""}` : "",
    many ? `按人轮流（${groups.filter(g => g.some(p => on.includes(p))).map(g => `${esc(who(g[0]))}：${list(g)}`).join("；")}）`
      : `${fo.length ? "其余" : ""}按份额轮流（${(on.length ? on : ps).map(p => `${esc(pname(p.id))} ×${wtxt(p.w)}`).join("、")}）`,
    pa.length ? `${pa.map(p => esc(pname(p.id) + flagText("paused", p))).join("、")} 暂停中` : ""].filter(Boolean).join("；");
  const how = `份额按人算：先比人，同一人的几个项目的近期 GPU 用量合起来算一份，小的先放；同一人的项目之间优先的先，再比用量 ÷ 权重，小的先（用量是所有主机上的卡·时，${wtxt(S.hl_h)} 小时减半）。卡在资源上等满 ${wtxt(S.cap_min)} 分钟的成为屏障，后面只放它放行前跑得完的。`;
  const edit = ps.some(p => canWrite(p.id));
  const row = p => {
    const now = [p.cards ? `占 ${wtxt(p.cards)} 张卡` : "", p.queued ? `排队 ${p.queued}` : "", p.waiting ? `${p.waiting} 个在等` : ""].filter(Boolean).join(" · ");
    const known = D.projects.some(x => x.id === p.id);
    return `<tr><td><span class="nw"><span class="dot" style="background:${color(p.id)}"></span> ${known ? `<a href="#/p/${encodeURIComponent(p.id)}">${esc(pname(p.id))}</a>` : esc(p.id)}</span>
        ${p.focus ? ` <span class="badge b-ok" ${tip(flagTip("focus", p))}>优先${p.focus.hosts ? "（部分主机）" : ""}</span>` : ""}${p.paused ? ` <span class="badge b-mute" ${tip(flagTip("paused", p))}>暂停中${p.paused.hosts ? "（部分主机）" : ""}</span>` : ""}</td>
      <td class="nw">${schedW(p)}</td><td style="min-width:170px">${shareBar(p)}</td><td class="sm">${now || `<span class="mut">—</span>`}</td>
      <td class="sm nw" ${p.wait_n ? tip(`近 1 天开始的 ${p.wait_n} 个任务，从排到队首（单元：申请）到放行平均等的时间`) : ""}>${p.wait_avg == null ? `<span class="mut">—</span>` : dur(p.wait_avg * 60)}</td>
      ${edit ? `<td class="r nw">${canWrite(p.id) ? schedBtns(p) : ""}</td>` : ""}</tr>`;
  };
  const head = g => `<tr class="sch-pp"><td><b>${esc(who(g[0]))}</b> <span class="sm mut">${g.length} 个项目</span></td><td></td><td style="min-width:170px">${personBar(g)}</td>
      <td class="sm mut" colspan="${edit ? 3 : 2}">合计 ${g1(gUse(g))} 卡·时</td></tr>`;
  return `<div class="card"><div class="chd wrap"><b>调度</b><span class="sm mut">${rule}
      <span class="ii" ${tip(how + "\nworker 每轮按这套规则放行，推演也用它（排位与预计开始见任务卡的等待）。不抢占：正在跑的不停。\n权重、优先与暂停由你定：都写在主机清单的 [sched]（[sched.weight]、[sched.focus]、[sched.pause]），所有主机生效、含之后加入的（同不带 --host 的 labmon focus / pause）。")}>ⓘ</span></span></div>
    ${SMSG ? `<div class="rn-state">${esc(SMSG)}</div>` : ""}
    <div class="scroll"><table><tr><th>项目</th><th ${tip("同一人的项目之间，用量除以它再比；没写的为 1")}>权重</th><th ${tip("颜色条：近期用量（衰减后的卡·时）占的比例；竖线：应得的比例（份额按人算，再按权重分给这人的项目）")}>已用 / 应得</th><th>此刻</th><th>平均等</th>${edit ? "<th></th>" : ""}</tr>
    ${many ? groups.map(g => head(g) + g.map(row).join("")).join("") : ps.map(row).join("")}</table></div></div>`;
}
// 调度的写回（POST /sched/<what>）：回执写进 SMSG（smsg），1 分钟后收起
let SMT = 0;
const smsg = m => { SMSG = m; clearTimeout(SMT); SMT = setTimeout(() => { SMSG = null; render(false); }, 60000); };
async function schedPost(what, body) {
  const r = await postJSON(`sched/${what}`, body);
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || "HTTP " + r.status);
  smsg(j.msg);
}
async function schedAct(act, pid) {
  if (!canWrite(pid)) return;
  const P = POP;
  if (act === "wd" || act === "wu") {
    const p = D.sched?.projects.find(x => x.id === pid), cur = p && swCur(p), to = p && wStep(cur ? cur.w : p.w, act === "wd" ? -1 : 1);
    if (to == null || cur?.post) return;
    const e = {w: to, post: true, t: Date.now()}; SWB.set(pid, e); render(false);
    try {
      await schedPost("weight", {project: pid, weight: to});
    } catch (err) {
      smsg("改权重没成：" + (err.message || err));
      if (SWB.get(pid) === e) cur ? SWB.set(pid, cur) : SWB.delete(pid);      // 退回上一次写成功的值（没有就按快照）
      return render(false);
    }
    e.post = false; e.t = Date.now();
    return collectNow(true);
  }
  if (act === "f") popToggle("sf", pid, whenInit());
  else if (act === "uf") popToggle("su", pid);
  else if (act === "p") popToggle("sp", pid, whenInit());
  else if (act === "ur") popToggle("sr", pid);
  else {
    if (act === "fgo" && whenStop("schu", P.until, P.wnow)) return;
    if (act === "pgo" && whenStop("schp", P.until, P.wnow)) return;
    const body = act === "fgo" || act === "pgo" ? {project: pid, until: whenT(P.until)} : {project: pid};
    P.busy = true; P.err = null; render(false);
    try {
      await schedPost({fgo: "focus", ufgo: "unfocus", pgo: "pause", urgo: "resume"}[act], body);
      POP = null; whenBlur();
      return collectNow(true);
    } catch (e) { if (POP) { POP.busy = false; POP.err = e.message || String(e); } }
  }
  render(false);
}
// 等待表的排位解释：worker 的 admit 逐条比的（fair_key：这人的用量、同一人里的优先、用量 ÷ 权重、先来先跑；屏障），同一台主机上排在它前面的是谁
function rankTip(x, ws) {
  if (!x.pos) return x.held ? "被临时指令挡住，不参与排队" : x.dep ? "依赖还没满足，不参与排队（满足后从那时起排）" : "";
  const P = x.pos, cap = D.sched?.cap_min ?? 30, same = ws.filter(y => y.h === x.h && y.pos), prev = same.find(y => y.pos.n === P.n - 1);
  const sp = y => D.sched?.projects.find(p => p.id === y.project), w = sp(x)?.w ?? 1, many = schedPeople();
  const mate = prev && pkOf(sp(prev) || {}) === pkOf(sp(x) || {});      // 前面那个与它同一人（或都不知道是谁）
  const ahead = !prev ? "没有排在它前面的" : !mate && (prev.pos.puse ?? 0) < (P.puse ?? 0) - 1e-9 ? `前面的 ${pname(prev.project)} 是${who(sp(prev) || {})}的，这人近期用得更少（${g1(prev.pos.puse)} < ${g1(P.puse)} 卡·时）`
    : prev.pos.focus && !P.focus ? `前面的 ${pname(prev.project)} 优先`
    : prev.project === x.project ? `前面的 ${prev.name} 是同一项目、先来的` : prev.pos.ratio < P.ratio ? `前面的 ${pname(prev.project)} 是 ${prev.pos.ratio}，更小` : `前面的 ${pname(prev.project)} 也是 ${prev.pos.ratio}，它先来`;
  return [`${hostShort(x.h)} 上在等的 ${same.length} 个里排第 ${P.n}`,
    ...(many && P.puse != null ? [`先比人：${who(sp(x) || {})}名下几个项目的用量合计 ${g1(P.puse)} 卡·时，比别人小的先`] : []),
    P.focus ? `${pname(x.project)} 优先（focus），排在${many ? "同一人" : ""}不优先的项目前面` : "没有优先（focus）",
    `轮到它时 ${pname(x.project)} 的用量 ÷ 权重 = ${P.ratio}（权重 ${wtxt(w)}）；${ahead}`,
    `已等 ${x.since ? dur(D.generated - x.since) : "—"}` + (P.barrier ? `，等满 ${cap} 分钟又卡在资源上，成了屏障：后面的只放它放行前跑得完的`
      : P.behind ? `；${pname(P.behind[0])} 的 ${P.behind[1]} 等满 ${cap} 分钟成了屏障，这个在它放行前跑不完，让它先` : D.generated - x.since < cap * 60 ? `，还没等满 ${cap} 分钟` : ""),
    `在等：${x.why}`].join("\n");
}

// 记录（overview.records，经 /records.json 取）：每种事件的中文名、色调与分类见 overview.py 的 EVENT_KINDS，随快照带来
const evk = k => D.event_kinds?.[k] || [k, "mute", "queue"];
const RECS = {};                   // 记录片段：查询串 → {rows, more, gen, busy, err}；全局页采集了新的一轮才重取
const recQ = (f, n) => new URLSearchParams({...f, limit: n}).toString();
function recs(q) {
  if (!LIVE) return {rows: [], off: true};
  const c = RECS[q];
  if (!c || (c.gen !== D.generated && !c.busy)) loadRecs(q);
  return c || {rows: null};
}
async function loadRecs(q) {
  RECS[q] = {...(RECS[q] || {}), busy: true};
  try {
    const r = await api("records.json?" + q, {cache: "no-store"}, {retry: true});
    if (!r.ok) throw new Error("HTTP " + r.status);
    RECS[q] = {...await r.json(), gen: D.generated};
  } catch (e) { RECS[q] = {rows: RECS[q].rows || [], err: "记录读不了：" + (e.message || e), gen: D.generated}; }
  render(false);
}
// 一段记录（总览的记录卡、主机页、服务器与人员的抽屉）：最近 n 条，末尾链到记录页的同一筛选
function recSlice(f, n, empty = "还没有记录。") {
  const c = recs(recQ(f, n)), qs = new URLSearchParams(f).toString();
  if (c.off) return `<div class="empty">记录要经看板服务（labmon serve）打开才看得到。</div>`;
  if (!c.rows) return `<div class="empty">读取中…</div>`;
  return (c.rows.length ? recList(c.rows, {showHost: !f.host}) : `<div class="empty">${esc(empty)}</div>`)
    + `<div class="sm" style="margin-top:6px"><a href="#/log${qs ? "?" + qs : ""}">全部记录 →</a>${c.err ? ` <span class="rn-err">${esc(c.err)}</span>` : ""}</div>`;
}
const recNote = (f, n) => { const r = recs(recQ(f, n)).rows; return r?.length ? tsHM(r[0].t) : r ? "无" : "…"; };
// 一串记录（记录页、总览的记录卡、主机页、抽屉里的片段同用）。合成事：一次平台操作（开始到完成或停下；平台操作一次只做一个）
// 连同期间与结束后 5 分钟里这几台的连接、平台类记录合成一条；挨着的同一类、同一台的合成一条（×N）。点 ×N 展开
const RCAT = {link: "wifi", op: "cloud", config: "gear", lease: "hourglass", keepalive: "shield", disk: "db", sched: "zap", queue: "list", board: "dash"};
const REXP = new Set();            // 展开着的合并记录（按其中最新一条的 rowKey）
const hostsOf = e => e.hosts || (e.host ? [e.host] : []);
function recGroup(rows) {
  const ops = [], inOp = new Map(), out = [];
  for (let i = rows.length - 1; i >= 0; i--) {                       // 从旧到新：开始配上随后的完成或停下
    const e = rows[i], o = ops.at(-1);
    if (e.kind === "op_start") ops.push({start: e, end: null, rows: []});
    else if ((e.kind === "op_done" || e.kind === "op_fail") && o && !o.end) o.end = e;
  }
  ops.forEach((o, i) => {
    const hs = hostsOf(o.start), t1 = o.end ? o.end.t + 300 : ops[i + 1]?.start.t ?? Infinity;
    for (const e of rows) {
      const mine = e === o.start || e === o.end || !inOp.has(e) && !e.kind.startsWith("op_") && e.t >= o.start.t && e.t <= t1
        && (e.kind === "login" ? e.t <= (o.end?.t ?? t1) : ["link", "op"].includes(evk(e.kind)[2]) && hostsOf(e).length && hostsOf(e).every(h => hs.includes(h)));
      if (mine) { inOp.set(e, o); o.rows.push(e); }
    }
  });
  for (const e of rows) {
    const o = inOp.get(e);
    if (o) { if (e === (o.end || o.start)) out.push({op: o}); continue; }
    const p = out.at(-1), k = e.report_id || e.kind + "|" + hostsOf(e).join(",");
    if (p?.k === k && new Date(p.rows[0].t * 1000).toDateString() === new Date(e.t * 1000).toDateString()) p.rows.push(e);
    else out.push({k, rows: [e]});
  }
  return out;
}
// 这条记录说的是哪台：组内服务器取当时的使用人（who），自用主机取标签
function recName(id, e) {
  const h = D.hosts.find(x => x.id === id), x = D.fleet && [...D.fleet.servers, ...D.fleet.mine].find(v => v.id === id);
  return h ? h.label : x ? (e?.who || x.holder) : id;
}
// 正文：只说一台的，去掉正文开头重复的「名字（id）」（记录页前面有它的小签，主机页、抽屉里本来就是它）
function recText(e) {
  const hs = hostsOf(e);
  if (hs.length !== 1) return e.msg;
  const x = D.fleet && [...D.fleet.servers, ...D.fleet.mine].find(v => v.id === hs[0]), h = D.hosts.find(v => v.id === hs[0]);
  const ns = [hs[0], e.who, x?.holder, h?.label].filter(Boolean).map(n => n.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  const t = e.msg.replace(new RegExp(`^(?:${ns.join("|")})(?:（[^）]*）)?\\s*`), "");
  return t || e.msg;
}
// 点一行去哪：自用主机去主机页，组内服务器开它的抽屉，保活守护的设置开守护抽屉，人员的改动开这人的抽屉；已经在那儿了就不链
function recTarget(e) {
  const hs = hostsOf(e), id = hs[0];
  let h = null;
  if (hs.length === 1) h = D.hosts.some(x => x.id === id) ? "#/host/" + encodeURIComponent(id) : D.fleet?.servers.some(x => x.id === id) ? DH.srv(id) : null;
  else if (!hs.length) h = evk(e.kind)[2] === "keepalive" || /保活守护/.test(e.msg) ? DH.guard() : e.who && fops().people?.[e.who] ? DH.person(e.who)
    : e.project && D.projects.some(p => p.id === e.project) ? "#/p/" + encodeURIComponent(e.project) : null;
  return h && h !== location.hash ? h : null;
}
const recNav = h => !h ? "" : `${route().kind === "fleet" && h.startsWith("#/fleet/") ? "data-drw" : "data-go"}="${esc(h)}" tabindex="0" role="link"`;
function recList(rows, o = {}) {
  const tm = t => o.day ? tsHM(t) : ts(t);
  const chips = e => o.showHost ? hostsOf(e).map(id => `<button class="rchip" data-rf="${esc(id)}" ${tip(`只看 ${recName(id, e)}（${id}）的记录`)}>${esc(recName(id, e))}</button>`).join("") : "";
  const proj = e => e.project ? projCell({...e, link: D.projects.some(p => p.id === e.project) ? e.project : null}) : "";
  const kids = rs => rs.map(e => { const [k, c] = evk(e.kind);
    return `<span class="nw">${tm(e.t)}</span><span class="k" style="color:var(--${c === "mute" ? "muted" : c})">${e.msg.startsWith(k) ? "" : esc(k)}</span><span>${esc(e.msg)}${e.detail ? `<span class="mut"> · ${esc(e.detail)}</span>` : ""}</span>`; }).join("");
  const row = r => {
    const open = r.kids && REXP.has(r.key), go = recNav(r.target);
    return `<div class="rr ${r.tone}${r.tone === "mute" ? " dim" : ""}" ${go}><span class="rr-tm">${r.time}</span><span class="rr-ic">${icon(RCAT[r.cat] || "list")}</span>
      <div class="rr-b"><div class="rr-m">${r.label ? `<span class="rr-k">${esc(r.label)}</span>` : ""}${r.chips}<span class="rr-x">${r.text}</span>${r.badge || ""}${r.kids
        ? `<button class="rr-n${open ? " on" : ""}" data-rexp="${esc(r.key)}" aria-expanded="${open}">${r.more}${icon("chev")}</button>` : ""}</div>
      ${r.detail ? `<div class="rr-d">${esc(r.detail)}</div>` : ""}${open ? `<div class="rr-kids">${r.kids}</div>` : ""}</div>${go ? icon("chevr") : ""}</div>`;
  };
  return `<div class="rl${o.day ? "" : " long"}">` + recGroup(rows).map(it => {
    if (it.op) {
      const {start, end, rows: rs} = it.op, name = start.msg.replace(/^开始/, "").split("：")[0], [lab, tone] = end ? evk(end.kind) : ["进行中", "warn"];
      const tail = end ? end.msg.slice(end.msg.indexOf(name) + name.length).replace(/^（[^）]*）/, "").replace(/^完成：?/, "") : "";
      return row({tone, cat: "op", label: name, chips: chips(start), text: "", badge: `<span class="badge b-${tone}">${esc(lab)}</span>`, detail: tail,
                  time: tm(start.t), key: rowKey(end || start), target: recTarget(start), more: `${rs.length} 条`, kids: kids([...rs].reverse())});
    }
    const e = it.rows[0], [lab, tone, cat] = evk(e.kind), text = recText(e), n = it.rows.length;
    return row({tone, cat, label: text.startsWith(lab) ? "" : lab, chips: chips(e), text: proj(e) + (e.project ? " " : "") + esc(text), detail: [e.occurred != null ? `发生于 ${ts(e.occurred)}` : "", e.detail].filter(Boolean).join("\n"),
                time: tm(e.t), key: rowKey(e), target: recTarget(e),
                ...(n > 1 ? {more: `×${n} · ${tm(it.rows.at(-1).t)} 起`, kids: kids(it.rows)} : {})});
  }).join("") + `</div>`;
}

/* ---------- 平台续租：预备 → 跑完这格 → 续租（与组内服务器同一条操作：fopAct("renewone")，进度、验证码都用组内的） ---------- */
let RERR = {};                     // 按主机的上次预备、取消预备出错
// 这台主机正在续租或续租结束后还没点「知道了」的组内操作（续租、继续）
const renewOp = id => { const op = fops().op; return op?.steps && ["renew", "ka"].includes(op.kind) && (op.ids || []).includes(id) ? op : null; };
const RICON = {wait: "○", run: "…", ok: "✓", err: "✕"};
// 运行中任务的一行：进度、已跑多久、预计结束（项目看板按日志算的）
// 整台主机的操作（预备续租、续租、跑完就还、现在停）：这台上有几个项目在跑或在等时，写明都受影响
function affects(h) {
  const ps = [...new Set([...(h?.status?.running || []), ...(h?.status?.waiting || [])].map(j => j.project).filter(Boolean))];
  return ps.length > 1 ? `<div class="pop-w">管整台主机：${ps.map(p => esc(projName(p, {plain: true}).trim())).join("、")} 都受影响</div>` : "";
}
function jobLine(j) {
  const p = j.prog?.progress, t0 = j.t0, eta = j.prog?.eta_ts;
  return `${projName(j.project)}<b>${esc(j.name)}</b>${p ? ` ${p[0]}/${p[1]}` : ""}${t0 ? `，已跑 ${dur(D.generated - t0)}` : ""}${eta ? `，预计 ${tsHM(eta)}` : ""}`;
}
const CAP_OLD = 180;               // 验证码取来超过这么多秒就提示可能过期
const capAge = () => { const s = (Date.now() - FCAP.t) / 1000; return s < 60 ? "刚取的" : `${Math.floor(s / 60)} 分钟前取的` + (s > CAP_OLD ? "，可能已过期：点验证码换一张" : ""); };
setInterval(() => { const el = document.getElementById("capage"); if (el && FCAP) { el.textContent = capAge(); el.classList.toggle("rn-err", (Date.now() - FCAP.t) / 1000 > CAP_OLD); } }, 10000);
// 缺本地文件又补不了的等待项（worker 等待项的 miss）：提醒、概况条与本地依赖表都按它认
const missW = h => (h.status?.waiting || []).filter(w => w.miss);
// 本地依赖续租、换容器后怎么补（worker 的 local.jobs 的 how：copy 从共享盘主副本拷 / redo 命令自己重算 / mixed 两种都有 / null 补不了）
// 拷贝按共享盘约 100 MB/s 估时
const healText = rs => { const how = new Set(rs.map(r => r.heal ? r.how || "redo" : "fail")), b = rs.reduce((s, r) => s + (r.how === "copy" ? r.bytes : 0), 0);
  return how.size > 1 || how.has("mixed") ? "拷贝与重算" : how.has("copy") ? `从共享盘拷，约 ${dur(b / 1e8)}` : how.has("redo") ? "能自愈：重算" : "会失败"; };
// 续租会重启容器、清空本地盘：worker 扫出的待跑任务依赖的本地文件（会失败的 / 有主副本拷回的 / 命令能自己补的）
function lossNote(h) {
  const rows = h.status?.local?.jobs || [];
  if (!rows.length) return "";
  const bad = rows.filter(r => !r.heal), cp = rows.filter(r => r.how === "copy"), ok = rows.filter(r => r.heal && r.how !== "copy");
  const nm = rs => rs.slice(0, 3).map(r => `<b>${esc(r.job)}</b>`).join("、") + (rs.length > 3 ? ` 等 ${rs.length} 格` : "");
  const dirs = [...new Set(bad.flatMap(r => r.files.map(f => f.replace(/\/[^/]*$/, ""))))].slice(0, 3).map(esc).join("、");
  return (bad.length ? `<div class="rn-state bad">续租会重启容器、清空本地盘：${nm(bad)} 要用的本地文件（${dirs}）会丢，这些任务会失败。
      续租完先不恢复放行，等你处理。最好提前让命令能自己补（先 <code>[ -f 文件 ] ||</code> 重新生成），或在共享盘留主副本（needs=本地&lt;主副本）。</div>` : "")
    + (cp.length ? `<div class="rn-state">${nm(cp)} 的本地文件会丢，共享盘有主副本，续租后放行前拷回来（${healText(cp)}）。</div>` : "")
    + (ok.length ? `<div class="rn-state">${nm(ok)} 的本地文件会丢，命令能自己补，续租后重新生成（要多花些时间）。</div>` : "");
}
function renewChip(h) {
  const run = renewOp(h.id), r = h.renew;
  const why = r ? `${r.auto ? "已自动预备" : "已预备"}：${r.why || ""}${r.deadline && r.mode !== "deadline" ? `\n截止 ${ts(r.deadline)}（到期前 1 小时）` : ""}` : "";
  return run?.active ? `<span class="badge b-warn">续租中</span>` : run?.error ? `<span class="badge b-bad">续租停下</span>`
    : r?.state === "held" ? `<span class="badge b-warn" ${tip(r.why || "")}>待恢复放行</span>`
    : r?.state === "ready" ? `<span class="badge b-warn" ${tip(r.why || "")}>可续租</span>` : r ? `<span class="badge b-mute" ${tip(why)}>预备续租</span>`
    : h.auto_res?.off ? `<span class="badge b-mute" ${tip("这一期在自动预备的时段里取消过预备，到期前不再自动预备；要预备就点「预备续租」")}>不自动预备</span>` : "";
}
const RENEW_TIP = "停止作业、克隆重开、拉起 worker；作业已被平台停掉的直接重开。48 小时内登录过平台就不用再输验证码";
// 续租一台（主机页与组内服务器抽屉共用）：点了先在按钮旁确认，会中断在跑的要勾上。会中断是红框，可以续租了（primary）是主色
const goRenew = (x, primary = false) => !x ? "" : opPop("renew", x,
  `<button class="btn ${FRISKY(x) ? "danger" : primary ? "pri" : ""}" data-fop="renewone" data-id="${esc(x.id)}" ${fops().op?.active ? "disabled" : ""} ${tip(RENEW_TIP)}>${icon("renew")}续租</button>`,
  () => renewAsk(x),
  () => `<button class="btn pri${FRISKY(x) ? " dz" : ""}" data-fop="renew1" data-id="${esc(x.id)}" ${FRISKY(x) && !POP.ack || fops().op?.active ? "disabled" : ""}>${FBUSY(x) ? "中断并续租" : "续租"}</button>`);
// 续租一台的确认（分层见 riskSay）：自用主机补不了的本地文件也进结论，明细（lossNote）与管整台主机的项目折在「细节」里
function renewAsk(x) {
  const h = x.mine ? D.hosts.find(e => e.id === x.id) : null, fatal = (h?.status?.local?.jobs || []).filter(r => !r.heal);
  return `<div class="rk-say"><b>续租${myFleetHost(x) ? "" : " " + esc(x.holder)}</b>：${esc(riskSay(x, fatal.length ? [`丢掉 ${fatal.length} 格待跑任务要的本地文件`] : []))}</div>`
    + riskNeed(x, "续租", "data-pack", POP.ack)
    + riskMore(`<div>停旧作业 → 提交新作业、等运行 → ${x.mine ? "等 ssh、拉起 worker（ssh 断开约 2 分钟）" : "ssh 上去开保活"}。开始前会再${x.mine ? "看" : "探"}一次，在跑而这里没确认的就不动。</div>`
      + renewWarn(x, {local: false, risk: false}).map(t => `<div class="pop-w">${esc(t)}</div>`).join("") + (h ? affects(h) + lossNote(h) : ""));
}
// 自用主机的续租按钮（概况条）：预备续租 ↔ 取消预备原地换，续租；续租完保持不放行时只有「恢复放行」。
// 预备续租先在按钮旁确认；取消预备一点就生效，只有在自动预备的时段里（取消后这一期不再自动预备）才先确认
function renewBtns(h) {
  if (h.platform_kind && h.platform_kind !== "scm") return "";
  if (!canOps()) return opsNote();
  const r = h.renew, id = esc(h.id), dis = fops().op?.active ? "disabled" : "";
  const rb = (act, cls, ic, text, hint = "") => `<button class="btn ${cls}" data-renew="${act}" data-host="${id}" ${dis} ${hint ? tip(hint) : ""}>${ic ? icon(ic) : ""}${text}</button>`;
  const pb = (k, cls, ic, text, hint) => `<button class="btn ${cls}" data-fop="rpop" data-kind="${k}" data-id="${id}" ${dis} ${tip(hint)}>${icon(ic)}${text}</button>`;
  if (r?.state === "held") return rb("cancel", "pri", "play", "恢复放行", `${r.why ? r.why + "\n" : ""}撤掉 drain，worker 照常放行排队任务`);
  const run = (h.status?.running || []).filter(j => !j.hold);     // 暂停占位不等（同 renew.py）
  if (!r) return popConfirm(pb("reserve", "", "hourglass", "预备续租", "不再放新任务，正在跑的跑完后开保活，到时通知你续租"), popIs("reserve", h.id),
      `<b>预备续租</b>：不再放新任务，${run.length ? "下面这些跑完后" : "现在没有任务在跑，"}开保活、通知你来续租；离到期不到 1 小时就不再等。可以取消。`
      + (run.length ? `<ul class="rn-jobs">${run.map(j => `<li>${jobLine(j)}</li>`).join("")}</ul>` : "") + affects(h) + lossNote(h)
      + (h.lw?.risk ? `<div class="pop-w">${esc(lwText(h.lw))}</div>` : ""),
      rb("reserve", "pri", "", "预备续租")) + goRenew(srvById(h.id));
  const late = h.auto_res && D.generated >= h.auto_res.at;
  const undo = late ? popConfirm(pb("cancel", "undo", "undo", "取消预备", "撤掉预备，恢复放行新任务"), popIs("cancel", h.id),
      `撤掉预备，恢复放行新任务。这一期不再自动预备：${h.lease_end ? ts(h.lease_end) + " " : ""}到期时还在跑的会随作业被平台停掉。`, rb("cancel", "pri", "", "取消预备"))
    : rb("cancel", "undo", "undo", "取消预备", "撤掉预备时下的指令，恢复放行新任务");
  return undo + goRenew(srvById(h.id), r.state === "ready");
}
// 续租卡：只在续租进行中、停下了、在面板里输验证码时出现（进度与「继续」）；按钮都在概况条里
function renewCard(h) {
  if (!h.platform || (h.platform_kind && h.platform_kind !== "scm")) return "";
  const run = renewOp(h.id), P = fPanel(srvById(h.id) || {id: h.id});
  if (!P.h) return "";
  const lease = h.lease_end ? `${ts(h.lease_end)} 到期（${leaseRem(h.lease_end)}）` : "租期未知";
  const k = P.h.lastIndexOf('<div class="rn">'), go = `<button class="btn pri" data-fop="ka" data-id="${esc(h.id)}" ${tip("只等 ssh、拉起 worker，不碰平台")}>${icon("renew")}继续</button>`;
  const body = run?.error && run.servers?.[h.id]?.up && k >= 0 ? P.h.slice(0, k + 16) + go + P.h.slice(k + 16) : P.h;     // 「继续」放在「知道了」前面
  return `<div class="card"><div class="chd"><b style="display:inline-flex;align-items:center;gap:6px">${icon("cloud")}平台续租</b><span class="sm mut">${lease}</span></div>${body}</div>`;
}
async function renewAct(act, id) {               // 预备续租、取消预备（续租本身走组内操作）
  delete RERR[id];
  try {
    const r = await postJSON(`renew/${encodeURIComponent(id)}/${act}`);
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.error || "HTTP " + r.status);
    return collectNow(true);
  } catch (e) { RERR[id] = e.message; render(false); }
}
