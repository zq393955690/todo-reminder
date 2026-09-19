/* 待办事项提醒系统 v2 - 前端逻辑 */
"use strict";

const $ = (sel) => document.querySelector(sel);

let currentFilter = "all";
let editingId = null;

const PRIORITY_TEXT = { 0: "低", 1: "中", 2: "高" };
const REPEAT_TEXT = { none: "不重复", daily: "每天", weekly: "每周", monthly: "每月" };
const WEEK_DAYS = ["日", "一", "二", "三", "四", "五", "六"];

/* ---------- 通用 ---------- */
function toast(msg, isErr = false) {
  const el = $("#toast");
  el.textContent = (isErr ? "⚠️ " : "✅ ") + msg;
  el.style.background = isErr ? "rgba(200,50,50,.92)" : "rgba(35,42,60,.92)";
  el.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { el.hidden = true; }, 2800);
}

async function api(url, options = {}) {
  const res = await fetch(url, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  return res.json();
}

function pointsToInput(pts) {
  return (pts && pts.length) ? pts.join(",") : "";
}

/* ---------- 待办加载与渲染 ---------- */
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

    const check = document.createElement("div");
    check.className = "todo-check" + (t.done ? " checked" : "");
    check.textContent = "✓";
    check.title = t.done ? "标记为未完成" : "标记为完成";
    check.addEventListener("click", () => toggleTodo(t.id));

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
    const pts = t.remind_points || [];
    if (pts.length) {
      const rm = document.createElement("span");
      rm.className = "badge";
      rm.textContent = "⏰ " + pts.map((p) => fmtPoint(p)).join(" · ");
      meta.appendChild(rm);
    }
    body.appendChild(meta);

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

function fmtPoint(min) {
  if (min <= 0) return "到点";
  if (min % 1440 === 0) return `提前${min / 1440}天`;
  if (min % 60 === 0) return `提前${min / 60}小时`;
  return `提前${min}分`;
}

/* ---------- 增删改查 ---------- */
function todoPayload(prefix) {
  return {
    title: $(`#${prefix}-title`).value,
    note: $(`#${prefix}-note`).value,
    due_time: $(`#${prefix}-due`).value || "",
    priority: Number($(`#${prefix}-priority`).value),
    remind_points: $(`#${prefix}-remind`).value,
    repeat: $(`#${prefix}-repeat`).value,
  };
}

async function addTodo(ev) {
  ev.preventDefault();
  const res = await api("/api/todos", {
    method: "POST",
    body: JSON.stringify(todoPayload("f")),
  });
  if (res.ok) {
    $("#add-form").reset();
    $("#f-priority").value = "1";
    toast("已添加待办");
    loadTodos();
    loadStats();
  } else {
    toast(res.msg || "添加失败", true);
  }
}

async function toggleTodo(id) {
  const res = await api(`/api/todos/${id}/toggle`, { method: "POST" });
  if (res.ok) {
    toast(res.todo.done ? "已完成 🎉" : "已恢复为待办");
    loadTodos();
    loadStats();
  }
}

async function deleteTodo(id) {
  if (!confirm("确定删除这条待办吗？")) return;
  const res = await api(`/api/todos/${id}`, { method: "DELETE" });
  if (res.ok) {
    toast("已删除");
    loadTodos();
    loadStats();
  }
}

/* ---------- 编辑 ---------- */
function openEdit(t) {
  editingId = t.id;
  $("#e-title").value = t.title;
  $("#e-note").value = t.note || "";
  $("#e-due").value = t.due_time || "";
  $("#e-priority").value = String(t.priority);
  $("#e-remind").value = pointsToInput(t.remind_points);
  $("#e-repeat").value = t.repeat || "none";
  $("#edit-modal").hidden = false;
}

async function saveEdit(ev) {
  ev.preventDefault();
  const res = await api(`/api/todos/${editingId}`, {
    method: "PUT",
    body: JSON.stringify(todoPayload("e")),
  });
  if (res.ok) {
    $("#edit-modal").hidden = true;
    toast("已保存");
    loadTodos();
  } else {
    toast(res.msg || "保存失败", true);
  }
}

/* ---------- 统计 ---------- */
async function loadStats() {
  const s = await api("/api/stats");
  $("#st-today").textContent = s.today_done;
  $("#st-active").textContent = s.total_active;
  $("#st-done").textContent = s.total_done;
  $("#st-total").textContent = s.total;
  renderWeekChart(s.week);
}

function renderWeekChart(week) {
  const chart = $("#week-chart");
  chart.innerHTML = "";
  const max = Math.max(1, ...week.map((d) => d.count));
  week.forEach((d) => {
    const col = document.createElement("div");
    col.className = "week-col";
    const bar = document.createElement("div");
    bar.className = "week-bar" + (d.count === 0 ? " zero" : "");
    bar.style.height = Math.max(3, Math.round((d.count / max) * 52)) + "px";
    const num = document.createElement("div");
    num.className = "week-count";
    num.textContent = d.count;
    const day = document.createElement("div");
    day.className = "week-day";
    day.textContent = WEEK_DAYS[new Date(d.date + "T00:00").getDay()];
    col.append(bar, num, day);
    chart.appendChild(col);
  });
}

/* ---------- 设置 ---------- */
async function loadSettings() {
  const s = await api("/api/settings");
  $("#s-token").value = s.pushplus_token || "";
  $("#s-notify-wechat").checked = !!s.notify_wechat;
  $("#s-host").value = s.smtp_host || "";
  $("#s-port").value = s.smtp_port || "465";
  $("#s-ssl").value = s.smtp_ssl ? "1" : "0";
  $("#s-user").value = s.smtp_user || "";
  $("#s-pass").value = s.smtp_pass || "";
  $("#s-to").value = s.mail_to || "";
  $("#s-notify-email").checked = !!s.notify_email;
  updatePushStatus(s.wechat_configured || s.email_configured);
  loadAutostartStatus();
}

async function saveSettings() {
  const payload = {
    pushplus_token: $("#s-token").value.trim(),
    notify_wechat: $("#s-notify-wechat").checked ? "1" : "0",
    smtp_host: $("#s-host").value.trim(),
    smtp_port: $("#s-port").value.trim(),
    smtp_ssl: $("#s-ssl").value,
    smtp_user: $("#s-user").value.trim(),
    smtp_pass: $("#s-pass").value,
    mail_to: $("#s-to").value.trim(),
    notify_email: $("#s-notify-email").checked ? "1" : "0",
  };
  const res = await api("/api/settings", {
    method: "POST",
    body: JSON.stringify(payload),
  });
  if (res.ok) {
    updatePushStatus(res.wechat_configured || res.email_configured);
    showSettingsMsg("保存成功", true);
  } else {
    showSettingsMsg("保存失败", false);
  }
}

function updatePushStatus(configured) {
  const el = $("#push-status");
  el.textContent = configured ? "通知已配置" : "通知未配置";
  el.classList.toggle("configured", configured);
}

async function testNotify() {
  const btn = $("#btn-test-notify");
  btn.disabled = true;
  btn.textContent = "发送中...";
  const res = await api("/api/test-notify", { method: "POST" });
  btn.disabled = false;
  btn.textContent = "📱 发送测试微信";
  showSettingsMsg(res.msg, res.ok);
}

async function testEmail() {
  const btn = $("#btn-test-email");
  btn.disabled = true;
  btn.textContent = "发送中...";
  const res = await api("/api/test-email", { method: "POST" });
  btn.disabled = false;
  btn.textContent = "📧 发送测试邮件";
  showSettingsMsg(res.msg, res.ok);
}

function showSettingsMsg(msg, ok) {
  const el = $("#settings-msg");
  el.textContent = msg;
  el.className = "settings-msg " + (ok ? "ok" : "err");
}

/* ---------- 开机自启 ---------- */
async function loadAutostartStatus() {
  const s = await api("/api/autostart");
  $("#s-autostart").checked = !!s.enabled;
}

async function toggleAutostart() {
  const checked = $("#s-autostart").checked;
  const res = await api("/api/autostart", {
    method: "POST",
    body: JSON.stringify({ enabled: checked }),
  });
  if (res.ok) {
    toast(checked ? "已开启开机自启" : "已关闭开机自启");
  } else {
    toast("设置失败", true);
    $("#s-autostart").checked = !checked;
  }
}

/* ---------- 浏览器桌面通知 ---------- */
function initDesktopNotify() {
  const btn = $("#btn-desktop-notify");
  if (!("Notification" in window)) {
    btn.hidden = true;
    return;
  }
  btn.addEventListener("click", async () => {
    const perm = await Notification.requestPermission();
    btn.textContent = perm === "granted" ? "🔔 已开启" : "🔔 桌面通知";
    if (perm === "granted") {
      toast("桌面通知已开启，到点会在浏览器弹窗提醒");
      pollPending();
    } else {
      toast("浏览器拒绝了通知权限，可在地址栏重新允许", true);
    }
  });
  if (Notification.permission === "granted") {
    btn.textContent = "🔔 已开启";
    pollPending();
  } else if (Notification.permission === "denied") {
    btn.textContent = "🔔 被拒绝";
  }
  // 每 12 秒轮询一次"到点未提醒"的任务
  setInterval(() => {
    if (Notification.permission === "granted") pollPending();
  }, 12000);
}

let pendingSeen = new Set();

async function pollPending() {
  try {
    const list = await api("/api/pending-notifications");
    list.forEach((t) => {
      const key = `${t.id}|${t.due_time}|${t.point}`;
      if (pendingSeen.has(key)) return;
      pendingSeen.add(key);
      new Notification(`⏰ 待办提醒：${t.title}`, {
        body: (t.note ? t.note + "\n" : "") + `截止时间：${t.due_time.replace("T", " ")}`,
        tag: key,
      });
      toast(`到点提醒：${t.title}`);
    });
    if (pendingSeen.size > 500) pendingSeen.clear(); // 防止无限增长
  } catch (e) { /* 忽略轮询错误 */ }
}

/* ---------- 事件绑定 ---------- */
$("#add-form").addEventListener("submit", addTodo);
$("#edit-form").addEventListener("submit", saveEdit);
$("#btn-cancel-edit").addEventListener("click", () => { $("#edit-modal").hidden = true; });
$("#btn-settings").addEventListener("click", () => { $("#settings-modal").hidden = false; loadSettings(); });
$("#btn-cancel-settings").addEventListener("click", () => { $("#settings-modal").hidden = true; });
$("#btn-save-settings").addEventListener("click", saveSettings);
$("#btn-test-notify").addEventListener("click", testNotify);
$("#btn-test-email").addEventListener("click", testEmail);
$("#s-autostart").addEventListener("change", toggleAutostart);

document.querySelectorAll(".filter-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".filter-btn").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    currentFilter = btn.dataset.filter;
    loadTodos();
  });
});

document.querySelectorAll(".modal").forEach((m) => {
  m.addEventListener("click", (e) => {
    if (e.target === m) m.hidden = true;
  });
});

// 启动
loadTodos();
loadStats();
loadSettings();
initDesktopNotify();
