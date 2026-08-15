import os
import json
import re
import secrets
import imaplib
import email
from email.header import decode_header
from email.utils import parsedate_to_datetime
from datetime import datetime, timedelta
from functools import wraps

from flask import Flask, request, render_template_string, redirect, url_for, session, flash

os.environ["TZ"] = "Asia/Shanghai"

app = Flask(__name__)
app.secret_key = "mail-auto-secret-key-2026-v1"
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=2)

ADMIN_PASSWORD = "060910"
DOMAIN = "mailauto.zeabur.app"
PORT = int(os.environ.get("PORT", 8080))

# 数据目录：优先从环境变量读取，默认 /data
# Zeabur 挂载 Volume 到 /data，并设置环境变量 DATA_DIR=/data
DATA_DIR = "/data"
os.makedirs(DATA_DIR, exist_ok=True)

# accounts.txt 放在代码目录（随代码部署）
ACCOUNTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "accounts.txt")
# 动态数据放在挂载目录
LINKS_FILE = os.path.join(DATA_DIR, "links.json")
BACKUP_FILE = os.path.join(DATA_DIR, "links_backup.json")


def load_json(filepath, default=None):
    if default is None:
        default = {}
    if not os.path.exists(filepath):
        return default
    try:
        with open(filepath, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return default


def save_json(filepath, data):
    with open(filepath, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)


def save_links(data):
    save_json(LINKS_FILE, data)
    save_json(BACKUP_FILE, data)
    # 同时创建带时间戳的备份（保留最近30个）
    create_timestamp_backup()
    cleanup_old_backups()


def parse_accounts():
    accounts = {}
    print(f"[DEBUG] 尝试读取: {ACCOUNTS_FILE}, 存在: {os.path.exists(ACCOUNTS_FILE)}")
    if not os.path.exists(ACCOUNTS_FILE):
        print(f"[DEBUG] 文件不存在: {ACCOUNTS_FILE}")
        # 列出代码目录下所有文件，帮助排查
        code_dir = os.path.dirname(os.path.abspath(__file__))
        if os.path.exists(code_dir):
            files = os.listdir(code_dir)
            print(f"[DEBUG] 代码目录 {code_dir} 内容: {files}")
        return accounts
    with open(ACCOUNTS_FILE, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "----" in line:
                parts = line.split("----")
                if len(parts) == 2:
                    email_addr = parts[0].strip()
                    auth_code = parts[1].strip()
                    if email_addr and auth_code:
                        accounts[email_addr] = auth_code
            else:
                parts = line.split()
                if len(parts) >= 2:
                    auth_code = parts[-1].strip()
                    emails = [p.strip() for p in parts[:-1] if p.strip()]
                    for email_addr in emails:
                        if email_addr and "@" in email_addr:
                            accounts[email_addr] = auth_code
    print(f"[DEBUG] 成功解析 {len(accounts)} 个邮箱")
    return accounts


def get_links():
    return load_json(LINKS_FILE, {})


def generate_link_id():
    return secrets.token_urlsafe(16)


def create_sub_link(allowed_emails, days, max_emails=10):
    links = get_links()
    link_id = generate_link_id()
    now = datetime.now()
    expire = now + timedelta(days=int(days))
    link_data = {
        "id": link_id,
        "allowed_emails": allowed_emails,
        "max_emails": int(max_emails),
        "created_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "expire_at": expire.strftime("%Y-%m-%d %H:%M:%S"),
        "status": "有效"
    }
    links[link_id] = link_data
    save_links(links)
    return link_id


def invalidate_link(link_id):
    links = get_links()
    if link_id in links:
        links[link_id]["status"] = "已失效"
        save_links(links)
        return True
    return False


def get_link(link_id):
    links = get_links()
    return links.get(link_id)


def is_link_valid(link_data):
    if not link_data:
        return False
    if link_data.get("status") != "有效":
        return False
    expire_at = link_data.get("expire_at")
    if expire_at:
        try:
            expire_dt = datetime.strptime(expire_at, "%Y-%m-%d %H:%M:%S")
            if datetime.now() > expire_dt:
                return False
        except ValueError:
            pass
    return True


def decode_str(s):
    if not s:
        return ""
    try:
        parts = decode_header(s)
        result = []
        for part, charset in parts:
            if isinstance(part, bytes):
                result.append(part.decode(charset or "utf-8", errors="ignore"))
            else:
                result.append(str(part))
        return "".join(result)
    except Exception:
        return str(s)


def get_email_body(msg):
    body_html = ""
    body_text = ""
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            content_disposition = str(part.get("Content-Disposition", ""))
            if "attachment" in content_disposition:
                continue
            try:
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    text = payload.decode(charset, errors="ignore")
                    if content_type == "text/html":
                        body_html = text
                    elif content_type == "text/plain":
                        body_text = text
            except Exception:
                pass
    else:
        try:
            payload = msg.get_payload(decode=True)
            if payload:
                charset = msg.get_content_charset() or "utf-8"
                text = payload.decode(charset, errors="ignore")
                if msg.get_content_type() == "text/html":
                    body_html = text
                else:
                    body_text = text
        except Exception:
            pass
    return body_html if body_html else body_text


def get_ad_folders(mail):
    ad_folders = []
    try:
        _, folders = mail.list()
        for folder in folders:
            folder_str = folder.decode("utf-8", errors="ignore")
            if any(kw in folder_str for kw in ["广告", "Advertise", "Subscribe", "订阅", "Promo"]):
                match = re.search(r'"([^"]+)"$', folder_str)
                if match:
                    fname = match.group(1)
                    if fname not in ad_folders:
                        ad_folders.append(fname)
    except Exception:
        pass
    return ad_folders


def get_folder_label(folder):
    if folder == "INBOX":
        return "收件箱", "inbox"
    elif folder == "Junk":
        return "垃圾箱", "junk"
    else:
        return "广告邮件", "ad"



def format_email_time(date_str):
    """
    将邮件原始时间格式化为北京时间，风格类似QQ邮箱
    - 今天 -> 今天 HH:MM
    - 昨天 -> 昨天 HH:MM
    - 本周 -> 周X HH:MM
    - 今年 -> MM-DD HH:MM
    - 更早 -> YYYY-MM-DD HH:MM
    """
    if not date_str:
        return ""

    try:
        dt = parsedate_to_datetime(date_str)
    except Exception:
        try:
            dt = datetime.strptime(date_str.strip(), "%a, %d %b %Y %H:%M:%S %z")
        except Exception:
            return date_str

    # 统一转换为 offset-naive 的北京时间
    try:
        if dt.tzinfo is not None:
            # 带时区的，先转UTC再+8小时（简化处理）
            from datetime import timezone
            utc_dt = dt.astimezone(timezone.utc)
            dt = utc_dt.replace(tzinfo=None) + timedelta(hours=8)
        else:
            # 无时区，假设是UTC，+8小时
            dt = dt + timedelta(hours=8)
    except Exception:
        # 转换失败，去掉时区信息
        if dt.tzinfo is not None:
            dt = dt.replace(tzinfo=None)

    # 确保 dt 是 offset-naive
    if dt.tzinfo is not None:
        dt = dt.replace(tzinfo=None)

    now = datetime.now()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday = today - timedelta(days=1)

    # 判断日期
    if dt.date() == today.date():
        return dt.strftime("今天 %H:%M")
    elif dt.date() == yesterday.date():
        return dt.strftime("昨天 %H:%M")
    elif dt.year == today.year:
        weekday_names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        days_diff = (today.date() - dt.date()).days
        if 0 < days_diff < 7:
            weekday = weekday_names[dt.weekday()]
            return dt.strftime(f"{weekday} %H:%M")
        else:
            return dt.strftime("%m-%d %H:%M")
    else:
        return dt.strftime("%Y-%m-%d %H:%M")


def fetch_emails(email_addr, auth_code, limit=10):
    folders = ["INBOX", "Junk"]
    try:
        mail = imaplib.IMAP4_SSL("imap.qq.com", 993, timeout=15)
        mail.login(email_addr, auth_code)
        ad_folders = get_ad_folders(mail)
        for f in ad_folders:
            if f not in folders:
                folders.append(f)
        mail.logout()
    except Exception:
        pass

    all_emails = []
    for folder in folders:
        try:
            mail = imaplib.IMAP4_SSL("imap.qq.com", 993, timeout=15)
            mail.login(email_addr, auth_code)
            status, _ = mail.select(folder)
            if status != "OK":
                mail.logout()
                continue
            status, messages = mail.search(None, "ALL")
            if status != "OK":
                mail.logout()
                continue
            mail_ids = messages[0].split()
            if not mail_ids:
                mail.logout()
                continue
            fetch_count = min(len(mail_ids), limit * 2)
            fetch_ids = mail_ids[-fetch_count:]
            folder_label, folder_type = get_folder_label(folder)
            for mid in reversed(fetch_ids):
                status, msg_data = mail.fetch(mid, "(RFC822)")
                if status != "OK":
                    continue
                msg = email.message_from_bytes(msg_data[0][1])
                subject = decode_str(msg.get("Subject", "（无主题）"))
                from_ = decode_str(msg.get("From", "未知"))
                raw_date = msg.get("Date", "")
                date_dt = None
                try:
                    date_dt = parsedate_to_datetime(raw_date)
                    # 统一转为 offset-naive 用于排序
                    if date_dt.tzinfo is not None:
                        from datetime import timezone
                        utc_dt = date_dt.astimezone(timezone.utc)
                        date_dt = utc_dt.replace(tzinfo=None) + timedelta(hours=8)
                except Exception:
                    date_dt = datetime.now()
                date_str = format_email_time(raw_date)
                body = get_email_body(msg)
                preview_text = re.sub(r"<[^>]+>", " ", body)
                preview_text = re.sub(r"\s+", " ", preview_text).strip()
                preview = preview_text[:120] + ("..." if len(preview_text) > 120 else "")
                all_emails.append({
                    "folder": folder,
                    "folder_label": folder_label,
                    "folder_type": folder_type,
                    "subject": subject,
                    "from": from_,
                    "date_str": date_str,
                    "date_dt": date_dt,
                    "body_html": body,
                    "preview": preview,
                })
            mail.logout()
        except Exception:
            try:
                mail.logout()
            except Exception:
                pass
    
    # 确保所有 date_dt 都是 offset-naive
    for e in all_emails:
        if e["date_dt"] is not None and e["date_dt"].tzinfo is not None:
            from datetime import timezone
            utc_dt = e["date_dt"].astimezone(timezone.utc)
            e["date_dt"] = utc_dt.replace(tzinfo=None) + timedelta(hours=8)
    all_emails.sort(key=lambda x: x["date_dt"] or datetime.min, reverse=True)

    return all_emails[:limit]


def sanitize_email_html(html_content):
    if not html_content:
        return ""
    html_content = re.sub(r'<script[^>]*>.*?</script>', '', html_content, flags=re.DOTALL | re.IGNORECASE)
    html_content = re.sub(r'<script[^>]*/?>', '', html_content, flags=re.IGNORECASE)
    html_content = re.sub(r'<noscript[^>]*>.*?</noscript>', '', html_content, flags=re.DOTALL | re.IGNORECASE)
    html_content = re.sub(r'<iframe[^>]*>.*?</iframe>', '', html_content, flags=re.DOTALL | re.IGNORECASE)
    html_content = re.sub(r'<(object|embed)[^>]*>.*?</\1>', '', html_content, flags=re.DOTALL | re.IGNORECASE)
    html_content = re.sub(r'<form[^>]*>', '<div>', html_content, flags=re.IGNORECASE)
    html_content = re.sub(r'</form>', '</div>', html_content, flags=re.IGNORECASE)
    html_content = re.sub(r'<(input|button|textarea|select|option)[^>]*>', '', html_content, flags=re.IGNORECASE)
    html_content = re.sub(r'</(textarea|select)>', '', html_content, flags=re.IGNORECASE)
    html_content = re.sub(r'\son\w+\s*=\s*"[^"]*"', '', html_content, flags=re.IGNORECASE)
    html_content = re.sub(r"\son\w+\s*=\s*'[^']*'", '', html_content, flags=re.IGNORECASE)
    html_content = re.sub(r'\son\w+\s*=\s*[^\s>]+', '', html_content, flags=re.IGNORECASE)
    html_content = re.sub(r'javascript:', '', html_content, flags=re.IGNORECASE)
    html_content = re.sub(r'<meta[^>]*http-equiv\s*=\s*["\']?refresh["\']?[^>]*>', '', html_content, flags=re.IGNORECASE)
    html_content = re.sub(r'<a\s+([^>]*?)target\s*=\s*["\'][^"\']*["\']([^>]*?)>',
                          r'<a \1target="_blank"\2>', html_content, flags=re.IGNORECASE)
    html_content = re.sub(r'<a\s+(?!.*?target=)([^>]+)>',
                          r'<a \1 target="_blank">', html_content, flags=re.IGNORECASE)
    return html_content


def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get("admin_logged_in"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated_function


COMMON_CSS = """
<style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif; background: #f0f2f5; color: #333; line-height: 1.6; }
    .container { max-width: 1200px; margin: 0 auto; padding: 20px; }
    .card { background: #fff; border-radius: 12px; box-shadow: 0 2px 8px rgba(0,0,0,0.08); padding: 24px; margin-bottom: 20px; }
    h1 { font-size: 26px; margin-bottom: 20px; color: #1a1a2e; }
    h2 { font-size: 18px; margin-bottom: 16px; color: #16213e; border-left: 4px solid #e94560; padding-left: 12px; }
    .btn { display: inline-block; padding: 10px 22px; border-radius: 8px; border: none; cursor: pointer; font-size: 14px; transition: all 0.2s; text-decoration: none; }
    .btn-primary { background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: #fff; }
    .btn-primary:hover { opacity: 0.9; transform: translateY(-1px); }
    .btn-danger { background: #e74c3c; color: #fff; }
    .btn-danger:hover { background: #c0392b; }
    .btn-secondary { background: #95a5a6; color: #fff; }
    .btn-secondary:hover { background: #7f8c8d; }
    .btn-sm { padding: 6px 14px; font-size: 13px; }
    input, select, textarea { width: 100%; padding: 12px 14px; border: 1px solid #ddd; border-radius: 8px; font-size: 14px; margin-bottom: 12px; transition: border-color 0.2s; }
    input:focus, select:focus, textarea:focus { outline: none; border-color: #667eea; }
    label { display: block; margin-bottom: 6px; font-weight: 500; color: #555; font-size: 14px; }
    .form-group { margin-bottom: 16px; }
    .grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }
    .grid-3 { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 20px; }
    table { width: 100%; border-collapse: collapse; margin-top: 12px; font-size: 14px; }
    th, td { padding: 12px; text-align: left; border-bottom: 1px solid #eee; }
    th { background: #f8f9fa; font-weight: 600; color: #555; font-size: 13px; }
    tr:hover { background: #f8f9fa; }
    .badge { display: inline-block; padding: 4px 12px; border-radius: 20px; font-size: 12px; font-weight: 500; }
    .badge-success { background: #d4edda; color: #155724; }
    .badge-danger { background: #f8d7da; color: #721c24; }
    .badge-warning { background: #fff3cd; color: #856404; }
    .stats { display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; margin-bottom: 24px; }
    .stat-card { background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: #fff; padding: 24px; border-radius: 12px; text-align: center; }
    .stat-card h3 { font-size: 36px; margin-bottom: 6px; }
    .stat-card p { font-size: 14px; opacity: 0.9; }
    .nav { background: #1a1a2e; padding: 14px 0; margin-bottom: 20px; }
    .nav-inner { max-width: 1200px; margin: 0 auto; padding: 0 20px; display: flex; justify-content: space-between; align-items: center; }
    .nav a { color: #fff; text-decoration: none; margin-right: 24px; font-size: 14px; transition: color 0.2s; }
    .nav a:hover { color: #e94560; }
    .alert { padding: 14px 18px; border-radius: 8px; margin-bottom: 20px; font-size: 14px; }
    .alert-success { background: #d4edda; color: #155724; border: 1px solid #c3e6cb; }
    .alert-error { background: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; }
    .alert-info { background: #d1ecf1; color: #0c5460; border: 1px solid #bee5eb; }
    .mt-2 { margin-top: 12px; }
    .mt-3 { margin-top: 18px; }
    .mb-2 { margin-bottom: 12px; }
    .text-muted { color: #888; font-size: 13px; }
    .empty-state { text-align: center; padding: 40px; color: #999; }
    .link-url { background: #f0f2f5; padding: 8px 12px; border-radius: 6px; font-family: monospace; font-size: 13px; word-break: break-all; display: block; margin-top: 8px; }
    @media (max-width: 768px) {
        .grid-2, .grid-3, .stats { grid-template-columns: 1fr; }
        .nav-inner { flex-wrap: wrap; }
    }
</style>
"""


@app.route("/")
def index():
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        password = request.form.get("password", "")
        if password == ADMIN_PASSWORD:
            session["admin_logged_in"] = True
            session.permanent = True
            return redirect(url_for("admin"))
        flash("密码错误", "error")

    html = """
    <!DOCTYPE html>
    <html lang="zh-CN">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>管理员登录</title>
        """ + COMMON_CSS + """
    </head>
    <body>
        <div class="container">
            <div class="card" style="max-width:420px;margin:100px auto;">
                <h1 style="text-align:center;margin-bottom:30px;">管理员登录</h1>
                <form method="post">
                    <div class="form-group">
                        <label>管理密码</label>
                        <input type="password" name="password" placeholder="请输入管理密码" required>
                    </div>
                    <button type="submit" class="btn btn-primary" style="width:100%">登录</button>
                </form>
            </div>
        </div>
    </body>
    </html>
    """
    return render_template_string(html)


@app.route("/logout")
def logout():
    session.pop("admin_logged_in", None)
    return redirect(url_for("login"))


@app.route("/admin")
@admin_required
def admin():
    all_accounts = parse_accounts()
    total = len(all_accounts)
    links = get_links()
    valid_count = sum(1 for l in links.values() if l.get("status") == "有效")
    invalid_count = sum(1 for l in links.values() if l.get("status") == "已失效")

    html = """
    <!DOCTYPE html>
    <html lang="zh-CN">
    <head>
        <meta charset="UTF-8">
        <title>后台管理</title>
        """ + COMMON_CSS + """
    </head>
    <body>
        <div class="nav">
            <div class="nav-inner">
                <div>
                    <a href="{{ url_for('admin') }}">后台首页</a>
                    <a href="{{ url_for('total_query_page') }}">总查询</a>
                    <a href="{{ url_for('create_link_page') }}">生成子链接</a>
                    <a href="{{ url_for('links_list') }}">链接列表</a>
                    <a href="{{ url_for('backup_page') }}">备份管理</a>
                    <a href="{{ url_for('invalidate_by_id_page') }}">失效链接</a>
                </div>
                <div>
                    <a href="{{ url_for('logout') }}">退出</a>
                </div>
            </div>
        </div>
        <div class="container">
            {% with messages = get_flashed_messages(with_categories=true) %}
                {% if messages %}
                    {% for category, message in messages %}
                        <div class="alert alert-{{ category }}">{{ message }}</div>
                    {% endfor %}
                {% endif %}
            {% endwith %}

            <h1>后台管理</h1>
            <div class="stats">
                <div class="stat-card"><h3>""" + str(total) + """</h3><p>总邮箱数</p></div>
                <div class="stat-card"><h3>""" + str(valid_count) + """</h3><p>有效子链接</p></div>
                <div class="stat-card"><h3>""" + str(invalid_count) + """</h3><p>已失效链接</p></div>
            </div>
            <div class="grid-2">
                <div class="card">
                    <h2>快捷操作</h2>
                    <a href="{{ url_for('total_query_page') }}" class="btn btn-primary mb-2">总查询</a>
                    <a href="{{ url_for('create_link_page') }}" class="btn btn-primary mb-2" style="margin-left:8px">生成子链接</a>
                    <a href="{{ url_for('links_list') }}" class="btn btn-secondary mb-2" style="margin-left:8px">链接列表</a>
                </div>
            </div>
        </div>
    </body>
    </html>
    """
    return render_template_string(html)




# ============ 备份管理 ============

def get_backup_files():
    """获取所有备份文件列表"""
    backups = []
    if not os.path.exists(DATA_DIR):
        return backups
    for fname in sorted(os.listdir(DATA_DIR)):
        if fname.startswith("links_backup_") and fname.endswith(".json"):
            fpath = os.path.join(DATA_DIR, fname)
            try:
                stat = os.stat(fpath)
                size = stat.st_size
                mtime = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
                backups.append({
                    "name": fname,
                    "path": fpath,
                    "size": f"{size / 1024:.1f} KB" if size > 1024 else f"{size} B",
                    "time": mtime
                })
            except Exception:
                pass
    return sorted(backups, key=lambda x: x["time"], reverse=True)


def create_timestamp_backup():
    """创建带时间戳的备份"""
    if not os.path.exists(LINKS_FILE):
        return None
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_name = f"links_backup_{timestamp}.json"
    backup_path = os.path.join(DATA_DIR, backup_name)
    try:
        with open(LINKS_FILE, "r", encoding="utf-8") as src:
            data = src.read()
        with open(backup_path, "w", encoding="utf-8") as dst:
            dst.write(data)
        return backup_name
    except Exception as e:
        print(f"[Backup Error] {e}")
        return None


def restore_backup(backup_path):
    """从备份文件恢复"""
    try:
        with open(backup_path, "r", encoding="utf-8") as f:
            json.load(f)  # 验证JSON有效
        # 先创建当前状态的备份
        create_timestamp_backup()
        # 复制备份到主文件
        with open(backup_path, "r", encoding="utf-8") as src:
            data = src.read()
        with open(LINKS_FILE, "w", encoding="utf-8") as dst:
            dst.write(data)
        with open(BACKUP_FILE, "w", encoding="utf-8") as dst:
            dst.write(data)
        return True
    except Exception as e:
        print(f"[Restore Error] {e}")
        return False



def cleanup_old_backups(max_keep=30):
    """清理旧备份，只保留最近N个"""
    backups = []
    if not os.path.exists(DATA_DIR):
        return
    for fname in os.listdir(DATA_DIR):
        if fname.startswith("links_backup_") and fname.endswith(".json"):
            fpath = os.path.join(DATA_DIR, fname)
            try:
                mtime = os.path.getmtime(fpath)
                backups.append((mtime, fpath))
            except Exception:
                pass
    if len(backups) > max_keep:
        backups.sort(key=lambda x: x[0], reverse=True)
        for _, fpath in backups[max_keep:]:
            try:
                os.remove(fpath)
            except Exception:
                pass


# ============ 总查询系统（管理员自用） ============

@app.route("/admin/query", methods=["GET", "POST"])
@admin_required
def total_query_page():
    if request.method == "POST":
        email_addr = request.form.get("email", "").strip()
        limit = int(request.form.get("limit", 10))

        if not email_addr:
            return render_template_string(total_query_input_html("请输入邮箱号")), 400

        accounts = parse_accounts()
        auth_code = accounts.get(email_addr, "")
        if not auth_code:
            return render_template_string(total_query_input_html("该邮箱不在库存中或配置错误")), 404

        if limit < 1 or limit > 50:
            limit = 10

        emails_data = fetch_emails(email_addr, auth_code, limit)
        if not emails_data:
            return render_template_string(total_query_input_html("该邮箱暂无邮件或读取失败")), 404

        return render_template_string(total_query_result_html(email_addr, emails_data))

    return render_template_string(total_query_input_html())


def total_query_input_html(error_msg=""):
    error_html = f'<div style="background:#f8d7da;color:#721c24;padding:12px 16px;border-radius:8px;margin-bottom:16px;font-size:14px;">{error_msg}</div>' if error_msg else ""
    return f"""
    <!DOCTYPE html>
    <html lang="zh-CN">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>总查询 - 邮件查询系统</title>
        <style>
            * {{ box-sizing: border-box; margin: 0; padding: 0; }}
            body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif; background: #f0f2f5; color: #333; line-height: 1.6; }}
            .nav {{ background: #1a1a2e; padding: 14px 0; margin-bottom: 20px; }}
            .nav-inner {{ max-width: 1200px; margin: 0 auto; padding: 0 20px; display: flex; justify-content: space-between; align-items: center; }}
            .nav a {{ color: #fff; text-decoration: none; margin-right: 24px; font-size: 14px; transition: color 0.2s; }}
            .nav a:hover {{ color: #e94560; }}
            .container {{ max-width: 800px; margin: 0 auto; padding: 20px; }}
            .card {{ background: #fff; border-radius: 12px; box-shadow: 0 2px 8px rgba(0,0,0,0.08); padding: 28px; margin-bottom: 20px; }}
            h1 {{ font-size: 22px; margin-bottom: 8px; color: #1a1a2e; text-align: center; }}
            .subtitle {{ color: #888; font-size: 13px; text-align: center; margin-bottom: 24px; }}
            .form-group {{ margin-bottom: 20px; }}
            label {{ display: block; margin-bottom: 8px; font-weight: 500; color: #555; font-size: 14px; }}
            input {{ width: 100%; padding: 14px 16px; border: 1px solid #ddd; border-radius: 8px; font-size: 15px; transition: border-color 0.2s; }}
            input:focus {{ outline: none; border-color: #667eea; }}
            button {{ width: 100%; padding: 14px; border: none; border-radius: 8px; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: #fff; font-size: 15px; cursor: pointer; font-weight: 500; }}
            button:hover {{ opacity: 0.95; }}
            .footer {{ text-align: center; color: #aaa; font-size: 12px; margin-top: 30px; }}
        </style>
    </head>
    <body>
        <div class="nav">
            <div class="nav-inner">
                <div>
                    <a href="/admin">后台首页</a>
                    <a href="/admin/query">总查询</a>
                    <a href="/admin/create_link">生成子链接</a>
                    <a href="/admin/links">链接列表</a>
                    <a href="/admin/backup">备份管理</a>
                    <a href="/admin/invalidate_by_id">失效链接</a>
                </div>
                <div>
                    <a href="/logout">退出</a>
                </div>
            </div>
        </div>
        <div class="container">
            <div class="card">
                <h1>总查询</h1>
                <p class="subtitle">输入任意库存邮箱号，直接查询邮件（管理员专用）</p>
                {error_html}
                <form method="post" action="/admin/query">
                    <div class="form-group">
                        <label>邮箱号</label>
                        <input type="text" name="email" placeholder="例如: 123456@qq.com" required>
                    </div>
                    <div class="form-group">
                        <label>邮件数量（1-50）</label>
                        <input type="number" name="limit" min="1" max="50" value="10">
                    </div>
                    <button type="submit">查询邮件</button>
                </form>
            </div>
            <div class="footer">mailauto.zeabur.app</div>
        </div>
    </body>
    </html>
    """


def total_query_result_html(email_addr, emails_data):
    cards_html = ""
    for idx, mail in enumerate(emails_data):
        safe_body = sanitize_email_html(mail.get("body_html", ""))
        if not safe_body.strip():
            safe_body = f'<pre style="white-space:pre-wrap;word-wrap:break-word;">{mail.get("preview", "")}</pre>'
        folder_type = mail.get("folder_type", "inbox")
        folder_label = mail.get("folder_label", "收件箱")
        subject = mail.get("subject", "（无主题）")
        from_ = mail.get("from", "未知")
        date_str = mail.get("date_str", "")
        preview = mail.get("preview", "")
        cards_html += f"""
        <div class="email-card" onclick="toggleEmail({idx})">
            <div class="email-summary">
                <div class="email-row1">
                    <span class="email-from">{from_}</span>
                    <span class="email-date">{date_str}</span>
                </div>
                <div class="email-row2">
                    <span class="email-subject">{subject}</span>
                    <span class="folder-tag folder-{folder_type}">{folder_label}</span>
                </div>
                <div class="email-preview">{preview}</div>
            </div>
            <div class="email-full" id="email-full-{idx}">
                <div class="email-divider"></div>
                <div class="email-body-content">{safe_body}</div>
            </div>
        </div>
        """

    return f"""
    <!DOCTYPE html>
    <html lang="zh-CN">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>邮件列表 - {email_addr}</title>
        <style>
            * {{ box-sizing: border-box; margin: 0; padding: 0; }}
            body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif; background: #f0f2f5; color: #333; line-height: 1.6; }}
            .nav {{ background: #1a1a2e; padding: 14px 0; margin-bottom: 20px; }}
            .nav-inner {{ max-width: 1200px; margin: 0 auto; padding: 0 20px; display: flex; justify-content: space-between; align-items: center; }}
            .nav a {{ color: #fff; text-decoration: none; margin-right: 24px; font-size: 14px; transition: color 0.2s; }}
            .nav a:hover {{ color: #e94560; }}
            .container {{ max-width: 800px; margin: 0 auto; padding: 20px; }}
            .header-card {{ background: #fff; border-radius: 12px; box-shadow: 0 2px 8px rgba(0,0,0,0.08); padding: 20px 24px; margin-bottom: 16px; }}
            .header-top {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; flex-wrap: wrap; gap: 8px; }}
            .header-top h1 {{ font-size: 18px; color: #1a1a2e; margin: 0; }}
            .email-tag {{ display: inline-block; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: #fff; padding: 4px 14px; border-radius: 20px; font-size: 13px; }}
            .back-btn {{ display: inline-block; margin-bottom: 12px; color: #667eea; text-decoration: none; font-size: 14px; font-weight: 500; }}
            .back-btn:hover {{ text-decoration: underline; }}
            .email-list {{ margin-top: 8px; }}
            .email-card {{ background: #fff; border-radius: 10px; margin-bottom: 12px; box-shadow: 0 1px 4px rgba(0,0,0,0.08); cursor: pointer; transition: all 0.2s; overflow: hidden; }}
            .email-card:hover {{ box-shadow: 0 2px 12px rgba(0,0,0,0.12); }}
            .email-summary {{ padding: 16px 20px; }}
            .email-row1 {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px; }}
            .email-from {{ font-weight: 600; color: #1a1a2e; font-size: 15px; }}
            .email-date {{ color: #999; font-size: 12px; }}
            .email-row2 {{ display: flex; align-items: center; gap: 10px; margin-bottom: 8px; flex-wrap: wrap; }}
            .email-subject {{ color: #333; font-size: 14px; flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
            .folder-tag {{ font-size: 11px; padding: 2px 8px; border-radius: 4px; font-weight: 500; }}
            .folder-inbox {{ background: #e8f5e9; color: #2e7d32; }}
            .folder-junk {{ background: #ffebee; color: #c62828; }}
            .folder-ad {{ background: #fff8e1; color: #f57f17; }}
            .email-preview {{ color: #999; font-size: 13px; line-height: 1.5; overflow: hidden; text-overflow: ellipsis; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; }}
            .email-full {{ display: none; }}
            .email-full.active {{ display: block; }}
            .email-divider {{ height: 1px; background: #f0f0f0; margin: 0 20px; }}
            .email-body-content {{ padding: 20px; background: #fafafa; font-size: 14px; line-height: 1.8; }}
            .email-body-content img {{ max-width: 100%; height: auto; }}
            .email-body-content a {{ color: #667eea; }}
            .footer {{ text-align: center; color: #aaa; font-size: 12px; margin-top: 30px; padding-bottom: 20px; }}
        </style>
    </head>
    <body>
        <div class="nav">
            <div class="nav-inner">
                <div>
                    <a href="/admin">后台首页</a>
                    <a href="/admin/query">总查询</a>
                    <a href="/admin/create_link">生成子链接</a>
                    <a href="/admin/links">链接列表</a>
                    <a href="/admin/backup">备份管理</a>
                    <a href="/admin/invalidate_by_id">失效链接</a>
                </div>
                <div>
                    <a href="/logout">退出</a>
                </div>
            </div>
        </div>
        <div class="container">
            <a href="/admin/query" class="back-btn">&larr; 重新查询</a>
            <div class="header-card">
                <div class="header-top">
                    <h1>邮件列表</h1>
                    <span class="email-tag">{email_addr}</span>
                </div>
                <p style="color:#888;font-size:13px;">共 {len(emails_data)} 封邮件</p>
            </div>
            <div class="email-list">
                {cards_html}
            </div>
            <div class="footer">mailauto.zeabur.app</div>
        </div>
        <script>
            function toggleEmail(idx) {{
                var el = document.getElementById('email-full-' + idx);
                if (el) {{
                    el.classList.toggle('active');
                }}
            }}
        </script>
    </body>
    </html>
    """


# ============ 子链接生成与管理 ============

def gen_success_html(link_url, allowed_emails, max_emails, expire_at):
    emails_html = ""
    for e in allowed_emails:
        emails_html += "<div>邮箱：" + e + "</div>"
    html = """
    <!DOCTYPE html>
    <html lang="zh-CN">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>链接管理后台</title>
        <style>
            * { box-sizing: border-box; margin: 0; padding: 0; }
            body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif; background: #f5f5f5; color: #333; line-height: 1.8; padding: 40px 20px; }
            .container { max-width: 600px; margin: 0 auto; background: #fff; border-radius: 12px; padding: 32px; box-shadow: 0 2px 12px rgba(0,0,0,0.08); }
            .header { display: flex; align-items: center; margin-bottom: 24px; }
            .header-icon { width: 36px; height: 36px; background: #667eea; border-radius: 50%; display: flex; align-items: center; justify-content: center; color: #fff; font-size: 18px; margin-right: 12px; }
            .header h1 { font-size: 22px; color: #1a1a2e; font-weight: 600; }
            .success-msg { font-size: 16px; color: #28a745; margin-bottom: 16px; font-weight: 500; }
            .info-row { margin-bottom: 10px; font-size: 15px; color: #555; }
            .info-row a { color: #667eea; text-decoration: none; word-break: break-all; }
            .info-row a:hover { text-decoration: underline; }
            .back-btn { display: inline-block; margin-top: 20px; padding: 10px 24px; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: #fff; text-decoration: none; border-radius: 8px; font-size: 14px; }
            .back-btn:hover { opacity: 0.9; }
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <div class="header-icon">!</div>
                <h1>链接管理后台</h1>
            </div>
            <div class="success-msg">生成成功！</div>
            <div class="info-row">链接：<a href=""" + link_url + """ target="_blank">""" + link_url + """</a></div>
            """ + emails_html + """
            <div class="info-row">邮件数量：""" + str(max_emails) + """</div>
            <div class="info-row">有效期：""" + expire_at + """</div>
            <a href="/admin" class="back-btn">返回后台</a>
        </div>
    </body>
    </html>
    """
    return html


@app.route("/admin/create_link", methods=["GET", "POST"])
@admin_required
def create_link_page():
    if request.method == "POST":
        emails_text = request.form.get("emails", "")
        days = int(request.form.get("days", 30))
        max_emails = int(request.form.get("max_emails", 10))

        allowed_emails = [e.strip() for e in emails_text.split("\n") if e.strip() and "@" in e.strip()]
        if not allowed_emails:
            flash("请输入至少一个有效的邮箱地址", "error")
            return redirect(url_for("create_link_page"))

        all_accounts = parse_accounts()
        invalid_emails = [e for e in allowed_emails if e not in all_accounts]
        if invalid_emails:
            flash(f"以下邮箱不在库存中: {', '.join(invalid_emails)}", "error")
            return redirect(url_for("create_link_page"))

        if max_emails < 1 or max_emails > 50:
            flash("邮件数量限制必须在 1-50 之间", "error")
            return redirect(url_for("create_link_page"))

        link_id = create_sub_link(allowed_emails, days, max_emails)
        link_url = f"https://{DOMAIN}/s/{link_id}"
        expire_at = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        return render_template_string(gen_success_html(link_url, allowed_emails, max_emails, expire_at))

    html = """
    <!DOCTYPE html>
    <html lang="zh-CN">
    <head>
        <meta charset="UTF-8">
        <title>生成子链接</title>
        """ + COMMON_CSS + """
    </head>
    <body>
        <div class="nav">
            <div class="nav-inner">
                <div>
                    <a href="{{ url_for('admin') }}">后台首页</a>
                    <a href="{{ url_for('total_query_page') }}">总查询</a>
                    <a href="{{ url_for('create_link_page') }}">生成子链接</a>
                    <a href="{{ url_for('links_list') }}">链接列表</a>
                    <a href="{{ url_for('backup_page') }}">备份管理</a>
                    <a href="{{ url_for('invalidate_by_id_page') }}">失效链接</a>
                </div>
                <div>
                    <a href="{{ url_for('logout') }}">退出</a>
                </div>
            </div>
        </div>
        <div class="container">
            {% with messages = get_flashed_messages(with_categories=true) %}
                {% if messages %}
                    {% for category, message in messages %}
                        <div class="alert alert-{{ category }}">{{ message }}</div>
                    {% endfor %}
                {% endif %}
            {% endwith %}

            <h1>生成查询子链接</h1>
            <div class="card">
                <form method="post">
                    <div class="form-group">
                        <label>可查询邮箱（每行一个）</label>
                        <textarea name="emails" rows="8" placeholder="example1@qq.com&#10;example2@qq.com" required></textarea>
                    </div>
                    <div class="grid-2">
                        <div class="form-group">
                            <label>有效期（天）</label>
                            <input type="number" name="days" min="1" value="30" required>
                        </div>
                        <div class="form-group">
                            <label>邮件数量限制（1-50）</label>
                            <input type="number" name="max_emails" min="1" max="50" value="10" required>
                        </div>
                    </div>
                    <button type="submit" class="btn btn-primary">生成子链接</button>
                </form>
            </div>
        </div>
    </body>
    </html>
    """
    return render_template_string(html)


@app.route("/admin/links")
@admin_required
def links_list():
    links = get_links()
    links_sorted = sorted(links.values(), key=lambda x: x.get("created_at", ""), reverse=True)

    rows = ""
    for link in links_sorted:
        status_badge = '<span class="badge badge-success">有效</span>' if link.get("status") == "有效" else '<span class="badge badge-danger">已失效</span>'
        emails = link.get("allowed_emails", [])
        emails_display = "<br>".join(emails[:2])
        if len(emails) > 2:
            emails_display += f"<br><span class='text-muted'>等{len(emails)}个邮箱</span>"
        action_btn = """
            <form method="post" action=""" + '"' + url_for("invalidate_link_route") + '"' + """ style="display:inline" onsubmit="return confirm('确定要失效此链接吗？');">
                <input type="hidden" name="link_id" value=""" + link["id"] + """>
                <button type="submit" class="btn btn-danger btn-sm">失效</button>
            </form>
        """ if link.get("status") == "有效" else '<span class="text-muted">已失效</span>'

        rows += f"""
        <tr>
            <td><code>{link["id"][:18]}...</code></td>
            <td>{emails_display}</td>
            <td>{link.get("max_emails", 10)}</td>
            <td>{link.get("created_at", "")}</td>
            <td>{link.get("expire_at", "")}</td>
            <td>{status_badge}</td>
            <td>{action_btn}</td>
        </tr>
        """

    html = """
    <!DOCTYPE html>
    <html lang="zh-CN">
    <head>
        <meta charset="UTF-8">
        <title>链接列表</title>
        """ + COMMON_CSS + """
    </head>
    <body>
        <div class="nav">
            <div class="nav-inner">
                <div>
                    <a href="{{ url_for('admin') }}">后台首页</a>
                    <a href="{{ url_for('total_query_page') }}">总查询</a>
                    <a href="{{ url_for('create_link_page') }}">生成子链接</a>
                    <a href="{{ url_for('links_list') }}">链接列表</a>
                    <a href="{{ url_for('backup_page') }}">备份管理</a>
                    <a href="{{ url_for('invalidate_by_id_page') }}">失效链接</a>
                </div>
                <div>
                    <a href="{{ url_for('logout') }}">退出</a>
                </div>
            </div>
        </div>
        <div class="container">
            {% with messages = get_flashed_messages(with_categories=true) %}
                {% if messages %}
                    {% for category, message in messages %}
                        <div class="alert alert-{{ category }}">{{ message }}</div>
                    {% endfor %}
                {% endif %}
            {% endwith %}

            <h1>链接列表</h1>
            <div class="card">
                """ + ("""
                <div style="overflow-x:auto">
                <table>
                    <thead>
                        <tr>
                            <th>链接ID</th>
                            <th>可查询邮箱</th>
                            <th>邮件数量</th>
                            <th>创建时间</th>
                            <th>过期时间</th>
                            <th>状态</th>
                            <th>操作</th>
                        </tr>
                    </thead>
                    <tbody>""" + rows + """</tbody>
                </table>
                </div>
                """ if rows else '<div class="empty-state">暂无链接数据</div>') + """
            </div>
        </div>
    </body>
    </html>
    """
    return render_template_string(html)


@app.route("/admin/invalidate_link", methods=["POST"])
@admin_required
def invalidate_link_route():
    link_id = request.form.get("link_id", "")
    if invalidate_link(link_id):
        flash("链接已失效", "success")
    else:
        flash("链接不存在", "error")
    return redirect(url_for("links_list"))


@app.route("/admin/invalidate_by_id", methods=["GET", "POST"])
@admin_required
def invalidate_by_id_page():
    if request.method == "POST":
        link_id = request.form.get("link_id", "").strip()
        if invalidate_link(link_id):
            flash("链接已失效", "success")
        else:
            flash("链接不存在或已失效", "error")
        return redirect(url_for("invalidate_by_id_page"))

    html = """
    <!DOCTYPE html>
    <html lang="zh-CN">
    <head>
        <meta charset="UTF-8">
        <title>失效链接</title>
        """ + COMMON_CSS + """
    </head>
    <body>
        <div class="nav">
            <div class="nav-inner">
                <div>
                    <a href="{{ url_for('admin') }}">后台首页</a>
                    <a href="{{ url_for('total_query_page') }}">总查询</a>
                    <a href="{{ url_for('create_link_page') }}">生成子链接</a>
                    <a href="{{ url_for('links_list') }}">链接列表</a>
                    <a href="{{ url_for('backup_page') }}">备份管理</a>
                    <a href="{{ url_for('invalidate_by_id_page') }}">失效链接</a>
                </div>
                <div>
                    <a href="{{ url_for('logout') }}">退出</a>
                </div>
            </div>
        </div>
        <div class="container">
            {% with messages = get_flashed_messages(with_categories=true) %}
                {% if messages %}
                    {% for category, message in messages %}
                        <div class="alert alert-{{ category }}">{{ message }}</div>
                    {% endfor %}
                {% endif %}
            {% endwith %}

            <h1>输入ID失效链接</h1>
            <div class="card">
                <form method="post">
                    <div class="form-group">
                        <label>链接ID</label>
                        <input type="text" name="link_id" placeholder="输入完整的链接ID" required>
                    </div>
                    <button type="submit" class="btn btn-danger">确认失效</button>
                </form>
            </div>
        </div>
    </body>
    </html>
    """
    return render_template_string(html)




@app.route("/admin/backup")
@admin_required
def backup_page():
    backups = get_backup_files()
    rows = ""
    for b in backups:
        rows += f"""
        <tr>
            <td>{b['name']}</td>
            <td>{b['time']}</td>
            <td>{b['size']}</td>
            <td>
                <a href="/admin/backup/download/{b['name']}" class="btn btn-sm btn-primary" style="margin-right:6px">下载</a>
                <form method="post" action="/admin/backup/restore" style="display:inline">
                    <input type="hidden" name="backup_name" value="{b['name']}">
                    <button type="submit" class="btn btn-sm btn-secondary" onclick="return confirm('确定要恢复此备份吗？当前数据将被覆盖。')">恢复</button>
                </form>
            </td>
        </tr>
        """

    html = """
    <!DOCTYPE html>
    <html lang="zh-CN">
    <head>
        <meta charset="UTF-8">
        <title>备份管理</title>
        """ + COMMON_CSS + """
    </head>
    <body>
        <div class="nav">
            <div class="nav-inner">
                <div>
                    <a href="{{ url_for('admin') }}">后台首页</a>
                    <a href="{{ url_for('total_query_page') }}">总查询</a>
                    <a href="{{ url_for('create_link_page') }}">生成子链接</a>
                    <a href="{{ url_for('links_list') }}">链接列表</a>
                    <a href="{{ url_for('backup_page') }}">备份管理</a>
                    <a href="{{ url_for('invalidate_by_id_page') }}">失效链接</a>
                </div>
                <div>
                    <a href="{{ url_for('logout') }}">退出</a>
                </div>
            </div>
        </div>
        <div class="container">
            {% with messages = get_flashed_messages(with_categories=true) %}
                {% if messages %}
                    {% for category, message in messages %}
                        <div class="alert alert-{{ category }}">{{ message }}</div>
                    {% endfor %}
                {% endif %}
            {% endwith %}

            <h1>备份管理</h1>
            <div class="card">
                <h2>自动备份说明</h2>
                <p>每次生成/失效子链接时，系统会自动创建带时间戳的备份文件。</p>
                <p class="mt-2">主备份文件：<code>links_backup.json</code>（与主文件实时同步）</p>
                <p class="mt-2">历史备份：自动保留最近 <strong>30</strong> 个时间戳备份。</p>
            </div>
            <div class="card">
                <h2>历史备份列表</h2>
                """ + ("""
                <div style="overflow-x:auto">
                <table>
                    <thead>
                        <tr>
                            <th>文件名</th>
                            <th>备份时间</th>
                            <th>大小</th>
                            <th>操作</th>
                        </tr>
                    </thead>
                    <tbody>""" + rows + """</tbody>
                </table>
                </div>
                """ if rows else '<div class="empty-state">暂无历史备份</div>') + """
            </div>
        </div>
    </body>
    </html>
    """
    return render_template_string(html)


@app.route("/admin/backup/download/<filename>")
@admin_required
def download_backup(filename):
    from flask import send_file
    safe_name = os.path.basename(filename)
    if not safe_name.startswith("links_backup_") or not safe_name.endswith(".json"):
        return "非法文件名", 400
    fpath = os.path.join(DATA_DIR, safe_name)
    if not os.path.exists(fpath):
        return "文件不存在", 404
    return send_file(fpath, as_attachment=True, download_name=safe_name)


@app.route("/admin/backup/restore", methods=["POST"])
@admin_required
def restore_backup_route():
    backup_name = request.form.get("backup_name", "").strip()
    safe_name = os.path.basename(backup_name)
    if not safe_name.startswith("links_backup_") or not safe_name.endswith(".json"):
        flash("非法文件名", "error")
        return redirect(url_for("backup_page"))
    fpath = os.path.join(DATA_DIR, safe_name)
    if not os.path.exists(fpath):
        flash("备份文件不存在", "error")
        return redirect(url_for("backup_page"))
    if restore_backup(fpath):
        flash(f"已从 {safe_name} 恢复数据", "success")
    else:
        flash("恢复失败", "error")
    return redirect(url_for("backup_page"))


# ============ 子链接查询系统
# ============ 子链接查询系统（给用户用，无需登录） ============

@app.route("/s/<link_id>", methods=["GET", "POST"])
def sub_query(link_id):
    link_data = get_link(link_id)
    if not link_data:
        return "<h1>链接不存在</h1>", 404
    if not is_link_valid(link_data):
        return "<h1>链接已失效或已过期</h1>", 403

    allowed_emails = link_data.get("allowed_emails", [])
    max_emails = link_data.get("max_emails", 10)
    expire_at = link_data.get("expire_at", "")

    if request.method == "POST":
        email_addr = request.form.get("email", "").strip()

        if not email_addr:
            return render_template_string(sub_query_input_html(link_id, expire_at, "请输入邮箱号")), 400

        if email_addr not in allowed_emails:
            return render_template_string(sub_query_input_html(link_id, expire_at, "该邮箱不在此链接的查询范围内")), 403

        accounts = parse_accounts()
        auth_code = accounts.get(email_addr, "")
        if not auth_code:
            return render_template_string(sub_query_input_html(link_id, expire_at, "邮箱配置错误，无法读取")), 500

        emails_data = fetch_emails(email_addr, auth_code, max_emails)
        if not emails_data:
            return render_template_string(sub_query_input_html(link_id, expire_at, "该邮箱暂无邮件或读取失败")), 404

        return render_template_string(sub_query_result_html(link_id, email_addr, expire_at, emails_data))

    return render_template_string(sub_query_input_html(link_id, expire_at))


def sub_query_input_html(link_id, expire_at, error_msg=""):
    error_html = f'<div style="background:#f8d7da;color:#721c24;padding:12px 16px;border-radius:8px;margin-bottom:16px;font-size:14px;">{error_msg}</div>' if error_msg else ""
    return f"""
    <!DOCTYPE html>
    <html lang="zh-CN">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>邮箱查询</title>
        <style>
            * {{ box-sizing: border-box; margin: 0; padding: 0; }}
            body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif; background: #f0f2f5; color: #333; line-height: 1.6; }}
            .container {{ max-width: 500px; margin: 0 auto; padding: 20px; }}
            .card {{ background: #fff; border-radius: 12px; box-shadow: 0 2px 8px rgba(0,0,0,0.08); padding: 28px; margin-bottom: 20px; }}
            h1 {{ font-size: 22px; margin-bottom: 8px; color: #1a1a2e; text-align: center; }}
            .expire-info {{ color: #e74c3c; font-size: 13px; margin-bottom: 24px; text-align: center; }}
            .form-group {{ margin-bottom: 20px; }}
            label {{ display: block; margin-bottom: 8px; font-weight: 500; color: #555; font-size: 14px; }}
            input {{ width: 100%; padding: 14px 16px; border: 1px solid #ddd; border-radius: 8px; font-size: 15px; transition: border-color 0.2s; }}
            input:focus {{ outline: none; border-color: #667eea; }}
            button {{ width: 100%; padding: 14px; border: none; border-radius: 8px; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: #fff; font-size: 15px; cursor: pointer; font-weight: 500; }}
            button:hover {{ opacity: 0.95; }}
            .footer {{ text-align: center; color: #aaa; font-size: 12px; margin-top: 30px; }}
            .tips {{ background: #f8f9fa; padding: 14px; border-radius: 8px; font-size: 13px; color: #666; margin-top: 16px; line-height: 1.8; }}
            .tips strong {{ color: #333; }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="card">
                <h1>邮箱查询</h1>
                <p class="expire-info">有效期至: {expire_at} (北京时间)</p>
                {error_html}
                <form method="post" action="/s/{link_id}">
                    <div class="form-group">
                        <label>请输入要查询的邮箱号</label>
                        <input type="text" name="email" placeholder="例如: 123456@qq.com" required>
                    </div>
                    <button type="submit">查询邮件</button>
                </form>
                <div class="tips">
                    <strong>提示:</strong> 请输入您购买的完整邮箱地址，点击邮件卡片即可查看完整内容。
                </div>
            </div>
            <div class="footer">mailauto.zeabur.app</div>
        </div>
    </body>
    </html>
    """


def sub_query_result_html(link_id, email_addr, expire_at, emails_data):
    cards_html = ""
    for idx, mail in enumerate(emails_data):
        safe_body = sanitize_email_html(mail.get("body_html", ""))
        if not safe_body.strip():
            safe_body = f'<pre style="white-space:pre-wrap;word-wrap:break-word;">{mail.get("preview", "")}</pre>'
        folder_type = mail.get("folder_type", "inbox")
        folder_label = mail.get("folder_label", "收件箱")
        subject = mail.get("subject", "（无主题）")
        from_ = mail.get("from", "未知")
        date_str = mail.get("date_str", "")
        preview = mail.get("preview", "")
        cards_html += f"""
        <div class="email-card" onclick="toggleEmail({idx})">
            <div class="email-summary">
                <div class="email-row1">
                    <span class="email-from">{from_}</span>
                    <span class="email-date">{date_str}</span>
                </div>
                <div class="email-row2">
                    <span class="email-subject">{subject}</span>
                    <span class="folder-tag folder-{folder_type}">{folder_label}</span>
                </div>
                <div class="email-preview">{preview}</div>
            </div>
            <div class="email-full" id="email-full-{idx}">
                <div class="email-divider"></div>
                <div class="email-body-content">{safe_body}</div>
            </div>
        </div>
        """

    return f"""
    <!DOCTYPE html>
    <html lang="zh-CN">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>邮件列表 - {email_addr}</title>
        <style>
            * {{ box-sizing: border-box; margin: 0; padding: 0; }}
            body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif; background: #f0f2f5; color: #333; line-height: 1.6; }}
            .container {{ max-width: 800px; margin: 0 auto; padding: 20px; }}
            .header-card {{ background: #fff; border-radius: 12px; box-shadow: 0 2px 8px rgba(0,0,0,0.08); padding: 20px 24px; margin-bottom: 16px; }}
            .header-top {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; flex-wrap: wrap; gap: 8px; }}
            .header-top h1 {{ font-size: 18px; color: #1a1a2e; margin: 0; }}
            .email-tag {{ display: inline-block; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: #fff; padding: 4px 14px; border-radius: 20px; font-size: 13px; }}
            .expire-info {{ color: #e74c3c; font-size: 13px; }}
            .back-btn {{ display: inline-block; margin-bottom: 12px; color: #667eea; text-decoration: none; font-size: 14px; font-weight: 500; }}
            .back-btn:hover {{ text-decoration: underline; }}
            .email-list {{ margin-top: 8px; }}
            .email-card {{ background: #fff; border-radius: 10px; margin-bottom: 12px; box-shadow: 0 1px 4px rgba(0,0,0,0.08); cursor: pointer; transition: all 0.2s; overflow: hidden; }}
            .email-card:hover {{ box-shadow: 0 2px 12px rgba(0,0,0,0.12); }}
            .email-summary {{ padding: 16px 20px; }}
            .email-row1 {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px; }}
            .email-from {{ font-weight: 600; color: #1a1a2e; font-size: 15px; }}
            .email-date {{ color: #999; font-size: 12px; }}
            .email-row2 {{ display: flex; align-items: center; gap: 10px; margin-bottom: 8px; flex-wrap: wrap; }}
            .email-subject {{ color: #333; font-size: 14px; flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
            .folder-tag {{ font-size: 11px; padding: 2px 8px; border-radius: 4px; font-weight: 500; }}
            .folder-inbox {{ background: #e8f5e9; color: #2e7d32; }}
            .folder-junk {{ background: #ffebee; color: #c62828; }}
            .folder-ad {{ background: #fff8e1; color: #f57f17; }}
            .email-preview {{ color: #999; font-size: 13px; line-height: 1.5; overflow: hidden; text-overflow: ellipsis; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; }}
            .email-full {{ display: none; }}
            .email-full.active {{ display: block; }}
            .email-divider {{ height: 1px; background: #f0f0f0; margin: 0 20px; }}
            .email-body-content {{ padding: 20px; background: #fafafa; font-size: 14px; line-height: 1.8; }}
            .email-body-content img {{ max-width: 100%; height: auto; }}
            .email-body-content a {{ color: #667eea; }}
            .footer {{ text-align: center; color: #aaa; font-size: 12px; margin-top: 30px; padding-bottom: 20px; }}
        </style>
    </head>
    <body>
        <div class="container">
            <a href="/s/{link_id}" class="back-btn">&larr; 重新查询</a>
            <div class="header-card">
                <div class="header-top">
                    <h1>邮件列表</h1>
                    <span class="email-tag">{email_addr}</span>
                </div>
                <p class="expire-info">有效期至: {expire_at} (北京时间) | 共 {len(emails_data)} 封邮件</p>
            </div>
            <div class="email-list">
                {cards_html}
            </div>
            <div class="footer">mailauto.zeabur.app</div>
        </div>
        <script>
            function toggleEmail(idx) {{
                var el = document.getElementById('email-full-' + idx);
                if (el) {{
                    el.classList.toggle('active');
                }}
            }}
        </script>
    </body>
    </html>
    """


# ============ 启动 ============

if __name__ == "__main__":
    for f in [LINKS_FILE, BACKUP_FILE]:
        if not os.path.exists(f):
            save_json(f, {})
    app.run(host="0.0.0.0", port=PORT, debug=False)

for f in [LINKS_FILE, BACKUP_FILE]:
    if not os.path.exists(f):
        save_json(f, {})
