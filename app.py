# -*- coding: utf-8 -*-
"""
待办事项提醒系统（纯 Python 标准库版，零依赖）
- 本地网页管理待办事项
- 微信提醒（PushPlus）+ 邮箱提醒（SMTP），多级提前提醒
- 统计看板 / 数据导出 / 浏览器通知 / 开机自启 / 日志落盘
- 启动: python app.py  然后浏览器打开 http://127.0.0.1:5000
"""
import csv
import io
import json
import logging
import logging.handlers
import os
import re
import smtplib
import socket
import sqlite3
import sys
import threading
import time
import urllib.request
import urllib.error
from datetime import datetime, timedelta
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "todo.db")
STATIC_DIR = os.path.join(BASE_DIR, "static")
TEMPLATE_DIR = os.path.join(BASE_DIR, "templates")
LOG_DIR = os.path.join(BASE_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)

PUSHPLUS_API = "https://www.pushplus.plus/send"
CHECK_INTERVAL = 15          # 定时器检查间隔（秒）
SEND_TIMEOUT = 8             # 网络请求硬超时（秒），防止卡死
HOST = "127.0.0.1"
PORT = 5000

_db_lock = threading.Lock()
_stop_event = threading.Event()

MIME_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".csv": "text/csv; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}

SETTINGS_KEYS = [
    "pushplus_token", "notify_wechat",
    "smtp_host", "smtp_port", "smtp_ssl", "smtp_user", "smtp_pass", "mail_to",
    "notify_email",
]

# ---------------- 日志 ----------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.handlers.RotatingFileHandler(
            os.path.join(LOG_DIR, "todo.log"),
            maxBytes=1_000_000, backupCount=3, encoding="utf-8",
        ),
    ],
)


def log(msg):
    logging.info(msg)


# ---------------- 数据库 ----------------
def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def column_exists(conn, table, col):
    return any(r[1] == col for r in conn.execute(f"PRAGMA table_info({table})"))


