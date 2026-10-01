/* WinVoice 设置手册前端:vanilla JS,无构建。所有请求带 token。
 * 业务逻辑与上一版一致;渲染目标改为书页内的卡片与控件。 */

"use strict";

const token = new URLSearchParams(location.search).get("token") || "";
const headers = { "X-WV-Token": token, "Content-Type": "application/json" };

const state = {
  meta: { effects: {}, builtin_apps: [] },
  values: { "tools.apps": [], "tools.sensitive_apps": [], "kws.keywords": [] },
};

// 脏键:只保存用户真正改过的键——页面没碰过的键不写回,
// 这样配置文件在别处(脚本/手工)的改动不会被旧状态覆盖。
const dirty = new Set();
const markDirty = (key) => dirty.add(key);

const $ = (id) => document.getElementById(id);

async function api(path, options) {
  const response = await fetch(path + (path.includes("?") ? "&" : "?") + "token=" + token, options);
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw Object.assign(new Error(payload.error || "请求失败"), { payload, status: response.status });
  return payload;
}

function setStatus(text, kind) {
  const el = $("status");
  el.textContent = text;
  el.className = "status" + (kind ? " " + kind : "");
}

/* ── 加载 ─────────────────────────────────────────────────────── */

async function load() {
  const [meta, config] = await Promise.all([api("/api/v1/meta"), api("/api/v1/config")]);
  state.meta = meta;
  state.values = config.values;
  if (meta.version) $("colophon-version").textContent = "v" + meta.version;
  renderAll();
}

function renderAll() {
  renderApps();
  renderSensitive();
  renderKws();
  renderVoice();
  renderGeneral();
}

/* ── 应用卡片 ─────────────────────────────────────────────────── */

function appEntries() {
  return state.values["tools.apps"] || [];
}

function renderApps() {
  const host = $("app-cards");
  host.textContent = "";
  appEntries().forEach((entry, index) => host.appendChild(appCard(entry, index)));
}

function appCard(entry, index) {
  const card = document.createElement("div");
  card.className = "app-card";

  const main = document.createElement("div");
  main.className = "app-main";
  const labelInput = textInput(entry.label || "", "说名(微信)");
  labelInput.addEventListener("input", () => { appEntries()[index].label = labelInput.value; markDirty("tools.apps"); });
  const commandInput = textInput(entry.command || "", "启动命令(C:\\...\\Weixin.exe)");
  commandInput.addEventListener("input", () => { appEntries()[index].command = commandInput.value; markDirty("tools.apps"); });
  main.append(labelInput, commandInput);

  const extra = document.createElement("div");
  extra.className = "app-extra";
  const imageInput = textInput(entry.image || "", "进程映像名(可选,Weixin.exe)");
  imageInput.addEventListener("input", () => { appEntries()[index].image = imageInput.value; markDirty("tools.apps"); });
  const guestLabel = document.createElement("label");
  guestLabel.className = "guest-label";
  const guestCheck = document.createElement("input");
  guestCheck.type = "checkbox"; guestCheck.checked = !!entry.guest;
  guestCheck.addEventListener("change", () => { appEntries()[index].guest = guestCheck.checked; markDirty("tools.apps"); });
  guestLabel.append(guestCheck, document.createTextNode("访客可开"));
  const deleteButton = document.createElement("button");
  deleteButton.className = "delete"; deleteButton.type = "button";
  deleteButton.title = "删除"; deleteButton.textContent = "✕";
  deleteButton.addEventListener("click", () => { state.values["tools.apps"].splice(index, 1); markDirty("tools.apps"); renderApps(); renderSensitive(); });
  extra.append(imageInput, guestLabel, deleteButton);

  card.append(main, extra);
  return card;
}

function textInput(value, placeholder) {
  const input = document.createElement("input");
  input.type = "text"; input.value = value; input.placeholder = placeholder;
  return input;
}

function addAppEntry(entry) {
  markDirty("tools.apps");
  state.values["tools.apps"].push({
    id: entry.id || "app" + Date.now().toString(36),
    label: entry.label || "", command: entry.command || "", image: entry.image || "", guest: false,
  });
  renderApps();
  renderSensitive();
}

$("add-app").addEventListener("click", () => addAppEntry({}));
$("add-app-bottom").addEventListener("click", () => addAppEntry({}));

/* ── 扫描弹层 ─────────────────────────────────────────────────── */

