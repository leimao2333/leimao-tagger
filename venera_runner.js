#!/usr/bin/env node
/**
 * Venera 漫画源配置执行器（Node 22）。
 *
 * 用法：node venera_runner.js <config.js 路径> '<job JSON>'
 *   job: {"mode":"info"}
 *      | {"mode":"explore","entry":0,"page":1}
 *      | {"mode":"category","category":"","param":"tag:x","options":[],"page":1}
 * 输出：stdout 一行 JSON：{"ok":true,...} 或 {"ok":false,"error":"..."}
 *
 * Shim：Network（fetch + cookie jar）/ HtmlDocument（cheerio）/ Comic /
 * ComicSource / Convert（node crypto）/ UI（桩）/ localStorage（桩）。
 */
"use strict";

const fs = require("fs");
const path = require("path");
const crypto = require("crypto");

// ---------- 输出工具：配置里的 console 只进 stderr，stdout 只留结果 JSON ----------
const rawLog = (...a) => process.stderr.write(a.join(" ") + "\n");
console.log = rawLog;
console.info = rawLog;
console.warn = rawLog;
console.error = rawLog;

function fail(msg) {
  process.stdout.write(JSON.stringify({ ok: false, error: String(msg) }));
  process.exit(0);
}

// ---------- Network：fetch + 域名级 cookie jar ----------
const cookieJar = new Map(); // host -> {name: value}

function jarFor(host) {
  if (!cookieJar.has(host)) cookieJar.set(host, {});
  return cookieJar.get(host);
}

function storeCookies(host, resp) {
  const jar = jarFor(host);
  const raw = resp.headers.getSetCookie ? resp.headers.getSetCookie() : [];
  for (const line of raw) {
    const [pair] = line.split(";");
    const eq = pair.indexOf("=");
    if (eq > 0) jar[pair.slice(0, eq).trim()] = pair.slice(eq + 1).trim();
  }
}

function cookieHeader(host) {
  const jar = jarFor(host);
  const kv = Object.entries(jar).map(([k, v]) => `${k}=${v}`);
  return kv.length ? kv.join("; ") : "";
}

async function doFetch(url, method, headers, body) {
  const host = new URL(url).host;
  const h = { ...(headers || {}) };
  const ck = cookieHeader(host);
  if (ck && !h["Cookie"] && !h["cookie"]) h["Cookie"] = ck;
  if (body !== undefined && body !== null && !h["Content-Type"] && !h["content-type"]) {
    h["Content-Type"] = "application/json";
  }
  const resp = await fetch(url, { method, headers: h, body, redirect: "follow" });
  storeCookies(host, resp);
  const text = await resp.text();
  const hs = {};
  resp.headers.forEach((v, k) => (hs[k] = v));
  return { status: resp.status, headers: hs, body: text, url: resp.url };
}

function encodeBody(headers, data) {
  if (data === undefined || data === null) return undefined;
  const ct = String(
    (headers && (headers["Content-Type"] || headers["content-type"])) || "",
  ).toLowerCase();
  if (typeof data === "string") return data;
  if (ct.includes("x-www-form-urlencoded")) return new URLSearchParams(data).toString();
  return JSON.stringify(data);
}

const DEFAULT_UA =
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36";

const Network = {
  async get(url, headers) {
    return doFetch(url, "GET", { "User-Agent": DEFAULT_UA, ...(headers || {}) });
  },
  async post(url, headers, data) {
    const h = { "User-Agent": DEFAULT_UA, ...(headers || {}) };
    return doFetch(url, "POST", h, encodeBody(h, data));
  },
  async put(url, headers, data) {
    const h = { "User-Agent": DEFAULT_UA, ...(headers || {}) };
    return doFetch(url, "PUT", h, encodeBody(h, data));
  },
  async patch(url, headers, data) {
    const h = { "User-Agent": DEFAULT_UA, ...(headers || {}) };
    return doFetch(url, "PATCH", h, encodeBody(h, data));
  },
  async delete(url, headers) {
    return doFetch(url, "DELETE", { "User-Agent": DEFAULT_UA, ...(headers || {}) });
  },
  async sendRequest(method, url, headers, payload) {
    const m = String(method || "GET").toUpperCase();
    const h = { "User-Agent": DEFAULT_UA, ...(headers || {}) };
    return doFetch(url, m, h, encodeBody(h, payload));
  },
  async fetchBytes(url, headers) {
    const host = new URL(url).host;
    const ck = cookieHeader(host);
    const h = { "User-Agent": DEFAULT_UA, ...(headers || {}) };
    if (ck && !h["Cookie"] && !h["cookie"]) h["Cookie"] = ck;
    const resp = await fetch(url, { headers: h });
    storeCookies(host, resp);
    const buf = await resp.arrayBuffer();
    return { status: resp.status, headers: {}, bytes: new Uint8Array(buf) };
  },
  setCookies(cookies) {
    for (const c of cookies || []) {
      const host = (c.domain || "").replace(/^\./, "");
      if (!host) continue;
      jarFor(host)[c.name] = c.value;
    }
  },
  getCookies(url) {
    try {
      return Object.entries(jarFor(new URL(url).host)).map(([k, v]) => ({ name: k, value: v }));
    } catch {
      return [];
    }
  },
  deleteCookies(url) {
    try {
      cookieJar.delete(new URL(url).host);
    } catch {
      /* ignore */
    }
  },
};

