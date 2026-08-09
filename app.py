import os
os.environ['TZ'] = 'Asia/Shanghai'
try:
    import time
    time.tzset()
except:
    pass

from flask import Flask, request, jsonify, redirect, session
import imaplib
import email
import re
import html
import json
from email.header import decode_header
from email.utils import parsedate_to_datetime
import uuid
import random
from datetime import datetime, timedelta

app = Flask(__name__)
app.secret_key = 'your-secret-key-here'

LINKS_FILE = "links.json"
ACCOUNTS_FILE = "accounts.txt"
USED_EMAILS_FILE = "used_emails.json"
ADMIN_PASSWORD = "060910"
DEFAULT_DAYS = 30
DOMAIN = "mailauto.zeabur.app"

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
                        emails = parts[0:3]
                        auth = parts[3]
                        for email in emails:
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

def decode_str(s):
    if not s:
        return ""
    try:
        parts = []
        for part, charset in decode_header(s):
            if isinstance(part, bytes):
                if charset:
                    parts.append(part.decode(charset, errors='replace'))
                else:
                    parts.append(part.decode('utf-8', errors='replace'))
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
        else:
            payload = msg.get_payload(decode=True)
            if payload:
                content = payload.decode('utf-8', errors='replace')
    except:
        content = "解析失败"
    return content.strip()

def get_latest_mails(email_addr, limit=1):
    if email_addr not in ACCOUNTS:
        return {'error': f'邮箱 "{email_addr}" 未绑定'}
    
    auth_code = ACCOUNTS[email_addr]
    mail = None
    
    try:
        mail = imaplib.IMAP4_SSL("imap.qq.com")
        mail.login(email_addr, auth_code)
        
        all_mail_ids = []
        folders = ["INBOX", "垃圾箱", "广告邮件"]
        for folder in folders:
            try:
                mail.select(folder)
                status, data = mail.search(None, "ALL")
                if data[0]:
                    for mid in data[0].split():
                        all_mail_ids.append(mid)
            except:
                pass
        
        if not all_mail_ids:
            return []
        
        all_mail_ids = list(set(all_mail_ids))
        all_mail_ids.sort(key=lambda x: int(x))
        latest_ids = all_mail_ids[-limit:]
        
        mails = []
        for mail_id in reversed(latest_ids):
            try:
                mail.select("INBOX")
                _, msg_data = mail.fetch(mail_id, "(RFC822)")
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
                        subject = decode_str(msg.get("Subject", "无主题"))
                        sender = decode_str(msg.get("From", "未知发件人"))
                        content = get_mail_content(msg)
                        mails.append({
                            'mail_id': str(mail_id),
                            'sender': sender,
                            'subject': subject,
                            'content': content,
                            'time': send_time
                        })
                        break
            except:
                continue
        
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

def load_links():
    try:
        with open(LINKS_FILE, "r") as f:
            return json.load(f)
    except:
        return {}

