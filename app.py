import os
import json
import uuid
import random
import re
import html
import imaplib
import email
from datetime import datetime, timedelta
from email.header import decode_header
from email.utils import parsedate_to_datetime
from flask import Flask, request, jsonify, redirect, session

app = Flask(__name__)
app.secret_key = 'your-secret-key-here'

# ===== 配置 =====
ACCOUNTS_FILE = "accounts.txt"
LINKS_FILE = "links.json"
USED_EMAILS_FILE = "used_emails.json"
ADMIN_PASSWORD = "060910"
DEFAULT_DAYS = 30
DOMAIN = "mail-auto.zeabur.app"

# ===== 读取邮箱 =====
def load_accounts():
    accounts = {}
    try:
        with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                if "----" in line:
                    parts = line.split("----")
                    if len(parts) == 2:
                        email = parts[0].strip()
                        auth = parts[1].strip()
                        if '@' not in email:
                            email = email + "@qq.com"
                        accounts[email] = auth
                else:
                    parts = line.split()
                    if len(parts) >= 4:
                        auth = parts[3]
                        for email in parts[0:3]:
                            if '@' not in email:
                                email = email + "@qq.com"
                            accounts[email] = auth
                    elif len(parts) == 2:
                        email = parts[0]
                        auth = parts[1]
                        if '@' not in email:
                            email = email + "@qq.com"
                        accounts[email] = auth
    except:
        pass
    return accounts

ACCOUNTS = load_accounts()
print(f"已加载 {len(ACCOUNTS)} 个邮箱")

