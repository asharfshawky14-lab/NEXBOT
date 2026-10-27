import os
import hashlib
import asyncio
import json
import uuid
import glob
import re
import html
import subprocess
import time
import hashlib
import shutil
import tempfile
from urllib.parse import urlparse
from contextvars import ContextVar

import yt_dlp
from curl_cffi import requests

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    InputFile,
    InputMediaPhoto,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
)

from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
    CallbackQueryHandler,
    PreCheckoutQueryHandler,
)


# ============================================================
# إعدادات البوت
# ============================================================

try:
    with open("token.txt", "r", encoding="utf-8") as f:
        TOKEN = f.read().strip()
except Exception:
    raise RuntimeError("❌ ملف token.txt غير موجود أو لا يمكن قراءته.")

if not TOKEN:
    raise RuntimeError("❌ ملف token.txt فارغ.")


# ============================================================
# بيانات الأدمن
# ============================================================

ADMIN_ID = 2018495286


# ============================================================
# الملفات والمجلدات
# ============================================================

DOWNLOAD_DIR = "downloads"


def format_download_progress(label, percent, downloaded, total, speed=None, eta=None):
    """تنسيق شريط التقدم من بيانات التحميل الفعلية."""
    try:
        pct = max(0, min(100, int(percent)))
    except Exception:
        pct = 0
    width = 14
    filled = int(width * pct / 100)
    bar = "█" * filled + "░" * (width - filled)

    def fmt_size(value):
        if not value:
            return "—"
        value = float(value)
        units = ("B", "KB", "MB", "GB")
        i = 0
        while value >= 1024 and i < len(units) - 1:
            value /= 1024
            i += 1
        return f"{value:.1f} {units[i]}"

    def fmt_speed(value):
        if not value:
            return "—"
        return fmt_size(value) + "/ث"

    if eta is not None:
        try:
            eta_text = f"{int(eta)} ث"
        except Exception:
            eta_text = "—"
    else:
        eta_text = "—"

    total_text = fmt_size(total)
    downloaded_text = fmt_size(downloaded)
    return (
        f"⏳ {label}\n"
        f"{bar} {pct}%\n"
        f"📦 {downloaded_text} / {total_text}\n"
        f"⚡ {fmt_speed(speed)}\n"
        f"⏱️ متبقي: {eta_text}"
    )


async def monitor_download_progress(job_id, msg, state):
    """مراقب حي للتقدم؛ يعمل كطبقة عرض فقط ولا يغيّر إعدادات التحميل."""
    last_text = None
    last_edit = 0.0
    frames = ["█░░░░░░░░░░░░", "███░░░░░░░░░", "██████░░░░░░", "█████████░░░", "████████████"]
    while not state.get("done"):
        try:
            now = time.monotonic()
            total = state.get("total")
            downloaded = state.get("downloaded", 0) or 0
            percent = state.get("percent")
            if downloaded <= 0:
                try:
                    files = glob.glob(os.path.join(DOWNLOAD_DIR, f"{job_id}*"))
                    downloaded = sum(os.path.getsize(x) for x in files if os.path.isfile(x))
                except Exception:
                    downloaded = 0
            if percent is not None:
                percent = max(0, min(100, int(percent)))
                text_value = format_download_progress(state.get("label", "جاري التحميل..."), percent, downloaded, total, state.get("speed"), state.get("eta"))
            else:
                frame = frames[int(now * 2) % len(frames)]
                extra = f"\n📦 تم استلام: {downloaded / 1024 / 1024:.1f} MB" if downloaded else ""
                text_value = f"⏳ {state.get('label', 'جاري التحميل...')}\n{frame}{extra}"
            if text_value != last_text and now - last_edit >= 0.8:
                last_text = text_value
                last_edit = now
                try:
                    await msg.edit_text(text_value)
                except Exception as e:
                    print("⚠️ Progress monitor edit error:", type(e).__name__, e)
        except Exception as e:
            print("⚠️ Progress monitor error:", type(e).__name__, e)
        await asyncio.sleep(0.4)

# ============================================================
# حماية البوت من الضغط والتحميلات المتزامنة
# ============================================================
MAX_CONCURRENT_DOWNLOADS = 3
OVERLOAD_MESSAGE = (
    "🚀 الصاروخ مليان طلبات دلوقتي!\n"
    "⏳ سيب له ثواني وجرّب تاني يا بطل 🔥"
)
ORPHAN_FILE_MAX_AGE = 24 * 60 * 60

_download_semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)
_download_users_lock = asyncio.Lock()
_active_download_users = set()
_active_job_ids = set()
_download_guard_active = ContextVar(
    "download_guard_active",
    default=False
)

DATA_FILE = "users.json"
STATS_FILE = "stats.json"
MAINTENANCE_FILE = "maintenance.json"
ADS_FILE = "ads.json"
PREMIUM_FILE = "premium.json"
ADMINS_FILE = "admins.json"
ADMIN_PERMISSIONS_FILE = "admin_permissions.json"
OWNERS_FILE = "owners.json"
CONTACT_MESSAGES_FILE = "contact_messages.json"
CACHE_DIR = "cache"
CACHE_MAX_AGE = 24 * 60 * 60
RATE_LIMIT_SECONDS = 2.0
ERROR_ALERT_THRESHOLD = 3
ADMIN_ALERT_COOLDOWN = 60

os.makedirs(DOWNLOAD_DIR, exist_ok=True)
os.makedirs(CACHE_DIR, exist_ok=True)

_last_request_at = {}
_user_error_streak = {}
_last_admin_alert = {}
_cancel_jobs = set()
_download_queue_waiters = 0


# ============================================================
# امتدادات الملفات
# ============================================================

VIDEO_EXTENSIONS = (
    ".mp4",
    ".mkv",
    ".webm",
    ".mov",
    ".avi",
    ".flv",
    ".m4v",
)

IMAGE_EXTENSIONS = (
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
)


# ============================================================
# بيانات المستخدمين
# ============================================================

def load_users():

    if not os.path.exists(DATA_FILE):
        return {}

    try:

        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        if isinstance(data, dict):
            return data

        return {}

    except Exception as e:

        print(
            "⚠️ Users load error:",
            type(e).__name__,
            ":",
            e
        )

        return {}


def save_users(users_data):

    try:

        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(
                users_data,
                f,
                ensure_ascii=False,
                indent=2
            )

    except Exception as e:

        print(
            "⚠️ Users save error:",
            type(e).__name__,
            ":",
            e
        )


