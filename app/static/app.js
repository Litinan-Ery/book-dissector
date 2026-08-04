"use strict";

const $ = (sel) => document.querySelector(sel);

/* ---------- 设置区 ---------- */
async function loadSettings() {
  try {
    const res = await fetch("/api/settings");
    const data = await res.json();
    const configured = data.deepseek_api_key_configured;
    $("#settings-status").textContent = configured
      ? "已配置密钥 · 模型：" + data.deepseek_model
      : "尚未配置密钥（模型：" + data.deepseek_model + "）";
  } catch (err) {
    $("#settings-status").textContent = "读取设置失败：" + err.message;
  }
}

$("#settings-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const key = $("#api-key").value.trim();
  const res = await fetch("/api/settings", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ deepseek_api_key: key }),
  });
  if (res.ok) {
    $("#api-key").value = "";
    $("#settings-status").textContent = "已保存。";
    loadSettings();
  } else {
    $("#settings-status").textContent = "保存失败。";
  }
});

$("#btn-test").addEventListener("click", async () => {
  // 若输入框有未保存的新密钥，先保存再测试，避免误测旧值
  const newKey = $("#api-key").value.trim();
  if (newKey) {
    const saveRes = await fetch("/api/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ deepseek_api_key: newKey }),
    });
    if (!saveRes.ok) {
      $("#settings-status").textContent = "保存失败，无法测试。";
      return;
    }
    $("#api-key").value = "";
  }
  $("#settings-status").textContent = "测试中…";
  const res = await fetch("/api/settings/test", { method: "POST" });
  const data = await res.json();
  $("#settings-status").textContent = data.message;
});

$("#btn-clear").addEventListener("click", async () => {
  await fetch("/api/settings", { method: "DELETE" });
  $("#settings-status").textContent = "已清除密钥。";
});

/* ---------- 书库区 ---------- */
async function refreshBooks() {
  const res = await fetch("/api/books");
  const books = await res.json();
  const body = $("#book-body");
  body.innerHTML = "";
  if (books.length === 0) {
    body.innerHTML = '<tr><td colspan="4" class="hint">暂无书籍，请上传。</td></tr>';
    return;
  }
  for (const b of books) {
    const tr = document.createElement("tr");
    const size = b.size_bytes > 1024 * 1024
      ? (b.size_bytes / 1024 / 1024).toFixed(1) + " MB"
      : Math.max(1, Math.round(b.size_bytes / 1024)) + " KB";
    const time = new Date(b.uploaded_at).toLocaleString("zh-CN");
    const statusText = statusLabel(b);
    tr.innerHTML =
      "<td>" + (b.title || "-") + "</td>" +
      "<td>" + (b.author || "-") + "</td>" +
      "<td>" + (b.source_format || "-") + "</td>" +
      "<td>" + (b.word_count ? b.word_count.toLocaleString() : "-") + "</td>" +
      "<td>" + statusText + "</td>" +
      "<td>" + b.filename + "</td><td>" + size + "</td><td>" + time + "</td>" +
      '<td>' +
      '<button class="btn-prune" data-id="' + b.id + '" ' +
      (b.extract_status === "ok" ? "" : "disabled ") + '>删减</button> ' +
      '<button class="btn-disassemble" data-id="' + b.id + '" ' +
      (b.extract_status === "ok" ? "" : "disabled ") + '>拆解</button></td>';
    tr.querySelector(".btn-prune").addEventListener("click", () => {
      openPrune(b.id, b.filename);
    });
    tr.querySelector(".btn-disassemble").addEventListener("click", () => {
      openDistill(b.id, b.filename);
    });
    body.appendChild(tr);
  }
}

function statusLabel(b) {
  switch (b.extract_status) {
    case "ok": return "已提取";
    case "error": return "失败：" + (b.extract_error || "未知错误");
    case "processing": return "提取中…";
    default: return "等待中";
  }
}

function uploadOne(file) {
  return new Promise((resolve) => {
    const xhr = new XMLHttpRequest();
    const fd = new FormData();
    fd.append("file", file);
    $("#upload-progress-wrap").hidden = false;
    $("#upload-progress").value = 0;
    $("#upload-progress-text").textContent = "0%";
    xhr.open("POST", "/api/books/upload");
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) {
        const pct = Math.round((e.loaded / e.total) * 100);
        $("#upload-progress").value = pct;
        $("#upload-progress-text").textContent = pct + "%";
      }
    };
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) resolve({ ok: true });
      else {
        let detail = "HTTP " + xhr.status;
        try { detail = JSON.parse(xhr.responseText).detail || detail; } catch (_) {}
        resolve({ ok: false, detail });
      }
    };
    xhr.onerror = () => resolve({ ok: false, detail: "网络错误（服务可能正在重启，请重试）" });
    xhr.send(fd);
  });
}