# ===== 文件读写 =====
def load_links():
    try:
        with open(LINKS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return {}

def save_links(data):
    with open(LINKS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    try:
        with open("links_backup.json", "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except:
        pass

def load_used():
    try:
        with open(USED_EMAILS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return {"records": {}}

def save_used(data):
    with open(USED_EMAILS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# ===== 邮件解析 =====
def decode_str(s):
    if not s:
        return ""
    try:
        parts = []
        for part, charset in decode_header(s):
            if isinstance(part, bytes):
                parts.append(part.decode(charset or 'utf-8', errors='replace'))
            else:
                parts.append(str(part))
        return ' '.join(parts)
    except:
        return str(s)

def get_mail_content(msg):
    content = ""
    try:
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    payload = part.get_payload(decode=True)
                    if payload:
                        content = payload.decode('utf-8', errors='replace')
                        break
                elif part.get_content_type() == "text/html" and not content:
                    payload = part.get_payload(decode=True)
                    if payload:
                        html_text = payload.decode('utf-8', errors='replace')
                        content = re.sub(r'<[^>]+>', ' ', html_text)
                        content = html.unescape(content)
                        content = re.sub(r'\s+', ' ', content)
        else:
            payload = msg.get_payload(decode=True)
            if payload:
                content = payload.decode('utf-8', errors='replace')
    except:
        content = "解析失败"
    return content.strip() or "无内容"

def get_latest_mails(email, limit=1):
    if email not in ACCOUNTS:
        return {'error': '邮箱未绑定'}
    auth = ACCOUNTS[email]
    mail = None
    try:
        mail = imaplib.IMAP4_SSL("imap.qq.com")
        mail.login(email, auth)
        mail.select("INBOX")
        status, data = mail.search(None, "ALL")
        ids = data[0].split() if data[0] else []
        if not ids:
            return []
        latest = ids[-limit:]
        mails = []
        for mid in reversed(latest):
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
                    except:
                        send_time = date_str[:30]
                    mails.append({
                        'mail_id': mid.decode() if isinstance(mid, bytes) else str(mid),
                        'sender': decode_str(msg.get("From", "未知")),
                        'subject': decode_str(msg.get("Subject", "无主题")),
                        'content': get_mail_content(msg),
                        'time': send_time
                    })
                    break
        mail.close()
        mail.logout()
        return mails
    except Exception as e:
        return {'error': str(e)}
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

# ===== 类型识别 =====
def detect_type(email):
    if email.endswith("@foxmail.com"):
        return "foxmail"
    return "数字" if email.split("@")[0].isdigit() else "英文"

def assign_emails(type_name, quantity, buyer_id):
    all_emails = list(ACCOUNTS.keys())
    typed = [e for e in all_emails if detect_type(e) == type_name]
    if not typed:
        return None, f"类型 '{type_name}' 没有可用邮箱"
    used = load_used()
    buyer_used = used.get("records", {}).get(buyer_id, [])
    available = [e for e in typed if e not in buyer_used]
    if len(available) < quantity:
        return None, f"库存不足！需要 {quantity} 个，只剩 {len(available)} 个"
    selected = random.sample(available, quantity)
    if buyer_id not in used["records"]:
        used["records"][buyer_id] = []
    used["records"][buyer_id].extend(selected)
    save_used(used)
    return selected, None

# ===== 路由 =====
@app.route('/')
def index():
    return redirect('/admin')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        if request.form.get('password') == ADMIN_PASSWORD:
            session['logged_in'] = True
            return redirect('/admin')
        return '<h2>密码错误</h2><a href="/login">重新输入</a>'
    return '''
    <form method="post">
        <input type="password" name="password" placeholder="密码">
        <button type="submit">登录</button>
    </form>
    '''

@app.route('/logout')
def logout():
    session.clear()
    return redirect('/login')

@app.route('/admin')
def admin():
    if not session.get('logged_in'):
        return redirect('/login')
    links = load_links()
    used = load_used()
    all_emails = list(ACCOUNTS.keys())
    all_used = []
    for buyer, emails in used.get("records", {}).items():
        all_used.extend(emails)
    
    link_rows = ""
    for lid, data in links.items():
        status = data.get('status', 'active')
        link_rows += f'<tr><td>{lid}</td><td>{", ".join(data["emails"])}</td><td>{"有效" if status=="active" else "已失效"}</td><td><button onclick="disableLink(\'{lid}\')">失效</button></td></tr>'
    
    html_content = f'''
    <h2>邮箱管理后台</h2>
    <p>总邮箱：{len(all_emails)} | 已分配：{len(set(all_used))} | 可用：{len(all_emails) - len(set(all_used))}</p>
    <hr>
    <h3>手动生成链接</h3>
    <textarea id="emails_input" rows="4" cols="40" placeholder="每行一个邮箱"></textarea><br>
    <select id="type_select"><option value="数字">数字</option><option value="英文">英文</option><option value="foxmail">foxmail</option></select>
    <input type="number" id="days_input" value="30">
    <button onclick="generate()">生成链接</button>
    <div id="result"></div>
    <hr>
    <h3>链接失效</h3>
    <input id="disable_input" placeholder="输入链接ID">
    <button onclick="disableLink()">失效</button>
    <div id="disable_result"></div>
    <hr>
    <h3>已生成链接</h3>
    <table border="1">
        <tr><th>ID</th><th>邮箱</th><th>状态</th><th>操作</th></tr>
        {link_rows}
    </table>
    <script>
        async function generate() {{
            const emails = document.getElementById("emails_input").value.split("\\n").filter(e => e.trim());
            if(!emails.length) return alert("请输入邮箱");
            const res = await fetch("/api/admin_create_link", {{
                method: "POST",
                headers: {{"Content-Type": "application/json"}},
                body: JSON.stringify({{
                    emails: emails,
                    type: document.getElementById("type_select").value,
                    days: parseInt(document.getElementById("days_input").value) || 30
                }})
            }});
            const data = await res.json();
            if(data.error) return alert(data.error);
            document.getElementById("result").innerHTML = "生成成功！<br>链接：<a href=\\"" + data.link_url + "\\" target=\\"_blank\\">" + data.link_url + "</a><br>有效期至：" + data.expire_at;
        }}
        async function disableLink(id) {{
            if(!id) id = document.getElementById("disable_input").value.trim();
            if(!id) return alert("请输入链接ID");
            if(!confirm("确定失效吗？")) return;
            const res = await fetch("/api/disable_link", {{
                method: "POST",
                headers: {{"Content-Type": "application/json"}},
                body: JSON.stringify({{link_id: id}})
            }});
            const data = await res.json();
            if(data.success) {{ alert("已失效"); location.reload(); }}
            else alert("失败：" + data.error);
        }}
    </script>
    '''
    return html_content

@app.route('/api/admin_create_link', methods=['POST'])
def admin_create_link():
    data = request.get_json()
    emails = data.get('emails', [])
    type_name = data.get('type', '英文')
    days = data.get('days', 30)
    if not emails:
        return jsonify({'error': '请提供邮箱'})
    link_id = str(uuid.uuid4())[:8]
    links = load_links()
    now = datetime.now()
    links[link_id] = {
        'link_id': link_id,
        'buyer_id': 'admin',
        'type': type_name,
        'emails': emails,
        'quantity': len(emails),
        'created_at': now.strftime("%Y-%m-%d %H:%M:%S"),
        'expire_at': (now + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S"),
        'status': 'active',
        'query_count': 0
    }
    save_links(links)
    return jsonify({
        'success': True,
        'link_id': link_id,
        'link_url': f"https://{DOMAIN}/query?link={link_id}",
        'expire_at': links[link_id]['expire_at']
    })

@app.route('/api/disable_link', methods=['POST'])
def disable_link():
    data = request.get_json()
    link_id = data.get('link_id')
    links = load_links()
    if link_id not in links:
        return jsonify({'error': '链接不存在'})
    links[link_id]['status'] = 'disabled'
    save_links(links)
    return jsonify({'success': True})

@app.route('/query')
def query_page():
    link_id = request.args.get('link')
    if not link_id:
        return "缺少链接ID"
    links = load_links()
    if link_id not in links:
        return "链接不存在"
    data = links[link_id]
    if data.get('status') == 'disabled':
        return "链接已失效"
    if datetime.now() > datetime.strptime(data['expire_at'], "%Y-%m-%d %H:%M:%S"):
        return "链接已过期"
    
    emails = data['emails']
    html_content = f'<h2>邮箱查询</h2><p>链接ID：{link_id}</p><p>有效期至：{data["expire_at"]}</p><hr>'
    
    for email in emails:
        result = get_latest_mails(email, 1)
        if isinstance(result, dict) and 'error' in result:
            html_content += f'<p>❌ {email}：{result["error"]}</p>'
            continue
        if not result:
            html_content += f'<p>📭 {email}：暂无邮件</p>'
            continue
        mail = result[0]
        html_content += f'''
        <div style="border:1px solid #ddd;padding:10px;margin:5px 0;">
            <b>{mail["sender"]}</b> {mail["time"]}<br>
            {mail["subject"]}<br>
            {mail["content"][:200]}<br>
            <a href="/view_raw?link_id={link_id}&mail_id={mail["mail_id"]}" target="_blank">查看完整邮件</a>
        </div>
        '''
    return html_content

@app.route('/view_raw')
def view_raw():
    link_id = request.args.get('link_id')
    mail_id = request.args.get('mail_id')
    if not link_id or not mail_id:
        return "缺少参数"
    links = load_links()
    if link_id not in links:
        return "链接不存在"
    data = links[link_id]
    if data.get('status') == 'disabled':
        return "链接已失效"
    email = data['emails'][0]
    auth = ACCOUNTS.get(email)
    if not auth:
        return "授权码不存在"
    try:
        mail = imaplib.IMAP4_SSL("imap.qq.com")
        mail.login(email, auth)
        mail.select("INBOX")
        _, msg_data = mail.fetch(mail_id.encode(), "(RFC822)")
        for part in msg_data:
            if isinstance(part, tuple):
                msg = email.message_from_bytes(part[1])
                html_content = ""
                if msg.is_multipart():
                    for p in msg.walk():
                        if p.get_content_type() == "text/html":
                            payload = p.get_payload(decode=True)
                            if payload:
                                html_content = payload.decode('utf-8', errors='replace')
                                break
                else:
                    payload = msg.get_payload(decode=True)
                    if payload:
                        html_content = payload.decode('utf-8', errors='replace')
                if not html_content:
                    html_content = "<pre>" + get_mail_content(msg) + "</pre>"
                mail.close()
                mail.logout()
                return f'''
                <html><head><meta charset="UTF-8"></head>
                <body>
                    <h3>{email}</h3>
                    <div style="border:1px solid #ddd;padding:20px;background:white;">
                        {html_content}
                    </div>
                    <a href="/query?link={link_id}">返回</a>
                </body></html>
                '''
        mail.close()
        mail.logout()
    except Exception as e:
        return f"查看失败：{str(e)}"
    return "邮件不存在"

@app.route('/api/auto_create_link', methods=['POST'])
def auto_create_link():
    data = request.get_json() or {}
    type_name = data.get('type', '英文')
    quantity = data.get('quantity', 1)
    days = data.get('days', DEFAULT_DAYS)
    buyer_id = data.get('buyer_id', str(uuid.uuid4())[:8])
    if quantity <= 0:
        return "数量必须大于0", 400
    valid = ['数字', '英文', 'foxmail']
    if type_name not in valid:
        return f"无效类型，请选择: {', '.join(valid)}", 400
    selected, error = assign_emails(type_name, quantity, buyer_id)
    if error:
        return f"分配失败：{error}", 400
    link_id = str(uuid.uuid4())[:8]
    links = load_links()
    now = datetime.now()
    links[link_id] = {
        'link_id': link_id,
        'buyer_id': buyer_id,
        'type': type_name,
        'emails': selected,
        'quantity': quantity,
        'created_at': now.strftime("%Y-%m-%d %H:%M:%S"),
        'expire_at': (now + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S"),
        'status': 'active',
        'query_count': 0
    }
    save_links(links)
    link_url = f"https://{DOMAIN}/query?link={link_id}"
    return f"""您购买的邮箱已发货\n邮箱：{chr(10).join(selected)}\n查询链接：{link_url}\n有效期至：{links[link_id]['expire_at']}"""

@app.route('/api/groups', methods=['GET'])
def groups():
    all_emails = list(ACCOUNTS.keys())
    used = load_used()
    all_used = []
    for buyer, emails in used.get("records", {}).items():
        all_used.extend(emails)
    types = ["数字", "英文", "foxmail"]
    result = []
    for t in types:
        typed = [e for e in all_emails if detect_type(e) == t]
        result.append({"name": t, "total": len(typed), "available": len([e for e in typed if e not in all_used])})
    return jsonify(result)

if __name__ == '__main__':
    print("系统启动，密码: 060910")
    app.run(host='0.0.0.0', port=8080)