def load_stats():

    default = {
        "downloads": 0,
        "audio_downloads": 0,
        "videos_downloaded": 0,
        "errors": 0,
        "daily_date": "",
        "downloads_today": 0,
        "new_users_today": 0,
        "site_counts": {},
        "type_counts": {},
    }

    if not os.path.exists(STATS_FILE):
        return default.copy()

    try:

        with open(STATS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        if not isinstance(data, dict):
            return default.copy()

        for key in default:

            if key not in data:
                data[key] = 0

        return data

    except Exception as e:

        print(
            "⚠️ Stats load error:",
            type(e).__name__,
            ":",
            e
        )

        return default.copy()


def save_stats(stats_data):

    try:

        with open(STATS_FILE, "w", encoding="utf-8") as f:
            json.dump(
                stats_data,
                f,
                ensure_ascii=False,
                indent=2
            )

    except Exception as e:

        print(
            "⚠️ Stats save error:",
            type(e).__name__,
            ":",
            e
        )


# ============================================================
# إحصائيات متقدمة + كاش + حماية من السبام + تنبيهات الأدمن
# ============================================================

def _today_key():
    return time.strftime("%Y-%m-%d", time.localtime())

def ensure_daily_stats():
    today = _today_key()
    if stats.get("daily_date") != today:
        stats["daily_date"] = today
        stats["downloads_today"] = 0
        stats["new_users_today"] = 0
        stats["site_counts"] = {}
        stats["type_counts"] = {}
        save_stats(stats)

def record_new_user_today():
    ensure_daily_stats()
    stats["new_users_today"] = int(stats.get("new_users_today", 0)) + 1
    save_stats(stats)

def record_success_stats(site, kind):
    ensure_daily_stats()
    stats["downloads_today"] = int(stats.get("downloads_today", 0)) + 1
    stats.setdefault("site_counts", {})[site] = int(stats.get("site_counts", {}).get(site, 0)) + 1
    stats.setdefault("type_counts", {})[kind] = int(stats.get("type_counts", {}).get(kind, 0)) + 1
    save_stats(stats)

def cache_key(url, kind):
    return hashlib.sha256(f"{kind}|{url.strip()}".encode("utf-8", "ignore")).hexdigest()

def cache_paths(url, kind, ext):
    key=cache_key(url,kind)
    return os.path.join(CACHE_DIR,f"{key}.{ext}"), os.path.join(CACHE_DIR,f"{key}.json")

def cleanup_cache(max_age=CACHE_MAX_AGE):
    now=time.time(); removed=0
    for path in glob.glob(os.path.join(CACHE_DIR,"*")):
        try:
            if os.path.isfile(path) and now-os.path.getmtime(path)>max_age:
                os.remove(path); removed+=1
        except Exception as e: print("⚠️ Cache cleanup error:",e)
    if removed: print(f"🧹 تم تنظيف {removed} ملف من الكاش.")
    return removed

def save_cached_file(source_path,url,kind,ext,title=""):
    if not source_path or not os.path.isfile(source_path): return None
    cache_file,meta_file=cache_paths(url,kind,ext)
    try:
        shutil.copy2(source_path,cache_file)
        with open(meta_file,"w",encoding="utf-8") as f:
            json.dump({"title":str(title)[:150]},f,ensure_ascii=False)
        return cache_file
    except Exception as e:
        print("⚠️ Cache save error:",type(e).__name__,":",e); return None

def get_cached_file(url,kind):
    key=cache_key(url,kind); meta_path=os.path.join(CACHE_DIR,f"{key}.json")
    for path in glob.glob(os.path.join(CACHE_DIR,f"{key}.*")):
        if path.endswith(".json") or not os.path.isfile(path): continue
        try:
            if time.time()-os.path.getmtime(path)>CACHE_MAX_AGE:
                os.remove(path); continue
        except Exception: continue
        title=""
        try:
            with open(meta_path,"r",encoding="utf-8") as f: title=json.load(f).get("title","")
        except Exception: pass
        return path,title
    return None,""

async def notify_admin(bot,message,key="general",cooldown=ADMIN_ALERT_COOLDOWN):
    now=time.time()
    if now-_last_admin_alert.get(key,0)<cooldown: return
    try:
        await bot.send_message(ADMIN_ID,message); _last_admin_alert[key]=now
    except Exception as e: print("⚠️ Admin alert error:",e)

def validate_url(url):
    try:
        parsed=urlparse(url.strip())
        return parsed.scheme in ("http","https") and bool(parsed.netloc) and not any(c in url for c in "\n\r\t")
    except Exception: return False

def is_rate_limited(user_id):
    now=time.monotonic(); last=_last_request_at.get(user_id,0)
    if now-last<RATE_LIMIT_SECONDS:
        return True,max(1,int(RATE_LIMIT_SECONDS-(now-last)+0.999))
    _last_request_at[user_id]=now; return False,0

def record_error_streak(user_id):
    _user_error_streak[user_id]=int(_user_error_streak.get(user_id,0))+1
    return _user_error_streak[user_id]

def clear_error_streak(user_id): _user_error_streak.pop(user_id,None)

# ============================================================
# حماية البوت من الضغط
# ============================================================

def active_download_count():
    return len(_active_download_users)


def cleanup_orphan_files(max_age=ORPHAN_FILE_MAX_AGE):

    now = time.time()
    removed = 0

    if not os.path.isdir(DOWNLOAD_DIR):
        return 0

    for root, dirs, files in os.walk(DOWNLOAD_DIR, topdown=False):

        for name in files:
            path = os.path.join(root, name)

            try:
                if now - os.path.getmtime(path) > max_age:
                    os.remove(path)
                    removed += 1
                    print(f"🧹 تنظيف ملف قديم: {path}")
            except Exception as e:
                print(f"⚠️ تعذر تنظيف {path}: {e}")

        for name in dirs:
            path = os.path.join(root, name)

            try:
                if os.path.basename(path).endswith("_photos"):
                    if not os.listdir(path):
                        os.rmdir(path)
            except Exception:
                pass

    if removed:
        print(f"🧹 تم تنظيف {removed} ملف قديم من مجلد downloads.")

    return removed


def download_guard(func):

    async def wrapper(update, context, *args, **kwargs):

        # لو دالة تحميل استدعت دالة تحميل أخرى ضمن نفس العملية،
        # لا نحجز مكانًا ثانيًا ولا نرفض العملية.
        if _download_guard_active.get():
            return await func(update, context, *args, **kwargs)

        user = update.effective_user
        user_id = user.id if user else None

        if user_id is None:
            return await func(update, context, *args, **kwargs)

        async with _download_users_lock:
            if user_id in _active_download_users:
                try:
                    await update.message.reply_text("⏳ عندك تحميل شغال بالفعل. استخدم ⛔ إلغاء التحميل لو عايز توقفه.")
                except Exception:
                    pass
                return
            allowed, reason = consume_download_slot(user_id)
            if not allowed:
                try:
                    await update.message.reply_text(reason)
                except Exception:
                    pass
                return
            _active_download_users.add(user_id)

        acquired = False
        token = None
        queue_message = None

        try:
            if active_download_count() >= MAX_CONCURRENT_DOWNLOADS:
                try:
                    queue_message = await update.message.reply_text(
                        f"🕐 طلبك اتحط في طابور التحميل.\n📍 التحميلات النشطة: {active_download_count()}/{MAX_CONCURRENT_DOWNLOADS}\n⏳ استنى دورك..."
                    )
                except Exception:
                    queue_message = None
            await _download_semaphore.acquire()
            acquired = True
            if queue_message:
                try:
                    await queue_message.edit_text("🚀 جه دورك! جاري بدء التحميل...")
                except Exception:
                    pass
            token = _download_guard_active.set(True)
            return await func(update, context, *args, **kwargs)

        finally:
            if token is not None:
                _download_guard_active.reset(token)

            if acquired:
                _download_semaphore.release()

            async with _download_users_lock:
                _active_download_users.discard(user_id)

    return wrapper


# ============================================================
# نظام الإعلانات
# ============================================================

def load_ads():

    default = {
        "enabled": False,
        "text": "📢 إعلان صاروخ التحميل\n\nللتواصل والإعلان هنا، راسل الإدارة.",
        "frequency": 3,
        "impressions": 0,
    }

    if not os.path.exists(ADS_FILE):
        return default.copy()

    try:
        with open(ADS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        if not isinstance(data, dict):
            return default.copy()

        for key, value in default.items():
            if key not in data:
                data[key] = value

        try:
            data["frequency"] = max(1, int(data.get("frequency", 3)))
        except Exception:
            data["frequency"] = 3

        try:
            data["impressions"] = max(0, int(data.get("impressions", 0)))
        except Exception:
            data["impressions"] = 0

        data["enabled"] = bool(data.get("enabled", False))
        data["text"] = str(data.get("text", default["text"]))[:1000]
        return data

    except Exception as e:
        print("⚠️ Ads load error:", type(e).__name__, ":", e)
        return default.copy()


def save_ads(data):

    try:
        with open(ADS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("⚠️ Ads save error:", type(e).__name__, ":", e)


ads_data = load_ads()


async def maybe_send_ad(update, context):

    user = update.effective_user

    if not user or is_admin(user.id):
        return

    if not ads_data.get("enabled", False):
        return

    user_data = users.get(str(user.id), {})

    try:
        frequency = max(1, int(ads_data.get("frequency", 3)))
    except Exception:
        frequency = 3

    downloads_count = int(user_data.get("downloads", 0) or 0)

    if downloads_count <= 0 or downloads_count % frequency != 0:
        return

    ad_text = str(ads_data.get("text", "")).strip()

    if not ad_text:
        return

    try:
        await update.message.reply_text(ad_text)
        ads_data["impressions"] = int(ads_data.get("impressions", 0) or 0) + 1
        save_ads(ads_data)
        print(f"📢 إعلان تم إرساله للمستخدم {user.id}")
    except Exception as e:
        print(f"⚠️ Ads send error {user.id}: {e}")


# ============================================================
# الصيانة
# ============================================================

def load_maintenance():

    default = {
        "maintenance_mode": False,
        "update_pending": False,
    }

    if not os.path.exists(MAINTENANCE_FILE):
        return default.copy()

    try:

        with open(MAINTENANCE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        if not isinstance(data, dict):
            return default.copy()

        for key in default:

            if key not in data:
                data[key] = default[key]

        return data

    except Exception as e:

        print(
            "⚠️ Maintenance load error:",
            type(e).__name__,
            ":",
            e
        )

        return default.copy()


def save_maintenance(data):

    try:

        with open(MAINTENANCE_FILE, "w", encoding="utf-8") as f:
            json.dump(
                data,
                f,
                ensure_ascii=False,
                indent=2
            )

    except Exception as e:

        print(
            "⚠️ Maintenance save error:",
            type(e).__name__,
            ":",
            e
        )



def load_premium_config():
    default = {
        "enabled": False,
        "normal_limit": 10,
        "normal_window_minutes": 30,
        "premium_limit": 30,
        "premium_window_minutes": 30,
        "premium_plan_limits": {
            "week": {"limit": 50, "window_minutes": 60},
            "month": {"limit": 100, "window_minutes": 60},
            "year": {"limit": 200, "window_minutes": 60},
        },
        "premium_custom_limits": {},
        "prices": {"week": 30, "month": 80, "year": 700},
        "payment_method": "لم يتم تحديد طريقة الدفع بعد.",
        "payment_details": "يرجى ضبط بيانات الدفع من لوحة الأدمن.",
        "payment_requests": {},
        "subscriptions": {},
        "stars_orders": {},
        "stars_payments": {},
    }
    if not os.path.exists(PREMIUM_FILE):
        return default.copy()
    try:
        with open(PREMIUM_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return default.copy()
        for k, v in default.items():
            if k not in data:
                data[k] = v
        if not isinstance(data.get("prices"), dict):
            data["prices"] = default["prices"].copy()

        # توحيد مفاتيح الأسعار مهما كان شكلها القديم
        # (week/month/year أو عربي) حتى لا تظهر للمستخدم رسالة
        # "الباقة غير متاحة" بسبب اختلاف اسم المفتاح.
        raw_prices = data.get("prices", {})
        normalized_prices = {}
        aliases = {
            "week": "week", "أسبوع": "week", "اسبوع": "week",
            "month": "month", "شهر": "month",
            "year": "year", "سنة": "year", "سنه": "year",
        }
        for key, value in raw_prices.items():
            target = aliases.get(str(key).strip().lower())
            if target is None:
                continue
            try:
                number = float(value)
                if number > 0:
                    normalized_prices[target] = int(number) if number.is_integer() else number
            except (TypeError, ValueError):
                continue
        for k, v in default["prices"].items():
            normalized_prices.setdefault(k, v)
        data["prices"] = normalized_prices

        if not isinstance(data.get("subscriptions"), dict):
            data["subscriptions"] = {}
        if not isinstance(data.get("payment_requests"), dict):
            data["payment_requests"] = {}
        if not isinstance(data.get("stars_orders"), dict):
            data["stars_orders"] = {}
        if not isinstance(data.get("stars_payments"), dict):
            data["stars_payments"] = {}

        # حدود Premium حسب الباقة + حد مخصص لكل مشترك.
        # لو الملف قديم، نبدأ بنفس الحد القديم حتى لا نغيّر سلوك المشتركين فجأة.
        legacy_limit = max(1, int(data.get("premium_limit", default["premium_limit"])))
        legacy_window = max(1, int(data.get("premium_window_minutes", default["premium_window_minutes"])))
        plan_limits = data.get("premium_plan_limits")
        if not isinstance(plan_limits, dict):
            plan_limits = {}
        for plan_key in ("week", "month", "year"):
            item = plan_limits.get(plan_key)
            if not isinstance(item, dict):
                item = {"limit": legacy_limit, "window_minutes": legacy_window}
            try:
                item["limit"] = max(1, int(item.get("limit", legacy_limit)))
            except Exception:
                item["limit"] = legacy_limit
            try:
                item["window_minutes"] = max(1, int(item.get("window_minutes", legacy_window)))
            except Exception:
                item["window_minutes"] = legacy_window
            plan_limits[plan_key] = item
        data["premium_plan_limits"] = plan_limits

        custom_limits = data.get("premium_custom_limits")
        if not isinstance(custom_limits, dict):
            custom_limits = {}
        cleaned_custom = {}
        for uid, item in custom_limits.items():
            if not isinstance(item, dict):
                continue
            try:
                lim = int(item.get("limit", 0))
                win = int(item.get("window_minutes", 60))
                if lim > 0 and win > 0:
                    cleaned_custom[str(uid)] = {"limit": lim, "window_minutes": win}
            except Exception:
                continue
        data["premium_custom_limits"] = cleaned_custom

        data["payment_method"] = str(data.get("payment_method") or default["payment_method"])
        data["payment_details"] = str(data.get("payment_details") or default["payment_details"])
        return data
    except Exception as e:
        print("⚠️ Premium load error:", type(e).__name__, ":", e)
        return default.copy()


def save_premium_config(data):
    try:
        with open(PREMIUM_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("⚠️ Premium save error:", type(e).__name__, ":", e)


premium_data = load_premium_config()


def premium_subscription(user_id):
    item = premium_data.get("subscriptions", {}).get(str(user_id))
    if not isinstance(item, dict):
        return None
    expires = float(item.get("expires_at", 0) or 0)
    if expires <= time.time():
        premium_data["subscriptions"].pop(str(user_id), None)
        save_premium_config(premium_data)
        return None
    return item


def is_premium(user_id):
    if not premium_data.get("enabled", False) and not is_admin(user_id):
        return False
    return premium_subscription(user_id) is not None


def format_remaining(seconds):
    seconds = max(0, int(seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    parts = []
    if days: parts.append(f"{days} يوم")
    if hours: parts.append(f"{hours} ساعة")
    if minutes or not parts: parts.append(f"{minutes} دقيقة")
    return " و".join(parts[:2])


def premium_plan_display(plan):
    key = premium_plan_key(plan)
    return {"week": "أسبوع", "month": "شهر", "year": "سنة"}.get(key, str(plan or "غير محدد"))


def get_premium_limit_config(user_id):
    item = premium_subscription(user_id)
    if not item:
        return None, None, False

    uid = str(user_id)
    custom = premium_data.get("premium_custom_limits", {}).get(uid)
    if isinstance(custom, dict):
        try:
            limit = max(1, int(custom.get("limit", 0)))
            window = max(1, int(custom.get("window_minutes", 0)))
            if limit > 0 and window > 0:
                return limit, window, True
        except Exception:
            pass

    key = premium_plan_key(item.get("plan"))
    plan_cfg = premium_data.get("premium_plan_limits", {}).get(key, {})
    try:
        limit = max(1, int(plan_cfg.get("limit", premium_data.get("premium_limit", 30))))
    except Exception:
        limit = 30
    try:
        window = max(1, int(plan_cfg.get("window_minutes", premium_data.get("premium_window_minutes", 30))))
    except Exception:
        window = 30
    return limit, window, False


def download_limit_info(user_id):
    premium = is_premium(user_id)
    if premium:
        limit, window, _ = get_premium_limit_config(user_id)
        limit = int(limit or 30)
        window = int(window or 30)
    else:
        limit = int(premium_data.get("normal_limit", 10))
        window = int(premium_data.get("normal_window_minutes", 30))

    key = "premium_download_times" if premium else "download_times"
    user = users.get(str(user_id), {})
    now = time.time()
    times = []
    for x in user.get(key, []):
        try:
            fx = float(x)
            if now - fx < window * 60:
                times.append(fx)
        except Exception:
            continue
    user[key] = times
    save_users(users)
    remaining = max(0, limit - len(times))
    return limit, window, remaining, times


def consume_download_slot(user_id):
    if is_admin(user_id):
        return True, ""
    limit, window, remaining, times = download_limit_info(user_id)
    if remaining <= 0:
        oldest = min(times) if times else time.time()
        wait = max(1, int(window * 60 - (time.time() - oldest)))
        return False, f"🚫 وصلت للحد الأقصى: {limit} تحميلات خلال {window} دقيقة.\n⏳ جرّب بعد {format_remaining(wait)}."
    user = users.setdefault(str(user_id), {})
    key = "premium_download_times" if is_premium(user_id) else "download_times"
    user.setdefault(key, []).append(time.time())
    save_users(users)
    return True, ""


def cancel_requested(job_id):
    return bool(job_id and job_id in _cancel_jobs)


users = load_users()
stats = load_stats()
maintenance_data = load_maintenance()

maintenance_mode = maintenance_data.get(
    "maintenance_mode",
    False
)

update_pending = maintenance_data.get(
    "update_pending",
    False
)


# ============================================================
# رسائل التحديث
# ============================================================

UPDATE_MESSAGE = (
    "🚀✨ **جاري تحديث صاروخ التحميل...** ✨🚀\n\n"
    "🔧 بنعمل بعض التحديثات والتطويرات علشان نحسّن الخدمة "
    "ونخلي التحميل أسرع وأفضل.\n\n"
    "⏳ **جرّب تاني بعد شوية.**\n\n"
    "💙 شكرًا لصبرك وثقتك في **صاروخ التحميل**."
)


UPDATE_SUCCESS_MESSAGE = (
    "🚀✨ **تم تحديث صاروخ التحميل بنجاح!** ✨🚀\n\n"
    "🎉 التحديث اكتمل، والصاروخ رجع للعمل بكامل قوته!\n\n"
    "⚡ سرعة وأداء أفضل\n"
    "🔧 تحسينات وتطويرات جديدة\n"
    "🎬 وتجربة تحميل أكثر سلاسة\n\n"
    "💙 **جاهزين ننطلق من جديد... استمتع بالصاروخ!** 🚀"
)


# ============================================================
# نظام الأدمنز
# ============================================================

def load_admins():
    if not os.path.exists(ADMINS_FILE):
        return []
    try:
        with open(ADMINS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            return []
        return [int(x) for x in data if str(x).isdigit() and int(x) != ADMIN_ID]
    except Exception as e:
        print("⚠️ Admins load error:", type(e).__name__, ":", e)
        return []


def save_admins(data):
    try:
        with open(ADMINS_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted(set(int(x) for x in data if int(x) != ADMIN_ID)), f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("⚠️ Admins save error:", type(e).__name__, ":", e)


admin_ids = load_admins()

ADMIN_PERMISSION_NAMES = {
    "stats": "📊 الإحصائيات",
    "users": "👥 المستخدمين",
    "broadcast": "📢 الإذاعة",
    "moderation": "🛡️ الحظر والمعلومات",
    "limits": "⚙️ حدود التحميل",
    "premium": "💎 Premium",
    "ads": "📢 الإعلانات",
    "maintenance": "🛠️ الصيانة",
    "bot_settings": "🤖 إعدادات البوت",
    "contact_inbox": "📥 رسائل المستخدمين",
    "contact_user": "📤 إرسال لمستخدم",
    "contact_broadcast": "📢 رسالة جماعية",
}

def load_admin_permissions():
    if not os.path.exists(ADMIN_PERMISSIONS_FILE):
        return {}
    try:
        with open(ADMIN_PERMISSIONS_FILE, "r", encoding="utf-8") as f:
            data=json.load(f)
        if not isinstance(data, dict): return {}
        return {str(uid): [p for p in perms if p in ADMIN_PERMISSION_NAMES] for uid, perms in data.items() if isinstance(perms,list)}
    except Exception as e:
        print("⚠️ Admin permissions load error:",e)
        return {}

def save_admin_permissions(data):
    try:
        with open(ADMIN_PERMISSIONS_FILE,"w",encoding="utf-8") as f:
            json.dump(data,f,ensure_ascii=False,indent=2)
    except Exception as e:
        print("⚠️ Admin permissions save error:",e)

admin_permissions=load_admin_permissions()

def load_owners():
    if not os.path.exists(OWNERS_FILE):
        return [ADMIN_ID]
    try:
        with open(OWNERS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list): return [ADMIN_ID]
        vals = {int(x) for x in data if str(x).isdigit()}
        vals.add(ADMIN_ID)
        return sorted(vals)
    except Exception as e:
        print("⚠️ Owners load error:", e)
        return [ADMIN_ID]

def save_owners(data):
    try:
        vals = {int(x) for x in data if str(x).isdigit()}
        vals.add(ADMIN_ID)
        with open(OWNERS_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted(vals), f, ensure_ascii=False, indent=2)
    except Exception as e: print("⚠️ Owners save error:", e)

owner_ids = load_owners()

def is_owner(user_id):
    try: return int(user_id) in owner_ids
    except Exception: return False

def is_primary_owner(user_id):
    try: return int(user_id) == ADMIN_ID
    except Exception: return False

def is_admin(user_id):
    try: return int(user_id)==ADMIN_ID or int(user_id) in owner_ids or int(user_id) in admin_ids
    except Exception: return False

def has_admin_permission(user_id, permission):
    if is_owner(user_id): return True
    if not is_admin(user_id): return False
    return permission in set(admin_permissions.get(str(user_id), []))

def set_admin_permissions(user_id, permissions):
    admin_permissions[str(int(user_id))]=[p for p in permissions if p in ADMIN_PERMISSION_NAMES]
    save_admin_permissions(admin_permissions)


# ============================================================
# صلاحيات الأدمن
# ============================================================


def is_banned(user_id):

    user = users.get(str(user_id))

    if not user:
        return False

    return bool(
        user.get("banned", False)
    )


async def admin_only(update, permission=None):
    user_id=update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("❌ الأمر ده خاص بالأدمن.")
        return False
    if permission and not has_admin_permission(user_id, permission):
        await update.message.reply_text("❌ صلاحيتك مش بتسمح باستخدام الجزء ده من لوحة الأدمن.")
        return False
    return True


async def telegram_api_call(method, data=None, files=None):
    url=f"https://api.telegram.org/bot{TOKEN}/{method}"
    response=requests.post(url,data=data or {},files=files,timeout=60)
    payload=response.json()
    if not payload.get("ok"):
        raise RuntimeError(payload.get("description","Telegram API error"))
    return payload.get("result")


# ============================================================
# تسجيل المستخدم
# ============================================================

def register_user(user):

    user_id = str(user.id)

    if user_id not in users:

        users[user_id] = {
            "id": user.id,
            "first_name": user.first_name or "",
            "username": user.username or "",
            "name": "",
            "name_registered": False,
            "banned": False,
            "downloads": 0,
            "history": [],
            "default_quality": "🔥 أعلى جودة",
            "default_mode": "ask",
            "download_times": [],
            "premium_download_times": [],
        }

        save_users(users)
        record_new_user_today()

        return True

    current = users[user_id]

    current["first_name"] = user.first_name or ""
    current["username"] = user.username or ""

    if "history" not in current:
        current["history"] = []

    if "downloads" not in current:
        current["downloads"] = 0

    if "default_quality" not in current:
        current["default_quality"] = "🔥 أعلى جودة"

    if "name_registered" not in current:
        current["name_registered"] = bool(
            current.get("name")
        )

    if "banned" not in current:
        current["banned"] = False
    if "default_mode" not in current:
        current["default_mode"] = "ask"
    if "download_times" not in current:
        current["download_times"] = []
    if "premium_download_times" not in current:
        current["premium_download_times"] = []

    save_users(users)

    return False


# ============================================================
# سجل التحميلات
# ============================================================

def add_history(
    user_id,
    title,
    site,
    kind
):

    user_id = str(user_id)

    if user_id not in users:
        return

    if "history" not in users[user_id]:
        users[user_id]["history"] = []

    item = {
        "title": str(title)[:150],
        "site": site,
        "type": kind,
        "url": str(users[user_id].get("last_url", ""))[:2000],
    }

    users[user_id]["history"].insert(
        0,
        item
    )

    users[user_id]["history"] = (
        users[user_id]["history"][:10]
    )

    save_users(users)
    try:
        clear_error_streak(int(user_id))
    except Exception:
        pass


# ============================================================
# التعرف على الموقع
# ============================================================

def detect_site(url):

    url_lower = url.lower()

    if (
        "youtube.com" in url_lower
        or "youtu.be" in url_lower
    ):
        return "▶️ يوتيوب"

    if (
        "tiktok.com" in url_lower
        or "vt.tiktok.com" in url_lower
    ):
        return "🎵 تيك توك"

    if (
        "facebook.com" in url_lower
        or "fb.watch" in url_lower
    ):
        return "📘 فيسبوك"

    if "instagram.com" in url_lower:
        return "📸 إنستجرام"

    if (
        "twitter.com" in url_lower
        or "x.com" in url_lower
    ):
        return "🐦 X"

    if "snapchat.com" in url_lower:
        return "👻 سناب شات"

    if "reddit.com" in url_lower:
        return "👽 ريديت"

    if is_suno_url(url):
        return "🎶 Suno"

    if "pinterest.com" in url_lower:
        return "📌 بنترست"

    return "🌐 موقع آخر"


# ============================================================
# تنظيف ملفات عملية
# ============================================================

def clean_job_files(job_id):

    if not job_id:
        return

    pattern = os.path.join(
        DOWNLOAD_DIR,
        f"{job_id}.*"
    )

    for path in glob.glob(pattern):

        try:

            if os.path.isfile(path):
                os.remove(path)

                print(
                    f"🧹 تم حذف: {path}"
                )

        except Exception as e:

            print(
                f"⚠️ تعذر حذف {path}: {e}"
            )

    clean_photo_files(job_id)


def clean_photo_files(job_id):

    if not job_id:
        return

    folder = os.path.join(
        DOWNLOAD_DIR,
        f"{job_id}_photos"
    )

    if not os.path.isdir(folder):
        return

    for path in glob.glob(
        os.path.join(
            folder,
            "*"
        )
    ):

        try:

            if os.path.isfile(path):
                os.remove(path)

        except Exception:
            pass

    try:
        os.rmdir(folder)
    except Exception:
        pass


# ============================================================
# البحث عن ملف العملية
# ============================================================

def find_job_file(
    job_id,
    preferred_path=None
):

    if (
        preferred_path
        and os.path.exists(preferred_path)
        and os.path.isfile(preferred_path)
    ):
        return preferred_path

    files = []

    for path in glob.glob(
        os.path.join(
            DOWNLOAD_DIR,
            f"{job_id}.*"
        )
    ):

        if not os.path.isfile(path):
            continue

        lower = path.lower()

        if lower.endswith(
            (
                ".part",
                ".ytdl",
                ".temp",
                ".info.json",
            )
        ):
            continue

        files.append(path)

    if not files:
        return None

    mp4_files = [
        path
        for path in files
        if path.lower().endswith(".mp4")
    ]

    if mp4_files:
        return mp4_files[0]

    return files[0]


def is_video_file(path):

    if not path:
        return False

    return path.lower().endswith(
        VIDEO_EXTENSIONS
    )


# ============================================================
# تجهيز الفيديو للإرسال على تيليجرام
# ============================================================

TELEGRAM_SAFE_VIDEO_BYTES = 49 * 1024 * 1024


def _ffprobe_duration(path):
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                path,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
        )
        return float((result.stdout or "").strip())
    except Exception:
        return 0.0


async def prepare_video_for_telegram(path, job_id):
    """لو الفيديو كبير، يقلل حجمه تلقائيًا قبل الإرسال بدل ما التحميل ينجح ثم الإرسال يفشل."""
    try:
        if not path or not os.path.isfile(path):
            return path
        if os.path.getsize(path) <= TELEGRAM_SAFE_VIDEO_BYTES:
            return path

        duration = await asyncio.to_thread(_ffprobe_duration, path)
        if duration <= 0:
            return path

        target_bits = int((45 * 1024 * 1024 * 8) / duration)
        audio_bits = 96000
        video_bits = max(250000, target_bits - audio_bits)
        bitrate = f"{video_bits // 1000}k"
        compressed = os.path.join(DOWNLOAD_DIR, f"{job_id}.telegram.mp4")

        print(f"📦 الفيديو كبير ({os.path.getsize(path) / 1024 / 1024:.1f} MB)، جاري ضغطه للإرسال: {bitrate}")

        def compress():
            return subprocess.run(
                [
                    "ffmpeg", "-y", "-i", path,
                    "-map", "0:v:0", "-map", "0:a?",
                    "-c:v", "libx264", "-preset", "veryfast",
                    "-b:v", bitrate, "-maxrate", bitrate, "-bufsize", f"{max(video_bits // 1000 * 2, 500)}k",
                    "-c:a", "aac", "-b:a", "96k",
                    "-movflags", "+faststart",
                    compressed,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=max(300, int(duration * 4)),
            )

        result = await asyncio.to_thread(compress)
        if result.returncode == 0 and os.path.isfile(compressed):
            size = os.path.getsize(compressed)
            if size <= TELEGRAM_SAFE_VIDEO_BYTES:
                return compressed
            try:
                os.remove(compressed)
            except Exception:
                pass
            print(f"⚠️ الضغط الأول مازال أكبر من الحد: {size / 1024 / 1024:.1f} MB")
    except Exception as e:
        print("⚠️ Video size preparation error:", type(e).__name__, ":", e)

    return path


async def send_video_file(update, path, job_id):
    """إرسال موثوق: تجهيز الحجم ثم إرسال الفيديو، مع fallback كملف."""
    send_path = await prepare_video_for_telegram(path, job_id)
    try:
        with open(send_path, "rb") as video:
            await update.message.reply_video(
                video=video,
                read_timeout=300,
                write_timeout=300,
                connect_timeout=60,
                pool_timeout=60,
                supports_streaming=True,
            )
        return send_path
    except Exception as video_error:
        print("⚠️ Video send failed, trying document:", type(video_error).__name__, ":", video_error)
        with open(send_path, "rb") as document:
            await update.message.reply_document(
                document=document,
                filename=os.path.basename(send_path),
                read_timeout=300,
                write_timeout=300,
                connect_timeout=60,
                pool_timeout=60,
            )
        return send_path


# ============================================================
# TikTok
# ============================================================

TIKTOK_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 12; Redmi Note 9 Pro) "
        "AppleWebKit/537.36 "
        "(KHTML, like Gecko) "
        "Chrome/140.0.0.0 Mobile Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.tiktok.com/",
}


# ============================================================
# جلسة إنستجرام الاختيارية
# ============================================================
INSTAGRAM_COOKIE_FILES = ("instagram_cookies.txt", "cookies.txt")
INSTAGRAM_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 12; Redmi Note 9 Pro) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/140.0.0.0 Mobile Safari/537.36"
    ),
    "Accept-Language": "ar-EG,ar;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://www.instagram.com/",
    "Origin": "https://www.instagram.com",
}

def is_instagram_url(url):
    try:
        host = urlparse(str(url).strip()).netloc.lower()
        return host == "instagram.com" or host.endswith(".instagram.com")
    except Exception:
        return False


def is_suno_url(url):
    """التعرف على روابط Suno العامة فقط، بدون تمريرها إلى yt-dlp."""
    try:
        host = urlparse(str(url).strip()).netloc.lower().split(":")[0]
        return host == "suno.com" or host == "www.suno.com" or host.endswith(".suno.com")
    except Exception:
        return False


def extract_suno_song_id(url):
    """استخراج UUID لأغنية Suno من الرابط، مع دعم روابط /s المختصرة."""
    raw = str(url or "").strip()
    match = re.search(
        r"(?i)([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
        raw,
    )
    if match:
        return match.group(1)

    if not is_suno_url(raw):
        raise ValueError("رابط Suno غير صحيح.")

    # روابط المشاركة المختصرة قد تحتاج تحويلًا أولًا إلى صفحة الأغنية.
    response = requests.get(
        raw,
        allow_redirects=True,
        timeout=30,
        impersonate="chrome",
        headers={"Referer": "https://suno.com/", "User-Agent": "Mozilla/5.0"},
    )
    final_url = getattr(response, "url", "") or ""
    match = re.search(
        r"(?i)([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
        final_url + "\n" + (getattr(response, "text", "") or "")[:200000],
    )
    if not match:
        raise ValueError("تعذر استخراج رقم أغنية Suno من الرابط.")
    return match.group(1)



# ============================================================
# Suno authenticated official-download support (2026)
# ============================================================
_suno_auth_cache = {"token": None, "expires_at": 0.0}


def _suno_browser_token():
    payload = json.dumps({"timestamp": int(time.time() * 1000)}, separators=(",", ":")).encode("utf-8")
    token = base64.urlsafe_b64encode(payload).decode("utf-8").rstrip("=")
    return json.dumps({"token": token}, separators=(",", ":"))


def _suno_auth_configured():
    return bool(os.getenv("SUNO_COOKIE", "").strip() or os.getenv("SUNO_TOKEN", "").strip())


def _get_suno_bearer_token():
    """Return a short-lived Suno JWT from SUNO_TOKEN or a SUNO_COOKIE (__client)."""
    direct = os.getenv("SUNO_TOKEN", "").strip()
    if direct:
        return direct

    now = time.time()
    if _suno_auth_cache.get("token") and now < float(_suno_auth_cache.get("expires_at", 0)):
        return _suno_auth_cache["token"]

    cookie_value = os.getenv("SUNO_COOKIE", "").strip()
    if not cookie_value:
        return None

    # Accept either the raw __client value or a full Cookie header.
    if "__client=" in cookie_value:
        parts = dict(
            item.strip().split("=", 1)
            for item in cookie_value.split(";")
            if "=" in item
        )
        client_auth = parts.get("__client", "").strip()
    else:
        client_auth = cookie_value

    if not client_auth:
        return None

    clerk_version = os.getenv("SUNO_CLERK_VERSION", "5.117.0")
    common = {
        "Authorization": client_auth,
        "User-Agent": "Mozilla/5.0",
        "Origin": "https://suno.com",
        "Referer": "https://suno.com/",
        "Accept": "application/json, text/plain, */*",
    }

    session_url = (
        "https://auth.suno.com/v1/client"
        f"?__clerk_api_version=2025-11-10&_clerk_js_version={clerk_version}"
    )
    session_resp = requests.get(session_url, headers=common, timeout=30, impersonate="chrome")
    if session_resp.status_code >= 400:
        raise RuntimeError(f"Suno authentication HTTP {session_resp.status_code}")
    session_json = session_resp.json()
    sid = ((session_json.get("response") or {}).get("last_active_session_id"))
    if not sid:
        raise RuntimeError("تعذر الحصول على جلسة Suno. تأكد أن SUNO_COOKIE صحيح.")

    token_url = (
        f"https://auth.suno.com/v1/client/sessions/{sid}/tokens"
        f"?__clerk_api_version=2025-11-10&_clerk_js_version={clerk_version}"
    )
    token_resp = requests.post(token_url, headers=common, json={}, timeout=30, impersonate="chrome")
    if token_resp.status_code >= 400:
        raise RuntimeError(f"Suno token HTTP {token_resp.status_code}")
    token = str(token_resp.json().get("jwt") or "").strip()
    if not token:
        raise RuntimeError("Suno لم يرجّع رمز دخول صالح.")

    _suno_auth_cache["token"] = token
    _suno_auth_cache["expires_at"] = time.time() + 45 * 60
    return token


def _suno_auth_headers():
    token = _get_suno_bearer_token()
    if not token:
        return None
    device_id = os.getenv("SUNO_DEVICE_ID", "").strip() or str(uuid.uuid4())
    return {
        "Authorization": f"Bearer {token}",
        "Device-Id": device_id,
        "browser-token": _suno_browser_token(),
        "Origin": "https://suno.com",
        "Referer": "https://suno.com/",
        "User-Agent": "Mozilla/5.0",
        "Accept": "*/*",
    }


def _suno_extract_download_url(payload):
    if not isinstance(payload, dict):
        return None
    for key in ("download_url", "audio_url", "media_url", "url", "file_url"):
        value = payload.get(key)
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            return value
    for key in ("data", "result", "file"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            found = _suno_extract_download_url(nested)
            if found:
                return found
    return None


def download_suno_official_to_file(song_id, output_path, media_type="audio", progress_callback=None):
    """Use Suno's authenticated download endpoint, not the retired public CDN."""
    if media_type != "audio":
        return False
    headers = _suno_auth_headers()
    if not headers:
        return False

    endpoint = f"https://studio-api.prod.suno.com/api/download/clip/{song_id}?format=mp3"
    response = requests.get(
        endpoint,
        headers=headers,
        stream=True,
        allow_redirects=True,
        timeout=120,
        impersonate="chrome",
    )

    # Some current builds return a signed URL/JSON first; follow that official URL.
    content_type = (response.headers.get("content-type") or "").lower()
    if "json" in content_type:
        try:
            media_url = _suno_extract_download_url(response.json())
        except Exception:
            media_url = None
        if not media_url:
            raise RuntimeError(f"Suno download endpoint HTTP {response.status_code}")
        response = requests.get(
            media_url,
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://suno.com/"},
            stream=True,
            timeout=120,
            impersonate="chrome",
        )

    if response.status_code >= 400:
        raise RuntimeError(f"Suno official download HTTP {response.status_code}")

    total = int(response.headers.get("content-length") or 0)
    downloaded = 0
    with open(output_path, "wb") as f:
        for chunk in response.iter_content(chunk_size=1024 * 256):
            if not chunk:
                continue
            f.write(chunk)
            downloaded += len(chunk)
            if progress_callback and total:
                progress_callback(int(downloaded * 100 / total))

    if downloaded < 1024:
        raise RuntimeError("Suno رجّع ملفًا صغيرًا أو غير صالح.")
    return True

def get_suno_media(song_id, media_type="audio"):
    """جلب رابط الصوت/الفيديو من Studio API العام مع fallback للـ CDN."""
    if not re.fullmatch(r"(?i)[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", str(song_id)):
        raise ValueError("معرّف أغنية Suno غير صحيح.")

    api_url = f"https://studio-api-prod.suno.com/api/clip/{song_id}"
    response = requests.get(
        api_url,
        timeout=30,
        impersonate="chrome",
        headers={
            "Referer": "https://suno.com/",
            "Origin": "https://suno.com",
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json,text/plain,*/*",
        },
    )
    if response.status_code >= 400:
        raise RuntimeError(f"Suno API HTTP {response.status_code}")

    try:
        data = response.json()
    except Exception as e:
        raise RuntimeError("Suno رجّع بيانات غير صالحة.") from e

    title = str(data.get("title") or data.get("display_name") or "أغنية Suno").strip()
    media_key = "video_url" if media_type == "video" else "audio_url"
    media_url = data.get(media_key)

    # بعض الردود تضع البيانات داخل clip.
    if not media_url and isinstance(data.get("clip"), dict):
        clip = data["clip"]
        title = str(clip.get("title") or title).strip()
        media_url = clip.get(media_key)

    # fallback مباشر للـ CDN العام للأغنية.
    if not media_url:
        ext = "mp4" if media_type == "video" else "mp3"
        media_url = f"https://cdn1.suno.ai/{song_id}.{ext}"

    return str(media_url), title or "أغنية Suno", data


async def download_suno_media(update, context, url, media_type="audio"):
    """تحميل Suno مباشرة من المصدر، بدون yt-dlp."""
    kind = "audio" if media_type != "video" else "video"
    cached_path, cached_title = get_cached_file(url, kind)
    if cached_path:
        try:
            if kind == "audio":
                with open(cached_path, "rb") as f:
                    await update.message.reply_audio(audio=f, filename="suno.mp3", read_timeout=180, write_timeout=180, connect_timeout=60, pool_timeout=60)
            else:
                await send_video_file(update, cached_path, uuid.uuid4().hex)
            user_id = str(update.effective_user.id)
            users[user_id]["downloads"] = users[user_id].get("downloads", 0) + 1
            add_history(user_id, cached_title or "أغنية Suno", detect_site(url), "🎵 MP3" if kind == "audio" else "🎬 فيديو")
            save_users(users)
            stats["downloads"] += 1
            stats["audio_downloads" if kind == "audio" else "videos_downloaded"] += 1
            save_stats(stats)
            record_success_stats(detect_site(url), "🎵 MP3" if kind == "audio" else "🎬 فيديو")
            clear_error_streak(update.effective_user.id)
            await maybe_send_ad(update, context)
            context.user_data.pop("download_url", None)
            context.user_data.pop("download_stage", None)
            return
        except Exception as e:
            print("⚠️ Suno cache send failed:", e)

    msg = await update.message.reply_text(
        "⏳ جاري تحميل أغنية Suno...\n░░░░░░░░░░░░ 0%",
        reply_markup=ReplyKeyboardMarkup([["⛔ إلغاء التحميل"]], resize_keyboard=True),
    )
    job_id = uuid.uuid4().hex
    _active_job_ids.add(job_id)
    context.user_data["active_job_id"] = job_id
    ext = "mp4" if kind == "video" else "mp3"
    output_path = os.path.join(DOWNLOAD_DIR, f"{job_id}.suno.{ext}")

    try:
        loop = asyncio.get_running_loop()
        song_id = await asyncio.to_thread(extract_suno_song_id, url)
        media_url, title, _ = await asyncio.to_thread(get_suno_media, song_id, kind)

        def progress_cb(percent):
            if cancel_requested(job_id):
                raise RuntimeError("DOWNLOAD_CANCELLED")
            asyncio.run_coroutine_threadsafe(
                msg.edit_text(f"⏳ جاري تحميل أغنية Suno... {percent}%"),
                loop,
            )

        def download_file():
            # First try Suno's authenticated download channel. This is the path
            # used for permitted downloads after Suno retired the public MP3 CDN.
            if kind == "audio" and _suno_auth_configured():
                try:
                    if download_suno_official_to_file(song_id, output_path, kind, progress_cb):
                        return os.path.getsize(output_path)
                except Exception as official_error:
                    print("⚠️ Suno official download failed:", official_error)
                    # If the authenticated route is configured but rejected, do not
                    # pretend the old public CDN is still a valid download route.
                    if os.getenv("SUNO_TOKEN", "").strip() or os.getenv("SUNO_COOKIE", "").strip():
                        raise RuntimeError(f"Suno الرسمي رفض التنزيل: {official_error}")

            response = requests.get(
                media_url,
                stream=True,
                timeout=120,
                impersonate="chrome",
                headers={
                    "Referer": "https://suno.com/",
                    "User-Agent": "Mozilla/5.0",
                    "Accept": "*/*",
                },
            )
            if response.status_code >= 400:
                raise RuntimeError(
                    f"فشل تنزيل ملف Suno: HTTP {response.status_code}. "
                    "Suno لم يعد يسمح بالرابط العام القديم؛ يلزم تنزيل مصادق عليه."
                )
            total = int(response.headers.get("content-length") or 0)
            downloaded = 0
            last_percent = -1
            with open(output_path, "wb") as f:
                for chunk in response.iter_content(chunk_size=1024 * 256):
                    if cancel_requested(job_id):
                        raise RuntimeError("DOWNLOAD_CANCELLED")
                    if not chunk:
                        continue
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total:
                        percent = int(downloaded * 100 / total)
                        if percent != last_percent and percent % 5 == 0:
                            last_percent = percent
                            asyncio.run_coroutine_threadsafe(
                                msg.edit_text(f"⏳ جاري تحميل أغنية Suno... {percent}%"),
                                loop,
                            )
            if not os.path.isfile(output_path) or os.path.getsize(output_path) < 1024:
                raise RuntimeError("ملف Suno الناتج فارغ أو غير صالح.")
            return total, downloaded

        await asyncio.to_thread(download_file)

        try:
            await msg.edit_text("✅ اكتمل التحميل 100%\n📤 جاري إرسال الملف...")
        except Exception:
            pass

        if kind == "audio":
            with open(output_path, "rb") as audio:
                await update.message.reply_audio(
                    audio=audio, filename="suno.mp3",
                    read_timeout=300, write_timeout=300, connect_timeout=60, pool_timeout=60,
                )
            telegram_kind = "🎵 MP3"
            save_cached_file(output_path, url, "audio", "mp3", title)
        else:
            await send_video_file(update, output_path, job_id)
            telegram_kind = "🎬 فيديو"
            save_cached_file(output_path, url, "video", "mp4", title)

        try:
            await msg.delete()
        except Exception:
            pass

        user_id = str(update.effective_user.id)
        users[user_id]["downloads"] = users[user_id].get("downloads", 0) + 1
        add_history(user_id, title, detect_site(url), telegram_kind)
        save_users(users)
        stats["downloads"] += 1
        stats["audio_downloads" if kind == "audio" else "videos_downloaded"] += 1
        save_stats(stats)
        record_success_stats(detect_site(url), telegram_kind)
        clear_error_streak(update.effective_user.id)
        await maybe_send_ad(update, context)
        context.user_data.pop("download_url", None)
        context.user_data.pop("download_stage", None)

    except Exception as e:
        stats["errors"] += 1
        save_stats(stats)
        streak = record_error_streak(update.effective_user.id)
        print("❌ SUNO ERROR:", type(e).__name__, ":", e)
        if streak >= ERROR_ALERT_THRESHOLD:
            await notify_admin(
                context.bot,
                f"⚠️ أخطاء تحميل متكررة\n👤 المستخدم: {update.effective_user.id}\n🔢 الأخطاء المتتالية: {streak}\n🌐 الموقع: Suno",
                key=f"error_{update.effective_user.id}",
            )
        try:
            if "DOWNLOAD_CANCELLED" in str(e):
                await msg.edit_text("⛔ تم إلغاء التحميل.")
            else:
                error_text = str(e).strip() or "سبب غير معروف"
                if len(error_text) > 500:
                    error_text = error_text[:500] + "..."
                await msg.edit_text(
                    "❌ فشل تحميل أغنية Suno.\n\n"
                    f"📌 السبب: {error_text}\n\n"
                    "🔗 تأكد أن الأغنية عامة والرابط من نوع suno.com/song ثم جرّب مرة أخرى."
                )
        except Exception:
            pass
    finally:
        clean_job_files(job_id)
        _active_job_ids.discard(job_id)
        context.user_data.pop("active_job_id", None)

def apply_instagram_session(options, url):
    # إنستجرام فقط: استخدم كوكيز صالحة إن كانت موجودة، سواء شُغّل البوت
    # من مجلد المشروع أو من مجلد عمل مختلف. لا علاقة لهذا المسار بـ TikTok.
    if not is_instagram_url(url):
        return False

    search_dirs = [
        os.getcwd(),
        os.path.dirname(os.path.abspath(__file__)),
    ]
    cookie_file = None
    for directory in search_dirs:
        for name in INSTAGRAM_COOKIE_FILES:
            candidate = os.path.join(directory, name)
            if os.path.isfile(candidate):
                try:
                    if os.path.getsize(candidate) > 0:
                        cookie_file = candidate
                        break
                except OSError:
                    pass
        if cookie_file:
            break

    if not cookie_file:
        return False

    options["cookiefile"] = cookie_file
    headers = dict(options.get("http_headers") or {})
    headers.update(INSTAGRAM_HEADERS)
    options["http_headers"] = headers
    return True


def clean_instagram_url(url):
    """تنظيف رابط إنستجرام من بارامترات المشاركة قبل الاستخراج."""
    try:
        parsed = urlparse(str(url).strip())
        host = parsed.netloc.lower()
        if host == "instagram.com" or host.endswith(".instagram.com"):
            return f"https://www.instagram.com{parsed.path}"
    except Exception:
        pass
    return str(url).strip()


def _instagram_extract_media_from_html(text):
    """استخراج رابط الفيديو من HTML/JSON المضمّن كمسار بديل عن Instagram API."""
    if not text:
        return None

    candidates = []

    def add(value):
        if not isinstance(value, str):
            return
        value = html.unescape(value).strip()
        for _ in range(2):
            value = (
                value.replace('\\/', '/')
                .replace('\\u0026', '&')
                .replace('\\u003D', '=')
                .replace('\\u002F', '/')
                .replace('\\u003F', '?')
            )
        if value.startswith('http') and ('.mp4' in value.lower() or 'video' in value.lower()):
            candidates.append(value)

    # 1) OpenGraph/JSON-LD.
    patterns = [
        r'<meta[^>]+property=["\']og:video(?::secure_url)?["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:video(?::secure_url)?["\']',
        r'<meta[^>]+name=["\']twitter:player:stream["\'][^>]+content=["\']([^"\']+)["\']',
        r'"(?:video_url|playback_url|playable_url|contentUrl)"\s*:\s*"([^"\\]+)"',
        r'"(?:url|src)"\s*:\s*"(https?[^"\\]+\.mp4[^"\\]*)"',
    ]
    for pattern in patterns:
        for match in re.findall(pattern, text, flags=re.IGNORECASE):
            add(match)

    # 2) ابحث داخل الـ scripts عن video_versions/video_resources وغيرها.
    for script in extract_json_scripts(text):
        for pattern in (
            r'"(?:video_url|playback_url|playable_url|contentUrl)"\s*:\s*"([^"\\]+)"',
            r'"(?:url|src)"\s*:\s*"(https?[^"\\]+\.mp4[^"\\]*)"',
        ):
            for match in re.findall(pattern, script, flags=re.IGNORECASE):
                add(match)

        # بعض استجابات إنستجرام تحمل قائمة video_versions أو video_resources
        # وبداخلها رابط mp4 بدون المفتاح video_url.
        for match in re.findall(r'"(?:video_versions|video_resources)"\s*:\s*\[(.*?)\]', script, flags=re.IGNORECASE | re.DOTALL):
            for url_match in re.findall(r'"(?:url|src)"\s*:\s*"([^"\\]+)"', match, flags=re.IGNORECASE):
                add(url_match)

    # 3) آخر fallback: أي URL واضح ينتهي بـ mp4 داخل الصفحة.
    for match in re.findall(r'https?://[^\s"\'<>]+?\.mp4[^\s"\'<>]*', text, flags=re.IGNORECASE):
        add(match)

    # إزالة التكرار والحفاظ على الترتيب.
    unique = []
    seen = set()
    for value in candidates:
        if value not in seen:
            seen.add(value)
            unique.append(value)

    for value in unique:
        if '.mp4' in value.lower():
            return value
    return unique[0] if unique else None


def _load_netscape_cookies_into_session(session, cookie_file):
    if not cookie_file or not os.path.isfile(cookie_file):
        return
    try:
        with open(cookie_file, "r", encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or line.count("\t") < 6:
                    continue
                parts = line.split("\t")
                if len(parts) < 7:
                    continue
                domain, _, path, secure, _, name, value = parts[:7]
                if domain.lstrip(".").endswith("instagram.com"):
                    session.cookies.set(name, value, domain=domain, path=path or "/")
    except Exception as cookie_error:
        print(f"⚠️ [إنستجرام] تعذر قراءة ملف الكوكيز للمسار المباشر: {cookie_error}")


def instagram_direct_download(url, job_id, progress_hook):
    """Fallback مباشر لإنستجرام: يقرأ صفحة الـReel ويحمّل og:video دون Instagram API."""
    clean_url = clean_instagram_url(url)
    headers = dict(INSTAGRAM_HEADERS)
    cookie_file = None
    for directory in (os.getcwd(), os.path.dirname(os.path.abspath(__file__))):
        for name in INSTAGRAM_COOKIE_FILES:
            candidate = os.path.join(directory, name)
            if os.path.isfile(candidate) and os.path.getsize(candidate) > 0:
                cookie_file = candidate
                break
        if cookie_file:
            break

    session = requests.Session(impersonate="chrome")
    try:
        if cookie_file:
            _load_netscape_cookies_into_session(session, cookie_file)
        response = session.get(clean_url, headers=headers, timeout=30, allow_redirects=True)
        response.raise_for_status()
        media_url = _instagram_extract_media_from_html(response.text)
        if not media_url:
            raise RuntimeError("لم يتم العثور على رابط الفيديو المباشر داخل صفحة إنستجرام.")

        output = os.path.join(DOWNLOAD_DIR, f"{job_id}.instagram.mp4")
        with session.get(media_url, headers=headers, timeout=120, stream=True) as media:
            media.raise_for_status()
            total = int(media.headers.get("content-length") or 0)
            downloaded = 0
            progress_hook({"status": "downloading", "total_bytes": total or None, "downloaded_bytes": 0})
            with open(output, "wb") as fh:
                for chunk in media.iter_content(chunk_size=1024 * 256):
                    if cancel_requested(job_id):
                        raise RuntimeError("DOWNLOAD_CANCELLED")
                    if not chunk:
                        continue
                    fh.write(chunk)
                    downloaded += len(chunk)
                    progress_hook({
                        "status": "downloading",
                        "total_bytes": total or None,
                        "downloaded_bytes": downloaded,
                    })

        progress_hook({"status": "finished", "filename": output})
        return output, {"title": "Instagram Reel", "id": urlparse(clean_url).path.rstrip('/').split('/')[-1]}
    finally:
        try:
            session.close()
        except Exception:
            pass


def resolve_tiktok_url(url):

    try:

        response = requests.get(
            url,
            impersonate="chrome",
            allow_redirects=True,
            timeout=30,
            headers=TIKTOK_HEADERS
        )

        final_url = str(
            response.url
        )

        print("🔗 TikTok redirect:")
        print(url)
        print("➡️", final_url)

        return final_url

    except Exception as e:

        print(
            "⚠️ TikTok redirect error:",
            type(e).__name__,
            ":",
            e
        )

        return url


def clean_embedded_text(text):

    if not text:
        return ""

    text = html.unescape(
        str(text)
    )

    replacements = {
        "\\u002F": "/",
        "\\u0026": "&",
        "\\/": "/",
        "\\u003D": "=",
        "\\u003F": "?",
        "\\u0022": '"',
        "\\u0027": "'",
    }

    for old, new in replacements.items():
        text = text.replace(
            old,
            new
        )

    return text


def extract_json_scripts(page):

    scripts = []

    pattern = re.compile(
        r"<script[^>]*>(.*?)</script>",
        re.IGNORECASE | re.DOTALL
    )

    for match in pattern.finditer(page):

        content = match.group(1).strip()

        if not content:
            continue

        content = clean_embedded_text(
            content
        )

        scripts.append(content)

    return scripts


# ============================================================
# استخراج رقم منشور TikTok
# ============================================================

def get_tiktok_post_id(url):

    if not url:
        return None

    patterns = [
        r"/video/(\d+)",
        r"/photo/(\d+)",
    ]

    for pattern in patterns:

        match = re.search(
            pattern,
            url,
            re.IGNORECASE
        )

        if match:
            return match.group(1)

    return None


# ============================================================
# البحث عن ItemModule / itemStruct
# ============================================================

def find_tiktok_post_struct(
    data,
    post_id=None
):

    if isinstance(data, dict):

        item_module = data.get(
            "ItemModule"
        )

        if isinstance(
            item_module,
            dict
        ):

            if post_id:

                item = item_module.get(
                    str(post_id)
                )

                if isinstance(
                    item,
                    dict
                ):

                    return item

            else:

                for item in item_module.values():

                    if isinstance(
                        item,
                        dict
                    ):

                        if "imagePost" in item:
                            return item

        item_info = data.get(
            "itemInfo"
        )

        if isinstance(
            item_info,
            dict
        ):

            item_struct = item_info.get(
                "itemStruct"
            )

            if isinstance(
                item_struct,
                dict
            ):

                item_struct_id = str(
                    item_struct.get(
                        "id",
                        ""
                    )
                )

                if (
                    not post_id
                    or item_struct_id == str(post_id)
                ):

                    return item_struct

        item_struct = data.get(
            "itemStruct"
        )

        if isinstance(
            item_struct,
            dict
        ):

            item_struct_id = str(
                item_struct.get(
                    "id",
                    ""
                )
            )

            if (
                not post_id
                or item_struct_id == str(post_id)
            ):

                return item_struct

        for key, value in data.items():

            if key in (
                "ItemModule",
                "itemInfo",
                "itemStruct",
            ):
                continue

            if isinstance(
                value,
                (dict, list)
            ):

                found = find_tiktok_post_struct(
                    value,
                    post_id
                )

                if found:
                    return found

    elif isinstance(data, list):

        for item in data:

            if isinstance(
                item,
                (dict, list)
            ):

                found = find_tiktok_post_struct(
                    item,
                    post_id
                )

                if found:
                    return found

    return None


# ============================================================
# استخراج رابط صوت TikTok Photo
# ============================================================

def extract_tiktok_audio_url(
    page,
    post_id=None
):

    scripts = extract_json_scripts(page)

    audio_keys = (
        "playUrl",
        "play_url",
        "playApi",
        "playApiUrl",
        "audioUrl",
        "audio_url",
        "downloadUrl",
        "download_url",
    )

    def find_audio_value(data):

        if isinstance(data, dict):

            music = data.get("music")

            if isinstance(music, dict):

                for key in audio_keys:

                    value = music.get(key)

                    if isinstance(value, str):

                        value = clean_embedded_text(
                            value
                        )

                        if value.startswith("http"):
                            return value

                result = find_audio_value(music)

                if result:
                    return result

            for key, value in data.items():

                if str(key).lower() == "music":

                    result = find_audio_value(
                        value
                    )

                    if result:
                        return result

            for key in audio_keys:

                value = data.get(key)

                if isinstance(value, str):

                    value = clean_embedded_text(
                        value
                    )

                    if value.startswith("http"):
                        return value

            for value in data.values():

                if isinstance(
                    value,
                    (dict, list)
                ):

                    result = find_audio_value(
                        value
                    )

                    if result:
                        return result

        elif isinstance(data, list):

            for item in data:

                if isinstance(
                    item,
                    (dict, list)
                ):

                    result = find_audio_value(
                        item
                    )

                    if result:
                        return result

        return None

    for script in scripts:

        if (
            "music" not in script.lower()
            and "playUrl" not in script
            and "play_url" not in script
            and "audioUrl" not in script
            and "audio_url" not in script
        ):
            continue

        try:

            data = json.loads(
                script
            )

        except Exception:
            continue

        item_struct = find_tiktok_post_struct(
            data,
            post_id
        )

        if item_struct:

            audio_url = find_audio_value(
                item_struct
            )

            if audio_url:

                print(
                    "🎵 تم العثور على رابط صوت TikTok من المنشور."
                )

                return audio_url

    for script in scripts:

        try:

            data = json.loads(
                script
            )

        except Exception:
            continue

        audio_url = find_audio_value(
            data
        )

        if audio_url:

            print(
                "🎵 تم العثور على رابط الصوت بالبحث الاحتياطي."
            )

            return audio_url

    print(
        "⚠️ لم يتم العثور على رابط صوت TikTok."
    )

    return None


# ============================================================
# استخراج صور المنشور نفسه فقط
# ============================================================

def extract_images_from_post_struct(
    item_struct
):

    if not isinstance(
        item_struct,
        dict
    ):
        return []

    image_post = item_struct.get(
        "imagePost"
    )

    if not isinstance(
        image_post,
        dict
    ):
        return []

    images = image_post.get(
        "images"
    )

    if not isinstance(
        images,
        list
    ):
        return []

    result = []

    for image in images:

        if not isinstance(
            image,
            dict
        ):
            continue

        image_url = image.get(
            "imageURL"
        )

        candidates = []

        if isinstance(
            image_url,
            dict
        ):

            url_list = image_url.get(
                "urlList"
            )

            if isinstance(
                url_list,
                list
            ):

                for item in url_list:

                    if isinstance(
                        item,
                        str
                    ):

                        item = clean_embedded_text(
                            item
                        )

                        if item.startswith("http"):
                            candidates.append(item)

        elif isinstance(
            image_url,
            str
        ):

            image_url = clean_embedded_text(
                image_url
            )

            if image_url.startswith("http"):
                candidates.append(
                    image_url
                )

        if candidates:

            selected = candidates[0]

            if selected not in result:
                result.append(
                    selected
                )

    return result


# ============================================================
# استخراج صور TikTok Photo
# ============================================================

def extract_tiktok_photo_urls(
    page,
    post_id=None
):

    scripts = extract_json_scripts(
        page
    )

    found = []

    for script in scripts:

        if "imagePost" not in script:
            continue

        try:

            data = json.loads(
                script
            )

        except Exception:
            continue

        item_struct = find_tiktok_post_struct(
            data,
            post_id
        )

        if item_struct:

            urls = extract_images_from_post_struct(
                item_struct
            )

            for url in urls:

                if url not in found:
                    found.append(url)

            if found:
                break

    print(
        f"🖼️ TikTok Photo images found: {len(found)}"
    )

    return found


# ============================================================
# تحديد نوع رابط TikTok
# ============================================================

async def detect_tiktok_content_type(url):

    final_url = await asyncio.to_thread(
        resolve_tiktok_url,
        url
    )

    lower = final_url.lower()

    # نوع منشور TikTok يتحدد من الرابط النهائي فقط.
    # /photo/ = منشور صور حقيقي.
    # أي رابط TikTok آخر = فيديو، حتى لو كان الفيديو
    # عبارة عن صورة ثابتة مع موسيقى.
    if "/photo/" in lower:
        return "photo", final_url

    return "video", final_url


# ============================================================
# تحميل TikTok Photo
# ============================================================

@download_guard
async def download_tiktok_photos(
    url,
    job_id,
    progress_msg=None
):

    final_url = await asyncio.to_thread(
        resolve_tiktok_url,
        url
    )

    print(
        "🎵 TikTok final URL:",
        final_url
    )

    if "/photo/" not in final_url.lower():

        return None, final_url

    response = await asyncio.to_thread(
        requests.get,
        final_url,
        impersonate="chrome",
        timeout=45,
        headers=TIKTOK_HEADERS
    )

    print(
        "📡 TikTok Photo HTTP:",
        response.status_code
    )

    if response.status_code != 200:

        raise Exception(
            f"TikTok HTTP {response.status_code}"
        )

    page = response.text

    post_id = get_tiktok_post_id(
        final_url
    )

    photo_urls = extract_tiktok_photo_urls(
        page,
        post_id
    )

    if not photo_urls:

        try:

            response2 = await asyncio.to_thread(
                requests.get,
                final_url,
                impersonate="safari",
                timeout=45,
                headers=TIKTOK_HEADERS
            )

            if response2.status_code == 200:

                photo_urls = extract_tiktok_photo_urls(
                    response2.text,
                    post_id
                )

        except Exception as e:

            print(
                "⚠️ TikTok second request error:",
                e
            )

    if not photo_urls:

        raise Exception(
            "لم يتم العثور على صور المنشور نفسه."
        )

    folder = os.path.join(
        DOWNLOAD_DIR,
        f"{job_id}_photos"
    )

    os.makedirs(
        folder,
        exist_ok=True
    )

    files = []
    seen_hashes = set()
    total_photos = len(photo_urls)

    if progress_msg is not None:
        try:
            await progress_msg.edit_text(
                f"⏳ جاري تحميل الصور...\n░░░░░░░░░░░░ 0%\n🖼️ 0 / {total_photos}"
            )
        except Exception:
            pass

    for index, photo_url in enumerate(
        photo_urls,
        start=1
    ):

        try:

            if progress_msg is not None:
                try:
                    await progress_msg.edit_text(
                        f"⏳ جاري تحميل الصور...\n"
                        f"{build_progress_bar(int((index - 1) * 100 / total_photos))} {int((index - 1) * 100 / total_photos)}%\n"
                        f"🖼️ الصورة {index} من {total_photos}"
                    )
                except Exception:
                    pass

            image_response = await asyncio.to_thread(
                requests.get,
                photo_url,
                impersonate="chrome",
                timeout=45,
                headers={
                    **TIKTOK_HEADERS,
                    "Referer": final_url,
                }
            )

            if image_response.status_code != 200:
                continue

            content = image_response.content

            if not content:
                continue

            content_type = (
                image_response.headers
                .get(
                    "content-type",
                    ""
                )
                .lower()
            )

            if "png" in content_type:

                ext = ".png"

            elif (
                "jpeg" in content_type
                or "jpg" in content_type
            ):

                ext = ".jpg"

            elif "webp" in content_type:

                ext = ".webp"

            elif content.startswith(
                b"\x89PNG"
            ):

                ext = ".png"

            elif content.startswith(
                b"\xff\xd8\xff"
            ):

                ext = ".jpg"

            elif (
                content[:4] == b"RIFF"
                and b"WEBP" in content[:16]
            ):

                ext = ".webp"

            else:

                continue

            # TikTok may return multiple URLs for the same image.
            # Deduplicate by the actual image bytes, not only by URL.
            image_hash = hashlib.sha256(content).hexdigest()

            if image_hash in seen_hashes:
                print(
                    f"↩️ تم تجاهل صورة مكررة رقم {index}"
                )
                continue

            seen_hashes.add(image_hash)

            real_index = len(files) + 1
            path = os.path.join(
                folder,
                f"{real_index:03d}{ext}"
            )

            with open(
                path,
                "wb"
            ) as f:

                f.write(content)

            if progress_msg is not None:
                try:
                    pct = int(index * 100 / total_photos)
                    await progress_msg.edit_text(
                        f"⏳ جاري تحميل الصور...\n{build_progress_bar(pct)} {pct}%\n🖼️ {index} / {total_photos}"
                    )
                except Exception:
                    pass

            if (
                os.path.exists(path)
                and os.path.getsize(path) > 0
            ):

                files.append(path)

                print(
                    f"✅ تم تحميل صورة المنشور الفعلية رقم {real_index}"
                )

        except Exception as e:

            print(
                f"⚠️ TikTok Photo {index} error:",
                e
            )

    if not files:

        raise Exception(
            "فشل تحميل صور TikTok."
        )

    return files, final_url


# ============================================================
# إرسال صور TikTok
# ============================================================

@download_guard
async def send_tiktok_photos(
    update,
    files
):

    sent_count = 0

    for start in range(
        0,
        len(files),
        10
    ):

        batch = files[
            start:start + 10
        ]

        valid_batch = []

        for path in batch:

            if not os.path.exists(path):
                continue

            if os.path.getsize(path) <= 0:
                continue

            if not path.lower().endswith(
                IMAGE_EXTENSIONS
            ):
                continue

            valid_batch.append(
                path
            )

        if not valid_batch:
            continue

        opened = []
        media = []

        try:

            for path in valid_batch:

                file_obj = open(
                    path,
                    "rb"
                )

                opened.append(
                    file_obj
                )

                media.append(
                    InputMediaPhoto(
                        media=InputFile(
                            file_obj,
                            filename=os.path.basename(path)
                        )
                    )
                )

            try:

                await update.message.reply_media_group(
                    media=media
                )

                sent_count += len(
                    valid_batch
                )

                print(
                    f"📸 تم إرسال مجموعة: {len(valid_batch)}"
                )

                continue

            except Exception as group_error:

                print(
                    "⚠️ فشل Media Group:",
                    type(group_error).__name__,
                    ":",
                    group_error
                )

        finally:

            for file_obj in opened:

                try:
                    file_obj.close()
                except Exception:
                    pass

        for path in valid_batch:

            try:

                with open(
                    path,
                    "rb"
                ) as photo:

                    await update.message.reply_photo(
                        photo=InputFile(
                            photo,
                            filename=os.path.basename(path)
                        )
                    )

                sent_count += 1

                await asyncio.sleep(0.15)

            except Exception as e:

                print(
                    "⚠️ فشل إرسال صورة:",
                    path,
                    e
                )

    return sent_count


# ============================================================
# تنظيف حالة TikTok Photo
# ============================================================

def clear_tiktok_photo_state(
    context,
    clean_files=True
):

    job_id = context.user_data.get(
        "tiktok_photo_job"
    )

    if clean_files and job_id:

        clean_photo_files(
            job_id
        )

        for path in glob.glob(
            os.path.join(
                DOWNLOAD_DIR,
                f"{job_id}.*"
            )
        ):

            try:

                if os.path.isfile(path):
                    os.remove(path)

            except Exception:
                pass

    context.user_data.pop(
        "tiktok_photo_files",
        None
    )

    context.user_data.pop(
        "tiktok_photo_job",
        None
    )

    context.user_data.pop(
        "tiktok_photo_url",
        None
    )


# ============================================================
# TikTok Photo - الصورة فقط
# ============================================================

@download_guard
async def send_tiktok_photo_only(
    update,
    context
):

    files = context.user_data.get(
        "tiktok_photo_files"
    )

    if not files:

        clear_tiktok_photo_state(
            context,
            False
        )

        await update.message.reply_text(
            "❌ الصور مش موجودة.\n"
            "🔄 ابعت الرابط من جديد."
        )

        return

    msg = await update.message.reply_text(
        "🖼️ جاري إرسال الصور..."
    )

    try:

        sent_count = await send_tiktok_photos(
            update,
            files
        )

        if sent_count == 0:

            raise Exception(
                "فشل إرسال الصور."
            )

        user_id = str(
            update.effective_user.id
        )

        if user_id in users:

            users[user_id]["downloads"] = (
                users[user_id].get(
                    "downloads",
                    0
                ) + 1
            )

        add_history(
            user_id,
            "TikTok Photo",
            "🎵 تيك توك",
            "🖼️ صور"
        )

        save_users(users)

        stats["downloads"] += 1

        save_stats(stats)

        try:
            await msg.delete()
        except Exception:
            pass

        await update.message.reply_text(
            f"✅ تم إرسال {sent_count} صورة بنجاح 🖼️❤️"
        )

        await maybe_send_ad(update, context)

    except Exception as e:

        stats["errors"] += 1
        save_stats(stats)

        print(
            "❌ PHOTO ONLY ERROR:",
            type(e).__name__,
            ":",
            e
        )

        try:

            await msg.edit_text(
                "❌ حصل خطأ أثناء إرسال الصور."
            )

        except Exception:
            pass

    finally:

        clear_tiktok_photo_state(
            context,
            True
        )


# ============================================================
# TikTok Photo - استخراج الصوت
# ============================================================

@download_guard
async def download_tiktok_photo_audio(
    update,
    context,
    silent=False
):

    job_id = context.user_data.get(
        "tiktok_photo_job"
    )

    url = context.user_data.get(
        "tiktok_photo_url"
    )

    if not job_id or not url:

        if not silent:

            await update.message.reply_text(
                "❌ بيانات المنشور انتهت.\n"
                "🔄 ابعت الرابط من جديد."
            )

        return None

    msg = None

    if not silent:

        msg = await update.message.reply_text(
            "🎵 جاري استخراج الصوت..."
        )

    source_audio = os.path.join(
        DOWNLOAD_DIR,
        f"{job_id}.audio_source"
    )

    audio_path = os.path.join(
        DOWNLOAD_DIR,
        f"{job_id}.audio.mp3"
    )

    success = False

    try:

        final_url = await asyncio.to_thread(
            resolve_tiktok_url,
            url
        )

        print(
            "🎵 TikTok Photo Audio Final URL:",
            final_url
        )

        response = await asyncio.to_thread(
            requests.get,
            final_url,
            impersonate="chrome",
            timeout=45,
            headers=TIKTOK_HEADERS
        )

        print(
            "📡 TikTok Audio Page HTTP:",
            response.status_code
        )

        if response.status_code != 200:

            raise Exception(
                f"TikTok HTTP {response.status_code}"
            )

        page = response.text

        post_id = get_tiktok_post_id(
            final_url
        )

        audio_url = extract_tiktok_audio_url(
            page,
            post_id
        )

        if not audio_url:

            try:

                response2 = await asyncio.to_thread(
                    requests.get,
                    final_url,
                    impersonate="safari",
                    timeout=45,
                    headers=TIKTOK_HEADERS
                )

                if response2.status_code == 200:

                    audio_url = extract_tiktok_audio_url(
                        response2.text,
                        post_id
                    )

            except Exception as e:

                print(
                    "⚠️ TikTok Audio second request error:",
                    e
                )

        if audio_url:

            print(
                "🎵 تم العثور على رابط الموسيقى:"
            )

            print(
                audio_url[:300]
            )

            audio_response = await asyncio.to_thread(
                requests.get,
                audio_url,
                impersonate="chrome",
                timeout=90,
                headers={
                    "User-Agent": TIKTOK_HEADERS["User-Agent"],
                    "Accept": "*/*",
                    "Accept-Language": "en-US,en;q=0.9",
                    "Referer": final_url,
                }
            )

            content = audio_response.content

            content_type = (
                audio_response.headers
                .get(
                    "content-type",
                    ""
                )
                .lower()
            )

            print(
                "🎵 Audio HTTP:",
                audio_response.status_code
            )

            print(
                "🎵 Audio Content-Type:",
                content_type
            )

            print(
                "🎵 Audio Size:",
                len(content)
            )

            if (
                audio_response.status_code == 200
                and content
                and len(content) > 1000
                and "text/html" not in content_type
                and "application/json" not in content_type
            ):

                with open(
                    source_audio,
                    "wb"
                ) as f:

                    f.write(content)

                command = [
                    "ffmpeg",
                    "-y",
                    "-i",
                    source_audio,
                    "-vn",
                    "-codec:a",
                    "libmp3lame",
                    "-b:a",
                    "192k",
                    audio_path,
                ]

                result = await asyncio.to_thread(
                    subprocess.run,
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=180
                )

                print(
                    "🎵 FFmpeg return code:",
                    result.returncode
                )

                if (
                    result.returncode == 0
                    and os.path.exists(audio_path)
                    and os.path.getsize(audio_path) > 0
                ):

                    success = True

                    print(
                        "✅ تم استخراج صوت TikTok Photo مباشرة."
                    )

                    if msg:

                        try:
                            await msg.delete()
                        except Exception:
                            pass

                    return audio_path

        print(
            "🔄 جاري تجربة yt-dlp كحل احتياطي..."
        )

        loop = asyncio.get_running_loop()

        filename = os.path.join(
            DOWNLOAD_DIR,
            f"{job_id}.audio.%(ext)s"
        )

        options = {
            "outtmpl": filename,
            "format": "bestaudio/best",
            "noplaylist": True,
            "quiet": False,
            "no_warnings": False,
            "socket_timeout": 120,
            "retries": 5,
            "fragment_retries": 5,
            "continuedl": True,
            "overwrites": True,
            "http_headers": {
                "User-Agent": TIKTOK_HEADERS["User-Agent"],
                "Referer": "https://www.tiktok.com/",
            },
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }
            ],
        }

        def download_audio_file():

            with yt_dlp.YoutubeDL(
                options
            ) as ydl:

                return ydl.extract_info(
                    final_url,
                    download=True
                )

        await loop.run_in_executor(
            None,
            download_audio_file
        )

        mp3_files = [
            path
            for path in glob.glob(
                os.path.join(
                    DOWNLOAD_DIR,
                    f"{job_id}.audio.*"
                )
            )
            if (
                os.path.isfile(path)
                and path.lower().endswith(".mp3")
                and os.path.getsize(path) > 0
            )
        ]

        if mp3_files:

            audio_path = mp3_files[0]

            success = True

            print(
                "✅ تم استخراج الصوت باستخدام yt-dlp."
            )

            if msg:

                try:
                    await msg.delete()
                except Exception:
                    pass

            return audio_path

        raise Exception(
            "لم يتم العثور على ملف الصوت."
        )

    except Exception as e:

        print(
            "❌ TIKTOK PHOTO AUDIO ERROR:",
            type(e).__name__,
            ":",
            e
        )

        print(
            "❌ التفاصيل:",
            str(e)
        )

        if not silent and msg:

            try:

                await msg.edit_text(
                    "❌ مقدرتش أستخرج الصوت من المنشور.\n\n"
                    "🔄 جرّب مرة تانية."
                )

            except Exception:
                pass

        return None

    finally:

        try:

            if os.path.exists(
                source_audio
            ):

                os.remove(
                    source_audio
                )

        except Exception:
            pass

        if not success:

            try:

                if os.path.exists(
                    audio_path
                ):

                    os.remove(
                        audio_path
                    )

            except Exception:
                pass


# ============================================================
# TikTok Photo - الصورة + الصوت
# ============================================================
# الدالة القديمة أصبحت غير مستخدمة في الواجهة الجديدة،
# وتُركت هنا للحفاظ على باقي الكود كما هو.
# ============================================================

async def send_tiktok_photo_with_audio(
    update,
    context
):

    files = context.user_data.get(
        "tiktok_photo_files"
    )

    if not files:

        clear_tiktok_photo_state(
            context,
            False
        )

        await update.message.reply_text(
            "❌ الصور مش موجودة.\n"
            "🔄 ابعت الرابط من جديد."
        )

        return

    audio_path = await download_tiktok_photo_audio(
        update,
        context
    )

    if not audio_path:
        return

    try:

        sent_count = await send_tiktok_photos(
            update,
            files
        )

        if sent_count == 0:

            raise Exception(
                "فشل إرسال الصور."
            )

        with open(
            audio_path,
            "rb"
        ) as audio:

            await update.message.reply_audio(
                audio=audio,
                filename="audio.mp3",
                read_timeout=180,
                write_timeout=180,
                connect_timeout=60,
                pool_timeout=60
            )

        user_id = str(
            update.effective_user.id
        )

        if user_id in users:

            users[user_id]["downloads"] = (
                users[user_id].get(
                    "downloads",
                    0
                ) + 1
            )

        add_history(
            user_id,
            "TikTok Photo",
            "🎵 تيك توك",
            "🖼️ صور + صوت"
        )

        save_users(users)

        stats["downloads"] += 1

        save_stats(stats)

        await update.message.reply_text(
            "✅ تم إرسال الصور والصوت بنجاح 🖼️🎵❤️"
        )

        await maybe_send_ad(update, context)

    except Exception as e:

        stats["errors"] += 1
        save_stats(stats)

        print(
            "❌ PHOTO + AUDIO ERROR:",
            type(e).__name__,
            ":",
            e
        )

        await update.message.reply_text(
            "❌ حصل خطأ أثناء إرسال الصور والصوت."
        )

    finally:

        clear_tiktok_photo_state(
            context,
            True
        )


# ============================================================
# TikTok Photo - الصوت فقط
# ============================================================

@download_guard
async def send_tiktok_photo_audio_only(
    update,
    context
):

    audio_path = await download_tiktok_photo_audio(
        update,
        context
    )

    if not audio_path:
        return

    try:

        with open(
            audio_path,
            "rb"
        ) as audio:

            await update.message.reply_audio(
                audio=audio,
                filename="audio.mp3",
                read_timeout=180,
                write_timeout=180,
                connect_timeout=60,
                pool_timeout=60
            )

        user_id = str(
            update.effective_user.id
        )

        if user_id in users:

            users[user_id]["downloads"] = (
                users[user_id].get(
                    "downloads",
                    0
                ) + 1
            )

        add_history(
            user_id,
            "TikTok Photo",
            "🎵 تيك توك",
            "🎵 MP3"
        )

        save_users(users)

        stats["downloads"] += 1
        stats["audio_downloads"] += 1

        save_stats(stats)

        await update.message.reply_text(
            "✅ تم استخراج الصوت بنجاح 🎵❤️"
        )

        await maybe_send_ad(update, context)

    except Exception as e:

        stats["errors"] += 1
        save_stats(stats)

        print(
            "❌ PHOTO AUDIO ONLY ERROR:",
            type(e).__name__,
            ":",
            e
        )

        await update.message.reply_text(
            "❌ حصل خطأ أثناء إرسال الصوت."
        )

    finally:

        try:

            if os.path.exists(audio_path):
                os.remove(audio_path)

        except Exception:
            pass

        clear_tiktok_photo_state(
            context,
            True
        )


# ============================================================
# TikTok Photo - تحويل الصور إلى فيديو
# ============================================================

@download_guard
async def send_tiktok_photo_as_video(
    update,
    context
):

    files = context.user_data.get(
        "tiktok_photo_files"
    )

    job_id = context.user_data.get(
        "tiktok_photo_job"
    )

    if not files or not job_id:

        clear_tiktok_photo_state(
            context,
            False
        )

        await update.message.reply_text(
            "❌ الصور مش موجودة.\n"
            "🔄 ابعت الرابط من جديد."
        )

        return

    msg = await update.message.reply_text(
        "🎬 جاري تحويل الصور إلى فيديو..."
    )

    video_path = os.path.join(
        DOWNLOAD_DIR,
        f"{job_id}.photo_video.mp4"
    )

    concat_path = os.path.join(
        DOWNLOAD_DIR,
        f"{job_id}.concat.txt"
    )

    audio_path = None

    try:

        valid_files = [
            path
            for path in files
            if (
                os.path.exists(path)
                and os.path.getsize(path) > 0
            )
        ]

        if not valid_files:

            raise Exception(
                "لا توجد صور صالحة."
            )

        # ----------------------------------------------------
        # ملف concat الخاص بـ FFmpeg
        # ----------------------------------------------------

        with open(
            concat_path,
            "w",
            encoding="utf-8"
        ) as f:

            for path in valid_files:

                safe_path = os.path.abspath(
                    path
                ).replace(
                    "'",
                    "'\\''"
                )

                f.write(
                    f"file '{safe_path}'\n"
                )

                # لو المنشور صورة واحدة ومعاه صوت، لا نقفل الفيديو بعد 3 ثواني.
                # نترك مدة الصورة طويلة، و- shortest سيوقف الفيديو عند نهاية الأغنية.
                # المنشورات متعددة الصور تظل كل صورة فيها 3 ثواني كما كانت.
                image_duration = 3600 if len(valid_files) == 1 else 3

                f.write(
                    f"duration {image_duration}\n"
                )

            # تكرار الصورة الأخيرة مهم في concat
            last_path = os.path.abspath(
                valid_files[-1]
            ).replace(
                "'",
                "'\\''"
            )

            f.write(
                f"file '{last_path}'\n"
            )

        # ----------------------------------------------------
        # نحاول استخراج صوت المنشور بدون رسالة خطأ للمستخدم
        # ----------------------------------------------------

        audio_path = await download_tiktok_photo_audio(
            update,
            context,
            silent=True
        )

        # ----------------------------------------------------
        # إنشاء الفيديو
        # ----------------------------------------------------

        if (
            audio_path
            and os.path.exists(audio_path)
            and len(valid_files) == 1
        ):

            # صورة واحدة + صوت: نستخدم الصورة كـ input متكرر
            # بدل concat، عشان الفيديو يفضل شغال لحد نهاية الصوت
            # وما يحصلش تعليق أثناء تحويل صورة ثابتة مع أغنية.
            command = [
                "ffmpeg",
                "-y",
                "-loop",
                "1",
                "-i",
                valid_files[0],
                "-i",
                audio_path,
                "-vf",
                "scale=1080:1920:force_original_aspect_ratio=decrease,"
                "pad=1080:1920:(ow-iw)/2:(oh-ih)/2",
                "-r",
                "30",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "23",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-shortest",
                video_path,
            ]

        elif audio_path and os.path.exists(audio_path):

            command = [
                "ffmpeg",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                concat_path,
                "-i",
                audio_path,
                "-vf",
                "scale=1080:1920:force_original_aspect_ratio=decrease,"
                "pad=1080:1920:(ow-iw)/2:(oh-ih)/2",
                "-r",
                "30",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "23",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-shortest",
                video_path,
            ]

        else:

            command = [
                "ffmpeg",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                concat_path,
                "-vf",
                "scale=1080:1920:force_original_aspect_ratio=decrease,"
                "pad=1080:1920:(ow-iw)/2:(oh-ih)/2",
                "-r",
                "30",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "23",
                "-pix_fmt",
                "yuv420p",
                "-an",
                video_path,
            ]

        result = await asyncio.to_thread(
            subprocess.run,
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=300
        )

        if (
            result.returncode != 0
            or not os.path.exists(video_path)
            or os.path.getsize(video_path) <= 0
        ):

            print(
                "❌ PHOTO VIDEO FFMPEG:",
                result.stderr[-3000:]
            )

            raise Exception(
                "فشل إنشاء فيديو الصور."
            )

        try:
            await msg.delete()
        except Exception:
            pass

        with open(
            video_path,
            "rb"
        ) as video:

            await update.message.reply_video(
                video=video,
                filename="tiktok_photo_video.mp4",
                read_timeout=300,
                write_timeout=300,
                connect_timeout=60,
                pool_timeout=60
            )

        user_id = str(
            update.effective_user.id
        )

        if user_id in users:

            users[user_id]["downloads"] = (
                users[user_id].get(
                    "downloads",
                    0
                ) + 1
            )

        add_history(
            user_id,
            "TikTok Photo",
            "🎵 تيك توك",
            "🎬 فيديو"
        )

        save_users(users)

        stats["downloads"] += 1
        stats["videos_downloaded"] += 1

        save_stats(stats)

        await update.message.reply_text(
            "✅ تم تحويل الصور إلى فيديو وإرساله بنجاح 🎬❤️"
        )

        await maybe_send_ad(update, context)

    except Exception as e:

        stats["errors"] += 1
        save_stats(stats)

        print(
            "❌ PHOTO VIDEO ERROR:",
            type(e).__name__,
            ":",
            e
        )

        try:

            await msg.edit_text(
                "❌ حصل خطأ أثناء تحويل الصور إلى فيديو."
            )

        except Exception:
            pass

    finally:

        for path in (
            video_path,
            concat_path,
            audio_path
        ):

            try:

                if path and os.path.exists(path):
                    os.remove(path)

            except Exception:
                pass

        clear_tiktok_photo_state(
            context,
            True
        )


# ============================================================
# إرسال TikTok Photo
# ============================================================

@download_guard
@download_guard
async def download_tiktok_photo_post(
    update,
    context,
    url
):

    msg = await update.message.reply_text(
        "📸 تم التأكد إن ده منشور صور TikTok.\n\n"
        "⏳ جاري تجهيز الصور..."
    )

    job_id = uuid.uuid4().hex

    try:

        files, final_url = await download_tiktok_photos(
            url,
            job_id,
            msg
        )

        if not files:
            raise Exception(
                "لا توجد صور."
            )

        context.user_data[
            "tiktok_photo_files"
        ] = files

        context.user_data[
            "tiktok_photo_job"
        ] = job_id

        context.user_data[
            "tiktok_photo_url"
        ] = final_url

        try:
            await msg.delete()
        except Exception:
            pass

        await update.message.reply_text(
            f"📸 تم تجهيز {len(files)} صورة.\n\n"
            "اختار إيه اللي تحب تستلمه:",
            reply_markup=ReplyKeyboardMarkup(
                [
                    ["🖼️ الصورة فقط", "🎬 فيديو"],
                    ["🎵 صوت"],
                    ["🔙 رجوع"],
                ],
                resize_keyboard=True,
                one_time_keyboard=False
            )
        )

        return

    except Exception as e:

        stats["errors"] += 1
        save_stats(stats)

        print(
            "❌ TIKTOK PHOTO ERROR:",
            type(e).__name__,
            ":",
            e
        )

        try:

            await msg.edit_text(
                "❌ حصل خطأ أثناء تحميل صور TikTok.\n\n"
                "🔄 جرّب الرابط مرة تانية."
            )

        except Exception:
            pass

        clean_photo_files(
            job_id
        )


# ============================================================
# فحص الفيديو الثابت
# ============================================================

def is_static_video(file_path):

    if (
        not file_path
        or not os.path.exists(file_path)
    ):
        return False

    try:

        command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "info",
            "-i",
            file_path,
            "-vf",
            "select='gt(scene,0.015)',showinfo",
            "-an",
            "-f",
            "null",
            "-"
        ]

        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=120
        )

        output = (
            result.stdout
            + "\n"
            + result.stderr
        )

        scene_count = len(
            re.findall(
                r"Parsed_showinfo",
                output
            )
        )

        print(
            "🔎 Static check - scene changes:",
            scene_count
        )

        return scene_count <= 1

    except Exception as e:

        print(
            "⚠️ Static detection error:",
            type(e).__name__,
            ":",
            e
        )

        return False


# ============================================================
# أزرار الفيديو الثابت
# ============================================================

def static_video_keyboard():

    return [
        ["🖼️ صورة فقط", "🎬 فيديو"],
        ["🎵 صوت"],
        ["🔙 رجوع"],
    ]


# ============================================================
# تنظيف حالة الفيديو الثابت
# ============================================================

def clear_static_state(
    context,
    clean_files=True
):

    job_id = context.user_data.get(
        "static_video_job"
    )

    if clean_files and job_id:

        clean_job_files(
            job_id
        )

        _active_job_ids.discard(job_id)

    context.user_data.pop(
        "static_video_path",
        None
    )

    context.user_data.pop(
        "static_video_job",
        None
    )


# ============================================================
# صوت فقط من الفيديو الثابت
# ============================================================

@download_guard
async def send_static_audio(
    update,
    context
):

    path = context.user_data.get(
        "static_video_path"
    )

    job_id = context.user_data.get(
        "static_video_job"
    )

    if (
        not path
        or not os.path.exists(path)
    ):

        clear_static_state(
            context,
            False
        )

        await update.message.reply_text(
            "❌ الملف انتهى أو مش موجود.\n"
            "🔄 ابعت الرابط من جديد."
        )

        return

    msg = await update.message.reply_text(
        "🎵 جاري استخراج الصوت..."
    )

    audio_path = os.path.join(
        DOWNLOAD_DIR,
        f"{job_id}_static.mp3"
    )

    try:

        command = [
            "ffmpeg",
            "-y",
            "-i",
            path,
            "-vn",
            "-codec:a",
            "libmp3lame",
            "-b:a",
            "192k",
            audio_path,
        ]

        result = await asyncio.to_thread(
            subprocess.run,
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=180
        )

        if (
            result.returncode != 0
            or not os.path.exists(audio_path)
            or os.path.getsize(audio_path) <= 0
        ):

            raise Exception(
                "فشل استخراج الصوت."
            )

        with open(
            audio_path,
            "rb"
        ) as audio:

            await update.message.reply_audio(
                audio=audio,
                filename="audio.mp3",
                read_timeout=180,
                write_timeout=180,
                connect_timeout=60,
                pool_timeout=60
            )

        user_id = str(
            update.effective_user.id
        )

        if user_id in users:

            users[user_id]["downloads"] = (
                users[user_id].get(
                    "downloads",
                    0
                ) + 1
            )

        add_history(
            user_id,
            "TikTok Static Video",
            "🎵 تيك توك",
            "🎵 MP3"
        )

        save_users(users)

        stats["downloads"] += 1
        stats["audio_downloads"] += 1

        save_stats(stats)

        try:
            await msg.delete()
        except Exception:
            pass

        await update.message.reply_text(
            "✅ تم استخراج الأغنية بنجاح 🎵❤️"
        )

        await maybe_send_ad(update, context)

        context.user_data.pop(
            "download_url",
            None
        )

    except Exception as e:

        stats["errors"] += 1
        save_stats(stats)

        print(
            "❌ STATIC AUDIO ERROR:",
            type(e).__name__,
            ":",
            e
        )

        try:

            await msg.edit_text(
                "❌ حصل خطأ أثناء استخراج الأغنية."
            )

        except Exception:
            pass

    finally:

        try:

            if os.path.exists(audio_path):
                os.remove(audio_path)

        except Exception:
            pass

        clear_static_state(
            context,
            True
        )


# ============================================================
# صورة فقط من الفيديو الثابت
# ============================================================

@download_guard
async def send_static_image(
    update,
    context
):

    path = context.user_data.get(
        "static_video_path"
    )

    job_id = context.user_data.get(
        "static_video_job"
    )

    if (
        not path
        or not os.path.exists(path)
    ):

        clear_static_state(
            context,
            False
        )

        await update.message.reply_text(
            "❌ الملف انتهى أو مش موجود.\n"
            "🔄 ابعت الرابط من جديد."
        )

        return

    msg = await update.message.reply_text(
        "🖼️ جاري استخراج الصورة من الفيديو..."
    )

    image_path = os.path.join(
        DOWNLOAD_DIR,
        f"{job_id}_image.jpg"
    )

    try:

        command = [
            "ffmpeg",
            "-y",
            "-i",
            path,
            "-frames:v",
            "1",
            "-q:v",
            "2",
            image_path,
        ]

        result = await asyncio.to_thread(
            subprocess.run,
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=120
        )

        if (
            result.returncode != 0
            or not os.path.exists(image_path)
            or os.path.getsize(image_path) <= 0
        ):

            raise Exception(
                "فشل استخراج الصورة."
            )

        with open(
            image_path,
            "rb"
        ) as photo:

            await update.message.reply_photo(
                photo=photo,
                caption="🖼️ الصورة المستخرجة من الفيديو"
            )

        user_id = str(
            update.effective_user.id
        )

        if user_id in users:

            users[user_id]["downloads"] = (
                users[user_id].get(
                    "downloads",
                    0
                ) + 1
            )

        add_history(
            user_id,
            "TikTok Static Video",
            "🎵 تيك توك",
            "🖼️ صورة"
        )

        save_users(users)

        stats["downloads"] += 1

        save_stats(stats)

        try:
            await msg.delete()
        except Exception:
            pass

        await update.message.reply_text(
            "✅ تم استخراج الصورة من الفيديو بنجاح 🖼️❤️"
        )

        await maybe_send_ad(update, context)

        context.user_data.pop(
            "download_url",
            None
        )

    except Exception as e:

        stats["errors"] += 1
        save_stats(stats)

        print(
            "❌ STATIC IMAGE ERROR:",
            type(e).__name__,
            ":",
            e
        )

        try:

            await msg.edit_text(
                "❌ حصل خطأ أثناء استخراج الصورة."
            )

        except Exception:
            pass

    finally:

        try:

            if os.path.exists(image_path):
                os.remove(image_path)

        except Exception:
            pass

        clear_static_state(
            context,
            True
        )


# ============================================================
# فيديو للفيديو الثابت
# ============================================================

@download_guard
async def send_static_video(
    update,
    context
):

    path = context.user_data.get(
        "static_video_path"
    )

    job_id = context.user_data.get(
        "static_video_job"
    )

    if (
        not path
        or not os.path.exists(path)
    ):

        clear_static_state(
            context,
            False
        )

        await update.message.reply_text(
            "❌ الملف انتهى أو مش موجود.\n"
            "🔄 ابعت الرابط من جديد."
        )

        return

    msg = await update.message.reply_text(
        "🎬 جاري إرسال الفيديو..."
    )

    try:

        with open(
            path,
            "rb"
        ) as video:

            await update.message.reply_video(
                video=video,
                read_timeout=180,
                write_timeout=180,
                connect_timeout=60,
                pool_timeout=60
            )

        user_id = str(
            update.effective_user.id
        )

        if user_id in users:

            users[user_id]["downloads"] = (
                users[user_id].get(
                    "downloads",
                    0
                ) + 1
            )

        add_history(
            user_id,
            "TikTok Static Video",
            "🎵 تيك توك",
            "🎬 فيديو"
        )

        save_users(users)

        stats["downloads"] += 1
        stats["videos_downloaded"] += 1

        save_stats(stats)

        try:
            await msg.delete()
        except Exception:
            pass

        await update.message.reply_text(
            "✅ تم إرسال الفيديو بنجاح 🎬❤️"
        )

        await maybe_send_ad(update, context)

        context.user_data.pop(
            "download_url",
            None
        )

    except Exception as e:

        stats["errors"] += 1
        save_stats(stats)

        print(
            "❌ STATIC VIDEO ERROR:",
            type(e).__name__,
            ":",
            e
        )

        try:

            await msg.edit_text(
                "❌ حصل خطأ أثناء إرسال الفيديو."
            )

        except Exception:
            pass

    finally:

        clear_static_state(
            context,
            True
        )


# ============================================================
# القائمة الرئيسية
# ============================================================

async def show_main_menu(
    update,
    context
):

    context.user_data["nav_page"] = "main_menu"
    context.user_data.pop("nav_parent", None)
    user_id = update.effective_user.id

    user_data = users.get(
        str(user_id),
        {}
    )

    name = (
        user_data.get(
            "name",
            ""
        )
        or ""
    ).strip()

    keyboard = [
        ["🚀 انطلق"],
        ["📜 سجل تحميلاتي", "📊 إحصائياتي"],
        ["⚙️ الإعدادات", "❓ طريقة الاستخدام"],
        (["📨 رسائل المستخدمين"] if is_admin(user_id) else ["📞 التواصل مع الإدارة"]),
        ["💬 رسائل من الإدارة"] if not is_admin(user_id) else [],
        ["➡️ التالي", "🏠 القائمة الرئيسية"],
    ]

    keyboard = [row for row in keyboard if row]

    if premium_data.get("enabled", False):
        if is_premium(user_id):
            keyboard.append(["💎 حالة Premium"])
        elif not is_admin(user_id):
            keyboard.append(["💎 الاشتراك في Premium"])

    if is_admin(user_id):

        keyboard.append(
            ["👑 لوحة الأدمن"]
        )

    markup = ReplyKeyboardMarkup(
        keyboard,
        resize_keyboard=True,
        one_time_keyboard=False
    )

    if name:

        await update.message.reply_text(
            f"🚀✨ أهلاً وسهلاً بيك يا **{name}** ✨🚀\n\n"
            "نورت **صاروخ التحميل** 🚀\n"
            "🎬 ابعت الرابط وأنا هتولى الباقي.\n\n"
            "⚡ سريع وبسيط وسهل الاستخدام.",
            reply_markup=markup,
            parse_mode="Markdown"
        )

    else:

        await update.message.reply_text(
            "🚀 أهلاً بيك في صاروخ التحميل!\n\n"
            "⚡ حمّل فيديوهاتك بسرعة وسهولة.\n"
            "🎬 ابعت الرابط وأنا هتولى الباقي.\n\n"
            "👨‍💻 صاروخ التحميل | بواسطة أشرف شوقي\n\n"
            "📥 ابعت الرابط وخلّي الصاروخ ينطلق 🚀",
            reply_markup=markup
        )


# ============================================================
# البداية
# ============================================================

async def start(
    update,
    context
):

    user_id = update.effective_user.id

    is_new_user = register_user(update.effective_user)
    users[str(user_id)]["last_activity"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    save_users(users)
    if is_new_user:
        await notify_admin(context.bot, f"👤 مستخدم جديد دخل الصاروخ\n🆔 ID: {user_id}\n📝 الاسم: {update.effective_user.first_name or 'بدون اسم'}", key=f"new_user_{user_id}", cooldown=0)

    if is_banned(user_id):

        await update.message.reply_text(
            "🚫 تم حظر حسابك من استخدام البوت."
        )

        return

    if (
        maintenance_mode
        and not is_admin(user_id)
    ):

        await update.message.reply_text(
            UPDATE_MESSAGE,
            parse_mode="Markdown"
        )

        return

    if (
        not is_admin(user_id)
        and users.get(
            str(user_id),
            {}
        ).get(
            "name_registered",
            False
        ) is False
    ):

        context.user_data[
            "awaiting_name"
        ] = True

        await update.message.reply_text(
            "🚀 نورت صاروخ التحميل! ✨\n\n"
            "اكتب الاسم اللي تحب نناديك بيه 👤"
        )

        return

    context.user_data.pop(
        "admin_action",
        None
    )

    context.user_data.pop(
        "download_url",
        None
    )

    context.user_data.pop(
        "download_stage",
        None
    )

    clear_static_state(
        context,
        True
    )

    clear_tiktok_photo_state(
        context,
        True
    )

    await show_main_menu(
        update,
        context
    )


# ============================================================
# نظام التواصل بين المستخدمين والإدارة
# ============================================================
def load_contact_messages():
    if not os.path.exists(CONTACT_MESSAGES_FILE): return []
    try:
        with open(CONTACT_MESSAGES_FILE,"r",encoding="utf-8") as f: data=json.load(f)
        return data if isinstance(data,list) else []
    except Exception as e:
        print("⚠️ Contact messages load error:",e); return []

def save_contact_messages(data):
    try:
        with open(CONTACT_MESSAGES_FILE,"w",encoding="utf-8") as f: json.dump(data[-500:],f,ensure_ascii=False,indent=2)
    except Exception as e: print("⚠️ Contact messages save error:",e)

contact_messages=load_contact_messages()

async def delete_admin_message_later(bot, chat_id, message_id, delay=86400):
    try:
        await asyncio.sleep(delay)
        try:
            await bot.delete_message(chat_id=chat_id, message_id=message_id)
        except Exception as e:
            print("⚠️ Auto-delete admin message error:", e)
    except asyncio.CancelledError:
        return

async def user_contact_menu(update, context):
    context.user_data["nav_parent"]="main_menu"; context.user_data["nav_page"]="user_contact"
    context.user_data.pop("contact_mode",None)
    await update.message.reply_text(
        "💙 أهلاً بيك في خدمة التواصل مع الإدارة\n\n"
        "لو عندك مشكلة، استفسار، اقتراح أو محتاج مساعدة، إحنا موجودين علشان نساعدك 🤝🚀\n\n"
        "📩 اضغط الزر التالي، وبعدها ابعت رسالتك أو صورة توضح المشكلة.\n"
        "🔔 رسالتك هتوصل للإدارة مباشرة وهيتم الرد عليك هنا.",
        reply_markup=ReplyKeyboardMarkup([["👨‍💼 تواصل مع الأدمن"],["🔙 رجوع","🏠 القائمة الرئيسية"]],resize_keyboard=True)
    )

async def user_admin_messages(update, context):
    uid=update.effective_user.id
    now=time.time()
    # رسائل الإدارة محفوظة داخل contact_messages في قسم outbox، وتظل ظاهرة 24 ساعة فقط.
    items=[]
    changed=False
    for rec in contact_messages:
        if rec.get("type") not in ("admin_text","admin_photo"): continue
        if int(rec.get("user_id",0)) != int(uid): continue
        if now-float(rec.get("created_at",0)) <= 86400:
            items.append(rec)
        elif rec.get("status") != "expired":
            rec["status"]="expired"; changed=True
    if changed: save_contact_messages(contact_messages)
    items=items[-20:]
    if not items:
        await update.message.reply_text("📭 مفيش رسائل من الإدارة خلال آخر 24 ساعة.",reply_markup=ReplyKeyboardMarkup([["🔙 رجوع","🏠 القائمة الرئيسية"]],resize_keyboard=True))
        return
    lines=["💬 رسائل من الإدارة\n", "⏳ الرسائل بتختفي تلقائيًا بعد 24 ساعة.\n"]
    for rec in items:
        when=time.strftime("%Y-%m-%d %H:%M",time.localtime(float(rec.get("created_at",now))))
        if rec.get("type")=="admin_photo":
            lines.append(f"\n📷 صورة من الإدارة — {when}")
            if rec.get("text"): lines.append(f"💬 {rec.get('text')}")
        else:
            lines.append(f"\n💬 رسالة من الإدارة — {when}\n{rec.get('text','')}")
    await update.message.reply_text("\n".join(lines),reply_markup=ReplyKeyboardMarkup([["🔙 رجوع","🏠 القائمة الرئيسية"]],resize_keyboard=True))

async def start_user_contact(update, context):
    context.user_data["contact_mode"]="user_to_admin"
    context.user_data["nav_parent"]="user_contact"; context.user_data["nav_page"]="user_contact_input"
    await update.message.reply_text(
        "✍️ ابعت رسالتك دلوقتي أو أرسل صورة 📷\n\n"
        "📝 تقدر تشرح المشكلة بالتفصيل، وهتوصّل للإدارة مباشرة.\n\n"
        "🔙 للرجوع اضغط رجوع.",
        reply_markup=ReplyKeyboardMarkup([["🔙 رجوع","🏠 القائمة الرئيسية"]],resize_keyboard=True)
    )

async def send_contact_to_admin(update, context):
    user=update.effective_user
    if not context.user_data.get("contact_mode")=="user_to_admin": return False
    msg_id=uuid.uuid4().hex[:12]
    record={"id":msg_id,"user_id":user.id,"name":user.full_name or "بدون اسم","username":user.username or "","created_at":time.time(),"type":"text","text":update.message.text or "","photo_file_id":"","status":"new"}
    if update.message.photo:
        record["type"]="photo"; record["photo_file_id"]=update.message.photo[-1].file_id; record["text"]=update.message.caption or ""
    contact_messages.append(record); save_contact_messages(contact_messages)
    caption=(f"🔔 رسالة جديدة من مستخدم!\n\n👤 الاسم: {record['name']}\n🆔 ID: {user.id}\n🔗 Username: @{record['username'] or 'بدون'}\n\n"+(f"💬 الرسالة:\n{record['text'][:3500]}" if record['text'] else "📷 صورة من المستخدم"))
    kb=InlineKeyboardMarkup([[InlineKeyboardButton("💬 الرد على المستخدم",callback_data=f"contact_reply:{msg_id}")]])
    sent=0
    for oid in owner_ids:
        try:
            if record["type"]=="photo": await context.bot.send_photo(oid,record["photo_file_id"],caption=caption,reply_markup=kb)
            else: await context.bot.send_message(oid,caption,reply_markup=kb)
            sent+=1
        except Exception as e: print("⚠️ Contact owner notify error:",e)
    for aid in admin_ids:
        if has_admin_permission(aid,"contact_inbox"):
            try:
                if record["type"]=="photo": await context.bot.send_photo(aid,record["photo_file_id"],caption=caption,reply_markup=kb)
                else: await context.bot.send_message(aid,caption,reply_markup=kb)
                sent+=1
            except Exception as e: print("⚠️ Contact admin notify error:",e)
    await update.message.reply_text("✅ وصلت رسالتك للإدارة بنجاح.\n💙 هنرد عليك هنا في أقرب وقت.",reply_markup=ReplyKeyboardMarkup([["🔙 رجوع","🏠 القائمة الرئيسية"]],resize_keyboard=True))
    return True

async def admin_contact_menu(update, context):
    uid=update.effective_user.id
    if not (has_admin_permission(uid,"contact_inbox") or has_admin_permission(uid,"contact_user") or has_admin_permission(uid,"contact_broadcast")):
        await update.message.reply_text("❌ مفيش صلاحية للتواصل مع المستخدمين."); return
    context.user_data["nav_parent"]="admin_panel"; context.user_data["nav_page"]="admin_contact"
    kb=[]
    if has_admin_permission(uid,"contact_inbox"): kb.append(["📥 رسائل المستخدمين"])
    if has_admin_permission(uid,"contact_user"): kb.append(["📤 إرسال رسالة لمستخدم"])
    if has_admin_permission(uid,"contact_broadcast"): kb.append(["📢 رسالة جماعية"])
    kb.append(["🔙 رجوع","🏠 القائمة الرئيسية"])
    await update.message.reply_text("📨 رسائل المستخدمين\n\nاختار نوع التواصل:",reply_markup=ReplyKeyboardMarkup(kb,resize_keyboard=True))

async def admin_contact_inbox(update, context):
    uid=update.effective_user.id
    if not await admin_only(update,"contact_inbox"): return
    context.user_data["nav_parent"]="admin_contact"; context.user_data["nav_page"]="admin_contact_inbox"
    new=[x for x in contact_messages if x.get("status")=="new"][-20:]
    if not new:
        await update.message.reply_text("📥 مفيش رسائل جديدة حاليًا.",reply_markup=ReplyKeyboardMarkup([["🔙 رجوع","🏠 القائمة الرئيسية"]],resize_keyboard=True)); return
    kb=[]
    for x in reversed(new): kb.append([f"📩 {x.get('name','بدون اسم')} | {x.get('user_id')}"])
    kb.append(["🔙 رجوع","🏠 القائمة الرئيسية"])
    context.user_data["contact_inbox_map"]={f"📩 {x.get('name','بدون اسم')} | {x.get('user_id')}":x["id"] for x in new}
    context.user_data["admin_action"]="contact_select_message"
    await update.message.reply_text("📥 رسائل المستخدمين الجديدة:",reply_markup=ReplyKeyboardMarkup(kb,resize_keyboard=True))

async def admin_contact_send_start(update, context):
    context.user_data["admin_action"]="contact_find_user"; context.user_data["nav_parent"]="admin_contact"; context.user_data["nav_page"]="admin_contact_find"
    await update.message.reply_text("🔎 اكتب اسم المستخدم أو Telegram ID للبحث:",reply_markup=ReplyKeyboardMarkup([["🔙 رجوع","🏠 القائمة الرئيسية"]],resize_keyboard=True))

async def admin_contact_broadcast_start(update, context):
    context.user_data["admin_action"]="contact_broadcast"; context.user_data["nav_parent"]="admin_contact"; context.user_data["nav_page"]="admin_contact_broadcast"
    await update.message.reply_text("📢 أرسل الرسالة الجماعية أو صورة مع تعليق.\n\nبعدها هتظهر لك شاشة تأكيد قبل الإرسال.",reply_markup=ReplyKeyboardMarkup([["🔙 رجوع","🏠 القائمة الرئيسية"]],resize_keyboard=True))

async def contact_reply_callback(update, context):
    q=update.callback_query; await q.answer()
    if not has_admin_permission(q.from_user.id,"contact_inbox"):
        await q.answer("❌ غير مصرح لك.",show_alert=True); return
    mid=(q.data or "").split(":",1)[-1]
    rec=next((x for x in contact_messages if x.get("id")==mid),None)
    if not rec: await q.message.reply_text("❌ الرسالة غير موجودة."); return
    context.user_data["admin_action"]="contact_reply"; context.user_data["contact_reply_user_id"]=int(rec["user_id"]); context.user_data["nav_page"]="admin_contact_reply"; context.user_data["nav_parent"]="admin_contact"
    rec["status"]="handled"; save_contact_messages(contact_messages)
    await q.message.reply_text(f"💬 الرد على {rec.get('name','المستخدم')} | 🆔 {rec['user_id']}\n\n✍️ ابعت الرسالة أو الصورة:",reply_markup=ReplyKeyboardMarkup([["🔙 رجوع","🏠 القائمة الرئيسية"]],resize_keyboard=True))

# ============================================================
# لوحة الأدمن
# ============================================================

async def admin_panel(update, context):
    if context.user_data.get("nav_page") != "admin_panel":
        context.user_data["nav_parent"] = context.user_data.get("nav_page", "admin_panel")
    context.user_data["nav_page"] = "admin_panel"
    if not await admin_only(update): return
    context.user_data["admin_submenu"]=False
    uid=update.effective_user.id
    keyboard=[]
    if has_admin_permission(uid,"stats"): keyboard.append(["📊 الإحصائيات"])
    if has_admin_permission(uid,"users"): keyboard.append(["👥 المستخدمين"])
    if has_admin_permission(uid,"broadcast"): keyboard.append(["📢 إذاعة رسالة"])
    if has_admin_permission(uid,"moderation"): keyboard.extend([["🚫 حظر مستخدم","✅ فك حظر"],["🔎 معلومات مستخدم","📋 المحظورين"]])
    if has_admin_permission(uid,"ads"): keyboard.append(["📢 الإعلانات"])
    if has_admin_permission(uid,"limits"): keyboard.append(["⚙️ حدود التحميل"])
    if has_admin_permission(uid,"premium"): keyboard.append(["💎 Premium"])
    if has_admin_permission(uid,"maintenance"): keyboard.append(["🛠️ وضع الصيانة"])
    if is_owner(uid): keyboard.append(["👑 إدارة الأدمنز","🔐 صلاحيات الأدمن"])
    if has_admin_permission(uid,"contact_inbox") or has_admin_permission(uid,"contact_user") or has_admin_permission(uid,"contact_broadcast"):
        keyboard.append(["📨 رسائل المستخدمين"])
    if has_admin_permission(uid,"users"): keyboard.append(["👥 أعضاء البوت"])
    if has_admin_permission(uid,"bot_settings"): keyboard.append(["🤖 إعدادات البوت"])
    keyboard.append(["🔙 رجوع","➡️ التالي","🏠 القائمة الرئيسية"])
    await update.message.reply_text("👑 لوحة تحكم صاروخ التحميل\n\nاختار العملية اللي عندك صلاحية تستخدمها:",reply_markup=ReplyKeyboardMarkup(keyboard,resize_keyboard=True))


# ============================================================
# إعدادات حدود التحميل وPremium
# ============================================================

async def admin_limits(update, context):
    if context.user_data.get("nav_page") != "admin_limits":
        context.user_data["nav_parent"] = context.user_data.get("nav_page", "admin_panel")
    context.user_data["nav_page"] = "admin_limits"
    if not await admin_only(update,"limits"): return
    await update.message.reply_text(
        "⚙️ حدود التحميل الحالية\n\n"
        f"👤 العادي: {premium_data.get('normal_limit', 10)} تحميل / {premium_data.get('normal_window_minutes', 30)} دقيقة\n"
        f"💎 Premium: {premium_data.get('premium_limit', 30)} تحميل / {premium_data.get('premium_window_minutes', 30)} دقيقة\n\n"
        "اختار التعديل:",
        reply_markup=ReplyKeyboardMarkup([
            ["🔢 حد العادي", "⏱️ مدة العادي"],
            ["💎 حد Premium", "⏱️ مدة Premium"],
            ["🔙 رجوع"],
        ], resize_keyboard=True)
    )


async def admin_premium(update, context):
    if context.user_data.get("nav_page") != "admin_premium":
        context.user_data["nav_parent"] = context.user_data.get("nav_page", "admin_panel")
    context.user_data["nav_page"] = "admin_premium"
    if not await admin_only(update,"premium"): return
    prices = premium_data.get("prices", {})
    active = sum(1 for uid in list(premium_data.get("subscriptions", {})) if premium_subscription(uid))
    limits = premium_data.get("premium_plan_limits", {})
    def lim(k):
        x=limits.get(k,{})
        return f"{x.get('limit',30)} تحميل / {x.get('window_minutes',30)} دقيقة"
    await update.message.reply_text(
        "💎 إدارة Premium\n\n"
        f"الحالة العامة: {'🟢 مفعّل' if premium_data.get('enabled') else '🔴 متوقف'}\n"
        f"الأسبوع: {prices.get('week', 30)} جنيه — {lim('week')}\n"
        f"الشهر: {prices.get('month', 80)} جنيه — {lim('month')}\n"
        f"السنة: {prices.get('year', 700)} جنيه — {lim('year')}\n"
        f"👥 المشتركين النشطين: {active}\n\n"
        "اختار العملية:",
        reply_markup=ReplyKeyboardMarkup([
            ["🟢 تشغيل Premium", "🔴 إيقاف Premium"],
            ["💰 أسعار Premium", "💳 إعداد الدفع"],
            ["⚙️ حدود الباقات", "🎯 حد مشترك"],
            ["➕ إضافة مشترك", "➖ إزالة مشترك"],
            ["🔎 حالة Premium", "📨 طلبات الدفع"],
            ["📋 مشتركو Premium"],
            ["🔙 رجوع"],
        ], resize_keyboard=True)
    )


async def admin_premium_limits(update, context):
    if context.user_data.get("nav_page") != "admin_premium_limits":
        context.user_data["nav_parent"] = context.user_data.get("nav_page", "admin_panel")
    context.user_data["nav_page"] = "admin_premium_limits"
    if not await admin_only(update,"premium"): return
    limits = premium_data.get("premium_plan_limits", {})
    def line(k,n):
        x=limits.get(k,{})
        return f"{n}: {x.get('limit',30)} تحميل / {x.get('window_minutes',30)} دقيقة"
    await update.message.reply_text(
        "⚙️ حدود تحميل Premium حسب الباقة\n\n"
        f"🗓️ {line('week','الأسبوع')}\n"
        f"📅 {line('month','الشهر')}\n"
        f"📆 {line('year','السنة')}\n\n"
        "اختار الباقة، وبعدها ابعت: عدد التحميلات + عدد الدقائق\n"
        "مثال: 50 60 = 50 تحميل خلال ساعة.",
        reply_markup=ReplyKeyboardMarkup([
            ["💎 حد الأسبوع", "💎 حد الشهر"],
            ["💎 حد السنة"],
            ["🎯 حد مشترك", "🔙 رجوع"],
        ], resize_keyboard=True)
    )


async def admin_custom_premium_limit(update, context):
    if not await admin_only(update,"premium"): return
    await update.message.reply_text(
        "🎯 حد Premium لمشترك معين\n\n"
        "ابعت Telegram ID للمشترك.\n"
        "بعدها هتحدد له الحد الخاص.\n\n"
        "مثال: 2018495286"
    )
    context.user_data["admin_action"]="custom_premium_limit_user"


async def admin_payment_settings(update, context):
    if context.user_data.get("nav_page") != "admin_payment_settings":
        context.user_data["nav_parent"] = context.user_data.get("nav_page", "admin_panel")
    context.user_data["nav_page"] = "admin_payment_settings"
    if not await admin_only(update,"premium"): return
    await update.message.reply_text(
        "💳 إعدادات الدفع Premium\n\n"
        f"طريقة الدفع: {premium_data.get('payment_method', 'غير محددة')}\n"
        f"بيانات الدفع: {premium_data.get('payment_details', 'غير محددة')}\n\n"
        "اختار التعديل:",
        reply_markup=ReplyKeyboardMarkup([
            ["💳 تغيير طريقة الدفع"],
            ["📝 تغيير بيانات الدفع"],
            ["🔙 رجوع"],
        ], resize_keyboard=True)
    )


async def admin_payment_requests(update, context):
    if not await admin_only(update,"premium"): return
    requests_data = premium_data.get("payment_requests", {})
    pending = [(rid, item) for rid, item in requests_data.items() if isinstance(item, dict) and item.get("status") == "pending"]
    if not pending:
        await update.message.reply_text("📨 مفيش طلبات دفع معلقة حاليًا.")
        return
    for rid, item in pending[:20]:
        plan = item.get("plan", "غير محدد")
        price = item.get("price", 0)
        uid = item.get("user_id", "")
        username = item.get("username", "")
        ref = item.get("reference", "غير موجود")
        receipt_file_id = item.get("receipt_file_id")
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ قبول", callback_data=f"payapprove:{rid}"),
            InlineKeyboardButton("❌ رفض", callback_data=f"payreject:{rid}")
        ]])
        request_text = (
            f"📨 طلب دفع Premium\n\n"
            f"🆔 المستخدم: {uid}\n"
            f"👤 اليوزر: @{username or 'بدون'}\n"
            f"📦 الباقة: {plan}\n"
            f"💰 المبلغ: {price} جنيه\n"
            f"🔢 مرجع العملية: {ref}"
        )

        # لو الطلب فيه صورة إيصال محفوظة، نعرض الصورة نفسها داخل طلبات الدفع.
        if receipt_file_id:
            try:
                await update.message.reply_photo(
                    photo=receipt_file_id,
                    caption=request_text,
                    reply_markup=keyboard
                )
                continue
            except Exception as e:
                print("⚠️ Payment receipt display error:", e)

        await update.message.reply_text(
            request_text,
            reply_markup=keyboard
        )


async def admin_permissions_menu(update, context):
    context.user_data["nav_page"]="admin_permissions"; context.user_data["nav_parent"]="admin_manage_admins"
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("❌ المالكين فقط يقدروا يتحكموا في صلاحيات الأدمنز."); return
    lines=["🔐 صلاحيات الأدمنز\n"]
    for uid in admin_ids:
        perms=admin_permissions.get(str(uid),[]); labels=[ADMIN_PERMISSION_NAMES[p] for p in perms if p in ADMIN_PERMISSION_NAMES]
        lines.append(f"🆔 {uid}\n"+(" | ".join(labels) if labels else "❌ بدون صلاحيات")+"\n")
    await update.message.reply_text("\n".join(lines),reply_markup=ReplyKeyboardMarkup([["✏️ تعديل صلاحيات أدمن"],["🔙 رجوع","🏠 القائمة الرئيسية"]],resize_keyboard=True))

async def admin_bot_members(update, context):
    """عرض إحصائيات أعضاء البوت الفعلية من users.json."""
    if context.user_data.get("nav_page") != "admin_bot_members":
        context.user_data["nav_parent"] = context.user_data.get("nav_page", "admin_panel")
    context.user_data["nav_page"] = "admin_bot_members"
    if not await admin_only(update, "users"):
        return

    # الإجمالي = كل من تم تسجيله في قاعدة بيانات البوت.
    total = len(users)
    # المتاحين = الأعضاء المسجلون وغير المحظورين داخل البوت.
    available = sum(1 for u in users.values() if isinstance(u, dict) and not u.get("banned", False))
    # Premium = الاشتراكات النشطة فعلًا الآن.
    premium_active = sum(1 for uid in list(premium_data.get("subscriptions", {}).keys()) if premium_subscription(uid))

    await update.message.reply_text(
        "👥 أعضاء البوت\n\n"
        f"👤 إجمالي المنضمين: {total}\n"
        f"🟢 المتاحين حاليًا: {available}\n"
        f"💎 مشتركي Premium: {premium_active}\n\n"
        "🔄 اضغط تحديث لإعادة حساب الأرقام من قاعدة البيانات.",
        reply_markup=ReplyKeyboardMarkup([
            ["🔄 تحديث أعضاء البوت"],
            ["🔙 رجوع", "➡️ التالي", "🏠 القائمة الرئيسية"],
        ], resize_keyboard=True)
    )


async def admin_bot_settings(update, context):
    if context.user_data.get("nav_page") != "admin_bot_settings":
        context.user_data["nav_parent"] = context.user_data.get("nav_page", "admin_panel")
    context.user_data["nav_page"] = "admin_bot_settings"
    if not await admin_only(update,"bot_settings"): return
    await update.message.reply_text(
        "🤖 إعدادات البوت\n\n"
        "التعديلات هنا بتتطبق فعليًا على إعدادات بوت تيليجرام.\n\n"
        "اختار اللي عايز تغيّره:",
        reply_markup=ReplyKeyboardMarkup([
            ["✏️ تغيير اسم البوت"],
            ["🖼️ تغيير صورة البوت"],
            ["📝 تغيير وصف البوت"],
            ["🔙 رجوع","➡️ التالي","🏠 القائمة الرئيسية"]
        ], resize_keyboard=True)
    )


async def admin_manage_admins(update, context):
    if context.user_data.get("nav_page") != "admin_manage_admins":
        context.user_data["nav_parent"] = context.user_data.get("nav_page", "admin_panel")
    context.user_data["nav_page"] = "admin_manage_admins"
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("❌ إدارة الأدمنز متاحة للمالك الأساسي فقط.")
        return
    lines = ["👑 المالكين: "+", ".join(str(x) for x in owner_ids)]
    if admin_ids:
        lines.append("\n🛡️ الأدمنز الإضافيين:")
        for uid in admin_ids:
            u = users.get(str(uid), {})
            uname = u.get("username", "")
            lines.append(f"• {uid} | @{uname}" if uname else f"• {uid}")
    else:
        lines.append("\n🛡️ لا يوجد أدمنز إضافيين.")
    await update.message.reply_text(
        "\n".join(lines),
        reply_markup=ReplyKeyboardMarkup([
            ["➕ إضافة أدمن", "➖ حذف أدمن"],
            ["👑 إضافة مالك", "➖ حذف مالك"],
            ["📋 قائمة الأدمنز"],
            ["🔐 صلاحيات الأدمن"],
            ["🔙 رجوع", "➡️ التالي", "🏠 القائمة الرئيسية"],
        ], resize_keyboard=True)
    )


async def admin_premium_list(update, context):
    if context.user_data.get("nav_page") != "admin_premium_list":
        context.user_data["nav_parent"] = context.user_data.get("nav_page", "admin_panel")
    context.user_data["nav_page"] = "admin_premium_list"
    if not await admin_only(update,"premium"): return
    lines=[]
    for uid, item in list(premium_data.get("subscriptions", {}).items()):
        if premium_subscription(uid):
            exp=time.strftime("%Y-%m-%d %H:%M", time.localtime(float(item.get("expires_at", 0))))
            lim,win,custom=get_premium_limit_config(uid)
            tag="🎯 مخصص" if custom else "💎 حد الباقة"
            lines.append(f"🆔 {uid} | {premium_plan_display(item.get('plan'))} | {tag}: {lim}/{win}د | ينتهي {exp}")
    await update.message.reply_text("📋 مشتركو Premium النشطون:\n\n" + ("\n".join(lines) if lines else "لا يوجد مشتركون حاليًا."))


async def admin_premium_prices(update, context):
    if context.user_data.get("nav_page") != "admin_premium_prices":
        context.user_data["nav_parent"] = context.user_data.get("nav_page", "admin_panel")
    context.user_data["nav_page"] = "admin_premium_prices"
    if not await admin_only(update,"premium"): return
    p=premium_data.get("prices", {})
    await update.message.reply_text(
        f"⭐ أسعار Premium الحالية:\nالأسبوع: {p.get('week',30)} ⭐\nالشهر: {p.get('month',80)} ⭐\nالسنة: {p.get('year',700)} ⭐\n\n"
        "ابعت أسعار Stars بالترتيب: الأسبوع الشهر السنة\nمثال: 30 80 700"
    )
    context.user_data["admin_action"]="premium_prices"


# ============================================================
# إحصائيات الأدمن
# ============================================================

async def admin_stats(update, context):
    if context.user_data.get("nav_page") != "admin_stats":
        context.user_data["nav_parent"] = context.user_data.get("nav_page", "admin_panel")
    context.user_data["nav_page"] = "admin_stats"
    if not await admin_only(update,"stats"): return
    ensure_daily_stats()
    total_users=len(users)
    banned_users=sum(1 for u in users.values() if u.get("banned",False))
    top_sites=sorted((stats.get("site_counts",{}) or {}).items(),key=lambda x:x[1],reverse=True)[:5]
    top_types=sorted((stats.get("type_counts",{}) or {}).items(),key=lambda x:x[1],reverse=True)[:5]
    sites="\n".join(f"• {k}: {v}" for k,v in top_sites) or "• لا توجد بيانات اليوم"
    types="\n".join(f"• {k}: {v}" for k,v in top_types) or "• لا توجد بيانات اليوم"
    await update.message.reply_text(
        "📊 إحصائيات صاروخ التحميل\n\n"
        f"👥 إجمالي المستخدمين: {total_users}\n🆕 مستخدمين جدد اليوم: {stats.get('new_users_today',0)}\n"
        f"🚫 المحظورين: {banned_users}\n📥 إجمالي التحميلات: {stats['downloads']}\n"
        f"📅 تحميلات اليوم: {stats.get('downloads_today',0)}\n🎬 تحميلات الفيديو: {stats['videos_downloaded']}\n"
        f"🎵 تحميلات MP3: {stats['audio_downloads']}\n❌ الأخطاء: {stats['errors']}\n"
        f"📢 ظهور الإعلانات: {ads_data.get('impressions',0)}\n⚡ النشطة الآن: {active_download_count()}/{MAX_CONCURRENT_DOWNLOADS}\n\n"
        "🌐 أكثر المواقع استخدامًا اليوم:\n"+sites+"\n\n📦 أنواع التحميل اليوم:\n"+types+"\n\n"
        f"🛠️ وضع الصيانة: {'مفعل 🔴' if maintenance_mode else 'متوقف 🟢'}\n💎 Premium: {'مفعّل 🟢' if premium_data.get('enabled') else 'متوقف 🔴'}\n👤 حد العادي: {premium_data.get('normal_limit',10)}/{premium_data.get('normal_window_minutes',30)} دقيقة\n💎 حد Premium: {premium_data.get('premium_limit',30)}/{premium_data.get('premium_window_minutes',30)} دقيقة"
    )


# ============================================================
# المستخدمين
# ============================================================

async def admin_users(
    update,
    context
):
    if context.user_data.get("nav_page") != "admin_users":
        context.user_data["nav_parent"] = context.user_data.get("nav_page", "admin_panel")
    context.user_data["nav_page"] = "admin_users"

    if not await admin_only(update):
        return

    if not users:

        await update.message.reply_text(
            "👥 مفيش مستخدمين مسجلين لسه."
        )

        return

    text = "👥 قائمة المستخدمين:\n\n"

    for i, user in enumerate(
        users.values(),
        start=1
    ):

        name = (
            user.get("name")
            or user.get("first_name")
            or "بدون اسم"
        )

        username = user.get(
            "username",
            ""
        )

        username_text = (
            f"@{username}"
            if username
            else "بدون يوزرنيم"
        )

        status = (
            "🚫 محظور"
            if user.get("banned")
            else "🟢 نشط"
        )

        text += (
            f"👤 المستخدم رقم {i}\n"
            f"📝 الاسم: {name}\n"
            f"🆔 المعرف: {user['id']}\n"
            f"📱 اليوزر: {username_text}\n"
            f"📥 التحميلات: {user.get('downloads', 0)}\n"
            f"🕒 آخر نشاط: {user.get('last_activity', 'غير مسجل')}\n"
            f"🔗 آخر رابط: {str(user.get('last_url', 'غير مسجل'))[:120]}\n"
            f"الحالة: {status}\n"
            f"━━━━━━━━━━━━━━\n\n"
        )

        if len(text) > 3500:

            await update.message.reply_text(
                text
            )

            text = ""

    if text:

        await update.message.reply_text(
            text
        )


# ============================================================
# أوامر الأدمن
# ============================================================

async def admin_user_info(
    update,
    context
):
    if context.user_data.get("nav_page") != "admin_user_info":
        context.user_data["nav_parent"] = context.user_data.get("nav_page", "admin_panel")
    context.user_data["nav_page"] = "admin_user_info"

    if not await admin_only(update):
        return

    context.user_data[
        "admin_action"
    ] = "user_info"

    await update.message.reply_text(
        "🔎 ابعتلي Telegram ID الخاص بالمستخدم."
    )


async def admin_ban(
    update,
    context
):
    if context.user_data.get("nav_page") != "admin_ban":
        context.user_data["nav_parent"] = context.user_data.get("nav_page", "admin_panel")
    context.user_data["nav_page"] = "admin_ban"

    if not await admin_only(update):
        return

    context.user_data[
        "admin_action"
    ] = "ban"

    await update.message.reply_text(
        "🚫 ابعت Telegram ID للمستخدم اللي عايز تحظره."
    )


async def admin_unban(
    update,
    context
):
    if context.user_data.get("nav_page") != "admin_unban":
        context.user_data["nav_parent"] = context.user_data.get("nav_page", "admin_panel")
    context.user_data["nav_page"] = "admin_unban"

    if not await admin_only(update):
        return

    context.user_data[
        "admin_action"
    ] = "unban"

    await update.message.reply_text(
        "✅ ابعت Telegram ID للمستخدم اللي عايز تفك حظره."
    )


async def admin_banned(
    update,
    context
):
    if context.user_data.get("nav_page") != "admin_banned":
        context.user_data["nav_parent"] = context.user_data.get("nav_page", "admin_panel")
    context.user_data["nav_page"] = "admin_banned"

    if not await admin_only(update):
        return

    banned = [
        user
        for user in users.values()
        if user.get("banned", False)
    ]

    if not banned:

        await update.message.reply_text(
            "📋 مفيش أي مستخدمين محظورين."
        )

        return

    text = "📋 المستخدمين المحظورين:\n\n"

    for user in banned:

        name = (
            user.get("name")
            or user.get("first_name")
            or "بدون اسم"
        )

        username = user.get(
            "username",
            ""
        )

        username_text = (
            f"@{username}"
            if username
            else "بدون يوزرنيم"
        )

        text += (
            f"👤 {name}\n"
            f"🆔 {user['id']}\n"
            f"📛 {username_text}\n\n"
        )

    await update.message.reply_text(
        text
    )


async def admin_broadcast(
    update,
    context
):
    if context.user_data.get("nav_page") != "admin_broadcast":
        context.user_data["nav_parent"] = context.user_data.get("nav_page", "admin_panel")
    context.user_data["nav_page"] = "admin_broadcast"

    if not await admin_only(update):
        return

    context.user_data[
        "admin_action"
    ] = "broadcast"

    await update.message.reply_text(
        "📢 ابعت الرسالة اللي عايز أرسلها لكل المستخدمين."
    )


# ============================================================
# تنفيذ أوامر الأدمن
# ============================================================

def admin_permission_keyboard(selected):
    selected=set(selected or []); rows=[]
    items=list(ADMIN_PERMISSION_NAMES.items())
    for i in range(0,len(items),2):
        row=[]
        for key,label in items[i:i+2]: row.append(("✅ " if key in selected else "⬜ ")+label)
        rows.append(row)
    rows.append(["☑️ كل الصلاحيات"])
    rows.append(["💾 حفظ الصلاحيات"])
    rows.append(["🔙 رجوع","🏠 القائمة الرئيسية"])
    return rows

PERMISSION_BUTTON_TO_KEY={(("✅ " if False else "" )+label):key for key,label in ADMIN_PERMISSION_NAMES.items()}
for _k,_label in list(ADMIN_PERMISSION_NAMES.items()):
    PERMISSION_BUTTON_TO_KEY["⬜ "+_label]=_k; PERMISSION_BUTTON_TO_KEY["✅ "+_label]=_k

async def handle_admin_action(
    update,
    context,
    text
):

    action = context.user_data.get(
        "admin_action"
    )

    if not action:
        return False

    if not is_admin(
        update.effective_user.id
    ):

        context.user_data.pop(
            "admin_action",
            None
        )

        return False

    if action == "user_info":

        lookup = text.strip().lstrip("@")
        user_id = None
        if lookup.isdigit():
            user_id = int(lookup)
            user = users.get(str(user_id))
        else:
            user = next(
                (u for u in users.values() if str(u.get("username", "")).lower() == lookup.lower()),
                None
            )
            if user:
                user_id = user.get("id")

        if user_id is None:
            await update.message.reply_text("❌ ابعت Telegram ID أو Username صحيح.")
            return True

        if not user:

            await update.message.reply_text(
                "❌ المستخدم مش موجود في قاعدة بيانات البوت."
            )

            context.user_data.pop(
                "admin_action",
                None
            )

            return True

        name = (
            user.get("name")
            or user.get("first_name")
            or "بدون اسم"
        )

        username = user.get(
            "username",
            ""
        )

        username_text = (
            f"@{username}"
            if username
            else "بدون يوزرنيم"
        )

        await update.message.reply_text(
            "🔎 معلومات المستخدم\n\n"
            f"👤 الاسم: {name}\n"
            f"👤 اليوزر: {username_text}\n"
            f"🆔 ID: {user['id']}\n"
            f"📥 التحميلات: {user.get('downloads', 0)}\n"
            f"🕒 آخر نشاط: {user.get('last_activity', 'غير مسجل')}\n"
            f"🔗 آخر رابط: {str(user.get('last_url', 'غير مسجل'))[:200]}\n"
            f"🚫 الحالة: "
            f"{'محظور' if user.get('banned') else 'نشط'}"
        )

        context.user_data.pop(
            "admin_action",
            None
        )

        return True

    if action == "ban":

        try:
            user_id = int(text)
        except ValueError:

            await update.message.reply_text(
                "❌ ابعت Telegram ID صحيح."
            )

            return True

        if user_id == ADMIN_ID:

            await update.message.reply_text(
                "❌ مينفعش تحظر الأدمن نفسه 😄"
            )

            context.user_data.pop(
                "admin_action",
                None
            )

            return True

        if str(user_id) not in users:

            users[str(user_id)] = {
                "id": user_id,
                "first_name": "",
                "username": "",
                "name": "",
                "name_registered": False,
                "banned": True,
                "downloads": 0,
                "history": [],
                "default_quality": "🔥 أعلى جودة",
            }

        else:

            users[str(user_id)]["banned"] = True

        save_users(users)

        await update.message.reply_text(
            f"🚫 تم حظر المستخدم:\n{user_id}"
        )

        context.user_data.pop(
            "admin_action",
            None
        )

        return True

    if action == "unban":

        try:
            user_id = int(text)
        except ValueError:

            await update.message.reply_text(
                "❌ ابعت Telegram ID صحيح."
            )

            return True

        if str(user_id) not in users:

            await update.message.reply_text(
                "❌ المستخدم مش موجود."
            )

            context.user_data.pop(
                "admin_action",
                None
            )

            return True

        users[str(user_id)]["banned"] = False

        save_users(users)

        await update.message.reply_text(
            f"✅ تم فك حظر المستخدم:\n{user_id}"
        )

        context.user_data.pop(
            "admin_action",
            None
        )

        return True

    if action == "ad_text":

        ad_text = str(text).strip()

        if not ad_text:
            await update.message.reply_text("❌ نص الإعلان مينفعش يكون فاضي.")
            return True

        if len(ad_text) > 1000:
            await update.message.reply_text("❌ نص الإعلان طويل جدًا. الحد الأقصى 1000 حرف.")
            return True

        ads_data["text"] = ad_text
        save_ads(ads_data)
        context.user_data.pop("admin_action", None)
        await update.message.reply_text("✅ تم حفظ نص الإعلان الجديد.")
        return True

    if action == "ad_frequency":

        try:
            frequency = int(str(text).strip())
        except ValueError:
            await update.message.reply_text("❌ ابعت رقم صحيح، مثال: 3")
            return True

        if frequency < 1 or frequency > 1000:
            await update.message.reply_text("❌ الرقم لازم يكون من 1 إلى 1000.")
            return True

        ads_data["frequency"] = frequency
        save_ads(ads_data)
        context.user_data.pop("admin_action", None)
        await update.message.reply_text(
            f"✅ تم ضبط الإعلان ليظهر كل {frequency} تحميلات ناجحة للمستخدم."
        )
        return True

    if action == "normal_limit":
        try: value=int(text.strip())
        except ValueError:
            await update.message.reply_text("❌ ابعت رقم صحيح."); return True
        if not 1 <= value <= 10000:
            await update.message.reply_text("❌ الرقم من 1 إلى 10000."); return True
        premium_data["normal_limit"]=value; save_premium_config(premium_data)
        context.user_data.pop("admin_action", None)
        await update.message.reply_text(f"✅ حد العادي بقى {value} تحميلات."); return True

    if action == "normal_window":
        try: value=int(text.strip())
        except ValueError:
            await update.message.reply_text("❌ ابعت عدد الدقائق."); return True
        if not 1 <= value <= 10080:
            await update.message.reply_text("❌ المدة من دقيقة إلى 7 أيام."); return True
        premium_data["normal_window_minutes"]=value; save_premium_config(premium_data)
        context.user_data.pop("admin_action", None)
        await update.message.reply_text(f"✅ الفترة بقت {value} دقيقة."); return True

    if action in ("premium_plan_limit_week", "premium_plan_limit_month", "premium_plan_limit_year"):
        parts=text.replace(","," ").split()
        try:
            if len(parts)!=2: raise ValueError
            value=int(parts[0]); window=int(parts[1])
            if not 1 <= value <= 100000 or not 1 <= window <= 10080: raise ValueError
        except ValueError:
            await update.message.reply_text("❌ الصيغة غلط. ابعت رقمين: عدد التحميلات ثم الدقائق. مثال: 50 60")
            return True
        key=action.rsplit("_",1)[-1]
        premium_data.setdefault("premium_plan_limits",{})[key]={"limit":value,"window_minutes":window}
        save_premium_config(premium_data)
        context.user_data.pop("admin_action",None)
        names={"week":"الأسبوع","month":"الشهر","year":"السنة"}
        await update.message.reply_text(f"✅ تم حفظ حد {names[key]}.\n💎 {value} تحميل / {window} دقيقة")
        return True

    if action == "custom_premium_limit_user":
        uid=text.strip()
        if not uid.isdigit():
            await update.message.reply_text("❌ ابعت Telegram ID رقمي صحيح.")
            return True
        if not premium_subscription(uid):
            await update.message.reply_text("❌ المستخدم ده مشترك Premium نشط حاليًا.")
            context.user_data.pop("admin_action",None)
            return True
        context.user_data["custom_premium_limit_uid"]=uid
        context.user_data["admin_action"]="custom_premium_limit_value"
        current=premium_data.get("premium_custom_limits",{}).get(uid)
        if current:
            current_text=f"الحد الحالي المخصص: {current.get('limit')} تحميل / {current.get('window_minutes')} دقيقة"
        else:
            item=premium_subscription(uid); lim,win,_=get_premium_limit_config(uid)
            current_text=f"الحد الحالي من الباقة ({premium_plan_display(item.get('plan'))}): {lim} تحميل / {win} دقيقة"
        await update.message.reply_text(current_text+"\n\n🎯 ابعت الحد الجديد بالشكل: عدد التحميلات + الدقائق\nمثال: 150 60\n\nولو عايز ترجع المستخدم لحد الباقة، اكتب: إزالة")
        return True

    if action == "custom_premium_limit_value":
        uid=str(context.user_data.get("custom_premium_limit_uid", ""))
        if not uid:
            context.user_data.pop("admin_action",None)
            await update.message.reply_text("❌ انتهت العملية. ابدأ من 🎯 حد مشترك.")
            return True
        if text.strip().lower() in ("إزالة","ازالة","remove","default"):
            premium_data.setdefault("premium_custom_limits",{}).pop(uid,None)
            save_premium_config(premium_data)
            context.user_data.pop("admin_action",None); context.user_data.pop("custom_premium_limit_uid",None)
            lim,win,_=get_premium_limit_config(uid)
            await update.message.reply_text(f"🔄 تم إزالة الحد المخصص للمستخدم {uid}.\n💎 رجع تلقائيًا لحد الباقة: {lim} تحميل / {win} دقيقة.")
            return True
        parts=text.replace(","," ").split()
        try:
            if len(parts)!=2: raise ValueError
            value=int(parts[0]); window=int(parts[1])
            if not 1 <= value <= 100000 or not 1 <= window <= 10080: raise ValueError
        except ValueError:
            await update.message.reply_text("❌ الصيغة غلط. مثال: 150 60")
            return True
        premium_data.setdefault("premium_custom_limits",{})[uid]={"limit":value,"window_minutes":window}
        save_premium_config(premium_data)
        context.user_data.pop("admin_action",None); context.user_data.pop("custom_premium_limit_uid",None)
        await update.message.reply_text(f"✅ تم ضبط حد مخصص للمستخدم {uid}.\n🎯 {value} تحميل / {window} دقيقة")
        return True

    if action == "premium_limit":
        try: value=int(text.strip())
        except ValueError:
            await update.message.reply_text("❌ ابعت رقم صحيح."); return True
        if not 1 <= value <= 10000:
            await update.message.reply_text("❌ الرقم من 1 إلى 10000."); return True
        premium_data["premium_limit"]=value; save_premium_config(premium_data)
        context.user_data.pop("admin_action", None)
        await update.message.reply_text(f"✅ حد Premium بقى {value} تحميلات."); return True

    if action == "premium_window":
        try: value=int(text.strip())
        except ValueError:
            await update.message.reply_text("❌ ابعت عدد الدقائق."); return True
        if not 1 <= value <= 10080:
            await update.message.reply_text("❌ المدة من دقيقة إلى 7 أيام."); return True
        premium_data["premium_window_minutes"]=value; save_premium_config(premium_data)
        context.user_data.pop("admin_action", None)
        await update.message.reply_text(f"✅ فترة Premium بقت {value} دقيقة."); return True

    if action == "premium_prices":
        try:
            a=[float(x) for x in text.replace(","," ").split()]
            if len(a)!=3 or any(x<=0 for x in a): raise ValueError
        except ValueError:
            await update.message.reply_text("❌ اكتب 3 أسعار أكبر من صفر بالترتيب: الأسبوع الشهر السنة."); return True
        premium_data["prices"]={
            "week": int(a[0]) if a[0].is_integer() else a[0],
            "month": int(a[1]) if a[1].is_integer() else a[1],
            "year": int(a[2]) if a[2].is_integer() else a[2],
        }
        save_premium_config(premium_data)
        context.user_data.pop("admin_action", None)
        await update.message.reply_text(
            f"✅ تم حفظ أسعار Premium بالـStars.\n\n🗓️ الأسبوع: {premium_data['prices']['week']} ⭐\n📅 الشهر: {premium_data['prices']['month']} ⭐\n📆 السنة: {premium_data['prices']['year']} ⭐"
        ); return True

    if action == "premium_add":
        parts=text.strip().split()
        if len(parts)<2:
            await update.message.reply_text("❌ اكتب: ID + أسبوع أو شهر أو سنة. مثال: 123456789 شهر"); return True
        uid=parts[0]
        plan=parts[1].lower()
        days={"أسبوع":7,"اسبوع":7,"week":7,"شهر":30,"month":30,"سنة":365,"year":365}.get(plan)
        if not uid.isdigit() or not days:
            await update.message.reply_text("❌ الصيغة: ID ثم أسبوع/شهر/سنة."); return True
        now=time.time(); old=premium_subscription(uid)
        start=max(now, float(old.get("expires_at",now))) if old else now
        premium_data["subscriptions"][uid]={"plan":plan,"started_at":now,"expires_at":start+days*86400,"price":premium_data.get("prices",{}).get("month",0) if days==30 else premium_data.get("prices",{}).get("week",0) if days==7 else premium_data.get("prices",{}).get("year",0)}
        save_premium_config(premium_data); context.user_data.pop("admin_action",None)
        await update.message.reply_text(f"💎 تم تفعيل Premium للمستخدم {uid} لمدة {days} يوم."); return True

    if action == "premium_remove":
        uid=text.strip()
        if not uid.isdigit():
            await update.message.reply_text("❌ ابعت ID صحيح."); return True
        premium_data.get("subscriptions",{}).pop(uid,None); save_premium_config(premium_data)
        context.user_data.pop("admin_action",None)
        await update.message.reply_text(f"✅ تم إلغاء Premium للمستخدم {uid}."); return True

    if action == "premium_status":
        uid=text.strip()
        if not uid.isdigit():
            await update.message.reply_text("❌ ابعت ID صحيح."); return True
        item=premium_subscription(uid)
        if not item:
            await update.message.reply_text("🔴 المستخدم مشترك عادي حاليًا.")
        else:
            remain=max(0,int(float(item.get("expires_at",0))-time.time()))
            await update.message.reply_text(f"💎 Premium مفعّل\n🆔 {uid}\n📦 الباقة: {item.get('plan','')}\n⏳ المتبقي: {format_remaining(remain)}")
        context.user_data.pop("admin_action",None); return True

    if action == "payment_method":
        value=text.strip()
        if not value or len(value)>200:
            await update.message.reply_text("❌ اكتب طريقة دفع صحيحة بحد أقصى 200 حرف."); return True
        premium_data["payment_method"]=value; save_premium_config(premium_data); context.user_data.pop("admin_action",None)
        await update.message.reply_text("✅ تم حفظ طريقة الدفع."); return True

    if action == "payment_details":
        value=text.strip()
        if not value or len(value)>1000:
            await update.message.reply_text("❌ بيانات الدفع غير صالحة أو طويلة جدًا."); return True
        premium_data["payment_details"]=value; save_premium_config(premium_data); context.user_data.pop("admin_action",None)
        await update.message.reply_text("✅ تم حفظ بيانات الدفع."); return True

    if action == "add_owner":
        if not is_primary_owner(update.effective_user.id): context.user_data.pop("admin_action",None); await update.message.reply_text("❌ المالك الأساسي فقط."); return True
        v=text.strip().lstrip("@");
        if not v.isdigit(): await update.message.reply_text("❌ ابعت Telegram ID رقمي."); return True
        uid=int(v)
        if uid in owner_ids: await update.message.reply_text("ℹ️ المستخدم مالك بالفعل."); return True
        owner_ids.append(uid); save_owners(owner_ids)
        if uid in admin_ids: admin_ids.remove(uid); save_admins(admin_ids); admin_permissions.pop(str(uid),None); save_admin_permissions(admin_permissions)

        # إرسال إشعار فوري للمالك الشريك مع لوحة الدخول، إذا كان قد بدأ البوت من قبل.
        try:
            owner_kb = ReplyKeyboardMarkup([
                ["🚀 انطلق"],
                ["📜 سجل تحميلاتي", "📊 إحصائياتي"],
                ["⚙️ الإعدادات", "❓ طريقة الاستخدام"],
                ["💬 رسائل من الإدارة"],
                ["👑 لوحة الأدمن"],
                ["🏠 القائمة الرئيسية"],
            ], resize_keyboard=True)
            await context.bot.send_message(
                chat_id=uid,
                text=(
                    "👑✨ تم تعيينك مالكًا شريكًا في صاروخ التحميل 🚀\n\n"
                    "🔐 أصبح لديك صلاحيات المالك الكاملة.\n"
                    "👑 من هنا تقدر تدخل لوحة الأدمن وتدير البوت حسب صلاحيات المالك.\n\n"
                    "اضغط «👑 لوحة الأدمن» للبدء."
                ),
                reply_markup=owner_kb
            )
        except Exception as e:
            print("⚠️ Owner notify error:", e)

        context.user_data.pop("admin_action",None); await update.message.reply_text(f"👑 تم إضافة المالك الشريك {uid} فعليًا.\n🔐 أصبح لديه صلاحيات المالك.\n📨 تم إرسال رسالة له بالدخول إلى لوحة الأدمن إذا كان قد بدأ البوت من قبل."); return True

    if action == "remove_owner":
        if not is_primary_owner(update.effective_user.id): context.user_data.pop("admin_action",None); await update.message.reply_text("❌ المالك الأساسي فقط."); return True
        v=text.strip().lstrip("@");
        if not v.isdigit(): await update.message.reply_text("❌ ابعت Telegram ID رقمي."); return True
        uid=int(v)
        if uid==ADMIN_ID: await update.message.reply_text("❌ مينفعش حذف المالك الأساسي."); return True
        if uid not in owner_ids: await update.message.reply_text("❌ المستخدم ده مش مالك."); return True
        owner_ids.remove(uid); save_owners(owner_ids); context.user_data.pop("admin_action",None); await update.message.reply_text(f"✅ تم حذف المالك الشريك {uid}."); return True

    if action == "add_admin":
        if not is_owner(update.effective_user.id):
            context.user_data.pop("admin_action",None); await update.message.reply_text("❌ المالكين فقط يقدروا يضيفوا أدمن."); return True
        value=text.strip().lstrip("@")
        if not value.isdigit(): await update.message.reply_text("❌ ابعت Telegram ID رقمي للأدمن الجديد."); return True
        uid=int(value)
        if uid in owner_ids: await update.message.reply_text("ℹ️ المستخدم ده مالك بالفعل."); return True
        if uid in admin_ids: await update.message.reply_text("ℹ️ المستخدم ده أدمن بالفعل."); return True
        context.user_data["pending_admin_id"]=uid; context.user_data["pending_admin_perms"]=[]; context.user_data["admin_action"]="add_admin_permissions"
        await update.message.reply_text("🔐 اختار صلاحيات الأدمن. اضغط الزر لتفعيل/إلغاء الصلاحية، ثم احفظ:",reply_markup=ReplyKeyboardMarkup(admin_permission_keyboard([]),resize_keyboard=True)); return True

    if action == "add_admin_permissions":
        uid=int(context.user_data.get("pending_admin_id",0)); perms=list(context.user_data.get("pending_admin_perms",[]))
        if text=="☑️ كل الصلاحيات":
            context.user_data["pending_admin_perms"]=list(ADMIN_PERMISSION_NAMES.keys())
            await update.message.reply_text("✅ تم تفعيل كل الصلاحيات.",reply_markup=ReplyKeyboardMarkup(admin_permission_keyboard(list(ADMIN_PERMISSION_NAMES.keys())),resize_keyboard=True)); return True
        if text=="💾 حفظ الصلاحيات":
            if not uid: context.user_data.pop("admin_action",None); await update.message.reply_text("❌ انتهت العملية."); return True
            admin_ids.append(uid); save_admins(admin_ids); set_admin_permissions(uid,perms)
            context.user_data.pop("admin_action",None); context.user_data.pop("pending_admin_id",None); context.user_data.pop("pending_admin_perms",None)
            await update.message.reply_text(f"✅ تم إضافة الأدمن {uid} وحفظ صلاحياته فعليًا."); return True
        key=PERMISSION_BUTTON_TO_KEY.get(text)
        if key:
            if key in perms: perms.remove(key)
            else: perms.append(key)
            context.user_data["pending_admin_perms"]=perms
            await update.message.reply_text("🔐 الصلاحيات الحالية: "+("، ".join(ADMIN_PERMISSION_NAMES[x] for x in perms) if perms else "لا توجد") ,reply_markup=ReplyKeyboardMarkup(admin_permission_keyboard(perms),resize_keyboard=True)); return True
        return True

    if action == "remove_admin":
        if not is_owner(update.effective_user.id):
            context.user_data.pop("admin_action",None); await update.message.reply_text("❌ المالك الأساسي فقط يقدر يحذف أدمن."); return True
        value=text.strip().lstrip("@")
        if not value.isdigit():
            await update.message.reply_text("❌ ابعت Telegram ID صحيح."); return True
        uid=int(value)
        if uid == ADMIN_ID:
            await update.message.reply_text("❌ مينفعش حذف المالك الأساسي."); return True
        if uid not in admin_ids:
            await update.message.reply_text("❌ المستخدم ده مش أدمن."); return True
        admin_ids.remove(uid); save_admins(admin_ids); admin_permissions.pop(str(uid),None); save_admin_permissions(admin_permissions); context.user_data.pop("admin_action",None)
        await update.message.reply_text(f"✅ تم حذف الأدمن: {uid}"); return True

    if action == "set_admin_permissions":
        if not is_owner(update.effective_user.id):
            context.user_data.pop("admin_action",None); await update.message.reply_text("❌ المالك الأساسي فقط."); return True
        uid=text.strip()
        if not uid.isdigit() or int(uid) not in admin_ids:
            await update.message.reply_text("❌ ابعت ID لأدمن موجود بالفعل."); return True
        context.user_data["permission_admin_uid"]=uid
        context.user_data["admin_action"]="set_admin_permissions_value"
        await update.message.reply_text("🔐 الصلاحيات المتاحة:\n"+"\n".join(f"{k} = {v}" for k,v in ADMIN_PERMISSION_NAMES.items())+"\n\nمثال: stats users limits premium\nولإزالة كل الصلاحيات اكتب: إزالة")
        return True

    if action == "set_admin_permissions_value":
        uid=str(context.user_data.get("permission_admin_uid",""))
        if not uid or int(uid) not in admin_ids:
            context.user_data.pop("admin_action",None); context.user_data.pop("permission_admin_uid",None); await update.message.reply_text("❌ انتهت العملية."); return True
        if text.strip().lower() in ("إزالة","ازالة","remove","none"): perms=[]
        else:
            perms=[x.strip().lower() for x in text.replace(","," ").split() if x.strip()]
            bad=[x for x in perms if x not in ADMIN_PERMISSION_NAMES]
            if bad:
                await update.message.reply_text("❌ صلاحية غير معروفة: "+", ".join(bad)); return True
        set_admin_permissions(uid,perms)
        context.user_data.pop("admin_action",None); context.user_data.pop("permission_admin_uid",None)
        await update.message.reply_text("✅ تم حفظ صلاحيات الأدمن."); return True

    if action == "bot_name":
        if not has_admin_permission(update.effective_user.id,"bot_settings"): await update.message.reply_text("❌ غير مصرح لك."); return True
        value=text.strip()
        if not value or len(value)>64: await update.message.reply_text("❌ اسم البوت من 1 إلى 64 حرف."); return True
        try:
            await telegram_api_call("setMyName",{"name":value})
            context.user_data.pop("admin_action",None)
            await update.message.reply_text(f"✅ تم تغيير اسم البوت إلى: {value}")
        except Exception as e:
            print("⚠️ Bot name update error:", type(e).__name__, ":", e)
            await update.message.reply_text(f"❌ فشل تغيير اسم البوت.\n📌 السبب: {str(e)[:300]}")
        return True

    if action == "bot_description":
        if not has_admin_permission(update.effective_user.id,"bot_settings"): await update.message.reply_text("❌ غير مصرح لك."); return True
        value=text.strip()
        if len(value)>512: await update.message.reply_text("❌ الوصف حدّه 512 حرف."); return True
        try:
            await telegram_api_call("setMyDescription",{"description":value})
            context.user_data.pop("admin_action",None)
            await update.message.reply_text("✅ تم تغيير وصف البوت.")
        except Exception as e:
            print("⚠️ Bot description update error:", type(e).__name__, ":", e)
            await update.message.reply_text(f"❌ فشل تغيير وصف البوت.\n📌 السبب: {str(e)[:300]}")
        return True

    if action == "broadcast":

        sent = 0
        failed = 0

        await update.message.reply_text(
            "📢 بدأت الإذاعة...\n"
            "⏳ جاري إرسال الرسالة."
        )

        for user_id in list(
            users.keys()
        ):

            user_data = users[user_id]

            if user_data.get(
                "banned",
                False
            ):
                continue

            try:

                await context.bot.send_message(
                    chat_id=int(user_id),
                    text=text
                )

                sent += 1

                await asyncio.sleep(
                    0.05
                )

            except Exception as e:

                failed += 1

                print(
                    f"Broadcast error {user_id}: {e}"
                )

        await update.message.reply_text(
            "📢 انتهت الإذاعة.\n\n"
            f"✅ تم الإرسال: {sent}\n"
            f"❌ فشل الإرسال: {failed}"
        )

        context.user_data.pop(
            "admin_action",
            None
        )

        return True

    return False


# ============================================================
# رسائل الصيانة
# ============================================================

async def send_update_message(
    application
):

    sent = 0
    failed = 0

    print(
        "📢 جاري إرسال رسالة الصيانة..."
    )

    for user_id, user_data in list(
        users.items()
    ):

        if user_data.get(
            "banned",
            False
        ):
            continue

        try:

            await application.bot.send_message(
                chat_id=int(user_id),
                text=UPDATE_MESSAGE,
                parse_mode="Markdown"
            )

            sent += 1

            await asyncio.sleep(
                0.05
            )

        except Exception as e:

            failed += 1

            print(
                f"Maintenance error {user_id}: {e}"
            )

    print(
        f"✅ الصيانة | إرسال: {sent} | فشل: {failed}"
    )


async def send_update_success_message(
    application
):

    global update_pending

    if not update_pending:
        return

    sent = 0
    failed = 0

    for user_id, user_data in list(
        users.items()
    ):

        if user_data.get(
            "banned",
            False
        ):
            continue

        try:

            await application.bot.send_message(
                chat_id=int(user_id),
                text=UPDATE_SUCCESS_MESSAGE,
                parse_mode="Markdown"
            )

            sent += 1

            await asyncio.sleep(
                0.05
            )

        except Exception as e:

            failed += 1

            print(
                f"Update success error {user_id}: {e}"
            )

    update_pending = False

    maintenance_data[
        "update_pending"
    ] = False

    maintenance_data[
        "maintenance_mode"
    ] = False

    save_maintenance(
        maintenance_data
    )

    print(
        f"✅ اكتمال التحديث | إرسال: {sent} | فشل: {failed}"
    )


async def admin_ads(update, context):
    if context.user_data.get("nav_page") != "admin_ads":
        context.user_data["nav_parent"] = context.user_data.get("nav_page", "admin_panel")
    context.user_data["nav_page"] = "admin_ads"

    if not await admin_only(update):
        return

    status = "🟢 مفعلة" if ads_data.get("enabled", False) else "🔴 متوقفة"
    frequency = max(1, int(ads_data.get("frequency", 3)))
    text_preview = str(ads_data.get("text", "")).strip() or "(فارغ)"

    keyboard = [
        ["🟢 تفعيل الإعلانات", "🔴 إيقاف الإعلانات"],
        ["✏️ تغيير نص الإعلان"],
        ["🔢 عدد التحميلات"],
        ["📊 مشاهدات الإعلانات"],
        ["🔙 رجوع"],
    ]

    await update.message.reply_text(
        "📢 إعدادات الإعلانات\n\n"
        f"الحالة: {status}\n"
        f"يظهر الإعلان كل: {frequency} تحميلات ناجحة للمستخدم\n"
        f"عدد مرات الظهور: {ads_data.get('impressions', 0)}\n\n"
        f"النص الحالي:\n{text_preview}",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    )


async def toggle_maintenance(
    update,
    context
):

    global maintenance_mode
    global update_pending

    if not await admin_only(update,"maintenance"):
        return

    if not maintenance_mode:

        maintenance_mode = True
        update_pending = True

        maintenance_data[
            "maintenance_mode"
        ] = True

        maintenance_data[
            "update_pending"
        ] = True

        save_maintenance(
            maintenance_data
        )

        await update.message.reply_text(
            "🛠️ تم تشغيل وضع الصيانة 🔴\n\n"
            "المستخدمين مش هيقدروا يستخدموا التحميل حاليًا.\n\n"
            "🚀 بعد انتهاء التحديث، اقفل وضع الصيانة "
            "علشان البوت يرجع للعمل."
        )

        await send_update_message(
            context.application
        )

    else:

        maintenance_mode = False

        maintenance_data[
            "maintenance_mode"
        ] = False

        maintenance_data[
            "update_pending"
        ] = update_pending

        save_maintenance(
            maintenance_data
        )

        await update.message.reply_text(
            "🟢 تم إيقاف وضع الصيانة.\n\n"
            "🚀 جاري إرسال رسالة اكتمال التحديث..."
        )

        await send_update_success_message(
            context.application
        )

        await update.message.reply_text(
            "✅ تم الانتهاء من إرسال رسالة اكتمال التحديث."
        )


# ============================================================
# قائمة اختيار نوع التحميل
# ============================================================

def media_choice_keyboard():

    return [
        ["🎬 فيديو", "🎵 صوت فقط"],
        ["🖼️ الصورة المصغرة", "ℹ️ معلومات الفيديو"],
        ["🔙 رجوع", "🏠 القائمة الرئيسية"],
    ]


# ============================================================
# قائمة الجودة بالأرقام
# ============================================================

def quality_keyboard():

    return [
        ["2160p", "1440p"],
        ["1080p", "720p"],
        ["480p", "360p"],
        ["🔙 رجوع", "🏠 القائمة الرئيسية"],
    ]


# ============================================================
# معلومات الفيديو
# ============================================================

async def show_video_info(
    update,
    context,
    url
):

    msg = await update.message.reply_text(
        "⏳ جاري جلب معلومات الفيديو..."
    )

    try:

        loop = asyncio.get_running_loop()

        def extract_info():

            options = {
                "quiet": True,
                "no_warnings": True,
                "skip_download": True,
                "noplaylist": True,
                "socket_timeout": 60,
                "retries": 3,
            }

            with yt_dlp.YoutubeDL(
                options
            ) as ydl:

                return ydl.extract_info(
                    url,
                    download=False
                )

        info = await loop.run_in_executor(
            None,
            extract_info
        )

        title = (
            info.get("title")
            or "غير معروف"
        )

        uploader = (
            info.get("uploader")
            or info.get("channel")
            or "غير معروف"
        )

        duration = info.get(
            "duration"
        )

        if duration:

            minutes = int(
                duration // 60
            )

            seconds = int(
                duration % 60
            )

            duration_text = (
                f"{minutes}:{seconds:02d}"
            )

        else:

            duration_text = "غير معروف"

        views = info.get(
            "view_count"
        )

        views_text = (
            f"{views:,}"
            if views is not None
            else "غير متاح"
        )

        await msg.edit_text(
            "ℹ️ معلومات الفيديو\n\n"
            f"🎬 العنوان:\n{title[:100]}\n\n"
            f"👤 صاحب الفيديو:\n{uploader[:80]}\n\n"
            f"⏱️ المدة: {duration_text}\n"
            f"👀 المشاهدات: {views_text}\n"
            f"🌐 الموقع: {detect_site(url)}"
        )

    except Exception as e:

        stats["errors"] += 1
        save_stats(stats)

        print(
            "INFO ERROR:",
            type(e).__name__,
            ":",
            e
        )

        # TikTok يظل على مساره الحالي بدون تغيير.
        # باقي المنصات تحصل على رسالة فشل واضحة بدل ترك المستخدم على
        # "جاري التحميل" أو رسالة التحديث.
        if not is_tiktok:
            error_text = str(e).strip() or "سبب غير معروف"
            if len(error_text) > 700:
                error_text = error_text[:700] + "..."
            try:
                await msg.edit_text(
                    "❌ فشل تحميل الفيديو.\n\n"
                    f"📌 السبب: {error_text}\n\n"
                    "🔗 تأكد أن الرابط متاح ويمكن فتح الفيديو من المتصفح ثم جرّب مرة أخرى."
                )
            except Exception:
                pass
        else:
            try:
                await msg.edit_text(
                    UPDATE_MESSAGE,
                    parse_mode="Markdown"
                )
            except Exception:
                pass


# ============================================================
# الصورة المصغرة
# ============================================================

async def download_thumbnail(
    update,
    context,
    url
):

    msg = await update.message.reply_text(
        "⏳ جاري تجهيز الصورة المصغرة..."
    )

    job_id = uuid.uuid4().hex

    try:

        loop = asyncio.get_running_loop()

        def get_info():

            options = {
                "quiet": True,
                "no_warnings": True,
                "skip_download": True,
                "noplaylist": True,
                "socket_timeout": 60,
                "retries": 3,
            }

            with yt_dlp.YoutubeDL(
                options
            ) as ydl:

                return ydl.extract_info(
                    url,
                    download=False
                )

        info = await loop.run_in_executor(
            None,
            get_info
        )

        thumbnail_url = info.get(
            "thumbnail"
        )

        if not thumbnail_url:

            await msg.edit_text(
                "❌ الصورة المصغرة غير متاحة."
            )

            return

        image_path = os.path.join(
            DOWNLOAD_DIR,
            f"{job_id}.jpg"
        )

        response = await asyncio.to_thread(
            requests.get,
            thumbnail_url,
            impersonate="chrome",
            timeout=45,
            headers=TIKTOK_HEADERS
        )

        if response.status_code != 200:

            raise Exception(
                "فشل تحميل الصورة المصغرة."
            )

        with open(
            image_path,
            "wb"
        ) as f:

            f.write(
                response.content
            )

        if (
            not os.path.exists(image_path)
            or os.path.getsize(image_path) <= 0
        ):

            raise Exception(
                "ملف الصورة فارغ."
            )

        with open(
            image_path,
            "rb"
        ) as photo:

            await update.message.reply_photo(
                photo=photo,
                caption="🖼️ الصورة المصغرة"
            )

        try:
            await msg.delete()
        except Exception:
            pass

    except Exception as e:

        stats["errors"] += 1
        save_stats(stats)

        print(
            "THUMBNAIL ERROR:",
            type(e).__name__,
            ":",
            e
        )

        try:
            # مهم: رسالة الخطأ لازم تكون رسالة فشل فعلية،
            # وليس UPDATE_MESSAGE الخاص بوضع التحديث.
            error_text = str(e).strip() or "خطأ غير معروف."

            # اختصر رسائل yt-dlp الطويلة مع الاحتفاظ بالسبب المهم.
            if "Instagram sent an empty media response" in error_text:
                user_error = (
                    "❌ فشل تحميل فيديو إنستجرام.\n\n"
                    "📌 إنستجرام رفض إرجاع ملف الوسائط لهذا المنشور.\n"
                    "🔐 لو المنشور يحتاج تسجيل دخول، استخدم ملف كوكيز صالح.\n\n"
                    "⚠️ لم يتم ترك التحميل معلّقًا؛ المحاولة انتهت بالفشل."
                )
            elif "DOWNLOAD_CANCELLED" in error_text:
                user_error = "⛔ تم إلغاء التحميل."
            else:
                # لا نعرض كل تفاصيل traceback للمستخدم.
                clean_error = error_text.replace("ERROR:", "").strip()
                if len(clean_error) > 700:
                    clean_error = clean_error[:700] + "…"
                user_error = (
                    "❌ فشل تحميل الفيديو.\n\n"
                    f"📌 السبب: {clean_error}"
                )

            await msg.edit_text(user_error)

        except Exception as message_error:
            print("⚠️ Failed to update video error message:", message_error)

    finally:

        clean_job_files(
            job_id
        )

        _active_job_ids.discard(job_id)


# ============================================================
# تحميل الفيديو
# ============================================================

@download_guard
async def download_video(
    update,
    context,
    url,
    video_format
):

    # Suno له مسار مباشر خاص به؛ لا نمرره إلى yt-dlp.
    if is_suno_url(url):
        return await download_suno_media(update, context, url, "video")

    cached_path, cached_title = get_cached_file(url, "video")
    if cached_path:
        try:
            with open(cached_path, "rb") as cached_video:
                await update.message.reply_video(video=cached_video, read_timeout=180, write_timeout=180, connect_timeout=60, pool_timeout=60)
            user_id = str(update.effective_user.id)
            users[user_id]["downloads"] = users[user_id].get("downloads",0)+1
            add_history(user_id, cached_title or "فيديو محفوظ بالكاش", detect_site(url), "🎬 فيديو")
            save_users(users); stats["downloads"] += 1; stats["videos_downloaded"] += 1; save_stats(stats)
            record_success_stats(detect_site(url), "🎬 فيديو"); clear_error_streak(update.effective_user.id)
            await maybe_send_ad(update, context)
            context.user_data.pop("download_url",None); context.user_data.pop("download_stage",None)
            return
        except Exception as e: print("⚠️ Cache video send failed:",e)

    msg = await update.message.reply_text(
        "⏳ جاري تجهيز الفيديو...\n📡 جاري الاتصال بالمصدر...\n░░░░░░░░░░░░ 0%",
        reply_markup=ReplyKeyboardMarkup([["⛔ إلغاء التحميل"]], resize_keyboard=True)
    )

    job_id = uuid.uuid4().hex
    _active_job_ids.add(job_id)
    context.user_data["active_job_id"] = job_id

    filename = os.path.join(
        DOWNLOAD_DIR,
        f"{job_id}.%(title)s.%(ext)s"
    )

    # TikTok أحيانًا لا يوفر نفس امتدادات mp4/m4a التي يطلبها
    # التنسيق العام، وده كان يسبب: "التنسيق المطلوب غير متوفر".
    # لذلك نستخدم اختيارًا مرنًا لتنسيقات TikTok مع الحفاظ على الجودة.
    if (
        "tiktok.com" in url.lower()
        or "vt.tiktok.com" in url.lower()
    ):
        height_match = re.search(
            r"height<=([0-9]+)",
            video_format or ""
        )
        max_height = (
            height_match.group(1)
            if height_match
            else "2160"
        )

        video_format = (
            f"bv*[height<={max_height}]+ba/"
            f"b[height<={max_height}]/"
            "best"
        )

    if is_instagram_url(url):
        url = clean_instagram_url(url)

    options = {
        "outtmpl": filename,
        "format": video_format,
        "noplaylist": True,
        "quiet": False,
        "socket_timeout": 120,
        "retries": 10,
        "fragment_retries": 10,
        "file_access_retries": 5,
        "extractor_retries": 5,
        "retry_sleep_functions": {"http": lambda n: min(10, 1 + n)},
        "continuedl": True,
        "overwrites": True,
        "restrictfilenames": True,
        "concurrent_fragment_downloads": 4,
        "buffersize": 1024 * 64,
        "http_chunk_size": 10 * 1024 * 1024,
        "check_formats": True,
        "geo_bypass": True,
        "merge_output_format": "mp4",
        "writethumbnail": False,
        "writeinfojson": False,
        "writesubtitles": False,
    }


    if is_instagram_url(url):
        if apply_instagram_session(options, url):
            print("🔐 [إنستغرام] تم تفعيل جلسة الكوكيز المحلية.")
        else:
            print("ℹ️ [إنستغرام] لا يوجد ملف كوكيز؛ سيتم استخدام الوصول العام.")

    loop = asyncio.get_running_loop()

    # ========================================================
    # شريط التقدم
    # ملاحظة: مسار TikTok يستخدم نفس المنطق القديم حرفيًا.
    # التحسينات الجديدة للرقم والتحديثات تخص باقي المنصات فقط.
    # ========================================================
    is_tiktok_url_local = (
        "tiktok.com" in url.lower()
        or "vt.tiktok.com" in url.lower()
    )

    progress_started = [False]

    if is_tiktok_url_local:

        last_percent = [-1]

        def progress_hook(data):
            try:
                if cancel_requested(job_id):
                    raise RuntimeError("DOWNLOAD_CANCELLED")

                if data["status"] == "downloading":
                    progress_started[0] = True
                    total = (
                        data.get("total_bytes")
                        or data.get("total_bytes_estimate")
                    )

                    if total:
                        downloaded = data.get("downloaded_bytes", 0)
                        percent = int(downloaded * 100 / total)

                        if (
                            percent != last_percent[0]
                            and percent % 5 == 0
                        ):
                            last_percent[0] = percent
                            asyncio.run_coroutine_threadsafe(
                                msg.edit_text(
                                    f"⏳ جاري تحميل الفيديو... {percent}%"
                                ),
                                loop
                            )

                elif data["status"] == "finished":
                    asyncio.run_coroutine_threadsafe(
                        msg.edit_text(
                            "✅ اكتمل التحميل 100%\n"
                            "🔎 جاري فحص الفيديو..."
                        ),
                        loop
                    )

            except Exception as e:
                print("⚠️ Progress hook error:", e)

    else:

        last_percent = [-1]
        last_update_time = [0.0]

        def progress_hook(data):
            try:
                if cancel_requested(job_id):
                    raise RuntimeError("DOWNLOAD_CANCELLED")

                status = data.get("status")

                if status == "downloading":
                    progress_started[0] = True
                    total = data.get("total_bytes") or data.get("total_bytes_estimate")
                    downloaded = data.get("downloaded_bytes", 0) or 0
                    now = time.monotonic()

                    if total:
                        percent = max(0, min(100, int(downloaded * 100 / total)))
                        # حدّث الرقم عند كل تغير واضح، وبحد أقصى رسالة كل 0.8 ثانية.
                        if (
                            percent != last_percent[0]
                            and (
                                percent == 0
                                or percent >= 100
                                or now - last_update_time[0] >= 0.8
                                or percent - last_percent[0] >= 2
                            )
                        ):
                            last_percent[0] = percent
                            last_update_time[0] = now
                            asyncio.run_coroutine_threadsafe(
                                msg.edit_text(
                                    f"⏳ جاري تحميل الفيديو... {percent}%"
                                ),
                                loop
                            )
                    else:
                        # بعض المواقع لا ترسل الحجم النهائي؛ نوضح للمستخدم أن التحميل شغال.
                        if now - last_update_time[0] >= 2.0:
                            last_update_time[0] = now
                            asyncio.run_coroutine_threadsafe(
                                msg.edit_text(
                                    "⏳ جاري تحميل الفيديو...\n"
                                    "📥 جاري استلام البيانات..."
                                ),
                                loop
                            )

                elif status == "finished":
                    last_percent[0] = 100
                    last_update_time[0] = time.monotonic()
                    asyncio.run_coroutine_threadsafe(
                        msg.edit_text(
                            "✅ اكتمل التحميل 100%\n"
                            "🔎 جاري فحص الفيديو..."
                        ),
                        loop
                    )

            except Exception as e:
                print("⚠️ Progress hook error:", e)

    progress_state = {"done": False, "label": "جاري تحميل الفيديو...", "percent": None, "downloaded": 0, "total": None, "speed": None, "eta": None}
    original_progress_hook = progress_hook
    def tracked_progress_hook(data):
        try:
            if data.get("status") == "downloading":
                total = data.get("total_bytes") or data.get("total_bytes_estimate")
                downloaded = data.get("downloaded_bytes", 0) or 0
                progress_state.update(total=total, downloaded=downloaded, speed=data.get("speed"), eta=data.get("eta"))
                if total:
                    progress_state["percent"] = int(downloaded * 100 / total)
            elif data.get("status") == "finished":
                progress_state["percent"] = 100
        finally:
            original_progress_hook(data)
    options["progress_hooks"] = [tracked_progress_hook]
    progress_task = asyncio.create_task(monitor_download_progress(job_id, msg, progress_state))

    keep_file = False

    try:

        def download():

            # TikTok: هذا الجزء يظل كما هو تمامًا؛ أي فشل يخرج مباشرة
            # للمسار الحالي الخاص بـ TikTok بدون أي fallback عام.
            is_tiktok_local = (
                "tiktok.com" in url.lower()
                or "vt.tiktok.com" in url.lower()
            )

            if is_tiktok_local:
                with yt_dlp.YoutubeDL(options) as ydl:
                    info = ydl.extract_info(url, download=True)
                    prepared = ydl.prepare_filename(info)
                    return prepared, info

            # باقي المنصات: عدة محاولات بصيغ مرنة، مع الحفاظ على نفس
            # الإعدادات الأصلية قدر الإمكان. ده يمنع فشل الموقع لمجرد أن
            # الصيغة التي اختارها المستخدم غير متاحة عنده.
            attempts = []

            first_options = dict(options)
            attempts.append(first_options)

            fallback_formats = [
                "bv*+ba/b",
                "bestvideo*+bestaudio/best",
                "b[ext=mp4]/b",
                "best",
            ]

            for fmt in fallback_formats:
                opt = dict(options)
                opt["format"] = fmt
                opt["merge_output_format"] = "mp4"
                attempts.append(opt)

            # إنستجرام فقط: لو الوصول العام فشل، نجرب جلسة الكوكيز
            # الموجودة محليًا بصيغ الفيديو المرنة نفسها.
            if is_instagram_url(url):
                session_base = dict(options)
                session_base["format"] = "bv*+ba/bv*/b[ext=mp4]/b/best"
                session_base["merge_output_format"] = "mp4"
                if apply_instagram_session(session_base, url):
                    attempts.append(session_base)
                    for fmt in ("bv*+ba/b", "bestvideo*+bestaudio/best", "b[ext=mp4]/b", "best"):
                        opt = dict(session_base)
                        opt["format"] = fmt
                        attempts.append(opt)
                    print("🔐 [إنستغرام] تم تجهيز محاولات التحميل بجلسة الكوكيز.")
                else:
                    print("ℹ️ [إنستغرام] لا يوجد ملف كوكيز محلي صالح؛ سيتم الاعتماد على الوصول العام.")

            last_error = None
            for index, attempt_options in enumerate(attempts, 1):
                try:
                    # لو الاستخراج أخذ وقت طويل أو فشل بعد رسالة التجهيز،
                    # المستخدم يشوف أن البوت مازال بيحاول وليس متوقفًا.
                    if index > 1:
                        try:
                            asyncio.run_coroutine_threadsafe(
                                msg.edit_text(
                                    f"⏳ جاري محاولة تحميل الفيديو...\n🔄 المحاولة {index}/{len(attempts)}"
                                ),
                                loop
                            )
                        except Exception:
                            pass
                    with yt_dlp.YoutubeDL(attempt_options) as ydl:
                        info = ydl.extract_info(url, download=True)
                        prepared = ydl.prepare_filename(info)
                        return prepared, info
                except Exception as exc:
                    last_error = exc
                    print(f"⚠️ محاولة تحميل {index}/{len(attempts)} فشلت: {exc}")
                    continue

            # Instagram fallback: لو الـExtractor فشل بسبب Empty media response،
            # نحاول قراءة رابط الفيديو من صفحة الـReel نفسها قبل إعلان الفشل.
            if is_instagram_url(url):
                try:
                    print("🔄 [إنستجرام] تجربة المسار المباشر من صفحة الـReel والـJSON المضمّن...")
                    return instagram_direct_download(url, job_id, progress_hook)
                except Exception as direct_error:
                    print(f"⚠️ [إنستجرام] المسار المباشر فشل: {direct_error}")
                    last_error = direct_error

            raise last_error if last_error else Exception("فشل تحميل الفيديو.")

        original_path, info = (
            await loop.run_in_executor(None, download)
        )
        progress_state["done"] = True
        progress_state["percent"] = 100

        file_path = find_job_file(
            job_id,
            original_path
        )

        if (
            not file_path
            or not os.path.exists(file_path)
            or not is_video_file(file_path)
        ):

            is_tiktok = (
                "tiktok.com" in url.lower()
                or "vt.tiktok.com" in url.lower()
            )

            if is_tiktok:

                print(
                    "📸 الناتج ليس فيديو."
                )

                try:

                    resolved = await asyncio.to_thread(
                        resolve_tiktok_url,
                        url
                    )

                    if "/photo/" in resolved.lower():

                        try:
                            await msg.delete()
                        except Exception:
                            pass

                        await download_tiktok_photo_post(
                            update,
                            context,
                            url
                        )

                        return

                except Exception as photo_error:

                    print(
                        "⚠️ Photo fallback error:",
                        photo_error
                    )

            raise Exception(
                "لم يتم الحصول على ملف فيديو صالح."
            )

        # ====================================================
        # فيديو عادي
        # ====================================================

        send_path = await send_video_file(update, file_path, job_id)

        save_cached_file(send_path, url, "video", "mp4", info.get("title", "فيديو"))

        try:
            await msg.delete()
        except Exception:
            pass

        user_id = str(
            update.effective_user.id
        )

        if user_id in users:

            users[user_id]["downloads"] = (
                users[user_id].get(
                    "downloads",
                    0
                ) + 1
            )

        title = (
            info.get(
                "title",
                "فيديو"
            )
            or "فيديو"
        )

        add_history(
            user_id,
            title,
            detect_site(url),
            "🎬 فيديو"
        )

        save_users(users)

        stats["downloads"] += 1
        stats["videos_downloaded"] += 1

        save_stats(stats)
        record_success_stats(detect_site(url), "🎬 فيديو")
        clear_error_streak(update.effective_user.id)

        await maybe_send_ad(update, context)

        context.user_data.pop(
            "download_url",
            None
        )

        context.user_data.pop(
            "download_stage",
            None
        )

    except Exception as e:

        if "DOWNLOAD_CANCELLED" in str(e):
            try: await msg.edit_text("⛔ تم إلغاء التحميل.")
            except Exception: pass
            return

        is_tiktok = (
            "tiktok.com" in url.lower()
            or "vt.tiktok.com" in url.lower()
        )

        if is_tiktok:

            try:

                resolved = await asyncio.to_thread(
                    resolve_tiktok_url,
                    url
                )

                if "/photo/" in resolved.lower():

                    print(
                        "📸 فشل تنزيله كفيديو، جاري تجربة Photo..."
                    )

                    try:
                        await msg.delete()
                    except Exception:
                        pass

                    await download_tiktok_photo_post(
                        update,
                        context,
                        url
                    )

                    return

            except Exception as fallback_error:

                print(
                    "⚠️ TikTok Photo fallback failed:",
                    fallback_error
                )

        stats["errors"] += 1
        save_stats(stats)
        streak = record_error_streak(update.effective_user.id)
        if streak >= ERROR_ALERT_THRESHOLD:
            await notify_admin(
                context.bot,
                f"⚠️ أخطاء تحميل متكررة\n👤 المستخدم: {update.effective_user.id}\n🔢 الأخطاء المتتالية: {streak}\n🌐 الموقع: {detect_site(url)}",
                key=f"error_{update.effective_user.id}"
            )

        print(
            "❌ VIDEO ERROR:",
            type(e).__name__,
            ":",
            e
        )

        # مهم: لازم المستخدم يشوف الخطأ الحقيقي، وماينفعش يفضل على رسالة التجهيز
        # لو Telegram رفض تعديل الرسالة القديمة. نجرب التعديل مرتين، ولو فشل
        # نرسل رسالة خطأ جديدة بدل ما نسيب الحالة معلقة.
        error_text = str(e).strip() or "خطأ غير معروف."
        if "Instagram sent an empty media response" in error_text or "Empty media response" in error_text:
            user_error = (
                "❌ فشل تحميل فيديو إنستجرام.\n\n"
                "📌 إنستجرام لم يُرجع ملف الوسائط لهذا المنشور.\n"
                "🔐 لو الفيديو متاح فقط بعد تسجيل الدخول، لازم ملف كوكيز إنستجرام صالح.\n\n"
                "⚠️ انتهت المحاولات وتم إيقاف التحميل."
            )
        elif "لم يتم العثور على رابط الفيديو المباشر" in error_text:
            user_error = (
                "❌ إنستجرام لم يُرجع رابط الفيديو.\n\n"
                "📌 جرّب Reel عام آخر، أو استخدم cookies لحساب قادر على مشاهدة المنشور."
            )
        elif "Unable to download webpage" in error_text:
            clean_error = error_text.replace("ERROR:", "").strip()
            if len(clean_error) > 700:
                clean_error = clean_error[:700] + "…"
            user_error = f"❌ فشل الاتصال بالمصدر.\n\n📌 السبب: {clean_error}"
        else:
            clean_error = error_text.replace("ERROR:", "").strip()
            if len(clean_error) > 700:
                clean_error = clean_error[:700] + "…"
            user_error = f"❌ فشل تحميل الفيديو.\n\n📌 السبب: {clean_error}"

        # ابعت الخطأ في رسالة جديدة أولًا لضمان ظهوره حتى لو Telegram رفض تعديل
        # رسالة "جاري التجهيز" بسبب تحديثات التقدم المتزامنة.
        error_sent = False
        try:
            await update.message.reply_text(user_error)
            error_sent = True
        except Exception as message_error:
            print(
                "⚠️ Failed to send new video error message:",
                type(message_error).__name__,
                ":",
                message_error
            )

        # بعد إرسال الخطأ، نحاول حذف رسالة التجهيز القديمة.
        if error_sent:
            try:
                await msg.delete()
            except Exception as message_error:
                print(
                    "ℹ️ تعذر حذف رسالة التجهيز القديمة:",
                    type(message_error).__name__,
                    ":",
                    message_error
                )
        else:
            # حل أخير لو إرسال الرسالة الجديدة فشل.
            try:
                await msg.edit_text(user_error)
            except Exception as message_error:
                print(
                    "❌ تعذر عرض رسالة خطأ الفيديو:",
                    type(message_error).__name__,
                    ":",
                    message_error
                )

    finally:

        progress_state["done"] = True
        try:
            progress_task.cancel()
        except Exception:
            pass

        if not keep_file:

            clean_job_files(
                job_id
            )

            _active_job_ids.discard(job_id)
            _cancel_jobs.discard(job_id)
            context.user_data.pop("active_job_id", None)


# ============================================================
# تحميل الصوت
# ============================================================

@download_guard
async def download_audio(
    update,
    context,
    url
):

    # Suno له مسار صوت مباشر خاص به؛ لا نمرره إلى yt-dlp.
    if is_suno_url(url):
        return await download_suno_media(update, context, url, "audio")

    cached_path, cached_title = get_cached_file(url, "audio")
    if cached_path:
        try:
            with open(cached_path, "rb") as cached_audio:
                await update.message.reply_audio(audio=cached_audio, filename="audio.mp3", read_timeout=180, write_timeout=180, connect_timeout=60, pool_timeout=60)
            user_id = str(update.effective_user.id)
            users[user_id]["downloads"] = users[user_id].get("downloads",0)+1
            add_history(user_id, cached_title or "صوت محفوظ بالكاش", detect_site(url), "🎵 MP3")
            save_users(users); stats["downloads"] += 1; stats["audio_downloads"] += 1; save_stats(stats)
            record_success_stats(detect_site(url), "🎵 MP3"); clear_error_streak(update.effective_user.id)
            await maybe_send_ad(update, context)
            context.user_data.pop("download_url",None); context.user_data.pop("download_stage",None)
            return
        except Exception as e: print("⚠️ Cache audio send failed:",e)

    msg = await update.message.reply_text(
        "⏳ جاري استخراج الصوت...\n░░░░░░░░░░░░ 0%",
        reply_markup=ReplyKeyboardMarkup([["⛔ إلغاء التحميل"]], resize_keyboard=True)
    )

    job_id = uuid.uuid4().hex
    _active_job_ids.add(job_id)
    context.user_data["active_job_id"] = job_id

    filename = os.path.join(
        DOWNLOAD_DIR,
        f"{job_id}.%(title)s.%(ext)s"
    )

    options = {
        "outtmpl": filename,
        "format": "bestaudio/best",
        "noplaylist": True,
        "quiet": False,
        "socket_timeout": 120,
        "retries": 10,
        "fragment_retries": 10,
        "file_access_retries": 5,
        "extractor_retries": 5,
        "retry_sleep_functions": {"http": lambda n: min(10, 1 + n)},
        "continuedl": True,
        "overwrites": True,
        "restrictfilenames": True,
        "concurrent_fragment_downloads": 4,
        "buffersize": 1024 * 64,
        "check_formats": True,
        "geo_bypass": True,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ],
    }

    loop = asyncio.get_running_loop()

    last_percent = [-1]

    def progress_hook(data):

        try:

            if cancel_requested(job_id):
                raise RuntimeError("DOWNLOAD_CANCELLED")

            if data["status"] == "downloading":

                total = (
                    data.get("total_bytes")
                    or data.get("total_bytes_estimate")
                )

                if total:

                    downloaded = data.get(
                        "downloaded_bytes",
                        0
                    )

                    percent = int(
                        downloaded * 100 / total
                    )

                    if (
                        percent != last_percent[0]
                        and percent % 5 == 0
                    ):

                        last_percent[0] = percent

                        asyncio.run_coroutine_threadsafe(
                            msg.edit_text(
                                f"⏳ جاري استخراج الصوت... {percent}%"
                            ),
                            loop
                        )

            elif data["status"] == "finished":

                asyncio.run_coroutine_threadsafe(
                    msg.edit_text(
                        "✅ اكتمل الاستخراج 100%\n"
                        "📤 جاري إرسال MP3..."
                    ),
                    loop
                )

        except Exception as e:

            print(
                "⚠️ Audio progress error:",
                e
            )

    progress_state = {"done": False, "label": "جاري تحميل الصوت...", "percent": None, "downloaded": 0, "total": None, "speed": None, "eta": None}
    original_audio_hook = progress_hook
    def tracked_audio_hook(data):
        try:
            if data.get("status") == "downloading":
                total = data.get("total_bytes") or data.get("total_bytes_estimate")
                downloaded = data.get("downloaded_bytes", 0) or 0
                progress_state.update(total=total, downloaded=downloaded, speed=data.get("speed"), eta=data.get("eta"))
                if total:
                    progress_state["percent"] = int(downloaded * 100 / total)
            elif data.get("status") == "finished":
                progress_state["percent"] = 100
        finally:
            original_audio_hook(data)
    options["progress_hooks"] = [tracked_audio_hook]
    progress_task = asyncio.create_task(monitor_download_progress(job_id, msg, progress_state))

    try:

        def download():

            try:
                with yt_dlp.YoutubeDL(options) as ydl:
                    info = ydl.extract_info(url, download=True)
                    prepared = ydl.prepare_filename(info)
                    return prepared, info
            except Exception as first_error:
                # آخر محاولة عامة للمنصات غير TikTok.
                # لو الموقع لا يملك صيغة مطابقة للارتفاع المطلوب،
                # نطلب أي فيديو صالح بدل إيقاف العملية برسالة format unavailable.
                is_tiktok_local = (
                    "tiktok.com" in url.lower()
                    or "vt.tiktok.com" in url.lower()
                )
                if is_tiktok_local:
                    raise

                print("⚠️ فشل اختيار الصيغة المحددة، تجربة صيغة عامة:", first_error)
                fallback_options = dict(options)
                fallback_options["format"] = "bv*+ba/bv*/b[ext=mp4]/b/best"
                fallback_options["merge_output_format"] = "mp4"
                with yt_dlp.YoutubeDL(fallback_options) as ydl:
                    info = ydl.extract_info(url, download=True)
                    prepared = ydl.prepare_filename(info)
                    return prepared, info

        original_path, info = (
            await loop.run_in_executor(None, download)
        )
        progress_state["done"] = True
        progress_state["percent"] = 100

        expected_mp3 = (
            os.path.splitext(
                original_path
            )[0]
            + ".mp3"
        )

        file_path = find_job_file(
            job_id,
            expected_mp3
        )

        mp3_files = [
            path
            for path in glob.glob(
                os.path.join(
                    DOWNLOAD_DIR,
                    f"{job_id}.*"
                )
            )
            if (
                os.path.isfile(path)
                and path.lower().endswith(".mp3")
            )
        ]

        if mp3_files:

            file_path = mp3_files[0]

        if (
            not file_path
            or not os.path.exists(file_path)
        ):

            raise Exception(
                "ملف MP3 غير موجود."
            )

        with open(
            file_path,
            "rb"
        ) as audio:

            await update.message.reply_audio(
                audio=audio,
                filename="audio.mp3",
                read_timeout=180,
                write_timeout=180,
                connect_timeout=60,
                pool_timeout=60
            )

        save_cached_file(file_path, url, "audio", "mp3", info.get("title", "صوت"))

        try:
            await msg.delete()
        except Exception:
            pass

        user_id = str(
            update.effective_user.id
        )

        if user_id in users:

            users[user_id]["downloads"] = (
                users[user_id].get(
                    "downloads",
                    0
                ) + 1
            )

        title = (
            info.get(
                "title",
                "صوت"
            )
            or "صوت"
        )

        add_history(
            user_id,
            title,
            detect_site(url),
            "🎵 MP3"
        )

        save_users(users)

        stats["downloads"] += 1
        stats["audio_downloads"] += 1

        save_stats(stats)
        record_success_stats(detect_site(url), "🎵 MP3")
        clear_error_streak(update.effective_user.id)

        await maybe_send_ad(update, context)

        context.user_data.pop(
            "download_url",
            None
        )

        context.user_data.pop(
            "download_stage",
            None
        )

    except Exception as e:

        if "DOWNLOAD_CANCELLED" in str(e):
            try: await msg.edit_text("⛔ تم إلغاء التحميل.")
            except Exception: pass
            return

        stats["errors"] += 1
        save_stats(stats)
        streak = record_error_streak(update.effective_user.id)
        if streak >= ERROR_ALERT_THRESHOLD:
            await notify_admin(
                context.bot,
                f"⚠️ أخطاء تحميل متكررة\n👤 المستخدم: {update.effective_user.id}\n🔢 الأخطاء المتتالية: {streak}\n🌐 الموقع: {detect_site(url)}",
                key=f"error_{update.effective_user.id}"
            )

        print(
            "❌ AUDIO ERROR:",
            type(e).__name__,
            ":",
            e
        )

        try:
            err_text = str(e)
            if "Suno" in err_text or "suno" in err_text:
                await msg.edit_text(
                    "❌ فشل تنزيل أغنية Suno.\n\n"
                    f"📌 السبب: {err_text}\n\n"
                    "💡 لتفعيل التنزيل الرسمي على السيرفر، ضع SUNO_COOKIE "
                    "لحسابك المسجّل في Suno ثم أعد تشغيل البوت."
                )
            else:
                await msg.edit_text(UPDATE_MESSAGE, parse_mode="Markdown")
        except Exception:
            pass

    finally:

        progress_state["done"] = True
        try:
            progress_task.cancel()
        except Exception:
            pass

        clean_job_files(
            job_id
        )

        _active_job_ids.discard(job_id)


