// 页面脚本的行为检查：一个 node 进程里把渲染好的两页脚本装进 DOM 桩，按路由渲染、调各渲染函数，所有断言的结果以 JSON 打到标准输出。
// 由 tests/test_pages.py 调：node check.js <工作目录>，目录里是渲染出的 overview.html / page.html、抽出的 overview.js / page.js 与各份快照 JSON。
// 结果一行一条：{g: 分组, n: 名字, ok: 过没过, d: 没过时的说明}
"use strict";
const fs = require("fs"), path = require("path"), vm = require("vm");
const {mkCtx, run, fire, freezeClock, leak, ADMIN_ME} = require("./dom.js");

const W = process.argv[2];
const rd = f => fs.readFileSync(path.join(W, f), "utf8"), J = f => JSON.parse(rd(f));
const R = [];
const ok = (c, m) => { if (!c) throw new Error(m || "断言不成立"); };
function t(g, n, fn) {
  try { fn(); R.push({g, n, ok: true}); }
  catch (e) { R.push({g, n, ok: false, d: String(e && e.stack || e).split("\n").slice(0, 4).join(" | ").slice(0, 700)}); }
}
const PEND = [];          // 异步的用例（要等页面里的 await 走完）：结果在全部跑完后一起打出
function ta(g, n, fn) {
  PEND.push(Promise.resolve().then(fn).then(() => R.push({g, n, ok: true}),
    e => R.push({g, n, ok: false, d: String(e && e.stack || e).split("\n").slice(0, 4).join(" | ").slice(0, 700)})));
}
const BAD = "<i id=x>&\"'<!--<script>alert(1)</script>", BADESC = "&lt;i id=x&gt;&amp;&quot;&#39;&lt;!--&lt;script&gt;alert(1)&lt;/script&gt;";
const raw = html => /<i id=x>|<script>|<!--/.test(html);                 // 没转义的恶意字符串
const text = html => html.replace(/<[^>]*>/g, " ");
const tips = html => [...html.matchAll(/data-tip="([^"]*)"/g)].map(m => m[1]).join("\n");
const clean = (html, what) => { ok(!raw(html), what + "：恶意字符串没转义"); const l = leak(html); ok(!l, what + "：文字里有 " + JSON.stringify(l)); };
const has = (html, s, what) => ok(html.includes(s), `${what || ""}缺 ${JSON.stringify(s)}`);

const ovSrc = rd("overview.js"), pgSrc = rd("page.js"), ovHtml = rd("overview.html"), pgHtml = rd("page.html");
const OV = J("overview.json"), OVE = J("overview_evil.json"), OVS = J("overview_sparse.json");
const PJ = J("project.json"), PJE = J("project_evil.json"), PJS = J("project_sparse.json");
const RR = J("records.json"), RRE = J("records_evil.json"), EVN = J("events.json"), EVE = J("events_evil.json");
const NOW = OV.generated, H = 3600;
freezeClock(NOW * 1000);          // “现在”就是快照的 generated（dom.js 的 freezeClock），之后建的上下文都用它

/* ---------- 语法：与 node --check 同一件事（vm.Script 只编译不执行） ---------- */
t("syntax", "overview.html 的脚本能编译", () => new vm.Script(ovSrc, {filename: "overview.js"}));
t("syntax", "page.html 的脚本能编译", () => new vm.Script(pgSrc, {filename: "page.js"}));
t("syntax", "渲染出的页面各只有一个 <script>（数据里的 </script> 已转义）", () => {
  ok((ovHtml.match(/<script/g) || []).length === 1 && (pgHtml.match(/<script/g) || []).length === 1);
});
t("syntax", "总览五格指标的样式还在（k5 最后一格跨两列）", () => ok(/\.kpis\.k5 > :last-child \{ grid-column: span 2; \}/.test(ovHtml)));

/* ---------- 工作台：装进 DOM 桩 ---------- */
// 记录卡与记录页不经 fetch：recs 直接给出构造的记录；记录卡展开、选项卡由 RTAB 指定
function ovCtx(snap, rows, hash = "#/") {
  const O = mkCtx(ovSrc, snap, {hash, filename: "overview.js"});
  O.RR = rows; O.RTAB = "done";
  run(O, `recs = q => ({rows: RR, gen: D.generated, more: true});
    ST.get = (k, d) => k === "cardOpen" ? {"rec:home": 1, "rec:host": 1} : k === "recTab" ? {home: RTAB, host: RTAB} : d;`);
  return O;
}
const el = (O, id) => O.document.getElementById(id);
// 焦点停在能打字的控件里（Safari 点按钮、勾选框不挪焦点，#129、#131）：BL 记失焦次数，extra 给它别的字段
const focusText = (C, extra = "") => run(C, `BL = 0; document.activeElement = {type: "text", ${extra} closest(s) { return /input/.test(s) ? this : null; }, blur() { BL++; document.activeElement = document.body; }}`);
// 点一下：假元素的 closest 只认 sel 这一个选择器（返回自己），经页面在 document 上登记的点击处理分发（捕获的在先，dom.js 的 fire）
function clickOn(C, sel, dataset = {}, type = "submit") { const b = {dataset, type, disabled: false, tagName: "BUTTON", closest: q => q === sel ? b : null}; fire(C, "click", b); return b; }
function page(O, hash) {                 // 按路由整页渲染（顶栏 + 正文 + 抽屉）
  O.location.hash = hash;
  const key = hash.startsWith("#/log") ? (hash.split("?")[1] || "") : null;
  if (key != null) { O.LKEY = key; run(O, "LOG = {key: new URLSearchParams(LKEY).toString(), rows: RR, cats: {op: 3, link: 4}, gen: D.generated, more: true}"); }
  run(O, "render(true)");
  return [el(O, "top").innerHTML, el(O, "view").innerHTML, run(O, 'DKEY ? document.getElementById("drwp").innerHTML : ""')].join("\n");
}
const ROUTES = ["#/", "#/pool", "#/host/srv-a", "#/host/srv-b", "#/host/没有的主机", "#/log", "#/log?host=srv-a&cat=op&q=x", "#/fleet", "#/fleet/people", "#/fleet/disk",
  "#/fleet/srv/srv-a", "#/fleet/srv/srv-b", "#/fleet/srv/srv-d", "#/fleet/srv/srv-e", "#/fleet/srv/srv-f", "#/fleet/srv/srv-g", "#/fleet/srv/gone",
  "#/fleet/people/OWNER", "#/fleet/people/ME", "#/fleet/people/NEWBIE", "#/fleet/people/GONE", "#/fleet/people/+", "#/fleet/new/pool-public", "#/fleet/guard",
  "#/fleet/srv/srv-h", "#/fleet/renew", "#/fleet/renew/srv-d", "#/p/proj-a", "#/p/proj-a/manage", "#/p/proj-b/manage", "#/p/没有的项目/manage", "#/archived"];
const TABS = ["done", "events", "ovr", "queues", "deps", "lw", "log"];

let O;
t("overview", "正常快照装得起来，打开就是总览", () => { O = ovCtx(OV, RR); has(el(O, "view").innerHTML, "运行 / 等待", "总览"); });
for (const r of ROUTES) t("overview", `路由 ${r} 渲染不报错、不漏 NaN/undefined`, () => clean(page(O, r), r));
for (const tab of TABS) t("overview", `总览与主机页的记录卡：${tab}`, () => {
  O.RTAB = tab;
  for (const r of ["#/", "#/host/srv-a", "#/host/srv-b"]) { const h = page(O, r); clean(h, r + " " + tab); if (tab === "events") has(h, 'class="rl', r); }
  O.RTAB = "done";
});
t("overview", "GPU 利用率（#51）：首页、主机表、资源卡、组内卡片都由 utilText 出字，保活按 0、带此刻值与 24 小时平均", () => {
  const home = page(O, "#/");
  has(home, "近 20 分钟利用率 54%");                                                         // 三张卡 h1 平均；srv-b 的 worker 才起 20 分钟，按覆盖最短的写
  const a = page(O, "#/host/srv-a");
  has(a, '72%<span class="mut">此刻 95% · 4.8 小时 74%</span>'); has(a, "只有保活（顶到 99%）");
  has(run(O, `SPKG["h:srv-a"].at(${NOW})`), "近 10 分钟利用率 0%");                                // 曲线悬停按 series 卡的第 4 项
  const m = run(O, "fUtil(D.fleet.mine[0])"); ok(m.span === "近 1 小时" && m.big === "38%", "自用主机该取 worker 的平均值：" + JSON.stringify(m));
  const ka = run(O, "fUtil(D.fleet.servers.find(x => x.id === 'srv-d'))");
  ok(ka.big === "0%" && ka.span === "此刻" && ka.sub.includes("只有保活（顶到 82%）") && ka.tip.includes("探针此刻的样本"), "组内服务器只有保活：" + JSON.stringify(ka));
  has(run(O, "gpuTiles(D.fleet.mine[0].sample.gpus, 'job')"), "利用率 4%（近 1 小时）· 显存");
});
t("overview", "主机页：worker 停着（没有 GPU、曲线）、没有状态、连不上各有说明", () => {
  const S = ovCtx(OVS, RR);
  const old = page(S, "#/host/srv-old"); clean(old, "srv-old"); has(old, "数据还不够"); has(page(S, "#/host/srv-c"), "ssh: connect"); has(page(S, "#/host/srv-bad"), "status.json 解析不了");
  const bare = page(O, "#/host/没有的主机"); has(bare, "没有这台主机");
  has(run(O, 'hostPage("bare")'), "没有这台主机");
});
t("overview", "在跑的占位任务写了 after=：预计结束与依据取项目看板（来自推演），详情不再写清单估时", () => {
  const j = {...OV.hosts[0].status.running[1], h: OV.hosts[0]};
  const e = run(O, `etaOf(${JSON.stringify(j)})`); ok(e.ts === NOW + H && e.src === "等 proj-a 的 alpha_train_s1、beta_eval_s7 跑完", JSON.stringify(e));
  const d = run(O, `jobDetail(${JSON.stringify(j)})`); has(d, "没有进度行，预计结束依据：等 proj-a 的 alpha_train_s1、beta_eval_s7 跑完"); ok(!d.includes("清单估时"), d);
});
t("overview", "任务详情的速度行：有进度行、还算不出速度写“暂无速度（进度 i/N）”，没有进度行才写“没有进度行”（#17）", () => {
  const det = i => run(O, `jobDetail(${JSON.stringify({...OV.hosts[0].status.running[i], h: OV.hosts[0]})})`);
  const d = det(2); has(d, "暂无速度（进度 400/21050），预计结束依据：估时（进度刚开始）"); ok(!d.includes("没有进度行"), d);
  has(det(0), "2.5 步/分（预计结束依据：近期速度）");
});
t("overview", "总览：告警链到项目页 #/p/<id>，没有 /p/ 的绝对链接", () => {
  const h = page(O, "#/"); has(h, 'href="#/p/proj-a"', "总览"); ok(!/href="\/p\//.test(h), "有 href=\"/p/…\"");
});
t("overview", "总览的 KPI「预计全部跑完」只取 worker 推演（不是项目看板按进度算的）", () => {
  const h = page(O, "#/"), want = run(O, `ts(${NOW + 8 * H})`);
  has(h, `预计全部跑完</div><div class="v" style="font-size:19px">${want}`);
  ok(!h.includes(`font-size:19px">${run(O, `ts(${NOW + 9 * H})`)}`), "取了 prog.eta_ts");
  has(h, `预计 ${want} 全部跑完`, "任务卡标题");
});
t("overview", "总览：各项目的预计全部跑完、里程碑、最近跑完的时刻与时长格式", () => {
  const h = page(O, "#/");
  has(h, `>${run(O, `ts(${NOW + 8 * H})`)}${run(O, 'rebalMark(D.rebalance["proj-a"])')}</td>`, "proj-a 预计全部跑完（有挪法建议）");
  has(h, `投稿截止 ${run(O, `tsMD(${NOW + 3 * 86400})`)} <span class="mut">（3.0 天后）</span>`, "里程碑");
  has(h, `${run(O, `ts(${NOW - 1.2 * H})`)} beta_eval_s6`, "最近跑完的注");
  has(h, `<td class="sm nw">${run(O, `ts(${NOW - 1.2 * H})`)}</td>`, "doneTable 结束列");
  has(h, `<td class="r">${run(O, `dur(${2 * H})`)}</td>`, "jobsTable 已运行");
  has(h, "4<small> / 7 个任务</small>", "运行 / 等待");   // srv-b 上多一条约定归还挡住的
  has(h, "1 条队列受阻"); has(h, "1 个独占在等"); has(h, `<span class="badge b-warn">独占</span>`, "等待表的独占");
});
t("overview", "总览的「看板服务」小卡（#166）：八格、成立的提醒那格染色并逐条列出原文，悬停写门槛与待装更新的标题；不是 WSL（快照没有 hostcheck）时整张卡不出现", () => {
  const h = page(O, "#/"), V = OV.hostcheck, tp = tips(h);
  has(h, "<b>看板服务</b>"); has(h, "DASHBOARD-HOST · Windows + WSL · 体检于"); has(h, '<span class="badge b-warn">3 项要处理</span>');
  ok((h.match(/class="svc-t/g) || []).length === 8, "不是八格");
  ok((h.match(/class="svc-t hit/g) || []).length === 3, "成立的不是三格（开机、待装更新、更新方式）");
  has(h, `<div class="v">2<small>个</small></div><div class="s">安全更新 1 个</div>`, "待装更新");
  has(h, `<div class="v">${run(O, `dur(${31.2 * 86400})`)}</div>`, "开机多久");
  has(h, `暂停到 ${run(O, `tsMD(${V.pause_until})`)}`, "暂停到期");
  has(h, `<div class="svc-t off"`, "隧道没装是灰的"); has(h, "<div class=\"v\">没装</div>");
  has(h, `<div class="v">74<small>MiB</small></div>`, "服务进程");
  for (const m of Object.values(V.warn)) has(h, `<div class="w">⚠ ${run(O, `esc(${JSON.stringify(m)})`)}</div>`, "提醒原文");
  has(tp, "[安全] 2026-11 Windows 11 累积更新（KB5050505）（已挂 4.0 天）"); has(tp, "开机超过 30 天提醒"); has(tp, "白天 9–22 点推");
  ok(!page(ovCtx(OVS, RR), "#/").includes("看板服务"), "sparse 出了卡");
});
t("overview", "「看板服务」小卡：体检停了、还没体检过、更新方式是自动装、隧道断开、读不了 Windows 各有说法", () => {
  const S = JSON.parse(JSON.stringify(OV));
  Object.assign(S.hostcheck, {t: NOW - 3600, au: {manual: false, text: "自动安装（Windows 缺省）"}, tunnel: {state: "down", since: NOW - 1800, unit: "u"}, err: {win: "拒绝访问"}, warn: {}});
  const h = page(ovCtx(S, RR), "#/");
  has(h, `体检停在 ${run(O, `tsNear(${NOW - 3600})`)}`); has(h, '<span class="badge b-ok">正常</span>');
  has(h, `<div class="svc-t cau subhit"`, "自动装标黄"); has(h, "<div class=\"v\">自动装</div>");
  has(h, `<div class="v">断开</div><div class="s">已断 ${run(O, `dur(1800)`)}</div>`); has(h, "读不了 Windows：拒绝访问");
  S.hostcheck = {t: null}; has(page(ovCtx(S, RR), "#/"), "还没体检过");
});
t("overview", "「看板服务」小卡：计划维护中（#165）写到几点、怎么提前结束；没在维护不写", () => {
  ok(!page(O, "#/").includes("计划维护"), "没在维护也写了");
  const S = JSON.parse(JSON.stringify(OV)); S.hostcheck.maint = NOW + 1800;
  const h = page(ovCtx(S, RR), "#/");
  has(h, `<div class="svc-m sm">计划维护到 ${run(O, `tsNear(${NOW + 1800})`)}：期间不推体检提醒，TX 的看门狗也不报`); has(h, "labmon server maint off");
  S.hostcheck.maint = NOW - 60; ok(!page(ovCtx(S, RR), "#/").includes("计划维护"), "到期了还写");
});
t("overview", "备用（服务租约 #163）：各页顶上一条横幅，写 lease.text 给的那句；是主、没跑过租约都不出", () => {
  ok(!page(O, "#/").includes("<b>备用</b>"), "是主也出了横幅"); ok(!page(ovCtx(OVS, RR), "#/").includes("<b>备用</b>"), "没跑过租约也出了横幅");
  const S = JSON.parse(JSON.stringify(OV));
  S.server = {role: "standby", id: "mac-1", holder: "dashboard-host-1", say: "这台看板服务是备用：主是 dashboard-host-1"};
  for (const r of ["#/", "#/host/srv-a", "#/fleet", "#/log"]) has(page(ovCtx(S, RR), r), '<div class="lanbar"><b>备用</b><span>这台看板服务是备用：主是 dashboard-host-1</span></div>', r);
  const e = page(ovCtx(OVE, RRE), "#/"); clean(e, "evil 横幅"); has(e, "<b>备用</b><span>主是 " + BADESC);
});
t("overview", "「看板服务」小卡：恶意字符串都转义", () => {
  const h = page(ovCtx(OVE, RRE), "#/"); clean(h, "evil #/"); has(h, "机" + BADESC); has(h, "隧道" + BADESC); has(h, "错" + BADESC);
  has(h, '<span class="badge b-mute">备用</span>');
});
t("overview", "总览：项目表的挪法建议（快照的 rebalance，rebalMark 与项目页同一个图标）：有建议标「⇄ 可提前」（窄屏只留 ⇄）、悬停写那句话；只有原因的灰「⇄」、悬停写原因", () => {
  const h = page(O, "#/"), tp = tips(h);
  ok((h.match(/b-warn" data-tip="[^"]*">⇄/g) || []).length === 1, "只有 proj-a 有建议");
  has(h, run(O, 'rebalMark(D.rebalance["proj-a"])'), "proj-a 图标"); has(h, run(O, 'rebalMark(D.rebalance["proj-b"])'), "proj-b 灰图标");
  has(tp, run(O, "esc(\"可以挪：\" + D.rebalance[\"proj-a\"].text)"), "proj-a 悬停");
  has(tp, "别的主机接不了本项目的任务：", "proj-b 悬停");
  ok(run(O, "rebalTip(undefined)") === "" && run(O, "rebalMark(null)") === "" && run(O, "rebalMark({why: []})") === "");
  // #43：时长不明（est）同样是灰「⇄」、不写「可提前」，悬停写那句
  const est = run(O, 'rebalMark({key: "rebalance:p:est", est: true, text: "写 est_min=", why: []})');
  ok(est.includes("b-mute") && !est.includes("可提前") && est.includes("可能可以提前：写 est_min="), "est 灰图标：" + est);
});
t("overview", "顶栏：更新时刻 = tsMD + tsHM 两段（窄屏只留时分）", () => {
  const top = el(O, "top").innerHTML;
  has(top, `<span class="dsk">${run(O, `tsMD(${NOW})`)} </span>${run(O, `tsHM(${NOW})`)}<span class="dsk"> 更新</span>`);
});
t("overview", "等待表：排位、屏障、预计开始；排不上的写原因、被指令挡住的不推演", () => {
  const h = page(O, "#/");
  has(h, `第 1 <span class="badge b-warn">屏障</span>`, "alpha_train_s2");
  has(h, `<td class="sm nw" >${run(O, `ts(${NOW + 3 * H})`)}</td>`, "预计开始");
  has(h, 'data-tip="推演里排不上：推演到 7 天后还没轮到"', "beta_eval_s1");
  has(h, 'data-tip="被临时指令挡住的不推演"', "beta_eval_s2");
  has(h, "被旧 gpu_queue 占着", "队列受阻");
  const w = run(O, `(() => { const h = D.hosts[0]; const ws = h.status.waiting.map(w => ({...w, h})); return [rankTip(ws[1], ws), rankTip(ws[2], ws), rankTip(ws[3], ws), rankTip({...ws[2], since: null}, ws), rankTip(ws[4], ws)]; })()`);
  has(w[0], "成了屏障，这个在它放行前跑不完，让它先", "alpha_train_s3 的排位解释"); has(w[0], "是同一项目、先来的");
  has(w[1], "排第 3"); has(w[2], "被临时指令挡住，不参与排队"); has(w[3], "已等 —"); has(w[4], "依赖还没满足，不参与排队");
  has(h, "等的 beta_prep 跑失败了", "等待表写依赖原因");
  ok(!leak(w.join("\n")), "rankTip 漏 NaN/undefined");
});
t("overview", "调度卡：规则一句话短，算法细节（减半）只在 ⓘ 的提示里；权重、优先、暂停", () => {
  const h = run(O, "schedCard()"); clean(h, "调度卡");
  has(h, "按份额轮流（"); has(h, " 优先"); has(h, "暂停中");
  ok(!text(h).includes("减半"), "「减半」出现在正文里"); ok(tips(h).includes("减半"), "ⓘ 里没有算法细节");
  has(h, `到 ${run(O, `tsNear(${NOW + 6 * H})`)}`, "优先到期");
  has(h, run(O, "dur(12.5 * 60)"), "平均等");
  // 权重（#143）：与管理抽屉同一个胶囊「权重 − ×1 +」，不弹窗；−、+ 在档间走，现值不在档上的走到最近的档，到头的禁用
  has(h, '<td class="nw"><span class="flag wcap">', "调度卡的权重格该是胶囊"); has(h, 'data-sch="wd" data-p="proj-a"'); has(h, 'data-sch="wu" data-p="proj-b"');
  ok(!/data-sch="w"[ >]/.test(h), "调度卡的权重不该再是弹窗按钮"); has(h, run(O, "schedW(D.sched.projects[0])"), "调度卡与抽屉该是同一个 schedW");
  ok(run(O, "[wStep(2.5, -1), wStep(2.5, 1), wStep(1, -1), wStep(0.1, -1), wStep(1000, 1)].join()") === "2,3,0.75,,", run(O, "[wStep(2.5, -1), wStep(2.5, 1), wStep(1, -1)].join()"));
  const top = run(O, "schedW({id: 'x', w: 1000})"); ok(/data-sch="wu" data-p="x" aria-label="权重调高" disabled/.test(top) && !/data-sch="wd" data-p="x" aria-label="权重调低" disabled/.test(top), "到最高档时 + 该禁用、− 不禁用");
  run(O, `POP = null; SF = []; fetch = (u, o) => { SF.push([u, JSON.parse(o.body)]); return new Promise(() => {}); }; schedAct("wd", "proj-b")`);
  ok(run(O, "POP") === null && run(O, "JSON.stringify(SF)") === JSON.stringify([["sched/weight", {project: "proj-b", weight: 1.5}]]), "调度卡点 −：" + run(O, "JSON.stringify(SF)"));
  run(O, "SWB.clear()");
});
t("overview", "#23 份额按人：几个人在用卡时调度卡按人分组、应得按人均分再按权重分，排位解释先比人；只有一人时照旧", () => {
  const r = run(O, `(() => { const S = D.sched, old = JSON.stringify(S.projects), h = D.hosts[0];
    Object.assign(S.projects[0], {person: "张三", pk: "k1", puse: 20.9}); Object.assign(S.projects[1], {person: null, pk: "k2", puse: 3.2});
    S.projects.push({id: "proj-c", person: "张三", pk: "k1", puse: 20.9, w: 1, use: 2.0, cards: 0, queued: 1, waiting: 0, wait_avg: null, wait_n: 0, focus: null, paused: null});
    const prev = {project: "proj-a", name: "a9", h, pos: {n: 1, puse: 0.5, focus: false, ratio: 1.0}, since: D.generated - 60, why: "放不下"};
    const x = {project: "proj-b", name: "b9", h, pos: {n: 2, puse: 1.0, focus: false, ratio: 0.5}, since: D.generated - 60, why: "放不下"};
    const out = [schedCard(), shareBar(S.projects[1]), shareBar(S.projects[0]), rankTip(x, [prev, x])];
    S.projects = JSON.parse(old); return out; })()`);
  clean(r[0], "按人的调度卡");
  has(r[0], "按人轮流（"); has(r[0], 'class="sch-pp"'); has(r[0], "<b>张三</b>"); has(r[0], "<b>其他人</b>", "普通用户看别人的项目没有主人名");
  has(r[0], "在各自主人的项目里", "优先只在同一人的项目里排前");
  ok(r[0].indexOf("<b>其他人</b>") < r[0].indexOf("<b>张三</b>"), "人按近期用量合计从小到大排（与放行先后一致）");
  has(r[1], "/ 50%", "两个人各应得一半"); has(r[2], "/ 25%", "张三的一半再按权重分给他的两个项目");
  has(tips(r[2]), "应得 1 ÷ 2 人"); ok(tips(r[0]).includes("份额按人算"), "ⓘ 说明份额按人算");
  has(r[3], "先比人：其他人名下几个项目的用量合计 1.0 卡·时"); has(r[3], "这人近期用得更少（0.5 < 1.0 卡·时）");
  ok(!leak(r.join("\n")), "按人的调度卡漏 NaN/undefined");
  const one = run(O, "schedCard()"); has(one, "按份额轮流（"); ok(!one.includes("sch-pp"), "只有一个人时不分组");
});
t("overview", "#23 人的用量合计取看板按完整用量算的 puse（含已归档、卡上不列的项目），不只加卡上列着的几行", () => {
  const r = run(O, `(() => { const S = D.sched, old = JSON.stringify(S), save = JSON.stringify(S.projects);
    Object.assign(S.projects[0], {person: "张三", pk: "k1", puse: 22.1}); Object.assign(S.projects[1], {person: "李四", pk: "k2", puse: 33.2});   // 李四已归档的项目还有 30 卡·时
    S.use_sum = 52.1;
    const out = [schedCard(), personBar([S.projects[1]])]; S.projects = JSON.parse(save); Object.assign(D, {sched: JSON.parse(old)}); return out; })()`);
  clean(r[0], "按人的调度卡（含已归档用量）");
  ok(r[0].indexOf("<b>张三</b>") < r[0].indexOf("<b>李四</b>"), "李四名下合计更多（含已归档的），排在张三后面，与 worker 放行先后一致");
  has(r[0], "合计 33.2 卡·时", "人那一行写完整的合计");
  has(r[1], "64% <span", "占比的分母是所有项目的用量和（use_sum）");
  ok(!leak(r.join("\n")), "漏 NaN/undefined");
});
t("overview", "#96 调度卡的暂停与恢复：项目级的（所有主机）按钮撤它，只在几台单独下的徽标写主机与带 --host 的撤销命令", () => {
  const h = run(O, "POP = null; schedCard()"); clean(h, "调度卡");
  has(h, `data-sch="uf" data-p="proj-a"`, "proj-a 项目级优先：撤销优先"); has(h, `data-sch="p" data-p="proj-a"`, "proj-a 没暂停：暂停按钮");
  has(h, `data-sch="f" data-p="proj-b"`); has(h, `data-sch="p" data-p="proj-b"`, "只在 srv-a 单独暂停的：按钮仍是项目级的暂停");
  has(h, "暂停中（部分主机）"); has(tips(h), "labmon resume proj-b --host srv-a"); has(h, `（只在 ${run(O, "hostShort(D.hosts[0])")}）`, "规则一句写只在哪台");
  const g = run(O, "(() => { const p1 = D.sched.projects[1].paused; D.sched.projects[1].paused = {until: null, hosts: null}; const r = schedCard(); D.sched.projects[1].paused = p1; return r; })()");
  has(g, `data-sch="ur" data-p="proj-b"`, "项目级暂停：恢复按钮"); ok(!g.includes("部分主机"), "项目级的不该写部分主机"); has(tips(g), "无期限，所有主机（含之后加入的）");
  run(O, `POP = null; schedAct("p", "proj-a")`); ok(run(O, "POP.k") === "sp" && run(O, "POP.wnow") === true, "暂停的弹窗与到期缺省");
  const pp = run(O, "schedCard()"); has(pp, `id="schp"`); has(pp, "labmon pause proj-a --now"); clean(pp, "暂停弹窗");
  run(O, `SF = []; fetch = (u, o) => { SF.push([u, JSON.parse(o.body)]); return new Promise(() => {}); }; whenTap({wq: "clr"}); schedAct("pgo", "proj-a")`);
  ok(JSON.stringify(run(O, "SF")) === JSON.stringify([["sched/pause", {project: "proj-a", until: null}]]), JSON.stringify(run(O, "SF")));
  run(O, `POP = null; SF = []; schedAct("ur", "proj-b"); schedAct("urgo", "proj-b")`);
  ok(JSON.stringify(run(O, "SF")) === JSON.stringify([["sched/resume", {project: "proj-b"}]]), JSON.stringify(run(O, "SF")));
  run(O, "POP = null");
  // 临时指令表：项目级的主机写“全部”、撤销不带 --host；单机 pause/drain 对管理员给撤销按钮（#241，悬停含带 --host 的命令）；总览各主机合起来时项目级的只列一次
  const ot = run(O, `ovrTable([{kind: "focus", target: "proj-a", global: true, h: D.hosts[0]}, {kind: "pause", target: "proj-b", h: D.hosts[0]}], true)`);
  has(ot, `<td class="nw">全部</td>`); has(ot, "labmon unfocus proj-a</code>"); has(ot, 'data-ovr="open" data-kind="pause" data-host="srv-a" data-target="proj-b"'); has(tips(ot), "labmon resume proj-b --host srv-a");
  const dt = run(O, `ovrTable([{kind: "drain", target: null, h: D.hosts[0]}], false)`);
  has(dt, 'data-ovr="open" data-kind="drain" data-host="srv-a" data-target=""', "drain 的撤销按钮"); has(tips(dt), "labmon undrain srv-a");
  has(run(O, `(() => { ST.set("cardOpen", {"rec:home": true}); ST.set("recTab", {home: "ovr"}); const r = home(); ST.set("cardOpen", {}); return r; })()`), `临时指令<span class="mut">4 条</span>`, "项目级的只列一次");
});
t("overview", "etaOf：进度 → 推演的 run 条 → worker 的预计时长 → 清单估时 → 没有", () => {
  const r = run(O, `(() => { const [a, b] = D.hosts; const ja = {...a.status.running[0], h: a}, jb = {...b.status.running[0], h: b};
    const q = a.status.queues.find(q => q.root === ja.root && q.jobs === ja.queue), d = q.dur.alpha_train_s3;
    return [etaOf(ja), etaOf(jb), etaOf({...ja, prog: null, name: "x", est_min: 60}), etaOf({...ja, prog: null, name: "x", est_min: null}),
            etaOf({...ja, prog: null, name: "alpha_train_s3", est_min: 60}), d,
            etaOf({...ja, prog: null, name: "alpha_train_s3", est_min: 60, queue: "jobs/别的.txt"})]; })()`);
  ok(r[0].ts === NOW + 9 * H && r[0].src === "近期速度", JSON.stringify(r[0]));
  ok(r[1].ts === NOW + 2 * H && r[1].src === "worker 推演", JSON.stringify(r[1]));
  ok(r[2].ts === NOW - 2 * H + 3600 && r[2].src === "清单估时", JSON.stringify(r[2]));
  ok(r[3] === null);
  // #140：推演里没有这条 run 条时用 worker 给这条清单行的时长（同类历史等），不是原始 est_min；别的清单里的同名行不算
  ok(r[4].ts === NOW - 2 * H + r[5].min * 60 && r[4].src === r[5].src && r[5].min !== 60, JSON.stringify([r[4], r[5]]));
  ok(r[6].ts === NOW - 2 * H + 3600 && r[6].src === "清单估时", JSON.stringify(r[6]));
});
t("overview", "jobsTable / jobDetail / jobLine 的时刻与时长", () => {
  const r = run(O, `(() => { const a = D.hosts[0], j = {...a.status.running[0], h: a};
    return [jobsTable([j], true), jobsTable([{...j, t0: null}], true), jobDetail(j), jobLine(j), jobLine({...j, t0: null})]; })()`);
  has(r[0], `<td class="r">${run(O, `dur(${2 * H})`)}</td>`); has(r[0], `剩 ${run(O, `dur(${9 * H})`)} · ${run(O, `tsHM(${NOW + 9 * H})`)}`, "预计结束");
  has(r[1], `<td class="r">—</td>`, "没有 t0");
  has(r[2], `开始于 ${run(O, `ts(${NOW - 2 * H})`)}`);
  has(r[3], `已跑 ${run(O, `dur(${2 * H})`)}，预计 ${run(O, `tsHM(${NOW + 9 * H})`)}`); ok(!r[4].includes("已跑"), "没有 t0 还写已跑");
});
t("overview", "记录：按天分组只写时分，片段写月-日 时分；平台操作合成一事；同类合并 ×N", () => {
  const h = page(O, "#/log");
  const last = RR[0].t;                  // 最新一条在 NOW - 60：快照在 0 点后一分钟内生成时它是昨天的，标题不带“今天”
  has(h, `${new Date(last * 1000).toDateString() === new Date(NOW * 1000).toDateString() ? "今天 · " : ""}${run(O, `tsMD(${last})`)} 周`, "日期标题");
  ok(!/class="rr-tm">\d\d-\d\d /.test(h), "记录页的行写了月-日");
  has(h, `class="rr-tm">${run(O, `tsHM(${NOW - 60})`)}</span>`);
  has(h, "续租", "合成事"); has(h, `<span class="badge b-`, "合成事的结果");
  const frag = run(O, "recSlice({}, 20)"); has(frag, `class="rr-tm">${run(O, `ts(${NOW - 60})`)}</span>`, "片段");
  has(frag, 'href="#/log"', "链到记录页"); has(run(O, "recSlice({host: 'srv-a'}, 40)"), 'href="#/log?host=srv-a"');
  has(h, "worker 新加的事件", "未知种类照样列出");
});
t("overview", "问题报告可在记录页逐条查看，现象和多行证据转义", () => {
  const evidence = "<img src=x>\n第二行";
  const rows = ["R-a", "R-b"].map((report_id, n) => ({kind: "report", report_id, host: "srv-a", project: "proj-a",
    t: NOW - n, occurred: NOW - H, msg: report_id + " · reporter：<script>", detail: evidence}));
  const out = page(ovCtx(OV, rows), "#/log?cat=report");
  has(out, "问题报告");
  has(out, "R-a · reporter：&lt;script&gt;");
  has(out, "R-b · reporter：&lt;script&gt;");
  has(out, "发生于 ");
  has(out, "&lt;img src=x&gt;\n第二行");
  ok(!out.includes(evidence), "证据必须转义");
});
t("overview", "刷新不冲掉输入：焦点在输入框或正文里选着字时定时刷新只记 PENDING", () => {
  O.location.hash = "#/"; run(O, "render(true)");
  run(O, 'document.activeElement = {closest: s => s.includes("input") ? {} : null}; PENDING = false; render(false)');
  ok(run(O, "PENDING") === true && run(O, "selecting()") === true, "输入框里没有挡住重画");
  run(O, "document.activeElement = document.body; render(false)"); ok(run(O, "PENDING") === false);
  run(O, 'SEL_T = Date.now(); getSelection = () => ({isCollapsed: false, anchorNode: {}}); document.getElementById("main").contains = () => true; render(false)');
  ok(run(O, "PENDING") === true, "选着字没有挡住重画");
  // #66：选区放着超过 SEL_HOLD（复制完告警不取消选区）照常重画；勾选框、开关点过以后焦点留在上面不算正在操作
  run(O, "SEL_T = Date.now() - SEL_HOLD - 1; render(false)"); ok(run(O, "PENDING") === false, "选区放久了仍挡着重画，告警一直挂着（#66）");
  run(O, 'getSelection = () => ({isCollapsed: true, anchorNode: null}); document.getElementById("main").contains = () => false; render(false)');
  ok(run(O, "PENDING") === false);
  for (const ty of ["checkbox", "radio", "button"]) {
    run(O, `document.activeElement = {closest: s => s.includes("input") ? {type: "${ty}"} : null}; render(false)`);
    ok(run(O, "PENDING") === false, `焦点在 ${ty} 上挡住了重画（#66）`);
  }
  for (const ty of ["text", "number", "search", "select-one"]) {
    run(O, `document.activeElement = {closest: s => s.includes("input") ? {type: "${ty}"} : null}; render(false)`);
    ok(run(O, "PENDING") === true, `焦点在 ${ty} 上没挡住重画`);
  }
  run(O, "document.activeElement = document.body; render(false)");
});
t("overview", "嵌着项目页时更新时间、动作和反馈分开", () => {
  O.FR = {dataset: {p: "proj-a"}, hidden: false, contentWindow: {embedState: () => ({now: NOW - 3, offline: null, cached: true}), drawTimeline() {}}};
  run(O, 'document.querySelectorAll = s => s.includes("iframe") ? [FR] : []');
  O.location.hash = "#/p/proj-a"; run(O, "render(true)"); run(O, "render(false)");
  const top = el(O, "top").innerHTML;
  has(top, "Alpha 实验");
  has(top, `<span class="dsk">${run(O, `tsMD(${NOW - 3})`)} </span>${run(O, `tsHM(${NOW - 3})`)}<span class="dsk"> 更新</span>`, "取嵌着的页面的时刻并与其它顶栏一致");
  ok(!top.includes("project-update"), "更新时间不应再用独占一行的 project-update");
  ok(!top.includes("刚采过"));
  const actions = html => html.match(/<div class="project-actions">([\s\S]*?)<\/div>/)?.[1];
  O.FR.contentWindow.embedState = () => ({now: NOW, offline: "ECONNREFUSED", busy: true}); run(O, "render(true)");
  const fail = el(O, "top").innerHTML;
  has(fail, ">ECONNREFUSED</span>");
  ok(actions(top) === actions(fail)?.replace("disabled", ""), "反馈挤进动作区或更改了按钮文字");
  O.FR.contentWindow.embedState = () => ({now: NOW, offline: null, busy: true}); run(O, "render(true)");
  has(el(O, "top").innerHTML, 'role="status"><span class="sm mut">采集中…');
  run(O, "document.querySelectorAll = () => []");
});
t("overview", "主机页：借来的主机写借用、归还约定与留量；自用主机写租期、本地依赖", () => {
  const b = page(O, "#/host/srv-b"); has(b, "OWNER"); has(b, "前不用：不给对方留余量"); has(b, "借用详情");   // 快照里 OWNER 说好不用（#30）
  const aw = run(O, "D.hosts[1].borrow.keep.away"); run(O, "delete D.hosts[1].borrow.keep.away");
  has(page(O, "#/host/srv-b"), "借来时对方空着，不预先留"); run(O, `D.hosts[1].borrow.keep.away = ${aw}`);
  const a = page(O, "#/host/srv-a"); has(a, "本地依赖"); has(a, run(O, "healText([{heal: true, how: 'copy', bytes: 3e9}])"));
  has(a, "1 格缺文件"); has(a, "alpha_need_w0");                     // 等待项的 miss 字段：概况条与本地依赖表都认它
  ok(run(O, "missW({status: {waiting: [{miss: true, why: \"x\"}, {why: \"缺本地文件：y\"}, {why: \"等资源\"}]}}).length") === 1, "missW：只认 miss 字段，不看文字");
  ok(run(O, "healText([{heal: true, how: 'copy', bytes: 3e9}])") === "从共享盘拷，约 30 秒");
  ok(run(O, "healText([{heal: true, how: 'copy', bytes: 60e9}])") === "从共享盘拷，约 10 分钟");
  ok(run(O, "healText([{heal: false}])") === "会失败");
});
t("overview", "主机压力与任务读写：主机页的限流、共享盘读写与基线、本地盘读写；任务详情与跑完的悬停", () => {
  const a = page(O, "#/host/srv-a"), ta = tips(a);
  has(a, "限流 38.5%"); has(a, "共享盘读写"); has(a, "31.4 ms/次"); has(a, "基线 20.0"); has(a, "本地盘读写"); has(a, "忙 38%");
  has(a, "background:var(--bad)", "比基线慢标红"); has(ta, "样本 120 / 288 个，还不够，先取 20 ms"); has(ta, "nvme0n1"); has(ta, "192.0.2.7");
  const b = page(O, "#/host/srv-b"), tb = tips(b);
  has(b, "此刻没有读写"); has(b, "基线 26.8"); has(tb, "3900 个样本"); has(tb, "PSI：近 60 秒有任务在等读写的时间占 2.1%");
  ok(!b.includes("本地盘读写"), "没找到本地盘的设备也画了一行"); ok(!b.includes("限流"), "没有限流计数也写了");
  const S = ovCtx(OVS, RR), old = page(S, "#/host/srv-old"); ok(!old.includes("共享盘读写") && !old.includes("本地盘读写"), "旧 worker 没有 press 也画了");
  const d = run(O, `jobDetail(${JSON.stringify({...OV.hosts[0].status.running[0], h: OV.hosts[0]})})`);
  has(d, "此刻 1.0 核、读 35.2 / 写 0.4 MiB/s"); has(d, "CPU 峰值 2.6 核"); has(d, "峰值 180.3 MiB/s"); has(d, "cpus=4 io=heavy");
  const h = run(O, `jobDetail(${JSON.stringify({...OV.hosts[0].status.running[1], h: OV.hosts[0]})})`); ok(!h.includes("CPU 与读写") && !h.includes("资源提示"), "旧 worker 的任务没有这些字段也写了");
  has(tips(run(O, "doneTable(D.hosts[0].done.map(r => ({...r, h: D.hosts[0]})), false)")), "读 48.0 GiB（峰值 210.5 MiB/s）、写 3.0 GiB（峰值 12.0 MiB/s）");
  ok(run(O, `ioText({cores_mean: 1})`) === "", "旧用量行没有这些字段也写了");
});
t("overview", "CPU 被配额限流（#39）：工作台告警指出超核的任务，主机表写 GPU 利用率与限流比例，资源卡标红并在条的悬停写超核", () => {
  const A = "srv-a（A6000）：容器 CPU 被配额限流 38.5%（已持续 25 分钟），超核的任务：Alpha 实验 的 alpha_train_s1 实测 9.2 核（给 4 核），线程数、进程数按 $LABMON_CPUS 设就不再抢核";
  ok(run(O, "allAlerts()").includes(A), "工作台告警没有限流那句：" + JSON.stringify(run(O, "allAlerts()")));
  ok(run(O, "alertsOf(D.hosts[0]).find(a => a.msg.includes('限流')).to") === "both");
  const home = page(O, "#/");
  has(home, "显存 · 利用率"); has(home, "46%<span class=\"mut\"> · 72%</span>"); has(home, "· 4%</span>");     // 近 1 小时吃满程度（#51）
  has(home, 'style="color:var(--bad)"'); has(home, "限流 38.5%"); has(tips(home), "超核：Alpha 实验 的 alpha_train_s1 实测 9.2 核（给 4 核）");
  ok(!/限流 —|限流 null/.test(home), "没有限流计数的主机也写了");
  const a = page(O, "#/host/srv-a"); has(a, "限流 38.5%"); has(tips(a), "超核：给 4 核，1 分钟窗口实测 9.2 核"); has(tips(a), "持续限流 25 分钟");
  // 还没持续满：不告警、琥珀色；持续满但无超核且外部进程不足 1 核时不报告警，限流百分比与标红仍保留；外部进程 ≥ 1 核照常告警；缓过来（throttle 为 null）只剩灰色小字
  run(O, "D.hosts[0].status.throttle.alert = false");
  ok(!run(O, "allAlerts()").some(x => x.includes("限流")), "还没持续满也告警了"); has(page(O, "#/"), 'style="color:var(--warn)"');
  run(O, "D.hosts[0].status.throttle.alert = true; D.hosts[0].status.throttle.over = []; D.hosts[0].status.throttle.other = 0.3");
  ok(!run(O, "allAlerts()").some(x => x.includes("限流")), "无超核且无外部抢核仍报告警");
  ok(!run(O, "alertsOf(D.hosts[0])").some(a => a.msg.includes("限流")), "主机告警里仍有无动作价值的限流提示");
  const noOverHome = page(O, "#/"), noOverHost = page(O, "#/host/srv-a");
  has(noOverHome, 'style="color:var(--bad)"'); has(noOverHome, "限流 38.5%"); has(tips(noOverHome), "持续限流 25 分钟");
  has(noOverHost, 'style="color:var(--bad)"'); has(noOverHost, "限流 38.5%"); has(tips(noOverHost), "持续限流 25 分钟");
  run(O, "D.hosts[0].status.throttle.other = 3.24");
  ok(run(O, "allAlerts()").some(x => x.endsWith("labmon 之外的进程占 3.2 核")), "外部抢核 ≥1 核时未告警");
  ok(run(O, "thrText(D.hosts[0])").endsWith("labmon 之外的进程占 3.2 核"));
  run(O, "D.hosts[0].status.throttle = null");
  has(page(O, "#/"), '<span class="sm mut" style="" ');
  run(O, `D.hosts[0].status.throttle = ${JSON.stringify(OV.hosts[0].status.throttle)}`);
});
t("overview", "本地盘开机以来的新写入（#149）：主机页的资源行、概况条与「本地盘」选项卡，组内卡片的小标与抽屉，续租、关机、换人、批量续租的确认，借用归还的确认", () => {
  const LA = "本地盘开机以来 labmon 之外写了 11.7 GiB（/root/.cache/huggingface/hub、/root/work/ckpt、/opt/conda/pkgs），续租会清空，要留的先挪到共享盘";
  ok(run(O, "lwText(D.hosts[0].lw)") === LA, "lwText 与 fleet.lw_text 口径不同：" + run(O, "lwText(D.hosts[0].lw)"));   // 同一句在 test_fleet 里对 Python 也断言
  ok(run(O, 'lwPath("/root/.")') === "/root（本层）" && run(O, 'lwPath("/.")') === "/（本层）" && run(O, 'lwPath("/a/b")') === "/a/b");
  // 主机页：资源行的总量条（项目色的缓存、斜纹的 labmon 之外按目录）、过门槛的 --heat 数字与小标、概况条一句、原来的 /tmp 一行与「未纳管」没了
  O.RTAB = "lw"; const a = page(O, "#/host/srv-a"), ta = tips(a); O.RTAB = "done";
  has(a, "labmon 之外 <span class=\"heat-n hi\">11.4</span>"); has(a, "27.4 / 750 GiB"); has(a, '<span class="heat hi"'); has(a, "本地盘 11.7 GiB");
  has(ta, "/root/.cache/huggingface/hub\n4.8 GiB，最近写入 3.0 小时前"); has(ta, "proj-a 缓存 $LABMON_CACHE：14.0 GiB"); has(ta, LA + "\n各目录在下方「记录 · 本地盘」");
  ok(!a.includes("未纳管") && !a.includes("容器本地 /tmp"), "旧的 /tmp 一行还在");
  has(a, "开机以来新写的目录"); has(a, "/<wbr>root/<wbr>.cache/<wbr>huggingface/<wbr>hub"); has(a, "/root（本层）"); has(a, "其余 3 处"); has(a, "扫于 10 分钟前");
  // 选项卡里的总量条按写的总量画（缓存项目色、单格临时淡色、labmon 之外斜纹、/dev/shm）：各段加起来是整条；资源行的按配额画，只占一小截
  has(a, "proj-a 的缓存"); has(a, "斜纹：labmon 之外，按目录（过 1 GiB 变紫）");
  const wsum = js => [...run(O, js).matchAll(/width:([\d.]+)%/g)].reduce((s, m) => s + +m[1], 0);
  const whole = wsum("lwBar(D.hosts[0].lw, D.hosts[0].status.storage.local, true)"), cap = wsum("lwBar(D.hosts[0].lw, D.hosts[0].status.storage.local)");
  ok(Math.abs(whole - 100) < 1 && cap > 3 && cap < 4.5, `总量条各段之和：按写的 ${whole}%（该是 100%），按配额 ${cap}%（该是 27.4 / 750）`);
  // 借来的主机：目录从借来时算起；归还确认列借用期间 labmon 目录之外新写的（对方原有的不列）
  O.RTAB = "lw"; const b = page(O, "#/host/srv-b"); O.RTAB = "done";
  has(b, run(O, "ts(D.hosts[1].lw.scan.since)") + " 以后新写的目录"); has(b, "还有 1 个目录没扫"); has(b, "对方续租时清空");
  ok(!b.includes("续租会清空，要留的"), "借来的主机概况条不该提续租");
  for (const k of ["bret", "bnow"]) {
    const p = run(O, `POP = {k: "${k}", id: "srv-b"}; borrowBtns(srvById("srv-b"))`);
    has(p, "借用期间本地盘 labmon 目录之外新写了 1.6 GiB（多半是我们的任务写的：/root/beta_out、/tmp（本层））。归还只删 /tmp/labmon，这些留在 OWNER 那边、我们拿不到；要留的先挪到共享盘。", k);
  }
  ok(run(O, `lwLeft({lw: {scan: {b: 0.0001, top: []}}}, "x")`) === "" && run(O, "lwLeft(undefined, 'x')") === "", "不到 1 MiB 的不该说");
  // 组内：过门槛的卡片有小标，没过的没有；抽屉里一行总量条、一节明细（没扫的说为什么）
  const f = page(O, "#/fleet"), card = id => { const i = f.indexOf(`id="fc-${id}"`), j = f.indexOf('id="fc-', i + 5); return f.slice(i, j < 0 ? undefined : j); };
  has(card("srv-d"), "本地盘 6.2 GiB"); ok(!card("srv-g").includes("本地盘"), "没过门槛的也标了");
  const d = page(O, "#/fleet/srv/srv-d"); has(d, "<b>本地盘</b>"); has(d, "/<wbr>root/<wbr>data/<wbr>out/<wbr>run1"); has(d, "其余 5 处"); has(d, "扫于 30 分钟前");
  has(d, "labmon 之外 <span class=\"heat-n hi\">6.2</span>");
  has(page(O, "#/fleet/srv/srv-b"), "借用中不扫：扫出来的混着我们的"); has(page(O, "#/fleet/srv/srv-g"), "下一次探测时扫");
  has(page(O, "#/fleet/srv/srv-a"), "/<wbr>root/<wbr>work/<wbr>ckpt", "自用主机的抽屉");
  // 确认：过门槛的要勾（同 fleetops._risk），勾选框说清是清空还是中断；续租一台、关机、换人、批量续租都一样
  ok(run(O, 'FRISKY(srvById("srv-d")) && !FBUSY(srvById("srv-d")) && !FRISKY(srvById("srv-g"))'), "FRISKY 没算上本地盘");
  const LD = v => `本地盘开机以来 labmon 之外写了 6.2 GiB（/root/data/out/run1、/root/.vscode-server、/home/u/.cache/pip），${v}会清空，要留的先挪到共享盘`;
  let r = run(O, 'POP = {k: "renew", id: "srv-d", ack: false}; renewAsk(srvById("srv-d"))'); has(r, LD("续租")); has(r, "已和 PERSON3 说好，本地盘可以清空");
  r = run(O, 'goRenew(srvById("srv-d"))'); has(r, 'class="btn danger"'); has(r, "disabled>续租</button>", "没勾时确认钮该灰着、写「续租」不写「中断」");
  ok(run(O, 'renewAsk(srvById("srv-a"))').includes("确认可以中断、本地盘可以清空"), "自用主机在跑任务又写得多：两件都要说");
  // 分层（#14）：一句结论 → 琥珀框里要勾的 → 折起来的细节；细节的开合跟着 POP.more，重画不收回去
  r = run(O, 'POP = {k: "renew", id: "srv-d", ack: false}; renewAsk(srvById("srv-d"))');
  has(r, '<div class="rk-say"><b>续租 PERSON3</b>：会清空本地盘上 labmon 之外写的 6.2 GiB。</div>'); has(r, '<div class="rk-need">');
  ok(r.indexOf("rk-need") < r.indexOf("<summary>细节</summary>"), "细节该在要勾的风险后面"); ok(!/<details class="rk-more" open>/.test(r), "细节缺省该收着");
  has(r.slice(r.indexOf("<summary>细节</summary>")), "停旧作业 → 提交新作业", "步骤该折进细节");
  ok(/<details class="rk-more" open>/.test(run(O, 'POP.more = true; renewAsk(srvById("srv-d"))')), "展开过的细节重画后又收起了");
  r = run(O, 'POP = {k: "renew", id: "srv-a", ack: false}; renewAsk(srvById("srv-a"))');
  has(r, "：会中断在跑的任务，清空本地盘上 labmon 之外写的"); has(r, "丢掉 1 格待跑任务要的本地文件。", "补不了的本地文件该进结论");
  ok(!run(O, 'renewAsk(srvById("srv-g"))').includes("rk-need"), "空闲又没写多少的不该有要勾的框");
  has(run(O, 'renewAsk(srvById("srv-g"))'), "现在没有实验在跑，本地盘也没写多少。"); run(O, "POP = null");
  r = run(O, 'POP = {k: "off", id: "srv-d", ack: false}; offAsk(srvById("srv-d"))'); has(r, LD("关机")); has(r, "本地盘可以清空"); ok(!r.includes("会中断它"), "只是本地盘写得多不该说中断");
  ok(run(O, 'offGo(srvById("srv-d"))').includes("disabled"), "没勾就能关机");
  r = run(O, 'FWIZ = {kind: "swap", sid: "srv-d", pv: {}, ack: false}; wizForm()'); has(r, LD("换人")); has(r, "已和 PERSON3 说好，本地盘可以清空"); run(O, "FWIZ = null");
  r = run(O, 'RPICK = {key: DH.renew(""), sel: new Set(["srv-d"]), ack: new Set()}; renewDrawer("")'); has(r, LD("续租")); has(r, 'data-rack="srv-d"'); has(r, "在跑实验或本地盘写得多：先勾选确认");
  ok(/data-fop="renewgo" disabled/.test(r), "没勾就能批量续租");
  ok(!/data-fop="renewgo" disabled/.test(run(O, 'RPICK.ack.add("srv-d"); renewDrawer("")')), "勾了确认还是不能批量续租");
  // 输验证码时勾选锁住：要提交的 ids、ack 已记进 FCAP.next，再改勾选只改了页面、提交的还是旧的（Codex 审查 #20）
  r = run(O, 'FCAP = {code: "ab12", tok: "t", input: "", t: Date.now(), next: {act: "renew", body: rpBody(RPICK)}, pop: "renew"}; renewDrawer("")');
  ok(/data-rsel="srv-d" checked\s+disabled/.test(r) && /data-rack="srv-d" checked\s+disabled/.test(r), "输验证码时还能改勾选");
  run(O, "FCAP = null; RPICK = null");
  // #171：借用中的服务器在批量续租里禁选并写清解开办法；单台掉线借用节同样提示先点「与平台核对」
  r = run(O, 'RPICK = {key: DH.renew("srv-b"), sel: new Set(["srv-b"]), ack: new Set()}; renewDrawer("srv-b")');
  ok(/data-rsel="srv-b"\s+disabled/.test(r), "借用中的服务器在批量续租里还能勾选");
  has(r, "正借来在用：先在它的「借用」里还掉（若已被平台停掉连不上，先点「与平台核对」，确认作业不在运行后会自动归还）再续租");
  ok(JSON.stringify(run(O, "rpBody(RPICK).ids")) === "[]", "rpBody 不应带出借用中的服务器");
  run(O, "RPICK = null");
  const bsDown = run(O, 'borrowSec({...D.fleet.servers.find(s => s.id === "srv-b"), state: "down"})');
  has(bsDown, "这台已掉线，ssh 连不上时无法直接归还或续租：若怀疑作业已被平台停掉，先在组内服务器页点「与平台核对」");
  ok(!run(O, 'borrowSec(D.fleet.servers.find(s => s.id === "srv-b"))').includes("这台已掉线"), "在线的借用机不该出掉线解开提示");
  // 预备续租的确认也提一句
  has(run(O, 'POP = {k: "reserve", id: "srv-a"}; renewBtns(D.hosts[0])'), LA); run(O, "POP = null");
});
t("overview", "CPU 记账、页缓存、让路与读写拥塞嫌疑：主机页的 CPU 与内存两行、任务详情与等待表的估计、CPU 被挡的告警、记录页的分类", () => {
  const a = page(O, "#/host/srv-a"), ta = tips(a);
  has(a, "记账 4.0 / 17"); has(a, "可放 12.5"); has(ta, "放行按 CPU 记账"); has(ta, "alpha_train_s1：记账还没占上的 3.0 核"); has(ta, "$LABMON_CPUS 是开跑时给它记的核数");
  has(a, "收不回 4.5"); has(ta, "页缓存 15.4 GiB"); has(ta, "放行与内存闸门不算它"); has(ta, "红线 90% 是内存闸门");
  has(ta, "共享盘扫描与拷本地依赖先让路"); has(ta, "此刻：慢"); has(ta, "先不开始新的扫描，同一件最多推迟 30 分钟");
  const b = page(O, "#/host/srv-b"); has(b, "记账 1.2 / 7"); has(b, "可放 4.8"); ok(!tips(b).includes("此刻：慢"), "共享盘不慢也写了慢");
  const S = ovCtx(OVS, RR), old = page(S, "#/host/srv-old"); ok(!old.includes("记账 ") && !old.includes("可放 "), "旧 worker 没有 room 也画了记账、可放");
  const d = run(O, `jobDetail(${JSON.stringify({...OV.hosts[0].status.running[0], h: OV.hosts[0]})})`);
  has(d, "CPU 4.0 核（内存、显存 = max(估计值, 实测)"); has(d, "CPU 4 核（提示值）"); has(d, "cpus= 进 CPU 记账，io= 只记录");
  const w = run(O, "waitingTable(D.hosts[0].status.waiting.map(x => ({...x, h: D.hosts[0]})), false)");
  has(tips(w), "CPU 8 核（同名 近 3 次峰值 ×1.1）"); has(w, "CPU 不够：要 8 核");
  // #61：内存与显存的估计来源不同时分开写，相同时只写一次；没有 ram_src 的（#61 之前放行、被接管的）按同一来源
  has(tips(w), "内存 5.8 GiB（同队列 近 2 次峰值 ×1.1）、每卡显存 20.9 GiB（提示值）"); has(d, "估计 内存 5.8 GiB、每卡显存 20.9 GiB（同 tag 近 5 次峰值 ×1.1）");
  const rb = OV.hosts.flatMap(h => (h.status?.running || []).map(j => ({...j, h}))).find(j => j.name === "beta_eval_s7");
  has(run(O, `jobDetail(${JSON.stringify(rb)})`), "估计 内存 3.0 GiB、每卡显存 7.0 GiB（提示值）");
  const al = run(O, "alertsOf(D.hosts[0]).map(a => a.msg)").join("\n"); has(al, `beta_eval_s1 因 CPU 被挡 ${run(O, "dur(900)")}：CPU 不够`);
  ok(!al.includes("alpha_train_s2 因 CPU"), "没被 CPU 挡的也报了");
  ok(JSON.stringify(run(O, 'evk("io_suspect")')) === JSON.stringify(["读写拥塞嫌疑", "warn", "disk"]), "io_suspect 的名字与分类");
  has(page(O, "#/log"), "读写拥塞嫌疑：alpha_train_s1");
});
t("overview", "告警给谁看（#142）：依赖等不来、因 CPU 被挡只给实验会话，不算总览、侧栏与主机圆点，主机页折在「给实验会话的提示」里", () => {
  const to = run(O, "alertsOf(D.hosts[0]).map(a => [a.to, a.msg])"), ag = to.filter(([w]) => w === "agent").map(([, m]) => m);
  ok(ag.some(m => m.includes("beta_eval_s3 等的 beta_prep 跑失败了")) && ag.some(m => m.includes("因 CPU 被挡")), "依赖、CPU 该只给会话：" + JSON.stringify(to));
  ok(to.every(([w]) => ["user", "agent", "both"].includes(w)), "受众写错：" + JSON.stringify(to));
  const all = run(O, "allAlerts()").join("\n"); ok(!all.includes("因 CPU 被挡") && !all.includes("等的 beta_prep 跑失败了"), "总览算进了只给会话的：" + all);
  const nb = run(O, `navModel(route()).secs.find(s => s.id === "host").items[0].bad`); ok(nb === run(O, "forUser(alertsOf(D.hosts[0])).length"), "侧栏红点算进了只给会话的：" + nb);
  const hp = page(O, "#/host/srv-a"), box = hp.slice(hp.indexOf('<div class="alerts">'), hp.indexOf("</div></div>", hp.indexOf('<div class="alerts">')));
  ok(!box.includes("因 CPU 被挡"), "主机页红框里有只给会话的");
  const fold = hp.slice(hp.indexOf('<details class="agal'), hp.indexOf("</details>", hp.indexOf('<details class="agal')));
  has(fold, `给实验会话的提示 ${ag.length} 条`); has(fold, "因 CPU 被挡"); ok(!/<details class="agal[^>]*\bopen\b/.test(fold), "主机页的提示该默认折叠");
  // 借来的约定挡住两边都要：工作台上写给用户的那句（borrow.late_say），不写给会话的命令
  const bc = run(O, "alertsOf(D.hosts[1])").find(a => a.msg.includes("不会在这台放行")); ok(bc.to === "both" && !bc.msg.includes("labmon worker add"), JSON.stringify(bc));
});
t("overview", "alertsOf：可续租时有 [fleet] 段说到主机页点，没有就说去平台；掉线、旧状态、租期将到", () => {
  const a = run(O, `alertsOf({...D.hosts[0], renew: {state: "ready", why: "都跑完了"}}).map(a => a.msg)`).join("\n"); has(a, "到主机页点「续租」");
  const S = ovCtx(OVS, RR), s = run(S, "D.hosts.map(h => alertsOf(h).map(a => a.msg))");
  has(s[0].join("\n"), `掉线 ${run(S, "dur(20 * 60)")}。租期已到`); has(s[1].join("\n"), "去平台续租"); has(s[1].join("\n"), `worker 已 ${run(S, "dur(1000)")}没更新状态`);
  has(s[3].join("\n"), "读不出"); ok(!leak(s.flat().join("\n")), "告警里漏 NaN/undefined");
  has(run(O, `alertsOf({...D.hosts[0], lease_end: D.generated + 3600}).map(a => a.msg)`).join("\n"), "到期");
  const dw = run(O, "alertsOf(D.hosts[0]).map(a => a.msg)").join("\n"); ok(dw.includes("的 beta_eval_s3 等的 beta_prep 跑失败了"), "依赖等不来：" + dw);
  ok(!run(O, "alertsOf(D.hosts[0]).map(a => a.msg)").join("\n").includes("beta_eval_s2"), "被临时指令挡住的不报警");
  const bc = run(O, "alertsOf(D.hosts[1]).map(a => a.msg)").join("\n");                 // 借来的主机上约定归还挡住、不会再放的清单（borrow.late_jobs）
  has(bc, "srv-b：Beta 实验 的 车道 3 · b3 还有 2 个任务不会在这台放行：约定归还 11-14 09:00 前跑不完（估 330 分钟）；截止取的是 OWNER 的租期到期，提前 1 小时，延长约定推不迟它，由实验会话换到别的在用主机");
  ok(!bc.includes("改约定"), "截止取对方租期时不写改约定");                 // 车道名取 laneOf + laneName，文字取 borrow.late_say
  has(run(O, `alertsOf({...D.hosts[1], borrow: {...D.hosts[1].borrow, late: [{...D.hosts[1].borrow.late[0], queue: "jobs/zz.txt"}]}}).map(a => a.msg)`).join("\n"), "的 jobs/zz.txt 还有", "对不上快照车道的用队列名");
  ok(!run(O, "alertsOf({...D.hosts[1], borrow: {...D.hosts[1].borrow, late: []}}).map(a => a.msg)").join("\n").includes("不会在这台放行"), "没有 late 也报了约定挡住");
  has(page(O, "#/host/srv-b"), "不会在这台放行");
});
t("overview", "概况条：主机在 [fleet] 里才给「续租」按钮，不在就只有预备 / 取消预备", () => {
  const h = run(O, "hostStrip(D.hosts[0])"); has(h, "renewone"); has(h, "预备续租"); has(h, "本地依赖 2 格");
  const S = ovCtx(OVS, RR), h2 = run(S, "hostStrip(D.hosts[1])"); ok(!h2.includes("renewone"), "主机清单没有 [fleet] 段还给了续租一台的按钮"); has(h2, "取消预备");
  clean(run(O, "hostStrip(D.hosts[1])"), "借来的概况条");
  const bs = run(O, "borrowSec(D.fleet.servers[0])"); has(bs, `借来</span><span>${run(O, `ts(${NOW - 16 * H})`)}`, "借用抽屉：借来的时刻是 epoch 秒，经 ts");
  ok(run(O, "dtLocal(0)") === "" && /^\d{4}-\d\d-\d\dT\d\d:\d\d$/.test(run(O, `dtLocal(${NOW})`)), "dtLocal：epoch 秒 → 时刻控件的值");
});
t("overview", "窄屏的弹窗贴在屏幕底部，宽表照常横向滚，页面不被撑宽（Issue #11）", () => {
  const m = /@media \(max-width: 860px\) \{([\s\S]*?)\n\}\n<\/style>/.exec(ovHtml); ok(m, "找不到窄屏一段");
  const nar = m[1].replace(/\s+/g, " ");                 // 空白压成一个空格再比，换行、对齐改了不误报
  ok(/\.scroll:has\(\.pop\) ?\{ ?overflow-x: auto;? ?\}/.test(nar), "窄屏下开着弹窗的滚动区又不滚了（宽表会把整页撑宽）");
  ok(/\.pop, ?\.pop-l \.pop ?\{ ?position: fixed; top: auto;[^}]*bottom: calc\(var\(--tabh\) \+ var\(--kb, 0px\)\);[^}]*max-width: none;[^}]*overflow-y: auto;/.test(nar),
     "窄屏的弹窗要 fixed 贴底、让出底栏与软键盘、太高时自己滚");
  ok(/:focus\) \.pop, ?#drw \.pop ?\{ ?bottom: calc\(8px \+ env\(safe-area-inset-bottom\) \+ var\(--kb, 0px\)\);/.test(nar), "打字时（底栏收起）与抽屉里的弹窗贴到底、让出软键盘");
  ok(/body:has\(#drw [^{]*:focus\) #drw \.pane ?\{ ?bottom: var\(--kb, 0px\);/.test(nar), "抽屉里打字时抽屉要让出软键盘（批量续租底部的验证码，Codex 审查 #20）");
  ok(/\.scroll:has\(\.pop\) ?\{ ?overflow: visible;? ?\}/.test(ovHtml.slice(0, m.index).replace(/\s+/g, " ")), "宽屏的弹窗不被滚动区裁掉（这条不能丢）");
  // fitPops：量软键盘占掉的高度与看得见的高度；贴底（fixed）的不挪；宽屏的超出所在区域（与视口取交）左边往右挪、右边往左挪（#143：抽屉里靠右、向右开的弹窗）
  const O = ovCtx(OV, RR);
  run(O, `SP = {}; document.documentElement.style.setProperty = (k, v) => { SP[k] = v; };
    DB = {getBoundingClientRect: () => ({left: 600, right: 1200})};      // 抽屉贴右，比视口（1000 宽）还宽出一截
    mkPop = (pos, left, right, db) => ({pos, style: {}, offsetLeft: 10, closest: q => q === ".db" && db ? DB : null, getBoundingClientRect: () => ({left, right})});
    PS = [mkPop("fixed", -40, 200), mkPop("absolute", -40, 200), mkPop("absolute", 900, 1180, true), mkPop("absolute", 650, 900, true)];
    PS[0].style.right = "-57px"; PS[0].style.left = "3px"; document.querySelectorAll = q => q === ".pop" ? PS : [];
    getComputedStyle = el => ({position: el.pos}); visualViewport = {height: 500, offsetTop: 20}; innerHeight = 800; document.documentElement.clientWidth = 1000; fitPops()`);
  ok(run(O, 'SP["--kb"]') === "280px" && run(O, 'SP["--vvh"]') === "500px", "软键盘：" + JSON.stringify(run(O, "SP")));
  const ps = i => run(O, `PS[${i}].style.left + "|" + PS[${i}].style.right`);
  ok(ps(2) === "-178px|auto", "抽屉里超出视口右边的弹窗没往左挪回视口（#143）：" + ps(2));
  ok(run(O, `(() => { const f0 = fitPops; let n = 0; fitPops = () => n++; location.hash = "#/p/proj-a/manage"; render(true); render(false); fitPops = f0; location.hash = "#/"; return n; })()`) === 2,
     "项目页（「管理」抽屉）重画后没量弹窗（#143）");
  ok(ps(0) === "|", "宽屏挪过又转到窄屏的弹窗，内联的 left、right 没清掉（会盖过贴底的样式、伸出屏幕）：" + ps(0));
  ok(ps(1) === "58px|auto", "宽屏超出左边的没往右挪到区域左边 8px 处：" + ps(1));
  ok(ps(3) === "|", "放得下的弹窗不该挪：" + ps(3));
  run(O, "SP = {}; visualViewport = undefined; fitPops()"); ok(!("--kb" in run(O, "SP")), "没有 visualViewport 时不该写 --kb");
});
t("overview", "选一个将来的时刻（Issue #9、#26）：whenPick 的日期框 + 时分下拉、缺省现在、小签累加、一句到几点；过去的、没填完的、停在现在的拦下；显示经 tsNear", () => {
  const O = ovCtx(OV, RR), T = NOW + 26 * H + 37 * 60, until = OV.fleet.servers.find(s => s.id === "srv-b").borrow.until;
  ok(new Date(until * 1000).getMinutes() === 37, "快照里的约定该是 18:37");
  // 借用抽屉、概况条、小标签：约定与截止经 tsNear（今天的只写时分），精确到分
  const bs = run(O, 'borrowSec(D.fleet.servers.find(s => s.id === "srv-b"))');
  has(bs, `约定</span><span>${run(O, `tsNear(${until})`)}</span>`, "借用抽屉的约定"); has(bs, `截止</span><span>${run(O, `tsNear(${NOW + 6 * 86400})`)} `, "借用抽屉的截止");
  ok(run(O, `tsNear(${until})`).endsWith(":37"), "约定要写到分");
  const soon = NOW + 120, near = run(O, `tsNear(${soon})`), bb = run(O, `borrowBadge({...D.hosts[1].borrow, cutoff: ${soon}, why: "约定"}, D.generated)`);
  has(bb, `到 ${near}`); clean(bb, "约定在今天的小标签");
  if (new Date(soon * 1000).toDateString() === new Date().toDateString()) ok(near === run(O, `tsHM(${soon})`), "今天的只写时分：" + near);
  has(run(O, `borrowText({...D.hosts[1].borrow, cutoff: ${soon}, why: "约定"}, D.generated)`), `${near} 前跑得完的任务才放行（约定）`);
  // 借来用的表单（Issue #26）：日期框 + 时、分两个下拉（分按 5 分钟一档，已有的值不在档上时另加它那一分），小签、一句到几点
  const form = v => run(O, `POP = {k: "bor", id: "srv-d", sel: new Set(), until: ${v}, ok: true, light: true, ack: false}; borrowForm(srvById("srv-d"))`);
  const TD = run(O, `dtLocal(${T})`), [, TDd, TDh, TDm] = /^(.*)T(\d\d):(\d\d)$/.exec(TD);
  let f = form(`dtLocal(${T})`);
  has(f, `<input type="date" id="fbuntil" class="when-in when-d" min="${run(O, "dtLocal(Date.now() / 1000).slice(0, 10)")}" value="${TDd}">`);
  ok(!/type="datetime-local"/.test(f), "还在用 datetime-local（有的浏览器只弹日期，时分要手打）");
  ok(new RegExp(`id="fbuntil-h"[^>]*>(<option>\\d\\d</option>)*<option selected>${TDh}</option>`).test(f), "时的下拉没选中 " + TDh);
  ok(new RegExp(`id="fbuntil-m"[^>]*>.*<option selected>${TDm}</option>`).test(f), "分的下拉没选中 " + TDm + "（不在 5 分钟档上的要另加）");
  ok((f.match(/<select id="fbuntil-m"[^]*?<\/select>/)[0].match(/<option/g) || []).length === 12 + (+TDm % 5 ? 1 : 0), "分的下拉按 5 分钟一档");
  ok(!/<option value="" selected>--/.test(f), "有值时不该留「--」");
  for (const c of [">现在<", ">+1 小时<", ">+3 小时<", ">+1 天<", ">+3 天<", ">不约定<"]) has(f, c, "小签");
  for (const c of ["+2 小时", "明早 9 点", "今晚 23 点"]) ok(!f.includes(c), "旧的快捷小签没删：" + c);
  has(f, `到 <b>${run(O, `tsNear(${T})`)}</b>，还有 `); has(f, "约定还的时间"); clean(f, "借来用的表单");
  // 不约定：空着时两个下拉写「--」、「不约定」选中；点「借来用」照样发（until 为空）
  f = form('""'); has(f, "不约定：只按对方租期截止"); has(f, `class="wq clr on"`, "空着时「不约定」选中"); ok((f.match(/<option value="" selected>--<\/option>/g) || []).length === 2, "空着时时、分写「--」");
  run(O, `FP = []; fleetPost = async (k, b) => { FP.push(b); return {}; }; pollOps = () => {}; borrowAct("bgo", "srv-d")`);
  ok(run(O, "FP.length") === 1 && run(O, "FP[0].until") === null, "不约定没发出去：" + JSON.stringify(run(O, "FP")));
  // 缺省：打开借来用的弹窗，值是现在的下一个 5 分钟档；说明行写“请选一个将来的时刻”（不标红），点确认时拦下
  run(O, `POP = null; borrowAct("bpick", "srv-d")`);
  const wn = run(O, "whenNow()"), now = Date.now() / 1000;
  ok(wn % 300 === 0 && wn >= now - 1 && wn - now <= 300, "whenNow 该是下一个 5 分钟档：" + (wn - now));
  ok(run(O, "POP.until") === run(O, `dtLocal(${wn})`) && run(O, "POP.wnow") === true, "缺省该是现在：" + run(O, "POP.until"));
  f = run(O, 'borrowForm(srvById("srv-d"))');
  has(f, `<span class="mut">请选一个将来的时刻`); ok(!f.includes(`class="when bad"`), "缺省（现在）不该一打开就标红"); ok(/class="wq on"[^>]*>现在</.test(f), "缺省时「现在」选中");
  run(O, `FP = []; POP.ok = true; borrowAct("bgo", "srv-d")`); ok(run(O, "FP.length") === 0, "停在「现在」还发了请求"); ok(!run(O, "POP.err"), "拦下时又写了一行红字");
  has(run(O, 'whenSay(POP.until, "不约定", {fresh: true, stop: true})'), `<span class="bad">请选一个将来的时刻`, "拦下后说明行标红");
  // 小签在当前的值上累加（可连点）；「现在」回到下一个 5 分钟档；清空是不约定
  run(O, `whenTap({add: "24"}); whenTap({add: "24"})`);
  ok(run(O, "POP.until") === run(O, `dtLocal(${wn + 48 * H})`) && run(O, "POP.wnow") === false, "点两次 +1 天该是 +2 天：" + run(O, "POP.until"));
  run(O, `whenTap({add: "3"})`); ok(run(O, "POP.until") === run(O, `dtLocal(${wn + 51 * H})`), "+3 小时接着加");
  has(run(O, 'borrowForm(srvById("srv-d"))'), `到 <b>${run(O, `tsNear(${wn + 51 * H})`)}</b>`);
  run(O, `FP = []; borrowAct("bgo", "srv-d")`); ok(run(O, "FP.length") === 1 && run(O, "FP[0].until") === wn + 51 * H, "累加后的约定没发对：" + JSON.stringify(run(O, "FP")));
  run(O, `POP = {k: "bor", id: "srv-d", sel: new Set(), until: dtLocal(${T}), ok: true, light: true}; whenTap({add: "1"})`); ok(run(O, "POP.until") === run(O, `dtLocal(${T + H})`), "+1 小时要加在选择器当前的值上");
  run(O, `whenTap({wq: "now"})`); ok(run(O, "POP.until") === run(O, `dtLocal(whenNow())`) && run(O, "POP.wnow") === true, "「现在」没回到现在");
  run(O, `whenTap({wq: "clr"})`); ok(run(O, "POP.until") === "" && run(O, "POP.wnow") === false, "清空没变成不约定");
  run(O, `whenTap({add: "1"})`); ok(run(O, "POP.until") === run(O, `dtLocal(whenNow() + ${H})`), "空着时 + 从现在加");
  // 已经过去的值（改约定时原约定已过）：+ 从现在加，不在过去的值上加
  run(O, `POP.until = dtLocal(${NOW - 5 * H}); whenTap({add: "1"})`); ok(run(O, "POP.until") === run(O, `dtLocal(whenNow() + ${H})`), "过去的值点 + 该从现在加：" + run(O, "POP.until"));
  run(O, `POP.until = "T15:25"; whenTap({add: "24"})`); ok(run(O, "POP.until") === run(O, `dtLocal(whenNow() + ${24 * H})`), "没填完的点 + 该从现在加");
  // 焦点停在时刻控件上点小签：先失焦再重画（common.js 的点击处理），显示的与 POP 里的（也就是要发的）一致；不失焦时 render(false) 被 editing 挡住（审查 #27 第 1 条，#131）
  {
    const Q = ovCtx(OV, RR, "#/"), F = NOW + 26 * H, D9 = run(Q, `dtLocal(${F})`).slice(0, 10) + "T09:55";
    run(Q, `render(true); POP = {k: "sf", id: "proj-b", until: "${D9}", wnow: false}; render(true); BL = 0`);
    has(el(Q, "view").innerHTML, `<option selected>09</option>`, "改时 09 后的弹窗");
    for (const k of [{when: "schu", add: "3"}, {when: "schu", wq: "now"}, {when: "schu", wq: "clr"}]) {
      run(Q, `document.activeElement = {type: "select-one", closest(q) { return /select/.test(q) ? this : null; }, blur() { BL++; document.activeElement = document.body; }}`);
      clickOn(Q, "[data-when]", k, "button");
      ok(run(Q, "document.activeElement === document.body"), JSON.stringify(k) + "：点小签前没让时刻控件失焦");
      if (k.add) has(el(Q, "view").innerHTML, `<option selected>12</option>`, "点 +3 小时后重画没跟上（发的是 12:55、显示还是 09:55）");
      if (k.wq === "now") has(el(Q, "view").innerHTML, `<option selected>${run(Q, "dtLocal(whenNow()).slice(11, 13)")}</option>`, "点「现在」后重画没跟上");
      if (k.wq === "clr") has(el(Q, "view").innerHTML, `class="wq clr on"`, "点「无期限」后重画没跟上");
    }
    ok(run(Q, "BL") === 3, "失焦次数：" + run(Q, "BL"));
  }
  // 改约定：已有的约定照旧；没约定的借用缺省仍是不约定
  run(O, `POP = null; borrowAct("bedit", "srv-b")`); ok(run(O, "POP.until") === run(O, `dtLocal(${until})`) && !run(O, "POP.wnow"), "改约定的缺省该是现在的约定");
  // 改了日期、时或分：空着的另外几段从现在补上；日期只填一半（badInput）算没填完
  run(O, `WB = (() => { const ps = [{value: "2026-12-20", validity: {}}, {value: ""}, {value: ""}], say = {dataset: {none: "不约定", empty: "不约定"}, innerHTML: ""};
    const box = {querySelectorAll: q => q === ".when-in" ? ps : [], querySelector: () => say, classList: {toggle(c, on) { box.bad = on; }}};
    ps.forEach(p => { p.closest = () => box; }); return {ps, say, box}; })(); POP = {k: "bor", id: "srv-d", until: "", wnow: true}; whenInput(WB.ps[0])`);
  ok(run(O, "POP.until") === "2026-12-20T" + run(O, "dtLocal(whenNow()).slice(11)") && run(O, "POP.wnow") === false, "选了日期没补时分：" + run(O, "POP.until"));
  has(run(O, "WB.say.innerHTML"), "到 <b>"); ok(!run(O, "WB.box.bad"), "补齐的不该标红");
  run(O, `WB.ps[0].value = ""; WB.ps[0].validity = {badInput: true}; whenInput(WB.ps[0])`);
  ok(run(O, "whenHalf(POP.until)"), "日期只填一半该算没填完：" + run(O, "POP.until")); has(run(O, "WB.say.innerHTML"), "没填完"); ok(run(O, "WB.box.bad"), "没填完的要标红");
  // 过去的：标红并拦下
  f = form(`dtLocal(${NOW - 600})`); has(f, `<span class="bad">${run(O, `tsNear(${NOW - 600})`)} 已经过去了`); clean(f, "过去的约定");
  has(f, `class="when bad"`, "过去的时刻：控件标红");
  // 点「借来用」时约定已过去：不发请求，也不另写一行红字（说明行就是提示）
  run(O, `FP = []; POP.ok = true; borrowAct("bgo", "srv-d")`);
  ok(run(O, "FP.length") === 0, "过去的约定还发了请求"); ok(!run(O, "POP.err"), "拦下时又写了一行红字：" + run(O, "POP.err"));
  ok(!run(O, 'borrowForm(srvById("srv-d"))').includes("rn-err"), "拦下后表单里多出 rn-err");
  // 没填完（缺日期，或日期框 badInput）：拦下，不当成「不约定」
  run(O, `FP = []; POP.until = "T15:25"; borrowAct("bgo", "srv-d")`); ok(run(O, "FP.length") === 0, "缺日期的约定发了出去");
  run(O, `FP = []; POP.until = ""; document.getElementById("fbuntil").validity = {badInput: true}; borrowAct("bgo", "srv-d")`);
  ok(run(O, "FP.length") === 0, "没填完的约定被当成不约定发了出去"); run(O, `document.getElementById("fbuntil").validity = {badInput: false}`);
  ok(run(O, 'whenSay("T15:25", "不约定：只按对方租期截止", {empty: "不约定"})').includes("没填完"), "没填完的说明");
  // 约定晚于对方租期截止：说明行写按租期截止
  const cut = OV.fleet.servers.find(s => s.id === "srv-b").lease_cut;
  f = run(O, `POP = {k: "bor", id: "srv-b", sel: new Set(), until: dtLocal(${until}), ok: true, light: true, ack: false}; borrowForm(srvById("srv-b"))`);
  has(f, `晚于 ${run(O, `tsNear(${cut})`)}（租期截止），按它截止`, "约定晚于租期截止");
  f = run(O, `POP.until = dtLocal(${cut - 3600}); borrowForm(srvById("srv-b"))`); ok(!f.includes("按它截止"), "早于租期截止的不该提");
  // 对方说好不用（#30）：勾选框要有约定时间，勾上的随请求发 away；说明句、抽屉的标签都换成说好不用
  f = run(O, `POP = {k: "bor", id: "srv-d", sel: new Set(), until: "", ok: true, light: true, away: true}; borrowForm(srvById("srv-d"))`);
  ok(/data-baway\s+disabled/.test(f), "不约定时说好不用该灰掉"); has(f, "说好不用要有个时限：先选约定时间");
  f = run(O, `POP.until = dtLocal(${until}); borrowForm(srvById("srv-d"))`); ok(/data-baway checked\s*>/.test(f), "有约定时说好不用该能勾"); has(f, "说好约定时间前不用：不给这人留余量");
  run(O, `FP = []; borrowAct("bgo", "srv-d")`); ok(run(O, "FP.length") === 1 && run(O, "FP[0].away") === true, "说好不用没发出去：" + JSON.stringify(run(O, "FP")));
  run(O, `FP = []; POP = {k: "bor", id: "srv-d", sel: new Set(), until: "", ok: true, light: true, away: true}; borrowAct("bgo", "srv-d")`);
  ok(run(O, "FP.length") === 1 && run(O, "FP[0].away") === false, "不约定时不该发 away");
  // 说明句：勾了说好不用就不再接“按近 7 天推荐、超出的部分不放”；勾选框与约定时间改了，说明句就地跟上（焦点在控件上时 render(false) 被跳过）
  f = run(O, `POP = {k: "bor", id: "srv-d", sel: new Set(), until: dtLocal(${until}), ok: true, light: true, away: true}; borrowSay(srvById("srv-d"), POP)`);
  has(f, "前不用：不给对方留余量"); ok(!f.includes("对方超出的部分不放新任务") && !f.includes("近 7 天"), "说好不用时说明句前后矛盾：" + f);
  run(O, `POP.away = false; borrowSync()`); has(run(O, `document.getElementById("bfsay").innerHTML`), "近 7 天", "取消勾后说明句没跟上");
  run(O, `POP = null; borrowAct("bedit", "srv-b")`); ok(run(O, "POP.away") === true, "改约定时说好不用该按现在的借用勾着");
  run(O, `POP = {k: "bor", id: "srv-d", sel: new Set(), until: dtLocal(${until}), ok: true, light: true, away: true}`);
  run(O, `POP.away = true; POP.until = ""; borrowSync()`);
  ok(run(O, `document.getElementById("bfaway").disabled && !document.getElementById("bfawayno").hidden`), "约定时间清空后说好不用该灰掉");
  const kw = run(O, `keepText({vram: 2, ram: 7.7, cpu: 1.7, hold: true, away: ${until}})`);
  has(kw, `对方说好 ${run(O, `tsNear(${until})`)} 前不用：不给对方留余量`); ok(!kw.includes("给对方留显存"), "说好不用时不该再写留多少");
  const bsa = run(O, `const k0 = D.hosts[1].borrow.keep; D.hosts[1].borrow.keep = {...k0, away: ${until}}; const r = borrowSec(D.fleet.servers.find(s => s.id === "srv-b")); D.hosts[1].borrow.keep = k0; r`);
  has(bsa, `说好不用</span><span>${run(O, `tsNear(${until})`)} 前不用，不给对方留余量`, "抽屉里说好不用的一行"); clean(bsa, "说好不用的借用抽屉");
  ok(run(O, 'whenT("") === null && !whenPast("") && whenPast("T15:25") && whenHalf("T15:25") && !whenHalf("") && !whenHalf(dtLocal(D.generated))'), "whenT / whenPast / whenHalf");
  // 改优先的到期也是同一个控件：缺省现在，停在现在拦下，无期限照常
  run(O, `POP = null; schedAct("f", "proj-b")`); ok(run(O, "POP.wnow") === true && run(O, "POP.until") === run(O, "dtLocal(whenNow())"), "优先的到期缺省该是现在");
  const sc = run(O, "schedCard()");
  has(sc, `id="schu" class="when-in when-d"`); has(sc, `id="schu-h"`); has(sc, ">无期限<"); has(sc, "请选一个将来的时刻"); clean(sc, "优先的到期");
  run(O, `SF = 0; fetch = () => { SF++; return new Promise(() => {}); }; schedAct("fgo", "proj-b")`); ok(run(O, "SF") === 0, "优先停在「现在」还发了请求");
  run(O, `whenTap({wq: "clr"}); schedAct("fgo", "proj-b")`); ok(run(O, "SF") === 1, "无期限的优先没发出去");
  has(run(O, 'POP = {k: "sf", id: "proj-b", until: ""}; schedCard()'), "无期限：用完点「撤销优先」");
  // 优先的到期：规则一句、小标签的悬停、临时指令表都经 tsNear（今天的只写时分）
  const sn = NOW + 120, sd = new Date(sn * 1000).toDateString() === new Date().toDateString();
  const sc2 = run(O, `POP = null; D.sched.projects[0].focus = {until: ${sn}}; schedCard()`);
  has(sc2, `（到 ${run(O, `tsNear(${sn})`)}）`, "规则一句"); has(tips(sc2), `到 ${run(O, `tsNear(${sn})`)}`, "优先小标签的悬停");
  const ot = run(O, `ovrTable([{kind: "focus", target: "proj-a", until: ${sn}, h: D.hosts[0]}], true)`); has(ot, `<td class="sm">${run(O, `tsNear(${sn})`)}</td>`, "临时指令表的到期");
  if (sd) for (const [s, w] of [[sc2, "规则一句"], [ot, "临时指令表"]]) ok(!s.includes(run(O, `ts(${sn})`)), w + "：今天的还写月-日");
  // 恶意字符串：对方名字进了表单
  const E = ovCtx(OVE, RRE);
  for (const s of run(E, "D.fleet.servers.map(s => s.id)")) clean(run(E, `POP = {k: "bor", id: ${JSON.stringify(s)}, sel: new Set(), until: dtLocal(${T}), ok: false, light: true}; borrowForm(srvById(${JSON.stringify(s)}))`), "恶意快照的借用表单 " + s);
});
t("overview", "保活看门狗：保活被拉起进主机告警，借用抽屉列出暂停着的与可能的看门狗", () => {
  has(run(O, "alertsOf(D.hosts[1]).map(a => a.msg)").join("\n"), "GPU 保活在任务运行中被拉起 1 次（1 小时内，最近");
  ok(!run(O, "alertsOf(D.hosts[0]).map(a => a.msg)").some(a => a.includes("保活")), "没被拉起的主机不该有保活告警");
  const bs = run(O, 'borrowSec(D.fleet.servers.find(s => s.id === "srv-b"))');
  has(bs, "暂停着（还卡时恢复）：PID 4242（bash keepalive_final_check.sh：提到保活程序，在循环里）"); has(bs, "找到、没动：PID 4300");
  clean(bs, "借用抽屉的看门狗");
});

t("overview", "保活停不下来（#102）：worker 要卡时停保活一直没停成，借来的主机给用户一条告警，写明原因与该找谁", () => {
  const al = run(O, "alertsOf(D.hosts[1])").filter(a => a.msg.includes("GPU 保活停不下来"));
  ok(al.length === 1 && al[0].to === "user", "停不了的保活该给用户一条告警");
  has(al[0].msg, "（ka.sh stop 退出码 1：另一个调用者正持着锁，");
  has(al[0].msg, "起 160 次），任务和它同卡分算力、少了它占的显存；请 OWNER 停掉它或查 ka.sh");
});
// #72：换人登记了新的一台、旧的从列表去掉后，开着旧那台的抽屉跟到新的那台（地址原地换掉），不停在“已经没有这一台”
t("overview", "换人后抽屉跟到新的那台", () => {
  run(O, `SAVED = [D, OPS, history.replaceState]; history.replaceState = (s, t, h) => { location.hash = h; };
    D = {...D, fleet: {...D.fleet, servers: D.fleet.servers.filter(s => s.id !== "srv-d")}};
    OPS = {t: D.generated + 1, op: {kind: "swap", person: "PERSON4", active: true, ids: ["srv-d"], t0: D.generated, steps: [{name: "登录", state: "ok"}], conn: {sid: "srv-e", holder: "PERSON4", cmd: "ssh -p 1 x"}}}`);
  const h = page(O, "#/fleet/srv/srv-d");
  ok(O.location.hash === "#/fleet/srv/srv-e", "抽屉没跟到新的那台：" + O.location.hash);
  ok(!h.includes("组内服务器里已经没有这一台"), "换人后抽屉停在“已经没有这一台”");
  has(h, "swap（→ PERSON4）进行中", "新的那台抽屉里接着给进度");
  run(O, 'OPS = {...OPS, op: {...OPS.op, conn: null}}');            // 不知道换到了哪台：照旧说一句
  has(page(O, "#/fleet/srv/srv-d"), "组内服务器里已经没有这一台"); ok(O.location.hash === "#/fleet/srv/srv-d");
  run(O, 'OPS = {...OPS, op: {...OPS.op, ids: ["srv-x"], conn: {sid: "srv-e", holder: "PERSON4", cmd: "ssh -p 1 x"}}}');   // 别的操作的 conn：不把无关的抽屉带走
  has(page(O, "#/fleet/srv/srv-d"), "组内服务器里已经没有这一台"); ok(O.location.hash === "#/fleet/srv/srv-d", "op.ids 不含这一台也跳走了：" + O.location.hash);
  run(O, "[D, OPS, history.replaceState] = SAVED");
});
t("overview", "short：去掉括号、超过 5 个字截成 4 个加省略号", () => { ok(run(O, 'short("abcdefg")') === "abcd…"); ok(run(O, 'short("主机（A6000）")') === "主机"); });
t("overview", "组内服务器页：指标、各状态的卡片、未识别进程；人员与共享盘选项卡", () => {
  const h = page(O, "#/fleet");
  has(h, "2 / 4 台", "专用池"); has(h, "3 / 2 台", "公用池"); has(h, "超过全组上限"); has(h, "未识别的 GPU 进程"); has(h, "宿主机 PID 777");
  has(h, "PERSON4 的作业在平台上找不到", "核对的提醒"); has(h, "已关机"); has(h, "掉线");
  const p = page(O, "#/fleet/people"); has(p, "NEWBIE"); has(page(O, "#/fleet/people/NEWBIE"), "下个月来");
  const d = page(O, "#/fleet/disk"); has(d, "OWNER"); has(d, "stray_dir");
});
t("overview", "人员页（#15）：头像分得清人、卡上没有无字图标、账号与服务器分两行标、提醒在人员选项卡收成一行、设置浮窗够宽", () => {
  const O = ovCtx(OV, RR);
  const ab = run(O, '["PERSON3", "PERSON4", "PERSON12", "OWNER", "alice-2", "ab_cd", "张三丰", ""].map(pabbr).join(",")');
  ok(ab === "P3,P4,P12,OW,A2,AC,张三,?", "缩写：" + ab);
  const p = page(O, "#/fleet/people");
  has(p, ">P3</span>"); has(p, ">P4</span>"); ok(!p.includes(">PER</span>"), "人员卡头像还是 PER");
  has(p, 'style="--pc:', "共享盘上有配色的人，头像沿用那份配色");
  const grid = p.slice(p.indexOf("people-grid"));
  ok(!/class="ib"|data-place="card"/.test(grid), "人员卡上还有没文字的图标按钮");
  has(grid, '<span class="pk sm">服务器</span>'); has(grid, '<span class="pk">看板账号</span>');
  has(p, ">修改默认设置</button>", "默认设置是带文字的按钮");
  has(page(O, "#/fleet/people/OWNER"), "分配一台</button>", "分配一台在人员抽屉里");
  // 提醒：服务器选项卡完整显示、指标条在；人员与共享盘收成一行可展开，不放指标条
  const sv = page(O, "#/fleet"); has(sv, '<div class="fsum">'); has(sv, '<div class="alerts"><div>⚠');
  for (const r of ["#/fleet/people", "#/fleet/disk"]) {
    const h = page(O, r); ok(!h.includes('<div class="fsum">'), r + " 还有指标条");
    ok(/<details class="alerts fold fleet-al" data-fold="fleetal" ><summary>[\s\S]*?⚠ 组内 \d+ 条提醒/.test(h), r + " 的提醒没收成一行");
  }
  run(O, 'const G0 = ST.get; ST.get = (k, d) => k === "cardOpen" ? {fleetal: true} : G0(k, d)');
  has(page(O, "#/fleet/people"), 'data-fold="fleetal" open>', "展开后重画又收起");
  has(ovHtml, ".pop:has(> .pform) { width: min(560px", "人员设置浮窗的宽度");
});
t("overview", "人员卡按当前管理员显示我（#70）", () => {
  const O = ovCtx(OV, RR);
  run(O, 'ME = {...ME, fleet_person: "ME"}');
  has(page(O, "#/fleet/people"), '>ME</b><span class="chip">我</span>');
  run(O, 'ME = {...ME, fleet_person: "PERSON3"}');
  has(page(O, "#/fleet/people"), '>PERSON3</b><span class="chip">我</span>');
  ok(!page(O, "#/fleet/people").includes('>ME</b><span class="chip">我</span>'));
});

t("overview", "人员卡（#52）：借卡的用量算给借的人，主人那边记“借出”；自用主机也有占比条与逐小时条", () => {
  const p = page(O, "#/fleet/people");
  ok(!p.includes("看主机页") && !p.includes("自用主机的用量"), "自用主机不再特殊化");
  const card = n => { const i = p.indexOf(`aria-label="${n} 的详情"`); const j = p.indexOf('class="fc pc', i); return p.slice(i, j < 0 ? undefined : j); };
  const me = card("ME"), ow = card("OWNER");
  has(me, "实验 88%"); has(me, "+1 台"); has(page(O, "#/fleet/people/ME"), "借 OWNER · 15105"); has(me, "借 OWNER 的 15105 16 小时");
  has(ow, "借出 16 小时（35%）"); has(ow, "借给 ME 16 小时（15105）"); ok(!ow.includes("实验时利用率"), "借出不算进主人的利用率");
  has(page(O, "#/fleet"), "fc-hs", "卡片墙"); ok((page(O, "#/fleet").match(/class="fc-hs"/g) || []).length === 6, "自用主机的卡也有逐小时条（关机的那台没有）");
  const sa = page(O, "#/fleet/srv/srv-a"); has(sa, "近 7 天"); has(sa, "近 30 天");
  has(run(O, 'hourStrip(D.fleet.servers[0], true)'), "借给 ME");
  const ph = run(O, `perHist({d: {name: "ME", days: [{t: ${NOW - 86400}, h: {job: 20, lent: 0}, util_job: 90}], total: {...D.fleet.holders.find(h => h.holder === "ME")},
    servers: [{id: "srv-b", from: "OWNER", first: ${NOW - 86400}, last: ${NOW}, hours: 16, frac: {job: 1}, util_job: 95}, {id: "srv-a", first: ${NOW - 86400}, last: ${NOW}, hours: 46, frac: {job: 0.9}, util_job: 90}]}})`);
  has(ph, "借 OWNER 的"); has(ph, "借来 / 借出"); clean(ph, "人员抽屉的近 30 天");
  const po = run(O, `perHist({d: {name: "OWNER", days: [{t: ${NOW - 86400}, h: {keepalive: 8, lent: 16}, util_job: null}], total: {...D.fleet.holders.find(h => h.holder === "OWNER")},
    servers: [{id: "srv-b", first: ${NOW - 86400}, last: ${NOW}, hours: 24, frac: {job: 0, lent: 0.66}, util_job: null, lent: 16, lent_to: [{id: "srv-b", to: "ME", h: 16}]}]}})`);
  has(po, "借出 16 小时"); has(po, "借出</span>", "图例"); clean(po, "主人的近 30 天");
});
// 自用主机的抽屉与组内服务器同一套（#58）：节一一对应（借用换成 worker），概况的行同名；按钮按这台能做什么给，续租是主机页那一组（renewBtns）
t("overview", "自用主机抽屉（#58）：与组内服务器同一套模板；按钮按能力给；三处小问题", () => {
  const secs = h => [...h.matchAll(/<section class="card"[^>]*><div class="chd wrap"><b>([^<]*)<\/b>/g)].map(m => m[1]);
  const keys = h => { const i = h.indexOf("<b>概况</b>"), j = h.indexOf("</section>", i); return [...h.slice(i, j).matchAll(/<div class="kv">([\s\S]*?)<\/div>/g)]
    .flatMap(m => [...m[1].matchAll(/<span>([^<]*)<\/span><span>/g)].map(x => x[1])); };     // kvHtml：<span>键</span><span>值…
  const sa = page(O, "#/fleet/srv/srv-a"), sd = page(O, "#/fleet/srv/srv-d");
  ok(JSON.stringify(secs(sa)) === JSON.stringify(["概况", "worker", "现在", "在跑什么", "本地盘", "近 7 天", "开机参数", "事件"]), "自用主机的节：" + JSON.stringify(secs(sa)));
  ok(JSON.stringify(secs(sd).filter(s => s !== "借用")) === JSON.stringify(secs(sa).filter(s => s !== "worker")), "与组内服务器的节对不上：" + JSON.stringify(secs(sd)));
  // 「算力池」（入池，#24）：自用主机也能入池（#79），两边同一行
  ok(JSON.stringify(keys(sa)) === JSON.stringify(keys(sd)), `概况的行不一样：${JSON.stringify(keys(sa))} / ${JSON.stringify(keys(sd))}`);
  has(sa, "保活由 worker 停启"); ok(!sa.includes("资源曲线、续租与开保活在主机页"), "旧的那句还在");
  for (const b of ["预备续租", "续租", "换人", "关机"]) has(sa, `${b}</button>`, "自用主机的按钮：");
  for (const b of ["立即探测", "开保活", "这是保活", "借来用"]) ok(!sa.includes(b + "</button>"), "自用主机不该有：" + b);
  has(sd, "立即探测", "组内服务器照旧能立即探测");
  for (const l of ["利用率", "显存", "CPU", "内存", "本地盘"]) has(text(sa.slice(sa.indexOf("<b>现在</b>"))), l, "现在：");
  has(tips(sa), "含保活", "内存悬停：worker 状态里没有保活进程树单独的量");
  has(sa, "alpha_train_s1"); has(sa, "labmon 之外：jupyter"); has(sa, 'href="#/host/srv-a"', "worker 节链到主机页");
  ok(!/<b>worker<\/b>[\s\S]*?hold_until_data[\s\S]*?<\/section>/.test(sa.slice(0, sa.indexOf("<b>现在</b>"))), "worker 节的在跑不列占位任务");
  has(sa, "registry.local/lab/ubuntu:me", "开机参数");
  clean(sa, "自用主机抽屉");
  // 人员抽屉里的那一行与抽屉标题同一句池类型（fKind）：以前写成“自用 srv-a 专用”
  const pm = page(O, "#/fleet/people/ME"), r0 = pm.indexOf('class="drow" data-drw="#/fleet/srv/srv-a"'), row = pm.slice(r0, pm.indexOf("</div>", r0));
  ok(r0 >= 0, "人员抽屉里没有 srv-a 那一行"); has(row, "自用"); ok(!row.includes("专用"), "人员抽屉把自用主机写成专用：" + row);
  ok(run(O, "D.fleet.servers.concat(D.fleet.mine).map(fKind).join()") === "公用,公用,公用,公用,专用,公用,自用");
  // 掉线的自用主机：红点、提醒条有它（以前 fleetAlerts 只看组内服务器）；租期快到的也提醒
  const dn = run(O, `(() => { const m = D.fleet.mine[0], o = {...m}; Object.assign(m, {state: "down", down: "连不上", since: ${NOW - 600}, sample: null, lease_end: ${NOW + 3 * H}});
    const r = [fDot(m), fleetAlerts().filter(a => a.startsWith("ME（srv-a）")), fleetDue().map(x => x.id)]; Object.assign(m, o); return r; })()`);
  ok(dn[0] === "var(--bad)", "掉线的自用主机该是红点：" + dn[0]);
  ok(dn[1].some(a => a.includes("掉线")) && dn[1].some(a => a.includes("续租")), "提醒条该有自用主机的掉线与租期：" + JSON.stringify(dn[1]));
  ok(dn[2].includes("srv-a"), "租期快到的自用主机也进批量续租的缺省勾选");
  // 卡片：显存同一个口径（去掉保活），利用率取样本；worker 状态过期（没有样本）时写“—”，不拿旧数
  const fw = page(O, "#/fleet"), ca = fw.slice(fw.indexOf('id="fc-srv-a"'), fw.indexOf('id="fc-', fw.indexOf('id="fc-srv-a"') + 5));
  has(ca, "显存（去掉保活）"); ok(!fw.includes("显存（实验）"));
  ok(run(O, "fUtil({...D.fleet.mine[0], sample: null}).v") === null, "没有样本时利用率该是空");
  // 首页只列一次（审查 B1）：自用主机的掉线、租期快到由主机自己的告警（alertsOf）报，组内提醒不再重复；「告警」指标卡同一口径
  const hm = run(O, `(() => { const m = D.fleet.mine[0], h = D.hosts.find(e => e.id === "srv-a"), om = {...m}, oh = {...h};
    Object.assign(m, {state: "down", down: "连不上", since: ${NOW - 600}, lease_end: ${NOW + 3 * H}});
    Object.assign(h, {health: {...h.health, state: "down", since: ${NOW - 600}, why: "连不上"}, lease_end: ${NOW + 3 * H}});
    location.hash = "#/"; render(true); const v = document.getElementById("view").innerHTML;
    const al = v.slice(v.indexOf('<div class="alerts">'), v.indexOf("</div></div>", v.indexOf('<div class="alerts">')));
    const n = (allAlerts().length + fleetAlerts(false).length + projAlerts().length), kpi = v.match(/<div class="k">告警<\\/div><div class="v"[^>]*>(\\d+)/)[1];
    const r = [al, n, +kpi, fleetAlerts().filter(a => a.startsWith("ME（srv-a）")).length]; Object.assign(m, om); Object.assign(h, oh); return r; })()`);
  const rows = hm[0].split("<div>⚠").filter(a => a.includes("srv-a"));
  ok(rows.filter(a => a.includes("租期")).length === 1, "首页 srv-a 的租期该只列一条：" + JSON.stringify(rows));
  ok(rows.filter(a => a.includes("掉线")).length === 1, "首页 srv-a 的掉线该只列一条：" + JSON.stringify(rows));
  ok(hm[2] === hm[1], `「告警」指标卡多算了：${hm[2]} / ${hm[1]}`);
  ok(hm[3] === 2, "组内页、侧栏、抽屉的提醒照旧带自用主机：" + hm[3]);
  // 本机不在内网时 worker 节先写错误，不拿旧时钟写“在写状态 N 秒前”（审查 N1）
  const lo = run(O, `(() => { const m = D.fleet.mine[0], h = D.hosts.find(e => e.id === "srv-a"), om = {...m}, oh = {...h};
    Object.assign(h, {lan_off: true}); Object.assign(m, {error: "本机不在内网"}); const r = workerSec(m); Object.assign(m, om); Object.assign(h, oh); return r; })()`);
  has(lo, "本机不在内网"); ok(!lo.includes("在写状态"), "不在内网时还写在写状态：" + lo);
  has(run(O, "workerSec(D.fleet.mine[0])"), "在写状态");
  // 有任务时续租是红框、确认要勾「可以中断」，没勾时确认键禁用（审查 N3；renewBtns / goRenew 共用，抽屉里守着）
  const rn = run(O, `(() => { const m = D.fleet.mine[0], os = m.state; m.state = "job"; const b = srvBtns(m); POP = {k: "renew", id: m.id, ack: false};
    const pop = srvBtns(m); POP.ack = true; const ok2 = srvBtns(m); m.state = "keepalive"; POP = null; const lw = m.lw; m.lw = null; const ka = srvBtns(m); m.lw = lw; m.state = os;
    return [b, pop, ok2, ka]; })()`);
  has(rn[0], 'class="btn danger" data-fop="renewone"', "有任务时续租该是红框");
  has(rn[1], "确认可以中断"); ok(/data-fop="renew1"[^>]*disabled/.test(rn[1]), "没勾确认时「中断并续租」该禁用");
  ok(!/data-fop="renew1"[^>]*disabled/.test(rn[2]), "勾了确认后该能点");
  ok(!rn[3].includes('class="btn danger" data-fop="renewone"'), "只有保活（本地盘也没写多少）时续租不该是红框");
});
t("overview", "fleetAlerts / guardText / gdIcon", () => {
  const al = run(O, "fleetAlerts()"); ok(al.some(a => a.includes("PERSON3")), "srv-d 20 小时内到期该提醒：" + JSON.stringify(al)); ok(!leak(al.join("\n")));
  const g = run(O, "D.fleet.servers.map(guardText)"); has(g[1], `（${run(O, `tsHM(${NOW - H})`)} 开）`); has(g[0], "借来在用"); has(g[2], "没开成：ssh 失败"); has(g[3], "没开守护"); has(run(O, "guardText({...D.fleet.servers[3], guard: null})"), "已关机");
  const manual = run(O, `guardText({...D.fleet.servers[2], guard: {on: true, status: "fail", fail: {msg: "保活持续空转，请核对脚本"}}})`);
  ok(manual === "保活持续空转，请核对脚本", manual);
  ok(!leak(g.join("\n")), JSON.stringify(g));
  ok(!leak(run(O, "D.fleet.servers.map(gdIcon).join('')")));
});
t("overview", "人员抽屉：共享盘提醒线输入框，重画后填回还没保存的值", () => {
  let h = run(O, 'diskOwner("ME")'); has(h, 'id="dklim"'); has(h, 'value="100"');
  run(O, 'FDK.limit = "12"'); h = run(O, 'diskOwner("ME")'); has(h, 'value="12"');
  ok(run(O, 'FIN.dklim[0] === "K" && finOf(FIN.dklim) === FDK'), "dklim 的值没记在 FDK");
  run(O, "FDK = {}");
  has(run(O, 'diskOwner("NOBODY")'), "没有归到 NOBODY 的目录");
});
t("overview", "共享盘的两张表（#108）：长目录名折行、不把「统计于」挤出去；窄屏目录名独占一行、按人只留五列", () => {
  const long = "me/codex-remote-batch/snapshots/run_20261107_seed0_lr3e4_bs256_warmup2000_long";
  const h = run(O, 'diskOwner("ME")');
  has(h, '<table class="dku">'); has(h, `<code>${long.replaceAll("/", "/<wbr>")}</code>`, "目录名在 / 处可折行");
  has(tips(h), long + "\nstep_120000.ckpt", "悬停第一行是全名");
  has(tips(h), "me/archives\nbig.tar", "原来的最大几项照样在");
  const css = ovHtml.replace(/\s+/g, " ");
  has(css, ".dku code { overflow-wrap: anywhere; }"); has(css, ".dkp td:first-child b { overflow-wrap: anywhere; }");
  const narrow = css.slice(css.indexOf("@media (max-width: 600px)"));
  has(narrow, ".dku tr { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); }", "窄屏两行网格");
  has(narrow, ".dku th:first-child { display: none; }");
  const d = page(O, "#/fleet/disk"), hd = d.slice(d.indexOf('<table class="dkp">'));
  ok(hd.length > 20, "按人表没有 dkp");
  for (const c of ["7 天", "近 30 天", "文件数", "最近修改"]) has(hd, `hide-sm">${c}</th>`, "窄屏收起 " + c);
  for (const c of ["24 小时", "90 天没改", "统计于"]) has(hd, `<th class="r">${c}</th>`, "窄屏留着 " + c);
  ok(!/<td class="nw"><i class="dot"/.test(hd), "按人表第一列不该 nowrap：长目录名会把右边几列挤出去");
  // #179：只剩已删除单位、没有现存单位（t0 为空且 pending 为 0）的行不出现在「按人」表里，不出现「还没扫（0 个」
  ok(!hd.includes("EXPORT_GONE"), "只剩已删除单位的行不该出现在按人表里");
  ok(!hd.includes("还没扫（0 个"), "不该出现自相矛盾的「还没扫（0 个」");
  has(hd, "stray_dir", "仍有待扫目录的行照常显示");
  has(hd, "还没扫（3 个目录排着）", "pending > 0 时照常写还没扫");
  ok(run(O, 'diskLine("EXPORT_GONE")') === "", "人员卡不该把只剩已删除单位的行写成待扫描");
});
t("overview", "共享盘增长的关注档（#108）：7 天内占满或暴涨告警「暴涨」，30 天内「在涨」，不只靠颜色、不借告警红", () => {
  const G = 2 ** 30, free = run(O, "D.disk.df.free"), lv = x => run(O, `diskHeat(${JSON.stringify(x)})?.lv ?? null`);
  ok(lv({d24: free / 3, d7: 0}) === "hi", "3 天占满");
  ok(lv({d24: free / 20, d7: 0}) === "mid", "20 天占满");
  ok(lv({d24: 0, d7: 7 * free / 20}) === "mid", "近 7 天的速度也算");
  ok(lv({d24: free / 60, d7: free / 60}) === null, "60 天才占满不标");
  ok(lv({d24: -5 * G, d7: -5 * G}) === null, "在减少不标");
  ok(run(O, `diskHeat({d24: 0, d7: 0}, true)?.lv`) === "hi", "暴涨告警在就是高");
  run(O, "SAVED = D.disk.df.free; D.disk.df.free = 2 ** 39");                 // 剩 512 GiB：每天不到 1 GiB 的不算在涨
  ok(lv({d24: 0.9 * G, d7: 0}) === null, "每天不到 1 GiB 不标");
  run(O, "D.disk.df.free = SAVED");
  const h = run(O, `diskHeat({d24: ${free / 3}, d7: 0})`);
  has(h.why, "约 3 天占满剩余"); has(h.why, "照近 24 小时的速度");
  const d = page(O, "#/fleet/disk"), hd = d.slice(d.indexOf('<table class="dkp">'));
  const own = hd.slice(hd.indexOf('data-drw="#/fleet/people/OWNER"') - 80); ok(own.slice(0, 80).includes("heat-hi"), "OWNER 有暴涨告警：行标高");
  has(own, '⇈暴涨</span>', "带文字与箭头"); has(own, '<span class="heat-n hi">+6.5</span>', "24 小时数字同色加粗");
  ok(!hd.includes("color:var(--bad)\">+"), "增量不再用告警红");
  const people = page(O, "#/fleet/people");
  has(people, "⇈暴涨</span> · 共享盘", "人员卡把增长标记放在共享盘读数前");
  const css = ovHtml.replace(/\s+/g, " ");
  ok(/--heat: #[0-9a-f]{6}/.test(css) && !/--heat: var\(--(bad|warn|ok|accent)\)/.test(css), "关注档有自己的颜色");
});
t("overview", "时间线：里程碑在视野内画线、在视野外写「远处」；全部视图没有数据时范围照样有限", () => {
  O.location.hash = "#/"; run(O, "render(true)");
  const box = run(O, `(() => { const box = document.getElementById("tlbox"); return [tlSvg(box, [], {markers: [{label: "近碑", ts: D.generated + 3600}]}), tlSvg(box, [], {markers: [{label: "远碑", ts: D.generated + 30 * 86400}]})]; })()`);
  has(box[0], ">近碑</text>"); has(box[1], `>远碑 ${run(O, `tsMD(${NOW + 30 * 86400})`)} ›</text>`);
  run(O, 'TLH = null; ST.get = (k, d) => k === "ovtl" ? "all" : d; TL.v = null');
  ok(run(O, "TL.cfg.all()").every(x => x === null)); const r = run(O, "tlRange()"); ok(Number.isFinite(r.t0) && Number.isFinite(r.t1) && r.t1 > r.t0, JSON.stringify(r));
  run(O, "TLH = {hs: D.hosts, showHost: true}"); const r2 = run(O, "tlRange()"); ok(Number.isFinite(r2.t0) && r2.t1 > r2.t0);
  ok(run(O, "TL.cfg.all()").every(Number.isFinite));
  run(O, "drawTimeline()"); clean(el(O, "tlbox").innerHTML, "时间线"); has(el(O, "tlbox").innerHTML, "<svg");
  ok((el(O, "tlbox").innerHTML.match(/stroke-opacity="\.4"/g) || []).length === 1, "srv-a 有两张卡：srv-b 的卡上面该有一条主机分隔线");
  run(O, `ST.get =(k, d) => k === "cardOpen" ? {"rec:home": 1, "rec:host": 1} : k === "recTab" ? {home: RTAB, host: RTAB} : d`);
});
t("overview", "afterText / occItems / borrowBadge / hostShort / noWorkBadge", () => {
  ok(run(O, 'afterText([["proj-b", "x"], ["proj-a", "y"]], "proj-a")') === "proj-b 的 x、y");
  ok(run(O, "hostShort(D.hosts[1])") === "借 OWNER" && run(O, "hostShort(D.hosts[0])") === "srv-a");
  const b = run(O, "borrowBadge(D.hosts[1].borrow, D.generated)"); clean(b, "borrowBadge"); has(b, "OWNER");
  const nw = run(O, `noWorkBadge(${NOW - 86400})`); has(nw, run(O, "NO_WORK")); has(nw, run(O, `ts(${NOW - 86400})`));
  ok(run(O, "noWorkBadge(null)") === "");
});

/* ---------- 项目的「管理」抽屉（#93） ---------- */
// 只取抽屉（项目页的正文是 iframe，#view 里留着上一个路由的内容）
const drw = (C, hash) => { page(C, hash); return run(C, 'DKEY ? document.getElementById("drwp").innerHTML : ""'); };
t("manage", "路由：#/p/<id>/manage 是项目页加抽屉（同一页、换抽屉不记历史），id 里的 / 照样认；关掉回到项目页", () => {
  const r = run(O, 'route("#/p/a%2Fb/manage")'); ok(r.kind === "p" && r.id === "a/b" && r.drw.k === "proj" && r.drw.id === "a/b", JSON.stringify(r));
  ok(run(O, 'route("#/p/a%2Fb").drw') === undefined, "没有 /manage 的不开抽屉");
  ok(run(O, 'samePage(route("#/p/proj-a"), route("#/p/proj-a/manage"))'), "开抽屉该算同一页");
  page(O, "#/p/proj-a/manage"); ok(run(O, "DKEY") === "proj:proj-a", "抽屉没开：" + run(O, "DKEY"));
  run(O, "SAVED = history.replaceState; history.replaceState = (s, t, h) => { location.hash = h; }; closeDrw(); history.replaceState = SAVED");
  ok(O.location.hash === "#/p/proj-a", "关掉没回到项目页：" + O.location.hash);
  page(O, "#/p/proj-a"); ok(!run(O, "DKEY"), "回到项目页抽屉没收");
});
t("manage", "顶栏：项目页有「管理」（只在看板服务上），别的页没有", () => {
  has(page(O, "#/p/proj-a"), 'data-drw="#/p/proj-a/manage"', "项目页顶栏");
  ok(!page(O, "#/").includes("/manage"), "总览不该有「管理」");
  const F = mkCtx(ovSrc, OV, {hash: "#/p/proj-a", filename: "overview.js", protocol: "file:"});
  run(F, "render(true)");
  ok(!el(F, "top").innerHTML.includes("/manage"), "file:// 打开的离线页不该有「管理」");
});
t("manage", "主机与队列：每台主机一行（清单数按状态分），逐条折在「展开 N 条」里只读，项目页收起的不列；已归还的灰显、标没读到；没登记的单列“只显示”", () => {
  const b = drw(O, "#/p/proj-b/manage"), sec = b.slice(b.indexOf("主机与队列"), b.indexOf("<b>调度</b>"));
  const starts = [...sec.matchAll(/<div class="rg( rg-off)?">/g)].map(m => m.index);       // 每台主机一组
  const grp = h => { const k = sec.indexOf(`data-h="${h}"`), i = starts.filter(x => x < k).pop(); return k < 0 ? "" : sec.slice(i, starts.find(x => x > k)); };
  // srv-a：jobs/b.txt 车道 2 在跑（占位任务）、限定卡 0；jobs/bd.txt 空满 2 天已休眠，项目页已收起（quiet）：数进“跑完”，展开里不列并标含已休眠
  const a = grp("srv-a");
  has(a, 'href="#/host/srv-a"'); has(a, "2 条清单"); has(a, "1 在跑"); has(a, "1 跑完"); has(a, "展开 1 条"); has(a, "<b>车道 2</b>"); has(a, "限定卡 0");
  has(a, "另有 1 条已收起，不列（含 1 条已休眠）"); ok(!a.includes("jobs/bd.txt"), "项目页收起的清单不该列出");
  has(tips(a), "其中 1 条空满 2 天已自动休眠：worker 只检查清单文件是否变动，不读状态目录", "休眠登记提示（quiet 的 sleeping，#164）");
  ok(/<details class="dfold rg-q"><summary/.test(a), "逐条清单该折起来");
  // srv-b（借来的，短名“借 OWNER”）：jobs/b2.txt 在跑、jobs/b4.txt 刚登记（下一轮采集后显示）；停法 file:STOP
  const bb = grp("srv-b");
  has(bb, ">借 OWNER</a>"); has(bb, "下一轮采集后显示"); has(bb, "暂停时写停止文件 STOP"); has(bb, "1 在跑");
  ok(sec.indexOf('data-h="srv-b"') < sec.indexOf('data-h="srv-old2"'), "在用的该排在已归还的前面");
  // srv-old2 已归还、这轮没读到：灰显、写读的时刻；车道取项目快照（还有 3 个没跑 → 在等）；限定卡与登记时刻
  const o2 = grp("srv-old2");
  has(o2, 'class="rg rg-off"'); has(o2, "已归还 " + run(O, `ts(${NOW - 30 * H})`)); has(o2, "这轮没读到，用的是 " + run(O, `ts(${NOW - 600})`) + " 的");
  has(o2, "1 在等"); has(o2, "还有 3 个没跑"); has(o2, "暂停时发 SIGTERM"); has(o2, "限定卡 0,1"); has(o2, "2026-11-08 10:00 登记");
  has(grp("srv-old3"), "1 跑完");
  // monitor.toml 写着 srv-b 的 jobs/b3.txt、哪台都没登记：只显示；srv-gone 的登记读不到：不算没登记，另写一句
  has(sec, "只显示（monitor.toml 列着、没登记）1 条"); has(sec, "jobs/b3.txt"); ok(!sec.includes("jobs/b7.txt"), "读不到登记的车道不该进“只显示”");
  has(sec, "另有 1 条车道所在主机的登记读不到"); has(tips(sec), "srv-gone 车道 1");
  ok(!sec.includes("jobs/z.txt") && !sec.includes("jobs/q1.txt"), "别的项目的登记混进来了");
  has(sec, "labmon worker add &lt;主机&gt; ROOT JOBS --project proj-b", "加主机的命令提示");
  ok(!/data-reg="(mv|rm)" data-h="[^"]*" data-r=/.test(sec), "逐条的搬、移除该去掉了");
  clean(b, "proj-b 的抽屉");
  // proj-a：主机清单里已没有的 old-id 上的登记
  const pa = drw(O, "#/p/proj-a/manage"); has(pa, "主机清单里没有"); has(pa, "jobs/z.txt"); has(pa, "运行中 2 个"); has(pa, "还有 2 个没跑"); clean(pa, "proj-a 的抽屉");
});
t("manage", "主机与队列：对不上车道时在用主机按 worker 状态数（todo 里这条清单还会跑的、running）；没有登记数据、没有登记时各有说明", () => {
  const n = run(O, `regRow(D.regs[0], D.regs[0].queues[0], {id: "proj-a", lanes: []})`);
  has(n, "下一轮采集后显示"); has(n, "运行中 2 个");
  has(run(O, `regRow(D.regs[0], {root: "/data/me/none", jobs: "x.txt", project: "proj-b", empty: true}, {id: "proj-b", lanes: []})`), "空着");
  has(run(O, `regRow(D.regs[0], {root: "/data/me/none", jobs: "s.txt", project: "proj-b", empty: true, sleeping: true}, {id: "proj-b", lanes: []})`), "已休眠");
  ok(run(O, `regTally(D.regs[0], [{root: "/data/me/none", jobs: "x.txt", project: "proj-b", empty: true}], {id: "proj-b", lanes: []}).done`) === 1, "状态里 empty 的登记该算跑完");
  has(run(O, `regRow({host: "x", state: "returned"}, {root: "/r", jobs: "j.txt"}, {id: "p", lanes: []})`), "进度没读到");
  // 暂停、drain 的清单推演里没有（forecast 排除），todo 里照样列着：按 todo 数，不当成空着；todo 里也没有、又没标 empty 的说不出（Codex 审查 #105）
  run(O, `SAVEDT = D.hosts[0].status.todo; D.hosts[0].status.todo = [{project: "proj-b", root: "/data/me/proj-b/q2", queue: "jobs/b5.txt", jobs: ["x1", "x2", "x3"], held: "pause"}]`);
  const Q5 = `{root: "/data/me/proj-b/q2/", jobs: "jobs/b5.txt", project: "proj-b", empty: false}`, NQ = `{root: "/x", jobs: "jobs/none.txt", project: "proj-b", empty: false}`;
  has(run(O, `regRow(D.regs[0], ${Q5}, {id: "proj-b", lanes: []})`), "还有 3 个没跑");
  const ty = run(O, `regTally(D.regs[0], [${Q5}, ${NQ}], {id: "proj-b", lanes: []})`);
  ok(ty.wait === 1 && ty.unk === 1 && !ty.done, "todo 里还有的算在等、说不出的单列：" + JSON.stringify(ty));
  has(run(O, `tallyHtml(${JSON.stringify(ty)}, 2)`), "1 条没读到进度");
  has(run(O, `regRow(D.regs[0], ${NQ}, {id: "proj-b", lanes: []})`), "进度没读到");
  // 对上了车道、但项目探针没读到它（serve 给的 run、unfinished 是空值）：退回 worker 状态，按 todo 数（Codex 审查 #105）
  const LN = `{id: "proj-b", lanes: [{host: D.regs[0].host, root: "/data/me/proj-b/q2", queue: "jobs/b5.txt", label: "车道 9", run: null, unfinished: null}]}`;
  const mv = run(O, `regRow(D.regs[0], ${Q5}, ${LN})`);
  has(mv, "还有 3 个没跑"); has(mv, "车道 9");
  run(O, `SAVEDR = D.hosts[0].status.running; D.hosts[0].status.running = [{project: "proj-b", root: "/data/me/proj-b/q2", queue: "jobs/b5.txt", name: "x1"}]`);
  const mr = run(O, `regRow(D.regs[0], ${Q5}, ${LN})`);
  has(mr, "运行中 1 个"); has(mr, "还有 2 个没跑");
  run(O, "D.hosts[0].status.running = SAVEDR");
  run(O, "D.hosts[0].status.todo = SAVEDT");
  has(run(O, 'projRegSec({id: "nobody", lanes: []})'), "哪台主机上都没有登记这个项目的清单");
  const R = run(O, "JSON.stringify(D.regs)");
  has(run(O, 'delete D.regs; projRegSec(D.projects[0])'), "还没有登记数据"); run(O, `D.regs = ${R}`);
});
// 项目配置的一次输入（页面上 input / change 事件经 cfgInput 记进草稿）
const cfIn = (C, k, v, p = "proj-a", how = "input") => run(C, `cfgInput({dataset: {cf: ${JSON.stringify(k)}, p: ${JSON.stringify(p)}}, value: ${JSON.stringify(v)}}, ${JSON.stringify(how)})`);
const cfSec = h => h.slice(h.indexOf("<b>项目配置</b>"), h.indexOf("<b>归档这个项目</b>"));
t("manage", "项目配置（#133）：控件一直显示、从现值起（没有「修改」）；档位与范围取 cfg.spec；只读的三块；计划项的进度、自动归出来的、没归组的；没改时没有保存条", () => {
  run(O, "CFE = Object.create(null); CFMSG = null");
  const sc = cfSec(drw(O, "#/p/proj-a/manage"));
  ok(!sc.includes('data-cfb="edit"'), "不该再有「修改」");
  has(sc, 'data-cf="title" data-p="proj-a" value="Alpha 实验" maxlength="60"');
  has(sc, 'data-cf="interval" data-p="proj-a" value="120" min="30" max="3600" step="1"'); has(sc, 'data-cf="hide_done_hours" data-p="proj-a" value="24" min="1" max="720" step="any"');
  ok(new RegExp(`class="cf-chip on" data-cfb="set:interval:120" data-p="proj-a"[^>]*>${run(O, `cfDur("interval", 120)`)}<span class="dft">缺省</span>`).test(sc), "现值那档该亮、缺省那档标出");
  has(sc, 'data-cfb="set:interval:30"'); has(sc, 'data-cfb="set:hide_done_hours:168"'); has(sc, 'class="rhy"');
  // 取值范围、缺省取 cfg.spec（config.cfg_spec），页面不另抄一份
  run(O, "D.projects[0].cfg.spec.interval.lo = 45; D.projects[0].cfg.spec.interval.dft = 60");
  const s2 = cfSec(drw(O, "#/p/proj-a/manage")); has(s2, 'value="120" min="45" max="3600"'); ok(/data-cfb="set:interval:60"[^>]*>[^<]*<span class="dft">缺省/.test(s2), "缺省档没跟着 spec");
  run(O, "D.projects[0].cfg.spec.interval.lo = 30; D.projects[0].cfg.spec.interval.dft = 120");
  // 只读的三块：看板上的配置（登记目录里，能复制）、项目 id、输出目录；项目目录里的是工作副本、wait 每轮同步（#161）
  has(sc, 'class="idg"'); has(sc, "<code>proj-a</code>"); has(sc, "看板上的配置"); has(sc, 'data-copy="/Users/me/.local/state/labmon/projects/proj-a/monitor.toml"');
  has(sc, "/Users/me/.local/state/labmon/projects/proj-a/out"); has(sc, "项目目录里的 monitor.toml 是工作副本"); ok(!sc.includes("重启看板服务才生效"), "#161 后 id 与输出目录不再靠重启生效");
  // 计划项：写明的能改名、换色、排顺序，带这组的进度；自动归出来的名字与颜色只读、标「自动」，也能排顺序（#150）；没归组的另写
  has(sc, 'data-cf="g:0" data-p="proj-a" value="E1 主实验"'); has(sc, 'type="color" data-cf="gc:0" data-p="proj-a" value="#7F77DD"'); has(sc, "6 个 · 跑完 3 · ");
  has(sc, "失败 1"); has(sc, "在跑 1"); has(sc, "2 个 · 跑完 0"); has(sc, '<span class="gp-l">ext_</span>'); has(sc, ">自动</span>"); has(sc, "另有 2 个任务没归进任何组（名字里没有 _ 或 - 前缀）");
  has(sc, 'data-cfb="up:0" data-p="proj-a" class="cf-x" aria-label="上移" disabled', "第一项不能再上移"); has(sc, 'data-cfb="down:2" data-p="proj-a" class="cf-x" aria-label="下移" disabled');
  has(sc, 'data-cf="auto_groups" data-p="proj-a" checked');
  // 里程碑：日期框 + 可不填的时刻框 + 名字；告警：数字嵌在句子里，日志静默关着时置灰
  has(sc, 'type="date" data-cf="md:0" data-p="proj-a" value="2026-11-07"'); has(sc, 'type="time" data-cf="mt:0" data-p="proj-a" value=""'); has(sc, 'data-cf="mt:1" data-p="proj-a" value="12:00"');
  has(sc, 'data-cf="ml:1" data-p="proj-a" value="投稿截止"'); has(sc, 'class="mtl"');
  has(sc, 'data-cf="a:gpu_idle_util" data-p="proj-a" value="5"'); has(sc, "关着：不报"); ok(!/data-cf="logon"[^>]*checked/.test(sc), "log_silent_min 为 0 时开关该关着");
  has(sc, '<div class="cf-save" data-cfv="save"></div>', "没改动时保存条该空着"); ok(!sc.includes("看板服务启动时用 --interval"), "没定死间隔的不该提示");
  // 启动参数定了间隔的：那一行整排置灰、写明；日志静默开着
  const sb = cfSec(drw(O, "#/p/proj-b/manage"));
  has(sb, "看板服务启动时用 --interval 定了间隔"); has(sb, 'class="fr off" data-f="interval"'); has(sb, 'data-cf="interval" data-p="proj-b" value="300" min="30" max="3600" step="1" aria-label="采集间隔（秒）" disabled');
  ok(/data-cf="logon" data-p="proj-b" checked/.test(sb), "日志静默开着"); has(sb, 'data-cf="a:log_silent_min" data-p="proj-b" value="30"'); has(sb, 'data-cf="auto_groups" data-p="proj-b" >');
  has(sb, "还没有计划项");
  ok(/@media \(max-width: 600px\) \{[^}]*\.rg, \.fr \{ grid-template-columns: minmax\(0, 1fr\); \}/.test(ovHtml), "窄屏抽屉的行该上下排");
  // 窄屏按钮占满一行只给行上的按钮，弹窗里的「换过去」「取消」不撑满（不然挤出弹窗）；没有名字的行（里程碑、计划项、告警）只有一列，不挤进名字那列
  has(ovHtml, ".rg-b > .popw > .btn { width: 100%"); has(ovHtml, ".dz-r > .pop-l > .popw > .btn { width: 100%"); ok(!/\n {2}\.(rg-b|dz-r) \.btn \{/.test(ovHtml), "窄屏的按钮规则别管到弹窗里");
  for (const f of ["title", "markers", "groups", "al-gpu", "al-stale", "al-log"]) ok(new RegExp(`class="fr wide" data-f="${f}"`).test(sc), f + " 该占整行");
});
t("manage", "项目配置：“数据过期”那行写实际门槛（cfg.stale，与 labmon wait 同一个），填的值小于下限时说明不起作用、不再标“易误报”；草稿改采集间隔或这项时跟着变（#141）", () => {
  run(O, "CFE = Object.create(null); CFMSG = null");
  const al = id => { const h = cfSec(drw(O, `#/p/${id}/manage`)); return text(h.slice(h.indexOf('data-f="al-stale"'), h.indexOf('data-f="al-log"'))); };
  has(al("proj-a"), "实际按 7 分钟：至少等 3 次采集（每 2 分钟）加 1 分钟，填的 5 分钟不起作用", "采集 2 分钟、填 5：" + al("proj-a"));
  ok(!/误报|约 [\d.]+ 次采集/.test(al("proj-a")), "旧说法还在：" + al("proj-a"));
  has(al("proj-b"), "实际按 6 分钟：填的 5 分钟加 1 分钟", "--interval 定死时按 monitor.toml 的间隔（cfg.stale.iv）：");
  run(O, "D.projects[0].cfg.stale.s = 999");                        // 没改草稿时直接用后端算好的
  has(al("proj-a"), "实际按 17 分钟"); run(O, "D.projects[0].cfg.stale.s = 420");
  cfIn(O, "a:stale_min", "10"); has(al("proj-a"), "实际按 11 分钟：填的 10 分钟加 1 分钟");
  run(O, `cfgAct("set:interval:300", "proj-a")`); has(al("proj-a"), "实际按 16 分钟：至少等 3 次采集（每 5 分钟）加 1 分钟，填的 10 分钟不起作用");
  cfIn(O, "a:stale_min", "1", "proj-b"); has(al("proj-b"), "实际按 4 分钟：至少等 3 次采集（每 1 分钟）");
  run(O, "CFE = Object.create(null)");
});
t("manage", "项目配置（#133）：打字只记草稿，改了的行标出、「原 X ↺」单项还原，保存条写改了几项与对照；档位、上下移、换色、加删里程碑；只交改了的项", () => {
  run(O, "CFE = Object.create(null); CFMSG = null");
  cfIn(O, "title", " 新标题 ");
  let f = cfSec(drw(O, "#/p/proj-a/manage"));
  ok(/class="fr wide dirty" data-f="title"/.test(f), "改了的行没标出"); has(f, "改了 <b>1</b> 项：标题"); has(f, "保存到 monitor.toml"); has(f, "原 「Alpha 实验」 ↺");
  ok(!cfSec(drw(O, "#/p/proj-b/manage")).includes("改了 <b>"), "草稿按项目各存一份");
  run(O, `cfgAct("undo:title", "proj-a")`); f = cfSec(drw(O, "#/p/proj-a/manage"));
  has(f, 'data-cf="title" data-p="proj-a" value="Alpha 实验"'); ok(!run(O, '"proj-a" in CFE'), "还原后没改动了，草稿该丢掉");
  run(O, `cfgAct("set:interval:300", "proj-a")`); cfIn(O, "a:gpu_idle_min", "30"); cfIn(O, "g:1", "消融 B"); cfIn(O, "gc:0", "#112233", "proj-a", "change");
  cfIn(O, "md:0", "2026-11-08"); run(O, `cfgInput({dataset: {cf: "auto_groups", p: "proj-a"}, checked: false}, "change")`);
  run(O, `cfgAct("down:0", "proj-a"); cfgAct("mdel:1", "proj-a"); cfgAct("madd", "proj-a")`); cfIn(O, "md:1", "2026-12-01"); cfIn(O, "mt:1", "18:30"); cfIn(O, "ml:1", "答辩");
  f = cfSec(drw(O, "#/p/proj-a/manage"));
  ok(f.indexOf('value="消融 B"') < f.indexOf('value="E1 主实验"'), "下移后顺序没变"); has(f, 'data-cf="ml:1" data-p="proj-a" value="答辩"'); has(f, 'data-cf="mt:1" data-p="proj-a" value="18:30"');
  ok(/class="cf-chip on" data-cfb="set:interval:300"/.test(f), "点的那档没亮"); has(f, "改了 <b>5</b> 项");
  run(O, `cfgAct("diff", "proj-a")`); f = cfSec(drw(O, "#/p/proj-a/manage"));
  has(f, '<pre class="cf-diff">/Users/me/.local/state/labmon/projects/proj-a/monitor.toml'); has(f, "- interval = 120"); has(f, "+ interval = 300"); has(f, "[[groups]] id = &quot;E1&quot;"); has(f, "+ color = &quot;#112233&quot;"); has(f, "收起改动");
  run(O, `SF = []; fetch = (u, o) => { SF.push([u, JSON.parse(o.body)]); return new Promise(() => {}); }; cfgAct("save", "proj-a")`);
  ok(run(O, "JSON.stringify(SF)") === JSON.stringify([["project/config", {project: "proj-a", set: {interval: "300", auto_groups: false, alerts: {gpu_idle_min: "30"},
    groups: [{id: "abl", label: "消融 B"}, {id: "E1", label: "E1 主实验", color: "#112233"}], markers: [{date: "2026-11-08", label: "开题"}, {date: "2026-12-01 18:30", label: "答辩"}]}}]]), run(O, "JSON.stringify(SF)"));
  has(drw(O, "#/p/proj-a/manage"), "正在保存…", "提交中按钮置灰");      // 回执要等 fetch 的 promise（check.js 同步跑，不等），这里直接放
  run(O, `CFE = Object.create(null); CFMSG = "改了 proj-a 的 monitor.toml"`); has(drw(O, "#/p/proj-a/manage"), "改了 proj-a 的 monitor.toml"); run(O, "CFMSG = null");
  // 日志静默的开关：打开取原值或 30
  run(O, `cfgInput({dataset: {cf: "logon", p: "proj-a"}, checked: true}, "change")`); has(cfSec(drw(O, "#/p/proj-a/manage")), 'data-cf="a:log_silent_min" data-p="proj-a" value="30"');
  run(O, `SF = []; cfgAct("save", "proj-a")`); ok(JSON.stringify(run(O, "SF[0][1].set")) === JSON.stringify({alerts: {log_silent_min: "30"}}), JSON.stringify(run(O, "SF")));
  run(O, "CFE = Object.create(null)");
});
t("manage", "计划项拖动排顺序（#146）：只从把手起拖，拖着时其余行让位、占位跟着落点走，松手按占位改草稿顺序；Esc、拖出列表、行被换掉都取消；拖着时定时刷新不重画；列表滚了落点跟着算", () => {
  run(O, "CFE = Object.create(null); CFMSG = null");
  const sc = cfSec(drw(O, "#/p/proj-a/manage"));
  has(sc, '<div class="gp-list"><div class="gp-row" data-f="g:0"', "写明的计划项包在一个列表里"); has(sc, '<span class="gp-hd" data-sort'); ok(!sc.includes("draggable"), "原生 drag 该删掉");
  has(sc, "按住 ⋮⋮ 拖动或 ↑↓ 排顺序");
  has(ovHtml, ".gp-hd[data-sort] { cursor: grab; touch-action: none;", "触屏从把手拖时不滚页面");
  // 列表 3 行：高 30、间距 4（每行占 34），列表顶在 100 - SCR（SCR 是滚过的距离）
  run(O, `D.projects[0].cfg.groups.push({id: "x3", label: "第三组", color: "#123456"}); SCR = 0;
    SL = document.createElement("div"); SL.getBoundingClientRect = () => ({left: 0, right: 400, top: 100 - SCR, bottom: 198 - SCR, width: 400, height: 98});
    SR = [0, 1, 2].map(i => { const r = document.createElement("div"); r.dataset = {gi: String(i), p: "proj-a"}; r.matches = s => s === ".gp-row[data-gi]"; r.closest = s => s === ".gp-row[data-gi]" ? r : null;
      r.getBoundingClientRect = () => ({left: 0, right: 400, top: 100 - SCR + 34 * i, bottom: 130 - SCR + 34 * i, width: 400, height: 30});
      r.hd = {closest: s => s === ".gp-hd[data-sort]" ? r.hd : s === ".gp-row[data-gi]" ? r : null, setPointerCapture() {}, releasePointerCapture() {}};
      return r; });
    SL.append(...SR);
    order = () => cfgEdit(D.projects[0]).v.groups.filter(g => !g.auto).map(g => g.id).join(",");
    ph = () => SL.children.find(x => x.className === "sort-ph");
    tf = () => SR.map(r => r.style.transform || "").join("|");`);
  const P = (type, tgt, x, y) => fire(O, type, tgt, {clientX: x, clientY: y, pointerId: 1, button: 0});
  const row = i => run(O, `SR[${i}]`), hd = i => run(O, `SR[${i}].hd`), S = k => run(O, `SORT && SORT.${k}`);
  ok(run(O, "order()") === "E1,abl,x3", run(O, "order()"));
  P("pointerdown", row(0), 50, 115); ok(run(O, "SORT") === null, "从行上别处按下不该起拖（点名字改名）");
  P("pointerdown", hd(0), 50, 115); P("pointermove", hd(0), 51, 117);
  ok(run(O, "SORT && !SORT.on && !ph() && !editing()"), "挪不到 4 像素不算拖");
  P("pointermove", hd(0), 50, 140);
  ok(S("on") && run(O, "editing()"), "拖着时 editing() 该为真"); ok(run(O, 'SR[0].classList.contains("sort-lift") && SR[1].classList.contains("sort-shift")'), "被拖的行该抬起、其余行该能让位");
  ok(S("to") === 0 && run(O, "ph().style.top") === "0px" && run(O, "SR[0].style.transform") === "translateY(25px)", `还没过下一行的中线：${S("to")} ${run(O, "ph().style.top")} ${run(O, "SR[0].style.transform")}`);
  P("pointermove", hd(0), 50, 150);
  ok(S("to") === 1 && run(O, "ph().style.top") === "34px" && run(O, "tf()") === "translateY(35px)|translateY(-34px)|", `过了第二行中线：第二行上移让位、占位挪到第二格：${S("to")} ${run(O, "ph().style.top")} ${run(O, "tf()")}`);
  ok(run(O, "PENDING = false; render(false); PENDING") === true, "拖着时定时刷新该挡下（记 PENDING），不换掉拖着的行");
  P("pointermove", hd(0), 50, 190);
  ok(S("to") === 2 && run(O, "ph().style.top") === "68px" && run(O, "tf()") === "translateY(75px)|translateY(-34px)|translateY(-34px)", `拖到最后：${S("to")} ${run(O, "ph().style.top")} ${run(O, "tf()")}`);
  P("pointermove", hd(0), 50, 115);
  ok(S("to") === 0 && run(O, "ph().style.top") === "0px" && run(O, "tf()") === "translateY(0px)||", `拖回原处，让位的行回去：${run(O, "tf()")}`);
  P("pointermove", hd(0), 50, 150); ok(S("to") === 1);
  run(O, "SCR = 34"); fire(O, "scroll", run(O, "document.body"));          // 指针不动、只滚了列表（滚轮、触控板）：不等下一次 pointermove（Codex 审查 #148）
  ok(S("to") === 2 && run(O, "SR[0].style.transform") === "translateY(69px)", `列表往上滚了一行、指针不动：落点跟着往下一格 ${S("to")} ${run(O, "SR[0].style.transform")}`);
  run(O, "SCR = 0"); fire(O, "scroll", run(O, "document.body")); ok(S("to") === 1, "滚回来落点跟着回来：" + S("to"));
  P("pointermove", hd(0), 50, 190); P("pointerup", hd(0), 50, 190);
  ok(run(O, "order()") === "abl,x3,E1", "松手该落在占位处：" + run(O, "order()"));
  ok(run(O, "SORT === null && !ph() && !editing() && tf() === '||' && !SR[0].classList.contains('sort-lift') && !SR[1].classList.contains('sort-shift')"), "松手后样式与占位该清掉");
  has(cfSec(drw(O, "#/p/proj-a/manage")), "改了 <b>1</b> 项：计划项", "落下只改草稿、进保存条");
  // Esc 取消：回原位，抽屉不跟着关
  P("pointerdown", hd(2), 50, 183); P("pointermove", hd(2), 50, 110); ok(S("to") === 0, "从第三行拖到最上：" + S("to"));
  fire(O, "keydown", run(O, "document.body"), {key: "Escape"});
  ok(run(O, "SORT === null && !ph() && tf() === '||'") && run(O, "order()") === "abl,x3,E1", "Esc 该取消拖动：" + run(O, "order()"));
  ok(run(O, "DKEY") === "proj:proj-a", "Esc 只取消拖动，不连带关抽屉");
  // 拖出列表：被拖的行变淡、占位收起，松手不改
  P("pointerdown", hd(0), 50, 115); P("pointermove", hd(0), 50, 190); ok(S("to") === 2);
  P("pointermove", hd(0), 520, 190);
  ok(S("out") && S("to") === 0 && run(O, "ph().hidden") && run(O, "SR[0].classList.contains('sort-out')") && run(O, "tf()").endsWith("||"), "拖出列表：落点回原位、占位收起");
  P("pointerup", hd(0), 520, 190); ok(run(O, "SORT === null") && run(O, "order()") === "abl,x3,E1", "拖出列表松手不该改顺序：" + run(O, "order()"));
  // 拖着时行被别的重画换掉了：取消
  P("pointerdown", hd(0), 50, 115); P("pointermove", hd(0), 50, 190); run(O, "SR[0].isConnected = false"); P("pointermove", hd(0), 50, 191);
  ok(run(O, "SORT === null") && run(O, "order()") === "abl,x3,E1", "行没了该取消");
  run(O, "D.projects[0].cfg.groups.pop(); CFE = Object.create(null)");
});
t("manage", "自动归出的计划项也能排顺序（#150）：和写明的同一个列表、有把手与 ↑↓；挪到写明的之间的（及排到别的自动组前面的）保存时写成 [[groups]]，末尾照原先后的留给自动归组；「看改动」列出新块", () => {
  run(O, "CFE = Object.create(null); CFMSG = null");
  let f = cfSec(drw(O, "#/p/proj-a/manage"));
  ok(/<div class="gp-row auto" data-f="g:2" data-gi="2" data-p="proj-a"><span class="gp-hd" data-sort/.test(f), "自动组该在同一个列表里、带把手");
  has(f, 'data-cfb="up:2" data-p="proj-a" class="cf-x" aria-label="上移"'); ok(!f.includes("保存后写明") && !f.includes('data-cf="g:2"'), "没挪时不写、名字只读");
  run(O, `cfgAct("up:2", "proj-a")`); f = cfSec(drw(O, "#/p/proj-a/manage"));
  ok(/<div class="gp-row auto new" data-f="g:1"[^]*?ext_[^]*?保存后写明/.test(f), "挪到写明的之间该标「保存后写明」"); has(f, "改了 <b>1</b> 项：计划项");
  run(O, `cfgAct("diff", "proj-a")`); f = cfSec(drw(O, "#/p/proj-a/manage"));
  for (const x of ["+ [[groups]]", "+ id = &quot;ext_&quot;", "+ label = &quot;ext_&quot;", "+ prefix = &quot;ext_&quot;", "+ color = &quot;#BA7517&quot;", "[[groups]] 顺序改成 <span class=\"a\">E1 主实验、ext_、消融</span>"]) has(f, x);
  run(O, `SF = []; fetch = (u, o) => { SF.push([u, JSON.parse(o.body)]); return new Promise(() => {}); }; cfgAct("save", "proj-a")`);
  ok(JSON.stringify(run(O, "SF[0][1].set")) === JSON.stringify({groups: [{id: "E1", label: "E1 主实验"}, {id: "ext_", label: "ext_", color: "#BA7517", auto: true}, {id: "abl", label: "消融"}]}), JSON.stringify(run(O, "SF")));
  // 挪回去：没改动，草稿丢掉
  run(O, `Object.assign(CFE["proj-a"], {busy: false, show: false}); cfgAct("down:1", "proj-a")`); cfSec(drw(O, "#/p/proj-a/manage")); ok(!run(O, '"proj-a" in CFE'), "挪回原处该没改动");
  // 两个自动组：后一个排到前一个前面，只写它（接在写明的末尾，不算换顺序）；末尾照原先后的 ext_ 留给自动归组
  run(O, `D.projects[0].groups = [...D.projects[0].groups, {id: "zz_", label: "zz_*", color: "#123123", auto: true, total: 1, done: 0, failed: 0, running: 1, queued: 0}]`);
  run(O, `cfgAct("up:3", "proj-a"); cfgAct("diff", "proj-a")`); f = cfSec(drw(O, "#/p/proj-a/manage"));
  ok(/data-f="g:2"[^]*?保存后写明[^]*?data-f="g:3"[^]*?>自动<\/span>/.test(f), "zz_ 该写、ext_ 不写"); has(f, "+ id = &quot;zz_&quot;"); ok(!f.includes("顺序改成") && !f.includes("&quot;ext_&quot;"), "只接在末尾不算换顺序");
  run(O, `SF = []; cfgAct("save", "proj-a")`);
  ok(JSON.stringify(run(O, "SF[0][1].set.groups")) === JSON.stringify([{id: "E1", label: "E1 主实验"}, {id: "abl", label: "消融"}, {id: "zz_", label: "zz_", color: "#123123", auto: true}]), JSON.stringify(run(O, "SF")));
  // 改着时新归出来的自动组：不进草稿，只读列在后面
  run(O, `CFE["proj-a"].busy = false; D.projects[0].groups = [...D.projects[0].groups, {id: "new_", label: "new_*", color: "#321321", auto: true, total: 1, done: 0, failed: 0, running: 0, queued: 1}]`);
  f = cfSec(drw(O, "#/p/proj-a/manage")); ok(/<div class="gp-row auto"><span class="gp-hd"><\/span>[^]*?new_\*/.test(f), "改着时新归出来的该只读列在后面");
  // 保存后快照里已是写明的（cfg.groups 有了、项目快照还标 auto）：不重复列
  run(O, `CFE = Object.create(null); D.projects[0].cfg.groups.push({id: "zz_", label: "zz_", color: "#123123"})`);
  ok(run(O, `cfgDraft(D.projects[0].cfg, D.projects[0]).groups.map(g => g.id + (g.auto ? "*" : "")).join(",")`) === "E1,abl,zz_,ext_*,new_*", run(O, `cfgDraft(D.projects[0].cfg, D.projects[0]).groups.map(g => g.id).join(",")`));
  run(O, `D.projects[0].cfg.groups.pop(); D.projects[0].groups = D.projects[0].groups.filter(g => !["zz_", "new_"].includes(g.id)); CFE = Object.create(null)`);
});
t("manage", "项目配置（#133）：当场校验改了的项（说法与范围同 config），出错的行标红、不能保存；服务端按 field 报的标到那一行；撤销；保存只交改过的项（Codex 审查 #128）", () => {
  run(O, "CFE = Object.create(null); CFMSG = null");
  cfIn(O, "interval", "5"); let f = cfSec(drw(O, "#/p/proj-a/manage"));
  has(f, 'class="fr dirty err" data-f="interval"'); has(f, "采集间隔要在 30–3600 秒之间"); ok(/<button class="btn pri" data-cfb="save" data-p="proj-a" disabled/.test(f), "有错时保存该置灰");
  run(O, `SF = []; fetch = (u, o) => { SF.push([u, JSON.parse(o.body)]); return new Promise(() => {}); }; cfgAct("save", "proj-a")`); ok(run(O, "SF.length") === 0, "有错还发了请求");
  cfIn(O, "interval", "90.5"); has(cfSec(drw(O, "#/p/proj-a/manage")), "采集间隔要填整数");
  cfIn(O, "g:0", ""); has(cfSec(drw(O, "#/p/proj-a/manage")), "名字不能空，最多 40 个字");
  run(O, `cfgAct("reset", "proj-a")`); f = cfSec(drw(O, "#/p/proj-a/manage")); ok(!run(O, '"proj-a" in CFE'), "撤销后草稿该丢掉"); has(f, 'data-cf="interval" data-p="proj-a" value="120"');
  // 服务端校验不过：带 field 的标到那一行，不带的写在保存条里；草稿留着
  cfIn(O, "refresh", "20"); run(O, `Object.assign(CFE["proj-a"], {err: "页面刷新改不了", field: "refresh"})`);
  f = cfSec(drw(O, "#/p/proj-a/manage")); has(f, 'class="fr dirty err" data-f="refresh"'); has(f, '<span class="cf-err">页面刷新改不了</span>'); has(f, 'data-cf="refresh" data-p="proj-a" value="20"');
  run(O, `Object.assign(CFE["proj-a"], {err: "monitor.toml 写不进去", field: null})`); has(cfSec(drw(O, "#/p/proj-a/manage")), '<div class="rn-err">monitor.toml 写不进去</div>');
  run(O, "CFE = Object.create(null)");
  // Codex 审查 #128：改着的时候快照里的配置被别处改了，保存只交草稿相对第一次输入时改过的项，不把别处的改动改回去
  cfIn(O, "title", "只改标题"); run(O, "D.projects[0].cfg = {...D.projects[0].cfg, interval: 60, groups: [...D.projects[0].cfg.groups].reverse()}");
  run(O, `SF = []; cfgAct("save", "proj-a")`);
  ok(run(O, "JSON.stringify(SF)") === JSON.stringify([["project/config", {project: "proj-a", set: {title: "只改标题"}}]]), run(O, "JSON.stringify(SF)"));
  run(O, "CFE = Object.create(null); D.projects[0].cfg = {...D.projects[0].cfg, interval: 120, groups: [...D.projects[0].cfg.groups].reverse()}");
  // Codex 审查 #136：文件里不带年份的里程碑（load 经 parse_time 认得）按 cfg 带的 ts 填日期框，改别的里程碑不被它挡住，提交时补上年份
  const M0 = run(O, "JSON.stringify(D.projects[0].cfg.markers)");
  run(O, `D.projects[0].cfg.markers[0] = {date: "11-07", label: "开题", ts: new Date(2026, 10, 7, 23, 59, 59).getTime() / 1000}`);
  f = cfSec(drw(O, "#/p/proj-a/manage")); has(f, 'type="date" data-cf="md:0" data-p="proj-a" value="2026-11-07"'); has(f, 'data-cf="mt:0" data-p="proj-a" value=""'); has(f, "<em>11-07</em>");
  cfIn(O, "ml:1", "截稿"); f = cfSec(drw(O, "#/p/proj-a/manage")); ok(!f.includes("要选日期"), "不带年份的里程碑被当成没选日期");
  run(O, `SF = []; cfgAct("save", "proj-a")`);
  ok(JSON.stringify(run(O, "SF[0][1].set")) === JSON.stringify({markers: [{date: "2026-11-07", label: "开题"}, {date: "2026-11-13 12:00", label: "截稿"}]}), JSON.stringify(run(O, "SF")));
  run(O, `CFE = Object.create(null); D.projects[0].cfg.markers = ${M0}`);
  // Codex 审查 #136：项目 id 叫 constructor、toString、__proto__ 的，草稿不读到原型上的东西，抽屉照样画、照样能改
  for (const id of ["constructor", "toString", "__proto__"]) {
    const h = run(O, `D.projects.push({...D.projects[1], id: ${JSON.stringify(id)}}); projCfgSec(D.projects.at(-1))`); has(h, `data-cf="title" data-p="${id}"`); has(h, '<div class="cf-save" data-cfv="save"></div>');
    cfIn(O, "title", "改一下", id); has(run(O, "projCfgSec(D.projects.at(-1))"), "改了 <b>1</b> 项：标题", id);
    run(O, `cfgAct("reset", ${JSON.stringify(id)}); D.projects.pop()`); ok(!Object.keys(run(O, "CFE")).length, id + " 的草稿没丢掉");
  }
  // file:// 打开的离线页：同一版面、控件全置灰，点了也不记草稿
  const F = mkCtx(ovSrc, OV, {hash: "#/p/proj-a/manage", filename: "overview.js", protocol: "file:"});
  run(F, "render(true)"); const fh = cfSec(run(F, 'document.getElementById("drwp").innerHTML'));
  const ins = [...fh.matchAll(/<input [^>]*data-cf="[^"]*"[^>]*>/g)].map(m => m[0]); ok(ins.length > 10 && ins.every(x => / disabled>$/.test(x)), "离线页的控件该全置灰：" + ins.find(x => !/ disabled>$/.test(x)));
  has(fh, "在项目目录的 monitor.toml 里改，labmon wait 或 labmon project push 推上来后下一轮自动重读"); ok(!fh.includes("保存到 monitor.toml"));
  run(F, `cfgAct("set:interval:300", "proj-a"); cfgInput({dataset: {cf: "title", p: "proj-a"}, value: "x"}, "input")`); ok(!Object.keys(run(F, "CFE")).length, "离线页记了草稿");
});
ta("overview", "保活守护时间：草稿有效且有变化才可点保存或按回车，不合法时当场写范围", async () => {
  const C = accCtx(OV, ADMIN_ME, "#/fleet/guard");
  run(C, `D.fleet.interval = 635; render(true); fopAct("guard-edit");
    POSTS = []; fleetPost = async (kind, body) => { POSTS.push([kind, body]); throw new Error("测试保存失败"); };
    SAVE = {disabled: true, toggleAttribute(k, on) { if (k === "disabled") this.disabled = on; }};
    HINT = {hidden: true, textContent: ""};
    oldQuery = document.querySelector.bind(document);
    document.querySelector = q => q === "[data-gdsave]" ? SAVE : q === "[data-gdhint]" ? HINT : oldQuery(q);`);
  const enter = (isComposing = false) => fire(C, "keydown", {id: "gdidle"}, {key: "Enter", isComposing});
  const input = (id, value, disabled) => {
    fire(C, "input", {id, value});
    ok(run(C, "SAVE.disabled") === disabled, id + " 输入 " + value + " 的保存状态不符");
  };
  for (const v of ["", "3", "30.5", "1441", "060"]) { input("gdidle", v, true); enter(); }
  ok(run(C, "POSTS.length") === 0, "无效或等价草稿被回车提交");
  ok(run(C, "HINT.hidden && document.getElementById('gdidle').attrs['aria-invalid'] === 'false'"), "等价草稿也出了范围提示");
  // 不合法：范围提示当场出现（只列不合法的那项），回车把焦点留在那个框里（#26）
  input("gdidle", "1441", true);
  ok(run(C, "!HINT.hidden && HINT.textContent === '空闲多久开填 10 到 1440 的整数分钟'"), "不合法时没有范围提示：" + run(C, "HINT.textContent"));
  ok(run(C, "document.getElementById('gdidle').attrs['aria-invalid'] === 'true'"), "不合法的框没标 aria-invalid");
  run(C, `FOCUSED = null; document.getElementById("gdidle").focus = () => { FOCUSED = "gdidle"; }`);
  enter();
  ok(run(C, "FOCUSED === 'gdidle' && POSTS.length === 0"), "回车没把焦点留在不合法的框");
  has(run(C, "guardDrawer()"), "空闲多久开填 10 到 1440 的整数分钟</div>", "重画后范围提示没了");
  has(run(C, "guardDrawer()"), 'min="10" max="1440"');
  input("gdidle", "30", false); input("gdwatch", "61", true); input("gdwatch", "5", false);
  focusText(C, 'id: "gdidle",');
  run(C, "fops().guard.idle_min = 30; render(false)");
  ok(run(C, "SAVE.disabled && BL === 0"), "刷新后保存状态未更新或打断了输入");
  input("gdwatch", "0", false); enter(true);
  ok(run(C, "POSTS.length") === 0, "输入法选词触发了保存");
  enter();
  ok(run(C, "POSTS.length === 1 && POP.busy && POSTS[0][0] === 'guard' && POSTS[0][1].idle_min === 30 && POSTS[0][1].watch_min === 0"), "有效草稿没有按整数提交");
  enter(); await Promise.resolve();
  ok(run(C, "POSTS.length === 1 && !POP.busy && POP.err === '测试保存失败'"), "重复提交或丢了失败提示");
  has(run(C, "guardDrawer()"), 'value="0"', "保存失败后丢草稿");
});
t("overview", "Safari 点按钮、勾选框不挪焦点：common.js 的点击处理先让输入框失焦，点完当场重画（#129、#131）", () => {
  // 项目配置：焦点在表单的输入框里点下移、保存，抽屉不经整页渲染就重画
  const C = ovCtx(OV, RR, "#/p/proj-a/manage"); let f;
  drw(C, "#/p/proj-a/manage"); run(C, `CFE = Object.create(null); cfgInput({dataset: {cf: "title", p: "proj-a"}, value: "Alpha 2"}, "input"); render(false)`);
  focusText(C, `dataset: {cf: "title", p: "proj-a"},`); clickOn(C, "[data-cfb]", {cfb: "down:0", p: "proj-a"});
  ok(run(C, "BL === 1 && document.activeElement === document.body && PENDING === false"), "点上移下移后焦点还在输入框里、重画被挡住");
  f = run(C, 'document.getElementById("drwp").innerHTML'); ok(f.indexOf('value="消融 B"') < f.indexOf('value="E1 主实验"'), "下移后抽屉没重画");
  run(C, `fetch = () => new Promise(() => {})`);
  focusText(C, `dataset: {cf: "title", p: "proj-a"},`); clickOn(C, "[data-cfb]", {cfb: "save", p: "proj-a"});
  ok(run(C, "BL === 1 && PENDING === false"), "保存后焦点还在输入框里、重画被挡住");
  has(run(C, 'document.getElementById("drwp").innerHTML'), "正在保存…", "保存时抽屉没重画");
  // 暂停的弹窗：焦点在到期的输入框里点「暂停」，弹窗当场换成「正在保存…」
  run(C, `CFE = Object.create(null); location.hash = "#/"; render(true); schedAct("p", "proj-a"); whenTap({wq: "clr"}); render(false)`); has(el(C, "view").innerHTML, `id="schp"`, "暂停弹窗");
  focusText(C, `id: "schp",`); clickOn(C, "[data-sch]", {sch: "pgo", p: "proj-a"});
  ok(run(C, "BL === 1 && PENDING === false"), "点暂停的确认后焦点还在输入框里、重画被挡住"); has(el(C, "view").innerHTML, "正在保存…", "点确认后弹窗没重画");
  // 勾选框（保活守护的开关）：点它的 click 先于 change，change 里的 render(false) 不被挡
  run(C, `POP = null; location.hash = "#/fleet/guard"; render(true)`);
  focusText(C, `id: "gdidle",`);
  const cb = {dataset: {gd: "srv-d"}, type: "checkbox", checked: true, closest(q) { return /input/.test(q) ? this : null; }};
  fire(C, "click", cb); ok(run(C, "BL === 1"), "点勾选框没让输入框失焦"); fire(C, "change", cb); ok(run(C, "PENDING === false"), "勾选框的 change 重画被挡住");
  // 点的就是能打字的控件（焦点所在的那个、另一个输入框、指向输入框的 label）：不动，浏览器自己给焦点
  focusText(C); fire(C, "click", run(C, "document.activeElement")); ok(run(C, "BL === 0"), "点输入框自己也失焦了");
  fire(C, "click", {type: "number", closest(q) { return /input/.test(q) ? this : null; }}); ok(run(C, "BL === 0"), "点另一个输入框时先失焦了");
  const lab = {type: "", closest(q) { return q === "label" ? lab : null; }, control: {type: "date", closest(q) { return /input/.test(q) ? this : null; }}};
  fire(C, "click", lab); ok(run(C, "BL === 0"), "点时刻控件的 label 时先失焦了");
  lab.control = {type: "checkbox", closest(q) { return /input/.test(q) ? this : null; }};
  fire(C, "click", lab); ok(run(C, "BL === 1"), "点勾选框的 label 没让输入框失焦");
  run(C, "document.activeElement = document.body");
});
t("manage", "调度（可改，与调度卡同一套按钮与同一个「已用 / 应得」条 shareBar）；没有配置、调度数据时各有说明", () => {
  const a = drw(O, "#/p/proj-a/manage"), sa = a.slice(a.indexOf("<b>调度</b>"), a.indexOf("<b>项目配置</b>"));
  has(sa, "×1"); has(sa, "优先中（到 " + run(O, `tsNear(${NOW + 6 * H})`)); has(tips(sa), "所有主机（含之后加入的）"); ok(!sa.includes("总览的调度卡</a>"), "不该再让去总览改");
  has(sa, '<div class="flags"><span class="flag wcap">', "权重胶囊与「优先」「暂停」并列（#143）");
  has(sa, 'data-sch="wd" data-p="proj-a"', "权重 −"); has(sa, 'data-sch="wu" data-p="proj-a"', "权重 +"); ok(!sa.includes('data-sch="w"'), "权重不该再是弹窗按钮"); has(sa, 'data-sch="uf" data-p="proj-a"', "项目级优先：撤销优先"); has(sa, 'data-sch="p" data-p="proj-a"', "暂停按钮");
  ok(!sa.includes('data-p="proj-b"'), "只该有这一个项目的按钮");
  const bar = run(O, "shareBar(D.sched.projects[0])"); has(bar, 'class="due"'); has(sa, bar, "抽屉的份额条"); has(sa, "已用 / 应得（竖线）· 此刻占 1 张卡 · 排队 2 个");
  has(page(O, "#/"), bar, "调度卡的份额条该是同一个"); page(O, "#/p/proj-a/manage");
  // 点权重 +：不出弹窗，当场 POST /sched/weight，写回期间胶囊显示新值、这个项目的 −、+ 禁用；回执写在调度节
  run(O, `POP = null; SF = []; fetch = (u, o) => { SF.push([u, JSON.parse(o.body)]); return new Promise(() => {}); }; schedAct("wu", "proj-a")`);
  ok(run(O, "POP") === null, "点权重不该再开弹窗");
  ok(run(O, "JSON.stringify(SF)") === JSON.stringify([["sched/weight", {project: "proj-a", weight: 1.5}]]), run(O, "JSON.stringify(SF)"));
  const pend = drw(O, "#/p/proj-a/manage"); has(pend, "×1.5", "写回期间显示新值"); ok(/data-sch="wu" data-p="proj-a" aria-label="权重调高" disabled/.test(pend), "写回期间 + 没禁用");
  run(O, `SWB.clear(); SMSG = "改好了"`); has(drw(O, "#/p/proj-a/manage"), "改好了"); run(O, "SMSG = null");
  const b = drw(O, "#/p/proj-b/manage"); has(b, "暂停中</span>（只在 "); has(tips(b), "labmon resume proj-b --host srv-a", "单独下的暂停悬停写撤销命令");
  has(b, "×2");
  has(drw(O, "#/p/没有的项目/manage"), "看板服务里没有这个项目");
  const F = mkCtx(ovSrc, OV, {hash: "#/p/proj-a/manage", filename: "overview.js", protocol: "file:"});
  run(F, "render(true)"); const fh = run(F, 'document.getElementById("drwp").innerHTML');
  ok(!fh.includes("data-sch"), "file:// 打开的离线页不该有调度按钮"); ok(/>权重<\/span><b class="num">×1<\/b><\/span>/.test(fh), "离线页的权重胶囊只读、没有 −、+");
  const P = run(O, "JSON.stringify(D.projects[0].cfg)");
  has(run(O, "delete D.projects[0].cfg; projCfgSec(D.projects[0])"), "还没有配置摘要"); run(O, `D.projects[0].cfg = ${P}`);
  const S = run(O, "JSON.stringify(D.sched)");
  has(run(O, "D.sched = null; projSchedSec(D.projects[0])"), "还没有调度数据"); run(O, `D.sched = ${S}`);
});
t("manage", "恶意快照：抽屉里的登记、车道、配置都转义", () => {
  const E = ovCtx(OVE, RRE), h = drw(E, "#/p/evil/manage");
  clean(h, "evil 的抽屉"); has(h, "/r/" + BADESC); has(h, "/x/" + BADESC); has(h, "加" + BADESC); has(h, "暂停时写停止文件 " + BADESC); has(h, "组" + BADESC); has(h, "g" + BADESC); has(h, "自" + BADESC); has(h, "题" + BADESC);
  ok(!raw(tips(h)), "悬停提示没转义");
});
/* ---------- 按主机搬、移除登记（#95，#121 改成按主机）与接上原来的项目 ---------- */
t("reg", "抽屉每台主机一行（#133）：「换到其他主机」（只有一台能去时写它的名字）、「不用这台」；这项目在这台有任务在跑时置灰；悬停不写“登记”“整台”；主机页「队列」只读；file:// 打开的没有按钮", () => {
  const b = drw(O, "#/p/proj-b/manage"), sec = b.slice(b.indexOf("主机与队列"), b.indexOf("<b>调度</b>"));
  has(b, 'data-reg="mv" data-h="srv-old2" data-p="proj-b"'); has(b, 'data-reg="rm" data-h="srv-old3" data-p="proj-b"'); has(b, "换到其他主机"); has(b, ">换到 借 OWNER</button>"); has(b, ">不用这台</button>");
  ok(!/整台搬|从这台移除/.test(b), "旧按钮名还在"); ok(!/登记|整台/.test(tips(sec.replace(/<details[\s\S]*?<\/details>/g, ""))), "按钮与主机行的悬停不该写内部词：" + tips(sec));
  ok(/data-reg="rm" data-h="srv-a" data-p="proj-b" disabled/.test(b), "srv-a 上 proj-b 有任务在跑：「不用这台」该置灰"); has(tips(b), "有 1 个任务在跑：先暂停或等跑完");
  has(sec, 'class="rg-lg"'); has(sec, 'class="bar thin rg-bar"');
  ok(!/data-reg="rm" data-h="srv-old3" data-p="proj-b" disabled/.test(b), "没在跑的不该置灰");
  page(O, "#/host/srv-a"); run(O, `ST.get = (k, d) => k === "cardOpen" ? {"rec:host": 1} : k === "recTab" ? {host: "queues"} : d; render(true)`);
  const q = el(O, "view").innerHTML, qs = q.slice(q.indexOf("jobs/q1.txt"));
  has(qs, "运行中 2 个"); ok(!qs.includes(`data-reg="mv"`) && !qs.includes(`data-reg="rm"`), "主机页「队列」该只读"); has(qs, "在项目页的「管理」里");
  run(O, `ST.get = (k, d) => k === "cardOpen" ? {"rec:home": 1, "rec:host": 1} : k === "recTab" ? {home: RTAB, host: RTAB} : d`);
  const F = mkCtx(ovSrc, OV, {hash: "#/p/proj-b/manage", filename: "overview.js", protocol: "file:"});
  run(F, "render(true)"); ok(!run(F, 'document.getElementById("drwp").innerHTML').includes("data-reg"), "file:// 打开的离线页不该有搬、移除");
});
t("reg", "换到别的主机：能去的是同一 worker 目录的别的在用主机，借来的只给别的项目用的置灰；几台能去时弹窗里单选（写空几张卡、租期）；卡号不带、在跑的跑完再走；提交这台这项目的全部清单", () => {
  const to = run(O, `regTo(D.regs[0], "proj-a")`);           // srv-a 上的 proj-a：srv-b 借来只给 proj-b
  ok(to.length === 1 && to[0].id === "srv-b" && to[0].no, JSON.stringify(to));
  const tb = run(O, `regTo(D.regs.find(x => x.host === "srv-old2"), "proj-b")`);
  ok(tb.map(t => t.id).join() === "srv-a,srv-b" && !tb.some(t => t.no), "已归还主机上 proj-b 的登记该能搬到 srv-a、srv-b：" + JSON.stringify(tb));
  ok(run(O, `regTo({...D.regs[0], home: "/别的盘"}, "proj-a").length`) === 0, "worker 目录不同的不该列");
  run(O, `POP = null; regAct("mv", {h: "srv-old2", p: "proj-b"})`);
  const p = drw(O, "#/p/proj-b/manage"), pop = p.slice(p.indexOf('role="dialog"'));
  has(pop, 'type="radio" name="regto" data-regto="srv-a" checked'); has(pop, 'data-regto="srv-b"'); has(pop, "张卡"); ok(!pop.includes("<select"), "该是单选，不是下拉");
  has(pop, "1 条原来限定了 srv-old2（借 X） 的卡号，换过去不限"); has(pop, "把 <b>Beta 实验</b> 在 <b>srv-old2（借 X）</b> 上的 1 条清单改到哪台跑："); has(pop, "实验会话会收到一条消息"); has(pop, "换过去");
  const pin = pop.slice(0, pop.indexOf("pop-b")); ok(!/登记|整台/.test(text(pin)), "确认弹窗不该写内部词：" + text(pin));
  ok(run(O, "POP.to") === "srv-a", "缺省该选第一台能去的：" + run(O, "POP.to"));
  fire(O, "change", {dataset: {regto: "srv-b"}, classList: {contains: () => false}}); ok(run(O, "POP.to") === "srv-b", "点单选没换目标");
  has(drw(O, "#/p/proj-b/manage"), "没跑的到 <b>借 OWNER</b> 接着跑");
  run(O, `SF = []; fetch = (u, o) => { SF.push([u, JSON.parse(o.body)]); return new Promise(() => {}); }`);
  run(O, `POP = null; regAct("mv", {h: "srv-a", p: "proj-b"}); POP.to = "srv-b"; regAct("mvgo", {h: "srv-a", p: "proj-b"})`);
  const sent = run(O, "SF[0]");
  ok(sent[0] === "reg/move" && sent[1].to === "srv-b" && sent[1].items.map(i => i.jobs).join() === "jobs/b.txt,jobs/bd.txt" && sent[1].items.every(i => i.host === "srv-a" && i.project === "proj-b"),
    "整台搬该带这台这项目的全部登记（收起的也搬）：" + JSON.stringify(sent));
  run(O, `POP = null; SF = []; regAct("rm", {h: "srv-old3", p: "proj-b"})`);
  const rp = drw(O, "#/p/proj-b/manage"); has(rp, "经在用主机代办"); has(rp, "不再跑 <b>Beta 实验</b> 的 1 条清单"); ok(!/登记|整台/.test(text(rp.slice(rp.indexOf(`role="dialog"`), rp.indexOf("pop-b")))), "确认弹窗不该写内部词");
  run(O, `regAct("rmgo", {h: "srv-old3", p: "proj-b"})`);
  ok(JSON.stringify(run(O, "SF[0]")) === JSON.stringify(["reg/rm", {host: "srv-old3", home: "/data/me/labmon", project: "proj-b", items: [{root: "/data/me/proj-b/q8", jobs: "jobs/b8.txt"}]}]),
    "移除该成批发这台这项目的登记：" + JSON.stringify(run(O, "SF")));
  run(O, "POP = null");
});
t("reg", "接上原来的项目：这台没有登记时出现，上一台排第一、别的折叠，按项目分组，有没跑完的默认勾上，借来的只能勾给它用的项目", () => {
  const SV = run(O, "JSON.stringify([0, 1].map(i => [D.regs[i].queues, D.hosts[i].status.queues]))");
  ok(!run(O, 'hostPage("srv-a")').includes("接上原来的项目"), "有登记时不该出");
  run(O, "D.regs[0].queues = []; D.hosts[0].status.queues = []; ADOPT = null");
  const h = run(O, 'hostPage("srv-a")'), card = h.slice(h.indexOf("接上原来的项目"), h.indexOf("搬到这台") + 40);
  has(card, "上一台自用主机 <b>srv-old3</b>"); ok(card.indexOf("srv-old3") < card.indexOf("别的已归还主机上还有 2 条"), "上一台该排第一、别的折叠");
  has(card, "jobs/b9.txt"); has(card, "jobs/z.txt"); has(card, "Beta 实验");
  const sel = run(O, "ADOPT.sel"), id = (hh, r, j) => `${hh}\n${r}\n${j}`;
  ok(sel[id("srv-old2", "/data/me/proj-b/q9", "jobs/b9.txt")] === true, "还有没跑完的该默认勾上：" + JSON.stringify(sel));
  ok(sel[id("srv-old3", "/data/me/proj-b/q8", "jobs/b8.txt")] === false, "空着的不该勾");
  ok(sel[id("old-id", "/data/me/proj-a/z", "jobs/z.txt")] === false, "进度说不出的不该勾");
  has(card, "搬到这台（1 条）"); clean(card, "接上卡");
  has(run(O, 'adoptHint("srv-a")'), "上一台 srv-old3 上登记着 1 条队列"); ok(run(O, 'adoptHint("srv-b")') === "", "有登记的不该提示");
  // 借来的主机（只给 proj-b）：proj-a 的那条置灰、不算进去
  run(O, "D.regs[1].queues = []; D.hosts[1].status.queues = []; ADOPT = null");
  const hb = run(O, 'hostPage("srv-b")'); has(hb, "已归还的主机 <b>srv-old2（借 X）</b>"); has(tips(hb), "只给 proj-b 用"); ok(/data-adopt="old-id[^"]*"[^>]*disabled/.test(hb), "别的项目该置灰");
  run(O, `${SV}.forEach(([q, s], i) => { D.regs[i].queues = q; D.hosts[i].status.queues = s; }); ADOPT = null`);
  const F = mkCtx(ovSrc, OV, {hash: "#/", filename: "overview.js", protocol: "file:"});
  run(F, "D.regs[0].queues = []; D.hosts[0].status.queues = []"); ok(!run(F, 'hostPage("srv-a")').includes("接上原来的项目"), "file:// 打开的不该有接上卡");
});
t("reg", "恶意快照：换到、不用这台的弹窗与接上卡都转义", () => {
  const E = ovCtx(OVE, RRE);
  run(E, `regAct("mv", {h: "srv-a", p: "evil"})`);
  const a = run(E, `projRegSec(D.projects.find(p => p.id === "evil"))`); clean(a, "换到的弹窗"); ok(!raw(tips(a)), "悬停没转义"); has(a, 'class="pop');
  run(E, `POP = null; regAct("rm", {h: D.regs.find(y => y.host.startsWith("坏")).host, p: D.regs.find(y => y.host.startsWith("坏")).queues[0].project})`);
  const x = run(E, `(() => { const r = D.regs.find(y => y.host.startsWith("坏")); return regHostBtns(r, r.queues, {id: r.queues[0].project, lanes: []}); })()`); clean(x, "不用这台的弹窗"); has(x, "不再跑 <b>");
  run(E, "POP = null; D.regs[0].queues = []; D.hosts[0].status.queues = []; D.regs.at(-1).home = D.regs[0].home; ADOPT = null");
  const c = run(E, 'hostPage("srv-a")'); clean(c, "接上卡"); has(c, "坏" + BADESC);
});
t("reg", "给项目用（#173）：无登记自用主机显示（多项目进抽屉、单项目走浮窗），借来或有登记的不显示，普通用户按权限过滤，恶意快照转义", () => {
  const S = ovCtx(OV, RR);
  ok(!run(S, 'hostPage("srv-a")').includes("给项目用"), "有登记时不该出");
  run(S, "D.regs[0].queues = []; D.hosts[0].status.queues = []");
  const ha = run(S, 'hostPage("srv-a")');
  has(ha, "给项目用"); has(ha, 'data-drw="#/host/srv-a/assign"'); clean(ha, "多项目给项目用卡片");
  const dh = page(S, "#/host/srv-a/assign");
  has(dh, 'class="dft"'); has(dh, 'data-assignto="proj-a"'); has(dh, 'data-assignto="proj-b"'); clean(dh, "给项目用抽屉");
  // 直接借来的组内服务器不显示；换人换给自己的自用主机（borrow.swap）显示
  run(S, "D.regs[1].queues = []; D.hosts[1].status.queues = []");
  ok(!run(S, 'hostPage("srv-b")').includes("给项目用"), "借来的组内服务器不该出");
  run(S, "D.hosts[1].borrow.swap = true");
  has(run(S, 'hostPage("srv-b")'), "给项目用");
  // 普通用户（只有 1 个项目 proj-a）：有别人登记不显示；无登记时用短确认浮窗（data-assign="open"）；无主机权限时不显示
  const U = mkCtx(ovSrc, OV, {hash: "#/host/srv-a", filename: "overview.js", me: {...ADMIN_ME, name: "amy", role: "user", admin: false, ops: false, write: true, own: ["proj-a"], hosts: ["srv-a"]}});
  run(U, "D.regs[0].queues = []");
  ok(!run(U, 'hostPage("srv-a")').includes("给项目用"), "主机上有别人登记时不该误判为空");
  run(U, "D.hosts[0].status.queues = []");
  has(run(U, 'hostPage("srv-a")'), 'data-assign="open"');
  run(U, 'ME = {...ME, hosts: []}');
  ok(!run(U, 'hostPage("srv-a")').includes("给项目用"), "无主机权限时不该出");
  // file:// 离线文件不显示
  const F = mkCtx(ovSrc, OV, {hash: "#/host/srv-a", filename: "overview.js", protocol: "file:"});
  run(F, "D.regs[0].queues = []; D.hosts[0].status.queues = []");
  ok(!run(F, 'hostPage("srv-a")').includes("给项目用"), "file:// 不该出");
  // 恶意快照转义（抽屉与单项目浮窗）
  const E = ovCtx(OVE, RRE);
  run(E, 'D.regs[0].queues = []; D.hosts[0].status.queues = []');
  clean(page(E, "#/host/srv-a/assign"), "恶意快照给项目用抽屉");
  run(E, 'D.projects = D.projects.slice(0, 1); assignAct("open", {h: "srv-a"})');
  clean(run(E, 'hostPage("srv-a")'), "恶意快照单项目给项目用浮窗");
});
ta("reg", "给项目用（#173）：多项目抽屉与单项目浮窗提交 POST host/assign，成功显示回执并关闭，失败报错", async () => {
  const S = ovCtx(OV, RR, "#/host/srv-a"), sent = [];
  run(S, "D.regs[0].queues = []; D.hosts[0].status.queues = []");
  S.fetch = async (u, o) => {
    const b = JSON.parse(o.body); sent.push([u, b]);
    return b.project === "proj-b" ? {ok: true, status: 200, json: async () => ({msg: "已通知 proj-b 的实验会话使用 srv-a"})}
      : {ok: false, status: 409, json: async () => ({error: "项目已归档"})};
  };
  // 多项目抽屉：默认选中 proj-a，失败时在抽屉底部报错并保持打开
  page(S, "#/host/srv-a/assign");
  await run(S, 'assignAct("dgo", {h: "srv-a"})');
  has(run(S, 'assignDrawer("srv-a")'), "项目已归档");
  ok(run(S, 'APICK?.host === "srv-a"'), "失败后抽屉选择状态应保留");
  // 切换选 proj-b 提交成功：关闭抽屉并回到主机页显示回执
  run(S, 'APICK.to = "proj-b"');
  await run(S, 'assignAct("dgo", {h: "srv-a"})');
  ok(sent.length === 2 && sent[1][0] === "host/assign" && sent[1][1].host === "srv-a" && sent[1][1].project === "proj-b", "请求不对：" + JSON.stringify(sent));
  ok(S.location.hash === "#/host/srv-a" && !run(S, "APICK"), "成功后应关闭抽屉回到主机页");
  has(run(S, 'hostPage("srv-a")'), "已通知 proj-b 的实验会话使用 srv-a");
  // 单项目浮窗提交成功
  const U = mkCtx(ovSrc, OV, {hash: "#/host/srv-a", filename: "overview.js", me: {...ADMIN_ME, name: "amy", role: "user", admin: false, ops: false, write: true, own: ["proj-b"], hosts: ["srv-a"]}});
  run(U, "D.regs[0].queues = []; D.hosts[0].status.queues = []");
  U.fetch = S.fetch;
  run(U, 'assignAct("open", {h: "srv-a"})');
  await run(U, 'assignAct("go", {h: "srv-a"})');
  ok(!run(U, "POP"), "单项目浮窗成功后应关闭");
  has(run(U, 'hostPage("srv-a")'), "已通知 proj-b 的实验会话使用 srv-a");
});
t("manage", "缺省快照（没有 regs、项目没有 cfg）：抽屉照样画、写明还没有数据", () => {
  const S = ovCtx(OVS, RR), h = drw(S, "#/p/proj-a/manage");
  clean(h, "缺省快照的抽屉"); has(h, "还没有登记数据"); has(h, "还没有配置摘要"); has(h, "还没有调度数据");
});

/* ---------- 归档与恢复（#97） ---------- */
t("archive", "管理抽屉最下面的危险区（#133）：红框、一句话、「归档项目」；能归档的给确认弹窗（这一轮读到几条登记、删什么留什么），不能的置灰写 archive_block 的原因；离线页、没数据时各有说明", () => {
  const a = drw(O, "#/p/proj-a/manage"), sa = a.slice(a.indexOf("<b>归档这个项目</b>"));
  ok(a.indexOf("<b>项目配置</b>") < a.indexOf("<b>归档这个项目</b>"), "「归档」该在最后"); has(a, '<section class="card dz">'); has(sa, 'class="btn danger" data-pj="a"'); has(sa, "归档项目");
  has(sa, "现在不能归档：还有实验：2 个任务在跑，2 个在等，清单里还有 4 个没跑完"); ok(/data-pj="a" data-p="proj-a" disabled/.test(sa), "有实验时按钮该置灰");
  has(sa, "数据与配置都留着，能恢复"); clean(a, "proj-a 的归档节");
  const b = drw(O, "#/p/proj-b/manage"), sb = b.slice(b.indexOf("<b>归档这个项目</b>"));
  ok(/data-pj="a" data-p="proj-b" >/.test(sb), "能归档时按钮不该置灰：" + sb.slice(0, 600)); ok(!sb.includes("现在不能归档"));
  run(O, 'pjAct("a", "proj-b")');
  const bp = drw(O, "#/p/proj-b/manage");
  has(bp, "<b>归档 Beta 实验</b>"); has(bp, 'data-pj="ago" data-p="proj-b"'); has(bp, "这一轮读到 4 台主机上有它的 6 条登记"); has(bp, "不删：看板上的配置与输出目录");
  run(O, "POP = null");
  const F = mkCtx(ovSrc, OV, {hash: "#/p/proj-b/manage", filename: "overview.js", protocol: "file:"});
  run(F, "render(true)"); const fh = run(F, 'document.getElementById("drwp").innerHTML');
  ok(!fh.includes("data-pj"), "file:// 打开的离线页不该有归档按钮"); has(fh, "在本机的看板服务上归档");
  has(drw(ovCtx(OVS, RR), "#/p/proj-a/manage"), "还不知道能不能归档");
});
t("archive", "侧栏「已归档 N 个」与已归档的抽屉：路由、列出（移除的登记折起、没动的）、恢复弹窗、关掉回到总览；归档了的项目的管理抽屉指过去", () => {
  page(O, "#/");
  const sd = el(O, "side").innerHTML; has(sd, "已归档 2 个"); has(sd, 'href="#/archived"'); has(tips(sd) + run(O, "JSON.stringify(navModel(route()).secs[1].items.at(-1))"), "Gamma 旧实验、proj-older");
  const r = run(O, 'route("#/archived")'); ok(r.kind === "home" && r.drw.k === "arch", JSON.stringify(r));
  const h = drw(O, "#/archived");
  has(h, "已归档的项目"); has(h, "<b>Gamma 旧实验</b>"); has(h, "<b>proj-older</b>"); has(h, run(O, `ts(${NOW - 3 * 86400})`));
  has(h, "<summary class=\"sm\">2 条</summary>"); has(h, "<code>/data/me/gamma/q/<b>jobs/g.txt</b></code>"); has(h, "srv-x（登记没读到）");
  ok(h.indexOf("Gamma") < h.indexOf("proj-older"), "按快照的顺序（新的在前）"); clean(h, "已归档的抽屉");
  run(O, 'pjAct("r", "proj-old")');
  const hp = drw(O, "#/archived"); has(hp, "<b>恢复 Gamma 旧实验</b>"); has(hp, 'data-pj="rgo" data-p="proj-old"'); has(hp, "labmon worker add … --project proj-old");
  run(O, "POP = null; PJMSG = '恢复 proj-old：看板接着采集它'"); has(drw(O, "#/archived"), '<div class="rn-state">恢复 proj-old：看板接着采集它</div>'); run(O, "PJMSG = null");
  run(O, "SAVED = history.replaceState; history.replaceState = (s, t, h) => { location.hash = h; }; closeDrw(); history.replaceState = SAVED");
  ok(O.location.hash === "#/", "关掉没回到总览：" + O.location.hash);
  has(drw(O, "#/p/proj-old/manage"), "已经归档了");
  page(O, "#/");
  const S = ovCtx(OVS, RR); page(S, "#/"); ok(!el(S, "side").innerHTML.includes("已归档"), "没有归档的项目时不该有这一行");
  has(drw(S, "#/archived"), "没有归档的项目");
});
ta("overview", "批量续租：验证码回来前抽屉关了或换了一份勾选，旧回执作废（Codex 审查 #20）", async () => {
  const C = accCtx(OV, ADMIN_ME, "#/fleet/renew/srv-d");
  run(C, `render(true); OPS = {...fops(), session: {logged_in: false}}; RPICK.ack.add("srv-d");
    fleetPost = async k => { if (k === "captcha") { RPICK = {key: DH.renew("srv-e"), sel: new Set(["srv-e"]), ack: new Set()}; return {code: "c1", tok: "t1"}; } return {}; }`);
  await run(C, 'startOp("renew", rpBody(RPICK), "renew")');
  ok(run(C, "FCAP") === null, "抽屉换了一份勾选，旧的验证码回执还是记进了 FCAP");
  run(C, 'fleetPost = async k => k === "captcha" ? {code: "c2", tok: "t2"} : {}');
  await run(C, 'startOp("renew", rpBody(RPICK), "renew")');
  ok(run(C, 'FCAP?.next.body.ids.join()') === "srv-e", "同一份勾选的验证码该记下");
  // 验证码已出来、不关抽屉直接换地址（换了一份勾选）：旧验证码作废，底部回到「续租所选」（Codex 审查 #20）
  const h = run(C, 'RPICK = {key: DH.renew("srv-d"), sel: new Set(["srv-d"]), ack: new Set(["srv-d"])}; FCAP = {code: "c3", tok: "t3", input: "", t: Date.now(), next: {act: "renew", body: rpBody(RPICK)}, pop: "renew"}; renewDrawer("srv-e")');
  ok(run(C, "FCAP") === null && h.includes('data-fop="renewgo"') && !h.includes("fcapin"), "换了抽屉地址，旧的验证码还留着");
  // 从续租抽屉切到别的抽屉（服务器抽屉）再关：离开续租那一刻就作废，在途的回执也不再记下（Codex 审查 #20）
  drw(C, "#/fleet/renew/srv-d");
  run(C, 'RPICK.ack.add("srv-d"); FCAP = {code: "c4", tok: "t4", input: "", t: Date.now(), next: {act: "renew", body: rpBody(RPICK)}, pop: "renew"}');
  drw(C, "#/fleet/srv/srv-e");
  ok(run(C, "RPICK") === null && run(C, "FCAP") === null, "切到服务器抽屉后，续租的勾选与验证码还留着");
  drw(C, "#/fleet/renew/srv-d");
  run(C, 'RPICK.ack.add("srv-d"); fleetPost = async k => { if (k === "captcha") { location.hash = "#/fleet/srv/srv-e"; render(true); return {code: "c5", tok: "t5"}; } return {}; }');
  await run(C, 'startOp("renew", rpBody(RPICK), "renew")');
  drw(C, "#/fleet");
  ok(run(C, "FCAP") === null && run(C, "RPICK") === null, "离开续租抽屉后回来的验证码回执还是记进了 FCAP");
  ok(!drw(C, "#/fleet/renew/srv-d").includes("fcapin"), "重新打开续租抽屉恢复了旧的验证码");
});
ta("archive", "永久删除要求管理员和当前归档批次的确认", async () => {
  const A = ovCtx(OV, RR, "#/archived"), U = ovCtx(OV, RR, "#/archived");
  run(U, 'ME = {...ME, admin: false, scopes: ["project", "read"], own: ["proj-old"]}');
  has(drw(U, "#/archived"), 'data-pj="r" data-p="proj-old"');
  ok(!drw(U, "#/archived").includes("永久删除"), "普通用户的归档抽屉不应有删除入口");
  has(drw(A, "#/archived"), 'data-pj="d" data-p="proj-old"');
  run(A, 'poll = async () => {}; fetch = async (path, opts) => { globalThis.RESULT = [path, JSON.parse(opts.body)]; return {ok: true, json: async () => ({msg: "已删除"})}; }');
  await run(A, 'pjAct("dgo", "proj-old")');
  ok(!A.RESULT, "未打开浮窗不能提交");
  await run(A, 'pjAct("d", "proj-old")');
  const h = drw(A, "#/archived");
  has(h, "<b>永久删除 Gamma 旧实验</b>");
  has(h, "共享实验文件、用量和审计记录会保留");
  await run(A, 'pjAct("dgo", "proj-old")');
  ok(A.RESULT[0] === "project/delete" && A.RESULT[1].revision === "demo-archive-100");
  const rest = drw(A, "#/archived");
  has(rest, "proj-older"); has(rest, "已删除"); ok(!rest.includes("Gamma 旧实验"));
});
t("archive", "恶意快照：已归档的抽屉、归档节、侧栏都转义", () => {
  const E = ovCtx(OVE, RRE), h = drw(E, "#/archived");
  clean(h, "已归档的抽屉"); has(h, "档" + BADESC); has(h, "没" + BADESC); has(h, "/r/" + BADESC);
  const m = drw(E, "#/p/evil/manage"); clean(m, "evil 的归档节"); has(m, "因" + BADESC);
  page(E, "#/"); clean(el(E, "side").innerHTML, "侧栏"); ok(!raw(tips(el(E, "side").innerHTML)), "侧栏悬停没转义");
});

/* ---------- 工作台：恶意字符串 ---------- */
t("escape", "恶意快照：各路由与记录卡都转义", () => {
  const E = ovCtx(OVE, RRE);
  for (const r of ROUTES) { const h = page(E, r); ok(!raw(h), r + " 没转义"); }
  for (const tab of TABS) { E.RTAB = tab; for (const r of ["#/", "#/host/srv-a", "#/host/srv-b"]) ok(!raw(page(E, r)), `${r} ${tab} 没转义`); }
  E.RTAB = "done";
  for (const [r, what] of [["#/", "总览"], ["#/host/srv-a", "主机页"], ["#/fleet", "组内"], ["#/fleet/disk", "共享盘"], ["#/fleet/srv/srv-d", "服务器抽屉"], ["#/fleet/srv/srv-a", "自用主机抽屉"], ["#/fleet/people/NEWBIE", "人员抽屉"], ["#/log", "记录页"], ["#/fleet/guard", "保活守护"], ["#/fleet/renew", "批量续租抽屉"]])
    has(page(E, r), BADESC, what + " 没把恶意串转义后写出来：");
  { const tp = tips(page(E, "#/")); has(tp, "挪" + BADESC, "挪法建议悬停"); }
  E.RTAB = "log"; has(page(E, "#/host/srv-a"), BADESC, "worker 日志");
  E.RTAB = "ovr"; has(page(E, "#/host/srv-a"), "proj-b", "临时指令");
  run(E, 'ST.get = (k, d) => k === "open" ? d : k === "cardOpen" ? {} : d; OPEN.add(jobKey({h: D.hosts[0], ...D.hosts[0].status.running[0]}))');
  const h = page(E, "#/host/srv-a"); has(h, `<pre class="tail">行${BADESC}`, "展开的日志末尾"); ok(!raw(h));
  ok(!raw(run(E, "schedCard()")) && run(E, "schedCard()").includes(BADESC));
  ok(!raw(run(E, "diskOwner('人' + BAD)".replace("BAD", JSON.stringify(BAD)))), "diskOwner");
  const tl = run(E, 'TLH = {hs: D.hosts, showHost: true}; drawTimeline(); document.getElementById("tlbox").innerHTML'); ok(!raw(tl), "时间线");
  const w = run(E, `(() => { const h = D.hosts[0]; const ws = h.status.waiting.map(w => ({...w, h})); return waitingTable(ws, true); })()`); ok(!raw(w), "等待表"); has(w, "因" + BADESC, "排位提示里的原因");
  // 本地盘的路径（#149）：主机页选项卡、抽屉、续租与关机的确认、借用归还的确认（上面把 ST.get 换了，记录卡收着：另起一份）
  const E2 = ovCtx(OVE, RRE); E2.RTAB = "lw"; has(page(E2, "#/host/srv-a"), "/<wbr>root/<wbr>" + BADESC.replaceAll("/", "/<wbr>"), "本地盘选项卡");
  has(page(E2, "#/fleet/srv/srv-d"), "/<wbr>root/<wbr>" + BADESC.replaceAll("/", "/<wbr>") + "（本层）", "抽屉的本地盘");
  for (const [js, what] of [['POP = {k: "renew", id: "srv-d", ack: false}; renewAsk(srvById("srv-d"))', "续租确认"], ['POP = {k: "off", id: "srv-d", ack: false}; offAsk(srvById("srv-d"))', "关机确认"],
    ['POP = {k: "bret", id: "srv-b"}; borrowBtns(srvById("srv-b"))', "归还确认"], ['POP = {k: "renew", id: "srv-a", ack: false}; renewAsk(srvById("srv-a"))', "自用主机续租确认"]]) {
    const h = run(E2, js); ok(!raw(h), what + " 没转义"); has(h, BADESC, what + " 没写出路径：");
  }
});

/* ---------- 工作台：字段大量缺省 ---------- */
let S;
t("sparse", "缺省快照装得起来", () => { S = ovCtx(OVS, RR); has(el(S, "view").innerHTML, "运行 / 等待"); });
for (const r of ["#/", "#/host/srv-c", "#/host/srv-old", "#/host/srv-lan", "#/host/srv-bad", "#/log"])
  t("sparse", `路由 ${r} 渲染不报错、不漏 NaN/undefined`, () => clean(page(S, r), r));
t("sparse", "总览：不在内网、组内与共享盘读不了的错误都写出来；没有调度卡", () => {
  const h = page(S, "#/"); has(h, "组内服务器数据读不了：boom"); has(h, "共享盘数据读不了：boom"); has(h, "没有排队的任务"); ok(!h.includes("按份额轮流"));
  has(h, "暂时没实验"); has(h, "到期");
});
// overview.collect 读不了组内数据时给的兜底是 {error, servers: [], mine: []}：fleetPage 只写原因，抽屉不开（以前取 F.share.vram 报错、写 NaN）
t("sparse", "组内服务器页在 fleet 兜底（只有 error）时渲染：写出原因，共享盘照看，抽屉不开", () => {
  for (const r of ["#/fleet", "#/fleet/people", "#/fleet/disk", "#/fleet/srv/x", "#/fleet/people/x", "#/fleet/guard"]) clean(page(S, r), r);
  has(page(S, "#/fleet"), "这一轮组内服务器的数据没读出来"); ok(!run(S, "DKEY"), "兜底时不该开抽屉");
});

/* ---------- 项目页 ---------- */
function pjCtx(snap, ev) {
  const P = mkCtx(pgSrc, snap, {filename: "page.js"});
  P.EVD = ev; run(P, "EV = EVD; render()");
  return P;
}
const app = P => el(P, "app").innerHTML;
let P;
t("project", "正常快照渲染不报错、不漏 NaN/undefined，各块都在", () => {
  P = pjCtx(PJ, EVN); const h = app(P); clean(h, "项目页");
  for (const s of ["全部完成", "车道 1", "车道 2", "借 OWNER", "任务明细（12）", "alpha_*", "评估", "投稿截止", "<svg"]) has(h, s);
  has(h, "车道 2 还有 2 个任务不会在这台放行");                          // collect 的 borrow_cut 告警照常列在告警栏
  // 告警给谁看（#142）：红框与条数只算给用户的（borrow_cut 两边都要，列给用户的那句）；失败、空转、挪法建议只给实验会话，
  // 收在红框下面默认折叠的「给实验会话的提示」里，不算告警数
  const box = h.slice(h.indexOf('<div class="alerts">'), h.indexOf("</div></div>", h.indexOf('<div class="alerts">')));
  has(box, "由实验会话换到别的在用主机", "红框里是给用户的那句（late_say）"); ok(!box.includes("labmon worker add"), "红框里不该有给会话的命令：" + box);
  for (const s of ["alpha_probe_s9", "GPU 利用率已 15 分钟", run(P, "esc(D.rebalance.text)")]) ok(!box.includes(s), "只给实验会话的进了红框：" + s);
  has(h, "⚠ 1 条告警", "告警数不算只给实验会话的");
  const ag = h.slice(h.indexOf('<details class="agal'), h.indexOf("</details>", h.indexOf('<details class="agal')));
  has(ag, "给实验会话的提示 3 条"); ok(!/<details class="agal[^>]*\bopen\b/.test(ag), "给实验会话的提示该默认折叠");
  for (const s of ["alpha_probe_s9.failed", "GPU 利用率已 15 分钟", run(P, "esc(D.rebalance.text)")]) has(ag, s, "折叠里该有：" + s);
  ok(!ag.includes("不会在这台放行"), "两边都要的不该再进折叠");
  run(P, 'SS.set("agal", true); render()'); ok(/<details class="agal[^>]*\bopen\b/.test(app(P)), "展开过的重画后照旧展开");
  run(P, 'SS.set("agal", false); render()');
  has(h, run(P, "rebalMark(D.rebalance)"), "全部完成旁的图标");
  ok((h.match(/⇄/g) || []).length === 1, "图标只一个");
  has(tips(h), run(P, "esc(\"可以挪：\" + D.rebalance.text)"), "悬停写那句");
});
t("project", "会话的 wait 断了（#141）：wait_gone 只给用户，进红框、算告警数", () => {
  const msg = "实验会话 11-10 09:00 起没在挂 labmon wait：本项目还有任务没跑完，给会话的告警（失败、依赖卡住、借到卡等）送不到它。回到负责这个项目的会话，让它重挂 labmon wait";
  const Q = pjCtx({...PJ, alerts: [...PJ.alerts, {key: "wait_gone:1793600000", to: "user", msg}]}, EVN), h = app(Q); clean(h, "项目页");
  const box = h.slice(h.indexOf('<div class="alerts">'), h.indexOf("</div></div>", h.indexOf('<div class="alerts">')));
  has(box, "起没在挂 labmon wait"); has(h, "⚠ 2 条告警");
  const ag = h.slice(h.indexOf('<details class="agal'), h.indexOf("</details>", h.indexOf('<details class="agal')));
  ok(!ag.includes("没在挂 labmon wait"), "只给用户的不该进给会话的折叠");
});
t("project", "GPU 利用率（#51）：大字是近 1 小时的吃满程度（保活按 0），小字是各卡与本项目近 24 小时；没有 worker 数据的卡退回探针此刻值", () => {
  const h = app(P), i0 = h.lastIndexOf('<div class="kpi"', h.indexOf("GPU 利用率 · ")), kpi = h.slice(i0, h.indexOf('<div class="kpi"', i0 + 1));
  has(kpi, "GPU 利用率 · 4 卡 · 整机"); has(kpi, '62<small>% 近 1 小时 平均');                 // (72.3 + 4.2 + 90 + 80) / 4：srv-b 没有 busy，按探针此刻值逐卡补进平均（#54 审查）
  has(kpi, "srv-a·0 72%"); has(kpi, "srv-a·1 4%"); has(kpi, "借 OWNER 90%"); has(kpi, "srv-h 80%"); has(kpi, "本项目近 24 小时 63%");
  const tt = tips(kpi); has(tt, "近 1 小时平均 62%"); has(tt, "其中 1 张卡没有 worker 数据"); has(tt, "只有保活（顶到 99%）"); has(tt, "保活按 0 算"); has(tt, "14.5 卡·时，平均把所用的卡用到 63%");
  has(h, '38%<span class="mut" style="font-weight:400"> 近 1 小时</span>');                    // 主机小图
  ok(run(P, "utilText({now: 0, m10: 0, h1: 4.2, cov: 1200, ka: 99}).span") === "近 20 分钟", "worker 刚起不满 1 小时该写实际多久");
  has(run(P, "utilText({now: 0, m10: 0, h1: 4.2, cov: 1200, ka: 99}).sub"), "只有保活（顶到 99%）");
  const raw = run(P, "utilText(null, 55)"); ok(raw.span === "此刻" && raw.big === "55%" && raw.tip.includes("含保活"), "没有 worker 数据该退回探针此刻值并写明");
  // 多卡里有卡没有 worker 数据（#54 审查）：那张按探针此刻值算进平均，悬停写明，不把部分卡当整机
  const mix = run(P, "utilText(utilAvg([{now: 0, m10: 0, h1: 0, cov: 3600}, null], [0, 100]))");
  ok(mix.big === "50%" && mix.tip.includes("1 张卡没有 worker 数据"), "混合时该逐卡回退：" + JSON.stringify(mix));
  ok(run(P, "utilText(utilAvg([null], [40])).span") === "此刻", "全是探针值时按此刻写");
  ok(run(P, "utilText(utilAvg([{now: 30, m10: null, h1: null, cov: 20}]))").span === "此刻", "样本不够时不该冒充 10 分钟平均");
  ok(run(P, "utilText(null).big") === "—" && run(P, "utilMine(null)") === null, "没有数据该是空");
  // 本项目刚跑完、卡此刻空着（近 1 小时平均还高）：不该说是别的项目在用（#54 审查：按此刻值判）
  const Q = pjCtx({...PJ, jobs: PJ.jobs.filter(j => j.status !== "running"), hosts: PJ.hosts.map(x => ({...x, gpus: (x.gpus || []).map(g => ({...g, util: 0})),
    busy: (x.busy || []).map(b => ({...b, now: 0, h1: 70}))}))}, EVN);
  ok(!app(Q).includes("是别的项目或进程在用"), "历史平均高、此刻空着也说别的项目在用");
  run(Q, "D.hosts[1].gpus[0].util = 60; render()"); has(app(Q), "是别的项目或进程在用");
  // 时间线主机行的浅底、利用率小图按 history 的 busy：前一半旧行没有 busy，不画
  ok(run(P, "HIST('srv-a').filter(r => r.busy).length") === 72, "history 的 busy 行数不对");
});
t("project", "单独打开的项目页顶栏有「管理」，链到工作台的管理抽屉；嵌在工作台里、file:// 打开时没有（#113）", () => {
  const top = app(P).split('class="project-feedback"')[0];
  has(top, `href="../../#/p/${PJ.id}/manage"`, "单独的项目页（看板服务上）");
  const E = mkCtx(pgSrc, PJ, {filename: "page.js", embed: true}); E.EVD = EVN; run(E, "EV = EVD; render()");
  ok(!app(E).includes("/manage"), "嵌在工作台里不该再有（工作台顶栏有）");
  const F = mkCtx(pgSrc, PJ, {filename: "page.js", protocol: "file:"}); F.EVD = EVN; run(F, "EV = EVD; render()");
  ok(!app(F).includes("/manage"), "file:// 打开的离线页不该有");
});
t("project", "顶栏“N 分钟未更新”按快照的 thresholds.stale_s 标红，与 labmon wait 的 collector_stale 同一门槛（#141）", () => {
  const top = ago => { const C = mkCtx(pgSrc, {...PJ, generated: NOW - ago}, {filename: "page.js"}); C.EVD = EVN; run(C, "EV = EVD; render()"); const h = app(C); return h.split('class="project-feedback"')[0]; };
  ok(PJ.thresholds.stale_s === 420, JSON.stringify(PJ.thresholds));
  ok(!top(6 * 60).includes("未更新"), "过了 stale_min（5 分钟）、没到实际门槛（7 分钟）不该标");
  has(top(8 * 60), "（8 分钟未更新）");
});
t("project", "容量一律 GiB（快照里挂载点的 *_gb 就是 GiB）；本机那一列去掉了（#164）", () => {
  const h = app(P), t2 = text(h);
  ok(!t2.includes("内存空闲"), "本机内存空闲还在");
  ok(!/\bGB\b/.test(t2), "正文里还有 GB：" + (/.{40}\bGB\b.{20}/.exec(t2) || [])[0]);
  ok(!t2.includes("内存空闲") && !t2.includes("交换 "), "项目页还有「本机」一栏");
  has(h, "余 787.6 GiB", "挂载点");
});
t("project", "KPI「全部完成」取快照的 all_done_ts，距里程碑的余量", () => {
  const h = app(P), end = run(P, `ts(${NOW + 7 * H})`);
  has(h, `<div class="v">${end}${run(P, "rebalMark(D.rebalance)")}</div>`); has(h, `距投稿截止余 ${run(P, `dur(${3 * 86400 - 7 * H})`)}`);
  has(h, "1 条车道暂停，未计入");
});
t("project", "任务明细（全部）：预计时长、已用 / 实际、开始与结束按 tsNear", () => {
  run(P, 'LS.get = (k, d) => k === "filter" ? "all" : d; render()'); const h = app(P); clean(h, "任务明细");
  has(h, `<td class="r">${run(P, "dur(28.5 * 60)")}</td>`, "实际");
  has(h, `<td class="r hide-sm">${run(P, `tsNear(${NOW - 5 * H})`)}</td><td class="r">${run(P, `tsNear(${NOW - 4.5 * H})`)}</td>`, "开始 / 结束");
  has(h, `<td class="r">${run(P, "dur(120 * 60)")}</td>`, "已用"); has(h, `清单估时 ${run(P, "dur(300 * 60)")}，预计按同 tag alpha-train 近 11 个`, "估时提示");
  has(h, "被终止"); has(h, "暂停中");
  has(h, `<td class="r">—</td>`, "没有估时");
});
t("project", "已跑完的无编号车道（done_lane，#164）：KPI 标注已跑完数量、时间线默认折起可展开、任务明细折进 dlane-more", () => {
  const N = JSON.parse(JSON.stringify(PJ));
  N.lanes.push({id: "done/jobs/d.txt", label: "已跑完", host: "srv-a", type: "gpu_queue", stale: false, root: "/data/me/proj-a/done",
    queue: "jobs/d.txt", alive: false, managed: true, total: 2, done: 2, failed: 0, unfinished: 0, done_lane: true});
  N.jobs.push({id: "done_j1", est_min: 10, status: "done", start_ts: NOW - 2 * H, end_ts: NOW - 1.5 * H, lane: "done/jobs/d.txt", hold: false, group: "alpha_", dur_min: 10, actual_min: 30},
    {id: "done_j2", est_min: 10, status: "done", start_ts: NOW - 1.5 * H, end_ts: NOW - H, lane: "done/jobs/d.txt", hold: false, group: "alpha_", dur_min: 10, actual_min: 30});
  const A = pjCtx(N, EVN);
  run(A, 'LS.get = (k, d) => k === "filter" ? "all" : d; render()');
  const h = app(A);
  has(h, "· 1 已跑完", "KPI 标出已跑完车道数");
  has(h, 'class="dlane-more"', "任务明细折起已跑完车道");
  has(h, "另有 1 条已跑完的车道（2 个任务）", "明细折叠汇总");
  run(A, "drawTimeline()");
  const tl1 = el(A, "tlbox").innerHTML;
  ok(!tl1.includes("done_j1"), "有未完成车道时时间线默认折起 done_lane");
  has(tl1, "另有 1 条已跑完的车道，显示", "时间线展开提示");
  run(A, 'LS.get = (k, d) => k === "tlall" ? true : d; drawTimeline()');
  const tl2 = el(A, "tlbox").innerHTML;
  has(tl2, "done_j1", "点展开后显示已跑完车道");
  has(tl2, "收起已跑完与这段时间没有任务的车道", "展开后收起文案");
  // 第四轮主审意见 #2：点击时间线里的已跑完任务条时，必须同时展开外层 details.dlane-more，不能让目标行仍藏在折叠里
  const jrect = {dataset: {id: "done_j1"}, matches: s => s.includes("rect.j"), closest(s) { return s.includes("rect.j") ? this : null; }};
  fire(A, "click", jrect);
  has(app(A), '<details class="dlane-more" open', "点击已跑完任务条应展开外层 dlane-more");
});
// 时间线上的依赖连线（depLines）：[[等的, 等它的, 类名]]
const depOf = html => [...html.matchAll(/<g class="dl([^"]*)" data-a="([^"]*)" data-b="([^"]*)"/g)].map(m => [m[2], m[3], m[1].trim().split(" ").filter(Boolean)]);
t("project", "依赖：时间线从本项目等的任务结束连一条线到它开始，悬停写等谁（别的项目的只写在悬停里），图例有依赖", () => {
  const A = pjCtx(PJ, EVN); app(A); run(A, "drawTimeline()");
  const tl = el(A, "tlbox").innerHTML, ds = depOf(tl);
  ok(ds.length === 1 && ds[0][0] === "alpha_train_s1" && ds[0][1] === "alpha_train_s3", `依赖连线应只有 1 条（alpha_train_s1 → alpha_train_s3），实际 ${JSON.stringify(ds)}`);
  ok(!ds[0][2].includes("k"), "同一车道上的依赖平时不该画（清单顺序已说明先后）");
  ok(tl.indexOf('<g class="dl') < tl.indexOf('class="j"'), "连线要压在任务条下面");
  has(tips(tl), "等 alpha_train_s1、proj-b 的 beta_eval_s7 跑完才开始", "任务条悬停");
  has(el(A, "tlkey").innerHTML, "依赖（after=）", "图例");
  const N = JSON.parse(JSON.stringify(PJ)); N.jobs.forEach(j => delete j.deps);
  const B = pjCtx(N, EVN); app(B); run(B, "drawTimeline()");
  ok(!el(B, "tlbox").innerHTML.includes('class="dl') && !el(B, "tlkey").innerHTML.includes("依赖（after=）"), "没写依赖时不画连线、不列图例");
});
t("project", "依赖多、跨两台主机（#111）：平时只画跨车道、不能经别的依赖推出来的；悬停一格只高亮它的上下游", () => {
  const A = pjCtx(J("project_deps.json"), EVN); clean(app(A), "依赖多");
  run(A, `TL.v = {a: ${-10 * H}, b: ${8 * H}}; drawTimeline()`);
  const tl = el(A, "tlbox").innerHTML, ds = depOf(tl), cls = (a, b) => (ds.find(x => x[0] === a && x[1] === b) || [, , null])[2];
  clean(tl, "依赖多的时间线");
  ok(ds.length === 39, `39 条 after= 都该进图（平时藏起来的高亮时用），实际 ${ds.length}`);
  const tr = [1, 2, 3, 4, 5, 6, 7];
  const key = [...tr.map(i => `dp_prep2>dp_tr${i}`), ...tr.map(i => `dp_ev${i}>dp_merge`), "dp_merge>dp_report", "dp_side_a>dp_side_b"].sort();
  const got = ds.filter(x => x[2].includes("k")).map(x => x[0] + ">" + x[1]).sort();
  ok(JSON.stringify(got) === JSON.stringify(key), `平时画的应是跨车道的关键边，实际 ${got.join(" ")}`);
  ok(!cls("dp_prep", "dp_prep2").includes("k"), "同一车道前后相连的不该画");
  for (const i of tr) {
    ok(!cls(`dp_tr${i}`, `dp_ev${i}`).includes("k"), "同一车道的不该画");
    ok(!cls("dp_prep", `dp_tr${i}`).includes("k") && cls("dp_prep", `dp_tr${i}`).includes("t"), "传递边（prep → prep2 → tr）不该画");
    ok(!cls("dp_prep2", `dp_ev${i}`).includes("k"), "经跨车道的依赖再加车道顺序推得出来的不该画");
  }
  ok(!tl.includes(' on"') && !el(A, "tlbox").classList.contains("dh"), "没悬停时不该有高亮");
  ok(run(A, "depChain('dp_prep')") !== null && run(A, "depChain('alpha_train_s0')") === null, "没有上下游的格不高亮");
  run(A, "DEPH.id = 'dp_tr3'; drawTimeline()");
  const hl = el(A, "tlbox").innerHTML, hs = depOf(hl);
  ok(el(A, "tlbox").classList.contains("dh"), "悬停后 #tlbox 该加 dh");
  const onJ = [...hl.matchAll(/<g class="jg on" data-j="([^"]*)"/g)].map(m => m[1]).sort();
  const chain = ["dp_ev3", "dp_merge", "dp_prep", "dp_prep2", "dp_report", "dp_tr3"];
  ok(JSON.stringify(onJ) === JSON.stringify(chain), `高亮的格应是 dp_tr3 的上下游，实际 ${onJ.join(" ")}`);
  const onE = hs.filter(x => x[2].includes("on")).map(x => x[0] + ">" + x[1]).sort();
  const want = ["dp_ev3>dp_merge", "dp_merge>dp_report", "dp_prep2>dp_tr3", "dp_prep>dp_prep2", "dp_tr3>dp_ev3"].sort();
  ok(JSON.stringify(onE) === JSON.stringify(want), `高亮的线应只是这条链上的（传递边不算），实际 ${onE.join(" ")}`);
  ok(hl.includes('<g class="jg" data-j="dp_tr4"') && hl.includes('<g class="jg" data-j="dp_side_a"'), "链外的格不该高亮");
  run(A, "DEPH.id = null; drawTimeline()");
  ok(!el(A, "tlbox").classList.contains("dh"), "移开后取消高亮");
});
t("project", "占位任务写了 after=：卡片写预计放行、提示写等谁，时间线与 KPI 不再说按估时猜；没写的照旧", () => {
  const A = pjCtx(PJ, EVN), h = app(A), tp = tips(h), who = "等 alpha_train_s1、proj-b 的 beta_eval_s7 跑完";
  has(h, `已等 <b>${run(A, "dur(30 * 60)")}</b> · 预计 ${run(A, `tsNear(${NOW + H})`)} 放行`, "运行中卡片");
  has(tp, `预计 ${run(A, `ts(${NOW + H})`)} 放行（${who}；放不放仍看它自己的条件）`, "卡片提示");
  run(A, "drawTimeline()"); const tl = tips(el(A, "tlbox").innerHTML);
  has(tl, `（${who}）`, "时间线"); has(tp, "含暂停任务，按它等的任务预计结束算", "KPI"); ok(!(tp + tl).includes("按估时猜"), "还写着按估时猜");
  const N = JSON.parse(JSON.stringify(PJ)), j = N.jobs.find(x => x.id === "hold_until_data");
  Object.assign(j, {deps: undefined, dur_min: null, dur_src: null, eta_source: "暂停（等条件放行，时长按估时猜）", eta_ts: NOW, remaining_min: 0});
  const B = pjCtx(N, EVN), hb = app(B), tb = tips(hb); clean(hb, "没写 after=");
  run(B, "drawTimeline()"); has(tb, "含暂停任务，没写 after= 的时长按估时猜", "KPI"); has(tips(el(B, "tlbox").innerHTML), "（时长按估时猜）", "时间线"); ok(!hb.includes(" 放行</span>"), "没写 after= 的不该写预计放行");
});
t("project", "运行中的条上百分比是现在线在条上的位置，不是当前这段的步数（Issue #33）", () => {
  const N = JSON.parse(JSON.stringify(PJ)), j = N.jobs.find(x => x.id === "alpha_train_s1");
  // 真数据里的多段任务：当前这段 500/7820（6%），预计结束按估时 − 已用，已用 138 分钟、还剩 119 分钟，现在线在条上 54% 处
  Object.assign(j, {progress: [500, 7820], start_ts: NOW - 138 * 60, elapsed_min: 138, dur_min: 257, remaining_min: 119, eta_ts: NOW + 119 * 60,
    eta_source: "估时（多段任务，进度只算当前这段）"});
  const A = pjCtx(N, EVN); run(A, "drawTimeline()"); const tl = el(A, "tlbox").innerHTML;
  has(tl, ">train_s1 · 54%<", "条上百分比"); ok(!tl.includes(">train_s1 · 6%<"), "条上还写着这一段的步数比例");
  has(tips(tl), "进度 500/7820\n预计 4.3 小时\n预计结束依据：估时（多段任务，进度只算当前这段）", "悬停写步数与依据，依据不紧挨着估时");
  // 没有进度行也没有估时：预计结束就是现在，不能写成 100%；进度到头的照写 100%
  Object.assign(j, {progress: null, dur_min: null, start_ts: NOW - 5 * 3600, eta_ts: NOW, remaining_min: 0, eta_source: "无估时"});
  const B = pjCtx(N, EVN); run(B, "drawTimeline()"); const tb = el(B, "tlbox").innerHTML;
  has(tb, ">train_s1 · 无估时<", "无估时"); ok(!tb.includes("100%"), "无估时写成了 100%");
  Object.assign(j, {progress: [100, 100], dur_min: 257, eta_source: "近期速度"});
  const C = pjCtx(N, EVN); run(C, "drawTimeline()"); has(el(C, "tlbox").innerHTML, ">train_s1 · 100%<", "进度到头");
  ok(!tips(app(C)).includes("ETA 依据"), "运行中卡片还写着 ETA 依据");
});
t("project", "时间线按主机分组：主机一行表头（利用率、事件都在里面），车道单行缩进，条上去掉自动分组的前缀", () => {
  const A = pjCtx(PJ, EVN); run(A, `TL.v = {a: ${-3 * H}, b: ${12 * H}}; drawTimeline()`);     // 固定 15 小时的视图：条宽与下面省略的写法对得上
  const tl = el(A, "tlbox").innerHTML, labs = [...tl.matchAll(/<text class="lab"[^>]*>(.*?)<\/text>/g)].map(m => m[1]);
  ok(JSON.stringify(labs) === JSON.stringify(["srv-a", "车道 1 · q1", "车道 2 · q2", "借 OWNER", "车道 1 · b2", "srv-h", "车道 1 · h"]), JSON.stringify(labs));   // 算力池不是主机，不画一行（#89）
  ok(!tl.includes("GPU 利用率") && !tl.includes(" 事件<") && !tl.includes("同机别的项目<"), "还有单独的利用率、事件行或主机前缀");
  ok((tl.match(/stroke-opacity="\.4"/g) || []).length === 2, "三组之间该各有一条分隔线");
  has(tl, ">train_s1 · 44%<", "条上去掉 alpha_"); has(tl, ">⏸ unt…ata<", "占位任务"); has(tips(tl), "alpha_train_s1", "悬停是全名");
  ok(!tl.includes('r="2.5"'), "图上只有一个计划项时不该画计划项圆点");
  run(A, 'TL.v = null; LS.set("tl", "week"); drawTimeline()');
  ok((el(A, "tlbox").innerHTML.match(/r="2.5"/g) || []).length === 3, "7 天里 srv-a 车道 1 跑了 probe、alpha，借 OWNER 车道 1 跑了 eval");
});
t("project", "#147 并行清单：车道名后标「并行 k/N」，同时在跑与排队的重叠格分道画（一行变高），串行车道照旧一道", () => {
  const N = JSON.parse(JSON.stringify(PJ)), ld = "sw/jobs/sweep.txt", base = N.lanes[0];
  N.lanes.push({...base, id: ld, label: "车道 3", root: "/data/me/proj-a/sw", queue: "jobs/sweep.txt", alive: true, paused: null, held: null, waiting: null, stuck: null,
    par: 3, running_n: 2, total: 4, done: 0, failed: 0, unfinished: 4, eta_ts: NOW + 600});
  const job = (i, st, kw) => ({id: `sweep_s${i}`, est_min: 8, status: st, lane: ld, hold: false, group: null, dur_min: 8, dur_src: "清单估时", ...kw});
  N.jobs.push(job(1, "running", {start_ts: NOW - 360, elapsed_min: 6, speed: [], eta_source: "清单估时", remaining_min: 2, eta_ts: NOW + 120}),
              job(2, "running", {start_ts: NOW - 180, elapsed_min: 3, speed: [], eta_source: "清单估时", remaining_min: 5, eta_ts: NOW + 300}),
              job(3, "queued", {plan_start_ts: NOW + 120, eta_ts: NOW + 600}), job(4, "queued", {plan_start_ts: NOW + 300, eta_ts: NOW + 780}));
  const A = pjCtx(N, EVN); run(A, `TL.v = {a: ${-1 * H}, b: ${1 * H}}; drawTimeline()`);
  const tl = el(A, "tlbox").innerHTML; clean(tl, "时间线");
  const labs = [...tl.matchAll(/<text class="lab"[^>]*>(.*?)<\/text>/g)].map(m => m[1]);
  ok(labs.includes("车道 3 · sweep"), JSON.stringify(labs));                     // 分了两道：「并行 2/3」写在车道名下面一行
  has(tl, ">并行 2/3</text>", "车道名下面的并行 k/N");
  N.jobs = N.jobs.filter(j => j.id !== "sweep_s2" && j.id !== "sweep_s4"); N.lanes[N.lanes.length - 1].running_n = 1;
  const B = pjCtx(N, EVN); run(B, `TL.v = {a: ${-1 * H}, b: ${1 * H}}; drawTimeline()`);
  const lb = [...el(B, "tlbox").innerHTML.matchAll(/<text class="lab"[^>]*>(.*?)<\/text>/g)].map(m => m[1]);
  ok(lb.includes("车道 3 · sweep（并行 1/3）"), "只有一道时接在车道名后面：" + JSON.stringify(lb));
  has(tips(tl), "并行清单：同时最多 3 行，此刻在跑 2 行", "行名的悬停");
  const P2 = run(A, `(() => { const l = D.lanes.find(x => x.id === ${JSON.stringify(ld)}); const p = parPack(l, rowJobs(l, l.host)); return [p.n, ...["sweep_s1", "sweep_s2", "sweep_s3", "sweep_s4"].map(i => p.k.get(i))]; })()`);
  ok(JSON.stringify(P2) === JSON.stringify([2, 0, 1, 0, 1]), "两格同时在跑分两道、后面的接在先空出的那道：" + JSON.stringify(P2));
  ok(run(A, `parPack(D.lanes[0], rowJobs(D.lanes[0], D.lanes[0].host)).n`) === 1, "串行车道该只有一道");
});
t("project", "#100 清单搬走后原主机在跑完的那一格：画在原主机下另起一行（只画那一格），登记所在主机那行不画它；运行中与明细标「在 X 上跑完」", () => {
  const A = pjCtx(J("project_moved.json"), EVN); run(A, `TL.v = {a: ${-3 * H}, b: ${12 * H}}; drawTimeline()`);
  const tl = el(A, "tlbox").innerHTML, labs = [...tl.matchAll(/<text class="lab"[^>]*>(.*?)<\/text>/g)].map(m => m[1]);
  ok(JSON.stringify(labs) === JSON.stringify(["srv-a", "车道 1 · q1", "车道 2 · q2", "车道 1 · b2（在这台跑完）", "借 OWNER", "车道 1 · b2", "srv-h", "车道 1 · h",
    "srv-z", "车道 1 · b2（在这台跑完）"]), JSON.stringify(labs));
  clean(tl, "时间线");
  const rows = tl.split('stroke-opacity=".4"'), at = id => rows.findIndex(r => r.includes(`data-id="${id}"`));
  ok(at("alpha_eval_s5") === 0 && at("alpha_train_s1") === 0, "alpha_eval_s5 该画在 srv-a 那组：" + at("alpha_eval_s5"));
  ok(at("alpha_eval_s4") === rows.length - 1, "主机表里没有的 srv-z 另起一组：" + at("alpha_eval_s4"));
  ok((tl.match(/data-id="alpha_eval_s5"/g) || []).length === 1, "这一格只该画一次（登记所在的 srv-b 那行不画）");
  has(tips(tl), "在 srv-a 上跑完（清单已搬到 借 OWNER）", "条的悬停");
  has(tips(tl), "这一格是登记搬走前 srv-a（A6000） 放行的，在这台跑完", "行名的悬停");
  const h = app(A); clean(h, "项目页");
  has(h, "借 OWNER 车道 1 · b2（在 srv-a 上跑完）", "运行中卡片"); has(h, "在 srv-z 上跑完", "运行中卡片");
  run(A, 'SS.set("jobsOpen", true); render()'); has(app(A), '<span class="sm mut nw">在 srv-a 上跑完</span>', "任务明细");
});
t("project", "#99 清单交回算力池后：跑过、在跑的任务与失败的尝试画在实际跑的那台下（另起一行），还没放的不进时间线，视图范围也算上它们", () => {
  const A = pjCtx(J("project_pool_back.json"), EVN); run(A, `TL.v = {a: ${-6 * H}, b: ${6 * H}}; drawTimeline()`);
  const tl = el(A, "tlbox").innerHTML, labs = [...tl.matchAll(/<text class="lab"[^>]*>(.*?)<\/text>/g)].map(m => m[1]);
  ok(JSON.stringify(labs.slice(-2)) === JSON.stringify(["srv-y", "车道 1 · p（在这台跑完）"]), "回池的车道该在 srv-y 下另起一行：" + JSON.stringify(labs));
  clean(tl, "时间线");
  for (const id of ["alpha_pool_s0", "alpha_pool_s4"]) ok((tl.match(new RegExp(`data-id="${id}"`, "g")) || []).length === 1, id + " 该画一次");
  ok(!tl.includes('data-id="alpha_pool_s1"'), "还在池里待放的不该画");
  has(tips(tl), "失败的尝试", "srv-y 上失败的尝试该画在那一行");
  has(tips(tl), "在 srv-y 上跑完（清单已交回算力池）", "条的悬停");
  has(tips(tl), "清单已交回算力池，之后的任务等看板放到某台", "行名的悬停");
  has(tl, "算力池 车道 1 · p（1 个任务）不在图上", "时间线下只数还没放的");
  const near = run(A, "JSON.stringify(D.jobs.filter(j => !inPool(j)).map(j => j.id))");
  ok(near.includes("alpha_pool_s0") && near.includes("alpha_pool_s4") && !near.includes("alpha_pool_s1"), "视图范围该算上跑过的、不算待放的：" + near);
});
t("project", "#108 车道名的清单名长、计划项多：左栏按行名放宽，放不下时留住「车道 N」与清单名末段，圆点收成个数，悬停是全名", () => {
  const A = pjCtx(J("project_longq.json"), EVN), labs = () => [...el(A, "tlbox").innerHTML.matchAll(/<text class="lab"[^>]*>(.*?)<\/text>/g)].map(m => m[1]);
  for (const w of [1200, 375, 330]) {
    el(A, "tlbox").clientWidth = w; run(A, 'TL.v = null; LS.set("tl", "week"); drawTimeline()');
    const tl = el(A, "tlbox").innerHTML, L = run(A, "TL.g.L"), ls = labs(), a = ls.find(x => x.startsWith("车道 1")), b = ls.find(x => x.startsWith("车道 2"));
    ok(L > (w < 600 ? 82 : 116) && L <= (w < 600 ? 128 : 200), w + " 宽左栏该放宽到上限之内：" + L);
    const e = w > 340 ? "part0" : "0";     // 手机宽（330）最窄：至少留住序号
    ok(a && a.endsWith(e + "7") && b && b.endsWith(e + "8") && a.startsWith("车道 1") && b.startsWith("车道 2"), w + " 宽车道名该留住「车道 N」与清单名的末段：" + JSON.stringify(ls));
    if (w > 600) ok(a.startsWith("车道 1 · gpu") && b.startsWith("车道 2 · cpu"), "宽屏车道名该留住「车道 N · 」与 gpu/cpu：" + JSON.stringify(ls));
    ok(ls.every(x => run(A, `textW(${JSON.stringify(x)})`) <= L - 18), w + " 宽车道名超出了左栏：" + JSON.stringify(ls));
    has(tl, '<text x="', "圆点收成个数"); ok(/fill:var\(--muted\)">4<\/text>/.test(tl), w + " 宽车道 1 跑了四个计划项，该收成一个点加 4");
    has(tips(tl), "车道 1 · gpu_srvx_sweep_part07", "悬停是全名");
  }
  el(A, "tlbox").clientWidth = 900; run(A, 'drawTimeline()');
  ok(run(A, 'laneFit({label: "车道 2", queue: "q.txt"}, 200)') === "车道 2 · q", "放得下照原样");
});
t("project", "fit：缺省从尾部截，mid 省略中间，字符串是原样留着的前缀", () => {
  ok(run(P, 'fit("p1_cifar_vit_wu_nz_exampleb_s42_memfix", 120)') === "p1_cifar_vit_wu_nz…");
  ok(run(P, 'fit("p1_cifar_vit_wu_nz_exampleb_s42_memfix", 120, true)') === "p1_cifar_…42_memfix");
  ok(run(P, 'fit("短", 120, true)') === "短" && run(P, 'fit("abc", 100, "proj-b ")') === "proj-b abc");
  ok(run(P, 'fit("beta_eval_s8_long_name", 60, "proj-b ")') === "proj-b b…e");
});
t("project", "hostBands：换容器只按带 started 的事件画，掉线区间、drain 与约定归还的区间", () => {
  const a = run(P, 'hostBands("srv-a")'), b = run(P, 'hostBands("srv-b")');
  const rs = a.ticks.filter(x => x.k === "restart"); ok(rs.length === 1 && rs[0].t === NOW - 9.45 * H, JSON.stringify(rs));
  ok(a.iv.some(x => x.k === "down" && x.t0 === NOW - 10 * H && x.t1 === NOW - 9.5 * H), JSON.stringify(a.iv));
  ok(a.iv.some(x => x.k === "drain" && x.t0 === NOW - 5 * H && x.t1 === NOW - 4 * H), JSON.stringify(a.iv));
  ok(a.ticks.filter(x => x.k === "renew").length === 4, "续租、重新拉起、作废、中断各一个刻度：" + JSON.stringify(a.ticks.map(x => x.short)));
  ok(!b.iv.some(x => x.k === "deadline"), "约定归还在到点之前不该画成区间：" + JSON.stringify(b.iv));
  ok(!leak(JSON.stringify([a, b])));
});
t("project", "#103 借来的主机过了约定时间：约定归还与到点自动下的 drain 并成一条「约定归还 → 跑完就还」，画到在跑的预计跑完，标签不叠", () => {
  const A = pjCtx(J("project_overdue.json"), J("events_overdue.json")), cut = NOW - 30 * 60;
  const b = run(A, 'hostBands("srv-b")').iv.filter(x => x.k !== "nospan");
  ok(b.length === 1 && b[0].k === "ret" && b[0].t0 === cut && b[0].t1 === NOW + 15 * 60, JSON.stringify(b));
  ok(b[0].short === "约定归还 → 跑完就还", b[0].short);
  has(b[0].text, `约定 ${run(A, `ts(${cut})`)} 还给 OWNER：到点不再放新任务`, "悬停写约定");
  has(b[0].text, "到了约定时间：不再放新任务，正在跑的跑完就还给 OWNER", "悬停写跑完就还"); has(b[0].text, `预计 ${run(A, `ts(${NOW + 15 * 60})`)} 跑完归还`);
  // 用户先点了「跑完就还」、后到约定时间：仍是一条，先写跑完就还
  const r = run(A, `(() => { const E0 = EV; EV = {...EV, events: {...EV.events, "srv-b": [...EV.events["srv-b"].slice(0, 2),
      {t: ${NOW - 2 * H + 30}, kind: "override", msg: "临时指令：drain 主机", act: "add", okind: "drain", target: null, until: null, at: ${NOW - 2 * H + 30}},
      {t: ${NOW - 2 * H + 31}, kind: "borrow", act: "return", now: false, host: "srv-b", msg: "要还"}]}};
    const b = hostBands("srv-b").iv.filter(x => x.k !== "nospan"); EV = E0; return b.map(x => [x.k, x.t0, x.short]); })()`);
  ok(JSON.stringify(r) === JSON.stringify([["ret", NOW - 2 * H + 30, "跑完就还 · 约定 " + run(A, `tsHM(${cut})`)]]), JSON.stringify(r));
  // 主机行底边：两条带起点重叠时只写一个标签（别的指令同理）
  for (const w of [1200, 375]) {
    el(A, "tlbox").clientWidth = w; run(A, `TL.v = {a: ${-3 * H}, b: ${H}}; EV.events["srv-b"].push({t: ${cut + 100}, kind: "override", msg: "x", act: "add", okind: "pause", target: "proj-a", until: null, at: ${cut + 100}}); drawTimeline()`);
    const tl = el(A, "tlbox").innerHTML, xs = [...tl.matchAll(/<text x="([\d.]+)" y="[\d.]+" style="font-size:9.5px[^>]*>(.*?)<\/text>/g)].map(m => [+m[1], m[2]]);
    ok(xs.some(([, t]) => t.startsWith("约定归")), w + " 宽缺约定归还的标签：" + JSON.stringify(xs));
    ok(!xs.some(([, t]) => t === "drain"), w + " 宽不该再有单独的 drain 标签：" + JSON.stringify(xs));
    const sorted = xs.sort((a, b) => a[0] - b[0]);
    ok(sorted.every(([x, t], i) => !i || x >= sorted[i - 1][0] + run(A, `textW(${JSON.stringify(sorted[i - 1][1])})`)), w + " 宽标签叠在一起：" + JSON.stringify(xs));
    run(A, 'EV.events["srv-b"].pop()');
  }
});
t("project", "#96 hostBands：项目级的暂停（glob）与这台单独下的各自配对，撤一个不关另一个的区间（Codex P2）", () => {
  const r = run(P, `(() => { const E0 = EV; EV = {...EV, events: {...EV.events, "srv-a": [
      {t: ${NOW - 3 * H}, kind: "override", msg: "全局暂停", act: "add", okind: "pause", target: "proj-a", glob: true},
      {t: ${NOW - 2 * H}, kind: "override", msg: "单台暂停", act: "add", okind: "pause", target: "proj-a"},
      {t: ${NOW - H}, kind: "override", msg: "撤单台", act: "rm", okind: "pause", target: "proj-a"},
      {t: ${NOW - 0.5 * H}, kind: "borrow_return", msg: "还了"}]}};
    const b = hostBands("srv-a"); EV = E0; return b.iv.filter(x => x.k === "pause").map(x => [x.t0, x.t1, !!x.open]); })()`);
  // 归还只关这台的指令（finish 删的是这台的文件），项目级的照旧开着（Codex 审查）
  ok(JSON.stringify(r) === JSON.stringify([[NOW - 2 * H, NOW - H, false], [NOW - 3 * H, NOW, true]]), JSON.stringify(r));
});
t("project", "ovrOf：读事件带的字段，只有文字的不认", () => {
  const r = run(P, `[ovrOf({t: 1, kind: "override", msg: "x", act: "add", okind: "drain", target: null, until: 5, at: 4}),
    ovrOf({t: 1, kind: "override", msg: "撤销临时指令：pause p0", act: "rm", okind: "pause", target: "p0"}), ovrOf({t: 1, kind: "override", msg: "临时指令：暂停 p0"})]`);
  ok(r[0].act === "add" && r[0].until === 5 && r[0].at === 4, JSON.stringify(r[0]));
  ok(r[1].act === "rm" && r[1].okind === "pause" && r[1].target === "p0", JSON.stringify(r[1]));
  ok(r[2] === null, JSON.stringify(r[2]));
});
t("project", "项目页顶栏反馈和嵌入状态", () => {
  ok(run(P, "embedState().now") === NOW);
  run(P, "BUSY = true; render()"); has(app(P), 'role="status"><span class="sm mut">采集中…');
  ok(run(P, "embedState().busy"));
  run(P, 'BUSY = false; OFFLINE = "ECONNREFUSED"; render()');
  has(app(P), `显示的是 ${run(P, `tsNear(${NOW})`)} 的数据`); run(P, "OFFLINE = null");
});
t("project", "刷新不冲掉输入：正文里选着字时 redraw 只记 PENDING", () => {
  run(P, 'SEL_T = Date.now(); getSelection = () => ({isCollapsed: false, anchorNode: {}}); document.getElementById("app").contains = () => true; lastDraw = 0; redraw()');
  ok(run(P, "PENDING") === true && run(P, "lastDraw") === 0);
  run(P, "SEL_T = Date.now() - SEL_HOLD - 1; lastDraw = 0; redraw()"); ok(run(P, "PENDING") === false && run(P, "lastDraw") > 0, "选区放久了仍挡着重画（#66）");
  run(P, "SEL_T = Date.now(); lastDraw = 0; redraw()"); ok(run(P, "PENDING") === true);
  run(P, 'getSelection = () => ({isCollapsed: true, anchorNode: null}); document.getElementById("app").contains = () => false; redraw()');
  ok(run(P, "PENDING") === false && run(P, "lastDraw") > 0);
  run(P, 'document.activeElement = {closest: s => s.includes("select") ? {} : null}; lastDraw = 0; redraw()'); ok(run(P, "PENDING") === true);
  run(P, "document.activeElement = document.body; redraw()"); ok(run(P, "PENDING") === false);
});
t("project", "时间线：全部视图没有任务时范围照样有限", () => {
  run(P, 'LS.set("tl", "all"); TL.v = null'); const r = run(P, "tlRange()"); ok(Number.isFinite(r.t0) && r.t1 > r.t0);
  ok(run(P, "TL.cfg.all()").every(Number.isFinite));
  run(P, "D.jobs = []; D.attempts = []"); ok(run(P, "TL.cfg.all()").every(x => x === null));
  const r2 = run(P, "tlRange()"); ok(Number.isFinite(r2.t0) && r2.t1 > r2.t0, JSON.stringify(r2));
  run(P, "render()"); clean(app(P), "没有任务时");
});
/* ---------- 大量小规模并行：计划项与运行中的紧凑视图（Issue #69） ---------- */
const PJM = J("project_many.json");
const cardOf = (h, hd) => h.split('<div class="card"').find(x => x.includes(`<span class="hd">${hd}</span>`)) || "";
t("compact", "计划项多：完成的全收进一行（项数、任务数、配色小方块），只列未完成的", () => {
  const A = pjCtx(PJM, EVN), h = app(A); clean(h, "大量并行");
  const g = cardOf(h, "计划项"), [top, fold] = g.split('<details class="gdone"');
  ok((top.match(/class="grow"/g) || []).length === 4, "外面应只列 4 个没完成的计划项");
  has(fold, "已完成 26 项 · 286 个任务<span class=\"sqs\">", "收起那一行"); ok(!fold.includes("一天前就跑完的"), "多的时候不该再写一天前");
  ok((fold.split("</summary>")[0].match(/class="gsq"/g) || []).length === 26, "每项一个小方块");
  has(tips(fold), "计划项多于 8 个，完成的都收在这里", "悬停");
  const B = pjCtx(PJ, EVN), gb = cardOf(app(B), "计划项");          // 正常快照 4 个计划项：照旧只收一天前的（old_ 没有任务，算一天前）
  has(gb, "已完成 1 项 · 3 个任务（一天前就跑完的）");
});
/* ---------- 收起行的小方块（#115）：每格自己的悬停、按跑完时刻排、点一格定位 ---------- */
const sqOf = h => { const f = cardOf(h, "计划项").split('<details class="gdone"')[1] || "", [sm, list] = f.split("</summary>");
  return {sm, list, ids: [...sm.matchAll(/data-gsq="([^"]*)"/g)].map(m => m[1]), rows: [...list.matchAll(/data-gid="([^"]*)"/g)].map(m => m[1])}; };
const finOf = (snap, g) => Math.max(0, ...snap.jobs.filter(j => j.group === g.id).map(j => j.end_ts || 0));
t("gsq", "每格一个悬停（名字、完成数、几点跑完）；整行只一句话；配色重复也认得出", () => {
  const A = pjCtx(PJM, EVN), {sm} = sqOf(app(A));
  const sqTips = [...sm.matchAll(/class="gsq"[^>]*data-tip="([^"]*)"/g)].map(m => m[1]);
  ok(sqTips.length === 26 && new Set(sqTips).size === 26, "每格的悬停应各不相同：" + sqTips.length);
  const g = PJM.groups.find(x => x.id === "p1_v03_");
  has(sqTips.join("\n"), `p1_v03_*\n完成 11/11 · ${run(A, `ts(${finOf(PJM, g)})`)} 跑完\n点一下定位到下面那一行`, "一格的悬停");
  const colors = new Set([...sm.matchAll(/class="gsq"[^>]*style="background:([^"]*)"/g)].map(m => m[1]));
  ok(colors.size < 26, "快照应有重复的配色：" + colors.size);
  const sumTip = /<summary [^>]*data-tip="([^"]*)"/.exec(sm)[1];
  ok(!sumTip.includes("\n") && sumTip.includes("移到小方块上看那一项"), "整行悬停应缩成一句话：" + sumTip);
  ok(!sumTip.includes("p1_v"), "整行悬停不再列各项");
});
t("gsq", "按跑完时刻从早到晚排，展开后的列表同一顺序", () => {
  const A = pjCtx(PJM, EVN), {ids, rows} = sqOf(app(A));
  const want = PJM.groups.filter(g => g.status === "完成").sort((a, b) => finOf(PJM, a) - finOf(PJM, b)).map(g => g.id);
  ok(JSON.stringify(ids) === JSON.stringify(want), "方块顺序：" + ids.slice(0, 5).join(","));
  ok(JSON.stringify(rows) === JSON.stringify(want), "列表顺序：" + rows.slice(0, 5).join(","));
  ok(JSON.stringify(ids) !== JSON.stringify([...ids].sort()), "快照里跑完的先后应与编号错开");
});
t("gsq", "多于上限只画最近跑完的那些，最前面一个“…”写着更早的几项", () => {
  const A = pjCtx(PJM, EVN); run(A, "COMPACT.sq = 10; render()");
  const {sm, ids, rows} = sqOf(app(A));
  ok(ids.length === 10 && JSON.stringify(ids) === JSON.stringify(rows.slice(-10)), "应是最后 10 个：" + ids.join(","));
  ok(rows.length === 26, "展开列表照样列全"); has(tips(sm), "更早跑完的还有 16 项，点开看");
  ok(sm.indexOf("…") < sm.indexOf("data-gsq"), "“…”在最前面");
});
t("gsq", "点一格：展开列表、滚到那一行并闪一下；触屏第一次点只看悬停，再点同一格才定位", () => {
  const A = pjCtx(PJM, EVN);
  run(A, `var ROW = {cls: new Set(), sc: 0, scrollIntoView() { this.sc++; }}; ROW.classList = {add: c => ROW.cls.add(c), remove: c => ROW.cls.delete(c)};
    var DET = {open: false, q: null, querySelector(s) { this.q = s; return ROW; }};
    document.querySelector = s => s === "details.gdone" ? DET : null;`);
  ok(run(A, 'gsqClick("p1_v03_")') === true, "鼠标：点一下就定位");
  ok(run(A, "DET.open") === true && run(A, 'SS.get("gdone", false)') === true, "应展开并记住");
  ok(run(A, "DET.q") === '[data-gid="p1_v03_"]' && run(A, "ROW.sc") === 1 && run(A, 'ROW.cls.has("hl")'), "应滚到那一行并闪");
  run(A, 'matchMedia = () => ({matches: true}); DET.open = false; ROW.sc = 0; render()');
  has(tips(sqOf(app(A)).sm), "再点一下定位到下面那一行", "触屏的悬停文字");
  ok(run(A, 'gsqClick("p1_v03_")') === false && run(A, "DET.open") === false && run(A, "ROW.sc") === 0, "触屏第一次只显示悬停");
  ok(run(A, 'gsqClick("p1_v04_")') === false, "换一格又是第一次");
  ok(run(A, 'gsqClick("p1_v04_")') === true && run(A, "DET.open") === true, "再点同一格才定位");
  run(A, 'document.querySelector = () => null'); ok(run(A, 'gsqClick("p1_v04_") || gsqClick("p1_v04_")') === false, "没有收起行时什么都不做");
});
t("compact", "在跑的多：运行中按计划项分组、一个任务一行，不画速度小图；没在跑的车道合成一行", () => {
  const A = pjCtx(PJM, EVN), r = cardOf(app(A), "运行中");
  has(r, 'data-rv="compact" class="on"', "缺省紧凑"); has(r, "11 个", "卡头写在跑数");
  ok((r.match(/class="jr"/g) || []).length === 11, "每个在跑任务一行");
  ok(!r.includes('class="mini"') && !r.includes("速度 · 近"), "紧凑视图不该画速度小图");
  const heads = [...r.matchAll(/<summary [^>]*>[\s\S]*?<b class="ell">(.*?)<\/b>/g)].map(m => m[1]);
  ok(JSON.stringify(heads) === JSON.stringify(["p1_v26_*", "p1_v27_*", "未分组"]), "组按最早结束排：" + JSON.stringify(heads));
  has(r, `6 个在跑 · 最早 ${run(A, `tsNear(${NOW + 4 * 60})`)} 结束`, "组头");
  has(r, '<span class="mono ell">s0</span>', "任务名去掉计划项前缀"); has(r, "超估时 +6 分钟"); has(r, "⏸ 已等 20 分钟", "占位任务");
  has(tips(r), "速度 现在 1.5 · 近 4 分钟 均 1.4 步/分", "速度放悬停");
  has(r, "另有 2 条车道没在跑：等待 1 · 暂停 1"); has(tips(r), "车道 13 · m12：已暂停：临时指令 pause", "悬停逐条列车道状态");
});
t("compact", "没在跑那一行的车道类别与悬停同一个判断（laneKind）：队列进程在、又在等资源的算排队（#86）", () => {
  const A = pjCtx(PJM, EVN);
  run(A, 'Object.assign(D.lanes.find(l => l.waiting && l.unfinished && !D.jobs.some(j => j.lane === l.id && j.status === "running")), {alive: true, pid: 4321}); render()');
  const r = cardOf(app(A), "运行中");
  has(r, "另有 2 条车道没在跑：排队 1 · 暂停 1", "计数与悬停应同序：alive 在 waiting 前"); has(tips(r), "队列在跑（PID 4321）");
});
t("compact", "「未分组」组头的最早结束不算占位任务；占位任务紧凑行的悬停与详细视图同一句（#86）", () => {
  const A = pjCtx(PJM, EVN), r = cardOf(app(A), "运行中");
  const un = r.slice(r.indexOf('data-rg=""'));
  has(un, '<b class="ell">未分组</b><span class="sm mut nw">1 个在跑</span>', "只有占位任务的组头不该写最早几点结束");
  ok(!tips(un).includes("结束"), "组头悬停也不该把占位任务的放行当结束");
  has(tips(r), "hold_merge（"); has(tips(r), `估时 ${run(A, "dur(3600)")}（等条件放行，实际可长可短）`, "没写依赖的占位任务悬停写估时");
});
t("compact", "手动切回「详细」存本机，回到原来的样子；组头收起的存这一次浏览", () => {
  const A = pjCtx(PJM, EVN);
  run(A, 'LS.set("runview", "full"); render()'); const r = cardOf(app(A), "运行中");
  has(r, 'data-rv="full" class="on"'); ok(!r.includes('class="jr"'), "详细视图不该有紧凑行"); has(r, "速度 · 近", "详细视图有速度小图");
  run(A, 'LS.set("runview", "compact"); SS.set("rclosed", ["p1_v26_"]); render()');
  const r2 = cardOf(app(A), "运行中"); has(r2, '<details class="rgrp" data-rg="p1_v26_" >', "收起的组"); has(r2, '<details class="rgrp" data-rg="p1_v27_" open>', "别的组照开");
});
t("compact", "不到阈值（正常快照）：没有切换，照旧详细；恶意字符串强制紧凑时都转义", () => {
  const A = pjCtx(PJ, EVN), r = cardOf(app(A), "运行中");
  ok(!r.includes("data-rv") && !r.includes('class="jr"'), "正常快照不该触发紧凑视图"); has(r, "进度 · 速度");
  const E = pjCtx(PJE, EVE); run(E, "COMPACT.run = 0; COMPACT.groups = 0; render()"); const h = app(E);
  clean(h, "恶意快照的紧凑视图"); ok(h.includes('class="jr"') && h.includes("gdone"), "应走紧凑视图");
});

/* ---------- 时间线的近景：按现在前后的任务定宽（Issue #49） ---------- */
// 高并行短任务的快照（snapshots.project_dense / overview_dense）：旧的近景固定 −3/+12 小时，最晚结束只落在视图 25% 处
const PJD = J("project_dense.json"), OVD = J("overview_dense.json"), clone = x => JSON.parse(JSON.stringify(x));
const DEND = Math.max(...PJD.jobs.filter(j => j.status !== "done").map(j => j.eta_ts));
const fitOf = (X, end) => { const r = run(X, "tlRange()"), w = r.t1 - r.t0; return {w, at: (end - r.t0) / w, now: (NOW - r.t0) / w, r}; };
const near = (x, y) => Math.abs(x - y) < 1e-6;
t("timeline", "项目页高并行短任务：近景按内容放大，最晚结束落在视图 60%–95%，现在线在左起 1/3", () => {
  const A = pjCtx(PJD, EVN); run(A, "TL.v = null; TL.fw = 0");
  const f = fitOf(A, DEND);
  ok(f.w <= 2 * H, `近景宽 ${f.w / H} 小时，应不超过 2 小时`); ok(f.at >= .6 && f.at <= .95, `最晚结束落在视图 ${f.at.toFixed(3)} 处`);
  ok(near(f.now, 1 / 3), `现在线在 ${f.now}`);
  run(A, "drawTimeline()"); clean(el(A, "tlbox").innerHTML, "高并行时间线"); has(el(A, "tlbar").innerHTML, 'data-tl="near" class="on"', "近景按钮");
});
// 工作台的 overview_dense 在高并行短任务之外还有原来的长任务：srv-a 在跑到 +8 小时、排队到 +5、+7 小时，srv-b 在跑到 +2、排队到 +4 小时
t("timeline", "工作台高并行短任务带几条长任务：总览与主机页同一口径（主机页只算那一台），长尾按 90% 分位判", () => {
  const O = ovCtx(OVD, RR); page(O, "#/"); run(O, "TL.v = null; TL.fw = 0");
  // 两台 41 条：90% 分位落在第一条长任务（+2 小时），最晚的 +8 小时超过它的 2 倍，算长尾 → 包住 +2 小时，取 4 小时一档
  const f = fitOf(O, NOW + 2 * H);
  ok(f.w === 4 * H && f.at <= .95 && near(f.now, 1 / 3), JSON.stringify(f));
  page(O, "#/host/srv-a"); run(O, "TL.fw = 0");
  // srv-a 21 条里 3 条长的（多于一成），90% 分位已是 +5 小时，+8 小时不算长尾：全包住，取 16 小时一档
  const a = fitOf(O, NOW + 8 * H); ok(a.w === 16 * H && a.at <= .95, `srv-a：${JSON.stringify(a)}`);
  page(O, "#/host/srv-b"); run(O, "TL.fw = 0");
  // srv-b 20 条里 2 条长的（一成），+2、+4 小时都算长尾：只包住这批短任务
  const eb = Math.max(...OVD.occ["srv-b"].filter(x => x.st !== "done" && x.t1 < NOW + H).map(x => x.t1)), g = fitOf(O, eb);
  ok(g.w <= 2 * H && g.at >= .6 && g.at <= .95, `srv-b：${JSON.stringify(g)}`);
  run(O, "drawTimeline()"); clean(el(O, "tlbox").innerHTML, "工作台高并行时间线");
});
t("timeline", "无估时的在跑任务（预计结束就是现在）不进分位：后面真有的排队照样包住", () => {
  const N = clone(PJD), ln = N.lanes.map(l => l.id);
  N.jobs = [...Array(10)].map((_, i) => ({id: `p1_x${i}`, est_min: null, status: "running", lane: ln[i], hold: false, group: "alpha_", dur_min: null, dur_src: null,
    start_ts: NOW - 20 * 60, elapsed_min: 20, remaining_min: 0, eta_ts: NOW, eta_source: "无估时", speed: []}));
  N.jobs.push({id: "p1_next", est_min: 120, status: "queued", lane: ln[10], hold: false, group: "alpha_", dur_min: 120, dur_src: "清单估时",
    plan_start_ts: NOW + H, eta_ts: NOW + 3 * H});
  const A = pjCtx(N, EVN); run(A, "TL.v = null; TL.fw = 0"); const f = fitOf(A, NOW + 3 * H);
  ok(f.at <= .95 && near(f.now, 1 / 3), `排到 +3 小时的任务落在视图 ${f.at.toFixed(2)} 处：${JSON.stringify(f)}`);
  run(A, "D.jobs = D.jobs.slice(0, 10); TL.fw = 0"); const e = fitOf(A, NOW);
  ok(e.w === H && near(e.now, 1 / 3), `只有无估时的在跑任务时取最窄一档：${JSON.stringify(e)}`);
});
t("timeline", "长尾：再加一个两天后才跑的排队任务，近景不跟着拉宽", () => {
  const N = clone(PJD);
  N.jobs.push({id: "p1_tail", est_min: 180, status: "queued", lane: N.lanes[0].id, hold: false, group: "alpha_", dur_min: 180, dur_src: "清单估时",
    plan_start_ts: NOW + 2 * 86400, eta_ts: NOW + 2 * 86400 + 3 * H});
  const A = pjCtx(N, EVN); run(A, "TL.v = null; TL.fw = 0"); const f = fitOf(A, DEND);
  ok(f.w <= 2 * H, JSON.stringify(f));            // 算长尾时改取 90% 分位：这批里最晚的几个可能出右边缘，条照旧画到边上
});
t("timeline", "任务跨几小时（正常快照）：宽度取到档上，最晚结束落在视图里、右边留边", () => {
  const A = pjCtx(PJ, EVN); run(A, "TL.v = null; TL.fw = 0");
  const end = Math.max(...PJ.jobs.filter(j => j.status === "running" || j.status === "queued").map(j => j.eta_ts || 0)), f = fitOf(A, end);
  ok(run(A, "TL_FIT.lad").includes(f.w) && f.w <= 24 * H, `宽 ${f.w / H} 小时`); ok(f.at > .5 && f.at <= .95, `最晚结束落在 ${f.at}`);
});
// 构造全部完成、历史较长的页面（Issue #85）：车道多、全跑完了，最后一批 15 小时前跑完、开始在 24 小时以前；旧的近景看过去，现在线在 90% 处、包进一天的历史任务
t("timeline", "没有在跑、排队的：近景取 24 小时，现在线照样在 1/3，不为包住已跑完的任务缩放；什么都没有时同样", () => {
  const N = clone(PJD); N.jobs = N.jobs.filter(j => j.status === "done").map((j, i) => ({...j, start_ts: NOW - 26 * H + i * 60, end_ts: NOW - 15 * H - i * 60}));
  N.attempts = [{lane: N.lanes[0].id, name: N.jobs[0].id, t0: NOW - 16 * H, t1: NOW - 16 * H + 30}];
  const A = pjCtx(N, EVN); run(A, "TL.v = null; TL.fw = 0"); const f = fitOf(A, NOW);
  ok(f.w === 24 * H && near(f.now, 1 / 3), JSON.stringify(f));
  run(A, "drawTimeline()"); clean(el(A, "tlbox").innerHTML, "全跑完了时");
  run(A, `D.jobs = D.jobs.map(j => ({...j, start_ts: j.start_ts + ${24 * H}, end_ts: j.end_ts + ${14 * H}})); TL.fw = 0`); const g = fitOf(A, NOW);     // 1 小时前才跑完：宽度照旧
  ok(g.w === 24 * H && near(g.now, 1 / 3), JSON.stringify(g));
  run(A, "D.jobs = []; D.attempts = []; TL.fw = 0"); const e = fitOf(A, NOW);
  ok(e.w === 24 * H && near(e.now, 1 / 3), JSON.stringify(e));
  run(A, "drawTimeline()"); clean(el(A, "tlbox").innerHTML, "没有任务时");
  const O = ovCtx(clone(OVD), RR); page(O, "#/"); run(O, 'for (const k in D.occ) D.occ[k] = D.occ[k].filter(x => x.st === "done"); TL.v = null; TL.fw = 0');
  const o = fitOf(O, NOW); ok(o.w === 24 * H && near(o.now, 1 / 3), `工作台：${JSON.stringify(o)}`);
});
t("timeline", "滞回：最晚结束在档边界两侧来回时宽度不来回跳，缩小要等到下一档的 85% 以下", () => {
  const ws = [36, 38, 36, 32, 30].map(m => run(P, `(r => (r.t1 - r.t0) / 3600)(tlFit(${NOW}, [[${NOW}, ${NOW + m * 60}, true]]))`));
  run(P, "TL.fw = 0");
  ok(JSON.stringify(ws) === JSON.stringify([1, 1.5, 1.5, 1.5, 1]), JSON.stringify(ws));
});
t("timeline", "拖过以后刷新不改视图；点「回到现在」回到近景、按最新任务算", () => {
  const A = pjCtx(PJD, EVN); run(A, `TL.fw = 0; tlView({t0: ${NOW - 5 * H}, t1: ${NOW - 4 * H}}, false)`);
  run(A, `D.jobs.push({id: "p1_late", status: "queued", lane: D.lanes[0].id, hold: false, group: "alpha_", plan_start_ts: ${NOW + H}, eta_ts: ${NOW + 2 * H}}); drawTimeline()`);
  const v = run(A, "tlRange()"); ok(v.t0 === NOW - 5 * H && v.t1 === NOW - 4 * H, JSON.stringify(v));
  run(A, 'tlClick({dataset: {tz: "now"}})'); ok(run(A, "TL.v") === null, "回到现在没清掉拖过的视图");
  const r = run(A, "tlRange()"), e = run(A, `tlFit(${NOW}, TL.cfg.near())`);
  ok(r.t0 === e.t0 && r.t1 === e.t1 && r.t1 - r.t0 <= 2 * H && near((NOW - r.t0) / (r.t1 - r.t0), 1 / 3), JSON.stringify([r, e]));
});
// 页面一直开着：每轮刷新换上新快照（“现在”往后走），现在线在视图里的位置不变（维护者 11-09：拖过以后视图锁在绝对时刻，过一阵只剩历史）
const nowAt = X => { const r = run(X, "tlRange()"), now = run(X, "TL.cfg.now()"); return {at: (now - r.t0) / (r.t1 - r.t0), w: r.t1 - r.t0, r, now}; };
t("timeline", "页面一直开着、选着预设：每轮刷新按新的现在定视图，现在线位置不变", () => {
  for (const tl of ["near", "day", "week"]) {
    const A = pjCtx(PJD, EVN); run(A, `TL.v = null; TL.fw = 0; LS.set("tl", "${tl}"); drawTimeline()`); const a = nowAt(A);
    run(A, "setData({...D, generated: D.generated + 600}); drawTimeline()"); const b = nowAt(A);
    ok(b.now === NOW + 600 && near(a.at, b.at) && a.w === b.w, `${tl}：${JSON.stringify([a, b])}`);
  }
});
t("timeline", "页面一直开着、拖过或缩放过：宽度不变、随现在平移，现在线停在原来的屏幕位置；两页同一处", () => {
  const A = pjCtx(PJD, EVN); run(A, `tlView({t0: ${NOW - 5 * H}, t1: ${NOW + H}}, false); drawTimeline()`); const a = nowAt(A);
  ok(near(a.at, 5 / 6), JSON.stringify(a));
  run(A, "setData({...D, generated: D.generated + 1800}); drawTimeline()"); const b = nowAt(A);
  ok(near(b.at, 5 / 6) && b.w === a.w && b.r.t0 === a.r.t0 + 1800, `刷新后现在线在 ${b.at.toFixed(3)} 处：${JSON.stringify(b)}`);
  run(A, `tlZoom(0.5, ${NOW + 1800})`); const c = nowAt(A);          // 以现在为中心放大一倍：之后照样随现在平移
  run(A, "setData({...D, generated: D.generated + 600}); drawTimeline()"); const d = nowAt(A);
  ok(near(c.at, d.at) && c.w === d.w, JSON.stringify([c, d]));
  const O = ovCtx(OVD, RR); page(O, "#/"); run(O, `tlView({t0: ${NOW - 2 * H}, t1: ${NOW + 2 * H}}, false)`); const e = nowAt(O);
  run(O, "D.generated += 3600; drawTimeline()"); const f = nowAt(O);
  ok(near(e.at, .5) && near(f.at, .5) && e.w === f.w, `工作台：${JSON.stringify([e, f])}`);
});
t("timeline", "拖过的视图存本机的是相对现在的偏移，重开页面照样跟着现在；旧的绝对区间不认", () => {
  const A = pjCtx(PJD, EVN);
  run(A, `TL.cfg.set("tlv", {a: ${-2 * H}, b: ${H}}); tlInit(TL.cfg)`); const a = nowAt(A); ok(near(a.at, 2 / 3) && a.w === 3 * H, JSON.stringify(a));
  run(A, `TL.cfg.set("tlv", {t0: ${NOW - 30 * 86400}, t1: ${NOW - 29 * 86400}}); tlInit(TL.cfg)`); ok(run(A, "TL.v") === null, "旧的绝对区间还被当成拖过的视图");
});
t("timeline", "窄屏（343 宽）：近景同一比例，渲染不漏 NaN/undefined", () => {
  const A = pjCtx(PJD, EVN); el(A, "tlbox").clientWidth = 343; run(A, "TL.v = null; TL.fw = 0; drawTimeline()");
  const tl = el(A, "tlbox").innerHTML; clean(tl, "窄屏时间线"); has(tl, 'viewBox="0 0 343 ');
  const f = fitOf(A, DEND); ok(f.w <= 2 * H && near(f.now, 1 / 3), JSON.stringify(f));
});
t("timeline", "#178 并行车道 par=10 重叠 7 层不重叠、超 4 道压细不写字；超上限最后一道合成「+N 个」聚合条", () => {
  const A = pjCtx(J("project_par10.json"), EVN);
  run(A, `TL.v = {a: ${-2 * H}, b: ${2 * H}}; drawTimeline()`);
  const tl = el(A, "tlbox").innerHTML; clean(tl, "par10 时间线");
  // 提取 p10_r0..r6 的主矩形（class="j"）的 y 与 height，验证 7 层互不重叠且无条内文字
  const rects = id => {
    const m = tl.match(new RegExp(`<g class="jg[^"]*" data-j="${id}">([\\s\\S]*?)</g>`));
    ok(m, "缺少任务条 " + id);
    const r = m[1].match(/<rect x="([^"]+)" y="([^"]+)" width="([^"]+)" height="([^"]+)"[^>]*class="j"/);
    ok(r, "缺少主矩形 " + id);
    return {x: +r[1], y: +r[2], w: +r[3], h: +r[4], hasText: m[1].includes("<text ")};
  };
  const rs = Array.from({length: 7}, (_, i) => rects(`p10_r${i}`)).sort((a, b) => a.y - b.y);
  for (let i = 1; i < rs.length; i++) ok(rs[i - 1].y + rs[i - 1].h <= rs[i].y + 0.01, `p10 第 ${i} 层与前一层重叠：${JSON.stringify(rs)}`);
  ok(rs.every(r => !r.hasText), "超过 4 道时条上不该写字：" + JSON.stringify(rs));
  const pk10 = run(A, 'parPack(D.lanes.find(x => x.id === "par/jobs/p10.txt"), rowJobs(D.lanes.find(x => x.id === "par/jobs/p10.txt"), "srv-a"))');
  ok(pk10.n === 7 && pk10.h <= 86 && !pk10.over, "par=10 7 层应分 7 道且行高 <= 86：" + JSON.stringify(pk10));
  // par=6 但重叠 8 层：前 5 道正常画，第 6 道合成 +3 个聚合条，悬停列明细
  const pk6 = run(A, 'parPack(D.lanes.find(x => x.id === "par/jobs/p6.txt"), rowJobs(D.lanes.find(x => x.id === "par/jobs/p6.txt"), "srv-a"))');
  ok(pk6.n === 6 && pk6.over && pk6.h <= 86, "par=6 8 层应封顶 6 道并触发聚合：" + JSON.stringify(pk6));
  has(tl, 'class="tl-more"', "缺少溢出聚合条");
  has(tl, ">+3 个</text>", "聚合条应标 +3 个");
  const tp = tips(tl);
  has(tp, "另有 3 个重叠任务", "聚合条悬停应列明细");
  for (const id of ["p6_j5", "p6_j6", "p6_j7"]) has(tp, id, "聚合条悬停应包含 " + id);
  // 聚合簇内先结束的任务被另一车道后继依赖时，保留真实端点坐标，依赖连线不丢失
  const Ndep = J("project_par10.json");
  Ndep.jobs.push({id: "after_p6_j5", est_min: 10, status: "queued", lane: Ndep.lanes[0].id, hold: false, group: "alpha_",
    plan_start_ts: NOW + 1860, eta_ts: NOW + 2460, dur_min: 10, deps: [[Ndep.id, "p6_j5"]]});
  const Adep = pjCtx(Ndep, EVN); run(Adep, `TL.v = {a: ${-2 * H}, b: ${2 * H}}; drawTimeline()`);
  has(el(Adep, "tlbox").innerHTML, 'data-a="p6_j5" data-b="after_p6_j5"', "聚合簇内任务的依赖连线不该丢失");
  // 窄屏渲染不漏 NaN/undefined
  el(A, "tlbox").clientWidth = 343; run(A, "drawTimeline()");
  clean(el(A, "tlbox").innerHTML, "窄屏 par10 时间线");
});
t("timeline", "#178 占用行（工作台卡行与项目页同机别的项目）共用防叠与聚合，超 6 道合成「+N 个」", () => {
  const mkOcc = n => Array.from({length: n}, (_, i) => ({p: "proj-b", n: `occ_${i}`, lane: "车道 1", t0: NOW - 1800 + i * 30, t1: NOW + 1800 + i * 30, g: [0], st: "run"}));
  const O = ovCtx(clone(OV), RR); page(O, "#/");
  run(O, `D.occ["srv-a"] = ${JSON.stringify(mkOcc(8))}; TL.v = {a: ${-2 * H}, b: ${2 * H}}; drawTimeline()`);
  const otl = el(O, "tlbox").innerHTML; clean(otl, "工作台高重叠占用");
  has(otl, 'class="tl-more"', "工作台占用行应出聚合条");
  has(otl, ">+3 个</text>", "工作台占用行应聚合多出的 3 条");
  has(tips(otl), "另有 3 个重叠任务", "工作台聚合条悬停明细");
  const P2 = pjCtx(clone(PJ), {...EVN, occ: {"srv-a": mkOcc(8)}});
  run(P2, `TL.v = {a: ${-2 * H}, b: ${2 * H}}; drawTimeline()`);
  const ptl = el(P2, "tlbox").innerHTML; clean(ptl, "项目页同机别的项目高重叠");
  has(ptl, 'class="tl-more"', "项目页 othersRow 应出聚合条");
  has(ptl, ">+3 个</text>", "项目页 othersRow 应聚合多出的 3 条");
});
t("timeline", "#143 左右平移视图时道号与行高不变；项目页拖动期间锁行结构、松手后收紧", () => {
  // 1. 工作台：srv-a 卡 0 有多条重叠任务，只平移视图（不触发拖动）使最早一条滑出窗口，同一条任务的 y 与整图高度不变
  const O = ovCtx(clone(OV), RR); page(O, "#/");
  const ovOcc = [
    {p: "proj-a", n: "early_0", lane: "车道 1", t0: NOW - 7200, t1: NOW - 3000, g: [0], st: "done"},
    {p: "proj-b", n: "mid_1", lane: "车道 1", t0: NOW - 5400, t1: NOW - 1200, g: [0], st: "done"},
    {p: "proj-a", n: "late_2", lane: "车道 1", t0: NOW - 4800, t1: NOW + 600, g: [0], st: "run"},
  ];
  run(O, `D.occ["srv-a"] = ${JSON.stringify(ovOcc)}; TL.v = {a: ${-3 * H}, b: ${H}}; drawTimeline()`);
  const svgH = X => Number((el(X, "tlbox").innerHTML.match(/<svg[^>]*height="([^"]+)"/) || [])[1]);
  const barY = (X, name) => {
    const m = el(X, "tlbox").innerHTML.match(new RegExp(`<rect x="[^"]+" y="([^"]+)"[^>]*data-tip="[^"]*${name}`));
    ok(m, "找不到任务条 " + name); return +m[1];
  };
  const h1 = svgH(O), y1 = barY(O, "late_2");
  // 向右平移使 early_0 完全滑出视图
  run(O, `TL.v = {a: ${-2600}, b: ${-2600 + 4 * H}}; drawTimeline()`);
  const h2 = svgH(O), y2 = barY(O, "late_2");
  ok(h1 === h2 && y1 === y2, `工作台平移后高度或道号跳动：h ${h1}→${h2}, y ${y1}→${y2}`);
  // 2. 项目页：拖动锁期间平移到无任务区间，行数、图高与左栏宽不收缩；松手解锁后收紧
  const A = pjCtx(clone(PJ), EVN);
  run(A, `TL.v = {a: ${-4 * H}, b: ${4 * H}}; drawTimeline()`);
  const ph0 = svgH(A), pl0 = run(A, "TL.g.L"), rows0 = (el(A, "tlbox").innerHTML.match(/<text class="lab"/g) || []).length;
  run(A, `tlLock(); TL.v = {a: ${20 * H}, b: ${28 * H}}; drawTimeline()`);
  const ph1 = svgH(A), pl1 = run(A, "TL.g.L"), rows1 = (el(A, "tlbox").innerHTML.match(/<text class="lab"/g) || []).length;
  ok(ph1 >= ph0 && pl1 >= pl0 && rows1 >= rows0, `项目页拖动中不该收缩：h ${ph0}→${ph1}, L ${pl0}→${pl1}, rows ${rows0}→${rows1}`);
  // 松手解锁后重画，允许收紧
  run(A, "TL.lk = null; drawTimeline()");
  const ph2 = svgH(A);
  ok(ph2 <= ph1, `松手后应允许收紧：${ph1}→${ph2}`);
});
t("timeline", "#207 总览与主机页时间线：有纯 CPU 任务的主机动态显示 CPU 行，无则不画，仅纯 CPU 主机可渲染", () => {
  const O = ovCtx(clone(OV), RR);
  page(O, "#/");
  const tl = el(O, "tlbox").innerHTML;
  clean(tl, "总览含纯 CPU 时间线");
  has(tl, ">srv-a CPU</text>", "总览有纯 CPU 任务的主机应画 CPU 行");
  ok(!tl.includes("借 OWNER CPU</text>"), "总览无纯 CPU 任务的主机不该画 CPU 行");
  const tp = tips(tl);
  has(tp, "cpu_eval_s1", "总览 CPU 行应画在跑的纯 CPU 任务");
  has(tp, "· 纯 CPU", "纯 CPU 任务悬停应写「纯 CPU」");
  ok(!tp.includes("· 卡 \n") && !tp.endsWith("· 卡 "), "纯 CPU 任务悬停不该出现空的「卡 」");
  // 主机页：srv-a 有 CPU 行，srv-b 无 CPU 行
  page(O, "#/host/srv-a");
  const ha = el(O, "tlbox").innerHTML;
  clean(ha, "srv-a 主机页时间线");
  has(ha, ">CPU</text>", "主机页有纯 CPU 任务应画 CPU 行");
  has(tips(ha), "cpu_eval_s1", "主机页 CPU 行含纯 CPU 任务条");
  page(O, "#/host/srv-b");
  const hb = el(O, "tlbox").innerHTML;
  clean(hb, "srv-b 主机页时间线");
  ok(!hb.includes(">CPU</text>"), "主机页无纯 CPU 任务不该画 CPU 行");
  // 仅有纯 CPU 任务且无 GPU 卡的主机：TLH 仍成立并正常渲染 CPU 行
  const D0 = clone(OV);
  D0.hosts = [D0.hosts[0]];
  D0.hosts[0].status.info.gpus = [];
  D0.occ = {"srv-a": [{p: "proj-a", n: "cpu_only_job", lane: "车道 1", t0: NOW - H, t1: NOW + H, g: [], st: "run"}]};
  const O0 = ovCtx(D0, RR);
  page(O0, "#/host/srv-a");
  const h0 = el(O0, "tlbox")?.innerHTML || "";
  clean(h0, "仅纯 CPU 主机时间线");
  has(h0, ">CPU</text>", "仅纯 CPU 主机应正常渲染 CPU 行");
  has(tips(h0), "cpu_only_job", "仅纯 CPU 主机应含任务条");
  has(tips(h0), "· 纯 CPU", "仅纯 CPU 主机悬停应写「纯 CPU」");
});
t("escape", "恶意项目快照与事件：转义", () => {
  const E = pjCtx(PJE, EVE); run(E, 'LS.get = (k, d) => k === "filter" ? "all" : d; render()');
  const h = app(E); ok(!raw(h), "项目页没转义"); has(h, BADESC);
  for (const s of ["题", "任务", "行", "组", "主机", "检", "盘", "道", "告警", "碑", "挪"]) has(h, s + BADESC, s);
  for (const s of ["目录", "写", "顶"]) has(tips(h), s + BADESC, "本项目占用的悬停：" + s);
  const hb = run(E, 'hostBands("srv-a")'); ok(hb.iv.some(x => x.text.includes("事" + BAD)), "hostBands 的提示是纯文字，经 tip() 再转义");
  run(E, 'LS.set("tl", "all"); TL.v = null; render()'); ok(!raw(app(E)), "全部视图");
});
t("project", "本项目占用（#53）：项目快照的 storage，按项目根下的一级目录列占用与增量、删掉的、写得多的任务；涨得快标色；没有 storage 不画", () => {
  const A = pjCtx(PJ, EVN), h = app(A), tp = tips(h);
  has(h, "本项目占用 53.1 GiB · 24 小时 +32.6 · 本地 15.8 GiB");
  has(h, 'style="color:var(--warn);font-weight:600">本项目占用', "burst 时标色");
  has(tp, "本项目在共享盘上（/data/share/me/proj-a；");
  has(tp, "cache  39.1 GiB · 24 小时 +30.7 · 7 天 +36.3");
  has(tp, "项目根下的文件  0.9 GiB · 24 小时 0 · 7 天 0");
  has(tp, "tmp_old（已删）  7 天 −4.7"); has(tp, "还有 1 个目录没扫到");
  has(tp, "alpha_train_s0  +6.9 GiB（cache +6.4）"); has(tp, "alpha_probe_s9  +0.2 GiB（out +0.2） · 还在等重扫");
  has(tp, "共享盘剩 205 GiB，按近 24 小时约 6.3 天满");
  ok((h.match(/本项目占用/g) || []).length === 1, "共享盘只在第一台主机写一次");
  const B = pjCtx({...PJ, storage: undefined}, EVN), hb = app(B); clean(hb, "旧快照没有 storage"); ok(!hb.includes("本项目占用"), "没有 storage 也画了占用");
});
t("sparse", "项目缺省快照：探针失败、已归还、缺清单、没有任务、暂时没实验", () => {
  const Q = pjCtx(PJS, {colors: {}, events: {}, occ: {}}); const h = app(Q); clean(h, "项目页");
  has(h, "已归还"); has(h, run(Q, "NO_WORK")); has(h, "探针超时"); has(h, "没跑完");
  // 只有挪不了的原因：与「⇄ 可提前」同一个小徽章（不跟着时刻用大字），只换灰色；悬停写原因
  has(h, `<div class="v">—${run(Q, "rebalMark(D.rebalance)")}</div>`, "全部完成旁的灰图标");
  has(h, `<span class="badge b-mute" data-tip=`, "灰图标是徽章"); ok((h.match(/⇄/g) || []).length === 1, "图标只一个");
  has(tips(h), run(Q, "esc(rebalTip(D.rebalance))"), "悬停写原因");
  const Q2 = pjCtx(PJS, {}); clean(app(Q2), "events 为空对象");
});
t("sparse", "车道都收起了（#117：清单都跑完一天以上，快照里一条车道、一个任务也没有，主机还在）：照常画、标暂时没实验", () => {
  const Q = pjCtx({...PJS, lanes: [], jobs: [], alerts: []}, {colors: {}, events: {}, occ: {}}); const h = app(Q); clean(h, "项目页");
  has(h, run(Q, "NO_WORK")); has(h, "探针超时", "主机照列");
  run(Q, 'LS.get = (k, d) => k === "filter" ? "all" : d; render()'); clean(app(Q), "任务明细筛全部");
});

/* ---------- 第 4 批：两页的平行实现收进 common.js（源码上只定义一次且两页同一行；行为上各一条） ---------- */
// name 在 src 里的顶层定义行（function / const / let）；common.js 内联进两页，所以两页里的定义行必须一样且只有一行
const defs = (src, name) => src.split("\n").filter(l => new RegExp(`^(?:function ${name}\\b|(?:const|let) ${name} =)`).test(l));
const once = name => { const a = defs(ovSrc, name), b = defs(pgSrc, name); ok(a.length === 1 && b.length === 1 && a[0] === b[0], `${name} 的定义：工作台 ${a.length} 处、项目页 ${b.length} 处，或两页不一样`); };
t("common", "悬停提示的外壳只在 common.js：showTip / hideTip / flashTip / tipCheck / tipBind，两页不再各自定义，tipGuards、TOUCH、TIPEL 已删", () => {
  for (const n of ["placeTip", "showTip", "hideTip", "flashTip", "tipCheck", "tipBind", "TIP", "TIPS"]) once(n);
  for (const [src, what] of [[ovSrc, "工作台"], [pgSrc, "项目页"]]) {
    ok(!/tipGuards|\bTIPEL\b|\bTOUCH\b|\btipT\b/.test(src), what + " 还有旧外壳的名字");
    ok((src.match(/addEventListener\("mousemove"/g) || []).length === 1, what + " mousemove 绑了不止一次");
    ok((src.match(/addEventListener\("mouseout"/g) || []).length === 1, what + " mouseout 绑了不止一次");
  }
  // 行为：显示记下元素，元素不在页面上了 tipCheck 收起；flashTip 也显示
  const r = run(O, `(() => { const el = {isConnected: true}; showTip({clientX: 10, clientY: 10}, "文字", el); const a = [TIP.style.display, TIP.textContent, TIPS.el === el];
    tipCheck(); a.push(TIP.style.display); el.isConnected = false; tipCheck(); a.push(TIP.style.display, TIPS.el);
    flashTip({clientX: 10, clientY: 10}, "闪", null); a.push(TIP.style.display); hideTip(); a.push(TIP.style.display); return a; })()`);
  ok(JSON.stringify(r) === JSON.stringify(["block", "文字", true, "block", "none", null, "block", "none"]), JSON.stringify(r));
  ok(run(P, 'showTip({clientX: 1, clientY: 1}, "p"); TIP.style.display') === "block");
});
t("common", "租期：「一天内到期」只由 leaseSoon 判，带配色的只由 leaseHtml 出；页面上别处不再算 lease_end - now", () => {
  ok(!/lease_end\s*-\s*(now|D\.generated)\b/.test(ovSrc), "还有自己算租期剩余的：" + (/.{0,60}lease_end\s*-\s*(now|D\.generated).{0,40}/.exec(ovSrc) || [])[0]);
  ok((ovSrc.match(/< 86400/g) || []).length === 1, "「< 86400」出现在 leaseSoon 之外");
  const tinted = ovSrc.split("\n").filter(l => /lease/i.test(l) && l.includes("color:var(--bad)"));
  ok(tinted.length === 1 && tinted[0].startsWith("const leaseHtml"), "租期的红色配色不只在 leaseHtml：\n" + tinted.map(l => l.slice(0, 120)).join("\n"));
  const r = run(O, `[leaseSoon(D.generated + 3600), leaseSoon(D.generated + 3 * 86400), leaseSoon(D.generated - 5), leaseSoon(null), leaseSoon(undefined),
    leaseHtml(D.generated + 3600), leaseHtml(D.generated + 3 * 86400, "租期"), leaseHtml(D.generated - 5, "租期"), leaseRem(D.generated + 3600), leaseText({lease_end: null}), leaseLeft(D.generated + 7)]`);
  ok(r[0] === true && r[1] === false && r[2] === true && r[3] === false && r[4] === false, "leaseSoon：" + JSON.stringify(r.slice(0, 5)));
  ok(r[5] === `剩 <b style="color:var(--bad)">${run(O, "dur(3600)")}</b>`, r[5]);
  ok(r[6] === `租期剩 <b style="">${run(O, "dur(3 * 86400)")}</b>`, r[6]);
  ok(r[7] === `<b style="color:var(--bad)">租期已过</b>` && r[8] === `剩 ${run(O, "dur(3600)")}` && r[9] === "" && r[10] === 7, JSON.stringify(r.slice(7)));
  // 总览 KPI、主机表、组内指标与卡片都经 leaseHtml
  const h = page(O, "#/"); has(h, `<div class="k">最早到期的租期</div><div class="v">${run(O, "leaseHtml(D.hosts.filter(h => h.lease_end).sort((a, b) => a.lease_end - b.lease_end)[0].lease_end)")}</div>`, "KPI");
  has(h, run(O, 'leaseHtml(D.hosts[0].lease_end, "租期")'), "主机表");
  const f = page(O, "#/fleet"); ok(f.includes("租期剩 <b"), "组内卡片的租期不经 leaseHtml");
  ok(/(下一个到期|最早到期)<\/span><b style="">(剩 <b style="[^"]*">[^<]+<\/b> · |<b style="color:var\(--bad\)">已过<\/b> · |—)/.test(f), "组内指标「下一个到期」不经 leaseHtml");
});
t("common", "主机名只有三种：短名 hostShort（表格的主机列、时间线、KPI 的注）、全名 h.label、组内 fName；表格里借来的主机写「借 EVA」", () => {
  once("hostShort");
  // srv-a 的任务、等待、已跑完与临时指令都挂到借来的 srv-b 上：主机列该写「借 OWNER」
  const r = run(O, `(() => { const [a, b] = D.hosts; const jb = {...a.status.running[0], h: b}; const ws = a.status.waiting.map(w => ({...w, h: b}));
    return [jobsTable([jb], true), waitingTable(ws, true), doneTable([{...a.done[0], h: b}], true), rankTip(ws[1], ws), ovrTable([{...a.status.overrides[0], h: b}], true)]; })()`);
  has(r[0], `>${run(O, "hostShort(D.hosts[1])")}</a>`, "jobsTable 主机列"); ok(!r[0].includes(">srv-b</a>"), "jobsTable 还写 id");
  has(r[1], `<td class="nw">借 OWNER</td>`, "waitingTable 主机列"); has(r[2], `<td class="nw">借 OWNER</td>`, "doneTable 主机列"); has(r[3], "借 OWNER 上在等的", "rankTip");
  has(r[4], `<td class="nw">借 OWNER</td>`, "ovrTable 主机列");
  has(page(O, "#/"), `${run(O, "esc(hostShort(D.hosts.filter(h => h.lease_end).sort((a, b) => a.lease_end - b.lease_end)[0]))")} · `, "KPI 的注");
});
t("common", "车道名只有 common.js 的 laneName(车道, 主机表)：几台主机时加主机短名、后面接清单名（laneLabel）；工作台的车道标签取快照里 projects[].lanes 的 label", () => {
  once("laneName"); once("laneLabel");
  ok(!/\blname\b/.test(ovSrc) && !/const laneName = l =>/.test(pgSrc), "旧的 lname / 单参数 laneName 还在");
  ok(!/\blaneName\((?![^)]*D\.hosts\))/.test(pgSrc.replace(/^const laneName = .*$/m, "")), "项目页有没带主机表的 laneName 调用：" + (/laneName\([^)]*\)/.exec(pgSrc.replace(/^const laneName = .*$/m, "")) || [])[0]);
  ok(run(O, 'laneName({host: "srv-b", label: "车道 1"}, D.hosts)') === "借 OWNER 车道 1");
  ok(run(O, 'laneName({host: "srv-a", label: "车道 1"}, D.hosts)') === "srv-a 车道 1");
  ok(run(O, 'laneName({host: "srv-a", label: "车道 1"}, [D.hosts[0]])') === "车道 1", "只有一台主机还加了前缀");
  ok(run(O, 'laneName({host: "没有的", label: "车道 9"}, D.hosts)') === "没有的 车道 9", "主机表里没有的按 id");
  // 车道名后面接清单名（清单文件名去掉扩展名，#47）；没有清单路径的（对不上快照的、自动发现还没读到的）只写 label
  ok(run(O, 'laneName({host: "srv-a", label: "车道 2 训练", queue: "jobs/a6000.txt"}, D.hosts)') === "srv-a 车道 2 训练 · a6000");
  ok(run(O, 'laneLabel({host: "srv-a", label: "车道 2", queue: "q.v2.txt"})') === "车道 2 · q.v2", "laneLabel 不带主机、只去最后一个扩展名");
  // 任务卡头部「推演里排不上」的提示：车道按快照的 label 加清单名写（proj-b 在 srv-a 的车道 2）
  const h = page(O, "#/"); has(h, "srv-a 车道 2 · b：推演到 7 天后还没轮到", "stuck 文字");
  has(page(O, "#/host/srv-a"), "车道 2 · b：推演到 7 天后还没轮到", "主机页只有一台：不加主机名");
  ok(run(P, "D.lanes.map(l => laneName(l, D.hosts) === (D.hosts.length > 1 ? hostShort(D.hosts.find(h => h.id === l.host) || {id: l.host}) + ' ' : '') + laneLabel(l)).every(Boolean)"));
  has(app(P), run(P, "esc(laneName(D.lanes[0], D.hosts))"), "项目页车道名");
});
t("common", "本机存的设置只有 common.js 的 mkStore；两页的键名不变（labmon:… 与 labmon:<标题>…），sessionStorage 与 localStorage 分开", () => {
  once("mkStore");
  for (const [src, what] of [[ovSrc, "工作台"], [pgSrc, "项目页"]]) ok(!/(localStorage|sessionStorage)\.(getItem|setItem)\(/.test(src), what + " 还直接读写 Storage");
  ok(!/\bstore\.(l?get|l?set)\b/.test(pgSrc), "项目页还有旧的 store.*");
  const a = run(O, '(() => { const s = mkStore("t:"); s.set("k", [1]); return [localStorage.getItem("t:k"), s.get("k")[0], s.get("z", 9)]; })()');
  ok(JSON.stringify(a) === '["[1]",1,9]', JSON.stringify(a));
  run(O, 'ST.set("fold", {home: true})'); ok(O.localStorage.getItem("labmon:fold") === '{"home":true}', "工作台的键名变了");
  run(P, 'LS.set("filter", "all"); SS.set("scroll", 5)');
  const key = run(P, "KEY"); ok(key === "labmon:" + PJ.title, key);
  ok(P.localStorage.getItem(key + "filter") === '"all"' && P.localStorage.getItem(key + "scroll") == null, "项目页 localStorage 的键名或分工变了");
  ok(P.sessionStorage.getItem(key + "scroll") === "5" && P.sessionStorage.getItem(key + "filter") == null, "项目页 sessionStorage 的键名或分工变了");
  ok(run(P, 'SS.get("scroll", 0)') === 5 && run(P, 'LS.get("nope", 7)') === 7);
});

ta("overview", "权重胶囊（#143，Codex 审查 #147）：写回成功、立即采集回来的快照还是旧权重（worker 还没回传）时，照旧显示刚写的值，再点接着往下一档走；快照确认了才收；失败时退回上一次写成功的值", async () => {
  const O = ovCtx(OV, RR);
  run(O, `SF = []; FAIL = false; fetch = (u, o) => { SF.push([u, o?.body ? JSON.parse(o.body) : null]);
    if (u.startsWith("sched/")) return Promise.resolve(FAIL ? {ok: false, json: () => Promise.resolve({error: "写不了"})} : {ok: true, json: () => Promise.resolve({msg: "改好了"})});
    const snap = JSON.parse(JSON.stringify(D)); return Promise.resolve({ok: true, json: () => Promise.resolve(snap)}); }`);
  const w = () => run(O, `(D.sched.projects.find(p => p.id === "proj-a").w) + "|" + (/<b class="num">×([^<]*)</.exec(schedW(D.sched.projects.find(p => p.id === "proj-a")))[1])`);
  await run(O, `schedAct("wu", "proj-a")`);
  ok(run(O, "SF.some(x => x[0].startsWith('collect'))"), "写回后没立即采集");
  ok(w() === "1|1.5", "采集回来的快照还是旧权重，胶囊却退回了旧值：" + w());
  await run(O, `schedAct("wu", "proj-a")`);
  ok(run(O, "JSON.stringify(SF.filter(x => x[0] === 'sched/weight').map(x => x[1].weight))") === "[1.5,2]", "再点 + 没接着走到下一档：" + run(O, "JSON.stringify(SF)"));
  run(O, "FAIL = true"); await run(O, `schedAct("wu", "proj-a")`);
  ok(w() === "1|2", "写回失败后该退回上一次写成功的值：" + w()); ok(/改权重没成：写不了/.test(run(O, "SMSG")), "失败回执：" + run(O, "SMSG"));
  run(O, `D.sched.projects.find(p => p.id === "proj-a").w = 2`); ok(w() === "2|2" && !run(O, 'SWB.has("proj-a")'), "快照确认了还留着乐观值");
});

t("overview", "写请求经 postJSON：带 JSON 类型与 JSON 请求体，看板只认 application/json 的 POST（#158）；原来没带类型的立即采集（两页）、续租预备、探测也经它", () => {
  const O2 = ovCtx(OV, RR), P2 = mkCtx(pgSrc, PJ, {filename: "page.js"});
  const spy = `SF = []; fetch = (u, o) => { SF.push([u, o.method, o.headers?.["Content-Type"], o.body]); return new Promise(() => {}); };`;
  run(O2, spy + `collectNow(); collectNow(true); renewAct("reserve", "srv-a"); probeNow("srv-a")`);
  run(P2, spy + `collectNow()`);
  const got = C => run(C, "JSON.stringify(SF)");
  ok(got(O2) === JSON.stringify([["collect", "POST", "application/json", "{}"], ["collect?fresh=1", "POST", "application/json", "{}"],
    ["renew/srv-a/reserve", "POST", "application/json", "{}"], ["fleet/probe/srv-a", "POST", "application/json", "{}"]]), "工作台：" + got(O2));
  ok(got(P2) === JSON.stringify([["collect", "POST", "application/json", "{}"]]), "项目页：" + got(P2));
});

// 看板服务重启的那几秒（#177）：fetch 抛 TypeError（Safari「Load failed」）。页面上不露原始报错，说人话；
// 定时取数只在顶栏标连不上、提早再试，连上自动恢复；立即采集与读数据自动重试；会改状态的写请求只发一次、提示再点
const NETFAIL = `fetch = (u, o) => { SF.push(u); return Promise.reject(new TypeError("Load failed")); }`;
const NETTIMERS = `T = []; setTimeout = (f, ms) => { T.push(ms); if (ms < 15000 && !NOT) Promise.resolve().then(f); return 0; }; NOT = false;`;
ta("overview", "立即探测读响应体时断开，提示等待页面刷新；收到响应后不重发", async () => {
  const O = ovCtx(OV, RR);
  run(O, `N = 0; ${NETTIMERS}
    fetch = () => {
      if (++N === 1) return Promise.reject(new TypeError("Load failed"));
      return Promise.resolve({ok: true, json: async () => D.fleet});
    };`);
  await run(O, 'probeNow("srv-a")');
  ok(run(O, 'N === 2 && !PERR["srv-a"] && !PROBING'), "收响应前断开后没有自动恢复");
  run(O, `N = 0; R = 0; fetch = u => {
    if (u === "overview.json") { R++; return Promise.resolve({ok: true, json: async () => D}); }
    N++; return Promise.resolve({ok: true, json: async () => { throw new TypeError("Load failed"); }});
  };`);
  await run(O, 'probeNow("srv-a")');
  ok(run(O, 'N === 1 && R === 1 && PERR["srv-a"] === "探测失败：" + netText("write")'), "读响应体断开后没有刷新概览，重发了探测或露出原始错误");
  run(O, `N = 0; fetch = () => { N++; return Promise.resolve({ok: false, status: 409, json: async () => ({error: "回到内网再操作"})}); };`);
  await run(O, 'probeNow("srv-a")');
  ok(run(O, 'N === 1 && PERR["srv-a"] === "探测失败：回到内网再操作"'), "HTTP 拒绝应直接显示服务端原因");
});
ta("overview", "看板服务连不上（#177）：顶栏一句人话、提早重试、连上恢复；立即采集与读数自动重试；会改状态的写请求不重发", async () => {
  const O = ovCtx(OV, RR);
  run(O, `SF = []; NOT = true; ${NETTIMERS.replace("NOT = false;", "")} ${NETFAIL}`);
  await run(O, "poll()");
  const off = run(O, "OFFLINE");
  ok(off === run(O, 'netText("poll")') && !/Load failed/.test(off), "定时取数连不上：" + off);
  run(O, "render(true)"); const top = el(O, "top").innerHTML;
  has(top, run(O, 'netText("poll")'), "顶栏"); ok(!/Load failed/.test(top), "顶栏露了浏览器的原始报错");
  ok(run(O, "T.some(ms => ms < 15000)"), "连不上时没有提早再取：" + run(O, "JSON.stringify(T)"));
  run(O, `fetch = u => { SF.push(u); return Promise.resolve({ok: true, json: () => Promise.resolve(JSON.parse(JSON.stringify(D)))}); }`);
  await run(O, "poll()"); ok(run(O, "OFFLINE") === null, "连上了还标着连不上");
  // 立即采集：连不上两次后连上，自动重试到成
  run(O, `SF = []; NOT = false; N = 0; fetch = u => { SF.push(u); return ++N <= 2 ? Promise.reject(new TypeError("Load failed")) : Promise.resolve({ok: true, json: () => Promise.resolve(JSON.parse(JSON.stringify(D)))}); }`);
  await run(O, "collectNow()");
  ok(run(O, "SF.filter(u => u === 'collect').length") === 3 && run(O, "OFFLINE") === null && !run(O, "BUSY"), "立即采集没自动重试到成：" + run(O, "JSON.stringify([SF, OFFLINE])"));
  // 一直连不上：重试完说一句，不露原始报错
  run(O, `SF = []; ${NETFAIL}`); await run(O, "collectNow()");
  ok(run(O, "SF.length") === run(O, "NET_WAIT.length") + 1, "立即采集重试次数：" + run(O, "SF.length"));
  ok(run(O, "OFFLINE") === "立即采集失败：" + run(O, 'netText("gave")'), "重试完的说法：" + run(O, "OFFLINE"));
  // 读数据（记录）：连不上一次后连上
  run(O, `SF = []; N = 0; fetch = u => { SF.push(u); return ++N <= 1 ? Promise.reject(new TypeError("Load failed")) : Promise.resolve({ok: true, json: () => Promise.resolve({rows: [], more: false})}); }`);
  await run(O, `loadRecs("host=srv-a")`); ok(run(O, "SF.length") === 2 && !run(O, 'RECS["host=srv-a"].err'), "读记录没自动重试：" + run(O, "JSON.stringify(RECS)"));
  // 会改状态的写请求：只发一次，说法是 netText("write")
  const W = run(O, 'netText("write")');
  run(O, `SF = []; POP = null; ${NETFAIL}`);
  await run(O, `schedAct("wu", "proj-a")`);
  ok(run(O, "SF.filter(u => u.startsWith('sched/')).length") === 1, "改权重连不上时重发了：" + run(O, "JSON.stringify(SF)"));
  ok(run(O, "SMSG") === "改权重没成：" + W, "改权重的提示：" + run(O, "SMSG"));
  await run(O, `renewAct("reserve", "srv-a")`);
  ok(run(O, "SF.filter(u => u.startsWith('renew/')).length") === 1, "预备续租连不上时重发了");
  ok(run(O, 'RERR["srv-a"]') === W, "预备续租的提示：" + run(O, 'RERR["srv-a"]'));
  let err = null; try { await run(O, `regPost("rm", {items: []})`); } catch (e) { err = e; }
  ok(err && err.message === W && run(O, "SF.filter(u => u.startsWith('reg/')).length") === 1, "移除登记连不上时重发了或说法不对");
  err = null; try { await run(O, `fleetPost("renew", {})`); } catch (e) { err = e; }
  ok(err && err.message === W && run(O, "SF.filter(u => u.startsWith('fleet/op/')).length") === 1, "平台操作连不上时重发了或说法不对");
});
ta("project", "项目页：看板服务连不上（#177）时顶栏一句人话、提早重试、连上恢复；立即采集自动重试", async () => {
  const P = mkCtx(pgSrc, PJ, {filename: "page.js"});
  run(P, `SF = []; NOT = true; ${NETTIMERS.replace("NOT = false;", "")} ${NETFAIL}`);
  await run(P, "poll()");
  ok(run(P, "OFFLINE") === run(P, 'netText("poll")'), "项目页定时取数连不上：" + run(P, "OFFLINE"));
  run(P, "render()"); const app = P.document.getElementById("app").innerHTML;
  has(app, run(P, 'netText("poll")'), "项目页顶栏"); ok(!/Load failed/.test(app), "项目页露了浏览器的原始报错");
  ok(run(P, "T.some(ms => ms < 15000)"), "项目页连不上时没有提早再取");
  run(P, `SF = []; NOT = false; N = 0; fetch = u => { SF.push(u); return ++N <= 2 ? Promise.reject(new TypeError("Load failed"))
    : Promise.resolve({ok: true, json: () => Promise.resolve(u === "collect" ? JSON.parse(JSON.stringify(D)) : {})}); }`);
  await run(P, "collectNow()");
  ok(run(P, "SF.filter(u => u === 'collect').length") === 3 && run(P, "OFFLINE") === null, "项目页立即采集没自动重试到成：" + run(P, "JSON.stringify([SF, OFFLINE])"));
});
ta("project", "项目页打开时 events.json 连不上（#177，Codex 审查 #179）：不等重试，先用页面里的快照画出来；取不到的事件交给下一轮定时取数补", async () => {
  const P = mkCtx(pgSrc, PJ, {filename: "page.js"});      // 桩里的 setTimeout 不触发：要是等重试，loadEvents 永远不回来
  run(P, `SF = []; ${NETFAIL}; DONE = false; loadEvents().then(() => { DONE = true; })`);
  for (let i = 0; i < 20; i++) await Promise.resolve();
  ok(run(P, "DONE"), "events.json 连不上时 loadEvents 在等重试，首次渲染被挡住");
  run(P, `SF = []; EVJ = ${JSON.stringify(JSON.stringify(EVN))}; fetch = u => { SF.push(u); return Promise.resolve({ok: true, json: () => Promise.resolve(u.startsWith("events") ? JSON.parse(EVJ) : JSON.parse(JSON.stringify(D)))}); }`);
  await run(P, "poll()");
  ok(run(P, "SF.includes('events.json')") && run(P, "EVOK"), "快照没变时下一轮没补取事件：" + run(P, "JSON.stringify(SF)"));
});

/* ---------- 看板账号（#160）：普通用户、从公网入口来的管理员看到什么；「我的账号」「管理 → 账号」；会话失效回登录页；登录页 ---------- */
const OVU = J("overview_user.json"), OVUM = J("overview_user_member.json"), OVUP = J("overview_user_pool.json"), lgSrc = rd("login.js"), lgHtml = rd("login.html");
const USER_ME = {...ADMIN_ME, name: "amy", role: "user", role_name: "普通用户", scopes: ["read"], admin: false, ops: false, write: false, token_scopes: ["read"],
  sessions: [{id: "s1", t: NOW - 86400, seen: NOW - 60, until: NOW + 29 * 86400, entry: "lan", entry_name: "内网入口", ua: "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit Safari/605", this: true},
             {id: "s2", t: NOW - 3 * 86400, seen: NOW - 7200, until: NOW + 27 * 86400, entry: "public", entry_name: "公网入口", ua: BAD}],
  tokens: [{id: "t1", name: BAD, scopes: ["read"], t: NOW - 86400, seen: null, until: null, client_commit: "a".repeat(40), client_seen: NOW - 60, client_status: "below_minimum"}]};
const PUB_ME = {...ADMIN_ME, ops: false, entry: "public", entry_name: "公网入口"};
const side = O => el(O, "side").innerHTML;
const USERS = {people: ["ME", "OWNER", "PERSON3"], admins: 1, roles: {admin: "管理员", user: "普通用户"}, scope_info: ADMIN_ME.scope_info, users: [
  {name: "boss", fleet_person: "ME", role: "admin", t: NOW - 9 * 86400, by: "本机命令行", pw: true, setup: null, lock: null, seen: NOW - 30, sessions: [USER_ME.sessions[0]], tokens: []},
  {name: "amy", fleet_person: "OWNER", role: "user", t: NOW - 86400, by: "boss", pw: false, setup: NOW + 3600, lock: null, seen: null, sessions: [], tokens: []},
  {name: "bob", fleet_person: "PERSON3", role: "user", t: NOW - 86400, by: BAD, pw: true, setup: null, lock: NOW + 600, seen: NOW - 86400, sessions: [], tokens: [{...USER_ME.tokens[0]}]}]};
function accCtx(snap, me, hash = "#/", extra = {}) {
  const O = mkCtx(ovSrc, snap, {hash, filename: "overview.js", me, ...extra});
  O.RR = RR;
  run(O, "recs = q => ({rows: RR, gen: D.generated, more: true})");
  return O;
}

t("auth", "共有型算力池入口与旧借用池隔离（#212）：有共有池时侧栏与 #/pool 正常展示，无池快照不显示入口；项目管理抽屉不再出现旧池字样", () => {
  const O = accCtx(OV, ADMIN_ME);
  has(side(O), 'href="#/pool"', "有共有池时侧栏该有算力池入口");
  const html = page(O, "#/p/proj-a/manage");
  for (const word of ["运行资源", "池任务记录"]) ok(!html.includes(word), "项目管理抽屉不该再有" + word);
  has(html, "等着放到某台"); has(html, "从算力池撤回");
  const pp = page(O, "#/pool"); clean(pp, "管理员 #/pool");
  has(pp, "共有算力池"); has(pp, "amy、bob"); has(pp, "alpha_pool_s1");
  // 无共有池的快照（sparse）不显示侧栏算力池入口，直接访问 #/pool 给出空提示
  const S = accCtx(OVS, ADMIN_ME);
  ok(!side(S).includes('href="#/pool"'), "无共有池时侧栏不该出现算力池");
  has(page(S, "#/pool"), "没有可用的共有型算力池");
});

t("auth", "普通用户：侧栏没有组内服务器与「管理」，有「我」；没有立即采集、调度按钮与平台操作；组内页只说要管理员；记录没有平台操作这类", () => {
  ok(OVU.fleet === null && !OVU.notices.length, "users.view 没去掉组内");
  ok(OV.hostcheck && OVU.hostcheck === null, "users.view 没去掉看板服务所在机的体检（#166）");
  // #17：普通用户的快照按白名单取：别的项目的配色、正在扫的路径、别人的目录、主备细节都不给，页面照常画
  ok(Object.keys(OVU.colors).every(k => OVU.access.own.includes(k)), "配色里还有别人的项目：" + Object.keys(OVU.colors));
  ok(!("now" in (OVU.disk || {})) && !("colors" in (OVU.disk || {})) && (OVU.disk?.ours?.dirs || []).every(d => OVU.access.own.includes(d.p)), "共享盘给多了");
  ok(!OVU.server || Object.keys(OVU.server).every(k => ["role", "say"].includes(k)), "主备给多了");
  ok(!("pool_observed" in OVU) && !("host_notices" in OVU), "派单库存或主机提醒给了普通用户");
  ok(!page(accCtx(OVU, USER_ME), "#/").includes("svc-t"), "普通用户的总览不该有「看板服务」小卡");
  const U = accCtx(OVU, USER_ME);
  for (const r of ["#/", "#/host/srv-a", "#/host/srv-b", "#/p/proj-a/manage", "#/fleet", "#/fleet/srv/srv-a", "#/log", "#/me", "#/admin/users"]) {
    const h = page(U, r); clean(h + side(U), r);
    ok(!/data-sec="fleet"|data-sec="admin"|#\/admin\/users/.test(side(U)), r + "：侧栏有组内或管理");
    ok(!/data-now|data-renew|data-fop="(rpop|renewone|wiz|bpick|sync)"|data-sch="(f|p|wu|wd)"|data-reg=|data-pj="a"/.test(h), r + "：有写或平台操作的按钮");
    ok(!h.includes("请从内网入口操作，或为此管理员开启公网平台操作"), r + "：普通用户不该看到入口的说法");
  }
  has(side(U), 'href="#/me"', "侧栏的「我」");
  has(page(U, "#/fleet"), "你名下当前没有持有的服务器");
  has(page(U, "#/fleet/people"), "组内人员与账号管理只有管理员看得到");
  has(page(U, "#/admin/users"), "组内人员与账号管理只有管理员看得到");
  page(U, "#/log"); ok(!/平台操作/.test(el(U, "top").innerHTML), "记录的分类里还有平台操作");
  const m = page(U, "#/p/proj-a/manage"); has(m, "只读：你的账号没有项目的写权限"); has(m, " disabled"); ok(!m.includes("归档这个项目"), "普通用户的管理抽屉里有归档");
  const P = mkCtx(pgSrc, PJ, {filename: "page.js", me: USER_ME});
  ok(!/data-now|立即采集/.test(el(P, "app").innerHTML), "项目页还有立即采集");
});

const OWNER_ME = {...USER_ME, scopes: ["read", "project"], write: true, token_scopes: ["read", "project"],
  own: ["proj-a", "proj-old"], hosts: ["srv-a"], commit: "a1b2c3d0000", push: {set: true, kind: "bark", target: "Bark · abc…xyz"}};

t("auth", "普通用户只改自己的项目，调度保留其他项目行和份额", () => {
  const U = accCtx(OVU, OWNER_ME);
  const h = page(U, "#/"); clean(h, "普通用户总览");
  const sched = run(U, "schedCard()");
  has(sched, 'href="#/p/proj-a"');
  has(sched, " proj-b</span>");
  ok(!sched.includes('href="#/p/proj-b"') && !/data-sch="[^"]*" data-p="proj-b"/.test(sched), "有别人项目的链接或写按钮");
  ok(/data-sch="[^"]*" data-p="proj-a"/.test(sched), "自己的项目没有调度按钮");
  ok(!/data-sch="(wu|wd)"|data-now/.test(sched + el(U, "top").innerHTML), "普通用户可以改权重或采集全局");
  ok(!/data-sch="(f|uf)"/.test(sched + drw(U, "#/p/proj-a/manage")), "普通用户可以加撤优先（#16：只给管理员）");
  const paused = by => run(U, `(() => { const x = D.sched.projects[0], p0 = x.paused; x.paused = {until: null, hosts: null, by: ${JSON.stringify(by)}};
    const r = schedCard() + projSchedSec(D.projects[0]); x.paused = p0; return r; })()`);
  const theirs = paused(null), mine = paused("AMY");
  ok(!theirs.includes('data-sch="ur"'), "没记是谁（当管理员）下的暂停，普通用户有恢复按钮"); has(tips(theirs), "不是你下的，恢复找管理员");
  ok(!paused("boss").includes('data-sch="ur"'), "管理员下的暂停，普通用户有恢复按钮"); has(tips(paused("boss")), "boss 下的");
  has(mine, 'data-sch="ur" data-p="proj-a"', "自己下的暂停该能恢复"); ok(!tips(mine).includes("找管理员"), "自己下的暂停不该叫找管理员");
  // #27：只在几台单独下的暂停，不是他下的那几台不给撤销命令、写找管理员；自己下的照给命令
  const hostTip = (who, by) => run(who, `(() => { const x = D.sched.projects[0], p0 = x.paused; x.paused = {until: null, hosts: ["srv-a", "srv-b"], by: ${JSON.stringify(by)}};
    const r = flagTip("paused", x); x.paused = p0; return r; })()`);
  const ht = hostTip(U, {"srv-a": "boss", "srv-b": "amy"});
  ok(!ht.includes("--host srv-a") && ht.includes("--host srv-b"), "普通用户看到别人在那台下的暂停的撤销命令：" + ht);
  has(ht, "上的不是你下的，撤销找管理员");
  ok(!hostTip(U, {"srv-a": "AMY", "srv-b": "amy"}).includes("找管理员"), "自己在几台下的暂停不该叫找管理员");
  has(hostTip(accCtx(OV, ADMIN_ME), {"srv-a": "boss", "srv-b": null}), "--host srv-a", "管理员该有每台的撤销命令");
  const p = page(U, "#/p/proj-a/manage");
  has(p, "改了就写回看板上的 monitor.toml"); has(p, "归档这个项目");
  has(p, "<b>项目归属</b>"); ok(!p.includes('data-pj="owner"'), "普通用户可以换项目主人");
  has(el(U, "top").innerHTML, "data-now", "自己的项目可以立即采集");
  has(run(U, "archDrawer()"), 'data-pj="r" data-p="proj-old"');
  ok(!h.includes('href="#/p/proj-b"'), "主机上别人的任务有项目页链接");
});

ta("auth", "可把自己项目的清单从未授权主机搬到获授权主机", async () => {
  const snap = {...OV, fleet: null, projects: OV.projects.filter(p => p.id === "proj-b"),
    regs: OV.regs.map(r => ({...r, queues: r.queues?.filter(q => q.project === "proj-b")}))};
  const U = accCtx(snap, {...OWNER_ME, own: ["proj-b"], hosts: ["srv-a"]});
  const h = drw(U, "#/p/proj-b/manage");
  ok(/data-reg="mv" data-h="srv-old2" data-p="proj-b"\s+data-tip=/.test(h), "来源主机未授权时，「换到其他主机」应该仍可用");
  ok(!h.includes('data-reg="rm" data-h="srv-old2"'), "来源主机未授权时不能移除登记");
  run(U, `SF = []; fetch = (u, o) => { if (o?.body) SF.push([u, JSON.parse(o.body)]);
    return Promise.resolve({ok: true, json: async () => u === "overview.json" ? D : {msg: "搬好了"}}); };`);
  await run(U, 'regAct("rm", {h:"srv-old2", p:"proj-b"})');
  ok(run(U, "POP") === null, "未授权来源仍打开移除弹窗");
  await run(U, 'regAct("mv", {h:"srv-old2", p:"proj-b"})');
  ok(run(U, "POP?.to") === "srv-a", "没有选唯一获授权的目标");
  run(U, 'ME.hosts = []');
  await run(U, 'regAct("mvgo", {h:"srv-old2", p:"proj-b"})');
  ok(run(U, "SF.length") === 0, "目标权限撤销后仍发了搬动请求");
  run(U, 'ME.hosts = ["srv-a"]');
  await run(U, 'regAct("mvgo", {h:"srv-old2", p:"proj-b"})');
  const posts = run(U, "SF");
  ok(posts.length === 1 && posts[0][0] === "reg/move" && posts[0][1].to === "srv-a" &&
     posts[0][1].items[0].host === "srv-old2", "搬动请求写错：" + JSON.stringify(posts));
});

t("auth", "刚加入的普通用户有入门提示，命令行下载保留公网路径前缀", () => {
  const U = accCtx({...OVU, hosts: [], projects: []}, {...OWNER_ME, own: [], hosts: [], entry: "public"}, "#/", {path: "/pfx/"});
  const h = page(U, "#/"); has(h, "请管理员在「组内服务器 → 人员」里打开你"); has(h, "labmon project push"); ok(!h.includes("「账号」"), "入门提示还指向已并进人员的「账号」");
  run(U, "ACC.at.me = 1");
  const m = page(U, "#/me");
  has(m, "看板版本 <code>a1b2c3d</code>");
  const push = page(U, "#/me/push"); has(push, "Bark · abc…xyz"); has(push, 'data-acc="pushtest"');
  has(m, "api/client/install.sh?base=http%3A%2F%2F127.0.0.1%3A8765%2Fpfx%2F");
  has(m, "公网连接需向管理员申请客户端证书");
});

ta("auth", "同一轮概览刷新更新项目和主机权限", async () => {
  const U = accCtx(OVU, OWNER_ME);
  run(U, `fetch = () => Promise.resolve({ok: true, json: async () => ({...D,
    access: {own: [], hosts: []}, projects: [], archived: [], hosts: []})});`);
  await run(U, "poll()");
  ok(!run(U, 'canWrite("proj-a") || canUseHost("srv-a")'), "权限改动到下一轮仍没刷新");
  has(page(U, "#/"), "请管理员在「组内服务器 → 人员」里打开你");
});

t("auth", "已能用主机的普通用户不再看到「开始使用」的开主机提示（#15）", () => {
  // 没单独分配主机、但自己项目的任务已在 srv-a 上（快照给他看 srv-a）：不算没有主机
  const U = accCtx(OVU, {...OWNER_ME, hosts: []});
  const h = page(U, "#/"); ok(!h.includes("开始使用") && !h.includes("还没有能用的主机"), "有可用主机仍提示开主机");
  run(U, "ACC.at.me = 1");
  const m = page(U, "#/me"); has(m, "没有单独分配"); has(m, "你的项目还在 srv-a 上有任务");
  const A = accCtx(OVU, OWNER_ME); ok(!page(A, "#/").includes("开始使用"), "分配了主机仍有开始使用");
});

ta("auth", "管理员换主人、开主机走对应接口，普通用户可以试推", async () => {
  const U = accCtx(OVU, OWNER_ME, "#/me");
  run(U, `ACC.at.me = 1; SF = []; fetch = (u, o) => { if (o?.body) SF.push([u, JSON.parse(o.body)]);
    return Promise.resolve({ok: true, json: async () => ME}); };`);
  await run(U, 'accAct("pushtest")');
  ok(JSON.stringify(run(U, "SF")) === JSON.stringify([["api/push/test", {}]]), "试推接口写错");
  const A = accCtx(OV, ADMIN_ME, "#/admin/users");
  run(A, `ACC.at.users = 1; ACC.users = ${JSON.stringify({...USERS, hosts: [{id: "srv-a", label: "A"}]})}; SF = [];
    fetch = (u, o) => { if (o?.body) SF.push([u, JSON.parse(o.body)]);
      return Promise.resolve({ok: true, json: async () => u === "overview.json" || u.startsWith("collect") ? D : ACC.users}); };`);
  page(A, "#/admin/users/amy");
  await run(A, 'accAct("hosts", {n:"amy"})');
  has(run(A, 'directoryDrawer(route().drw)'), 'data-achost="srv-a"');
  run(A, 'POP.sel.add("srv-a")');
  await run(A, 'accAct("hostsgo", {n:"amy"})');
  page(A, "#/p/proj-a/manage");
  await run(A, 'pjAct("owner", "proj-a")');
  has(run(A, 'projDrawer("proj-a")'), 'data-owner');
  run(A, 'POP.owner = "bob"');
  await run(A, 'pjAct("ownergo", "proj-a")');
  const posts = run(A, "SF").filter(([u]) => !u.startsWith("collect"));
  ok(JSON.stringify(posts) === JSON.stringify([
    ["api/users/hosts", {name: "amy", hosts: ["srv-a"]}],
    ["project/owner", {project: "proj-a", owner: "bob"}]
  ]), "授权接口写错：" + JSON.stringify(posts));
});

t("auth", "获准管理员公网显示平台操作", () => {
  const A = accCtx(OV, {...PUB_ME, ops: true});
  const h = page(A, "#/host/srv-a");
  ok(/data-fop="rpop"|data-renew=/.test(h), "获准公网管理员缺续租按钮");
});

t("auth", "管理员从公网入口来：平台操作的按钮换成一句「请从内网入口操作，或为此管理员开启公网平台操作」，项目的写操作照常", () => {
  const A = accCtx(OV, ADMIN_ME), Pb = accCtx(OV, PUB_ME);
  const a = page(A, "#/host/srv-a"), b = page(Pb, "#/host/srv-a");
  ok(/data-fop="rpop"|data-renew=/.test(a), "内网入口的主机页没有续租按钮");
  ok(!/data-fop="rpop"|data-renew=|data-fop="renewone"/.test(b), "公网入口的主机页还有续租按钮"); has(b, "请从内网入口操作，或为此管理员开启公网平台操作");
  has(b, "data-now", "公网入口的立即采集");
  for (const r of ["#/fleet", "#/fleet/srv/srv-a", "#/fleet/people", "#/fleet/guard", "#/fleet/renew"]) {
    const h = page(Pb, r); clean(h, r);
    ok(!/data-fop="(sync|renewgo|wiz|offpick|bpick|ka|padd|pgive|pdef|pedit|logout)"|data-gdsave|data-drw="#\/fleet\/renew/.test(h), r + "：公网入口还有平台操作按钮");
  }
  has(page(Pb, "#/fleet"), "请从内网入口操作，或为此管理员开启公网平台操作"); has(page(Pb, "#/fleet/srv/srv-a"), "请从内网入口操作，或为此管理员开启公网平台操作");
  has(page(A, "#/fleet"), 'data-fop="sync"', "内网入口的核对按钮");
  ok(!side(A).includes("#/admin/users"), "管理员侧栏仍有独立账号入口"); has(side(A), 'href="#/me"');
});

t("auth", "管理员从公网入口来：账号管理只在内网入口，人员页不取账号列表、说一句不标红（#17）", () => {
  for (const [me, lan] of [[PUB_ME, false], [{...PUB_ME, ops: true}, false], [ADMIN_ME, true]]) {
    const O = accCtx(OV, me);
    run(O, `SF = []; fetch = (u, o) => { SF.push(u); return new Promise(() => {}); }`);
    const people = page(O, "#/fleet/people"), drawer = page(O, "#/fleet/people/OWNER/account");
    const got = run(O, "SF.filter(u => String(u).includes('api/users')).length");
    ok(lan ? got > 0 : got === 0, `${me.entry}：取了 ${got} 次 /api/users`);
    for (const h of [people, drawer]) {
      ok(h.includes("账号管理只在内网入口") === !lan, me.entry + "：提示不对");
      ok(lan || !h.includes('data-acc="reload"'), "公网入口还给了重试");
    }
    ok(lan || !people.includes("未开通") && people.includes("在内网入口查看"), "没取账号列表却说未开通");
  }
});

ta("overview", "与平台核对的验证码在原按钮旁，取消后不接收旧请求", async () => {
  const A = accCtx(OV, ADMIN_ME, "#/fleet");
  page(A, "#/fleet");
  run(A, `D.fleet.ops = {...D.fleet.ops, session: {logged_in: false}};
    ANSWER = null; fleetPost = () => new Promise(resolve => { ANSWER = resolve; });`);
  const start = run(A, 'fopAct("sync")');
  await run(A, 'fopAct("popx")');
  run(A, 'ANSWER({code:"已取消的验证码", tok:"t1"})');
  await start;
  ok(run(A, "FCAP === null"), "取消后接收了旧验证码");
  run(A, 'fleetPost = async () => ({code: "测试验证码", tok:"t2"})');
  await run(A, 'fopAct("sync")');
  const h = page(A, "#/fleet");
  has(h, 'role="dialog"'); has(h, "测试验证码");
  ok(run(A, "FCAP.pop && !fPanel().h"), "验证码出现在页面内的操作面板");
});

t("auth", "「我的账号」四区共用正文布局，采集只留在看板", () => {
  const U = accCtx(OV, ADMIN_ME, "#/me");
  run(U, "ACC.at.me = 1");
  for (const r of ["#/me", "#/me/security", "#/me/tokens", "#/me/push"]) {
    has(page(U, r), 'class="account-content"');
    const top = el(U, "top").innerHTML;
    has(top, 'class="account-content account-top"');
    ok(!top.includes("data-now"), r + " 不显示采集动作");
  }
  has(page(U, "#/me"), 'class="account-command"');
  page(U, "#/");
  has(el(U, "top").innerHTML, "data-now");
});

t("auth", "「我的账号」四区保留看板断连与恢复提示", () => {
  const U = accCtx(OV, ADMIN_ME, "#/me");
  run(U, "ACC.at.me = 1");
  const offline = run(U, 'netText("poll")');
  for (const r of ["#/me", "#/me/security", "#/me/tokens", "#/me/push"]) {
    run(U, 'OFFLINE = netText("poll")');
    page(U, r);
    has(el(U, "top").innerHTML, offline, r);
    run(U, "OFFLINE = null; render(true)");
    ok(!el(U, "top").innerHTML.includes(offline), r + " 恢复后清除提示");
  }
});

t("auth", "「我的账号」：角色、入口、能做什么；改口令表单（口令管理认得出）；登录的浏览器（这个浏览器不给吊销）与令牌；转义", () => {
  const U = accCtx(OVU, USER_ME, "#/me");
  run(U, "ACC.at.me = 1");
  const h = page(U, "#/me"); clean(h, "#/me");
  has(h, "普通用户"); has(h, "从内网入口来"); has(h, "他人服务器、人员管理与平台操作只有管理员看得到"); has(el(U, "top").innerHTML, "我的账号");
  has(h, "下载安装脚本"); has(h, 'data-acc="open-pw"'); has(h, 'data-acc="out"'); ok(!h.includes("acpo") && !h.includes("acn"), "概览展开了表单");
  let sec = page(U, "#/me/security"); has(sec, "Safari · macOS"); has(sec, "这个浏览器");
  ok(!sec.includes('data-id="s1"') && !sec.includes("acpo"), "当前会话不吊销，口令表单按需展开");
  run(U, 'accAct("open-pw")');
  sec = page(U, "#/me/security"); has(sec, 'class="pop keep"'); has(sec, 'autocomplete="current-password"'); has(sec, 'autocomplete="new-password"'); has(sec, 'method="post"');
  let tok = page(U, "#/me/tokens"); has(tok, "低于最低版本"); has(tok, "aaaaaaa"); has(tok, "运行 <code>labmon update</code>");
  has(tok, BADESC, "令牌名转义"); has(tok, 'data-acc="rv"');
  run(U, 'accAct("open-tok")');
  tok = page(U, "#/me/tokens");
  has(tok, 'type="radio" name="acs" data-pop-field="toks" value="read" checked', "普通用户只能建只读令牌");
  has(tok, "<b>只读</b>", "令牌权限单选行应显示标签");
  ok(!tok.includes('value="admin"'), "普通用户的选项里有 admin");
  const tb = run(U, `(() => { const n = navModel(route()); return tabbar(route()) + morePanel(route()); })()`);
  has(tb, 'href="#/me"', "窄屏「更多」里的「我」");
});

ta("auth", "「我的账号」：改口令两遍不一样不发；建令牌发 /api/tokens/add、原文只显示一次（复制、知道了），离开这页就收起；退出发 /api/logout 后回登录页", async () => {
  const U = accCtx(OVU, USER_ME, "#/me");
  run(U, `ACC.at.me = 1; SF = []; fetch = (u, o) => { SF.push([u, o?.body ? JSON.parse(o.body) : null]);
    if (u === "api/tokens/add") return Promise.resolve({ok: true, status: 200, json: () => Promise.resolve({token: "lm_原文只此一次", id: "t9", name: "部署", scopes: ["read"], until: null, msg: "建好了"})});
    if (u === "api/me") return Promise.resolve({ok: true, status: 200, json: () => Promise.resolve(ME)});
    return Promise.resolve({ok: true, status: 200, json: () => Promise.resolve({msg: "好了"})}); }`);
  page(U, "#/me/security"); run(U, 'accAct("open-pw")');
  el(U, "acpo").value = "old-pass"; el(U, "acpn").value = "new-pass-1"; el(U, "acpn2").value = "new-pass-2";
  await run(U, `accAct("pw")`);
  ok(run(U, "SF.length") === 0, "两遍不一样还发了"); has(page(U, "#/me/security"), "两遍新口令不一样");
  el(U, "acpo").value = "old-pass"; el(U, "acpn").value = "new-pass-1"; el(U, "acpn2").value = "new-pass-1";
  await run(U, `accAct("pw")`);
  ok(run(U, `JSON.stringify(SF[0])`) === JSON.stringify(["api/password", {old: "old-pass", new: "new-pass-1"}]), "改口令：" + run(U, "JSON.stringify(SF[0])"));
  page(U, "#/me/tokens"); run(U, 'accAct("open-tok")');
  el(U, "acn").value = "部署"; run(U, 'POP.draft.toks = "read"');
  await run(U, `accAct("tok")`);
  ok(run(U, `JSON.stringify(SF.find(x => x[0] === "api/tokens/add")[1])`) === JSON.stringify({name: "部署", scopes: ["read"]}), "建令牌的请求体");
  const h = page(U, "#/me/tokens"); has(h, "lm_原文只此一次"); has(h, 'data-copy="lm_原文只此一次"'); has(h, 'data-acc="showx"');
  page(U, "#/me/security"); ok(!page(U, "#/me/tokens").includes("lm_原文只此一次"), "切换选项卡后原文还在");
  const tId = USER_ME.tokens[0].id;
  await run(U, `accAct("rv", {id:${JSON.stringify(tId)}})`);
  has(page(U, "#/me/tokens"), "确认吊销");
  ok(run(U, 'SF.every(x => x[0] !== "api/revoke")'), "未确认就提交了吊销");
  await run(U, `accAct("revokego", {id:${JSON.stringify(tId)}, where:"tok"})`);
  has(run(U, 'SF.map(x => x[0]).join(",")'), "api/revoke");
  run(U, `toLogin = () => { WENT = 1; }`);
  await run(U, `accAct("out")`);
  ok(run(U, `SF.at(-1)[0]`) === "api/logout" && run(U, "WENT") === 1, "退出没回登录页");
});

ta("auth", "令牌创建响应晚到时，切走再回来也不显示原文", async () => {
  const U = accCtx(OVU, USER_ME, "#/me/tokens");
  run(U, `ACC.at.me = 1; ANSWER = null; fetch = u => u === "api/tokens/add"
    ? new Promise(resolve => { ANSWER = resolve; })
    : Promise.resolve({ok: true, status: 200, json: async () => ME});`);
  page(U, "#/me/tokens"); run(U, 'accAct("open-tok")');
  el(U, "acn").value = "脚本"; run(U, 'POP.draft.toks = "read"');
  const req = run(U, 'accAct("tok")');
  page(U, "#/me/security"); page(U, "#/me/tokens");
  run(U, 'ANSWER({ok: true, status: 200, json: async () => ({name: "脚本", token: "lm_晚到原文"})})');
  await req;
  ok(!page(U, "#/me/tokens").includes("lm_晚到原文"), "导航后还显示晚到的原文");
});

ta("auth", "关闭口令浮窗后，迟到的错误不会显示在另一个浮窗", async () => {
  const U = accCtx(OVU, USER_ME, "#/me/tokens");
  run(U, `ACC.at.me = 1; ANSWER = null; fetch = u => u === "api/password"
    ? new Promise(resolve => { ANSWER = resolve; })
    : Promise.resolve({ok: true, json: async () => ME});`);
  page(U, "#/me/tokens"); run(U, 'accAct("open-pw")');
  el(U, "acpo").value = "old-pass"; el(U, "acpn").value = el(U, "acpn2").value = "new-pass";
  const req = run(U, 'accAct("pw")');
  await run(U, 'fopAct("popx")');
  run(U, 'accAct("open-tok")');
  run(U, 'ANSWER({ok: false, json: async () => ({error: "迟到的口令错误"})})');
  await req;
  const h = page(U, "#/me/tokens");
  has(h, 'data-accf="tok"');
  ok(!h.includes("迟到的口令错误") && !h.includes('data-accf="pw"'), "关闭后的请求改变了当前浮窗");
});

t("auth", "「我的账号」能做什么说人话、不列作用域名；项目归属写人员名、账号放悬停（#15）", () => {
  const U = accCtx(OVU, OWNER_ME, "#/me"); run(U, "ACC.at.me = 1");
  const h = page(U, "#/me"); has(h, "可以查看能用的主机、本人共享盘与自己持有的服务器，管理自己的项目"); ok(!/能做：|只读、项目/.test(h), "还在列作用域名");
  has(h, '<div class="acc-name"><b>amy</b>', "角色、入口的标签与名字同一行");
  const A = accCtx(OV, ADMIN_ME, "#/me"); run(A, "ACC.at.me = 1");
  has(page(A, "#/me"), "可以管理全部项目、主机与账号");
  ok(run(A, 'meCan({account: false, scopes: ["deploy", "read"]}, ME.scope_info)').startsWith("这个令牌能做：看页面与数据"), "令牌打开的说法");
  run(A, `ACC.at.users = 1; ACC.users = ${JSON.stringify(USERS)}`);
  const m = page(A, "#/p/proj-a/manage"); has(m, ">OWNER</b>", "项目归属写人员名"); has(m, "看板账号 amy");
  run(A, 'popToggle("owner", "proj-a", {owner: "amy"})'); has(page(A, "#/p/proj-a/manage"), ">OWNER（账号 amy）</option>");
  const N = accCtx(OV, ADMIN_ME, "#/p/proj-a/manage"); run(N, "ACC.at.users = 1; ACC.users = null");
  has(page(N, "#/p/proj-a/manage"), ">amy</b>", "读不到账号库时写账号名");
});

t("auth", "「管理 → 账号」：自己不能重置、删除，最后一个管理员撤不掉；等设口令、锁定的状态；确认弹窗；转义", () => {
  const A = accCtx(OV, ADMIN_ME, "#/admin/users");
  run(A, `ACC.at.users = 1; ACC.users = ${JSON.stringify(USERS)}`);
  const h = page(A, "#/admin/users"); clean(h, "#/admin/users");
  has(el(A, "top").innerHTML, "组内服务器"); ok(!h.includes("添加账号")); has(run(A, 'accountCardStatus({...ACC.users.users.find(u => u.name === "bob"), lock: null}, ACC.users.roles)'), "1 个待更新");
  has(h, "等设口令"); has(h, "已锁定"); has(h, 'data-drw="#/fleet/people/OWNER"');
  ok(!h.includes('data-acc="rm"') && !h.includes('data-accf="add"'), "名单展开了添加表单或危险操作");
  const boss = page(A, "#/admin/users/boss");
  has(boss, 'data-acc="role" data-n="boss" disabled', "最后一个管理员的撤销该置灰");
  ok(!boss.includes('data-acc="reset"') && !boss.includes('data-acc="rm"'), "自己不该有重置、删除");
  has(page(A, "#/admin/users/amy"), 'data-acc="rm" data-n="amy"');
  run(A, `accAct("rm", {n: "amy"})`);
  const p = page(A, "#/admin/users/amy"); has(p, "删除账号 amy"); has(p, 'data-acc="rmgo" data-n="amy"');
  const U = accCtx(OVU, USER_ME, "#/admin/users");
  ok(!/data-acc=/.test(page(U, "#/admin/users")), "普通用户在账号页有按钮");
});

t("auth", "人员目录：同一人只一张卡，双入口切换详情，冲突与只读权限分别显示", () => {
  const L = {...USERS, users: USERS.users.map(u => u.name === "amy" ? {...u, fleet_person: "OWNER"}
    : u.name === "bob" ? {...u, fleet_person: null, fleet_person_error: "人员对应多个账号"} : {...u, fleet_person: "candidate"})};
  const A = accCtx(OV, ADMIN_ME, "#/admin/users");
  run(A, `ACC.at.users = 1; ACC.users = ${JSON.stringify(L)};`);
  const list = page(A, "#/admin/users");
  has(list, "OWNER"); has(page(A, "#/admin/users/bob"), "暂无可确认的组内人员");
  has(list, '<span class="pk">看板账号</span><span class="mut">未开通</span>'); has(list, "暂无用量记录");
  ok((list.match(/data-drw="#\/fleet\/people\/OWNER"/g) || []).length === 1, "同一人重复列在目录");
  ok(!list.includes("candidate"), "仅有名字的候选人员被当成已有人员");
  const detail = page(A, "#/admin/users/amy");
  has(detail, "账号与权限"); has(detail, "服务器与用量"); has(detail, "访问权限");
  run(A, 'ACC.show = {page:"users", k:"code", where:"list", name:"amy", v:"测试设置码原文"}');
  const usage = page(A, "#/admin/users/amy/usage");
  has(usage, "近 30 天"); ok(!usage.includes("测试设置码原文"), "切换区仍显示设置码");
  ok(!page(A, "#/admin/users/amy").includes("测试设置码原文"), "切回后仍显示设置码");
  const accountPanel = page(A, "#/fleet/people/OWNER/account");
  const usagePanel = page(A, "#/fleet/people/OWNER");
  has(accountPanel, "直接归属项目");
  const header = html => html.match(/<header class="dh">[\s\S]*?<\/header>/)?.[0];
  ok(header(accountPanel) && header(accountPanel) === header(usagePanel), "分区切换改变了人员抽屉标题");
  ok(/<\/header><nav class="ftabs drawer-tabs"/.test(usagePanel), "人员抽屉分区导航随正文滚动");
  const P = accCtx(OV, PUB_ME, "#/fleet/people/OWNER");
  run(P, `ACC.at.users = 1; ACC.users = ${JSON.stringify(L)};`);
  const pub = page(P, "#/fleet/people/OWNER");
  has(pub, "账号与权限"); ok(!pub.includes('data-fop="pedit"'), "公网能改人员设置");
  const R = accCtx(OV, {...ADMIN_ME, scopes: ["read"], via: "token", account: false, ops: false}, "#/fleet/people/OWNER");
  const readonly = page(R, "#/fleet/people/OWNER");
  has(readonly, "近 30 天"); ok(!readonly.includes("账号与权限"), "只读令牌能看账号区");
  ok(!page(R, "#/admin/users/amy").includes("访问权限"), "只读令牌直达账号详情");
  const N = accCtx({...OV, fleet: null}, ADMIN_ME, "#/admin/users/amy");
  run(N, `ACC.at.users = 1; ACC.users = ${JSON.stringify(L)};`);
  const noFleet = page(N, "#/admin/users/amy");
  has(noFleet, "访问权限"); has(noFleet, "可以管理已登记人员的账号"); has(noFleet, "labmon user person");
});

t("auth", "从组内服务器和共享盘打开人员抽屉，离开账号区会清除设置码", () => {
  for (const origin of ["#/fleet", "#/fleet/disk"]) {
    const A = accCtx(OV, ADMIN_ME, origin);
    page(A, origin);
    run(A, `ACC.at.users = 1; ACC.users = ${JSON.stringify(USERS)};
      ACC.users.users.push(${JSON.stringify(USERS.users.find(u => u.name === "amy"))});`);
    has(page(A, "#/fleet/people/OWNER/account"), "访问权限");
    run(A, 'ACC.show = {page:"users", k:"code", where:"list", name:"amy", v:"来自人员抽屉的设置码"}');
    has(page(A, "#/fleet/people/OWNER/account"), "来自人员抽屉的设置码");
    page(A, "#/fleet/people/OWNER");
    ok(!page(A, "#/fleet/people/OWNER/account").includes("来自人员抽屉的设置码"), "切回仍显示原文");
    run(A, 'ACC.show = {page:"users", k:"code", where:"list", name:"amy", v:"来自人员抽屉的设置码"}');
    page(A, origin);
    ok(!page(A, "#/fleet/people/OWNER/account").includes("来自人员抽屉的设置码"), "关了再开仍显示原文");
  }
});

ta("auth", "从组内人员开通账号，设置码跟随原抽屉并在离开后清空", async () => {
  const A = accCtx(OV, ADMIN_ME, "#/fleet/people/OWNER");
  run(A, `ACC.at.users = 1; ACC.users = ${JSON.stringify({...USERS, users: USERS.users.filter(u => u.name !== "amy"), person_choices: ["OWNER"]})}; SF = [];
    fetch = (url, opts) => {
      if (url === "api/users/add") {
        SF.push(JSON.parse(opts.body));
        ACC.users.users.push(${JSON.stringify(USERS.users.find(u => u.name === "amy"))});
        return Promise.resolve({ok:true, json:async () => ({code:"开户设置码"})});
      }
      return Promise.resolve({ok:true, json:async () => ACC.users});
    };`);
  has(page(A, "#/fleet/people/OWNER"), "开通账号");
  run(A, 'accAct("open-onboard", {n:"OWNER"})');
  const opened = page(A, "#/fleet/people/OWNER");
  has(opened, "<b>OWNER</b>"); has(opened, 'data-accf="onboard"'); has(opened, 'class="pop keep"');
  el(A, "acun").value = "amy";
  await run(A, 'accAct("onboard")');
  ok(run(A, "SF[0].person") === "OWNER");
  has(page(A, "#/fleet/people/OWNER"), "开户设置码");
  page(A, "#/fleet/people/OWNER/account");
  ok(!page(A, "#/fleet/people/OWNER").includes("开户设置码"), "切走后设置码仍显示");
});

ta("auth", "人员详情：重置口令确认后发 /api/users/reset 并显示新的设置码；出错写在弹窗里", async () => {
  const A = accCtx(OV, ADMIN_ME, "#/admin/users");
  run(A, `ACC.at.users = 1; ACC.users = ${JSON.stringify(USERS)}; SF = []; FAIL = false; fetch = (u, o) => { SF.push([u, o?.body ? JSON.parse(o.body) : null]);
    if (FAIL) return Promise.resolve({ok: false, status: 409, json: () => Promise.resolve({error: "删不掉：" + ${JSON.stringify(BAD)}})});
    if (u === "api/users/add" || u === "api/users/reset") return Promise.resolve({ok: true, status: 200, json: () => Promise.resolve({code: "ABCD-EFGH-JKLM", msg: "好了"})});
    if (u === "api/users") return Promise.resolve({ok: true, status: 200, json: () => Promise.resolve(ACC.users)});
    return Promise.resolve({ok: true, status: 200, json: () => Promise.resolve(ME)}); }`);
  page(A, "#/admin/users/bob");
  run(A, `accAct("reset", {n: "bob"})`); has(page(A, "#/admin/users/bob"), "重置 bob 的口令");
  await run(A, `accAct("resetgo", {n: "bob"})`);
  ok(run(A, `JSON.stringify(SF.find(x => x[0] === "api/users/reset")[1])`) === '{"name":"bob"}');
  let h = page(A, "#/admin/users/bob"); has(h, "bob 的设置码"); ok(!run(A, "POP"), "成功后弹窗没关");
  page(A, "#/admin/users/amy");
  ok(!page(A, "#/admin/users/bob").includes("ABCD-EFGH-JKLM"), "关闭详情后设置码还在");
  page(A, "#/admin/users/amy");
  run(A, `FAIL = true; accAct("rm", {n: "amy"})`);
  await run(A, `accAct("rmgo", {n: "amy"})`);
  h = page(A, "#/admin/users/amy"); has(h, "删除账号 amy"); has(h, "删不掉：" + BADESC, "错误写在弹窗里、转义");
});

ta("auth", "会话失效：取数据拿到 401 就回登录页（带着视图）；项目页从 p/<id>/ 回看板根的登录页", async () => {
  const fx = () => Promise.resolve({ok: false, status: 401, json: () => Promise.resolve({error: "登录已过期或已退出，请重新登录"})});
  const O = mkCtx(ovSrc, OV, {hash: "#/host/srv-a", filename: "overview.js", fetch: fx});
  await run(O, `fetch("/overview.json")`);
  ok(O.location.href === "http://127.0.0.1:8765/login?next=#/host/srv-a", "工作台：" + O.location.href);
  const P = mkCtx(pgSrc, PJ, {filename: "page.js", fetch: fx, hash: "#x"});
  P.location.pathname = "/p/proj-a/"; P.location.href = "http://127.0.0.1:8765/p/proj-a/#x";
  await run(P, `fetch("snapshot.json")`);
  ok(P.location.href === "http://127.0.0.1:8765/login?next=p%2Fproj-a%2F#x", "项目页：" + P.location.href);
  const F = mkCtx(ovSrc, OV, {filename: "overview.js", fetch: fx, protocol: "file:"});
  await run(F, `fetch("x")`); ok(F.location.href.startsWith("file:"), "file:// 打开的不该跳");
});

t("auth", "登录页：一个 <script>、能编译；有账号时是登录，名字口令带 autocomplete；没有账号时直接是设口令，写着怎么建第一个管理员", () => {
  new vm.Script(lgSrc, {filename: "login.js"});
  ok((lgHtml.match(/<script/g) || []).length === 1, "登录页不止一个 <script>");
  const L = mkCtx(lgSrc, {empty: false, entry: "lan", entry_name: "内网入口", next: "p/proj-a/", pw_min: 8, lock_min: 15, fails: 5}, {filename: "login.js", hash: ""});
  let h = el(L, "app").innerHTML; clean(h, "登录页");
  has(h, 'autocomplete="username"'); has(h, 'autocomplete="current-password"'); has(h, "内网入口"); has(h, 'method="post"'); ok(!h.includes("labmon user add &lt;"), "有账号还写着建第一个");
  const E = mkCtx(lgSrc, {empty: true, entry: "public", entry_name: "公网入口<b>", next: "", pw_min: 8, lock_min: 15, fails: 5}, {filename: "login.js", hash: ""});
  h = el(E, "app").innerHTML; clean(h, "登录页（没有账号）");
  has(h, "labmon user add &lt;账号&gt; --person &lt;人员名&gt; --admin"); has(h, 'autocomplete="one-time-code"'); has(h, "公网入口&lt;b&gt;");
});

ta("auth", "登录页：提交发 api/login，成功回 next（带着视图）；失败写原因、清掉口令", async () => {
  const L = mkCtx(lgSrc, {empty: false, entry: "lan", entry_name: "内网入口", next: "p/proj-a/", pw_min: 8, lock_min: 15, fails: 5}, {filename: "login.js", hash: "#/host/srv-a"});
  L.location.href = "http://127.0.0.1:8765/login?next=p%2Fproj-a%2F#/host/srv-a";
  run(L, `SF = []; OK = false; fetch = (u, o) => { SF.push([u, JSON.parse(o.body)]);
    return Promise.resolve(OK ? {ok: true, status: 200, json: () => Promise.resolve({name: "boss", msg: "已登录"})} : {ok: false, status: 401, json: () => Promise.resolve({error: "名字或口令不对"})}); }`);
  el(L, "name").value = "boss"; el(L, "pw").value = "wrong";
  fire(L, "submit", {closest: () => null});
  await new Promise(r => setImmediate(r));
  ok(run(L, "JSON.stringify(SF[0])") === JSON.stringify(["api/login", {name: "boss", password: "wrong"}]), "请求：" + run(L, "JSON.stringify(SF)"));
  ok(el(L, "msg").textContent === "名字或口令不对" && el(L, "pw").value === "", "失败：" + el(L, "msg").textContent);
  // 看板服务正在重启（#177）：不自动重发（输错要计数、设置码只能用一次），说一句再点，不露浏览器的原始报错
  run(L, `F0 = fetch; NF = 0; fetch = () => { NF++; return Promise.reject(new TypeError("Load failed")); }`); el(L, "pw").value = "right-pass";
  fire(L, "submit", {closest: () => null});
  for (let i = 0; i < 20; i++) await Promise.resolve();
  ok(run(L, "NF") === 1 && el(L, "msg").textContent === run(L, "netText()") + "，过几秒再点一次" && !run(L, "document.getElementById('go').disabled"),
     "连不上时：" + run(L, "NF") + " 次，" + el(L, "msg").textContent);
  run(L, "fetch = F0; OK = true"); el(L, "pw").value = "right-pass";
  fire(L, "submit", {closest: () => null});
  await new Promise(r => setImmediate(r));
  ok(L.location.href === "http://127.0.0.1:8765/p/proj-a/#/host/srv-a", "成功后去：" + L.location.href);
});

// 反代在前面加了路径前缀（TX 的 nginx：/<前缀>/ → 公网入口，#165）：页面上的请求、链接与跳转都是相对地址，留在前缀下
ta("auth", "路径前缀（#165）：工作台在 /pfx/、项目页在 /pfx/p/<id>/ 时，取数、写请求、iframe、链接、回登录页与登录后的跳转都留在 /pfx/ 下", async () => {
  const base = "http://127.0.0.1:8765", at = (C, u) => new URL(u, C.location.href).pathname;
  const O = mkCtx(ovSrc, OV, {path: "/pfx/", filename: "overview.js"});
  run(O, `SF = []; fetch = (u, o) => { SF.push(u); return new Promise(() => {}); }`);
  run(O, `collectNow(); collectNow(true); renewAct("reserve", "srv-a"); probeNow("srv-a"); poll()`);
  const sent = run(O, "SF");
  ok(sent.length >= 5 && sent.every(u => at(O, u).startsWith("/pfx/")), "工作台的请求出了前缀：" + JSON.stringify(sent));
  run(O, `CE = document.createElement; FRS = []; document.createElement = t => { const e = CE(t); if (t === "iframe") FRS.push(e); return e; }`);
  page(O, "#/p/proj-a");
  const fr = run(O, `FRS[0]?.src || ""`);
  ok(at(O, fr) === "/pfx/p/proj-a/", "嵌的项目页：" + fr);
  const P = mkCtx(pgSrc, PJ, {path: "/pfx/p/proj-a/", filename: "page.js"}); P.EVD = EVN; run(P, "EV = EVD; render()");
  const top = app(P).split('class="project-feedback"')[0];
  const hrefs = [...top.matchAll(/href="([^"#][^"]*)"/g)].map(m => m[1]);
  ok(hrefs.length >= 2 && hrefs.every(h => at(P, h) === "/pfx/"), "项目页的链接出了前缀：" + JSON.stringify(hrefs));
  const fx = () => Promise.resolve({ok: false, status: 401, json: () => Promise.resolve({error: "登录已过期或已退出，请重新登录"})});
  const O4 = mkCtx(ovSrc, OV, {path: "/pfx/", hash: "#/host/srv-a", filename: "overview.js", fetch: fx});
  await run(O4, `fetch("overview.json")`);
  ok(O4.location.href === base + "/pfx/login?next=#/host/srv-a", "工作台回登录页：" + O4.location.href);
  const P4 = mkCtx(pgSrc, PJ, {path: "/pfx/p/proj-a/", hash: "#x", filename: "page.js", fetch: fx});
  await new Promise(r => setImmediate(r)); P4.location.href = base + "/pfx/p/proj-a/#x";   // 装进来时自己取的那次已经跳过一回（桩里 href 跟着变，浏览器里跳转前不变）
  await run(P4, `fetch("snapshot.json")`);
  ok(P4.location.href === base + "/pfx/login?next=p%2Fproj-a%2F#x", "项目页回登录页：" + P4.location.href);
  const L = mkCtx(lgSrc, {empty: false, entry: "public", entry_name: "公网入口", next: "p/proj-a/", pw_min: 8, lock_min: 15, fails: 5},
                  {path: "/pfx/login", filename: "login.js", hash: "#/host/srv-a"});
  run(L, `SF = []; fetch = (u, o) => { SF.push(u); return Promise.resolve({ok: true, status: 200, json: () => Promise.resolve({name: "boss", msg: "已登录"})}); }`);
  el(L, "name").value = "boss"; el(L, "pw").value = "right-pass";
  fire(L, "submit", {closest: () => null});
  await new Promise(r => setImmediate(r));
  ok(new URL(run(L, "SF[0]"), base + "/pfx/login").pathname === "/pfx/api/login", "登录请求：" + run(L, "SF[0]"));
  ok(L.location.href === base + "/pfx/p/proj-a/#/host/srv-a", "登录后去：" + L.location.href);
});

/* ---------- 旧借用型算力池 UI 已彻底移除（#212 PR 1） ---------- */
t("overview", "旧借用型算力池 UI 已移除（#212）：组内页、服务器抽屉、主机页、我的账号与侧栏不再出现入池/收回/旧池抽屉", () => {
  const C = ovCtx(OV, RR, "#/fleet");
  const fl = page(C, "#/fleet"); clean(fl, "组内页");
  for (const bad of ['data-drw="#/fleet/pool"', "台入池", "没有服务器入池", ">入池</span>"]) {
    ok(!fl.includes(bad), "组内页残留旧池 UI：" + bad);
  }
  ok(!run(C, "morePanel(route())").includes("#/fleet/pool"), "「更多」里不该再有旧算力池子项");
  for (const id of ["srv-a", "srv-g", "srv-h"]) {
    const dr = page(C, "#/fleet/srv/" + id); clean(dr, "抽屉 " + id);
    ok(!dr.includes('data-lend=') && !dr.includes("入池设置") && !dr.includes("算力池借着"), `${id} 抽屉残留旧入池/收回 UI`);
  }
  const hp = page(C, "#/host/srv-a"); clean(hp, "主机页");
  ok(!hp.includes('data-lend=') && !hp.includes("入池设置"), "自用主机页残留旧入池 UI");
  // 直接访问已删除的 #/fleet/pool 安全降级，不报错也不打开抽屉
  page(C, "#/fleet/pool"); ok(!run(C, "DKEY"), "访问已删除的 #/fleet/pool 不该打开抽屉");
  // 我的账号页不再渲染旧入池「我的服务器」
  const U = accCtx(OVU, OWNER_ME, "#/me"); run(U, "ACC.at.me = 1");
  const m = page(U, "#/me"); clean(m, "我的账号");
  ok(!m.includes("我的服务器") && !m.includes("入池设置"), "我的账号页残留旧入池节");
});
ta("auth", "普通用户入口开放（#211，无池数据时安全降级）：我的资源侧栏、本人服务器/抽屉、本人共享盘与防泄密；poll 带 D.fleet 不误刷", async () => {
  const MEMBER_ME = {...OWNER_ME, fleet_person: "OWNER", pool_member: false};
  const U = accCtx(OVUM, MEMBER_ME);
  // 1. 侧栏有「我的资源」两项（我的服务器、共享盘），无池数据时安全降级不报错，不含「人员」「保活守护」
  const sd = side(U);
  has(sd, 'data-sec="myres"'); has(sd, "我的资源"); has(sd, 'href="#/fleet"'); has(sd, 'href="#/fleet/disk"');
  ok(!sd.includes("#/fleet/people") && !sd.includes("#/fleet/guard") && !sd.includes("#/fleet/pool"), "普通用户侧栏出现了人员、保活守护或旧算力池");
  // 2. 我的服务器页：含本人 srv-b、保留借用人名 ME、屏蔽对方项目 proj-b 与他人服务器、无平台操作与旧入池按钮
  const fl = page(U, "#/fleet"); clean(fl, "普通用户我的服务器");
  has(fl, "OWNER"); has(fl, "借给 ME");
  for (const bad of ["PERSON3", "PERSON4", "PERSON7", "srv-d", "srv-e", "srv-h", "proj-b", 'data-fop="sync"', 'data-drw="#/fleet/renew"', "入池设置"]) {
    ok(!fl.includes(bad), "我的服务器页泄露或多余：" + bad);
  }
  // 3. 本人服务器抽屉：可打开且不含平台操作按钮/开机参数/人员链接/旧入池按钮
  const drw = (X, r) => { page(X, r); return run(X, 'DKEY ? document.getElementById("drwp").innerHTML : ""'); };
  const srv = drw(U, "#/fleet/srv/srv-b"); clean(srv, "普通用户本人服务器抽屉");
  has(srv, "OWNER"); has(srv, "已借给 ME"); has(srv, "<span>借用人</span><span>ME</span>");
  for (const bad of ['data-fop="wiz"', 'data-probe=', 'data-drw="#/fleet/people', "开机参数", "proj-b", 'data-lend=']) {
    ok(!srv.includes(bad), "本人服务器抽屉泄露或多余：" + bad);
  }
  // 4. 共享盘页：仅总盘 + 本人 OWNER 目录，无他人目录与归属表单
  const dk = page(U, "#/fleet/disk"); clean(dk, "普通用户共享盘");
  has(dk, "OWNER"); has(dk, "owner/archives"); has(dk, "其他已用");
  for (const bad of ["PERSON3", "stray_dir", 'data-disk="own"', "设置提醒线"]) {
    ok(!dk.includes(bad), "普通用户共享盘泄露或多余：" + bad);
  }
  // 5. #/fleet/pool 访问安全降级不报错
  clean(page(U, "#/fleet/pool"), "普通用户访问旧 #/fleet/pool 安全降级");
  ok(!run(U, "DKEY"), "访问已删除的 #/fleet/pool 不该打开抽屉");
  // 6. #/fleet/people 拦截
  has(page(U, "#/fleet/people"), "组内人员与账号管理只有管理员看得到");
  // 7. poll() 角色变化判定回归：普通用户收到含 D.fleet(scope=self) 的快照不 reload；收到管理员快照（无 access）触发 reload
  run(U, `RELOADED = 0; location.reload = () => { RELOADED++; };
    fetch = () => Promise.resolve({ok: true, json: async () => JSON.parse(JSON.stringify(D))});`);
  await run(U, "poll()");
  ok(run(U, "RELOADED") === 0, "普通用户收到含本人 D.fleet 的快照误触发了 location.reload");
  run(U, `fetch = () => Promise.resolve({ok: true, json: async () => (${JSON.stringify(OV)})});`);
  await run(U, "poll()");
  ok(run(U, "RELOADED") === 1, "普通用户变成管理员快照时未触发 location.reload");
  // 管理员收到降级后的普通用户快照（fleet.ops 缺失）应触发 reload
  const A = accCtx(OV, ADMIN_ME);
  run(A, `RELOADED = 0; location.reload = () => { RELOADED++; };
    fetch = () => Promise.resolve({ok: true, json: async () => (${JSON.stringify(OVUM)})});`);
  await run(A, "poll()");
  ok(run(A, "RELOADED") === 1, "管理员被降级为普通用户快照时未触发 location.reload");
});
t("reg", "交给算力池的清单（#24）：项目抽屉里一行「算力池 · 等着放到某台」、能撤回；接上卡不搬池里的；池成员能撤", () => {
  const C = ovCtx(OV, RR);
  const h = page(C, "#/p/proj-a/manage"); has(h, "算力池"); has(h, "等着放到某台"); has(h, "jobs/p.txt"); has(h, "从算力池撤回");
  const i = h.indexOf("等着放到某台"); ok(i > 0 && i < h.indexOf("主机清单里没有"), "池里的该排在已不在用的前面");
  run(C, "D.regs[0].queues = []; D.hosts[0].status.queues = []; ADOPT = null");
  ok(!run(C, 'hostPage("srv-a")').includes("jobs/p.txt"), "接上卡不该列池里的清单");
  const U = accCtx(OVU, {...OWNER_ME, pool_member: true}); ok(run(U, 'canUseHost("pool")'), "池成员该能用 pool");
  ok(!run(accCtx(OVU, OWNER_ME), 'canUseHost("pool")'), "不是池成员不该能用 pool");
});
t("project", "项目页的算力池（#24）：顶栏一个「算力池」，车道写在池里等着，资源小图不画它", () => {
  const C = pjCtx(PJ, EVN), h = app(C);
  has(h, "算力池"); has(tips(h), "交给算力池、还没放到哪台的清单");
  ok(run(C, 'laneState(D.lanes.find(l => l.host === "pool"))') === "等待中：在算力池里等着放到某台：池里主机都放不下（srv-h：显存不够）（已等 60 分钟）", "池车道的状态：原因与已等多久（#89）");
  ok(run(C, 'laneName(D.lanes.find(l => l.host === "pool"), D.hosts)') === "算力池 车道 1 · p", "池车道名前写算力池");
  ok(run(C, 'laneName(D.lanes.find(l => l.host === "pool"), [{id: "srv-a"}, {id: "pool", pool: true, label: "算力池"}])') === "算力池 车道 1 · p", "只有一台真主机时池车道也写算力池");
  ok(run(C, 'laneName(D.lanes.find(l => l.host === "srv-a"), [{id: "srv-a"}, {id: "pool", pool: true, label: "算力池"}])') === "车道 1 · q1", "算力池不算进几台主机");
  has(h, "算力池里待放 1 条清单", "顶栏写池里待放几条，不当成一台主机");
  run(C, "drawTimeline()"); const tl = el(C, "tlbox").innerHTML;
  ok(![...tl.matchAll(/<text class="lab"[^>]*>(.*?)<\/text>/g)].some(m => m[1].includes("算力池") || m[1] === "车道 1 · p"), "时间线不该有算力池行（#89）");
  has(tl, "算力池 车道 1 · p（1 个任务）不在图上：等待中：在算力池里等着放到某台：池里主机都放不下（srv-h：显存不够）（已等 60 分钟）", "时间线下写池里待放的原因与已等多久");
  const rc = run(C, "resourcesCard()");
  ok(!rc.includes("</span> 算力池</b>"), "资源小图不该画算力池"); has(rc, "srv-h", "项目主机照常画");
  ok(run(C, 'hostShort(D.hosts.find(h => h.pool))') === "算力池", "短名");
});
t("project", "项目页 CPU 占用监控（#208）：顶部 CPU 卡片展示核数/配额与限流，时间线主机行同轴叠画不同颜色 CPU 折线与图例、悬停读数，兼容旧快照", () => {
  const C = pjCtx(PJ, EVN), h = app(C);
  has(h, "CPU 占用", "顶部指标区应有 CPU 占用卡片");
  has(h, "12.4/17 核（73%）", "有配额主机应展示 核数/配额（百分比）");
  has(h, "限流 29%", "限流主机应给出限流提示");
  has(h, "1.1 核（7%）", "无配额主机应展示核数与按可用核数换算的百分比");
  has(h, "配额 8 核（3%）", "缺失 cpu 字段的旧快照主机应向后兼容");
  run(C, "drawTimeline()");
  const tl = el(C, "tlbox").innerHTML, tk = el(C, "tlkey").innerHTML;
  has(tl, 'stroke="var(--accent)"', "时间线应保留蓝色 GPU 折线");
  has(tl, 'stroke="var(--cpu)"', "时间线主机行应同轴叠画青绿色 CPU 折线");
  has(tl, 'class="tl-h" data-thost="srv-a"', "时间线主机行应有折线悬停命中层");
  has(tk, "GPU 利用率", "图例应精简为短词 GPU 利用率");
  has(tk, "整机 GPU 利用率（蓝底蓝线", "图例悬停应含 GPU 折线颜色说明");
  has(tk, "CPU 占用（青绿线）", "图例应写明 CPU 折线为青绿线");
  has(tk, "整机 CPU 占用率（青绿线", "图例悬停应写明 CPU 折线详细说明");
  // 悬停主机表头折线区域应返回 GPU + CPU 读数
  const hov = run(C, '(() => { const b = document.getElementById("tlbox"); b.getBoundingClientRect = () => ({left: 0, width: 800}); return tlHostHover({dataset: {thost: "srv-a"}}, {clientX: TL.g.L + TL.g.PW * 0.25}); })()');
  has(hov, "GPU 利用率", "悬停应含 GPU 读数"); has(hov, "CPU 占用", "悬停应含 CPU 读数"); has(hov, "此刻 CPU 12.4/17 核（73%）", "悬停应含此刻 CPU 读数");
  // 长窗口（7 天）应使用 cpu_hourly 正常画出 CPU 折线；悬停在无采样断档区时不得用视窗跨度误填数小时前的旧值
  run(C, 'LS.set("tl", "week"); drawTimeline()');
  has(el(C, "tlbox").innerHTML, 'stroke="var(--cpu)"', "7 天视图应由 cpu_hourly 画出 CPU 折线");
  const gapSnap = {...PJ, history: [], util_hourly: {"srv-a": [[NOW - 24 * H, 80.0], [NOW - 12 * H, 70.0]]}, cpu_hourly: {"srv-a": [[NOW - 24 * H, 50.0], [NOW - 12 * H, 60.0]]}};
  const G = pjCtx(gapSnap, EVN); run(G, 'LS.set("tl", "week"); drawTimeline()');
  const gapHov = run(G, `(() => { const b = document.getElementById("tlbox"); b.getBoundingClientRect = () => ({left: 0, width: 800}); const t = ${NOW - 18 * H}; const x = TL.g.L + (t - TL.g.t0) / (TL.g.t1 - TL.g.t0) * TL.g.PW; return tlHostHover({dataset: {thost: "srv-a"}}, {clientX: x}); })()`);
  has(gapHov, "该时刻无 GPU / CPU 采样", "7 天视图断档空白区悬停不应误填数小时前旧值：" + gapHov);
  ok(!gapHov.includes("GPU 利用率") && !gapHov.includes("CPU 占用"), "断档空白区悬停混入了旧采样：" + gapHov);
  // 兼容旧快照：删掉 hosts[].cpu 与 cpu_hourly 不报错、不漏 NaN/undefined
  const oldSnap = {...PJ, cpu_hourly: undefined, hosts: PJ.hosts.map(x => { const y = {...x}; delete y.cpu; return y; })};
  const O2 = pjCtx(oldSnap, EVN); clean(app(O2), "旧快照项目页");
});
t("overview", "选规格与同池换节点（#172）：多规格池分组显示各规格空位、换节点按钮按池规格显隐、开机/换人/换节点向导选规格与空位拦截、进度条新连接命令", () => {
  const C = ovCtx(OV, RR, "#/fleet");
  const fl = page(C, "#/fleet");
  has(fl, "各规格空：nodeA-1GPU 2/4 · nodeB-1GPU 0/4");
  has(fl, "平台上这种规格空 2 / 4");
  // 多规格池（pool-public）显示可用「换节点」按钮；单规格池（pool-private）不显示；未核对过规格时置灰提示先核对
  const bd = run(C, 'srvBtns(srvById("srv-d"))');
  has(bd, 'data-kind="move"'); ok(!/data-kind="move"[^>]*disabled/.test(bd), "多规格池的换节点按钮不该灰着");
  ok(!run(C, 'srvBtns(srvById("srv-a"))').includes('data-kind="move"'), "单规格池不该显示换节点");
  const bNoSpecs = run(C, '(() => { const p = D.fleet.ops.pools["pool-public"], old = p.specs; p.specs = []; const h = srvBtns(srvById("srv-d")); p.specs = old; return h; })()');
  has(bNoSpecs, 'data-kind="move"'); ok(/data-kind="move"[^>]*disabled/.test(bNoSpecs), "还没读规格列表时换节点该置灰");
  has(tips(bNoSpecs), "先点「与平台核对」");
  // 服务器抽屉开机参数显示规格
  has(run(C, 'srvDrawer("srv-d")'), "<span>规格</span><span>nodeA-1GPU</span>");
  // 换人向导：同时列选人与各规格空位，缺省原规格，改选无空位规格时标错禁用
  const pvBad = {kind: "swap", pool: "pool-public", person: "PERSON5", job: "PERSON5", name: "PERSON5", spec: "s2b", orig_spec: "s2a", spec_name: "nodeB-1GPU",
    port: "15003", image: "registry.local/lab/ubuntu:base", image_src: "这人的设置", run_cmd: "sleep infinity", nodes: ["root@192.0.2.9", "root@192.0.2.10"],
    specs: OV.fleet.ops.pools["pool-public"].specs,
    items: [{item: "作业名", ok: true, note: "PERSON5"}, {item: "端口", ok: true, note: "15003（沿用原端口）"}, {item: "镜像", ok: true, note: "img"},
      {item: "配额", ok: true, note: "公用池开完 2 台"}, {item: "规格", ok: false, note: "nodeB-1GPU，上次核对时空 0 / 4"}]};
  const sw = run(C, `FWIZ = {kind: "swap", sid: "srv-d", pool: "pool-public", person: "PERSON5", spec: "s2b", ack: true, newp: "", adv: true, pv: ${JSON.stringify(pvBad)}}; wizForm()`);
  has(sw, 'data-ws="s2a"'); has(sw, 'data-ws="s2b"'); has(sw, "原规格 · 空 2/4"); has(sw, "空 0/4");
  has(sw, "规格不对，改好才能开始"); has(sw, "<span>规格</span><span>nodeB-1GPU</span>");
  ok(/data-fop="wizgo" disabled/.test(sw), "无空位规格不该允许提交");
  // 换节点向导：无选人区、当前规格禁用标「当前」、未选目标规格提示先选、带本地盘风险确认、换节点标题与进度条
  const mv0 = run(C, 'FWIZ = {kind: "move", sid: "srv-d", pool: "pool-public", person: "PERSON3", spec: null, ack: false, newp: "", adv: false, pv: {orig_spec: "s2a", specs: D.fleet.ops.pools["pool-public"].specs, items: [{item: "规格", ok: false, note: "要选目标规格"}]}}; wizForm()');
  has(mv0, "换到哪个节点（规格）？"); ok(!mv0.includes('data-wp='), "换节点不该出现选人按钮");
  ok(/data-ws="s2a" disabled/.test(mv0), "换节点里当前规格必须禁用"); has(mv0, "当前 · 空 2/4");
  has(mv0, "先选一个目标规格"); has(mv0, "换节点会清空"); has(mv0, "已和 PERSON3 说好，本地盘可以清空");
  has(run(C, 'opStrip(fPanel(), DH.srv("srv-d"))'), "换节点向导开着");
  // 换节点完成进度条：显示新规格与新连接命令
  const prog = run(C, 'opProgress({kind: "move", t0: D.generated - 60, active: false, error: null, ids: ["srv-d"], spec: "s2b", spec_name: "nodeB-1GPU", steps: [{name: "登录", state: "ok", note: "u"}], conn: {sid: "10-15003", holder: "PERSON3", cmd: "ssh -p 15003 root@192.0.2.10", spec: "nodeB-1GPU", mine: false}})');
  has(prog, "换节点（→ nodeB-1GPU）完成"); has(prog, "ssh -p 15003 root@192.0.2.10");
  run(C, "FWIZ = null");
});
t("overview", "独立主机分类与域隔离（#114）：主机表与主机页显「独立」标签、不显集群共享盘条、外平台保留到期提醒并隐藏续租按钮、管理抽屉禁跨独立机搬登记", () => {
  const S = JSON.parse(JSON.stringify(OV));
  const ext = JSON.parse(JSON.stringify(OV.hosts[0]));
  Object.assign(ext, {
    id: "ext-1", label: "ext-1（外租机）", platform: null, platform_kind: "other", share: "private", standalone: true,
    lease_end: NOW + 5 * H, renew: null, auto_res: null, lend: null, borrow: null,
  });
  const ext2 = JSON.parse(JSON.stringify(ext));
  Object.assign(ext2, {id: "ext-2", label: "ext-2（自有机）", platform_kind: "none", lease_end: null});
  S.hosts.push(ext, ext2);
  S.regs.push(
    {host: "ext-1", label: "ext-1（外租机）", state: "active", returned: null, home: "~/.local/share/labmon/work/labmon", standalone: true, stale: false, t: NOW - 4,
      queues: [{root: "/home/u/work/projects/proj-a", jobs: "queues/q.txt", project: "proj-a", stop: "boundary", gpus: null, empty: false}]},
    {host: "ext-2", label: "ext-2（自有机）", state: "active", returned: null, home: "~/.local/share/labmon/work/labmon", standalone: true, stale: false, t: NOW - 4, queues: []},
  );
  const C = ovCtx(S, RR);
  const home = page(C, "#/");
  clean(home, "含独立主机的总览");
  has(home, "ext-1（外租机）");
  has(home, ">独立</span>");
  has(home, "独立主机不挂集群共享盘");
  has(home, "请在外平台手动续租或提前迁移任务");
  const hp = page(C, "#/host/ext-1");
  clean(hp, "外平台独立主机页");
  has(hp, "外平台独立主机");
  has(hp, "外平台手动续租");
  ok(!hp.includes("预备续租"), "外平台独立主机不应显示预备续租按钮");
  // 两台独立主机即使 home 字符串相同，regTo 也不允许互搬登记，且 adoptSrc 返回 null
  ok(run(C, 'regTo(D.regs.find(x => x.host === "ext-1"), "proj-a").length') === 0, "独立主机不应列出可搬目标");
  ok(run(C, 'regTo(D.regs.find(x => x.host === "srv-a"), "proj-a").every(x => x.id !== "ext-1" && x.id !== "ext-2")'), "共享主机不应把独立主机列为可搬目标");
  ok(run(C, 'adoptSrc(D.hosts.find(x => x.id === "ext-2"))') === null, "独立主机不应触发跨机接上项目");
  const mg = page(C, "#/p/proj-a/manage");
  clean(mg, "含独立主机的项目管理抽屉");
  has(tips(mg), "独立主机不共享 worker 目录，不能把登记搬到别的主机");
});

/* ---------- #213 推模式临时主机与云盘存储 ---------- */
const OVEPH = J("overview_ephemeral.json"), PJEPH = J("project_ephemeral.json");
t("overview", "推模式临时主机（#213）：总览标「临时」徽标、云盘余量与低余量标红、租期列写临时实例，主机页隐藏续租/借用/临时指令与实验室共享盘", () => {
  const C = ovCtx(OVEPH, RR);
  ok(run(C, 'hostShort({id: "colab-t4-01", ephemeral: true})') === "临时 colab-t4-01", "hostShort 临时前缀");
  const home = page(C, "#/"); clean(home, "ephemeral #/");
  has(home, '<span class="badge b-mute">临时</span>');
  has(home, "临时实例");
  has(home, "Google Drive 剩 12.4 GiB");
  has(home, 'style="color:var(--bad);font-weight:600"', "云盘 < 1 GiB 标红");
  has(home, "Google Drive 剩 0.6 GiB");
  has(home, "临时离线");
  ok(!run(C, 'alertsOf(D.hosts.find(h => h.id === "colab-t4-01"))').some(a => a.msg.includes("worker 不在")), "在线临时主机不报 worker 不在");
  ok(!run(C, 'alertsOf(D.hosts.find(h => h.id === "colab-l4-down"))').some(a => a.msg.includes("labmon worker start")), "离线临时主机不提示 labmon worker start");
  // 主机页：展示实例 id、项目链接、云盘/本地盘资源卡，不显示续租、借用、接上卡、临时指令/队列/本地盘选项卡
  const hp = page(C, "#/host/colab-t4-01"); clean(hp, "ephemeral #/host/colab-t4-01");
  has(hp, "inst-t4-01"); has(hp, "临时在线"); has(hp, "Google Drive"); has(hp, "Colab 本地盘");
  has(hp, "colab_s1"); ok(!hp.includes("旧队列"), "临时主机任务不应标成旧队列");
  ok(!hp.includes("续租"), "临时主机页不应出现续租");
  ok(!hp.includes("借用详情"), "临时主机页不应出现借用详情");
  ok(!hp.includes("临时指令"), "临时主机页记录卡不应出现临时指令选项卡");
  ok(!hp.includes("接上原来的项目"), "临时主机页不应出现接上卡");
  const hpLow = page(C, "#/host/colab-t4-02"); clean(hpLow, "ephemeral #/host/colab-t4-02");
  has(hpLow, "低于 1 GiB");
  const hpDown = page(C, "#/host/colab-l4-down"); clean(hpDown, "ephemeral #/host/colab-l4-down");
  has(hpDown, "临时离线");
  // 任务详情展示产物目录与临时实例来源
  const det = run(C, '(() => { const h = D.hosts.find(x => x.id === "colab-t4-01"); return jobDetail({...h.status.running[0], h}); })()');
  has(det, "产物目录"); has(det, "/content/drive/MyDrive/labmon/proj-a/runs/colab_s1"); has(det, "proj-a 的临时主机上报");
  // 恶意字符串在临时主机字段里必须全部转义
  const badSnap = JSON.parse(JSON.stringify(OVEPH));
  const bh = badSnap.hosts.find(h => h.id === "colab-t4-01");
  bh.label = "云" + BAD; bh.instance_id = "id" + BAD;
  bh.storage[0].label = "盘" + BAD; bh.storage[0].path = "/drv/" + BAD;
  bh.status.running[0].artifact_dir = "/art/" + BAD;
  const BC = ovCtx(badSnap, RR);
  clean(page(BC, "#/"), "ephemeral evil #/");
  clean(page(BC, "#/host/colab-t4-01"), "ephemeral evil #/host/colab-t4-01");
});
t("project", "项目页临时主机与云盘存储（#213）：资源卡区隔实验室共享盘与云盘、云盘 < 1 GiB 标红、时间线与明细展示 artifact_dir 及三类告警", () => {
  const C = pjCtx(PJEPH, EVN), h = app(C);
  clean(h, "ephemeral project");
  has(h, "Colab T4 #1"); has(h, "临时");
  has(h, "/content/drive/MyDrive/labmon/proj-a/runs/colab_s1");
  has(h, "临时主机 Colab L4 离线（colab-l4-down）心跳超时离线");
  // 资源卡：第一台实验室主机（srv-a）仍展示共享盘，临时主机展示云盘与本地盘且 < 1 GiB 标红
  const rc = run(C, "resourcesCard()"); clean(rc, "ephemeral resourcesCard");
  has(rc, "Google Drive"); has(rc, "Colab 本地盘"); has(rc, "云盘"); has(rc, "余 0.6 GiB");
  has(rc, 'style="color:var(--bad)">余 0.6 GiB</span>', "云盘低于 1 GiB 标红");
  // 时间线渲染临时车道不报错
  run(C, "drawTimeline()");
  const tl = el(C, "tlbox").innerHTML; clean(tl, "ephemeral timeline");
  has(tl, "colab_s1");
  // 恶意字符串在项目页临时主机与 artifact_dir 中必须转义
  const badPj = JSON.parse(JSON.stringify(PJEPH));
  badPj.hosts.find(x => x.id === "colab-t4-01").mounts[0].label = "云" + BAD;
  badPj.jobs.find(j => j.id === "colab_s1").artifact_dir = "/p/" + BAD;
  clean(app(pjCtx(badPj, EVN)), "ephemeral evil project");
});
ta("auth", "共有型算力池前端权限隔离、独占池屏蔽与首页 noHost 提示收口（#212）", async () => {
  // 1. 普通池成员（OVUP）：侧栏见「算力池」，#/pool 见所属池与本人待放清单，绝不泄露 members 或他人待放任务
  const P_ME = {...OWNER_ME, hosts: [], pools: ["pool"], pool_member: true};
  const UP = accCtx(OVUP, P_ME);
  has(side(UP), 'href="#/pool"', "池成员侧栏应有算力池入口");
  const pp = page(UP, "#/pool"); clean(pp, "池成员 #/pool");
  has(pp, "共有算力池"); has(pp, "alpha_pool_s1");
  for (const bad of ["amy、bob", "beta_secret_job", "jobs/pb.txt"]) ok(!pp.includes(bad), "池成员 #/pool 泄露：" + bad);
  // 2. 非池成员（OVU）：侧栏无算力池入口，#/pool 提示无可用共有池
  const UN = accCtx(OVU, {...OWNER_ME, pools: [], pool_member: false});
  ok(!side(UN).includes('href="#/pool"'), "非池成员侧栏不该出现算力池");
  has(page(UN, "#/pool"), "没有可用的共有型算力池");
  // 3. 首页 noHost 收口：① 算力池成员（无单独分配主机、D.hosts 为空）不算缺主机，不弹“你的账号还没有能用的主机”
  const poolOnlySnap = {...JSON.parse(JSON.stringify(OVUP)), hosts: [], projects: []};
  const hpPool = page(accCtx(poolOnlySnap, P_ME), "#/"); clean(hpPool, "仅池成员首页");
  ok(!hpPool.includes("你的账号还没有能用的主机"), "池成员不该弹无可用主机提示");
  has(hpPool, "共有算力池");
  // 4. 首页 noHost 收口：② 无分配主机、不在共有池，但名下持有 1 台组内服务器时给明确提示（#212、#224）
  const fleetOnlySnap = {...JSON.parse(JSON.stringify(OVUM)), hosts: [], projects: [], pool: null};
  const hpFleet = page(accCtx(fleetOnlySnap, {...OWNER_ME, hosts: [], pools: [], pool_member: false}), "#/"); clean(hpFleet, "仅名下有组内服务器首页");
  has(hpFleet, '你名下有 1 台组内服务器（当前暂未接入调度，可在<a href="#/fleet">我的服务器</a>查看）');
  ok(!hpFleet.includes("你的账号还没有能用的主机"), "名下有组内服务器时不该再笼统说没有能用的主机");
  // 5. 首页 noHost 收口：③ 既无主机、不在池、名下也无组内服务器时保留原提示并补充加入共有池引导
  const emptySnap = {...JSON.parse(JSON.stringify(OVU)), hosts: [], projects: [], pool: null, fleet: null};
  const hpEmpty = page(accCtx(emptySnap, {...OWNER_ME, hosts: [], pools: [], pool_member: false}), "#/");
  has(hpEmpty, "你的账号还没有能用的主机"); has(hpEmpty, "加入共有算力池");
  // 6. 独占共有池主机：regTo / assignSrc / adoptSrc 全部屏蔽单台直投
  const exSnap = JSON.parse(JSON.stringify(OV));
  exSnap.hosts[0].pool_id = "shared"; exSnap.hosts[0].pool_label = "共有池"; exSnap.hosts[0].pool_exclusive = true;
  exSnap.hosts[0].status.queues = []; exSnap.regs[0].queues = [];
  const EX = accCtx(exSnap, ADMIN_ME);
  ok(run(EX, 'assignSrc(D.hosts[0])') === null, "独占池单台主机不应出给项目用");
  ok(run(EX, 'adoptSrc(D.hosts[0])') === null, "独占池单台主机不应出接上卡");
  ok(!run(EX, 'regTo(D.regs[1], "proj-b").some(t => t.id === "srv-a")'), "独占池单台主机不应出现在换到目标列表");
  has(page(EX, "#/host/srv-a"), "独占 · 请经池入口提交");
  // 7. canUseHost 多池与单池别名 + evil 转义 + poll 同步 access.pools
  const M1 = accCtx(OVUP, {...OWNER_ME, pools: ["pool:a100"], pool_member: true});
  ok(run(M1, 'canUseHost("pool:a100") && canUseHost("pool") && !canUseHost("pool:other")'), "单命名池权限判定不对");
  const M2 = accCtx(OVUP, {...OWNER_ME, pools: ["pool:a100", "pool:4090"], pool_member: true});
  ok(run(M2, 'canUseHost("pool:a100") && canUseHost("pool:4090") && !canUseHost("pool")'), "多命名池权限判定不对");
  clean(page(ovCtx(OVE, RRE), "#/pool"), "evil #/pool");
  run(UN, `fetch = () => Promise.resolve({ok: true, json: async () => (${JSON.stringify(OVUP)})});`);
  await run(UN, "poll()");
  ok(run(UN, 'ME.pool_member === true && JSON.stringify(ME.pools) === JSON.stringify(["pool"])'), "poll 未同步 D.access.pools 到 ME");
});

t("overview", "独立主机本地工作盘、磁盘闸门、可回收目录与外部主机右侧抽屉（#114 P2）", () => {
  const S = JSON.parse(JSON.stringify(OV));
  const ext = JSON.parse(JSON.stringify(OV.hosts[0]));
  const wd = {
    host: "ext-4090", label: "ext-4090（外租）",
    root: "/home/u/.local/share/labmon/work", free: 8 * 2 ** 30, total: 500 * 2 ** 30, used: 492 * 2 ** 30, work_b: 120 * 2 ** 30,
    warn_gb: 20, stop_gb: 10, quota_gb: 200, low: true, quota_warn: false,
    disk_gate: {on: true, why: "本地盘可用空间不足：剩 8.0 GiB（低于停止线 10 GiB）", free_gb: 8.0, stop_gb: 10, recover_gb: 12},
    dirs: [
      {u: "projects/proj-a", name: "proj-a", p: "proj-a", b: 80 * 2 ** 30, d24: 4 * 2 ** 30, m: NOW - 3600},
      {u: "projects/old-exp", name: "old-exp", p: "old-exp", b: 30 * 2 ** 30, d24: 0, m: NOW - 20 * 86400},
      {u: "datasets", name: "datasets", p: null, b: 10 * 2 ** 30, d24: null, m: null},
    ],
    projects: {"proj-a": {b: 80 * 2 ** 30, d24: 4 * 2 ** 30, m: NOW - 3600}, "old-exp": {b: 30 * 2 ** 30, d24: 0, m: NOW - 20 * 86400}},
    common: {datasets: 10 * 2 ** 30},
    reclaimable: [{u: "projects/old-exp", path: "/home/u/.local/share/labmon/work/projects/old-exp", project: "old-exp", kind: "project", b: 30 * 2 ** 30, idle_days: 20, m: NOW - 20 * 86400}],
    pending: 0, t: NOW - 60,
  };
  Object.assign(ext, {
    id: "ext-4090", label: "ext-4090（外租）", platform: null, platform_kind: "other", share: "private", standalone: true,
    owner: "amy", lw: null, work_disk: wd, lease_end: NOW + 5 * 86400, renew: null, auto_res: null, lend: null, borrow: null,
  });
  ext.status.disk_gate = wd.disk_gate;
  S.hosts.push(ext);
  const C = ovCtx(S, RR);
  const home = page(C, "#/");
  clean(home, "含本地工作盘与闸门的总览");
  has(home, "+ 添加外部主机");
  has(home, "闸门 · 剩 8");
  has(home, "本地盘可用空间不足");
  C.RTAB = "lw";
  const hp = page(C, "#/host/ext-4090");
  clean(hp, "独立主机本地工作盘与选项卡");
  has(hp, "本地工作盘");
  has(hp, "已暂停放新任务");
  has(hp, "绝不自动删除");
  has(hp, "可回收");
  has(hp, "labmon worker clean ext-4090");
  has(hp, "管理外部机");
  ok(!hp.includes("续租会清空"), "独立主机资源卡与工作盘不应出现续租清空提示");
  // 打开新增外部机抽屉
  const drw = page(C, "#/ext/new");
  clean(drw, "新增外部主机右侧抽屉");
  has(drw, "添加外部主机");
  has(drw, "保存并生成公钥");
  has(drw, "零私钥上传");
  // 「我的账号」概览展示我的外部主机
  const me = page(C, "#/me");
  clean(me, "我的账号含外部主机");
  has(me, "我的外部主机");
  has(me, "ext-4090（外租）");
  // 恶意字符串转义检查
  const SE = JSON.parse(JSON.stringify(S));
  SE.hosts.find(x => x.id === "ext-4090").work_disk.root = "/work/" + BAD;
  SE.hosts.find(x => x.id === "ext-4090").work_disk.disk_gate.why = "原因" + BAD;
  SE.hosts.find(x => x.id === "ext-4090").status.disk_gate.why = "原因" + BAD;
  const CE = ovCtx(SE, RR);
  CE.RTAB = "lw";
  clean(page(CE, "#/host/ext-4090"), "恶意 work_disk 转义");
  // 项目页 storageChip 独立主机分支
  const PS = JSON.parse(JSON.stringify(PJ));
  PS.storage = {...(PS.storage || {}), standalone: {"srv-a": {root: "/home/u/work", b: 12 * 2 ** 30, d24: 2 ** 30, free: 8 * 2 ** 30, total: 500 * 2 ** 30, warn_gb: 20, stop_gb: 10, low: true, disk_gate: wd.disk_gate, reclaimable: wd.reclaimable}}};
  const PC = mkCtx(pgSrc, PS, {filename: "page.js"});
  const ph = run(PC, "resourcesCard()");
  clean(ph, "项目页独立主机 storageChip");
  has(ph, "本地工作盘");
  has(ph, "闸门");
});

t("overview", "WSL2 外部占卡暂停放行、共享模式、不报异常与抽屉 9p 路径提醒（#114 P3）", () => {
  const S = JSON.parse(JSON.stringify(OV));
  const wsl = JSON.parse(JSON.stringify(OV.hosts[0]));
  Object.assign(wsl, {
    id: "wsl-4090", label: "WSL2-4090", platform: null, platform_kind: "none", share: "private", standalone: true,
    owner: "amy", lw: null, work_disk: null, lease_end: null, renew: null, auto_res: null, lend: null, borrow: null,
  });
  wsl.status = {
    ...wsl.status,
    running: [], external: [], gpu_procs: [],
    kafight: null, throttle: null, keepalive: {}, disk_gate: null, gate: null,
    info: {
      ...wsl.status.info,
      wsl: true,
      gpus: [{i: 0, uuid: "GPU-wsl-0", name: "RTX 4090", used: 6400, total: 24576, util: 35,
              unacct_mb: 6400, unacct_busy: true, unacct_why: "外部未归因显存 6.2 GiB（≥ 1.5 GiB）"}],
    },
    room: {gpus: {"GPU-wsl-0": 18176}, unacct: ["GPU-wsl-0"], ram: 48.0, cpu: 12.0, cpu_cap: 16},
    waiting: [{project: "proj-a", name: "wsl_s1", root: "/home/u/work/projects/proj-a", queue: "jobs.txt",
               since: NOW - 120, why: "外部（如宿主机）占卡暂停放行（GPU 0 约 6.2 GiB，本环境不可见其进程，释放后自动恢复）",
               pos: {n: 1, puse: 1.0, focus: false, ratio: 1.0}}],
  };
  S.hosts.push(wsl);
  const C = ovCtx(S, RR);
  // 外部占卡暂停属于正常让路，绝不进 alertsOf 告警
  ok(run(C, 'alertsOf(D.hosts.find(h => h.id === "wsl-4090"))').length === 0, "外部占卡暂停不应触发 alertsOf 告警");
  const home = page(C, "#/");
  clean(home, "WSL2 外部占卡总览");
  has(home, "外部占卡");
  has(home, "外部（如宿主机）占卡暂停放行");
  const hp = page(C, "#/host/wsl-4090");
  clean(hp, "WSL2 外部占卡主机页");
  has(hp, "外部占卡暂停放行");
  has(tips(hp), "默认整卡暂停放新任务且不杀已跑任务，释放后自动恢复");
  // allow_shared_gpu 开启时：不在 room.unacct，显示可放与“外部占 X.X GiB”，不写暂停放行
  const S2 = JSON.parse(JSON.stringify(S));
  S2.hosts.find(x => x.id === "wsl-4090").status.room.unacct = [];
  const hpShared = page(ovCtx(S2, RR), "#/host/wsl-4090");
  clean(hpShared, "WSL2 共享模式主机页");
  has(hpShared, "外部占 6.3 GiB");
  ok(!hpShared.includes("外部占卡暂停放行"), "共享模式不应显示暂停放行徽章");
  // 外部主机抽屉：allow_shared_gpu 开关与 /mnt/[a-z]/ 9p 性能风险提示
  const drw = page(C, "#/ext/new");
  clean(drw, "外部主机抽屉含 allow_shared_gpu");
  has(drw, 'data-extf="allow_shared_gpu"');
  ok(!drw.includes("9p 挂载"), "默认 ext4 路径不应显示 9p 警告");
  run(C, 'EXT.form.data_root = "/mnt/c/labmon/work"; render(false)');
  const drwMnt = el(C, "drwp").innerHTML;
  clean(drwMnt, "抽屉 /mnt/c 提醒");
  has(drwMnt, "9p 挂载");
  run(C, 'EXT.form.data_root = "/mnt/data/labmon"; render(false)');
  ok(!el(C, "drwp").innerHTML.includes("9p 挂载"), "/mnt/data 不是 Windows 盘符挂载，不应误报");
  // 恶意字符串在 unacct_why 里必须转义
  const SE = JSON.parse(JSON.stringify(S));
  SE.hosts.find(x => x.id === "wsl-4090").status.info.gpus[0].unacct_why = "占卡" + BAD;
  const CE = ovCtx(SE, RR);
  clean(page(CE, "#/"), "WSL2 恶意 unacct_why 总览");
  clean(page(CE, "#/host/wsl-4090"), "WSL2 恶意 unacct_why 主机页");
  // 项目页 resourcesCard：展示外部占卡徽标与悬停说明
  const PS = JSON.parse(JSON.stringify(PJ));
  PS.hosts[0].gpus[0].unacct_mb = 6400;
  PS.hosts[0].gpus[0].unacct_paused = true;
  const ph = run(mkCtx(pgSrc, PS, {filename: "page.js"}), "resourcesCard()");
  clean(ph, "项目页外部占卡 resourcesCard");
  has(ph, "外部占卡");
  has(tips(ph), "外部（如宿主机）占卡暂停放行");
});

ta("auth", "已开账号的组员服务器自动接入调度、我的服务器切换与首页 noHost 仅监控收口（#224）", async () => {
  const S = JSON.parse(JSON.stringify(OVUM));
  const srv = S.fleet.servers[0];
  srv.id = "srv-own"; srv.holder = "OWNER"; srv.borrow = null;
  srv.manage = {on: true, auto: true, opt_out: false, hosted: false, account: "owner", can_toggle: true, worker_up: true, why: "已接入本人调度（worker 运行中）"};
  const hostObj = {...JSON.parse(JSON.stringify(OV.hosts[0])), id: "srv-own", label: "node9（OWNER）", member_auto: true};
  S.hosts = [hostObj];
  S.projects = [];
  S.pool = null;
  const U_ME = {...OWNER_ME, hosts: ["srv-own"], fleet_person: "OWNER", pools: [], pool_member: false, write: true, admin: false};
  const UC = accCtx(S, U_ME);
  // 1. 总览：自动接入后不再报 noHost，主机表展示「我的服务器」徽标
  const hp = page(UC, "#/"); clean(hp, "#224 自动接入总览");
  ok(!hp.includes("你的账号还没有能用的主机"), "自动接入后不应出现无可用主机提示");
  has(hp, "我的服务器"); has(hp, "node9（OWNER）");
  // 2. 「我的服务器」卡片与抽屉展示「已接入调度」、调度接入行、切为仅监控弹窗与 worker 节
  const fl = page(UC, "#/fleet"); clean(fl, "#224 我的服务器卡片");
  has(fl, "已接入调度");
  const drw = page(UC, "#/fleet/srv/srv-own"); clean(drw, "#224 服务器抽屉");
  has(drw, "调度接入"); has(drw, "已接入本人调度（worker 运行中）"); has(drw, "切为仅监控"); has(drw, "<b>worker</b>"); has(drw, "#/host/srv-own");
  // 3. 「我的账号 → 概览」展示「我的服务器」卡片及切换按钮
  const me = page(UC, "#/me"); clean(me, "#224 我的账号含我的服务器");
  has(me, "<b>我的服务器</b>"); has(me, "srv-own"); has(me, "切为仅监控");
  // 4. 点开弹窗并确认提交 POST api/fleet/manage（切为仅监控）
  run(UC, `POSTED = []; fetch = (u, o) => { POSTED.push([u, o?.body]); return Promise.resolve({ok: true, json: async () => ({id: "srv-own", on: false, msg: "已切为仅监控", manage: {on: false, auto: false, opt_out: true, hosted: false, account: "owner", can_toggle: true, worker_up: false, why: "已切为仅监控（不自动拉起 worker）"}})}); }; poll = () => {};`);
  await run(UC, 'manageAct("pick", {id: "srv-own", where: "me"})');
  const mePop = page(UC, "#/me"); clean(mePop, "#224 切为仅监控弹窗");
  has(mePop, "容器内正在跑的任务不受影响"); has(mePop, "确认切为仅监控");
  await run(UC, 'manageAct("go", {id: "srv-own", on: "0", where: "me"})');
  ok(run(UC, 'POSTED.length === 1 && POSTED[0][0] === "api/fleet/manage" && JSON.parse(POSTED[0][1]).on === false'), "未正确发送 POST api/fleet/manage");
  ok(run(UC, 'D.fleet.servers[0].manage.opt_out === true'), "切为仅监控后未原地更新 manage 状态");
  // 5. 切为仅监控后若无其他主机，首页 noHost 精准提示「处于仅监控状态」并引导去「我的服务器」或「我的账号」一键重接
  const S2 = JSON.parse(JSON.stringify(S));
  S2.hosts = [];
  S2.fleet.servers[0].manage = {on: false, auto: false, opt_out: true, hosted: false, account: "owner", can_toggle: true, worker_up: false, why: "已切为仅监控（不自动拉起 worker）"};
  const UC2 = accCtx(S2, {...U_ME, hosts: []});
  const hp2 = page(UC2, "#/"); clean(hp2, "#224 仅监控首页 noHost");
  has(hp2, "处于仅监控状态"); has(hp2, "一键重新接入本人调度");
  has(page(UC2, "#/fleet"), "仅监控");
  has(page(UC2, "#/me"), "接入调度");
  // 6. 管理员在组内页与抽屉可看到并代切换，恶意 why 必须转义
  const S3 = JSON.parse(JSON.stringify(OV));
  S3.fleet.servers.find(x => x.id === "srv-d").manage.why = "原因" + BAD;
  const AC = accCtx(S3, ADMIN_ME);
  clean(page(AC, "#/fleet"), "#224 管理员组内页转义");
  clean(page(AC, "#/fleet/srv/srv-d"), "#224 管理员抽屉转义");
  has(page(AC, "#/fleet/srv/srv-d"), "接入调度");
  // 7. #236：manage 受阻（manual_stopped）与拉起失败（relaunch_failed）徽标、恢复拉起弹窗，以及他人 member_auto 不进管理员 assignCard/adoptCard/regTo
  const S4 = JSON.parse(JSON.stringify(OV));
  S4.fleet.servers.find(x => x.id === "srv-d").manage = {
    on: true, auto: true, opt_out: false, hosted: false, account: "person3", can_toggle: true, worker_up: false,
    state: "manual_stopped", blocked: true, why: "已接入本人调度，但已手动停用 worker（2026-11-27 12:00:00），不自动拉起",
  };
  S4.fleet.servers.find(x => x.id === "srv-g").manage = {
    on: true, auto: true, opt_out: false, hosted: false, account: "person6", can_toggle: true, worker_up: false,
    state: "relaunch_failed", blocked: false, why: "已接入本人调度，但上次自动拉起失败（退出码 3）：PermissionError，10 分钟后重试",
  };
  const maOther = {...JSON.parse(JSON.stringify(OV.hosts[0])), id: "srv-other-ma", label: "node9（ALICE）", member_auto: true, member_account: "alice", status: {...JSON.parse(JSON.stringify(OV.hosts[0].status)), queues: []}};
  S4.hosts.push(maOther);
  S4.regs.push({host: "srv-other-ma", label: "node9（ALICE）", state: "active", home: S4.regs[0].home, standalone: false, stale: false, queues: []});
  const AC4 = accCtx(S4, ADMIN_ME);
  const fl4 = page(AC4, "#/fleet"); clean(fl4, "#236 组内页受阻与拉起失败徽标");
  has(fl4, "接入受阻"); has(fl4, "拉起失败");
  const drw4 = page(AC4, "#/fleet/srv/srv-d"); clean(drw4, "#236 手动停用抽屉含恢复拉起");
  has(drw4, "已手动停用 worker"); has(drw4, "恢复拉起");
  run(AC4, `POSTED = []; fetch = (u, o) => { POSTED.push([u, o?.body]); return Promise.resolve({ok: true, json: async () => ({id: "srv-d", on: true, msg: "已重新接入", manage: {on: true, auto: true, opt_out: false, hosted: false, account: "person3", can_toggle: true, worker_up: false, state: "pending", blocked: false, why: "已接入本人调度（等待自动拉起 worker）"}})}); }; collectNow = () => {};`);
  await run(AC4, 'manageAct("pick", {id: "srv-d", where: "drw", k: "fman-drw-res"})');
  has(page(AC4, "#/fleet/srv/srv-d"), "确认恢复拉起");
  await run(AC4, 'manageAct("go", {id: "srv-d", on: "1", where: "drw"})');
  ok(run(AC4, 'POSTED.length === 1 && POSTED[0][0] === "api/fleet/manage" && JSON.parse(POSTED[0][1]).on === true'), "#236 恢复拉起未提交 on:true");
  // 前端双保险：他人 member_auto 不出现 assignCard、adoptCard、也不进 regTo
  ok(run(AC4, 'assignSrc(D.hosts.find(h => h.id === "srv-other-ma")) === null'), "#236 他人 member_auto 不应出现 assignCard");
  ok(run(AC4, 'adoptSrc(D.hosts.find(h => h.id === "srv-other-ma")) === null'), "#236 他人 member_auto 不应出现 adoptCard");
  ok(!run(AC4, 'regTo(D.regs[0], "proj-a").some(t => t.id === "srv-other-ma")'), "#236 他人 member_auto 不应出现在 regTo");
  // alertsOf 对 stopped_blocks=false（历史纯时间戳）提示将自动拉起，对 stopped_blocks=true（manual:）提示已手动停用
  const alLegacy = run(AC4, 'alertsOf({id:"m1",label:"M1",auto:true,member_auto:true,error:null,status_error:null,lan_off:false,status:{t:1},worker_up:false,stopped:"2026-11-27 06:31:36",stopped_blocks:false}).map(x=>x.msg).join("\\n")');
  has(alLegacy, "看板会自动拉起");
  const alMan = run(AC4, 'alertsOf({id:"m1",label:"M1",auto:true,member_auto:true,error:null,status_error:null,lan_off:false,status:{t:1},worker_up:false,stopped:"manual:2026-11-27 12:00:00",stopped_blocks:true}).map(x=>x.msg).join("\\n")');
  has(alMan, "已手动停用"); ok(!alMan.includes("看板会自动拉起"), "手动停用不应提示自动拉起");
});

t("overview", "macOS Apple Silicon MPS 逻辑卡、统一内存与桌面留核渲染（#114 P4）", () => {
  const S = JSON.parse(JSON.stringify(OV));
  const mac = JSON.parse(JSON.stringify(OV.hosts[0]));
  Object.assign(mac, {
    id: "mac-m4", label: "MacBook-M4", platform: null, platform_kind: "none", share: "private", standalone: true,
    owner: "amy", lw: null, work_disk: null, lease_end: null, renew: null, auto_res: null, lend: null, borrow: null,
  });
  mac.status = {
    ...mac.status,
    running: [], external: [], gpu_procs: [],
    kafight: null, throttle: null, keepalive: {}, disk_gate: null, gate: null,
    info: {
      os: "darwin", nproc: 10, cpu_quota: 8.0, cpu_quota_src: "macos_default", cpu_used: 1.8, started: NOW - 86400,
      mem: {limit: 16.0, used: 10.2, anon: 9.4, src: "vm_stat"},
      gpus: [{i: 0, uuid: "mps-0", name: "Apple M4 (MPS)", backend: "mps", total: 10649.6, used: 3891.2, free: 6758.4, util: null}],
      disk: [{path: "/Users/u/work", free: 180.0, total: 460.0, kind: "local"}],
    },
    room: {gpus: {"mps-0": 6758.4}, unacct: [], ram: 6.6, cpu: 8.0, cpu_cap: 8},
    waiting: [],
  };
  S.hosts.push(mac);
  const C = ovCtx(S, RR);
  ok(run(C, 'alertsOf(D.hosts.find(h => h.id === "mac-m4"))').length === 0, "Apple Silicon MPS 主机不应触发 alertsOf 误报");
  const home = page(C, "#/");
  clean(home, "macOS MPS 总览");
  has(home, "MacBook-M4");
  has(home, "MPS");
  const hp = page(C, "#/host/mac-m4");
  clean(hp, "macOS MPS 主机页");
  has(hp, "显存（MPS）");
  has(hp, "MPS 不采集实时利用率");
  has(hp, "内存（macOS）");
  has(tips(hp), "默认留 2 核给桌面");
  // 恶意芯片名必须转义
  const SE = JSON.parse(JSON.stringify(S));
  SE.hosts.find(x => x.id === "mac-m4").status.info.gpus[0].name = "Apple M4 " + BAD;
  clean(page(ovCtx(SE, RR), "#/"), "macOS MPS 恶意芯片名总览");
  clean(page(ovCtx(SE, RR), "#/host/mac-m4"), "macOS MPS 恶意芯片名主机页");
  // 项目页 resourcesCard：MPS 卡显示芯片名 chip，不出现 "—°C · — W"
  const PS = JSON.parse(JSON.stringify(PJ));
  PS.hosts = [PS.hosts[0]];
  PS.hosts[0].gpus = [{index: 0, name: "Apple M4 (MPS)", backend: "mps", util: null, mem_used: 3891.2, mem_total: 10649.6, temp: null, power: null, power_limit: null}];
  PS.hosts[0].gpu_procs = [];
  const ph = run(mkCtx(pgSrc, PS, {filename: "page.js"}), "resourcesCard()");
  clean(ph, "项目页 macOS MPS resourcesCard");
  has(ph, "MPS 显存");
  has(ph, "Apple M4 (MPS)");
  ok(!ph.includes("°C · "), "MPS 卡不应渲染温功耗 chip");
  // 恶意芯片名在项目页 chip 里也必须转义
  PS.hosts[0].gpus[0].name = "Apple M4 " + BAD;
  clean(run(mkCtx(pgSrc, PS, {filename: "page.js"}), "resourcesCard()"), "项目页 MPS 恶意芯片名转义");
});

t("overview", "前端 UI 打磨（#237）：分段按钮组、外部机抽屉重构、窄屏磁盘徽标、算力池标题与告警折叠等 10 项回归", () => {
  // P0-1 & P0-2：推送方式、开通账号角色、外部机平台/密钥分段按钮组；外部机抽屉固定底栏、高级选项默认折叠/非默认展开、独立危险区 popConfirm
  const A = accCtx(OV, ADMIN_ME, "#/me/push"); run(A, "ACC.at.me = 1; ACC.at.users = 1; ACC.users = " + JSON.stringify(USERS));
  run(A, 'accAct("push")');
  const pushH = page(A, "#/me/push");
  has(pushH, 'class="seg form-seg"', "推送方式应改用分段按钮组");
  has(pushH, 'data-pushkind="bark"'); has(pushH, 'data-pushkind="ntfy"');
  ok(!pushH.includes("<select"), "推送配置弹窗不应再包含 select");
  run(A, 'accAct("open-onboard", {n:"OWNER"})');
  const obH = run(A, 'onboardPop("OWNER")');
  has(obH, 'data-pop-seg="ur" data-val="user"'); has(obH, 'data-pop-seg="ur" data-val="admin"');
  ok(!obH.includes('<select class="ain" id="acur"'), "开通账号角色不应再是 select");
  // 外部主机新增抽屉：默认高级选项折叠、固定底栏、无 <select>、无英文 managed/system 选项
  const extNew = page(A, "#/ext/new");
  has(extNew, '<footer class="dft">', "外部机抽屉主操作应固定在底部 dft");
  has(extNew, 'class="aform ext-grid"', "外部机抽屉应使用 2 列栅格");
  has(extNew, '<details class="dfold" data-extadv>', "默认参数时高级选项应折叠");
  has(extNew, 'data-extseg="platform_kind"'); has(extNew, 'data-extseg="ssh_key"');
  ok(!extNew.includes("<select"), "外部机抽屉不应再包含 select");
  ok(!extNew.includes("（managed"), "SSH 密钥选项应去掉英文词");
  // 触发 /mnt/c 时高级选项自动展开
  run(A, 'EXT.form.data_root = "/mnt/c/work"; render(false)');
  has(el(A, "drwp").innerHTML, '<details class="dfold" data-extadv open>', "触发 9p 警告时高级选项应自动展开");
  // 编辑态抽屉：独立危险区 .card.dz + popConfirm 删除
  const S = JSON.parse(JSON.stringify(OV));
  const ext = JSON.parse(JSON.stringify(OV.hosts[0]));
  Object.assign(ext, {id: "ext-4090", label: "ext-4090", standalone: true, platform: null, platform_kind: "other", owner: "admin",
    work_disk: {root: "/home/u/work", total: 500 * 2 ** 30, free: 8 * 2 ** 30, work_b: 120 * 2 ** 30, warn_gb: 20, stop_gb: 10, low: true,
      disk_gate: {on: true, why: "剩余不足"}, dirs: [], projects: {}, reclaimable: [{u: "projects/old"}]}});
  ext.status.disk_gate = ext.work_disk.disk_gate;
  S.hosts.push(ext);
  const C = accCtx(S, ADMIN_ME, "#/ext/ext-4090"); run(C, "ACC.at.me = 1");
  const extEd = page(C, "#/ext/ext-4090");
  has(extEd, '<section class="card dz">', "编辑态外部机抽屉应有独立危险区");
  run(C, 'extAct("delask")');
  has(el(C, "drwp").innerHTML, 'data-ext="delgo"', "点击删除应弹出 popConfirm 确认删除");
  // P1-5：本地工作盘徽标在 .v 中（不被窄屏隐藏），workDiskBlock 有复制按钮
  const hp = page(C, "#/host/ext-4090");
  const wdb = run(C, '(() => { const h = D.hosts.find(x => x.id === "ext-4090"); return workDiskBlock(h.work_disk, h); })()');
  has(wdb, 'data-copy="labmon worker clean ext-4090"', "workDiskBlock 清理命令旁应有复制按钮");
  const wdRes = run(C, 'resCard(D.hosts.find(x => x.id === "ext-4090"))');
  has(wdRes, '<span class="badge b-bad">已暂停放新任务</span></span><div class="spk"><span class="sm mut">持久保存</span></div>', "本地工作盘告警徽标应位于 .v 数值列而非隐藏的 .spk");
  // P1-6：总览算力池副标题说人话，共有池待放清单显示项目标题
  const SP = JSON.parse(JSON.stringify(OV));
  SP.pool = {pools: [{id: "p1", label: "共有池", host_id: "pool:p1", exclusive: true, online: 1, total: 1, free_gpus: 1, gpus: 2, running: 1, queued: 0, placed: 1, pending: 1, members: ["*"], hosts: []}],
    lists: {l1: {pool: "p1", project: "proj-a", root: "/r", jobs: "j.txt", head: "h1", n: 2, since: NOW - 60, why: "等待落机"}}};
  const CP = ovCtx(SP, RR);
  const homeP = page(CP, "#/");
  has(homeP, "按人公平份额自动挑机", "总览算力池副标题应说人话");
  const poolP = page(CP, "#/pool");
  has(poolP, SP.projects[0].title, "共有池待放清单应显示项目标题");
  // P1-8：主机页「给项目用」紧凑单行与抽屉项目运行/排队简况
  const SA = JSON.parse(JSON.stringify(OV));
  SA.hosts[0].status.queues = []; SA.regs = SA.regs.map(r => r.host === "srv-a" ? {...r, queues: []} : r);
  const CA = accCtx(SA, ADMIN_ME, "#/host/srv-a");
  const hAsg = page(CA, "#/host/srv-a");
  has(hAsg, '<div class="card assign"><div class="row wrap"', "给项目用应合并为紧凑单行提示条");
  const dAsg = page(CA, "#/host/srv-a/assign");
  ok(/运行 \d+|排队 \d+|空闲|暂时没实验/.test(dAsg), "给项目用抽屉列表应含项目运行/排队简况");
  // P2-10：总览超 5 条告警默认折叠可展开；storage.js 归属 select 补紧凑样式；project.js 归属表单包入 .aform
  const SAL = JSON.parse(JSON.stringify(OV));
  SAL.projects[0].alert_msgs = ["告警1", "告警2", "告警3", "告警4", "告警5", "告警6", "告警7"];
  const CAL = ovCtx(SAL, RR);
  const hFold = page(CAL, "#/");
  has(hFold, 'class="al-extra" hidden', "超 5 条告警默认应折叠");
  has(hFold, 'data-alexp aria-expanded="false"');
  run(CAL, "AL_EXP = true; render(false)");
  const hOpen = el(CAL, "view").innerHTML;
  has(hOpen, 'data-alexp aria-expanded="true"');
  has(hOpen, "收起告警");
  // storage.js select 样式与 project.js 归属表单 .aform
  const dOwn = run(A, 'diskOwner("ME")');
  if (dOwn.includes("data-dkown")) has(dOwn, 'class="ain sm-sel" data-dkown');
  page(A, "#/p/proj-a/manage"); run(A, 'pjAct("owner", "proj-a")');
  has(el(A, "drwp").innerHTML, '<div class="aform acc-pop-form"><label>归属给谁<select class="ain" data-owner', "归属表单应包入 .aform");
  // 总览时间线 GPU/CPU 分车道紧凑密度：单行高 15px、同主机行零间隙
  const OT = ovCtx(clone(OV), RR); page(OT, "#/");
  run(OT, `D.occ["srv-a"].push({p: "proj-a", n: "cpu_job", lane: "CPU", t0: ${NOW - 1800}, t1: ${NOW + 1800}, g: [], st: "run"}); drawTimeline()`);
  const tlH = Number((el(OT, "tlbox").innerHTML.match(/<svg[^>]*height="([^"]+)"/) || [])[1]);
  ok(run(OT, "OCC_CARD_LAY.base") === 15 && tlH > 0 && tlH < 120, `总览时间线应紧凑：base=${run(OT, "OCC_CARD_LAY.base")}, svgH=${tlH}`);
  // 账号表单 data-pop-field 的 change 事件：文本框失焦不触发重绘，radio 仅更新草稿不重绘
  const AT = accCtx(OV, ADMIN_ME, "#/me/tokens"); run(AT, "ACC.at.me = 1");
  page(AT, "#/me/tokens"); run(AT, 'accAct("open-tok")');
  run(AT, "RN = 0; ORIG_R = render; render = (...a) => { RN++; return ORIG_R(...a); }");
  fire(AT, "change", {type: "text", value: "新名称", dataset: {popField: "tokn"}, classList: {contains: () => false}});
  const rText = run(AT, "RN");
  fire(AT, "change", {type: "radio", value: "admin", dataset: {popField: "toks"}, classList: {contains: () => false}});
  const rRadio = run(AT, "RN"), toks = run(AT, "POP.draft.toks");
  run(AT, "render = ORIG_R");
  ok(rText === 0 && rRadio === 0 && toks === "admin", `data-pop-field change 不应重建表单且应更新 radio 草稿：rText=${rText}, rRadio=${rRadio}, toks=${toks}`);
});

ta("overview", "#241 补齐 4 处前端实用入口：Colab 引导、本地工作盘清理、单机指令撤销与普通用户空态导航", async () => {
  // 1. Colab 临时 worker 接入引导（我的账号 → 命令行）
  const meHtml = page(mkCtx(ovSrc, OV, {hash: "#/me", filename: "overview.js", path: "/pfx/"}), "#/me");
  clean(meHtml, "我的账号 Colab 引导");
  has(meHtml, "Colab / 临时上报");
  has(meHtml, 'href="api/client/ephemeral.py" download="labmon_ephemeral.py"');
  has(meHtml, 'from google.colab import userdata; os.environ[&quot;LABMON_TOKEN&quot;] = userdata.get(&quot;LABMON_TOKEN&quot;)');
  has(meHtml, "!python3 labmon_ephemeral.py run --url http://127.0.0.1:8765/pfx --project &lt;项目ID&gt; --job &lt;任务名&gt; -- python3 train.py");
  has(meHtml, "LABMON_TOKEN"); has(meHtml, 'href="#/me/tokens"');

  // 2. 独立主机本地工作盘缓存预览与清理（POST /api/worker/clean）
  const S = JSON.parse(JSON.stringify(OV));
  const ext = JSON.parse(JSON.stringify(OV.hosts[0]));
  Object.assign(ext, {
    id: "ext-1", label: "外租机-1", standalone: true, worker_up: true, error: null, owner: "alice",
    platform: null, platform_kind: "other", lw: null, lease_end: null, borrow: null,
    work_disk: {root: "/data/work", total: 200 * 1024 ** 3, free: 80 * 1024 ** 3, work_b: 12 * 1024 ** 3, warn_gb: 20, stop_gb: 10, projects: {"proj-a": {b: 8 * 1024 ** 3}}, dirs: [], reclaimable: []},
  });
  S.hosts.push(ext);
  const calls = [];
  let cleanOut = "可清理 2 项，共 1.5 GiB（目录：/data/work/.tmp；预览模式未删除）：\n  [缓存] proj-a  1.2 GiB  (/data/work/.tmp/cache/proj-a)\n  [单格临时] " + BAD;
  const fakeFetch = async (u, o) => {
    const body = o?.body ? JSON.parse(o.body) : null;
    if (u.startsWith("api/")) calls.push([u, body]);
    if (u === "api/worker/clean") return {ok: true, status: 200, json: async () => ({rc: 0, out: body.yes ? "已清理 2 项，释放 1.5 GiB" : cleanOut, err: ""})};
    if (u === "api/override/undrain") return {ok: true, status: 200, json: async () => ({rc: 0, out: "[srv-a] 已撤销 drain", err: ""})};
    if (u === "api/override/resume") return {ok: true, status: 200, json: async () => ({rc: 0, out: "[srv-a] 已恢复 proj-b", err: ""})};
    return {ok: true, status: 200, json: async () => structuredClone(S)};
  };
  const A = mkCtx(ovSrc, S, {hash: "#/host/ext-1", filename: "overview.js", fetch: fakeFetch});
  A.RR = RR; A.RTAB = "lw";
  run(A, `recs = () => ({rows: RR, gen: D.generated, more: false}); ST.get = (k, d) => k === "cardOpen" ? {"rec:host": 1} : k === "recTab" ? {host: "lw"} : d;`);
  const hp = page(A, "#/host/ext-1");
  clean(hp, "独立主机工作盘页");
  has(hp, 'data-wclean="open" data-host="ext-1" data-where="block"');
  await run(A, `wcleanAct("open", {host: "ext-1", where: "block"})`);
  const popHtml = page(A, "#/host/ext-1");
  clean(popHtml, "工作盘清理预览弹窗");
  has(popHtml, "可清理 2 项，共 1.5 GiB");
  has(popHtml, "清空范围：此主机全部项目的 <code>.tmp/{cache,jobs}</code>");
  has(popHtml, "删除后不可恢复");
  has(popHtml, "绝不触碰 <code>projects/</code>");
  has(popHtml, 'type="checkbox" data-pack');
  ok(/data-wclean="go"[^>]*disabled/.test(popHtml), "未勾选风险确认时确认按钮应禁用");
  ok(JSON.stringify(calls[0]) === JSON.stringify(["api/worker/clean", {host: "ext-1", dry_run: true}]), "预览请求参数不对：" + JSON.stringify(calls[0]));
  // 未勾选风险确认直接调 go 必须拦截
  await run(A, `wcleanAct("go", {host: "ext-1", where: "block"})`);
  ok(calls.length === 1, "未勾选风险确认不应发起清理请求");
  // 勾选风险确认后启用并可提交
  fire(A, "change", {type: "checkbox", checked: true, dataset: {pack: ""}, classList: {contains: () => false}});
  ok(!/data-wclean="go"[^>]*disabled/.test(page(A, "#/host/ext-1")), "勾选风险确认后确认按钮应启用");
  await run(A, `wcleanAct("go", {host: "ext-1", where: "block"})`);
  ok(JSON.stringify(calls[1]) === JSON.stringify(["api/worker/clean", {host: "ext-1", yes: true}]), "确认清理请求参数不对：" + JSON.stringify(calls[1]));
  has(page(A, "#/host/ext-1"), "已清理 2 项，释放 1.5 GiB");
  // 无可清理项时确认按钮禁用
  cleanOut = "没有可清理的自管缓存或已结束单格临时目录（目录：/data/work/.tmp）";
  await run(A, `wcleanAct("open", {host: "ext-1", where: "block"})`);
  const emptyPop = page(A, "#/host/ext-1");
  ok(/data-wclean="go"[^>]*disabled/.test(emptyPop), "无可清理项时确认按钮应禁用");
  // 离线主机按钮灰显
  run(A, `D.hosts.find(h => h.id === "ext-1").worker_up = false; POP = null;`);
  has(page(A, "#/host/ext-1"), 'disabled data-tip="worker 未运行或主机离线，无法清理缓存"');
  // 普通用户：多项目用紧凑药丸选项目（无下拉框），自动带 project；无权主机或只读用户不显示
  const userMe = {name: "alice", role: "user", role_name: "普通用户", scopes: ["project", "read"], admin: false, ops: false, write: true, account: true, hosts: ["ext-1", "srv-a"], own: ["proj-a", "proj-b"], pools: []};
  const uCalls = [];
  const U = mkCtx(ovSrc, S, {hash: "#/host/ext-1", filename: "overview.js", me: userMe, fetch: async (u, o) => {
    const body = o?.body ? JSON.parse(o.body) : null;
    if (u.startsWith("api/")) uCalls.push([u, body]);
    return {ok: true, status: 200, json: async () => ({rc: 0, out: "可清理 1 项，共 200 MiB", err: ""})};
  }});
  await run(U, `wcleanAct("open", {host: "ext-1", where: "block"})`);
  const uPop = run(U, `workDiskBlock(D.hosts.find(h => h.id === "ext-1").work_disk, D.hosts.find(h => h.id === "ext-1"))`);
  ok(!uPop.includes("<select"), "普通用户选项目不应使用下拉框");
  has(uPop, 'class="seg form-seg"'); has(uPop, 'data-wclean="proj"'); has(uPop, 'data-p="proj-a"'); has(uPop, 'data-p="proj-b"');
  has(uPop, "清空范围：项目 <code>proj-a</code> 的 <code>.tmp/{cache,jobs}</code>");
  ok(JSON.stringify(uCalls[0]) === JSON.stringify(["api/worker/clean", {host: "ext-1", project: "proj-a", dry_run: true}]), "普通用户应带 project：" + JSON.stringify(uCalls[0]));
  const roUser = {...userMe, write: false, scopes: ["read"]};
  const RO = mkCtx(ovSrc, S, {hash: "#/host/ext-1", filename: "overview.js", me: roUser});
  ok(!run(RO, `workCleanPop(D.hosts.find(h => h.id === "ext-1"), "block")`), "只读用户不应显示清理按钮");

  // 3. 临时指令表单机撤销按钮（POST /api/override/undrain & /api/override/resume）
  calls.length = 0;
  run(A, `D.hosts[0].status.overrides = [{kind: "drain", target: null}, {kind: "pause", target: "proj-b", by: "boss"}]; POP = null;`);
  run(A, `ovrUndoAct("open", {kind: "drain", host: "srv-a", target: ""})`);
  await run(A, `ovrUndoAct("go", {kind: "drain", host: "srv-a", target: ""})`);
  ok(JSON.stringify(calls[0]) === JSON.stringify(["api/override/undrain", {target: "srv-a"}]), "undrain 请求不对：" + JSON.stringify(calls[0]));
  run(A, `ovrUndoAct("open", {kind: "pause", host: "srv-a", target: "proj-b"})`);
  await run(A, `ovrUndoAct("go", {kind: "pause", host: "srv-a", target: "proj-b"})`);
  ok(JSON.stringify(calls[1]) === JSON.stringify(["api/override/resume", {target: "proj-b", hosts: ["srv-a"]}]), "resume 请求不对：" + JSON.stringify(calls[1]));
  // 普通用户权限边界：自己下的单机 pause 可撤；别人下的单机 pause 与单机 drain 保留只读 CLI
  const uOvr = run(U, `ovrTable([
    {kind: "pause", target: "proj-a", by: "alice", h: D.hosts[0]},
    {kind: "pause", target: "proj-a", by: "boss", h: D.hosts[0]},
    {kind: "drain", target: null, h: D.hosts[0]},
  ], true)`);
  clean(uOvr, "普通用户临时指令表");
  ok((uOvr.match(/data-ovr="open"/g) || []).length === 1, "普通用户只能撤销自己下的单机 pause：" + uOvr);
  has(uOvr, "labmon resume proj-a --host srv-a</code>");
  has(uOvr, "labmon undrain srv-a</code>");

  // 4. 普通用户空态导航闭环
  const emptySnap = JSON.parse(JSON.stringify(OVS));
  emptySnap.hosts = []; emptySnap.projects = []; emptySnap.pool = {pools: []};
  emptySnap.fleet = {scope: "self", mine: [], servers: [{id: "srv-me", holder: "alice", off: true, manage: {on: false, opt_out: false}}]};
  const meEmpty = {...userMe, hosts: [], own: []};
  const E1 = mkCtx(ovSrc, emptySnap, {hash: "#/", filename: "overview.js", me: meEmpty});
  const h1 = page(E1, "#/");
  clean(h1, "普通用户有组员机空态");
  has(h1, 'href="#/fleet">前往我的服务器</a>');
  has(h1, 'data-drw="#/ext/new">+ 添加外部主机</button>');
  // 有项目但无主机（进入主机表空态行）
  emptySnap.projects = [OV.projects[0]];
  const E2 = mkCtx(ovSrc, emptySnap, {hash: "#/", filename: "overview.js", me: meEmpty});
  const h2 = page(E2, "#/");
  clean(h2, "普通用户主机表空态");
  has(h2, "当前没有可用主机");
  has(h2, 'href="#/fleet">前往我的服务器</a>接入名下组员机');
  has(h2, 'data-drw="#/ext/new">+ 添加外部主机</button>');
  // 只读普通用户不显示添加外部主机
  const E3 = mkCtx(ovSrc, emptySnap, {hash: "#/", filename: "overview.js", me: {...meEmpty, write: false, scopes: ["read"]}});
  ok(!page(E3, "#/").includes("添加外部主机"), "只读用户空态不应显示添加外部主机");
});

Promise.all(PEND).then(() => process.stdout.write(JSON.stringify(R)));