async function uploadBooks(files) {
  const status = $("#upload-status");
  for (const file of files) {
    status.textContent = "上传中：" + file.name;
    const r = await uploadOne(file);
    if (!r.ok) {
      status.textContent = file.name + " 上传失败：" + (r.detail || "未知错误");
      $("#upload-progress-wrap").hidden = true;
      return;
    }
  }
  $("#upload-progress-wrap").hidden = true;
  status.textContent = "上传完成，正在后台提取文本…（大书可能需要十几秒）";
  refreshBooks();
}

$("#btn-upload").addEventListener("click", () => {
  const files = $("#file-input").files;
  if (files.length > 0) uploadBooks(files);
});

/* ---------- 删减预览 ---------- */
let currentPrune = null; // { bookId, filename, result, restored:[[s,e],...] }

const reasonNames = {
  copyright: "版权页",
  toc: "目录",
  backmatter: "书末部分",
  duplicate: "重复段",
};

async function openPrune(bookId, filename) {
  $("#prune-panel").hidden = false;
  $("#prune-title").textContent = "删减预览：" + filename;
  $("#prune-stats").textContent = "执行删减中…";
  const res = await fetch("/api/books/" + bookId + "/prune", { method: "POST" });
  if (!res.ok) {
    const err = await res.json();
    $("#prune-stats").textContent = "删减失败：" + (err.detail || res.status);
    return;
  }
  currentPrune = { bookId, filename, result: await res.json(), restored: [] };
  renderPrune();
}

function renderPrune() {
  const r = currentPrune.result;
  $("#prune-stats").textContent =
    "原文字数 " + r.original_chars.toLocaleString() +
    " → 删减后 " + (r.original_chars - r.removed_chars).toLocaleString() +
    "（保留 " + Math.round(r.kept_ratio * 100) + "%），删除 " + r.regions.length + " 处";
  $("#prune-result").textContent = r.pruned_text.slice(0, 6000) +
    (r.pruned_text.length > 6000 ? "\n…（预览截断，完整稿已保存）" : "");

  // 原文片段：展示前 1200 字符 + 删除标记
  const orig = currentPrune.originalText ? currentPrune.originalText : fetchOriginal();
  const list = $("#prune-regions");
  list.innerHTML = "";
  if (r.regions.length === 0) {
    list.innerHTML = "<li>未识别到需要删除的内容。</li>";
  }
  for (const reg of r.regions) {
    const li = document.createElement("li");
    const reason = reasonNames[reg.reason] || reg.reason;
    li.innerHTML =
      '<span class="region-reason">' + reason + "</span>" +
      '<span class="region-label" title="' + (reg.label || "") + '">' +
      (reg.label || "(无摘要)") + "</span>" +
      "<span>" + (reg.end - reg.start).toLocaleString() + " 字</span>";
    const btn = document.createElement("button");
    btn.textContent = "恢复";
    btn.addEventListener("click", async () => {
      currentPrune.restored.push([reg.start, reg.end]);
      await applyRestore();
    });
    li.appendChild(btn);
    list.appendChild(li);
  }
}

async function fetchOriginal() {
  try {
    const res = await fetch("/api/books/" + currentPrune.bookId + "/prune/original");
    if (res.ok) {
      currentPrune.originalText = await res.text();
    } else {
      currentPrune.originalText = "（无法获取原文）";
    }
  } catch (_) {
    currentPrune.originalText = "（无法获取原文）";
  }
  $("#prune-original").textContent = currentPrune.originalText.slice(0, 2000);
  return currentPrune.originalText;
}

async function applyRestore() {
  const res = await fetch("/api/books/" + currentPrune.bookId + "/prune/restore", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ regions: currentPrune.restored }),
  });
  if (!res.ok) {
    const err = await res.json();
    alert("恢复失败：" + (err.detail || res.status));
    return;
  }
  currentPrune.result = await res.json();
  renderPrune();
}

$("#btn-close-prune").addEventListener("click", () => {
  $("#prune-panel").hidden = true;
});

/* ---------- 拆解面板（M4 蒸馏） ---------- */
let currentDistill = null; // { bookId, filename, timer }

