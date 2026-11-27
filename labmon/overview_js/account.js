/* ---------- 账号（#160）：「我的账号」#/me（登录了都有）、组内服务器的人员抽屉（管理员）。
   数据现取 /api/me、/api/users（不进快照，进页时取一次，动了以后重取）；口令只在表单里，令牌原文与设置码只显示这一次（关掉、离开这页就没了） ---------- */
const ACC = {me: null, users: null, at: {}, load: {}, loadError: {}, busy: false, msg: null, err: null, show: null, view: null, visit: 0};
// show：刚拿到、只显示这一次的 {page, k: "tok" | "code", where, name, v, until}；msg、err：{k: 显示在哪, m}
const accAv = (n, cls = "") => `<span class="uav${cls}" aria-hidden="true">${esc(pabbr(n))}</span>`;
function uaName(ua) {                                  // 会话的浏览器写成「Safari · macOS」，全文在悬停里
  const s = ua || "", b = /Edg\//.test(s) ? "Edge" : /Firefox\//.test(s) ? "Firefox" : /Chrome\//.test(s) ? "Chrome" : /Safari\//.test(s) ? "Safari" : s ? "其他客户端" : "浏览器";
  const o = /iPhone/.test(s) ? "iPhone" : /iPad/.test(s) ? "iPad" : /Android/.test(s) ? "Android" : /Mac OS X|Macintosh/.test(s) ? "macOS" : /Windows/.test(s) ? "Windows" : /Linux/.test(s) ? "Linux" : "";
  return o ? `${b} · ${o}` : b;
}
const accLine = k => (ACC.msg?.k === k ? `<div class="rn-state">${esc(ACC.msg.m)}</div>` : "") + (ACC.err?.k === k ? `<div class="rn-err">${esc(ACC.err.m)}</div>` : "");
function accLeave(r) {
  const directory = r.kind === "fleet" && (r.tab === "people" || r.drw?.k === "person");
  const view = directory || r.kind === "me" ? location.hash : null;
  if (ACC.view === view) return;
  ACC.view = view; ACC.visit++;
  ACC.show = ACC.msg = ACC.err = null;
  if (POP?.k.startsWith("acc-")) popClose();
  if (!directory) delete ACC.at.users;
  if (r.kind !== "me") delete ACC.at.me;
}
const ACC_LAN = "账号管理只在内网入口：从内网打开看板再管理账号";
const accLan = () => ME?.entry === "lan";
// 账号列表读不了的那句：公网入口的提示不标红、不给重试
const accErr = (e, retry = true) => !e ? "" : e === ACC_LAN ? `<p class="sm mut">${esc(e)}</p>`
  : `<p class="rn-err">${esc(e)}${retry ? ' <button class="lnk" data-acc="reload">重试</button>' : ""}</p>`;