$("scan").addEventListener("click", async () => {
  $("scan-modal").classList.remove("hidden");
  $("scan-results").textContent = "正在扫描…";
  try {
    const payload = await api("/api/v1/apps/scan");
    const known = new Set(appEntries().map((entry) => entry.command.toLowerCase()));
    $("scan-results").textContent = "";
    const candidates = payload.candidates.filter((entry) => !known.has(entry.path.toLowerCase()));
    if (!candidates.length) {
      $("scan-results").textContent = "没有发现新的程序(已过滤卸载器等)。";
      return;
    }
    candidates.forEach((entry) => {
      const label = document.createElement("label");
      const box = document.createElement("input");
      box.type = "checkbox"; box.dataset.payload = JSON.stringify(entry);
      const name = document.createElement("span");
      name.textContent = entry.label;
      const path = document.createElement("span");
      path.className = "path"; path.textContent = entry.path;
      label.append(box, name, path);
      $("scan-results").appendChild(label);
    });
  } catch (error) {
    $("scan-results").textContent = "扫描失败:" + error.message;
  }
});

$("scan-close").addEventListener("click", () => $("scan-modal").classList.add("hidden"));
$("scan-add").addEventListener("click", () => {
  const boxes = $("scan-results").querySelectorAll("input[type=checkbox]:checked");
  boxes.forEach((box) => addAppEntry(JSON.parse(box.dataset.payload)));
  $("scan-modal").classList.add("hidden");
  setStatus("已添加到清单,别忘了点「保存」。", "ok");
});

/* ── 访客禁开清单 ─────────────────────────────────────────────── */

function renderSensitive() {
  const chips = $("sensitive-chips");
  const select = $("sensitive-add");
  chips.textContent = "";
  select.textContent = "";

  const labelOf = (id) => {
    const builtin = state.meta.builtin_apps.find((app) => app.id === id);
    if (builtin) return builtin.label;
    const own = appEntries().find((entry) => entry.id === id);
    return own ? (own.label || id) : id;
  };

  (state.values["tools.sensitive_apps"] || []).forEach((id) => {
    const chip = document.createElement("span");
    chip.className = "chip";
    chip.append(document.createTextNode(labelOf(id)));
    const remove = document.createElement("button");
    remove.type = "button"; remove.textContent = "✕"; remove.title = "移除";
    remove.addEventListener("click", () => {
      state.values["tools.sensitive_apps"] = state.values["tools.sensitive_apps"].filter((x) => x !== id);
      markDirty("tools.sensitive_apps");
      renderSensitive();
    });
    chip.appendChild(remove);
    chips.appendChild(chip);
  });

  const known = new Set([
    ...state.meta.builtin_apps.map((app) => app.id),
    ...appEntries().map((entry) => entry.id),
  ]);
  const placeholder = document.createElement("option");
  placeholder.value = ""; placeholder.textContent = "选择要禁开的应用…";
  select.appendChild(placeholder);
  [...known].sort().forEach((id) => {
    const option = document.createElement("option");
    option.value = id; option.textContent = labelOf(id) + "(" + id + ")";
    select.appendChild(option);
  });
}

$("sensitive-add").addEventListener("change", () => {
  const id = $("sensitive-add").value;
  if (!id) return;
  const list = state.values["tools.sensitive_apps"] || [];
  if (!list.includes(id)) { state.values["tools.sensitive_apps"] = [...list, id]; markDirty("tools.sensitive_apps"); }
  renderSensitive();
});

/* ── 唤醒词 ───────────────────────────────────────────────────── */

function renderKws() {
  $("kws-input").value = (state.values["kws.keywords"] || []).join("\n");
  $("kws-error").textContent = "";
}

function kwsFromInput() {
  return $("kws-input").value.split("\n").map((line) => line.trim()).filter(Boolean);
}

// 输入即标脏——漏了这一行,唤醒词的改动就永远进不了保存 payload
// (点笔只会显示「本子还没有改动」,其他输入都有对应的一句,唯独这里漏了)。
$("kws-input").addEventListener("input", () => markDirty("kws.keywords"));

/* ── 语音 ─────────────────────────────────────────────────────── */

function bindSlider(id, out, key) {
  const slider = $(id);
  slider.value = state.values[key];
  $(out).textContent = Number(state.values[key]).toFixed(2);
  slider.addEventListener("input", () => { $(out).textContent = Number(slider.value).toFixed(2); markDirty(key); });
}

function renderVoice() {
  bindSlider("speed", "speed-out", "tts.speed");
  bindSlider("guest-speed", "guest-speed-out", "tts.guest_speed");
  bindSlider("pitch", "pitch-out", "tts.pitch");
}