function openDistill(bookId, filename) {
  currentDistill = { bookId, filename, timer: null };
  $("#distill-panel").hidden = false;
  $("#distill-title").textContent = "拆解：" + filename;
  $("#distill-progress").textContent = "选择书籍类型与压缩强度，点击开始拆解。";
  $("#distill-result").hidden = true;
  $("#distill-result").textContent = "";
  $("#distill-stats").textContent = "";
}

$("#btn-start-distill").addEventListener("click", async () => {
  if (!currentDistill) return;
  const btn = $("#btn-start-distill");
  btn.disabled = true;
  $("#distill-progress").textContent = "任务创建中…";
  const res = await fetch("/api/books/" + currentDistill.bookId + "/disassemble", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      book_type: $("#distill-type").value,
      strength: $("#distill-strength").value,
    }),
  });
  if (!res.ok) {
    const err = await res.json();
    $("#distill-progress").textContent = "启动失败：" + (err.detail || res.status);
    btn.disabled = false;
    return;
  }
  pollDistill();
});

async function pollDistill() {
  const res = await fetch("/api/tasks/" + currentDistill.bookId);
  if (!res.ok) {
    $("#distill-progress").textContent = "查询任务失败";
    $("#btn-start-distill").disabled = false;
    return;
  }
  const t = await res.json();
  if (t.status === "running" || t.status === "pending") {
    $("#distill-progress").textContent =
      (t.current > 0 ? "进度 " + t.current + "/" + (t.total || "?") + "：" : "") + t.stage;
    currentDistill.timer = setTimeout(pollDistill, 2000);
    return;
  }
  $("#btn-start-distill").disabled = false;
  if (t.status === "error") {
    $("#distill-progress").textContent = "拆解失败：" + (t.error || "未知错误");
    return;
  }
  // done：拉取结果
  const r = await fetch("/api/tasks/" + currentDistill.bookId + "/result");
  if (!r.ok) {
    $("#distill-progress").textContent = "获取结果失败：" + r.status;
    return;
  }
  const d = await r.json();
  $("#distill-progress").textContent = "拆解完成（API 调用 " + d.api_calls + " 次）。";
  $("#distill-result").hidden = false;
  $("#distill-result").textContent = d.final_text;
  $("#distill-stats").textContent =
    "原文 " + d.total_source_chars.toLocaleString() + " 字 → 精华 " +
    d.total_output_chars.toLocaleString() + " 字（保留 " +
    Math.round(d.kept_ratio * 100) + "%）" +
    (d.errors.length > 0 ? "；有 " + d.errors.length + " 处章节失败：" + d.errors.join("；") : "");
  $("#export-actions").hidden = false;
  $("#export-status").textContent = "";
}

$("#btn-preview-export").addEventListener("click", async () => {
  const res = await fetch("/api/books/" + currentDistill.bookId + "/export/preview");
  if (!res.ok) {
    $("#export-status").textContent = "预览失败：" + res.status;
    return;
  }
  $("#distill-result").textContent = await res.text();
  $("#export-status").textContent = "（以上为将导出的完整内容预览）";
});

$("#btn-do-export").addEventListener("click", async () => {
  const btn = $("#btn-do-export");
  btn.disabled = true;
  $("#export-status").textContent = "导出中…";
  const res = await fetch("/api/books/" + currentDistill.bookId + "/export", { method: "POST" });
  btn.disabled = false;
  if (!res.ok) {
    const err = await res.json();
    $("#export-status").textContent = "导出失败：" + (err.detail || res.status);
    return;
  }
  const e = await res.json();
  const size = e.size_bytes > 1024
    ? (e.size_bytes / 1024).toFixed(1) + " KB"
    : e.size_bytes + " B";
  $("#export-status").innerHTML =
    "已导出：" + e.filename + "（" + size + "） " +
    '<a href="/api/outputs/' + encodeURIComponent(e.filename) + '" download>下载</a>';
});

$("#btn-close-distill").addEventListener("click", () => {
  if (currentDistill && currentDistill.timer) {
    clearTimeout(currentDistill.timer);
  }
  $("#distill-panel").hidden = true;
  $("#export-actions").hidden = true;
});

/* ---------- 初始化 ---------- */
loadSettings();
refreshBooks();
setInterval(async () => {
  try {
    const res = await fetch("/api/books");
    const books = await res.json();
    if (books.some((b) => b.extract_status === "processing" || b.extract_status === "pending")) {
      refreshBooks();
    }
  } catch (_) { /* 服务未就绪时忽略 */ }
}, 2000);
