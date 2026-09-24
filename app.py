import os
import json
import re
import secrets
import imaplib
import email
import base64
import time
import random
import threading
from email.header import decode_header
from email.utils import parsedate_to_datetime
from datetime import datetime, timedelta
from functools import wraps

from flask import Flask, request, render_template_string, redirect, url_for, session, flash

os.environ["TZ"] = "Asia/Shanghai"

app = Flask(__name__)
app.secret_key = "mail-auto-secret-key-2026-v1"
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=2)

TEMP_ATTACHMENT_CACHE = {}

ADMIN_PASSWORD = "060910"
DOMAIN = "mail-auto.zeabur.app"
PORT = int(os.environ.get("PORT", 8080))

DATA_DIR = "/data"
os.makedirs(DATA_DIR, exist_ok=True)

ACCOUNTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "accounts.txt")
LINKS_FILE = os.path.join(DATA_DIR, "links.json")
BACKUP_FILE = os.path.join(DATA_DIR, "links_backup.json")

_ACCOUNTS_CACHE = None


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
    if filepath == LINKS_FILE:
        old_count = 0
        try:
            if os.path.exists(filepath):
                with open(filepath, 'r', encoding='utf-8') as f:
                    old_count = len(json.load(f))
        except:
            pass
        new_count = len(data)
        if old_count > 100 and new_count < old_count * 0.5:
            print(f"⚠️ 警告：数据异常缩水！旧:{old_count} 新:{new_count}，拒绝覆盖！")
            return

    backup_file = filepath + '.bak'
    try:
        if os.path.exists(filepath):
            import shutil
            shutil.copy2(filepath, backup_file)
    except:
        pass

    try:
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"❌ 保存 {filepath} 失败: {e}")
        if os.path.exists(backup_file):
            try:
                import shutil
                shutil.copy2(backup_file, filepath)
            except:
                pass


def save_links(data):
    save_json(LINKS_FILE, data)
    save_json(BACKUP_FILE, data)
    create_timestamp_backup()
    cleanup_old_backups()


def parse_accounts():
    global _ACCOUNTS_CACHE
    if _ACCOUNTS_CACHE is not None:
        return _ACCOUNTS_CACHE

    accounts = {}
    print(f"[DEBUG] 开始加载账号: {ACCOUNTS_FILE}, 存在: {os.path.exists(ACCOUNTS_FILE)}")
    if not os.path.exists(ACCOUNTS_FILE):
        print(f"[DEBUG] 文件不存在: {ACCOUNTS_FILE}")
        _ACCOUNTS_CACHE = accounts
        return accounts

    try:
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
    except Exception as e:
        print(f"[ERROR] 读取账号失败: {e}")

    print(f"[DEBUG] 账号加载完成，共 {len(accounts)} 个邮箱")
    _ACCOUNTS_CACHE = accounts
    return accounts


def get_links():
    return load_json(LINKS_FILE, {})


def generate_link_id():
    return secrets.token_urlsafe(16)


def create_sub_link(allowed_emails, days, max_emails=1):
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
    if not date_str:
        return ""
    try:
        dt = parsedate_to_datetime(date_str)
    except Exception:
        try:
            dt = datetime.strptime(date_str.strip(), "%a, %d %b %Y %H:%M:%S %z")
        except Exception:
            return ""
    try:
        if dt.tzinfo is not None:
            from datetime import timezone
            utc_dt = dt.astimezone(timezone.utc)
            dt = utc_dt.replace(tzinfo=None) + timedelta(hours=8)
        else:
            dt = dt + timedelta(hours=8)
    except Exception:
        if dt.tzinfo is not None:
            dt = dt.replace(tzinfo=None)
    if dt.tzinfo is not None:
        dt = dt.replace(tzinfo=None)
    now = datetime.now()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday = today - timedelta(days=1)
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


def format_file_size(size):
    if size < 1024:
        return f"{size} B"
    elif size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    else:
        return f"{size / (1024 * 1024):.2f} MB"


