import os
import ast
import json
import operator
import urllib.request
import urllib.parse
import urllib.error
import imaplib
import email
import traceback
from datetime import datetime
from email.header import decode_header
from dotenv import load_dotenv
from flask import Flask, request, jsonify, render_template, session, redirect, url_for

# 1. Setup Flask App
app = Flask(__name__)
# Fixed fallback secret key so login sessions survive across Vercel serverless invocations
app.secret_key = os.getenv("SECRET_KEY", "artic-ai-vercel-secret-key-2026")

# 2. Load Credentials (local .env locally; Vercel dashboard Environment Variables in production)
env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(env_path):
    load_dotenv(dotenv_path=env_path)

AGENT_NAME = "Artic"
api_key = os.getenv("GEMINI_API_KEY")
SENDER_EMAIL = os.getenv("SENDER_EMAIL")
APP_PASSWORD = os.getenv("APP_PASSWORD")
BREVO_API_KEY = os.getenv("BREVO_API_KEY")
SUPABASE_URL = (os.getenv("SUPABASE_URL") or "").rstrip("/")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
LOGIN_PASSWORD = os.getenv("LOGIN_PASSWORD", "artic123")

# Safe client initialization so missing/delayed env vars never crash the login page on boot
client = None
if api_key:
    try:
        from google import genai
        client = genai.Client(api_key=api_key, http_options={'timeout': 35000})
    except Exception as e:
        print(f"Gemini client init warning: {e}")

# 3. Setup Writable Fallback Folders (/tmp is the only writable directory on Vercel)
BASE_TMP = "/tmp" if os.path.exists("/tmp") else os.path.dirname(os.path.abspath(__file__))
NOTES_DIR = os.path.join(BASE_TMP, "notes")
try:
    os.makedirs(NOTES_DIR, exist_ok=True)
except Exception:
    pass
MEMORY_FILE = os.path.join(NOTES_DIR, "memory.txt")

# --- SUPABASE REST HELPER ---
def supabase_request(endpoint: str, method: str = "GET", payload=None, extra_headers=None):
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise RuntimeError("SUPABASE_URL or SUPABASE_KEY is missing in environment variables.")
    url = f"{SUPABASE_URL}/rest/v1/{endpoint}"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json"
    }
    if extra_headers:
        headers.update(extra_headers)
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            body = response.read().decode("utf-8")
            return json.loads(body) if body else []
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"Supabase HTTP {e.code}: {detail}")

# --- CLOUD-READY TOOLS ---
def calculate_math(expression: str) -> str:
    """Safely evaluates mathematical expressions (e.g., '23456 * 435354345 / 3456677')."""
    operators = {
        ast.Add: operator.add, ast.Sub: operator.sub,
        ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.Pow: operator.pow, ast.Mod: operator.mod,
        ast.USub: operator.neg
    }
    def eval_node(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        elif isinstance(node, ast.BinOp) and type(node.op) in operators:
            return operators[type(node.op)](eval_node(node.left), eval_node(node.right))
        elif isinstance(node, ast.UnaryOp) and type(node.op) in operators:
            return operators[type(node.op)](eval_node(node.operand))
        raise ValueError("Unsupported mathematical expression")

    try:
        clean_expr = expression.replace("^", "**").replace("×", "*").replace("÷", "/")
        result = eval_node(ast.parse(clean_expr, mode='eval').body)
        return f"Exact result: {result:,.6f}".rstrip('0').rstrip('.')
    except Exception as e:
        return f"Calculation error: {e}"

def get_weather(city: str) -> str:
    try:
        url = f"https://wttr.in/{urllib.parse.quote(city)}?format=3"
        req = urllib.request.Request(url, headers={'User-Agent': 'curl/7.68.0'})
        with urllib.request.urlopen(req, timeout=10) as response:
            return response.read().decode('utf-8').strip()
    except Exception as e:
        return f"Weather error: {e}"

def search_web(query: str) -> str:
    try:
        from ddgs import DDGS
        results = DDGS().text(query, max_results=4)
        return str(results) if results else "No results found."
    except Exception as e:
        return f"Search error: {e}"

def send_email(recipient_email: str, subject: str, body: str) -> str:
    if not BREVO_API_KEY:
        return "Failed to send email: BREVO_API_KEY is missing in environment variables."
    if not SENDER_EMAIL:
        return "Failed to send email: SENDER_EMAIL is missing in environment variables."

    try:
        url = "https://api.brevo.com/v3/smtp/email"
        payload = {
            "sender": {
                "name": "Amarendra",
                "email": SENDER_EMAIL
            },
            "to": [
                {
                    "email": recipient_email
                }
            ],
            "subject": subject,
            "textContent": body.replace('\\n', '\n')
        }
        headers = {
            "accept": "application/json",
            "api-key": BREVO_API_KEY,
            "content-type": "application/json"
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=15) as response:
            if response.status in (200, 201, 202):
                return f"Email successfully sent to {recipient_email}."
            return f"Brevo API returned status {response.status}."
    except urllib.error.HTTPError as e:
        error_detail = e.read().decode("utf-8", errors="ignore")
        return f"Failed to send email. Brevo API Error ({e.code}): {error_detail}"
    except Exception as e:
        return f"Failed to send email. Error: {e}"

def check_emails(max_emails: int = 5) -> str:
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com", timeout=25)
        mail.login(SENDER_EMAIL, APP_PASSWORD)
        mail.select("inbox", readonly=True)
        status, messages = mail.search(None, "UNSEEN")
        email_ids = messages[0].split()

        if not email_ids:
            mail.logout()
            return "You have no unread emails in your inbox."

        latest_ids = email_ids[-max_emails:]
        summaries = []

        for e_id in reversed(latest_ids):
            res, msg_data = mail.fetch(e_id, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT)] BODY.PEEK[1]<0.250>)")
            sender = "Unknown Sender"
            subject = "(No Subject)"
            preview = ""
            for part in msg_data:
                if isinstance(part, tuple):
                    header_label = part[0].decode(errors="ignore").upper()
                    raw_bytes = part[1]
                    if "HEADER" in header_label:
                        msg = email.message_from_bytes(raw_bytes)
                        raw_sub, enc = decode_header(msg["Subject"] or "(No Subject)")[0]
                        subject = raw_sub.decode(enc or "utf-8", errors="ignore") if isinstance(raw_sub, bytes) else raw_sub
                        sender = msg.get("From", "Unknown Sender")
                    else:
                        preview = " ".join(raw_bytes.decode("utf-8", errors="ignore").split())[:150]
            summaries.append(f"From: {sender} | Subject: {subject} | Preview: {preview}...")
        mail.logout()
        return "\n---\n".join(summaries)
    except Exception as e:
        return f"Error reading inbox: {e}"

