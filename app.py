import os
os.environ['TZ'] = 'Asia/Shanghai'
try:
    import time
    time.tzset()
except:
    pass

from flask import Flask, request, jsonify, redirect, session, send_from_directory
import imaplib
import email
import re
import html
import os
import json
import shutil
from email.header import decode_header
from email.utils import parsedate_to_datetime
import uuid
import random
from datetime import datetime, timedelta

app = Flask(__name__)
app.secret_key = 'your-secret-key-here'

# ===== 配置文件 =====
LINKS_FILE = "links.json"
ACCOUNTS_FILE = "accounts.txt"
USED_EMAILS_FILE = "used_emails.json"
ADMIN_PASSWORD = "060910"
DEFAULT_DAYS = 30
DOMAIN = "mail-auto.zeabur.app"

# ===== 读取账号 =====
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
                        auth_code = parts[1].strip()
                        if '@' not in email:
                            email = email + "@qq.com"
                        accounts[email] = auth_code
                else:
                    parts = line.split()
                    if len(parts) >= 4:
                        emails = parts[0:3]
                        auth_code = parts[3]
                        for email in emails:
                            if '@' not in email:
                                email = email + "@qq.com"
                            accounts[email] = auth_code
                    elif len(parts) == 2:
                        email = parts[0]
                        auth_code = parts[1]
                        if '@' not in email:
                            email = email + "@qq.com"
                        accounts[email] = auth_code
    except Exception as e:
        print(f"读取账号失败: {e}")
    return accounts

ACCOUNTS = load_accounts()
print(f"已加载 {len(ACCOUNTS)} 个绑定邮箱")

def get_auth_map():
    return ACCOUNTS

# ===== 邮件解析 =====
def decode_str(s):
    if not s:
        return ""
    try:
        decoded_parts = []
        for part, charset in decode_header(s):
            if isinstance(part, bytes):
                if charset:
                    decoded_parts.append(part.decode(charset, errors='replace'))
                else:
                    decoded_parts.append(part.decode('utf-8', errors='replace'))
            else:
                decoded_parts.append(str(part))
        return ' '.join(decoded_parts)
    except:
        return str(s)