// ---------- HtmlDocument：cheerio 实现 ----------
let cheerio = null;
try {
  cheerio = require("cheerio");
} catch {
  /* 没有 cheerio 时 HtmlDocument 不可用，配置会报错并返回 error */
}

class VenElement {
  constructor($, el) {
    this.$ = $;
    this.el = el;
  }
  get attributes() {
    return this.el.attribs || {};
  }
  get text() {
    return this.$(this.el).text();
  }
  get innerHTML() {
    return this.$(this.el).html() || "";
  }
  get children() {
    return this.$(this.el)
      .children()
      .toArray()
      .map((e) => new VenElement(this.$, e));
  }
  get parentElement() {
    const p = this.el.parent;
    return p && p.type === "tag" ? new VenElement(this.$, p) : null;
  }
  querySelector(sel) {
    const e = this.$(this.el).find(sel).get(0);
    return e ? new VenElement(this.$, e) : null;
  }
  querySelectorAll(sel) {
    const out = [];
    this.$(this.el)
      .find(sel)
      .each((_, e) => out.push(new VenElement(this.$, e)));
    return out;
  }
}

class HtmlDocument {
  constructor(html) {
    if (!cheerio) throw "HtmlDocument 需要 cheerio（NODE_PATH 未配置）";
    this.$ = cheerio.load(html || "");
  }
  querySelector(sel) {
    const e = this.$(sel).get(0);
    return e ? new VenElement(this.$, e) : null;
  }
  querySelectorAll(sel) {
    const out = [];
    this.$(sel).each((_, e) => out.push(new VenElement(this.$, e)));
    return out;
  }
  getElementById(id) {
    const e = this.$(`[id="${String(id).replace(/"/g, '\\"')}"]`).get(0);
    return e ? new VenElement(this.$, e) : null;
  }
  dispose() {}
}

// ---------- Comic / ComicSource ----------
class Comic {
  constructor(o) {
    if (o && typeof o === "object") Object.assign(this, o);
  }
}

// 实例级 KV 存储（loadData/saveData），进程内有效即可
const __srcStore = new Map();

class ComicSource {
  init() {}

  // loadData/saveData/deleteData：按 source key 隔离的内存存储
  loadData(dataKey) {
    const m = __srcStore.get(this.key);
    if (!m || !m.has(dataKey)) return null;
    return m.get(dataKey);
  }
  saveData(dataKey, data) {
    let m = __srcStore.get(this.key);
    if (!m) { m = new Map(); __srcStore.set(this.key, m); }
    m.set(dataKey, data);
  }
  deleteData(dataKey) {
    const m = __srcStore.get(this.key);
    if (m) m.delete(dataKey);
  }

  // loadSetting：从 settings 数组按 id 匹配取 default 值
  loadSetting(key) {
    const arr = Array.isArray(this.settings) ? this.settings : [];
    for (const item of arr) {
      if (item && typeof item === "object" && (item.id === key || item.key === key)) {
        if (item.default !== undefined) return item.default;
        if (item.value !== undefined) return item.value;
        return null;
      }
    }
    return null;
  }
  saveSetting() {}

  get isLogged() {
    return false;
  }

  // translate：translation[locale][key] ?? key
  translate(key) {
    const locale = (typeof APP !== "undefined" && APP && APP.locale) || "zh_CN";
    const t = this.translation;
    if (t && typeof t === "object") {
      const byLocale = t[locale] || t[locale.split("_")[0]];
      if (byLocale && typeof byLocale === "object" && byLocale[key] !== undefined) {
        return byLocale[key];
      }
      if (t[key] !== undefined) return t[key]; // 兼容不带 locale 的写法
    }
    return key;
  }
}

// ---------- Convert：node crypto ----------
function toBuf(data) {
  if (data instanceof Uint8Array) return Buffer.from(data);
  if (data instanceof ArrayBuffer) return Buffer.from(new Uint8Array(data));
  if (typeof data === "string") return Buffer.from(data, "utf8");
  return Buffer.from(data);
}