# ============================================================
# إلغاء التحميل وإعادة التحميل من السجل
# ============================================================

async def cancel_current_download(update, context):
    job_id = context.user_data.get("active_job_id")
    if not job_id or job_id not in _active_job_ids:
        await update.message.reply_text("ℹ️ مفيش تحميل شغال حاليًا.")
        return
    _cancel_jobs.add(job_id)
    await update.message.reply_text("⛔ تم طلب إلغاء التحميل... لحظة واحدة.")


async def reload_history_item(update, context, index):
    user_id=str(update.effective_user.id)
    history=users.get(user_id,{}).get("history",[])
    if index<1 or index>len(history):
        await update.message.reply_text("❌ رقم التحميل غير موجود في السجل.")
        return
    url=history[index-1].get("url","")
    if not url:
        await update.message.reply_text("❌ الرابط القديم غير محفوظ في هذا السجل.")
        return
    context.user_data["download_url"]=url
    await download_handler(update, context)


async def user_premium_menu(update, context):
    uid=update.effective_user.id
    if context.user_data.get("nav_page") != "premium_menu":
        context.user_data["nav_parent"] = context.user_data.get("nav_page", "main_menu")
    context.user_data["nav_page"] = "premium_menu"
    if not premium_data.get("enabled", False):
        await update.message.reply_text("💎 نظام Premium متوقف حاليًا.")
        return

    # المشترك يشوف التفاصيل فقط في الدخول العادي.
    # أثناء الترقية/التجديد لازم نعرض الباقات بدل ما نرجعه للحالة مرة تانية.
    if premium_subscription(uid) and not context.user_data.get("premium_change_plan"):
        await user_premium_status(update, context)
        return

    p=premium_data.get("prices", {})
    await update.message.reply_text(
        "💎 الاشتراك في Premium\n\n"
        f"🗓️ أسبوع: {p.get('week',30)} ⭐\n"
        f"📅 شهر: {p.get('month',80)} ⭐\n"
        f"📆 سنة: {p.get('year',700)} ⭐\n\n"
        "⭐ الدفع يتم داخل Telegram\n"
        "اختار الباقة:",
        reply_markup=ReplyKeyboardMarkup([
            [f"🗓️ أسبوع - {p.get('week',30)} ⭐"],
            [f"📅 شهر - {p.get('month',80)} ⭐"],
            [f"📆 سنة - {p.get('year',700)} ⭐"],
            ["🔙 رجوع", "🏠 القائمة الرئيسية"],
        ], resize_keyboard=True)
    )