def clean_html_to_text(html_text):
    if not html_text:
        return ""
    text = re.sub(r'<style[^>]*>.*?</style>', '', html_text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<script[^>]*>.*?</script>', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<br\s*/?>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'</?(div|p|tr|td|li|h[1-6])[^>]*>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = html.unescape(text)
    text = re.sub(r'\n\s*\n', '\n\n', text)
    text = re.sub(r'[ \t]+', ' ', text)
    return text.strip()

def get_mail_content(msg):
    import re
    import html
    
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
        
    except Exception as e:
        return f"解析失败"

def get_latest_mails(email_addr, limit=1):
    if email_addr not in ACCOUNTS:
        return {'error': f'邮箱 "{email_addr}" 未绑定'}
    
    auth_code = ACCOUNTS[email_addr]
    mail = None
    
    try:
        mail = imaplib.IMAP4_SSL("imap.qq.com")
        mail.login(email_addr, auth_code)
        
        all_mail_ids = []
        folder_info = []
        
        folders_to_read = ["INBOX", "垃圾箱", "广告邮件"]
        for folder in folders_to_read:
            try:
                mail.select(folder)
                status, data = mail.search(None, "ALL")
                if data[0]:
                    for mid in data[0].split():
                        all_mail_ids.append(mid)
                        folder_info.append(folder)
            except Exception as e:
                print(f"读取 {folder} 失败: {e}")
        
        if not all_mail_ids:
            return []
        
        seen = set()
        unique_ids = []
        unique_folders = []
        for mid, folder in zip(all_mail_ids, folder_info):
            mid_str = mid.decode() if isinstance(mid, bytes) else str(mid)
            if mid_str not in seen:
                seen.add(mid_str)
                unique_ids.append(mid)
                unique_folders.append(folder)
        
        sorted_pairs = sorted(zip(unique_ids, unique_folders), key=lambda x: int(x[0]))
        latest_pairs = sorted_pairs[-limit:]
        
        mails = []
        
        for mail_id, folder in reversed(latest_pairs):
            try:
                mail_id_str = mail_id.decode() if isinstance(mail_id, bytes) else str(mail_id)
                
                mail.select(folder)
                _, msg_data = mail.fetch(mail_id, "(RFC822)")
                
                for part in msg_data:
                    if isinstance(part, tuple):
                        msg = email.message_from_bytes(part[1])
                        
                        date_str = msg.get("Date", "")
                        send_time = ""
                        try:
                            from email.utils import parsedate_to_datetime
                            if date_str:
                                dt = parsedate_to_datetime(date_str)
                                send_time = dt.strftime("%Y-%m-%d %H:%M:%S")
                        except:
                            send_time = date_str[:30]
                        subject = decode_str(msg.get("Subject", "无主题"))
                        sender = decode_str(msg.get("From", "未知发件人"))
                        content = get_mail_content(msg)
                        
                        mails.append({
                            'mail_id': mail_id_str,
                            'sender': sender,
                            'subject': subject,
                            'content': content,
                            'time': send_time,
                            'folder': folder
                        })
                        break
            except Exception as e:
                print(f"读取单封邮件失败 (ID:{mail_id_str}, Folder:{folder}): {e}")
                continue
        
        return mails
        
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

# ===== 链接管理 =====
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
    except Exception as e:
        print(f"备份失败: {e}")

def load_used_emails():
    try:
        with open(USED_EMAILS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return {"records": {}}

def save_used_emails(data):
    with open(USED_EMAILS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def detect_email_type(email):
    if email.endswith("@foxmail.com"):
        return "foxmail"
    username = email.split("@")[0]
    if username.isdigit():
        return "数字"
    return "英文"

def assign_emails(type_name, quantity, buyer_id):
    all_emails = list(ACCOUNTS.keys())
    type_emails = [e for e in all_emails if detect_email_type(e) == type_name]
    
    if not type_emails:
        return None, f"类型 '{type_name}' 没有可用邮箱"
    
    used_data = load_used_emails()
    buyer_used = used_data.get("records", {}).get(buyer_id, [])
    available = [e for e in type_emails if e not in buyer_used]
    
    if len(available) < quantity:
        return None, f"类型 '{type_name}' 库存不足！需要 {quantity} 个，该买家还能领 {len(available)} 个"
    
    selected = random.sample(available, quantity)
    
    if buyer_id not in used_data["records"]:
        used_data["records"][buyer_id] = []
    used_data["records"][buyer_id].extend(selected)
    save_used_emails(used_data)
    
    return selected, None

# ===== 失效链接接口 =====
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

# ===== 登录 =====
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        pwd = request.form.get('password')
        if pwd == ADMIN_PASSWORD:
            session['logged_in'] = True
            return redirect('/admin')
        else:
            return '''
            <h2>密码错误</h2>
            <p><a href="/login">重新输入</a></p>
            '''
    
    return '''
    <!DOCTYPE html>
    <html>
    <head><meta charset="UTF-8"><title>后台登录</title></head>
    <body style="font-family: Arial; max-width: 400px; margin: 100px auto; padding: 20px;">
        <h2>后台登录</h2>
        <form method="post">
            <input type="password" name="password" placeholder="请输入密码" style="width:100%;padding:12px;font-size:16px;margin:10px 0;border:2px solid #ddd;border-radius:8px;">
            <button type="submit" style="width:100%;padding:12px;background:#4CAF50;color:white;border:none;font-size:16px;cursor:pointer;border-radius:8px;">登录</button>
        </form>
    </body>
    </html>
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
    
    link_data = links[link_id]
    if link_data.get('status') == 'disabled':
        return "链接已失效"
    
    email = link_data['emails'][0]
    auth_code = ACCOUNTS.get(email)
    
    if not auth_code:
        return "授权码不存在"
    
    try:
        mail = imaplib.IMAP4_SSL("imap.qq.com")
        mail.login(email, auth_code)
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
                                charset = p.get_content_charset() or 'utf-8'
                                try:
                                    html_content = payload.decode(charset, errors='replace')
                                except:
                                    html_content = payload.decode('utf-8', errors='replace')
                                break
                else:
                    payload = msg.get_payload(decode=True)
                    if payload:
                        charset = msg.get_content_charset() or 'utf-8'
                        try:
                            html_content = payload.decode(charset, errors='replace')
                        except:
                            html_content = payload.decode('utf-8', errors='replace')
                
                if not html_content:
                    html_content = "<pre>" + get_mail_content(msg) + "</pre>"
                
                html_content = html_content.replace('"', '&quot;').replace("'", '&#39;')
                
                mail.close()
                mail.logout()
                
                return f"""
                <!DOCTYPE html>
                <html>
                <head>
                    <meta charset="UTF-8">
                    <title>查看完整邮件</title>
                    <style>
                        body {{ margin: 0; padding: 0; background: #f0f0f0; font-family: Arial, sans-serif; }}
                        .header {{ background: #fff; padding: 15px 20px; border-bottom: 1px solid #ddd; }}
                        .header h2 {{ margin: 0; font-size: 18px; }}
                        .header .info {{ color: #666; font-size: 13px; margin-top: 5px; }}
                        iframe {{ width: 100%; min-height: 800px; border: none; background: white; }}
                        .back {{ display: inline-block; margin: 15px 20px; color: #667eea; text-decoration: none; }}
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
                """
        
        mail.close()
        mail.logout()
        return "邮件不存在"
        
    except Exception as e:
        return f"查看失败：{str(e)}"

# ===== 路由 =====
@app.route('/')
def index():
    return redirect('/admin')

@app.route('/admin')
def admin():
    if not session.get('logged_in'):
        return redirect('/login')
    
    links = load_links()
    used_data = load_used_emails()
    all_emails = list(ACCOUNTS.keys())
    
    total = len(all_emails)
    all_used = []
    for buyer, emails in used_data.get("records", {}).items():
        all_used.extend(emails)
    used = len(set(all_used))
    
    link_list = ""
    for link_id, data in links.items():
        status = data.get('status', 'active')
        status_text = '有效' if status == 'active' else '已失效'
        link_list += f"""
        <tr>
            <td>{link_id}</td>
            <td>{data.get('buyer_id', '未知')}</td>
            <td>{data.get('type', '未知')}</td>
            <td>{len(data['emails'])}</td>
            <td>{data['created_at']}</td>
            <td>{data['expire_at']}</td>
            <td>{status_text}</td>
            <td>
                <button onclick="disableLink('{link_id}')" style="padding:4px 12px;background:#e74c3c;color:white;border:none;border-radius:4px;cursor:pointer;">失效</button>
            </td>
        </tr>
        """
    
    html_admin = f'''
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
                <span>总邮箱：<strong>{total}</strong></span>
                <span>已分配：<strong>{used}</strong></span>
                <span>可用：<strong>{total - used}</strong></span>
            </div>
        </div>

        <div class="card">
            <h2>手动生成链接</h2>
            <div class="row">
                <div class="field">
                    <label>输入邮箱</label>
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
                    <tr><th>链接ID</th><th>买家</th><th>类型</th><th>数量</th><th>创建时间</th><th>过期时间</th><th>状态</th><th>操作</th></tr>
                    {link_list if link_list else '<tr><td colspan="8">暂无链接</td></tr>'}
                </table>
            </div>
        </div>
    </div>

    <script>
        async function generateManualLink() {
            var emailsText = document.getElementById('manualEmails').value.trim();
            var type = document.getElementById('emailType').value;
            var days = parseInt(document.getElementById('manualDays').value) || 30;

            if (!emailsText) {
                alert('请输入邮箱地址');
                return;
            }

            var emails = emailsText.split('\n').map(function(e) { return e.trim(); }).filter(function(e) { return e; });
            if (emails.length === 0) {
                alert('请输入有效邮箱地址');
                return;
            }

            var resultBox = document.getElementById('manualResultBox');
            var resultContent = document.getElementById('manualResultContent');
            resultBox.style.display = 'block';
            resultContent.innerHTML = '生成中...';

            try {
                var res = await fetch('/api/admin_create_link', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        emails: emails,
                        type: type,
                        days: days
                    })
                });
                var data = await res.json();

                if (data.error) {
                    resultContent.innerHTML = '<div style="color:red;">' + data.error + '</div>';
                    return;
                }

                var html = '<div style="font-weight:bold;margin-bottom:10px;">生成成功</div>';
                html += '<div style="margin-bottom:8px;">邮箱列表：</div>';
                for (var i = 0; i < data.emails.length; i++) {
                    html += '<div class="email-item">' + (i+1) + '. ' + data.emails[i] + '</div>';
                }
                html += '<div class="link-area">查询链接：<span style="color:#667eea;">' + data.link_url + '</span>';
                html += '<button class="copy-btn" onclick="copyText(\'' + data.link_url + '\')">复制链接</button></div>';
                html += '<div style="margin-top:8px;color:#999;font-size:13px;">有效期至：' + data.expire_at + '</div>';
                html += '<button onclick="alert(\'邮箱：' + data.emails.join('、') + '\\n查询链接：' + data.link_url + '\')" style="margin-top:12px;padding:8px 20px;background:#667eea;color:white;border:none;border-radius:6px;cursor:pointer;font-size:13px;">复制全部</button>';

                resultContent.innerHTML = html;
                location.reload();

            } catch (e) {
                resultContent.innerHTML = '<div style="color:red;">请求失败：' + e.message + '</div>';
            }
        }

        async function disableLink(linkId) {
            if (!confirm('确定要失效该链接吗？')) return;
            
            try {
                var res = await fetch('/api/disable_link', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({link_id: linkId})
                });
                var data = await res.json();
                if (data.success) {
                    alert('链接已失效');
                    location.reload();
                } else {
                    alert('操作失败：' + data.error);
                }
            } catch (e) {
                alert('请求失败');
            }
        }

        async function disableLinkByInput() {
            var linkId = document.getElementById('disableLinkInput').value.trim();
            if (!linkId) {
                alert('请输入链接ID');
                return;
            }
            
            if (!confirm('确定要失效链接 ' + linkId + ' 吗？')) return;
            
            try {
                var res = await fetch('/api/disable_link', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({link_id: linkId})
                });
                var data = await res.json();
                if (data.success) {
                    document.getElementById('disableResult').innerHTML = '<div style="color:green;font-weight:bold;">链接已失效</div>';
                    setTimeout(function(){ location.reload(); }, 1000);
                } else {
                    document.getElementById('disableResult').innerHTML = '<div style="color:red;">' + data.error + '</div>';
                }
            } catch (e) {
                document.getElementById('disableResult').innerHTML = '<div style="color:red;">请求失败</div>';
            }
        }

        function copyText(text) {
            navigator.clipboard.writeText(text).then(function() {
                alert('已复制');
            });
        }

        function copyAll(emails, link) {
            var text = '邮箱：' + emails.replace(/,/g, '、') + '\\n查询链接：' + link;
            navigator.clipboard.writeText(text).then(function() {
                alert('已复制全部内容');
            });
        }
    </script>
