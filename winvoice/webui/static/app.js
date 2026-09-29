/* WinVoice 设置前端:vanilla JS,无构建。所有请求带 token。 */

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
  renderAll();
}

function renderAll() {
  renderApps();
  renderSensitive();
  renderKws();
  renderVoice();
  renderGeneral();
}

/* ── 应用表 ───────────────────────────────────────────────────── */

function appRows() {
  return state.values["tools.apps"] || [];
}

function renderApps() {
  const tbody = $("app-rows");
  tbody.textContent = "";
  appRows().forEach((entry, index) => tbody.appendChild(appRow(entry, index)));
}

function appRow(entry, index) {
  const tr = document.createElement("tr");

  const label = document.createElement("td");
  const labelInput = document.createElement("input");
  labelInput.type = "text"; labelInput.value = entry.label || ""; labelInput.placeholder = "微信";
  labelInput.addEventListener("input", () => { appRows()[index].label = labelInput.value; });
  label.appendChild(labelInput);

  const command = document.createElement("td");
  const commandInput = document.createElement("input");
  commandInput.type = "text"; commandInput.value = entry.command || "";
  commandInput.placeholder = "C:\\Program Files\\Tencent\\Weixin.exe";
  commandInput.addEventListener("input", () => { appRows()[index].command = commandInput.value; });
  command.appendChild(commandInput);

  const image = document.createElement("td");
  const imageInput = document.createElement("input");
  imageInput.type = "text"; imageInput.value = entry.image || ""; imageInput.placeholder = "Weixin.exe(可选)";
  imageInput.addEventListener("input", () => { appRows()[index].image = imageInput.value; });
  image.appendChild(imageInput);

  const guest = document.createElement("td");
  const guestCheck = document.createElement("input");
  guestCheck.type = "checkbox"; guestCheck.checked = !!entry.guest;
  guestCheck.addEventListener("change", () => { appRows()[index].guest = guestCheck.checked; });
  guest.appendChild(guestCheck);

  const remove = document.createElement("td");
  const deleteButton = document.createElement("button");
  deleteButton.className = "delete"; deleteButton.title = "删除"; deleteButton.textContent = "✕";
  deleteButton.addEventListener("click", () => { state.values["tools.apps"].splice(index, 1); renderApps(); renderSensitive(); });
  remove.appendChild(deleteButton);

  tr.append(label, command, image, guest, remove);
  return tr;
}

function addAppEntry(entry) {
  state.values["tools.apps"].push({ id: entry.id || "", label: entry.label || "", command: entry.command || "", image: entry.image || "", guest: false });
  renderApps();
  renderSensitive();
}

$("add-app").addEventListener("click", () => {
  const id = "app" + Date.now().toString(36);
  addAppEntry({ id, label: "", command: "" });
});

/* ── 扫描弹层 ─────────────────────────────────────────────────── */

$("scan").addEventListener("click", async () => {
  $("scan-modal").classList.remove("hidden");
  $("scan-results").textContent = "正在扫描…";
  try {
    const payload = await api("/api/v1/apps/scan");
    const known = new Set(appRows().map((entry) => entry.command.toLowerCase()));
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
      const text = document.createElement("span");
      text.textContent = entry.label;
      const path = document.createElement("span");
      path.className = "path"; path.textContent = entry.path;
      label.append(box, text, path);
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
  setStatus("已添加到列表,记得点「保存」。");
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
    const own = appRows().find((entry) => entry.id === id);
    return own ? (own.label || id) : id;
  };

  (state.values["tools.sensitive_apps"] || []).forEach((id) => {
    const chip = document.createElement("span");
    chip.className = "chip";
    chip.append(document.createTextNode(labelOf(id)));
    const remove = document.createElement("button");
    remove.textContent = "✕"; remove.title = "移除";
    remove.addEventListener("click", () => {
      state.values["tools.sensitive_apps"] = state.values["tools.sensitive_apps"].filter((x) => x !== id);
      renderSensitive();
    });
    chip.appendChild(remove);
    chips.appendChild(chip);
  });

  const known = new Set([
    ...state.meta.builtin_apps.map((app) => app.id),
    ...appRows().map((entry) => entry.id),
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

/* ── 标签页 ───────────────────────────────────────────────────── */

$("tabs").addEventListener("click", (event) => {
  const button = event.target.closest("button");
  if (!button) return;
  document.querySelectorAll("#tabs button").forEach((b) => b.classList.toggle("active", b === button));
  document.querySelectorAll(".tab").forEach((section) => section.classList.toggle("active", section.id === "tab-" + button.dataset.tab));
});

/* ── 保存 ─────────────────────────────────────────────────────── */

$("save").addEventListener("click", async () => {
  const words = kwsFromInput();
  if (words.length > 8) return setStatus("唤醒词最多 8 个。", "err");
  if (words.some((word) => word.length < 2 || word.length > 12)) {
    return setStatus("每个唤醒词需要 2–12 个字。", "err");
  }

  const changes = {
    "tools.apps": appRows().filter((entry) => entry.id && entry.command),
    "tools.sensitive_apps": state.values["tools.sensitive_apps"] || [],
    "kws.keywords": words,
    "tts.speed": Number($("speed").value),
    "tts.guest_speed": Number($("guest-speed").value),
    "tts.pitch": Number($("pitch").value),
    "weather.city": $("city").value.trim() || "北京",
    "weather.enabled": $("weather-enabled").checked,
  };

  try {
    const payload = await api("/api/v1/config", { method: "POST", headers, body: JSON.stringify({ changes }) });
    const details = Object.entries(payload.effects)
      .map(([key, effect]) => key + (effect === "hot" ? "(即时生效)" : "(重启后生效)"))
      .join("、");
    if (payload.mode === "full") {
      setStatus("已保存(注意:配置结构无法原位编辑,注释已丢失)。 " + details, "ok");
    } else {
      setStatus("已保存。 " + details, "ok");
    }
    await load();
  } catch (error) {
    const details = (error.payload && error.payload.errors || []).join("; ");
    setStatus("保存失败:" + (details || error.message), "err");
  }
});

/* ── 心跳:页面开着,服务器就活着 ──────────────────────────────── */

setInterval(() => { api("/api/v1/ping").catch(() => {}); }, 30000);

load().then(() => setStatus("")).catch((error) => setStatus("加载失败:" + error.message, "err"));
