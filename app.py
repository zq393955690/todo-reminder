# -*- coding: utf-8 -*-
"""
待办事项提醒系统（纯 Python 标准库版，零依赖）
- 本地网页管理待办事项
- 通过 PushPlus 推送到个人微信提醒
- 启动: python app.py  然后浏览器打开 http://127.0.0.1:5000
"""
import json
import os
import re
import socket
import sqlite3
import threading
import time
import urllib.request
import urllib.error
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "todo.db")
STATIC_DIR = os.path.join(BASE_DIR, "static")
TEMPLATE_DIR = os.path.join(BASE_DIR, "templates")

PUSHPLUS_API = "https://www.pushplus.plus/send"
CHECK_INTERVAL = 15          # 定时器检查间隔（秒）
HOST = "127.0.0.1"
PORT = 5000

_db_lock = threading.Lock()
_stop_event = threading.Event()

MIME_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}


# ---------------- 数据库 ----------------
def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with _db_lock:
        conn = get_db()
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS todos (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                title         TEXT NOT NULL,
                note          TEXT DEFAULT '',
                due_time      TEXT,                -- 'YYYY-MM-DDTHH:MM' 本地时间
                priority      INTEGER DEFAULT 1,   -- 0低 1中 2高
                remind_before INTEGER DEFAULT 0,   -- 提前提醒分钟数
                repeat        TEXT DEFAULT 'none', -- none/daily/weekly/monthly
                done          INTEGER DEFAULT 0,
                created_at    TEXT,
                last_notified TEXT                 -- 已提醒的 due_time
            );
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT
            );
            """
        )
        conn.commit()
        conn.close()


def get_setting(key, default=None):
    with _db_lock:
        conn = get_db()
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        conn.close()
        return row["value"] if row else default


def set_setting(key, value):
    with _db_lock:
        conn = get_db()
        conn.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        conn.commit()
        conn.close()


def get_pushplus_token():
    return get_setting("pushplus_token", "")


# ---------------- PushPlus 推送 ----------------
SEND_TIMEOUT = 8  # 推送网络请求硬超时（秒），防止卡死整个服务


def send_pushplus(title, content):
    """发送到个人微信。返回 (ok, message)。
    任何网络异常都返回失败，绝不抛出、绝不长时间阻塞。
    """
    token = get_pushplus_token()
    if not token:
        return False, "尚未配置 PushPlus token，请先在网页右上角设置"
    payload = json.dumps(
        {"token": token, "title": title, "content": content, "template": "html"},
        ensure_ascii=False,
    ).encode("utf-8")
    req = urllib.request.Request(
        PUSHPLUS_API, data=payload,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    old_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(SEND_TIMEOUT)  # 覆盖 DNS/连接/握手等所有阶段
    try:
        with urllib.request.urlopen(req, timeout=SEND_TIMEOUT) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        if result.get("code") == 200:
            return True, "发送成功"
        return False, f"PushPlus 返回错误: {result.get('msg', result)}"
    except Exception as e:  # 含 TimeoutError / URLError / 连接错误等
        return False, f"发送失败: {type(e).__name__}: {e}"
    finally:
        socket.setdefaulttimeout(old_timeout)


def fmt_time(iso):
    return iso.replace("T", " ") if iso else ""


PRIORITY_NAME = {0: "低", 1: "中", 2: "高"}
REPEAT_NAME = {"none": "不重复", "daily": "每天", "weekly": "每周", "monthly": "每月"}


def build_message(row):
    lines = [f"<b>{row['title']}</b>"]
    if row["note"]:
        lines.append(f"备注：{row['note']}")
    lines.append(f"截止时间：{fmt_time(row['due_time'])}")
    lines.append(f"优先级：{PRIORITY_NAME.get(row['priority'], '中')}")
    if row["repeat"] != "none":
        lines.append(f"重复：{REPEAT_NAME.get(row['repeat'])}")
    return "<br>".join(lines)


def advance_due(due_iso, repeat):
    """把截止时间推进到未来的下一次。"""
    try:
        dt = datetime.strptime(due_iso, "%Y-%m-%dT%H:%M")
    except ValueError:
        return None
    now = datetime.now()
    interval = {
        "daily": timedelta(days=1),
        "weekly": timedelta(days=7),
        "monthly": timedelta(days=30),
    }.get(repeat)
    if not interval:
        return None
    for _ in range(370):
        dt += interval
        if dt > now:
            return dt.strftime("%Y-%m-%dT%H:%M")
    return None


# ---------------- 定时提醒线程 ----------------
def check_due():
    now = datetime.now()

    # 阶段一：持锁快照出所有待提醒任务（只读，快速）
    with _db_lock:
        conn = get_db()
        rows = conn.execute(
            "SELECT * FROM todos WHERE done=0 AND due_time IS NOT NULL AND due_time != ''"
        ).fetchall()
        conn.close()

    # 阶段二：无锁发送通知（网络操作，允许慢/失败）
    results = []  # (row, ok)
    for row in rows:
        try:
            due = datetime.strptime(row["due_time"], "%Y-%m-%dT%H:%M")
        except ValueError:
            continue
        effective = due - timedelta(minutes=row["remind_before"] or 0)
        if now >= effective and row["last_notified"] != row["due_time"]:
            ok, msg = send_pushplus(f"待办提醒：{row['title']}", build_message(row))
            print(f"[notify] todo#{row['id']} -> ok={ok} ({msg})", flush=True)
            results.append((row, ok))

    # 阶段三：持锁更新状态（快速）。只有发送成功才标记已提醒，
    # 失败则下个周期自动重试，避免丢提醒。
    if results:
        with _db_lock:
            conn = get_db()
            for row, ok in results:
                if not ok:
                    continue
                if row["repeat"] != "none":
                    next_due = advance_due(row["due_time"], row["repeat"])
                    if next_due:
                        conn.execute(
                            "UPDATE todos SET due_time=?, last_notified=NULL WHERE id=?",
                            (next_due, row["id"]),
                        )
                    else:
                        conn.execute(
                            "UPDATE todos SET last_notified=? WHERE id=?",
                            (row["due_time"], row["id"]),
                        )
                else:
                    conn.execute(
                        "UPDATE todos SET last_notified=? WHERE id=?",
                        (row["due_time"], row["id"]),
                    )
            conn.commit()
            conn.close()


def scheduler_loop():
    while not _stop_event.is_set():
        try:
            check_due()
        except Exception as e:
            print(f"[scheduler] error: {e}")
        _stop_event.wait(CHECK_INTERVAL)


# ---------------- 业务逻辑 ----------------
def row_to_dict(row):
    d = dict(row)
    d["due_time"] = d["due_time"] or ""
    return d


def list_todos(filter_="all"):
    with _db_lock:
        conn = get_db()
        sql = "SELECT * FROM todos"
        if filter_ == "active":
            sql += " WHERE done=0"
        elif filter_ == "done":
            sql += " WHERE done=1"
        sql += (" ORDER BY done ASC, due_time IS NULL ASC, "
                "due_time ASC, priority DESC, id DESC")
        rows = conn.execute(sql).fetchall()
        conn.close()
    return [row_to_dict(r) for r in rows]


def create_todo(data):
    title = (data.get("title") or "").strip()
    if not title:
        return {"ok": False, "msg": "标题不能为空"}, 400
    with _db_lock:
        conn = get_db()
        cur = conn.execute(
            "INSERT INTO todos(title, note, due_time, priority, remind_before, repeat, created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                title,
                (data.get("note") or "").strip(),
                (data.get("due_time") or "").strip(),
                int(data.get("priority", 1)),
                int(data.get("remind_before", 0)),
                data.get("repeat", "none"),
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM todos WHERE id=?", (cur.lastrowid,)).fetchone()
        conn.close()
    return {"ok": True, "todo": row_to_dict(row)}, 200


def update_todo(todo_id, data):
    title = (data.get("title") or "").strip()
    if not title:
        return {"ok": False, "msg": "标题不能为空"}, 400
    with _db_lock:
        conn = get_db()
        conn.execute(
            "UPDATE todos SET title=?, note=?, due_time=?, priority=?, "
            "remind_before=?, repeat=?, last_notified=NULL WHERE id=?",
            (
                title,
                (data.get("note") or "").strip(),
                (data.get("due_time") or "").strip(),
                int(data.get("priority", 1)),
                int(data.get("remind_before", 0)),
                data.get("repeat", "none"),
                todo_id,
            ),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM todos WHERE id=?", (todo_id,)).fetchone()
        conn.close()
    if not row:
        return {"ok": False, "msg": "待办不存在"}, 404
    return {"ok": True, "todo": row_to_dict(row)}, 200


def toggle_todo(todo_id):
    with _db_lock:
        conn = get_db()
        conn.execute("UPDATE todos SET done = 1 - done WHERE id=?", (todo_id,))
        conn.commit()
        row = conn.execute("SELECT * FROM todos WHERE id=?", (todo_id,)).fetchone()
        conn.close()
    if not row:
        return {"ok": False, "msg": "待办不存在"}, 404
    return {"ok": True, "todo": row_to_dict(row)}, 200


def delete_todo(todo_id):
    with _db_lock:
        conn = get_db()
        conn.execute("DELETE FROM todos WHERE id=?", (todo_id,))
        conn.commit()
        conn.close()
    return {"ok": True}, 200


# ---------------- HTTP 服务 ----------------
class TodoHandler(BaseHTTPRequestHandler):
    server_version = "TodoReminder/1.0"

    def log_message(self, fmt, *args):  # 简化日志
        print(f"[http] {self.address_string()} {fmt % args}")

    # ---- 工具方法 ----
    def _send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, filepath):
        try:
            with open(filepath, "rb") as f:
                body = f.read()
        except OSError:
            self._send_json({"ok": False, "msg": "not found"}, 404)
            return
        ext = os.path.splitext(filepath)[1].lower()
        ctype = MIME_TYPES.get(ext, "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}

    # ---- 路由 ----
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/":
            self._send_file(os.path.join(TEMPLATE_DIR, "index.html"))
        elif path.startswith("/static/"):
            filepath = os.path.normpath(os.path.join(STATIC_DIR, path[len("/static/"):]))
            if filepath.startswith(STATIC_DIR) and os.path.isfile(filepath):
                self._send_file(filepath)
            else:
                self._send_json({"ok": False, "msg": "not found"}, 404)
        elif path == "/api/todos":
            qs = parse_qs(parsed.query)
            self._send_json(list_todos(qs.get("filter", ["all"])[0]))
        elif path == "/api/settings":
            token = get_pushplus_token()
            self._send_json({"pushplus_token": token, "configured": bool(token)})
        else:
            self._send_json({"ok": False, "msg": "not found"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/todos":
            result, status = create_todo(self._read_json())
            self._send_json(result, status)
        elif path == "/api/settings":
            data = self._read_json()
            token = (data.get("pushplus_token") or "").strip()
            set_setting("pushplus_token", token)
            self._send_json({"ok": True, "configured": bool(token)})
        elif path == "/api/test-notify":
            ok, msg = send_pushplus(
                "✅ 待办系统测试消息",
                "如果你收到这条消息，说明微信通知已经配置成功！<br>以后待办到点就会这样提醒你。",
            )
            self._send_json({"ok": ok, "msg": msg})
        elif path.startswith("/api/todos/") and path.endswith("/toggle"):
            m = re.match(r"^/api/todos/(\d+)/toggle$", path)
            if m:
                result, status = toggle_todo(int(m.group(1)))
                self._send_json(result, status)
            else:
                self._send_json({"ok": False, "msg": "not found"}, 404)
        else:
            self._send_json({"ok": False, "msg": "not found"}, 404)

    def do_PUT(self):
        path = urlparse(self.path).path
        m = re.match(r"^/api/todos/(\d+)$", path)
        if m:
            result, status = update_todo(int(m.group(1)), self._read_json())
            self._send_json(result, status)
        else:
            self._send_json({"ok": False, "msg": "not found"}, 404)

    def do_DELETE(self):
        path = urlparse(self.path).path
        m = re.match(r"^/api/todos/(\d+)$", path)
        if m:
            result, status = delete_todo(int(m.group(1)))
            self._send_json(result, status)
        else:
            self._send_json({"ok": False, "msg": "not found"}, 404)


def main():
    init_db()
    threading.Thread(target=scheduler_loop, daemon=True,
                     name="reminder-scheduler").start()
    server = ThreadingHTTPServer((HOST, PORT), TodoHandler)
    server.daemon_threads = True
    print("=" * 52, flush=True)
    print("  待办事项提醒系统已启动", flush=True)
    print(f"  请在浏览器打开:  http://{HOST}:{PORT}", flush=True)
    print("  按 Ctrl+C 停止服务", flush=True)
    print("=" * 52, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止...")
    finally:
        _stop_event.set()
        server.server_close()


if __name__ == "__main__":
    main()
