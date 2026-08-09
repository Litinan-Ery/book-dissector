"use strict";

const $ = (selector) => document.querySelector(selector);
const reasonNames = { copyright: "版权页", toc: "目录", duplicate: "重复副本", frontmatter: "元信息" };
let currentBook = null;
let currentTaskId = null;
let currentPrune = null;
let pollTimer = null;
let bookPollTimer = null;
let cloudConsentConfirmed = false;
let booksById = new Map();
let confirmationAction = null;

const BOOK_REFRESH_MS = 1200;
const RETRY_REFRESH_MS = 2500;

function clear(node) { node.replaceChildren(); }
function el(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) node.textContent = String(text);
  if (className) node.className = className;
  return node;
}
function formatSize(bytes) {
  return bytes > 1024 * 1024 ? `${(bytes / 1024 / 1024).toFixed(1)} MB` : `${Math.max(1, Math.round(bytes / 1024))} KB`;
}
function formatSeconds(seconds) {
  if (seconds < 60) return `${Math.round(seconds)} 秒`;
  return `${Math.floor(seconds / 60)} 分 ${Math.round(seconds % 60)} 秒`;
}
async function jsonOrError(response) {
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
  return data;
}

function openConfirmation(title, lines, confirmLabel, action) {
  const dialog = $("#confirm-dialog");
  $("#confirm-title").textContent = title;
  const details = $("#confirm-details");
  clear(details);
  for (const line of lines) details.append(el("p", line));
  $("#confirm-error").hidden = true;
  $("#confirm-error").textContent = "";
  $("#confirm-submit").textContent = confirmLabel;
  $("#confirm-submit").disabled = false;
  confirmationAction = action;
  dialog.showModal();
}

function closeConfirmation() {
  confirmationAction = null;
  if ($("#confirm-dialog").open) $("#confirm-dialog").close();
}

$("#confirm-cancel").addEventListener("click", closeConfirmation);
$("#confirm-dialog").addEventListener("cancel", () => { confirmationAction = null; });
$("#confirm-submit").addEventListener("click", async () => {
  if (!confirmationAction) return;
  const submit = $("#confirm-submit");
  const errorBox = $("#confirm-error");
  submit.disabled = true;
  errorBox.hidden = true;
  try {
    await confirmationAction();
    closeConfirmation();
  } catch (error) {
    errorBox.textContent = error.message;
    errorBox.hidden = false;
    submit.disabled = false;
  }
});

async function loadSettings() {
  try {
    const data = await jsonOrError(await fetch("/api/settings"));
    cloudConsentConfirmed = Boolean(data.cloud_consent_confirmed);
    $("#settings-status").textContent = data.deepseek_api_key_configured
      ? `已配置密钥 · 模型：${data.deepseek_model}`
      : `尚未配置密钥（模型：${data.deepseek_model}）`;
  } catch (error) { $("#settings-status").textContent = `读取设置失败：${error.message}`; }
}

$("#settings-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await jsonOrError(await fetch("/api/settings", {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ deepseek_api_key: $("#api-key").value.trim() }),
    }));
    $("#api-key").value = "";
    loadSettings();
  } catch (error) { $("#settings-status").textContent = `保存失败：${error.message}`; }
});
$("#btn-test").addEventListener("click", async () => {
  $("#settings-status").textContent = "测试中…";
  try { $("#settings-status").textContent = (await jsonOrError(await fetch("/api/settings/test", { method: "POST" }))).message; }
  catch (error) { $("#settings-status").textContent = `连接失败：${error.message}`; }
});
$("#btn-clear").addEventListener("click", async () => { await fetch("/api/settings", { method: "DELETE" }); loadSettings(); });

function bookStatus(book) {
  if (book.extract_status === "ok") return "已提取";
  if (book.extract_status === "error") return `失败：${book.extract_error || "未知错误"}`;
  if (book.extract_status === "processing") return "提取中…";
  return "等待中";
}

