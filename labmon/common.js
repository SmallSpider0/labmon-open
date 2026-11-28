/* 项目页（page.html）与看板（overview.html）共用：渲染时内联进页面（file:// 打开也能用）。 */
// 经 labmon serve（http）打开时原地取新快照；file:// 打开时整页重载
const LIVE = location.protocol.startsWith("http");
let OFFLINE = null, BUSY = false;
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const tip = s => `data-tip="${esc(s)}"`;
const pad = n => String(n).padStart(2, "0");
const GIB = 2 ** 30;
// 共享盘上的量（字节 → GiB 的数字，不带单位）：100 GiB 以上取整，否则一位小数；dGB 是增量，带正负号，不到 0.05 GiB 写 0（工作台的共享盘与项目页的本项目占用共用）
const GB = b => b == null ? "—" : Math.abs(b) >= 100 * GIB ? String(Math.round(b / GIB)) : (b / GIB).toFixed(1);
const dGB = b => !b || Math.abs(b) < GIB / 20 ? "0" : (b > 0 ? "+" : "−") + GB(Math.abs(b));
// 正在操作（定时刷新先不重画，免得冲掉输入、关掉选择器、取消选区）：正拖着排顺序（sortBind），焦点在输入框、文本框或下拉框上，或在 ids 这些元素里选着文字。
// 勾选框、单选、按钮类的 input 不算：点过以后焦点留在上面，整页会一直停在旧数据上（#66）；
// 选着字只挡 SEL_HOLD：复制完告警不取消选区，页面也照常换新（#66）
let SEL_T = 0;
const SEL_HOLD = 60000;
document.addEventListener("selectionchange", () => { SEL_T = Date.now(); });
// 能打字的控件（输入框、文本框、下拉框；勾选框、单选、按钮类的 input 不算）：el 在它里面就返回它，否则 null
const typingEl = el => { const a = el?.closest?.("input,textarea,select,[contenteditable]"); return a && !/^(checkbox|radio|button|submit|reset|image|file|range|color)$/.test(a.type) ? a : null; };
function editing(...ids) {
  const s = getSelection();
  return !!SORT?.on || !!typingEl(document.activeElement)
    || !!s && !s.isCollapsed && Date.now() - SEL_T < SEL_HOLD && ids.some(id => document.getElementById(id)?.contains(s.anchorNode));
}
// 点了不能打字的地方（按钮、勾选框、文字）：先让焦点所在的能打字控件失焦，与 Chrome 按下鼠标时一样。
// Safari 点按钮、勾选框不挪焦点，焦点留在输入框里 editing 就一直为真，点完的 render(false) 只记 PENDING 就返回、整页停住（#129、#131）。
// 捕获阶段：在页面自己的点击处理与勾选框的 change 之前；输入的字都已由 input 事件记进草稿，失焦不丢东西。点 label 的控件能打字时不动（浏览器自己把焦点给它）
document.addEventListener("click", e => {
  const a = typingEl(document.activeElement), t = e.target;
  if (a && !typingEl(t) && !typingEl(t?.closest?.("label")?.control)) a.blur();
}, true);

/* ---------- 拖动排顺序（pointer 事件，鼠标与触屏同一套；页面单文件内联，不引外部库） ----------
   sortBind({handle, row, drop})：handle 把手的选择器（只从它起拖，免得和点名字改名冲突），row 行的选择器（同一个父元素下的这些行算一个列表），
   松手后调 drop(row, from, to)（取消了、没挪也调，to === from）；可选的 cover 是盖在滚动容器下边的元素（吸底的保存条）的选择器，自动滚的下边取它的上沿。按下后挪过 4 像素才算拖：被拖的行跟着指针走、抬起（sort-lift），
   其余行实时让位（transform 加过渡，sort-shift），要落下的位置画一个同高的虚线占位（sort-ph）；Esc、pointercancel、拖出列表 48 像素以外（sort-out）取消回原位。
   列表在能滚动的容器里时拖到容器上下边自动滚；拖着时滚了（自动滚、滚轮）当场重算落点。拖着的时候 editing() 为真：定时刷新先不重画，免得把拖着的行换掉（行被别的重画换掉了就取消）。
   样式（sort-*）写在用它的页面里 */