const Convert = {
  encodeUtf8: (s) => new Uint8Array(Buffer.from(String(s), "utf8")),
  decodeUtf8: (b) => Buffer.from(toBuf(b)).toString("utf8"),
  encodeBase64: (d) => Buffer.from(toBuf(d)).toString("base64"),
  decodeBase64: (s) => new Uint8Array(Buffer.from(String(s), "base64")),
  hexEncode: (d) => Buffer.from(toBuf(d)).toString("hex"),
  md5: (d) => crypto.createHash("md5").update(toBuf(d)).digest("hex"),
  sha256: (d) => crypto.createHash("sha256").update(toBuf(d)).digest("hex"),
  sha512: (d) => crypto.createHash("sha512").update(toBuf(d)).digest("hex"),
  hmacString: (key, data, algo) =>
    crypto
      .createHmac(String(algo || "sha256").replace("-", ""), String(key))
      .update(String(data))
      .digest("hex"),
  encryptAesCbc: (data, key, iv) =>
    new Uint8Array(
      crypto
        .createCipheriv("aes-128-cbc", toBuf(key), toBuf(iv))
        .update(toBuf(data)),
    ),
  decryptAesCbc: (data, key, iv) => {
    const k = toBuf(key);
    const alg = k.length === 32 ? "aes-256-cbc" : k.length === 24 ? "aes-192-cbc" : "aes-128-cbc";
    const dec = crypto.createDecipheriv(alg, k, toBuf(iv));
    dec.setAutoPadding(false);
    return new Uint8Array(Buffer.concat([dec.update(toBuf(data)), dec.final()]));
  },
  decryptAesEcb: (data, key) => {
    const k = toBuf(key);
    const alg = k.length === 32 ? "aes-256-ecb" : k.length === 24 ? "aes-192-ecb" : "aes-128-ecb";
    const dec = crypto.createDecipheriv(alg, k, null);
    dec.setAutoPadding(false);
    return new Uint8Array(Buffer.concat([dec.update(toBuf(data)), dec.final()]));
  },
};

// ---------- UI / 存储桩 ----------
const UI = {
  showMessage: () => {},
  showDialog: () => {},
  showLoading: () => {},
  cancelLoading: () => {},
  launchUrl: () => {},
  showInputDialog: async () => "",
};
const DataStorage = class {
  constructor() {
    this.m = new Map();
  }
  get(k, d) {
    return this.m.has(k) ? this.m.get(k) : d;
  }
  set(k, v) {
    this.m.set(k, v);
  }
  delete(k) {
    this.m.delete(k);
  }
};
const __lsMap = new Map();
const localStorage = {
  getItem: (k) => (__lsMap.has(k) ? __lsMap.get(k) : null),
  setItem: (k, v) => __lsMap.set(k, String(v)),
  removeItem: (k) => __lsMap.delete(k),
  clear: () => __lsMap.clear(),
};

// ---------- 全局函数 / 全局对象（官方 _venera_.js API 面） ----------
const APP = {
  locale: "zh_CN",
  platform: "windows",
  isAndroid: false,
  isIOS: false,
  isWindows: true,
  isLinux: false,
  isMacOS: false,
  version: "1.0.0",
};

// 全局 document 桩（个别源在 JS 里直接用 document.getElementById）
const document = {
  getElementById: () => null,
  querySelector: () => null,
  querySelectorAll: () => [],
  createElement: () => ({ style: {}, setAttribute() {}, appendChild() {} }),
  body: { appendChild() {} },
  cookie: "",
};

function createUuid() {
  return crypto.randomUUID();
}
function randomInt(min, max) {
  min = Math.ceil(Number(min) || 0);
  max = Math.floor(Number(max) || 0);
  if (max <= min) return min;
  return min + Math.floor(Math.random() * (max - min));
}
function randomDouble(min, max) {
  min = Number(min) || 0;
  max = Number(max) || 0;
  if (max <= min) return min;
  return min + Math.random() * (max - min);
}
// Cookie：官方是 app 侧 cookie 管理，这里直接接到 Network 的 cookie jar
const Cookie = {
  set: (url, cookies) => Network.setCookies(cookies || []),
  get: (url) => Network.getCookies(url),
  delete: (url) => Network.deleteCookies(url),
};
// log(level, title, content) → stderr
function log(level, title, content) {
  rawLog("[venera-log]", level || "", title || "", content !== undefined ? content : "");
}
function setClipboard() {}
function getClipboard() { return ""; }
// compute(func, args)：把函数序列化后在本地执行
async function compute(func, args) {
  const f = typeof func === "string" ? eval(`(${func})`) : func;
  return f(...(args || []));
}

