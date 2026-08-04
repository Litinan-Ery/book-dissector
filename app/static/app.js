"use strict";

const $ = (selector) => document.querySelector(selector);
const reasonNames = { copyright: "版权页", toc: "目录", duplicate: "重复副本", frontmatter: "元信息" };
let currentBook = null;
let currentTaskId = null;
let currentPrune = null;
let pollTimer = null;

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

async function loadSettings() {
  try {
    const data = await jsonOrError(await fetch("/api/settings"));
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

async function refreshBooks() {
  const books = await jsonOrError(await fetch("/api/books"));
  const body = $("#book-body");
  clear(body);
  if (!books.length) {
    const row = el("tr"); const cell = el("td", "暂无书籍，请先上传。", "hint"); cell.colSpan = 5; row.append(cell); body.append(row); return;
  }
  for (const book of books) {
    const row = el("tr");
    const name = el("td"); name.append(el("span", book.title || book.filename, "book-title"), el("span", `${book.author || "未知作者"} · ${book.filename} · ${formatSize(book.size_bytes)}`, "book-meta"));
    row.append(name, el("td", book.source_format || "-"), el("td", book.word_count ? book.word_count.toLocaleString() : "-"), el("td", bookStatus(book)));
    const actions = el("td");
    const start = el("button", "一键拆解", "primary compact"); start.disabled = book.extract_status !== "ok"; start.addEventListener("click", () => openDistill(book)); actions.append(start); row.append(actions); body.append(row);
  }
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
  $("#upload-progress-wrap").hidden = true; $("#upload-status").textContent = "上传完成，正在提取文本…"; refreshBooks();
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
  $("#distill-progress").textContent = "确认配置与云端数据范围后即可开始。"; $("#distill-result").hidden = true; $("#quality-summary").hidden = true; $("#export-actions").hidden = true; $("#cloud-consent").checked = false; await loadEstimate(); $("#distill-panel").scrollIntoView({ behavior: "smooth" });
}
$("#distill-type").addEventListener("change", loadEstimate); $("#distill-strength").addEventListener("change", loadEstimate);
$("#btn-start-distill").addEventListener("click", async () => {
  if (!currentBook) return;
  const button = $("#btn-start-distill"); button.disabled = true; $("#distill-progress").textContent = "正在创建端到端任务…";
  try {
    const task = await jsonOrError(await fetch(`/api/books/${currentBook.id}/disassemble`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ book_type: $("#distill-type").value, strength: $("#distill-strength").value, cloud_consent: $("#cloud-consent").checked }) }));
    currentTaskId = task.task_id; refreshTasks(); pollCurrentTask();
  } catch (error) { $("#distill-progress").textContent = `启动失败：${error.message}`; button.disabled = false; }
});

function statusClass(status) { return ["error", "quality_failed", "cancelled"].includes(status) ? "status fail" : ["pending", "running"].includes(status) ? "status wait" : "status"; }
function statusLabel(status) { return ({ pending: "等待", running: "处理中", done: "完成", quality_failed: "质量未通过", error: "失败", cancelled: "已取消" })[status] || status; }
async function taskAction(task, actionPath) { try { await jsonOrError(await fetch(`/api/tasks/${task.task_id}${actionPath}`, { method: "POST" })); refreshTasks(); } catch (error) { window.alert(error.message); } }
function renderTask(task) {
  const item = el("div", null, "task-item");
  const identity = el("div"); identity.append(el("div", task.book_id, "task-name"), el("div", task.run_id || task.task_id, "task-detail"));
  const middle = el("div", null, "task-progress"); middle.append(el("span", statusLabel(task.status), statusClass(task.status)));
  const progress = el("progress"); progress.max = task.total || 1; progress.value = task.current || 0; middle.append(progress, el("span", task.message || task.stage || "等待处理", "task-detail"));
  const actions = el("div", null, "row");
  if (["pending", "running"].includes(task.status)) { const cancel = el("button", "取消", "compact"); cancel.addEventListener("click", () => taskAction(task, "/cancel")); actions.append(cancel); }
  if (["cancelled", "error", "quality_failed"].includes(task.status)) { const resume = el("button", "继续", "compact"); resume.addEventListener("click", () => taskAction(task, "/resume")); actions.append(resume); }
  if (["error", "quality_failed"].includes(task.status)) { const retry = el("button", "仅重试失败单元", "compact"); retry.addEventListener("click", () => taskAction(task, "/retry-failed")); actions.append(retry); }
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
  $("#distill-stats").textContent = `核心正文 ${data.total_source_chars.toLocaleString()} 字 → 知识正文 ${data.total_output_chars.toLocaleString()} 字 · API ${data.api_calls} 次 · 本地缓存 ${data.cache_hits} 次 · 实际费用约 ¥${data.actual_cost_cny.toFixed(4)}`;
  const quality = data.quality_report; const summary = $("#quality-summary"); summary.hidden = false; summary.className = quality.status === "pass" ? "quality-summary" : "quality-summary fail";
  const issues = quality.blocking_issues.length ? `；未解决：${quality.blocking_issues.join("；")}` : ""; summary.textContent = `质量 ${quality.status.toUpperCase()} · 正文覆盖 ${Math.round(quality.body_coverage * 100)}% · 锚点覆盖 ${Math.round(quality.anchor_coverage * 100)}% · 合并重复 ${quality.duplicate_merged_count} 条${issues}`;
  $("#export-actions").hidden = false; $("#btn-do-export").hidden = quality.status !== "pass"; $("#btn-diagnostic-export").hidden = quality.status === "pass";
}
async function pollCurrentTask() {
  if (!currentTaskId) return;
  const task = await jsonOrError(await fetch(`/api/tasks/${currentTaskId}`));
  $("#distill-progress").textContent = `${statusLabel(task.status)} · ${task.message || task.stage} ${task.total ? `(${task.current}/${task.total})` : ""}`; refreshTasks();
  if (["pending", "running"].includes(task.status)) { pollTimer = window.setTimeout(pollCurrentTask, 1200); return; }
  $("#btn-start-distill").disabled = false;
  if (["done", "quality_failed"].includes(task.status)) await showTaskResult(task);
}

