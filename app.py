import os
os.environ['TZ'] = 'Asia/Shanghai'
try:
    import time
    time.tzset()
except:
    pass

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
        
        all_ids = []
        folder_info = []
        folders = ["INBOX", "垃圾箱", "广告邮件"]
        for folder in folders:
            try:
                mail.select(folder)
                status, data = mail.search(None, "ALL")
                if data[0]:
                    for mid in data[0].split():
                        all_ids.append(mid)
                        folder_info.append(folder)
            except:
                continue
        
        if not all_ids:
            return []
        
        seen = set()
        unique_ids = []
        unique_folders = []
        for mid, folder in zip(all_ids, folder_info):
            mid_str = mid.decode() if isinstance(mid, bytes) else str(mid)
            if mid_str not in seen:
                seen.add(mid_str)
                unique_ids.append(mid)
                unique_folders.append(folder)
        
        sorted_pairs = sorted(zip(unique_ids, unique_folders), key=lambda x: int(x[0]))
        latest_pairs = sorted_pairs[-limit:]
        mails = []
        
        for mid, folder in reversed(latest_pairs):
            try:
                mail.select(folder)
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
                            'time': send_time,
                            'folder': folder
                        })
                        break
            except:
                continue
        
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

# ===== 查看完整邮件 =====
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
                html_content = html_content.replace('"', '&quot;').replace("'", '&#39;')
                mail.close()
                mail.logout()
                return f'''
                <!DOCTYPE html>
                <html>
                <head><meta charset="UTF-8"><title>查看完整邮件</title>
                <style>
                    body{{margin:0;padding:0;background:#f0f0f0;font-family:Arial,sans-serif;}}
                    .header{{background:#fff;padding:15px 20px;border-bottom:1px solid #ddd;}}
                    .header h2{{margin:0;font-size:18px;}}
                    .header .info{{color:#666;font-size:13px;margin-top:5px;}}
                    iframe{{width:100%;min-height:800px;border:none;background:white;}}
                    .back{{display:inline-block;margin:15px 20px;color:#667eea;text-decoration:none;}}
                </style>
                </head>
                <body>
                    <div class="header">
                        <h2>📧 {email}</h2>
                        <div class="info">发件人：{decode_str(msg.get('From', '未知'))} | 主题：{decode_str(msg.get('Subject', '无主题'))}</div>
                    </div>
                    <iframe srcdoc="{html_content}" sandbox="allow-popups allow-same-origin"></iframe>
                    <a href="/query?link={link_id}" class="back">← 返回</a>
                </body>
                </html>
                '''
        mail.close()
        mail.logout()
        return "邮件不存在"
    except Exception as e:
        return f"查看失败：{str(e)}"

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
        status_text = '有效' if status == 'active' else '已失效'
        link_rows += f'''
        <tr>
            <td>{lid}</td>
            <td>{", ".join(data["emails"])}</td>
            <td>{data["created_at"]}</td>
            <td>{data["expire_at"]}</td>
            <td>{status_text}</td>
            <td><button onclick="disableLink('{lid}')" style="padding:4px 12px;background:#e74c3c;color:white;border:none;border-radius:4px;cursor:pointer;">失效</button></td>
        </tr>
        '''
    
    html_content = f'''
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <title>邮箱管理后台</title>
        <style>
            * {{ margin: 0; padding: 0; box-sizing: border-box; }}
            body {{ font-family: Arial, sans-serif; background: #f0f2f5; padding: 20px; }}
            .container {{ max-width: 1200px; margin: 0 auto; }}
            .card {{ background: white; border-radius: 12px; padding: 24px; margin-bottom: 20px; box-shadow: 0 2px 8px rgba(0,0,0,0.1); }}
            .card h2 {{ margin-bottom: 16px; font-size: 18px; }}
            .row {{ display: flex; gap: 12px; flex-wrap: wrap; align-items: end; }}
            .field {{ display: flex; flex-direction: column; }}
            .field label {{ font-size: 13px; color: #666; margin-bottom: 4px; }}
            .field input, .field select, .field textarea {{ padding: 10px; border: 2px solid #ddd; border-radius: 8px; font-size: 14px; }}
            .btn {{ padding: 10px 30px; background: #4CAF50; color: white; border: none; border-radius: 8px; cursor: pointer; font-size: 14px; font-weight: bold; }}
            .btn:hover {{ background: #45a049; }}
            .btn-blue {{ background: #667eea; }}
            .btn-blue:hover {{ background: #5a67d8; }}
            .btn-danger {{ background: #e74c3c; }}
            .btn-danger:hover {{ background: #c0392b; }}
            .result-box {{ background: #f8f9fa; padding: 16px; border-radius: 8px; margin-top: 16px; display: none; }}
            .result-box .email-item {{ padding: 6px 0; border-bottom: 1px solid #eee; font-family: monospace; }}
            .result-box .link-area {{ background: #e8f5e9; padding: 12px; border-radius: 6px; margin-top: 10px; word-break: break-all; }}
            .copy-btn {{ padding: 6px 16px; background: #667eea; color: white; border: none; border-radius: 6px; cursor: pointer; font-size: 12px; margin-left: 8px; }}
            .copy-btn:hover {{ background: #5a67d8; }}
            .stats {{ display: flex; gap: 30px; flex-wrap: wrap; }}
            .stats span {{ font-size: 14px; color: #666; }}
            .stats strong {{ font-size: 18px; color: #1a1a2e; }}
            .logout {{ float: right; color: #e74c3c; text-decoration: none; font-size: 14px; }}
            table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
            th {{ background: #f8f9fa; padding: 10px; text-align: left; border-bottom: 2px solid #ddd; }}
            td {{ padding: 10px; border-bottom: 1px solid #eee; }}
            .separator {{ border: none; border-top: 2px dashed #ddd; margin: 20px 0; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="card">
            <h2>邮箱管理后台 <a href="/logout" class="logout">退出</a></h2>
            <div class="stats">
                <span>总邮箱：<strong>{len(all_emails)}</strong></span>
                <span>已分配：<strong>{len(set(all_used))}</strong></span>
                <span>可用：<strong>{len(all_emails) - len(set(all_used))}</strong></span>
            </div>
        </div>

        <div class="card">
            <h2>生成邮箱链接</h2>
            <div class="row">
                <div class="field">
                    <label>输入邮箱（每行一个）</label>
                    <textarea id="manualEmails" placeholder="每行一个邮箱" rows="5" style="min-width:350px;"></textarea>
                </div>
                <div class="field">
                    <label>选择类型</label>
                    <select id="emailType">
                        <option value="数字">数字邮箱</option>
                        <option value="英文">英文邮箱</option>
                        <option value="foxmail">foxmail邮箱</option>
                    </select>
                </div>
                <div class="field">
                    <label>有效期（天）</label>
                    <input type="number" id="manualDays" value="30" min="1" max="365">
                </div>
                <button class="btn btn-blue" onclick="generateManualLink()">生成链接</button>
            </div>
            <div class="result-box" id="manualResultBox">
                <div id="manualResultContent"></div>
            </div>
        </div>

        <hr class="separator">

        <div class="card" style="border:2px solid #e74c3c;">
            <h2 style="color:#e74c3c;">输入链接ID使其失效</h2>
            <div class="row">
                <div class="field">
                    <label>链接ID</label>
                    <input type="text" id="disableLinkInput" placeholder="例如：abc123" style="min-width:250px;">
                </div>
                <button class="btn btn-danger" onclick="disableLinkByInput()">失效</button>
            </div>
            <div id="disableResult" style="margin-top:10px;"></div>
        </div>

        <div class="card">
            <h2>已生成的链接</h2>
            <div style="overflow-x:auto;">
                <table>
                    <tr><th>链接ID</th><th>邮箱</th><th>创建时间</th><th>过期时间</th><th>状态</th><th>操作</th></tr>
                    {link_rows if link_rows else '<tr><td colspan="6">暂无链接</td></tr>'}
                </table>
            </div>
        </div>
    </div>

    <script>
        async function disableLink(linkId) {{
            if (!confirm('确定要失效该链接吗？')) return;
            try {{
                const res = await fetch('/api/disable_link', {{
                    method: 'POST',
                    headers: {{'Content-Type': 'application/json'}},
                    body: JSON.stringify({{link_id: linkId}})
                }});
                const data = await res.json();
                if (data.success) {{
                    alert('链接已失效');
                    location.reload();
                }} else {{
                    alert('操作失败：' + data.error);
                }}
            }} catch (e) {{
                alert('请求失败');
            }}
        }}

        async function disableLinkByInput() {{
            const linkId = document.getElementById('disableLinkInput').value.trim();
            if (!linkId) {{
                alert('请输入链接ID');
                return;
            }}
            if (!confirm('确定要失效链接 ' + linkId + ' 吗？')) return;
            try {{
                const res = await fetch('/api/disable_link', {{
                    method: 'POST',
                    headers: {{'Content-Type': 'application/json'}},
                    body: JSON.stringify({{link_id: linkId}})
                }});
                const data = await res.json();
                if (data.success) {{
                    document.getElementById('disableResult').innerHTML = '<div style="color:green;font-weight:bold;">链接已失效</div>';
                    setTimeout(function(){{ location.reload(); }}, 1000);
                }} else {{
                    document.getElementById('disableResult').innerHTML = '<div style="color:red;">' + data.error + '</div>';
                }}
            }} catch (e) {{
                document.getElementById('disableResult').innerHTML = '<div style="color:red;">请求失败</div>';
            }}
        }}

        async function generateManualLink() {{
            const emailsText = document.getElementById('manualEmails').value.trim();
            const type = document.getElementById('emailType').value;
            const days = parseInt(document.getElementById('manualDays').value) || 30;

            if (!emailsText) {{
                alert('请输入邮箱地址');
                return;
            }}

            const emails = emailsText.split('\\n').map(e => e.trim()).filter(e => e);
            if (emails.length === 0) {{
                alert('请输入有效邮箱地址');
                return;
            }}

            const resultBox = document.getElementById('manualResultBox');
            const resultContent = document.getElementById('manualResultContent');
            resultBox.style.display = 'block';
            resultContent.innerHTML = '生成中...';

            try {{
                const res = await fetch('/api/admin_create_link', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{
                        emails: emails,
                        type: type,
                        days: days
                    }})
                }});
                const data = await res.json();

                if (data.error) {{
                    resultContent.innerHTML = '<div style="color:red;">' + data.error + '</div>';
                    return;
                }}

                let html = '<div style="font-weight:bold;margin-bottom:10px;">生成成功</div>';
                html += '<div style="margin-bottom:8px;">邮箱列表：</div>';
                data.emails.forEach((email, idx) => {{
                    html += '<div class="email-item">' + (idx+1) + '. ' + email + '</div>';
                }});
                html += '<div class="link-area">查询链接：<span style="color:#667eea;">' + data.link_url + '</span>';
                html += '<button class="copy-btn" onclick="copyText(\'' + data.link_url + '\')">复制链接</button></div>';
                html += '<div style="margin-top:8px;color:#999;font-size:13px;">有效期至：' + data.expire_at + '</div>';
                html += '<button onclick="copyAll(\'' + data.emails.join(',') + '\', \'' + data.link_url + '\')" style="margin-top:12px;padding:8px 20px;background:#667eea;color:white;border:none;border-radius:6px;cursor:pointer;font-size:13px;">复制全部</button>';

                resultContent.innerHTML = html;
                location.reload();

            }} catch (e) {{
                resultContent.innerHTML = '<div style="color:red;">请求失败：' + e.message + '</div>';
            }}
        }}

        function copyText(text) {{
            navigator.clipboard.writeText(text).then(() => {{
                alert('已复制');
            }});
        }}

        function copyAll(emails, link) {{
            const text = '邮箱：' + emails.replace(/,/g, '、') + '\\n查询链接：' + link;
            navigator.clipboard.writeText(text).then(() => {{
                alert('已复制全部内容');
            }});
        }}
    </script>
</body>
</html>
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
        'emails': emails,
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
    html_content = f'''
    <!DOCTYPE html>
    <html>
    <head><meta charset="UTF-8"><title>邮箱查询系统</title></head>
    <body style="font-family:Arial,sans-serif;max-width:700px;margin:50px auto;padding:20px;">
        <div style="background:white;padding:30px;border-radius:12px;box-shadow:0 2px 10px rgba(0,0,0,0.1);">
            <h2>邮箱查询系统</h2>
            <p style="color:#999;font-size:13px;">有效期至：{data["expire_at"]}</p>
            <hr>
    '''
    
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
        <div style="border-bottom:1px solid #ddd;padding:10px;">
            <b>{mail["sender"]}</b> <span style="color:#999;font-size:12px;">{mail.get("time", "")}</span><br>
            <span style="color:#666;">{mail["subject"]}</span><br>
            <span style="font-size:14px;">{mail["content"][:200]}</span>
            <br>
            <a href="/view_raw?link_id={link_id}&mail_id={mail["mail_id"]}" target="_blank" style="color:#667eea;font-size:13px;">查看完整邮件</a>
        </div>
        '''
    
    html_content += '''
        </div>
    </body>
    </html>
    '''
    return html_content