def save_links(data):
    with open(LINKS_FILE, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    try:
        with open("links_backup.json", "w") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except:
        pass

def load_used_emails():
    try:
        with open(USED_EMAILS_FILE, "r") as f:
            return json.load(f)
    except:
        return {"records": {}}

def save_used_emails(data):
    with open(USED_EMAILS_FILE, "w") as f:
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
        return None, f"库存不足"
    selected = random.sample(available, quantity)
    if buyer_id not in used_data["records"]:
        used_data["records"][buyer_id] = []
    used_data["records"][buyer_id].extend(selected)
    save_used_emails(used_data)
    return selected, None

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

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        pwd = request.form.get('password')
        if pwd == ADMIN_PASSWORD:
            session['logged_in'] = True
            return redirect('/admin')
        else:
            return '<h2>密码错误</h2><p><a href="/login">重新输入</a></p>'
    
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
            <td><button onclick="disableLink('{link_id}')" style="padding:4px 12px;background:#e74c3c;color:white;border:none;border-radius:4px;cursor:pointer;">失效</button></td>
        </tr>
        """
    
    html_admin = f'''
    <!DOCTYPE html>
    <html>
    <head><meta charset="UTF-8"><title>邮箱管理后台</title></head>
    <body style="font-family: Arial; background: #f0f2f5; padding: 20px;">
        <div style="max-width: 1200px; margin: 0 auto;">
            <div style="background: white; padding: 24px; border-radius: 12px; margin-bottom: 20px;">
                <h2>邮箱管理后台 <a href="/logout" style="float:right;color:#e74c3c;text-decoration:none;">退出</a></h2>
                <div style="display:flex;gap:30px;flex-wrap:wrap;">
                    <span>总邮箱：<strong>{total}</strong></span>
                    <span>已分配：<strong>{used}</strong></span>
                    <span>可用：<strong>{total - used}</strong></span>
                </div>
            </div>
            
            <div style="background: white; padding: 24px; border-radius: 12px; margin-bottom: 20px;">
                <h3>手动生成链接</h3>
                <div style="display:flex;gap:12px;flex-wrap:wrap;align-items:end;">
                    <div>
                        <label>输入邮箱</label><br>
                        <textarea id="manualEmails" rows="5" style="width:350px;padding:10px;border:2px solid #ddd;border-radius:8px;"></textarea>
                    </div>
                    <div>
                        <label>选择类型</label><br>
                        <select id="emailType" style="padding:10px;border:2px solid #ddd;border-radius:8px;width:120px;">
                            <option value="数字">数字邮箱</option>
                            <option value="英文">英文邮箱</option>
                            <option value="foxmail">foxmail邮箱</option>
                        </select>
                    </div>
                    <div>
                        <label>有效期（天）</label><br>
                        <input type="number" id="manualDays" value="30" style="padding:10px;border:2px solid #ddd;border-radius:8px;width:80px;">
                    </div>
                    <button onclick="generateManualLink()" style="padding:10px 30px;background:#667eea;color:white;border:none;border-radius:8px;cursor:pointer;">生成链接</button>
                </div>
                <div id="manualResultBox" style="display:none;background:#f8f9fa;padding:16px;border-radius:8px;margin-top:16px;">
                    <div id="manualResultContent"></div>
                </div>
            </div>
            
            <div style="background: white; padding: 24px; border-radius: 12px; margin-bottom: 20px; border:2px solid #e74c3c;">
                <h3 style="color:#e74c3c;">输入链接ID使其失效</h3>
                <div style="display:flex;gap:12px;flex-wrap:wrap;align-items:end;">
                    <div>
                        <label>链接ID</label><br>
                        <input type="text" id="disableLinkInput" placeholder="例如：abc123" style="padding:10px;border:2px solid #ddd;border-radius:8px;width:250px;">
                    </div>
                    <button onclick="disableLinkByInput()" style="padding:10px 30px;background:#e74c3c;color:white;border:none;border-radius:8px;cursor:pointer;">失效</button>
                </div>
                <div id="disableResult" style="margin-top:10px;"></div>
            </div>
            
            <div style="background: white; padding: 24px; border-radius: 12px;">
                <h3>已生成的链接</h3>
                <table style="width:100%;border-collapse:collapse;font-size:13px;">
                    <tr>
                        <th style="text-align:left;padding:10px;border-bottom:2px solid #ddd;">链接ID</th>
                        <th style="text-align:left;padding:10px;border-bottom:2px solid #ddd;">买家</th>
                        <th style="text-align:left;padding:10px;border-bottom:2px solid #ddd;">类型</th>
                        <th style="text-align:left;padding:10px;border-bottom:2px solid #ddd;">数量</th>
                        <th style="text-align:left;padding:10px;border-bottom:2px solid #ddd;">创建时间</th>
                        <th style="text-align:left;padding:10px;border-bottom:2px solid #ddd;">过期时间</th>
                        <th style="text-align:left;padding:10px;border-bottom:2px solid #ddd;">状态</th>
                        <th style="text-align:left;padding:10px;border-bottom:2px solid #ddd;">操作</th>
                    </tr>
                    {link_list if link_list else '<tr><td colspan="8" style="text-align:center;padding:20px;">暂无链接</td></tr>'}
                </table>
            </div>
        </div>
        
        <script>
        function generateManualLink() {
            var emailsText = document.getElementById('manualEmails').value.trim();
            var type = document.getElementById('emailType').value;
            var days = parseInt(document.getElementById('manualDays').value) || 30;
            
            if (!emailsText) { alert('请输入邮箱地址'); return; }
            var emails = emailsText.split('\\n').map(function(e) { return e.trim(); }).filter(function(e) { return e; });
            if (emails.length === 0) { alert('请输入有效邮箱地址'); return; }
            
            var resultBox = document.getElementById('manualResultBox');
            var resultContent = document.getElementById('manualResultContent');
            resultBox.style.display = 'block';
            resultContent.innerHTML = '生成中...';
            
            fetch('/api/admin_create_link', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({emails: emails, type: type, days: days})
            })
            .then(function(res) { return res.json(); })
            .then(function(data) {
                if (data.error) {
                    resultContent.innerHTML = '<div style="color:red;">' + data.error + '</div>';
                    return;
                }
                var html = '<div style="font-weight:bold;margin-bottom:10px;">生成成功</div>';
                html += '<div style="margin-bottom:8px;">邮箱列表：</div>';
                for (var i = 0; i < data.emails.length; i++) {
                    html += '<div style="padding:4px 0;border-bottom:1px solid #eee;font-family:monospace;">' + (i+1) + '. ' + data.emails[i] + '</div>';
                }
                html += '<div style="background:#e8f5e9;padding:12px;border-radius:6px;margin-top:10px;word-break:break-all;">查询链接：<span style="color:#667eea;">' + data.link_url + '</span>';
                html += '<button onclick="copyText(\'' + data.link_url + '\')" style="padding:4px 12px;background:#667eea;color:white;border:none;border-radius:4px;cursor:pointer;margin-left:8px;">复制链接</button></div>';
                html += '<div style="margin-top:8px;color:#999;font-size:13px;">有效期至：' + data.expire_at + '</div>';
                html += '<button onclick="copyAll(\'' + data.emails.join(',') + '\', \'' + data.link_url + '\')" style="margin-top:12px;padding:8px 20px;background:#667eea;color:white;border:none;border-radius:6px;cursor:pointer;font-size:13px;">复制全部</button>';
                resultContent.innerHTML = html;
                setTimeout(function(){ location.reload(); }, 1500);
            })
            .catch(function(e) {
                resultContent.innerHTML = '<div style="color:red;">请求失败：' + e.message + '</div>';
            });
        }
        
        function disableLink(linkId) {
            if (!confirm('确定要失效该链接吗？')) return;
            fetch('/api/disable_link', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({link_id: linkId})
            })
            .then(function(res) { return res.json(); })
            .then(function(data) {
                if (data.success) { alert('链接已失效'); location.reload(); }
                else { alert('操作失败：' + data.error); }
            })
            .catch(function(e) { alert('请求失败'); });
        }
        
        function disableLinkByInput() {
            var linkId = document.getElementById('disableLinkInput').value.trim();
            if (!linkId) { alert('请输入链接ID'); return; }
            if (!confirm('确定要失效链接 ' + linkId + ' 吗？')) return;
            fetch('/api/disable_link', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({link_id: linkId})
            })
            .then(function(res) { return res.json(); })
            .then(function(data) {
                if (data.success) {
                    document.getElementById('disableResult').innerHTML = '<div style="color:green;font-weight:bold;">链接已失效</div>';
                    setTimeout(function(){ location.reload(); }, 1000);
                } else {
                    document.getElementById('disableResult').innerHTML = '<div style="color:red;">' + data.error + '</div>';
                }
            })
            .catch(function(e) {
                document.getElementById('disableResult').innerHTML = '<div style="color:red;">请求失败</div>';
            });
        }
        
        function copyText(text) {
            navigator.clipboard.writeText(text).then(function() { alert('已复制'); });
        }
        
        function copyAll(emails, link) {
            var text = '邮箱：' + emails.replace(/,/g, '、') + '\\n查询链接：' + link;
            navigator.clipboard.writeText(text).then(function() { alert('已复制全部内容'); });
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
    except:
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
    
    return '''
    <!DOCTYPE html>
    <html>
    <head><meta charset="UTF-8"><title>邮箱查询系统</title></head>
    <body style="font-family: Arial; max-width: 600px; margin: 50px auto; padding: 20px;">
        <div style="background: white; padding: 30px; border-radius: 12px; box-shadow: 0 2px 10px rgba(0,0,0,0.1);">
            <h2>邮箱查询系统</h2>
            <p>输入已绑定的邮箱，查看最新邮件</p>
            <p style="color: #999; font-size: 13px;">有效期至：''' + link_data['expire_at'] + '''</p>
            <form action="/api/query_mail" method="post">
                <input type="hidden" name="link_id" value="''' + link_id + '''">
                <input type="text" name="email" placeholder="请输入邮箱地址" style="width:100%;padding:12px;font-size:16px;margin:10px 0;border:2px solid #ddd;border-radius:8px;">
                <button type="submit" style="width:100%;padding:12px;background:#4CAF50;color:white;border:none;font-size:16px;cursor:pointer;border-radius:8px;">查询邮件</button>
            </form>
        </div>
    </body>
    </html>
    '''

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
        return f"该邮箱不在本链接中"
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
    return f"""
    <h3>{email} 的最新邮件</h3>
    <div style="border:1px solid #ddd;padding:15px;margin:10px 0;">
        <b>发件人：</b>{mail['sender']}<br>
        <b>主题：</b>{mail['subject']}<br>
        <b>时间：</b>{mail.get('time', '')}<br>
        <hr>
        <div style="margin:10px 0;">{mail['content'][:1000]}</div>
    </div>
    """

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
        result.append({"name": t, "total": len(type_emails), "available": available})
    return jsonify(result)

if __name__ == '__main__':
    print("=" * 60)
    print("邮箱查询系统启动")
    print("=" * 60)
    print(f"已绑定 {len(ACCOUNTS)} 个邮箱")
    print("后台密码: 060910")
    print("=" * 60)
    app.run(host='0.0.0.0', port=8080)