async function exportCurrent(diagnostic) {
  if (!currentBook) return; const endpoint = diagnostic ? "export/diagnostic" : "export"; $("#export-status").textContent = "导出中…";
  try { const data = await jsonOrError(await fetch(`/api/books/${currentBook.id}/${endpoint}`, { method: "POST" })); const link = el("a", `下载 ${data.filename}`); link.href = `/api/outputs/${encodeURIComponent(data.filename)}`; link.download = data.filename; const status = $("#export-status"); clear(status); status.append("已导出：", link); }
  catch (error) { $("#export-status").textContent = `导出失败：${error.message}`; }
}
$("#btn-do-export").addEventListener("click", () => exportCurrent(false)); $("#btn-diagnostic-export").addEventListener("click", () => exportCurrent(true));

async function openPrune() {
  if (!currentBook) return; $("#prune-panel").hidden = false; $("#prune-title").textContent = `删减预览：${currentBook.title || currentBook.filename}`;
  try { currentPrune = { bookId: currentBook.id, restored: [], result: await jsonOrError(await fetch(`/api/books/${currentBook.id}/prune`, { method: "POST" })) }; await renderPrune(); }
  catch (error) { $("#prune-stats").textContent = `删减失败：${error.message}`; }
}
async function renderPrune() {
  const result = currentPrune.result; $("#prune-stats").textContent = `原文 ${result.original_chars.toLocaleString()} 字 → 删减后 ${(result.original_chars - result.removed_chars).toLocaleString()} 字 · 映射校验 ${result.span_map_report.valid ? "通过" : "失败"}`;
  $("#prune-result").textContent = result.pruned_text.slice(0, 6000); const original = await fetch(`/api/books/${currentPrune.bookId}/prune/original`); $("#prune-original").textContent = (await original.text()).slice(0, 6000);
  const list = $("#prune-regions"); clear(list); if (!result.regions.length) list.append(el("li", "未识别到需要删除的内容。"));
  for (const region of result.regions) { const row = el("li"); row.append(el("span", reasonNames[region.reason] || region.reason, "region-reason"), el("span", region.label || "无摘要", "region-label"), el("span", `${region.end - region.start} 字`)); const restore = el("button", "恢复", "compact"); restore.addEventListener("click", async () => { currentPrune.restored.push([region.start, region.end]); currentPrune.result = await jsonOrError(await fetch(`/api/books/${currentPrune.bookId}/prune/restore`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ regions: currentPrune.restored }) })); renderPrune(); }); row.append(restore); list.append(row); }
}
$("#btn-preview-prune").addEventListener("click", openPrune); $("#btn-close-prune").addEventListener("click", () => { $("#prune-panel").hidden = true; });
$("#btn-close-distill").addEventListener("click", () => { if (pollTimer) window.clearTimeout(pollTimer); $("#distill-panel").hidden = true; });

loadSettings(); refreshBooks(); refreshTasks(); window.setInterval(refreshTasks, 2500);
