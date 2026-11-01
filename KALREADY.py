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
import socket
import tarfile
import heapq
import ast
from concurrent.futures import ThreadPoolExecutor
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

# Hard guard: if a host launched this very file as a hosted script, stop - never run the host over itself.
if os.environ.get('KALPRO_HOSTED_CHILD') == '1' and os.environ.get('ALLOW_NESTED_HOST') != '1':
    print("[KALPRO] Refusing to start: this file is the hosting bot itself and was launched as a hosted script. "
          "A hosted script must be a normal bot with its own token.", flush=True)
    sys.exit(3)

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

# Standalone (Render): set BOT_TOKEN. Nested under a parent bot: set SECOND_BOT_TOKEN (BOT_TOKEN is then the parent's).
_SECOND_TOKEN = os.getenv('SECOND_BOT_TOKEN')
_MAIN_TOKEN_ENV = os.getenv('BOT_TOKEN')
_FALLBACK_TOKEN = '9001715806:AAHJb39aNjBzqJsyWzvt12ECDuj4tXmN5IY'
if _SECOND_TOKEN:
    TOKEN = _SECOND_TOKEN
    _PARENT_TOKEN = _MAIN_TOKEN_ENV
else:
    TOKEN = _MAIN_TOKEN_ENV or _FALLBACK_TOKEN
    _PARENT_TOKEN = None
RUN_WEB = bool(os.getenv('RENDER')) or os.getenv('KALPRO_WEB', '').lower() in ('1', 'true', 'yes')
OWNER_ID_STR = os.getenv('OWNER_ID')
ADMIN_ID_STR = os.getenv('ADMIN_ID')
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

def _safe_name(name):
    """Safe, short filename (callback_data is limited to 64 bytes)."""
    name = os.path.basename((name or 'file').replace('\\', '/'))
    if name.lower() == '.env':
        return '.env'
    stem, ext = os.path.splitext(name)
    stem = re.sub(r'[^A-Za-z0-9._-]+', '_', stem).strip('._') or 'file'
    return stem[:28] + ext.lower()

def _child_env():
    """Environment for hosted scripts: no host tokens/IDs/Render vars, their own free PORT, unbuffered output."""
    secrets = [t for t in (TOKEN, _PARENT_TOKEN) if t]
    env = {}
    for k, v in os.environ.items():
        ku = k.upper()
        if ku in _SECRET_ENV_KEYS or ku == 'RENDER' or ku.startswith(('RENDER_', 'KALPRO_')):
            continue
        if any(t in v for t in secrets):
            continue
        env[k] = v
    env.update({k: v for k, v in _tool_env().items() if k in ('GOCACHE', 'GOPATH', 'npm_config_cache')})
    env['PYTHONUNBUFFERED'] = '1'
    env['PYTHONIOENCODING'] = 'utf-8'
    env['KALPRO_HOSTED_CHILD'] = '1'
    env['PORT'] = str(_free_port())
    return env

def _shield_env_file(folder):
    """An empty .env stops python-dotenv from walking up and loading the HOST's .env."""
    env_file = Path(folder) / '.env'
    if not env_file.exists():
        env_file.write_text('', encoding='utf-8')

def _proc_alive(key):
    """True only while the hosted process is really running (the watchdog does the cleanup)."""
    info = bot_scripts.get(key)
    return bool(info) and info['process'].poll() is None

# ═══════════════════════════════════════════════════════════════════
#  ENGINE  ·  languages · dependency installer · self-host guard · fast archives
# ═══════════════════════════════════════════════════════════════════
CACHE_DIR = BASE_DIR / 'cache'
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# file extension -> (label, icon). Anything listed here can be run by the host.
RUN_KINDS = {
    '.py': ('Python', '🐍'), '.js': ('Node.js', '🟨'), '.mjs': ('Node.js', '🟨'), '.cjs': ('Node.js', '🟨'),
    '.ts': ('TypeScript', '🔷'), '.sh': ('Shell', '🐚'), '.bash': ('Shell', '🐚'), '.php': ('PHP', '🐘'),
    '.rb': ('Ruby', '💎'), '.go': ('Go', '🐹'), '.java': ('Java', '☕'), '.jar': ('Java', '☕'),
    '.pl': ('Perl', '🐪'), '.lua': ('Lua', '🌙'), '.c': ('C', '⚙️'), '.cpp': ('C++', '⚙️'),
    '.cc': ('C++', '⚙️'), '.rs': ('Rust', '🦀'),
}
ARCHIVE_SUFFIXES = ('.zip', '.tar', '.tar.gz', '.tgz', '.tar.bz2', '.tbz2', '.tar.xz', '.txz')
TEXT_ICONS = {'json': '🧾', 'txt': '📝', 'md': '📝', 'env': '🔐', 'yml': '🧾', 'yaml': '🧾', 'toml': '🧾',
              'html': '🌐', 'css': '🎨', 'db': '🗄️', 'sqlite': '🗄️', 'csv': '📊', 'sql': '🗄️'}