def init_db():
    with _db_lock:
        conn = get_db()
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS todos (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                title          TEXT NOT NULL,
                note           TEXT DEFAULT '',
                due_time       TEXT,                -- 'YYYY-MM-DDTHH:MM' 本地时间
                priority       INTEGER DEFAULT 1,   -- 0低 1中 2高
                remind_points  TEXT DEFAULT '[]',   -- JSON 数组: 提前提醒分钟数，如 [60,10]
                notified_points TEXT DEFAULT '[]',  -- JSON 数组: 已提醒的分钟点
                repeat         TEXT DEFAULT 'none', -- none/daily/weekly/monthly
                done           INTEGER DEFAULT 0,
                created_at     TEXT,
                completed_at   TEXT                 -- 完成时间
            );
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT
            );
            """
        )
        # 老版本数据迁移
        if column_exists(conn, "todos", "remind_before"):
            if not column_exists(conn, "todos", "remind_points"):
                conn.execute("ALTER TABLE todos ADD COLUMN remind_points TEXT DEFAULT '[]'")
            conn.execute(
                "UPDATE todos SET remind_points = CASE WHEN remind_before > 0 "
                "THEN '[' || remind_before || ']' ELSE '[]' END "
                "WHERE remind_points = '[]'"
            )
        if not column_exists(conn, "todos", "notified_points"):
            conn.execute("ALTER TABLE todos ADD COLUMN notified_points TEXT DEFAULT '[]'")
        if not column_exists(conn, "todos", "completed_at"):
            conn.execute("ALTER TABLE todos ADD COLUMN completed_at TEXT")
        # 清理旧版本遗留字段（SQLite 3.35+ 支持 DROP COLUMN）
        for legacy in ("remind_before", "last_notified"):
            if column_exists(conn, "todos", legacy):
                try:
                    conn.execute(f"ALTER TABLE todos DROP COLUMN {legacy}")
                except sqlite3.OperationalError:
                    pass  # 旧版 SQLite 不支持则忽略
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


def notify_wechat_enabled():
    return get_setting("notify_wechat", "1") == "1" and bool(get_pushplus_token())


def notify_email_enabled():
    return (
        get_setting("notify_email", "1") == "1"
        and bool(get_setting("smtp_host"))
        and bool(get_setting("smtp_user"))
        and bool(get_setting("smtp_pass"))
        and bool(get_setting("mail_to"))
    )


# ---------------- 微信推送（PushPlus） ----------------
def send_pushplus(title, content):
    """发送到个人微信。返回 (ok, message)。绝不抛出、绝不长时间阻塞。"""
    token = get_pushplus_token()
    if not token:
        return False, "尚未配置 PushPlus token"
    payload = json.dumps(
        {"token": token, "title": title, "content": content, "template": "html"},
        ensure_ascii=False,
    ).encode("utf-8")
    req = urllib.request.Request(
        PUSHPLUS_API, data=payload,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    old_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(SEND_TIMEOUT)
    try:
        with urllib.request.urlopen(req, timeout=SEND_TIMEOUT) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        if result.get("code") == 200:
            return True, "发送成功"
        return False, f"PushPlus 返回错误: {result.get('msg', result)}"
    except Exception as e:
        return False, f"微信发送失败: {type(e).__name__}: {e}"
    finally:
        socket.setdefaulttimeout(old_timeout)


# ---------------- 邮件推送（SMTP） ----------------
def send_email(title, content_plain, content_html=None):
    """发送邮件。返回 (ok, message)。绝不抛出、绝不长时间阻塞。"""
    host = get_setting("smtp_host", "")
    try:
        port = int(get_setting("smtp_port", "465"))
    except (TypeError, ValueError):
        port = 465
    user = get_setting("smtp_user", "")
    pwd = get_setting("smtp_pass", "")
    to = get_setting("mail_to", "")
    if not (host and user and pwd and to):
        return False, "邮箱尚未配置（SMTP 服务器/账号/授权码/收件人）"
    msg = EmailMessage()
    msg["Subject"] = title
    msg["From"] = user
    msg["To"] = to
    msg.set_content(content_plain)
    if content_html:
        msg.add_alternative(content_html, subtype="html")
    old_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(SEND_TIMEOUT)
    server = None
    try:
        if get_setting("smtp_ssl", "1") == "1":
            server = smtplib.SMTP_SSL(host, port)
        else:
            server = smtplib.SMTP(host, port)
            server.starttls()
        server.login(user, pwd)
        server.send_message(msg)
        return True, "发送成功"
    except Exception as e:
        return False, f"邮件发送失败: {type(e).__name__}: {e}"
    finally:
        if server:
            try:
                server.quit()
            except Exception:
                pass
        socket.setdefaulttimeout(old_timeout)


# ---------------- 消息内容 ----------------
def fmt_time(iso):
    return iso.replace("T", " ") if iso else ""


PRIORITY_NAME = {0: "低", 1: "中", 2: "高"}
REPEAT_NAME = {"none": "不重复", "daily": "每天", "weekly": "每周", "monthly": "每月"}


def build_html(row):
    lines = [f"<b>{row['title']}</b>"]
    if row["note"]:
        lines.append(f"备注：{row['note']}")
    lines.append(f"截止时间：{fmt_time(row['due_time'])}")
    lines.append(f"优先级：{PRIORITY_NAME.get(row['priority'], '中')}")
    if row["repeat"] != "none":
        lines.append(f"重复：{REPEAT_NAME.get(row['repeat'])}")
    return "<br>".join(lines)


def build_plain(row):
    lines = [f"【待办提醒】{row['title']}"]
    if row["note"]:
        lines.append(f"备注：{row['note']}")
    lines.append(f"截止时间：{fmt_time(row['due_time'])}")
    lines.append(f"优先级：{PRIORITY_NAME.get(row['priority'], '中')}")
    if row["repeat"] != "none":
        lines.append(f"重复：{REPEAT_NAME.get(row['repeat'])}")
    return "\n".join(lines)


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


def parse_points(row):
    """把 remind_points 解析为分钟数列表；空则默认 [0]（到点提醒）。"""
    try:
        pts = json.loads(row["remind_points"] or "[]")
        pts = [int(x) for x in pts if str(x).lstrip("-").isdigit()]
    except (ValueError, TypeError):
        pts = []
    pts = sorted(set(pts), reverse=True)
    return pts if pts else [0]


def parse_points_input(raw):
    """把用户输入 '60,10' 解析为列表；空返回 []。"""
    if raw is None:
        return []
    parts = [p.strip() for p in str(raw).split(",") if p.strip()]
    out = []
    for p in parts:
        if p.lstrip("-").isdigit() and int(p) >= 0:
            out.append(int(p))
    return sorted(set(out), reverse=True)


# ---------------- 定时提醒线程 ----------------
def check_due():
    now = datetime.now()

    # 阶段一：快照待提醒任务（只读，快速）
    with _db_lock:
        conn = get_db()
        rows = conn.execute(
            "SELECT * FROM todos WHERE done=0 AND due_time IS NOT NULL AND due_time != ''"
        ).fetchall()
        conn.close()

    # 阶段二：无锁发送通知（多渠道，网络操作允许慢/失败）
    tasks = []  # (row, point)
    for row in rows:
        try:
            due = datetime.strptime(row["due_time"], "%Y-%m-%dT%H:%M")
        except ValueError:
            continue
        pts = parse_points(row)
        try:
            done_pts = set(json.loads(row["notified_points"] or "[]"))
        except (ValueError, TypeError):
            done_pts = set()
        for p in pts:
            if p in done_pts:
                continue
            if now >= due - timedelta(minutes=p):
                tasks.append((row, p))

    results = []
    for row, p in tasks:
        detail = []
        any_ok = False
        if notify_wechat_enabled():
            ok, msg = send_pushplus(f"待办提醒：{row['title']}", build_html(row))
            detail.append(f"微信:{'✓' if ok else '✗'} {msg}")
            any_ok = any_ok or ok
        if notify_email_enabled():
            ok, msg = send_email(
                f"待办提醒：{row['title']}", build_plain(row), build_html(row)
            )
            detail.append(f"邮件:{'✓' if ok else '✗'} {msg}")
            any_ok = any_ok or ok
        if detail:
            log(f"提醒 todo#{row['id']} 点={p}分 -> {'成功' if any_ok else '失败'} | " + " | ".join(detail))
        results.append((row, p, any_ok))

    # 阶段三：持锁更新状态（快速）。至少一个渠道成功才算提醒成功，失败下周期重试。
    if results:
        with _db_lock:
            conn = get_db()
            for row, p, any_ok in results:
                if not any_ok:
                    continue
                try:
                    done_pts = set(json.loads(row["notified_points"] or "[]"))
                except (ValueError, TypeError):
                    done_pts = set()
                done_pts.add(p)
                pts = parse_points(row)
                try:
                    due = datetime.strptime(row["due_time"], "%Y-%m-%dT%H:%M")
                except ValueError:
                    continue
                all_done = now >= due and done_pts.issuperset(set(pts))
                if row["repeat"] != "none" and all_done:
                    next_due = advance_due(row["due_time"], row["repeat"])
                    if next_due:
                        conn.execute(
                            "UPDATE todos SET due_time=?, notified_points=? WHERE id=?",
                            (next_due, "[]", row["id"]),
                        )
                        log(f"重复任务 todo#{row['id']} 已排到下一次: {next_due}")
                        continue
                conn.execute(
                    "UPDATE todos SET notified_points=? WHERE id=?",
                    (json.dumps(sorted(done_pts)), row["id"]),
                )
            conn.commit()
            conn.close()


def scheduler_loop():
    while not _stop_event.is_set():
        try:
            check_due()
        except Exception as e:
            log(f"调度器异常: {e}")
        _stop_event.wait(CHECK_INTERVAL)


# ---------------- 业务逻辑 ----------------
def row_to_dict(row):
    d = dict(row)
    d["due_time"] = d["due_time"] or ""
    try:
        d["remind_points"] = json.loads(d["remind_points"] or "[]")
    except (ValueError, TypeError):
        d["remind_points"] = []
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
    remind_points = json.dumps(parse_points_input(data.get("remind_points")))
    with _db_lock:
        conn = get_db()
        cur = conn.execute(
            "INSERT INTO todos(title, note, due_time, priority, remind_points, repeat, created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                title,
                (data.get("note") or "").strip(),
                (data.get("due_time") or "").strip(),
                int(data.get("priority", 1)),
                remind_points,
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
    remind_points = json.dumps(parse_points_input(data.get("remind_points")))
    with _db_lock:
        conn = get_db()
        conn.execute(
            "UPDATE todos SET title=?, note=?, due_time=?, priority=?, "
            "remind_points=?, repeat=?, notified_points='[]' WHERE id=?",
            (
                title,
                (data.get("note") or "").strip(),
                (data.get("due_time") or "").strip(),
                int(data.get("priority", 1)),
                remind_points,
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
        row = conn.execute("SELECT done FROM todos WHERE id=?", (todo_id,)).fetchone()
        if not row:
            conn.close()
            return {"ok": False, "msg": "待办不存在"}, 404
        new_done = 1 - row["done"]
        if new_done:
            conn.execute(
                "UPDATE todos SET done=1, completed_at=? WHERE id=?",
                (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), todo_id),
            )
        else:
            conn.execute("UPDATE todos SET done=0, completed_at=NULL WHERE id=?", (todo_id,))
        conn.commit()
        row = conn.execute("SELECT * FROM todos WHERE id=?", (todo_id,)).fetchone()
        conn.close()
    return {"ok": True, "todo": row_to_dict(row)}, 200


def delete_todo(todo_id):
    with _db_lock:
        conn = get_db()
        conn.execute("DELETE FROM todos WHERE id=?", (todo_id,))
        conn.commit()
        conn.close()
    return {"ok": True}, 200


def compute_stats():
    with _db_lock:
        conn = get_db()
        today = datetime.now().strftime("%Y-%m-%d")
        today_done = conn.execute(
            "SELECT COUNT(*) c FROM todos WHERE done=1 AND completed_at LIKE ?",
            (today + "%",),
        ).fetchone()["c"]
        total_active = conn.execute(
            "SELECT COUNT(*) c FROM todos WHERE done=0"
        ).fetchone()["c"]
        total_done = conn.execute(
            "SELECT COUNT(*) c FROM todos WHERE done=1"
        ).fetchone()["c"]
        total = conn.execute("SELECT COUNT(*) c FROM todos").fetchone()["c"]
        week = []
        for i in range(6, -1, -1):
            day = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
            n = conn.execute(
                "SELECT COUNT(*) c FROM todos WHERE done=1 AND completed_at LIKE ?",
                (day + "%",),
            ).fetchone()["c"]
            week.append({"date": day, "count": n})
        conn.close()
    return {
        "today_done": today_done,
        "total_active": total_active,
        "total_done": total_done,
        "total": total,
        "week": week,
    }


def export_data(fmt="json"):
    with _db_lock:
        conn = get_db()
        rows = [dict(r) for r in conn.execute("SELECT * FROM todos ORDER BY id").fetchall()]
        conn.close()
    if fmt == "csv":
        buf = io.StringIO()
        w = csv.writer(buf)
        if rows:
            w.writerow(rows[0].keys())
            for r in rows:
                w.writerow(r.values())
        return buf.getvalue(), "text/csv; charset=utf-8", "todo-backup.csv"
    return (
        json.dumps(rows, ensure_ascii=False, indent=2),
        "application/json; charset=utf-8",
        "todo-backup.json",
    )


def pending_notifications():
    """返回当前"到点但还没提醒成功"的待办，供浏览器通知轮询。"""
    now = datetime.now()
    out = []
    with _db_lock:
        conn = get_db()
        rows = conn.execute(
            "SELECT * FROM todos WHERE done=0 AND due_time IS NOT NULL AND due_time != ''"
        ).fetchall()
        conn.close()
    for row in rows:
        try:
            due = datetime.strptime(row["due_time"], "%Y-%m-%dT%H:%M")
        except ValueError:
            continue
        pts = parse_points(row)
        try:
            done_pts = set(json.loads(row["notified_points"] or "[]"))
        except (ValueError, TypeError):
            done_pts = set()
        for p in pts:
            if p not in done_pts and now >= due - timedelta(minutes=p):
                out.append({
                    "id": row["id"],
                    "title": row["title"],
                    "note": row["note"],
                    "due_time": row["due_time"],
                    "point": p,
                    "priority": row["priority"],
                })
                break
    return out


# ---------------- 开机自启 ----------------
def startup_bat_path():
    ap = os.environ.get("APPDATA", "")
    return os.path.join(
        ap, "Microsoft", "Windows", "Start Menu", "Programs", "Startup",
        "todo-reminder.bat",
    )


def autostart_status():
    return {"enabled": os.path.isfile(startup_bat_path()), "path": startup_bat_path()}


def autostart_set(enabled):
    path = startup_bat_path()
    try:
        if enabled:
            pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
            if not os.path.isfile(pyw):
                pyw = "pythonw.exe"
            content = (
                "@echo off\r\n"
                f'cd /d "{BASE_DIR}"\r\n'
                f'start "" "{pyw}" app.py\r\n'
            )
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
        else:
            if os.path.isfile(path):
                os.remove(path)
        return {"ok": True, **autostart_status()}
    except Exception as e:
        log(f"开机自启设置失败: {e}")
        return {"ok": False, "msg": f"设置失败: {e}"}


# ---------------- HTTP 服务 ----------------
class TodoHandler(BaseHTTPRequestHandler):
    server_version = "TodoReminder/2.0"

    def log_message(self, fmt, *args):
        pass  # 静默 HTTP 访问日志，避免刷屏

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

    def _send_download(self, body, ctype, filename):
        data = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(data)

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
            self._send_json(self._settings_dict())
        elif path == "/api/stats":
            self._send_json(compute_stats())
        elif path == "/api/pending-notifications":
            self._send_json(pending_notifications())
        elif path == "/api/autostart":
            self._send_json(autostart_status())
        elif path == "/api/export":
            qs = parse_qs(parsed.query)
            fmt = qs.get("format", ["json"])[0]
            body, ctype, fname = export_data(fmt if fmt in ("json", "csv") else "json")
            self._send_download(body, ctype, fname)
        else:
            self._send_json({"ok": False, "msg": "not found"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/todos":
            result, status = create_todo(self._read_json())
            self._send_json(result, status)
        elif path == "/api/settings":
            data = self._read_json()
            for k in SETTINGS_KEYS:
                if k in data and data[k] is not None:
                    set_setting(k, str(data[k]).strip())
            self._send_json({"ok": True, **self._settings_dict()})
        elif path == "/api/test-notify":
            ok, msg = send_pushplus(
                "✅ 待办系统测试消息",
                "如果你收到这条消息，说明微信通知已经配置成功！<br>以后待办到点就会这样提醒你。",
            )
            self._send_json({"ok": ok, "msg": msg})
        elif path == "/api/test-email":
            ok, msg = send_email(
                "✅ 待办系统测试邮件",
                "如果你收到这封邮件，说明邮箱提醒已经配置成功！以后待办到点就会发邮件提醒你。",
            )
            self._send_json({"ok": ok, "msg": msg})
        elif path == "/api/autostart":
            data = self._read_json()
            self._send_json(autostart_set(bool(data.get("enabled"))))
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

    def _settings_dict(self):
        token = get_pushplus_token()
        return {
            "pushplus_token": token,
            "wechat_configured": bool(token),
            "notify_wechat": get_setting("notify_wechat", "1") == "1",
            "smtp_host": get_setting("smtp_host", ""),
            "smtp_port": get_setting("smtp_port", "465"),
            "smtp_ssl": get_setting("smtp_ssl", "1") == "1",
            "smtp_user": get_setting("smtp_user", ""),
            "smtp_pass": get_setting("smtp_pass", ""),
            "mail_to": get_setting("mail_to", ""),
            "notify_email": get_setting("notify_email", "1") == "1",
            "email_configured": notify_email_enabled(),
        }


def main():
    init_db()
    threading.Thread(target=scheduler_loop, daemon=True,
                     name="reminder-scheduler").start()
    server = ThreadingHTTPServer((HOST, PORT), TodoHandler)
    server.daemon_threads = True
    log("=" * 52)
    log("  待办事项提醒系统 v2 已启动")
    log(f"  请在浏览器打开:  http://{HOST}:{PORT}")
    log("  按 Ctrl+C 停止服务")
    log("=" * 52)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("正在停止...")
    finally:
        _stop_event.set()
        server.server_close()


if __name__ == "__main__":
    main()
