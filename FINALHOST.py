import asyncio
import os
import sys
import logging
import subprocess
import re
import importlib.util
import psutil
import sqlite3
import hashlib
import json
import zipfile
import shutil
from datetime import datetime, timedelta, timezone
import time
from aiogram import Bot, Dispatcher, types, F, BaseMiddleware
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton as _AiogramButton, FSInputFile, ErrorEvent
from aiogram.fsm.storage.memory import MemoryStorage
from aiohttp import web
import aiohttp
from pathlib import Path
from dotenv import load_dotenv

# All timestamps are UTC so subscription dates match what users see.
os.environ['TZ'] = 'UTC'
if hasattr(time, 'tzset'):
    time.tzset()

load_dotenv()

# ---------- Button helper: clean labels + colored buttons ----------
# Telegram's colored inline buttons use the `style` field:
#   "primary" = blue, "success" = green, "danger" = red (default = grey)
# Clients/API versions that don't support it simply show a normal button.
_EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\u23E9-\u23FF\u25A0-\u25FF\u2190-\u21FF\uFE0F\u200D]+")
_KEEP_ICON = {"\u2B50", "\u2606", "\u274C"}  # star / empty star / cross are state indicators

def _clean_label(text):
    if text.strip() in _KEEP_ICON:
        return text
    return re.sub(r"\s{2,}", " ", _EMOJI_RE.sub("", text)).strip() or text

def _pick_style(text, data):
    t = (text or "").lower()
    d = (data or "").lower()
    if "unban" in t:
        return "success"
    if any(w in t for w in ("delete", "stop", "ban user", "remove", "revoke", "kill", "pause", "lock bot", "restart bot")) \
            or d.startswith(("delete_file", "stop_script", "admin_revoke", "admin_kill", "admin_ban")):
        return "danger"
    if any(w in t for w in ("run", "upload", "extract", "grant", "trial", "download", "confirm", "activate", "add admin")) \
            or d.startswith(("run_script", "upload_file", "extract_zip", "bot_run", "grant_do")):
        return "success"
    return "primary"

def InlineKeyboardButton(text, keep_emoji=False, style=None, **kwargs):
    # Every button is coloured: green = go/create, red = stop/delete, blue = everything else.
    label = text if keep_emoji else _clean_label(text)
    if style is None or style == "none":
        style = _pick_style(label, kwargs.get("callback_data")) or "primary"
    kwargs["style"] = style
    return _AiogramButton(text=label, **kwargs)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.getenv('SECOND_BOT_TOKEN') or '9001715806:AAE4Qxfyndmcv1q6cAowgw6tqazjUTehHXE'
OWNER_ID_STR = os.getenv('OWNER_ID')
ADMIN_ID_STR = os.getenv('ADMIN_ID') or '8680332219'
YOUR_USERNAME = os.getenv('YOUR_USERNAME')
UPDATE_CHANNEL = os.getenv('UPDATE_CHANNEL')

if not TOKEN:
    logger.error("Second bot token not configured!")
    raise ValueError("SECOND_BOT_TOKEN is required. Set a valid, active token in the parent host environment.")

OWNER_ID_STR = OWNER_ID_STR or ADMIN_ID_STR
ADMIN_ID_STR = ADMIN_ID_STR or OWNER_ID_STR
if not OWNER_ID_STR:
    logger.error("OWNER_ID / ADMIN_ID not found in environment variables!")
    raise ValueError("Set OWNER_ID (and optionally ADMIN_ID) in the environment or .env file.")

_PARENT_TOKEN = os.getenv('BOT_TOKEN')
if _PARENT_TOKEN and _PARENT_TOKEN == TOKEN:
    logger.error("This bot's token is the same as the parent bot's BOT_TOKEN - two bots cannot poll one token.")
    raise ValueError("Token conflict: KALPRO Hosting Bot must use its OWN token (SECOND_BOT_TOKEN), not the parent's BOT_TOKEN.")

try:
    OWNER_ID = int(OWNER_ID_STR)
    ADMIN_ID = int(ADMIN_ID_STR)
except ValueError:
    logger.error("OWNER_ID or ADMIN_ID must be valid integers!")
    raise

YOUR_USERNAME = '@Kaleabdot'
UPDATE_CHANNEL = os.getenv('UPDATE_CHANNEL', 'https://t.me/Ethio_Free_Datass')
CHANNEL_USERNAME = '@' + UPDATE_CHANNEL.rstrip('/').split('/')[-1]

# On Render attach a Disk and set DATA_DIR=/var/data so users, bots and the database survive restarts.
BASE_DIR = Path(os.getenv('DATA_DIR') or (Path(__file__).resolve().parent / 'kalpro_nested_data'))
BASE_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_BOTS_DIR = BASE_DIR / 'upload_bots'
IROTECH_DIR = BASE_DIR / 'inf'
DELETED_DIR = BASE_DIR / 'deleted_bots'   # copies of deleted bots, owner-only download
DATABASE_PATH = IROTECH_DIR / 'bot_data.db'

FREE_USER_LIMIT = float('inf')
SUBSCRIBED_USER_LIMIT = float('inf')
ADMIN_LIMIT = 999
OWNER_LIMIT = float('inf')

UPLOAD_BOTS_DIR.mkdir(parents=True, exist_ok=True)
IROTECH_DIR.mkdir(exist_ok=True)
DELETED_DIR.mkdir(parents=True, exist_ok=True)


# ---------- Hardening helpers ----------
HOST_FILE = Path(__file__).resolve()
try:
    HOST_HASH = hashlib.sha256(HOST_FILE.read_bytes()).hexdigest()
except Exception:
    HOST_HASH = None

MAX_UPLOAD_MB = 20  # Telegram bots cannot download files bigger than 20 MB
_SECRET_ENV_KEYS = {
    'BOT_TOKEN', 'SECOND_BOT_TOKEN', 'TOKEN', 'API_TOKEN', 'TELEGRAM_TOKEN', 'TELEGRAM_BOT_TOKEN',
    'OWNER_ID', 'ADMIN_ID', 'ADMIN_PASSWORD', 'PORT', 'RENDER_EXTERNAL_URL',
}

# ---------- Supported languages (any code the server has an interpreter for) ----------
LANGS = {
    '.py': 'Python', '.js': 'Node.js', '.mjs': 'Node.js', '.cjs': 'Node.js', '.ts': 'TypeScript',
    '.sh': 'Bash', '.bash': 'Bash', '.php': 'PHP', '.rb': 'Ruby', '.go': 'Go', '.java': 'Java',
    '.pl': 'Perl', '.lua': 'Lua',
}
RUN_TYPES = tuple(e[1:] for e in LANGS)
SUPPORT_EXTS = {'.txt', '.json', '.env', '.yml', '.yaml', '.toml', '.ini', '.cfg', '.csv', '.md', '.html',
                '.css', '.sql', '.xml', '.conf', '.lock'}
ARCHIVE_EXTS = ('.zip', '.tar', '.tar.gz', '.tgz', '.tar.bz2', '.tbz2', '.tar.xz', '.txz')
TEXT_SCAN_EXTS = set(LANGS) | SUPPORT_EXTS | {'.env', ''}
SKIP_DIRS = {'node_modules', '.git', '__pycache__', 'venv', '.venv', 'env', 'site-packages', '.idea', '.vscode'}
JUNK_PARTS = {'__MACOSX', '__pycache__', '.git', '.idea', '.vscode', '.DS_Store', 'Thumbs.db'}
ICONS = {'py': '🐍', 'js': '🟨', 'mjs': '🟨', 'cjs': '🟨', 'ts': '🔷', 'sh': '🖥️', 'bash': '🖥️', 'php': '🐘',
         'rb': '💎', 'go': '🐹', 'java': '☕', 'pl': '🐪', 'lua': '🌙', 'zip': '📦', 'tar': '📦'}


def _icon(ftype):
    return ICONS.get(ftype, '📄')


def _is_archive(name):
    return (name or '').lower().endswith(ARCHIVE_EXTS)


def _safe_name(name):
    """Safe, short filename (callback_data is limited to 64 bytes)."""
    name = os.path.basename((name or 'file').replace('\\', '/'))
    if name.lower() == '.env':
        return '.env'
    low = name.lower()
    for double in ('.tar.gz', '.tar.bz2', '.tar.xz'):
        if low.endswith(double):
            stem = re.sub(r'[^A-Za-z0-9._-]+', '_', name[:-len(double)]).strip('._') or 'file'
            return stem[:28] + double
    stem, ext = os.path.splitext(name)
    stem = re.sub(r'[^A-Za-z0-9._-]+', '_', stem).strip('._') or 'file'
    return stem[:28] + ext.lower()


def project_root(uid, fname):
    """Folder a script runs in: its own project folder for ZIP projects, the user folder for flat uploads."""
    base = UPLOAD_BOTS_DIR / str(uid)
    return base / fname.split('/', 1)[0] if '/' in fname else base


def rel_in_root(fname):
    return fname.split('/', 1)[1] if '/' in fname else fname


def _flat(fname):
    """Name usable as a single file name (project paths contain '/')."""
    return fname.replace('/', '__')


def _launch_cmd(ext, rel):
    """(command, error). Picks the right interpreter for the file type."""
    w = shutil.which
    label = LANGS.get(ext, ext)
    if ext == '.py':
        return [sys.executable, '-u', rel], None
    if ext in ('.js', '.mjs', '.cjs'):
        exe = w('node')
        return ([exe, rel], None) if exe else (None, "Node.js is not installed on this server.")
    if ext == '.ts':
        if w('tsx'):
            return [w('tsx'), rel], None
        if w('npx'):
            return [w('npx'), '--yes', 'tsx', rel], None
        if w('node'):
            return [w('node'), '--experimental-strip-types', rel], None
        return None, "Node.js (needed for TypeScript) is not installed on this server."
    table = {'.sh': ('bash', ['bash']), '.bash': ('bash', ['bash']), '.php': ('php', ['php']),
             '.rb': ('ruby', ['ruby']), '.pl': ('perl', ['perl']), '.lua': ('lua', ['lua'])}
    if ext in table:
        exe = w(table[ext][0])
        return ([exe, rel], None) if exe else (None, f"{label} is not installed on this server.")
    if ext == '.go':
        exe = w('go')
        return ([exe, 'run', rel], None) if exe else (None, "Go is not installed on this server.")
    if ext == '.java':
        exe = w('java')
        return ([exe, rel], None) if exe else (None, "Java is not installed on this server.")
    return None, f"{label} files cannot be run."


# ---------- Host protection: a hosted script must never take over / duplicate the host ----------
_TOKEN_RE = re.compile(r'(?<![0-9])(\d{7,12}):([A-Za-z0-9_-]{30,})')
_HOST_MARKERS = ('kalpro_nested_data', 'KALPRO Hosting Bot', 'def kill_duplicate_instances', 'class BanGuardMiddleware')


def _host_bot_ids():
    ids = set()
    for t in (TOKEN, _PARENT_TOKEN):
        if t and ':' in t:
            ids.add(t.split(':', 1)[0])
    return ids


def _candidate_files(fp, root=None, cap=400):
    """Files whose text could reveal a bot token: the script itself + config files (+ the whole project for ZIPs)."""
    fp = Path(fp)
    out = [fp]
    if root:
        n = 0
        for dirpath, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for f in files:
                p = Path(dirpath) / f
                if p == fp or p.suffix.lower() not in TEXT_SCAN_EXTS:
                    continue
                try:
                    if p.stat().st_size > 2 * MB_SCAN:
                        continue
                except OSError:
                    continue
                out.append(p)
                n += 1
                if n >= cap:
                    return out
    else:
        for name in ('.env', 'config.json', 'config.py', 'settings.py', 'config.js', 'secrets.json', 'secrets.py'):
            p = fp.parent / name
            if p.is_file() and p != fp:
                out.append(p)
    return out


MB_SCAN = 1024 * 1024


def _guard_scan(file_path, root=None):
    """Returns (reason or None, set_of_bot_ids_found). Blocks the host's own token / bot id / a host clone."""
    host_ids = _host_bot_ids()
    found = set()
    for p in _candidate_files(file_path, root):
        try:
            data = Path(p).read_bytes()
        except Exception:
            continue
        if p == Path(file_path) and HOST_HASH and hashlib.sha256(data).hexdigest() == HOST_HASH:
            return "this file is a copy of the host itself", found
        text = data.decode('utf-8', errors='ignore')
        if TOKEN and TOKEN in text:
            return "this file contains the host bot's own token", found
        if _PARENT_TOKEN and _PARENT_TOKEN in text:
            return "this file contains the parent bot's token", found
        for m in _TOKEN_RE.finditer(text):
            if m.group(1) in host_ids:
                return "this file uses the same Telegram bot as the host", found
            if root or Path(p) == Path(file_path):
                found.add(m.group(1))
        if sum(1 for mk in _HOST_MARKERS if mk in text) >= 2:
            return "this file is a copy of the hosting bot", found
    return None, found


def _self_run_reason(file_path, root=None):
    """Return a reason string if this file would run the host itself / reuse its token."""
    try:
        return _guard_scan(file_path, root)[0]
    except Exception as e:
        logger.warning(f"guard scan error: {e}")
        return None


def detect_tokens(file_path, root=None):
    """Telegram bot tokens found in the user's script / project / .env / config files (host tokens excluded)."""
    host_ids = _host_bot_ids()
    out = []
    for p in _candidate_files(file_path, root):
        try:
            text = Path(p).read_bytes().decode('utf-8', errors='ignore')
        except Exception:
            continue
        for m in _TOKEN_RE.finditer(text):
            if m.group(1) in host_ids:
                continue
            if m.group(0) not in out:
                out.append(m.group(0))
        # single-file upload: the script's own token wins; the shared .env/config is only a fallback
        if root is None and out and Path(p) == Path(file_path):
            break
    return out


def mask_token(tok):
    head, _, secret = tok.partition(':')
    return f"{head}:{secret[:4]}…{secret[-3:]}"


async def validate_token(token):
    """Ask Telegram if the token is alive. Returns (state, username): True = valid, False = rejected, None = unknown."""
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as sess:
            async with sess.get(f"https://api.telegram.org/bot{token}/getMe") as r:
                if r.status == 200:
                    data = await r.json()
                    return True, (data.get('result') or {}).get('username')
                if r.status in (401, 404):
                    return False, None
    except Exception:
        pass
    return None, None


async def detect_and_check(file_path, root=None):
    """[(token, (state, username))] for every bot token found in the user's files (max 5)."""
    tokens = await asyncio.to_thread(detect_tokens, file_path, root)
    if not tokens:
        return []
    res = await asyncio.gather(*(validate_token(t) for t in tokens[:5]))
    return list(zip(tokens, res))


def _bot_ids_in(file_path, root=None):
    try:
        return _guard_scan(file_path, root)[1]
    except Exception:
        return set()


def _proc_alive(key):
    """True only while the hosted process is really running (the watchdog does the cleanup)."""
    info = bot_scripts.get(key)
    return bool(info) and info['process'].poll() is None


def _alloc_port(key):
    """Every hosted script gets its own PORT so keep-alive web servers never fight the host or each other."""
    import socket, zlib
    try:
        host_port = int(os.getenv('PORT') or 0)
    except ValueError:
        host_port = 0
    taken = {i.get('port') for i in bot_scripts.values() if i.get('port')}
    base = zlib.crc32(key.encode()) % 20000
    for i in range(300):
        p = 20000 + (base + i) % 20000
        if p == host_port or p in taken:
            continue
        with socket.socket() as s:
            try:
                s.bind(('127.0.0.1', p))
            except OSError:
                continue
        return p
    return None


def _child_env(port=None, home=None):
    """Environment for hosted scripts: no host tokens/IDs/Render secrets, own PORT, unbuffered output."""
    env = {k: v for k, v in os.environ.items()
           if k.upper() not in _SECRET_ENV_KEYS and not k.upper().startswith('RENDER')
           and v not in (TOKEN, _PARENT_TOKEN)}
    env['PYTHONUNBUFFERED'] = '1'
    env['PYTHONIOENCODING'] = 'utf-8'
    if port:
        env['PORT'] = str(port)
    if home:
        env['HOME'] = str(home)
    return env


def _shield_env_file(folder):
    """An empty .env stops python-dotenv from walking up and loading the HOST's .env."""
    env_file = Path(folder) / '.env'
    if not env_file.exists():
        env_file.write_text('', encoding='utf-8')


# ---------- Per-user OS isolation (needs the host to run as root, e.g. the Docker deploy on Render) ----------
# Every user's scripts run under their OWN unprivileged uid. They cannot read the host's source, database or
# /proc/<host>/environ (so they cannot steal the host token), and cannot read other users' files.
def _isolation_id(uid):
    try:
        if os.name != 'posix' or os.geteuid() != 0 or os.getenv('ISOLATE_USERS', '1') == '0':
            return None
        return int(os.getenv('HOSTED_UID_BASE', '100000')) + (int(uid) % 900000)
    except Exception:
        return None


def _own_tree(path, ids):
    path = str(path)
    try:
        os.chown(path, ids, ids)
        os.chmod(path, 0o700)
    except OSError:
        return
    for dirpath, dirs, files in os.walk(path):
        for n in dirs + files:
            try:
                os.lchown(os.path.join(dirpath, n), ids, ids)
            except OSError:
                pass


def _lock_folder(path):
    """A user folder not yet handed to its owner's uid stays root-only (no other user can read it)."""
    if _isolation_id(0) is None:
        return
    try:
        if os.stat(path).st_uid == 0:
            os.chmod(path, 0o700)
    except OSError:
        pass


def _secure_dirs():
    """Host-only data stays unreadable for hosted scripts."""
    if _isolation_id(0) is None:
        return
    for p, mode in ((BASE_DIR, 0o711), (UPLOAD_BOTS_DIR, 0o711), (IROTECH_DIR, 0o700), (DELETED_DIR, 0o700)):
        try:
            os.chmod(p, mode)
        except OSError as e:
            logger.warning(f"chmod {p}: {e}")
    try:
        os.chmod(HOST_FILE, 0o600)
    except OSError:
        pass
    try:
        for d in UPLOAD_BOTS_DIR.iterdir():
            if d.is_dir():
                _lock_folder(d)
    except OSError:
        pass
    logger.info("Per-user isolation is ON: hosted scripts run under separate unprivileged users.")


# ---------- Fast, safe archive extraction (zip / tar*) ----------
def _clean_member(name):
    name = name.replace('\\', '/').lstrip('/')
    parts = [p for p in name.split('/') if p not in ('', '.')]
    if not parts or '..' in parts:
        return None
    if any(p in JUNK_PARTS for p in parts) or parts[-1].endswith('.pyc'):
        return None
    return parts


def _strip_common(entries):
    """entries: list of (parts, obj). Drops a single wrapping top-level folder (project/ inside the zip)."""
    if entries and all(len(p) > 1 for p, _ in entries) and len({p[0] for p, _ in entries}) == 1:
        return [(p[1:], o) for p, o in entries]
    return entries


