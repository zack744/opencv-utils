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
  browserCameraBtn: document.querySelector("#browserCameraBtn"),
  stopBrowserCameraBtn: document.querySelector("#stopBrowserCameraBtn"),
  recordBtn: document.querySelector("#recordBtn"),
  recordingTime: document.querySelector("#recordingTime"),
  statsList: document.querySelector("#statsList"),
  refreshRecordingsBtn: document.querySelector("#refreshRecordingsBtn"),
  recordingPlayer: document.querySelector("#recordingPlayer"),
  recordingPlayerHint: document.querySelector("#recordingPlayerHint"),
  recordingList: document.querySelector("#recordingList"),
  toast: document.querySelector("#toast"),
};

let currentStatus = null;
let modes = [];
let activeRecordingName = null;
let browserCameraStream = null;
let browserCameraVideo = null;
let browserCameraCanvas = null;
let browserCameraTimer = null;
let browserCameraUploadInFlight = false;
let browserCameraLastErrorAt = 0;

const BROWSER_CAMERA_SOURCE = "browser";
const BROWSER_CAMERA_UPLOAD_FPS = 12;

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

function isBrowserCameraRunning() {
  return Boolean(browserCameraStream);
}

function syncBrowserCameraControls() {
  const running = isBrowserCameraRunning();
  els.browserCameraBtn.disabled = running;
  els.stopBrowserCameraBtn.hidden = !running;
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
  // 抓旧状态做边沿检测 —— 录制从在录 → 停止时,自动刷新录像列表
  const prevRecording = currentStatus ? currentStatus.recording : null;
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
  const codec = status.recording_codec ? ` · ${status.recording_codec}` : "";
  els.recordingTime.textContent = status.recording ? `${Number(status.recording_elapsed || 0).toFixed(1)}s${codec}` : "";
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

  // 录制刚结束:延后 700ms 让后端 writer 释放文件句柄,再拉一次录像列表
  if (prevRecording === true && status.recording === false) {
    window.setTimeout(fetchRecordings, 700);
  }
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

function formatBytes(bytes) {
  if (!Number.isFinite(bytes) || bytes <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  let value = bytes;
  let i = 0;
  while (value >= 1024 && i < units.length - 1) {
    value /= 1024;
    i++;
  }
  return `${value.toFixed(value >= 10 || i === 0 ? 0 : 1)} ${units[i]}`;
}

function formatTime(ts) {
  if (!ts) return "--";
  const d = new Date(ts * 1000);
  if (Number.isNaN(d.getTime())) return "--";
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

async function fetchRecordings() {
  try {
    const data = await api("/api/recordings");
    renderRecordings(data.items || []);
  } catch (err) {
    // 录像列表是辅助功能,失败时只在 console 留痕,不打扰主流程
    // eslint-disable-next-line no-console
    console.debug("recordings fetch failed:", err.message);
  }
}

function renderRecordings(items) {
  const list = els.recordingList;
  const player = els.recordingPlayer;
  const hint = els.recordingPlayerHint;

  // 当前正在播放的录像如果已从列表里消失(被删/重命名),清掉播放器
  if (activeRecordingName && !items.find((it) => it.name === activeRecordingName)) {
    activeRecordingName = null;
    player.removeAttribute("src");
    try { player.load(); } catch (_) { /* noop */ }
    hint.textContent = "从下方列表选择一段录像进行回放";
  }

  list.innerHTML = "";

  if (!items.length) {
    const empty = document.createElement("li");
    empty.className = "recording-empty";
    empty.textContent = "暂无录像文件，点击「开始录制」录制一段试试";
    list.appendChild(empty);
    return;
  }

  items.forEach((item) => {
    const li = document.createElement("li");
    li.className = "recording-row";
    li.dataset.active = item.name === activeRecordingName ? "true" : "false";
    li.title = "点击回放";

    const info = document.createElement("div");
    info.className = "recording-info";
    const name = document.createElement("div");
    name.className = "recording-name";
    name.textContent = item.name;
    const meta = document.createElement("div");
    meta.className = "recording-meta";
    meta.textContent = `${formatBytes(item.size)} · ${formatTime(item.mtime)}`;
    info.append(name, meta);

    const actions = document.createElement("div");
    actions.className = "recording-actions";
    const download = document.createElement("a");
    download.href = item.download_url;
    download.download = item.name;
    download.textContent = "下载";
    download.setAttribute("aria-label", `下载 ${item.name}`);
    download.addEventListener("click", (ev) => ev.stopPropagation());
    actions.appendChild(download);

    li.append(info, actions);
    li.addEventListener("click", () => playRecording(item));
    list.appendChild(li);
  });
}

function playRecording(item) {
  const player = els.recordingPlayer;
  const hint = els.recordingPlayerHint;
  // 已经在播这一条 —— 切换 播放/暂停
  if (activeRecordingName === item.name && player.src) {
    if (player.paused) {
      player.play().catch(() => { /* 自动播放受限,用户点 ▶ 即可 */ });
    } else {
      player.pause();
    }
    return;
  }
  activeRecordingName = item.name;
  player.src = item.url;
  hint.textContent = `${item.name} · ${formatBytes(item.size)}`;
  // 刷新高亮
  Array.from(list_rows()).forEach((row) => {
    const rowName = row.querySelector(".recording-name")?.textContent;
    row.dataset.active = rowName === item.name ? "true" : "false";
  });
  player.play().catch(() => { /* 自动播放受限,用户点 ▶ 即可 */ });
}

function list_rows() {
  return els.recordingList.querySelectorAll(".recording-row");
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

function ensureBrowserCameraElements() {
  if (!browserCameraVideo) {
    browserCameraVideo = document.createElement("video");
    browserCameraVideo.muted = true;
    browserCameraVideo.playsInline = true;
    browserCameraVideo.autoplay = true;
    browserCameraVideo.style.display = "none";
    document.body.appendChild(browserCameraVideo);
  }
  if (!browserCameraCanvas) {
    browserCameraCanvas = document.createElement("canvas");
  }
}

async function blobFromBrowserCamera() {
  const video = browserCameraVideo;
  const canvas = browserCameraCanvas;
  if (!video || !canvas || !video.videoWidth || !video.videoHeight) {
    return null;
  }
  canvas.width = 640;
  canvas.height = 480;
  const ctx = canvas.getContext("2d", { alpha: false });
  ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
  return new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", 0.72));
}

async function uploadBrowserCameraFrame() {
  if (!isBrowserCameraRunning() || browserCameraUploadInFlight) return;
  browserCameraUploadInFlight = true;
  try {
    const blob = await blobFromBrowserCamera();
    if (!blob) return;
    const res = await fetch("/api/browser-camera/frame", {
      method: "POST",
      headers: { "Content-Type": "image/jpeg" },
      body: blob,
    });
    if (!res.ok) {
      let detail = "网页摄像头帧上传失败";
      try {
        const body = await res.json();
        detail = body.detail || detail;
      } catch (_) {
        detail = await res.text();
      }
      throw new Error(detail);
    }
  } catch (err) {
    const now = Date.now();
    if (now - browserCameraLastErrorAt > 3000) {
      browserCameraLastErrorAt = now;
      toast(err.message, "red");
    }
  } finally {
    browserCameraUploadInFlight = false;
  }
}

function startBrowserCameraUploadLoop() {
  window.clearInterval(browserCameraTimer);
  browserCameraTimer = window.setInterval(
    uploadBrowserCameraFrame,
    Math.round(1000 / BROWSER_CAMERA_UPLOAD_FPS)
  );
}

async function startBrowserCamera() {
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    toast("当前浏览器不支持摄像头权限 API", "red");
    return;
  }
  if (!window.isSecureContext) {
    toast("网页摄像头需要 HTTPS，或在 localhost 打开", "red");
    return;
  }

  try {
    toast("正在请求浏览器摄像头权限...", "neutral");
    ensureBrowserCameraElements();
    browserCameraStream = await navigator.mediaDevices.getUserMedia({
      audio: false,
      video: {
        width: { ideal: 640 },
        height: { ideal: 480 },
        frameRate: { ideal: BROWSER_CAMERA_UPLOAD_FPS, max: 15 },
      },
    });
    browserCameraVideo.srcObject = browserCameraStream;
    await browserCameraVideo.play();

    const status = await api("/api/camera", {
      method: "POST",
      body: JSON.stringify({ source: BROWSER_CAMERA_SOURCE }),
    });
    els.cameraInput.value = BROWSER_CAMERA_SOURCE;
    renderStatus(status);
    startBrowserCameraUploadLoop();
    syncBrowserCameraControls();
    toast("网页摄像头已接入检测", "green");
  } catch (err) {
    stopBrowserCamera({ silent: true });
    toast(err.message, "red");
  }
}

function stopBrowserCamera(options = {}) {
  window.clearInterval(browserCameraTimer);
  browserCameraTimer = null;
  browserCameraUploadInFlight = false;
  if (browserCameraStream) {
    browserCameraStream.getTracks().forEach((track) => track.stop());
  }
  browserCameraStream = null;
  if (browserCameraVideo) {
    browserCameraVideo.srcObject = null;
  }
  syncBrowserCameraControls();
  if (!options.silent) {
    toast("网页摄像头已停止", "green");
  }
}

async function boot() {
  const data = await api("/api/modes");
  modes = data.modes;
  renderModes(data.current);
  await refreshStatus();
  window.setInterval(refreshStatus, 500);
  // 启动时拉一次录像列表,之后每 10s 兜底轮询一次
  // (处理"另一台客户端刚刚停止录制"等本机感知不到的状态变化)
  fetchRecordings();
  window.setInterval(fetchRecordings, 10000);
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

els.browserCameraBtn.addEventListener("click", startBrowserCamera);
els.stopBrowserCameraBtn.addEventListener("click", () => stopBrowserCamera());

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

els.refreshRecordingsBtn.addEventListener("click", async () => {
  await fetchRecordings();
  toast("已刷新录像列表", "green");
});

syncBrowserCameraControls();
boot().catch((err) => toast(err.message, "red"));
