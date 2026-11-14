// 部署后的页面冒烟检查（tools/deploy.py 调，Issue #75）：把一个“看板服务布局”的目录里的两种页面装进 dom.js 的桩，按路由渲染，
// 抛异常或文字里漏 NaN/undefined/Infinity 的记为不过。结果以 JSON 打到标准输出：[{w: 哪一页哪个路由, ok, d: 没过时的说明}]。
//   node smoke.js <目录>
// 目录布局照看板服务（tests/pages/preview.py 的 build 渲染出的也一样）：
//   index.html、overview.json、records.json（可缺）；p/<项目>/index.html、snapshot.json、events.json
// 断言只有这两条（不报错、不漏），页面细节的回归在 check.js。
"use strict";
const fs = require("fs"), path = require("path");
const {mkCtx, run, freezeClock, leak} = require("./dom.js");

const W = process.argv[2];
const rd = f => fs.readFileSync(path.join(W, f), "utf8");
const J = (f, d) => fs.existsSync(path.join(W, f)) ? JSON.parse(rd(f)) : d;
const R = [];
function t(w, fn) {
  try {
    const l = leak(fn());
    R.push(l ? {w, ok: false, d: "文字里有 " + JSON.stringify(l)} : {w, ok: true});
  } catch (e) { R.push({w, ok: false, d: String(e && e.stack || e).split("\n").slice(0, 3).join(" | ").slice(0, 400)}); }
}
function script(f) {               // 渲染好的页面只有一个 <script>
  const m = [...rd(f).matchAll(/<script>([\s\S]*?)<\/script>/g)];
  if (m.length !== 1) throw new Error(`${f} 有 ${m.length} 个 <script>，应是 1 个`);
  return m[0][1];
}

const D = J("overview.json", null);
if (!D) { process.stdout.write(JSON.stringify([{w: "overview.json", ok: false, d: "没有 overview.json"}])); process.exit(0); }
freezeClock((D.generated || 0) * 1000);       // “现在”取快照的生成时刻，与 check.js 一样

/* 工作台：各路由整页渲染（顶栏 + 正文 + 抽屉） */
let O = null;
t("工作台装载", () => {
  O = mkCtx(script("index.html"), D, {hash: "#/", filename: "overview.js"});
  const rec = J("records.json", {rows: [], more: false, cats: {}});
  O.RR = rec;
  run(O, `recs = q => ({rows: RR.rows || [], gen: D.generated, more: !!RR.more});
    LOG = {key: "", rows: RR.rows || [], cats: RR.cats || {}, gen: D.generated, more: !!RR.more}`);
  return "";
});
if (O) {
  const F = D.fleet || {}, enc = encodeURIComponent;
  const routes = ["#/", ...(D.hosts || []).map(h => "#/host/" + enc(h.id)), "#/log"];
  if (D.fleet) {
    routes.push("#/fleet", "#/fleet/people", "#/fleet/disk", "#/fleet/guard");
    for (const s of [...(F.servers || []), ...(F.mine || [])]) routes.push("#/fleet/srv/" + enc(s.id));
  }
  for (const r of routes) t("工作台 " + r, () => {
    O.location.hash = r;
    run(O, "render(true)");
    const g = id => O.document.getElementById(id).innerHTML;
    return [g("top"), g("view"), run(O, 'DKEY ? document.getElementById("drwp").innerHTML : ""')].join("\n");
  });
}

/* 项目页：各项目整页渲染 */
const pd = path.join(W, "p");
for (const id of fs.existsSync(pd) ? fs.readdirSync(pd).sort() : []) {
  const f = p => path.join("p", id, p);
  t("项目页 " + id, () => {
    const P = mkCtx(script(f("index.html")), J(f("snapshot.json")), {filename: "page.js"});
    P.EVD = J(f("events.json"), null);
    run(P, "EV = EVD; render()");
    return P.document.getElementById("app").innerHTML;
  });
}

process.stdout.write(JSON.stringify(R));