def premium_plan_key(plan):
    return {
        "أسبوع": "week",
        "اسبوع": "week",
        "week": "week",
        "شهر": "month",
        "month": "month",
        "سنة": "year",
        "سنه": "year",
        "year": "year",
    }.get(str(plan).strip().lower())


def get_premium_price(plan):
    key = premium_plan_key(plan)
    if not key:
        return None
    prices = premium_data.get("prices", {})
    value = prices.get(key)
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    return int(value) if value.is_integer() else value


async def user_start_payment(update, context, plan):
    uid = update.effective_user.id
    if not premium_data.get("enabled", False):
        await update.message.reply_text("💎 نظام Premium متوقف حاليًا.")
        return

    existing = premium_subscription(uid)
    if existing and not context.user_data.get("premium_change_plan"):
        await user_premium_status(update, context)
        return

    price = get_premium_price(plan)
    if price is None:
        await update.message.reply_text(
            "❌ الباقة دي غير متاحة حاليًا.\n"
            "الأدمن لم يحدد سعر Stars صحيحًا لها.\n\n"
            "🛠️ اطلب من الأدمن ضبط سعر الباقة من لوحة Premium."
        )
        return

    try:
        price = int(price)
    except Exception:
        await update.message.reply_text("❌ سعر Stars غير صحيح.")
        return
    if price < 1 or price > 100000:
        await update.message.reply_text("❌ سعر الباقة يجب أن يكون بين 1 و100000 ⭐.")
        return

    plan_key = premium_plan_key(plan)
    payload = f"premium:{plan_key}:{uid}:{uuid.uuid4().hex[:16]}"
    premium_data.setdefault("stars_orders", {})[payload] = {
        "user_id": uid,
        "plan": plan_key,
        "price": price,
        "created_at": time.time(),
    }
    save_premium_config(premium_data)

    try:
        await update.message.reply_invoice(
            title=f"Premium {premium_plan_display(plan)}",
            description=(
                f"اشتراك Premium لمدة { {'week': '7 أيام', 'month': '30 يومًا', 'year': '365 يومًا'}.get(plan_key, plan) } "
                "في صاروخ التحميل 🚀"
            ),
            payload=payload,
            provider_token="",
            currency="XTR",
            prices=[LabeledPrice(f"Premium {premium_plan_display(plan)}", price)],
        )
        context.user_data.pop("premium_payment_plan", None)
        context.user_data["nav_page"] = "premium_payment"
    except Exception as e:
        premium_data.get("stars_orders", {}).pop(payload, None)
        save_premium_config(premium_data)
        print("⚠️ Stars invoice error:", type(e).__name__, e)
        await update.message.reply_text(
            "❌ تعذر إنشاء فاتورة Telegram Stars حاليًا.\n"
            "جرب مرة أخرى بعد قليل."
        )