function clearBookRefresh() {
  if (bookPollTimer) window.clearTimeout(bookPollTimer);
  bookPollTimer = null;
}

function scheduleBookRefresh(delay = BOOK_REFRESH_MS) {
  clearBookRefresh();
  bookPollTimer = window.setTimeout(() => {
    bookPollTimer = null;
    refreshBooks();
  }, delay);
}

async function refreshBooks() {
  const body = $("#book-body");
  clearBookRefresh();
  try {
    const books = await jsonOrError(await fetch("/api/books"));
    booksById = new Map(books.map((book) => [book.id, book]));
    clear(body);
    if (!books.length) {
      const row = el("tr"); const cell = el("td", "暂无书籍，请先上传。", "hint"); cell.colSpan = 5; row.append(cell); body.append(row); return;
    }
    for (const book of books) {
      const row = el("tr");
      const name = el("td"); name.append(el("span", book.title || book.filename, "book-title"), el("span", `${book.author || "未知作者"} · ${book.filename} · ${formatSize(book.size_bytes)}`, "book-meta"));
      row.append(name, el("td", book.source_format || "-"), el("td", book.word_count ? book.word_count.toLocaleString() : "-"), el("td", bookStatus(book)));
      const actions = el("td");
      const actionGroup = el("div", null, "book-actions");
      const start = el("button", "一键拆解", "primary compact"); start.disabled = book.extract_status !== "ok"; start.addEventListener("click", () => openDistill(book));
      const remove = el("button", "删除书籍", "danger compact"); remove.addEventListener("click", () => requestBookDelete(book));
      actionGroup.append(start, remove); actions.append(actionGroup); row.append(actions); body.append(row);
    }
    const extracting = books.some((book) => ["pending", "processing"].includes(book.extract_status));
    if (extracting) {
      scheduleBookRefresh();
    } else if ($("#upload-status").textContent.includes("正在提取文本")) {
      $("#upload-status").textContent = "文本提取完成，可以开始拆解。";
    }
  } catch (error) {
    clear(body);
    const row = el("tr"); const cell = el("td", `书库读取失败，正在重试：${error.message}`, "hint"); cell.colSpan = 5; row.append(cell); body.append(row);
    scheduleBookRefresh(RETRY_REFRESH_MS);
  }
}

async function requestBookDelete(book) {
  let preview;
  try {
    preview = await jsonOrError(await fetch(`/api/books/${book.id}/deletion-preview`));
  } catch (error) {
    window.alert(`无法准备删除：${error.message}`);
    return;
  }
  if (preview.active_task_ids.length) {
    window.alert(`该书存在运行中或等待中的任务，请先删除任务：${preview.active_task_ids.join("、")}`);
    return;
  }
  openConfirmation(
    "删除书籍",
    [
      `书名：${book.title || book.filename}`,
      `Book ID：${book.id}`,
      `关联任务：${preview.task_count} 个`,
      `将清理：${preview.will_delete.join("、")}`,
      `将保留：${preview.will_keep.join("、")}`,
    ],
    "删除书籍",
    async () => {
      await jsonOrError(await fetch(`/api/books/${book.id}`, { method: "DELETE" }));
      if (currentBook && currentBook.id === book.id) {
        currentBook = null;
        currentTaskId = null;
        if (pollTimer) window.clearTimeout(pollTimer);
        pollTimer = null;
        $("#distill-panel").hidden = true;
        $("#prune-panel").hidden = true;
      }
      await Promise.all([refreshBooks(), refreshTasks()]);
    },
  );
}