async function accLoad(k) {                            // k：me、users
  if (ACC.load[k]) return;
  ACC.at[k] = Date.now();
  if (k === "users" && !accLan()) { ACC.users = null; ACC.loadError.users = ACC_LAN; return; }   // 公网入口没有 /api/users（#17）：不发请求，直接说
  ACC.load[k] = true;
  try {
    const r = await api(k === "me" ? "api/me" : "api/users", {cache: "no-store"}, {retry: true}), j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.error || "HTTP " + r.status);
    ACC[k] = j;
    if (k === "me") ME = j;                            // 角色变了（被撤了管理员）：侧栏、按钮跟着变
    ACC.loadError[k] = null;
  } catch (e) { ACC[k] = null; ACC.loadError[k] = "读不了：" + (e.message || e); }
  ACC.load[k] = false;
  render(false);
}
async function accPost(path, body, k, reload, after) {
  document.activeElement?.blur?.();                    // 回车提交时焦点还在输入框里：不失焦的话 render 一直被 editing 挡住
  const popup = POP, visit = ACC.visit;
  ACC.busy = true; ACC.err = ACC.msg = null; render(false);
  let ok = false;
  try {
    const r = await postJSON("api/" + path, body), j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.error || "HTTP " + r.status);
    ok = true;
    if (visit === ACC.visit && (!popup || POP === popup)) {
      if (popup) { FFOCUS = popup.trigger || FFOCUS; popClose(); }
      ACC.msg = {k, m: j.msg || "好了"};
      clearTimeout(ACC.mt); ACC.mt = setTimeout(() => { ACC.msg = null; render(false); }, 60000);
      after?.(j);
    }
  } catch (e) { if (visit === ACC.visit && (!popup || POP === popup)) ACC.err = {k: popup ? "pop" : k, m: e.message || String(e)}; }
  ACC.busy = false;
  render(false);
  if (ok) { accLoad(reload); if (reload !== "me" && body?.name === ME?.name) accLoad("me"); }
}
async function accAct(act, d = {}) {
  const v = id => document.getElementById(id)?.value || "", n = d.n;
  if (act === "reload") return accLoad("users");
  if (act === "open-onboard") {
    popToggle("acc-onboard", n, {route: location.hash, trigger: "open-onboard",
      draft: {un: /^[a-z][a-z0-9._-]{0,31}$/i.test(n) ? n : ""}});
    ACC.show = ACC.err = ACC.msg = null;
    FFOCUS = POP ? "acun" : "open-onboard";
    return render(false);
  }
  if (act === "open-pw" || act === "open-tok") {
    const k = act.slice(5);
    popToggle("acc-" + k, ME.name, {route: location.hash, trigger: act, draft: {}});
    ACC.show = ACC.err = ACC.msg = null;
    FFOCUS = POP ? (k === "pw" ? "acpo" : "acn") : act;
    return render(false);
  }
  if (act === "rv" || act === "urv1") {
    popToggle("acc-revoke", d.id, {name: n}); ACC.err = null; return render(false);
  }
  if (act === "revokego" && popIs("acc-revoke", d.id) && POP.name === n)
    return accPost(n ? "users/revoke" : "revoke", n ? {name: n, id: d.id} : {id: d.id}, d.where, n ? "users" : "me");
  if (["role", "publicops", "reset", "urv", "rm"].includes(act)) { popToggle("acc-" + act, n); ACC.err = null; return render(false); }
  if (act === "hosts") {
    const u = ACC.users?.users?.find(u => u.name === n && u.role !== "admin");
    if (!u) return;
    popToggle("acc-hosts", n, {sel: new Set(u.hosts || [])}); ACC.err = null; return render(false);
  }
  if (act === "hostsgo" && popIs("acc-hosts", n)) return accPost("users/hosts", {name: n, hosts: [...POP.sel]}, "list", "users");
  if (act === "push") {
    popToggle("acc-push", ME.name, {kind: ME.push?.kind || "bark"}); ACC.err = null; return render(false);
  }
  if (act === "pushrm") {
    popToggle("acc-pushrm", ME.name); ACC.err = null; return render(false);
  }
  if (act === "pushrmgo") return accPost("push", {push: null}, "push", "me");
  if (act === "pushtest") return accPost("push/test", {}, "push", "me");
  if (act === "pushgo" && popIs("acc-push", ME.name)) {
    const k = POP.kind, target = v("acpushkey").trim(), secret = v("acpushsecret");
    if (!target) { ACC.err = {k: "pop", m: k === "bark" ? "要填 Bark 推送密钥" : "要填 ntfy 主题"}; return render(false); }
    const conf = k === "bark" ? {key: target, encrypt_key: secret} : {topic: target, token: secret};
    return accPost("push", {push: k, [k]: conf}, "push", "me");
  }
  if (act === "showx") { ACC.show = null; return render(false); }
  if (act === "out") { await postJSON("api/logout", {}).catch(() => null); return toLogin(); }
  if (act === "pw" && popIs("acc-pw", ME.name)) {
    document.activeElement?.blur();
    if (!v("acpo") || !v("acpn")) { ACC.err = {k: "pop", m: "原口令、新口令都要填"}; return render(false); }
    if (v("acpn") !== v("acpn2")) { ACC.err = {k: "pop", m: "两遍新口令不一样"}; return render(false); }
    return accPost("password", {old: v("acpo"), new: v("acpn")}, "pw", "me");
  }
  if (act === "tok" && popIs("acc-tok", ME.name)) return accPost("tokens/add", {name: v("acn").trim(), scopes: [v("acs")]}, "tok", "me",
    j => { ACC.show = {page: "me", k: "tok", name: j.name, v: j.token, until: j.until}; ACC.msg = null; });
  if (act === "onboard" && POP?.k === "acc-onboard") {
    const name = v("acun").trim(), person = POP.id;
    if (!name || !person) { ACC.err = {k: "pop", m: "要填登录名并确认组内人员"}; return render(false); }
    return accPost("users/add", {name, admin: v("acur") === "admin", person}, act, "users",
      j => { ACC.show = {k: "code", where: act, name, subject: person, v: j.code}; ACC.msg = null; });
  }
  const U = ACC.users?.users?.find(u => u.name === n);
  if (act === "publicopsgo") return accPost("users/public-ops", {name: n, enabled: !U?.public_ops}, "list", "users");
  if (act === "rolego") return accPost("users/role", {name: n, admin: U?.role !== "admin"}, "list", "users");
  if (act === "resetgo") return accPost("users/reset", {name: n}, "list", "users", j => { ACC.show = {page: "users", k: "code", where: "list", name: n, v: j.code}; ACC.msg = null; });
  if (act === "urvgo") return accPost("users/revoke", {name: n}, "list", "users");
  if (act === "rmgo") return accPost("users/remove", {name: n}, "list", "users", closeDrw);
}
// 只显示这一次的令牌原文、设置码：复制、知道了
function accShow(k, where, name) {
  const S = ACC.show;
  if (S?.k !== k || (where && S.where !== where) || (name && S.name !== name)) return "";
  const tok = k === "tok";
  return `<div class="secret" role="status"><div class="row"><b>${tok ? `令牌「${esc(S.name)}」` : `${esc(S.name)} 的设置码`}</b><span class="sp"></span>
      <button class="btn mini-btn" data-copy="${esc(S.v)}">复制</button><button class="btn mini-btn" data-acc="showx">知道了</button></div>
    <code class="v">${esc(S.v)}</code>
    <div class="sm mut">${tok ? `只显示这一次，关掉或离开这页就看不到了（看板只存哈希）。脚本里带请求头 <code>Authorization: Bearer &lt;令牌&gt;</code>${S.until ? `；${tsNear(S.until)} 过期` : ""}。`
      : `24 小时内有效、用一次就作废，只显示这一次。交给这人：打开看板，登录页点「用设置码设口令」，填名字 <b>${esc(S.name)}</b>、设置码与新口令。`}</div></div>`;
}
const accGo = (act, n, text, danger = false) => `<button class="btn pri${danger ? " dz" : ""}" data-acc="${act}" data-n="${esc(n)}" ${ACC.busy ? "disabled" : ""}>${text}</button>`;
const accScopes = (ss, info) => ss.map(s => `<span class="chip" ${tip(info?.[s]?.[1] || "")}>${esc(info?.[s]?.[0] || s)}</span>`).join(" ");
const clientLabels = {outdated: "可更新", below_minimum: "低于最低版本", unknown: "版本待确认", inactive: "超过 30 天未报告"};
function accClient(x) {
  if (!x.client_commit) return "";
  const status = x.client_status;
  const label = clientLabels[status];
  const action = status === "outdated" || status === "below_minimum";
  return `<div class="sm mut">客户端 <code>${esc(x.client_commit.slice(0, 7))}</code> · ${tsNear(x.client_seen)}
    ${label ? `<span class="badge ${status === "below_minimum" ? "b-bad" : action ? "b-warn" : "b-mute"}">${label}</span>` : ""}
    ${action ? "运行 <code>labmon update</code>" : ""}</div>`;
}
// 一行会话或令牌（「我的账号」两张卡、「账号」里展开的明细共用）：x 是会话（有 ua）或令牌（有 scopes）
function accItem(x, info, owner = "") {
  const tok = !!x.scopes;
  const name = tok ? `<b>${esc(x.name)}</b> ${accScopes(x.scopes, info)}` : `<b ${tip(x.ua || "")}>${esc(uaName(x.ua))}</b>${x.this ? ` <span class="badge b-ok">这个浏览器</span>` : ""}`;
  const recent = x.seen ? `最后用 ${tsNear(x.seen)}` : "还没用过";
  const when = [tok ? recent : "", tok ? `${tsNear(x.t)} 创建` : `${esc(x.entry_name || "")} · ${tsNear(x.t)} 登录`,
    x.until ? `${tsNear(x.until)} ${tok ? "过期" : "到期"}` : tok ? "不过期" : ""].filter(Boolean).join(" · ");
  const attr = `data-id="${esc(x.id)}"${owner ? ` data-n="${esc(owner)}"` : ""}`;
  const btn = x.this && !owner ? "" : popConfirm(
    `<button class="btn mini-btn" data-acc="${owner ? "urv1" : "rv"}" ${attr}>吊销${tok ? "令牌" : "会话"}</button>`,
    popIs("acc-revoke", x.id) && POP.name === (owner || undefined),
    `<b>吊销这个${tok ? "令牌" : "会话"}？</b><div class="rn-note">${tok ? "使用此令牌的脚本和命令行将无法继续访问看板。" : "这个浏览器下次请求时需要重新登录。"}</div>${accLine("pop")}`,
    `<button class="btn pri dz" data-acc="revokego" ${attr} data-where="${owner ? "list" : tok ? "tok" : "session"}" ${ACC.busy ? "disabled" : ""}>确认吊销</button>`);
  return `<div class="urow sm-row"><div class="u">${icon(tok ? "lock" : "user")}<div class="t">${name}${tok ? "" : ` <span class="sm mut">${recent}</span>`}<div class="sm mut">${when}</div>${tok ? accClient(x) : ""}</div></div>
    ${btn ? `<div class="acts">${btn}</div>` : ""}</div>`;
}
function pushSec(M) {
  const P = M.push || {}, name = M.name, open = popIs("acc-push", name), bark = POP?.kind !== "ntfy";
  const b = (act, title) => `<button class="btn" data-acc="${act}" ${ACC.busy ? "disabled" : ""}>${title}</button>`;
  const edit = popConfirm(b("push", P.set ? "更换推送配置" : "设置推送"), open, open ? `<div class="aform">
    <label>推送方式<select class="ain" data-pushkind><option value="bark"${bark ? " selected" : ""}>Bark</option><option value="ntfy"${bark ? "" : " selected"}>ntfy</option></select></label>
    <label>${bark ? "推送密钥" : "主题"}<input id="acpushkey" class="ain" type="password" autocomplete="off"></label>
    <label>${bark ? "加密密钥（可选）" : "访问令牌（可选）"}<input id="acpushsecret" class="ain" type="password" autocomplete="off"></label></div>
    ${accLine("pop")}` : "", accGo("pushgo", name, "保存"), true);
  const rm = P.set || P.error ? popConfirm(b("pushrm", "删除推送配置"), popIs("acc-pushrm", name),
    `<div class="rn-note">删除这份配置后，看板停止向这个目标推送。</div>${accLine("pop")}`, accGo("pushrmgo", name, "删除", true)) : "";
  return dSec("推送", `<div class="row wrap account-push"><span class="account-target">${P.set ? esc(P.target) : "还没设置推送"}</span>
    ${edit}${P.set ? b("pushtest", "试推") : ""}${rm}</div>${P.error ? `<div class="rn-err">${esc(P.error)}</div>` : ""}${accLine("push")}`);
}
function clientSec(M) {
  const base = new URL(".", location.href).href;
  const url = `api/client/install.sh?base=${encodeURIComponent(base)}`;
  const cmd = "sh ~/Downloads/labmon-install.sh";
  return dSec("命令行", `<div class="account-install">
    <a class="btn" href="${esc(url)}" download="labmon-install.sh">下载安装脚本</a>
    <div class="account-command"><code tabindex="0" aria-label="安装命令">${cmd}</code><button class="btn mini-btn" data-copy="${cmd}">复制命令</button></div>
    <p class="sm mut">运行脚本后执行 <code>labmon login</code>。以后用 <code>labmon update</code> 更新。</p>
    ${M.entry === "public" ? '<p class="sm mut">公网连接需向管理员申请客户端证书，并把证书和私钥路径填进 client.toml。</p>' : ""}
    </div>`,
    M.commit ? `<span class="sm mut">看板版本 <code>${esc(M.commit.slice(0, 7))}</code></span>` : "");
}
const accountList = xs => xs?.length ? xs.map(esc).join("、") : "没有";
const ME_TABS = [["overview", "概览"], ["security", "登录与安全"], ["tokens", "令牌"], ["push", "推送"]];
function meTabs(tab) {
  return `<nav class="ftabs" aria-label="账号设置">${ME_TABS.map(([k, label]) =>
    `<a class="ftab${tab === k ? " on" : ""}" href="#/me${k === "overview" ? "" : "/" + k}"${tab === k ? ' aria-current="page"' : ""}>${label}</a>`).join("")}</nav>`;
}
function accountFormPop(k, name, label, fields) {
  const open = popIs("acc-" + k, name), act = "open-" + k;
  const btn = `<button class="btn" id="${act}" data-acc="${act}" aria-haspopup="dialog" aria-expanded="${open}">${label}</button>`;
  return popConfirm(btn, open, open ? `<form class="aform acc-pop-form" data-accf="${k}" method="post" autocomplete="on">
    ${fields}${accLine("pop")}<div class="pop-b"><button class="btn pri" type="submit" ${ACC.busy ? "disabled" : ""}>${label}</button>${popX}</div></form>` : "", null, true);
}
// 「我的账号」页头那句能做什么（#15）：浏览器登录的按角色说人话；令牌打开的按令牌的作用域说它能做的事（scope_info 的说明），不列作用域名
function meCan(M, info) {
  const fleet = "他人服务器、人员管理与平台操作只有管理员看得到";
  if (M.account) return M.admin ? `可以管理全部项目、主机与账号${M.ops ? "，也能做平台操作（续租、开关机、换人、借卡）" : `；平台操作：${OPS_NOTE}`}。`
    : M.write ? `可以查看能用的主机、本人共享盘与自己持有的服务器，管理自己的项目（调度、配置、登记与归档）。${fleet}。`
    : `可以查看能用的主机、本人共享盘与自己持有的服务器，不能改项目。${fleet}。`;
  const ss = Object.keys(info).length ? Object.keys(info).filter(s => M.scopes.includes(s)) : M.scopes;
  return `这个令牌能做：${ss.map(s => info[s]?.[1] || info[s]?.[0] || s).join("；")}。`;
}
function mePage(tab) {
  if (!LIVE || !ME) return `<div class="card"><div class="empty">账号要经看板服务（labmon serve）打开才看得到。</div></div>`;
  if (!ACC.at.me) accLoad("me");
  const M = ACC.me || ME, info = M.scope_info || {};
  const pw = M.account ? accountFormPop("pw", M.name, "修改口令", `
    <input type="text" name="username" autocomplete="username" value="${esc(M.name)}" hidden>
    <label>原口令<input class="ain" id="acpo" type="password" autocomplete="current-password" required></label>
    <label>新口令<input class="ain" id="acpn" type="password" autocomplete="new-password" required></label>
    <label>确认新口令<input class="ain" id="acpn2" type="password" autocomplete="new-password" required></label>
    <p class="sm mut">修改后，其他浏览器需要重新登录。</p>`) : "";
  const head = `<section class="card"><div class="acc-hd">${accAv(M.name)}<div class="who"><div class="acc-name"><b>${esc(M.name)}</b>
      <span class="badge ${M.admin ? "b-ok" : "b-mute"}">${esc(M.role_name)}</span><span class="badge b-mute">从${esc(M.entry_name)}来</span></div>
      <div class="sm mut acc-can">${esc(meCan(M, info))}</div></div>
      ${M.account ? `<div class="acc-hd-actions">${pw}<button class="btn" data-acc="out">${icon("power")}退出登录</button></div>` : ""}</div>${accLine("pw")}${accLine("top")}${ACC.loadError.me ? `<div class="rn-err">${esc(ACC.loadError.me)}</div>` : ""}</section>`;
  if (!M.account) return head + clientSec(M) + `<div class="card"><div class="empty">用令牌打开的页面不能改账号：在浏览器里登录后再来。</div></div>`;
  if (tab === "push") return head + pushSec(M);
  const ss = M.sessions || [], tk = M.tokens || [];
  const sess = dSec("登录的浏览器", (ss.length ? `<div class="ulist">${ss.map(x => accItem(x, info)).join("")}</div>`
    : `<div class="empty">没有。</div>`) + accLine("session"), `<span class="sm mut">${ss.length} 个，30 天没用的自动过期</span>`);
  if (tab === "security") return head + sess;
  const draft = popIs("acc-tok", M.name) ? POP.draft : {};
  const sel = draft.toks || (M.token_scopes || [])[0];
  const opts = (M.token_scopes || []).map(s => `<option value="${esc(s)}"${s === sel ? " selected" : ""}>${esc(info[s]?.[0] || s)}：${esc(info[s]?.[1] || "")}</option>`).join("");
  const toks = dSec("令牌", `<div class="sm mut" style="margin-bottom:8px">给脚本与命令行用（部署脚本、备份）：请求头带 <code>Authorization: Bearer &lt;令牌&gt;</code>。原文只在建好时显示一次，看板只存哈希。</div>
      ${tk.length ? `<div class="ulist">${tk.map(x => accItem(x, info)).join("")}</div>` : `<div class="empty">还没有令牌。</div>`}
      ${accShow("tok")}${accLine("tok")}`,
    `<span class="sm mut">${tk.length} 个</span><span class="sp"></span>${accountFormPop("tok", M.name, "创建令牌", `
      <label>名称<input class="ain" id="acn" data-pop-field="tokn" value="${esc(draft.tokn || "")}" placeholder="如：部署脚本" maxlength="40" autocomplete="off"></label>
      <label>能做什么<select class="ain" id="acs" data-pop-field="toks">${opts}</select></label>`)}`);
  if (tab === "tokens") return head + toks;
  // 没单独分配主机、但自己的项目在别的主机上有车道的，照实说在哪几台（与总览「开始使用」同一口径：myHosts）
  const seen = myHosts().filter(id => !(M.hosts || []).includes(id));
  const own = M.admin ? "" : dSec("项目与主机", kvHtml([
    ["我的项目", accountList(M.own)],
    ["能用的主机", (M.hosts?.length ? accountList(M.hosts) : "没有单独分配") + (seen.length ? `<span class="sm mut">；你的项目还在 ${accountList(seen)} 上有任务</span>` : "")]
  ]));
  return head + clientSec(M) + own;
}
function accStatus(u, brief = false) {
  if (u.lock) return `<span class="badge b-bad">${brief ? "已锁定" : `锁到 ${tsNear(u.lock)}`}</span>`;
  if (u.pw) return brief ? "" : `<span class="badge b-ok">可登录</span>`;
  if (u.setup > Date.now() / 1000) return `<span class="badge b-warn">等设口令${brief ? "" : ` · ${tsNear(u.setup)} 前有效`}</span>`;
  return `<span class="badge b-bad">设置码已过期</span>`;
}
// 人员卡上「看板账号」那一行（#15）：账号的角色与要处理的状态单独一行、带标签，不与下面服务器那一行（人员在组内的状态）混在一起
function accountCardStatus(u, roles, known = true) {
  const k = '<span class="pk">看板账号</span>';
  // 账号列表没取到（公网入口不取、只读令牌、正在读或读不了）：不说“未开通”（#17）
  if (!known) return `<div class="people-meta sm">${k}<span class="mut">${accLan() ? "—" : "在内网入口查看"}</span></div>`;
  if (!u) return `<div class="people-meta sm">${k}<span class="mut">未开通</span></div>`;
  const lagging = u.tokens.filter(t => t.client_commit && ["outdated", "below_minimum"].includes(t.client_status)).length;
  const status = accStatus(u, true);
  const alert = status || (u.fleet_person_error ? `<span class="badge b-bad" ${tip(u.fleet_person_error)}>关联有误</span>`
    : lagging ? `<span class="badge b-warn">${lagging} 个待更新</span>` : "");
  return `<div class="people-meta sm">${k}<span${u.name !== u.fleet_person ? ` ${tip("账号 " + u.name)}` : ""}>${esc(roles?.[u.role] || u.role)}</span>${alert}</div>`;
}
function directoryUsers() {
  if (LIVE && ME?.admin && ME.scopes.includes("admin")) {
    if (!ACC.at.users) accLoad("users");
    return ACC.users;
  }
  return null;
}
function directoryPeople(L) {
  const people = new Map(Object.entries(fops(false).people || {}));
  for (const n of L?.people || []) if (!people.has(n)) people.set(n, {});
  for (const h of D.fleet?.holders || []) if (!people.has(h.holder)) people.set(h.holder, null);
  return people;
}
function directoryAccount(n, L) {
  return L?.users.find(u => u.fleet_person === n);
}
const permissionRow = (label, value, action = "") => `<div class="fr permission-row"><div class="l">${label}</div><div class="c">${value}${action}</div></div>`;
function accountSections(u, L) {
  const self = u.name === ME?.name, adm = u.role === "admin", last = adm && L.admins <= 1, n = esc(u.name), pk = k => popIs("acc-" + k, u.name);
  const b = (k, cls, text, dis, hint) => `<button class="btn ${cls}" data-acc="${k}" data-n="${n}" ${dis ? "disabled" : ""} ${hint ? tip(hint) : ""}>${text}</button>`;
  const err = accLine("pop"), nk = u.sessions.length + u.tokens.length;
  const role = popConfirm(b("role", "mini-btn", "更改角色", last, last ? "最后一个管理员撤不掉：先把别人设为管理员" : ""), pk("role"),
    (adm ? `<b>把 ${n} 改成普通用户</b>：这人的会话与令牌从下一次请求起按项目和主机授权，看不到组内服务器、平台操作与账号。${self ? `<div class="pop-w">这是你自己：改完你就不能再管账号了。</div>` : ""}`
      : `<b>把 ${n} 设为管理员</b>：此账号可以管理所有项目和账号。<div class="sm mut">续租、开关机、换人和借卡可从内网操作，也可按账号开启公网平台操作。</div>`) + err, accGo("rolego", u.name, adm ? "改成普通用户" : "设为管理员"));
  const publicops = adm ? popConfirm(b("publicops", "mini-btn", u.public_ops ? "关闭" : "开启", ME.entry !== "lan", ME.entry !== "lan" ? "从内网入口修改此设置" : ""), pk("publicops"), `<b>${u.public_ops ? "关闭" : "开启"} ${n} 的公网平台操作</b><p>包括续租、开关机、换人、借还和保活管理。</p>${err}`, accGo("publicopsgo", u.name, "确认")) : "";
  const reset = self ? "" : popConfirm(b("reset", "", "重置口令"), pk("reset"),
    `<b>重置 ${n} 的口令</b>：原口令立即失效，这人已登录的浏览器都要重新登录，${u.tokens.length ? `${u.tokens.length} 个令牌一并吊销（命令行与实验会话要重新 labmon login）` : "令牌也一并吊销"}。给一个新的设置码（24 小时有效），交给这人在登录页设新口令。${err}`, accGo("resetgo", u.name, "重置"));
  const rv = popConfirm(b("urv", "", "吊销全部会话与令牌", !nk, nk ? "" : "没有会话与令牌"), pk("urv"),
    `<b>吊销 ${n} 的全部会话与令牌</b>（${u.sessions.length} 个会话、${u.tokens.length} 个令牌）：下一次请求就失效，口令不变。${err}`, accGo("urvgo", u.name, "吊销"));
  const rm = self ? "" : popConfirm(b("rm", "danger", "删除", last, last ? "最后一个管理员删不掉" : ""), pk("rm"),
    `<b>删除账号 ${n}</b>：这人的会话、令牌与推送配置一并删除，名下项目归管理员。${err}`, accGo("rmgo", u.name, "删除", true));
  const pick = L.hosts || [], open = pk("hosts");
  const hostHint = L.hosts_error ? "主机清单暂不可用" : "主机清单里没有可分配的主机";
  const edit = adm ? "" : popConfirm(b("hosts", "mini-btn", "分配主机", !pick.length, !pick.length ? hostHint : ""),
    open, open ? `<div class="rg-tos">${pick.map(h => `<label class="rg-to"><input type="checkbox" data-achost="${esc(h.id)}"${POP.sel.has(h.id) ? " checked" : ""}>
      <b>${esc(h.label)}</b><span class="sm mut">${esc(h.id)}${h.returned ? " · 已归还" : ""}</span></label>`).join("")}</div>${err}` : "",
    accGo("hostsgo", u.name, "保存"), true);
  const projects = (u.projects || []).map(id => D.projects.some(p => p.id === id)
    ? `<a href="#/p/${encodeURIComponent(id)}">${esc(pname(id))}</a>` : esc(id)).join("、") || "暂无直接归属项目";
  const hosts = adm ? "全部主机 · 管理员权限" : L.hosts_error ? hostHint : u.hosts?.length ? accountList(u.hosts) : "未分配主机";
  const access = permissionRow("账号角色", esc(L.roles[u.role] || u.role), role)
    + (adm ? permissionRow("公网平台操作", u.public_ops ? "已开启" : "未开启", publicops) : "")
    + permissionRow("直接归属项目", projects) + permissionRow("主机使用权限", hosts, edit);
  const items = xs => xs.length ? `<div class="ulist">${xs.map(x => accItem(x, L.scope_info, u.name)).join("")}</div>` : '<div class="empty">没有。</div>';
  const meta = [u.by ? `${esc(u.by)} ${tsNear(u.t)} 添加` : "", u.seen ? `最后用 ${tsNear(u.seen)}` : "还没用过"].filter(Boolean);
  const created = `<p class="account-created sm mut">${meta.join(" · ")}</p>`;
  return `${dSec("访问权限", access + created)}
    ${dSec("登录的浏览器", items(u.sessions), `<span class="sm mut">${u.sessions.length} 个</span>`)}
    ${dSec("令牌", items(u.tokens), `<span class="sm mut">${u.tokens.length} 个</span>`)}
    ${dSec("账号操作", `<div class="row wrap">${reset}${rv}${rm}</div>${accShow("code", "list", u.name)}${accLine("list")}`)}`;
}
function onboardPop(n) {
  const open = popIs("acc-onboard", n), draft = open ? POP.draft : {};
  const btn = `<button class="btn mini-btn" id="open-onboard" data-acc="open-onboard" data-n="${esc(n)}"
    aria-haspopup="dialog" aria-expanded="${open}">开通账号</button>`;
  return popConfirm(btn, open, open ? `<form class="aform acc-pop-form" data-accf="onboard" method="post">
    <p class="sm mut">为 <b>${esc(n)}</b> 开通看板账号</p>
    <label>登录名<input class="ain" id="acun" data-pop-field="un" value="${esc(draft.un || "")}" maxlength="32" autocomplete="off" autocapitalize="off" spellcheck="false" required></label>
    <label>账号角色<select class="ain" id="acur" data-pop-field="ur"><option value="user"${draft.ur !== "admin" ? " selected" : ""}>普通用户</option>
      <option value="admin"${draft.ur === "admin" ? " selected" : ""}>管理员</option></select></label>
    ${accLine("pop")}<div class="pop-b"><button class="btn pri" type="submit" ${ACC.busy ? "disabled" : ""}>开通账号</button>${popX}</div></form>` : "", null, true);
}
