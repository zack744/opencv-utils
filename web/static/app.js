const els = {
  appTitle: document.querySelector("#appTitle"),
  modeTabs: document.querySelector("#modeTabs"),
  stream: document.querySelector("#stream"),
  recBadge: document.querySelector("#recBadge"),
  cameraStatus: document.querySelector("#cameraStatus"),
  fps: document.querySelector("#fps"),
  source: document.querySelector("#source"),
  statusBlock: document.querySelector("#statusBlock"),
  stateDot: document.querySelector("#stateDot"),
  stateText: document.querySelector("#stateText"),
  stateSub: document.querySelector("#stateSub"),
  mainText: document.querySelector("#mainText"),
  progressFill: document.querySelector("#progressFill"),
  recognitionToggle: document.querySelector("#recognitionToggle"),
  cameraInput: document.querySelector("#cameraInput"),
  applyCameraBtn: document.querySelector("#applyCameraBtn"),
  pushupTargetBlock: document.querySelector("#pushupTargetBlock"),
  pushupTargetInput: document.querySelector("#pushupTargetInput"),
  applyPushupTargetBtn: document.querySelector("#applyPushupTargetBtn"),
  switchCameraBtn: document.querySelector("#switchCameraBtn"),
  recordBtn: document.querySelector("#recordBtn"),
  recordingTime: document.querySelector("#recordingTime"),
  statsList: document.querySelector("#statsList"),
  toast: document.querySelector("#toast"),
};

let currentStatus = null;
let modes = [];

function toast(message, tone = "neutral") {
  els.toast.textContent = message;
  els.toast.dataset.tone = tone;
  els.toast.classList.add("show");
  window.clearTimeout(toast.timer);
  toast.timer = window.setTimeout(() => els.toast.classList.remove("show"), 2600);
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!res.ok) {
    let detail = "请求失败";
    try {
      const body = await res.json();
      detail = body.detail || detail;
    } catch (_) {
      detail = await res.text();
    }
    throw new Error(detail);
  }
  return res.json();
}

function renderModes(active) {
  els.modeTabs.innerHTML = "";
  modes.forEach((mode) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.textContent = mode.label;
    btn.dataset.active = mode.id === active ? "true" : "false";
    btn.addEventListener("click", () => switchMode(mode.id));
    els.modeTabs.appendChild(btn);
  });
}

function toneFromColor(color) {
  return ["green", "orange", "red", "gray", "blue"].includes(color) ? color : "gray";
}

function renderStatus(status) {
  currentStatus = status;
  const tone = toneFromColor(status.state_color);
  els.appTitle.textContent = status.app_title || status.mode_label || "检测系统";
  els.stateDot.dataset.tone = tone;
  els.statusBlock.dataset.alert = status.alert ? "true" : "false";
  els.stateText.textContent = status.state || "--";
  els.stateSub.textContent = status.sub_text || "";
  els.mainText.textContent = status.main_text || "";
  els.progressFill.style.width = `${Math.max(0, Math.min(1, Number(status.progress || 0))) * 100}%`;
  els.cameraStatus.textContent = `摄像头: ${status.camera_status === "ok" ? "在线" : "离线"}`;
  els.cameraStatus.dataset.tone = status.camera_status === "ok" ? "green" : "red";
  els.fps.textContent = `FPS: ${status.fps || 0}`;
  els.source.textContent = `源: ${status.camera_source_value || "--"}`;
  els.recognitionToggle.checked = Boolean(status.recognition_enabled);
  els.recBadge.classList.toggle("show", Boolean(status.recording));
  els.recordBtn.textContent = status.recording ? "停止录制" : "开始录制";
  els.recordBtn.classList.toggle("active", Boolean(status.recording));
  els.recordingTime.textContent = status.recording ? `${Number(status.recording_elapsed || 0).toFixed(1)}s` : "";
  if (document.activeElement !== els.cameraInput) {
    els.cameraInput.value = status.camera_source_value || "";
  }
  // 俯卧撑目标次数控件：仅在 pushup 模式显示
  const isPushup = status.mode === "pushup";
  els.pushupTargetBlock.hidden = !isPushup;
  if (isPushup) {
    const target = status.target_reps;
    if (target != null && document.activeElement !== els.pushupTargetInput) {
      els.pushupTargetInput.value = String(target);
    }
  }
  renderModes(status.mode);
  renderStats(status.stats || []);
}