_NODE_ENTRY_FILES = ('package.json',)


def is_archive_name(name):
    return str(name).lower().endswith(ARCHIVE_SUFFIXES)


def is_runnable(fname):
    base = os.path.basename(str(fname)).lower()
    return base in _NODE_ENTRY_FILES or os.path.splitext(base)[1] in RUN_KINDS


def file_kind(fname):
    """Human label for a file name (Python, Node.js, PHP, ZIP archive, ...)."""
    base = os.path.basename(str(fname)).lower()
    if base == 'package.json':
        return 'Node.js project'
    if is_archive_name(base):
        return 'ZIP archive' if base.endswith('.zip') else 'TAR archive'
    ext = os.path.splitext(base)[1]
    if ext in RUN_KINDS:
        return RUN_KINDS[ext][0]
    return (ext[1:].upper() + ' file') if ext else 'File'


def file_icon(fname):
    base = os.path.basename(str(fname)).lower()
    if base == 'package.json':
        return '🟨'
    if is_archive_name(base):
        return '📦'
    ext = os.path.splitext(base)[1]
    if ext in RUN_KINDS:
        return RUN_KINDS[ext][1]
    return TEXT_ICONS.get(ext[1:], '📄')


def stored_type(fname):
    """Value kept in user_files.file_type."""
    base = os.path.basename(str(fname)).lower()
    if base.endswith('.zip'):
        return 'zip'
    if is_archive_name(base):
        return 'tar'
    ext = os.path.splitext(base)[1][1:]
    return ext or 'file'


def supported_text():
    langs = sorted({v[0] for v in RUN_KINDS.values()})
    return ', '.join(langs)


def _flat(fname):
    """File name that is safe to use inside a single path component."""
    return str(fname).replace('/', '__').replace('\\', '__')


def _which(*names):
    for n in names:
        p = shutil.which(n)
        if p:
            return p
    return None


_NICE = ['nice', '-n', '5'] if shutil.which('nice') else []


def _free_port():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(('127.0.0.1', 0))
            return s.getsockname()[1]
    except Exception:
        return 20000 + (os.getpid() % 20000)


def _tool_env():
    env = dict(os.environ)
    env.update({'GOCACHE': str(CACHE_DIR / 'go'), 'GOPATH': str(CACHE_DIR / 'gopath'),
                'npm_config_cache': str(CACHE_DIR / 'npm'), 'PIP_DISABLE_PIP_VERSION_CHECK': '1',
                'PIP_NO_INPUT': '1', 'CI': '1'})
    return env


def _run_tool(cmd, cwd, timeout):
    """Run an installer/compiler. Returns (ok, output_tail)."""
    try:
        r = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout, env=_tool_env())
        out = ((r.stdout or '') + (r.stderr or '')).strip()
        return r.returncode == 0, out[-600:]
    except subprocess.TimeoutExpired:
        return False, f"timed out after {timeout}s"
    except Exception as e:
        return False, str(e)


# ───────────── pip (batched, with fallback for old pip) ─────────────
_pip_failed = set()


def _pip(args, timeout, cwd=None):
    base = [sys.executable, '-m', 'pip', 'install'] + args + ['--quiet']
    for flags in (['--break-system-packages'], []):
        try:
            r = subprocess.run(base + flags, capture_output=True, text=True, timeout=timeout,
                               cwd=str(cwd) if cwd else None, env=_tool_env())
        except Exception as e:
            logger.warning(f"pip error: {e}")
            return False
        if r.returncode == 0:
            return True
        if 'no such option' not in (r.stderr or '').lower():
            return False
    return False


def _install_requirements(user_folder):
    req = Path(user_folder) / 'requirements.txt'
    marker = Path(user_folder) / '.requirements.sha'
    if not req.exists():
        return None
    digest = hashlib.sha256(req.read_bytes()).hexdigest()
    if marker.exists() and marker.read_text().strip() == digest:
        return None
    ok = _pip(['-r', str(req)], 600, cwd=user_folder)
    if ok:
        marker.write_text(digest)
    return ok