async def handle_stars_pre_checkout(update, context):
    query = update.pre_checkout_query
    payload = query.invoice_payload or ""
    order = premium_data.get("stars_orders", {}).get(payload)
    if not isinstance(order, dict):
        await query.answer(ok=False, error_message="❌ الطلب غير موجود أو انتهت صلاحيته.")
        return

    try:
        valid_user = int(order.get("user_id")) == int(query.from_user.id)
        valid_amount = int(order.get("price")) == int(query.total_amount)
    except Exception:
        valid_user = False
        valid_amount = False

    if query.currency != "XTR" or not valid_user or not valid_amount:
        await query.answer(ok=False, error_message="❌ بيانات الدفع غير مطابقة للطلب.")
        return

    await query.answer(ok=True)


async def handle_stars_successful_payment(update, context):
    payment = update.message.successful_payment if update.message else None
    if not payment:
        return

    uid = update.effective_user.id
    charge_id = payment.telegram_payment_charge_id
    payments = premium_data.setdefault("stars_payments", {})
    if charge_id in payments:
        await update.message.reply_text("ℹ️ عملية الدفع دي اتسجلت بالفعل وتم تفعيل الاشتراك.")
        return

    payload = payment.invoice_payload or ""
    order = premium_data.get("stars_orders", {}).get(payload)
    if not isinstance(order, dict):
        await update.message.reply_text(
            "⚠️ تم استلام الدفع لكن لم أجد بيانات الباقة.\n"
            "تواصل مع الإدارة فورًا مع الاحتفاظ بإيصال الدفع."
        )
        return

    if int(order.get("user_id", 0)) != int(uid) or payment.currency != "XTR":
        await update.message.reply_text("⚠️ بيانات الدفع لا تطابق المستخدم أو الباقة.")
        return

    try:
        paid_amount = int(payment.total_amount)
        order_amount = int(order.get("price"))
    except Exception:
        paid_amount = -1
        order_amount = -2
    if paid_amount != order_amount:
        await update.message.reply_text("⚠️ مبلغ الدفع لا يطابق سعر الباقة. تواصل مع الإدارة.")
        return

    plan_key = str(order.get("plan"))
    days = {"week": 7, "month": 30, "year": 365}.get(plan_key)
    if not days:
        await update.message.reply_text("⚠️ تعذر تحديد مدة الباقة. تواصل مع الإدارة.")
        return

    now = time.time()
    old = premium_subscription(uid)
    start = max(now, float(old.get("expires_at", now))) if old else now
    expires_at = start + days * 86400
    canonical_plan = {"week": "أسبوع", "month": "شهر", "year": "سنة"}[plan_key]

    premium_data.setdefault("subscriptions", {})[str(uid)] = {
        "plan": canonical_plan,
        "started_at": now,
        "expires_at": expires_at,
        "price": paid_amount,
        "payment_method": "Telegram Stars",
        "telegram_payment_charge_id": charge_id,
        "invoice_payload": payload,
    }
    payments[charge_id] = {
        "user_id": uid,
        "plan": canonical_plan,
        "plan_key": plan_key,
        "stars": paid_amount,
        "payload": payload,
        "created_at": now,
    }
    premium_data.get("stars_orders", {}).pop(payload, None)
    save_premium_config(premium_data)

    context.user_data.pop("premium_change_plan", None)
    context.user_data.pop("premium_payment_plan", None)
    limit, window, custom = get_premium_limit_config(uid)
    await update.message.reply_text(
        "🎉💎 تم الدفع بنجاح وتفعيل Premium تلقائيًا!\n\n"
        f"📦 الباقة: {canonical_plan}\n"
        f"⭐ المدفوع: {paid_amount} ⭐\n"
        f"⏳ المدة المضافة: {days} يوم\n"
        f"📅 تاريخ الانتهاء: {time.strftime('%Y-%m-%d %H:%M', time.localtime(expires_at))}\n"
        f"🚀 حد التحميل: {limit} تحميل / {window} دقيقة\n"
        "استمتع بصاروخ التحميل 🚀🔥"
    )

    try:
        await context.bot.send_message(
            ADMIN_ID,
            "💰 عملية Premium جديدة عبر Telegram Stars\n\n"
            f"🆔 المستخدم: {uid}\n"
            f"📦 الباقة: {canonical_plan}\n"
            f"⭐ المبلغ: {paid_amount} ⭐\n"
            f"🧾 Charge ID: {charge_id}"
        )
    except Exception as e:
        print("⚠️ Stars admin notify error:", e)


