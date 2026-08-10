import os
import json
import re
import secrets
import imaplib
import email
import random
from email.header import decode_header
from datetime import datetime, timedelta
from functools import wraps

from flask import Flask, request, render_template_string, redirect, url_for, session, flash

# 设置时区为北京时间
os.environ["TZ"] = "Asia/Shanghai"

app = Flask(__name__)
app.secret_key = "mail-auto-secret-key-2026-v1"
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=2)

# ============ 配置 ============
ADMIN_PASSWORD = "060910"
DOMAIN = "mailauto.zeabur.app"
PORT = 8080

# 数据目录（Zeabur Volume 挂载 /app）
DATA_DIR = "/app" if os.path.exists("/app") else os.path.dirname(os.path.abspath(__file__))
ACCOUNTS_FILE = os.path.join(DATA_DIR, "accounts.txt")
LINKS_FILE = os.path.join(DATA_DIR, "links.json")
BACKUP_FILE = os.path.join(DATA_DIR, "links_backup.json")
USED_EMAILS_FILE = os.path.join(DATA_DIR, "used_emails.json")

# ============ 工具函数 ============

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
    """保存链接数据并自动备份"""
    save_json(LINKS_FILE, data)
    save_json(BACKUP_FILE, data)

# ============ 邮箱账号管理 ============