# ───────────── Node.js dependencies ─────────────
_NODE_BUILTINS = None
_NODE_STATIC = {'assert', 'async_hooks', 'buffer', 'child_process', 'cluster', 'console', 'constants', 'crypto',
                'dgram', 'diagnostics_channel', 'dns', 'domain', 'events', 'fs', 'http', 'http2', 'https',
                'inspector', 'module', 'net', 'os', 'path', 'perf_hooks', 'process', 'punycode', 'querystring',
                'readline', 'repl', 'stream', 'string_decoder', 'timers', 'tls', 'trace_events', 'tty', 'url',
                'util', 'v8', 'vm', 'wasi', 'worker_threads', 'zlib', 'test', 'sea', 'sqlite'}
_JS_IMPORT_RE = re.compile(
    r'''(?:require\(\s*|import\s+(?:[\w*{}\s,]+\s+from\s+)?|import\(\s*|export\s+[\w*{}\s,]+\s+from\s+)['"]([^'"\n]+)['"]''')


def _node_builtins():
    global _NODE_BUILTINS
    if _NODE_BUILTINS is None:
        _NODE_BUILTINS = set(_NODE_STATIC)
        node = _which('node', 'nodejs')
        if node:
            try:
                r = subprocess.run([node, '-p', "JSON.stringify(require('module').builtinModules)"],
                                   capture_output=True, text=True, timeout=15)
                _NODE_BUILTINS |= set(json.loads(r.stdout.strip()))
            except Exception:
                pass
    return _NODE_BUILTINS


def _js_packages(fp):
    pkgs = set()
    try:
        text = Path(fp).read_text(errors='ignore')[:2_000_000]
    except Exception:
        return pkgs
    builtins = _node_builtins()
    for m in _JS_IMPORT_RE.finditer(text):
        spec = m.group(1).strip()
        if spec.startswith(('.', '/', 'node:', 'file:', 'http:', 'https:', 'data:', '#', '~')):
            continue
        parts = spec.split('/')
        name = '/'.join(parts[:2]) if spec.startswith('@') and len(parts) > 1 else parts[0]
        if name and name not in builtins and re.fullmatch(r'(@[\w.-]+/)?[\w.-]+', name):
            pkgs.add(name)
    return pkgs


def _has_node_module(folder, name):
    p = Path(folder).resolve()
    root = UPLOAD_BOTS_DIR.resolve()
    for d in [p] + list(p.parents):
        if (d / 'node_modules' / name).exists():
            return True
        if d == root or d == root.parent:
            break
    return False


def _prepare_node(fp, notes):
    folder = fp.parent
    npm = _which('npm')
    if not npm:
        return "npm is not installed on this server (use the Dockerfile that ships with this bot)."
    pkg = folder / 'package.json'
    if pkg.exists():
        h = hashlib.sha256(pkg.read_bytes())
        lock = folder / 'package-lock.json'
        if lock.exists():
            h.update(lock.read_bytes())
        digest = h.hexdigest()
        marker = folder / '.package.sha'
        if not (folder / 'node_modules').exists() or not marker.exists() or marker.read_text().strip() != digest:
            ok, out = _run_tool([npm, 'install', '--omit=dev', '--no-audit', '--no-fund', '--loglevel=error'],
                                folder, 900)
            notes.append("npm install " + ("finished" if ok else f"FAILED: {out[-200:]}"))
            if ok:
                marker.write_text(digest)
    if fp.suffix.lower() in ('.js', '.mjs', '.cjs', '.ts'):
        missing = sorted(p for p in _js_packages(fp) if not _has_node_module(folder, p) and p not in _pip_failed)
        if missing:
            ok, out = _run_tool([npm, 'install', '--no-audit', '--no-fund', '--no-save', '--loglevel=error']
                                + missing[:30], folder, 600)
            if ok:
                notes.append("Installed npm packages: " + ', '.join(missing[:30]))
            else:
                _pip_failed.update(missing)
                notes.append(f"npm could not install: {', '.join(missing[:30])}")
    return None


# ───────────── per-language preparation ─────────────
def _missing_runtime(label):
    return (f"{label} is not installed on this server. Deploy with the Dockerfile that comes with this bot "
            f"(it installs the common runtimes), or ask the owner to install {label}.")