function uploadOne(file) {
  return new Promise((resolve) => {
    const request = new XMLHttpRequest(); const data = new FormData(); data.append("file", file);
    $("#upload-progress-wrap").hidden = false; request.open("POST", "/api/books/upload");
    request.upload.onprogress = (event) => { if (event.lengthComputable) { const percent = Math.round(event.loaded / event.total * 100); $("#upload-progress").value = percent; $("#upload-progress-text").textContent = `${percent}%`; } };
    request.onload = () => resolve(request.status >= 200 && request.status < 300 ? null : `HTTP ${request.status}`);
    request.onerror = () => resolve("网络错误"); request.send(data);
  });
}
$("#btn-upload").addEventListener("click", async () => {
  for (const file of $("#file-input").files) {
    $("#upload-status").textContent = `上传中：${file.name}`;
    const error = await uploadOne(file); if (error) { $("#upload-status").textContent = `${file.name} 上传失败：${error}`; return; }
  }
  $("#upload-progress-wrap").hidden = true; $("#upload-status").textContent = "上传完成，正在提取文本…"; await refreshBooks();
});

async function loadEstimate() {
  if (!currentBook) return;
  const params = new URLSearchParams({ book_type: $("#distill-type").value, strength: $("#distill-strength").value });
  $("#estimate-box").textContent = "正在计算时间、调用量与费用区间…";
  try {
    const estimate = await jsonOrError(await fetch(`/api/books/${currentBook.id}/estimate?${params}`));
    $("#estimate-box").textContent = `预计 ${estimate.api_calls} 次模型调用 · 输入约 ${estimate.input_tokens.toLocaleString()} tokens · 输出约 ${estimate.output_tokens.toLocaleString()} tokens · ${formatSeconds(estimate.time_seconds_low)}–${formatSeconds(estimate.time_seconds_high)} · 约 ¥${estimate.cost_cny_low.toFixed(4)}–¥${estimate.cost_cny_high.toFixed(4)}`;
  } catch (error) { $("#estimate-box").textContent = `估算失败：${error.message}`; }
}
async function openDistill(book) {
  currentBook = book; currentTaskId = null; $("#distill-panel").hidden = false; $("#distill-title").textContent = `一键拆解：${book.title || book.filename}`;
  // 上一本书创建任务时会禁用按钮；切换书籍后必须恢复，
  // 否则用户无法连续提交多本书进入持久化队列。
  $("#btn-start-distill").disabled = false;
  $("#distill-progress").textContent = cloudConsentConfirmed ? "查看估算后即可开始。" : "首次使用请确认云端数据范围。";
  $("#distill-result").hidden = true; $("#modality-warnings").hidden = true; $("#export-actions").hidden = true;
  $("#cloud-consent-row").hidden = cloudConsentConfirmed; $("#cloud-consent").checked = cloudConsentConfirmed;
  await loadEstimate(); $("#distill-panel").scrollIntoView({ behavior: "smooth" });
}
$("#distill-type").addEventListener("change", loadEstimate); $("#distill-strength").addEventListener("change", loadEstimate);
$("#btn-start-distill").addEventListener("click", async () => {
  if (!currentBook) return;
  const button = $("#btn-start-distill"); button.disabled = true; $("#distill-progress").textContent = "正在创建端到端任务…";
  try {
    const task = await jsonOrError(await fetch(`/api/books/${currentBook.id}/disassemble`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ book_type: $("#distill-type").value, strength: $("#distill-strength").value, cloud_consent: $("#cloud-consent").checked }) }));
    currentTaskId = task.task_id; cloudConsentConfirmed = true; $("#cloud-consent-row").hidden = true; refreshTasks(); pollCurrentTask();
  } catch (error) { $("#distill-progress").textContent = `启动失败：${error.message}`; button.disabled = false; }
});

function statusClass(status) { return ["error", "cancelled"].includes(status) ? "status fail" : ["pending", "running", "deleting"].includes(status) ? "status wait" : "status"; }
function statusLabel(status) { return ({ pending: "等待", running: "处理中", done: "完成", error: "失败", cancelled: "已取消" })[status] || status; }
async function taskAction(task, actionPath) { try { await jsonOrError(await fetch(`/api/tasks/${task.task_id}${actionPath}`, { method: "POST" })); refreshTasks(); } catch (error) { window.alert(error.message); } }