/* ── 常规 ─────────────────────────────────────────────────────── */

function renderGeneral() {
  $("city").value = state.values["weather.city"] || "";
  $("weather-enabled").checked = !!state.values["weather.enabled"];
}
$("city").addEventListener("input", () => markDirty("weather.city"));
$("weather-enabled").addEventListener("change", () => markDirty("weather.enabled"));

/* ── 保存:点铅笔,静默落盘(成功不打扰,错误才说话)──────────── */

let saving = false;
let errorTimer = null;

function showError(text) {
  setStatus(text, "err");
  clearTimeout(errorTimer);
  errorTimer = setTimeout(() => setStatus(""), 3500);
}

$("pen").addEventListener("click", async () => {
  if (saving) return;
  // 唤醒词只在真正改过时校验:没动过的旧词不该挡住其他键的保存
  if (dirty.has("kws.keywords")) {
    const words = kwsFromInput();
    if (!words.length) { $("kws-error").textContent = "唤醒词至少要 1 个。"; return showError("唤醒词至少要 1 个。"); }
    if (words.length > 8) { $("kws-error").textContent = "唤醒词最多 8 个。"; return showError("唤醒词最多 8 个。"); }
    if (words.some((word) => word.length < 2 || word.length > 12)) {
      $("kws-error").textContent = "每个唤醒词需要 2–12 个字。";
      return showError("每个唤醒词需要 2–12 个字。");
    }
  }
  $("kws-error").textContent = "";

  // 缺「启动命令」的卡片报错而不是静默丢弃——保存过的东西必须看得见地活着
  const rawApps = appEntries();
  const apps = rawApps.filter((entry) => entry.id && entry.command);
  if (dirty.has("tools.apps") && apps.length !== rawApps.length) {
    return showError("有应用卡片还没填「启动命令」,补上再保存。");
  }

  const all = {
    "tools.apps": apps,
    "tools.sensitive_apps": state.values["tools.sensitive_apps"] || [],
    "kws.keywords": kwsFromInput(),
    "tts.speed": Number($("speed").value),
    "tts.guest_speed": Number($("guest-speed").value),
    "tts.pitch": Number($("pitch").value),
    "weather.city": $("city").value.trim() || "北京",
    "weather.enabled": $("weather-enabled").checked,
  };
  // 只写脏键:页面没碰过的键不进 payload,别的来源改的值不会被覆盖
  const changes = {};
  for (const key of dirty) if (all[key] !== undefined) changes[key] = all[key];
  if (!Object.keys(changes).length) {
    setStatus("本子还没有改动。");
    setTimeout(() => setStatus(""), 2000);
    return;
  }

  saving = true;
  try {
    const payload = await api("/api/v1/config", { method: "POST", headers, body: JSON.stringify({ changes }) });
    Object.keys(payload.effects || {}).forEach((key) => dirty.delete(key));
    if (payload.mode === "full") {
      setStatus("已写入(配置结构无法原位编辑,注释已丢失)。", "ok");
      clearTimeout(errorTimer);
      errorTimer = setTimeout(() => setStatus(""), 3500);
    } else {
      setStatus("已保存 ✓", "ok");
      clearTimeout(errorTimer);
      errorTimer = setTimeout(() => setStatus(""), 2600);
    }
  } catch (error) {
    const details = (error.payload && error.payload.errors || []).join("; ");
    showError("没写进去:" + (details || error.message));
  } finally {
    // 无论成败必须释放:否则一次失败(如服务重启瞬间的网络错误)会让
    // saving 永远为 true,之后每次点铅笔都被静默吞掉——实测踩过。
    saving = false;
  }
  await load().catch(() => setStatus("已保存,但刷新视图失败——请手动刷新页面", "err"));
});

/* ── 心跳 + 配置同步 ──────────────────────────────────────────── */

setInterval(() => { api("/api/v1/ping").catch(() => {}); }, 30000);

// 没有未保存改动时,窗口获焦/定时拉取磁盘最新配置——别处的改动
// (脚本、手工编辑、另一台设备的写入)会被捡回来,而不是被旧状态覆盖
async function syncIfClean() {
  if (document.hidden || saving || dirty.size) return;
  try { await load(); } catch (e) { /* 服务不在了,铅笔会报错 */ }
}
window.addEventListener("focus", syncIfClean);
document.addEventListener("visibilitychange", () => { if (!document.hidden) syncIfClean(); });
setInterval(syncIfClean, 60000);

Flipbook.init();
load().then(() => setStatus("")).catch((error) => setStatus("加载失败:" + error.message, "err"));