async def user_premium_status(update, context):
    uid=update.effective_user.id
    if context.user_data.get("nav_page") != "premium_status":
        context.user_data["nav_parent"] = context.user_data.get("nav_page", "main_menu")
    context.user_data["nav_page"] = "premium_status"
    item=premium_subscription(uid)
    if not item:
        await user_premium_menu(update, context)
        return

    remain=max(0,int(float(item.get("expires_at",0))-time.time()))
    limit,window,custom=get_premium_limit_config(uid)
    _,_,remaining_downloads,_=download_limit_info(uid)
    limit_tag="🎯 حد مخصص لحسابك" if custom else "💎 حد الباقة"
    exp=time.strftime('%Y-%m-%d %H:%M', time.localtime(float(item.get('expires_at',0))))

    await update.message.reply_text(
        "💎✨ تفاصيل اشتراكك في Premium\n\n"
        f"📦 الباقة الحالية: {premium_plan_display(item.get('plan'))}\n"
        f"⏳ المتبقي: {format_remaining(remain)}\n"
        f"📅 تاريخ الانتهاء: {exp}\n"
        f"{limit_tag}: {limit} تحميل / {window} دقيقة\n"
        f"🚀 المتاح حاليًا: {remaining_downloads} تحميل\n\n"
        "اختار الإجراء اللي عايزه:",
        reply_markup=ReplyKeyboardMarkup([
            ["⬆️ ترقية / زيادة الاشتراك"],
            ["🔄 تجديد الاشتراك"],
            ["❌ إلغاء الاشتراك"],
            ["🔙 رجوع", "🏠 القائمة الرئيسية"],
        ], resize_keyboard=True)
    )