def _extract_archive(path, dest, cap_bytes=200 * MB_SCAN * 1, max_files=20000, workers=4):
    """Extract a zip/tar archive into dest. No zip-slip, no symlinks, size cap on REAL bytes written,
    parallel for zip. Returns the list of extracted relative paths (posix)."""
    import tarfile
    import threading
    from concurrent.futures import ThreadPoolExecutor
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    dest_r = dest.resolve()
    written = [0]
    lock = threading.Lock()
    BUF = 1024 * 1024

    def target_for(parts):
        t = dest_r.joinpath(*parts).resolve()
        if t != dest_r and dest_r not in t.parents:
            return None
        return t

    def write_stream(src, t):
        t.parent.mkdir(parents=True, exist_ok=True)
        with open(t, 'wb') as out:
            while True:
                chunk = src.read(BUF)
                if not chunk:
                    break
                with lock:
                    written[0] += len(chunk)
                    if written[0] > cap_bytes:
                        raise ValueError(f"Archive is too large to extract ({cap_bytes // MB_SCAN} MB of space available)")
                out.write(chunk)

    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as zf:
            entries = []
            for m in zf.infolist():
                if m.is_dir() or ((m.external_attr >> 16) & 0o170000) == 0o120000:
                    continue
                parts = _clean_member(m.filename)
                if parts:
                    entries.append((parts, m.filename))
            if len(entries) > max_files:
                raise ValueError(f"Too many files in the archive ({len(entries)} > {max_files})")
            declared = sum(zf.getinfo(n).file_size for _, n in entries)
            if declared > cap_bytes:
                raise ValueError(f"Archive is too large to extract ({cap_bytes // MB_SCAN} MB of space available)")
            entries = _strip_common(entries)
        jobs = []
        for parts, name in entries:
            t = target_for(parts)
            if t:
                jobs.append((name, t, '/'.join(parts)))
        for d in {t.parent for _, t, _ in jobs}:
            d.mkdir(parents=True, exist_ok=True)
        n_workers = max(1, min(workers, len(jobs) // 8 or 1))
        chunks = [jobs[i::n_workers] for i in range(n_workers)]

        def work(chunk):
            with zipfile.ZipFile(path) as z:        # one handle per worker (ZipFile isn't thread-safe)
                for name, t, _rel in chunk:
                    with z.open(name) as src:
                        write_stream(src, t)

        if n_workers == 1:
            work(chunks[0])
        else:
            with ThreadPoolExecutor(max_workers=n_workers) as ex:
                for f in [ex.submit(work, c) for c in chunks]:
                    f.result()
        return [rel for _, _, rel in jobs]

    if tarfile.is_tarfile(path):
        with tarfile.open(path) as tf:
            entries = []
            for m in tf:
                if not m.isfile():
                    continue
                parts = _clean_member(m.name)
                if parts:
                    entries.append((parts, m))
            if len(entries) > max_files:
                raise ValueError(f"Too many files in the archive ({len(entries)} > {max_files})")
            entries = _strip_common(entries)
            names = []
            for parts, m in entries:
                t = target_for(parts)
                if not t:
                    continue
                src = tf.extractfile(m)
                if src:
                    with src:
                        write_stream(src, t)
                    names.append('/'.join(parts))
            return names
    raise zipfile.BadZipFile("Not a valid zip/tar archive")


ENTRY_NAMES = ('main', 'bot', 'app', 'run', 'start', 'index', 'server', '__main__', 'host', 'manager')
NOT_ENTRY = ('setup', 'conftest', '__init__')


def _pick_entries(names, limit=8):
    """Choose which files of an extracted project become runnable bots (not every helper module)."""
    cands = [n for n in names if Path(n).suffix.lower() in LANGS
             and Path(n).stem not in NOT_ENTRY and not Path(n).stem.startswith('test_')]
    named = [n for n in cands if n.count('/') <= 1 and Path(n).stem.lower() in ENTRY_NAMES]
    top = [n for n in cands if n.count('/') == 0]
    chosen = named or top or sorted(cands, key=lambda n: (n.count('/'), n))[:5]
    return sorted(dict.fromkeys(chosen))[:limit]


bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())
BOT_START_TIME = datetime.now()

bot_scripts = {}
user_subscriptions = {}
user_files = {}
user_favorites = {}
banned_users = set()
active_users = set()
admin_ids = {ADMIN_ID, OWNER_ID}
user_profiles = {}
bot_locked = False
locked_files = set()   # {(user_id, file_name)} - protected bots that users cannot delete or overwrite
bot_stats = {'total_uploads': 0, 'total_downloads': 0, 'total_runs': 0}

class BanGuardMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if user and user.id in banned_users:
            if isinstance(event, types.CallbackQuery):
                await event.answer("🚫 You are banned from KALPRO Hosting Bot.", show_alert=True)
            elif isinstance(event, types.Message):
                await event.answer("🚫 You are banned from KALPRO Hosting Bot. Contact the owner for assistance.")
            return
        return await handler(event, data)

dp.message.outer_middleware(BanGuardMiddleware())
dp.callback_query.outer_middleware(BanGuardMiddleware())

def migrate_db():
    logger.info("Running database migrations...")
    try:
        conn = sqlite3.connect(DATABASE_PATH)
        c = conn.cursor()
        
        c.execute("PRAGMA table_info(user_files)")
        columns = [row[1] for row in c.fetchall()]
        if 'upload_date' not in columns:
            logger.info("Adding upload_date column to user_files table...")
            c.execute('ALTER TABLE user_files ADD COLUMN upload_date TEXT')
            logger.info("upload_date column added successfully.")
        
        c.execute("PRAGMA table_info(active_users)")
        columns = [row[1] for row in c.fetchall()]
        if 'join_date' not in columns:
            logger.info("Adding join_date column to active_users table...")
            c.execute('ALTER TABLE active_users ADD COLUMN join_date TEXT')
            logger.info("join_date column added successfully.")
        if 'last_active' not in columns:
            logger.info("Adding last_active column to active_users table...")
            c.execute('ALTER TABLE active_users ADD COLUMN last_active TEXT')
            logger.info("last_active column added successfully.")
        
        def _ensure(table, col, decl):
            c.execute(f"PRAGMA table_info({table})")
            if col not in [r[1] for r in c.fetchall()]:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
        for col, decl in [('plan', 'TEXT'), ('start_date', 'TEXT'), ('is_trial', 'INTEGER DEFAULT 0'),
                          ('granted_by', 'INTEGER'), ('reminded_for', 'TEXT'), ('expired_notified_for', 'TEXT'),
                          ('bot_limit', 'INTEGER')]:
            _ensure('subscriptions', col, decl)
        for col, decl in [('username', 'TEXT'), ('full_name', 'TEXT')]:
            _ensure('active_users', col, decl)
        
        conn.commit()
        conn.close()
        logger.info("Database migrations completed successfully.")
    except Exception as e:
        logger.error(f"Database migration error: {e}", exc_info=True)

def init_db():
    logger.info(f"Initializing database at: {DATABASE_PATH}")
    try:
        conn = sqlite3.connect(DATABASE_PATH)
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS subscriptions
                     (user_id INTEGER PRIMARY KEY, expiry TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS user_files
                     (user_id INTEGER, file_name TEXT, file_type TEXT, upload_date TEXT,
                      PRIMARY KEY (user_id, file_name))''')
        c.execute('''CREATE TABLE IF NOT EXISTS active_users
                     (user_id INTEGER PRIMARY KEY, join_date TEXT, last_active TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS admins
                     (user_id INTEGER PRIMARY KEY)''')
        c.execute('''CREATE TABLE IF NOT EXISTS banned_users
                     (user_id INTEGER PRIMARY KEY, banned_date TEXT, reason TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS favorites
                     (user_id INTEGER, file_name TEXT, PRIMARY KEY (user_id, file_name))''')
        c.execute('''CREATE TABLE IF NOT EXISTS bot_stats
                     (stat_name TEXT PRIMARY KEY, stat_value INTEGER)''')
        
        c.execute('''CREATE TABLE IF NOT EXISTS bots
                     (user_id INTEGER, file_name TEXT, status TEXT DEFAULT 'stopped', started_at TEXT,
                      updated_at TEXT, restarts INTEGER DEFAULT 0, resume_on_renew INTEGER DEFAULT 0, pid INTEGER,
                      PRIMARY KEY (user_id, file_name))''')
        c.execute('''CREATE TABLE IF NOT EXISTS trials
                     (user_id INTEGER PRIMARY KEY, granted_at TEXT, granted_by INTEGER)''')
        c.execute('''CREATE TABLE IF NOT EXISTS locked_files
                     (user_id INTEGER, file_name TEXT, locked_at TEXT, locked_by INTEGER,
                      PRIMARY KEY (user_id, file_name))''')
        c.execute('''CREATE TABLE IF NOT EXISTS deleted_files
                     (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, file_name TEXT, file_type TEXT,
                      size_bytes INTEGER, deleted_at TEXT, deleted_by INTEGER)''')
        c.execute("PRAGMA table_info(deleted_files)")
        if 'archive_path' not in [r[1] for r in c.fetchall()]:
            c.execute('ALTER TABLE deleted_files ADD COLUMN archive_path TEXT')
        c.execute('INSERT OR IGNORE INTO admins (user_id) VALUES (?)', (OWNER_ID,))
        if ADMIN_ID != OWNER_ID:
            c.execute('INSERT OR IGNORE INTO admins (user_id) VALUES (?)', (ADMIN_ID,))
        
        for stat in ['total_uploads', 'total_downloads', 'total_runs', 'bot_locked']:
            c.execute('INSERT OR IGNORE INTO bot_stats (stat_name, stat_value) VALUES (?, 0)', (stat,))
        
        conn.commit()
        conn.close()
        logger.info("Database initialized successfully.")
    except Exception as e:
        logger.error(f"Database initialization error: {e}", exc_info=True)

def load_data():
    logger.info("Loading data from database...")
    try:
        conn = sqlite3.connect(DATABASE_PATH)
        c = conn.cursor()
        
        c.execute('SELECT user_id, expiry, plan, start_date, is_trial, reminded_for, expired_notified_for, bot_limit FROM subscriptions')
        for user_id, expiry, plan, start, is_trial, reminded, exp_notified, bot_limit in c.fetchall():
            try:
                user_subscriptions[user_id] = {
                    'expiry': datetime.fromisoformat(expiry), 'plan': plan or 'lite',
                    'is_trial': bool(is_trial), 'start': datetime.fromisoformat(start) if start else None,
                    'reminded_for': reminded, 'expired_notified_for': exp_notified, 'bot_limit': bot_limit}
            except ValueError:
                logger.warning(f"Invalid expiry date for user {user_id}")
        
        c.execute('SELECT user_id, username, full_name FROM active_users')
        for user_id, username, full_name in c.fetchall():
            if username or full_name:
                user_profiles[user_id] = {'name': full_name or '', 'username': username or ''}
        
        c.execute('SELECT user_id, file_name, file_type FROM user_files')
        for user_id, file_name, file_type in c.fetchall():
            if user_id not in user_files:
                user_files[user_id] = []
            user_files[user_id].append((file_name, file_type))
        
        c.execute('SELECT user_id FROM active_users')
        active_users.update(user_id for (user_id,) in c.fetchall())
        
        c.execute('SELECT user_id FROM admins')
        admin_ids.update(user_id for (user_id,) in c.fetchall())
        
        c.execute('SELECT user_id FROM banned_users')
        banned_users.update(user_id for (user_id,) in c.fetchall())
        
        c.execute('SELECT user_id, file_name FROM favorites')
        for user_id, file_name in c.fetchall():
            if user_id not in user_favorites:
                user_favorites[user_id] = []
            user_favorites[user_id].append(file_name)
        
        c.execute('SELECT stat_name, stat_value FROM bot_stats')
        for stat_name, stat_value in c.fetchall():
            bot_stats[stat_name] = stat_value

        c.execute('SELECT user_id, file_name FROM locked_files')
        locked_files.update((u, f) for u, f in c.fetchall())
        
        conn.close()
        logger.info(f"Data loaded: {len(active_users)} users, {len(banned_users)} banned, {len(admin_ids)} admins.")
    except Exception as e:
        logger.error(f"Error loading data: {e}", exc_info=True)

init_db()
migrate_db()
load_data()
bot_locked = bool(bot_stats.get('bot_locked', 0))   # admin 'Lock Bot' survives restarts

# ═══════════════════════════════════════════════════════════════════
#  ACCESS CONTROL · PLANS · BOT MANAGER   (KALPRO Hosting Bot 3.0)
# ═══════════════════════════════════════════════════════════════════
import html as _html
import urllib.parse as _urlparse

MB = 1024 * 1024
CONTACT_USERNAME = '@Kaleabdot'
CONTACT_URL = 'https://t.me/Kaleabdot'
CURRENCY = 'ETB'
PLAN_DAYS = 30            # length of one paid period
TRIAL_DAYS = 7            # free trial length (/trial)
TRIAL_PLAN = 'lite'       # limits used during the free trial
MAX_AUTO_RESTARTS = 3     # crash auto-restarts allowed per 10 minutes

PLANS = {
    'lite':    {'name': 'Lite',    'emoji': '🌱', 'price': 100, 'bots': 5,  'storage_mb': 500},
    'starter': {'name': 'Starter', 'emoji': '⚡', 'price': 200, 'bots': 10, 'storage_mb': 1024},
    'pro':     {'name': 'Pro',     'emoji': '🚀', 'price': 350, 'bots': 15, 'storage_mb': 3072},
    'premium': {'name': 'Premium', 'emoji': '👑', 'price': 600, 'bots': 20, 'storage_mb': 10240,
                'note': 'Full support included'},
}
PLAN_ORDER = ['lite', 'starter', 'pro', 'premium']
OPEN_CALLBACKS = {'plans', 'plan', 'back_denied', 'get_premium'}   # usable without an active subscription

pending_input = {}      # admin_id -> {'action': str, 'ts': float}
trials_used = set()
bot_rows = {}           # (user_id, file_name) -> persisted bot state
_start_locks = {}
_crash_hist = {}
_size_cache = {}


def now_utc():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def esc(value):
    return _html.escape(str(value), quote=False)


def dbx(sql, params=()):
    """Small sqlite helper. Returns rows for SELECT, rowcount otherwise."""
    conn = sqlite3.connect(DATABASE_PATH, timeout=10)
    try:
        cur = conn.execute(sql, params)
        conn.commit()
        if sql.lstrip().upper().startswith('SELECT'):
            return cur.fetchall()
        return cur.rowcount
    finally:
        conn.close()


# ───────────── formatting helpers ─────────────
def fmt_size(num_bytes):
    if num_bytes >= 1024 ** 3:
        return f"{num_bytes / 1024 ** 3:.1f} GB"
    if num_bytes >= MB:
        return f"{num_bytes / MB:.1f} MB"
    return f"{num_bytes / 1024:.1f} KB"


def fmt_storage_mb(mb):
    return f"{mb / 1024:g} GB" if mb >= 1024 else f"{mb:g} MB"


def fmt_left(delta):
    secs = int(delta.total_seconds())
    if secs <= 0:
        return "expired"
    days, rem = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h left"
    if hours:
        return f"{hours}h {minutes}m left"
    return f"{max(minutes, 1)}m left"


def fmt_uptime(delta):
    secs = max(int(delta.total_seconds()), 0)
    days, rem = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m {secs % 60}s"


def fmt_dt(dt):
    return dt.strftime('%Y-%m-%d %H:%M UTC')


def bar(percent, width=10):
    percent = max(0.0, min(100.0, float(percent)))
    filled = round(percent / 100 * width)
    return '▰' * filled + '▱' * (width - filled)


def cb_name(file_name):
    """File name that fits Telegram's 64-byte callback_data limit."""
    if len(file_name.encode('utf-8')) <= 40:
        return file_name
    return 'h~' + hashlib.sha1(file_name.encode('utf-8')).hexdigest()[:10]


def _fname_from_cb(callback):
    tok = callback.data.split(":", 1)[1]
    return resolve_name(callback.from_user.id, tok) or os.path.basename(tok)


def resolve_name(user_id, token):
    files = [f for f, _ in user_files.get(user_id, [])]
    if token in files:
        return token
    if token.startswith('h~'):
        for f in files:
            if cb_name(f) == token:
                return f
    return None


# ───────────── storage ─────────────
def folder_size(user_id, ttl=15):
    now = time.time()
    cached = _size_cache.get(user_id)
    if cached and now - cached[0] < ttl:
        return cached[1]
    total = 0
    root = UPLOAD_BOTS_DIR / str(user_id)
    if root.exists():
        for dirpath, _dirs, files in os.walk(root):
            for name in files:
                try:
                    total += os.path.getsize(os.path.join(dirpath, name))
                except OSError:
                    pass
    _size_cache[user_id] = (now, total)
    return total


def invalidate_size(user_id):
    _size_cache.pop(user_id, None)


# ───────────── access & plans ─────────────
def get_access(user_id):
    if user_id in admin_ids:
        return {'ok': True, 'kind': 'admin', 'plan': 'admin', 'expiry': None, 'trial': False}
    sub = user_subscriptions.get(user_id)
    if not sub:
        return {'ok': False, 'reason': 'none'}
    expiry = sub['expiry']
    if expiry > now_utc():
        return {'ok': True, 'kind': 'user', 'plan': sub.get('plan') or 'lite',
                'expiry': expiry, 'trial': bool(sub.get('is_trial'))}
    return {'ok': False, 'reason': 'expired', 'expiry': expiry}


def plan_limits(user_id):
    if user_id in admin_ids:
        return {'key': 'admin', 'name': 'Admin', 'emoji': '', 'bots': 999, 'storage_mb': None}
    sub = user_subscriptions.get(user_id) or {}
    key = sub.get('plan') or 'lite'
    if key not in PLANS:
        key = 'lite'
    lim = {'key': key, **PLANS[key]}
    if sub.get('bot_limit'):
        lim['bots'] = int(sub['bot_limit'])     # custom limit set by the admin
    return lim


def access_label(user_id):
    if user_id == OWNER_ID:
        return 'OWNER'
    if user_id in admin_ids:
        return 'ADMIN'
    sub = user_subscriptions.get(user_id) or {}
    return 'TRIAL USER' if sub.get('is_trial') else 'HOSTING USER'


def denied_text(acc):
    if acc.get('reason') == 'expired':
        return (f"⛔ Access expired. Your hosting subscription ended on {fmt_dt(acc['expiry'])}.\n\n"
                "Please contact the server owner to renew your hosting access.")
    return "⛔ Access denied. Ask the owner to grant access."


def denied_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="View Plans", callback_data="plans", style="primary")],
        [InlineKeyboardButton(text="Contact Admin", url=CONTACT_URL)],
    ])


def remember_user(user):
    uid = user.id
    name = (user.full_name or '').strip()
    uname = user.username or ''
    cur = user_profiles.get(uid)
    if cur and cur['name'] == name and cur['username'] == uname:
        return
    user_profiles[uid] = {'name': name, 'username': uname}
    now = now_utc().isoformat()
    dbx('INSERT OR IGNORE INTO active_users (user_id, join_date, last_active) VALUES (?, ?, ?)', (uid, now, now))
    dbx('UPDATE active_users SET username = ?, full_name = ?, last_active = ? WHERE user_id = ?',
        (uname, name, now, uid))
    active_users.add(uid)


def user_label(uid):
    prof = user_profiles.get(uid) or {}
    name = prof.get('name') or f"User {uid}"
    uname = f" @{prof['username']}" if prof.get('username') else ''
    return f"{esc(name)}{uname}"


async def notify_user(uid, text, kb=None):
    try:
        await bot.send_message(uid, text, reply_markup=kb, parse_mode="HTML")
        return True
    except Exception as e:
        logger.info(f"Could not notify {uid}: {e}")
        return False


async def safe_edit(cb, text, kb=None):
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception as e:
        if 'message is not modified' in str(e).lower():
            return
        try:
            await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
        except Exception as e2:
            logger.error(f"safe_edit failed: {e2}")


# ───────────── subscriptions ─────────────
async def grant_access(uid, plan_key, days, granted_by, trial=False, bot_limit=None):
    """Grant or extend hosting access. Returns (subscription, number_of_bots_resumed)."""
    plan_key = plan_key if plan_key in PLANS else 'lite'
    now = now_utc()
    cur = user_subscriptions.get(uid)
    active = bool(cur and cur['expiry'] > now)
    base = cur['expiry'] if active else now
    expiry = base + timedelta(days=days)
    sub = {'expiry': expiry, 'plan': plan_key, 'is_trial': bool(trial),
           'start': (cur.get('start') if active and cur.get('start') else now),
           'reminded_for': None, 'expired_notified_for': None, 'bot_limit': bot_limit}
    user_subscriptions[uid] = sub
    dbx('INSERT OR REPLACE INTO subscriptions (user_id, expiry, plan, start_date, is_trial, granted_by, '
        'reminded_for, expired_notified_for, bot_limit) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?)',
        (uid, expiry.isoformat(), plan_key, sub['start'].isoformat(), 1 if trial else 0, granted_by, bot_limit))
    if trial:
        trials_used.add(uid)
        dbx('INSERT OR REPLACE INTO trials (user_id, granted_at, granted_by) VALUES (?, ?, ?)',
            (uid, now.isoformat(), granted_by))
    resumed = await resume_user_bots(uid)
    return sub, resumed


async def revoke_access(uid):
    for key, info in list(bot_scripts.items()):
        if info['script_owner_id'] == uid:
            await stop_bot(uid, info['file_name'], status='paused', resume_on_renew=1)
    existed = user_subscriptions.pop(uid, None) is not None
    dbx('DELETE FROM subscriptions WHERE user_id = ?', (uid,))
    return existed


# ───────────── bot manager ─────────────
def bkey(uid, fname):
    return f"{uid}_{fname}"


def staged_path(uid, fname):
    return UPLOAD_BOTS_DIR / str(uid) / f".staged_{_flat(fname)}"


def has_staged(uid, fname):
    return staged_path(uid, fname).is_file()


def apply_staged(uid, fname):
    """Swap a staged new version into place (atomic). Nothing else is touched."""
    sp = staged_path(uid, fname)
    if not sp.is_file():
        return False
    os.replace(sp, UPLOAD_BOTS_DIR / str(uid) / fname)
    invalidate_size(uid)
    try:
        dbx('UPDATE user_files SET upload_date = ? WHERE user_id = ? AND file_name = ?',
            (now_utc().isoformat(), uid, fname))
    except Exception:
        pass
    return True


def log_path(uid, fname):
    return UPLOAD_BOTS_DIR / str(uid) / f"{Path(_flat(fname)).stem}.log"


def load_bot_rows():
    try:
        for uid, fname, status, started, restarts, renew, pid in dbx(
                'SELECT user_id, file_name, status, started_at, restarts, resume_on_renew, pid FROM bots'):
            bot_rows[(uid, fname)] = {'status': status or 'stopped', 'started_at': started,
                                      'restarts': restarts or 0, 'resume_on_renew': renew or 0, 'pid': pid}
        for (uid,) in dbx('SELECT user_id FROM trials'):
            trials_used.add(uid)
    except Exception as e:
        logger.error(f"load_bot_rows error: {e}")


def set_bot_status(uid, fname, status, **extra):
    row = bot_rows.setdefault((uid, fname), {'status': 'stopped', 'started_at': None, 'restarts': 0,
                                             'resume_on_renew': 0, 'pid': None})
    row['status'] = status
    row.update(extra)
    try:
        dbx('INSERT OR REPLACE INTO bots (user_id, file_name, status, started_at, updated_at, restarts, '
            'resume_on_renew, pid) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
            (uid, fname, status, row.get('started_at'), now_utc().isoformat(), row.get('restarts', 0),
             row.get('resume_on_renew', 0), row.get('pid')))
    except Exception as e:
        logger.error(f"set_bot_status error: {e}")


def forget_bot(uid, fname):
    bot_rows.pop((uid, fname), None)
    _crash_hist.pop(bkey(uid, fname), None)
    try:
        dbx('DELETE FROM bots WHERE user_id = ? AND file_name = ?', (uid, fname))
        staged_path(uid, fname).unlink(missing_ok=True)
        lp = log_path(uid, fname)
        if lp.exists():
            lp.unlink()
    except Exception as e:
        logger.warning(f"forget_bot error: {e}")
    invalidate_size(uid)


def _alive(key):
    info = bot_scripts.get(key)
    return bool(info) and info['process'].poll() is None


def running_count(uid):
    return sum(1 for k, i in bot_scripts.items() if i['script_owner_id'] == uid and i['process'].poll() is None)


def get_bot_status(uid, fname):
    if _alive(bkey(uid, fname)):
        return 'running'
    status = (bot_rows.get((uid, fname)) or {}).get('status', 'stopped')
    return status if status in ('paused', 'crashed') else 'stopped'


def runnable_files(uid):
    return sorted([f for f, t in user_files.get(uid, []) if t in RUN_TYPES], key=str.lower)


def _close_log(info):
    lf = info.get('log_file')
    try:
        if lf and not lf.closed:
            lf.close()
    except Exception:
        pass


def _kill_tree(proc, force=False):
    try:
        parent = psutil.Process(proc.pid)
        procs = parent.children(recursive=True) + [parent]
    except psutil.NoSuchProcess:
        return
    except Exception:
        procs = []
    for p in procs:
        try:
            p.kill() if force else p.terminate()
        except Exception:
            pass
    try:
        _gone, alive = psutil.wait_procs(procs, timeout=1 if force else 4)
        for p in alive:
            try:
                p.kill()
            except Exception:
                pass
    except Exception:
        pass
    try:
        proc.wait(timeout=2)
    except Exception:
        pass


def _open_log(uid, fname):
    p = log_path(uid, fname)
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        if p.exists() and p.stat().st_size > MB:
            tail = p.read_bytes()[-200 * 1024:]
            p.write_bytes(b"...(older output trimmed)...\n" + tail)
    except OSError:
        pass
    f = open(p, 'a', encoding='utf-8', errors='replace', buffering=1)
    f.write(f"\n===== Started {fmt_dt(now_utc())} =====\n")
    f.flush()
    return f


_ANSI_RE = re.compile(r'\x1b\[[0-9;?]*[ -/]*[@-~]')


def read_log_tail(uid, fname, limit=3300):
    p = log_path(uid, fname)
    if not p.exists():
        return None
    try:
        with open(p, 'rb') as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 16 * 1024))
            data = fh.read()
        text = _ANSI_RE.sub('', data.decode('utf-8', errors='replace')).strip()
    except OSError:
        return None
    if len(text) > limit:
        text = "...\n" + text[-limit:]
    return text


def _install_requirements(user_folder):
    req = Path(user_folder) / 'requirements.txt'
    marker = Path(user_folder) / '.requirements.sha'
    if not req.exists():
        return None
    digest = hashlib.sha256(req.read_bytes()).hexdigest()
    if marker.exists() and marker.read_text().strip() == digest:
        return None
    try:
        result = subprocess.run(
            [sys.executable, '-m', 'pip', 'install', '-r', str(req), '--break-system-packages', '--quiet'],
            capture_output=True, text=True, timeout=300, cwd=str(user_folder))
        if result.returncode == 0:
            marker.write_text(digest)
            return True
        return False
    except Exception as e:
        logger.warning(f"requirements install error: {e}")
        return False


async def start_bot(uid, fname, auto=False, apply_update=False):
    lock = _start_locks.setdefault(bkey(uid, fname), asyncio.Lock())
    async with lock:
        return await _start_bot_locked(uid, fname, auto, apply_update)


async def _start_bot_locked(uid, fname, auto, apply_update=False):
    user_folder = UPLOAD_BOTS_DIR / str(uid)
    fp = user_folder / fname
    if not fp.is_file():
        return False, "File not found."
    ext = fp.suffix.lower()
    if ext not in LANGS:
        return False, "This file type can't be run. Supported: " + ", ".join(sorted(LANGS)) + "."
    if not get_access(uid)['ok']:
        return False, "Your subscription is not active."
    key = bkey(uid, fname)
    if _alive(key):
        return False, "Already running."
    stale = bot_scripts.pop(key, None)
    if stale:
        _close_log(stale)
    limit = plan_limits(uid)['bots']
    if running_count(uid) >= limit:
        return False, f"Bot limit reached ({running_count(uid)}/{limit}). Pause another bot or upgrade your plan."
    applied = False
    if apply_update:                      # only when the user taps Restart/Run, never on auto-restarts
        applied = apply_staged(uid, fname)
    root = project_root(uid, fname)
    rel = rel_in_root(fname)
    scan_root = root if '/' in fname else None
    reason = await asyncio.to_thread(_self_run_reason, fp, scan_root)
    if reason:
        return False, f"Blocked: {reason}. A hosted script must use its own bot token."
    ids = await asyncio.to_thread(_bot_ids_in, fp, scan_root)
    for k, i in list(bot_scripts.items()):
        if k != key and i['process'].poll() is None and i.get('bot_ids') and (i['bot_ids'] & ids):
            return False, (f"Blocked: <b>{esc(i['file_name'])}</b> is already running with the same Telegram bot token. "
                           "Two scripts cannot poll one token. Stop it first or use a different token.")
    found = await detect_and_check(fp, scan_root)
    if not found:
        return False, ("<b>SET BOT TOKEN</b>\n\nNo bot token was found in your files. Put your bot token "
                       "(from @BotFather) inside your script, or in a .env file, then press Run again.")
    if found and all(r[0] is False for _t, r in found):
        return False, ("Telegram rejected the bot token found in your files (it is wrong or revoked). "
                       "Get a fresh token from @BotFather, put it in your file or .env, then run again.")
    usernames = [r[1] for _t, r in found if r[0] and r[1]]
    cmd, err = _launch_cmd(ext, rel)
    if err:
        return False, err
    _shield_env_file(root)
    log_file = _open_log(uid, fname)
    try:
        if ext == '.py':
            req = await asyncio.to_thread(_install_requirements, root)
            if req is True:
                log_file.write("[AUTO-INSTALLER] requirements.txt installed\n")
            elif req is False:
                log_file.write("[AUTO-INSTALLER] requirements.txt install failed\n")
            installed, failed = await asyncio.to_thread(install_missing_dependencies, fp, 180, root, scan_root is not None)
            if installed:
                log_file.write(f"[AUTO-INSTALLER] Installed: {', '.join(installed)}\n")
            if failed:
                log_file.write(f"[AUTO-INSTALLER] Failed to install: {', '.join(failed)}\n")
        elif ext in ('.js', '.mjs', '.cjs', '.ts'):
            nres = await asyncio.to_thread(_install_node_deps, root)
            if nres is True:
                log_file.write("[AUTO-INSTALLER] npm packages installed\n")
            elif nres is False:
                log_file.write("[AUTO-INSTALLER] npm install failed (is npm available?)\n")
        log_file.flush()
        port = _alloc_port(key)
        iso = _isolation_id(uid)
        pkw = {}
        if iso:
            await asyncio.to_thread(_own_tree, user_folder, iso)
            pkw = {'user': iso, 'group': iso, 'extra_groups': []}
        proc = subprocess.Popen(cmd, cwd=str(root), stdin=subprocess.DEVNULL, stdout=log_file,
                                stderr=subprocess.STDOUT, env=_child_env(port, home=root if iso else None),
                                start_new_session=True, **pkw)
    except FileNotFoundError:
        log_file.close()
        return False, f"{LANGS.get(ext, 'Interpreter')} is not installed on this server."
    except Exception as e:
        log_file.close()
        logger.error(f"start_bot error: {e}")
        return False, f"Could not start: {e}"
    bot_scripts[key] = {'process': proc, 'file_name': fname, 'script_owner_id': uid,
                        'start_time': datetime.now(), 'user_folder': str(root),
                        'type': ext[1:], 'log_file': log_file, 'bot_ids': ids, 'port': port,
                        'bot_usernames': usernames}
    set_bot_status(uid, fname, 'running', started_at=now_utc().isoformat(), pid=proc.pid, resume_on_renew=0)
    try:
        dbx('UPDATE bot_stats SET stat_value = stat_value + 1 WHERE stat_name = ?', ('total_runs',))
        bot_stats['total_runs'] = bot_stats.get('total_runs', 0) + 1
    except Exception:
        pass
    msg = f"Started with the new version (PID {proc.pid})." if applied else f"Started (PID {proc.pid})."
    if usernames:
        msg += " Detected bot: " + ", ".join("@" + esc(u) for u in usernames) + "."
    elif found:
        msg += " Token found but Telegram could not be reached to verify it."
    return True, msg


async def stop_bot(uid, fname, status='paused', resume_on_renew=0, force=False):
    """Stop a bot process. Files, logs and data are never touched."""
    key = bkey(uid, fname)
    info = bot_scripts.pop(key, None)       # pop first so the watchdog ignores this exit
    if info:
        _close_log(info)
        await asyncio.to_thread(_kill_tree, info['process'], force)
    if status:
        set_bot_status(uid, fname, status, pid=None, resume_on_renew=resume_on_renew)
    return bool(info)


async def resume_user_bots(uid):
    started = 0
    for (u, f), row in list(bot_rows.items()):
        if u == uid and row.get('resume_on_renew'):
            ok, _msg = await start_bot(u, f, auto=True)
            if ok:
                started += 1
            else:
                row['resume_on_renew'] = 0
    return started


# ───────────── access guard middleware ─────────────
_seen_updates = {}


def _is_duplicate(event):
    """True if this exact message/button press was already handled (re-delivered update)."""
    mid = getattr(event, 'message_id', None) if isinstance(event, types.Message) else getattr(event, 'id', None)
    if mid is None:
        return False
    key = (type(event).__name__, getattr(getattr(event, 'chat', None), 'id', None), mid)
    now = time.time()
    if len(_seen_updates) > 5000:
        for k, t in list(_seen_updates.items()):
            if now - t > 120:
                _seen_updates.pop(k, None)
    if key in _seen_updates and now - _seen_updates[key] < 120:
        return True
    _seen_updates[key] = now
    return False


class AccessGuardMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if user is None:
            return await handler(event, data)
        if _is_duplicate(event):
            return
        if isinstance(event, types.Message) and event.chat.type != 'private':
            return
        try:
            remember_user(user)
        except Exception as e:
            logger.warning(f"remember_user error: {e}")
        if user.id in admin_ids:
            return await handler(event, data)
        acc = get_access(user.id)
        if acc['ok']:
            return await handler(event, data)
        if isinstance(event, types.CallbackQuery):
            if (event.data or '').split(':', 1)[0] in OPEN_CALLBACKS:
                return await handler(event, data)
            await event.answer(denied_text(acc)[:200], show_alert=True)
            return
        if isinstance(event, types.Message):
            await event.answer(denied_text(acc), reply_markup=denied_keyboard())
        return


dp.message.outer_middleware(AccessGuardMiddleware())
dp.callback_query.outer_middleware(AccessGuardMiddleware())


# ───────────── dashboard & plans views ─────────────
def server_block():
    try:
        cpu = psutil.cpu_percent(interval=None)
        mem = psutil.virtual_memory().percent
        disk = psutil.disk_usage(str(BASE_DIR)).percent
    except Exception:
        return ""
    return (f"<code>CPU    {cpu:5.1f}% {bar(cpu)}\n"
            f"Memory {mem:5.1f}% {bar(mem)}\n"
            f"Disk   {disk:5.1f}% {bar(disk)}</code>")


def build_dashboard(uid, full_name, note=None):
    acc = get_access(uid)
    lim = plan_limits(uid)
    used = folder_size(uid)
    active = running_count(uid)
    lines = ["╔═══════════════════════╗", "    <b>KALPRO HOSTING BOT</b>", "    <i>Script Runner Bot</i>",
             "╚═══════════════════════╝", "",
             f"<b>Welcome, {esc(full_name)}!</b>", f"ID: <code>{uid}</code>", "",
             f"Access level: <b>{access_label(uid)}</b>"]
    if acc.get('kind') == 'admin':
        lines += ["Subscription: <b>Unlimited</b>", f"Storage: {fmt_size(used)} used", f"Bots: {active} running"]
    else:
        trial = " (free trial)" if acc.get('trial') else ""
        lines.append(f"Plan: <b>{lim['name']}</b>{trial}")
        lines.append(f"Subscription: <b>{fmt_left(acc['expiry'] - now_utc())}</b> (until {fmt_dt(acc['expiry'])})")
        quota = lim['storage_mb'] * MB
        pct = used / quota * 100 if quota else 0
        lines.append(f"Storage: {fmt_size(used)} / {fmt_storage_mb(lim['storage_mb'])} ({pct:.1f}%)")
        lines.append(f"<code>{bar(pct)}</code>")
        lines.append(f"Bots: {active} / {lim['bots']} active")
    srv = server_block()
    if srv:
        lines += ["", srv]
    lines += ["", note or "Select an option below.", "", "<b>Developed by</b> KALPRO · Version 3.0"]
    return "\n".join(lines)


def plans_text(uid=None):
    lines = ["<b>HOSTING PLANS</b>", ""]
    for key in PLAN_ORDER:
        p = PLANS[key]
        extra = f" · {p['note']}" if p.get('note') else ""
        lines.append(f"{p['emoji']} <b>{p['name']}</b> — {p['price']} {CURRENCY}\n"
                     f"     {p['bots']} active bots · {fmt_storage_mb(p['storage_mb'])} storage{extra}")
    lines += ["", f"Billing period: {PLAN_DAYS} days.",
              f"To pay or subscribe, contact the admin {CONTACT_USERNAME} and send your User ID."]
    if uid:
        lines.append(f"Your ID: <code>{uid}</code>")
    return "\n".join(lines)


def plans_keyboard(back_cb):
    rows = []
    for key in PLAN_ORDER:
        p = PLANS[key]
        rows.append([InlineKeyboardButton(text=f"{p['emoji']} {p['name']} · {p['price']} {CURRENCY}",
                                          callback_data=f"plan:{key}", keep_emoji=True, style="primary")])
    rows.append([InlineKeyboardButton(text="Contact Admin to Pay", url=CONTACT_URL)])
    rows.append([InlineKeyboardButton(text="Back", callback_data=back_cb)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@dp.callback_query(F.data == "back_denied")
async def cb_back_denied(cb: types.CallbackQuery):
    await safe_edit(cb, denied_text(get_access(cb.from_user.id)), denied_keyboard())
    await cb.answer()


@dp.callback_query(F.data.in_({"plans", "get_premium"}))
async def cb_plans(cb: types.CallbackQuery):
    uid = cb.from_user.id
    back = 'back_to_main' if get_access(uid)['ok'] else 'back_denied'
    await safe_edit(cb, plans_text(uid), plans_keyboard(back))
    await cb.answer()


@dp.callback_query(F.data.startswith("plan:"))
async def cb_plan_detail(cb: types.CallbackQuery):
    uid = cb.from_user.id
    key = cb.data.split(":", 1)[1]
    p = PLANS.get(key)
    if not p:
        await cb.answer("Unknown plan.", show_alert=True)
        return
    note = f"\n{p['note']}" if p.get('note') else ""
    text = (f"{p['emoji']} <b>{p['name']}</b>\n\n"
            f"Price: <b>{p['price']} {CURRENCY}</b> / {PLAN_DAYS} days\n"
            f"Active bots: <b>{p['bots']}</b>\n"
            f"Storage: <b>{fmt_storage_mb(p['storage_mb'])}</b>{note}\n\n"
            f"To subscribe, contact {CONTACT_USERNAME} for payment and send your User ID:\n"
            f"<code>{uid}</code>")
    prefill = _urlparse.quote(f"Hello, I want the {p['name']} plan. My User ID: {uid}")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"Contact Admin to Pay", url=f"{CONTACT_URL}?text={prefill}")],
        [InlineKeyboardButton(text="Back to Plans", callback_data="plans")],
    ])
    await safe_edit(cb, text, kb)
    await cb.answer()


# ───────────── My Bots ─────────────
BOTS_PER_PAGE = 8
_STATUS_WORD = {'running': 'Running', 'paused': 'Paused', 'crashed': 'Crashed', 'stopped': 'Stopped'}
_STATUS_STYLE = {'running': 'success', 'paused': 'primary', 'crashed': 'danger', 'stopped': 'primary'}


def my_bots_view(uid, page=0):
    files = runnable_files(uid)
    lim = plan_limits(uid)
    pages = max(1, -(-len(files) // BOTS_PER_PAGE))
    page = max(0, min(page, pages - 1))
    used = folder_size(uid)
    limit_txt = '' if lim['storage_mb'] is None else f" / {fmt_storage_mb(lim['storage_mb'])}"
    lines = ["<b>MY BOTS</b>",
             f"Active {running_count(uid)} / {lim['bots']} · Storage {fmt_size(used)}{limit_txt}", ""]
    rows = []
    if not files:
        lines.append("No bots yet. Upload a .py or .js file (or a .zip) to get started.")
    else:
        lines.append("Tap a bot to run, pause or read its logs.")
        for f in files[page * BOTS_PER_PAGE:(page + 1) * BOTS_PER_PAGE]:
            st = get_bot_status(uid, f)
            label = f if len(f) <= 24 else f[:21] + '...'
            rows.append([InlineKeyboardButton(text=f"{label} · {_STATUS_WORD[st]}",
                                              callback_data=f"bot:{cb_name(f)}", style=_STATUS_STYLE[st])])
        if pages > 1:
            nav = []
            if page > 0:
                nav.append(InlineKeyboardButton(text="Prev", callback_data=f"my_bots:{page - 1}"))
            nav.append(InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data=f"my_bots:{page}"))
            if page < pages - 1:
                nav.append(InlineKeyboardButton(text="Next", callback_data=f"my_bots:{page + 1}"))
            rows.append(nav)
    rows.append([InlineKeyboardButton(text="Upload File", callback_data="upload_file", style="success"),
                 InlineKeyboardButton(text="Plans", callback_data="plans", style="primary")])
    rows.append([InlineKeyboardButton(text="Main Menu", callback_data="back_to_main", style="primary")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


@dp.callback_query(F.data.startswith("my_bots"))
async def cb_my_bots(cb: types.CallbackQuery):
    page = 0
    if ':' in cb.data:
        try:
            page = int(cb.data.split(':', 1)[1])
        except ValueError:
            page = 0
    text, kb = my_bots_view(cb.from_user.id, page)
    await safe_edit(cb, text, kb)
    await cb.answer()


def bot_detail_text(uid, fname, note=None):
    st = get_bot_status(uid, fname)
    fp = UPLOAD_BOTS_DIR / str(uid) / fname
    kind = 'Python' if fname.lower().endswith('.py') else 'Node.js'
    size = fmt_size(fp.stat().st_size) if fp.exists() else 'missing'
    lines = []
    if note:
        lines += [esc(note), ""]
    lines += [f"<b>{esc(fname)}</b>", f"{kind} · {size}", ""]
    row = bot_rows.get((uid, fname)) or {}
    if st == 'running':
        info = bot_scripts.get(bkey(uid, fname)) or {}
        up = fmt_uptime(datetime.now() - info['start_time']) if info.get('start_time') else '-'
        mem = ''
        try:
            parent = psutil.Process(info['process'].pid)
            rss = sum(p.memory_info().rss for p in [parent] + parent.children(recursive=True))
            mem = f" · RAM {fmt_size(rss)}"
        except Exception:
            pass
        lines.append(f"Status: <b>Running</b> · up {up}{mem}")
    elif st == 'paused':
        lines.append("Status: <b>Paused</b>")
        lines.append("Your files and data are safe. Press Run to continue.")
    elif st == 'crashed':
        lines.append("Status: <b>Crashed</b>")
        lines.append("Open the logs to see the error, fix it, then press Run.")
    else:
        lines.append("Status: <b>Stopped</b>")
    if row.get('restarts'):
        lines.append(f"Auto-restarts: {row['restarts']}")
    lim = plan_limits(uid)
    lines.append(f"Active bots: {running_count(uid)} / {lim['bots']}")
    if is_locked(uid, fname):
        lines.append("Security: <b>Encrypted by admin</b>")
    if has_staged(uid, fname):
        lines += ["", "<b>New version ready.</b> Tap <b>Restart</b> to update."]
    return "\n".join(lines)


def bot_detail_kb(uid, fname):
    tok = cb_name(fname)
    st = get_bot_status(uid, fname)
    staged = has_staged(uid, fname)
    restart_style = "success" if staged else "primary"
    if st == 'running':
        first = [InlineKeyboardButton(text="Pause", callback_data=f"bot_pause:{tok}", style="danger"),
                 InlineKeyboardButton(text="Restart", callback_data=f"bot_restart:{tok}", style=restart_style)]
    elif staged:
        first = [InlineKeyboardButton(text="Restart", callback_data=f"bot_restart:{tok}", style="success")]
    else:
        first = [InlineKeyboardButton(text="Run", callback_data=f"bot_run:{tok}", style="success")]
    rows = [first,
            [InlineKeyboardButton(text="Logs", callback_data=f"bot_logs:{tok}", style="primary"),
             InlineKeyboardButton(text="Update", callback_data=f"bot_update:{tok}", style="primary")]]
    if staged:
        rows.append([InlineKeyboardButton(text="Discard Update", callback_data=f"bot_discard:{tok}", style="danger")])
    rows.append([InlineKeyboardButton(text="Delete Bot", callback_data=f"bot_del:{tok}", style="danger")])
    rows.append([InlineKeyboardButton(text="My Bots", callback_data="my_bots", style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def show_bot(cb, uid, fname, note=None):
    await safe_edit(cb, bot_detail_text(uid, fname, note), bot_detail_kb(uid, fname))


def _bot_from_cb(cb):
    uid = cb.from_user.id
    tok = cb.data.split(":", 1)[1]
    fname = resolve_name(uid, tok)
    return uid, fname


@dp.callback_query(F.data.startswith("bot:"))
async def cb_bot_detail(cb: types.CallbackQuery):
    uid, fname = _bot_from_cb(cb)
    if not fname:
        await cb.answer("Bot not found.", show_alert=True)
        return
    await show_bot(cb, uid, fname)
    await cb.answer()


@dp.callback_query(F.data.startswith("bot_pause:"))
async def cb_bot_pause(cb: types.CallbackQuery):
    uid, fname = _bot_from_cb(cb)
    if not fname:
        await cb.answer("Bot not found.", show_alert=True)
        return
    await stop_bot(uid, fname, status='paused', resume_on_renew=0)
    await cb.answer("Paused. Your files and data are kept.")
    await show_bot(cb, uid, fname, note="Paused. Your files and data are kept.")


@dp.callback_query(F.data.startswith("bot_run:"))
async def cb_bot_run(cb: types.CallbackQuery):
    uid, fname = _bot_from_cb(cb)
    if not fname:
        await cb.answer("Bot not found.", show_alert=True)
        return
    await cb.answer("Starting...")
    await safe_edit(cb, bot_detail_text(uid, fname, note="Starting... (installing dependencies if needed)"),
                    bot_detail_kb(uid, fname))
    ok, msg = await start_bot(uid, fname, apply_update=True)
    await show_bot(cb, uid, fname, note=msg)


@dp.callback_query(F.data.startswith("bot_restart:"))
async def cb_bot_restart(cb: types.CallbackQuery):
    uid, fname = _bot_from_cb(cb)
    if not fname:
        await cb.answer("Bot not found.", show_alert=True)
        return
    await cb.answer("Restarting...")
    await stop_bot(uid, fname, status='stopped')
    ok, msg = await start_bot(uid, fname, apply_update=True)
    note = ("Updated to the new version and restarted." if "new version" in msg else "Restarted.") if ok else msg
    await show_bot(cb, uid, fname, note=note)


async def show_logs(cb, uid, fname):
    tok = cb_name(fname)
    content = read_log_tail(uid, fname)
    st = _STATUS_WORD[get_bot_status(uid, fname)]
    if content is None:
        body = "No logs yet. Run the bot first."
    elif not content:
        body = "(no output yet)"
    else:
        shown = esc(content)
        while len(shown) > 3300:
            content = content[len(content) // 8:]
            shown = esc(content)
        body = f"<pre>{shown}</pre>"
    text = f"<b>Logs · {esc(fname)}</b>\nStatus: {st}\n\n{body}"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Refresh", callback_data=f"bot_logs:{tok}", style="primary"),
         InlineKeyboardButton(text="Download", callback_data=f"bot_logdl:{tok}", style="primary")],
        [InlineKeyboardButton(text="Back to Bot", callback_data=f"bot:{tok}")],
    ])
    await safe_edit(cb, text, kb)


@dp.callback_query(F.data.startswith("bot_logs:"))
async def cb_bot_logs(cb: types.CallbackQuery):
    uid, fname = _bot_from_cb(cb)
    if not fname:
        await cb.answer("Bot not found.", show_alert=True)
        return
    await show_logs(cb, uid, fname)
    await cb.answer()


@dp.callback_query(F.data.startswith("bot_logdl:"))
async def cb_bot_logdl(cb: types.CallbackQuery):
    uid, fname = _bot_from_cb(cb)
    lp = log_path(uid, fname) if fname else None
    if not lp or not lp.exists():
        await cb.answer("No log file yet.", show_alert=True)
        return
    await cb.message.answer_document(FSInputFile(lp, filename=lp.name))
    await cb.answer()


# ───────────── plan picking / admin helpers ─────────────
def plan_pick_keyboard(target, back_cb="admin_panel"):
    rows = [[InlineKeyboardButton(text=f"{PLANS[k]['emoji']} {PLANS[k]['name']} · {PLANS[k]['price']} {CURRENCY}",
                                  callback_data=f"grant_plan:{target}:{k}", keep_emoji=True, style="primary")]
            for k in PLAN_ORDER]
    rows.append([InlineKeyboardButton(text="Back", callback_data=back_cb)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def duration_keyboard(target, plan_key):
    def b(days):
        return InlineKeyboardButton(text=f"{days} days", callback_data=f"grant_do:{target}:{plan_key}:{days}",
                                    style="success")
    return InlineKeyboardMarkup(inline_keyboard=[
        [b(7), b(30)], [b(60), b(90)],
        [InlineKeyboardButton(text="Back", callback_data=f"admin_grantpick:{target}")],
    ])


def user_summary_line(uid):
    acc = get_access(uid)
    if acc.get('kind') == 'admin':
        return "Admin · unlimited"
    sub = user_subscriptions.get(uid)
    if not sub:
        return "No access"
    name = PLANS.get(sub.get('plan') or 'lite', PLANS['lite'])['name']
    tag = " (trial)" if sub.get('is_trial') else ""
    if sub.get('bot_limit'):
        tag += f" · max {sub['bot_limit']} bots"
    if acc['ok']:
        return f"{name}{tag} · {fmt_left(acc['expiry'] - now_utc())} · until {fmt_dt(acc['expiry'])}"
    return f"{name}{tag} · EXPIRED {fmt_dt(acc['expiry'])}"


def user_bots_line(uid, limit=4):
    files = runnable_files(uid)
    if not files:
        return "Bots: none"
    shown = [f"{esc(f)} ({_STATUS_WORD[get_bot_status(uid, f)].lower()})" for f in files[:limit]]
    more = f" +{len(files) - limit} more" if len(files) > limit else ""
    return f"Bots {len(files)}: " + ", ".join(shown) + more


def all_users_sorted():
    ids = (set(active_users) | set(user_subscriptions) | set(user_files)) - set(admin_ids)
    now = now_utc()

    def key(uid):
        sub = user_subscriptions.get(uid)
        if sub and sub['expiry'] > now:
            return (0, sub['expiry'])
        if sub:
            return (1, sub['expiry'])
        return (2, datetime.max)
    return sorted(ids, key=key)


USERS_PER_PAGE = 6


def users_view(page=0):
    ids = all_users_sorted()
    now = now_utc()
    active = sum(1 for u in ids if u in user_subscriptions and user_subscriptions[u]['expiry'] > now)
    expired = sum(1 for u in ids if u in user_subscriptions and user_subscriptions[u]['expiry'] <= now)
    pages = max(1, -(-len(ids) // USERS_PER_PAGE))
    page = max(0, min(page, pages - 1))
    lines = [f"<b>USERS</b> · {len(ids)} total · page {page + 1}/{pages}",
             f"Active {active} · Expired {expired} · No access {len(ids) - active - expired}", ""]
    rows, pair = [], []
    chunk = ids[page * USERS_PER_PAGE:(page + 1) * USERS_PER_PAGE]
    if not chunk:
        lines.append("No users yet.")
    for i, uid in enumerate(chunk, page * USERS_PER_PAGE + 1):
        lines.append(f"{i}. <b>{user_label(uid)}</b> <code>{uid}</code>")
        lines.append(f"    {user_summary_line(uid)}")
        lines.append(f"    {user_bots_line(uid)}")
        prof = user_profiles.get(uid) or {}
        short = (prof.get('name') or str(uid))[:12]
        st = get_access(uid)
        style = "success" if st['ok'] else ("danger" if st.get('reason') == 'expired' else "primary")
        pair.append(InlineKeyboardButton(text=f"{i}. {short}", callback_data=f"admin_user:{uid}", style=style))
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    if pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="Prev", callback_data=f"admin_users:{page - 1}"))
        if page < pages - 1:
            nav.append(InlineKeyboardButton(text="Next", callback_data=f"admin_users:{page + 1}"))
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="Grant Access", callback_data="admin_grant", style="success"),
                 InlineKeyboardButton(text="Free Trial", callback_data="admin_trial", style="success")])
    rows.append([InlineKeyboardButton(text="Admin Panel", callback_data="admin_panel", style="primary")])
    text = "\n".join(lines)
    if len(text) > 3800:
        text = text[:text.rfind("\n", 0, 3780)] + "\n..."
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@dp.callback_query(F.data.startswith("admin_users") | (F.data == "admin_premium_users"))
async def cb_admin_users(cb: types.CallbackQuery):
    if cb.from_user.id not in admin_ids:
        await cb.answer("Admin only!", show_alert=True)
        return
    page = 0
    if ':' in cb.data:
        try:
            page = int(cb.data.split(':', 1)[1])
        except ValueError:
            page = 0
    text, kb = users_view(page)
    await safe_edit(cb, text, kb)
    await cb.answer()


def admin_user_view(uid, viewer_id=None):
    prof = user_profiles.get(uid) or {}
    row = dbx('SELECT join_date, last_active FROM active_users WHERE user_id = ?', (uid,))
    joined = (row[0][0] or '')[:10] if row else '-'
    last = (row[0][1] or '')[:16].replace('T', ' ') if row else '-'
    lim = plan_limits(uid)
    quota = '' if lim['storage_mb'] is None else f" / {fmt_storage_mb(lim['storage_mb'])}"
    lines = [f"<b>{user_label(uid)}</b>", f"ID <code>{uid}</code>", "",
             f"Access: {user_summary_line(uid)}",
             f"Joined: {joined} · Last active: {last} UTC",
             f"Storage: {fmt_size(folder_size(uid))}{quota}", ""]
    files = runnable_files(uid)
    if files:
        lines.append(f"<b>Bots ({len(files)}, {running_count(uid)} running)</b>")
        for f in files[:15]:
            lines.append(f"• {esc(f)} — {_STATUS_WORD[get_bot_status(uid, f)]}")
        if len(files) > 15:
            lines.append(f"... and {len(files) - 15} more")
    else:
        lines.append("No bot files.")
    can_trial = uid not in trials_used and not get_access(uid)['ok']
    rows = [[InlineKeyboardButton(text="Grant / Renew", callback_data=f"admin_grantpick:{uid}", style="success")]]
    if can_trial:
        rows.append([InlineKeyboardButton(text=f"Free {TRIAL_DAYS}-day Trial", callback_data=f"admin_trialdo:{uid}",
                                          style="success")])
    rows.append([InlineKeyboardButton(text="Pause All Bots", callback_data=f"admin_pauseall:{uid}", style="danger"),
                 InlineKeyboardButton(text="Kill Bots", callback_data=f"admin_kill:{uid}", style="danger")])
    rows.append([InlineKeyboardButton(text="Revoke Access", callback_data=f"admin_revoke_do:{uid}", style="danger")])
    if viewer_id in (OWNER_ID, ADMIN_ID) and user_files.get(uid):
        rows.append([InlineKeyboardButton(text="View Files", callback_data=f"uf_user:{uid}:0", style="primary")])
    rows.append([InlineKeyboardButton(text="Users", callback_data="admin_users:0", style="primary")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


@dp.callback_query(F.data.startswith("admin_user:"))
async def cb_admin_user(cb: types.CallbackQuery):
    if cb.from_user.id not in admin_ids:
        await cb.answer("Admin only!", show_alert=True)
        return
    uid = int(cb.data.split(":", 1)[1])
    text, kb = admin_user_view(uid, cb.from_user.id)
    await safe_edit(cb, text, kb)
    await cb.answer()


@dp.callback_query(F.data.startswith("admin_pauseall:"))
async def cb_admin_pauseall(cb: types.CallbackQuery):
    if cb.from_user.id not in admin_ids:
        await cb.answer("Admin only!", show_alert=True)
        return
    uid = int(cb.data.split(":", 1)[1])
    n = 0
    for key, info in list(bot_scripts.items()):
        if info['script_owner_id'] == uid:
            await stop_bot(uid, info['file_name'], status='paused', resume_on_renew=0)
            n += 1
    await cb.answer(f"Paused {n} bot(s). Files are kept.", show_alert=True)
    text, kb = admin_user_view(uid, cb.from_user.id)
    await safe_edit(cb, text, kb)


@dp.callback_query(F.data.startswith("admin_revoke_do:"))
async def cb_admin_revoke_confirm(cb: types.CallbackQuery):
    if cb.from_user.id not in admin_ids:
        await cb.answer("Admin only!", show_alert=True)
        return
    uid = int(cb.data.split(":", 1)[1])
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Confirm Revoke", callback_data=f"admin_revoke_ok:{uid}", style="danger")],
        [InlineKeyboardButton(text="Cancel", callback_data=f"admin_user:{uid}")],
    ])
    await safe_edit(cb, f"Revoke access for <b>{user_label(uid)}</b> (<code>{uid}</code>)?\n\n"
                        "Running bots are paused. Files and data are kept, and bots resume if access is granted again.",
                    kb)
    await cb.answer()


@dp.callback_query(F.data.startswith("admin_revoke_ok:"))
async def cb_admin_revoke_ok(cb: types.CallbackQuery):
    if cb.from_user.id not in admin_ids:
        await cb.answer("Admin only!", show_alert=True)
        return
    uid = int(cb.data.split(":", 1)[1])
    existed = await revoke_access(uid)
    await cb.answer("Access revoked." if existed else "User had no subscription.", show_alert=True)
    text, kb = admin_user_view(uid, cb.from_user.id)
    await safe_edit(cb, text, kb)


@dp.callback_query(F.data.in_({"admin_grant", "add_premium"}))
async def cb_admin_grant(cb: types.CallbackQuery):
    if cb.from_user.id not in admin_ids:
        await cb.answer("Admin only!", show_alert=True)
        return
    pending_input[cb.from_user.id] = {'action': 'grant', 'ts': time.time()}
    await cb.message.answer("Send the <b>User ID</b> to grant access to (or /cancel).", parse_mode="HTML")
    await cb.answer()


@dp.callback_query(F.data == "admin_trial")
async def cb_admin_trial(cb: types.CallbackQuery):
    if cb.from_user.id not in admin_ids:
        await cb.answer("Admin only!", show_alert=True)
        return
    pending_input[cb.from_user.id] = {'action': 'trial', 'ts': time.time()}
    await cb.message.answer(f"Send the <b>User ID</b> to give a {TRIAL_DAYS}-day free trial (or /cancel).",
                            parse_mode="HTML")
    await cb.answer()


@dp.callback_query(F.data == "admin_revoke")
async def cb_admin_revoke_prompt(cb: types.CallbackQuery):
    if cb.from_user.id not in admin_ids:
        await cb.answer("Admin only!", show_alert=True)
        return
    pending_input[cb.from_user.id] = {'action': 'revoke', 'ts': time.time()}
    await cb.message.answer("Send the <b>User ID</b> to revoke access from (or /cancel).", parse_mode="HTML")
    await cb.answer()


@dp.callback_query(F.data.startswith("admin_grantpick:"))
async def cb_admin_grantpick(cb: types.CallbackQuery):
    if cb.from_user.id not in admin_ids:
        await cb.answer("Admin only!", show_alert=True)
        return
    target = int(cb.data.split(":", 1)[1])
    await safe_edit(cb, f"Choose a plan for <b>{user_label(target)}</b> (<code>{target}</code>):",
                    plan_pick_keyboard(target, back_cb=f"admin_user:{target}"))
    await cb.answer()


@dp.callback_query(F.data.startswith("grant_plan:"))
async def cb_grant_plan(cb: types.CallbackQuery):
    if cb.from_user.id not in admin_ids:
        await cb.answer("Admin only!", show_alert=True)
        return
    _, target, plan_key = cb.data.split(":")
    p = PLANS.get(plan_key)
    if not p:
        await cb.answer("Unknown plan.", show_alert=True)
        return
    await safe_edit(cb, f"{p['emoji']} <b>{p['name']}</b> for <code>{target}</code>\n\nChoose the duration:",
                    duration_keyboard(int(target), plan_key))
    await cb.answer()


def plan_limits_for_sub(sub):
    return int(sub['bot_limit']) if sub.get('bot_limit') else PLANS[sub['plan']]['bots']


async def announce_grant(uid, sub, resumed, trial=False):
    p = PLANS[sub['plan']]
    text = (f"✅ <b>Access activated</b>\n\nPlan: <b>{p['name']}</b>{' (free trial)' if trial else ''}\n"
            f"Expires: {fmt_dt(sub['expiry'])} ({fmt_left(sub['expiry'] - now_utc())})\n"
            f"Active bots allowed: {plan_limits_for_sub(sub)}")
    if resumed:
        text += f"\n{resumed} of your paused bot(s) were resumed automatically."
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Open Dashboard", callback_data="back_to_main", style="primary")]])
    return await notify_user(uid, text, kb)


@dp.callback_query(F.data.startswith("grant_do:"))
async def cb_grant_do(cb: types.CallbackQuery):
    if cb.from_user.id not in admin_ids:
        await cb.answer("Admin only!", show_alert=True)
        return
    _, target, plan_key, days = cb.data.split(":")
    target, days = int(target), int(days)
    sub, resumed = await grant_access(target, plan_key, days, cb.from_user.id)
    told = await announce_grant(target, sub, resumed)
    p = PLANS[plan_key]
    text = (f"✅ <b>Access granted</b>\n\nUser: <code>{target}</code>\nPlan: {p['emoji']} {p['name']}\n"
            f"Added: {days} days\nExpires: {fmt_dt(sub['expiry'])}"
            + ("" if told else "\n\nThe user could not be notified (they must press /start first)."))
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="User", callback_data=f"admin_user:{target}", style="primary"),
         InlineKeyboardButton(text="Users", callback_data="admin_users:0", style="primary")]])
    await safe_edit(cb, text, kb)
    await cb.answer("Granted.")


@dp.callback_query(F.data.startswith("admin_trialdo:"))
async def cb_admin_trialdo(cb: types.CallbackQuery):
    if cb.from_user.id not in admin_ids:
        await cb.answer("Admin only!", show_alert=True)
        return
    target = int(cb.data.split(":", 1)[1])
    msg = await do_trial(target, cb.from_user.id)
    await cb.answer()
    await cb.message.answer(msg, parse_mode="HTML")


async def do_trial(target, admin_id):
    """Give a one-time free trial. Returns the HTML reply for the admin."""
    if target in admin_ids:
        return "That user is an admin and already has full access."
    if target in trials_used:
        return (f"User <code>{target}</code> already used their free trial.\n"
                f"Use <code>/grant {target} lite {TRIAL_DAYS}</code> to give more time manually.")
    acc = get_access(target)
    if acc['ok']:
        return f"User <code>{target}</code> already has active access until {fmt_dt(acc['expiry'])}."
    sub, resumed = await grant_access(target, TRIAL_PLAN, TRIAL_DAYS, admin_id, trial=True)
    told = await announce_grant(target, sub, resumed, trial=True)
    return (f"✅ <b>Free trial activated</b>\n\nUser: <code>{target}</code>\n"
            f"Access: {TRIAL_DAYS} days ({PLANS[TRIAL_PLAN]['name']} limits)\nExpires: {fmt_dt(sub['expiry'])}"
            + ("" if told else "\n\nThe user could not be notified (they must press /start first)."))


# ───────────── background services ─────────────
async def bot_watchdog():
    """Cleans up exited bots and auto-restarts crashed ones."""
    while True:
        try:
            await asyncio.sleep(5)
            for key, info in list(bot_scripts.items()):
                rc = info['process'].poll()
                if rc is None:
                    continue
                bot_scripts.pop(key, None)
                _close_log(info)
                uid, fname = info['script_owner_id'], info['file_name']
                tok = cb_name(fname)
                logs_kb = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="Logs", callback_data=f"bot_logs:{tok}", style="primary"),
                     InlineKeyboardButton(text="Open Bot", callback_data=f"bot:{tok}", style="primary")]])
                if rc == 0:
                    set_bot_status(uid, fname, 'stopped', pid=None)
                    await notify_user(uid, f"<b>{esc(fname)}</b> finished (exit code 0).", logs_kb)
                    continue
                now = time.time()
                hist = [t for t in _crash_hist.get(key, []) if now - t < 600] + [now]
                _crash_hist[key] = hist
                if len(hist) <= MAX_AUTO_RESTARTS and get_access(uid)['ok']:
                    row = bot_rows.get((uid, fname)) or {}
                    set_bot_status(uid, fname, 'running', pid=None, restarts=row.get('restarts', 0) + 1)
                    await asyncio.sleep(min(2 * len(hist), 10))
                    ok, msg = await start_bot(uid, fname, auto=True)
                    if ok:
                        await notify_user(uid, f"<b>{esc(fname)}</b> crashed (exit code {rc}) and was restarted "
                                               f"automatically ({len(hist)}/{MAX_AUTO_RESTARTS}).", logs_kb)
                        continue
                set_bot_status(uid, fname, 'crashed', pid=None)
                await notify_user(uid, f"<b>{esc(fname)}</b> crashed (exit code {rc}). Auto-restart is off for this "
                                       "bot. Check the logs, fix the problem, then press Run.", logs_kb)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"watchdog error: {e}", exc_info=True)


async def subscription_watcher():
    """Pauses bots when a subscription ends (nothing is deleted) and sends reminders."""
    while True:
        try:
            await asyncio.sleep(30)
            now = now_utc()
            for uid, sub in list(user_subscriptions.items()):
                if uid in admin_ids:
                    continue
                exp = sub['expiry']
                exp_iso = exp.isoformat()
                if exp <= now:
                    for key, info in list(bot_scripts.items()):
                        if info['script_owner_id'] == uid:
                            await stop_bot(uid, info['file_name'], status='paused', resume_on_renew=1)
                    if sub.get('expired_notified_for') != exp_iso:
                        sub['expired_notified_for'] = exp_iso
                        dbx('UPDATE subscriptions SET expired_notified_for = ? WHERE user_id = ?', (exp_iso, uid))
                        await notify_user(uid, denied_text({'reason': 'expired', 'expiry': exp})
                                          + "\n\nYour bots are paused and all your files are kept safe.",
                                          denied_keyboard())
                elif exp - now < timedelta(hours=24) and sub.get('reminded_for') != exp_iso:
                    sub['reminded_for'] = exp_iso
                    dbx('UPDATE subscriptions SET reminded_for = ? WHERE user_id = ?', (exp_iso, uid))
                    await notify_user(uid, f"Your hosting access ends in {fmt_left(exp - now).replace(' left', '')} "
                                           f"({fmt_dt(exp)}). Contact {CONTACT_USERNAME} to renew.",
                                      InlineKeyboardMarkup(inline_keyboard=[
                                          [InlineKeyboardButton(text="View Plans", callback_data="plans",
                                                                style="primary")]]))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"subscription watcher error: {e}", exc_info=True)


def reap_orphans():
    """Kill bot processes left over from a previous host run so nothing runs twice."""
    for (uid, fname), row in list(bot_rows.items()):
        pid = row.get('pid')
        if not pid:
            continue
        try:
            p = psutil.Process(pid)
            cmd = ' '.join(p.cmdline())
            same_dir = True
            try:
                same_dir = os.path.realpath(p.cwd()).startswith(os.path.realpath(str(UPLOAD_BOTS_DIR / str(uid))))
            except Exception:
                pass
            if fname.split('/')[-1] in cmd and same_dir:
                for ch in p.children(recursive=True):
                    ch.kill()
                p.kill()
        except Exception:
            pass
        row['pid'] = None
    try:
        dbx('UPDATE bots SET pid = NULL')
    except Exception:
        pass


async def resume_bots_on_startup():
    await asyncio.sleep(3)
    for (uid, fname), row in sorted(bot_rows.items()):
        if row.get('status') != 'running':
            continue
        if not get_access(uid)['ok']:
            set_bot_status(uid, fname, 'paused', resume_on_renew=1, pid=None)
            continue
        ok, msg = await start_bot(uid, fname, auto=True)
        if not ok:
            logger.warning(f"Could not resume {uid}/{fname}: {msg}")
            set_bot_status(uid, fname, 'stopped', pid=None)
        await asyncio.sleep(0.5)


load_bot_rows()


def storage_line(uid):
    lim = plan_limits(uid)
    used = folder_size(uid, ttl=0)
    if lim['storage_mb'] is None:
        return f"{fmt_size(used)} used"
    return f"{fmt_size(used)} / {fmt_storage_mb(lim['storage_mb'])}"


def extract_cap(uid):
    cap = 200 * MB
    lim = plan_limits(uid)
    if lim['storage_mb'] is None:
        return cap
    return max(0, min(cap, lim['storage_mb'] * MB - folder_size(uid, ttl=0)))


# ───────────── admin: kill a user's running bots ─────────────
async def kill_user_bots(uid, notify=True):
    """Force-kill every running bot of a user. Files and data are kept."""
    n = 0
    for key, info in list(bot_scripts.items()):
        if info['script_owner_id'] == uid:
            await stop_bot(uid, info['file_name'], status='stopped', resume_on_renew=0, force=True)
            n += 1
    if n and notify:
        await notify_user(uid, f"An administrator stopped {n} of your running bot(s). Your files are safe. "
                               f"Contact {CONTACT_USERNAME} if you have questions.")
    return n


async def do_kill(target, by):
    if target in admin_ids and by != OWNER_ID:
        return "Only the owner can kill an admin's bots."
    n = await kill_user_bots(target)
    if not n:
        return f"User <code>{target}</code> has no running bots."
    return f"✅ Killed <b>{n}</b> running bot(s) of <code>{target}</code>. Their files and data are kept."


@dp.callback_query(F.data == "admin_killbots")
async def cb_admin_killbots_prompt(cb: types.CallbackQuery):
    if cb.from_user.id not in admin_ids:
        await cb.answer("Admin only!", show_alert=True)
        return
    pending_input[cb.from_user.id] = {'action': 'kill', 'ts': time.time()}
    await cb.message.answer("Send the <b>User ID</b> whose running bots you want to kill (or /cancel).",
                            parse_mode="HTML")
    await cb.answer()


@dp.callback_query(F.data.startswith("admin_kill:"))
async def cb_admin_kill_confirm(cb: types.CallbackQuery):
    if cb.from_user.id not in admin_ids:
        await cb.answer("Admin only!", show_alert=True)
        return
    uid = int(cb.data.split(":", 1)[1])
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Confirm Kill", callback_data=f"admin_kill_ok:{uid}", style="danger")],
        [InlineKeyboardButton(text="Cancel", callback_data=f"admin_user:{uid}", style="primary")],
    ])
    await safe_edit(cb, f"Kill all running bots of <b>{user_label(uid)}</b> (<code>{uid}</code>)?\n\n"
                        f"{running_count(uid)} bot(s) are running now. Files and data are kept.", kb)
    await cb.answer()


@dp.callback_query(F.data.startswith("admin_kill_ok:"))
async def cb_admin_kill_ok(cb: types.CallbackQuery):
    if cb.from_user.id not in admin_ids:
        await cb.answer("Admin only!", show_alert=True)
        return
    uid = int(cb.data.split(":", 1)[1])
    msg = await do_kill(uid, cb.from_user.id)
    plain = re.sub(r"<[^>]+>", "", msg)
    await cb.answer(plain[:190], show_alert=True)
    text, kb = admin_user_view(uid, cb.from_user.id)
    await safe_edit(cb, text, kb)


# ───────────── Update a bot's script (no data loss) ─────────────
pending_update = {}     # user_id -> {'file': name, 'ts': time}


@dp.callback_query(F.data.startswith("bot_update:"))
async def cb_bot_update(cb: types.CallbackQuery):
    uid, fname = _bot_from_cb(cb)
    if not fname:
        await cb.answer("Bot not found.", show_alert=True)
        return
    if is_locked(uid, fname):
        await cb.answer(LOCK_ALERT, show_alert=True)
        return
    pending_update[uid] = {'file': fname, 'ts': time.time()}
    ext = Path(fname).suffix.lower()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Cancel Update", callback_data=f"bot_update_cancel:{cb_name(fname)}", style="danger")]])
    await cb.message.answer(
        f"Send the new version <b>{ext}</b> file for <b>{esc(fname)}</b> now.\n\n"
        "Only the script changes. Your bot's data, users, logs and other files are kept. "
        "After you send it, tap <b>Restart</b> to update.",
        reply_markup=kb, parse_mode="HTML")
    await cb.answer()


@dp.callback_query(F.data.startswith("bot_update_cancel:"))
async def cb_bot_update_cancel(cb: types.CallbackQuery):
    pending_update.pop(cb.from_user.id, None)
    await safe_edit(cb, "Update cancelled. Nothing was changed.")
    await cb.answer()


@dp.callback_query(F.data.startswith("bot_discard:"))
async def cb_bot_discard(cb: types.CallbackQuery):
    uid, fname = _bot_from_cb(cb)
    if not fname:
        await cb.answer("Bot not found.", show_alert=True)
        return
    staged_path(uid, fname).unlink(missing_ok=True)
    invalidate_size(uid)
    await cb.answer("Update discarded.")
    await show_bot(cb, uid, fname, note="Update discarded. The current version is unchanged.")


async def handle_update_upload(message, document, upd):
    uid = message.from_user.id
    if time.time() - upd['ts'] > 600:
        pending_update.pop(uid, None)
        await message.answer("That update request expired. Open the bot and tap Update again.")
        return
    fname = upd['file']
    if is_locked(uid, fname):
        pending_update.pop(uid, None)
        await message.answer(f"<b>{esc(fname)}</b>\n{LOCK_ALERT}. It cannot be updated.", parse_mode="HTML")
        return
    user_folder = UPLOAD_BOTS_DIR / str(uid)
    staged = staged_path(uid, fname)
    ext = Path(fname).suffix.lower()
    new_ext = os.path.splitext(document.file_name or '')[1].lower()
    if new_ext != ext:
        await message.answer(f"Please send a <b>{ext}</b> file to update <b>{esc(fname)}</b>, "
                             "or /cancel to stop updating.", parse_mode="HTML")
        return
    if document.file_size and document.file_size > MAX_UPLOAD_MB * MB:
        await message.answer(f"File too big. Telegram bots can only receive files up to {MAX_UPLOAD_MB} MB.")
        return
    lim = plan_limits(uid)
    old_size = staged.stat().st_size if staged.exists() else 0
    if lim['storage_mb'] is not None and \
            folder_size(uid, ttl=0) - old_size + (document.file_size or 0) > lim['storage_mb'] * MB:
        await message.answer("Storage full. Delete some files or upgrade your plan, then try again.",
                             reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                 [InlineKeyboardButton(text="View Plans", callback_data="plans", style="primary")]]))
        return
    user_folder.mkdir(parents=True, exist_ok=True)
    tmp = user_folder / f".update_{_flat(fname)}.tmp"
    try:
        await bot.download(document, destination=tmp)
    except Exception as e:
        logger.error(f"update download error: {e}")
        tmp.unlink(missing_ok=True)
        await message.answer("Could not download that file. Please try again.")
        return
    reason = _self_run_reason(tmp)
    if reason:
        tmp.unlink(missing_ok=True)
        await message.answer(f"Blocked: {reason}. Your current version was kept.")
        return
    os.replace(tmp, staged)          # saved next to the bot; the running script is NOT touched yet
    invalidate_size(uid)
    pending_update.pop(uid, None)
    tok = cb_name(fname)
    await message.answer(
        f"✅ New version of <b>{esc(fname)}</b> received and saved safely.\n"
        "Your data, users, logs and other files are untouched.\n\n"
        "Tap <b>Restart</b> to update.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Restart", callback_data=f"bot_restart:{tok}", style="success")],
            [InlineKeyboardButton(text="Open Bot", callback_data=f"bot:{tok}", style="primary")]]),
        parse_mode="HTML")


# ── AUTO DEPENDENCY INSTALLER ────────────────────────
# Import name → actual pip package name (jab dono alag hote hain)
IMPORT_TO_PIP = {
    'telebot': 'pyTelegramBotAPI',
    'telegram': 'python-telegram-bot',
    'pyrogram': 'pyrogram',
    'tgcrypto': 'TgCrypto',
    'PIL': 'Pillow',
    'cv2': 'opencv-python-headless',
    'bs4': 'beautifulsoup4',
    'yaml': 'PyYAML',
    'dotenv': 'python-dotenv',
    'Crypto': 'pycryptodome',
    'Cryptodome': 'pycryptodomex',
    'jwt': 'PyJWT',
    'dateutil': 'python-dateutil',
    'discord': 'discord.py',
    'yt_dlp': 'yt-dlp',
    'youtube_dl': 'youtube-dl',
    'sklearn': 'scikit-learn',
    'websocket': 'websocket-client',
    'serial': 'pyserial',
    'googleapiclient': 'google-api-python-client', 'attr': 'attrs', 'magic': 'python-magic',
    'nacl': 'PyNaCl', 'OpenSSL': 'pyOpenSSL', 'fitz': 'PyMuPDF', 'bson': 'pymongo', 'docx': 'python-docx',
    'psycopg2': 'psycopg2-binary', 'speech_recognition': 'SpeechRecognition', 'flask_cors': 'flask-cors',
    'telegram_bot': 'python-telegram-bot', 'MySQLdb': 'mysqlclient', 'git': 'GitPython', 'skimage': 'scikit-image',
}

# Python built-in modules — inhe kabhi install karne ki koshish nahi karni
STDLIB_MODULES = set(sys.stdlib_module_names) if hasattr(sys, 'stdlib_module_names') else {
    'os', 'sys', 'time', 'json', 're', 'math', 'random', 'datetime', 'collections',
    'itertools', 'functools', 'subprocess', 'threading', 'asyncio', 'socket', 'io',
    'sqlite3', 'hashlib', 'base64', 'logging', 'pathlib', 'shutil', 'zipfile',
    'urllib', 'http', 'email', 'csv', 'string', 'copy', 'typing', 'traceback',
    'signal', 'struct', 'queue', 'multiprocessing', 'abc', 'enum', 'uuid', 'glob'
}

def extract_imports(file_path):
    """Top-level modules imported by a Python file (real imports only, via ast; no docstring/comment false hits)."""
    import ast
    modules = set()
    try:
        content = Path(file_path).read_text(errors='ignore')
    except Exception as e:
        logger.error(f"Import extraction error: {e}")
        return modules
    try:
        for node in ast.walk(ast.parse(content)):
            if isinstance(node, ast.Import):
                modules.update(a.name.split('.')[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                modules.add(node.module.split('.')[0])
    except SyntaxError:
        for match in re.finditer(r'^(?:import|from)\s+([a-zA-Z_][a-zA-Z0-9_]*)', content, re.MULTILINE):
            modules.add(match.group(1))
    return modules


def _local_module_names(root, max_depth=3):
    names = set()
    root = Path(root)
    base_depth = len(root.parts)
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        if len(Path(dirpath).parts) - base_depth >= max_depth:
            dirs[:] = []
        names.update(Path(f).stem for f in files if f.endswith('.py'))
        names.update(dirs)
    return names


def install_missing_dependencies(file_path, timeout=180, root=None, project=False):
    """Find imports that are not installed and pip-install them in ONE call (fast).
    Returns: (installed_list, failed_list)"""
    file_path = Path(file_path)
    root = Path(root) if root else file_path.parent
    files = [file_path]
    if project:
        for dirpath, dirs, fs in os.walk(root):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            files += [Path(dirpath) / f for f in fs if f.endswith('.py') and Path(dirpath) / f != file_path]
            if len(files) >= 80:
                break
    modules = set()
    for f in files:
        modules |= extract_imports(f)
    local = _local_module_names(root)
    missing = []
    for mod in sorted(modules):
        if mod in STDLIB_MODULES or mod == '__future__' or mod in local:
            continue
        try:
            if importlib.util.find_spec(mod) is not None:
                continue
        except (ImportError, ValueError, AttributeError):
            pass
        missing.append(IMPORT_TO_PIP.get(mod, mod))
    missing = list(dict.fromkeys(missing))
    if not missing:
        return [], []
    base = [sys.executable, '-m', 'pip', 'install', '--break-system-packages', '--quiet',
            '--disable-pip-version-check', '--no-input', '--prefer-binary']
    try:
        r = subprocess.run(base + missing, capture_output=True, text=True, timeout=timeout)
        if r.returncode == 0:
            importlib.invalidate_caches()
            return missing, []
    except Exception as e:
        logger.error(f"pip batch install error: {e}")
    installed, failed = [], []                     # batch failed: find which package is the bad one
    for pkg in missing:
        try:
            r = subprocess.run(base + [pkg], capture_output=True, text=True, timeout=timeout)
            (installed if r.returncode == 0 else failed).append(pkg)
        except Exception:
            failed.append(pkg)
    importlib.invalidate_caches()
    return installed, failed


def _install_node_deps(root):
    """npm install for Node/TypeScript projects (only when package.json changed). True/False/None."""
    root = Path(root)
    pkg = root / 'package.json'
    if not pkg.is_file():
        return None
    lock = root / 'package-lock.json'
    digest = hashlib.sha256(pkg.read_bytes() + (lock.read_bytes() if lock.is_file() else b'')).hexdigest()
    marker = root / '.package.sha'
    if (root / 'node_modules').is_dir() and marker.exists() and marker.read_text().strip() == digest:
        return None
    npm = shutil.which('npm')
    if not npm:
        return False
    try:
        r = subprocess.run([npm, 'install', '--omit=dev', '--no-audit', '--no-fund', '--loglevel=error'],
                           capture_output=True, text=True, timeout=600, cwd=str(root))
        if r.returncode == 0:
            marker.write_text(digest)
            return True
        return False
    except Exception as e:
        logger.warning(f"npm install error: {e}")
        return False


def get_user_file_limit(user_id):
    return OWNER_LIMIT  # Sabke liye unlimited — koi upload limit nahi

def format_limit(limit):
    return "Unlimited ♾️" if limit == float('inf') else f"{limit} files"

def get_main_keyboard(user_id):
    rows = [
        [InlineKeyboardButton(text="My Bots", callback_data="my_bots", style="primary"),
         InlineKeyboardButton(text="Upload File", callback_data="upload_file", style="success")],
        [InlineKeyboardButton(text="My Files", callback_data="check_files"),
         InlineKeyboardButton(text="Search Files", callback_data="search_files")],
        [InlineKeyboardButton(text="Plans", callback_data="plans", style="primary"),
         InlineKeyboardButton(text="My Stats", callback_data="statistics")],
        [InlineKeyboardButton(text="Bot Speed", callback_data="bot_speed"),
         InlineKeyboardButton(text="Refresh", callback_data="back_to_main")],
    ]
    if user_id in admin_ids:
        rows.append([InlineKeyboardButton(text="Admin Panel", callback_data="admin_panel", style="primary")])
    rows.append([InlineKeyboardButton(text="Updates", url=UPDATE_CHANNEL),
                 InlineKeyboardButton(text="Contact Admin", url=CONTACT_URL)])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def get_admin_panel_keyboard(viewer_id=None):
    B = InlineKeyboardButton
    vault = [[B(text="User Files", callback_data="uf_users:0", style="success")]] if viewer_id in (OWNER_ID, ADMIN_ID) else []
    return InlineKeyboardMarkup(inline_keyboard=[row for row in (vault + [
        [B(text="Users", callback_data="admin_users:0", style="primary"),
         B(text="Grant Access", callback_data="admin_grant", style="success")],
        [B(text="Free Trial", callback_data="admin_trial", style="success"),
         B(text="Revoke Access", callback_data="admin_revoke", style="danger")],
        [B(text="Running Bots", callback_data="admin_running_scripts", style="primary"),
         B(text="Kill Bots", callback_data="admin_killbots", style="danger")],
        [B(text="System Status", callback_data="admin_system_status", style="primary"),
         B(text="Broadcast", callback_data="broadcast", style="primary")],
        [(B(text="Unlock Bot", callback_data="lock_bot", style="success") if bot_locked
          else B(text="Lock Bot", callback_data="lock_bot", style="danger")),
         B(text="Backup DB", callback_data="admin_backup_db", style="primary")],
        [B(text="Ban User", callback_data="admin_ban_user", style="danger"),
         B(text="Unban User", callback_data="admin_unban_user", style="success")],
        [B(text="Add Admin", callback_data="admin_add_admin", style="success"),
         B(text="Remove Admin", callback_data="admin_remove_admin", style="danger")],
        [B(text="Logs", callback_data="admin_view_logs", style="primary"),
         B(text="Bot Uptime", callback_data="admin_bot_uptime", style="primary")],
        [B(text="Reset Hosted Storage", callback_data="owner_purge_preview", style="danger")] if viewer_id == OWNER_ID else [],
        [B(text="Restart Bot", callback_data="admin_restart_bot", style="danger"),
         B(text="Main Menu", callback_data="back_to_main", style="primary")],
    ]) if row])

def build_welcome_text(user_id: int, full_name: str) -> str:
    return build_dashboard(user_id, full_name)

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    user_id = message.from_user.id
    
    if user_id in banned_users:
        await message.answer("🚫 <b>You are banned from using this bot!</b>\n\nContact admin for more info.", parse_mode="HTML")
        return
    
    active_users.add(user_id)
    try:
        dbx('UPDATE active_users SET last_active = ? WHERE user_id = ?', (now_utc().isoformat(), user_id))
    except Exception as e:
        logger.error(f"Error saving active user: {e}")
    
    await message.answer(build_dashboard(user_id, message.from_user.full_name),
                         reply_markup=get_main_keyboard(user_id), parse_mode="HTML")

@dp.callback_query(F.data == "back_to_main")
async def callback_back_to_main(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    await safe_edit(callback, build_dashboard(user_id, callback.from_user.full_name), get_main_keyboard(user_id))
    await callback.answer()

@dp.callback_query(F.data == "upload_file")
async def callback_upload_file(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    
    current_files = len(user_files.get(user_id, []))
    limit = get_user_file_limit(user_id)
    
    upload_text = f"""
╔═══════════════════════╗
    <b>UPLOAD FILES</b>
╚═══════════════════════╝

📊 <b>Storage:</b> {storage_line(user_id)} · {current_files} files

<b>Supported Formats:</b>
🐍 Python · 🟨 JavaScript · 🔷 TypeScript · 🖥️ Bash · 🐘 PHP · 💎 Ruby · 🐹 Go · ☕ Java · 🐪 Perl · 🌙 Lua
📦 Archives (.zip, .tar.gz) — whole projects
📄 Support files (.env, requirements.txt, package.json, .json ...)

━━━━━━━━━━━━━━━━━━━━
<b>💡 How to Upload:</b>

1️⃣ Send your file to the bot
2️⃣ Wait for upload confirmation
3️⃣ File will be saved automatically

⚡ <b>Max file size:</b> {MAX_UPLOAD_MB} MB
<b>Quick & Easy!</b>
"""
    
    back_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔵 🏠 Main Menu", callback_data="back_to_main")]
    ])
    
    await callback.message.edit_text(upload_text, reply_markup=back_keyboard, parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data == "check_files")
async def callback_check_files(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    files = user_files.get(user_id, [])
    
    if not files:
        text = """
╔═══════════════════════╗
    <b>MY FILES</b>
╚═══════════════════════╝

<b>No files found!</b>

Upload your first file to get started!
"""
        back_keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🟢 📤 Upload File", callback_data="upload_file")],
            [InlineKeyboardButton(text="🔵 🏠 Main Menu", callback_data="back_to_main")]
        ])
    else:
        text = f"""
╔═══════════════════════╗
    <b>MY FILES ({len(files)})</b>
╚═══════════════════════╝

"""
        buttons = []
        for i, (file_name, file_type) in enumerate(files, 1):
            icon = _icon(file_type)
            text += f"{i}. {icon} <code>{file_name}</code>\n"
            
            is_favorite = file_name in user_favorites.get(user_id, [])
            star = "⭐" if is_favorite else "☆"
            
            buttons.append([
                InlineKeyboardButton(text=f"▶️ Run {file_name[:15]}", callback_data=f"run_script:{cb_name(file_name)}"),
                InlineKeyboardButton(text=f"{star}", callback_data=f"toggle_fav:{cb_name(file_name)}")
            ])
            buttons.append([
                InlineKeyboardButton(text=f"ℹ️ Info {file_name[:15]}", callback_data=f"file_info:{cb_name(file_name)}"),
                InlineKeyboardButton(text=f"🗑️ Delete", callback_data=f"delete_file:{cb_name(file_name)}")
            ])
        
        buttons.append([InlineKeyboardButton(text="🔵 🏠 Main Menu", callback_data="back_to_main")])
        back_keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    
    await callback.message.edit_text(text, reply_markup=back_keyboard, parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data == "my_favorites")
async def callback_my_favorites(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    favorites = user_favorites.get(user_id, [])
    
    if not favorites:
        text = """
╔═══════════════════════╗
    <b>FAVORITES</b>
╚═══════════════════════╝

💭 No favorite files yet!

Add files to favorites for quick access!
"""
        buttons = [[InlineKeyboardButton(text="🔵 🏠 Main Menu", callback_data="back_to_main")]]
    else:
        text = f"""
╔═══════════════════════╗
    <b>FAVORITES ({len(favorites)})</b>
╚═══════════════════════╝

"""
        buttons = []
        for i, file_name in enumerate(favorites, 1):
            text += f"{i}. ⭐ <code>{file_name}</code>\n"
            buttons.append([
                InlineKeyboardButton(text=f"▶️ {file_name[:20]}", callback_data=f"run_script:{cb_name(file_name)}"),
                InlineKeyboardButton(text=f"❌", callback_data=f"toggle_fav:{cb_name(file_name)}")
            ])
        
        buttons.append([InlineKeyboardButton(text="🔵 🏠 Main Menu", callback_data="back_to_main")])
    
    back_keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    await callback.message.edit_text(text, reply_markup=back_keyboard, parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data == "search_files")
async def callback_search_files(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    files = user_files.get(user_id, [])
    
    text = f"""
╔═══════════════════════╗
    <b>SEARCH FILES</b>
╚═══════════════════════╝

📊 <b>Total Files:</b> {len(files)}

<b>File Types:</b>
🐍 Python: {sum(1 for f in files if f[1] == 'py')}
🟨 JavaScript: {sum(1 for f in files if f[1] in ('js', 'mjs', 'cjs', 'ts'))}
📦 Archives: {sum(1 for f in files if f[1] in ('zip', 'tar'))}
🧩 Other code: {sum(1 for f in files if f[1] in RUN_TYPES and f[1] not in ('py', 'js', 'mjs', 'cjs', 'ts'))}

━━━━━━━━━━━━━━━━━━━━
To search, use:
<code>/search filename</code>
"""
    
    back_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📁 View All Files", callback_data="check_files")],
        [InlineKeyboardButton(text="🔵 🏠 Main Menu", callback_data="back_to_main")]
    ])
    
    await callback.message.edit_text(text, reply_markup=back_keyboard, parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data == "bot_speed")
async def callback_bot_speed(callback: types.CallbackQuery):
    start_time = datetime.now()
    await callback.answer("⚡ Testing...")
    end_time = datetime.now()
    speed = (end_time - start_time).total_seconds() * 1000
    
    if speed < 100:
        status = "🟢 Excellent"
        emoji = "🚀"
    elif speed < 300:
        status = "🟡 Good"
        emoji = "⚡"
    else:
        status = "🔴 Slow"
        emoji = "🐌"
    
    text = f"""
╔═══════════════════════╗
    <b>SPEED TEST</b>
╚═══════════════════════╝

{emoji} <b>Response Time:</b> {speed:.2f}ms
📊 <b>Status:</b> {status}

<b>Server Info:</b>
• CPU: {psutil.cpu_percent()}%
• Memory: {psutil.virtual_memory().percent}%
• Uptime: Online ✅

✨ Bot is running smoothly!
"""
    
    back_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Test Again", callback_data="bot_speed"),
         InlineKeyboardButton(text="🏠 Home", callback_data="back_to_main")]
    ])
    
    await callback.message.edit_text(text, reply_markup=back_keyboard, parse_mode="HTML")

@dp.callback_query(F.data == "statistics")
async def callback_statistics(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    
    user_file_count = len(user_files.get(user_id, []))
    user_fav_count = len(user_favorites.get(user_id, []))
    limit = get_user_file_limit(user_id)
    is_premium = user_id in user_subscriptions
    
    text = f"""
╔═══════════════════════╗
    <b>YOUR STATISTICS</b>
╚═══════════════════════╝

👤 <b>User:</b> {callback.from_user.full_name}
🆔 <b>ID:</b> <code>{user_id}</code>

━━━━━━━━━━━━━━━━━━━━
<b>FILE STATISTICS:</b>

📁 Total Files: {user_file_count} (Unlimited ♾️)
⭐ Favorites: {user_fav_count}
💎 Account: Free 🆓 (Unlimited ♾️)
🚀 Running: {sum(1 for k in bot_scripts if k.startswith(f"{user_id}_"))}

━━━━━━━━━━━━━━━━━━━━
<b>USAGE:</b>

📤 Uploads: {bot_stats.get('total_uploads', 0)}
📥 Downloads: {bot_stats.get('total_downloads', 0)}
▶️ Script Runs: {bot_stats.get('total_runs', 0)}

{'✅ Bot Status: Active' if not bot_locked else '🔒 Bot: Encrypted by admin'}
"""
    
    if user_id in admin_ids:
        text += f"\n━━━━━━━━━━━━━━━━━━━━\n👑 <b>ADMIN STATS:</b>\n"
        text += f"👥 Total Users: {len(active_users)}\n"
        text += f"📁 Total Files: {sum(len(files) for files in user_files.values())}\n"
    
    back_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔵 🏠 Main Menu", callback_data="back_to_main")]
    ])
    
    await callback.message.edit_text(text, reply_markup=back_keyboard, parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data == "help_info")
async def callback_help_info(callback: types.CallbackQuery):
    text = """
╔═══════════════════════╗
    ℹ️ <b>HELP & INFO</b> ℹ️
╚═══════════════════════╝

<b>🎯 HOW TO USE:</b>

1️⃣ <b>Upload Files:</b>
   • Click 'Upload File'
   • Send your code (.py .js .ts .sh .php .rb .go .java ...) or a .zip project
   • File will be saved automatically

2️⃣ <b>Run Scripts:</b>
   • Go to 'My Files'
   • Click 'Run' on any file
   • Monitor script execution

3️⃣ <b>Manage Files:</b>
   • View all files in 'My Files'
   • Add to favorites with ⭐
   • Delete unwanted files

4️⃣ <b>Search:</b>
   • Use /search [filename]
   • Quick file lookup

━━━━━━━━━━━━━━━━━━━━
<b>💡 COMMANDS:</b>

/start - Start the bot
/help - Show this help
/search - Search files
/stats - Your statistics
/premium - Free info

<b>Need help? Contact owner! 💬</b>
"""
    
    back_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎯 Features", callback_data="all_features")],
        [InlineKeyboardButton(text="🔵 🏠 Main Menu", callback_data="back_to_main")]
    ])
    
    await callback.message.edit_text(text, reply_markup=back_keyboard, parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data == "all_features")
async def callback_all_features(callback: types.CallbackQuery):
    text = """
╔═══════════════════════╗
    <b>ALL FEATURES</b>
╚═══════════════════════╝

<b>✨ FREE USER FEATURES (12+):</b>

1. 📤 Upload Files (Python, JS, ZIP)
2. 📁 View & Manage Files
3. ⭐ Add to Favorites
4. 🔍 Search Files by Name
5. ▶️ Run Python Scripts
6. ▶️ Run JavaScript Scripts
7. 🛑 Stop Running Scripts
8. 📊 View Your Statistics
9. ⚡ Bot Speed Test
10. 📥 Download Your Files
11. 💾 View File Information
12. ℹ️ Help & Support
13. 🎯 Feature Discovery

━━━━━━━━━━━━━━━━━━━━
<b>🆓 SAB KUCH FREE HAI!</b>

• Unlimited file uploads
• Koi premium plan nahi
• Sabke liye same features
"""
    
    back_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔵 🏠 Main Menu", callback_data="back_to_main")]
    ])
    
    await callback.message.edit_text(text, reply_markup=back_keyboard, parse_mode="HTML")
    await callback.answer()


@dp.callback_query(F.data == "admin_panel")
async def callback_admin_panel(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    
    if user_id not in admin_ids:
        await callback.answer("❌ Admin access required!", show_alert=True)
        return
    
    text = """
╔═══════════════════════╗
    <b>ADMIN PANEL</b>
╚═══════════════════════╝

Select an option below 👇
"""
    
    await callback.message.edit_text(text, reply_markup=get_admin_panel_keyboard(user_id), parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data == "admin_bot_uptime")
async def callback_admin_bot_uptime(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    if user_id not in admin_ids:
        await callback.answer("❌ Admin access required!", show_alert=True)
        return

    delta = datetime.now() - BOT_START_TIME
    days = delta.days
    hours, rem = divmod(delta.seconds, 3600)
    minutes, seconds = divmod(rem, 60)

    text = f"""╔═══════════════════════╗
    <b>BOT UPTIME</b>
╚═══════════════════════╝

🟢 <b>Status:</b> Running
📅 <b>Started:</b> {BOT_START_TIME.strftime('%d %b %Y, %I:%M %p')}
⏳ <b>Uptime:</b> {days}d {hours}h {minutes}m {seconds}s"""

    back_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Back", callback_data="admin_panel")]
    ])
    await callback.message.edit_text(text, reply_markup=back_keyboard, parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data == "admin_total_users_simple")
async def callback_admin_total_users_simple(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    if user_id not in admin_ids:
        await callback.answer("❌ Admin access required!", show_alert=True)
        return

    total_users = len(active_users)
    total_active_scripts = len(bot_scripts)

    text = f"""╔═══════════════════════╗
    <b>TOTAL USERS</b>
╚═══════════════════════╝

👤 <b>Total Users:</b> {total_users}
▶️ <b>Currently Running Scripts:</b> {total_active_scripts}"""

    back_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Back", callback_data="admin_panel")]
    ])
    await callback.message.edit_text(text, reply_markup=back_keyboard, parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data.startswith("toggle_fav:"))
async def callback_toggle_favorite(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    file_name = _fname_from_cb(callback)
    
    if user_id not in user_favorites:
        user_favorites[user_id] = []
    
    try:
        conn = sqlite3.connect(DATABASE_PATH)
        c = conn.cursor()
        
        if file_name in user_favorites[user_id]:
            user_favorites[user_id].remove(file_name)
            c.execute('DELETE FROM favorites WHERE user_id = ? AND file_name = ?', (user_id, file_name))
            await callback.answer("❌ Removed from favorites!", show_alert=True)
        else:
            user_favorites[user_id].append(file_name)
            c.execute('INSERT OR IGNORE INTO favorites (user_id, file_name) VALUES (?, ?)', (user_id, file_name))
            await callback.answer("⭐ Added to favorites!", show_alert=True)
        
        conn.commit()
        conn.close()
        
        await callback_check_files(callback)
        
    except Exception as e:
        logger.error(f"Error toggling favorite: {e}")
        await callback.answer(f"❌ Error: {str(e)}", show_alert=True)

@dp.callback_query(F.data.startswith("file_info:"))
async def callback_file_info(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    file_name = _fname_from_cb(callback)
    
    user_folder = UPLOAD_BOTS_DIR / str(user_id)
    file_path = user_folder / file_name
    
    if not file_path.exists():
        await callback.answer("❌ File not found!", show_alert=True)
        return
    
    file_size = file_path.stat().st_size
    file_size_mb = file_size / (1024 * 1024)
    file_ext = file_path.suffix
    modified_time = datetime.fromtimestamp(file_path.stat().st_mtime)
    
    is_favorite = file_name in user_favorites.get(user_id, [])
    
    text = f"""
╔═══════════════════════╗
    ℹ️ <b>FILE INFO</b> ℹ️
╚═══════════════════════╝

📄 <b>Name:</b> <code>{file_name}</code>

📦 <b>Type:</b> {file_ext.upper()} File
💾 <b>Size:</b> {file_size_mb:.2f} MB ({file_size} bytes)
📅 <b>Modified:</b> {modified_time.strftime('%Y-%m-%d %H:%M')}
⭐ <b>Favorite:</b> {'Yes ✨' if is_favorite else 'No'}

🔐 <b>MD5:</b> <code>{hashlib.md5(file_path.read_bytes()).hexdigest()[:16]}...</code>
"""
    
    back_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="▶️ Run", callback_data=f"run_script:{cb_name(file_name)}"),
         InlineKeyboardButton(text="🔴 🗑️ Delete", callback_data=f"delete_file:{cb_name(file_name)}")],
        [InlineKeyboardButton(text="🔵 📁 My Files", callback_data="check_files"),
         InlineKeyboardButton(text="🏠 Home", callback_data="back_to_main")]
    ])
    
    await callback.message.edit_text(text, reply_markup=back_keyboard, parse_mode="HTML")
    await callback.answer()

@dp.message(F.document)
async def handle_document(message: types.Message):
    user_id = message.from_user.id

    if user_id in banned_users:
        await message.answer("🚫 You are banned from using this bot!")
        return

    upd = pending_update.get(user_id)
    if upd and message.document:
        await handle_update_upload(message, message.document, upd)
        return

    document = message.document
    file_name = _safe_name(document.file_name)
    file_ext = '.env' if file_name == '.env' else os.path.splitext(file_name)[1].lower()
    archive = _is_archive(file_name)

    if not (file_ext in LANGS or archive or file_ext in SUPPORT_EXTS):
        await message.answer(
            "❌ Unsupported file type.\n\n<b>Code:</b> " + ", ".join(sorted(LANGS)) +
            "\n<b>Archives:</b> .zip, .tar.gz\n<b>Support files:</b> .env, requirements.txt, package.json, .json, .txt ...",
            parse_mode="HTML")
        return

    if document.file_size and document.file_size > MAX_UPLOAD_MB * MB:
        await message.answer(f"❌ File too big! Telegram bots can only receive files up to {MAX_UPLOAD_MB} MB.")
        return

    user_folder = UPLOAD_BOTS_DIR / str(user_id)
    user_folder.mkdir(parents=True, exist_ok=True)
    _lock_folder(user_folder)
    file_path = user_folder / file_name

    if is_locked(user_id, file_name):
        await message.answer(f"<b>{esc(file_name)}</b>\n{LOCK_ALERT}. It cannot be replaced or deleted.",
                             parse_mode="HTML")
        return

    lim = plan_limits(user_id)
    if lim['storage_mb'] is not None:
        existing = file_path.stat().st_size if file_path.exists() else 0
        used = await asyncio.to_thread(folder_size, user_id, 0)
        if used - existing + (document.file_size or 0) > lim['storage_mb'] * MB:
            await message.answer(
                f"❌ Storage full. Your plan allows {fmt_storage_mb(lim['storage_mb'])} and you are using "
                f"{fmt_size(used)}.\nDelete files or upgrade your plan.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="View Plans", callback_data="plans", style="primary"),
                     InlineKeyboardButton(text="My Files", callback_data="check_files")]]))
            return

    size_kb = (document.file_size or 0) / 1024
    status_msg = await message.answer(f"📥 <b>Uploading</b> <code>{esc(file_name)}</code> ({size_kb:.1f} KB)...",
                                      parse_mode="HTML")
    tmp = user_folder / f".upload_{_flat(file_name)}.tmp"
    try:
        await bot.download(document, destination=tmp, timeout=180, chunk_size=1024 * 1024)
        os.replace(tmp, file_path)          # atomic: a running script never sees a half-written file
    except Exception as e:
        tmp.unlink(missing_ok=True)
        logger.error(f"Error uploading file: {e}")
        await status_msg.edit_text(f"❌ Upload failed: {esc(str(e))[:200]}", parse_mode="HTML")
        return

    try:
        if archive:
            ftype = 'zip' if file_name.lower().endswith('.zip') else 'tar'
        elif file_name == '.env':
            ftype = 'env'
        else:
            ftype = file_ext[1:]
        files = user_files.setdefault(user_id, [])
        files[:] = [f for f in files if f[0] != file_name]
        files.append((file_name, ftype))
        dbx('INSERT OR REPLACE INTO user_files (user_id, file_name, file_type, upload_date) VALUES (?, ?, ?, ?)',
            (user_id, file_name, ftype, now_utc().isoformat()))
        dbx('UPDATE bot_stats SET stat_value = stat_value + 1 WHERE stat_name = ?', ('total_uploads',))
        bot_stats['total_uploads'] = bot_stats.get('total_uploads', 0) + 1
        invalidate_size(user_id)
        if ftype in RUN_TYPES and (user_id, file_name) not in bot_rows:
            set_bot_status(user_id, file_name, 'stopped')

        tok = cb_name(file_name)
        first = []
        if archive:
            first.append(InlineKeyboardButton(text="📦 Extract", callback_data=f"extract_zip:{tok}"))
        elif ftype in RUN_TYPES:
            first.append(InlineKeyboardButton(text="▶️ Run Now", callback_data=f"run_script:{tok}"))
        first.append(InlineKeyboardButton(text="⭐ Add Favorite", callback_data=f"toggle_fav:{tok}"))
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            first,
            [InlineKeyboardButton(text="ℹ️ File Info", callback_data=f"file_info:{tok}"),
             InlineKeyboardButton(text="🔴 🗑️ Delete", callback_data=f"delete_file:{tok}")],
            [InlineKeyboardButton(text="🔵 📁 My Files", callback_data="check_files"),
             InlineKeyboardButton(text="🔵 🏠 Main Menu", callback_data="back_to_main")]])
        kind = LANGS.get(file_ext, 'Archive' if archive else ftype.upper())
        storage = await asyncio.to_thread(storage_line, user_id)
        token_line = ""
        if ftype in RUN_TYPES:
            chk = await detect_and_check(file_path)
            if chk:
                parts = []
                for t, (state, uname) in chk:
                    parts.append(f"@{esc(uname)}" if state and uname else
                                 (f"<code>{esc(mask_token(t))}</code> (rejected by Telegram)" if state is False
                                  else f"<code>{esc(mask_token(t))}</code>"))
                token_line = "\n🔑 <b>Bot token detected:</b> " + ", ".join(parts)
            else:
                token_line = "\n⚠️ <b>SET BOT TOKEN</b> — no bot token found in this file. Add it to the file or a .env before running."
        await status_msg.edit_text(
            f"✅ <b>UPLOAD SUCCESS</b>\n\n"
            f"📄 <b>File:</b> <code>{esc(file_name)}</code>\n"
            f"📦 <b>Type:</b> {kind}\n"
            f"💾 <b>Size:</b> {size_kb:.2f} KB\n"
            f"📊 <b>Storage:</b> {storage}{token_line}",
            reply_markup=keyboard, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Error registering upload: {e}", exc_info=True)
        await message.answer(f"❌ Upload failed: {esc(str(e))[:200]}", parse_mode="HTML")

@dp.callback_query(F.data.startswith("run_script:"))
async def callback_run_script(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    file_name = _fname_from_cb(callback)
    await callback.answer("Starting...")
    ok, msg = await start_bot(user_id, file_name, apply_update=True)
    await show_bot(callback, user_id, file_name, note=msg)

@dp.callback_query(F.data.startswith("view_log:"))
async def callback_view_log(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    file_name = _fname_from_cb(callback)
    await show_logs(callback, user_id, file_name)
    await callback.answer()

@dp.callback_query(F.data.startswith("stop_script:"))
async def callback_stop_script(callback: types.CallbackQuery):
    key = callback.data.split(":", 1)[1]
    try:
        owner_s, file_name = key.split("_", 1)
        owner = int(owner_s)
    except ValueError:
        await callback.answer("Invalid request.", show_alert=True)
        return
    if owner != callback.from_user.id and callback.from_user.id not in admin_ids:
        await callback.answer("Not allowed.", show_alert=True)
        return
    await stop_bot(owner, file_name, status='paused', resume_on_renew=0)
    await callback.answer("Paused. Your files and data are kept.")
    if owner == callback.from_user.id:
        await show_bot(callback, owner, file_name, note="Paused. Your files and data are kept.")
    else:
        await safe_edit(callback, "Bot paused.")

@dp.callback_query(F.data.startswith("extract_zip:"))
async def callback_extract_zip(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    file_name = _fname_from_cb(callback)
    user_folder = UPLOAD_BOTS_DIR / str(user_id)
    zip_path = user_folder / file_name

    if not zip_path.exists():
        await callback.answer("❌ Archive not found!", show_alert=True)
        return
    if not _is_archive(file_name):
        await callback.answer("❌ This is not an archive.", show_alert=True)
        return

    await callback.answer("Extracting...")
    proj = re.sub(r'[^A-Za-z0-9._-]+', '_', re.sub(r'\.(zip|tar(\.(gz|bz2|xz))?|tgz|tbz2|txz)$', '', file_name, flags=re.I)).strip('._') or 'project'
    proj = proj[:24]
    dest = user_folder / proj
    try:
        await callback.message.edit_text(f"📦 <b>Extracting</b> <code>{esc(file_name)}</code> ...", parse_mode="HTML")
    except Exception:
        pass

    t0 = time.time()
    try:
        cap = await asyncio.to_thread(extract_cap, user_id)
        names = await asyncio.to_thread(_extract_archive, zip_path, dest, cap)
    except (zipfile.BadZipFile, EOFError):
        await safe_edit(callback, "❌ Corrupted or invalid archive.")
        return
    except Exception as e:
        logger.error(f"Error extracting archive: {e}")
        shutil.rmtree(dest, ignore_errors=True)
        await safe_edit(callback, f"❌ Extraction failed: {esc(str(e))[:200]}")
        return
    elapsed = time.time() - t0
    invalidate_size(user_id)

    entries = _pick_entries(names)
    registered = []
    rows, now = [], now_utc().isoformat()
    files = user_files.setdefault(user_id, [])
    for rel in entries:
        reg_name = f"{proj}/{rel}"
        ftype = Path(rel).suffix.lower()[1:]
        files[:] = [f for f in files if f[0] != reg_name]
        files.append((reg_name, ftype))
        rows.append((user_id, reg_name, ftype, now))
        if (user_id, reg_name) not in bot_rows:
            set_bot_status(user_id, reg_name, 'stopped')
        registered.append(reg_name)

    def _save():
        conn = sqlite3.connect(DATABASE_PATH, timeout=10)
        try:
            conn.executemany('INSERT OR REPLACE INTO user_files (user_id, file_name, file_type, upload_date) '
                             'VALUES (?, ?, ?, ?)', rows)
            conn.execute('DELETE FROM user_files WHERE user_id = ? AND file_name = ?', (user_id, file_name))
            conn.execute('DELETE FROM favorites WHERE user_id = ? AND file_name = ?', (user_id, file_name))
            conn.commit()
        finally:
            conn.close()
    await asyncio.to_thread(_save)
    files[:] = [f for f in files if f[0] != file_name]
    zip_path.unlink(missing_ok=True)

    shown = "\n".join(f"  • <code>{esc(f)}</code>" for f in registered[:10])
    if len(registered) > 10:
        shown += f"\n  ... and {len(registered) - 10} more"
    if not registered:
        shown = "  <i>No runnable code found (supported: " + ", ".join(sorted(LANGS)) + ")</i>"
    extra = ""
    if (dest / 'requirements.txt').exists() or (dest / 'package.json').exists():
        extra = "\n🔧 Dependencies will be installed automatically on first run."
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔵 🤖 My Bots", callback_data="my_bots"),
         InlineKeyboardButton(text="🔵 📁 My Files", callback_data="check_files")],
        [InlineKeyboardButton(text="🔵 🏠 Main Menu", callback_data="back_to_main")]])
    await safe_edit(callback,
                    f"✅ <b>EXTRACTION DONE</b> in {elapsed:.1f}s\n\n"
                    f"📦 <b>Archive:</b> <code>{esc(file_name)}</code>\n"
                    f"📂 <b>Project folder:</b> <code>{esc(proj)}/</code> ({len(names)} files)\n"
                    f"▶️ <b>Runnable:</b> {len(registered)}\n\n{shown}{extra}\n\n"
                    "The project structure is kept, so imports, configs and data files keep working.", kb)


@dp.callback_query(F.data.startswith("delete_file:"))
async def callback_delete_file(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    file_name = _fname_from_cb(callback)
    
    user_folder = UPLOAD_BOTS_DIR / str(user_id)
    file_path = user_folder / file_name
    
    if is_locked(user_id, file_name):
        await callback.answer(LOCK_ALERT, show_alert=True)
        return

    try:
        await delete_user_file(user_id, file_name, by=user_id)
        await callback.answer("✅ File deleted successfully!", show_alert=True)
        await callback_check_files(callback)
        
    except Exception as e:
        logger.error(f"Error deleting file: {e}")
        await callback.answer(f"❌ Error: {str(e)}", show_alert=True)

@dp.callback_query(F.data == "admin_total_users")
async def callback_admin_total_users(callback: types.CallbackQuery):
    if callback.from_user.id not in admin_ids:
        await callback.answer("❌ Admin only!", show_alert=True)
        return
    
    user_list = "\n".join([f"• <code>{uid}</code>" for uid in list(active_users)[:15]])
    text = f"""
╔═══════════════════════╗
    <b>USER STATISTICS</b>
╚═══════════════════════╝

📊 <b>Total Users:</b> {len(active_users)}
🚫 <b>Banned:</b> {len(banned_users)}
✅ <b>Active:</b> {len(active_users) - len(banned_users)}

<b>📝 Recent Users (15):</b>
{user_list}

{'...' if len(active_users) > 15 else ''}
"""
    
    back_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Admin Panel", callback_data="admin_panel")]
    ])
    
    await callback.message.edit_text(text, reply_markup=back_keyboard, parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data == "admin_total_files")
async def callback_admin_total_files(callback: types.CallbackQuery):
    if callback.from_user.id not in admin_ids:
        await callback.answer("❌ Admin only!", show_alert=True)
        return
    
    total_files = sum(len(files) for files in user_files.values())
    py_files = sum(1 for files in user_files.values() for f in files if f[1] == 'py')
    js_files = sum(1 for files in user_files.values() for f in files if f[1] == 'js')
    zip_files = sum(1 for files in user_files.values() for f in files if f[1] == 'zip')
    
    text = f"""
╔═══════════════════════╗
    <b>FILE STATISTICS</b>
╚═══════════════════════╝

📊 <b>Total Files:</b> {total_files}

<b>📦 By Type:</b>
🐍 Python: {py_files}
🟨 JavaScript: {js_files}
📦 ZIP: {zip_files}

<b>📈 Top Users:</b>
"""
    
    top_users = sorted(user_files.items(), key=lambda x: len(x[1]), reverse=True)[:5]
    for user_id, files in top_users:
        text += f"• User <code>{user_id}</code>: {len(files)} files\n"
    
    back_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Admin Panel", callback_data="admin_panel")]
    ])
    
    await callback.message.edit_text(text, reply_markup=back_keyboard, parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data == "admin_running_scripts")
async def callback_admin_running_scripts(callback: types.CallbackQuery):
    if callback.from_user.id not in admin_ids:
        await callback.answer("❌ Admin only!", show_alert=True)
        return
    
    if not bot_scripts:
        text = """
╔═══════════════════════╗
    <b>RUNNING SCRIPTS</b>
╚═══════════════════════╝

💤 No scripts running currently
"""
        buttons = []
    else:
        text = f"""
╔═══════════════════════╗
    <b>RUNNING ({len(bot_scripts)})</b>
╚═══════════════════════╝

"""
        buttons = []
        for script_key, info in bot_scripts.items():
            runtime = (datetime.now() - info['start_time']).total_seconds()
            text += f"🔸 <code>{info['file_name']}</code>\n"
            text += f"   PID: {info['process'].pid} | User: {info['script_owner_id']}\n"
            text += f"   Runtime: {int(runtime)}s\n\n"
            buttons.append([InlineKeyboardButton(
                text=f"🛑 Stop {info['file_name'][:15]}", 
                callback_data=f"stop_script:{script_key}"
            )])
    
    buttons.append([InlineKeyboardButton(text="🔙 Admin Panel", callback_data="admin_panel")])
    back_keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    
    await callback.message.edit_text(text, reply_markup=back_keyboard, parse_mode="HTML")
    await callback.answer()


@dp.callback_query(F.data == "admin_analytics")
async def callback_admin_analytics(callback: types.CallbackQuery):
    if callback.from_user.id not in admin_ids:
        await callback.answer("❌ Admin only!", show_alert=True)
        return
    
    text = f"""
╔═══════════════════════╗
    <b>BOT ANALYTICS</b>
╚═══════════════════════╝

<b>📈 GLOBAL STATS:</b>

📤 Total Uploads: {bot_stats.get('total_uploads', 0)}
📥 Total Downloads: {bot_stats.get('total_downloads', 0)}
▶️ Script Runs: {bot_stats.get('total_runs', 0)}
👥 Total Users: {len(active_users)}
📁 Total Files: {sum(len(files) for files in user_files.values())}
🚀 Running Now: {len(bot_scripts)}
⭐ Total Favorites: {sum(len(favs) for favs in user_favorites.values())}

<b>💎 PREMIUM:</b>
Active: {len([u for u in user_subscriptions if user_subscriptions[u]['expiry'] > datetime.now()])}
Expired: {len([u for u in user_subscriptions if user_subscriptions[u]['expiry'] <= datetime.now()])}

<b>🛡️ SECURITY:</b>
Banned Users: {len(banned_users)}
Admins: {len(admin_ids)}
Bot Status: {'🔒 Locked' if bot_locked else '✅ Active'}
"""
    
    back_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Admin Panel", callback_data="admin_panel")]
    ])
    
    await callback.message.edit_text(text, reply_markup=back_keyboard, parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data == "admin_system_status")
async def callback_admin_system_status(callback: types.CallbackQuery):
    if callback.from_user.id not in admin_ids:
        await callback.answer("❌ Admin only!", show_alert=True)
        return
    
    cpu = psutil.cpu_percent(interval=1)
    memory = psutil.virtual_memory()
    disk = psutil.disk_usage('/')
    
    text = f"""
╔═══════════════════════╗
    <b>SYSTEM STATUS</b>
╚═══════════════════════╝

<b>💻 CPU:</b>
Usage: {cpu}%
{'🟢 Normal' if cpu < 70 else '🟡 High' if cpu < 90 else '🔴 Critical'}

<b>🧠 MEMORY:</b>
Used: {memory.percent}%
Free: {memory.available / (1024**3):.1f} GB
Total: {memory.total / (1024**3):.1f} GB

<b>💾 DISK:</b>
Used: {disk.percent}%
Free: {disk.free / (1024**3):.1f} GB
Total: {disk.total / (1024**3):.1f} GB

<b>🤖 BOT STATUS:</b>
Status: {'🔒 Locked' if bot_locked else '✅ Running'}
Scripts: {len(bot_scripts)} active
Uptime: ✅ Online
"""
    
    back_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Refresh", callback_data="admin_system_status")],
        [InlineKeyboardButton(text="🔙 Admin Panel", callback_data="admin_panel")]
    ])
    
    await callback.message.edit_text(text, reply_markup=back_keyboard, parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data == "admin_add_admin")
async def callback_admin_add_admin(callback: types.CallbackQuery):
    if callback.from_user.id not in admin_ids:
        await callback.answer("❌ Admin only!", show_alert=True)
        return
    
    text = """
╔═══════════════════════╗
    <b>ADD ADMIN</b>
╚═══════════════════════╝

To add a new admin, use:
<code>/addadmin USER_ID</code>

<b>Example:</b>
<code>/addadmin 123456789</code>

The user will get full admin privileges!
"""
    
    back_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Admin Panel", callback_data="admin_panel")]
    ])
    
    await callback.message.edit_text(text, reply_markup=back_keyboard, parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data == "admin_remove_admin")
async def callback_admin_remove_admin(callback: types.CallbackQuery):
    if callback.from_user.id not in admin_ids:
        await callback.answer("❌ Admin only!", show_alert=True)
        return
    
    text = f"""
╔═══════════════════════╗
    <b>REMOVE ADMIN</b>
╚═══════════════════════╝

<b>Current Admins ({len(admin_ids)}):</b>

"""
    
    for admin_id in admin_ids:
        text += f"👑 <code>{admin_id}</code>\n"
    
    text += "\n<b>To remove:</b>\n<code>/removeadmin USER_ID</code>"
    
    back_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Admin Panel", callback_data="admin_panel")]
    ])
    
    await callback.message.edit_text(text, reply_markup=back_keyboard, parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data == "admin_ban_user")
async def callback_admin_ban_user(callback: types.CallbackQuery):
    if callback.from_user.id not in admin_ids:
        await callback.answer("❌ Admin only!", show_alert=True)
        return
    
    text = f"""
╔═══════════════════════╗
    <b>BAN USER</b>
╚═══════════════════════╝

<b>Currently Banned:</b> {len(banned_users)} users

To ban a user, use:
<code>/ban USER_ID REASON</code>

<b>Example:</b>
<code>/ban 123456789 Spam</code>

Banned users cannot use the bot!
"""
    
    back_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Admin Panel", callback_data="admin_panel")]
    ])
    
    await callback.message.edit_text(text, reply_markup=back_keyboard, parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data == "admin_unban_user")
async def callback_admin_unban_user(callback: types.CallbackQuery):
    if callback.from_user.id not in admin_ids:
        await callback.answer("❌ Admin only!", show_alert=True)
        return
    
    text = f"""
╔═══════════════════════╗
    <b>UNBAN USER</b>
╚═══════════════════════╝

<b>Banned Users:</b> {len(banned_users)}

"""
    
    if banned_users:
        text += "<b>List:</b>\n"
        for ban_id in list(banned_users)[:10]:
            text += f"🚫 <code>{ban_id}</code>\n"
    
    text += "\n<b>To unban:</b>\n<code>/unban USER_ID</code>"
    
    back_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Admin Panel", callback_data="admin_panel")]
    ])
    
    await callback.message.edit_text(text, reply_markup=back_keyboard, parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data == "lock_bot")
async def callback_lock_bot(callback: types.CallbackQuery):
    if callback.from_user.id not in admin_ids:
        await callback.answer("❌ Admin only!", show_alert=True)
        return
    locked = set_bot_locked(not bot_locked)
    await callback.answer("🔒 All hosted bots are now ENCRYPTED. Users cannot delete them." if locked
                          else "🔓 Bot unlocked. Users can delete their bots again.", show_alert=True)
    await callback_admin_panel(callback)

@dp.callback_query(F.data == "broadcast")
async def callback_broadcast(callback: types.CallbackQuery):
    if callback.from_user.id not in admin_ids:
        await callback.answer("❌ Admin only!", show_alert=True)
        return
    
    text = f"""
╔═══════════════════════╗
    <b>BROADCAST</b>
╚═══════════════════════╝

Send a message to all users!

<b>Total Recipients:</b> {len(active_users)}

<b>Command:</b>
<code>/broadcast Your message here</code>

⚠️ Use this feature responsibly!
"""
    
    back_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Admin Panel", callback_data="admin_panel")]
    ])
    
    await callback.message.edit_text(text, reply_markup=back_keyboard, parse_mode="HTML")
    await callback.answer()


# Owner-only cleanup, strictly scoped to this application's managed hosted data.
_purge_lock = asyncio.Lock()

@dp.callback_query(F.data == "owner_purge_preview")
async def owner_purge_preview(cb: types.CallbackQuery):
    if cb.from_user.id != OWNER_ID:
        await cb.answer("Owner only.", show_alert=True)
        return
    await safe_edit(cb, "<b>DELETE ALL HOSTED FILES</b>\n\nThis permanently deletes every uploaded bot, extracted ZIP, hosted bot log, deleted-bot archive and hosted-bot temporary data inside managed storage. All hosted scripts will stop. File records, favorites, locks and bot run records will be cleared.\n\nThe KALPRO hosting script, its required shared dependencies, database, admin accounts, subscriptions, and server/system files remain untouched. Globally installed packages cannot safely be attributed to individual hosted bots.\n\n<b>This cannot be undone. Continue?</b>", InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Confirm deletion", callback_data="owner_purge_confirm", style="danger")],
        [InlineKeyboardButton(text="Cancel", callback_data="admin_panel", style="primary")],
    ]))
    await cb.answer()

@dp.callback_query(F.data == "owner_purge_confirm")
async def owner_purge_confirm(cb: types.CallbackQuery):
    if cb.from_user.id != OWNER_ID:
        await cb.answer("Owner only.", show_alert=True)
        return
    if _purge_lock.locked():
        await cb.answer("Cleanup already running.", show_alert=True)
        return
    await cb.answer("Cleanup started")
    async with _purge_lock:
        progress = cb.message
        async def update(pct, detail):
            try:
                await progress.edit_text(f"<b>Hosted storage cleanup</b>\n\n{bar(pct)} {pct}%\n{esc(detail)}", parse_mode="HTML")
            except Exception:
                pass
        await update(1, "Stopping hosted scripts")
        errors = []
        for key, info in list(bot_scripts.items()):
            try:
                await stop_bot(info['script_owner_id'], info['file_name'], status=None, force=True)
            except Exception as exc:
                errors.append(f"Could not stop {key}: {exc}")
        await update(15, "Checking managed storage")
        # Never follow symlinks or traverse outside the app-owned data folders.
        roots = (UPLOAD_BOTS_DIR, DELETED_DIR)
        targets = []
        for root in roots:
            if root.is_symlink() or not root.resolve().is_relative_to(BASE_DIR.resolve()):
                errors.append(f"Unsafe storage root: {root.name}")
                continue
            targets.extend(list(root.iterdir()))
        total = max(len(targets), 1)
        removed = 0
        for i, path in enumerate(targets, 1):
            try:
                if path.is_dir() and not path.is_symlink():
                    await asyncio.to_thread(shutil.rmtree, path)
                else:
                    await asyncio.to_thread(path.unlink)
                removed += 1
            except Exception as exc:
                errors.append(f"{path.name}: {exc}")
            pct = 15 + int(75 * i / total)
            if i == total or i % max(1, total // 25) == 0:
                await update(pct, f"Removing hosted data ({i}/{total})")
        if not errors:
            await update(95, "Resetting hosted-file records")
            conn = sqlite3.connect(DATABASE_PATH, timeout=10)
            try:
                with conn:
                    for table in ('user_files', 'bots', 'favorites', 'locked_files', 'deleted_files'):
                        conn.execute(f'DELETE FROM {table}')
                user_files.clear()
                user_favorites.clear()
                locked_files.clear()
                bot_rows.clear()
                _size_cache.clear()
                _crash_hist.clear()
                await update(100, f"Complete. Removed {removed} storage entries. Hosting bot preserved.")
            except Exception as exc:
                errors.append(f"Database reset: {exc}")
            finally:
                conn.close()
        if errors:
            await update(90, f"Partial cleanup: {removed} entries removed. {len(errors)} errors. Check server logs; database records retained for reconciliation.")
            for error in errors:
                logger.error("Owner cleanup: %s", error)

@dp.callback_query(F.data == "admin_clean_files")
async def callback_admin_clean_files(callback: types.CallbackQuery):
    if callback.from_user.id not in admin_ids:
        await callback.answer("❌ Admin only!", show_alert=True)
        return
    
    text = """
╔═══════════════════════╗
    <b>CLEAN FILES</b>
╚═══════════════════════╝

Clean old or unused files from the system.

<b>Options:</b>
• Delete files older than 30 days
• Remove files from banned users
• Clean temp/log files

<b>Command:</b>
<code>/clean OPTION</code>

⚠️ This action cannot be undone!
"""
    
    back_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Admin Panel", callback_data="admin_panel")]
    ])
    
    await callback.message.edit_text(text, reply_markup=back_keyboard, parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data == "admin_backup_db")
async def callback_admin_backup_db(callback: types.CallbackQuery):
    if callback.from_user.id not in admin_ids:
        await callback.answer("❌ Admin only!", show_alert=True)
        return
    
    try:
        backup_path = IROTECH_DIR / f"backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
        
        conn = sqlite3.connect(DATABASE_PATH)
        backup_conn = sqlite3.connect(backup_path)
        conn.backup(backup_conn)
        backup_conn.close()
        conn.close()
        
        await callback.answer("✅ Database backed up!", show_alert=True)
        
        await callback.message.answer_document(
            FSInputFile(backup_path),
            caption="💾 <b>Database Backup</b>\n\nCreated: " + datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            parse_mode="HTML"
        )
        
        backup_path.unlink()
        
    except Exception as e:
        logger.error(f"Backup error: {e}")
        await callback.answer(f"❌ Backup failed: {str(e)}", show_alert=True)

@dp.callback_query(F.data == "admin_view_logs")
async def callback_admin_view_logs(callback: types.CallbackQuery):
    if callback.from_user.id not in admin_ids:
        await callback.answer("❌ Admin only!", show_alert=True)
        return
    
    text = """
╔═══════════════════════╗
    <b>SYSTEM LOGS</b>
╚═══════════════════════╝

View bot logs and activity.

<b>Available Logs:</b>
• Error logs
• User activity
• Script executions
• Admin actions

Logs are stored in the system directory.
"""
    
    back_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Admin Panel", callback_data="admin_panel")]
    ])
    
    await callback.message.edit_text(text, reply_markup=back_keyboard, parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data == "admin_restart_bot")
async def callback_admin_restart_bot(callback: types.CallbackQuery):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("❌ Owner only!", show_alert=True)
        return
    
    text = """
╔═══════════════════════╗
    <b>RESTART BOT</b>
╚═══════════════════════╝

<b>WARNING:</b>
This will restart the entire bot!

All running scripts will be stopped.
Users may experience brief downtime.

<b>Only use if necessary!</b>

Use <code>/restart</code> to confirm.
"""
    
    back_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Admin Panel", callback_data="admin_panel")]
    ])
    
    await callback.message.edit_text(text, reply_markup=back_keyboard, parse_mode="HTML")
    await callback.answer()

@dp.message(Command("addadmin"))
async def cmd_add_admin(message: types.Message):
    if message.from_user.id not in admin_ids:
        await message.answer("❌ Permission denied!")
        return
    
    try:
        args = message.text.split()
        if len(args) != 2:
            await message.answer("Usage: /addadmin USER_ID")
            return
        
        new_admin_id = int(args[1])
        
        if new_admin_id in admin_ids:
            await message.answer(f"✅ User {new_admin_id} is already an admin!")
            return
        
        admin_ids.add(new_admin_id)
        
        conn = sqlite3.connect(DATABASE_PATH)
        c = conn.cursor()
        c.execute('INSERT OR IGNORE INTO admins (user_id) VALUES (?)', (new_admin_id,))
        conn.commit()
        conn.close()
        
        await message.answer(f"✅ User <code>{new_admin_id}</code> added as admin!", parse_mode="HTML")
        
    except ValueError:
        await message.answer("❌ Invalid USER_ID!")
    except Exception as e:
        logger.error(f"Error adding admin: {e}")
        await message.answer(f"❌ Error: {str(e)}")

@dp.message(Command("removeadmin"))
async def cmd_remove_admin(message: types.Message):
    if message.from_user.id != OWNER_ID:
        await message.answer("❌ Only owner can remove admins!")
        return
    
    try:
        args = message.text.split()
        if len(args) != 2:
            await message.answer("Usage: /removeadmin USER_ID")
            return
        
        remove_admin_id = int(args[1])
        
        if remove_admin_id == OWNER_ID:
            await message.answer("❌ Cannot remove owner!")
            return
        
        if remove_admin_id not in admin_ids:
            await message.answer(f"❌ User {remove_admin_id} is not an admin!")
            return
        
        admin_ids.remove(remove_admin_id)
        
        conn = sqlite3.connect(DATABASE_PATH)
        c = conn.cursor()
        c.execute('DELETE FROM admins WHERE user_id = ?', (remove_admin_id,))
        conn.commit()
        conn.close()
        
        await message.answer(f"✅ User <code>{remove_admin_id}</code> removed from admins!", parse_mode="HTML")
        
    except ValueError:
        await message.answer("❌ Invalid USER_ID!")
    except Exception as e:
        logger.error(f"Error removing admin: {e}")
        await message.answer(f"❌ Error: {str(e)}")


@dp.message(Command("ban"))
async def cmd_ban_user(message: types.Message):
    if message.from_user.id != OWNER_ID:
        await message.answer("❌ Permission denied!")
        return
    
    try:
        args = message.text.split(maxsplit=2)
        if len(args) < 2:
            await message.answer("Usage: /ban USER_ID [REASON]")
            return
        
        ban_user_id = int(args[1])
        reason = args[2] if len(args) > 2 else "No reason provided"
        
        if ban_user_id in admin_ids:
            await message.answer("❌ Cannot ban an admin!")
            return
        
        banned_users.add(ban_user_id)
        # Stop all scripts currently running under this user's account.
        for script_key, script_info in list(bot_scripts.items()):
            if script_info.get('script_owner_id') == ban_user_id:
                _stop_script(script_key)
        
        conn = sqlite3.connect(DATABASE_PATH)
        c = conn.cursor()
        c.execute('INSERT OR REPLACE INTO banned_users (user_id, banned_date, reason) VALUES (?, ?, ?)',
                  (ban_user_id, datetime.now().isoformat(), reason))
        conn.commit()
        conn.close()
        
        await message.answer(f"🚫 User <code>{ban_user_id}</code> has been banned!\n\nReason: {reason}", parse_mode="HTML")
        
    except ValueError:
        await message.answer("❌ Invalid USER_ID!")
    except Exception as e:
        logger.error(f"Error banning user: {e}")
        await message.answer(f"❌ Error: {str(e)}")

@dp.message(Command("unban"))
async def cmd_unban_user(message: types.Message):
    if message.from_user.id != OWNER_ID:
        await message.answer("❌ Permission denied!")
        return
    
    try:
        args = message.text.split()
        if len(args) != 2:
            await message.answer("Usage: /unban USER_ID")
            return
        
        unban_user_id = int(args[1])
        
        if unban_user_id not in banned_users:
            await message.answer(f"❌ User {unban_user_id} is not banned!")
            return
        
        banned_users.remove(unban_user_id)
        
        conn = sqlite3.connect(DATABASE_PATH)
        c = conn.cursor()
        c.execute('DELETE FROM banned_users WHERE user_id = ?', (unban_user_id,))
        conn.commit()
        conn.close()
        
        await message.answer(f"✅ User <code>{unban_user_id}</code> has been unbanned!", parse_mode="HTML")
        
    except ValueError:
        await message.answer("❌ Invalid USER_ID!")
    except Exception as e:
        logger.error(f"Error unbanning user: {e}")
        await message.answer(f"❌ Error: {str(e)}")

@dp.message(Command("broadcast"))
async def cmd_broadcast(message: types.Message):
    if message.from_user.id not in admin_ids:
        await message.answer("❌ Permission denied!")
        return
    
    try:
        broadcast_text = message.text.replace("/broadcast", "", 1).strip()
        
        if not broadcast_text:
            await message.answer("Usage: /broadcast Your message here")
            return
        
        sent_count = 0
        failed_count = 0
        
        status_msg = await message.answer(f"📢 Broadcasting to {len(active_users)} users...")
        
        for user_id in active_users:
            if user_id in banned_users:
                continue
            
            try:
                await bot.send_message(user_id, f"📢 <b>Announcement:</b>\n\n{broadcast_text}", parse_mode="HTML")
                sent_count += 1
                await asyncio.sleep(0.05)
            except Exception as e:
                logger.error(f"Failed to send to {user_id}: {e}")
                failed_count += 1
        
        await status_msg.edit_text(
            f"✅ <b>Broadcast Complete!</b>\n\n"
            f"✅ Sent: {sent_count}\n"
            f"❌ Failed: {failed_count}",
            parse_mode="HTML"
        )
        
    except Exception as e:
        logger.error(f"Error broadcasting: {e}")
        await message.answer(f"❌ Error: {str(e)}")

@dp.message(Command("search"))
async def cmd_search_files(message: types.Message):
    user_id = message.from_user.id
    
    try:
        args = message.text.split(maxsplit=1)
        if len(args) < 2:
            await message.answer("Usage: /search filename")
            return
        
        search_term = args[1].lower()
        user_file_list = user_files.get(user_id, [])
        
        matches = [f for f in user_file_list if search_term in f[0].lower()]
        
        if not matches:
            await message.answer(f"🔍 No files found matching '<code>{search_term}</code>'", parse_mode="HTML")
            return
        
        text = f"🔍 <b>Search Results ({len(matches)}):</b>\n\n"
        
        for file_name, file_type in matches:
            icon = _icon(file_type)
            text += f"{icon} <code>{file_name}</code>\n"
        
        await message.answer(text, parse_mode="HTML")
        
    except Exception as e:
        logger.error(f"Search error: {e}")
        await message.answer(f"❌ Error: {str(e)}")


@dp.message(Command("stats"))
async def cmd_stats(message: types.Message):
    user_id = message.from_user.id
    user_file_count = len(user_files.get(user_id, []))
    user_fav_count = len(user_favorites.get(user_id, []))
    is_premium = user_id in user_subscriptions and user_subscriptions[user_id]['expiry'] > datetime.now()
    
    text = f"""
╔═══════════════════════╗
    <b>YOUR STATISTICS</b>
╚═══════════════════════╝

<b>👤 USER INFO:</b>

🆔 User ID: <code>{user_id}</code>
👤 Name: {message.from_user.full_name}
📦 Files Uploaded: {user_file_count} (Unlimited ♾️)
⭐ Favorites: {user_fav_count}
💎 Account: Free 🆓 (Unlimited ♾️)
🚀 Running: {sum(1 for k in bot_scripts if k.startswith(f"{user_id}_"))}

━━━━━━━━━━━━━━━━━━━━
<b>USAGE:</b>

📤 Uploads: {bot_stats.get('total_uploads', 0)}
📥 Downloads: {bot_stats.get('total_downloads', 0)}
▶️ Script Runs: {bot_stats.get('total_runs', 0)}

{'✅ Bot Status: Active' if not bot_locked else '🔒 Bot: Encrypted by admin'}
"""
    
    if user_id in admin_ids:
        text += f"\n━━━━━━━━━━━━━━━━━━━━\n👑 <b>ADMIN STATS:</b>\n"
        text += f"👥 Total Users: {len(active_users)}\n"
        text += f"📁 Total Files: {sum(len(files) for files in user_files.values())}\n"
    
    back_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔵 🏠 Main Menu", callback_data="back_to_main")]
    ])
    
    await message.answer(text, reply_markup=back_keyboard, parse_mode="HTML")


# ── WEBSITE: LANDING PAGE + ADMIN PANEL ──────────────
import base64
import hmac
import html as html_lib

ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD') or 'KaleabAb39'
_BOT_USERNAME = None

def _auth_ok(request):
    if not ADMIN_PASSWORD:
        return True  # password set nahi hai to panel open rahega
    h = request.headers.get('Authorization', '')
    if not h.startswith('Basic '):
        return False
    try:
        pwd = base64.b64decode(h[6:]).decode('utf-8').partition(':')[2]
    except Exception:
        return False
    return hmac.compare_digest(pwd.encode(), ADMIN_PASSWORD.encode())

def _deny():
    if not ADMIN_PASSWORD:
        return web.Response(text="Panel off hai. Render Environment mein ADMIN_PASSWORD set karo.", status=503)
    return web.Response(status=401, headers={'WWW-Authenticate': 'Basic realm="Admin"'}, text="Login required")

def _running_list():
    out = []
    for key, info in list(bot_scripts.items()):
        try:
            if info['process'].poll() is None:
                out.append({'key': key, 'owner': info['script_owner_id'], 'file': info['file_name'],
                            'type': info['type'], 'since': info['start_time'].strftime('%d %b %H:%M')})
        except Exception:
            pass
    return out

def _collect():
    now = datetime.now()
    running = _running_list()
    users = []
    try:
        conn = sqlite3.connect(DATABASE_PATH)
        c = conn.cursor()
        c.execute('SELECT user_id, join_date, last_active FROM active_users ORDER BY COALESCE(last_active, join_date) DESC LIMIT 300')
        for uid, jd, la in c.fetchall():
            users.append({'id': uid, 'joined': (jd or '-')[:16].replace('T', ' '), 'last': (la or '-')[:16].replace('T', ' '),
                          'files': len(user_files.get(uid, [])), 'banned': uid in banned_users, 'admin': uid in admin_ids,
                          'sub': uid in user_subscriptions and user_subscriptions[uid]['expiry'] > now,
                          'running': sum(1 for r in running if r['owner'] == uid)})
        conn.close()
    except Exception as e:
        logger.error(f"Panel user fetch error: {e}")
    extra = {'joins7': [], 'new_today': 0, 'active24': 0}
    try:
        conn = sqlite3.connect(DATABASE_PATH)
        c = conn.cursor()
        d6 = (now - timedelta(days=6)).strftime('%Y-%m-%d')
        c.execute("SELECT substr(join_date,1,10), COUNT(*) FROM active_users WHERE join_date >= ? GROUP BY 1", (d6,))
        cnt = dict(c.fetchall())
        days = [now - timedelta(days=i) for i in range(6, -1, -1)]
        extra['joins7'] = [[x.strftime('%d %b'), cnt.get(x.strftime('%Y-%m-%d'), 0)] for x in days]
        extra['new_today'] = cnt.get(now.strftime('%Y-%m-%d'), 0)
        c.execute("SELECT COUNT(*) FROM active_users WHERE last_active >= ?", ((now - timedelta(hours=24)).isoformat(),))
        extra['active24'] = c.fetchone()[0]
        conn.close()
    except Exception as e:
        logger.error(f"Panel stats error: {e}")
    types = {}
    for fl in user_files.values():
        for f in fl:
            types[f[1]] = types.get(f[1], 0) + 1
    top = sorted(((u, len(fl)) for u, fl in user_files.items()), key=lambda x: -x[1])[:5]
    try:
        ram = round(psutil.Process().memory_info().rss / 1024 / 1024, 1)
    except Exception:
        ram = 0
    up = now - BOT_START_TIME
    return {'total_users': len(active_users), 'banned': len(banned_users), 'admins': len(admin_ids),
            'total_files': sum(len(f) for f in user_files.values()), 'running_now': len(running),
            'uploads': bot_stats.get('total_uploads', 0), 'downloads': bot_stats.get('total_downloads', 0),
            'runs': bot_stats.get('total_runs', 0), 'locked': bot_locked,
            'uptime': f"{up.days}d {up.seconds // 3600}h {(up.seconds % 3600) // 60}m",
            'files_list': [{'owner': u, 'name': f[0], 'type': f[1], 'on': f'{u}_{f[0]}' in bot_scripts and bot_scripts[f'{u}_{f[0]}']['process'].poll() is None} for u, fl in user_files.items() for f in fl][:500], 'ram_mb': ram, 'cpu': psutil.cpu_percent(interval=None), 'running': running, 'users': users, 'types': types, 'top': top, **extra}

def _stop_script(key, mark='stopped'):
    """Stop a hosted process (sync). mark=None keeps the saved state (used for host restarts)."""
    info = bot_scripts.pop(key, None)
    if not info:
        return False
    _close_log(info)
    _kill_tree(info['process'])
    if mark:
        set_bot_status(info['script_owner_id'], info['file_name'], mark, pid=None, resume_on_renew=0)
    return True

def _db(sql, params=()):
    conn = sqlite3.connect(DATABASE_PATH)
    conn.execute(sql, params)
    conn.commit()
    conn.close()

async def _broadcast(text):
    sent = 0
    for uid in list(active_users):
        if uid in banned_users:
            continue
        try:
            await bot.send_message(uid, text)
            sent += 1
        except Exception:
            pass
        await asyncio.sleep(0.06)
    logger.info(f"Broadcast finished: {sent} users")

async def panel_action(request):
    global bot_locked
    if not _auth_ok(request):
        return _deny()
    if request.headers.get('X-Panel') != '1':
        return web.json_response({'ok': False, 'msg': 'Bad request'}, status=400)
    try:
        d = await request.json()
        act = d.get('action')
        uid = int(d['user_id']) if d.get('user_id') not in (None, '') else None
    except Exception:
        return web.json_response({'ok': False, 'msg': 'Invalid data'}, status=400)

    if act in ('ban', 'unban', 'stop_user') and uid is None:
        return web.json_response({'ok': False, 'msg': 'User ID chahiye'})
    if act == 'ban':
        if uid in admin_ids:
            return web.json_response({'ok': False, 'msg': 'Admin ko ban nahi kar sakte'})
        banned_users.add(uid)
        _db('INSERT OR REPLACE INTO banned_users (user_id, banned_date, reason) VALUES (?, ?, ?)',
            (uid, datetime.now().isoformat(), 'Banned from web panel'))
        for r in _running_list():
            if r['owner'] == uid:
                _stop_script(r['key'])
        return web.json_response({'ok': True, 'msg': f'{uid} banned'})
    if act == 'unban':
        banned_users.discard(uid)
        _db('DELETE FROM banned_users WHERE user_id = ?', (uid,))
        return web.json_response({'ok': True, 'msg': f'{uid} unbanned'})
    if act == 'stop_user':
        n = sum(1 for r in _running_list() if r['owner'] == uid and _stop_script(r['key']))
        return web.json_response({'ok': True, 'msg': f'{n} bot(s) stop kiye'})
    if act == 'stop_script':
        return web.json_response({'ok': _stop_script(d.get('key')), 'msg': 'Script stop kiya'})
    if act == 'stop_all':
        n = sum(1 for r in _running_list() if _stop_script(r['key']))
        return web.json_response({'ok': True, 'msg': f'{n} bot(s) stop kiye'})
    if act == 'lock':
        set_bot_locked(not bot_locked)
        return web.json_response({'ok': True, 'msg': 'Bot LOCKED' if bot_locked else 'Bot UNLOCKED'})
    if act == 'broadcast':
        text = (d.get('text') or '').strip()
        if not text or len(text) > 3500:
            return web.json_response({'ok': False, 'msg': 'Message khali ya bahut lamba hai'})
        asyncio.create_task(_broadcast(text))
        return web.json_response({'ok': True, 'msg': f'Broadcast shuru: {len(active_users)} users ko'})
    return web.json_response({'ok': False, 'msg': 'Unknown action'})

async def panel_api(request):
    if not _auth_ok(request):
        return _deny()
    return web.json_response(_collect())

ADMIN_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>KALPRO HOSTING BOT - Dashboard</title><style>
*{box-sizing:border-box}body{margin:0;font-family:system-ui,sans-serif;background:#0b0f17;color:#e6edf3;display:flex;min-height:100vh}
#side{width:220px;background:#111826;border-right:1px solid #1f2a3d;padding:16px 10px;position:fixed;top:0;bottom:0;left:0;z-index:20;transition:.2s}
.logo{font-weight:800;font-size:17px;padding:4px 10px 16px}.logo span{color:#58a6ff}
.nav{display:block;width:100%;text-align:left;background:none;border:0;color:#9aa7b5;padding:11px 12px;border-radius:8px;font-size:14px;cursor:pointer;margin-bottom:3px}
.nav:hover{background:#1a2438}.nav.on{background:#1f6feb;color:#fff}
.side-foot{position:absolute;bottom:14px;left:14px;font-size:11px;color:#6b7785}
#main{margin-left:220px;flex:1;padding:18px;min-width:0}
#top{display:none;align-items:center;gap:10px;margin-bottom:12px}
h2{margin:0 0 4px;font-size:20px}.sub{color:#8b949e;font-size:12px;margin-bottom:16px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(135px,1fr));gap:10px;margin-bottom:18px}
.card{background:#111826;border:1px solid #1f2a3d;border-radius:12px;padding:14px}.n{font-size:22px;font-weight:700}.l{font-size:12px;color:#8b949e;margin-top:3px}
.box{background:#111826;border:1px solid #1f2a3d;border-radius:12px;padding:14px;margin-bottom:14px}
.wrap{overflow-x:auto}table{width:100%;border-collapse:collapse;font-size:13px}th,td{text-align:left;padding:8px;border-bottom:1px solid #1f2a3d;white-space:nowrap}th{color:#8b949e;font-weight:600}
button.a{background:#21262d;color:#e6edf3;border:1px solid #30363d;border-radius:6px;padding:6px 10px;font-size:12px;cursor:pointer}
.r{background:#da3633!important;border-color:#da3633!important}.g{background:#238636!important;border-color:#238636!important}.b{background:#1f6feb!important;border-color:#1f6feb!important}
input,textarea{background:#0b0f17;color:#e6edf3;border:1px solid #30363d;border-radius:8px;padding:9px;font-size:13px;width:100%}
.row{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px}.row input{flex:1;min-width:120px}
.tag{padding:2px 8px;border-radius:20px;font-size:11px}.on{}.t-on{background:#12361f;color:#3fb950}.t-off{background:#2a2f38;color:#8b949e}
.two{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:14px}.bars{display:flex;align-items:flex-end;gap:8px;height:130px;margin-top:12px}
.bc{flex:1;text-align:center;display:flex;flex-direction:column;justify-content:flex-end;align-items:center}.bc i{font-style:normal;font-size:11px;color:#9aa7b5}.bc small{font-size:10px;color:#6b7785}
.bar{width:100%;background:linear-gradient(#58a6ff,#1f6feb);border-radius:5px 5px 0 0}.hb{display:flex;align-items:center;gap:8px;margin-top:8px;font-size:12px}.hb span{width:56px;overflow:hidden;text-overflow:ellipsis}
.hb div{flex:1;background:#1a2438;border-radius:4px;height:8px}.hb div div{background:#1f6feb;height:8px}.hb em{font-style:normal;color:#9aa7b5;width:28px;text-align:right}
#toast{position:fixed;bottom:16px;left:50%;transform:translateX(-50%);padding:10px 18px;border-radius:8px;display:none;font-size:13px;z-index:99}
@media(max-width:760px){#side{left:-230px}#side.open{left:0}#main{margin-left:0}#top{display:flex}}
</style></head><body>
<div id="side"><div class="logo">🚀 KALPRO <span>HOSTING BOT</span></div>
<button class="nav on" data-p="over">📊 Overview</button><button class="nav" data-p="bots">🤖 Running Bots</button>
<button class="nav" data-p="files">📁 All Files</button><button class="nav" data-p="users">👥 Users</button>
<button class="nav" data-p="cast">📢 Broadcast</button><button class="nav" data-p="set">⚙️ Settings</button>
<a class="nav" href="/" style="text-decoration:none">🌐 Website</a><div class="side-foot">Developed by KALPRO</div></div>
<div id="main"><div id="top"><button class="a b" onclick="$('side').classList.toggle('open')">☰ Menu</button><b>KALPRO HOSTING BOT</b></div>
<h2 id="ttl"></h2><div class="sub" id="st"></div><div id="view"></div></div><div id="toast"></div>
<script>
const $=id=>document.getElementById(id);let D=null,P='over',Q='';
const esc=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function toast(m,ok=true){const t=$('toast');t.textContent=m;t.style.background=ok?'#238636':'#da3633';t.style.display='block';setTimeout(()=>t.style.display='none',2600)}
async function act(b){try{const r=await fetch('/admin/action',{method:'POST',headers:{'Content-Type':'application/json','X-Panel':'1'},body:JSON.stringify(b)});const j=await r.json();toast(j.msg||'Done',j.ok);load()}catch(e){toast('Error',false)}}
document.querySelectorAll('button.nav').forEach(b=>b.onclick=()=>{P=b.dataset.p;document.querySelectorAll('button.nav').forEach(x=>x.classList.toggle('on',x==b));$('side').classList.remove('open');render()});
const card=(l,n)=>`<div class="card"><div class="n">${esc(n)}</div><div class="l">${l}</div></div>`;
function bots(){return `<div class="box wrap"><table><tr><th>User</th><th>File</th><th>Type</th><th>Start</th><th></th></tr>`+(D.running.map(r=>`<tr><td>${r.owner}</td><td>${esc(r.file)}</td><td>${esc(r.type)}</td><td>${esc(r.since)}</td><td><button class="a r" onclick='act({action:"stop_script",key:${JSON.stringify(r.key)}})'>Stop</button></td></tr>`).join('')||'<tr><td colspan=5>Koi bot nahi chal raha</td></tr>')+`</table></div>`}
function render(){if(!D)return;const T={over:'Overview',bots:'Running Bots',files:'All Files',users:'Users',cast:'Broadcast',set:'Settings'};$('ttl').textContent=T[P];
$('st').textContent=(D.locked?'🔒 Locked (delete blocked)':'✅ Bot active')+' • uptime '+D.uptime+' • auto-refresh 15s';let h='';
if(P=='over'){const mx=Math.max(1,...D.joins7.map(x=>x[1])),tt=Math.max(1,...Object.values(D.types));
h=`<div class="grid">${card('👥 Total Members',D.total_users)}${card('🆕 New today',D.new_today)}${card('🔥 Active 24h',D.active24)}${card('🟢 Bots running',D.running_now)}${card('📁 Files',D.total_files)}${card('🚫 Banned',D.banned)}${card('👑 Admins',D.admins)}${card('📤 Uploads',D.uploads)}${card('📥 Downloads',D.downloads)}${card('▶️ Script runs',D.runs)}${card('🧠 RAM MB',D.ram_mb)}${card('⚙️ CPU %',D.cpu)}</div>
<div class="two"><div class="box"><b>📈 Naye members (7 din)</b><div class="bars">${D.joins7.map(x=>`<div class="bc"><i>${x[1]}</i><div class="bar" style="height:${8+x[1]/mx*80}px"></div><small>${x[0]}</small></div>`).join('')}</div></div>
<div class="box"><b>🗂 File types</b>${Object.entries(D.types).map(([k,v])=>`<div class="hb"><span>${esc(k)}</span><div><div style="width:${v/tt*100}%"></div></div><em>${v}</em></div>`).join('')||'<p class="sub">Abhi koi file nahi</p>'}
<b style="display:block;margin-top:14px">🏆 Top users (files)</b>${D.top.map(t=>`<div class="hb"><span>${t[0]}</span><div><div style="width:${t[1]/Math.max(1,D.top[0][1])*100}%"></div></div><em>${t[1]}</em></div>`).join('')||'<p class="sub">-</p>'}</div></div>
<h3>🤖 Abhi chal rahe bots</h3>`+bots()}
if(P=='bots')h=bots();
if(P=='files')h=`<input placeholder="🔍 File ya user ID search..." value="${esc(Q)}" oninput="Q=this.value;render()" style="margin-bottom:10px"><div class="box wrap"><table><tr><th>User</th><th>File</th><th>Type</th><th>Status</th></tr>`+(D.files_list.filter(f=>(f.name+f.owner).toLowerCase().includes(Q.toLowerCase())).map(f=>`<tr><td>${f.owner}</td><td>${esc(f.name)}</td><td>${esc(f.type)}</td><td><span class="tag ${f.on?'t-on':'t-off'}">${f.on?'Running':'Stopped'}</span></td></tr>`).join('')||'<tr><td colspan=4>Koi file nahi</td></tr>')+`</table></div>`;
if(P=='users')h=`<input placeholder="🔍 User ID search..." value="${esc(Q)}" oninput="Q=this.value;render()" style="margin-bottom:10px"><div class="box wrap"><table><tr><th>User ID</th><th>Joined</th><th>Last active</th><th>Files</th><th>Run</th><th>Tags</th><th></th></tr>`+(D.users.filter(u=>String(u.id).includes(Q)).map(u=>`<tr><td>${u.id}</td><td>${esc(u.joined)}</td><td>${esc(u.last)}</td><td>${u.files}</td><td>${u.running}</td><td>${u.banned?'🚫':''}${u.admin?'👑':''}</td><td>${u.banned?`<button class="a g" onclick="act({action:'unban',user_id:${u.id}})">Unban</button>`:`<button class="a r" onclick="act({action:'ban',user_id:${u.id}})">Ban</button>`} <button class="a" onclick="act({action:'stop_user',user_id:${u.id}})">Stop bots</button></td></tr>`).join('')||'<tr><td colspan=7>Koi user nahi</td></tr>')+`</table></div>`;
if(P=='cast')h=`<div class="box"><b>📢 Saare users ko message</b><textarea id="bc" rows="5" placeholder="Message likho..." style="margin:10px 0"></textarea><button class="a b" onclick="cast()">Send to all users</button></div>`;
if(P=='set')h=`<div class="box"><b>Bot control</b><div class="row" style="margin-top:10px"><button class="a b" onclick="act({action:'lock'})">🔒 Lock / Unlock bot</button><button class="a r" onclick="if(confirm('Saare bots stop karein?'))act({action:'stop_all'})">🛑 Stop ALL bots</button></div></div><div class="box"><b>Manual ban / unban</b><div class="row" style="margin-top:10px"><input id="uid" placeholder="User ID" inputmode="numeric"><button class="a r" onclick="m('ban')">Ban</button><button class="a g" onclick="m('unban')">Unban</button></div></div>`;
$('view').innerHTML=h;const i=document.querySelector('#view input[oninput]');if(i&&Q){i.focus();i.setSelectionRange(Q.length,Q.length)}}
function m(a){const u=$('uid').value.trim();if(!u)return toast('User ID daalo',false);act({action:a,user_id:u})}
function cast(){const t=$('bc').value.trim();if(!t)return toast('Message likho',false);if(confirm('Sabko bhejein?')){act({action:'broadcast',text:t});$('bc').value=''}}
async function load(){try{D=await (await fetch('/admin/api')).json();const a=document.activeElement;if(a&&(a.tagName=='TEXTAREA'||a.id=='uid'))return;render()}catch(e){$('st').textContent='Load error'}}
load();setInterval(load,15000);
</script></body></html>"""

async def admin_page(request):
    if not _auth_ok(request):
        return _deny()
    return web.Response(text=ADMIN_HTML, content_type='text/html')

LANDING_HTML = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>KALPRO HOSTING BOT - Free Telegram Bot Hosting</title><meta name="description" content="Python aur Node.js bots ko Telegram se free mein host karo. Unlimited uploads, koi premium nahi.">
<style>
*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;font-family:system-ui,sans-serif;background:#070b12;color:#e6edf3;line-height:1.55}
nav{display:flex;justify-content:space-between;align-items:center;padding:14px 22px;position:sticky;top:0;background:#070b12ee;backdrop-filter:blur(8px);border-bottom:1px solid #151d2c;z-index:5}
nav b{font-size:18px}nav b span,h1 span,h2 span{color:#58a6ff}nav a{color:#9aa7b5;text-decoration:none;margin-left:16px;font-size:14px}
.hero{text-align:center;padding:70px 20px 40px;background:radial-gradient(circle at 50% 0,#14366a 0,#070b12 62%)}
.badge{display:inline-block;background:#12361f;color:#3fb950;border-radius:20px;padding:5px 14px;font-size:13px;margin-bottom:16px}
h1{font-size:40px;margin:0 0 12px;line-height:1.15}.hero p{color:#9aa7b5;max-width:560px;margin:0 auto 26px;font-size:17px}
.btn{display:inline-block;padding:13px 24px;border-radius:10px;text-decoration:none;font-weight:600;margin:5px;font-size:15px}.p{background:#1f6feb;color:#fff}.s{border:1px solid #2b374d;color:#e6edf3}
.stats{display:flex;justify-content:center;gap:12px;flex-wrap:wrap;padding:6px 20px 30px}.st{background:#0f1624;border:1px solid #1f2a3d;border-radius:14px;padding:16px 30px;text-align:center}.st b{font-size:26px;display:block}.st small{color:#8b949e}
section{max-width:900px;margin:0 auto;padding:36px 20px}h2{text-align:center;font-size:26px;margin:0 0 22px}
.g{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:14px}.f{background:#0f1624;border:1px solid #1f2a3d;border-radius:14px;padding:18px}.f h3{margin:0 0 6px;font-size:16px}.f p{margin:0;color:#9aa7b5;font-size:14px}
.step{display:flex;gap:14px;align-items:flex-start;background:#0f1624;border:1px solid #1f2a3d;border-radius:14px;padding:16px;margin-bottom:10px}.num{background:#1f6feb;border-radius:50%;min-width:34px;height:34px;display:flex;align-items:center;justify-content:center;font-weight:700}
details{background:#0f1624;border:1px solid #1f2a3d;border-radius:12px;padding:14px;margin-bottom:10px}summary{cursor:pointer;font-weight:600}details p{color:#9aa7b5;margin:10px 0 0}
.cta{text-align:center;background:linear-gradient(135deg,#12315f,#0f1624);border-radius:18px;padding:34px 20px}
footer{text-align:center;color:#6b7785;padding:28px 10px;font-size:13px;border-top:1px solid #151d2c}footer a{color:#58a6ff;text-decoration:none;margin:0 6px}
</style></head><body>
<nav><b>🚀 KALPRO <span>HOSTING BOT</span></b><div><a href="#features">Features</a><a href="#how">How it works</a><a href="#faq">FAQ</a></div></nav>
<div class="hero"><div class="badge">🆓 100% Free &bull; Unlimited Uploads</div>
<h1>Apne Bots ko <span>24/7</span> Host Karo<br>Seedha Telegram Se</h1>
<p>Python (.py), Node.js (.js) ya ZIP file bhejo, dependencies khud install hongi aur bot chalu. Koi premium plan nahi, koi limit nahi.</p>
<a class="btn p" href="__BOTLINK__">🚀 Start on Telegram</a><a class="btn s" href="__CHANNEL__">📢 Updates Channel</a></div>
<div class="stats"><div class="st"><b>__USERS__</b><small>Users</small></div><div class="st"><b>__RUNNING__</b><small>Bots running now</small></div><div class="st"><b>__FILES__</b><small>Files hosted</small></div></div>
<section id="features"><h2>Features</h2><div class="g">
<div class="f"><h3>📤 Easy Upload</h3><p>.py, .js aur .zip files seedha Telegram chat se upload karo.</p></div>
<div class="f"><h3>⚙️ Auto Install</h3><p>Zaroori Python packages apne aap install ho jate hain.</p></div>
<div class="f"><h3>▶️ Run &amp; Stop</h3><p>Bot ke buttons se script start, stop aur manage karo.</p></div>
<div class="f"><h3>📁 File Manager</h3><p>Files dekho, search karo, download aur delete karo.</p></div>
<div class="f"><h3>⭐ Favorites</h3><p>Apni important scripts ko favorite mein rakho.</p></div>
<div class="f"><h3>🆓 Totally Free</h3><p>Unlimited uploads, sabke liye same features.</p></div></div></section>
<section id="how"><h2>Kaise use karein</h2>
<div class="step"><div class="num">1</div><div><b>Bot kholo</b><br><span style="color:#9aa7b5">Telegram pe bot open karke /start dabao.</span></div></div>
<div class="step"><div class="num">2</div><div><b>File upload karo</b><br><span style="color:#9aa7b5">Apni .py, .js ya .zip file bhejo.</span></div></div>
<div class="step"><div class="num">3</div><div><b>Run dabao</b><br><span style="color:#9aa7b5">Bot chalu, aur stop bhi kabhi bhi kar sakte ho.</span></div></div></section>
<section id="faq"><h2>FAQ</h2>
<details><summary>Kya ye sach mein free hai?</summary><p>Haan, koi premium plan ya upload limit nahi hai.</p></details>
<details><summary>Kaunsi files chalti hain?</summary><p>Python (.py), Node.js (.js) aur ZIP projects.</p></details>
<details><summary>Problem aaye to?</summary><p>Telegram pe owner @Kaleabdot se contact karo.</p></details></section>
<section><div class="cta"><h2 style="margin-bottom:8px">Ready to host? 🚀</h2><p style="color:#9aa7b5;margin:0 0 16px">Abhi Telegram pe bot kholo.</p><a class="btn p" href="__BOTLINK__">Open Bot</a></div></section>
<footer>Developed by <b>KALPRO</b><br><a href="__CHANNEL__">Telegram Channel</a>&bull;<a href="https://t.me/__OWNER__">Contact</a></footer></body></html>"""

async def landing_page(request):
    global _BOT_USERNAME
    if not _BOT_USERNAME:
        try:
            _BOT_USERNAME = (await bot.get_me()).username
        except Exception:
            _BOT_USERNAME = ''
    ch = UPDATE_CHANNEL if UPDATE_CHANNEL.startswith('http') else 'https://t.me/' + UPDATE_CHANNEL.lstrip('@')
    page = (LANDING_HTML.replace('__BOTLINK__', f'https://t.me/{_BOT_USERNAME}' if _BOT_USERNAME else ch)
            .replace('__CHANNEL__', html_lib.escape(ch))
            .replace('__OWNER__', html_lib.escape(YOUR_USERNAME.lstrip('@')))
            .replace('__USERS__', str(len(active_users))).replace('__RUNNING__', str(len(_running_list())))
            .replace('__FILES__', str(sum(len(f) for f in user_files.values()))))
    return web.Response(text=page, content_type='text/html')

async def keep_alive():
    """Render free service ko sone se bachane ke liye khud ko ping karta hai"""
    url = (os.getenv('RENDER_EXTERNAL_URL') or '').rstrip('/')
    if not url:
        return
    await asyncio.sleep(30)
    while True:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url + '/health', timeout=aiohttp.ClientTimeout(total=20)) as r:
                    logger.info(f"Keep-alive ping: {r.status}")
        except Exception as e:
            logger.warning(f"Keep-alive failed: {e}")
        await asyncio.sleep(240)  # every 4 min (Render free sleeps after 15 min without traffic)

async def web_server():
    app = web.Application()
    
    async def health(request):
        return web.Response(text="ok")
    
    app.router.add_get('/', landing_page)
    app.router.add_get('/health', health)
    app.router.add_get('/admin', admin_page)
    app.router.add_get('/admin/api', panel_api)
    app.router.add_post('/admin/action', panel_action)
    
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv('PORT', 5000))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    logger.info(f"🌐 Web server started on port {port}")

# ═══════════════════════════════════════════════════════════════════
#  COMMANDS · ADMIN INPUT FLOWS · BACKGROUND SERVICES  (KALPRO Hosting Bot 3.0)
# ═══════════════════════════════════════════════════════════════════
def _admin_only(message):
    return message.from_user and message.from_user.id in admin_ids


@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    uid = message.from_user.id
    text = ("<b>KALPRO HOSTING BOT · HELP</b>\n\n"
            "<b>How it works</b>\n"
            "1. Upload your code file or a .zip project (any language)\n"
            "2. Open <b>My Bots</b> and press <b>Run</b>\n"
            "3. <b>Pause</b> any bot at any time. Your files and data are never lost\n"
            "4. Use <b>Logs</b> to see what a bot prints\n"
            "5. Tap <b>Update</b> on a bot, send the new file, then tap <b>Restart</b>. Your data is kept\n\n"
            "<b>Commands</b>\n"
            "/start - open the dashboard\n"
            "/mybots - list and control your bots\n"
            "/plans - plans and prices\n"
            "/search name - find a file\n"
            "/stats - your usage\n")
    if uid in admin_ids:
        text += ("\n<b>Admin commands</b>\n"
                 "/trial - free 7-day trial (asks for the User ID)\n"
                 "/grant ID PLAN DAYS [BOTS] - grant or renew access\n"
                 "/revoke ID - remove access (data is kept)\n"
                 "/kill ID - force-stop all running bots of a user\n"
                 "/users - users, remaining time and bots\n"
                 "/ban ID, /unban ID, /broadcast text\n"
                 "/cancel - cancel a pending question\n")
    text += f"\nPayment and support: {CONTACT_USERNAME}"
    await message.answer(text, parse_mode="HTML")


@dp.message(Command("mybots"))
async def cmd_mybots(message: types.Message):
    text, kb = my_bots_view(message.from_user.id)
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


@dp.message(Command("plans"))
async def cmd_plans(message: types.Message):
    uid = message.from_user.id
    back = 'back_to_main' if get_access(uid)['ok'] else 'back_denied'
    await message.answer(plans_text(uid), reply_markup=plans_keyboard(back), parse_mode="HTML")


@dp.message(Command("cancel"))
async def cmd_cancel(message: types.Message):
    had_update = pending_update.pop(message.from_user.id, None)
    if pending_input.pop(message.from_user.id, None) or had_update:
        await message.answer("Cancelled.")
    else:
        await message.answer("Nothing to cancel.")


@dp.message(Command("trial", ignore_case=True))
async def cmd_trial(message: types.Message):
    uid = message.from_user.id
    if not _admin_only(message):
        await message.answer(f"Free trials are given by the owner. Contact {CONTACT_USERNAME} to request one.")
        return
    parts = (message.text or '').split()
    if len(parts) > 1 and parts[1].isdigit():
        await message.answer(await do_trial(int(parts[1]), uid), parse_mode="HTML")
        return
    pending_input[uid] = {'action': 'trial', 'ts': time.time()}
    await message.answer(f"Send the <b>User ID</b> to give a {TRIAL_DAYS}-day free trial.\n\nSend /cancel to abort.",
                         parse_mode="HTML")


@dp.message(Command("grant", ignore_case=True))
async def cmd_grant(message: types.Message):
    if not _admin_only(message):
        return
    uid = message.from_user.id
    parts = (message.text or '').split()
    if len(parts) == 1:
        pending_input[uid] = {'action': 'grant', 'ts': time.time()}
        await message.answer("Send the <b>User ID</b> to grant access to.\n\nSend /cancel to abort.", parse_mode="HTML")
        return
    if not parts[1].isdigit():
        await message.answer("Usage: /grant USER_ID PLAN DAYS [BOTS]\nExample: /grant 123456789 pro 30 10")
        return
    target = int(parts[1])
    if len(parts) == 2:
        await message.answer(f"Choose a plan for <code>{target}</code>:", reply_markup=plan_pick_keyboard(target),
                             parse_mode="HTML")
        return
    plan_key = parts[2].lower()
    if plan_key not in PLANS:
        await message.answer("Unknown plan. Use: " + ", ".join(PLAN_ORDER))
        return
    try:
        days = int(parts[3]) if len(parts) > 3 else PLAN_DAYS
    except ValueError:
        days = 0
    if not 1 <= days <= 3650:
        await message.answer("Days must be between 1 and 3650.")
        return
    bot_limit = None
    if len(parts) > 4:
        if not parts[4].isdigit() or not 1 <= int(parts[4]) <= 999:
            await message.answer("Bot limit must be a number between 1 and 999.\nExample: /grant 123456789 pro 30 10")
            return
        bot_limit = int(parts[4])
    sub, resumed = await grant_access(target, plan_key, days, uid, bot_limit=bot_limit)
    told = await announce_grant(target, sub, resumed)
    p = PLANS[plan_key]
    await message.answer(f"✅ <b>Access granted</b>\n\nUser: <code>{target}</code>\nPlan: {p['emoji']} {p['name']}\n"
                         f"Added: {days} days\nActive bots allowed: {plan_limits_for_sub(sub)}"
                         + (" (custom)" if bot_limit else "") + f"\nExpires: {fmt_dt(sub['expiry'])}"
                         + ("" if told else "\n\nThe user could not be notified (they must press /start first)."),
                         parse_mode="HTML")


@dp.message(Command("addpremium", ignore_case=True))
async def cmd_addpremium(message: types.Message):
    """Legacy alias: /addpremium USER_ID DAYS [PLAN]"""
    if not _admin_only(message):
        return
    parts = (message.text or '').split()
    if len(parts) < 3 or not parts[1].isdigit() or not parts[2].isdigit():
        await message.answer("Usage: /addpremium USER_ID DAYS [PLAN]\nBetter: /grant USER_ID PLAN DAYS")
        return
    plan_key = parts[3].lower() if len(parts) > 3 and parts[3].lower() in PLANS else 'lite'
    target, days = int(parts[1]), int(parts[2])
    if not 1 <= days <= 3650:
        await message.answer("Days must be between 1 and 3650.")
        return
    sub, resumed = await grant_access(target, plan_key, days, message.from_user.id)
    await announce_grant(target, sub, resumed)
    await message.answer(f"✅ Granted {PLANS[plan_key]['name']} to <code>{target}</code> until {fmt_dt(sub['expiry'])}.",
                         parse_mode="HTML")


@dp.message(Command("revoke", ignore_case=True))
async def cmd_revoke(message: types.Message):
    if not _admin_only(message):
        return
    uid = message.from_user.id
    parts = (message.text or '').split()
    if len(parts) < 2:
        pending_input[uid] = {'action': 'revoke', 'ts': time.time()}
        await message.answer("Send the <b>User ID</b> to revoke access from.\n\nSend /cancel to abort.",
                             parse_mode="HTML")
        return
    if not parts[1].isdigit():
        await message.answer("Usage: /revoke USER_ID")
        return
    await message.answer(await do_revoke(int(parts[1])), parse_mode="HTML")


async def do_revoke(target):
    if target in admin_ids:
        return "Admins cannot be revoked. Use /removeadmin first."
    existed = await revoke_access(target)
    if not existed:
        return f"User <code>{target}</code> has no subscription."
    await notify_user(target, denied_text({'reason': 'none'}), denied_keyboard())
    return (f"✅ Access revoked for <code>{target}</code>.\nTheir bots are paused; files and data are kept.")


@dp.message(Command("kill", ignore_case=True))
async def cmd_kill(message: types.Message):
    if not _admin_only(message):
        return
    uid = message.from_user.id
    parts = (message.text or '').split()
    if len(parts) < 2:
        pending_input[uid] = {'action': 'kill', 'ts': time.time()}
        await message.answer("Send the <b>User ID</b> whose running bots you want to kill.\n\nSend /cancel to abort.",
                             parse_mode="HTML")
        return
    if not parts[1].isdigit():
        await message.answer("Usage: /kill USER_ID")
        return
    await message.answer(await do_kill(int(parts[1]), uid), parse_mode="HTML")


@dp.message(Command("users", ignore_case=True))
async def cmd_users(message: types.Message):
    if not _admin_only(message):
        return
    text, kb = users_view(0)
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


@dp.message(F.text, lambda m: bool(m.from_user) and m.from_user.id in pending_input)
async def on_pending_text(message: types.Message):
    uid = message.from_user.id
    state = pending_input.get(uid)
    text = (message.text or '').strip()
    if not state or time.time() - state['ts'] > 300:
        pending_input.pop(uid, None)
        await message.answer("That request expired. Start again from the admin panel or with the command.")
        return
    if text.startswith('/'):
        pending_input.pop(uid, None)
        return
    match = re.search(r'\d{3,15}', text)
    if not match:
        await message.answer("Please send a numeric User ID (digits only), or /cancel.")
        return
    target = int(match.group())
    action = state['action']
    pending_input.pop(uid, None)
    if action == 'trial':
        await message.answer(await do_trial(target, uid), parse_mode="HTML")
    elif action == 'revoke':
        await message.answer(await do_revoke(target), parse_mode="HTML")
    elif action == 'kill':
        await message.answer(await do_kill(target, uid), parse_mode="HTML")
    elif action == 'grant':
        await message.answer(f"Choose a plan for <b>{user_label(target)}</b> (<code>{target}</code>):",
                             reply_markup=plan_pick_keyboard(target), parse_mode="HTML")


# ═══════════════════════════════════════════════════════════════════
#  USER FILES VAULT  (owner only)  +  BOT LOCK / DELETE
#  - Owner browses every user's files, downloads the exact file, deletes it,
#    or Locks / Unlocks it.
#  - A locked bot cannot be deleted, replaced or updated by its user.
# ═══════════════════════════════════════════════════════════════════
LOCK_ALERT = "THE BOT IS ENCRYPTED BY ADMIN"
UF_USERS_PER_PAGE = 6
UF_FILES_PER_PAGE = 8
UF_LINE = "━━━━━━━━━━━━━━━━━━"
TG_SEND_LIMIT = 49 * 1024 * 1024   # Telegram bots can send files up to 50 MB

_FILE_KIND = {**{e[1:]: v for e, v in LANGS.items()}, 'zip': 'ZIP archive', 'tar': 'TAR archive', 'env': 'Environment file'}


def is_locked(uid, fname):
    """Locked = the admin pressed the panel's Lock Bot (every hosted bot) or locked this single file."""
    return bot_locked or (uid, fname) in locked_files


def set_bot_locked(value):
    global bot_locked
    bot_locked = bool(value)
    bot_stats['bot_locked'] = int(bot_locked)
    try:
        dbx('INSERT OR REPLACE INTO bot_stats (stat_name, stat_value) VALUES (?, ?)', ('bot_locked', int(bot_locked)))
    except Exception as e:
        logger.warning(f"set_bot_locked db error: {e}")
    logger.info(f"GLOBAL LOCK {'ON' if bot_locked else 'OFF'}")
    return bot_locked


def set_locked(uid, fname, locked, by):
    if locked:
        locked_files.add((uid, fname))
        dbx('INSERT OR REPLACE INTO locked_files (user_id, file_name, locked_at, locked_by) VALUES (?, ?, ?, ?)',
            (uid, fname, now_utc().isoformat(), by))
    else:
        locked_files.discard((uid, fname))
        dbx('DELETE FROM locked_files WHERE user_id = ? AND file_name = ?', (uid, fname))
    logger.info(f"File {'LOCKED' if locked else 'UNLOCKED'}: user={uid} file={fname} by={by}")


def log_deleted_file(uid, fname, by):
    """Keep an exact copy of the file and remember the deletion for the owner."""
    try:
        fp = UPLOAD_BOTS_DIR / str(uid) / fname
        size, arch = 0, None
        if fp.is_file():
            size = fp.stat().st_size
            folder = DELETED_DIR / str(uid)
            folder.mkdir(parents=True, exist_ok=True)
            stamp = now_utc().strftime('%Y%m%d%H%M%S%f')
            dest = folder / f"{stamp}_{_flat(fname)}"
            shutil.copy2(fp, dest)
            arch = f"{uid}/{dest.name}"
        dbx('INSERT INTO deleted_files (user_id, file_name, file_type, size_bytes, deleted_at, deleted_by, archive_path) '
            'VALUES (?, ?, ?, ?, ?, ?, ?)',
            (uid, fname, _file_type(uid, fname), size, now_utc().isoformat(), by, arch))
    except Exception as e:
        logger.warning(f"log_deleted_file error: {e}")


def deleted_counts():
    try:
        return {u: n for u, n in dbx('SELECT user_id, COUNT(*) FROM deleted_files GROUP BY user_id')}
    except Exception:
        return {}


def deleted_list(uid):
    try:
        return dbx('SELECT file_name, file_type, size_bytes, deleted_at, deleted_by, id, archive_path FROM deleted_files '
                   'WHERE user_id = ? ORDER BY id DESC', (uid,))
    except Exception:
        return []


async def delete_user_file(uid, fname, by=None):
    """Stop the bot and remove the file everywhere (disk, memory, database)."""
    log_deleted_file(uid, fname, by if by is not None else uid)
    try:
        await stop_bot(uid, fname, status=None)
    except Exception as e:
        logger.warning(f"delete_user_file stop error: {e}")
    fp = UPLOAD_BOTS_DIR / str(uid) / fname
    if fp.exists():
        fp.unlink()
    forget_bot(uid, fname)
    if uid in user_files:
        user_files[uid] = [f for f in user_files[uid] if f[0] != fname]
    if fname in user_favorites.get(uid, []):
        user_favorites[uid].remove(fname)
    dbx('DELETE FROM user_files WHERE user_id = ? AND file_name = ?', (uid, fname))
    dbx('DELETE FROM favorites WHERE user_id = ? AND file_name = ?', (uid, fname))
    locked_files.discard((uid, fname))
    dbx('DELETE FROM locked_files WHERE user_id = ? AND file_name = ?', (uid, fname))
    invalidate_size(uid)


def _owner_only(cb):
    return cb.from_user.id in (OWNER_ID, ADMIN_ID)


def _file_type(uid, fname):
    for f, t in user_files.get(uid, []):
        if f == fname:
            return t
    return os.path.splitext(fname)[1].lstrip('.').lower()


def _upload_date(uid, fname):
    try:
        row = dbx('SELECT upload_date FROM user_files WHERE user_id = ? AND file_name = ?', (uid, fname))
        if row and row[0][0]:
            return row[0][0][:16].replace('T', ' ') + ' UTC'
    except Exception:
        pass
    return '-'


def _short(text, n):
    return text if len(text) <= n else text[:n - 1] + '…'


# ───────────── screen 1: users that have files ─────────────
def uf_users_view(page=0):
    dcount = deleted_counts()
    ids = sorted({u for u, fl in user_files.items() if fl} | set(dcount),
                 key=lambda u: (user_profiles.get(u, {}).get('name') or str(u)).lower())
    total_files = sum(len(user_files.get(u, [])) for u in ids)
    total_locked = sum(1 for u, f in locked_files if any(x[0] == f for x in user_files.get(u, [])))
    pages = max(1, -(-len(ids) // UF_USERS_PER_PAGE))
    page = max(0, min(page, pages - 1))
    lines = ["<b>USER FILES</b>", "Secure file vault · owner access", UF_LINE,
             f"Users: <b>{len(ids)}</b>   Files: <b>{total_files}</b>   Locked: <b>{total_locked}</b>", ""]
    rows, pair = [], []
    chunk = ids[page * UF_USERS_PER_PAGE:(page + 1) * UF_USERS_PER_PAGE]
    if not chunk:
        lines.append("No user files yet.")
    else:
        lines.append("Select a user to view their files.")
        lines.append("")
    for i, uid in enumerate(chunk, page * UF_USERS_PER_PAGE + 1):
        n = len(user_files.get(uid, []))
        nl = sum(1 for f, _ in user_files.get(uid, []) if is_locked(uid, f))
        lock_txt = (f" · {nl} locked" if nl else "") + (f" · {dcount[uid]} deleted" if dcount.get(uid) else "")
        lines.append(f"{i}. <b>{user_label(uid)}</b> <code>{uid}</code>")
        lines.append(f"     {n} file{'s' if n != 1 else ''}{lock_txt}")
        name = (user_profiles.get(uid) or {}).get('name') or str(uid)
        pair.append(InlineKeyboardButton(text=f"{i}. {_short(name, 12)} ({n})",
                                         callback_data=f"uf_user:{uid}:0", style="primary"))
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    if pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="Prev", callback_data=f"uf_users:{page - 1}", style="primary"))
        nav.append(InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data=f"uf_users:{page}", style="primary"))
        if page < pages - 1:
            nav.append(InlineKeyboardButton(text="Next", callback_data=f"uf_users:{page + 1}", style="primary"))
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="Refresh", callback_data=f"uf_users:{page}", style="primary"),
                 InlineKeyboardButton(text="Admin Panel", callback_data="admin_panel", style="primary")])
    text = "\n".join(lines)
    if len(text) > 3800:
        text = text[:text.rfind("\n", 0, 3780)] + "\n..."
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


# ───────────── screen 2: files of one user ─────────────
def uf_files_view(uid, page=0):
    files = sorted(user_files.get(uid, []), key=lambda x: x[0].lower())
    pages = max(1, -(-len(files) // UF_FILES_PER_PAGE))
    page = max(0, min(page, pages - 1))
    lines = [f"<b>{user_label(uid)}</b>", f"ID <code>{uid}</code>", UF_LINE,
             f"Files: <b>{len(files)}</b>   Running: <b>{running_count(uid)}</b>   "
             f"Storage: <b>{fmt_size(folder_size(uid))}</b>", ""]
    rows = []
    if not files:
        lines.append("This user has no files.")
    else:
        lines.append("Select a file to download, lock or delete.")
        lines.append("")
    start = page * UF_FILES_PER_PAGE
    for i, (fname, ftype) in enumerate(files[start:start + UF_FILES_PER_PAGE], start + 1):
        fp = UPLOAD_BOTS_DIR / str(uid) / fname
        size = fmt_size(fp.stat().st_size) if fp.exists() else 'missing'
        locked = is_locked(uid, fname)
        if ftype in RUN_TYPES:
            state = _STATUS_WORD[get_bot_status(uid, fname)]
        else:
            state = 'Archive'
        lines.append(f"{i}. <b>{esc(fname)}</b>")
        lines.append(f"     {_FILE_KIND.get(ftype, ftype.upper())} · {size} · {state}"
                     f"{' · <b>Locked</b>' if locked else ''}")
        label = f"{'Locked · ' if locked else ''}{i}. {_short(fname, 22)}"
        rows.append([InlineKeyboardButton(text=label, callback_data=f"uf_file:{uid}:{cb_name(fname)}",
                                          style="danger" if locked else "primary")])
    if pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="Prev", callback_data=f"uf_user:{uid}:{page - 1}", style="primary"))
        nav.append(InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data=f"uf_user:{uid}:{page}", style="primary"))
        if page < pages - 1:
            nav.append(InlineKeyboardButton(text="Next", callback_data=f"uf_user:{uid}:{page + 1}", style="primary"))
        rows.append(nav)
    nd = deleted_counts().get(uid, 0)
    if nd:
        rows.append([InlineKeyboardButton(text=f"Deleted Files ({nd})", callback_data=f"uf_deleted:{uid}:0",
                                          style="danger")])
    rows.append([InlineKeyboardButton(text="All Users", callback_data="uf_users:0", style="primary"),
                 InlineKeyboardButton(text="Admin Panel", callback_data="admin_panel", style="primary")])
    text = "\n".join(lines)
    if len(text) > 3800:
        text = text[:text.rfind("\n", 0, 3780)] + "\n..."
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


# ───────────── screen 2b: deleted files of one user ─────────────
def uf_deleted_view(uid, page=0):
    items = deleted_list(uid)
    per = 6
    pages = max(1, -(-len(items) // per))
    page = max(0, min(page, pages - 1))
    lines = [f"<b>DELETED FILES</b>", f"{user_label(uid)} · <code>{uid}</code>", UF_LINE,
             f"Total deleted: <b>{len(items)}</b>", ""]
    if not items:
        lines.append("No deleted files.")
    rows = []
    start = page * per
    for n, (fname, ftype, size, when, by, did, arch) in enumerate(items[start:start + per], start + 1):
        who = "the user" if by == uid else "admin"
        lines.append(f"<b>DELETED FILE {n}</b>")
        lines.append(f"File: <b>{esc(fname)}</b>")
        lines.append(f"{_FILE_KIND.get((ftype or '').lower(), (ftype or '?').upper())} · {fmt_size(size or 0)}")
        lines.append(f"Deleted: {(when or '-')[:16].replace('T', ' ')} UTC · by {who}")
        lines.append("")
        rows.append([InlineKeyboardButton(text=f"{n}. {_short(fname, 24)}", callback_data=f"uf_dfile:{did}",
                                          style="danger")])
    if items:
        lines.append("Select a deleted file to download it.")
    if pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="Prev", callback_data=f"uf_deleted:{uid}:{page - 1}", style="primary"))
        nav.append(InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data=f"uf_deleted:{uid}:{page}", style="primary"))
        if page < pages - 1:
            nav.append(InlineKeyboardButton(text="Next", callback_data=f"uf_deleted:{uid}:{page + 1}", style="primary"))
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="User Files", callback_data=f"uf_user:{uid}:0", style="primary"),
                 InlineKeyboardButton(text="All Users", callback_data="uf_users:0", style="primary")])
    text = "\n".join(lines)
    if len(text) > 3800:
        text = text[:text.rfind("\n", 0, 3780)] + "\n..."
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def _deleted_row(did):
    r = dbx('SELECT user_id, file_name, file_type, size_bytes, deleted_at, deleted_by, archive_path '
            'FROM deleted_files WHERE id = ?', (did,))
    return r[0] if r else None


def uf_dfile_view(did):
    row = _deleted_row(did)
    if not row:
        return "Deleted file record not found.", InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="All Users", callback_data="uf_users:0", style="primary")]])
    uid, fname, ftype, size, when, by, arch = row
    has_copy = bool(arch) and (DELETED_DIR / arch).is_file()
    lines = ["<b>DELETED FILE</b>", UF_LINE,
             f"File: <b>{esc(fname)}</b>",
             f"Owner: {user_label(uid)} · <code>{uid}</code>",
             f"Type: {_FILE_KIND.get((ftype or '').lower(), (ftype or '?').upper())} · {fmt_size(size or 0)}",
             f"Deleted: {(when or '-')[:16].replace('T', ' ')} UTC · by {'the user' if by == uid else 'admin'}", ""]
    lines.append("Download sends this exact deleted file." if has_copy
                 else "No saved copy exists for this file (it was deleted before copies were kept).")
    rows = []
    if has_copy:
        rows.append([InlineKeyboardButton(text="Download File", callback_data=f"uf_ddl:{did}", style="success")])
    rows.append([InlineKeyboardButton(text="Deleted Files", callback_data=f"uf_deleted:{uid}:0", style="primary"),
                 InlineKeyboardButton(text="All Users", callback_data="uf_users:0", style="primary")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


# ───────────── screen 3: one file ─────────────
def uf_file_view(uid, fname, note=None):
    ftype = _file_type(uid, fname)
    fp = UPLOAD_BOTS_DIR / str(uid) / fname
    size = fmt_size(fp.stat().st_size) if fp.exists() else 'missing from disk'
    locked = is_locked(uid, fname)
    tok = cb_name(fname)
    lines = []
    if note:
        lines += [f"<i>{esc(note)}</i>", ""]
    lines += ["<b>FILE DETAILS</b>", UF_LINE,
              f"File: <b>{esc(fname)}</b>",
              f"Owner: {user_label(uid)} · <code>{uid}</code>",
              f"Type: {_FILE_KIND.get(ftype, ftype.upper())} · {size}",
              f"Uploaded: {_upload_date(uid, fname)}"]
    if ftype in RUN_TYPES:
        lines.append(f"Status: <b>{_STATUS_WORD[get_bot_status(uid, fname)]}</b>")
    lines.append(f"Security: <b>{'Locked and Encrypted' if locked else 'Unlocked'}</b>")
    lines += ["", "Download sends this exact file as the user uploaded it."]
    if locked:
        lines.append("The user cannot delete, replace or update this bot. Pause, Run and Restart still work.")
    own_lock = (uid, fname) in locked_files
    lock_btn = (InlineKeyboardButton(text="Unlock Bot", callback_data=f"uf_unlock:{uid}:{tok}", style="success")
                if own_lock else
                InlineKeyboardButton(text="Lock Bot", callback_data=f"uf_lock:{uid}:{tok}", style="danger"))
    rows = [[InlineKeyboardButton(text="Download File", callback_data=f"uf_dl:{uid}:{tok}", style="success")],
            [InlineKeyboardButton(text="Delete Bot", callback_data=f"uf_del:{uid}:{tok}", style="danger"), lock_btn],
            [InlineKeyboardButton(text="User Files", callback_data=f"uf_user:{uid}:0", style="primary"),
             InlineKeyboardButton(text="All Users", callback_data="uf_users:0", style="primary")]]
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


def _uf_parse(cb):
    """callback 'prefix:uid:token' -> (uid, fname or None)."""
    parts = cb.data.split(":", 2)
    uid = int(parts[1])
    fname = resolve_name(uid, parts[2]) if len(parts) > 2 else None
    return uid, fname


# ───────────── handlers ─────────────
@dp.callback_query(F.data.startswith("uf_users:"))
async def cb_uf_users(cb: types.CallbackQuery):
    if not _owner_only(cb):
        await cb.answer("Owner only.", show_alert=True)
        return
    try:
        page = int(cb.data.split(":", 1)[1])
    except ValueError:
        page = 0
    text, kb = uf_users_view(page)
    await safe_edit(cb, text, kb)
    await cb.answer()


@dp.callback_query(F.data.startswith("uf_user:"))
async def cb_uf_user(cb: types.CallbackQuery):
    if not _owner_only(cb):
        await cb.answer("Owner only.", show_alert=True)
        return
    parts = cb.data.split(":")
    try:
        uid, page = int(parts[1]), int(parts[2]) if len(parts) > 2 else 0
    except ValueError:
        await cb.answer("Invalid request.", show_alert=True)
        return
    text, kb = uf_files_view(uid, page)
    await safe_edit(cb, text, kb)
    await cb.answer()


@dp.callback_query(F.data.startswith("uf_deleted:"))
async def cb_uf_deleted(cb: types.CallbackQuery):
    if not _owner_only(cb):
        await cb.answer("Owner only.", show_alert=True)
        return
    parts = cb.data.split(":")
    try:
        uid, page = int(parts[1]), int(parts[2]) if len(parts) > 2 else 0
    except ValueError:
        await cb.answer("Invalid request.", show_alert=True)
        return
    text, kb = uf_deleted_view(uid, page)
    await safe_edit(cb, text, kb)
    await cb.answer()


@dp.callback_query(F.data.startswith("uf_dfile:"))
async def cb_uf_dfile(cb: types.CallbackQuery):
    if not _owner_only(cb):
        await cb.answer("Owner only.", show_alert=True)
        return
    try:
        did = int(cb.data.split(":")[1])
    except (ValueError, IndexError):
        await cb.answer("Invalid request.", show_alert=True)
        return
    text, kb = uf_dfile_view(did)
    await safe_edit(cb, text, kb)
    await cb.answer()


@dp.callback_query(F.data.startswith("uf_ddl:"))
async def cb_uf_deleted_download(cb: types.CallbackQuery):
    if not _owner_only(cb):
        await cb.answer("Owner only.", show_alert=True)
        return
    try:
        row = _deleted_row(int(cb.data.split(":")[1]))
    except (ValueError, IndexError):
        row = None
    if not row or not row[6]:
        await cb.answer("No saved copy of this file.", show_alert=True)
        return
    uid, fname, _t, _s, _w, _b, arch = row
    fp = DELETED_DIR / arch
    if not fp.is_file():
        await cb.answer("The saved copy is missing from disk.", show_alert=True)
        return
    if fp.stat().st_size > TG_SEND_LIMIT:
        await cb.answer("File is larger than Telegram's 50 MB bot limit.", show_alert=True)
        return
    await cb.answer("Sending deleted file...")
    try:
        await cb.message.answer_document(
            FSInputFile(fp, filename=fname),
            caption=f"{fname}\nDELETED FILE · Owner: {uid} · {fmt_size(fp.stat().st_size)}")
        logger.info(f"Owner {cb.from_user.id} downloaded deleted file {fname} of user {uid}")
    except Exception as e:
        logger.error(f"deleted download error: {e}")
        await cb.message.answer(f"Could not send the file: {esc(str(e))[:200]}", parse_mode="HTML")


@dp.callback_query(F.data.startswith("uf_file:"))
async def cb_uf_file(cb: types.CallbackQuery):
    if not _owner_only(cb):
        await cb.answer("Owner only.", show_alert=True)
        return
    uid, fname = _uf_parse(cb)
    if not fname:
        await cb.answer("File not found. It may have been deleted.", show_alert=True)
        return
    text, kb = uf_file_view(uid, fname)
    await safe_edit(cb, text, kb)
    await cb.answer()


@dp.callback_query(F.data.startswith("uf_dl:"))
async def cb_uf_download(cb: types.CallbackQuery):
    if not _owner_only(cb):
        await cb.answer("Owner only.", show_alert=True)
        return
    uid, fname = _uf_parse(cb)
    if not fname:
        await cb.answer("File not found.", show_alert=True)
        return
    fp = UPLOAD_BOTS_DIR / str(uid) / fname
    if not fp.is_file():
        await cb.answer("The file is missing from disk.", show_alert=True)
        return
    if fp.stat().st_size > TG_SEND_LIMIT:
        await cb.answer("File is larger than Telegram's 50 MB bot limit.", show_alert=True)
        return
    await cb.answer("Sending file...")
    try:
        await cb.message.answer_document(
            FSInputFile(fp, filename=fname),
            caption=f"{fname}\nOwner: {uid} · {fmt_size(fp.stat().st_size)}")
        logger.info(f"Owner {cb.from_user.id} downloaded {fname} of user {uid}")
    except Exception as e:
        logger.error(f"uf download error: {e}")
        await cb.message.answer(f"Could not send the file: {esc(str(e))[:200]}", parse_mode="HTML")


@dp.callback_query(F.data.startswith("uf_lock:") | F.data.startswith("uf_unlock:"))
async def cb_uf_lock(cb: types.CallbackQuery):
    if not _owner_only(cb):
        await cb.answer("Owner only.", show_alert=True)
        return
    uid, fname = _uf_parse(cb)
    if not fname:
        await cb.answer("File not found.", show_alert=True)
        return
    lock = cb.data.startswith("uf_lock:")
    set_locked(uid, fname, lock, cb.from_user.id)
    note = "Bot locked. THE BOT IS ENCRYPTED BY ADMIN - the user can no longer delete or replace it, but can still Pause, Run and Restart." if lock else "Bot unlocked."
    await cb.answer("Bot locked - button is now Unlock Bot" if lock else "Bot unlocked - button is now Lock Bot", show_alert=False)
    text, kb = uf_file_view(uid, fname, note=note)
    await safe_edit(cb, text, kb)


@dp.callback_query(F.data.startswith("uf_del:"))
async def cb_uf_delete(cb: types.CallbackQuery):
    if not _owner_only(cb):
        await cb.answer("Owner only.", show_alert=True)
        return
    uid, fname = _uf_parse(cb)
    if not fname:
        await cb.answer("File not found.", show_alert=True)
        return
    if (uid, fname) in locked_files:
        await cb.answer(f"{LOCK_ALERT}. Unlock it first to delete.", show_alert=True)
        return
    tok = cb_name(fname)
    text = (f"<b>DELETE BOT</b>\n{UF_LINE}\n"
            f"File: <b>{esc(fname)}</b>\nOwner: {user_label(uid)} · <code>{uid}</code>\n\n"
            "This stops the bot and permanently removes the file. This cannot be undone.")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Confirm Delete", callback_data=f"uf_delok:{uid}:{tok}", style="danger"),
         InlineKeyboardButton(text="Cancel", callback_data=f"uf_file:{uid}:{tok}", style="primary")]])
    await safe_edit(cb, text, kb)
    await cb.answer()


@dp.callback_query(F.data.startswith("uf_delok:"))
async def cb_uf_delete_ok(cb: types.CallbackQuery):
    if not _owner_only(cb):
        await cb.answer("Owner only.", show_alert=True)
        return
    uid, fname = _uf_parse(cb)
    if not fname:
        await cb.answer("File not found.", show_alert=True)
        return
    if (uid, fname) in locked_files:
        await cb.answer(f"{LOCK_ALERT}. Unlock it first to delete.", show_alert=True)
        return
    try:
        await delete_user_file(uid, fname, by=cb.from_user.id)
    except Exception as e:
        logger.error(f"uf delete error: {e}")
        await cb.answer(f"Delete failed: {str(e)[:150]}", show_alert=True)
        return
    logger.info(f"Owner {cb.from_user.id} deleted {fname} of user {uid}")
    await cb.answer("File deleted.")
    await notify_user(uid, f"Your file <b>{esc(fname)}</b> was removed by the administrator.")
    text, kb = uf_files_view(uid, 0)
    await safe_edit(cb, text, kb)


# ───────────── user side: Delete Bot button ─────────────
@dp.callback_query(F.data.startswith("bot_del:"))
async def cb_bot_del(cb: types.CallbackQuery):
    uid, fname = _bot_from_cb(cb)
    if not fname:
        await cb.answer("Bot not found.", show_alert=True)
        return
    if is_locked(uid, fname):
        await cb.answer(LOCK_ALERT, show_alert=True)
        return
    tok = cb_name(fname)
    text = (f"<b>Delete {esc(fname)}?</b>\n\n"
            "The bot will be stopped and the file permanently removed. This cannot be undone.")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Yes, Delete", callback_data=f"bot_delok:{tok}", style="danger"),
         InlineKeyboardButton(text="Cancel", callback_data=f"bot:{tok}", style="primary")]])
    await safe_edit(cb, text, kb)
    await cb.answer()


@dp.callback_query(F.data.startswith("bot_delok:"))
async def cb_bot_del_ok(cb: types.CallbackQuery):
    uid, fname = _bot_from_cb(cb)
    if not fname:
        await cb.answer("Bot not found.", show_alert=True)
        return
    if is_locked(uid, fname):
        await cb.answer(LOCK_ALERT, show_alert=True)
        return
    try:
        await delete_user_file(uid, fname, by=cb.from_user.id)
    except Exception as e:
        logger.error(f"bot delete error: {e}")
        await cb.answer(f"Delete failed: {str(e)[:150]}", show_alert=True)
        return
    await cb.answer("Bot deleted.")
    text, kb = my_bots_view(uid)
    await safe_edit(cb, f"Deleted <b>{esc(fname)}</b>.\n\n" + text, kb)



def kill_duplicate_instances(my_start=None):
    """Stop OLDER copies of this bot that poll the same token.
    Two copies polling one token is what makes every reply arrive twice."""
    try:
        me = psutil.Process(os.getpid())
        protected = {me.pid} | {p.pid for p in me.parents()}
        if my_start is None:
            my_start = me.create_time()
    except Exception:
        return 0
    killed = 0
    for p in psutil.process_iter(['pid', 'cmdline']):
        try:
            if p.pid in protected:
                continue
            cmd = p.info.get('cmdline') or []
            if len(cmd) < 2 or 'python' not in os.path.basename(cmd[0]).lower():
                continue
            script = next((x for x in cmd[1:] if x.lower().endswith('.py')), None)
            if not script:
                continue
            path = Path(script)
            if not path.is_absolute():
                path = Path(p.cwd()) / script
            path = path.resolve()
            if not path.is_file():
                continue
            if path != HOST_FILE:
                if path.stat().st_size > 8 * MB:
                    continue
                if TOKEN not in path.read_text(encoding='utf-8', errors='ignore'):
                    continue
            if p.create_time() >= my_start:
                continue                       # never touch a newer copy
            victims = p.children(recursive=True) + [p]
            for v in victims:
                try:
                    v.terminate()
                except Exception:
                    pass
            _gone, alive = psutil.wait_procs(victims, timeout=3)
            for v in alive:
                try:
                    v.kill()
                except Exception:
                    pass
            killed += 1
            logger.warning(f"Stopped an older copy of this bot (PID {p.pid}, {path.name}) to prevent duplicate replies")
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            continue
        except Exception as e:
            logger.debug(f"duplicate scan error: {e}")
    return killed


background_tasks = set()


def _spawn(coro):
    task = asyncio.create_task(coro)
    background_tasks.add(task)
    task.add_done_callback(background_tasks.discard)
    return task


async def startup_services():
    psutil.cpu_percent(interval=None)       # prime the CPU counter so the dashboard is accurate
    await asyncio.to_thread(_secure_dirs)
    await asyncio.to_thread(reap_orphans)
    _spawn(bot_watchdog())
    _spawn(subscription_watcher())
    _spawn(resume_bots_on_startup())


async def shutdown_services():
    for task in list(background_tasks):
        task.cancel()
    # mark=None keeps each bot's saved 'running' state so it resumes on the next start
    for key in list(bot_scripts.keys()):
        await asyncio.to_thread(_stop_script, key, None)



@dp.message(Command("restart"))
async def cmd_restart(message: types.Message):
    if message.from_user.id != OWNER_ID:
        return
    await message.answer("Restarting... all hosted scripts will be stopped.")
    for key in list(bot_scripts.keys()):
        _stop_script(key, None)   # keep saved state so bots resume after the restart
    await asyncio.sleep(1)
    os.execv(sys.executable, [sys.executable] + sys.argv)

async def on_error(event: ErrorEvent):
    exc = event.exception
    if 'message is not modified' in str(exc).lower():
        cb = event.update.callback_query
        if cb:
            try:
                await cb.answer()
            except Exception:
                pass
        return True
    logger.error(f"Handler error: {exc}", exc_info=exc)
    cb = event.update.callback_query
    if cb:
        try:
            await cb.answer("Something went wrong, please try again.", show_alert=True)
        except Exception:
            pass
    return True

dp.errors.register(on_error)

async def _graceful_stop():
    try:
        await dp.stop_polling()
    except Exception:
        pass


async def main():
    logger.info("Starting KALPRO Hosting Bot...")

    # Bind the HTTP port FIRST: Render marks a web service as failed if no port opens in time.
    await web_server()
    _spawn(keep_alive())

    try:
        me = await bot.get_me()
        logger.info(f"Logged in as @{me.username} (id {me.id})")
    except Exception as e:
        logger.error(f"TOKEN REJECTED by Telegram ({e}). Revoked or wrong token - get a fresh one from @BotFather.")
        raise
    # An older copy polling the same token makes every reply arrive twice - stop it first.
    if await asyncio.to_thread(kill_duplicate_instances):
        await asyncio.sleep(2)
    # A leftover webhook silently blocks polling - always clear it.
    await bot.delete_webhook(drop_pending_updates=True)
    await startup_services()
    try:                                    # Render stops a service with SIGTERM: shut down cleanly
        import signal
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, lambda: asyncio.ensure_future(_graceful_stop()))
    except (NotImplementedError, RuntimeError, ValueError):
        pass
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types(), handle_signals=False)
    finally:
        await shutdown_services()


if __name__ == "__main__":
    asyncio.run(main())
