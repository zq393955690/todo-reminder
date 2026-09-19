/* 待办事项提醒系统 - 前端逻辑 */
"use strict";

const $ = (sel) => document.querySelector(sel);

let currentFilter = "all";
let editingId = null;

const PRIORITY_TEXT = { 0: "低", 1: "中", 2: "高" };
const REPEAT_TEXT = { none: "不重复", daily: "每天", weekly: "每周", monthly: "每月" };

/* ---------- 通用 ---------- */
function toast(msg, isErr = false) {
  const el = $("#toast");
  el.textContent = (isErr ? "⚠️ " : "✅ ") + msg;
  el.style.background = isErr ? "rgba(200,50,50,.92)" : "rgba(35,42,60,.92)";
  el.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { el.hidden = true; }, 2600);
}

async function api(url, options = {}) {
  const res = await fetch(url, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  return res.json();
}

/* ---------- 数据加载与渲染 ---------- */
async function loadTodos() {
  const todos = await api(`/api/todos?filter=${currentFilter}`);
  renderList(todos);
}

function renderList(todos) {
  const list = $("#todo-list");
  const emptyTip = $("#empty-tip");
  list.innerHTML = "";
  emptyTip.hidden = todos.length > 0;

  todos.forEach((t) => {
    const item = document.createElement("div");
    item.className = `todo-item priority-${t.priority}${t.done ? " done" : ""}`;
    item.dataset.id = t.id;

    // 勾选
    const check = document.createElement("div");
    check.className = "todo-check" + (t.done ? " checked" : "");
    check.textContent = "✓";
    check.title = t.done ? "标记为未完成" : "标记为完成";
    check.addEventListener("click", () => toggleTodo(t.id));

    // 主体
    const body = document.createElement("div");
    body.className = "todo-body";
    const title = document.createElement("div");
    title.className = "todo-title";
    title.textContent = t.title;
    body.appendChild(title);

    if (t.note) {
      const note = document.createElement("div");
      note.className = "todo-note";
      note.textContent = t.note;
      body.appendChild(note);
    }

    // 元信息
    const meta = document.createElement("div");
    meta.className = "todo-meta";
    if (t.due_time) {
      const due = document.createElement("span");
      due.className = "badge";
      due.textContent = "🕐 " + t.due_time.replace("T", " ");
      if (!t.done && new Date(t.due_time) < new Date()) due.classList.add("overdue");
      meta.appendChild(due);
    }
    const pri = document.createElement("span");
    pri.className = "badge p-" + (t.priority === 2 ? "h" : t.priority === 1 ? "m" : "l");
    pri.textContent = "优先级：" + PRIORITY_TEXT[t.priority];
    meta.appendChild(pri);
    if (t.repeat !== "none") {
      const rep = document.createElement("span");
      rep.className = "badge repeat";
      rep.textContent = "🔁 " + REPEAT_TEXT[t.repeat];
      meta.appendChild(rep);
    }
    if (t.remind_before > 0) {
      const rm = document.createElement("span");
      rm.className = "badge";
      rm.textContent = "⏰ 提前 " + t.remind_before + " 分钟";
      meta.appendChild(rm);
    }
    body.appendChild(meta);

    // 操作按钮
    const actions = document.createElement("div");
    actions.className = "todo-actions";
    const btnEdit = document.createElement("button");
    btnEdit.className = "icon-btn";
    btnEdit.textContent = "✏️";
    btnEdit.title = "编辑";
    btnEdit.addEventListener("click", () => openEdit(t));
    const btnDel = document.createElement("button");
    btnDel.className = "icon-btn";
    btnDel.textContent = "🗑️";
    btnDel.title = "删除";
    btnDel.addEventListener("click", () => deleteTodo(t.id));
    actions.append(btnEdit, btnDel);

    item.append(check, body, actions);
    list.appendChild(item);
  });

  $("#count-hint").textContent = `共 ${todos.length} 条`;
}

/* ---------- 增删改查 ---------- */
async function addTodo(ev) {
  ev.preventDefault();
  const payload = {
    title: $("#f-title").value,
    note: $("#f-note").value,
    due_time: $("#f-due").value || "",
    priority: Number($("#f-priority").value),
    remind_before: Number($("#f-remind").value || 0),
    repeat: $("#f-repeat").value,
  };
  const res = await api("/api/todos", {
    method: "POST",
    body: JSON.stringify(payload),
  });
  if (res.ok) {
    $("#add-form").reset();
    $("#f-priority").value = "1";
    toast("已添加待办");
    loadTodos();
  } else {
    toast(res.msg || "添加失败", true);
  }
}

async function toggleTodo(id) {
  const res = await api(`/api/todos/${id}/toggle`, { method: "POST" });
  if (res.ok) {
    toast(res.todo.done ? "已完成 🎉" : "已恢复为待办");
    loadTodos();
  }
}

async function deleteTodo(id) {
  if (!confirm("确定删除这条待办吗？")) return;
  const res = await api(`/api/todos/${id}`, { method: "DELETE" });
  if (res.ok) {
    toast("已删除");
    loadTodos();
  }
}

/* ---------- 编辑 ---------- */
function openEdit(t) {
  editingId = t.id;
  $("#e-title").value = t.title;
  $("#e-note").value = t.note || "";
  $("#e-due").value = t.due_time || "";
  $("#e-priority").value = String(t.priority);
  $("#e-remind").value = t.remind_before || 0;
  $("#e-repeat").value = t.repeat || "none";
  $("#edit-modal").hidden = false;
}

async function saveEdit(ev) {
  ev.preventDefault();
  const payload = {
    title: $("#e-title").value,
    note: $("#e-note").value,
    due_time: $("#e-due").value || "",
    priority: Number($("#e-priority").value),
    remind_before: Number($("#e-remind").value || 0),
    repeat: $("#e-repeat").value,
  };
  const res = await api(`/api/todos/${editingId}`, {
    method: "PUT",
    body: JSON.stringify(payload),
  });
  if (res.ok) {
    $("#edit-modal").hidden = true;
    toast("已保存");
    loadTodos();
  } else {
    toast(res.msg || "保存失败", true);
  }
}

/* ---------- 设置 ---------- */
async function loadSettings() {
  const s = await api("/api/settings");
  $("#s-token").value = s.pushplus_token || "";
  updatePushStatus(s.configured);
}

function updatePushStatus(configured) {
  const el = $("#push-status");
  el.textContent = configured ? "微信通知已配置" : "微信通知未配置";
  el.classList.toggle("configured", configured);
}

async function saveSettings() {
  const res = await api("/api/settings", {
    method: "POST",
    body: JSON.stringify({ pushplus_token: $("#s-token").value.trim() }),
  });
  if (res.ok) {
    updatePushStatus(res.configured);
    showSettingsMsg("保存成功", true);
  }
}

async function testNotify() {
  const btn = $("#btn-test-notify");
  btn.disabled = true;
  btn.textContent = "发送中...";
  const res = await api("/api/test-notify", { method: "POST" });
  btn.disabled = false;
  btn.textContent = "📱 发送测试";
  showSettingsMsg(res.msg, res.ok);
}

function showSettingsMsg(msg, ok) {
  const el = $("#settings-msg");
  el.textContent = msg;
  el.className = "settings-msg " + (ok ? "ok" : "err");
}

/* ---------- 事件绑定 ---------- */
$("#add-form").addEventListener("submit", addTodo);
$("#edit-form").addEventListener("submit", saveEdit);
$("#btn-cancel-edit").addEventListener("click", () => { $("#edit-modal").hidden = true; });
$("#btn-settings").addEventListener("click", () => { $("#settings-modal").hidden = false; loadSettings(); });
$("#btn-cancel-settings").addEventListener("click", () => { $("#settings-modal").hidden = true; });
$("#btn-save-settings").addEventListener("click", saveSettings);
$("#btn-test-notify").addEventListener("click", testNotify);

document.querySelectorAll(".filter-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".filter-btn").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    currentFilter = btn.dataset.filter;
    loadTodos();
  });
});

// 点击弹窗遮罩关闭
document.querySelectorAll(".modal").forEach((m) => {
  m.addEventListener("click", (e) => {
    if (e.target === m) m.hidden = true;
  });
});

// 启动
loadTodos();
loadSettings();