async def cancel_user_premium(update, context):
    uid=str(update.effective_user.id)
    if not premium_subscription(uid):
        await user_premium_menu(update, context)
        return
    premium_data.setdefault("subscriptions", {}).pop(uid, None)
    premium_data.setdefault("premium_custom_limits", {}).pop(uid, None)
    save_premium_config(premium_data)
    await update.message.reply_text(
        "✅ تم إلغاء اشتراك Premium الخاص بك.\n\n"
        "يمكنك الاشتراك مرة أخرى في أي وقت.",
        reply_markup=ReplyKeyboardMarkup([["💎 الاشتراك في Premium"],["🔙 رجوع", "🏠 القائمة الرئيسية"]], resize_keyboard=True)
    )


# ============================================================
# سجل المستخدم
# ============================================================

async def user_history(
    update,
    context
):

    context.user_data["nav_parent"] = context.user_data.get("nav_page", "main_menu")
    context.user_data["nav_page"] = "user_history"

    user_id = str(
        update.effective_user.id
    )

    user = users.get(
        user_id,
        {}
    )

    history = user.get(
        "history",
        []
    )

    if not history:

        await update.message.reply_text(
            "📜 سجل تحميلاتك فاضي لسه.\n\n"
            "🚀 ابعت رابط وابدأ أول تحميل ليك."
        )

        return

    text = "📜 آخر تحميلاتك:\n\n"

    for i, item in enumerate(
        history[:10],
        start=1
    ):

        title = item.get(
            "title",
            "بدون عنوان"
        )

        site = item.get(
            "site",
            "🌐 موقع آخر"
        )

        kind = item.get(
            "type",
            "🎬 فيديو"
        )

        text += (
            f"{i}️⃣ {kind}\n"
            f"🎬 {title[:80]}\n"
            f"🌐 {site}\n"
            f"━━━━━━━━━━━━\n"
        )

    buttons=[]
    for i in range(1, min(10, len(history)) + 1):
        buttons.append(f"🔄 إعادة تحميل {i}")
    keyboard=[buttons[i:i+2] for i in range(0,len(buttons),2)]
    keyboard.append(["🔙 رجوع"])
    await update.message.reply_text(
        text + "\nاختار تحميل قديم لإعادته:",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    )


# ============================================================
# إحصائيات المستخدم
# ============================================================

async def user_stats(
    update,
    context
):

    context.user_data["nav_parent"] = context.user_data.get("nav_page", "main_menu")
    context.user_data["nav_page"] = "user_stats"

    user_id = str(
        update.effective_user.id
    )

    user = users.get(
        user_id,
        {}
    )

    downloads = user.get(
        "downloads",
        0
    )

    history_count = len(
        user.get(
            "history",
            []
        )
    )

    await update.message.reply_text(
        "📊 إحصائياتك\n\n"
        f"📥 إجمالي تحميلاتك: {downloads}\n"
        f"📜 المحفوظ في السجل: {history_count}\n\n"
        "🚀 استمر في استخدام صاروخ التحميل!"
    )


# ============================================================
# طريقة الاستخدام
# ============================================================

async def show_help(
    update,
    context
):

    context.user_data["nav_parent"] = context.user_data.get("nav_page", "main_menu")
    context.user_data["nav_page"] = "help"

    await update.message.reply_text(
        "❓ طريقة استخدام صاروخ التحميل\n\n"
        "1️⃣ اضغط 🚀 انطلق\n"
        "2️⃣ ابعت رابط الفيديو\n"
        "3️⃣ اختار 🎬 فيديو أو 🎵 صوت فقط\n"
        "4️⃣ لو اخترت فيديو، اختار الجودة بالأرقام\n\n"
        "🎬 الجودة المطلوبة هي الحد الأقصى للجودة.\n"
        "⬇️ لو الجودة المطلوبة مش متاحة، الصاروخ يختار أقرب جودة أقل منها.\n\n"
        "🖼️ الفيديو الثابت + أغنية هتظهرله اختيارات:\n"
        "🖼️ صورة فقط\n"
        "🎬 فيديو\n"
        "🎵 صوت\n\n"
        "📸 TikTok Photo هتظهرله نفس الاختيارات:\n"
        "🖼️ صورة فقط\n"
        "🎬 فيديو\n"
        "🎵 صوت\n\n"
        "🖼️ الصورة المصغرة = صورة الفيديو\n"
        "ℹ️ معلومات الفيديو = تفاصيل الفيديو"
    )


# ============================================================
# الإعدادات
# ============================================================

def settings_keyboard():

    return [
        ["🔥 أعلى جودة افتراضية"],
        ["⚡ متوسطة افتراضية"],
        ["📱 خفيفة افتراضية"],
        ["🎵 MP3 افتراضي"],
        ["🎬 اسألني كل مرة", "🎬 فيديو افتراضي"],
        ["🎵 صوت افتراضي"],
        ["🔙 رجوع", "🏠 القائمة الرئيسية"],
    ]


async def show_settings(
    update,
    context
):

    context.user_data["nav_parent"] = context.user_data.get("nav_page", "main_menu")
    context.user_data["nav_page"] = "settings"

    user_id = str(
        update.effective_user.id
    )

    user = users.get(
        user_id,
        {}
    )

    current = user.get(
        "default_quality",
        "🔥 أعلى جودة"
    )

    await update.message.reply_text(
        "⚙️ إعدادات صاروخ التحميل\n\n"
        f"🎯 الجودة الافتراضية الحالية:\n{current}\n\n"
        "اختار الجودة اللي تحبها كافتراضية:",
        reply_markup=ReplyKeyboardMarkup(
            settings_keyboard(),
            resize_keyboard=True
        )
    )


async def change_default_quality(
    update,
    context,
    quality
):

    user_id = str(
        update.effective_user.id
    )

    if user_id not in users:

        register_user(
            update.effective_user
        )

    users[user_id][
        "default_quality"
    ] = quality

    save_users(users)

    await update.message.reply_text(
        f"✅ تم حفظ الإعداد.\n\n"
        f"🎯 الجودة الافتراضية: {quality}"
    )

    await show_main_menu(
        update,
        context
    )


# ============================================================
# المعالج الرئيسي
# ============================================================


async def handle_navigation_button(update, context, text, user_id):
    """معالجة أزرار التنقل قبل أي حالة إدخال، حتى لا تُعتبر الإجابة نصًا."""

    if text not in ("🔙 رجوع", "➡️ التالي", "🏠 القائمة الرئيسية"):
        return False

    # الرئيسية: إلغاء أي عملية إدخال معلقة ثم العودة للرئيسية.
    if text == "🏠 القائمة الرئيسية":
        for key in list(context.user_data.keys()):
            if key.startswith("awaiting_"):
                context.user_data.pop(key, None)
        for key in (
            "admin_action", "premium_payment_plan", "premium_change_plan",
            "permission_admin_uid", "custom_premium_limit_uid",
            "download_url", "download_stage"
        ):
            context.user_data.pop(key, None)
        clear_static_state(context, True)
        clear_tiktok_photo_state(context, True)
        await show_main_menu(update, context)
        return True

    # التالي: لا يدخل لأي معالج إدخال. ينقل خطوة واحدة فقط عندما توجد خطوة واضحة.
    if text == "➡️ التالي":
        stage = context.user_data.get("download_stage")
        if stage == "media_choice":
            context.user_data["download_stage"] = "quality"
            await update.message.reply_text(
                "🎬 اختار الجودة:",
                reply_markup=ReplyKeyboardMarkup(
                    quality_keyboard(), resize_keyboard=True
                )
            )
            return True

        # لو إحنا في شاشة الباقات، لا يوجد إدخال مطلوب؛ الاختيار نفسه هو الخطوة التالية.
        if context.user_data.get("nav_page") == "premium_menu":
            await update.message.reply_text("ℹ️ اختار الباقة اللي عايزها من القائمة.")
            return True

        if context.user_data.get("nav_page") == "premium_payment":
            await update.message.reply_text(
                "ℹ️ الخطوة الحالية: ابعت رقم العملية أو صورة الإيصال بعد التحويل."
            )
            return True

        await update.message.reply_text("ℹ️ مفيش خطوة تالية متاحة هنا.")
        return True

    # الرجوع: نفّذه قبل أي awaiting/admin_action حتى لا يتحول النص لإجابة.
    stage = context.user_data.get("download_stage")
    if stage == "quality" and context.user_data.get("download_url"):
        context.user_data["download_stage"] = "media_choice"
        await update.message.reply_text(
            "🔙 رجعنا خطوة واحدة.\n\nاختار هتستلم إيه:",
            reply_markup=ReplyKeyboardMarkup(
                media_choice_keyboard(), resize_keyboard=True
            )
        )
        return True

    if stage == "media_choice" and context.user_data.get("download_url"):
        context.user_data["download_stage"] = "awaiting_url"
        context.user_data.pop("download_url", None)
        await update.message.reply_text(
            "🔙 رجعنا خطوة واحدة.\n\n🚀 ابعت رابط الفيديو من جديد:",
            reply_markup=ReplyKeyboardMarkup(
                [["🔙 رجوع", "🏠 القائمة الرئيسية"]], resize_keyboard=True
            )
        )
        return True

    if stage == "awaiting_url":
        context.user_data.pop("download_stage", None)
        await show_main_menu(update, context)
        return True

    # أي عملية إدخال معلقة (رقم مستخدم، اسم، إعداد، دفع...): إلغاء الإدخال
    # والعودة للصفحة التي بدأنا منها، بدل اعتبار "رجوع" قيمة مدخلة.
    if context.user_data.get("premium_payment_plan"):
        context.user_data.pop("premium_payment_plan", None)
        context.user_data["nav_page"] = "premium_menu"
        await user_premium_menu(update, context)
        return True

    if context.user_data.get("admin_action"):
        context.user_data.pop("admin_action", None)
        context.user_data.pop("permission_admin_uid", None)
        context.user_data.pop("custom_premium_limit_uid", None)

    parent = context.user_data.get("nav_parent")
    page = context.user_data.get("nav_page")

    if parent and parent != page:
        context.user_data["nav_page"] = parent
        if parent == "main_menu":
            await show_main_menu(update, context); return True
        if parent == "premium_status":
            await user_premium_status(update, context); return True
        if parent == "premium_menu":
            context.user_data.pop("premium_change_plan", None)
            await user_premium_menu(update, context); return True
        if parent == "admin_panel":
            await admin_panel(update, context); return True
        if parent == "admin_premium":
            await admin_premium(update, context); return True
        if parent == "admin_limits":
            await admin_limits(update, context); return True
        if parent == "admin_premium_limits":
            await admin_premium_limits(update, context); return True
        if parent == "admin_payment_settings":
            await admin_payment_settings(update, context); return True
        if parent == "admin_permissions":
            context.user_data["nav_parent"] = "admin_panel"
            await admin_permissions_menu(update, context); return True
        if parent == "admin_bot_settings":
            context.user_data["nav_parent"] = "admin_panel"
            await admin_bot_settings(update, context); return True
        if parent == "admin_manage_admins":
            await admin_manage_admins(update, context); return True
        if parent == "admin_premium_list":
            await admin_premium_list(update, context); return True
        if parent == "admin_premium_prices":
            await admin_premium_prices(update, context); return True
        if parent == "admin_stats":
            await admin_stats(update, context); return True
        if parent == "admin_users":
            await admin_users(update, context); return True
        if parent == "admin_user_info":
            await admin_user_info(update, context); return True
        if parent == "admin_ban":
            await admin_ban(update, context); return True
        if parent == "admin_unban":
            await admin_unban(update, context); return True
        if parent == "admin_banned":
            await admin_banned(update, context); return True
        if parent == "admin_broadcast":
            await admin_broadcast(update, context); return True
        if parent == "admin_ads":
            await admin_ads(update, context); return True
        if parent == "settings":
            await show_settings(update, context); return True
        if parent == "user_history":
            await user_history(update, context); return True
        if parent == "user_stats":
            await user_stats(update, context); return True
        if parent == "help":
            await show_help(update, context); return True

    # لا يوجد أب معروف: الرئيسية هي آخر نقطة آمنة.
    context.user_data.pop("download_url", None)
    context.user_data.pop("download_stage", None)
    await show_main_menu(update, context)
    return True