let SORT = null;
const SORTS = [];
const sortBind = o => SORTS.push(o);
const SORT_MIN = 4, SORT_OUT = 48, SORT_EDGE = 40;
document.addEventListener("pointerdown", e => {
  if (SORT || e.button > 0) return;
  for (const o of SORTS) {
    const h = e.target?.closest?.(o.handle), row = h?.closest?.(o.row), list = row?.parentElement;
    if (!row || !list) continue;
    e.preventDefault();                                  // 不选字、不挪焦点；触屏不滚动靠把手的 touch-action: none
    const rows = [...list.children].filter(x => x.matches?.(o.row)), lr = list.getBoundingClientRect(), r = rows.map(x => x.getBoundingClientRect());
    const tops = r.map(b => b.top - lr.top), hs = r.map(b => b.height);
    // 每行占的高度（到下一行顶上，含间距）；最后一行按上一个间距算
    const pitch = tops.map((t, i) => i + 1 < tops.length ? tops[i + 1] - t : hs[i] + (i ? Math.max(0, t - tops[i - 1] - hs[i - 1]) : 0));
    SORT = {o, h, row, list, rows, from: rows.indexOf(row), to: rows.indexOf(row), x0: e.clientX, y0: e.clientY, x: e.clientX, y: e.clientY, top0: lr.top, tops, hs, pitch, id: e.pointerId, on: false, out: false};
    try { h.setPointerCapture?.(e.pointerId); } catch (err) {}
    return;
  }
});
document.addEventListener("pointermove", e => {
  const S = SORT;
  if (!S || e.pointerId !== S.id) return;
  if (!S.row.isConnected) return sortEnd(false);
  S.x = e.clientX; S.y = e.clientY;
  if (!S.on) {
    if (Math.abs(S.y - S.y0) < SORT_MIN && Math.abs(S.x - S.x0) < SORT_MIN) return;
    sortStart(S);
  }
  e.preventDefault();
  sortMove(S);
});
document.addEventListener("pointerup", e => { if (SORT && e.pointerId === SORT.id) sortEnd(SORT.on && !SORT.out); });
document.addEventListener("pointercancel", e => { if (SORT && e.pointerId === SORT.id) sortEnd(false); });
// 指针不动、只滚了（滚轮、触控板、自动滚）：列表跟着挪了，被拖的行与落点当场重算（scroll 不冒泡，捕获阶段收得到各容器的）
document.addEventListener("scroll", () => { if (SORT?.on) sortMove(SORT); }, true);
document.addEventListener("keydown", e => {                // 捕获阶段、拦下：Esc 只取消拖动，不连带关抽屉
  if (SORT?.on && e.key === "Escape") { e.preventDefault(); e.stopImmediatePropagation?.(); sortEnd(false); }
}, true);
function sortStart(S) {
  S.on = true;
  typingEl(document.activeElement)?.blur();
  if (typeof hideTip === "function") hideTip();
  document.body.classList.add("sorting");
  S.list.classList.add("sort-list");
  S.row.classList.add("sort-lift");
  for (const x of S.rows) if (x !== S.row) x.classList.add("sort-shift");
  S.ph = document.createElement("div");
  S.ph.className = "sort-ph";
  S.ph.style.height = S.hs[S.from] + "px";
  S.ph.style.top = S.tops[S.from] + "px";
  S.list.append(S.ph);
  sortScroll(S);
}
// 指针挪了或列表滚了：被拖的行跟着指针（扣掉列表这期间滚过的），按它的中线数过了几行定落点，其余行让位，占位挪到落点
function sortMove(S) {
  const lr = S.list.getBoundingClientRect(), dy = S.y - S.y0 - (lr.top - S.top0), f = S.from;
  S.out = S.x < lr.left - SORT_OUT || S.x > lr.right + SORT_OUT || S.y < lr.top - SORT_OUT || S.y > lr.bottom + SORT_OUT;
  const mid = S.tops[f] + S.hs[f] / 2 + dy;
  S.to = S.out ? f : S.rows.filter((x, k) => k !== f && S.tops[k] + S.hs[k] / 2 < mid).length;
  const t = S.to;
  S.row.style.transform = `translateY(${dy}px)`;
  S.row.classList.toggle("sort-out", S.out);
  S.rows.forEach((x, k) => { if (k !== f) x.style.transform = f < k && k <= t ? `translateY(${-S.pitch[f]}px)` : t <= k && k < f ? `translateY(${S.pitch[f]}px)` : ""; });
  let top = S.tops[t];
  if (t > f) { top = S.tops[f]; for (let k = f + 1; k <= t; k++) top += S.pitch[k]; }
  S.ph.style.top = top + "px";
  S.ph.hidden = S.out;
}
// 拖到能滚动的容器（看得见的那段）上下边 SORT_EDGE 像素以内、列表在那一头还有没露出来的：越靠边滚得越快，每帧滚完重算落点
function sortScroll(S) {
  let sc = S.list.parentElement;
  try { while (sc && sc !== document.body && !(/(auto|scroll)/.test(getComputedStyle(sc).overflowY) && sc.scrollHeight > sc.clientHeight)) sc = sc.parentElement; } catch (e) { sc = null; }
  if (!sc || sc === document.body) sc = document.scrollingElement || document.documentElement;
  const step = () => {
    if (SORT !== S) return;
    const b = sc === document.scrollingElement ? {top: 0, bottom: innerHeight} : sc.getBoundingClientRect(), top = Math.max(0, b.top), c = S.o.cover && sc.querySelector?.(S.o.cover);
    const bot = Math.min(innerHeight, b.bottom, c?.offsetHeight ? c.getBoundingClientRect().top : Infinity);
    const lr = S.list.getBoundingClientRect();          // 列表这一头已经整个露出来了就不再滚，免得把列表滚走、指针落到列表外
    const v = S.y < top + SORT_EDGE && lr.top < top ? -(top + SORT_EDGE - S.y) : S.y > bot - SORT_EDGE && lr.bottom > bot ? S.y - (bot - SORT_EDGE) : 0;
    if (v && !S.out) sc.scrollTop += Math.max(-SORT_EDGE, Math.min(SORT_EDGE, v)) / 3;     // 滚了由 scroll 事件重算落点
    requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}
// 收尾：清掉样式与占位；ok 时落在占位处，否则回原位
function sortEnd(ok) {
  const S = SORT;
  SORT = null;
  try { S.h.releasePointerCapture?.(S.id); } catch (e) {}
  if (!S.on) return;
  document.body.classList.remove("sorting");
  S.list.classList.remove("sort-list");
  for (const x of S.rows) { x.classList.remove("sort-lift", "sort-shift", "sort-out"); x.style.transform = ""; }
  S.ph?.remove();
  S.o.drop(S.row, S.from, ok ? S.to : S.from);
}

/* ---------- 连不上看板服务（#177）：服务重启的那几秒 fetch 抛 TypeError（Safari「Load failed」、Chrome「Failed to fetch」）。
   页面上的请求都经 api：连不上时换成 down 为真的错误，文字只由 netText 生成（两页同一处），不把浏览器的原始报错给人看。
   retry 为真的按 NET_WAIT 自动重试（每次等之前调 onWait）：只用于做两遍也没事的——读数据（GET）、立即采集、立即探测。
   json 为真时一并读响应体；已收到响应头的请求不重发，读响应体时断开也用 netText。
   其余写请求不重发：连接可能断在服务做完之后，重发就做了两遍；只说一句让人看页面、没变再点。
   定时取数（两页的 poll）不重试，交给 pollNet：顶栏标连不上、提早再取，取到了复位 ---------- */
const NET_WAIT = [1, 2, 3, 5, 8];          // 自动重试前等的秒数，合计约 19 秒，盖得住一次重启（平时几秒）
const NET_SAY = {down: "", poll: "，几秒后自动重试", write: "：这一下不知道做成没有，过几秒看页面，没变再点一次",
                 gave: `：自动重试了 ${NET_WAIT.length} 次还没连上，过一会再试`};
const netText = (how = "down") => "看板服务连不上，可能正在重启" + NET_SAY[how];
async function api(url, opt = {}, {retry = false, onWait, json = false} = {}) {
  let r;
  for (let i = 0; ; i++) {
    try {
      r = await fetch(url, opt);
      break;
    } catch (e) {
      if (e?.name !== "TypeError") throw e;                   // HTTP 错误码照常返回
      if (!retry || i >= NET_WAIT.length) throw Object.assign(new Error(netText(retry ? "gave" : opt.method === "POST" ? "write" : "down")), {down: true});
      onWait?.(i);
      await new Promise(r => setTimeout(r, NET_WAIT[i] * 1000));
    }
  }
  if (!json) return r;
  try { return {ok: r.ok, status: r.status, body: await r.json()}; }
  catch (e) {
    if (e?.name === "TypeError") throw Object.assign(new Error(netText(opt.method === "POST" ? "write" : "down")), {down: true, responseLost: true});
    throw e;
  }
}
// 定时取数的结果：取到了传 null（复位，返回 null）；出错返回顶栏的字，连不上时按 NET_WAIT 提早再调 again（不等下一个 15 秒）
let NETT = 0, NETN = 0;
function pollNet(e, again) {
  clearTimeout(NETT);
  if (!e) { NETN = 0; return null; }
  if (!e.down) return "看板服务出错（" + (e.message || e) + "）";
  NETT = setTimeout(again, NET_WAIT[Math.min(NETN++, NET_WAIT.length - 1)] * 1000);
  return netText("poll");
}
// 顶栏上连不上、出错的那句（两页同一个样式）；tail 接在后面
const offBadge = (off, tail = "") => off ? `<span class="sm" style="color:var(--bad);font-weight:600">${esc(off)}${tail}</span>` : "";

/* ---------- 写请求：页面上的 POST 都经 postJSON，一律带 JSON 类型（看板服务只认 application/json 的 POST，防别的网站用表单跨站提交，#158）；
   不另写发 POST 的 fetch：漏了类型的那个按钮会被服务拒掉（415；tests/test_pages.py 查模板里只有这一处 POST）。body 缺省 {}；
   how 传给 api：只有做两遍也没事的（立即采集、立即探测）才带 {retry: true} ---------- */
const postJSON = (url, body = {}, how) => api(url, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)}, how);
const sendJSON = (method, url, body = {}, how) => api(url, {method, headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)}, how);

/* ---------- 看板账号（#160）：ME 是看板服务发页面时填进来的“我是谁”（users.me，与 /api/me 同一份）；file:// 打开的、离线预览没填的是 null（只读）。
   canOps：平台操作（续租、关机、换人、开机、借卡、保活守护、探测、人员、共享盘归属：管理员、入口获准）；
   canWrite：项目的写操作（project 作用域，普通用户只改自己的）；canWeight：调度权重与优先（管理员，#16）。
   opsNote：管理员从公网入口来时，平台操作按钮的位置换成的那句；别的人那里什么都不出。
   会话过期、被吊销或退出了：取数据拿到 401 就回登录页（toLogin），带着当前地址与视图；嵌在工作台里的项目页带着工作台一起回 ---------- */
const canOps = () => LIVE && !!ME?.ops;
const canWrite = pid => LIVE && !!ME?.write && (!!ME.admin || !!ME.own?.includes(pid));
const canWeight = () => canWrite();
const isPoolId = id => id === "pool" || String(id || "").startsWith("pool:");
const canUseHost = id => LIVE && (!!ME?.admin || !!ME?.hosts?.includes(id)
  || (isPoolId(id) && (ME?.pools ? ME.pools.includes(id) || (id === "pool" && ME.pools.length === 1) : !!ME?.pool_member)));   // 共有型算力池（#212）：池成员能交、能撤
const OPS_NOTE = "请从内网入口操作，或为此管理员开启公网平台操作";
const opsNote = (cls = "sm mut") => LIVE && ME?.admin && !ME.ops ? `<span class="${cls} opsn">${OPS_NOTE}</span>` : "";
function toLogin() {
  let w = window;
  try { if (window.top !== window && window.top.location.pathname) w = window.top; } catch (e) { /* 嵌在别处：只跳自己 */ }
  const p = w.location.pathname, m = /(?:^|\/)(p\/[^/]+\/)(?:index\.html)?$/.exec(p);       // 项目页在 p/<id>/ 下，登录页在看板根
  w.location.href = new URL((m ? "../../" : "") + "login?next=" + encodeURIComponent(m ? m[1] : ""), w.location.href).href + w.location.hash;
}
if (LIVE && ME && typeof fetch === "function") {
  const fetch0 = fetch;
  fetch = async (...a) => { const r = await fetch0(...a); if (r.status === 401) toLogin(); return r; };      // eslint-disable-line no-global-assign
}

/* ---------- 本机存的设置（JSON；读写出错当没有）：mkStore(前缀, 存储) 给 get(k, d) / set(k, v)。工作台 ST（labmon:），项目页 SS / LS（labmon:<标题>，分 session 与 local） ---------- */
const mkStore = (prefix, sto = localStorage) => ({
  get(k, d) { try { const v = sto.getItem(prefix + k); return v == null ? d : JSON.parse(v); } catch (e) { return d; } },
  set(k, v) { try { sto.setItem(prefix + k, JSON.stringify(v)); } catch (e) {} },
});