# ==================== 【关键】按邮件ID顺序抓取，最新在最前 ====================
def fetch_emails(email_addr, auth_code, limit=10):
    if email_addr not in _ACCOUNTS_CACHE:
        return []
    mail = None
    try:
        mail = imaplib.IMAP4_SSL("imap.qq.com", 993, timeout=15)
        mail.login(email_addr, auth_code)

        all_mails = []
        folders_to_check = ["INBOX", "Junk", "垃圾箱", "Junk Email", "[Gmail]/Spam"]

        for folder in folders_to_check:
            try:
                status, _ = mail.select(folder)
                if status != 'OK':
                    continue
                status, data = mail.search(None, "ALL")
                if not data[0]:
                    continue
                ids = data[0].split()
                latest_ids = ids[-limit:] if len(ids) > limit else ids
                for mid in latest_ids:
                    try:
                        mail_id_str = mid.decode() if isinstance(mid, bytes) else str(mid)
                        _, msg_data = mail.fetch(mid, "(RFC822)")
                        for part in msg_data:
                            if isinstance(part, tuple):
                                msg = email.message_from_bytes(part[1])
                                date_str = msg.get("Date", "")
                                send_time = ""
                                try:
                                    if date_str:
                                        dt = parsedate_to_datetime(date_str)
                                        send_time = dt.strftime("%Y-%m-%d %H:%M:%S")
                                except Exception:
                                    send_time = ""
                                subject = decode_str(msg.get("Subject", "无主题"))
                                sender = decode_str(msg.get("From", "未知发件人"))
                                content = get_mail_content(msg)
                                all_mails.append({
                                    'mail_id': mail_id_str,
                                    'sender': sender,
                                    'subject': subject,
                                    'content': content,
                                    'time': send_time,
                                    'folder': folder
                                })
                                break
                    except Exception as e:
                        print(f"读取邮件失败: {e}")
                        continue
            except Exception as e:
                print(f"读取文件夹 {folder} 失败: {e}")
                continue

        all_mails.reverse()
        return all_mails[:limit]
    except Exception as e:
        return {'error': f'连接失败：{str(e)}'}
    finally:
        if mail:
            try:
                mail.close()
            except:
                pass
            try:
                mail.logout()
            except:
                pass
# ===========================================================================


