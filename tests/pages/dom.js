// 页面测试用的最小 DOM 桩：让 overview.html 与 page.html 的 <script> 能在 node 的 vm 里跑起来、把渲染函数的输出拿出来检查。
// 只实现页面脚本在加载与渲染时真会碰到的接口；取到的元素按 id 记住，渲染后从 innerHTML 读结果。
"use strict";
const vm = require("vm");

function mkEl(tag = "div") {
  const el = {
    tagName: tag.toUpperCase(), id: "", innerHTML: "", textContent: "", value: "", href: "", src: "", title: "", hidden: false, type: "",
    scrollTop: 0, offsetHeight: 20, offsetWidth: 900, clientWidth: 900, clientHeight: 400, isConnected: true, selectionStart: 0, selectionEnd: 0,
    style: {display: "none", setProperty() {}}, dataset: {}, children: [], attrs: {},
    classList: {_s: new Set(), add(...c) { c.forEach(x => this._s.add(x)); }, remove(...c) { c.forEach(x => this._s.delete(x)); },
                toggle(c, on) { (on ?? !this._s.has(c)) ? this._s.add(c) : this._s.delete(c); return this._s.has(c); }, contains(c) { return this._s.has(c); }},
    append(...xs) { el.children.push(...xs); xs.forEach(x => { x.parentElement = el; }); }, appendChild(x) { el.children.push(x); return x; },
    setAttribute(k, v) { el.attrs[k] = String(v); }, getAttribute(k) { return el.attrs[k] ?? null; }, removeAttribute(k) { delete el.attrs[k]; },
    toggleAttribute(k, on) { on ? (el.attrs[k] = "") : delete el.attrs[k]; },
    querySelector() { return null; }, querySelectorAll() { return []; }, getBoundingClientRect() { return {left: 0, top: 0, width: 900, height: 400, right: 900, bottom: 400}; },
    contains() { return false; }, closest() { return null; }, matches() { return false; },
    focus() {}, blur() {}, click() {}, scrollIntoView() {}, addEventListener() {}, removeEventListener() {},
    remove() { const p = el.parentElement; if (p) p.children = p.children.filter(x => x !== el); el.parentElement = null; },
  };
  return el;
}

function mkDoc() {
  const byId = new Map();
  const doc = {
    getElementById(id) { if (!byId.has(id)) { const e = mkEl(); e.id = id; byId.set(id, e); } return byId.get(id); },
    querySelector() { return null; }, querySelectorAll() { return []; },
    addEventListener(type, fn, opt) { (doc.on[type] ||= []).push({fn, cap: opt === true || !!opt?.capture}); }, removeEventListener() {}, createElement: mkEl,
    documentElement: mkEl("html"), body: mkEl("body"), title: "", hidden: false, visibilityState: "visible", on: {},
  };
  doc.activeElement = doc.body;
  return doc;
}

function mkStorage() {
  const m = new Map();
  return {getItem: k => m.has(k) ? m.get(k) : null, setItem: (k, v) => m.set(k, String(v)), removeItem: k => m.delete(k), clear: () => m.clear()};
}

// 固定的时钟：页面里的 Date.now() 与 new Date()（不带参数）都取 ms。check.js 开头用快照的 generated 调一次 freezeClock，
// node 主进程与各页面上下文同一个“现在”：页面上“今天”、tsNear、whenPick 都按它算，跑测试时跨过午夜（快照在 23:59 生成、node 过了 0 点才跑）也不变
function freezeClock(ms) {
  const Real = Date;
  globalThis.Date = class extends Real {
    constructor(...a) { if (a.length) super(...a); else super(ms); }
    static now() { return ms; }
  };
}

// 看板账号（#160）的缺省：管理员、从内网入口来（平台操作与写操作都能做），与 users.me 同形
const ADMIN_ME = {name: "boss", fleet_person: "ME", role: "admin", role_name: "管理员", scopes: ["admin", "backup", "deploy", "project", "read"], via: "session", entry: "lan", entry_name: "内网入口",
  admin: true, ops: true, write: true, account: true, sessions: [], tokens: [], token_scopes: ["read", "project", "deploy", "backup", "admin"],
  scope_info: {read: ["只读", "看页面与数据"], project: ["项目", "项目的写操作"], deploy: ["部署", "部署脚本"], backup: ["备份", "备份脚本"], admin: ["管理员", "全部；1 小时过期"]}};