</body>
</html>
    '''
    return html_admin

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
    
    link_url = f"https://{DOMAIN}/query?link={link_id}"
    
    return jsonify({
        'success': True,
        'link_id': link_id,
        'link_url': link_url,
        'emails': emails,
        'expire_at': links[link_id]['expire_at']
    })

@app.route('/api/auto_create_link', methods=['POST'])
def auto_create_link():
    data = request.get_json() or {}
    
    type_name = data.get('type', '英文')
    
    try:
        quantity = int(data.get('quantity', 1))
        days = int(data.get('days', DEFAULT_DAYS))
    except (TypeError, ValueError):
        return "quantity 和 days 必须为整数", 400
    
    buyer_id = str(data.get('buyer_id') or str(uuid.uuid4())[:8])
    
    if quantity <= 0:
        return "数量必须大于0", 400
    
    valid_types = ['数字', '英文', 'foxmail']
    if type_name not in valid_types:
        return f"无效类型，请选择: {', '.join(valid_types)}", 400
    
    selected_emails, error = assign_emails(type_name, quantity, buyer_id)
    if error:
        return f"分配失败：{error}", 400
    
    link_id = str(uuid.uuid4())[:8]
    links = load_links()
    now = datetime.now()
    
    links[link_id] = {
        'link_id': link_id,
        'buyer_id': buyer_id,
        'type': type_name,
        'emails': selected_emails,
        'quantity': quantity,
        'created_at': now.strftime("%Y-%m-%d %H:%M:%S"),
        'expire_at': (now + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S"),
        'status': 'active',
        'query_count': 0
    }
    save_links(links)
    
    link_url = f"https://{DOMAIN}/query?link={link_id}"

    return f"""您购买的邮箱已发货

邮箱：
{chr(10).join(selected_emails)}

查询链接：{link_url}
有效期至：{links[link_id]['expire_at']}"""
    
@app.route('/query')
def query_page():
    link_id = request.args.get('link')
    if not link_id:
        return "缺少链接ID"
    
    links = load_links()
    if link_id not in links:
        return "链接不存在"
    
    link_data = links[link_id]
    
    if link_data.get('status') == 'disabled':
        return "链接已失效"
    
    now = datetime.now()
    expire_time = datetime.strptime(link_data['expire_at'], "%Y-%m-%d %H:%M:%S")
    
    if now > expire_time:
        return "链接已过期"
    
    if link_data['status'] != 'active':
        return "链接已被禁用"
    
    html_content = f'''
    <!DOCTYPE html>
    <html>
    <head><meta charset="UTF-8"><title>邮箱查询系统</title></head>
    <body style="font-family: Arial, sans-serif; max-width: 600px; margin: 50px auto; padding: 20px;">
        <div style="background: white; padding: 30px; border-radius: 12px; box-shadow: 0 2px 10px rgba(0,0,0,0.1);">
            <h2>邮箱查询系统</h2>
            <p>输入已绑定的邮箱，查看最新邮件</p>
            <p style="color: #999; font-size: 13px;">有效期至：{link_data['expire_at']}</p>
            <form action="/api/query_mail" method="post">
                <input type="hidden" name="link_id" value="{link_id}">
                <input type="text" name="email" placeholder="请输入邮箱地址" style="width:100%;padding:12px;font-size:16px;margin:10px 0;border:2px solid #ddd;border-radius:8px;">
                <button type="submit" style="width:100%;padding:12px;background:#4CAF50;color:white;border:none;font-size:16px;cursor:pointer;border-radius:8px;">查询邮件</button>
            </form>
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
    
    link_data = links[link_id]
    if email not in link_data['emails']:
        return f"该邮箱不在本链接中，可查询的邮箱：{', '.join(link_data['emails'])}"
    
    if link_data.get('status') == 'disabled':
        return "链接已失效"
    
    if email not in ACCOUNTS:
        return f"邮箱 {email} 未绑定"
    
    result = get_latest_mails(email, limit=1)
    
    if isinstance(result, dict) and 'error' in result:
        return f"查询失败：{result['error']}"
    
    if not result:
        return "<h3>暂无邮件</h3>"
    
    mail = result[0]
    html_result = f"""
    <h3>{email} 的最新邮件</h3>
    <div style="border:1px solid #ddd;padding:15px;margin:10px 0;">
        <b>发件人：</b>{mail['sender']}<br>
        <b>主题：</b>{mail['subject']}<br>
        <b>时间：</b>{mail.get('time', '')}<br>
        <hr>
        <div style="margin:10px 0;">{mail['content'][:1000]}</div>
        <br>
        <a href="/view_raw?link_id={link_id}&mail_id={mail['mail_id']}" target="_blank" style="color:#667eea;">查看完整邮件（含链接）</a>
    </div>
    """
    return html_result