def save_note(filename: str, content: str) -> str:
    if not filename.endswith(".txt"):
        filename += ".txt"
    clean_name = os.path.basename(filename)
    clean_content = content.replace('\\n', '\n')

    if SUPABASE_URL and SUPABASE_KEY:
        try:
            supabase_request(
                "notes?on_conflict=filename",
                method="POST",
                payload={
                    "filename": clean_name,
                    "content": clean_content,
                    "updated_at": datetime.utcnow().isoformat()
                },
                extra_headers={"Prefer": "resolution=merge-duplicates,return=minimal"}
            )
            return f"Saved '{clean_name}' to Supabase cloud notes."
        except Exception as e:
            return f"Error saving cloud note: {e}"

    filepath = os.path.join(NOTES_DIR, clean_name)
    try:
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(clean_content)
        return f"Saved '{clean_name}' to temporary notes (Supabase keys not set)."
    except Exception as e:
        return f"Error saving note: {e}"

def read_note(filename: str) -> str:
    if not filename.endswith(".txt"):
        filename += ".txt"
    clean_name = os.path.basename(filename)

    if SUPABASE_URL and SUPABASE_KEY:
        try:
            encoded = urllib.parse.quote(clean_name, safe="")
            rows = supabase_request(f"notes?filename=eq.{encoded}&select=content")
            if not rows:
                return f"Note '{clean_name}' does not exist."
            return rows[0]["content"]
        except Exception as e:
            return f"Error reading cloud note: {e}"

    filepath = os.path.join(NOTES_DIR, clean_name)
    try:
        if not os.path.exists(filepath):
            return f"Note '{clean_name}' does not exist."
        with open(filepath, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        return f"Error reading note: {e}"

def list_notes() -> str:
    if SUPABASE_URL and SUPABASE_KEY:
        try:
            rows = supabase_request("notes?select=filename&order=updated_at.desc")
            files = [r["filename"] for r in rows]
            return f"Saved cloud notes: {', '.join(files)}" if files else "No notes saved yet."
        except Exception as e:
            return f"Error listing cloud notes: {e}"

    try:
        files = [f for f in os.listdir(NOTES_DIR) if f.endswith(".txt") and f != "memory.txt"]
        return f"Saved notes: {', '.join(files)}" if files else "No notes saved yet."
    except Exception as e:
        return f"Error listing notes: {e}"

def get_current_time() -> str:
    return datetime.now().strftime("Current Date & Time: %A, %B %d, %Y at %I:%M %p")

def remember_fact(fact: str) -> str:
    if SUPABASE_URL and SUPABASE_KEY:
        try:
            supabase_request(
                "memories",
                method="POST",
                payload={"fact": fact},
                extra_headers={"Prefer": "return=minimal"}
            )
            return f"Successfully remembered in Supabase cloud memory: {fact}"
        except Exception as e:
            return f"Error saving cloud memory: {e}"

    try:
        with open(MEMORY_FILE, "a", encoding="utf-8") as f:
            f.write(f"- {fact}\n")
        return f"Successfully remembered locally (Supabase keys not set): {fact}"
    except Exception as e:
        return f"Error saving memory: {e}"

# 4. Read Memory & Configure Agent
def load_long_term_memory() -> str:
    if SUPABASE_URL and SUPABASE_KEY:
        try:
            rows = supabase_request("memories?select=fact&order=id.asc")
            return "\n".join(f"- {r['fact']}" for r in rows if r.get("fact"))
        except Exception as e:
            print(f"Warning: Could not load cloud memory: {e}")

    if os.path.exists(MEMORY_FILE):
        with open(MEMORY_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    return ""

def get_system_prompt() -> str:
    long_term_memory = load_long_term_memory()
    rules = (
        f"Your name is {AGENT_NAME}. You are Amarendra's personal cloud AI assistant with live internet, Gmail, cloud notes, and a math calculator. "
        "Keep responses conversational, sharp, and well-formatted. "
        "Always use calculate_math for any arithmetic or equations. "
        "MANDATORY TOOL RULES: "
        "1. Whenever the user asks you to 'remember', 'note down', or 'keep in mind' any fact or preference, you MUST immediately call the `remember_fact` tool—never just say 'Got it' without calling the tool first. "
        "2. Whenever the user asks you to save, read, or list notes, you MUST call `save_note`, `read_note`, or `list_notes`. "
        "3. If any tool returns an error message, report the exact error to the user so it can be fixed. "
        "4. If asked for a 'briefing', 'update', or 'status', autonomously: check time, check live weather for Hyderabad, and check latest unread emails, then summarize naturally."
    )
    if long_term_memory:
        rules += f"\nLong-term memory facts:\n{long_term_memory}"
    return rules

AGENT_TOOLS = [calculate_math, get_weather, search_web, send_email, check_emails, save_note, read_note, list_notes, get_current_time, remember_fact]
MODELS = ['gemini-3.8-flash', 'gemini-3.5-flash-lite', 'gemini-3.8-pro']
active_model_index = 0
chat_session = None

def build_chat_session(model_name: str, history=None):
    from google.genai import types
    return client.chats.create(
        model=model_name,
        history=history,
        config=types.GenerateContentConfig(
            system_instruction=get_system_prompt(),
            tools=AGENT_TOOLS
        )
    )

# 5. Web Routes & Authentication
@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        if request.form.get('password') == LOGIN_PASSWORD:
            session['authenticated'] = True
            return redirect(url_for('home'))
        error = "Invalid access key."
    return render_template('login.html', error=error)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/')
def home():
    if not session.get('authenticated'):
        return redirect(url_for('login'))
    return render_template('index.html')

@app.route('/chat', methods=['POST'])
def chat():
    global chat_session, active_model_index
    if not session.get('authenticated'):
        return jsonify({"reply": "🔒 Session expired. Please refresh and log in again."}), 401

    if not client:
        return jsonify({"reply": "⚠️ GEMINI_API_KEY is missing in Vercel Environment Variables."}), 500

    user_input = (request.json or {}).get("message")
    if not user_input:
        return jsonify({"reply": "I didn't catch that."})

    if chat_session is None:
        chat_session = build_chat_session(MODELS[active_model_index])

    errors = []
    for offset in range(len(MODELS)):
        idx = (active_model_index + offset) % len(MODELS)
        if idx != active_model_index:
            active_model_index = idx
            chat_session = build_chat_session(MODELS[idx], history=chat_session.get_history())
        try:
            response = chat_session.send_message(user_input)
            return jsonify({"reply": response.text})
        except Exception as e:
            err_str = str(e).lower()
            errors.append(f"{MODELS[idx]}: {e}")
            if any(code in err_str for code in ("503", "429", "404", "timeout", "timed out")):
                continue
            traceback.print_exc()
            return jsonify({"reply": f"Debug Error ({type(e).__name__}): {e}"})

    return jsonify({"reply": "All models failed:\n" + "\n".join(errors)})

# 6. Run the Server (Local Dev Only; Vercel uses the `app` WSGI object directly)
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)