function requestTaskDelete(task) {
  const book = booksById.get(task.book_id);
  openConfirmation(
    "删除任务",
    [
      `书籍：${book ? (book.title || book.filename) : task.book_id}`,
      `Task ID：${task.task_id}`,
      `当前状态：${statusLabel(task.status)}`,
      "将删除任务记录与任务单元；书籍、已导出文件、MyDatabase 内容和可复用缓存保留。",
    ],
    "删除任务",
    async () => {
      const outcome = await jsonOrError(await fetch(`/api/tasks/${task.task_id}`, { method: "DELETE" }));
      if (currentTaskId === task.task_id) {
        currentTaskId = null;
        if (pollTimer) window.clearTimeout(pollTimer);
        pollTimer = null;
        $("#btn-start-distill").disabled = false;
        $("#distill-progress").textContent = outcome.state === "deleting"
          ? "正在停止并删除任务…"
          : "任务已删除。";
      }
      await refreshTasks();
    },
  );
}

async function revealTaskOutput(task) {
  try {
    await jsonOrError(await fetch(`/api/tasks/${task.task_id}/reveal-output`, { method: "POST" }));
  } catch (error) {
    window.alert(error.message);
  }
}

function renderTask(task) {
  const item = el("div", null, "task-item");
  const book = booksById.get(task.book_id);
  const identity = el("div"); identity.append(el("div", book ? (book.title || book.filename) : task.book_id, "task-name"), el("div", `${task.book_id} · ${task.task_id}`, "task-detail"));
  const deleting = Boolean(task.delete_requested);
  const middle = el("div", null, "task-progress"); middle.append(el("span", deleting ? "正在停止并删除" : statusLabel(task.status), statusClass(deleting ? "deleting" : task.status)));
  const progress = el("progress"); progress.max = task.total || 1; progress.value = task.current || 0; middle.append(progress, el("span", task.message || task.stage || "等待处理", "task-detail"));
  const actions = el("div", null, "row");
  if (!deleting && ["pending", "running"].includes(task.status)) { const cancel = el("button", "取消", "compact"); cancel.addEventListener("click", () => taskAction(task, "/cancel")); actions.append(cancel); }
  if (!deleting && ["cancelled", "error"].includes(task.status)) { const resume = el("button", "继续", "compact"); resume.addEventListener("click", () => taskAction(task, "/resume")); actions.append(resume); }
  if (!deleting && task.status === "error") { const retry = el("button", "仅重试失败单元", "compact"); retry.addEventListener("click", () => taskAction(task, "/retry-failed")); actions.append(retry); }
  if (!deleting && task.status === "done") { const reveal = el("button", "打开文件夹", "compact"); reveal.addEventListener("click", () => revealTaskOutput(task)); actions.append(reveal); }
  const remove = el("button", deleting ? "正在删除…" : "删除任务", "danger compact"); remove.disabled = deleting; remove.addEventListener("click", () => requestTaskDelete(task)); actions.append(remove);
  item.append(identity, middle, actions); return item;
}
async function refreshTasks() {
  try { const tasks = await jsonOrError(await fetch("/api/tasks")); const list = $("#task-list"); clear(list); if (!tasks.length) list.append(el("div", "暂无任务。上传书籍后点击“一键拆解”。", "task-empty")); else tasks.forEach((task) => list.append(renderTask(task))); }
  catch (error) { $("#task-list").textContent = `任务列表读取失败：${error.message}`; }
}
$("#btn-refresh-tasks").addEventListener("click", refreshTasks);