/* ---------- 悬停提示（两个页面同一套外壳）：#tip 元素、显示与收起、鼠标与触屏的绑定；页面只在 tipBind 里给自己的悬停（曲线十字线、嵌着的另一页） ---------- */
const TIP = document.getElementById("tip") || document.body.appendChild(Object.assign(document.createElement("div"), {id: "tip"}));
const TIPS = {el: null, t: 0, hover: null, hide: null};     // el：正显示提示的元素（重画后它不在页面上了就收起）；t：flashTip 的定时器
function placeTip(e, text) {
  TIP.textContent = text; TIP.style.display = "block";
  const w = TIP.offsetWidth, hgt = TIP.offsetHeight;
  TIP.style.left = Math.max(4, Math.min(innerWidth - w - 8, e.clientX + 12)) + "px";
  TIP.style.top = (e.clientY + 14 + hgt > innerHeight ? e.clientY - hgt - 10 : e.clientY + 14) + "px";
}
// showTip 显示（el 记下是谁的）；flashTip 显示 3 秒后收起（触屏点一下、不在内网时点了按钮）；hideTip 收起，再做页面自己的收尾（o.hide）
function showTip(e, text, el = null) { TIPS.el = el; placeTip(e, text); }
function flashTip(e, text, el = null) { clearTimeout(TIPS.t); showTip(e, text, el); TIPS.t = setTimeout(hideTip, 3000); }
function hideTip() {
  if (TIP.style.display === "none") return;
  TIP.style.display = "none"; TIPS.el = null;
  TIPS.hide?.();
}
const tipCheck = () => { if (TIPS.el && !TIPS.el.isConnected) hideTip(); };     // 重画后调：悬停着的元素没了就收起
// 每页绑一次。o.hover(e)：页面自己的悬停，返回 [文字, 元素] 就显示它，返回空再按 [data-tip] 找；o.hide()：收起时的收尾
// 收起的时机：移出窗口（relatedTarget 为空）或进了嵌入的 iframe、窗口失焦、切走标签页、滚动（快速移到另一块屏幕时浏览器偶尔不发 mouseout）；
// 触屏没有悬停：点一下显示 3 秒
function tipBind(o = {}) {
  TIPS.hover = o.hover || null; TIPS.hide = o.hide || null;
  document.addEventListener("mousemove", e => {
    const r = TIPS.hover?.(e);
    if (r) return showTip(e, r[0], r[1]);
    const t = e.target.closest?.("[data-tip]");
    t ? showTip(e, t.dataset.tip, t) : hideTip();
  });
  document.addEventListener("mouseout", e => { if (!e.relatedTarget || e.relatedTarget.tagName === "IFRAME") hideTip(); });
  document.documentElement.addEventListener("mouseleave", hideTip);
  window.addEventListener("blur", hideTip);
  document.addEventListener("visibilitychange", hideTip);
  document.addEventListener("scroll", hideTip, true);
  if (matchMedia("(hover: none)").matches) document.addEventListener("click", e => { const t = e.target.closest?.("[data-tip]"); t ? flashTip(e, t.dataset.tip, t) : hideTip(); });
}

/* ---------- 时刻与时长的文字（两个页面只有这一套）：时刻一律是 epoch 秒，空值给“—”；页面不解析时刻字符串 ---------- */
const tsMD = t => { if (!t) return "—"; const d = new Date(t * 1000); return `${pad(d.getMonth() + 1)}-${pad(d.getDate())}`; };            // 月-日
const tsHM = t => { if (!t) return "—"; const d = new Date(t * 1000); return `${pad(d.getHours())}:${pad(d.getMinutes())}`; };             // 时:分
const ts = t => t ? `${tsMD(t)} ${tsHM(t)}` : "—";                                                                                        // 月-日 时:分
const tsNear = t => t ? (new Date(t * 1000).toDateString() === new Date().toDateString() ? tsHM(t) : ts(t)) : "—";                         // 今天的只写时:分
// 时长（秒）：1 分钟内写秒，90 分钟内写分钟，两天内写小时，再长写天
const dur = s => s == null || isNaN(s) ? "—" : s < 60 ? `${Math.max(0, Math.round(s))} 秒` : s < 5400 ? `${Math.round(s / 60)} 分钟` : s < 48 * 3600 ? `${(s / 3600).toFixed(1)} 小时` : `${(s / 86400).toFixed(1)} 天`;

/* ---------- 借用（borrow.py）：主机的 borrow 由 borrow.check 算好（holder、until、for、cutoff、why、returning、back、tight、overdue、swap、keep、other） ---------- */
// 给对方留的量（borrow.reserve_for：按对方近 7 天轻用时的用量推荐；借用中被挤到会调高）
// hold 为假：借来时对方空着，不预先留，对方回来用时才按它判断超出；away：对方说好不用到几点，那以前不留（与 borrow.keep_text 同一句话）
// short：借用抽屉里一行的短式（行名已写“给对方留”或“说好不用”）
const keepText = (k, short) => (k.away ? (short ? `${tsNear(k.away)} 前不用，不给对方留余量`
    : `对方说好 ${tsNear(k.away)} 前不用：不给对方留余量，对方此刻占着的照算、空着的都能用；对方有了新的 GPU 进程、或内核杀了 labmon 之外的进程，就当对方回来了，恢复按预留借卡`)
  : k.hold !== false ? `${short ? "" : "给对方留"}显存 ${k.vram} GiB、内存 ${k.ram} GiB、CPU ${k.cpu} 核`
  : `借来时对方空着，不预先留；对方回来用时，有这人 GPU 进程的卡、内存超过 ${k.ram} GiB、CPU 超过 ${k.cpu} 核就不放新任务`) + (k.bump > 1 ? `（被挤到后 ×${k.bump}）` : "");
// 借用现在怎样的一句话（看板的卡片、抽屉、主机页，项目页的顶栏与资源组都取它）。now：页面上的“现在”
function borrowText(b, now) {
  const who = b.holder || "原使用人", cut = b.cutoff ? `${tsNear(b.cutoff)} 前跑得完的任务才放行（${b.why}），` : "没约定时间，";
  const fg = (b.back?.length ? `；${who} 用得比预留多（${b.back.slice(0, 2).join("；")}），这部分不再放新任务` : "")
    + (b.tight ? `；快撞上限（${b.tight}），正在跑的照常跑完，要停和 ${who} 商量后点「现在停」` : "");
  if (b.swap) return (b.cutoff && now >= b.cutoff ? `到了约定时间：不再放新任务，跑完后在组内页「换人」换回给 ${who}` : `换给自己借来的，${cut}到时换回给 ${who}`) + fg;
  if (b.returning) return `要还给 ${who}：不再放新任务，${b.overdue ? "过了约定时间，" : ""}正在跑的跑完就还，还完开保活` + fg;
  return `借 ${who} 的，${cut}到点不停正在跑的，跑完自动还` + (b.for ? `；只给 ${b.for.join("、")} 用` : "") + (b.keep ? `；${keepText(b.keep)}` : "") + fg;
}
// 借用的小标签：对方在用、过了约定时间标红；悬停是 borrowText。pre、post：标签前后另加的字
function borrowBadge(b, now, pre = "", post = "") {
  const due = b.returning || (b.cutoff && now >= b.cutoff), bad = b.back?.length || b.overdue;
  const t = b.back?.length ? `${b.holder} 在用` : b.overdue ? "过了约定时间" : due ? (b.swap ? "到了约定时间" : "跑完就还") : b.cutoff ? `到 ${tsNear(b.cutoff)}` : "没约定时间";
  return `<span class="badge ${bad ? "b-bad" : "b-warn"}" ${tip(borrowText(b, now))}>${esc(pre + t + post)}</span>`;
}
// 共有算力池小角标（#251，项目页顶栏/资源卡/时间线与工作台主机表共用唯一实现）：紧凑显示「池」，悬停看所属共有池名（及独占/开放模式）
const POOL_BADGE_TEXT = "池", POOL_BADGE_W = 16;
const poolBadgeTip = h => !h?.pool_label ? "" : `所属共有算力池：${h.pool_label}`
  + (h.pool_exclusive != null ? `（${h.pool_exclusive ? "独占，请经池入口提交" : "开放，允许直投单台"}）` : "");