// 建一个上下文并把页面脚本跑起来：脚本第一行 `let D = …;` 换成传入的快照（与 collect.write_page 同样把 < 转义）
// protocol：缺省 http:（经看板服务打开，LIVE 为真）；file: 是直接打开渲染出的文件；embed：项目页嵌在工作台的 iframe 里（window.top 不是自己）
// me：页面上的 ME（看板服务发页面时填的「我是谁」）；不给时模板里还没填的按管理员（ADMIN_ME），看板服务已填好的（部署时的 smoke.js）照原样
// fetch：页面一装进来就拿到的 fetch（common.js 在它外面包一层：401 回登录页）；缺省永远不回
// path：经看板服务打开时的路径（缺省 /：工作台在看板根）；反代加了前缀的写 /pfx/、/pfx/p/proj-a/（#165）
function mkCtx(script, D, {hash = "#/", filename = "page.js", protocol = "http:", embed = false, me, fetch, path = "/"} = {}) {
  const ctx = {
    document: mkDoc(), console, Date, URLSearchParams, URL, CSS: {escape: s => s}, structuredClone,
    location: {protocol, hash, pathname: protocol === "file:" ? "/x/index.html" : path, search: "", href: (protocol === "file:" ? "file:///x/index.html" : "http://127.0.0.1:8765" + path) + hash,
               reload() {}, replace(u) { this.href = u; }},
    history: {state: null, replaceState(state, _, href) { if (href?.startsWith("#")) ctx.location.hash = href; }, pushState() {}, go() {}, back() {}},
    localStorage: mkStorage(), sessionStorage: mkStorage(),
    matchMedia: () => ({matches: false, addEventListener() {}}), getSelection: () => ({isCollapsed: true, anchorNode: null}),
    addEventListener() {}, removeEventListener() {}, navigator: {}, innerWidth: 1200, innerHeight: 800, scrollY: 0, scrollTo() {},
    setTimeout: () => 0, clearTimeout() {}, setInterval: () => 0, clearInterval() {}, requestAnimationFrame: () => 0,
    fetch: fetch || (() => new Promise(() => {})),
  };
  ctx.window = ctx.top = ctx.self = ctx.parent = ctx.globalThis = ctx;
  if (embed) ctx.top = ctx.parent = {frameChanged() {}};
  vm.createContext(ctx);
  const data = JSON.stringify(D).replace(/</g, "\\u003c");
  let src = script.replace(/^let D = .*$/m, `let D = ${data};`);
  if (src === script) throw new Error("脚本里没有 `let D = …` 这一行");
  if (me !== undefined || /^let ME = \/\*__ME__\*\/null;/m.test(src)) src = src.replace(/^let ME = .*$/m, `let ME = ${JSON.stringify(me === undefined ? ADMIN_ME : me).replace(/</g, "\\u003c")};`);
  vm.runInContext(src, ctx, {filename});
  return ctx;
}
const run = (ctx, code) => vm.runInContext(code, ctx);
// 在页面的 document 上分发一个事件（页面脚本登记的监听都记在 document.on）：先捕获阶段的、再冒泡的，target 在 document 里面时真浏览器就是这个顺序
// extra：事件上的别的字段（clientX、pointerId、key 之类）
function fire(ctx, type, target, extra = {}) {
  let stop = false;
  const ev = {type, target, preventDefault() {}, stopPropagation() { stop = true; }, stopImmediatePropagation() { stop = true; }, ...extra};
  for (const cap of [true, false]) for (const l of ctx.document.on[type] || []) if (l.cap === cap && !stop) l.fn(ev);
  return ev;
}

// 渲染结果的文字里漏出的 NaN / undefined / Infinity：返回前后一段上下文，没有返回 null（check.js 与部署时的 smoke.js 共用）
const leak = html => { const m = /\b(NaN|undefined|Infinity)\b/.exec(html); return m ? html.slice(Math.max(0, m.index - 80), m.index + 40) : null; };

module.exports = {mkEl, mkDoc, mkCtx, run, fire, freezeClock, leak, ADMIN_ME};