def get_mail_content(msg):
    content = ""
    try:
        all_parts = []
        if msg.is_multipart():
            for part in msg.walk():
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or 'utf-8'
                    try:
                        text = payload.decode(charset, errors='replace')
                    except:
                        text = payload.decode('utf-8', errors='replace')
                    if text.strip():
                        all_parts.append((part.get_content_type(), text))
        else:
            payload = msg.get_payload(decode=True)
            if payload:
                charset = msg.get_content_charset() or 'utf-8'
                try:
                    text = payload.decode(charset, errors='replace')
                except:
                    text = payload.decode('utf-8', errors='replace')
                if text.strip():
                    all_parts.append((msg.get_content_type(), text))
        for content_type, text in all_parts:
            if content_type == "text/plain":
                content = text.strip()
                break
        if not content:
            for content_type, text in all_parts:
                if content_type == "text/html":
                    content = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL)
                    content = re.sub(r'<[^>]+>', ' ', content)
                    content = html.unescape(content)
                    content = re.sub(r'\s+', ' ', content)
                    content = content.strip()
                    break
        if not content:
            return "无法解析邮件内容"
        code = None
        match = re.search(r'(\d)\s*(\d)\s*(\d)\s*(\d)\s*(\d)\s*(\d)', content)
        if match:
            code = match.group(1)+match.group(2)+match.group(3)+match.group(4)+match.group(5)+match.group(6)
        if not code:
            match = re.search(r'\b(\d{6})\b', content)
            if match:
                code = match.group(1)
        content = content[:1000]
        if code:
            return f"验证码：{code}\n\n{content}"
        return content
    except Exception:
        return "解析失败"


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
    .btn-sm { padding: 6px 14px; font-size: 13px; }
    input, select, textarea { width: 100%; padding: 12px 14px; border: 1px solid #ddd; border-radius: 8px; font-size: 14px; margin-bottom: 12px; }
    label { display: block; margin-bottom: 6px; font-weight: 500; color: #555; font-size: 14px; }
    .form-group { margin-bottom: 16px; }
    .grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }
    table { width: 100%; border-collapse: collapse; margin-top: 12px; font-size: 14px; }
    th, td { padding: 12px; text-align: left; border-bottom: 1px solid #eee; }
    th { background: #f8f9fa; font-weight: 600; color: #555; font-size: 13px; }
    .badge { display: inline-block; padding: 4px 12px; border-radius: 20px; font-size: 12px; }
    .badge-success { background: #d4edda; color: #155724; }
    .badge-danger { background: #f8d7da; color: #721c24; }
    .stats { display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; margin-bottom: 24px; }
    .stat-card { background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: #fff; padding: 24px; border-radius: 12px; text-align: center; }
    .stat-card h3 { font-size: 36px; margin-bottom: 6px; }
    .stat-card p { font-size: 14px; opacity: 0.9; }
    .nav { background: #1a1a2e; padding: 14px 0; margin-bottom: 20px; }
    .nav-inner { max-width: 1200px; margin: 0 auto; padding: 0 20px; display: flex; justify-content: space-between; align-items: center; }
    .nav a { color: #fff; text-decoration: none; margin-right: 24px; font-size: 14px; }
    .alert { padding: 14px 18px; border-radius: 8px; margin-bottom: 20px; font-size: 14px; }
    .alert-success { background: #d4edda; color: #155724; }
    .alert-error { background: #f8d7da; color: #721c24; }
    .alert-info { background: #d1ecf1; color: #0c5460; }
    .mt-2 { margin-top: 12px; }
    .empty-state { text-align: center; padding: 40px; color: #999; }
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
    <head><meta charset="UTF-8"><title>管理员登录</title>""" + COMMON_CSS + """</head>
    <body>
        <div class="container">
            <div class="card" style="max-width:420px;margin:100px auto;">
                <h1 style="text-align:center;">管理员登录</h1>
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
    now = datetime.now()
    expired_count = 0
    for link_id, link_data in links.items():
        try:
            expire_time = datetime.strptime(link_data['expire_at'], "%Y-%m-%d %H:%M:%S")
            if now > expire_time and link_data.get('status') == '有效':
                expired_count += 1
        except:
            pass

    html = """
    <!DOCTYPE html>
    <html lang="zh-CN">
    <head><meta charset="UTF-8"><title>后台管理</title>""" + COMMON_CSS + """</head>
    <body>
        <div class="nav">
            <div class="nav-inner">
                <div>
                    <a href="/admin">后台首页</a>
                    <a href="/admin/links">链接列表</a>
                </div>
                <div><a href="/logout">退出</a></div>
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
            <div class="card">
                <h2>快捷操作</h2>
                <a href="/admin/create_link" class="btn btn-primary" style="margin-right:8px">生成子链接</a>
                <a href="/admin/links" class="btn btn-secondary" style="margin-right:8px">链接列表</a>
                <a href="/admin/clean_expired" class="btn btn-danger" onclick="return confirm('确定清理所有过期链接吗？此操作不可恢复！');">清理过期链接（""" + str(expired_count) + """ 条）</a>
            </div>
        </div>
    </body>
    </html>
    """
    return render_template_string(html)


@app.route("/admin/clean_expired")
@admin_required
def clean_expired_links():
    links = get_links()
    if not links:
        flash("当前没有链接数据", "info")
        return redirect(url_for("admin"))
    now = datetime.now()
    cleaned_count = 0
    cleaned_links = {}
    for link_id, link_data in links.items():
        expire_at = link_data.get("expire_at")
        is_expired = False
        if expire_at:
            try:
                expire_dt = datetime.strptime(expire_at, "%Y-%m-%d %H:%M:%S")
                if now > expire_dt:
                    is_expired = True
            except Exception:
                pass
        if is_expired:
            cleaned_count += 1
            continue
        cleaned_links[link_id] = link_data
    if cleaned_count == 0:
        flash("没有发现已过期的链接", "info")
    else:
        save_links(cleaned_links)
        flash(f"已清理 {cleaned_count} 条过期链接，剩余 {len(cleaned_links)} 条", "success")
    return redirect(url_for("admin"))


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
            max_emails = 10
        link_id = create_sub_link(allowed_emails, days, max_emails)
        link_url = f"https://{DOMAIN}/s/{link_id}"
        expire_at = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        flash(f"生成成功！链接：{link_url}（有效期至 {expire_at}）", "success")
        return redirect(url_for("admin"))
    html = """
    <!DOCTYPE html>
    <html lang="zh-CN">
    <head><meta charset="UTF-8"><title>生成子链接</title>""" + COMMON_CSS + """</head>
    <body>
        <div class="nav"><div class="nav-inner"><div><a href="/admin">后台首页</a></div><div><a href="/logout">退出</a></div></div></div>
        <div class="container">
            <h1>生成查询子链接</h1>
            <div class="card">
                <form method="post">
                    <div class="form-group">
                        <label>可查询邮箱（每行一个）</label>
                        <textarea name="emails" rows="8" placeholder="example1@qq.com" required></textarea>
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
            emails_display += f"<br>等{len(emails)}个邮箱"
        action_btn = f'<form method="post" action="/admin/invalidate_link" style="display:inline" onsubmit="return confirm(\'确定要失效此链接吗？\');"><input type="hidden" name="link_id" value="{link["id"]}"><button type="submit" class="btn btn-danger btn-sm">失效</button></form>' if link.get("status") == "有效" else '<span style="color:#888;">已失效</span>'
        rows += f"""
        <tr>
            <td>{link["id"][:18]}...</td>
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
    <head><meta charset="UTF-8"><title>链接列表</title>""" + COMMON_CSS + """</head>
    <body>
        <div class="nav"><div class="nav-inner"><div><a href="/admin">后台首页</a></div><div><a href="/logout">退出</a></div></div></div>
        <div class="container">
            {% with messages = get_flashed_messages(with_categories=true) %}
                {% if messages %}
                    {% for category, message in messages %}
                        <div class="alert alert-{{ category }}">{{ message }}</div>
                    {% endfor %}
                {% endif %}
            {% endwith %}
            <h1>链接列表（共 """ + str(len(links_sorted)) + """ 条）</h1>
            <div class="card">
                """ + ("""
                <div style="overflow-x:auto">
                <table>
                    <thead><tr><th>链接ID</th><th>可查询邮箱</th><th>邮件数量</th><th>创建时间</th><th>过期时间</th><th>状态</th><th>操作</th></tr></thead>
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


# ============ 子链接查询系统（给用户用） ============

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
    <head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>邮箱查询</title>
    <style>
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif; background: #f0f2f5; color: #333; line-height: 1.6; }}
        .container {{ max-width: 500px; margin: 0 auto; padding: 20px; }}
        .card {{ background: #fff; border-radius: 12px; box-shadow: 0 2px 8px rgba(0,0,0,0.08); padding: 28px; margin-bottom: 20px; }}
        h1 {{ font-size: 22px; margin-bottom: 8px; color: #1a1a2e; text-align: center; }}
        .expire-info {{ color: #e74c3c; font-size: 13px; margin-bottom: 24px; text-align: center; }}
        .form-group {{ margin-bottom: 20px; }}
        label {{ display: block; margin-bottom: 8px; font-weight: 500; color: #555; font-size: 14px; }}
        input {{ width: 100%; padding: 14px 16px; border: 1px solid #ddd; border-radius: 8px; font-size: 15px; }}
        button {{ width: 100%; padding: 14px; border: none; border-radius: 8px; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: #fff; font-size: 15px; cursor: pointer; }}
    </style>
    </head>
    <body>
        <div class="container">
            <div class="card">
                <h1>邮箱查询</h1>
                <p class="expire-info">有效期至: {expire_at}</p>
                {error_html}
                <form method="post" action="/s/{link_id}">
                    <div class="form-group">
                        <label>请输入要查询的邮箱号</label>
                        <input type="text" name="email" placeholder="例如: 123456@qq.com" required>
                    </div>
                    <button type="submit">查询邮件</button>
                </form>
            </div>
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
        cards_html += f"""
        <div class="email-card" style="background:#fff;border-radius:10px;margin-bottom:12px;padding:16px 20px;box-shadow:0 1px 4px rgba(0,0,0,0.08);">
            <div style="display:flex;justify-content:space-between;margin-bottom:6px;">
                <span style="font-weight:600;color:#1a1a2e;">{mail.get('from', '未知')}</span>
                <span style="color:#999;font-size:12px;">{mail.get('time', '')}</span>
            </div>
            <div style="margin-bottom:8px;">
                <span style="color:#333;">{mail.get('subject', '无主题')}</span>
            </div>
            <div style="color:#555;font-size:13px;">{mail.get('content', '')[:500]}</div>
        </div>
        """
    return f"""
    <!DOCTYPE html>
    <html lang="zh-CN">
    <head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>邮件列表</title>
    <style>
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif; background: #f0f2f5; color: #333; line-height: 1.6; }}
        .container {{ max-width: 800px; margin: 0 auto; padding: 20px; }}
    </style>
    </head>
    <body>
        <div class="container">
            <a href="/s/{link_id}" style="display:inline-block;margin-bottom:12px;color:#667eea;text-decoration:none;">&larr; 重新查询</a>
            <h1 style="font-size:18px;margin-bottom:16px;">邮件列表 - {email_addr}</h1>
            <p style="color:#888;font-size:13px;margin-bottom:12px;">有效期至: {expire_at} | 共 {len(emails_data)} 封邮件</p>
            {cards_html}
        </div>
    </body>
    </html>
    """


# ============ 闲鱼自动发货接口 ============
@app.route("/api/auto_create_link", methods=["POST"])
def auto_create_link():
    data = request.get_json() or {}
    type_name = data.get("type", "英文")
    try:
        quantity = int(data.get("quantity", 1))
        days = int(data.get("days", 30))
    except (TypeError, ValueError):
        return "quantity 和 days 必须为整数"
    buyer_id = str(data.get("buyer_id") or data.get("remark") or secrets.token_urlsafe(8))
    if quantity <= 0:
        return "数量必须大于0"
    if type_name == "QQ英文邮箱":
        type_name = "英文"
    elif type_name == "QQ数字邮箱":
        type_name = "数字"
    all_accounts = parse_accounts()

    def detect_type(email):
        if email.endswith("@foxmail.com"):
            return "foxmail"
        username = email.split("@")[0]
        return "数字" if username.isdigit() else "英文"

    type_emails = [e for e in all_accounts.keys() if detect_type(e) == type_name]
    if not type_emails:
        return f"类型 '{type_name}' 没有可用邮箱"
    if len(type_emails) < quantity:
        return f"库存不足，需要 {quantity} 个，实际只有 {len(type_emails)} 个"
    selected_emails = random.sample(type_emails, quantity)
    link_id = create_sub_link(selected_emails, days, max_emails=1)
    link_url = f"https://{DOMAIN}/s/{link_id}"
    expire_at = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    return f"""您购买的邮箱已发货
邮箱：
{chr(10).join(selected_emails)}
查询链接：{link_url}
有效期至：{expire_at}"""


# ============ 自动清理（每天一次，防缩水） ============
def auto_clean_expired_worker():
    while True:
        time.sleep(86400)
        try:
            links = get_links()
            if not links:
                continue
            now = datetime.now()
            cleaned_count = 0
            cleaned_links = {}
            for link_id, link_data in links.items():
                expire_at = link_data.get("expire_at")
                is_expired = False
                if expire_at:
                    try:
                        expire_dt = datetime.strptime(expire_at, "%Y-%m-%d %H:%M:%S")
                        if now > expire_dt:
                            is_expired = True
                    except Exception:
                        pass
                if is_expired:
                    cleaned_count += 1
                    continue
                cleaned_links[link_id] = link_data
            if cleaned_count > 0:
                if len(cleaned_links) < len(links) * 0.5:
                    print(f"⚠️ 自动清理异常：缩水超过一半，拒绝执行")
                    continue
                save_links(cleaned_links)
                print(f"✅ 自动清理完成：删除 {cleaned_count} 条，剩余 {len(cleaned_links)} 条")
        except Exception as e:
            print(f"❌ 自动清理出错: {e}")

auto_clean_thread = threading.Thread(target=auto_clean_expired_worker, daemon=True)
auto_clean_thread.start()


def create_timestamp_backup():
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


def cleanup_old_backups(max_keep=30):
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


# ============ 启动 ============
if __name__ == "__main__":
    for f in [LINKS_FILE, BACKUP_FILE]:
        if not os.path.exists(f):
            save_json(f, {})
    app.run(host="0.0.0.0", port=PORT, debug=False)

for f in [LINKS_FILE, BACKUP_FILE]:
    if not os.path.exists(f):
        save_json(f, {})