function renderStats(stats) {
  els.statsList.innerHTML = "";
  if (!stats.length) {
    const empty = document.createElement("p");
    empty.className = "empty";
    empty.textContent = "暂无统计";
    els.statsList.appendChild(empty);
    return;
  }
  stats.forEach(([key, value]) => {
    const row = document.createElement("div");
    row.className = "stat-row";
    const dt = document.createElement("dt");
    const dd = document.createElement("dd");
    dt.textContent = key;
    dd.textContent = value;
    row.append(dt, dd);
    els.statsList.appendChild(row);
  });
}

async function refreshStatus() {
  try {
    const status = await api("/api/status");
    renderStatus(status);
  } catch (err) {
    toast(err.message, "red");
  }
}

async function switchMode(mode) {
  try {
    toast("正在切换模式...", "neutral");
    const source = els.cameraInput.value.trim() || null;
    const status = await api("/api/mode", {
      method: "POST",
      body: JSON.stringify({ mode, source }),
    });
    els.stream.src = `/stream?t=${Date.now()}`;
    renderStatus(status);
    toast("模式已切换", "green");
  } catch (err) {
    toast(err.message, "red");
  }
}

async function boot() {
  const data = await api("/api/modes");
  modes = data.modes;
  renderModes(data.current);
  await refreshStatus();
  window.setInterval(refreshStatus, 500);
}

els.recognitionToggle.addEventListener("change", async () => {
  try {
    const status = await api("/api/recognition", {
      method: "POST",
      body: JSON.stringify({ enabled: els.recognitionToggle.checked }),
    });
    renderStatus(status);
  } catch (err) {
    els.recognitionToggle.checked = currentStatus?.recognition_enabled ?? true;
    toast(err.message, "red");
  }
});

els.applyCameraBtn.addEventListener("click", async () => {
  const source = els.cameraInput.value.trim();
  if (!source) return;
  try {
    toast("正在连接摄像头...", "neutral");
    const status = await api("/api/camera", {
      method: "POST",
      body: JSON.stringify({ source }),
    });
    renderStatus(status);
    toast("摄像头源已应用", "green");
  } catch (err) {
    toast(err.message, "red");
  }
});

els.switchCameraBtn.addEventListener("click", async () => {
  try {
    const status = await api("/api/camera/switch", { method: "POST", body: "{}" });
    renderStatus(status);
    toast("摄像头已切换", "green");
  } catch (err) {
    toast(err.message, "red");
  }
});

els.recordBtn.addEventListener("click", async () => {
  const enabled = !(currentStatus && currentStatus.recording);
  try {
    const status = await api("/api/recording", {
      method: "POST",
      body: JSON.stringify({ enabled }),
    });
    renderStatus(status);
    toast(enabled ? "开始录制" : "录制已停止", enabled ? "red" : "green");
  } catch (err) {
    toast(err.message, "red");
  }
});

els.applyPushupTargetBtn.addEventListener("click", async () => {
  const raw = els.pushupTargetInput.value.trim();
  if (!raw) {
    toast("请输入俯卧撑目标次数", "red");
    return;
  }
  const reps = Number.parseInt(raw, 10);
  if (!Number.isFinite(reps) || String(reps) !== raw) {
    toast("请输入 1-100 之间的整数", "red");
    return;
  }
  els.applyPushupTargetBtn.disabled = true;
  try {
    const status = await api("/api/pushup/target", {
      method: "POST",
      body: JSON.stringify({ reps }),
    });
    renderStatus(status);
    toast(`目标已设为 ${reps} 个`, "green");
  } catch (err) {
    toast(err.message, "red");
  } finally {
    els.applyPushupTargetBtn.disabled = false;
  }
});

els.pushupTargetInput.addEventListener("keydown", (ev) => {
  if (ev.key === "Enter") {
    ev.preventDefault();
    els.applyPushupTargetBtn.click();
  }
});

boot().catch((err) => toast(err.message, "red"));