def _compile_native(fp, notes):
    """Compile C / C++ / Rust to a binary. Returns (binary_path, err)."""
    ext = fp.suffix.lower()
    out_dir = fp.parent / '.build'
    out_dir.mkdir(exist_ok=True)
    out = out_dir / (fp.stem + '.bin')
    if ext == '.c':
        cc = _which('gcc', 'cc', 'clang')
        cmd = [cc, '-O2', '-o', str(out), str(fp), '-lm'] if cc else None
        label = 'A C compiler (gcc)'
    elif ext in ('.cpp', '.cc'):
        cc = _which('g++', 'c++', 'clang++')
        cmd = [cc, '-O2', '-o', str(out), str(fp)] if cc else None
        label = 'A C++ compiler (g++)'
    else:
        cc = _which('rustc')
        cmd = [cc, '-O', '-o', str(out), str(fp)] if cc else None
        label = 'Rust (rustc)'
    if not cmd:
        return None, _missing_runtime(label)
    ok, log = _run_tool(cmd, fp.parent, 300)
    if not ok:
        return None, f"Compile failed:\n{log[-400:]}"
    notes.append(f"Compiled {fp.name}")
    return out, None


def prepare_run(fp):
    """Blocking. Install dependencies / compile, then return (cmd, notes, error)."""
    fp = Path(fp)
    ext = fp.suffix.lower()
    name = fp.name.lower()
    folder = fp.parent
    notes = []
    try:
        if ext == '.py':
            req = _install_requirements(folder)
            if req is True:
                notes.append("requirements.txt installed")
            elif req is False:
                notes.append("requirements.txt install failed")
            installed, failed = install_missing_dependencies(fp)
            if installed:
                notes.append("Installed: " + ', '.join(installed))
            if failed:
                notes.append("Failed to install: " + ', '.join(failed))
            return _NICE + [sys.executable, '-u', str(fp)], notes, None
        if name == 'package.json' or ext in ('.js', '.mjs', '.cjs', '.ts'):
            node = _which('node', 'nodejs')
            if not node:
                return None, notes, _missing_runtime('Node.js')
            err = _prepare_node(fp, notes)
            if err:
                return None, notes, err
            if name == 'package.json':
                return _NICE + [_which('npm'), 'start'], notes, None
            if ext == '.ts':
                tsx = _which('tsx')
                if tsx:
                    return _NICE + [tsx, str(fp)], notes, None
                return _NICE + [node, '--experimental-strip-types', str(fp)], notes, None
            return _NICE + [node, str(fp)], notes, None
        if ext in ('.sh', '.bash'):
            sh = _which('bash', 'sh')
            return (_NICE + [sh, str(fp)], notes, None) if sh else (None, notes, _missing_runtime('Bash'))
        if ext == '.php':
            php = _which('php')
            if not php:
                return None, notes, _missing_runtime('PHP')
            if (folder / 'composer.json').exists() and not (folder / 'vendor').exists() and _which('composer'):
                ok, out = _run_tool([_which('composer'), 'install', '--no-dev', '--no-interaction'], folder, 600)
                notes.append("composer install " + ("finished" if ok else "FAILED"))
            return _NICE + [php, str(fp)], notes, None
        if ext == '.rb':
            ruby = _which('ruby')
            if not ruby:
                return None, notes, _missing_runtime('Ruby')
            if (folder / 'Gemfile').exists() and _which('bundle') and not (folder / '.bundle.done').exists():
                ok, _out = _run_tool([_which('bundle'), 'install'], folder, 600)
                notes.append("bundle install " + ("finished" if ok else "FAILED"))
                if ok:
                    (folder / '.bundle.done').write_text('1')
            return _NICE + [ruby, str(fp)], notes, None
        if ext == '.go':
            go = _which('go')
            if not go:
                return None, notes, _missing_runtime('Go')
            if (folder / 'go.mod').exists():
                if not (folder / '.gomod.done').exists():
                    ok, _out = _run_tool([go, 'mod', 'tidy'], folder, 600)
                    notes.append("go mod tidy " + ("finished" if ok else "FAILED"))
                    if ok:
                        (folder / '.gomod.done').write_text('1')
                return _NICE + [go, 'run', '.'], notes, None
            return _NICE + [go, 'run', str(fp)], notes, None
        if ext == '.java':
            java = _which('java')
            return (_NICE + [java, str(fp)], notes, None) if java else (None, notes, _missing_runtime('Java (JDK)'))
        if ext == '.jar':
            java = _which('java')
            return (_NICE + [java, '-jar', str(fp)], notes, None) if java else (None, notes, _missing_runtime('Java'))
        if ext == '.pl':
            perl = _which('perl')
            return (_NICE + [perl, str(fp)], notes, None) if perl else (None, notes, _missing_runtime('Perl'))
        if ext == '.lua':
            lua = _which('lua', 'lua5.4', 'lua5.3', 'luajit')
            return (_NICE + [lua, str(fp)], notes, None) if lua else (None, notes, _missing_runtime('Lua'))
        if ext in ('.c', '.cpp', '.cc', '.rs'):
            binary, err = _compile_native(fp, notes)
            if err:
                return None, notes, err
            return _NICE + [str(binary)], notes, None
    except Exception as e:
        logger.error(f"prepare_run error: {e}", exc_info=True)
        return None, notes, f"Could not prepare the bot: {e}"
    return None, notes, "This file type cannot be run. Supported: " + supported_text()