@app.route('/api/query_mail', methods=['POST'])
def query_mail():
    link_id = request.form.get('link_id')
    email = request.form.get('email')
    if not email:
        return "请输入邮箱"
    email = email.strip()
    if '@' not in email:
        email = email + "@qq.com"
    links = load_links()
    if link_id not in links:
        return "链接无效"
    data = links[link_id]
    if email not in data['emails']:
        return f"该邮箱不在本链接中"
    if data.get('status') == 'disabled':
        return "链接已失效"
    if email not in ACCOUNTS:
        return f"邮箱 {email} 未绑定"
    result = get_latest_mails(email, 1)
    if isinstance(result, dict) and 'error' in result:
        return f"查询失败：{result['error']}"
    if not result:
        return "<h3>暂无邮件</h3>"
    mail = result[0]
    return f"""
    <h3>{email} 的最新邮件</h3>
    <div style="border:1px solid #ddd;padding:15px;margin:10px 0;">
        <b>发件人：</b>{mail['sender']}<br>
        <b>主题：</b>{mail['subject']}<br>
        <b>时间：</b>{mail.get('time', '')}<br>
        <hr>
        <div>{mail['content'][:1000]}</div>
        <br>
        <a href="/view_raw?link_id={link_id}&mail_id={mail['mail_id']}" target="_blank">查看完整邮件</a>
    </div>
    """

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
