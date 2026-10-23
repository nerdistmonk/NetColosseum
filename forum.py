import http.server
import urllib.parse
import os
import shutil
import time
import uuid
import re
import html
import io
from email.parser import BytesParser
from email.policy import default
from PIL import Image, ImageSequence

PORT = 8080
DATA_DIR = os.path.abspath("forum_data")
CONFIG_FILE = os.path.join(DATA_DIR, "forum_title.txt")
PIN_FILE = os.path.join(DATA_DIR, "admin_pin.txt")
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB limit
ALLOWED_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.gif', '.webp'}

# Ensure our base data folder exists
if not os.path.exists(DATA_DIR):
    os.makedirs(DATA_DIR)

def get_forum_title():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    return "NetColosseum"

def set_forum_title(title):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        f.write(title)

def get_admin_pin():
    if os.path.exists(PIN_FILE):
        with open(PIN_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    return "0000"  # Default initial PIN

def set_admin_pin(pin):
    with open(PIN_FILE, "w", encoding="utf-8") as f:
        f.write(pin)

def slugify(text):
    text = re.sub(r'[^\w\s-]', '', text).strip().lower()
    return re.sub(r'[-\s]+', '-', text) or 'thread'

def is_safe_path(base_dir, path):
    target = os.path.abspath(path)
    return os.path.commonpath([base_dir, target]) == base_dir

def linkify(text):
    safe_text = html.escape(text)
    url_pattern = re.compile(r'(https?://[^\s]+)')
    return url_pattern.sub(r'<a href="\1" target="_blank" rel="noopener">\1</a>', safe_text)

class FlatFileForumHandler(http.server.BaseHTTPRequestHandler):

    def do_GET(self):
        parsed_path = urllib.parse.urlparse(self.path)
        decoded_path = urllib.parse.unquote(parsed_path.path)
        path = decoded_path.strip('/')
        
        if not path:
            self.render_home()
        elif path.startswith("thread/"):
            thread_slug = path.split("thread/", 1)[1]
            query_params = urllib.parse.parse_qs(parsed_path.query)
            provided_pin = query_params.get('pin', [''])[0]
            self.render_thread(thread_slug, provided_pin)
        elif path == "back.jpg" and os.path.exists("back.jpg"):
            self.serve_file("back.jpg")
        elif path.startswith("media/"):
            file_subpath = path.split("media/", 1)[1]
            file_path = os.path.join(DATA_DIR, file_subpath)
            
            if is_safe_path(DATA_DIR, file_path) and os.path.exists(file_path) and os.path.isfile(file_path):
                self.serve_file(file_path)
            else:
                self.send_error(404, "File Not Found")
        else:
            self.send_error(404, "Page Not Found")

    def do_POST(self):
        content_length = int(self.headers.get('Content-Length', 0))
        
        if content_length > MAX_FILE_SIZE + (512 * 1024):
            self.send_error(413, "Payload Too Large (Max 10MB)")
            return

        content_type = self.headers.get('Content-Type', '')
        form_data = {}
        uploaded_file = None
        file_name_original = ""

        if 'multipart/form-data' in content_type:
            raw_data = self.rfile.read(content_length)
            msg_bytes = f"Content-Type: {content_type}\n\n".encode('utf-8') + raw_data
            msg = BytesParser(policy=default).parsebytes(msg_bytes)

            for part in msg.walk():
                disposition = part.get("Content-Disposition")
                if not disposition:
                    continue
                
                params = part.get_params(header='content-disposition')
                param_dict = {k: v for k, v in params} if params else {}
                field_name = param_dict.get('name')

                if part.get_content_maintype() == 'multipart':
                    continue

                if 'filename' in param_dict:
                    file_name_original = param_dict['filename']
                    uploaded_file = part.get_payload(decode=True)
                elif field_name:
                    form_data[field_name] = part.get_payload(decode=True).decode('utf-8', errors='ignore')

        elif 'application/x-www-form-urlencoded' in content_type:
            raw_data = self.rfile.read(content_length).decode('utf-8', errors='ignore')
            parsed = urllib.parse.parse_qs(raw_data)
            form_data = {k: v[0] for k, v in parsed.items()}
        else:
            self.send_error(400, "Bad Request")
            return

        action = form_data.get('action', '')
        admin_pin = get_admin_pin()

        if action == 'create_thread':
            title = form_data.get('title', '').strip()
            author = form_data.get('author', 'Anonymous').strip() or 'Anonymous'
            message = form_data.get('message', '').strip()
            thread_pin = form_data.get('thread_pin', '').strip()

            if title and message:
                thread_slug = f"{slugify(title)}-{int(time.time())}"
                thread_dir = os.path.join(DATA_DIR, thread_slug)
                
                if is_safe_path(DATA_DIR, thread_dir):
                    os.makedirs(thread_dir, exist_ok=True)
                    file_id = f"{int(time.time())}_{uuid.uuid4().hex[:6]}"
                    
                    file_content = f"TITLE:{title}\nAUTHOR:{author}\n"
                    if thread_pin:
                        file_content += f"PIN:{thread_pin}\n"
                    file_content += f"BODY:{message}\n"

                    with open(os.path.join(thread_dir, f"{file_id}.txt"), "w", encoding="utf-8") as f:
                        f.write(file_content)

                    if uploaded_file:
                        self.save_uploaded_image(thread_dir, file_id, uploaded_file, file_name_original)

                    redirect_url = f"/thread/{thread_slug}"
                    if thread_pin:
                        redirect_url += f"?pin={thread_pin}"
                    self.redirect(redirect_url)
                    return

        elif action == 'reply':
            thread_slug = form_data.get('thread_slug', '')
            author = form_data.get('author', 'Anonymous').strip() or 'Anonymous'
            message = form_data.get('message', '').strip()
            current_pin = form_data.get('current_pin', '').strip()

            thread_dir = os.path.join(DATA_DIR, thread_slug)
            if message and is_safe_path(DATA_DIR, thread_dir) and os.path.exists(thread_dir):
                file_id = f"{int(time.time())}_{uuid.uuid4().hex[:6]}"
                with open(os.path.join(thread_dir, f"{file_id}.txt"), "w", encoding="utf-8") as f:
                    f.write(f"AUTHOR:{author}\nBODY:{message}\n")

                if uploaded_file:
                    self.save_uploaded_image(thread_dir, file_id, uploaded_file, file_name_original)

                redirect_url = f"/thread/{thread_slug}"
                if current_pin:
                    redirect_url += f"?pin={current_pin}"
                self.redirect(redirect_url)
                return

        elif action == 'edit_forum_title':
            new_title = form_data.get('new_forum_title', '').strip()
            pin = form_data.get('pin', '').strip()

            if pin == admin_pin and new_title:
                set_forum_title(new_title)
            self.redirect("/")
            return

        elif action == 'edit_admin_pin':
            old_pin = form_data.get('old_pin', '').strip()
            new_pin = form_data.get('new_pin', '').strip()

            if old_pin == admin_pin and new_pin:
                set_admin_pin(new_pin)
            self.redirect("/")
            return

        elif action == 'edit_thread_title':
            thread_slug = form_data.get('thread_slug', '').strip()
            new_title = form_data.get('new_title', '').strip()
            username = form_data.get('username', '').strip()
            current_pin = form_data.get('current_pin', '').strip()

            thread_dir = os.path.join(DATA_DIR, thread_slug)
            if is_safe_path(DATA_DIR, thread_dir) and os.path.exists(thread_dir) and new_title and username:
                txt_files = sorted([f for f in os.listdir(thread_dir) if f.endswith('.txt')])
                if txt_files:
                    first_file_path = os.path.join(thread_dir, txt_files[0])
                    with open(first_file_path, 'r', encoding='utf-8') as f:
                        content = f.read()

                    author_match = re.search(r'^AUTHOR:(.*)$', content, re.M)
                    original_author = author_match.group(1).strip() if author_match else ""

                    if username.lower() == original_author.lower():
                        if re.search(r'^TITLE:.*$', content, re.M):
                            updated_content = re.sub(r'^TITLE:.*$', f'TITLE:{new_title}', content, flags=re.M)
                        else:
                            updated_content = f'TITLE:{new_title}\n' + content

                        with open(first_file_path, 'w', encoding='utf-8') as f:
                            f.write(updated_content)

            redirect_url = f"/thread/{thread_slug}"
            if current_pin:
                redirect_url += f"?pin={current_pin}"
            self.redirect(redirect_url)
            return

        elif action == 'delete_thread':
            thread_slug = form_data.get('thread_slug', '').strip()
            pin = form_data.get('pin', '').strip()

            if pin == admin_pin:
                thread_dir = os.path.join(DATA_DIR, thread_slug)
                if is_safe_path(DATA_DIR, thread_dir) and os.path.exists(thread_dir):
                    shutil.rmtree(thread_dir)
            self.redirect("/")
            return

        elif action == 'delete_post':
            thread_slug = form_data.get('thread_slug', '').strip()
            file_id = form_data.get('file_id', '').strip()
            pin = form_data.get('pin', '').strip()
            current_pin = form_data.get('current_pin', '').strip()

            if pin == admin_pin and file_id:
                thread_dir = os.path.join(DATA_DIR, thread_slug)
                if is_safe_path(DATA_DIR, thread_dir) and os.path.exists(thread_dir):
                    txt_path = os.path.join(thread_dir, f"{file_id}.txt")
                    if os.path.exists(txt_path):
                        os.remove(txt_path)
                    for ext in ALLOWED_EXTENSIONS:
                        img_path = os.path.join(thread_dir, f"{file_id}{ext}")
                        if os.path.exists(img_path):
                            os.remove(img_path)

            redirect_url = f"/thread/{thread_slug}"
            if current_pin:
                redirect_url += f"?pin={current_pin}"
            self.redirect(redirect_url)
            return

        self.redirect("/")

    def save_uploaded_image(self, thread_dir, file_id, file_bytes, original_filename):
        if len(file_bytes) > MAX_FILE_SIZE:
            return

        ext = os.path.splitext(original_filename)[1].lower()
        if ext not in ALLOWED_EXTENSIONS:
            return

        image_filename = f"{file_id}{ext}"
        image_path = os.path.join(thread_dir, image_filename)
        
        if is_safe_path(DATA_DIR, image_path):
            try:
                img = Image.open(io.BytesIO(file_bytes))
                img.verify() # Check if it's a valid, uncorrupted image matrix
                
                # Re-open for saving
                img = Image.open(io.BytesIO(file_bytes))
                
                # Preserve animated frames if it's a multi-frame GIF
                if getattr(img, "is_animated", False):
                    frames = []
                    durations = []
                    loops = img.info.get('loop', 0)
                    
                    for frame in ImageSequence.Iterator(img):
                        frames.append(frame.copy())
                        durations.append(frame.info.get('duration', 100))
                    
                    frames[0].save(
                        image_path,
                        save_all=True,
                        append_images=frames[1:],
                        duration=durations,
                        loop=loops,
                        optimize=False
                    )
                else:
                    img.save(image_path)
            except Exception:
                pass

    def serve_file(self, file_path):
        ext = os.path.splitext(file_path)[1].lower()
        content_types = {
            '.png': 'image/png',
            '.jpg': 'image/jpeg',
            '.jpeg': 'image/jpeg',
            '.gif': 'image/gif',
            '.webp': 'image/webp'
        }
        ct = content_types.get(ext, 'application/octet-stream')
        
        with open(file_path, 'rb') as f:
            data = f.read()
        
        self.send_response(200)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def redirect(self, url):
        self.send_response(303)
        self.send_header('Location', url)
        self.end_headers()

    def render_home(self):
        forum_name = get_forum_title()
        threads = []
        if os.path.exists(DATA_DIR):
            for dir_name in os.listdir(DATA_DIR):
                thread_path = os.path.join(DATA_DIR, dir_name)
                if os.path.isdir(thread_path) and is_safe_path(DATA_DIR, thread_path):
                    files = sorted([f for f in os.listdir(thread_path) if f.endswith('.txt')])
                    if files:
                        with open(os.path.join(thread_path, files[0]), 'r', encoding='utf-8') as f:
                            content = f.read()
                            title_match = re.search(r'^TITLE:(.*)$', content, re.M)
                            author_match = re.search(r'^AUTHOR:(.*)$', content, re.M)
                            has_pin = bool(re.search(r'^PIN:.*$', content, re.M))
                            title = title_match.group(1) if title_match else dir_name
                            author = author_match.group(1) if author_match else 'Anonymous'
                            
                        all_files = os.listdir(thread_path)
                        reply_count = len([f for f in all_files if f.endswith('.txt')]) - 1
                        threads.append({
                            'slug': dir_name,
                            'title': html.escape(title),
                            'author': html.escape(author),
                            'replies': max(0, reply_count),
                            'locked': has_pin
                        })

        threads_html = ""
        for t in threads:
            lock_icon = " 🔒" if t['locked'] else ""
            threads_html += f"""
            <div class="card">
                <h3><a href="/thread/{t['slug']}">{t['title']}</a>{lock_icon}</h3>
                <p class="meta">Started by <strong>{t['author']}</strong> | Replies: {t['replies']}</p>
            </div>
            """

        if not threads_html:
            threads_html = "<p>No threads yet. Be the first to start one below!</p>"

        body = f"""
        <div class="header-flex">
            <h1 style="margin: 0;">🏛️ {html.escape(forum_name)}</h1>
            <div class="header-controls">
                <details class="gear-menu">
                    <summary title="Change Admin Code">#</summary>
                    <div class="gear-dropdown">
                        <form method="POST">
                            <input type="hidden" name="action" value="edit_admin_pin">
                            <p style="margin-top:0;"><input type="password" name="old_pin" placeholder="Current PIN" maxlength="4" required></p>
                            <p><input type="password" name="new_pin" placeholder="New PIN" maxlength="4" required></p>
                            <p style="margin-bottom:0;"><button type="submit" class="secondary-btn" style="width:100%;">Update PIN</button></p>
                        </form>
                    </div>
                </details>
                <details class="gear-menu">
                    <summary title="Settings">⚙️</summary>
                    <div class="gear-dropdown">
                        <form method="POST">
                            <input type="hidden" name="action" value="edit_forum_title">
                            <p style="margin-top:0;"><input type="text" name="new_forum_title" placeholder="New Forum Name" required></p>
                            <p><input type="password" name="pin" placeholder="Admin PIN" maxlength="4" required></p>
                            <p style="margin-bottom:0;"><button type="submit" class="secondary-btn" style="width:100%;">Save</button></p>
                        </form>
                    </div>
                </details>
            </div>
        </div>

        <div class="section" style="margin-top: 30px;">
            <h2>Threads</h2>
            {threads_html}
        </div>
        <div class="section">
            <h2>Start a New Thread</h2>
            <form method="POST" enctype="multipart/form-data">
                <input type="hidden" name="action" value="create_thread">
                <p><input type="text" name="author" placeholder="Your Name" required></p>
                <p><input type="text" name="title" placeholder="Thread Title" required></p>
                <p><textarea name="message" placeholder="What's on your mind? (URLs like https://... become links)" rows="4" required></textarea></p>
                <p><input type="password" name="thread_pin" placeholder="Optional Private Thread Pincode (hides contents)" style="max-width: 300px;"></p>
                <p><label class="file-label">Attach Image (Max 10MB):<br><input type="file" name="image" accept=".png,.jpg,.jpeg,.gif,.webp"></label></p>
                <p><button type="submit">Create Thread</button></p>
            </form>
        </div>
        """
        self.send_html(body)

    def render_thread(self, thread_slug, provided_pin):
        thread_dir = os.path.join(DATA_DIR, thread_slug)
        if not is_safe_path(DATA_DIR, thread_dir) or not os.path.exists(thread_dir):
            self.send_error(404, "Thread Not Found")
            return

        txt_files = sorted([f for f in os.listdir(thread_dir) if f.endswith('.txt')])
        if not txt_files:
            self.send_error(404, "Thread Empty")
            return

        first_file_path = os.path.join(thread_dir, txt_files[0])
        with open(first_file_path, 'r', encoding='utf-8') as f:
            first_content = f.read()

        pin_match = re.search(r'^PIN:(.*)$', first_content, re.M)
        if pin_match:
            required_pin = pin_match.group(1).strip()
            if provided_pin != required_pin:
                title_match = re.search(r'^TITLE:(.*)$', first_content, re.M)
                thread_title = title_match.group(1) if title_match else thread_slug
                
                body = f"""
                <p><a href="/">&larr; Back to Forum Home</a></p>
                <h1>🔒 {html.escape(thread_title)} (Protected)</h1>
                <div class="card" style="text-align: center; padding: 30px;">
                    <p>This thread is private and protected with a pincode.</p>
                    <form method="GET" action="/thread/{thread_slug}">
                        <p><input type="password" name="pin" placeholder="Enter Thread Pincode" required style="max-width: 250px; margin: 0 auto; display: block;"></p>
                        <p><button type="submit">Unlock Thread</button></p>
                    </form>
                </div>
                """
                self.send_html(body)
                return

        posts_html = ""
        thread_title = "Untitled Thread"

        for idx, filename in enumerate(txt_files):
            file_id = filename.split('.')[0]
            file_path = os.path.join(thread_dir, filename)
            
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
                
                title_match = re.search(r'^TITLE:(.*)$', content, re.M)
                if title_match and idx == 0:
                    thread_title = title_match.group(1)

                author_match = re.search(r'^AUTHOR:(.*)$', content, re.M)
                author = author_match.group(1) if author_match else "Anonymous"

                body_parts = content.split("BODY:", 1)
                body_text = body_parts[1].strip() if len(body_parts) > 1 else ""

                formatted_body = linkify(body_text).replace('\n', '<br>')

                image_html = ""
                for ext in ALLOWED_EXTENSIONS:
                    img_filename = f"{file_id}{ext}"
                    img_path = os.path.join(thread_dir, img_filename)
                    if os.path.exists(img_path):
                        image_html = f'<div class="post-image"><img src="/media/{thread_slug}/{img_filename}" alt="Attached Image"></div>'
                        break

                delete_post_html = f"""
                <details class="delete-post-details">
                    <summary title="Delete Post">🗑️</summary>
                    <form method="POST" style="margin-top: 5px;">
                        <input type="hidden" name="action" value="delete_post">
                        <input type="hidden" name="thread_slug" value="{thread_slug}">
                        <input type="hidden" name="file_id" value="{file_id}">
                        <input type="hidden" name="current_pin" value="{provided_pin}">
                        <input type="password" name="pin" placeholder="Admin PIN" maxlength="4" required style="width: 120px; padding: 4px; display: inline-block;">
                        <button type="submit" class="danger-btn" style="padding: 4px 10px; font-size: 0.85em;">Delete</button>
                    </form>
                </details>
                """

                posts_html += f"""
                <div class="card post" style="position: relative;">
                    <div style="display: flex; justify-content: space-between; align-items: flex-start;">
                        <p class="meta" style="margin-top: 0;"><strong>{html.escape(author)}</strong> posted:</p>
                        {delete_post_html}
                    </div>
                    <div class="post-body">{formatted_body}</div>
                    {image_html}
                </div>
                """

        body = f"""
        <p><a href="/">&larr; Back to Forum Home</a></p>
        <h1>🔒 {html.escape(thread_title)}</h1>
        
        <div class="edit-box-container">
            <div class="edit-box">
                <details>
                    <summary>✏️ Edit Thread Title (Original Creator Only)</summary>
                    <form method="POST" style="margin-top: 10px;">
                        <input type="hidden" name="action" value="edit_thread_title">
                        <input type="hidden" name="thread_slug" value="{thread_slug}">
                        <input type="hidden" name="current_pin" value="{provided_pin}">
                        <p><input type="text" name="new_title" placeholder="New Thread Title" required></p>
                        <p><input type="text" name="username" placeholder="Confirm Your Exact Creator Username" required></p>
                        <p><button type="submit" class="secondary-btn">Save Thread Title</button></p>
                    </form>
                </details>
            </div>
            
            <div class="edit-box" style="margin-top: 10px;">
                <details>
                    <summary style="color: #cc0000;">🗑️ Delete Entire Thread (Admin PIN Required)</summary>
                    <form method="POST" style="margin-top: 10px;">
                        <input type="hidden" name="action" value="delete_thread">
                        <input type="hidden" name="thread_slug" value="{thread_slug}">
                        <p><input type="password" name="pin" placeholder="Admin PIN" maxlength="4" required style="max-width: 200px;"></p>
                        <p><button type="submit" class="danger-btn">Permanently Delete Thread</button></p>
                    </form>
                </details>
            </div>
        </div>

        <div class="section" style="margin-top: 20px;">
            {posts_html}
        </div>
        <div class="section">
            <h3>Post a Reply</h3>
            <form method="POST" enctype="multipart/form-data">
                <input type="hidden" name="action" value="reply">
                <input type="hidden" name="thread_slug" value="{thread_slug}">
                <input type="hidden" name="current_pin" value="{provided_pin}">
                <p><input type="text" name="author" placeholder="Your Name" required></p>
                <p><textarea name="message" placeholder="Write your reply... (URLs become links)" rows="4" required></textarea></p>
                <p><label class="file-label">Attach Image (Max 10MB):<br><input type="file" name="image" accept=".png,.jpg,.jpeg,.gif,.webp"></label></p>
                <p><button type="submit">Post Reply</button></p>
            </form>
        </div>
        """
        self.send_html(body)

    def send_html(self, content):
        self.send_response(200)
        self.send_header("Content-type", "text/html; charset=utf-8")
        self.end_headers()
        
        html_style = ""
        if os.path.exists("back.jpg"):
            html_style = "background-image: url('/back.jpg'); background-size: cover; background-attachment: fixed; background-position: center;"

        full_page = f"""<!DOCTYPE html>
        <html>
        <head>
            <meta charset="utf-8">
            <title>{html.escape(get_forum_title())}</title>
            <style>
                html {{ min-height: 100%; {html_style} }}
                body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 700px; margin: 40px auto; padding: 20px; line-height: 1.6; background: #fff; color: #333; border-radius: 8px; box-shadow: 0 4px 20px rgba(0,0,0,0.15); }}
                h1, h2, h3 {{ color: #111; }}
                a {{ color: #0066cc; text-decoration: none; }}
                a:hover {{ text-decoration: underline; }}
                .card {{ background: #fff; padding: 15px 20px; border-radius: 6px; border: 1px solid #ddd; margin-bottom: 15px; }}
                .meta {{ font-size: 0.85em; color: #666; margin-top: 5px; }}
                .section {{ margin-bottom: 40px; }}
                input[type="text"], input[type="password"], textarea {{ width: 100%; padding: 10px; border: 1px solid #ccc; border-radius: 4px; box-sizing: border-box; margin-top: 5px; font-family: inherit; }}
                .file-label {{ font-size: 0.9em; color: #555; display: block; margin-top: 10px; }}
                input[type="file"] {{ margin-top: 5px; }}
                button {{ background: #0066cc; color: white; border: none; padding: 10px 18px; border-radius: 4px; cursor: pointer; font-size: 1em; }}
                button:hover {{ background: #0052a3; }}
                .secondary-btn {{ background: #555; }}
                .secondary-btn:hover {{ background: #333; }}
                .danger-btn {{ background: #cc0000; color: white; border: none; padding: 10px 18px; border-radius: 4px; cursor: pointer; font-size: 1em; }}
                .danger-btn:hover {{ background: #990000; }}
                .post-image img {{ max-width: 100%; max-height: 400px; border-radius: 4px; margin-top: 12px; border: 1px solid #eee; display: block; }}
                .edit-box {{ background: #fff; padding: 10px 15px; border-radius: 6px; border: 1px solid #ddd; }}
                .edit-box summary {{ cursor: pointer; color: #0066cc; font-weight: 500; font-size: 0.9em; }}
                .header-flex {{ display: flex; align-items: center; justify-content: space-between; }}
                .header-controls {{ display: flex; gap: 10px; align-items: center; }}
                .gear-menu {{ position: relative; }}
                .gear-menu summary {{ list-style: none; cursor: pointer; font-size: 1.3em; padding: 5px; user-select: none; }}
                .gear-menu summary::-webkit-details-marker {{ display: none; }}
                .gear-dropdown {{ position: absolute; right: 0; background: #fff; border: 1px solid #ddd; padding: 15px; border-radius: 6px; box-shadow: 0 4px 12px rgba(0,0,0,0.1); z-index: 100; width: 240px; margin-top: 5px; }}
                .delete-post-details summary {{ list-style: none; cursor: pointer; font-size: 1.1em; padding: 2px 6px; user-select: none; }}
                .delete-post-details summary::-webkit-details-marker {{ display: none; }}
                .delete-post-details form {{ position: absolute; right: 0; background: #fff; border: 1px solid #ddd; padding: 10px; border-radius: 6px; box-shadow: 0 4px 12px rgba(0,0,0,0.1); z-index: 50; width: 200px; }}
            </style>
        </head>
        <body>
            {content}
        </body>
        </html>"""
        self.wfile.write(full_page.encode("utf-8"))

if __name__ == "__main__":
    server = http.server.HTTPServer(('localhost', PORT), FlatFileForumHandler)
    print(f"NetColosseum running securely at http://localhost:{PORT}")
    server.serve_forever()