# ───────────── self-host guard & token clash ─────────────
_TOKEN_RE = re.compile(r'(?<![0-9])\d{8,10}:[A-Za-z0-9_-]{35}(?![A-Za-z0-9_-])')
HOST_MARKERS = ('kalpro_nested_data', 'def _self_run_reason', 'def kill_duplicate_instances',
                'class AccessGuardMiddleware', 'KALPRO Hosting Bot', 'def reap_orphans')
_GENERIC_STEMS = {'main', 'bot', 'app', 'run', 'index', 'start', 'server'}
_SCAN_EXTS = {'.py', '.js', '.mjs', '.cjs', '.ts', '.json', '.env', '.txt', '.cfg', '.ini', '.yml', '.yaml',
              '.toml', '.sh', '.bash', '.php', '.rb', '.go', '.java', '.pl', '.lua', '.conf', '.properties'}
_SKIP_DIRS = {'node_modules', '.git', '__pycache__', 'venv', '.venv', '.build', 'vendor', 'site-packages'}
_token_owner = {}          # telegram token -> bot key of the hosted bot that is using it


def _read_text(p, limit=1_500_000):
    try:
        with open(p, 'rb') as fh:
            return fh.read(limit).decode('utf-8', errors='ignore')
    except Exception:
        return ''


def _is_user_root(folder):
    try:
        return Path(folder).resolve().parent == UPLOAD_BOTS_DIR.resolve()
    except Exception:
        return False


def _env_files(folder):
    try:
        return [p for p in Path(folder).iterdir() if p.is_file() and (p.name == '.env' or p.name.startswith('.env.'))]
    except Exception:
        return []


def _project_files(fp, max_files=120):
    """Files whose content could hold a secret for this bot. Flat uploads only look at themselves and .env."""
    fp = Path(fp)
    folder = fp.parent
    files = [fp] + _env_files(folder)
    if _is_user_root(folder):
        return files
    base_depth = len(folder.parts)
    for dirpath, dirs, names in os.walk(folder):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        if len(Path(dirpath).parts) - base_depth >= 3:
            dirs[:] = []
        for n in names:
            p = Path(dirpath) / n
            if p != fp and (p.suffix.lower() in _SCAN_EXTS or n.startswith('.env')):
                files.append(p)
                if len(files) >= max_files:
                    return files
    return files


def _tokens_in(fp):
    """Telegram bot tokens this bot will use (its own file + .env next to it)."""
    found = set()
    for p in [Path(fp)] + _env_files(Path(fp).parent):
        found.update(_TOKEN_RE.findall(_read_text(p)))
    return found


def _self_run_reason(file_path, deep=False):
    """Return a reason string if running this file would run the host itself or reuse its token."""
    try:
        data = Path(file_path).read_bytes()
    except Exception:
        return None
    if HOST_HASH and hashlib.sha256(data).hexdigest() == HOST_HASH:
        return "this file is a copy of the host itself"
    text = data.decode('utf-8', errors='ignore')
    if sum(1 for m in HOST_MARKERS if m in text) >= 3:
        return "this file is a copy or clone of the hosting bot itself"
    stem = HOST_FILE.stem
    if stem not in _GENERIC_STEMS and len(stem) >= 5 and re.search(
            rf'^\s*(?:import|from)\s+{re.escape(stem)}\b', text, re.MULTILINE):
        return "this file imports the hosting bot"
    secrets = [t for t in (TOKEN, _PARENT_TOKEN) if t]
    for p in (_project_files(file_path) if deep else [Path(file_path)]):
        body = text if Path(p) == Path(file_path) else _read_text(p)
        for t in secrets:
            if t in body:
                who = "the host bot's own token" if t == TOKEN else "the parent bot's token"
                where = "" if Path(p) == Path(file_path) else f" ({Path(p).name})"
                return f"this project contains {who}{where}"
    return None


