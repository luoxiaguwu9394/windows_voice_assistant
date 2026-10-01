/* WinVoice 设置手册前端:vanilla JS,无构建。所有请求带 token。
 * 业务逻辑与上一版一致;渲染目标改为书页内的卡片与控件。 */

"use strict";

const token = new URLSearchParams(location.search).get("token") || "";
const headers = { "X-WV-Token": token, "Content-Type": "application/json" };

const state = {
  meta: { effects: {}, builtin_apps: [] },
  values: { "tools.apps": [], "tools.sensitive_apps": [], "kws.keywords": [] },
};

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
  labelInput.addEventListener("input", () => { appEntries()[index].label = labelInput.value; });
  const commandInput = textInput(entry.command || "", "启动命令(C:\\...\\Weixin.exe)");
  commandInput.addEventListener("input", () => { appEntries()[index].command = commandInput.value; });
  main.append(labelInput, commandInput);

  const extra = document.createElement("div");
  extra.className = "app-extra";
  const imageInput = textInput(entry.image || "", "进程映像名(可选,Weixin.exe)");
  imageInput.addEventListener("input", () => { appEntries()[index].image = imageInput.value; });
  const guestLabel = document.createElement("label");
  guestLabel.className = "guest-label";
  const guestCheck = document.createElement("input");
  guestCheck.type = "checkbox"; guestCheck.checked = !!entry.guest;
  guestCheck.addEventListener("change", () => { appEntries()[index].guest = guestCheck.checked; });
  guestLabel.append(guestCheck, document.createTextNode("访客可开"));
  const deleteButton = document.createElement("button");
  deleteButton.className = "delete"; deleteButton.type = "button";
  deleteButton.title = "删除"; deleteButton.textContent = "✕";
  deleteButton.addEventListener("click", () => { state.values["tools.apps"].splice(index, 1); renderApps(); renderSensitive(); });
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
  if (!list.includes(id)) state.values["tools.sensitive_apps"] = [...list, id];
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

/* ── 语音 ─────────────────────────────────────────────────────── */

function bindSlider(id, out, key) {
  const slider = $(id);
  slider.value = state.values[key];
  $(out).textContent = Number(state.values[key]).toFixed(2);
  slider.addEventListener("input", () => { $(out).textContent = Number(slider.value).toFixed(2); });
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

/* ── 保存:提笔 → 在书上书写 → 落墨成文 ─────────────────────── */

let saving = false;

function inkReveal() {
  const ink = $("ink");
  ink.classList.add("writing");
  const paths = [...ink.querySelectorAll("path")];
  paths.forEach((p) => {
    const len = p.getTotalLength();
    p.style.strokeDasharray = len;
    p.style.strokeDashoffset = len;
  });
  const tl = gsap.timeline();
  paths.forEach((p, i) => tl.to(p, { strokeDashoffset: 0, duration: 0.30, ease: "none" }, i * 0.26));
  return new Promise((resolve) => tl.eventCallback("onComplete", resolve));
}

$("pen").addEventListener("click", async () => {
  if (saving) return;
  const words = kwsFromInput();
  if (words.length > 8) { $("kws-error").textContent = "唤醒词最多 8 个。"; return setStatus("唤醒词最多 8 个。", "err"); }
  if (words.some((word) => word.length < 2 || word.length > 12)) {
    $("kws-error").textContent = "每个唤醒词需要 2–12 个字。";
    return setStatus("每个唤醒词需要 2–12 个字。", "err");
  }
  $("kws-error").textContent = "";

  const changes = {
    "tools.apps": appEntries().filter((entry) => entry.id && entry.command),
    "tools.sensitive_apps": state.values["tools.sensitive_apps"] || [],
    "kws.keywords": words,
    "tts.speed": Number($("speed").value),
    "tts.guest_speed": Number($("guest-speed").value),
    "tts.pitch": Number($("pitch").value),
    "weather.city": $("city").value.trim() || "北京",
    "weather.enabled": $("weather-enabled").checked,
  };

  saving = true;
  const pen = $("pen");
  const penR = pen.getBoundingClientRect();
  const bookR = $("book").getBoundingClientRect();
  const k = window.__stageScale || 1;
  const dx = (bookR.left + bookR.width * 0.56 - (penR.left + penR.width / 2)) / k;
  const dy = (bookR.top + bookR.height * 0.38 - (penR.top + penR.height / 2)) / k;

  setStatus("正在书写…");
  gsap.to(pen, { x: dx, y: dy, rotation: -36, duration: 0.55, ease: "power2.inOut" });
  await new Promise((resolve) => setTimeout(resolve, 620));

  const writing = inkReveal();
  try {
    const payload = await api("/api/v1/config", { method: "POST", headers, body: JSON.stringify({ changes }) });
    await writing;
    const details = Object.entries(payload.effects)
      .map(([key, effect]) => key + (effect === "hot" ? "(即时)" : "(重启后)"))
      .join("、");
    if (payload.mode === "full") {
      setStatus("已写入(配置结构无法原位编辑,注释已丢失)。 " + details, "ok");
    } else {
      setStatus("已写入。 " + details, "ok");
    }
    await load();
  } catch (error) {
    const details = (error.payload && error.payload.errors || []).join("; ");
    setStatus("没写进去:" + (details || error.message), "err");
  }
  gsap.to(pen, { x: 0, y: 0, rotation: 0, duration: 0.5, ease: "power2.inOut", delay: 0.2 });
  setTimeout(() => $("ink").classList.remove("writing"), 700);
  saving = false;
});

/* ── 心跳:页面开着,服务器就活着 ──────────────────────────────── */

setInterval(() => { api("/api/v1/ping").catch(() => {}); }, 30000);

Flipbook.init();
load().then(() => setStatus("")).catch((error) => setStatus("加载失败:" + error.message, "err"));