@app.route('/api/groups', methods=['GET'])
def get_groups():
    all_emails = list(ACCOUNTS.keys())
    used_data = load_used_emails()
    all_used = []
    for buyer, emails in used_data.get("records", {}).items():
        all_used.extend(emails)
    
    types = ["数字", "英文", "foxmail"]
    result = []
    for t in types:
        type_emails = [e for e in all_emails if detect_email_type(e) == t]
        available = len([e for e in type_emails if e not in all_used])
        result.append({
            "name": t,
            "total": len(type_emails),
            "available": available
        })
    return jsonify(result)

@app.route('/api/admin_logs')
def api_admin_logs():
    return jsonify({"logs": []})

@app.route('/api/test_login', methods=['POST'])
def test_login():
    data = request.get_json()
    email = data.get('email')
    auth = data.get('auth')
    
    try:
        mail = imaplib.IMAP4_SSL("imap.qq.com")
        mail.login(email, auth)
        mail.select("INBOX")
        mail.close()
        mail.logout()
        return "登录成功"
    except Exception as e:
        return f"登录失败：{str(e)}"

if __name__ == '__main__':
    print("=" * 60)
    print("邮箱查询系统启动")
    print("=" * 60)
    print(f"已绑定 {len(ACCOUNTS)} 个邮箱")
    print("后台密码: 060910")
    print("访问 http://127.0.0.1:5000")
    print("=" * 60)
    app.run(host='0.0.0.0', port=8080)