def _token_clash(key, tokens):
    for t in tokens:
        owner = _token_owner.get(t)
        if owner and owner != key and _alive(owner):
            return owner
    return None


# ───────────── fast archive extraction ─────────────
_JUNK_PARTS = {'__MACOSX', '.DS_Store', 'Thumbs.db', '__pycache__', '.git'}
MAX_ARCHIVE_MEMBERS = 30000
_ENTRY_STEMS = ('main', 'bot', 'app', 'index', 'server', 'run', 'start', '__main__', 'manage')


def _norm_member(name):
    parts = [p for p in (name or '').replace('\\', '/').split('/') if p not in ('', '.')]
    if not parts or any(p == '..' for p in parts) or re.match(r'^[A-Za-z]:$', parts[0]):
        return None
    if any(p in _JUNK_PARTS for p in parts):
        return None
    rel = '/'.join(parts)
    if len(rel) > 240 or any(len(p) > 120 or '\x00' in p for p in parts):
        return None
    return rel


def _strip_wrapper(entries):
    """If everything sits in ONE top-level folder, drop that folder (GitHub-style archives)."""
    if not entries:
        return entries, False
    tops = {e[0].split('/', 1)[0] for e in entries}
    if len(tops) == 1 and all('/' in e[0] for e in entries):
        return [(e[0].split('/', 1)[1],) + tuple(e[1:]) for e in entries], True
    return entries, False


def _balance(entries, n):
    buckets = [[] for _ in range(n)]
    heap = [(0, i) for i in range(n)]
    for e in sorted(entries, key=lambda x: -x[1]):
        load, i = heapq.heappop(heap)
        buckets[i].append(e)
        heapq.heappush(heap, (load + max(e[1], 8192), i))
    return [b for b in buckets if b]


def _zip_worker(archive, dest, items):
    done, failed = 0, []
    with zipfile.ZipFile(archive) as zf:
        for rel, _size, is_exec, zi in items:
            target = os.path.join(dest, *rel.split('/'))
            try:
                with zf.open(zi) as src, open(target, 'wb') as out:
                    shutil.copyfileobj(src, out, 1024 * 1024)
                if is_exec:
                    os.chmod(target, 0o755)
                done += 1
            except Exception:
                failed.append(rel)
    return done, failed


def _prepare_dirs(dest, entries):
    dest_real = os.path.realpath(dest)
    seen = set()
    for rel, *_ in entries:
        d = os.path.dirname(os.path.join(dest, *rel.split('/')))
        if d in seen:
            continue
        os.makedirs(d, exist_ok=True)
        real = os.path.realpath(d)
        if real != dest_real and not real.startswith(dest_real + os.sep):
            raise ValueError("Unsafe path in archive")
        seen.add(d)