def parse_accounts():
    """
    解析 accounts.txt
    格式一: 邮箱1 邮箱2 邮箱3 授权码
    格式二: 邮箱----授权码
    返回: {邮箱: 授权码}
    """
    accounts = {}
    if not os.path.exists(ACCOUNTS_FILE):
        return accounts
    with open(ACCOUNTS_FILE, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            # 格式二: 邮箱----授权码
            if "----" in line:
                parts = line.split("----")
                if len(parts) == 2:
                    email_addr = parts[0].strip()
                    auth_code = parts[1].strip()
                    if email_addr and auth_code:
                        accounts[email_addr] = auth_code
            else:
                # 格式一: 邮箱1 邮箱2 ... 授权码
                parts = line.split()
                if len(parts) >= 2:
                    auth_code = parts[-1].strip()
                    emails = [p.strip() for p in parts[:-1] if p.strip()]
                    for email_addr in emails:
                        if email_addr and "@" in email_addr:
                            accounts[email_addr] = auth_code
    return accounts

def get_email_type(email_addr):
    """识别邮箱类型: 数字/英文/foxmail"""
    if "@foxmail.com" in email_addr.lower():
        return "foxmail"
    local_part = email_addr.split("@")[0] if "@" in email_addr else email_addr
    if local_part.isdigit():
        return "数字"
    return "英文"

def get_emails_by_type(email_type):
    """按类型获取所有邮箱"""
    accounts = parse_accounts()
    result = {}
    for email_addr, auth_code in accounts.items():
        if get_email_type(email_addr) == email_type:
            result[email_addr] = auth_code
    return result

def get_assigned_emails():
    """获取已分配的邮箱（所有有效链接中的邮箱，去重）"""
    links = get_links()
    assigned = set()
    for link_data in links.values():
        if link_data.get("status") == "有效":
            for e in link_data.get("emails", []):
                assigned.add(e)
    return assigned

def get_available_emails_by_type(email_type):
    """获取某类型下可用的邮箱（未被分配到有效链接中）"""
    all_emails = get_emails_by_type(email_type)
    assigned = get_assigned_emails()
    return {k: v for k, v in all_emails.items() if k not in assigned}

# ============ 链接管理 ============

def get_links():
    return load_json(LINKS_FILE, {})

def get_used_emails():
    return load_json(USED_EMAILS_FILE, {})

def generate_link_id():
    return secrets.token_urlsafe(16)

def create_link(emails, days, buyer_id=None):
    """
    创建查询链接
    emails: 邮箱列表
    days: 有效期天数
    buyer_id: 买家ID（可选）
    """
    links = get_links()
    used = get_used_emails()
    link_id = generate_link_id()
    now = datetime.now()
    expire = now + timedelta(days=int(days))

    link_data = {
        "id": link_id,
        "emails": emails,
        "buyer_id": buyer_id,
        "created_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "expire_at": expire.strftime("%Y-%m-%d %H:%M:%S"),
        "status": "有效"
    }
    links[link_id] = link_data
    save_links(links)

    # 记录已分配邮箱（按 buyer_id）
    if buyer_id:
        if buyer_id not in used:
            used[buyer_id] = []
        for e in emails:
            if e not in used[buyer_id]:
                used[buyer_id].append(e)
        save_json(USED_EMAILS_FILE, used)

    return link_id

def invalidate_link(link_id):
    """使链接失效"""
    links = get_links()
    if link_id in links:
        links[link_id]["status"] = "已失效"
        save_links(links)
        return True
    return False

def get_link(link_id):
    """获取链接信息"""
    links = get_links()
    return links.get(link_id)

def is_link_valid(link_data):
    """检查链接是否有效（未失效且未过期）"""
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

# ============ IMAP 邮件读取 ============

def decode_str(s):
    """解码邮件头字段"""
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
    """提取邮件正文（优先HTML，其次纯文本）"""
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
    """从IMAP服务器获取广告邮件相关文件夹"""
    ad_folders = []
    try:
        _, folders = mail.list()
        for folder in folders:
            folder_str = folder.decode("utf-8", errors="ignore")
            # 匹配广告相关关键词
            if any(kw in folder_str for kw in ["广告", "Advertise", "Subscribe", "订阅", "Promo"]):
                # 提取引号内的文件夹名
                match = re.search(r'"([^"]+)"$', folder_str)
                if match:
                    fname = match.group(1)
                    if fname not in ad_folders:
                        ad_folders.append(fname)
    except Exception:
        pass
    return ad_folders

def fetch_latest_email(email_addr, auth_code):
    """
    从收件箱、垃圾箱、广告邮件获取最新1封邮件
    返回: dict 或 None
    """
    # 基础文件夹
    folders = ["INBOX", "Junk"]

    # 尝试获取广告邮件文件夹
    try:
        mail = imaplib.IMAP4_SSL("imap.qq.com", 993, timeout=15)
        mail.login(email_addr, auth_code)
        ad_folders = get_ad_folders(mail)
        for fname in ad_folders:
            if fname not in folders:
                folders.append(fname)
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

            # 获取最新一封（UID最大的）
            latest_id = mail_ids[-1]
            status, msg_data = mail.fetch(latest_id, "(RFC822)")
            if status != "OK":
                mail.logout()
                continue

            msg = email.message_from_bytes(msg_data[0][1])
            subject = decode_str(msg.get("Subject", "（无主题）"))
            from_ = decode_str(msg.get("From", "未知"))
            date_ = msg.get("Date", "")
            body = get_email_body(msg)

            # 生成预览文本（去除HTML标签）
            preview_text = re.sub(r"<[^>]+>", " ", body)
            preview_text = re.sub(r"\s+", " ", preview_text).strip()
            preview = preview_text[:200] + ("..." if len(preview_text) > 200 else "")

            all_emails.append({
                "folder": folder,
                "subject": subject,
                "from": from_,
                "date": date_,
                "body_html": body,
                "preview": preview,
            })

            mail.logout()
        except Exception as e:
            print(f"[IMAP Error] {email_addr} / {folder}: {e}")
            try:
                mail.logout()
            except Exception:
                pass

    if not all_emails:
        return None

    # 返回找到的最新邮件（IMAP中通常最后一个ID最新）
    return all_emails[0]

# ============ 登录装饰器 ============

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get("admin_logged_in"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated_function

# ============ 公共样式 ============
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
    .link-url { background: #f0f2f5; padding: 8px 12px; border-radius: 6px; font-family: monospace; font-size: 13px; word-break: break-all; display: block; margin-top: 8px; }
    .mt-2 { margin-top: 12px; }
    .mt-3 { margin-top: 18px; }
    .mb-2 { margin-bottom: 12px; }
    .text-muted { color: #888; font-size: 13px; }
    .empty-state { text-align: center; padding: 40px; color: #999; }
    .tab { display: flex; border-bottom: 2px solid #eee; margin-bottom: 24px; }
    .tab-item { padding: 12px 24px; cursor: pointer; border-bottom: 2px solid transparent; margin-bottom: -2px; font-size: 14px; color: #666; background: none; }
    .tab-item.active { border-bottom-color: #667eea; color: #667eea; font-weight: 600; }
    @media (max-width: 768px) {
        .grid-2, .grid-3, .stats { grid-template-columns: 1fr; }
        .nav-inner { flex-wrap: wrap; }
    }
</style>
"""

# ============ 路由 ============

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
                <h1 style="text-align:center;margin-bottom:30px;">🔐 管理员登录</h1>
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
    # 库存统计
    all_accounts = parse_accounts()
    total = len(all_accounts)
    assigned = len(get_assigned_emails())
    available = total - assigned

    # 链接统计
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
                    <a href="{{ url_for('admin') }}">🏠 后台首页</a>
                    <a href="{{ url_for('create_link_page') }}">➕ 生成链接</a>
                    <a href="{{ url_for('links_list') }}">📋 链接列表</a>
                    <a href="{{ url_for('invalidate_by_id_page') }}">❌ 失效链接</a>
                </div>
                <div>
                    <a href="{{ url_for('logout') }}">🚪 退出</a>
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

            <h1>📊 后台管理</h1>
            <div class="stats">
                <div class="stat-card"><h3>""" + str(total) + """</h3><p>总邮箱数</p></div>
                <div class="stat-card"><h3>""" + str(assigned) + """</h3><p>已分配</p></div>
                <div class="stat-card"><h3>""" + str(available) + """</h3><p>可用数量</p></div>
            </div>
            <div class="grid-2">
                <div class="card">
                    <h2>📈 快速统计</h2>
                    <p>有效链接: <strong>""" + str(valid_count) + """</strong></p>
                    <p class="mt-2">已失效链接: <strong>""" + str(invalid_count) + """</strong></p>
                    <p class="mt-2">总链接数: <strong>""" + str(len(links)) + """</strong></p>
                </div>
                <div class="card">
                    <h2>⚡ 快捷操作</h2>
                    <a href="{{ url_for('create_link_page') }}" class="btn btn-primary mb-2">生成新链接</a>
                    <a href="{{ url_for('links_list') }}" class="btn btn-secondary mb-2" style="margin-left:8px">查看链接列表</a>
                </div>
            </div>
        </div>
    </body>
    </html>
    """
    return render_template_string(html)

@app.route("/admin/create_link", methods=["GET", "POST"])
@admin_required
def create_link_page():
    if request.method == "POST":
        action = request.form.get("action")

        if action == "by_type":
            email_type = request.form.get("email_type")
            quantity = int(request.form.get("quantity", 1))
            days = int(request.form.get("days", 30))
            buyer_id = request.form.get("buyer_id", "").strip()

            available = get_available_emails_by_type(email_type)
            if len(available) < quantity:
                flash(f"库存不足！{email_type}邮箱可用 {len(available)} 个，需要 {quantity} 个", "error")
                return redirect(url_for("create_link_page"))

            # 排除该买家已分配过的邮箱
            used = get_used_emails()
            excluded = set()
            if buyer_id and buyer_id in used:
                excluded = set(used[buyer_id])

            candidates = [e for e in available.keys() if e not in excluded]
            if len(candidates) < quantity:
                flash(f"该买家已分配过部分邮箱，剩余可用 {len(candidates)} 个，需要 {quantity} 个", "error")
                return redirect(url_for("create_link_page"))

            selected = random.sample(candidates, quantity)
            link_id = create_link(selected, days, buyer_id)
            link_url = f"https://{DOMAIN}/query?link={link_id}"

            flash(f"链接生成成功！<br><span class=\"link-url\">{link_url}</span>", "success")
            return redirect(url_for("create_link_page"))

        elif action == "by_emails":
            emails_text = request.form.get("emails", "")
            days = int(request.form.get("days", 30))

            emails = [e.strip() for e in emails_text.split("\n") if e.strip() and "@" in e.strip()]
            if not emails:
                flash("请输入有效的邮箱地址", "error")
                return redirect(url_for("create_link_page"))

            # 验证邮箱是否在库存中
            all_accounts = parse_accounts()
            invalid_emails = [e for e in emails if e not in all_accounts]
            if invalid_emails:
                flash(f"以下邮箱不在库存中: {', '.join(invalid_emails)}", "error")
                return redirect(url_for("create_link_page"))

            link_id = create_link(emails, days)
            link_url = f"https://{DOMAIN}/query?link={link_id}"

            flash(f"链接生成成功！<br><span class=\"link-url\">{link_url}</span>", "success")
            return redirect(url_for("create_link_page"))

    # 统计各类型可用数量
    type_stats = {}
    for t in ["数字", "英文", "foxmail"]:
        available = get_available_emails_by_type(t)
        type_stats[t] = len(available)

    html = """
    <!DOCTYPE html>
    <html lang="zh-CN">
    <head>
        <meta charset="UTF-8">
        <title>生成链接</title>
        """ + COMMON_CSS + """
    </head>
    <body>
        <div class="nav">
            <div class="nav-inner">
                <div>
                    <a href="{{ url_for('admin') }}">🏠 后台首页</a>
                    <a href="{{ url_for('create_link_page') }}">➕ 生成链接</a>
                    <a href="{{ url_for('links_list') }}">📋 链接列表</a>
                    <a href="{{ url_for('invalidate_by_id_page') }}">❌ 失效链接</a>
                </div>
                <div>
                    <a href="{{ url_for('logout') }}">🚪 退出</a>
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

            <h1>➕ 生成查询链接</h1>
            <div class="card">
                <div class="tab">
                    <button class="tab-item active" onclick="switchTab(0)">按类型生成</button>
                    <button class="tab-item" onclick="switchTab(1)">指定邮箱生成</button>
                </div>

                <div class="tab-content" id="tab-0" style="display:block;">
                    <form method="post">
                        <input type="hidden" name="action" value="by_type">
                        <div class="form-group">
                            <label>邮箱类型</label>
                            <select name="email_type" required>
                                <option value="数字">数字邮箱 (可用: """ + str(type_stats["数字"]) + """)</option>
                                <option value="英文">英文邮箱 (可用: """ + str(type_stats["英文"]) + """)</option>
                                <option value="foxmail">Foxmail邮箱 (可用: """ + str(type_stats["foxmail"]) + """)</option>
                            </select>
                        </div>
                        <div class="grid-2">
                            <div class="form-group">
                                <label>数量</label>
                                <input type="number" name="quantity" min="1" value="1" required>
                            </div>
                            <div class="form-group">
                                <label>有效期（天）</label>
                                <input type="number" name="days" min="1" value="30" required>
                            </div>
                        </div>
                        <div class="form-group">
                            <label>买家ID（可选，用于防重复分配）</label>
                            <input type="text" name="buyer_id" placeholder="输入买家ID">
                        </div>
                        <button type="submit" class="btn btn-primary">生成链接</button>
                    </form>
                </div>

                <div class="tab-content" id="tab-1" style="display:none;">
                    <form method="post">
                        <input type="hidden" name="action" value="by_emails">
                        <div class="form-group">
                            <label>邮箱列表（每行一个）</label>
                            <textarea name="emails" rows="8" placeholder="example1@qq.com&#10;example2@qq.com" required></textarea>
                        </div>
                        <div class="form-group">
                            <label>有效期（天）</label>
                            <input type="number" name="days" min="1" value="30" required>
                        </div>
                        <button type="submit" class="btn btn-primary">生成链接</button>
                    </form>
                </div>
            </div>
        </div>
        <script>
            function switchTab(index) {
                document.querySelectorAll('.tab-item').forEach((el, i) => {
                    el.classList.toggle('active', i === index);
                });
                document.getElementById('tab-0').style.display = index === 0 ? 'block' : 'none';
                document.getElementById('tab-1').style.display = index === 1 ? 'block' : 'none';
            }
        </script>
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
        emails_html = "".join([f"<div>{e}</div>" for e in link.get("emails", [])])
        buyer = link.get("buyer_id") or "-"
        action_btn = """
            <form method="post" action=""" + '"' + url_for("invalidate_link_route") + '"' + """ style="display:inline" onsubmit="return confirm('确定要失效此链接吗？');">
                <input type="hidden" name="link_id" value=""" + link["id"] + """>
                <button type="submit" class="btn btn-danger btn-sm">失效</button>
            </form>
        """ if link.get("status") == "有效" else '<span class="text-muted">已失效</span>'

        rows += f"""
        <tr>
            <td><code>{link["id"][:18]}...</code></td>
            <td>{emails_html}</td>
            <td>{buyer}</td>
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
                    <a href="{{ url_for('admin') }}">🏠 后台首页</a>
                    <a href="{{ url_for('create_link_page') }}">➕ 生成链接</a>
                    <a href="{{ url_for('links_list') }}">📋 链接列表</a>
                    <a href="{{ url_for('invalidate_by_id_page') }}">❌ 失效链接</a>
                </div>
                <div>
                    <a href="{{ url_for('logout') }}">🚪 退出</a>
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

            <h1>📋 链接列表</h1>
            <div class="card">
                """ + ("""
                <div style="overflow-x:auto">
                <table>
                    <thead>
                        <tr>
                            <th>链接ID</th>
                            <th>绑定邮箱</th>
                            <th>买家ID</th>
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
                    <a href="{{ url_for('admin') }}">🏠 后台首页</a>
                    <a href="{{ url_for('create_link_page') }}">➕ 生成链接</a>
                    <a href="{{ url_for('links_list') }}">📋 链接列表</a>
                    <a href="{{ url_for('invalidate_by_id_page') }}">❌ 失效链接</a>
                </div>
                <div>
                    <a href="{{ url_for('logout') }}">🚪 退出</a>
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

            <h1>❌ 输入ID失效链接</h1>
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

# ============ 用户查询 ============

@app.route("/query")
def query():
    link_id = request.args.get("link", "")
    if not link_id:
        return "<h1>链接无效</h1>", 400

    link_data = get_link(link_id)
    if not link_data:
        return "<h1>链接不存在</h1>", 404

    if not is_link_valid(link_data):
        return "<h1>链接已失效或已过期</h1>", 403

    emails = link_data.get("emails", [])
    expire_at = link_data.get("expire_at", "")

    # 获取每封邮箱的最新邮件
    accounts = parse_accounts()
    email_data = []
    for e in emails:
        auth_code = accounts.get(e, "")
        mail_info = None
        if auth_code:
            mail_info = fetch_latest_email(e, auth_code)
        email_data.append({"email": e, "mail": mail_info})

    cards = ""
    for item in email_data:
        if item["mail"]:
            cards += f"""
            <div class="card email-card">
                <div class="email-header">
                    <span class="email-addr">{item["email"]}</span>
                </div>
                <div class="mail-item">
                    <div class="mail-field"><strong>发件人:</strong> {item["mail"]["from"]}</div>
                    <div class="mail-field"><strong>主题:</strong> {item["mail"]["subject"]}</div>
                    <div class="mail-field"><strong>时间:</strong> {item["mail"]["date"]}</div>
                    <div class="preview">{item["mail"]["preview"]}</div>
                    <a href="/query/email_detail?link={link_id}&email={item["email"]}" target="_blank" class="view-full">🔗 查看完整邮件</a>
                </div>
            </div>
            """
        else:
            cards += f"""
            <div class="card email-card">
                <div class="email-header">
                    <span class="email-addr">{item["email"]}</span>
                </div>
                <div class="no-mail">暂无邮件或读取失败</div>
            </div>
            """

    html = f"""
    <!DOCTYPE html>
    <html lang="zh-CN">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>邮箱查询</title>
        <style>
            * {{ box-sizing: border-box; margin: 0; padding: 0; }}
            body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif; background: #f0f2f5; color: #333; line-height: 1.6; }}
            .container {{ max-width: 800px; margin: 0 auto; padding: 20px; }}
            .card {{ background: #fff; border-radius: 12px; box-shadow: 0 2px 8px rgba(0,0,0,0.08); padding: 24px; margin-bottom: 20px; }}
            h1 {{ font-size: 22px; margin-bottom: 8px; color: #1a1a2e; }}
            .expire-info {{ color: #e74c3c; font-size: 14px; margin-bottom: 20px; }}
            .email-card {{ border: 1px solid #eee; border-radius: 10px; padding: 20px; margin-bottom: 16px; }}
            .email-header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }}
            .email-addr {{ font-weight: 600; font-size: 16px; color: #667eea; }}
            .no-mail {{ color: #999; text-align: center; padding: 20px; }}
            .mail-item {{ margin-top: 12px; }}
            .mail-field {{ margin-bottom: 8px; font-size: 14px; }}
            .mail-field strong {{ color: #555; display: inline-block; width: 70px; }}
            .preview {{ background: #f8f9fa; padding: 14px; border-radius: 8px; margin-top: 12px; font-size: 14px; color: #555; line-height: 1.8; }}
            .view-full {{ display: inline-block; margin-top: 12px; color: #667eea; text-decoration: none; font-size: 14px; font-weight: 500; }}
            .view-full:hover {{ text-decoration: underline; }}
            .footer {{ text-align: center; color: #aaa; font-size: 12px; margin-top: 30px; }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="card">
                <h1>📧 邮箱查询</h1>
                <p class="expire-info">⏰ 有效期至: {expire_at} (北京时间)</p>
            </div>
            {cards}
            <div class="footer">mail-auto.zeabur.app</div>
        </div>
    </body>
    </html>
    """
    return render_template_string(html)

@app.route("/query/email_detail")
def email_detail():
    link_id = request.args.get("link", "")
    email_addr = request.args.get("email", "")

    link_data = get_link(link_id)
    if not link_data or not is_link_valid(link_data):
        return "<h1>链接无效</h1>", 403

    if email_addr not in link_data.get("emails", []):
        return "<h1>无权查看</h1>", 403

    accounts = parse_accounts()
    auth_code = accounts.get(email_addr, "")
    if not auth_code:
        return "<h1>邮箱配置错误</h1>", 500

    mail_info = fetch_latest_email(email_addr, auth_code)
    if not mail_info:
        return "<h1>暂无邮件</h1>", 404

    body_html = mail_info.get("body_html", "")
    if not body_html.strip().startswith("<"):
        body_html = f'<pre style="white-space:pre-wrap;word-wrap:break-word;">{body_html}</pre>'

    # 转义用于 srcdoc
    import html as html_module
    body_escaped = html_module.escape(body_html)

    html = f"""
    <!DOCTYPE html>
    <html lang="zh-CN">
    <head>
        <meta charset="UTF-8">
        <title>完整邮件 - {email_addr}</title>
        <style>
            body {{ margin: 0; background: #f0f2f5; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif; }}
            .header {{ background: #1a1a2e; color: #fff; padding: 16px 24px; }}
            .header h1 {{ font-size: 16px; margin: 0; }}
            .header p {{ font-size: 13px; margin: 4px 0 0; opacity: 0.8; }}
            .iframe-wrap {{ padding: 20px; }}
            iframe {{ width: 100%; height: calc(100vh - 120px); border: none; border-radius: 8px; background: #fff; }}
        </style>
    </head>
    <body>
        <div class="header">
            <h1>📧 {mail_info["subject"]}</h1>
            <p>发件人: {mail_info["from"]} | 时间: {mail_info["date"]}</p>
        </div>
        <div class="iframe-wrap">
            <iframe sandbox="allow-same-origin allow-scripts allow-popups allow-forms" srcdoc="{body_escaped}"></iframe>
        </div>
    </body>
    </html>
    """
    return render_template_string(html)

# ============ 自动发货 API ============

@app.route("/api/auto_create_link", methods=["POST"])
def api_auto_create_link():
    """
    自动发货 API
    请求: POST JSON
    {
        "type": "数字",      // 数字/英文/foxmail
        "quantity": 1,
        "days": 30,
        "buyer_id": "买家ID"
    }
    返回: 纯文本
    """
    try:
        data = request.get_json(force=True)
    except Exception:
        data = {}

    email_type = data.get("type", "")
    quantity = int(data.get("quantity", 1))
    days = int(data.get("days", 30))
    buyer_id = data.get("buyer_id", "").strip()

    if not email_type or email_type not in ["数字", "英文", "foxmail"]:
        return "参数错误: type 必须是 数字/英文/foxmail 之一", 400

    if quantity < 1:
        return "参数错误: quantity 必须大于0", 400

    available = get_available_emails_by_type(email_type)
    if len(available) < quantity:
        return f"库存不足！{email_type}邮箱可用 {len(available)} 个", 400

    # 排除已分配给该买家的邮箱
    used = get_used_emails()
    excluded = set()
    if buyer_id and buyer_id in used:
        excluded = set(used[buyer_id])

    candidates = [e for e in available.keys() if e not in excluded]
    if len(candidates) < quantity:
        return f"该买家已分配过部分邮箱，剩余可用 {len(candidates)} 个", 400

    selected = random.sample(candidates, quantity)
    link_id = create_link(selected, days, buyer_id)
    link_url = f"https://{DOMAIN}/query?link={link_id}"
    expire_at = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")

    lines = ["您购买的邮箱已发货"]
    for e in selected:
        lines.append(f"邮箱：{e}")
    lines.append(f"查询链接：{link_url}")
    lines.append(f"有效期至：{expire_at}")

    return "\n".join(lines)

# ============ 启动 ============

if __name__ == "__main__":
    # 确保数据文件存在
    for f in [LINKS_FILE, BACKUP_FILE, USED_EMAILS_FILE]:
        if not os.path.exists(f):
            save_json(f, {})

    app.run(host="0.0.0.0", port=PORT, debug=False)