// ---------- 主流程 ----------
(async () => {
  const [configPath, jobJson] = process.argv.slice(2);
  if (!configPath || !jobJson) fail("参数不足");
  let job;
  try {
    job = JSON.parse(jobJson);
  } catch (e) {
    fail("job JSON 无效: " + e);
    return;
  }
  let code;
  try {
    code = fs.readFileSync(path.resolve(configPath), "utf8");
  } catch (e) {
    fail("读取配置失败: " + e);
    return;
  }
  const m = code.match(/class\s+(\w+)\s+extends\s+ComicSource/);
  if (!m) fail("未找到 extends ComicSource 的类");
  let src;
  try {
    const factory = new Function(
      "Network", "HtmlDocument", "Comic", "ComicSource", "Convert", "UI",
      "DataStorage", "localStorage", "globalThis",
      "APP", "document", "createUuid", "randomInt", "randomDouble",
      "Cookie", "log", "setClipboard", "getClipboard", "compute",
      code + `\n;return new ${m[1]}();`,
    );
    src = factory(
      Network, HtmlDocument, Comic, ComicSource, Convert, UI,
      DataStorage, localStorage, globalThis,
      APP, document, createUuid, randomInt, randomDouble,
      Cookie, log, setClipboard, getClipboard, compute,
    );
  } catch (e) {
    fail("配置加载失败: " + e);
    return;
  }

  const normComics = (r) => {
    let list = [];
    let maxPage = 0;
    if (Array.isArray(r)) {
      // multiPartPage: [{title, comics, viewMore}, ...] —— 展开所有分段的 comics
      list = r.flatMap((p) => {
        if (p && typeof p === "object" && p.comics !== undefined) {
          const inner = p.comics;
          if (Array.isArray(inner)) return inner;
          if (inner && typeof inner === "object") return Object.values(inner).flat();
        }
        return [p];
      });
    } else if (r && typeof r === "object") {
      list = r.comics !== undefined ? r.comics : [];
      maxPage = Number(r.maxPage || r.maxpage || 0) || 0;
      if (!Array.isArray(list) && typeof list === "object") {
        list = Object.values(list).flat(); // multiPart: {partName: comics[]}
      }
    }
    const comics = (Array.isArray(list) ? list : [])
      .map((c) => (c && typeof c === "object" ? c : {}))
      .filter((c) => c && (c.id !== undefined || c.title || c.name))
      .map((c) => ({
        id: String(c.id ?? c.comicId ?? c.title ?? ""),
        title: String(c.title ?? c.name ?? c.id ?? ""),
        cover: String(c.cover ?? c.coverUrl ?? c.image ?? c.subTitle ?? ""),
      }))
      .filter((c) => c.title && c.cover);
    return { comics, maxPage };
  };

  try {
    // 官方 app 会先调用 init()（很多源在这里挂辅助方法，如 this.fetchJson）
    if (typeof src.init === "function") {
      await src.init();
    }
    if (job.mode === "info") {
      process.stdout.write(
        JSON.stringify({
          ok: true,
          name: String(src.name || ""),
          key: String(src.key || ""),
          entries: Array.isArray(src.explore) ? src.explore.length : 0,
          hasCategory: !!src.categoryComics,
          hasSearch: !!src.search,
        }),
      );
      return;
    }
    if (job.mode === "explore") {
      const entries = Array.isArray(src.explore) ? src.explore : [];
      const e = entries[job.entry | 0];
      if (!e || typeof e.load !== "function") fail("explore 条目不存在");
      const r = await e.load(job.page || 1);
      process.stdout.write(JSON.stringify({ ok: true, ...normComics(r) }));
      return;
    }
    if (job.mode === "category") {
      if (!src.categoryComics || typeof src.categoryComics.load !== "function")
        fail("categoryComics 不可用");
      const r = await src.categoryComics.load(
        job.category || "", job.param || "", job.options || [], job.page || 1,
      );
      process.stdout.write(JSON.stringify({ ok: true, ...normComics(r) }));
      return;
    }
    if (job.mode === "search") {
      if (!src.search || typeof src.search.load !== "function") fail("search 不可用");
      const r = await src.search.load(job.keyword || "", job.page || 1);
      process.stdout.write(JSON.stringify({ ok: true, ...normComics(r) }));
      return;
    }
    fail("未知 mode: " + job.mode);
  } catch (e) {
    fail(typeof e === "string" ? e : (e && e.message) || JSON.stringify(e));
  }
})();