def extract_archive(archive, dest, cap_bytes, strip_wrapper=True):
    """Extract a ZIP or TAR archive safely and fast. Returns {'files': [...], 'bytes': n, 'seconds': s, ...}."""
    archive, dest = Path(archive), Path(dest)
    t0 = time.time()
    dest.mkdir(parents=True, exist_ok=True)
    total, skipped, entries = 0, 0, []
    if zipfile.is_zipfile(archive):
        kind = 'zip'
        with zipfile.ZipFile(archive) as zf:
            infos = zf.infolist()
        if len(infos) > MAX_ARCHIVE_MEMBERS:
            raise ValueError(f"Archive has too many files ({len(infos)}). Limit is {MAX_ARCHIVE_MEMBERS}.")
        for zi in infos:
            if zi.is_dir():
                continue
            rel = _norm_member(zi.filename)
            mode = (zi.external_attr >> 16) & 0o170000
            if rel is None or mode == 0o120000:      # junk, unsafe path or symlink
                skipped += 1
                continue
            if zi.flag_bits & 0x1:
                raise ValueError("This ZIP is password-protected. Remove the password and upload it again.")
            total += zi.file_size
            if total > cap_bytes:
                raise ValueError(f"Archive is too large to extract ({cap_bytes // (1024 * 1024)} MB of space available).")
            entries.append((rel, zi.file_size, bool((zi.external_attr >> 16) & 0o111), zi))
    elif tarfile.is_tarfile(archive):
        kind = 'tar'
        with tarfile.open(archive) as tf:
            members = tf.getmembers()
        if len(members) > MAX_ARCHIVE_MEMBERS:
            raise ValueError(f"Archive has too many files ({len(members)}). Limit is {MAX_ARCHIVE_MEMBERS}.")
        for m in members:
            if not m.isreg():
                continue
            rel = _norm_member(m.name)
            if rel is None:
                skipped += 1
                continue
            total += m.size
            if total > cap_bytes:
                raise ValueError(f"Archive is too large to extract ({cap_bytes // (1024 * 1024)} MB of space available).")
            entries.append((rel, m.size, bool(m.mode & 0o111), m))
    else:
        raise ValueError("Not a valid ZIP / TAR archive.")
    stripped = False
    if strip_wrapper:
        entries, stripped = _strip_wrapper(entries)
    _prepare_dirs(dest, entries)
    failed = []
    if kind == 'zip':
        cpus = os.cpu_count() or 1
        workers = 1 if cpus < 2 else min(8, cpus * 2)      # threads only help when there are spare cores
        if len(entries) < 24 or total < 4 * 1024 * 1024:
            workers = 1
        buckets = _balance(entries, workers) if entries else []
        if len(buckets) <= 1:
            done = 0
            for b in buckets:
                d, f = _zip_worker(archive, str(dest), b)
                done += d
                failed += f
        else:
            with ThreadPoolExecutor(max_workers=len(buckets)) as ex:
                for d, f in ex.map(lambda b: _zip_worker(archive, str(dest), b), buckets):
                    failed += f
    else:
        with tarfile.open(archive) as tf:
            for rel, _size, is_exec, m in entries:
                target = os.path.join(str(dest), *rel.split('/'))
                try:
                    src = tf.extractfile(m)
                    if src is None:
                        continue
                    with src, open(target, 'wb') as out:
                        shutil.copyfileobj(src, out, 1024 * 1024)
                    if is_exec:
                        os.chmod(target, 0o755)
                except Exception:
                    failed.append(rel)
    files = [e[0] for e in entries if e[0] not in set(failed)]
    return {'files': files, 'bytes': total, 'seconds': time.time() - t0, 'skipped': skipped + len(failed),
            'stripped': stripped, 'kind': kind}


def unique_dirname(user_folder, archive_name):
    base = archive_name
    low = base.lower()
    for suf in sorted(ARCHIVE_SUFFIXES, key=len, reverse=True):
        if low.endswith(suf):
            base = base[:-len(suf)]
            break
    base = re.sub(r'[^A-Za-z0-9._-]+', '_', base).strip('._')[:28] or 'project'
    name, n = base, 1
    while (Path(user_folder) / name).exists():
        n += 1
        name = f"{base}_{n}"
    return name


_MAIN_GUARD_RE = re.compile(r"__name__\s*==\s*['\"]__main__['\"]")


def _has_main_guard(path):
    p = Path(path)
    if p.suffix.lower() != '.py':
        return False
    return bool(_MAIN_GUARD_RE.search(_read_text(p, 400_000)))


def pick_entries(dest, rels, limit=30):
    """Choose which extracted files become runnable bots (not every helper module)."""
    dest = Path(dest)
    rels = list(rels)
    runnable = [r for r in rels if is_runnable(r) and os.path.basename(r).lower() != 'package.json']
    entries = []
    has_start = False
    if 'package.json' in rels:
        try:
            data = json.loads((dest / 'package.json').read_text(errors='ignore'))
            has_start = bool((data.get('scripts') or {}).get('start'))
        except Exception:
            pass
    if has_start:
        entries.append('package.json')
    root = [r for r in runnable if '/' not in r]
    if has_start:
        root = [r for r in root if Path(r).suffix.lower() not in ('.js', '.mjs', '.cjs', '.ts')]
    likely = [r for r in root if Path(r).stem.lower() in _ENTRY_STEMS or _has_main_guard(dest / r)]
    entries += sorted(likely or root, key=str.lower)      # skip helper modules when real entry points exist
    if not entries:
        deeper = [r for r in runnable if r.count('/') <= 2 and Path(r).stem.lower() in _ENTRY_STEMS]
        entries += sorted(deeper, key=lambda r: (r.count('/'), r.lower()))
    if not entries:
        entries += sorted(runnable, key=lambda r: (r.count('/'), r.lower()))[:10]
    return entries[:limit]


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
    return sorted([f for f, t in user_files.get(uid, []) if is_runnable(f)], key=str.lower)


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


async def start_bot(uid, fname, auto=False, apply_update=False):
    lock = _start_locks.setdefault(bkey(uid, fname), asyncio.Lock())
    async with lock:
        return await _start_bot_locked(uid, fname, auto, apply_update)