function poolBadge(h, svg) {
  if (!h?.pool_label) return "";
  const t = tip(poolBadgeTip(h));
  if (svg) {
    const w = POOL_BADGE_W, hh = 14, x = +svg.x.toFixed(1), y = +(svg.y - hh / 2).toFixed(1);
    return `<g class="pbadge-svg" ${t}><rect x="${x}" y="${y}" width="${w}" height="${hh}" rx="3.5" fill="var(--accent)" fill-opacity=".14" stroke="var(--accent)" stroke-opacity=".3" stroke-width=".75"/><text x="${+(x + w / 2).toFixed(1)}" y="${+(svg.y + 3.5).toFixed(1)}" text-anchor="middle" style="font-size:9.5px;font-weight:600;fill:var(--accent)">${POOL_BADGE_TEXT}</text></g>`;
  }
  return `<span class="badge b-pool" ${t}>${POOL_BADGE_TEXT}</span>`;
}
// 项目暂时没实验（快照的 no_work_since，collect 算）：两个页面同一个标记、同一句说明
const NO_WORK = "暂时没实验";
const noWorkTip = since => `${ts(since)} 起清单里没有待办，worker 上也没有本项目的任务：不占卡，往清单里加任务就接着跑。这时本项目不报主机的事（连不上、磁盘余量、空闲关机风险）`;
// 多主机负载均衡的提示（快照的 rebalance，看板的 rebalance.suggest 算的）：建议的那句话由看板写好（与 rebalance: 告警同一句），
// 整句是给实验会话的（rebalance: 告警），页面上只摆一个图标 rebalMark：有建议「⇄ 可提前」，只有挪不了的原因（why，不叫醒）同样的徽章换灰色「⇄」，
// 悬停（触屏点一下）看 rebalTip。项目页放在「全部完成」的时刻旁，工作台放在项目表「预计全部跑完」旁；窄屏只留「⇄」（#40）
// 时长不明（est：挪来的行没有估时，推演算不出能提前多少，#43）同样的灰色「⇄」，悬停看那一句
const rebalTip = r => r?.est ? "可能可以提前：" + r.text : r?.text ? "可以挪：" + r.text : r?.why?.length ? "别的主机接不了本项目的任务：\n" + r.why.join("\n") : "";
const rebalMark = r => r?.text && !r.est ? ` <span class="badge b-warn" ${tip(rebalTip(r))}>⇄<span class="hide-sm"> 可提前</span></span>`
  : r?.est ? ` <span class="badge b-mute" ${tip(rebalTip(r))}>⇄</span>`
  : r?.why?.length ? ` <span class="badge b-mute" ${tip(rebalTip(r))}>⇄</span>` : "";
const noWorkBadge = since => since ? `<span class="badge b-mute" ${tip(noWorkTip(since))}>${NO_WORK}</span>` : "";
// GPU 利用率（#51）：页面上的利用率文字只从 utilText 出（项目页、工作台、组内页共用）。b 是一张卡的「吃满程度」，或 utilAvg 合起来的几张卡：
// worker 每 10 秒一个样本，只有保活时按 0 算（保活只是占着卡），{now 此刻, m10 近 10 分钟, h1 近 1 小时, cov 1 小时窗口实际覆盖的秒数（worker 刚起时不满）,
// d1 近 24 小时（看板从主机曲线汇的，overview.gpu_busy）, d1cov, ka 此刻只有保活时保活顶出来的原值, how 换掉悬停里的口径说明（组内探针的样本）}。
// 没有 worker 的数据时 b 为空，raw 是探针此刻的值（不到 1 秒的瞬时值，含保活）。返回 {v 大字的数, big 大字, span 大字是多久的平均, ka 只有保活的那句（地方小时只写它）, sub 小字, tip 悬停}
const UTIL_HOW = "保活按 0 算：worker 每 10 秒采一次各卡的利用率，卡上只有保活时记 0（保活只是占着卡）。nvidia-smi 的利用率是有核函数在跑的时间占比";
const pctText = v => v == null ? "—" : Math.round(v) + "%";
// 几张卡合起来（bs 各卡的吃满程度，raws 各卡探针此刻的值）：没有 worker 数据的卡按探针此刻值逐卡补进平均，nprobe 记几张（#54 审查）；
// 近 10 分钟、1 小时有 worker 卡缺（样本不够）就不写那一档，24 小时同样；覆盖时长取有 worker 数据的卡里最短的
function utilAvg(bs, raws = []) {
  const cs = (bs || []).map((b, i) => b?.now != null ? b : raws[i] != null ? {now: raws[i], probe: true} : null).filter(Boolean);
  if (!cs.length) return null;
  const mean = v => v.reduce((a, x) => a + x, 0) / v.length, wk = cs.filter(c => !c.probe), nprobe = cs.length - wk.length;
  if (!wk.length) return {now: mean(cs.map(c => c.now)), probe: true};
  const m = k => wk.some(c => c[k] == null) ? null : mean(cs.map(c => c.probe ? c.now : c[k]));
  const nka = wk.filter(c => c.ka != null).length;
  return {now: mean(cs.map(c => c.now)), m10: m("m10"), h1: m("h1"), d1: nprobe || wk.some(c => c.d1 == null) ? null : mean(wk.map(c => c.d1)),
          cov: Math.min(...wk.map(c => c.cov || 0)), d1cov: Math.min(...wk.map(c => c.d1cov || 0)),
          ka: nka === cs.length ? mean(wk.map(c => c.ka)) : null, nka: nka < cs.length ? nka : 0, nprobe, how: wk[0].how};
}
function utilText(b, raw = null) {
  if (b?.probe) raw = b.now;
  if (b?.now == null || b.probe) return raw == null ? {v: null, big: "—", span: "", ka: "", sub: "", tip: "没有利用率数据"}
    : {v: raw, big: pctText(raw), span: "此刻", ka: "", sub: "", tip: `探针此刻的值 ${pctText(raw)}：不到 1 秒的瞬时值，含保活（worker 没在写状态，没有平均值）`};
  const v = b.h1 ?? b.m10 ?? b.now;
  const span = b.h1 != null ? (b.cov >= 3300 ? "近 1 小时" : `近 ${dur(b.cov)}`) : b.m10 != null ? "近 10 分钟" : "此刻";
  const ka = b.ka != null ? `只有保活（顶到 ${pctText(b.ka)}）` : b.nka ? `${b.nka} 张卡只有保活` : "";
  const d1 = b.d1 != null ? `${b.d1cov >= 82800 ? "24 小时" : dur(b.d1cov)} ${pctText(b.d1)}` : "";
  return {v, big: pctText(v), span, ka, sub: [span !== "此刻" ? `此刻 ${pctText(b.now)}` : "", d1, ka].filter(Boolean).join(" · "),
          tip: [`${span === "此刻" ? "此刻" : span + "平均"} ${pctText(v)}`, span !== "此刻" ? `此刻 ${pctText(b.now)}` : "", b.m10 != null && span !== "近 10 分钟" ? `近 10 分钟 ${pctText(b.m10)}` : "",
                d1 ? "近 " + d1 : "", ka, b.nprobe ? `其中 ${b.nprobe} 张卡没有 worker 数据，按探针此刻的值算（不到 1 秒的瞬时值，含保活）` : "", b.how || UTIL_HOW].filter(Boolean).join("\n")};
}
// 项目自己的平均利用率（collect.gpu_mine）：近 24 小时本项目的任务平均把所用的卡用到多少，按 卡数 × 时长 加权
const utilMine = m => m ? {big: pctText(m.util), sub: `本项目近 24 小时 ${pctText(m.util)}`,
  tip: `本项目近 24 小时 ${m.n} 个任务、${(m.card_s / 3600).toFixed(1)} 卡·时，平均把所用的卡用到 ${pctText(m.util)}（各任务运行期间所用卡的平均利用率，按卡数 × 时长加权；同卡有别的任务时是整卡合计）`} : null;