async function showTaskResult(task) {
  const data = await jsonOrError(await fetch(`/api/tasks/${task.task_id}/result`));
  $("#distill-result").hidden = false; $("#distill-result").textContent = data.final_text;
  $("#distill-stats").textContent = `原文 ${data.total_source_chars.toLocaleString()} 字 → 精华 ${data.total_output_chars.toLocaleString()} 字 · API ${data.api_calls} 次 · 本地缓存 ${data.cache_hits} 次`;
  const warningBox = $("#modality-warnings");
  if (data.modality_warnings.length) {
    warningBox.hidden = false;
    warningBox.textContent = `内容告警：${data.modality_warnings.map((item) => `${item.location} ${item.message}（${item.count} 处）`).join("；")}`;
  } else { warningBox.hidden = true; }
  $("#export-actions").hidden = false;
}
async function pollCurrentTask() {
  if (!currentTaskId) return;
  try {
    const task = await jsonOrError(await fetch(`/api/tasks/${currentTaskId}`));
    $("#distill-progress").textContent = `${statusLabel(task.status)} · ${task.message || task.stage} ${task.total ? `(${task.current}/${task.total})` : ""}`; refreshTasks();
    if (["pending", "running"].includes(task.status)) { pollTimer = window.setTimeout(pollCurrentTask, 1200); return; }
    $("#btn-start-distill").disabled = false;
    if (task.status === "done") await showTaskResult(task);
    else if (task.status === "error") $("#distill-progress").textContent = `拆解失败：${task.error || task.message || "未知错误"}`;
  } catch (error) {
    if (!currentTaskId) return;
    $("#distill-progress").textContent = `连接中断，正在重试：${error.message}`;
    pollTimer = window.setTimeout(pollCurrentTask, RETRY_REFRESH_MS);
  }
}

async function exportCurrent() {
  if (!currentBook) return; $("#export-status").textContent = "导出中…";
  try { const data = await jsonOrError(await fetch(`/api/books/${currentBook.id}/export`, { method: "POST" })); const link = el("a", `下载 ${data.filename}`); link.href = `/api/outputs/${encodeURIComponent(data.filename)}`; link.download = data.filename; const status = $("#export-status"); clear(status); status.append("已导出：", link); }
  catch (error) { $("#export-status").textContent = `导出失败：${error.message}`; }
}
$("#btn-do-export").addEventListener("click", exportCurrent);

async function openPrune() {
  if (!currentBook) return; $("#prune-panel").hidden = false; $("#prune-title").textContent = `删减预览：${currentBook.title || currentBook.filename}`;
  try { currentPrune = { bookId: currentBook.id, restored: [], result: await jsonOrError(await fetch(`/api/books/${currentBook.id}/prune`, { method: "POST" })) }; await renderPrune(); }
  catch (error) { $("#prune-stats").textContent = `删减失败：${error.message}`; }
}
async function renderPrune() {
  const result = currentPrune.result; $("#prune-stats").textContent = `原文 ${result.original_chars.toLocaleString()} 字 → 删减后 ${(result.original_chars - result.removed_chars).toLocaleString()} 字 · 删除 ${result.regions.length} 处`;
  $("#prune-result").textContent = result.pruned_text.slice(0, 6000); const original = await fetch(`/api/books/${currentPrune.bookId}/prune/original`); $("#prune-original").textContent = (await original.text()).slice(0, 6000);
  const list = $("#prune-regions"); clear(list); if (!result.regions.length) list.append(el("li", "未识别到需要删除的内容。"));
  for (const region of result.regions) { const row = el("li"); row.append(el("span", reasonNames[region.reason] || region.reason, "region-reason"), el("span", region.label || "无摘要", "region-label"), el("span", `${region.end - region.start} 字`)); const restore = el("button", "恢复", "compact"); restore.addEventListener("click", async () => { currentPrune.restored.push([region.start, region.end]); currentPrune.result = await jsonOrError(await fetch(`/api/books/${currentPrune.bookId}/prune/restore`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ regions: currentPrune.restored }) })); renderPrune(); }); row.append(restore); list.append(row); }
}
$("#btn-preview-prune").addEventListener("click", openPrune); $("#btn-close-prune").addEventListener("click", () => { $("#prune-panel").hidden = true; });
$("#btn-close-distill").addEventListener("click", () => { currentTaskId = null; if (pollTimer) window.clearTimeout(pollTimer); pollTimer = null; $("#distill-panel").hidden = true; });

loadSettings(); refreshBooks(); refreshTasks(); window.setInterval(refreshTasks, 2500);