async def _start_bot_locked(uid, fname, auto, apply_update=False):
    user_folder = UPLOAD_BOTS_DIR / str(uid)
    fp = user_folder / fname
    if not fp.is_file():
        return False, "File not found."
    if not is_runnable(fname):
        return False, "This file type cannot be run. Supported: " + supported_text() + "."
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
    reason = await asyncio.to_thread(_self_run_reason, fp, True)
    if reason:
        return False, f"Blocked: {reason}. A hosted script must be a normal bot with its own bot token."
    tokens = await asyncio.to_thread(_tokens_in, fp)
    clash = _token_clash(key, tokens)
    if clash:
        owner_uid, _sep, other = clash.partition('_')
        who = f"your bot {other}" if owner_uid == str(uid) else "another hosted bot"
        return False, (f"Blocked: this bot uses a Telegram token that {who} is already running with. "
                       "Two programs cannot poll one token. Stop the other one first, or give this bot its own token.")
    work_dir = fp.parent
    _shield_env_file(work_dir)
    log_file = _open_log(uid, fname)
    try:
        cmd, notes, err = await asyncio.to_thread(prepare_run, fp)
        for n in notes:
            log_file.write(f"[AUTO-INSTALLER] {n}\n")
        log_file.flush()
        if err:
            log_file.write(f"[HOST] {err}\n")
            log_file.close()
            return False, err
        proc = subprocess.Popen(cmd, cwd=str(work_dir), stdin=subprocess.DEVNULL, stdout=log_file,
                                stderr=subprocess.STDOUT, env=_child_env(), start_new_session=True)
    except FileNotFoundError as e:
        log_file.close()
        return False, f"Runtime not found: {e.filename or 'interpreter'}. Use the Dockerfile that ships with this bot."
    except Exception as e:
        log_file.close()
        logger.error(f"start_bot error: {e}")
        return False, f"Could not start: {e}"
    for t in tokens:
        _token_owner[t] = key
    kind = 'npm' if fp.name.lower() == 'package.json' else fp.suffix.lower()[1:]
    bot_scripts[key] = {'process': proc, 'file_name': fname, 'script_owner_id': uid,
                        'start_time': datetime.now(), 'user_folder': str(work_dir),
                        'type': kind, 'log_file': log_file}
    set_bot_status(uid, fname, 'running', started_at=now_utc().isoformat(), pid=proc.pid, resume_on_renew=0)
    try:
        dbx('UPDATE bot_stats SET stat_value = stat_value + 1 WHERE stat_name = ?', ('total_runs',))
        bot_stats['total_runs'] = bot_stats.get('total_runs', 0) + 1
    except Exception:
        pass
    return True, f"Started with the new version (PID {proc.pid})." if applied else f"Started (PID {proc.pid})."


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
    kind = file_kind(fname)
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
async def _scan_conflicts():
    """Warn the owner of a hosted bot when Telegram says another program polls the same token."""
    for key, info in list(bot_scripts.items()):
        try:
            if info.get('conflict_notified') or info['process'].poll() is not None:
                continue
            tail = read_log_tail(info['script_owner_id'], info['file_name'], limit=1500) or ''
            if 'terminated by other getupdates request' in tail.lower():
                info['conflict_notified'] = True
                tok = cb_name(info['file_name'])
                await notify_user(info['script_owner_id'],
                                  f"<b>{esc(info['file_name'])}</b>: Telegram says another program is using this bot's "
                                  "token at the same time (409 Conflict). Stop the other copy, or give this bot its own token.",
                                  InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
                                      text="Open Bot", callback_data=f"bot:{tok}", style="primary")]]))
        except Exception as e:
            logger.debug(f"conflict scan error: {e}")


async def bot_watchdog():
    """Cleans up exited bots and auto-restarts crashed ones."""
    tick = 0
    while True:
        try:
            await asyncio.sleep(5)
            tick += 1
            if tick % 6 == 0:
                await _scan_conflicts()
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
    try:
        protected = {os.getpid()} | {q.pid for q in psutil.Process().parents()}
    except Exception:
        protected = {os.getpid()}
    for (uid, fname), row in list(bot_rows.items()):
        pid = row.get('pid')
        if not pid or pid in protected:
            continue
        try:
            p = psutil.Process(pid)
            cmd = ' '.join(p.cmdline())
            same_dir = True
            try:
                same_dir = os.path.realpath(p.cwd()) == os.path.realpath(str(UPLOAD_BOTS_DIR / str(uid)))
            except Exception:
                pass
            if fname in cmd and same_dir:
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
    for key, info i