async def download_handler(
    update,
    context
):

    text = (
        update.message.text
        or ""
    ).strip()

    if not text:
        return

    user_id = update.effective_user.id

    # مهم جدًا: التنقل يتفحص أولًا قبل awaiting_name أو أي admin_action
    # حتى لا تُقرأ أزرار رجوع/التالي/الرئيسية كأنها إجابة المستخدم.
    if await handle_navigation_button(update, context, text, user_id):
        return

    # ========================================================
    # اختيار جودة الفيديو
    # ========================================================
    # لازم يتفحص قبل التحقق من أن الرسالة رابط؛ لأن أزرار
    # الجودة (2160p/1440p/1080p...) ليست روابط.
    if context.user_data.get("download_stage") == "quality":
        video_format = await handle_quality_action(
            update,
            context,
            text
        )

        if video_format:
            url = context.user_data.get("download_url")

            if not url:
                context.user_data.pop("download_stage", None)
                await update.message.reply_text(
                    "❌ رابط الفيديو مش موجود. ابعت الرابط من جديد."
                )
                return

            context.user_data.pop("download_stage", None)

            await download_video(
                update,
                context,
                url,
                video_format
            )

            return

    register_user(
        update.effective_user
    )

    # ========================================================
    # التواصل
    # ========================================================
    if context.user_data.get("contact_mode") == "user_to_admin":
        if await send_contact_to_admin(update, context):
            return

    if is_admin(user_id):
        action = context.user_data.get("admin_action")
        if action == "contact_find_user":
            q = text.strip().lstrip("@").lower()
            matches=[]
            for uid,data in users.items():
                if not isinstance(data,dict): continue
                name=str(data.get("name") or data.get("first_name") or "").lower()
                uname=str(data.get("username") or "").lower().lstrip("@")
                if q and (q==str(uid) or q in name or q in uname): matches.append((uid,data))
            if not matches:
                await update.message.reply_text("❌ ملقتش مستخدم بالبحث ده. جرّب الاسم أو الـID تاني."); return
            context.user_data["admin_action"]="contact_select_user"
            context.user_data["contact_user_map"]={}
            kb=[]
            for uid,data in matches[:20]:
                label=f"👤 {str(data.get('name') or data.get('first_name') or 'بدون اسم')[:30]} | 🆔 {uid}"
                context.user_data["contact_user_map"][label]=uid; kb.append([label])
            kb.append(["🔙 رجوع","🏠 القائمة الرئيسية"])
            await update.message.reply_text("🔎 نتائج البحث — اختار المستخدم:",reply_markup=ReplyKeyboardMarkup(kb,resize_keyboard=True)); return
        if action == "contact_select_message":
            mid=context.user_data.get("contact_inbox_map",{}).get(text)
            rec=next((x for x in contact_messages if x.get("id")==mid),None) if mid else None
            if rec:
                context.user_data["admin_action"]="contact_reply"; context.user_data["contact_reply_user_id"]=int(rec["user_id"])
                rec["status"]="handled"; save_contact_messages(contact_messages)
                await update.message.reply_text(f"💬 الرد على {rec.get('name','المستخدم')} | 🆔 {rec['user_id']}\n\n✍️ ابعت الرسالة أو الصورة:",reply_markup=ReplyKeyboardMarkup([["🔙 رجوع","🏠 القائمة الرئيسية"]],resize_keyboard=True)); return
        if action == "contact_select_user":
            uid=context.user_data.get("contact_user_map",{}).get(text)
            if uid:
                context.user_data["contact_target_user_id"]=int(uid); context.user_data["admin_action"]="contact_send_user"
                data=users.get(str(uid),{})
                await update.message.reply_text(f"👤 المستخدم: {data.get('name') or data.get('first_name') or 'بدون اسم'}\n🆔 ID: {uid}\n\n✍️ ابعت الرسالة أو الصورة:",reply_markup=ReplyKeyboardMarkup([["🔙 رجوع","🏠 القائمة الرئيسية"]],resize_keyboard=True)); return
        if action in ("contact_send_user","contact_reply"):
            target=context.user_data.get("contact_target_user_id") or context.user_data.get("contact_reply_user_id")
            if target:
                try:
                    sent_msg = await context.bot.send_message(int(target),text)
                    asyncio.create_task(delete_admin_message_later(context.bot,int(target),sent_msg.message_id))
                    contact_messages.append({"id":uuid.uuid4().hex[:12],"user_id":int(target),"admin_id":int(user_id),"name":users.get(str(target),{}).get("name", ""),"created_at":time.time(),"type":"admin_text","text":text,"photo_file_id":"","status":"sent","telegram_message_id":sent_msg.message_id})
                    save_contact_messages(contact_messages)
                    await update.message.reply_text("✅ تم إرسال الرسالة للمستخدم.")
                except Exception as e:
                    await update.message.reply_text("❌ فشل إرسال الرسالة للمستخدم. ممكن يكون حاجب البوت أو حسابه غير متاح.")
                    print("⚠️ Contact send error:",e)
                context.user_data.pop("admin_action",None); context.user_data.pop("contact_target_user_id",None); context.user_data.pop("contact_reply_user_id",None); return
        if action == "contact_broadcast":
            context.user_data["contact_broadcast_text"]=text; context.user_data["admin_action"]="contact_broadcast_confirm"
            await update.message.reply_text("📢 الرسالة جاهزة للإرسال لكل المستخدمين.\n\n⚠️ تأكيد الإرسال؟",reply_markup=ReplyKeyboardMarkup([["✅ تأكيد الإرسال","❌ إلغاء الإرسال"],["🔙 رجوع","🏠 القائمة الرئيسية"]],resize_keyboard=True)); return
        if action == "contact_broadcast_confirm":
            if text=="❌ إلغاء الإرسال": context.user_data.pop("admin_action",None); await update.message.reply_text("❌ تم إلغاء الرسالة الجماعية."); return
            if text=="✅ تأكيد الإرسال":
                body=context.user_data.get("contact_broadcast_text",""); photo=context.user_data.get("contact_broadcast_photo"); cap=context.user_data.get("contact_broadcast_caption",""); sent=failed=0
                for uid in list(users.keys()):
                    try:
                        if photo: await context.bot.send_photo(int(uid),photo=photo,caption=cap)
                        else: await context.bot.send_message(int(uid),body)
                        sent+=1
                    except Exception: failed+=1
                for k in ("admin_action","contact_broadcast_text","contact_broadcast_photo","contact_broadcast_caption"): context.user_data.pop(k,None)
                await update.message.reply_text(f"✅ انتهت الرسالة الجماعية.\n📨 تم الإرسال: {sent}\n❌ فشل: {failed}"); return

    # ========================================================
    # الصيانة
    # ========================================================

    if (
        maintenance_mode
        and not is_admin(user_id)
    ):

        await update.message.reply_text(
            UPDATE_MESSAGE,
            parse_mode="Markdown"
        )

        return

    # ========================================================
    # الاسم
    # ========================================================

    if (
        not is_admin(user_id)
        and context.user_data.get(
            "awaiting_name"
        )
    ):

        name = text.strip()

        if not name:

            await update.message.reply_text(
                "🙂 الاسم فاضي يا صاحبي.\n"
                "اكتب الاسم اللي تحب نناديك بيه."
            )

            return

        if len(name) > 50:

            await update.message.reply_text(
                "🙂 الاسم طويل شوية.\n"
                "اكتب اسم لحد 50 حرف."
            )

            return

        users[str(user_id)]["name"] = name

        users[str(user_id)][
            "name_registered"
        ] = True

        save_users(users)

        context.user_data.pop(
            "awaiting_name",
            None
        )

        await show_main_menu(
            update,
            context
        )

        return

    user_data = users.get(
        str(user_id),
        {}
    )

    if (
        not is_admin(user_id)
        and user_data.get(
            "name_registered",
            False
        ) is False
    ):

        context.user_data[
            "awaiting_name"
        ] = True

        await update.message.reply_text(
            "🚀 نورت صاروخ التحميل! ✨\n"
            "اكتب الاسم اللي تحب نناديك بيه 👤"
        )

        return

    if text == "⛔ إلغاء التحميل":
        await cancel_current_download(update, context)
        return

    if text == "💎 حالة Premium":
        context.user_data["nav_parent"]="main_menu"
        context.user_data["nav_page"]="main_menu"
        await user_premium_status(update, context)
        return

    if text == "💎 الاشتراك في Premium":
        await user_premium_menu(update, context)
        return

    if text in ("⬆️ ترقية / زيادة الاشتراك", "🔄 تجديد الاشتراك"):
        if premium_subscription(user_id):
            context.user_data["premium_change_plan"] = True
            await user_premium_menu(update, context)
        else:
            await user_premium_menu(update, context)
        return

    if text == "❌ إلغاء الاشتراك":
        await cancel_user_premium(update, context)
        return

    p=premium_data.get("prices", {})
    if text == f"🗓️ أسبوع - {p.get('week',30)} ⭐":
        await user_start_payment(update, context, "أسبوع")
        return
    if text == f"📅 شهر - {p.get('month',80)} ⭐":
        await user_start_payment(update, context, "شهر")
        return
    if text == f"📆 سنة - {p.get('year',700)} ⭐":
        await user_start_payment(update, context, "سنة")
        return

    # ========================================================
    # التنقل العام
    # ========================================================

    if text == "🏠 القائمة الرئيسية":
        context.user_data.pop("admin_action",None)
        context.user_data.pop("premium_payment_plan",None)
        context.user_data.pop("download_url",None)
        context.user_data.pop("download_stage",None)
        clear_static_state(context,True)
        clear_tiktok_photo_state(context,True)
        await show_main_menu(update,context)
        return

    if text == "➡️ التالي":
        stage=context.user_data.get("download_stage")
        if stage=="media_choice":
            context.user_data["download_stage"]="quality"
            await update.message.reply_text("🎬 اختار الجودة:",reply_markup=ReplyKeyboardMarkup(quality_keyboard(),resize_keyboard=True))
            return
        if context.user_data.get("nav_page") == "premium_menu":
            await update.message.reply_text("ℹ️ اختار الباقة من القائمة الحالية، وبعدها هتنتقل لخطوات الدفع.")
            return
        if context.user_data.get("nav_page") == "premium_payment":
            plan=context.user_data.get("premium_payment_plan")
            if plan:
                await update.message.reply_text("ℹ️ الخطوة الحالية هي تأكيد الدفع. ابعت رقم العملية أو اكتب بدون رقم.")
                return
        if is_admin(user_id):
            page=context.user_data.get("nav_page")
            # التالي في لوحة الأدمن ينتقل خطوة واحدة فقط بين الأقسام المتاحة.
            if page == "admin_panel":
                order=[
                    ("stats","📊 الإحصائيات"),("users","👥 المستخدمين"),("users","👥 أعضاء البوت"),("broadcast","📢 إذاعة رسالة"),
                    ("moderation","🚫 حظر مستخدم"),("ads","📢 الإعلانات"),("limits","⚙️ حدود التحميل"),
                    ("premium","💎 Premium"),("maintenance","🛠️ وضع الصيانة"),("bot_settings","🤖 إعدادات البوت"),("contact_inbox","📨 رسائل المستخدمين")
                ]
                for perm,label in order:
                    if has_admin_permission(user_id,perm):
                        if label=="📨 رسائل المستخدمين": context.user_data["nav_parent"]="admin_panel"; await admin_contact_menu(update,context)
                        elif label=="👥 أعضاء البوت": context.user_data["nav_parent"]="admin_panel"; await admin_bot_members(update,context)
                        elif perm=="stats": context.user_data["nav_parent"]="admin_panel"; await admin_stats(update,context)
                        elif perm=="users": context.user_data["nav_parent"]="admin_panel"; await admin_users(update,context)
                        elif perm=="broadcast": context.user_data["nav_parent"]="admin_panel"; await admin_broadcast(update,context)
                        elif perm=="moderation": context.user_data["nav_parent"]="admin_panel"; await admin_ban(update,context)
                        elif perm=="ads": context.user_data["nav_parent"]="admin_panel"; await admin_ads(update,context)
                        elif perm=="limits": context.user_data["nav_parent"]="admin_panel"; await admin_limits(update,context)
                        elif perm=="premium": context.user_data["nav_parent"]="admin_panel"; await admin_premium(update,context)
                        elif perm=="maintenance": await toggle_maintenance(update,context)
                        elif perm=="bot_settings": context.user_data["nav_parent"]="admin_panel"; await admin_bot_settings(update,context)
                        return
            if page == "admin_premium":
                context.user_data["nav_parent"]="admin_premium"
                await admin_premium_prices(update,context)
                return
            if page == "admin_premium_limits":
                context.user_data["nav_parent"]="admin_premium_limits"
                await admin_custom_premium_limit(update,context)
                return
            if page == "admin_manage_admins":
                context.user_data["nav_parent"]="admin_manage_admins"
                await admin_permissions_menu(update,context)
                return
            if page == "admin_permissions":
                context.user_data["nav_parent"]="admin_panel"
                await admin_bot_settings(update,context)
                return
            if page == "admin_bot_settings":
                await update.message.reply_text("ℹ️ دي آخر خطوة متاحة في القسم ده.")
                return
        await update.message.reply_text("ℹ️ مفيش خطوة تالية متاحة هنا.")
        return

    # ========================================================
    # الأدمن
    # ========================================================

    if text == "💬 رسائل من الإدارة" and not is_admin(user_id):
        await user_admin_messages(update, context); return
    if text == "📞 التواصل مع الإدارة" and not is_admin(user_id):
        await user_contact_menu(update, context); return
    if text == "👨‍💼 تواصل مع الأدمن" and not is_admin(user_id):
        await start_user_contact(update, context); return

    if is_admin(user_id):

        if text == "📨 رسائل المستخدمين":
            await admin_contact_menu(update, context); return
        if text == "📥 رسائل المستخدمين":
            await admin_contact_inbox(update, context); return
        if text == "📤 إرسال رسالة لمستخدم":
            if not has_admin_permission(user_id,"contact_user"): await update.message.reply_text("❌ غير مصرح لك."); return
            await admin_contact_send_start(update, context); return
        if text == "📢 رسالة جماعية":
            if not has_admin_permission(user_id,"contact_broadcast"): await update.message.reply_text("❌ غير مصرح لك."); return
            await admin_contact_broadcast_start(update, context); return

        if text == "👑 لوحة الأدمن":

            await admin_panel(
                update,
                context
            )

            return

        if text == "🔐 صلاحيات الأدمن":
            context.user_data["admin_submenu"]=True
            context.user_data["nav_parent"]="admin_panel"
            await admin_permissions_menu(update,context); return

        if text == "👥 أعضاء البوت":
            if not has_admin_permission(user_id,"users"): await update.message.reply_text("❌ غير مصرح لك."); return
            context.user_data["admin_submenu"]=True
            context.user_data["nav_parent"]="admin_panel"
            await admin_bot_members(update,context); return

        if text == "🔄 تحديث أعضاء البوت":
            if not has_admin_permission(user_id,"users"): await update.message.reply_text("❌ غير مصرح لك."); return
            await admin_bot_members(update,context); return

        if text == "🤖 إعدادات البوت":
            context.user_data["admin_submenu"]=True
            context.user_data["nav_parent"]="admin_panel"
            await admin_bot_settings(update,context); return

        if text == "✏️ تعديل صلاحيات أدمن":
            if not is_owner(user_id): await update.message.reply_text("❌ المالك الأساسي فقط."); return
            context.user_data["admin_action"]="set_admin_permissions"
            await update.message.reply_text("🔐 ابعت Telegram ID للأدمن اللي عايز تعدل صلاحياته."); return

        if text == "✏️ تغيير اسم البوت":
            if not has_admin_permission(user_id,"bot_settings"): await update.message.reply_text("❌ غير مصرح لك."); return
            context.user_data["admin_action"]="bot_name"
            await update.message.reply_text("✏️ ابعت اسم البوت الجديد (حتى 64 حرف)."); return

        if text == "📝 تغيير وصف البوت":
            if not has_admin_permission(user_id,"bot_settings"): await update.message.reply_text("❌ غير مصرح لك."); return
            context.user_data["admin_action"]="bot_description"
            await update.message.reply_text("📝 ابعت وصف البوت الجديد (حتى 512 حرف)."); return

        if text == "🖼️ تغيير صورة البوت":
            if not has_admin_permission(user_id,"bot_settings"): await update.message.reply_text("❌ غير مصرح لك."); return
            context.user_data["admin_action"]="bot_photo"
            await update.message.reply_text("🖼️ ابعت صورة البوت الجديدة الآن كصورة."); return

        if text == "👑 إدارة الأدمنز":
            context.user_data["nav_parent"]="admin_panel"
            await admin_manage_admins(update, context)
            return

        if text == "👑 إضافة مالك":
            if not is_primary_owner(user_id):
                await update.message.reply_text("❌ المالك الأساسي فقط يقدر يضيف مالك شريك.")
                return
            context.user_data["admin_action"]="add_owner"
            await update.message.reply_text("👑 ابعت Telegram ID للمالك الشريك الجديد.",reply_markup=ReplyKeyboardMarkup([["🔙 رجوع","🏠 القائمة الرئيسية"]],resize_keyboard=True))
            return

        if text == "➖ حذف مالك":
            if not is_primary_owner(user_id):
                await update.message.reply_text("❌ المالك الأساسي فقط يقدر يحذف مالك شريك.")
                return
            context.user_data["admin_action"]="remove_owner"
            await update.message.reply_text("➖ ابعت Telegram ID للمالك الشريك اللي عايز تحذفه.",reply_markup=ReplyKeyboardMarkup([["🔙 رجوع","🏠 القائمة الرئيسية"]],resize_keyboard=True))
            return

        if text == "➕ إضافة أدمن":
            if not is_owner(user_id):
                await update.message.reply_text("❌ المالك الأساسي فقط.")
                return
            context.user_data["admin_action"]="add_admin"
            await update.message.reply_text("➕ ابعت Telegram ID للأدمن الجديد.")
            return

        if text == "➖ حذف أدمن":
            if not is_owner(user_id):
                await update.message.reply_text("❌ المالك الأساسي فقط.")
                return
            context.user_data["admin_action"]="remove_admin"
            await update.message.reply_text("➖ ابعت Telegram ID للأدمن اللي عايز تحذفه.")
            return

        if text == "📋 قائمة الأدمنز":
            context.user_data["nav_parent"]="admin_panel"
            await admin_manage_admins(update, context)
            return

        if text == "📊 الإحصائيات":
            if not has_admin_permission(user_id,"stats"): await update.message.reply_text("❌ غير مصرح لك."); return

            context.user_data["admin_submenu"]=True
            context.user_data["nav_parent"]="admin_panel"
            await admin_stats(
                update,
                context
            )

            return

        if text == "👥 المستخدمين":
            if not has_admin_permission(user_id,"users"): await update.message.reply_text("❌ غير مصرح لك."); return

            context.user_data["admin_submenu"]=True
            context.user_data["nav_parent"]="admin_panel"
            await admin_users(
                update,
                context
            )

            return

        if text == "📢 إذاعة رسالة":
            if not has_admin_permission(user_id,"broadcast"): await update.message.reply_text("❌ غير مصرح لك."); return

            context.user_data["admin_submenu"]=True
            context.user_data["nav_parent"]="admin_panel"
            await admin_broadcast(
                update,
                context
            )

            return

        if text == "🚫 حظر مستخدم":
            if not has_admin_permission(user_id,"moderation"): await update.message.reply_text("❌ غير مصرح لك."); return

            context.user_data["admin_submenu"]=True
            context.user_data["nav_parent"]="admin_panel"
            await admin_ban(
                update,
                context
            )

            return

        if text == "✅ فك حظر":
            if not has_admin_permission(user_id,"moderation"): await update.message.reply_text("❌ غير مصرح لك."); return

            context.user_data["admin_submenu"]=True
            context.user_data["nav_parent"]="admin_panel"
            await admin_unban(
                update,
                context
            )

            return

        if text == "🔎 معلومات مستخدم":
            if not has_admin_permission(user_id,"moderation"): await update.message.reply_text("❌ غير مصرح لك."); return

            context.user_data["admin_submenu"]=True
            context.user_data["nav_parent"]="admin_panel"
            await admin_user_info(
                update,
                context
            )

            return

        if text == "📋 المحظورين":
            if not has_admin_permission(user_id,"moderation"): await update.message.reply_text("❌ غير مصرح لك."); return

            context.user_data["admin_submenu"]=True
            context.user_data["nav_parent"]="admin_panel"
            await admin_banned(
                update,
                context
            )

            return

        if text == "📢 الإعلانات":
            if not has_admin_permission(user_id,"ads"): await update.message.reply_text("❌ غير مصرح لك."); return

            context.user_data["admin_submenu"]=True
            context.user_data["nav_parent"]="admin_panel"
            await admin_ads(
                update,
                context
            )

            return

        if text == "⚙️ حدود التحميل":
            if not has_admin_permission(user_id,"limits"): await update.message.reply_text("❌ غير مصرح لك."); return
            context.user_data["nav_parent"]="admin_panel"
            await admin_limits(update, context)
            return

        if text == "💎 Premium":
            if not has_admin_permission(user_id,"premium"): await update.message.reply_text("❌ غير مصرح لك."); return
            context.user_data["nav_parent"]="admin_panel"
            await admin_premium(update, context)
            return

        if text == "🟢 تشغيل Premium":
            premium_data["enabled"] = True
            save_premium_config(premium_data)
            await update.message.reply_text("🟢 تم تشغيل نظام Premium.")
            return

        if text == "🔴 إيقاف Premium":
            premium_data["enabled"] = False
            save_premium_config(premium_data)
            await update.message.reply_text("🔴 تم إيقاف نظام Premium. الاشتراكات محفوظة.")
            return

        if text == "💰 أسعار Premium":
            context.user_data["nav_parent"]="admin_premium"
            await admin_premium_prices(update, context)
            return

        if text == "💳 إعداد الدفع":
            context.user_data["nav_parent"]="admin_premium"
            await admin_payment_settings(update, context)
            return

        if text == "⚙️ حدود الباقات":
            context.user_data["nav_parent"]="admin_premium"
            await admin_premium_limits(update, context)
            return

        if text == "🎯 حد مشترك":
            await admin_custom_premium_limit(update, context)
            return

        if text == "💎 حد الأسبوع":
            context.user_data["admin_action"]="premium_plan_limit_week"
            await update.message.reply_text("🗓️ حد الباقة الأسبوعية\nابعت: عدد التحميلات + الدقائق\nمثال: 50 60")
            return

        if text == "💎 حد الشهر":
            context.user_data["admin_action"]="premium_plan_limit_month"
            await update.message.reply_text("📅 حد الباقة الشهرية\nابعت: عدد التحميلات + الدقائق\nمثال: 100 60")
            return

        if text == "💎 حد السنة":
            context.user_data["admin_action"]="premium_plan_limit_year"
            await update.message.reply_text("📆 حد الباقة السنوية\nابعت: عدد التحميلات + الدقائق\nمثال: 200 60")
            return

        if text == "💳 تغيير طريقة الدفع":
            context.user_data["admin_action"]="payment_method"
            await update.message.reply_text("💳 ابعت اسم طريقة الدفع، مثال: Vodafone Cash أو InstaPay.")
            return

        if text == "📝 تغيير بيانات الدفع":
            context.user_data["admin_action"]="payment_details"
            await update.message.reply_text("📝 ابعت بيانات الدفع اللي تظهر للمستخدمين (الرقم/الحساب/التعليمات).")
            return

        if text == "📨 طلبات الدفع":
            await admin_payment_requests(update, context)
            return

        if text == "➕ إضافة مشترك":
            context.user_data["admin_action"] = "premium_add"
            await update.message.reply_text("➕ ابعت: ID + أسبوع/شهر/سنة")
            return

        if text == "➖ إزالة مشترك":
            context.user_data["admin_action"] = "premium_remove"
            await update.message.reply_text("➖ ابعت Telegram ID للمشترك.")
            return

        if text == "🔎 حالة Premium":
            context.user_data["admin_action"] = "premium_status"
            await update.message.reply_text("🔎 ابعت Telegram ID.")
            return

        if text == "📋 مشتركو Premium":
            await admin_premium_list(update, context)
            return

        if text == "🔢 حد العادي":
            context.user_data["admin_action"]="normal_limit"
            await update.message.reply_text("🔢 ابعت الحد الأقصى لتحميلات المستخدم العادي.")
            return

        if text == "⏱️ مدة العادي":
            context.user_data["admin_action"]="normal_window"
            await update.message.reply_text("⏱️ ابعت الفترة بالدقائق.")
            return

        if text == "💎 حد Premium":
            context.user_data["admin_action"]="premium_limit"
            await update.message.reply_text("💎 ابعت الحد الأقصى لتحميلات Premium.")
            return

        if text == "⏱️ مدة Premium":
            context.user_data["admin_action"]="premium_window"
            await update.message.reply_text("⏱️ ابعت فترة Premium بالدقائق.")
            return

        if text == "🟢 تفعيل الإعلانات":

            ads_data["enabled"] = True
            save_ads(ads_data)
            await update.message.reply_text("🟢 تم تفعيل الإعلانات بنجاح.")
            return

        if text == "🔴 إيقاف الإعلانات":

            ads_data["enabled"] = False
            save_ads(ads_data)
            await update.message.reply_text("🔴 تم إيقاف الإعلانات.")
            return

        if text == "✏️ تغيير نص الإعلان":

            context.user_data["admin_action"] = "ad_text"
            await update.message.reply_text("✏️ ابعتلي نص الإعلان الجديد.")
            return

        if text == "🔢 عدد التحميلات":

            context.user_data["admin_action"] = "ad_frequency"
            await update.message.reply_text("🔢 ابعت عدد التحميلات الناجحة بين كل إعلان والتاني (مثال: 3).")
            return

        if text == "📊 مشاهدات الإعلانات":

            await update.message.reply_text(
                f"📊 إجمالي مرات ظهور الإعلانات: {ads_data.get('impressions', 0)}"
            )
            return

        if text == "🛠️ وضع الصيانة":
            if not has_admin_permission(user_id,"maintenance"): await update.message.reply_text("❌ غير مصرح لك."); return

            await toggle_maintenance(
                update,
                context
            )

            return

        if await handle_admin_action(
            update,
            context,
            text
        ):

            return

    # ========================================================
    # الحظر
    # ========================================================

    if is_banned(user_id):

        await update.message.reply_text(
            "🚫 تم حظر حسابك من استخدام البوت."
        )

        return

    # ========================================================
    # اختيار TikTok Photo
    # ========================================================

    if text == "🖼️ الصورة فقط":

        if context.user_data.get(
            "tiktok_photo_files"
        ):

            await send_tiktok_photo_only(
                update,
                context
            )

            return

        if context.user_data.get(
            "static_video_path"
        ):

            await send_static_image(
                update,
                context
            )

            return

    if text == "🎬 فيديو":

        if context.user_data.get(
            "tiktok_photo_files"
        ):

            await send_tiktok_photo_as_video(
                update,
                context
            )

            return

        if context.user_data.get(
            "static_video_path"
        ):

            await send_static_video(
                update,
                context
            )

            return

        if context.user_data.get(
            "download_url"
        ):

            context.user_data[
                "download_stage"
            ] = "quality"

            await update.message.reply_text(
                "🎬 تمام، اختار الجودة:",
                reply_markup=ReplyKeyboardMarkup(
                    quality_keyboard(),
                    resize_keyboard=True,
                    one_time_keyboard=False
                )
            )

            return

    if text == "🎵 صوت":

        if context.user_data.get(
            "tiktok_photo_files"
        ):

            await send_tiktok_photo_audio_only(
                update,
                context
            )

            return

        if context.user_data.get(
            "static_video_path"
        ):

            await send_static_audio(
                update,
                context
            )

            return

    if text == "🎵 صوت فقط":

        url = context.user_data.get(
            "download_url"
        )

        if url:

            context.user_data.pop(
                "download_stage",
                None
            )

            await download_audio(
                update,
                context,
                url
            )

            return

    # ========================================================
    # الصورة المصغرة
    # ========================================================

    if text == "🖼️ الصورة المصغرة":

        url = context.user_data.get(
            "download_url"
        )

        if not url:

            await update.message.reply_text(
                "❌ ابعت رابط الفيديو الأول."
            )

            return

        await download_thumbnail(
            update,
            context,
            url
        )

        return

    # ========================================================
    # معلومات الفيديو
    # ========================================================

    if text == "ℹ️ معلومات الفيديو":

        url = context.user_data.get(
            "download_url"
        )

        if not url:

            await update.message.reply_text(
                "❌ ابعت رابط الفيديو الأول."
            )

            return

        await show_video_info(
            update,
            context,
            url
        )

        return

    # ========================================================
    # رجوع خطوة واحدة
    # ========================================================

    if text == "🔙 رجوع":
        stage=context.user_data.get("download_stage")
        if stage=="quality" and context.user_data.get("download_url"):
            context.user_data["download_stage"]="media_choice"
            await update.message.reply_text(
                "🔙 رجعنا خطوة واحدة.\n\nاختار هتستلم إيه:",
                reply_markup=ReplyKeyboardMarkup(media_choice_keyboard(),resize_keyboard=True)
            )
            return
        if stage=="media_choice" and context.user_data.get("download_url"):
            context.user_data["download_stage"]="awaiting_url"
            context.user_data.pop("download_url",None)
            await update.message.reply_text(
                "🔙 رجعنا خطوة واحدة.\n\n🚀 ابعت رابط الفيديو من جديد:",
                reply_markup=ReplyKeyboardMarkup([["🔙 رجوع", "🏠 القائمة الرئيسية"]], resize_keyboard=True)
            )
            return
        if stage=="awaiting_url":
            context.user_data.pop("download_stage",None)
            await show_main_menu(update,context)
            return

        parent=context.user_data.get("nav_parent")
        page=context.user_data.get("nav_page")
        if parent and parent != page:
            context.user_data["nav_page"] = parent
            # صفحة الأب المباشرة
            if parent == "main_menu":
                await show_main_menu(update,context); return
            if parent == "premium_status":
                await user_premium_status(update,context); return
            if parent == "premium_menu":
                context.user_data.pop("premium_change_plan",None)
                await user_premium_menu(update,context); return
            if parent == "admin_panel":
                await admin_panel(update,context); return
            if parent == "admin_premium":
                await admin_premium(update,context); return
            if parent == "admin_limits":
                await admin_limits(update,context); return
            if parent == "admin_premium_limits":
                await admin_premium_limits(update,context); return
            if parent == "admin_payment_settings":
                await admin_payment_settings(update,context); return
            if parent == "admin_permissions":
                context.user_data["nav_parent"]="admin_panel"
                await admin_permissions_menu(update,context); return
            if parent == "admin_bot_settings":
                context.user_data["nav_parent"]="admin_panel"
                await admin_bot_settings(update,context); return
            if parent == "admin_manage_admins":
                await admin_manage_admins(update,context); return
            if parent == "settings":
                await show_settings(update,context); return
            if parent == "user_history":
                await user_history(update,context); return
            if parent == "user_stats":
                await user_stats(update,context); return
            if parent == "help":
                await show_help(update,context); return

        clear_static_state(context,True)
        clear_tiktok_photo_state(context,True)
        context.user_data.pop("download_url",None)
        context.user_data.pop("download_stage",None)
        context.user_data.pop("admin_action",None)
        context.user_data.pop("premium_payment_plan",None)
        context.user_data.pop("premium_change_plan",None)
        await show_main_menu(update,context)
        return

    # ========================================================
    # انطلق
    # ========================================================

    if text == "🚀 انطلق":

        clear_static_state(
            context,
            True
        )

        clear_tiktok_photo_state(
            context,
            True
        )

        context.user_data.pop(
            "download_url",
            None
        )

        context.user_data.pop(
            "download_stage",
            None
        )

        await show_main_menu(
            update,
            context
        )

        return

    # ========================================================
    # السجل
    # ========================================================

    if text == "📜 سجل تحميلاتي":

        await user_history(
            update,
            context
        )

        return

    # ========================================================
    # الإحصائيات
    # ========================================================

    if text == "📊 إحصائياتي":

        await user_stats(
            update,
            context
        )

        return

    # ========================================================
    # المساعدة
    # ========================================================

    if text == "❓ طريقة الاستخدام":

        await show_help(
            update,
            context
        )

        return

    # ========================================================
    # الإعدادات
    # ========================================================

    if text == "⚙️ الإعدادات":

        await show_settings(
            update,
            context
        )

        return

    # ========================================================
    # إعدادات الجودة
    # ========================================================

    if text == "🔥 أعلى جودة افتراضية":

        await change_default_quality(
            update,
            context,
            "🔥 أعلى جودة"
        )

        return

    if text == "⚡ متوسطة افتراضية":

        await change_default_quality(
            update,
            context,
            "⚡ متوسطة"
        )

        return

    if text == "📱 خفيفة افتراضية":

        await change_default_quality(
            update,
            context,
            "📱 خفيفة"
        )

        return

    if text == "🎵 MP3 افتراضي":
        await change_default_quality(update, context, "🎵 صوت فقط MP3")
        return

    if text == "🎬 اسألني كل مرة":
        users[str(user_id)]["default_mode"]="ask"; save_users(users)
        await update.message.reply_text("✅ هسألك كل مرة تختار فيديو ولا صوت.")
        return

    if text == "🎬 فيديو افتراضي":
        users[str(user_id)]["default_mode"]="video"; save_users(users)
        await update.message.reply_text("✅ تم اختيار الفيديو كنوع افتراضي.")
        return

    if text == "🎵 صوت افتراضي":
        users[str(user_id)]["default_mode"]="audio"; save_users(users)
        await update.message.reply_text("✅ تم اختيار الصوت كنوع افتراضي.")
        return

    # ========================================================
    # التحقق من وجود رابط
    # ========================================================

    if not text.startswith(
        (
            "http://",
            "https://"
        )
    ):

        await update.message.reply_text(
            "❌ ابعت رابط فيديو صحيح."
        )

        return

    if not validate_url(text):
        await update.message.reply_text("❌ الرابط مش صحيح. ابعته كامل ويبدأ بـ http:// أو https://")
        return

    limited, wait_seconds = is_rate_limited(user_id)
    if limited and not is_admin(user_id):
        await update.message.reply_text(f"⏳ استنى {wait_seconds} ثانية قبل ما تبعت رابط تاني.")
        return

    users[str(user_id)]["last_url"] = text[:2000]
    save_users(users)

    # ========================================================
    # لو فيه عملية قديمة
    # ========================================================

    old_job = context.user_data.get(
        "static_video_job"
    )

    if old_job:

        clean_job_files(
            old_job
        )

    old_photo_job = context.user_data.get(
        "tiktok_photo_job"
    )

    if old_photo_job:

        clear_tiktok_photo_state(
            context,
            True
        )

    context.user_data.pop(
        "static_video_path",
        None
    )

    context.user_data.pop(
        "static_video_job",
        None
    )

    context.user_data.pop(
        "download_stage",
        None
    )

    # ========================================================
    # منع بدء طلب جديد أثناء تحميل سابق لنفس المستخدم
    # ========================================================

    if user_id in _active_download_users:

        await update.message.reply_text(
            OVERLOAD_MESSAGE
        )

        return

    # ========================================================
    # حفظ الرابط
    # ========================================================

    context.user_data[
        "download_url"
    ] = text

    site = detect_site(
        text
    )

    # ========================================================
    # TikTok:
    # لازم نتأكد الأول هل هو Photo حقيقي أم Video
    # ========================================================

    if (
        "tiktok.com" in text.lower()
        or "vt.tiktok.com" in text.lower()
    ):

        try:

            content_type, final_url = (
                await detect_tiktok_content_type(
                    text
                )
            )

            if content_type == "photo":

                await download_tiktok_photo_post(
                    update,
                    context,
                    text
                )

                return

            # مهم: لو الرابط مختصر، نستخدم الرابط النهائي في باقي مراحل التحميل
            # بدل الرجوع للرابط المختصر مرة أخرى عند اختيار الجودة/الصوت.
            if final_url and validate_url(final_url):
                context.user_data["download_url"] = final_url

            print(
                "🎬 TikTok تم التعرف عليه كفيديو:",
                final_url
            )

        except Exception as e:

            print(
                "⚠️ TikTok type detection failed:",
                e
            )

    # ========================================================
    # فيديو عادي:
    # أولاً فيديو أو صوت
    # ========================================================

    context.user_data[
        "download_stage"
    ] = "media_choice"

    default_mode = users.get(str(user_id), {}).get("default_mode", "ask")
    if default_mode == "audio":
        await download_audio(update, context, text)
        return
    if default_mode == "video":
        context.user_data["download_stage"]="quality"
        await update.message.reply_text("🎬 نوع التحميل الافتراضي: فيديو\nاختار الجودة:", reply_markup=ReplyKeyboardMarkup(quality_keyboard(), resize_keyboard=True))
        return

    await update.message.reply_text(
        f"🔍 تم التعرف على الرابط: {site}\n\n"
        "اختار هتحب تستلم إيه:",
        reply_markup=ReplyKeyboardMarkup(
            media_choice_keyboard(),
            resize_keyboard=True,
            one_time_keyboard=False
        )
    )


# ============================================================
# اختيار الجودة / تنفيذ التحميل
# ============================================================

async def handle_quality_action(
    update,
    context,
    text
):

    quality_heights = {
        "2160p": 2160,
        "1440p": 1440,
        "1080p": 1080,
        "720p": 720,
        "480p": 480,
        "360p": 360,
    }

    height = quality_heights.get(
        text
    )

    if not height:
        return None

    # --------------------------------------------------------
    # اختيار مرن لكل المنصات.
    # بعض المواقع (خصوصًا Facebook / Instagram / X) لا توفر
    # ملف فيديو+صوت جاهز بنفس الـ height، لذلك الاعتماد على
    # b[height<=...] فقط قد يؤدي إلى: format not available.
    # نبدأ بالجودة المطلوبة، ثم نسمح بأي جودة متاحة كحل نهائي.
    # --------------------------------------------------------
    return (
        f"bv*[height<={height}]+ba/"
        f"bv*[height<={height}]/"
        f"b[height<={height}][ext=mp4]/"
        f"b[height<={height}]/"
        "bv*+ba/"
        "bv*/"
        "b[ext=mp4]/"
        "b/"
        "best"
    )



# ============================================================
# استقبال طلبات الدفع Premium
# ============================================================

async def handle_contact_photo(update, context):
    if not update.message or not update.message.photo: return False
    uid=update.effective_user.id
    # مستخدم يرسل للإدارة
    if context.user_data.get("contact_mode")=="user_to_admin":
        await send_contact_to_admin(update,context); return True
    if not is_admin(uid): return False
    action=context.user_data.get("admin_action")
    if action not in ("contact_send_user","contact_reply","contact_broadcast"):
        return False
    target=context.user_data.get("contact_target_user_id") or context.user_data.get("contact_reply_user_id")
    photo_id=update.message.photo[-1].file_id; caption=update.message.caption or ""
    if action in ("contact_send_user","contact_reply") and target:
        perm="contact_user" if action=="contact_send_user" else "contact_inbox"
        if not has_admin_permission(uid,perm): await update.message.reply_text("❌ غير مصرح لك."); return True
        try:
            sent_msg = await context.bot.send_photo(int(target),photo=photo_id,caption=caption)
            asyncio.create_task(delete_admin_message_later(context.bot,int(target),sent_msg.message_id))
            contact_messages.append({"id":uuid.uuid4().hex[:12],"user_id":int(target),"admin_id":int(uid),"name":users.get(str(target),{}).get("name", ""),"created_at":time.time(),"type":"admin_photo","text":caption,"photo_file_id":photo_id,"status":"sent","telegram_message_id":sent_msg.message_id})
            save_contact_messages(contact_messages)
            await update.message.reply_text("✅ تم إرسال الصورة للمستخدم.")
        except Exception as e: await update.message.reply_text("❌ فشل إرسال الصورة للمستخدم."); print("⚠️ Contact photo send error:",e)
        context.user_data.pop("admin_action",None); context.user_data.pop("contact_target_user_id",None); context.user_data.pop("contact_reply_user_id",None); return True
    if action=="contact_broadcast":
        if not has_admin_permission(uid,"contact_broadcast"): await update.message.reply_text("❌ غير مصرح لك."); return True
        context.user_data["contact_broadcast_photo"]=photo_id; context.user_data["contact_broadcast_caption"]=caption; context.user_data["admin_action"]="contact_broadcast_confirm"
        await update.message.reply_text("📢 الصورة جاهزة للإرسال الجماعي.\n⚠️ تأكيد الإرسال؟",reply_markup=ReplyKeyboardMarkup([["✅ تأكيد الإرسال","❌ إلغاء الإرسال"],["🔙 رجوع","🏠 القائمة الرئيسية"]],resize_keyboard=True)); return True
    return False

async def handle_bot_profile_photo(update, context):
    if not update.message or not update.message.photo: return False
    uid=update.effective_user.id
    if context.user_data.get("admin_action")!="bot_photo": return False
    if not has_admin_permission(uid,"bot_settings"):
        context.user_data.pop("admin_action",None)
        await update.message.reply_text("❌ غير مصرح لك.")
        return True
    tmp_path=None
    try:
        tg_file=await context.bot.get_file(update.message.photo[-1].file_id)
        with tempfile.NamedTemporaryFile(delete=False,suffix=".jpg") as tmp: tmp_path=tmp.name
        await tg_file.download_to_drive(custom_path=tmp_path)
        with open(tmp_path,"rb") as f: data=f.read()
        await telegram_api_call("setMyProfilePhoto",data={"photo":json.dumps({"type":"static","photo":"attach://photo"})},files={"photo":("profile.jpg",data,"image/jpeg")})
        context.user_data.pop("admin_action",None)
        await update.message.reply_text("✅ تم تغيير صورة البوت بنجاح.")
    except Exception as e:
        print("⚠️ Bot profile photo error:",type(e).__name__,":",e)
        await update.message.reply_text("❌ حصل خطأ أثناء تغيير صورة البوت. تأكد إن الصورة JPG صالحة وحاول تاني.")
    finally:

        if tmp_path:
            try: os.remove(tmp_path)
            except Exception: pass
    return True


async def handle_payment_photo(update, context):
    uid=update.effective_user.id
    plan=context.user_data.get("premium_payment_plan")
    if not plan or not premium_data.get("enabled",False) or is_admin(uid):
        return
    price=get_premium_price(plan)
    if price is None:
        context.user_data.pop("premium_payment_plan",None)
        context.user_data.pop("premium_change_plan",None)
        await update.message.reply_text("❌ سعر الباقة غير مضبوط حاليًا.")
        return
    rid=uuid.uuid4().hex[:12]
    receipt_file_id=update.message.photo[-1].file_id
    req={
        "user_id":uid,
        "username":update.effective_user.username or "",
        "plan":plan,
        "price":price,
        "reference":"صورة إيصال",
        "receipt_file_id":receipt_file_id,
        "status":"pending",
        "created_at":time.time()
    }
    premium_data.setdefault("payment_requests",{})[rid]=req
    save_premium_config(premium_data)
    context.user_data.pop("premium_payment_plan",None)
    await update.message.reply_text("✅ استلمت صورة إيصال الدفع.\n📨 تم إرسال طلبك للإدارة للمراجعة.\n⏳ استنى تأكيد التفعيل.")
    keyboard=InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ قبول",callback_data=f"payapprove:{rid}"),
        InlineKeyboardButton("❌ رفض",callback_data=f"payreject:{rid}")
    ]])
    caption=(
        f"📨 طلب دفع Premium\n\n"
        f"🆔 المستخدم: {uid}\n"
        f"👤 اليوزر: @{update.effective_user.username or 'بدون'}\n"
        f"📦 الباقة: {premium_plan_display(plan)}\n"
        f"💰 المبلغ: {price} جنيه\n"
        f"🔢 الطلب: {rid}"
    )
    try:
        await context.bot.send_photo(chat_id=ADMIN_ID,photo=receipt_file_id,caption=caption,reply_markup=keyboard)
    except Exception as e:
        print("⚠️ Payment receipt admin send error:",e)


async def message_router(update, context):
    text=(update.message.text or "").strip()
    if context.user_data.get("premium_payment_plan") and not is_admin(update.effective_user.id):
        plan=context.user_data.get("premium_payment_plan")
        price=get_premium_price(plan)
        if price is None:
            context.user_data.pop("premium_payment_plan",None)
            await update.message.reply_text("❌ سعر الباقة غير مضبوط حاليًا.")
            return
        rid=uuid.uuid4().hex[:12]
        req={
            "user_id":update.effective_user.id,
            "username":update.effective_user.username or "",
            "plan":plan,
            "price":price,
            "reference":text[:200] or "بدون رقم",
            "status":"pending",
            "created_at":time.time()
        }
        premium_data.setdefault("payment_requests",{})[rid]=req
        save_premium_config(premium_data)
        context.user_data.pop("premium_payment_plan",None)
        await update.message.reply_text("✅ تم تسجيل طلب الدفع وإرساله للإدارة.\n⏳ بعد مراجعة التحويل هيوصلك تأكيد التفعيل.")
        keyboard=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ قبول",callback_data=f"payapprove:{rid}"),
            InlineKeyboardButton("❌ رفض",callback_data=f"payreject:{rid}")
        ]])
        caption=(
            f"📨 طلب دفع Premium\n\n"
            f"🆔 المستخدم: {update.effective_user.id}\n"
            f"👤 اليوزر: @{update.effective_user.username or 'بدون'}\n"
            f"📦 الباقة: {premium_plan_display(plan)}\n"
            f"💰 المبلغ: {price} جنيه\n"
            f"🔢 مرجع العملية: {text[:200] or 'بدون رقم'}\n"
            f"🆔 الطلب: {rid}"
        )
        try:
            await context.bot.send_message(chat_id=ADMIN_ID,text=caption,reply_markup=keyboard)
        except Exception as e:
            print("⚠️ Payment request admin send error:",e)
        return
    await download_handler(update,context)


# ============================================================
# المعالج النهائي للرسائل
# ============================================================

async def handle_payment_callback(update, context):
    query=update.callback_query
    if not has_admin_permission(query.from_user.id,"premium"):
        await query.answer("❌ غير مصرح لك.", show_alert=True)
        return
    await query.answer()
    data=query.data or ""
    if ":" not in data:
        return
    action, rid=data.split(":",1)
    req=premium_data.get("payment_requests", {}).get(rid)
    if not isinstance(req, dict) or req.get("status") != "pending":
        try:
            await query.message.delete()
        except Exception:
            try: await query.edit_message_text("ℹ️ الطلب غير موجود أو تم التعامل معه بالفعل.")
            except Exception: pass
        return

    uid=str(req.get("user_id"))

    if action == "payapprove":
        plan=req.get("plan")
        plan_key=premium_plan_key(plan)
        days={"week":7,"month":30,"year":365}.get(plan_key)
        if not days:
            try: await query.message.delete()
            except Exception: pass
            return

        now=time.time()
        old=premium_subscription(uid)
        start=max(now,float(old.get("expires_at",now))) if old else now
        expires_at=start+days*86400
        canonical_plan={"week":"أسبوع","month":"شهر","year":"سنة"}[plan_key]
        premium_data.setdefault("subscriptions",{})[uid]={
            "plan":canonical_plan, "started_at":now, "expires_at":expires_at,
            "price":req.get("price",0), "payment_request_id":rid, "payment_reference":req.get("reference","")
        }
        req["status"]="approved"
        req["approved_at"]=now
        req["approved_by"]=query.from_user.id
        save_premium_config(premium_data)

        limit,window,custom=get_premium_limit_config(uid)
        remaining_duration=format_remaining(max(0,int(expires_at-now)))
        try:
            await context.bot.send_message(
                int(uid),
                "🎉💎 مبروك! تم تفعيل Premium بنجاح\n\n"
                f"📦 الباقة: {canonical_plan}\n"
                f"⏳ المدة المضافة: {days} يوم\n"
                f"🕒 المتبقي: {remaining_duration}\n"
                f"📅 تاريخ الانتهاء: {time.strftime('%Y-%m-%d %H:%M', time.localtime(expires_at))}\n\n"
                f"🚀 حد التحميل: {limit} تحميل / {window} دقيقة\n"
                f"{'🎯 الحد ده مخصص لحسابك.' if custom else '💎 الحد ده هو حد باقة Premium.'}\n\n"
                "استمتع بصاروخ التحميل 🚀🔥"
            )
        except Exception as e:
            print("⚠️ Premium approval notify error:",e)

        # الطلب يختفي من لوحة الأدمن بعد التعامل معه.
        try:
            await query.message.delete()
        except Exception:
            try: await query.edit_message_reply_markup(reply_markup=None)
            except Exception: pass
        return

    if action == "payreject":
        now=time.time()
        req["status"]="rejected"
        req["rejected_at"]=now
        req["rejected_by"]=query.from_user.id
        save_premium_config(premium_data)
        try:
            await context.bot.send_message(
                int(uid),
                "❌📨 تم رفض طلب دفع Premium\n\n"
                f"📦 الباقة: {premium_plan_display(req.get('plan'))}\n"
                f"💰 المبلغ: {req.get('price',0)} جنيه\n\n"
                "لو شايف إن فيه خطأ في التحويل أو الإيصال، راجع بيانات الدفع وتواصل مع الإدارة ثم قدّم طلب جديد. 🙏"
            )
        except Exception as e:
            print("⚠️ Premium rejection notify error:",e)

        try:
            await query.message.delete()
        except Exception:
            try: await query.edit_message_reply_markup(reply_markup=None)
            except Exception: pass
        return


# ============================================================
# تشغيل البوت
# ============================================================

async def post_init(application):
    await asyncio.sleep(2)
    cleanup_orphan_files()
    cleanup_cache()
    ensure_daily_stats()
    print("🚀 البوت بدأ...")
    print(f"🛡️ الحد الأقصى للتحميلات المتزامنة: {MAX_CONCURRENT_DOWNLOADS}")


def main():
    app=(
        Application.builder()
        .token(TOKEN)
        .connect_timeout(60)
        .read_timeout(180)
        .write_timeout(180)
        .pool_timeout(60)
        .post_init(post_init)
        .build()
    )
    app.add_handler(CommandHandler("start",start))
    app.add_handler(CommandHandler("admin",admin_panel))
    app.add_handler(CallbackQueryHandler(contact_reply_callback,pattern=r"^contact_reply:"))
    app.add_handler(CallbackQueryHandler(handle_payment_callback,pattern=r"^pay(?:approve|reject):"))
    app.add_handler(PreCheckoutQueryHandler(handle_stars_pre_checkout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT,handle_stars_successful_payment))
    app.add_handler(MessageHandler(filters.PHOTO,handle_contact_photo))
    app.add_handler(MessageHandler(filters.PHOTO,handle_bot_profile_photo))
    app.add_handler(MessageHandler(filters.PHOTO,handle_payment_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,message_router))
    print("🤖 صاروخ التحميل شغال...")
    print(f"👑 الأدمن: {ADMIN_ID}")
    print("👥 البوت مفتوح لجميع المستخدمين.")
    if maintenance_mode:
        print("🛠️ وضع الصيانة: مفعل 🔴")
    if update_pending:
        print("🔧 يوجد تحديث في انتظار إيقاف الصيانة.")
    app.run_polling()


if __name__ == "__main__":
    main()