// 一台主机第 i 张卡的吃满程度（主机项的 busy：工作台 overview.gpu_busy 算好，项目快照照搬）
const gpuBusy = (h, i) => (h?.busy || []).find(b => b.i === i) || null;
// 告警给谁看（#142）：每条带 to（项目告警由 collect.alert 定，工作台主机告警由 alertsOf 定）：user 要用户处理、agent 只给实验会话、both 两边都要。
// forUser 给用户的文字（显眼列、算告警数；page 是 both 给用户的那份，没有就是 msg），与 collect.user_alerts 同一口径；forAgent 只给实验会话的
const forUser = al => al.filter(a => a.to !== "agent").map(a => a.page ?? a.msg);
const forAgent = al => al.filter(a => a.to === "agent").map(a => a.msg);
// 告警栏：给用户的进红框；只给实验会话的收进紧跟着的一行「给实验会话的提示」，默认折叠（open 由页面记在本机设置里，details.agal 开合时存）
function alertBox(al, open = false) {
  const u = forUser(al), g = forAgent(al);
  return (u.length ? `<div class="alerts">${u.map(m => `<div>⚠ ${esc(m)}</div>`).join("")}</div>` : "")
    + (g.length ? `<details class="agal sm mut" style="margin:-4px 0 10px;overflow-wrap:anywhere" ${open ? "open" : ""}><summary style="cursor:pointer;padding:2px 0;width:max-content;max-width:100%" `
      + `${tip("实验会话经 labmon wait 收到、自己处理的事（重跑失败的任务、补依赖、挪清单等），不算告警数")}>给实验会话的提示 ${g.length} 条</summary>`
      + `${g.map(m => `<div style="padding:2px 0 2px 14px">· ${esc(m)}</div>`).join("")}</details>` : "");
}
// 主机的名字只有三种写法：短名 hostShort（表格的主机列、小图与时间线的行名、车道名、KPI 的注；直接借来的叫「借 EVA」，其余用 id）；
// 全名 h.label（主机清单里写的，借来的是「节点（借 EVA）」：告警、标题、悬停里的整句话）；组内服务器 fName「使用人（id）」（overview.html）
// pool：项目快照里的算力池；ephemeral：推模式临时主机（#213、#281）
const hostShort = h => h.pool ? h.label || "算力池" : h.ephemeral ? `临时 ${h.id}` : h.borrow && !h.borrow.swap ? `借 ${h.borrow.holder}` : h.id;
const isColabHost = h => !!(h?.ephemeral && (/^colab/i.test(h.id || "") || /colab/i.test(h.label || "") || h.accel));
// Colab / 临时主机徽标（两页共用，#281）：有硬件规格时副标与悬停可见
function ephemeralBadge(h) {
  if (!h?.ephemeral) return "";
  const colab = isColabHost(h), acc = h.accel ? String(h.accel) : "";
  const lines = [colab ? "Colab 推模式临时实例" : "推模式临时主机",
    acc ? `硬件规格：${acc}` : "",
    h.cu_rate != null ? `估算消耗：约 ${h.cu_rate} CU/hr` : "",
    h.instance_id ? `实例：${h.instance_id}` : ""].filter(Boolean);
  const text = colab ? (acc ? `Colab · ${acc}` : "Colab") : "临时";
  return `<span class="badge b-mute" ${tip(lines.join("\n"))}>${esc(text)}</span>`;
}
// 临时主机已跑时长（相对 24h 参考上限，#281）：有 started_at 时显示「已跑 Xh / 24h」，无时回退「临时实例」
function ephemeralUptime(h, now) {
  const s = h?.started_at || h?.status?.info?.started;
  if (!s || !now) return {html: `<span class="mut">临时实例</span>`, tip: "推模式临时实例（无固定租期）"};
  const hrs = Math.max(0, (now - s) / 3600);
  const txt = `已跑 ${hrs < 10 ? hrs.toFixed(1) : Math.round(hrs)}h / 24h`;
  const col = hrs >= 23 ? "color:var(--bad);font-weight:600" : hrs >= 20 ? "color:var(--warn);font-weight:600" : "";
  return {
    html: `<span${col ? ` style="${col}"` : ""}>${txt}</span>`,
    tip: `实例 ${ts(s)} 启动，已运行 ${dur(Math.max(0, now - s))}（Colab 最长连续运行参考 24h，请确保 checkpoint 原子写入 Google Drive）`,
  };
}
// 个人 Colab 手动到期/刷新日提醒（#281）
function colabExpireInfo(expireAt, nowSec) {
  if (!expireAt || typeof expireAt !== "string") return null;
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(expireAt.trim());
  if (!m) return null;
  const target = new Date(+m[1], +m[2] - 1, +m[3], 23, 59, 59).getTime() / 1000;
  const days = Math.floor((target - (nowSec || Date.now() / 1000)) / 86400);
  if (days < 0) return {text: `已过期 ${-days} 天（${expireAt}）`, level: "bad", tip: `手动填写的到期/刷新日 ${expireAt} 已过`};
  if (days === 0) return {text: `今日到期（${expireAt}）`, level: "bad", tip: `手动填写的到期/刷新日为今天 ${expireAt}`};
  if (days <= 3) return {text: `${days} 天后到期（${expireAt}）`, level: "warn", tip: `手动填写的到期/刷新日为 ${expireAt}`};
  return {text: `到期 ${expireAt}（剩 ${days} 天）`, level: null, tip: `手动填写的到期/刷新日为 ${expireAt}`};
}
// 车道名：车道的 label（采集时按主机编号的“车道 N”，接可选的用途），后面接清单名（清单文件名去掉扩展名，#47：登记多了好认），
// 页面上有几台真主机时前面加主机短名。lane {host: 主机 id, label, queue?: 清单路径}，hosts 是页面上的主机表；laneLabel 不带主机（时间线行挂在主机表头下面）。
// 算力池（#89）不是主机、不算进“几台”：池里待放的车道总写「算力池」在前，免得和真主机的“车道 1”混在一起
const laneQ = lane => lane.queue ? lane.queue.replace(/^.*\//, "").replace(/(.)\.[^.]*$/, "$1") : "";
const laneLabel = lane => lane.label + (lane.queue ? " · " + laneQ(lane) : "");
// laneLabel 截到 px 宽（时间线左栏，#108）：「车道 N · 」原样留着，清单名省略中间，头上的 gpu_/cpu_ 与尾上区分清单的序号都看得到；
// 前缀占了一大半时（窄屏）只留「车道 N」与清单名的末段。tail 接在清单名后面一起截（如“（已归还）”）
const laneFit = (lane, px, tail = "") => {
  const q = laneQ(lane), pre = lane.label + (q ? " · " : ""), full = pre + q + tail;
  if (textW(full) <= px) return full;
  const n = px - charW("…");                         // fit 省略中间时头尾各占一半，省略号另算
  if (!q || textW(lane.label) + 20 > n) return fit(full, n, true);     // 连「车道 N」加清单名末尾三个字都放不下
  if (textW(pre) <= n / 2) return fit(q + tail, n, pre);
  const end = [...q + tail];
  let w = textW(lane.label), i = end.length;
  while (i > 0 && w + charW(end[i - 1]) <= n) w += charW(end[--i]);
  return lane.label + "…" + end.slice(i).join("");
};
const laneName = (lane, hosts) => {
  const h = hosts.find(x => x.id === lane.host);
  return (lane.pool || h?.pool || hosts.filter(x => !x.pool).length > 1 ? hostShort(h || (lane.pool ? {pool: true} : {id: lane.host})) + " " : "") + laneLabel(lane);
};
// 按估计字宽截断（中文约 12px，其余约 6.6px），超出加省略号。mid 时头尾各留一半、省略中间（任务名能区分的种子、变体常在结尾）；
// mid 是字符串时它是原样留着的前缀（如“⏸ ”、项目名），后面的再省略中间
const charW = c => c.charCodeAt(0) > 0x2e80 ? 12 : 6.6;
const textW = s => [...String(s)].reduce((w, c) => w + charW(c), 0);     // 按同一估计算的整串宽度（px）
function fit(s, px, mid = false) {
  const cw = charW;
  if (typeof mid === "string") return mid + fit(s, px - textW(mid), true);
  const a = [...String(s)];
  let w = 0, i = 0;
  while (i < a.length && (w += cw(a[i])) <= px) i++;
  if (i === a.length) return a.join("");
  if (!mid) return a.slice(0, i).join("") + "…";
  let h = 0, t = 0, b = a.length;
  for (i = 0; h + cw(a[i]) <= px / 2; i++) h += cw(a[i]);
  while (t + cw(a[b - 1]) <= px / 2) t += cw(a[--b]);
  return a.slice(0, i).join("") + "…" + a.slice(b).join("");
}

/* ---------- 时间线（项目页与工作台共用）：视图（预设窗口、拖动、缩放）、刻度、里程碑与现在线、任务条与占用条 ---------- */
// 预设窗口：相对现在的小时数（近景按现在前后的任务定宽，见 tlFit；全部包住所有任务），每次重画按页面上的“现在”重算。
// 拖动或缩放后视图记成相对现在的偏移（TL.v = {a, b}：开始、结束减去现在，秒；存在本机），宽度不变、随现在往右平移，现在线停在屏幕上原来的位置；
// 点预设或“回到现在”恢复按预设定视图
const TL_WIN = {near: ["近景", null, null], day: ["24 小时", -6, 18], week: ["7 天", -6 * 24, 24], all: ["全部", null, null]};
const TL_MIN = 1800, TL_MAX = 60 * 86400;
// 每页调一次 tlInit：get、set 读写本机存的设置（视图 tlv、预设 tl），now() 是页面上的“现在”，all() 给「全部」要包住的 [最早, 最晚]（经 tlSpan 算），
// near() 给近景定宽用的任务 [[开始, 结束, 在跑或推演里排队], …]（经 tlFit 算），draw() 重画
// key：窄屏上图例点开了没有；fw：近景上一次取的宽度档（滞回用，不存本机）；lk/lkt/last/cur：拖动与滚轮缩放平移时的布局锁（#143）
const TL = {cfg: null, v: null, g: null, d: null, sw: false, raf: 0, save: 0, key: false, fw: 0, lk: null, lkt: 0, last: null, cur: null};
function tlInit(cfg) {
  TL.cfg = cfg;
  clearTimeout(TL.lkt); TL.lkt = 0; TL.lk = null; TL.last = null; TL.cur = null;
  const v = cfg.get("tlv", null); TL.v = Number.isFinite(v?.a) && Number.isFinite(v?.b) ? v : null;
}
// 拖动/滚轮平移或缩放期间锁住行结构（#143）：行不消失、行高与左栏宽只升不缩，松手或停止滚动后再收紧。两页共用
function tlFrame() { TL.cur = {rows: new Map(), lw: 0, flags: Object.create(null)}; }
function tlLock() {
  clearTimeout(TL.lkt); TL.lkt = 0;
  if (TL.lk) return;
  const s = TL.last;
  TL.lk = s ? {rows: new Map(s.rows), lw: s.lw, flags: Object.assign(Object.create(null), s.flags)}
    : {rows: new Map(), lw: 0, flags: Object.create(null)};
}
function tlUnlock(delay = 0) {
  clearTimeout(TL.lkt); TL.lkt = 0;
  if (!TL.lk) return;
  if (delay > 0) { TL.lkt = setTimeout(() => tlUnlock(0), delay); return; }
  TL.lk = null;
  if (!TL.raf) TL.raf = setTimeout(() => { TL.raf = 0; TL.cfg?.draw(); }, 16);
}
function tlLockFlag(key, val) {
  const on = !!val || !!TL.lk?.flags[key];
  if (on && TL.cur) TL.cur.flags[key] = true;
  if (on && TL.lk) TL.lk.flags[key] = true;
  return on;
}
function tlLockRow(key, h) {
  const out = TL.lk ? Math.max(h, TL.lk.rows.get(key) || 0) : h;
  if (TL.cur) TL.cur.rows.set(key, out);
  if (TL.lk) TL.lk.rows.set(key, out);
  return out;
}
function tlLockLw(lw) {
  const v = Number.isFinite(lw) ? lw : 0;
  const out = TL.lk ? Math.max(v, TL.lk.lw || 0) : v;
  if (TL.cur) TL.cur.lw = out;
  if (TL.lk) TL.lk.lw = out;
  return out;
}
// 「全部」要包住的区间：各起点里最早的、各终点里最晚的；没有数据的一端为 null（tlRange 按现在算）
const tlSpan = (starts, ends) => [starts.length ? Math.min(...starts) : null, ends.length ? Math.max(...ends) : null];
// 近景（Issue #49）：现在线放在左起 1/3，往后包住在跑与排队的任务里最晚的结束、右边留 5%；最晚的离现在超过 90% 分位的 2 倍时算长尾，改取 90% 分位。
// 预计结束不晚于现在的（无估时的预计结束就是现在）说不准，不进分位：否则分位压到现在，往后真有的排队都会被当成长尾截掉。
// 宽度向上取到 TL_FIT.lad 的一档（1 到 24 小时）；放大立即放，缩小要等要的宽度不超过下一档的 85%（TL.fw 记上一档），刷新时不来回跳。
// 没有在跑、排队的（项目暂时没实验）：取最宽的一档，现在线照样在 1/3（−8/+16 小时），不为包住已跑完的任务缩放（Issue #85）
const TL_FIT = {lad: [1, 1.5, 2, 3, 4, 5, 6, 8, 10, 12, 16, 20, 24].map(h => h * 3600), now: 1 / 3, pad: .05, q: .9, tail: 2, shrink: .85};
function tlFit(now, xs) {
  const F = TL_FIT, lad = F.lad, live = xs.filter(x => x[2] && x[1] != null), up = live.map(x => x[1]).filter(t => t > now).sort((a, b) => a - b);
  let W = lad[lad.length - 1];
  if (!live.length) TL.fw = 0;
  else {
    let need = 0;     // 要的宽度
    if (up.length) {
      const q = up[Math.ceil(up.length * F.q) - 1], mx = up[up.length - 1];
      need = ((mx - now <= F.tail * (q - now) ? mx : q) - now) / (1 - F.now - F.pad);
    }
    const k = lad.findIndex(w => w >= need), p = lad.indexOf(TL.fw);
    if (k >= 0) W = lad[k];
    if (p > lad.indexOf(W) && need > F.shrink * lad[p - 1]) W = TL.fw;
    TL.fw = W;
  }
  return {t0: now - F.now * W, t1: now + (1 - F.now) * W};
}
function tlRange() {
  const now = TL.cfg.now();
  if (TL.v) return {t0: now + TL.v.a, t1: now + TL.v.b};
  const m = TL.cfg.get("tl", "near"), mode = TL_WIN[m] ? m : "near";
  if (mode === "near") return tlFit(now, TL.cfg.near());
  if (mode !== "all") { const [, b, a] = TL_WIN[mode]; return {t0: now + b * 3600, t1: now + a * 3600}; }
  const [s, e] = TL.cfg.all();
  const t0 = Math.max(now - TL_MAX + 86400, Math.min(now - 1800, s ?? now)), t1 = Math.max(now + 1800, e ?? now), p = (t1 - t0) * .02;
  return {t0: t0 - p, t1: t1 + p};
}
function tlView(r, persist = true) {
  const now = TL.cfg.now();
  let span = Math.min(TL_MAX, Math.max(TL_MIN, r.t1 - r.t0)), t0 = r.t0 + (r.t1 - r.t0 - span) / 2;
  t0 = Math.min(now + 30 * 86400 - span, Math.max(now - TL_MAX, t0));     // 往前最多 60 天、往后 30 天
  TL.v = {a: t0 - now, b: t0 + span - now};
  if (persist) { clearTimeout(TL.save); TL.save = setTimeout(() => TL.cfg.set("tlv", TL.v), 250); }
  if (!TL.raf) TL.raf = setTimeout(() => { TL.raf = 0; TL.cfg.draw(); }, 16);   // 约一帧画一次（页面在后台时 rAF 不触发，用定时器）
}
function tlZoom(f, ta) {
  const r = tlRange();
  ta = ta ?? (r.t0 + r.t1) / 2;
  tlView({t0: ta - (ta - r.t0) * f, t1: ta + (r.t1 - ta) * f});
}
function tlFollow() { clearTimeout(TL.lkt); TL.lkt = 0; TL.lk = null; TL.v = null; TL.cfg.set("tlv", null); TL.cfg.draw(); }
// 时间线右上的按钮；点击由各页的 click 交给 tlClick（data-tl 换预设，data-tz 缩放或回到现在，data-tlkey 窄屏开关图例）
function tlBar() {
  const mode = TL.cfg.get("tl", "near"), r = tlRange();
  return (TL.v ? `<span class="sm mut nw hide-sm">${ts(r.t0)} – ${ts(r.t1)}</span><button class="btn" data-tz="now">回到现在</button>` : "")
    + `<div class="seg"><button data-tz="out" ${tip("缩小（看更长）")}>−</button><button data-tz="in" ${tip("放大")}>＋</button></div>`
    + `<div class="seg">${Object.entries(TL_WIN).map(([k, v]) => `<button data-tl="${k}" class="${!TL.v && k === mode ? "on" : ""}">${v[0]}</button>`).join("")}</div>`;
}
function tlClick(t) {
  if (t.dataset.tlkey != null) { TL.key = !TL.key; TL.cfg.draw(); }
  else if (t.dataset.tl) { TL.cfg.set("tl", t.dataset.tl); tlFollow(); }
  else if (t.dataset.tz === "now") tlFollow();
  else tlZoom(t.dataset.tz === "in" ? 0.5 : 2);
}
function tlTicks(t0, t1, n) {
  const st = [10, 15, 30, 60, 120, 180, 360, 720, 1440, 2880, 4320, 10080].find(m => (t1 - t0) / 60 / m <= n) || 20160;
  const d = new Date(t0 * 1000); d.setHours(0, 0, 0, 0);
  const out = [];
  for (let t = d.getTime() / 1000; t <= t1; ) {
    if (t >= t0) out.push(t);
    if (st < 1440) t += st * 60;
    else { const x = new Date(t * 1000); x.setDate(x.getDate() + st / 1440); t = x.getTime() / 1000; }
  }
  return {out, st};
}
// 拖动平移、⌘/Ctrl＋滚轮或双指捏合缩放、横向滚动平移、双击放大（Shift 缩小）；box 本身不随重画替换，只绑一次
function tlBind(box) {
  if (box._tl) return;
  box._tl = 1;
  const tAt = cx => { const b = box.getBoundingClientRect(); return TL.g.t0 + (cx - b.left - TL.g.L) / TL.g.PW * (TL.g.t1 - TL.g.t0); };
  box.addEventListener("pointerdown", e => {
    if (e.button || !TL.g || e.target.closest("[data-tlall],[data-tz]")) return;
    TL.d = {x: e.clientX, r: tlRange(), moved: false};
  });
  box.addEventListener("pointermove", e => {
    if (!TL.d) return;
    const dx = e.clientX - TL.d.x;
    if (!TL.d.moved) {
      if (Math.abs(dx) < 4) return;
      TL.d.moved = true; box.setPointerCapture(e.pointerId); box.classList.add("drag"); hideTip(); tlLock();
    }
    const k = (TL.d.r.t1 - TL.d.r.t0) / TL.g.PW;
    tlView({t0: TL.d.r.t0 - dx * k, t1: TL.d.r.t1 - dx * k});
  });
  const up = () => {
    const moved = !!TL.d?.moved;
    if (moved) { TL.sw = true; setTimeout(() => { TL.sw = false; }, 0); }
    TL.d = null; box.classList.remove("drag");
    if (moved) tlUnlock(0);
  };
  box.addEventListener("pointerup", up);
  box.addEventListener("pointercancel", up);
  box.addEventListener("click", e => { if (TL.sw) { e.stopPropagation(); e.preventDefault(); TL.sw = false; } }, true);
  box.addEventListener("wheel", e => {
    if (!TL.g) return;
    const r = tlRange(), span = r.t1 - r.t0;
    if (e.ctrlKey || e.metaKey) {
      e.preventDefault();
      tlLock();
      tlZoom(Math.exp(Math.max(-1, Math.min(1, e.deltaY * (e.deltaMode ? 0.05 : 0.01)))), tAt(e.clientX));
      tlUnlock(250);
    } else if (Math.abs(e.deltaX) > Math.abs(e.deltaY)) {
      e.preventDefault();
      tlLock();
      const k = span / TL.g.PW * (e.deltaMode ? 16 : 1);
      tlView({t0: r.t0 + e.deltaX * k, t1: r.t1 + e.deltaX * k});
      tlUnlock(250);
    }
  }, {passive: false});
  box.addEventListener("dblclick", e => {
    if (!TL.g || e.target.closest("rect.j,[data-tlall],[data-tz]")) return;
    e.preventDefault();
    tlZoom(e.shiftKey ? 2 : 0.5, tAt(e.clientX));
  });
}
const TL_DEFS = `<defs><pattern id="hatch" width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
  <line x1="0" y1="0" x2="0" y2="6" stroke="var(--muted)" stroke-width="2.2" stroke-opacity=".55"/></pattern>
  <pattern id="hatchw" width="5" height="5" patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><rect width="5" height="5" fill="var(--warn)" fill-opacity=".12"/><line x1="0" y1="0" x2="0" y2="5" stroke="var(--warn)" stroke-width="2" stroke-opacity=".6"/></pattern></defs>`;
// 画一张时间线：rows [{h 行高, draw(y, h, c) → SVG, sep 一组的开头（上面画一条贯通的分隔线，第一行不画）}]，c 是坐标换算 {t0, t1, W, L, R, PW, narrow, X, cl, win, seen}；
// o.markers [{label, ts}] 里程碑（视图之外的在右上角提示），o.seen 收集图上出现了哪些画法（图例只列这些），o.under(c) 画完各行后调、结果压在各行下面。TL.g 记下布局给拖动、缩放换算
function tlSvg(box, rows, o = {}) {
  const {t0, t1} = tlRange(), now = TL.cfg.now(), seen = o.seen || new Set();
  for (const r of rows) if (r.key) r.h = tlLockRow(r.key, r.h);
  const lw = tlLockLw(o.lw || 0);
  // 左栏缺省宽屏 116、窄屏 82；o.lw 是行名要多宽（项目页按车道名算，#108），在缺省与上限（宽屏 200、不超过整宽的 30%；窄屏 128、不超过 34%）之间取
  const W = Math.max(300, box.clientWidth), narrow = W < 600, L0 = narrow ? 82 : 116, R = narrow ? 8 : 84;
  const L = Math.round(Math.max(L0, Math.min(lw, narrow ? 128 : 200, W * (narrow ? .34 : .3)))), PW = W - L - R;
  TL.g = {L, PW, t0, t1};
  const X = t => L + PW * (t - t0) / (t1 - t0);
  const c = {t0, t1, W, L, R, PW, narrow, X, cl: t => Math.max(L, Math.min(W - R, X(t))), win: (a, b) => a != null && b != null && b > t0 && a < t1, seen};
  const rowGap = (r, i) => !i ? 0 : r.sep ? (o.sepGap ?? 4) : (r.gap ?? 2);
  const bodyH = rows.reduce((acc, r, i) => acc + rowGap(r, i) + r.h, 0);
  const dual = 14 + bodyH + 18 > 400;                     // 高时间线（H > 400）顶部也渲染一行时间刻度，与底部共用 tlTicks
  const top = dual ? 28 : 14, H = top + bodyH + 18, tk = tlTicks(t0, t1, Math.min(10, PW / 64));
  const tickLab = (x, y, lab, sty, pos) => `<text class="tl-tick tl-tick-${pos}" x="${x}" y="${y}" text-anchor="middle" style="${sty}">${lab}</text>`;
  let s = "", y = top;
  for (const t of tk.out) {
    const d = new Date(t * 1000), mid = !d.getHours() && !d.getMinutes();
    const lab = mid || tk.st >= 1440 ? tsMD(t) : tsHM(t);
    const sty = mid && tk.st < 1440 ? "fill:var(--fg)" : "";
    s += `<line x1="${X(t)}" x2="${X(t)}" y1="${top - 2}" y2="${H - 16}" stroke="var(--line)"/>`
      + (dual ? tickLab(X(t), top - 6, lab, sty, "top") : "")
      + tickLab(X(t), H - 3, lab, sty, "bot");
  }
  let body = "";
  for (let i = 0; i < rows.length; i++) {
    const r = rows[i], g = rowGap(r, i);
    y += g;
    if (r.sep && i) body += `<line x1="0" x2="${W}" y1="${y - g / 2}" y2="${y - g / 2}" stroke="var(--muted)" stroke-opacity=".4"/>`;
    body += r.draw(y, r.h, c); y += r.h;
  }
  s += (o.under ? o.under(c) : "") + body;     // 压在各行下面的（项目页的依赖连线：行里把条的位置记进 c，画完各行再连，插到各行前面，条盖住线）
  const ms = o.markers || [], off = ms.filter(m => m.ts > t1);
  if (ms.some(m => m.ts >= t0 && m.ts <= t1)) seen.add("marker");
  for (const m of ms) if (m.ts >= t0 && m.ts <= t1) s += `<line x1="${X(m.ts)}" x2="${X(m.ts)}" y1="2" y2="${H - 16}" stroke="var(--fg)" stroke-dasharray="4 3" stroke-opacity=".6"/>
      <text x="${X(m.ts) + 3}" y="9" style="fill:var(--fg)">${esc(m.label)}</text>`;
  if (off.length) s += `<text x="${W}" y="9" text-anchor="end" style="fill:var(--fg)" ${tip(off.map(m => `${m.label}  ${ts(m.ts)}`).join("\n"))}>${esc(off[0].label)} ${tsMD(off[0].ts)} ›</text>`;
  if (now >= t0 && now <= t1) { seen.add("now"); s += `<line x1="${X(now)}" x2="${X(now)}" y1="2" y2="${H - 16}" stroke="var(--bad)" stroke-width="1.5"/>`; }
  else s += `<text x="${now < t0 ? L : W - R}" y="${H - 3}" text-anchor="${now < t0 ? "start" : "end"}" class="more" data-tz="now" style="fill:var(--accent);cursor:pointer">${now < t0 ? "‹ 现在" : "现在 ›"}</text>`;
  if (TL.cur) { TL.last = TL.cur; TL.cur = null; }
  return `<svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}">${TL_DEFS}<defs><clipPath id="tlclip"><rect x="${L}" y="0" width="${PW}" height="${H}"/></clipPath></defs>${s}</svg>`;
}
// 画进页面：#tlbox 放图（下面接 o.more），#tlkey 放图例，#tlbar 放按钮。o.items(seen) 在画完后调，给图例条目 [[画法, 说明, 样例, 可选悬停详述]]，
// 只列图上出现了的（seen）；里程碑与现在线各页都有，接在最后
function tlShow(box, rows, o = {}) {
  const seen = new Set();
  box.innerHTML = tlSvg(box, rows, {markers: o.markers, seen, under: o.under, lw: o.lw, sepGap: o.sepGap}) + (o.more || "");
  const items = [...(o.items ? o.items(seen) : []),
    ["marker", "里程碑", tlSw(`<line x1="6" x2="6" y1="0" y2="12" stroke="var(--fg)" stroke-dasharray="4 3" stroke-opacity=".6"/>`, 12)],
    ["now", "现在", tlSw(`<line x1="6" x2="6" y1="0" y2="12" stroke="var(--bad)" stroke-width="1.5"/>`, 12)]];
  const key = document.getElementById("tlkey");
  if (key) {
    key.innerHTML = `<button class="btn tlkey-b" data-tlkey>${TL.key ? "收起图例" : "图例"}</button><div class="tlkey-l">${items.filter(([k]) => seen.has(k)).map(([, t, sw, h]) => `<span${h ? " " + tip(h) : ""}>${sw}${t}</span>`).join("")}
      <span class="mut ops">拖动平移 · ⌘/Ctrl＋滚轮或双指捏合缩放 · 双击放大，Shift＋双击缩小</span></div>`;
    key.classList.toggle("open", TL.key);
  }
  const bar = document.getElementById("tlbar"); if (bar) bar.innerHTML = tlBar();
  return seen;
}
const tlSw = (svg, w = 22) => `<svg width="${w}" height="12" viewBox="0 0 ${w} 12">${svg}</svg>`;     // 图例里的小样例
// 任务条（图例的样例也用它，保证和图上一样）。st：done / running / queued / failed；hold 占位任务画斜纹；frac 运行中填色到哪；a 附加属性（class、提示）
function jobBar(st, hold, x, y, w, h, c, frac, a = "") {
  if (hold) return `<rect x="${x}" y="${y}" width="${w}" height="${h}" rx="3" fill="url(#hatch)" stroke="${st === "running" ? "var(--fg)" : "var(--muted)"}"
    stroke-width="${st === "running" ? 1.3 : 1}" stroke-dasharray="${st === "queued" ? "3 2" : "none"}" ${a}/>`;
  if (st === "running") return `<rect x="${x}" y="${y}" width="${w}" height="${h}" rx="3" fill="${c}" fill-opacity=".28"/>
    <rect x="${x}" y="${y}" width="${Math.min(w, Math.max(2, w * frac))}" height="${h}" rx="3" fill="${c}"/>
    <rect x="${x}" y="${y}" width="${w}" height="${h}" rx="3" fill="transparent" stroke="var(--fg)" stroke-width="1.3" ${a}/>`;
  return `<rect x="${x}" y="${y}" width="${w}" height="${h}" rx="3" fill="${c}" fill-opacity="${st === "queued" ? .2 : .85}" ${st === "queued" ? `stroke="${c}" stroke-opacity=".55"` : ""} ${a}/>`;
}
// 重叠的条分到几道（先开始的先占道；同起点按结束时间与名字稳定排序），各条写上道号 k；返回道数。按全部任务调用，道号不随视图平移跳动（#143）
function tlPack(items) {
  const ends = [];
  const ord = [...items].sort((a, b) => (a.t0 - b.t0) || (a.t1 - b.t1) || String(a.id ?? a.n ?? "").localeCompare(String(b.id ?? b.n ?? "")));
  for (const x of ord) {
    let k = ends.findIndex(e => e <= x.t0 + 1);
    if (k < 0) { k = ends.length; ends.push(0); }
    ends[k] = x.t1; x.k = k;
  }
  return Math.max(1, ends.length);
}
// 单行道数与行高布局（#178）：totalN 是全量分道数；cap 是该行上限（不超过整行高度 maxH 按 minStep 放得下的道数）。
// 1..hi 道用舒适步长（base + step*(n-1)，封顶 softH）；超 hi 道压细到 minStep（如 8px），总高封顶 maxH。totalN > cap 时 over=true，多出来的在最后一道经 tlOverflow 聚合，不叠在同一位置
const OCC_CARD_LAY = {base: 15, step: 10, hi: 3, softH: 32, maxH: 44, pad: 2, minStep: 7, cap: 6};
const OCC_OTHER_LAY = {base: 16, step: 10, hi: 4, softH: 44, maxH: 52, pad: 2, minStep: 8, cap: 6};
function tlRowLayout(totalN, o) {
  const pad = o.pad ?? 4, ms = o.minStep || 8, hi = o.hi || 4;
  const cap = Math.max(1, Math.min(o.cap || 6, Math.floor((o.maxH - pad) / ms)));
  const n = Math.max(1, Math.min(totalN || 1, cap)), over = cap >= 2 && totalN > cap;
  const soft = o.softH ?? (o.base + o.step * (hi - 1));
  const h = n <= hi ? Math.min(soft, o.base + o.step * (n - 1)) : Math.min(o.maxH, Math.max(soft, pad + ms * n));
  return {n, total: Math.max(1, totalN || 1), cap, over, h, pad};
}
// 溢出到最后一道（k >= cap - 1）的可见条按像素区间合并成互不相交的簇：单条照常画，多条合成「+N 个」聚合条（tlMoreBar），任何时候不重叠（#178）
function tlOverflow(items, c, gap = 1) {
  const xs = items.map(x => {
    const xa = c.cl(x.t0), xe = Math.max(xa + 1.5, c.cl(x.t1) - gap);
    return {x, xa, xe};
  }).sort((a, b) => (a.xa - b.xa) || (a.xe - b.xe));
  const cs = [];
  for (const it of xs) {
    const m = cs[cs.length - 1];
    if (m && it.xa < m.xe + 1) { m.xe = Math.max(m.xe, it.xe); m.items.push(it.x); }
    else cs.push({xa: it.xa, xe: it.xe, items: [it.x]});
  }
  return cs;
}
function tlMoreBar(xa, y, w, h, lines) {
  const n = lines.length, show = lines.slice(0, 12);
  const tt = `另有 ${n} 个重叠任务：\n` + show.join("\n") + (n > show.length ? `\n… 另 ${n - show.length} 个` : "");
  const lab = w >= 36 ? `+${n} 个` : w >= 20 ? `+${n}` : "";
  return `<rect class="tl-more" x="${xa}" y="${y}" width="${w}" height="${h}" rx="3" fill="var(--muted)" fill-opacity=".22" stroke="var(--muted)" stroke-width="1" stroke-dasharray="2 2" ${tip(tt)}/>`
    + (lab ? `<text x="${xa + w / 2}" y="${y + h / 2 + 3}" text-anchor="middle" style="font-size:${h < 10 ? 8.5 : 9.5}px;pointer-events:none;fill:var(--fg)">${esc(lab)}</text>` : "");
}
// 占用（工作台 overview.occupancy 给的：st 为 done / failed / stop / run / plan）画成任务条：已跑的实色，在跑的描边并填到现在，worker 推演的排队淡色；
// 同一行里重叠的分道（先 tlPack + tlRowLayout）。o.color(项目)、o.text(条) 悬停、o.label(条) 条上的字（放不下省略中间）、o.pre(条) 可选的前缀（原样留着）
const OCC_BAR = {done: "done", failed: "failed", stop: "failed", run: "running", plan: "queued"};
const OCC_ST = {done: "完成", failed: "失败", stop: "停下重排", run: "在跑（预计结束）", plan: "预计（推演）"};
const occState = x => OCC_ST[x.st];
// 推演里排在谁后面（[[项目, 任务]]）；self 是本项目时只写任务名
const afterText = (af, self) => af.map(([p, n]) => p === self ? n : `${p} 的 ${n}`).join("、");
// 占用条的图例条目（接进 tlShow 的 items）；pre 是说明前缀
const occItems = (c, pre = "") => [
  ["occ_done", pre + "已跑", tlSw(jobBar("done", false, 0, 1, 22, 10, c))],
  ["occ_run", pre + "在跑", tlSw(jobBar("running", false, 0, 1, 22, 10, c, .45)), pre + "在跑（填色到现在，右端是预计结束）"],
  ["occ_plan", pre + "排队", tlSw(jobBar("queued", false, 0, 1, 22, 10, c)), pre + "排队（worker 按放行规则推演，同机各项目一起排）"],
  ["occ_failed", pre + "失败", tlSw(jobBar("failed", false, 0, 1, 22, 10, "var(--bad)"))],
  ["occ_stop", pre + "停下重排", tlSw(jobBar("failed", false, 0, 1, 22, 10, "var(--warn)"))]];
function occBars(items, y, h, c, o) {
  const lay = o.layout || {n: Math.max(1, ...items.map(x => (x.k || 0) + 1)), over: false};
  const pad = lay.pad ?? 4, th = (h - pad) / lay.n, bh = Math.max(4, th - 1), now = TL.cfg.now();
  const drawOne = (x, k) => {
    const xa = c.cl(x.t0), w = Math.max(1.5, c.cl(x.t1) - xa - 1), yy = y + pad / 2 + k * th, st = OCC_BAR[x.st] || "done";
    c.seen.add("occ_" + x.st);
    let r = jobBar(st, false, xa, yy, w, bh, x.st === "failed" ? "var(--bad)" : x.st === "stop" ? "var(--warn)" : o.color(x.p),
      st === "running" ? (c.cl(now) - xa) / w : 0, tip(o.text(x)));
    if (w > 56 && th >= 11.5) r += `<text x="${xa + 4}" y="${yy + bh / 2 + 3.5}" style="font-size:10px;pointer-events:none;fill:${st === "done" ? "#fff" : "var(--fg)"}">${esc(fit(o.label(x), w - 8, o.pre ? o.pre(x) : true))}</text>`;
    return r;
  };
  let s = "";
  const over = [];
  for (const x of items) {
    if (!c.win(x.t0, x.t1)) continue;
    if (lay.over && x.k >= lay.n - 1) over.push(x);
    else s += drawOne(x, x.k || 0);
  }
  if (over.length) {
    const yy = y + pad / 2 + (lay.n - 1) * th;
    for (const m of tlOverflow(over, c, 1)) {
      if (m.items.length === 1) s += drawOne(m.items[0], lay.n - 1);
      else s += tlMoreBar(m.xa, yy, Math.max(1.5, m.xe - m.xa), bh,
        m.items.map(x => `${o.pre ? o.pre(x) : x.p ? x.p + " " : ""}${o.label(x)}（${occState(x)} · ${tsHM(x.t0)}→${tsHM(x.t1)}）`));
    }
  }
  return s;
}

