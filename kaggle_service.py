import os
import sys
import glob
import json
import time
import re
import html
import logging
import threading
import tempfile
import subprocess
import webbrowser
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
import shutil

# The accelerator-quota endpoint was added to the current Kaggle SDK. Keep the
# runtime self-contained so the dashboard does not depend on whichever older
# Kaggle package happens to be installed globally on the machine.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
KAGGLE_RUNTIME_DIR = os.path.join(BASE_DIR, ".kaggle_runtime")
if os.path.isdir(KAGGLE_RUNTIME_DIR):
    sys.path.insert(0, KAGGLE_RUNTIME_DIR)

if hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

# Ensure dummy credentials before Kaggle import
os.environ.setdefault('KAGGLE_USERNAME', 'dummy')
os.environ.setdefault('KAGGLE_KEY', 'dummy')
from kaggle.api.kaggle_api_extended import KaggleApi
try:
    # The current SDK otherwise prefers KAGGLE_API_TOKEN or ~/.kaggle/access_token
    # over the credential file passed to each request. This dashboard explicitly
    # monitors multiple legacy kaggle.json files, so force each client to use its
    # own username/key pair instead of silently reusing one global token.
    import kagglesdk.kaggle_http_client as _kaggle_http_client
    _kaggle_http_client.get_access_token_from_env = lambda: (None, None)
except Exception:
    pass
from kagglesdk.kernels.services.kernels_api_service import (
    ApiListKernelSessionOutputRequest, 
    ApiGetKernelRequest, 
    ApiGetKernelSessionStatusRequest,
    ApiListKernelFilesRequest,
    ApiListKernelsRequest,
    ApiCancelKernelSessionRequest,
    ApiDeleteKernelRequest,
    ApiSaveKernelRequest,
    ApiSaveKernelResponse,
)
from kagglesdk.kernels.types.kernels_enums import KernelsListSortType, KernelsListViewType
from kagglesdk.datasets.types.dataset_enums import DatasetSelectionGroup, DatasetSortBy
from kagglesdk.datasets.types.dataset_api_service import ApiListDatasetsRequest, ApiUpdateDatasetMetadataRequest, ApiGetDatasetMetadataRequest
from kagglesdk.datasets.types.dataset_types import DatasetSettings, DatasetCollaborator, CollaboratorType, SettingsLicense

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger("kaggle_service")

CRED_DIR = os.path.join(BASE_DIR, "Credentials")
CACHE_FILE = os.path.join(BASE_DIR, "data_cache.json")
QUOTA_FILE = os.path.join(BASE_DIR, "quota_data.json")
HISTORY_FILE = os.path.join(BASE_DIR, "training_history.json")

kaggle_api_lock = threading.Lock()

# Running-state checks are deliberately bounded. Kaggle can return a transient
# rate-limit/permission error when many credentials query sessions at once.
LIVE_SCAN_ACCOUNT_WORKERS = 3
LIVE_SCAN_KERNEL_LIMIT = 10
COLLABORATION_KERNEL_PAGE_LIMIT = 3
ACTIVE_KERNEL_STATUSES = {"RUNNING", "RUNNING_INTERACTIVE", "QUEUED"}

def format_bytes(size):
    if not size or not isinstance(size, (int, float)):
        return "0 B"
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if abs(size) < 1024.0:
            return f"{size:3.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} PB"

def parse_datetime_to_timestamp(dt):
    if not dt:
        return 0.0
    if isinstance(dt, (int, float)):
        return float(dt)
    if isinstance(dt, datetime):
        return dt.timestamp()
    if isinstance(dt, str):
        s = dt.strip()
        if not s:
            return 0.0
        try:
            cleaned = s.replace(" ", "T").rstrip("Z")
            if "+00:00" in cleaned:
                cleaned = cleaned.replace("+00:00", "")
            return datetime.fromisoformat(cleaned).timestamp()
        except Exception:
            return 0.0
    return 0.0

def format_datetime(dt):
    if not dt:
        return ""
    if isinstance(dt, str):
        s = dt.strip()
        if not s:
            return ""
        try:
            cleaned = s.replace(" ", "T").rstrip("Z")
            if "+00:00" in cleaned:
                cleaned = cleaned.replace("+00:00", "")
            parsed = datetime.fromisoformat(cleaned)
            return parsed.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return s
    try:
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(dt)


def _configure_kaggle_api(api, credential):
    """Configure both old and current Kaggle SDK clients without leaking keys."""
    config = {
        "username": str((credential or {}).get("username") or ""),
        "key": str((credential or {}).get("key") or ""),
    }
    # Kaggle >= 2.2 exposes the new quota_view() API and reads config_values
    # directly. Assigning the instance dictionary avoids process-wide env races
    # while the multi-account scanner runs in parallel.
    if hasattr(api, "quota_view"):
        api.config_values = config
        return api

    api._load_config(config)
    return api


def _duration_to_hours(duration):
    if duration is None:
        return None
    try:
        if hasattr(duration, "total_seconds"):
            return duration.total_seconds() / 3600.0
        seconds = getattr(duration, "seconds", None)
        nanos = getattr(duration, "nanos", 0)
        if seconds is not None:
            return (float(seconds) + (float(nanos or 0) / 1_000_000_000.0)) / 3600.0
        if isinstance(duration, (int, float)):
            return float(duration) / 3600.0
    except (TypeError, ValueError, AttributeError):
        return None
    return None


def _duration_to_seconds(duration):
    """Convert Kaggle protobuf duration or timedelta to seconds."""
    if duration is None:
        return 0.0
    try:
        if hasattr(duration, "total_seconds"):
            return float(duration.total_seconds())
        seconds = getattr(duration, "seconds", None)
        nanos = getattr(duration, "nanos", 0)
        if seconds is not None:
            return float(seconds) + (float(nanos or 0) / 1_000_000_000.0)
        if isinstance(duration, (int, float)):
            return float(duration)
        if isinstance(duration, str):
            s = duration.strip().rstrip("s")
            return float(s.split(".")[0])
    except (TypeError, ValueError, AttributeError):
        return 0.0
    return 0.0


def _coerce_bool(value):
    """Convert API boolean values without treating the string 'false' as True."""
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _read_kernel_bool(kernel, camel_name, snake_name, fallback=False):
    """Read an optional SDK boolean while preserving an explicit False.

    The generated Kaggle SDK exposes optional booleans through properties that
    return False even when the field was not present in the response. Inspect
    the backing field first so a missing field can fall back to the cache, but
    an explicit False can never be overwritten by stale cached data.
    """
    private_name = f"_{snake_name}"
    if hasattr(kernel, private_name):
        value = getattr(kernel, private_name)
        return _coerce_bool(fallback) if value is None else _coerce_bool(value)

    for name in (camel_name, snake_name):
        try:
            value = getattr(kernel, name)
        except (AttributeError, TypeError):
            continue
        if value is not None:
            return _coerce_bool(value)

    return _coerce_bool(fallback)


def _read_metadata_bool(metadata, key, fallback=False):
    """Read an optional boolean from a Kaggle metadata dictionary."""
    value = metadata.get(key) if isinstance(metadata, dict) else None
    return _coerce_bool(fallback) if value is None else _coerce_bool(value)


def _kernel_identity(account, ref):
    """Identify a notebook together with the credential being checked."""
    return (str(account or "").strip().lower(), str(ref or "").strip())


def _kernel_owner(ref, fallback=""):
    """Get the notebook owner encoded in a Kaggle ``owner/slug`` ref."""
    ref = str(ref or "").strip()
    return ref.split("/", 1)[0] if "/" in ref else str(fallback or "").strip()


def _score_running_candidate(item, quotas_map):
    """Score a candidate running record when multiple accounts claim the same ref."""
    acc = str(item.get("account") or "").strip()
    ref = str(item.get("ref") or "").strip()
    owner = item.get("owner") or _kernel_owner(ref, acc)
    is_gpu = _coerce_bool(item.get("enable_gpu"))
    is_tpu = _coerce_bool(item.get("enable_tpu"))
    score = 0

    q = quotas_map.get(acc) if isinstance(quotas_map, dict) else {}
    if isinstance(q, dict):
        gpu_rem = float(q.get("gpu_remaining") or 0.0)
        time_res = float(q.get("time_reserved_seconds") or q.get("gpu_reserved_seconds") or 0.0)
        if is_gpu:
            if gpu_rem <= 0.0:
                score -= 1000  # Cannot run GPU with 0 quota remaining
            elif time_res > 0.0:
                score += 1000  # Active GPU reservation confirmed on Kaggle
            elif gpu_rem > 0.0:
                score += 300
        elif is_tpu:
            tpu_rem = float(q.get("tpu_remaining") or 0.0)
            if tpu_rem <= 0.0:
                score -= 1000
            elif tpu_rem > 0.0:
                score += 300

    # In a multi-account setup, when account A shares notebook to account B,
    # it is specifically for account B (the collaborator) to execute it.
    # The owner (account A) sees it running only because they own the notebook on Kaggle.
    if acc.lower() != str(owner or "").lower():
        score += 100

    # Slight tie-breaker for newer run time
    lrt = str(item.get("last_run_time") or "")
    if lrt:
        score += 10

    return score


def _resolve_running_kernels(raw_items, quotas_map=None):
    """Ensure each distinct notebook ref appears at most once in running list.

    When a notebook is shared between multiple accounts (e.g. owner and collaborator),
    both accounts report status RUNNING from Kaggle API. This helper selects the
    single true runner based on active hardware reservation, available quota, and
    collaborator delegation preference.
    """
    by_ref = {}
    for item in raw_items or []:
        if not isinstance(item, dict):
            continue
        ref = str(item.get("ref") or "").strip()
        if not ref:
            continue
        canon = ref.lower()
        by_ref.setdefault(canon, []).append(item)

    resolved = []
    for canon, candidates in by_ref.items():
        if len(candidates) == 1:
            resolved.append(candidates[0])
        else:
            best = max(candidates, key=lambda c: _score_running_candidate(c, quotas_map or {}))
            resolved.append(best)

    return resolved


def _clean_kernel_session_log(raw_log):
    """Decode the stream format returned by Kaggle's session-output endpoint."""
    if not raw_log:
        return ""

    raw_text = raw_log.strip()
    if raw_text.startswith("[") and '{"stream_name"' in raw_text:
        try:
            fixed = raw_text
            if not fixed.endswith("]"):
                fixed = fixed.rstrip(", \r\n") + "]"
            entries = json.loads(fixed)
            return "".join(
                entry.get("data", "")
                for entry in entries
                if isinstance(entry, dict)
            )
        except Exception:
            parts = re.findall(r'"data":\s*"((?:[^"\\]|\\.)*)"', raw_text)
            if parts:
                try:
                    return "".join(
                        bytes(part, "utf-8").decode("unicode_escape", errors="replace")
                        for part in parts
                    )
                except Exception:
                    pass
    return raw_log


def _list_collaboration_kernels(api, *, page_size=100, page_limit=COLLABORATION_KERNEL_PAGE_LIMIT):
    """Return notebooks shared with the credential configured on ``api``.

    The regular ``kernels_list(mine=True)`` call is the account's profile
    group. A notebook shared by another user is in Kaggle's collaboration group
    instead. This only discovers the notebook for that credential; the live
    session status is still checked with that same credential below.
    """
    result = []
    with api.build_kaggle_client() as client:
        for page in range(1, max(1, page_limit) + 1):
            request = ApiListKernelsRequest()
            request.page = page
            request.page_size = page_size
            request.group = KernelsListViewType.COLLABORATION
            request.sort_by = KernelsListSortType.DATE_RUN
            request.user = ""
            request.language = "all"
            request.kernel_type = "all"
            request.output_type = "all"
            request.search = ""
            response = client.kernels.kernels_api_client.list_kernels(request)
            batch = getattr(response, "kernels", None)
            if batch is None:
                batch = response or []
            batch = list(batch or [])
            if not batch:
                break
            result.extend(batch)
            if len(batch) < page_size:
                break
    return result


def time_ago(dt):
    if not dt:
        return "Chưa chạy"
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt.replace('Z', '+00:00'))
        except Exception:
            return dt
    try:
        now = datetime.utcnow()
        if dt.tzinfo:
            now = datetime.now(dt.tzinfo)
        diff = now - dt
        seconds = int(diff.total_seconds())
        if seconds < 0:
            return "Vừa xong"
        if seconds < 60:
            return f"{seconds} giây trước"
        minutes = seconds // 60
        if minutes < 60:
            return f"{minutes} phút trước"
        hours = minutes // 60
        if hours < 24:
            return f"{hours} giờ trước"
        days = hours // 24
        if days < 30:
            return f"{days} ngày trước"
        months = days // 30
        if months < 12:
            return f"{months} tháng trước"
        return f"{months // 12} năm trước"
    except Exception:
        return str(dt)

def get_next_weekly_reset():
    now_utc = datetime.now(timezone.utc)
    days_until_saturday = (5 - now_utc.weekday()) % 7
    if days_until_saturday == 0 and (now_utc.hour > 0 or now_utc.minute > 0):
        days_until_saturday = 7
    target_date = (now_utc + timedelta(days=days_until_saturday)).replace(hour=0, minute=0, second=0, microsecond=0)
    cycle_start = target_date - timedelta(days=7)
    diff = target_date - now_utc
    total_seconds = int(diff.total_seconds())
    days = total_seconds // 86400
    hours = (total_seconds % 86400) // 3600
    minutes = (total_seconds % 3600) // 60
    
    target_vn = target_date + timedelta(hours=7)
    return {
        "reset_date": target_date.strftime("%Y-%m-%d 00:00 UTC"),
        "reset_date_vn": f"{target_vn.strftime('%H:%M')} Thứ Bảy ({target_vn.strftime('%d/%m')})",
        "cycle_start": cycle_start.isoformat(),
        "countdown_human": f"{days} ngày {hours} giờ {minutes} phút",
        "days": days,
        "hours": hours,
        "minutes": minutes
    }

class KaggleMonitorService:
    def __init__(self, cred_dir=CRED_DIR, cache_file=CACHE_FILE, quota_file=QUOTA_FILE):
        self.cred_dir = cred_dir
        self.cache_file = cache_file
        self.quota_file = quota_file
        self.is_refreshing = False
        self.refresh_progress = ""
        self.data = self.load_cache()
        self.quotas = self.load_quota_data()
        self.running_cache = self.extract_running_from_data()
        self.is_scanning_running = False
        self.last_running_scan = None
        self.running_scan_error = ""
        self._running_scan_lock = threading.Lock()
        self.is_scanning_quotas = False
        self.last_quota_scan = None
        self.quota_scan_error = ""
        self._quota_scan_lock = threading.Lock()
        self._cache_write_lock = threading.Lock()
        self._quota_write_lock = threading.Lock()

    def extract_running_from_data(self):
        candidates = []
        sources = [self.data.get("all_kernels", [])]
        sources.extend(
            account.get("kernels", [])
            for account in self.data.get("accounts", [])
        )
        seen_identities = set()
        for source in sources:
            for kernel in source or []:
                if not isinstance(kernel, dict):
                    continue
                ref = kernel.get("ref")
                if not ref:
                    continue
                if not kernel.get("owner"):
                    kernel["owner"] = _kernel_owner(ref, kernel.get("account", ""))
                status = str(kernel.get("status") or "").upper()
                if status in ACTIVE_KERNEL_STATUSES:
                    identity = _kernel_identity(kernel.get("account"), ref)
                    if identity not in seen_identities:
                        seen_identities.add(identity)
                        candidates.append(kernel)

        return _resolve_running_kernels(candidates, getattr(self, "quotas", {}))

    def persist_live_kernel(self, live_kernel):
        """Keep a newly discovered account-scoped live record in the cache."""
        if not isinstance(live_kernel, dict):
            return
        account = live_kernel.get("account")
        ref = live_kernel.get("ref")
        if not account or not ref:
            return
        canon_ref = str(ref or "").strip().lower()
        fields = (
            "account", "owner", "ref", "title", "url", "status",
            "failure_message", "enable_gpu", "enable_tpu", "enable_internet",
            "version_number", "last_run_time", "last_run_human",
        )

        def merge(target):
            for field in fields:
                if field in live_kernel:
                    target[field] = live_kernel[field]

        # In all_kernels, match by unique notebook ref
        global_record = next(
            (
                kernel for kernel in self.data.setdefault("all_kernels", [])
                if str(kernel.get("ref") or "").strip().lower() == canon_ref
            ),
            None,
        )
        if global_record is None:
            self.data["all_kernels"].append(dict(live_kernel))
        else:
            merge(global_record)
            global_record["account"] = account

        # Clean any accidental duplicate records for this ref in all_kernels
        deduped_all = []
        seen_all_refs = set()
        for k in self.data.get("all_kernels", []):
            r = str(k.get("ref") or "").strip().lower()
            if not r or r not in seen_all_refs:
                if r:
                    seen_all_refs.add(r)
                deduped_all.append(k)
        self.data["all_kernels"] = deduped_all
        self.data["all_kernels"].sort(
            key=lambda k: parse_datetime_to_timestamp(k.get("last_run_time")),
            reverse=True,
        )

        for account_data in self.data.get("accounts", []):
            if account_data.get("username") != account:
                continue
            account_record = next(
                (
                    kernel for kernel in account_data.get("kernels", [])
                    if str(kernel.get("ref") or "").strip().lower() == canon_ref
                ),
                None,
            )
            if account_record is None:
                account_data.setdefault("kernels", []).append(dict(live_kernel))
            else:
                merge(account_record)
            account_data["kernels"].sort(
                key=lambda k: parse_datetime_to_timestamp(k.get("last_run_time")),
                reverse=True,
            )
            account_data["kernels_count"] = len(account_data.get("kernels", []))
            break
        if "summary" in self.data:
            self.data["summary"]["total_kernels"] = len(self.data.get("all_kernels", []))

    def get_running_fast(self):
        return self.running_cache

    def load_cache(self):
        if os.path.exists(self.cache_file):
            try:
                with open(self.cache_file, "r", encoding="utf-8") as fp:
                    data = json.load(fp)
                    # Deduplicate all_kernels by ref so legacy duplicates from shared notebooks are purged
                    all_k = data.get("all_kernels", [])
                    if all_k:
                        by_ref = {}
                        for k in all_k:
                            r = (k.get("ref") or "").strip().lower()
                            if not r:
                                continue
                            if r not in by_ref:
                                by_ref[r] = k
                            else:
                                exist = by_ref[r]
                                k_run = k.get("status") in ACTIVE_KERNEL_STATUSES
                                ex_run = exist.get("status") in ACTIVE_KERNEL_STATUSES
                                if k_run and not ex_run:
                                    by_ref[r] = k
                                elif k_run == ex_run and parse_datetime_to_timestamp(k.get("last_run_time")) > parse_datetime_to_timestamp(exist.get("last_run_time")):
                                    by_ref[r] = k
                        deduped_kernels = list(by_ref.values())
                        deduped_kernels.sort(
                            key=lambda k: parse_datetime_to_timestamp(k.get("last_run_time")),
                            reverse=True,
                        )
                        data["all_kernels"] = deduped_kernels
                        if "summary" in data:
                            data["summary"]["total_kernels"] = len(data["all_kernels"])
                    logger.info("Loaded cached data with %d accounts", len(data.get("accounts", [])))
                    return data
            except Exception as e:
                logger.error("Error reading cache: %s", e)
        return {
            "accounts": [],
            "all_kernels": [],
            "all_datasets": [],
            "all_competitions": [],
            "summary": {
                "total_accounts": 0,
                "valid_accounts": 0,
                "total_kernels": 0,
                "running_kernels": 0,
                "queued_kernels": 0,
                "complete_kernels": 0,
                "error_kernels": 0,
                "total_datasets": 0,
                "total_competitions": 0,
            },
            "last_synced": None,
            "last_synced_human": "Chưa đồng bộ"
        }

    def save_cache(self):
        """Write cache atomically and never replace a populated cache with empty data."""
        try:
            with self._cache_write_lock:
                current_accounts = self.data.get("accounts", []) if isinstance(self.data, dict) else []
                if not current_accounts and os.path.exists(self.cache_file):
                    try:
                        with open(self.cache_file, "r", encoding="utf-8") as existing_fp:
                            existing = json.load(existing_fp)
                        if existing.get("accounts"):
                            logger.error("Refusing to overwrite populated cache with empty data")
                            return False
                    except (OSError, ValueError, TypeError):
                        pass

                temp_path = None
                try:
                    with tempfile.NamedTemporaryFile(
                        mode="w",
                        encoding="utf-8",
                        dir=BASE_DIR,
                        prefix=".data_cache_",
                        suffix=".tmp",
                        delete=False,
                    ) as fp:
                        temp_path = fp.name
                        json.dump(self.data, fp, ensure_ascii=False, indent=2, default=str)
                        fp.flush()
                        os.fsync(fp.fileno())
                    os.replace(temp_path, self.cache_file)
                    return True
                finally:
                    if temp_path and os.path.exists(temp_path):
                        try:
                            os.unlink(temp_path)
                        except OSError:
                            pass
        except Exception as e:
            logger.error("Error saving cache: %s", e)
            return False

    def load_quota_data(self):
        if os.path.exists(self.quota_file):
            try:
                with open(self.quota_file, "r", encoding="utf-8") as fp:
                    return json.load(fp)
            except Exception as e:
                logger.error("Error loading quota data: %s", e)
        return {}

    def save_quota_data(self):
        try:
            with self._quota_write_lock:
                temp_path = None
                try:
                    with tempfile.NamedTemporaryFile(
                        mode="w",
                        encoding="utf-8",
                        dir=BASE_DIR,
                        prefix=".quota_data_",
                        suffix=".tmp",
                        delete=False,
                    ) as fp:
                        temp_path = fp.name
                        json.dump(self.quotas, fp, ensure_ascii=False, indent=2)
                        fp.flush()
                        os.fsync(fp.fileno())
                    os.replace(temp_path, self.quota_file)
                    return True
                finally:
                    if temp_path and os.path.exists(temp_path):
                        try:
                            os.unlink(temp_path)
                        except OSError:
                            pass
        except Exception as e:
            logger.error("Error saving quota data: %s", e)
            return False

    def get_credential_files(self):
        if not os.path.exists(self.cred_dir):
            os.makedirs(self.cred_dir, exist_ok=True)
        return sorted(glob.glob(os.path.join(self.cred_dir, "*.json")))

    def get_credential_by_username(self, username):
        for f in self.get_credential_files():
            try:
                with open(f, "r", encoding="utf-8") as fp:
                    c = json.load(fp)
                    if c.get("username") == username:
                        return c
            except Exception:
                continue
        return None

    def get_all_account_usernames(self):
        usernames = []
        for f in self.get_credential_files():
            try:
                with open(f, "r", encoding="utf-8") as fp:
                    c = json.load(fp)
                    u = c.get("username")
                    if u and u not in usernames:
                        usernames.append(u)
            except Exception:
                continue
        if not usernames:
            for a in self.data.get("accounts", []):
                u = a.get("username")
                if u and u not in usernames:
                    usernames.append(u)
        return usernames

    def fetch_user_profile_info(self, username):
        display_name = username
        avatar_url = ""
        bio = ""
        try:
            url = f"https://www.kaggle.com/{username}"
            r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=6)
            if r.status_code == 200:
                m_name = re.search(r'og:title"\s*content="([^"]+)"', r.text)
                if m_name:
                    display_name = html.unescape(m_name.group(1).replace(" | Kaggle", "").strip())
                m_img = re.search(r'twitter:image"\s*content="([^"]+)"', r.text)
                if m_img:
                    avatar_url = m_img.group(1)
                m_bio = re.search(r'og:description"\s*content="([^"]+)"', r.text)
                if m_bio:
                    bio = html.unescape(m_bio.group(1)[:160].strip())
        except Exception as e:
            logger.warning("Could not fetch web profile for %s: %s", username, e)
        return display_name, avatar_url, bio

    def fetch_account_data(self, fpath):
        """Fetch all data for a single account file, with dateRun sorting and live status check."""
        filename = os.path.basename(fpath)
        try:
            with open(fpath, "r", encoding="utf-8") as fp:
                cred = json.load(fp)
        except Exception as e:
            return {
                "file": filename,
                "username": "unknown",
                "display_name": "Lỗi đọc file",
                "valid": False,
                "error_message": f"Không thể đọc file json: {e}",
                "kernels": [],
                "datasets": [],
                "competitions": [],
            }

        username = cred.get("username", "")
        key = cred.get("key", "")
        if not username or not key:
            return {
                "file": filename,
                "username": username or "trống",
                "display_name": "Thiếu thông tin",
                "valid": False,
                "error_message": "File JSON thiếu trường 'username' hoặc 'key'",
                "kernels": [],
                "datasets": [],
                "competitions": [],
            }

        display_name, avatar_url, bio = self.fetch_user_profile_info(username)

        cached_account = next(
            (a for a in self.data.get("accounts", []) if a.get("username") == username),
            {},
        )
        cached_kernels = cached_account.get("kernels", []) or []
        cached_datasets = cached_account.get("datasets", []) or []
        cached_competitions = cached_account.get("competitions", []) or []
        fetch_failures = []

        with kaggle_api_lock:
            api = KaggleApi()
            try:
                _configure_kaggle_api(api, {"username": username, "key": key})
            except Exception as e:
                return {
                    "file": filename,
                    "username": username,
                    "display_name": display_name,
                    "avatar_url": avatar_url,
                    "bio": bio,
                    "valid": False,
                    "error_message": f"Không thể nạp cấu hình: {e}",
                    "kernels": [],
                    "datasets": [],
                    "competitions": [],
                }

            # 1. Fetch kernels sorted by dateRun (paginated so all kernels across pages are retrieved)
            raw_kernels = []
            raw_collaboration_kernels = []
            try:
                page = 1
                while page <= 10:
                    k_batch = api.kernels_list(mine=True, sort_by="dateRun", page=page, page_size=100) or []
                    if not k_batch:
                        break
                    raw_kernels.extend(k_batch)
                    if len(k_batch) < 100:
                        break
                    page += 1
            except Exception as e:
                fetch_failures.append("kernels")
                logger.warning("Error getting kernels for %s: %s", username, e)

            # Shared notebooks are not guaranteed to be included in the
            # profile list. Add the collaboration list under this credential;
            # status is still resolved separately, so access is not treated as
            # proof that this account is the runner.
            try:
                raw_collaboration_kernels = _list_collaboration_kernels(api)
            except Exception as e:
                logger.warning("Error getting shared kernels for %s: %s", username, e)

            # 2. Fetch datasets (paginated mine=True with sort_by='updated' + shared datasets)
            raw_datasets = []
            dataset_refs_seen = set()
            try:
                page = 1
                while page <= 25:
                    ds_batch = api.dataset_list(mine=True, sort_by="updated", page=page) or []
                    if not ds_batch:
                        break
                    for d in ds_batch:
                        ref = getattr(d, "ref", "")
                        if ref and ref not in dataset_refs_seen:
                            dataset_refs_seen.add(ref)
                            raw_datasets.append(d)
                    if len(ds_batch) < 20:
                        break
                    page += 1
            except Exception as e:
                fetch_failures.append("datasets")
                logger.warning("Error getting datasets for %s: %s", username, e)

            # Also query datasets shared with the user
            try:
                with api.build_kaggle_client() as client:
                    req_ds = ApiListDatasetsRequest()
                    req_ds.group = DatasetSelectionGroup.DATASET_SELECTION_GROUP_USER_SHARED_WITH_ME
                    req_ds.sort_by = DatasetSortBy.DATASET_SORT_BY_UPDATED
                    res_ds = client.datasets.dataset_api_client.list_datasets(req_ds)
                    if res_ds and res_ds.datasets:
                        for d in res_ds.datasets:
                            ref = getattr(d, "ref", "")
                            if ref and ref not in dataset_refs_seen:
                                dataset_refs_seen.add(ref)
                                raw_datasets.append(d)
            except Exception as e:
                pass

            # 3. Fetch entered competitions
            raw_competitions = []
            try:
                competitions_response = api.competitions_list(group="entered")
                # kaggle>=2 returns ApiListCompetitionsResponse, while older
                # versions returned the list directly.
                if hasattr(competitions_response, "competitions"):
                    raw_competitions = getattr(competitions_response, "competitions", []) or []
                else:
                    raw_competitions = competitions_response or []
            except Exception as e:
                fetch_failures.append("competitions")
                logger.warning("Error getting competitions for %s: %s", username, e)

            # A transient API failure must never erase a previously populated
            # section of the cache. An empty successful response is still
            # accepted as authoritative, because an account can legitimately
            # have no items in that section.
            if "kernels" in fetch_failures:
                raw_kernels = cached_kernels
            if "datasets" in fetch_failures:
                raw_datasets = cached_datasets
            if "competitions" in fetch_failures:
                raw_competitions = cached_competitions

            known_kernel_refs = {
                getattr(kernel, "ref", "")
                for kernel in raw_kernels
                if getattr(kernel, "ref", "")
            }
            for shared_kernel in raw_collaboration_kernels:
                shared_ref = getattr(shared_kernel, "ref", "")
                if shared_ref and shared_ref not in known_kernel_refs:
                    raw_kernels.append(shared_kernel)
                    known_kernel_refs.add(shared_ref)
            raw_kernels.sort(
                key=lambda kernel: str(
                    getattr(kernel, "lastRunTime", getattr(kernel, "last_run_time", ""))
                    or ""
                ),
                reverse=True,
            )

            # Build lookup of existing known kernel metadata from cache
            existing_cache_map = {}
            for old_acc in self.data.get("accounts", []):
                if old_acc.get("username") == username:
                    for ok in old_acc.get("kernels", []):
                        existing_cache_map[ok.get("ref")] = ok

            # Parse kernels list
            kernels = []
            for k in raw_kernels:
                ref = getattr(k, "ref", "")
                title = getattr(k, "title", ref.split("/")[-1] if "/" in ref else ref)
                owner = _kernel_owner(ref, getattr(k, "author", "") or username)
                last_run_time = getattr(k, "lastRunTime", getattr(k, "last_run_time", None))
                total_votes = getattr(k, "totalVotes", getattr(k, "total_votes", 0))
                is_private = getattr(k, "isPrivate", getattr(k, "is_private", False))

                old_info = existing_cache_map.get(ref, {})
                cached_gpu = old_info.get("enable_gpu", False)
                cached_tpu = old_info.get("enable_tpu", False)
                cached_internet = old_info.get("enable_internet", False)
                cached_version = old_info.get("version_number", 0)

                # Use fresh API values when they are present.  In particular,
                # an explicit False must clear an old True from the cache.
                enable_gpu = _read_kernel_bool(k, "enableGpu", "enable_gpu", cached_gpu)
                enable_tpu = _read_kernel_bool(k, "enableTpu", "enable_tpu", cached_tpu)
                enable_internet = _read_kernel_bool(k, "enableInternet", "enable_internet", cached_internet)
                cur_version = getattr(k, "currentVersionNumber", None)
                if cur_version is None:
                    cur_version = getattr(k, "current_version_number", None)
                if cur_version is None:
                    cur_version = cached_version

                list_status_hint = str(
                    getattr(k, "status", getattr(k, "state", "")) or ""
                ).replace("KernelWorkerStatus.", "").upper()
                initial_status = (
                    list_status_hint
                    if list_status_hint in ACTIVE_KERNEL_STATUSES
                    else ("COMPLETE" if last_run_time else "NOT_CHECKED")
                )
                
                kernel_item = {
                    "account": username,
                    "owner": owner,
                    "ref": ref,
                    "title": title,
                    "url": f"https://www.kaggle.com/code/{ref}",
                    "last_run_time": format_datetime(last_run_time),
                    "last_run_human": time_ago(last_run_time),
                    "total_votes": total_votes,
                    "is_private": bool(is_private),
                    "enable_gpu": bool(enable_gpu),
                    "enable_tpu": bool(enable_tpu),
                    "enable_internet": bool(enable_internet),
                    "version_number": cur_version,
                    "status": initial_status,
                    "failure_message": ""
                }
                kernels.append(kernel_item)

            # Check status & metadata for top 10 most recent kernels (sorted by dateRun)
            try:
                with api.build_kaggle_client() as client:
                    for ki in kernels[:10]:
                        try:
                            stat = api.kernels_status(ki["ref"])
                            stat_str = "COMPLETE"
                            fail_msg = ""
                            if isinstance(stat, dict):
                                stat_str = stat.get("status", "COMPLETE")
                                fail_msg = stat.get("failureMessage", "")
                            elif hasattr(stat, "status"):
                                stat_str = str(getattr(stat, "status", "COMPLETE")).replace("KernelWorkerStatus.", "")
                                fail_msg = getattr(stat, "failure_message", getattr(stat, "failureMessage", ""))
                            else:
                                stat_str = str(stat).replace("KernelWorkerStatus.", "")

                            ki["status"] = stat_str
                            ki["failure_message"] = fail_msg or ""

                            # Always fetch true metadata for recent kernels
                            try:
                                req_meta = ApiGetKernelRequest()
                                owner, slug = ki["ref"].split("/", 1) if "/" in ki["ref"] else (username, ki["ref"])
                                req_meta.user_name = owner
                                req_meta.kernel_slug = slug
                                mres = client.kernels.kernels_api_client.get_kernel(req_meta)
                                if mres and mres.metadata:
                                    meta_d = mres.metadata.to_dict()
                                    ki["enable_gpu"] = _read_metadata_bool(meta_d, "enableGpu", ki["enable_gpu"])
                                    ki["enable_tpu"] = _read_metadata_bool(meta_d, "enableTpu", ki["enable_tpu"])
                                    ki["enable_internet"] = _read_metadata_bool(meta_d, "enableInternet", ki["enable_internet"])
                                    if meta_d.get("currentVersionNumber") is not None:
                                        ki["version_number"] = meta_d["currentVersionNumber"]
                                    if meta_d.get("lastRunTime"):
                                        ki["last_run_time"] = format_datetime(meta_d["lastRunTime"])
                                        ki["last_run_human"] = time_ago(meta_d["lastRunTime"])
                            except Exception:
                                pass
                        except Exception as e:
                            err_str = str(e)
                            if "404" in err_str:
                                ki["status"] = "COMPLETE"
                            else:
                                previous_info = existing_cache_map.get(ki["ref"], {})
                                previous_status = str(previous_info.get("status", "")).upper()
                                ki["status"] = (
                                    previous_status
                                    if previous_status in ACTIVE_KERNEL_STATUSES
                                    else "UNKNOWN"
                                )
                                ki["failure_message"] = previous_info.get("failure_message", "")
            except Exception:
                pass

        # Parse datasets
        datasets = []
        for d in raw_datasets:
            ref = getattr(d, "ref", "")
            title = getattr(d, "title", ref.split("/")[-1] if "/" in ref else ref)
            total_bytes = getattr(d, "totalBytes", getattr(d, "total_bytes", 0))
            last_updated = getattr(d, "lastUpdated", getattr(d, "last_updated", None))
            download_count = getattr(d, "downloadCount", getattr(d, "download_count", 0))
            view_count = getattr(d, "viewCount", getattr(d, "view_count", 0))
            is_private = getattr(d, "isPrivate", getattr(d, "is_private", False))

            datasets.append({
                "account": username,
                "ref": ref,
                "title": title,
                "url": f"https://www.kaggle.com/datasets/{ref}",
                "size_bytes": total_bytes,
                "size_human": format_bytes(total_bytes),
                "download_count": download_count,
                "view_count": view_count,
                "is_private": bool(is_private),
                "last_updated": format_datetime(last_updated),
                "last_updated_human": time_ago(last_updated)
            })

        # Parse competitions
        competitions = []
        for c in raw_competitions:
            ref = getattr(c, "ref", "")
            url = getattr(c, "url", ref)
            title = getattr(c, "title", ref)
            reward = getattr(c, "reward", "Knowledge")
            deadline = getattr(c, "deadline", None)
            team_count = getattr(c, "teamCount", getattr(c, "team_count", 0))
            user_rank = getattr(c, "userRank", getattr(c, "user_rank", 0))
            category = getattr(c, "category", "")

            competitions.append({
                "account": username,
                "ref": ref,
                "title": title,
                "url": url,
                "reward": reward,
                "deadline": format_datetime(deadline),
                "deadline_human": time_ago(deadline),
                "team_count": team_count,
                "user_rank": user_rank,
                "category": category
            })

        return {
            "file": filename,
            "username": username,
            "display_name": display_name,
            "avatar_url": avatar_url,
            "bio": bio,
            "valid": True,
            "error_message": "",
            "kernels_count": len(kernels),
            "datasets_count": len(datasets),
            "competitions_count": len(competitions),
            "kernels": kernels,
            "datasets": datasets,
            "competitions": competitions,
            "_fetch_failures": fetch_failures,
        }

    def fetch_datasets_live(self, target_account=None):
        """Fetch datasets directly from Kaggle API in real-time in parallel across accounts."""
        files = self.get_credential_files()
        if target_account and target_account.strip():
            matched = []
            for f in files:
                try:
                    with open(f, "r", encoding="utf-8") as fp:
                        c = json.load(fp)
                        if c.get("username") == target_account.strip():
                            matched.append(f)
                except Exception:
                    pass
            if matched:
                files = matched

        def fetch_acc_datasets(fpath):
            try:
                with open(fpath, "r", encoding="utf-8") as fp:
                    cred = json.load(fp)
            except Exception:
                return []

            username = cred.get("username", "")
            if not username:
                return []

            api = KaggleApi()
            try:
                _configure_kaggle_api(api, cred)
            except Exception:
                return []

            acc_datasets = []
            seen_refs = set()

            # 1. Paginated mine=True with sort_by='updated'
            page = 1
            while page <= 25:
                try:
                    ds_batch = api.dataset_list(mine=True, sort_by="updated", page=page) or []
                except Exception as e:
                    logger.warning("Live dataset fetch error for %s page %d: %s", username, page, e)
                    break
                if not ds_batch:
                    break
                for d in ds_batch:
                    ref = getattr(d, "ref", "")
                    if ref and ref not in seen_refs:
                        seen_refs.add(ref)
                        title = getattr(d, "title", ref.split("/")[-1] if "/" in ref else ref)
                        total_bytes = getattr(d, "totalBytes", getattr(d, "total_bytes", 0))
                        last_updated = getattr(d, "lastUpdated", getattr(d, "last_updated", None))
                        download_count = getattr(d, "downloadCount", getattr(d, "download_count", 0))
                        view_count = getattr(d, "viewCount", getattr(d, "view_count", 0))
                        is_private = getattr(d, "isPrivate", getattr(d, "is_private", False))

                        acc_datasets.append({
                            "account": username,
                            "ref": ref,
                            "title": title,
                            "url": f"https://www.kaggle.com/datasets/{ref}",
                            "size_bytes": total_bytes,
                            "size_human": format_bytes(total_bytes),
                            "download_count": download_count,
                            "view_count": view_count,
                            "is_private": bool(is_private),
                            "last_updated": format_datetime(last_updated),
                            "last_updated_human": time_ago(last_updated)
                        })
                if len(ds_batch) < 20:
                    break
                page += 1

            # 2. Check datasets shared with user
            try:
                with api.build_kaggle_client() as client:
                    req_ds = ApiListDatasetsRequest()
                    req_ds.group = DatasetSelectionGroup.DATASET_SELECTION_GROUP_USER_SHARED_WITH_ME
                    req_ds.sort_by = DatasetSortBy.DATASET_SORT_BY_UPDATED
                    res_ds = client.datasets.dataset_api_client.list_datasets(req_ds)
                    if res_ds and res_ds.datasets:
                        for d in res_ds.datasets:
                            ref = getattr(d, "ref", "")
                            if ref and ref not in seen_refs:
                                seen_refs.add(ref)
                                title = getattr(d, "title", ref.split("/")[-1] if "/" in ref else ref)
                                total_bytes = getattr(d, "total_bytes", 0)
                                last_updated = getattr(d, "last_updated", None)
                                download_count = getattr(d, "download_count", 0)
                                view_count = getattr(d, "view_count", 0)
                                is_private = getattr(d, "is_private", False)

                                acc_datasets.append({
                                    "account": username,
                                    "ref": ref,
                                    "title": title,
                                    "url": f"https://www.kaggle.com/datasets/{ref}",
                                    "size_bytes": total_bytes,
                                    "size_human": format_bytes(total_bytes),
                                    "download_count": download_count,
                                    "view_count": view_count,
                                    "is_private": bool(is_private),
                                    "last_updated": format_datetime(last_updated),
                                    "last_updated_human": time_ago(last_updated)
                                })
            except Exception:
                pass

            return acc_datasets

        with ThreadPoolExecutor(max_workers=min(10, max(1, len(files)))) as ex:
            results = list(ex.map(fetch_acc_datasets, files))

        live_datasets = [item for sublist in results for item in sublist]
        live_datasets.sort(key=lambda x: x.get("last_updated") or "", reverse=True)

        # Update cache in memory & file
        if target_account and target_account.strip():
            target_clean = target_account.strip()
            existing = [d for d in self.data.get("all_datasets", []) if d.get("account") != target_clean]
            combined = existing + live_datasets
            combined.sort(key=lambda x: x.get("last_updated") or "", reverse=True)
            self.data["all_datasets"] = combined
            for acc in self.data.get("accounts", []):
                if acc.get("username") == target_clean:
                    acc["datasets"] = live_datasets
                    acc["datasets_count"] = len(live_datasets)
        else:
            self.data["all_datasets"] = live_datasets
            acc_map = {}
            for d in live_datasets:
                acc_map.setdefault(d["account"], []).append(d)
            for acc in self.data.get("accounts", []):
                u = acc.get("username")
                acc["datasets"] = acc_map.get(u, [])
                acc["datasets_count"] = len(acc["datasets"])

        if "summary" in self.data:
            self.data["summary"]["total_datasets"] = len(self.data.get("all_datasets", []))

        self.save_cache()
        return live_datasets

    def scan_active_running_kernels(self, allow_during_refresh=False):
        """Check every credential while keeping notebook owner and runner separate."""
        if self.is_refreshing and not allow_during_refresh:
            return self.running_cache
        if not self._running_scan_lock.acquire(blocking=False):
            return self.running_cache

        self.is_scanning_running = True
        self.running_scan_error = ""

        try:
            files = self.get_credential_files()

            def update_cached_kernel(account, ref, status, fail_msg, gpu_flag, tpu_flag,
                                     internet_flag, cur_version, last_run_time=None):
                canon_ref = str(ref or "").strip().lower()
                fmt_time = format_datetime(last_run_time)
                ts_new = parse_datetime_to_timestamp(last_run_time)

                for ck in self.data.get("all_kernels", []):
                    ck_canon = str(ck.get("ref") or "").strip().lower()
                    if ck_canon == canon_ref:
                        ck["status"] = status
                        ck["failure_message"] = fail_msg
                        ck["enable_gpu"] = gpu_flag
                        ck["enable_tpu"] = tpu_flag
                        ck["enable_internet"] = internet_flag
                        ck["version_number"] = cur_version
                        if fmt_time:
                            ts_old = parse_datetime_to_timestamp(ck.get("last_run_time"))
                            if ts_new >= ts_old:
                                ck["last_run_time"] = fmt_time
                                ck["last_run_human"] = time_ago(last_run_time)
                                if status in ACTIVE_KERNEL_STATUSES:
                                    ck["account"] = account

                for acc in self.data.get("accounts", []):
                    if acc.get("username") != account:
                        continue
                    for ck in acc.get("kernels", []):
                        ck_canon = str(ck.get("ref") or "").strip().lower()
                        if ck_canon == canon_ref:
                            ck["status"] = status
                            ck["failure_message"] = fail_msg
                            ck["enable_gpu"] = gpu_flag
                            ck["enable_tpu"] = tpu_flag
                            ck["enable_internet"] = internet_flag
                            ck["version_number"] = cur_version
                            if fmt_time:
                                ck["last_run_time"] = fmt_time
                                ck["last_run_human"] = time_ago(last_run_time)

            def check_one(fpath):
                user = ""
                try:
                    with open(fpath, "r", encoding="utf-8") as fp:
                        cred = json.load(fp)
                    user = cred.get("username", "")
                    api = KaggleApi()
                    _configure_kaggle_api(api, cred)
                    found = []
                    status_errors = []

                    # dateRun is enough to put active sessions near the front,
                    # but inspect more than five so an older active notebook is
                    # not left behind in the cached running state.
                    kernels = api.kernels_list(
                        mine=True, sort_by="dateRun", page_size=100
                    ) or []
                    try:
                        shared_kernels = _list_collaboration_kernels(api)
                    except Exception as exc:
                        logger.info("Shared kernel list unavailable for %s: %s", user, exc)
                        shared_kernels = []
                    known_refs = {getattr(k, "ref", "") for k in kernels}
                    for shared_kernel in shared_kernels:
                        shared_ref = getattr(shared_kernel, "ref", "")
                        if shared_ref and shared_ref not in known_refs:
                            kernels.append(shared_kernel)
                            known_refs.add(shared_ref)
                    kernels.sort(
                        key=lambda kernel: str(
                            getattr(kernel, "lastRunTime", getattr(kernel, "last_run_time", ""))
                            or ""
                        ),
                        reverse=True,
                    )
                    candidate_kernels = []
                    candidate_refs = set()
                    for candidate in [*kernels[:LIVE_SCAN_KERNEL_LIMIT], *shared_kernels[:LIVE_SCAN_KERNEL_LIMIT]]:
                        candidate_ref = getattr(candidate, "ref", "")
                        if candidate_ref and candidate_ref not in candidate_refs:
                            candidate_kernels.append(candidate)
                            candidate_refs.add(candidate_ref)
                    for k in candidate_kernels:
                        ref = getattr(k, "ref", "")
                        if not ref:
                            continue

                        cached_info = next(
                            (
                                cached_kernel
                                for cached_kernel in self.data.get("all_kernels", [])
                                if _kernel_identity(cached_kernel.get("account"), cached_kernel.get("ref"))
                                == _kernel_identity(user, ref)
                            ),
                            {},
                        )
                        if not cached_info:
                            cached_info = next(
                                (
                                    cached_kernel
                                    for cached_kernel in self.data.get("all_kernels", [])
                                    if not cached_kernel.get("account") and cached_kernel.get("ref") == ref
                                ),
                                {},
                            )

                        gpu_flag = _read_kernel_bool(
                            k, "enableGpu", "enable_gpu", cached_info.get("enable_gpu", False)
                        )
                        tpu_flag = _read_kernel_bool(
                            k, "enableTpu", "enable_tpu", cached_info.get("enable_tpu", False)
                        )
                        internet_flag = _read_kernel_bool(
                            k, "enableInternet", "enable_internet",
                            cached_info.get("enable_internet", False)
                        )
                        cur_version = getattr(k, "currentVersionNumber", None)
                        if cur_version is None:
                            cur_version = getattr(k, "current_version_number", None)
                        if cur_version is None:
                            cur_version = cached_info.get("version_number", 0)

                        try:
                            stat = api.kernels_status(ref)
                            stat_str = str(getattr(stat, "status", stat)).replace(
                                "KernelWorkerStatus.", ""
                            ).upper()
                            fail_msg = getattr(
                                stat, "failure_message", getattr(stat, "failureMessage", "")
                            ) or ""
                        except Exception:
                            previous_status = str(cached_info.get("status", "")).upper()
                            status_hint = str(
                                getattr(k, "status", getattr(k, "state", "")) or ""
                            ).replace("KernelWorkerStatus.", "").upper()
                            if previous_status in ACTIVE_KERNEL_STATUSES:
                                stat_str = previous_status
                                fail_msg = cached_info.get("failure_message", "")
                            elif status_hint in ACTIVE_KERNEL_STATUSES:
                                stat_str = status_hint
                                fail_msg = ""
                            else:
                                stat_str = "UNKNOWN"
                                fail_msg = ""
                                status_errors.append(ref)

                        # Metadata is only needed for a running card. Avoid a
                        # second request for every historical candidate during
                        # the multi-account status scan.
                        if stat_str in ACTIVE_KERNEL_STATUSES:
                            try:
                                with api.build_kaggle_client() as client:
                                    rm = ApiGetKernelRequest()
                                    owner, slug = ref.split("/", 1) if "/" in ref else (user, ref)
                                    rm.user_name = owner
                                    rm.kernel_slug = slug
                                    metadata_response = client.kernels.kernels_api_client.get_kernel(rm)
                                    if metadata_response and metadata_response.metadata:
                                        metadata = metadata_response.metadata.to_dict()
                                        gpu_flag = _read_metadata_bool(metadata, "enableGpu", gpu_flag)
                                        tpu_flag = _read_metadata_bool(metadata, "enableTpu", tpu_flag)
                                        internet_flag = _read_metadata_bool(
                                            metadata, "enableInternet", internet_flag
                                        )
                                        if metadata.get("currentVersionNumber") is not None:
                                            cur_version = metadata["currentVersionNumber"]
                            except Exception:
                                pass

                        last_run_time = getattr(k, "lastRunTime", None) or getattr(k, "last_run_time", None)
                        title = getattr(k, "title", ref.split("/")[-1])
                        update_cached_kernel(
                            user, ref, stat_str, fail_msg, gpu_flag, tpu_flag,
                            internet_flag, cur_version, last_run_time=last_run_time
                        )

                        if stat_str in {"RUNNING", "RUNNING_INTERACTIVE", "QUEUED"}:
                            found.append({
                                "account": user,
                                "owner": _kernel_owner(ref, user),
                                "ref": ref,
                                "title": title,
                                "url": f"https://www.kaggle.com/code/{ref}",
                                "status": stat_str,
                                "failure_message": fail_msg,
                                "enable_gpu": gpu_flag,
                                "enable_tpu": tpu_flag,
                                "enable_internet": internet_flag,
                                "version_number": cur_version,
                                "last_run_time": format_datetime(last_run_time),
                                "last_run_human": time_ago(last_run_time)
                            })
                    return {
                        "username": user,
                        "items": found,
                        "status_errors": status_errors,
                        "success": True,
                    }
                except Exception as exc:
                    logger.warning("Live running scan failed for %s: %s", fpath, exc)
                    return {"username": user, "items": [], "success": False}

            with ThreadPoolExecutor(
                max_workers=min(LIVE_SCAN_ACCOUNT_WORKERS, max(1, len(files)))
            ) as ex:
                results = list(ex.map(check_one, files))

            failed_users = {
                result.get("username")
                for result in results
                if not result.get("success") and result.get("username")
            }
            raw_candidates = [
                item
                for result in results
                for item in result.get("items", [])
                if item.get("account") and item.get("ref")
            ]
            if failed_users:
                # Keep the last known active sessions for accounts that could
                # not be reached. A network/API error is not evidence that a
                # running notebook stopped.
                for item in self.running_cache:
                    if item.get("account") in failed_users and item.get("ref"):
                        raw_candidates.append(item)
                self.running_scan_error = (
                    "Không thể quét live cho: " + ", ".join(sorted(failed_users))
                )
            status_error_count = sum(len(result.get("status_errors", [])) for result in results)
            if status_error_count:
                prefix = f"{self.running_scan_error}; " if self.running_scan_error else ""
                self.running_scan_error = (
                    f"{prefix}{status_error_count} notebook chưa lấy được trạng thái mới; giữ cache cũ"
                )
            active_list = _resolve_running_kernels(raw_candidates, getattr(self, "quotas", {}))
            self.running_cache = active_list
            for live_kernel in active_list:
                self.persist_live_kernel(live_kernel)

            if self.data and "all_kernels" in self.data:
                self.data["all_kernels"].sort(
                    key=lambda k: parse_datetime_to_timestamp(k.get("last_run_time")),
                    reverse=True,
                )

            if self.data and "summary" in self.data:
                self.data["summary"]["running_kernels"] = len(active_list)
                self.data["summary"]["queued_kernels"] = sum(
                    1 for item in active_list if item.get("status") == "QUEUED"
                )
                self.save_cache()

            return active_list
        except Exception as exc:
            self.running_scan_error = str(exc)
            logger.warning("Live running scan failed: %s", exc)
            return self.running_cache
        finally:
            self.last_running_scan = datetime.now(timezone.utc).isoformat()
            self.is_scanning_running = False
            self._running_scan_lock.release()

    def check_single_kernel_status(self, account_username, kernel_ref):
        target_cred = self.get_credential_by_username(account_username)
        if not target_cred:
            return {"success": False, "message": "Không tìm thấy credential cho tài khoản này"}

        with kaggle_api_lock:
            api = KaggleApi()
            _configure_kaggle_api(api, target_cred)
            try:
                stat = api.kernels_status(kernel_ref)
                status_str = str(getattr(stat, "status", stat)).replace("KernelWorkerStatus.", "")
                failure_msg = getattr(stat, "failure_message", getattr(stat, "failureMessage", ""))

                target_identity = _kernel_identity(account_username, kernel_ref)
                for k in self.data.get("all_kernels", []):
                    if _kernel_identity(k.get("account"), k.get("ref")) == target_identity or (
                        not k.get("account") and k.get("ref") == kernel_ref
                    ):
                        k["status"] = status_str
                        k["failure_message"] = failure_msg
                for acc in self.data.get("accounts", []):
                    if acc.get("username") != account_username:
                        continue
                    for k in acc.get("kernels", []):
                        if _kernel_identity(k.get("account"), k.get("ref")) == target_identity or (
                            not k.get("account") and k.get("ref") == kernel_ref
                        ):
                            k["status"] = status_str
                            k["failure_message"] = failure_msg

                self.save_cache()
                return {
                    "success": True,
                    "ref": kernel_ref,
                    "status": status_str,
                    "failure_message": failure_msg
                }
            except Exception as e:
                return {"success": False, "message": str(e)}

    def _fetch_kernel_stream_log(self, cred, owner, slug, max_lines=300):
        """Fetch live Server-Sent Events (SSE) console stream for running or shared notebooks."""
        if not cred:
            return ""
        api = KaggleApi()
        _configure_kaggle_api(api, cred)
        resp = None
        chunks = []
        try:
            with api.build_kaggle_client() as kaggle:
                http = kaggle._http_client
                http._init_session()
                base = http._endpoint if getattr(http, "_endpoint", None) else "https://api.kaggle.com"
                url = f"{base}/v1/kernels/logs/stream/{owner}/{slug}"
                headers = dict(http._session.headers)
                headers["Accept"] = "text/event-stream, */*"
                headers.pop("Content-Type", None)

                resp = http._session.get(url, stream=True, headers=headers, auth=http._session.auth, timeout=(3.0, 1.2))
                for line in resp.iter_lines(decode_unicode=True):
                    if not line or not line.startswith("data:"):
                        continue
                    payload = line[len("data:"):].lstrip()
                    if payload == "END_OF_LOG":
                        break
                    try:
                        data = json.loads(payload)
                        if isinstance(data, dict) and "data" in data:
                            chunks.append(data["data"])
                    except Exception:
                        chunks.append(payload)
                    if len(chunks) >= max_lines:
                        break
        except Exception:
            # Live SSE terminates by buffer boundary or timeout, which is expected
            pass
        finally:
            if resp:
                try:
                    resp.close()
                except Exception:
                    pass
        return "".join(chunks)

    def get_kernel_live_output(self, account_username, kernel_ref, check_status=False):
        """Fetch the current session status and console log for a running notebook."""
        target_cred = self.get_credential_by_username(account_username)
        if not target_cred:
            return {"success": False, "message": "Không tìm thấy credential cho tài khoản này"}

        owner, slug = (
            kernel_ref.split("/", 1) if "/" in kernel_ref
            else (account_username, kernel_ref)
        )

        api = KaggleApi()
        _configure_kaggle_api(api, target_cred)
        try:
            status_str = ""
            failure_msg = ""
            status_checked = False
            raw_log = ""

            with api.build_kaggle_client() as client:
                if check_status:
                    req_status = ApiGetKernelSessionStatusRequest()
                    req_status.user_name = owner
                    req_status.kernel_slug = slug
                    try:
                        status_response = client.kernels.kernels_api_client.get_kernel_session_status(req_status)
                        status_str = str(getattr(status_response, "status", "")).replace(
                            "KernelWorkerStatus.", ""
                        ).upper()
                        failure_msg = getattr(
                            status_response,
                            "failure_message",
                            getattr(status_response, "failureMessage", ""),
                        ) or ""
                        status_checked = bool(status_str)
                    except Exception as exc:
                        logger.info("Could not refresh live status for %s: %s", kernel_ref, exc)

                req_output = ApiListKernelSessionOutputRequest()
                req_output.user_name = owner
                req_output.kernel_slug = slug
                try:
                    output_response = client.kernels.kernels_api_client.list_kernel_session_output(req_output)
                    raw_log = getattr(output_response, "log", "") or ""
                except Exception as exc:
                    logger.info("Could not fetch output log for %s via %s: %s", kernel_ref, account_username, exc)

            # If log is empty or notebook is active, fetch from live SSE stream
            if not raw_log or (status_str in ACTIVE_KERNEL_STATUSES):
                stream_log = self._fetch_kernel_stream_log(target_cred, owner, slug)
                if not stream_log and owner != account_username:
                    owner_cred = self.get_credential_by_username(owner)
                    if owner_cred:
                        stream_log = self._fetch_kernel_stream_log(owner_cred, owner, slug)
                if not stream_log:
                    running_item = next(
                        (k for k in self.running_cache if str(k.get("ref") or "").strip().lower() == kernel_ref.strip().lower()),
                        None
                    )
                    if running_item and running_item.get("account") and running_item.get("account") not in (account_username, owner):
                        alt_cred = self.get_credential_by_username(running_item.get("account"))
                        if alt_cred:
                            stream_log = self._fetch_kernel_stream_log(alt_cred, owner, slug)
                if stream_log:
                    raw_log = stream_log

            # Fallback to owner static output if still empty and queried with collaborator
            if not raw_log and owner != account_username:
                owner_cred = self.get_credential_by_username(owner)
                if owner_cred:
                    try:
                        owner_api = KaggleApi()
                        _configure_kaggle_api(owner_api, owner_cred)
                        with owner_api.build_kaggle_client() as o_client:
                            o_out_res = o_client.kernels.kernels_api_client.list_kernel_session_output(req_output)
                            if o_out_res and getattr(o_out_res, "log", None):
                                raw_log = o_out_res.log
                    except Exception:
                        pass

            log_clean = _clean_kernel_session_log(raw_log)

            is_active = status_str in ACTIVE_KERNEL_STATUSES if status_checked else None
            if not log_clean and is_active:
                log_clean = (
                    f"⚡ PHIÊN CHẠY ĐANG HOẠT ĐỘNG ({status_str})\n"
                    f"Tài khoản thực thi: @{account_username} | Chủ sở hữu: @{owner}\n"
                    f"[Ghi chú]: Kaggle đang khởi tạo worker container hoặc chuẩn bị truyền dữ liệu console.\n"
                    f"Dashboard đang tự động theo dõi và cập nhật logs mỗi 5 giây..."
                )

            return {
                "success": True,
                "ref": kernel_ref,
                "status": status_str,
                "status_checked": status_checked,
                "is_active": is_active,
                "failure_message": failure_msg,
                "output": {
                    "log": log_clean,
                    "log_length": len(log_clean),
                },
            }
        except Exception as e:
            return {"success": False, "message": str(e)}

    def get_kernel_details(self, account_username, kernel_ref, version=None):
        target_cred = self.get_credential_by_username(account_username)
        if not target_cred:
            return {"success": False, "message": "Không tìm thấy credential cho tài khoản này"}

        if "/" in kernel_ref:
            owner, slug = kernel_ref.split("/", 1)
        else:
            owner = account_username
            slug = kernel_ref

        v_label = f"v{version}" if version is not None and str(version).isdigit() else (str(version) if version is not None else None)

        with kaggle_api_lock:
            api = KaggleApi()
            _configure_kaggle_api(api, target_cred)
            try:
                with api.build_kaggle_client() as client:
                    # 1. Status
                    req_status = ApiGetKernelSessionStatusRequest()
                    req_status.user_name = owner
                    req_status.kernel_slug = slug
                    status_str = "COMPLETE"
                    failure_msg = ""
                    try:
                        stat_res = client.kernels.kernels_api_client.get_kernel_session_status(req_status)
                        status_str = str(stat_res.status).replace("KernelWorkerStatus.", "")
                        failure_msg = getattr(stat_res, "failure_message", getattr(stat_res, "failureMessage", ""))
                    except Exception:
                        pass

                    # 2. Metadata & Inputs & Source Code
                    req_meta = ApiGetKernelRequest()
                    req_meta.user_name = owner
                    req_meta.kernel_slug = slug
                    if v_label:
                        req_meta.version_label = v_label
                    meta = {}
                    blob_source = ""
                    try:
                        meta_res = client.kernels.kernels_api_client.get_kernel(req_meta)
                        if meta_res:
                            if meta_res.metadata:
                                meta = meta_res.metadata.to_dict()
                            if hasattr(meta_res, "blob") and meta_res.blob:
                                blob_source = getattr(meta_res.blob, "source", "") or ""
                    except Exception as e:
                        logger.warning("Could not get kernel metadata/source: %s", e)

                    # 3. Output logs and session files
                    req_out = ApiListKernelSessionOutputRequest()
                    req_out.user_name = owner
                    req_out.kernel_slug = slug
                    if v_label:
                        req_out.version_label = v_label
                    out_log_raw = ""
                    out_files = []
                    try:
                        out_res = client.kernels.kernels_api_client.list_kernel_session_output(req_out)
                        if out_res:
                            out_log_raw = out_res.log or ""
                            if out_res.files:
                                for f in out_res.files:
                                    out_files.append({
                                        "file_name": f.file_name,
                                        "size": "",
                                        "creation_date": "",
                                        "url": getattr(f, "url", ""),
                                    })
                    except Exception as e:
                        logger.warning("Could not get kernel output: %s", e)

                    # If log is empty or notebook is active, fetch from live SSE stream
                    if not out_log_raw or (status_str in ACTIVE_KERNEL_STATUSES):
                        stream_log = self._fetch_kernel_stream_log(target_cred, owner, slug)
                        if not stream_log and owner != account_username:
                            owner_cred = self.get_credential_by_username(owner)
                            if owner_cred:
                                stream_log = self._fetch_kernel_stream_log(owner_cred, owner, slug)
                        if not stream_log:
                            running_item = next(
                                (k for k in self.running_cache if str(k.get("ref") or "").strip().lower() == kernel_ref.strip().lower()),
                                None
                            )
                            if running_item and running_item.get("account") and running_item.get("account") not in (account_username, owner):
                                alt_cred = self.get_credential_by_username(running_item.get("account"))
                                if alt_cred:
                                    stream_log = self._fetch_kernel_stream_log(alt_cred, owner, slug)
                        if stream_log:
                            out_log_raw = stream_log

                    # If log is still empty and we queried with a collaborator, try owner cred for static output
                    if not out_log_raw and owner != account_username:
                        owner_cred = self.get_credential_by_username(owner)
                        if owner_cred:
                            try:
                                owner_api = KaggleApi()
                                _configure_kaggle_api(owner_api, owner_cred)
                                with owner_api.build_kaggle_client() as o_client:
                                    o_out_res = o_client.kernels.kernels_api_client.list_kernel_session_output(req_out)
                                    if o_out_res and o_out_res.log:
                                        out_log_raw = o_out_res.log
                            except Exception:
                                pass

                    # Also fetch persistent output files via list_kernel_files
                    try:
                        req_files = ApiListKernelFilesRequest()
                        req_files.user_name = owner
                        req_files.kernel_slug = slug
                        files_res = client.kernels.kernels_api_client.list_kernel_files(req_files)
                        if files_res and files_res.files:
                            existing_names = {f.get("file_name") for f in out_files}
                            for f in files_res.files:
                                fname = getattr(f, "name", "") or getattr(f, "file_name", "")
                                if fname and fname not in existing_names:
                                    fsize = getattr(f, "size", 0)
                                    created = getattr(f, "creation_date", "") or getattr(f, "creationDate", "")
                                    out_files.append({
                                        "file_name": fname,
                                        "size": format_bytes(fsize) if fsize else "",
                                        "creation_date": str(created),
                                        "url": f"https://www.kaggle.com/code/{owner}/{slug}/output?select={fname}",
                                    })
                    except Exception as e:
                        logger.warning("Could not list persistent kernel files: %s", e)

                # Decode the same event-stream format used by the live poller.
                log_clean = _clean_kernel_session_log(out_log_raw)

                # Check if this log only contains nbconvert build lines (Quick Save / Code Push)
                is_quick_save = False
                if log_clean and "[NbConvertApp]" in log_clean and "to __results__.html" in log_clean:
                    lines = [ln.strip() for ln in log_clean.strip().splitlines() if ln.strip()]
                    non_build_lines = [
                        ln for ln in lines 
                        if not ln.startswith("[NbConvertApp]") 
                        and not ("SyntaxWarning" in ln or "invalid escape sequence" in ln or "cells[i]" in ln or "re.sub" in ln)
                    ]
                    if len(non_build_lines) == 0:
                        is_quick_save = True

                if is_quick_save:
                    running_counterpart = next(
                        (k for k in self.running_cache if (k.get("ref") or "").split("/")[-1].lower() == slug.lower()),
                        None
                    )
                    running_hint = ""
                    if running_counterpart:
                        r_acc = running_counterpart.get("account")
                        r_ref = running_counterpart.get("ref")
                        running_hint = (
                            f"\n⚡ LƯU Ý:\n"
                            f"Notebook này hiện có một phiên đang chạy thực tế trực tiếp trên tài khoản @{r_acc} (Ref: {r_ref}).\n"
                            f"Bạn hãy chuyển sang tab 'Notebooks đang chạy' để theo dõi log thời gian thực của phiên đó!\n"
                        )

                    log_clean = (
                        f"ℹ️ THÔNG BÁO VỀ LOG PHIÊN CHẠY (HOÀN THÀNH):\n"
                        f"{'='*70}\n"
                        f"Phiên bản này được lưu dưới dạng 'Quick Save' (Lưu nhanh) hoặc đẩy mã nguồn từ API,\n"
                        f"không chạy thực thi code trên GPU/CPU container của Kaggle nên không sinh output console.\n\n"
                        f"Kaggle chỉ thực hiện bước biên dịch notebook sang HTML:\n"
                        f"{'-'*70}\n"
                        f"{log_clean.strip()}\n"
                        f"{'-'*70}\n"
                        f"{running_hint}\n"
                        f"💡 HƯỚNG DẪN:\n"
                        f"1. Để xem mã nguồn và các cell code đã lưu: Chuyển sang tab 'Mã Nguồn (Code)' bên cạnh.\n"
                        f"2. Bấm 'Mở trên Kaggle ↗' ở góc trên để mở notebook trực tiếp trên Kaggle.\n"
                        f"3. Nếu muốn notebook chạy tính toán thực tế và sinh log tiến trình, hãy chọn 'Save & Run All' trên Kaggle."
                    )
                elif not out_log_raw:
                    if status_str in ["RUNNING", "RUNNING_INTERACTIVE", "QUEUED"]:
                        cur_v = meta.get("currentVersionNumber", 1) or 1
                        log_clean = (
                            f"⚡ PHIÊN CHẠY ĐANG HOẠT ĐỘNG (VERSION {cur_v})\n"
                            f"Trạng thái: {status_str}\n"
                            f"Phần cứng: {'GPU Bật' if meta.get('enableGpu') else 'CPU'} | Internet: {'Bật' if meta.get('enableInternet') else 'Tắt'}\n"
                            f"Lần chạy cuối: {format_datetime(meta.get('lastRunTime'))}\n\n"
                            f"[Ghi chú]: Kaggle đang thực thi phiên chạy trên worker container.\n"
                            f"Dashboard tự kiểm tra log mỗi 5 giây; Kaggle chỉ cung cấp nội dung sau khi flush buffer.\n"
                            f"Nút 'Mở trên Kaggle ↗' mở trang notebook để xem console trực tiếp."
                        )
                    else:
                        log_clean = (
                            f"📋 THÔNG BÁO VỀ OUTPUT LOG:\n"
                            f"{'='*70}\n"
                            f"Phiên chạy này đã hoàn thành nhưng Kaggle không còn lưu cache console stream trực tiếp.\n"
                            f"(Kaggle tự động giải phóng bộ nhớ đệm console tạm thời sau khi container dừng).\n\n"
                            f"- Nếu phiên chạy có tạo tệp kết quả: xem tại tab 'Output Files (Tệp kết quả)'\n"
                            f"- Để xem toàn bộ mã nguồn của notebook: xem tại tab 'Mã Nguồn (Code)'\n"
                            f"- Để xem giao diện render đầy đủ có cả output từng ô cell: bấm 'Mở trên Kaggle ↗'"
                        )

                # Versions generator
                cur_version = meta.get("currentVersionNumber", 1) or 1
                try:
                    selected_version = int(version) if version is not None else cur_version
                except Exception:
                    selected_version = cur_version

                versions_list = []
                for v in range(cur_version, 0, -1):
                    v_label = f"Version {v}"
                    if v == cur_version:
                        if status_str in ["RUNNING", "RUNNING_INTERACTIVE", "QUEUED"]:
                            v_label += " (Đang chạy - Mới nhất)"
                        else:
                            v_label += " (Mới nhất)"
                    versions_list.append({
                        "version": v,
                        "label": v_label,
                        "is_latest": (v == cur_version),
                        "is_selected": (v == selected_version),
                        "url": f"https://www.kaggle.com/code/{owner}/{slug}?version={v}"
                    })

                if selected_version != cur_version:
                    log_clean = (
                        f"📌 ĐANG XEM PHIÊN BẢN LỊCH SỬ: VERSION {selected_version} (Phiên bản mới nhất: Version {cur_version})\n"
                        f"Kaggle URL: https://www.kaggle.com/code/{owner}/{slug}?version={selected_version}\n"
                        f"--------------------------------------------------------------------------------\n"
                        f"Ghi chú: Mã nguồn và file kết quả bên dưới là dữ liệu snapshot được đồng bộ. Bấm 'Mở trên Kaggle ↗' để đối chiếu trực tiếp trên máy chủ Kaggle.\n\n"
                        + log_clean
                    )

                # Parse code cells if notebook source exists
                notebook_cells = []
                if blob_source:
                    try:
                        nb = json.loads(blob_source)
                        if isinstance(nb, dict) and "cells" in nb:
                            for idx, c in enumerate(nb.get("cells", []), 1):
                                cell_type = c.get("cell_type", "code")
                                src = c.get("source", "")
                                if isinstance(src, list):
                                    src = "".join(src)
                                notebook_cells.append({
                                    "index": idx,
                                    "type": cell_type,
                                    "source": src
                                })
                    except Exception:
                        notebook_cells = [{"index": 1, "type": "script", "source": blob_source}]

                input_datasets = []
                for ds in meta.get("datasetSources", []) or meta.get("datasetDataSources", []) or []:
                    input_datasets.append({
                        "name": ds,
                        "url": f"https://www.kaggle.com/datasets/{ds}"
                    })

                input_competitions = []
                for comp in meta.get("competitionSources", []) or meta.get("competitionDataSources", []) or []:
                    input_competitions.append({
                        "name": comp,
                        "url": f"https://www.kaggle.com/competitions/{comp}"
                    })

                input_kernels = []
                for ks in meta.get("kernelSources", []) or meta.get("kernelDataSources", []) or []:
                    input_kernels.append({
                        "name": ks,
                        "url": f"https://www.kaggle.com/code/{ks}"
                    })

                input_models = []
                for ms in meta.get("modelSources", []) or meta.get("modelDataSources", []) or []:
                    input_models.append({
                        "name": ms,
                        "url": f"https://www.kaggle.com/models/{ms}"
                    })

                return {
                    "success": True,
                    "ref": kernel_ref,
                    "title": meta.get("title", slug),
                    "account": account_username,
                    "url": f"https://www.kaggle.com/code/{owner}/{slug}",
                    "status": status_str,
                    "failure_message": failure_msg,
                    "current_version": cur_version,
                    "selected_version": selected_version,
                    "selected_url": f"https://www.kaggle.com/code/{owner}/{slug}?version={selected_version}",
                    "is_selected_latest": (selected_version == cur_version),
                    "versions_list": versions_list,
                    "hardware": {
                        "gpu": bool(meta.get("enableGpu", False)),
                        "tpu": bool(meta.get("enableTpu", False)),
                        "internet": bool(meta.get("enableInternet", False)),
                        "language": meta.get("language", "python"),
                        "kernel_type": meta.get("kernelType", "notebook"),
                    },
                    "inputs": {
                        "datasets": input_datasets,
                        "competitions": input_competitions,
                        "kernels": input_kernels,
                        "models": input_models,
                    },
                    "output": {
                        "log": log_clean,
                        "log_length": len(log_clean),
                        "files": out_files,
                        "files_count": len(out_files)
                    },
                    "source_code": {
                        "cells": notebook_cells,
                        "total_cells": len(notebook_cells),
                        "raw_length": len(blob_source)
                    }
                }

            except Exception as e:
                return {"success": False, "message": str(e)}

    def get_google_profile_info(self):
        """Retrieve active Google Chrome profile information (email, name, directory)."""
        config_path = os.path.join(BASE_DIR, "config.json")
        info = {
            "profile_dir": "Default",
            "email": "truongquockiet1211@gmail.com",
            "name": "Kiet",
            "use_primary_for_all": True
        }
        if os.path.exists(config_path):
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                    info["profile_dir"] = cfg.get("default_chrome_profile", "Default")
                    info["email"] = cfg.get("primary_google_email", "truongquockiet1211@gmail.com")
                    info["name"] = cfg.get("primary_google_name", "Kiet")
                    info["use_primary_for_all"] = cfg.get("use_primary_profile_for_all", True)
            except Exception:
                pass
        return info

    def open_browser_for_account(self, account_username, url):
        """Open Chrome browser with the user's primary Google profile (Kiet - truongquockiet1211@gmail.com - Default)
        so that the user never has to log in again.
        """
        config_path = os.path.join(BASE_DIR, "config.json")
        use_primary_for_all = True
        profile_dir = "Default"
        profiles_map = {}

        if os.path.exists(config_path):
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                    use_primary_for_all = cfg.get("use_primary_profile_for_all", True)
                    profiles_map = cfg.get("chrome_profiles", {})
                    default_prof = cfg.get("default_chrome_profile", "Default")
                    if default_prof:
                        profile_dir = default_prof
            except Exception:
                pass

        if not use_primary_for_all and account_username:
            profile_dir = profiles_map.get(account_username, "Default")
        elif account_username == "kitnehi1211" or not account_username:
            profile_dir = "Default"

        opened = False
        try:
            if os.name == "nt":  # Windows
                chrome_candidates = [
                    os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
                    os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
                    os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
                ]
                chrome_exe = next((p for p in chrome_candidates if os.path.exists(p)), None)
                if chrome_exe and profile_dir:
                    subprocess.Popen([chrome_exe, f'--profile-directory={profile_dir}', url])
                    opened = True
                elif chrome_exe:
                    subprocess.Popen([chrome_exe, url])
                    opened = True
            elif sys.platform.startswith("linux"):
                chrome_candidates = [
                    "/opt/google/chrome/chrome",
                    "/usr/bin/google-chrome",
                    "/usr/bin/google-chrome-stable",
                    "/snap/bin/chromium",
                    "/usr/bin/chromium-browser",
                    "/usr/bin/chromium"
                ]
                chrome_bin = next((p for p in chrome_candidates if os.path.exists(p)), None)
                if chrome_bin and profile_dir:
                    subprocess.Popen([chrome_bin, f'--profile-directory={profile_dir}', url])
                    opened = True
                elif chrome_bin:
                    subprocess.Popen([chrome_bin, url])
                    opened = True

            if not opened:
                webbrowser.open(url)
                opened = True
        except Exception as e:
            logger.warning("Could not launch browser for account %s: %s", account_username, e)
            try:
                webbrowser.open(url)
                opened = True
            except Exception:
                opened = False
        return opened

    def cancel_kernel(self, account_username, kernel_ref):
        """Attempt to cancel an active kernel session via Kaggle API, with fallback browser launch."""
        target_cred = self.get_credential_by_username(account_username)
        if not target_cred:
            return {"success": False, "message": f"Không tìm thấy credential cho tài khoản {account_username}"}

        owner, slug = (
            kernel_ref.split("/", 1) if "/" in kernel_ref
            else (account_username, kernel_ref)
        )

        api_success = False
        api_error = ""

        creds_to_try = [target_cred]
        if owner != account_username:
            owner_cred = self.get_credential_by_username(owner)
            if owner_cred and owner_cred not in creds_to_try:
                creds_to_try.append(owner_cred)

        for cred in creds_to_try:
            try:
                api = KaggleApi()
                _configure_kaggle_api(api, cred)
                with api.build_kaggle_client() as client:
                    req_meta = ApiGetKernelRequest()
                    req_meta.user_name = owner
                    req_meta.kernel_slug = slug
                    mres = client.kernels.kernels_api_client.get_kernel(req_meta)
                    if mres and mres.metadata and getattr(mres.metadata, "id", None):
                        session_id = mres.metadata.id
                        cancel_req = ApiCancelKernelSessionRequest()
                        cancel_req.kernel_session_id = session_id
                        client.kernels.kernels_api_client.cancel_kernel_session(cancel_req)
                        api_success = True
                        break
            except Exception as exc:
                api_error = str(exc)

        if api_success:
            for item in self.running_cache:
                if str(item.get("ref", "")).lower() == kernel_ref.lower():
                    item["status"] = "CANCEL_ACKNOWLEDGED"
            self.save_cache()
            return {
                "success": True,
                "api_cancelled": True,
                "ref": kernel_ref,
                "account": account_username,
                "message": f"Đã gửi yêu cầu hủy phiên chạy notebook {kernel_ref} thành công qua Kaggle API!"
            }

        url = f"https://www.kaggle.com/code/{owner}/{slug}"
        opened = self.open_browser_for_account(account_username, url)
        return {
            "success": True,
            "api_cancelled": False,
            "opened_browser": opened,
            "url": url,
            "ref": kernel_ref,
            "account": account_username,
            "message": (
                f"Kaggle API giới hạn quyền hủy phiên qua API Token (cần session Web). "
                f"Dashboard đã tự động mở đúng trình duyệt của tài khoản @{account_username} "
                f"để bạn bấm Dừng tức thì trên trang!" if opened else
                f"Kaggle API giới hạn quyền hủy phiên qua token. Vui lòng mở trực tiếp trên Kaggle để bấm Dừng: {url}"
            )
        }

    def delete_kernel(self, account_username, kernel_ref):
        """Attempt to permanently delete a kernel on Kaggle via Kaggle API."""
        target_cred = self.get_credential_by_username(account_username)
        if not target_cred:
            return {"success": False, "message": f"Không tìm thấy credential cho tài khoản {account_username}"}

        owner, slug = (
            kernel_ref.split("/", 1) if "/" in kernel_ref
            else (account_username, kernel_ref)
        )

        creds_to_try = []
        owner_cred = self.get_credential_by_username(owner)
        if owner_cred:
            creds_to_try.append(owner_cred)
        if target_cred not in creds_to_try:
            creds_to_try.append(target_cred)

        api_success = False
        api_error = ""

        for cred in creds_to_try:
            try:
                api = KaggleApi()
                _configure_kaggle_api(api, cred)
                with api.build_kaggle_client() as client:
                    del_req = ApiDeleteKernelRequest()
                    del_req.user_name = owner
                    del_req.kernel_slug = slug
                    res = client.kernels.kernels_api_client.delete_kernel(del_req)
                    if res and getattr(res, "error_message", None):
                        api_error = str(res.error_message)
                        continue
                    api_success = True
                    break
            except Exception as exc:
                api_error = str(exc)
                logger.warning(f"Failed to delete kernel {owner}/{slug} with cred {cred.get('username')}: {exc}")

        is_already_gone = False
        if not api_success and api_error:
            err_lower = api_error.lower()
            if any(k in err_lower for k in ["403", "404", "not found", "forbidden", "cannot access kernel"]):
                is_already_gone = True

        if api_success or is_already_gone:
            # Remove from running_cache and cached_kernels if present
            self.running_cache = [
                item for item in self.running_cache 
                if str(item.get("ref", "")).lower() != kernel_ref.lower()
            ]
            if hasattr(self, 'cached_kernels'):
                self.cached_kernels = [
                    item for item in self.cached_kernels 
                    if str(item.get("ref", "")).lower() != kernel_ref.lower()
                ]
            for acc in self.data.get("accounts", []):
                acc["kernels"] = [
                    k for k in acc.get("kernels", [])
                    if str(k.get("ref", "")).lower() != kernel_ref.lower()
                ]
            self.data["all_kernels"] = [
                k for k in self.data.get("all_kernels", [])
                if str(k.get("ref", "")).lower() != kernel_ref.lower()
            ]
            self.save_cache()

            # Also remove from training_history.json
            try:
                if os.path.exists(HISTORY_FILE):
                    with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                        history = json.load(f)
                    if isinstance(history, list):
                        history = [h for h in history if str(h.get("ref", "")).lower() != kernel_ref.lower()]
                        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
                            json.dump(history, f, indent=2, ensure_ascii=False)
            except Exception as e:
                logger.warning(f"Error updating history file for deleted kernel: {e}")

            msg = f"Đã xóa vĩnh viễn notebook {kernel_ref} trên tài khoản @{owner}!" if api_success else f"Notebook {kernel_ref} không còn tồn tại trên Kaggle. Đã dọn sạch khỏi hệ thống!"
            return {
                "success": True,
                "message": msg,
                "ref": kernel_ref,
                "account": owner
            }
        else:
            return {
                "success": False,
                "message": f"Không thể xóa notebook {kernel_ref} qua API: {api_error}",
                "error": api_error
            }

    def delete_training_history_entry(self, run_id=None, ref=None, account=None):
        """Remove a run entry from training history and also clean up on Kaggle if possible."""
        try:
            if os.path.exists(HISTORY_FILE):
                with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                    history = json.load(f)
                if isinstance(history, list):
                    if run_id:
                        history = [h for h in history if h.get("id") != run_id]
                    elif ref:
                        history = [h for h in history if str(h.get("ref", "")).lower() != str(ref).lower()]
                    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
                        json.dump(history, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.warning(f"Error removing run from training history: {e}")

        # Also attempt to delete the kernel if ref provided
        if ref and account:
            self.delete_kernel(account, ref)

        return {"success": True, "message": "Đã xóa lượt chạy khỏi Sổ cái huấn luyện!"}

    def get_notebook_source_and_meta(self, account_username, kernel_ref):
        """Fetch full source code and metadata of an existing kernel for runner pre-fill."""
        target_cred = self.get_credential_by_username(account_username)
        if not target_cred:
            target_cred = self.get_credential_by_username("kitnehi1211")
        if not target_cred:
            c_files = self.get_credential_files()
            if c_files:
                target_cred = self.get_credential_by_username(os.path.basename(c_files[0]))
        if not target_cred:
            return {"success": False, "message": f"Không tìm thấy credential cho {account_username}"}

        owner, slug = (
            kernel_ref.split("/", 1) if "/" in kernel_ref
            else (account_username, kernel_ref)
        )

        with kaggle_api_lock:
            api = KaggleApi()
            _configure_kaggle_api(api, target_cred)
            try:
                with api.build_kaggle_client() as client:
                    req_meta = ApiGetKernelRequest()
                    req_meta.user_name = owner
                    req_meta.kernel_slug = slug
                    res = client.kernels.kernels_api_client.get_kernel(req_meta)
                    if not res:
                        return {"success": False, "message": "Không tìm thấy notebook trên Kaggle"}
                    
                    meta = res.metadata.to_dict() if res.metadata else {}
                    source = ""
                    if hasattr(res, "blob") and res.blob:
                        source = getattr(res.blob, "source", "") or ""

                    kernel_type = meta.get("kernelType", "notebook")
                    machine_shape = meta.get("machineShape", "")
                    if not machine_shape and meta.get("enableGpu"):
                        machine_shape = "NvidiaTeslaT4"
                    elif not machine_shape and meta.get("enableTpu"):
                        machine_shape = "Tpu1VmV38"

                    return {
                        "success": True,
                        "ref": f"{owner}/{slug}",
                        "owner": owner,
                        "slug": slug,
                        "title": meta.get("title", slug),
                        "language": meta.get("language", "python"),
                        "kernel_type": kernel_type,
                        "is_notebook": (kernel_type == "notebook"),
                        "is_private": meta.get("isPrivate", True),
                        "enable_gpu": meta.get("enableGpu", False),
                        "enable_tpu": meta.get("enableTpu", False),
                        "enable_internet": meta.get("enableInternet", True),
                        "machine_shape": machine_shape,
                        "dataset_sources": meta.get("datasetDataSources", []) or [],
                        "kernel_sources": meta.get("kernelDataSources", []) or [],
                        "competition_sources": meta.get("competitionDataSources", []) or [],
                        "source_code": source,
                    }
            except Exception as e:
                logger.error("Error fetching kernel source and meta: %s", e)
                return {"success": False, "message": str(e)}

    def push_and_run_kernel(
        self,
        target_account: str,
        slug: str,
        title: str,
        source_code: str,
        machine_shape: str = "NvidiaTeslaT4",
        enable_internet: bool = True,
        is_private: bool = True,
        dataset_sources: list = None,
        kernel_sources: list = None,
        competition_sources: list = None,
        is_notebook: bool = True,
        language: str = "python",
        session_timeout_seconds: int = None,
        backup_to_master: bool = False
    ):
        """Push and trigger execution of a kernel on a specific target Kaggle account."""
        target_cred = self.get_credential_by_username(target_account)
        if not target_cred:
            return {"success": False, "message": f"Không tìm thấy credential cho tài khoản {target_account}"}

        # Optional backup copy to kitnehi1211
        if backup_to_master and target_account != "kitnehi1211" and self.get_credential_by_username("kitnehi1211"):
            try:
                self.push_and_run_kernel(
                    target_account="kitnehi1211",
                    slug=slug,
                    title=title,
                    source_code=source_code,
                    machine_shape="",
                    enable_internet=enable_internet,
                    is_private=True,
                    dataset_sources=dataset_sources,
                    kernel_sources=kernel_sources,
                    competition_sources=competition_sources,
                    is_notebook=is_notebook,
                    language=language,
                    backup_to_master=False
                )
                logger.info("Backed up copy of notebook to @kitnehi1211")
            except Exception as e:
                logger.warning("Could not backup copy to @kitnehi1211: %s", e)

        clean_slug = re.sub(r'[^a-zA-Z0-9\-]+', '-', slug.strip().lower()).strip('-')
        if not clean_slug:
            clean_slug = "notebook-" + str(int(time.time()))
        
        clean_title = title.strip() if title and title.strip() else clean_slug
        if len(clean_title) < 5:
            clean_title = f"{clean_title} Run"

        formatted_source = source_code or ""
        if is_notebook:
            try:
                nb_json = json.loads(source_code) if source_code.strip().startswith("{") else None
                if nb_json and "cells" in nb_json:
                    for cell in nb_json["cells"]:
                        if cell.get("cell_type") == "code":
                            cell["outputs"] = []
                            cell["execution_count"] = None
                        if "source" in cell and isinstance(cell["source"], list):
                            cell["source"] = "".join(cell["source"])
                    formatted_source = json.dumps(nb_json)
                else:
                    formatted_source = json.dumps({
                        "cells": [
                            {
                                "cell_type": "code",
                                "execution_count": None,
                                "metadata": {},
                                "outputs": [],
                                "source": source_code
                            }
                        ],
                        "metadata": {
                            "language_info": {"name": "python"}
                        },
                        "nbformat": 4,
                        "nbformat_minor": 4
                    })
            except Exception as e:
                logger.warning("source_code was not json, wrapped into notebook cells: %s", e)
                formatted_source = json.dumps({
                    "cells": [
                        {
                            "cell_type": "code",
                            "execution_count": None,
                            "metadata": {},
                            "outputs": [],
                            "source": source_code
                        }
                    ],
                    "metadata": {
                        "language_info": {"name": "python"}
                    },
                    "nbformat": 4,
                    "nbformat_minor": 4
                })

        shape_val = (machine_shape or "").strip()
        enable_gpu = False
        enable_tpu = False
        if "Tesla" in shape_val or "gpu" in shape_val.lower() or shape_val == "gpu":
            enable_gpu = True
            if not shape_val or shape_val == "gpu":
                shape_val = "NvidiaTeslaT4"
        elif "Tpu" in shape_val or "tpu" in shape_val.lower() or shape_val == "tpu":
            enable_tpu = True
            if not shape_val or shape_val == "tpu":
                shape_val = "Tpu1VmV38"
        else:
            shape_val = None

        full_slug = f"{target_account}/{clean_slug}"

        with kaggle_api_lock:
            api = KaggleApi()
            _configure_kaggle_api(api, target_cred)
            try:
                with api.build_kaggle_client() as client:
                    request = ApiSaveKernelRequest()
                    request.slug = full_slug
                    request.new_title = clean_title
                    request.text = formatted_source
                    request.language = language or "python"
                    request.kernel_type = "notebook" if is_notebook else "script"
                    request.is_private = bool(is_private)
                    request.enable_internet = bool(enable_internet)
                    request.enable_gpu = enable_gpu
                    request.enable_tpu = enable_tpu
                    if shape_val:
                        request.machine_shape = shape_val
                    if session_timeout_seconds and session_timeout_seconds > 0:
                        request.session_timeout_seconds = int(session_timeout_seconds)

                    # Robust source cleaner & URL normalizer (handles https://www.kaggle.com/code/... and https://www.kaggle.com/datasets/...)
                    raw_datasets = dataset_sources or []
                    raw_kernels = kernel_sources or []
                    raw_comps = competition_sources or []

                    final_datasets = []
                    final_kernels = []
                    final_comps = []

                    def _clean_source(item):
                        s = str(item or "").strip()
                        s = re.sub(r'[\?\#].*$', '', s).strip('/')
                        m_c = re.search(r'kaggle\.com/code/([a-zA-Z0-9_\-]+)/([a-zA-Z0-9_\-]+)', s)
                        if m_c:
                            return 'kernel', f"{m_c.group(1)}/{m_c.group(2)}"
                        m_d = re.search(r'kaggle\.com/datasets/([a-zA-Z0-9_\-]+)/([a-zA-Z0-9_\-]+)', s)
                        if m_d:
                            return 'dataset', f"{m_d.group(1)}/{m_d.group(2)}"
                        m_comp = re.search(r'kaggle\.com/competitions/([a-zA-Z0-9_\-]+)', s)
                        if m_comp:
                            return 'competition', m_comp.group(1)
                        m_m = re.search(r'kaggle\.com/models/([a-zA-Z0-9_\-]+)/([a-zA-Z0-9_\-]+)', s)
                        if m_m:
                            return 'dataset', f"{m_m.group(1)}/{m_m.group(2)}"
                        return None, s

                    for item in raw_datasets:
                        stype, sval = _clean_source(item)
                        if stype == 'kernel':
                            final_kernels.append(sval)
                        elif stype == 'competition':
                            final_comps.append(sval)
                        elif sval:
                            final_datasets.append(sval)

                    for item in raw_kernels:
                        stype, sval = _clean_source(item)
                        if stype == 'dataset':
                            final_datasets.append(sval)
                        elif stype == 'competition':
                            final_comps.append(sval)
                        elif sval:
                            final_kernels.append(sval)

                    for item in raw_comps:
                        stype, sval = _clean_source(item)
                        if sval:
                            final_comps.append(sval)

                    final_datasets = list(dict.fromkeys(final_datasets))
                    final_kernels = list(dict.fromkeys(final_kernels))
                    final_comps = list(dict.fromkeys(final_comps))

                    # Auto-share private datasets across accounts if needed!
                    auto_shared_notes = []
                    if final_datasets:
                        for ds_clean in final_datasets:
                            if "/" in ds_clean:
                                ds_owner, ds_slug = ds_clean.split("/", 1)
                                if ds_owner != target_account and self.get_credential_by_username(ds_owner):
                                    try:
                                        share_res = self.share_dataset_collaborators(
                                            owner_account=ds_owner,
                                            dataset_slug=ds_slug,
                                            collaborator_usernames=[target_account],
                                            role="READER"
                                        )
                                        if share_res.get("success"):
                                            auto_shared_notes.append(f"Đã tự động cấp quyền {ds_clean} cho @{target_account}")
                                            logger.info("Auto-shared dataset %s from @%s to @%s", ds_slug, ds_owner, target_account)
                                    except Exception as e:
                                        logger.warning("Could not auto-share dataset %s to %s: %s", ds_clean, target_account, e)
                        request.dataset_data_sources = final_datasets

                    if final_kernels:
                        request.kernel_data_sources = final_kernels
                    if final_comps:
                        request.competition_data_sources = final_comps

                    response = client.kernels.kernels_api_client.save_kernel(request)

                    if response.error:
                        return {
                            "success": False,
                            "error": response.error,
                            "message": f"Kaggle báo lỗi: {response.error}",
                            "invalid_dataset_sources": getattr(response, "invalid_dataset_sources", []),
                            "invalid_kernel_sources": getattr(response, "invalid_kernel_sources", []),
                        }

                    kernel_url = getattr(response, "url", f"https://www.kaggle.com/code/{full_slug}")
                    v_num = getattr(response, "version_number", getattr(response, "versionNumber", 1))

                    new_running = {
                        "account": target_account,
                        "ref": full_slug,
                        "title": clean_title,
                        "status": "QUEUED",
                        "url": kernel_url,
                        "is_private": is_private,
                        "enable_gpu": enable_gpu,
                        "enable_tpu": enable_tpu,
                        "enable_internet": enable_internet,
                        "machine_shape": shape_val or ("NvidiaTeslaT4" if enable_gpu else "CPU"),
                        "start_time": datetime.now(timezone.utc).isoformat(),
                        "last_run_time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                        "run_seconds": 0,
                        "run_duration_human": "Vừa khởi chạy",
                        "failure_message": "",
                        "version_number": v_num
                    }

                    self.running_cache = [k for k in self.running_cache if str(k.get("ref", "")).lower() != full_slug.lower()]
                    self.running_cache.insert(0, new_running)
                    self.persist_live_kernel(new_running)
                    self.save_cache()

                    hist_entry = {
                        "id": f"run-{int(time.time())}-{target_account}",
                        "account": target_account,
                        "ref": full_slug,
                        "title": clean_title,
                        "slug": clean_slug,
                        "status": "QUEUED",
                        "url": kernel_url,
                        "machine_shape": shape_val or ("NvidiaTeslaT4" if enable_gpu else "CPU"),
                        "dataset_sources": request.dataset_data_sources or [],
                        "start_time": datetime.now(timezone.utc).isoformat(),
                        "start_time_human": datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M"),
                        "auto_shared": auto_shared_notes,
                    }
                    self.append_training_history(hist_entry)

                    return {
                        "success": True,
                        "message": f"Đã gửi lệnh chạy notebook thành công trên tài khoản @{target_account}!",
                        "ref": full_slug,
                        "account": target_account,
                        "url": kernel_url,
                        "version_number": v_num,
                        "running_item": new_running
                    }
            except Exception as e:
                logger.error("Exception in push_and_run_kernel: %s", e)
                return {"success": False, "message": str(e), "error": str(e)}

    def get_input_suggestions(self, query: str = "", limit: int = 50):
        """Provide autocomplete suggestions for datasets and kernels from synced cache."""
        q = (query or "").strip().lower()
        results = []

        # Frequently used pinned datasets (e.g. ZaloAI Datasets & Wheel packages)
        pinned = [
            {
                "type": "dataset",
                "ref": "thanhkhil/zaloai-lifemate-dataset",
                "title": "ZaloAI-LifeMate-Dataset (thanhkhil)",
                "account": "thanhkhil",
                "size": "ZaloAI 2024",
                "url": "https://www.kaggle.com/datasets/thanhkhil/zaloai-lifemate-dataset",
                "last_updated": "Pinned"
            },
            {
                "type": "dataset",
                "ref": "thanhkhil/zaloai-datamate-dataset",
                "title": "ZaloAI-DataMate-Dataset (thanhkhil)",
                "account": "thanhkhil",
                "size": "ZaloAI 2024",
                "url": "https://www.kaggle.com/datasets/thanhkhil/zaloai-datamate-dataset",
                "last_updated": "Pinned"
            },
            {
                "type": "kernel",
                "ref": "kitnehi1211/build-wheels",
                "title": "build-wheels (Wheel Packages)",
                "account": "kitnehi1211",
                "status": "COMPLETE",
                "url": "https://www.kaggle.com/code/kitnehi1211/build-wheels",
                "last_updated": "Pinned"
            }
        ]

        seen_refs = set()
        for p in pinned:
            if not q or (q in p["ref"].lower() or q in p["title"].lower() or q in p["account"].lower()):
                results.append(p)
                seen_refs.add(p["ref"].lower())

        all_datasets = self.data.get("all_datasets", [])
        for d in all_datasets:
            ref = str(d.get("ref", ""))
            if ref.lower() in seen_refs:
                continue
            title = str(d.get("title", ""))
            acc = str(d.get("account", ""))
            if not q or (q in ref.lower() or q in title.lower() or q in acc.lower()):
                results.append({
                    "type": "dataset",
                    "ref": ref,
                    "title": title or ref,
                    "account": acc,
                    "size": d.get("size", ""),
                    "url": d.get("url", f"https://www.kaggle.com/datasets/{ref}"),
                    "last_updated": d.get("last_updated_human", "")
                })
                seen_refs.add(ref.lower())
                if len(results) >= limit:
                    break

        all_kernels = self.data.get("all_kernels", [])
        for k in all_kernels:
            ref = str(k.get("ref", ""))
            title = str(k.get("title", ""))
            acc = str(k.get("account", ""))
            if not q or (q in ref.lower() or q in title.lower() or q in acc.lower()):
                results.append({
                    "type": "kernel",
                    "ref": ref,
                    "title": title or ref,
                    "account": acc,
                    "status": k.get("status", ""),
                    "url": k.get("url", f"https://www.kaggle.com/code/{ref}"),
                    "last_updated": k.get("last_run_human", "")
                })
                if len(results) >= limit * 2:
                    break

        return results

    def get_gpu_recommendations(self):
        """Analyze account quotas and return recommendations sorted by remaining GPU hours."""
        quotas_info = self.get_quotas_overview()
        accounts_quota = quotas_info.get("accounts_quota", [])
        
        active_counts = {}
        for r in self.running_cache:
            acc = r.get("account", "")
            active_counts[acc] = active_counts.get(acc, 0) + 1

        recommendations = []
        for acc in accounts_quota:
            username = acc.get("username", "")
            gpu_rem = float(acc.get("gpu_remaining", 0.0) or 0.0)
            gpu_lim = float(acc.get("gpu_limit", 30.0) or 30.0)
            tpu_rem = float(acc.get("tpu_remaining", 0.0) or 0.0)
            tpu_lim = float(acc.get("tpu_limit", 20.0) or 20.0)
            runs = active_counts.get(username, 0)
            valid = acc.get("valid", True)
            
            score = gpu_rem - (runs * 8.0)
            
            recommendations.append({
                "username": username,
                "gpu_remaining": gpu_rem,
                "gpu_limit": gpu_lim,
                "gpu_percent": round((gpu_rem / gpu_lim * 100), 1) if gpu_lim > 0 else 0,
                "tpu_remaining": tpu_rem,
                "tpu_limit": tpu_lim,
                "active_runs": runs,
                "valid": valid,
                "score": score,
                "is_available": (gpu_rem >= 1.0 and runs < 2 and valid),
                "label": f"{username} (Còn {gpu_rem}h GPU - {runs} notebook đang chạy)"
            })

        recommendations.sort(key=lambda x: x["score"], reverse=True)
        if recommendations and recommendations[0]["gpu_remaining"] >= 1.0:
            recommendations[0]["is_best"] = True

        return recommendations

    def share_dataset_collaborators(self, owner_account: str, dataset_slug: str, collaborator_usernames: list, role: str = "WRITER"):
        """Automatically add collaborators to a private or public dataset via Kaggle SDK."""
        target_cred = self.get_credential_by_username(owner_account)
        if not target_cred:
            return {"success": False, "message": f"Không tìm thấy credential cho {owner_account}"}

        owner = owner_account
        slug = dataset_slug
        if "/" in dataset_slug:
            owner, slug = dataset_slug.split("/", 1)

        role_enum = CollaboratorType.WRITER if str(role).upper() in ["WRITER", "EDIT", "EDITOR"] else CollaboratorType.READER

        with kaggle_api_lock:
            api = KaggleApi()
            _configure_kaggle_api(api, target_cred)
            try:
                with api.build_kaggle_client() as client:
                    req_get = ApiGetDatasetMetadataRequest()
                    req_get.owner_slug = owner
                    req_get.dataset_slug = slug
                    res_get = client.datasets.dataset_api_client.get_dataset_metadata(req_get)
                    
                    settings = DatasetSettings()
                    current_info = getattr(res_get, "info", None)
                    if current_info:
                        tit = getattr(current_info, "title", None)
                        if tit and isinstance(tit, str):
                            settings.title = tit
                        sub = getattr(current_info, "subtitle", None)
                        if sub and isinstance(sub, str):
                            settings.subtitle = sub
                        desc = getattr(current_info, "description", None)
                        if desc and isinstance(desc, str):
                            settings.description = desc
                        settings.is_private = bool(getattr(current_info, "is_private", True))

                    # Kaggle SDK requires exactly one license
                    lic_name = "CC0-1.0"
                    if current_info and getattr(current_info, "licenses", None):
                        lics = getattr(current_info, "licenses")
                        if isinstance(lics, list) and len(lics) > 0:
                            first_lic = lics[0]
                            n = getattr(first_lic, "name", None)
                            if n and isinstance(n, str):
                                lic_name = n
                            elif isinstance(first_lic, dict) and "name" in first_lic:
                                lic_name = str(first_lic["name"])
                    lic = SettingsLicense()
                    lic.name = str(lic_name)
                    settings.licenses = [lic]
                    
                    # Keep all existing collaborators and merge new ones
                    existing_roles = {}
                    if current_info and getattr(current_info, "collaborators", None):
                        for c in getattr(current_info, "collaborators"):
                            uname = getattr(c, "username", None)
                            crole = getattr(c, "role", None)
                            if uname and str(uname) != owner:
                                existing_roles[str(uname)] = CollaboratorType.WRITER if "WRITER" in str(crole).upper() else CollaboratorType.READER

                    for uname in collaborator_usernames:
                        uname = str(uname).strip()
                        if uname and uname != owner:
                            existing_roles[uname] = role_enum

                    collab_objs = []
                    for uname, urole in existing_roles.items():
                        c = DatasetCollaborator()
                        c.username = uname
                        c.role = urole
                        collab_objs.append(c)

                    settings.collaborators = collab_objs

                    req_update = ApiUpdateDatasetMetadataRequest()
                    req_update.owner_slug = owner
                    req_update.dataset_slug = slug
                    req_update.settings = settings

                    res_update = client.datasets.dataset_api_client.update_dataset_metadata(req_update)
                    errors = getattr(res_update, "errors", None)
                    if errors and len(errors) > 0:
                        err_str = "; ".join(str(e) for e in errors)
                        logger.error("Kaggle update_dataset_metadata error: %s", err_str)
                        return {"success": False, "message": f"Kaggle từ chối: {err_str}"}

                    saved_collabs = list(existing_roles.keys())

                    # Update local cache
                    ref_key = f"{owner}/{slug}"
                    all_ds = self.data.get("all_datasets", [])
                    for d in all_ds:
                        if d.get("ref") == ref_key or d.get("ref", "").endswith(f"/{slug}"):
                            d["collaborators"] = saved_collabs
                            break
                    self.save_cache()

                    return {
                        "success": True,
                        "message": f"Đã chia sẻ thành công dataset {owner}/{slug} cho {len(saved_collabs)} tài khoản!",
                        "collaborators": saved_collabs
                    }
            except Exception as e:
                logger.error("Error sharing dataset: %s", e)
                return {"success": False, "message": str(e)}

    def get_dataset_collaborators(self, owner_account: str, dataset_slug: str):
        """Fetch real-time list of collaborators for a dataset from Kaggle."""
        owner = owner_account
        slug = dataset_slug
        if "/" in dataset_slug:
            owner, slug = dataset_slug.split("/", 1)

        target_cred = self.get_credential_by_username(owner)
        if not target_cred:
            ref_key = f"{owner}/{slug}"
            for d in self.data.get("all_datasets", []):
                if d.get("ref") == ref_key:
                    return {
                        "success": True,
                        "owner": owner,
                        "slug": slug,
                        "collaborators": [
                            {"username": u, "role": "WRITER"} for u in d.get("collaborators", [])
                        ],
                        "source": "cache"
                    }
            return {"success": False, "message": f"Không có credential để kiểm tra quyền của @{owner}"}

        with kaggle_api_lock:
            api = KaggleApi()
            _configure_kaggle_api(api, target_cred)
            try:
                with api.build_kaggle_client() as client:
                    req_get = ApiGetDatasetMetadataRequest()
                    req_get.owner_slug = owner
                    req_get.dataset_slug = slug
                    res_get = client.datasets.dataset_api_client.get_dataset_metadata(req_get)
                    info = getattr(res_get, "info", None)
                    collabs = []
                    if info and getattr(info, "collaborators", None):
                        for c in getattr(info, "collaborators"):
                            uname = getattr(c, "username", None)
                            role = getattr(c, "role", None)
                            role_str = "WRITER" if "WRITER" in str(role).upper() else "READER"
                            if uname:
                                collabs.append({"username": uname, "role": role_str})

                    # Update local cache
                    ref_key = f"{owner}/{slug}"
                    for d in self.data.get("all_datasets", []):
                        if d.get("ref") == ref_key:
                            d["collaborators"] = [c["username"] for c in collabs]
                            break
                    self.save_cache()

                    return {
                        "success": True,
                        "owner": owner,
                        "slug": slug,
                        "collaborators": collabs,
                        "source": "kaggle_live"
                    }
            except Exception as e:
                logger.error("Error getting dataset collaborators: %s", e)
                return {"success": False, "message": str(e)}

    def check_dataset_slug_available(self, slug: str, target_account: str = "kitnehi1211"):
        """Check if a dataset slug is already taken on Kaggle or in local cache."""
        target_cred = self.get_credential_by_username(target_account)
        if not target_cred:
            return {"available": False, "message": f"Không tìm thấy credential cho {target_account}"}
        
        clean_slug = re.sub(r'[^a-zA-Z0-9\-]+', '-', slug.strip().lower()).strip('-')
        ref = f"{target_account}/{clean_slug}"
        
        # Check local cache first
        all_ds = self.data.get("all_datasets", [])
        for d in all_ds:
            if d.get("ref", "").lower() == ref.lower():
                return {"available": False, "ref": ref, "message": f"Dataset '{ref}' đã có trong danh sách local!"}
        
        # Check live Kaggle
        with kaggle_api_lock:
            api = KaggleApi()
            _configure_kaggle_api(api, target_cred)
            try:
                status = api.dataset_status(ref)
                if status:
                    return {"available": False, "ref": ref, "message": f"Dataset '{ref}' đã tồn tại trên Kaggle!"}
            except Exception:
                # 404 or not found means available
                pass
        return {"available": True, "ref": ref, "message": "Slug hợp lệ và có thể sử dụng!"}

    def update_dataset_privacy(self, dataset_ref: str, is_private: bool):
        """Update dataset privacy (Private <-> Public) on Kaggle."""
        owner = "kitnehi1211"
        slug = dataset_ref
        if "/" in dataset_ref:
            owner, slug = dataset_ref.split("/", 1)
        
        target_cred = self.get_credential_by_username(owner)
        if not target_cred:
            return {"success": False, "message": f"Không tìm thấy credential cho tài khoản {owner}"}

        with kaggle_api_lock:
            api = KaggleApi()
            _configure_kaggle_api(api, target_cred)
            try:
                with api.build_kaggle_client() as client:
                    req_get = ApiGetDatasetMetadataRequest()
                    req_get.owner_slug = owner
                    req_get.dataset_slug = slug
                    res_get = client.datasets.dataset_api_client.get_dataset_metadata(req_get)
                    
                    settings = DatasetSettings()
                    current_info = getattr(res_get, "info", None)
                    if current_info:
                        tit = getattr(current_info, "title", None)
                        if tit and isinstance(tit, str):
                            settings.title = tit
                        sub = getattr(current_info, "subtitle", None)
                        if sub and isinstance(sub, str):
                            settings.subtitle = sub
                        desc = getattr(current_info, "description", None)
                        if desc and isinstance(desc, str):
                            settings.description = desc
                        collabs = getattr(current_info, "collaborators", None)
                        if collabs and isinstance(collabs, list):
                            settings.collaborators = collabs
                    
                    # Exactly one license is required by Kaggle SDK
                    lic_name = "CC0-1.0"
                    if current_info and getattr(current_info, "licenses", None):
                        lics = getattr(current_info, "licenses")
                        if isinstance(lics, list) and len(lics) > 0:
                            first_lic = lics[0]
                            n = getattr(first_lic, "name", None)
                            if n and isinstance(n, str):
                                lic_name = n
                            elif isinstance(first_lic, dict) and "name" in first_lic:
                                lic_name = str(first_lic["name"])
                    lic = SettingsLicense()
                    lic.name = str(lic_name)
                    settings.licenses = [lic]

                    # Kaggle Policy Check: Public datasets CANNOT be reverted to Private
                    current_is_private = getattr(current_info, "is_private", None)
                    if current_is_private is None:
                        for d in self.data.get("all_datasets", []):
                            if d.get("ref") == f"{owner}/{slug}":
                                current_is_private = d.get("is_private")
                                break

                    if current_is_private is False and is_private:
                        return {
                            "success": False,
                            "message": (
                                "Theo chính sách của Kaggle, Dataset một khi đã được chuyển sang Public "
                                "sẽ không thể chuyển ngược lại về Private (để bảo vệ các notebook và quy trình "
                                "của cộng đồng đang phụ thuộc vào dataset này). Nếu muốn bảo mật lại dữ liệu, "
                                "bạn cần xóa dataset này trên Kaggle và tạo một dataset Private mới."
                            )
                        }

                    settings.is_private = bool(is_private)

                    req_update = ApiUpdateDatasetMetadataRequest()
                    req_update.owner_slug = owner
                    req_update.dataset_slug = slug
                    req_update.settings = settings

                    res_update = client.datasets.dataset_api_client.update_dataset_metadata(req_update)
                    errors = getattr(res_update, "errors", None)
                    if errors and len(errors) > 0:
                        return {"success": False, "message": "; ".join(str(e) for e in errors)}

                    # Update in cache
                    all_ds = self.data.get("all_datasets", [])
                    for d in all_ds:
                        if d.get("ref") == f"{owner}/{slug}":
                            d["is_private"] = bool(is_private)
                            break
                    self.save_cache()

                    status_label = "Private (Riêng tư)" if is_private else "Public (Công khai)"
                    return {
                        "success": True,
                        "message": f"Đã cập nhật quyền dataset {owner}/{slug} thành {status_label}!",
                        "is_private": bool(is_private),
                        "ref": f"{owner}/{slug}"
                    }
            except Exception as e:
                logger.error("Error updating dataset privacy: %s", e)
                return {"success": False, "message": str(e)}

    def create_dataset(
        self,
        title: str,
        slug: str = None,
        is_private: bool = True,
        local_folder_path: str = None,
        file_tuples: list = None,
        description: str = "",
        license_name: str = "CC0-1.0",
        subtitle: str = "",
        target_account: str = "kitnehi1211",
        auto_share_all: bool = True
    ):
        """Create a new dataset on Kaggle under kitnehi1211 (or target account)."""
        target_cred = self.get_credential_by_username(target_account)
        if not target_cred:
            return {"success": False, "message": f"Không tìm thấy credential cho tài khoản {target_account}"}

        # 1. Title formatting & validation (6 - 50 chars)
        clean_title = (title or "").strip()
        if not clean_title:
            return {"success": False, "message": "Vui lòng nhập tên Dataset (Title)."}
        if len(clean_title) < 6:
            clean_title = f"{clean_title} Dataset"[:50]
        if len(clean_title) > 50:
            clean_title = clean_title[:50]
        if len(clean_title) < 6:
            clean_title = clean_title.ljust(6, "-")

        # 2. Slug formatting & validation (6 - 50 chars, alphanumeric and hyphens)
        raw_slug = slug.strip() if slug and slug.strip() else clean_title
        clean_slug = re.sub(r'[^a-zA-Z0-9\-]+', '-', raw_slug.lower()).strip('-')
        clean_slug = re.sub(r'-+', '-', clean_slug)
        if len(clean_slug) < 6:
            clean_slug = f"{clean_slug}-dataset"[:50]
        if len(clean_slug) < 6:
            clean_slug = f"dataset-{int(time.time())}"
        if len(clean_slug) > 50:
            clean_slug = clean_slug[:50].rstrip('-')
        if len(clean_slug) < 6:
            clean_slug = clean_slug.ljust(6, '0')

        # 3. Subtitle validation (Kaggle requires 20 - 80 chars if specified)
        clean_subtitle = (subtitle or "").strip()
        if clean_subtitle and (len(clean_subtitle) < 20 or len(clean_subtitle) > 80):
            if len(clean_subtitle) < 20:
                clean_subtitle = ""  # Omit rather than failing Kaggle validation
            elif len(clean_subtitle) > 80:
                clean_subtitle = clean_subtitle[:80]

        # 4. Prepare staging directory
        staging_dir = tempfile.mkdtemp(prefix="kaggle_ds_staging_")
        try:
            file_count = 0
            # A. Process uploaded file tuples: [(filename, file_content_or_path)]
            if file_tuples:
                for fname, fcontent in file_tuples:
                    if not fname:
                        continue
                    clean_fname = os.path.basename(fname.strip())
                    if not clean_fname:
                        continue
                    dest_path = os.path.join(staging_dir, clean_fname)
                    if isinstance(fcontent, bytes):
                        with open(dest_path, "wb") as f_out:
                            f_out.write(fcontent)
                    elif isinstance(fcontent, str) and os.path.isfile(fcontent):
                        shutil.copy2(fcontent, dest_path)
                    elif hasattr(fcontent, "read"):
                        with open(dest_path, "wb") as f_out:
                            while True:
                                chunk = fcontent.read(1024 * 1024)
                                if not chunk:
                                    break
                                f_out.write(chunk)
                    file_count += 1

            # B. Process local folder path if provided
            if local_folder_path and local_folder_path.strip():
                l_path = local_folder_path.strip().strip('"').strip("'")
                if not os.path.isdir(l_path):
                    return {"success": False, "message": f"Thư mục cục bộ không tồn tại: {l_path}"}
                for item in os.listdir(l_path):
                    s = os.path.join(l_path, item)
                    d = os.path.join(staging_dir, item)
                    if item in ["dataset-metadata.json", "datapackage.json"]:
                        continue
                    if os.path.isdir(s):
                        shutil.copytree(s, d, dirs_exist_ok=True)
                        file_count += 1
                    else:
                        shutil.copy2(s, d)
                        file_count += 1

            if file_count == 0:
                return {
                    "success": False,
                    "message": "Cần ít nhất một file hoặc thư mục dữ liệu để tạo dataset trên Kaggle."
                }

            # 5. Create dataset-metadata.json
            ref_str = f"{target_account}/{clean_slug}"
            meta_data = {
                "title": clean_title,
                "id": ref_str,
                "licenses": [{"name": license_name or "CC0-1.0"}],
                "description": description or ""
            }
            if clean_subtitle:
                meta_data["subtitle"] = clean_subtitle

            meta_file = os.path.join(staging_dir, "dataset-metadata.json")
            with open(meta_file, "w", encoding="utf-8") as mf:
                json.dump(meta_data, mf, indent=2)

            # 6. Call Kaggle API
            with kaggle_api_lock:
                api = KaggleApi()
                _configure_kaggle_api(api, target_cred)
                try:
                    res = api.dataset_create_new(
                        folder=staging_dir,
                        public=not is_private,
                        quiet=False,
                        dir_mode="zip"
                    )
                except Exception as e:
                    logger.error("Error creating dataset via KaggleApi: %s", e)
                    return {"success": False, "message": f"Lỗi từ Kaggle API: {str(e)}"}

            err = getattr(res, "error", None)
            if err:
                return {"success": False, "message": str(err)}

            # 7. Calculate file size & update local cache
            total_bytes = 0
            for r, _, files in os.walk(staging_dir):
                for fn in files:
                    if fn != "dataset-metadata.json":
                        fp = os.path.join(r, fn)
                        try:
                            total_bytes += os.path.getsize(fp)
                        except Exception:
                            pass

            dataset_url = f"https://www.kaggle.com/datasets/{ref_str}"
            new_entry = {
                "account": target_account,
                "ref": ref_str,
                "title": clean_title,
                "url": dataset_url,
                "size_bytes": total_bytes,
                "size_human": format_bytes(total_bytes),
                "download_count": 0,
                "view_count": 0,
                "is_private": bool(is_private),
                "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "last_updated_human": "Vừa tạo"
            }

            all_ds = self.data.get("all_datasets", [])
            self.data["all_datasets"] = [new_entry] + [d for d in all_ds if d.get("ref") != ref_str]
            self.save_cache()

            # 8. Auto-share with all other accounts in the pool if requested
            shared_collabs = []
            if auto_share_all and is_private:
                try:
                    all_users = self.get_all_account_usernames()
                    other_accounts = [u for u in all_users if u and u != target_account]
                    if other_accounts:
                        share_res = self.share_dataset_collaborators(
                            owner_account=target_account,
                            dataset_slug=clean_slug,
                            collaborator_usernames=other_accounts,
                            role="WRITER"
                        )
                        if share_res.get("success"):
                            shared_collabs = share_res.get("collaborators", other_accounts)
                except Exception as e_share:
                    logger.warning("Could not auto-share dataset with all accounts: %s", e_share)

            privacy_label = "Private (Riêng tư)" if is_private else "Public (Công khai)"
            success_msg = f"Tạo dataset '{clean_title}' ({privacy_label}) thành công trên Kaggle!"
            if shared_collabs:
                success_msg += f" Đã tự động chia sẻ quyền Editor cho {len(shared_collabs)} tài khoản trong pool!"

            return {
                "success": True,
                "message": success_msg,
                "ref": ref_str,
                "url": dataset_url,
                "is_private": bool(is_private),
                "dataset": new_entry,
                "collaborators": shared_collabs
            }
        finally:
            shutil.rmtree(staging_dir, ignore_errors=True)


    def refresh_quotas_live(self):
        """Fetch weekly GPU/TPU balances from Kaggle for every credential."""
        if not self._quota_scan_lock.acquire(blocking=False):
            return {
                "success": False,
                "started": False,
                "message": "Đang có một lượt quét quota khác đang chạy.",
                "last_scan": self.last_quota_scan,
            }

        self.is_scanning_quotas = True
        self.quota_scan_error = ""
        started_at = datetime.now(timezone.utc)

        try:
            files = self.get_credential_files()

            def scan_one(fpath):
                credential = {}
                username = os.path.basename(fpath)
                try:
                    with open(fpath, "r", encoding="utf-8") as fp:
                        credential = json.load(fp)
                    username = credential.get("username") or username
                    api = _configure_kaggle_api(KaggleApi(), credential)
                    if not hasattr(api, "quota_view"):
                        raise RuntimeError(
                            "Kaggle SDK hiện tại chưa hỗ trợ quota_view; cần Kaggle SDK mới."
                        )

                    response = None
                    for attempt in range(3):
                        try:
                            response = api.quota_view()
                            break
                        except Exception as exc:
                            if "429" not in str(exc) or attempt >= 2:
                                raise
                            time.sleep(5 * (attempt + 1))
                    if response is None:
                        raise RuntimeError("Kaggle không trả về phản hồi quota.")
                    result = {
                        "username": username,
                        "success": True,
                        "quota_refresh_at": format_datetime(
                            getattr(response, "quota_refresh_time", None)
                        ),
                        "gpu": None,
                        "tpu": None,
                    }

                    for resource_name, response_name in (("gpu", "gpu_quota"), ("tpu", "tpu_quota")):
                        quota = getattr(response, response_name, None)
                        if quota is None:
                            continue
                        used = _duration_to_hours(getattr(quota, "time_used", None))
                        total = _duration_to_hours(getattr(quota, "total_time_allowed", None))
                        if used is None or total is None:
                            continue
                        reserved_sec = _duration_to_seconds(getattr(quota, "time_reserved", None))
                        result[resource_name] = {
                            "used_hours": max(0.0, used),
                            "total_hours": max(0.0, total),
                            "remaining_hours": max(0.0, total - used),
                            "reserved_seconds": max(0.0, reserved_sec),
                        }

                    if result["gpu"] is None and result["tpu"] is None:
                        raise RuntimeError("Kaggle không trả về thông tin quota GPU/TPU.")
                    # Leave a small gap between accounts to avoid Kaggle's
                    # per-client rate limit on GetAcceleratorQuotaStatistics.
                    time.sleep(0.75)
                    return result
                except Exception as exc:
                    logger.warning("Live quota scan failed for %s: %s", username, exc)
                    return {
                        "username": username,
                        "success": False,
                        "error": f"{type(exc).__name__}: {str(exc)[:240]}",
                    }

            if not files:
                self.quota_scan_error = "Không tìm thấy file credential nào."
                return {
                    "success": False,
                    "started": True,
                    "scanned": 0,
                    "updated": 0,
                    "accounts": [],
                    "errors": [self.quota_scan_error],
                }

            # Kaggle rate-limits this endpoint by client/IP. Query accounts one
            # at a time so a ten-account refresh does not turn every response
            # into HTTP 429.
            with ThreadPoolExecutor(max_workers=1) as executor:
                results = list(executor.map(scan_one, files))

            updated = 0
            errors = []
            updated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            for result in results:
                username = result.get("username")
                if not result.get("success"):
                    errors.append({
                        "username": username,
                        "error": result.get("error", "Unknown quota error"),
                    })
                    continue

                record = dict(self.quotas.get(username, {}))
                gpu = result.get("gpu") or {}
                tpu = result.get("tpu") or {}
                if result.get("gpu") is not None:
                    record.update({
                        "gpu_remaining": round(gpu["remaining_hours"], 4),
                        "gpu_used_hours": round(gpu["used_hours"], 4),
                        "gpu_total_hours": round(gpu["total_hours"], 4),
                        "gpu_reserved_seconds": round(gpu.get("reserved_seconds", 0.0), 2),
                        "time_reserved_seconds": round(gpu.get("reserved_seconds", 0.0), 2),
                        "gpu_quota_known": True,
                    })
                if result.get("tpu") is not None:
                    record.update({
                        "tpu_remaining": round(tpu["remaining_hours"], 4),
                        "tpu_used_hours": round(tpu["used_hours"], 4),
                        "tpu_total_hours": round(tpu["total_hours"], 4),
                        "tpu_reserved_seconds": round(tpu.get("reserved_seconds", 0.0), 2),
                        "tpu_quota_known": True,
                    })
                record["quota_source"] = "kaggle_api"
                record["quota_refresh_at"] = result.get("quota_refresh_at", "")
                record["last_updated"] = updated_at
                record.setdefault("notes", "")
                self.quotas[username] = record
                updated += 1

            self.last_quota_scan = datetime.now(timezone.utc).isoformat()
            if errors:
                self.quota_scan_error = "; ".join(
                    f"{item['username']}: {item['error']}" for item in errors
                )
            self.save_quota_data()

            return {
                "success": updated > 0,
                "started": True,
                "scanned": len(results),
                "updated": updated,
                "accounts": results,
                "errors": errors,
                "last_scan": self.last_quota_scan,
                "duration_seconds": round(
                    (datetime.now(timezone.utc) - started_at).total_seconds(), 2
                ),
            }
        except Exception as exc:
            self.quota_scan_error = str(exc)
            logger.warning("Live quota scan failed: %s", exc)
            return {
                "success": False,
                "started": True,
                "scanned": 0,
                "updated": 0,
                "accounts": [],
                "errors": [{"error": str(exc)}],
            }
        finally:
            if self.last_quota_scan is None:
                self.last_quota_scan = datetime.now(timezone.utc).isoformat()
            self.is_scanning_quotas = False
            self._quota_scan_lock.release()

    def get_quotas_overview(self):
        # Refresh from disk when it contains real data. A transient empty or
        # partially-written file must never erase a populated in-memory view.
        disk_data = self.load_cache()
        if disk_data.get("accounts") or not self.data.get("accounts"):
            self.data = disk_data
            # A previous process may have kept an in-memory running list from
            # an older cache. Rebuild it whenever the persisted data is loaded
            # so one account's active notebooks cannot appear on another card.
            self.running_cache = self.extract_running_from_data()
        disk_quotas = self.load_quota_data()
        if disk_quotas or not self.quotas:
            self.quotas = disk_quotas

        reset_info = get_next_weekly_reset()
        now_utc = datetime.now(timezone.utc)
        days_until_saturday = (5 - now_utc.weekday()) % 7
        if days_until_saturday == 0 and (now_utc.hour > 0 or now_utc.minute > 0):
            days_until_saturday = 7
        target_date = (now_utc + timedelta(days=days_until_saturday)).replace(hour=0, minute=0, second=0, microsecond=0)
        cycle_start = target_date - timedelta(days=7)

        accounts_quota = []

        for acc in self.data.get("accounts", []):
            u = acc.get("username")

            active_gpu_sessions = 0
            active_cpu_sessions = 0
            active_tpu_sessions = 0
            running_gpu_hours = 0.0
            running_cpu_hours = 0.0
            running_tpu_hours = 0.0
            running_sessions_detail = []
            latest_active_ref = None

            gpu_runs_this_week = 0
            cpu_runs_this_week = 0
            tpu_runs_this_week = 0

            # Historical kernels are used only for the weekly activity count.
            # Current resource usage must come exclusively from the latest
            # running scan, otherwise an old RUNNING value can survive in the
            # cache after the session has ended.
            account_kernels = list(acc.get("kernels", []))
            active_account_kernels = [
                k for k in self.running_cache if k.get("account") == u
            ]
            seen_refs = {k.get("ref") for k in account_kernels if k.get("ref")}
            for rk in active_account_kernels:
                if rk.get("ref") not in seen_refs:
                    account_kernels.append(rk)
                    seen_refs.add(rk.get("ref"))

            for k in active_account_kernels:
                lrt = k.get("last_run_time")
                dt = None
                if lrt:
                    try:
                        dt = datetime.fromisoformat(lrt.replace("Z", "+00:00")) if "T" in lrt else datetime.strptime(lrt, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                    except Exception:
                        pass

                st = (k.get("status") or "").upper()
                is_running = st in ["RUNNING", "RUNNING_INTERACTIVE", "QUEUED"]
                is_gpu = _coerce_bool(k.get("enable_gpu"))
                is_tpu = _coerce_bool(k.get("enable_tpu"))
                hw_type = "GPU" if is_gpu else ("TPU" if is_tpu else "CPU")

                if is_running:
                    elapsed = max(0.05, (now_utc - dt).total_seconds() / 3600) if dt else 0.1
                    if is_gpu:
                        active_gpu_sessions += 1
                        running_gpu_hours += elapsed
                        if not latest_active_ref:
                            latest_active_ref = k.get("ref")
                    elif is_tpu:
                        active_tpu_sessions += 1
                        running_tpu_hours += elapsed
                        if not latest_active_ref:
                            latest_active_ref = k.get("ref")
                    else:
                        active_cpu_sessions += 1
                        running_cpu_hours += elapsed
                        if not latest_active_ref:
                            latest_active_ref = k.get("ref")

                    running_sessions_detail.append({
                        "ref": k.get("ref"),
                        "title": k.get("title") or k.get("ref"),
                        "hardware": hw_type,
                        "status": st,
                        "last_run_time": lrt or "",
                        "last_run_human": k.get("last_run_human") or (time_ago(dt) if dt else "Đang chạy"),
                        "running_hours": round(elapsed, 1),
                        "url": f"https://www.kaggle.com/code/{k.get('ref')}"
                    })

            for k in account_kernels:
                lrt = k.get("last_run_time")
                dt = None
                if lrt:
                    try:
                        dt = datetime.fromisoformat(lrt.replace("Z", "+00:00")) if "T" in lrt else datetime.strptime(lrt, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                    except Exception:
                        pass

                is_gpu = _coerce_bool(k.get("enable_gpu"))
                is_tpu = _coerce_bool(k.get("enable_tpu"))
                if dt and dt >= cycle_start:
                    if is_gpu:
                        gpu_runs_this_week += 1
                    elif is_tpu:
                        tpu_runs_this_week += 1
                    else:
                        cpu_runs_this_week += 1

            # Valid Kaggle URL (Never 404)
            if latest_active_ref:
                gpu_settings_url = f"https://www.kaggle.com/code/{latest_active_ref}"
            else:
                gpu_settings_url = f"https://www.kaggle.com/{u}/code"

            total_active = active_gpu_sessions + active_cpu_sessions + active_tpu_sessions
            
            # Values returned by Kaggle's accelerator quota endpoint are already
            # account-level balances. Do not subtract the live session duration
            # a second time. The subtraction is retained only for legacy manual
            # records created before the API integration.
            user_quota_info = self.quotas.get(u, {})
            notes = user_quota_info.get("notes", "")
            quota_source = user_quota_info.get("quota_source", "manual")

            def read_stored_quota(key):
                if key not in user_quota_info or user_quota_info.get(key) is None:
                    return None
                try:
                    return max(0.0, float(user_quota_info[key]))
                except (TypeError, ValueError):
                    return None

            stored_gpu = read_stored_quota("gpu_remaining")
            stored_tpu = read_stored_quota("tpu_remaining")
            stored_gpu_max = read_stored_quota("gpu_total_hours")
            stored_tpu_max = read_stored_quota("tpu_total_hours")
            gpu_rem = (
                stored_gpu
                if quota_source == "kaggle_api"
                else (max(0.0, stored_gpu - running_gpu_hours) if stored_gpu is not None else None)
            )
            tpu_rem = (
                stored_tpu
                if quota_source == "kaggle_api"
                else (max(0.0, stored_tpu - running_tpu_hours) if stored_tpu is not None else None)
            )
            gpu_quota_known = stored_gpu is not None
            tpu_quota_known = stored_tpu is not None

            accounts_quota.append({
                "username": u,
                "display_name": acc.get("display_name"),
                "avatar_url": acc.get("avatar_url"),
                "file": acc.get("file"),
                "gpu_remaining": round(gpu_rem, 1) if gpu_rem is not None else None,
                "gpu_max": round(stored_gpu_max, 1) if stored_gpu_max is not None else 30.0,
                "gpu_max_sessions": 2,
                "gpu_quota_known": gpu_quota_known,
                "gpu_quota_source": quota_source if gpu_quota_known else "unavailable",
                "is_exhausted": gpu_quota_known and stored_gpu <= 0.0,
                "tpu_remaining": round(tpu_rem, 1) if tpu_rem is not None else None,
                "tpu_max": round(stored_tpu_max, 1) if stored_tpu_max is not None else 20.0,
                "tpu_quota_known": tpu_quota_known,
                "tpu_quota_source": quota_source if tpu_quota_known else "unavailable",
                "tpu_is_exhausted": tpu_quota_known and stored_tpu <= 0.0,
                "notes": notes,
                "quota_last_updated": user_quota_info.get("last_updated", ""),
                "quota_refresh_at": user_quota_info.get("quota_refresh_at", ""),
                "gpu_used_hours": user_quota_info.get("gpu_used_hours"),
                "tpu_used_hours": user_quota_info.get("tpu_used_hours"),
                "cpu_max_sessions": 10,
                "active_gpu_sessions": active_gpu_sessions,
                "active_cpu_sessions": active_cpu_sessions,
                "active_tpu_sessions": active_tpu_sessions,
                "total_active_sessions": total_active,
                "running_sessions_detail": running_sessions_detail,
                "running_gpu_hours": round(running_gpu_hours, 1),
                "running_cpu_hours": round(running_cpu_hours, 1),
                "running_tpu_hours": round(running_tpu_hours, 1),
                "is_consuming_gpu": active_gpu_sessions > 0,
                "is_consuming_cpu": active_cpu_sessions > 0,
                "is_consuming_tpu": active_tpu_sessions > 0,
                "gpu_runs_this_week": gpu_runs_this_week,
                "cpu_runs_this_week": cpu_runs_this_week,
                "tpu_runs_this_week": tpu_runs_this_week,
                "total_runs_this_week": gpu_runs_this_week + cpu_runs_this_week + tpu_runs_this_week,
                "gpu_settings_url": gpu_settings_url,
                "profile_url": f"https://www.kaggle.com/{u}"
            })

        return {
            "reset_info": reset_info,
            "accounts_quota": accounts_quota,
            "quota_scan": {
                "is_scanning": self.is_scanning_quotas,
                "last_scan": self.last_quota_scan,
                "error": self.quota_scan_error,
            },
        }

    def update_account_quota(self, username, gpu_remaining=None, tpu_remaining=None, notes=None):
        if username not in self.quotas:
            self.quotas[username] = {
                "notes": "",
                "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            }
        
        if gpu_remaining is not None:
            self.quotas[username]["gpu_remaining"] = float(gpu_remaining)
        if tpu_remaining is not None:
            self.quotas[username]["tpu_remaining"] = float(tpu_remaining)
        if notes is not None:
            self.quotas[username]["notes"] = str(notes)
            
        self.quotas[username]["last_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.save_quota_data()
        return True, self.quotas[username]

    def refresh_all(self):
        if self.is_refreshing:
            return False, "Đang trong quá trình làm mới dữ liệu..."

        self.is_refreshing = True
        self.refresh_progress = "Đang quét danh sách file credential..."
        start_time = time.time()
        previous_item_count = sum(
            len(self.data.get(key, []) or [])
            for key in ("all_kernels", "all_datasets", "all_competitions")
        )

        try:
            files = self.get_credential_files()
            total_files = len(files)
            if total_files == 0:
                self.data = {
                    "accounts": [],
                    "all_kernels": [],
                    "all_datasets": [],
                    "all_competitions": [],
                    "summary": {
                        "total_accounts": 0,
                        "valid_accounts": 0,
                        "total_kernels": 0,
                        "running_kernels": 0,
                        "queued_kernels": 0,
                        "complete_kernels": 0,
                        "error_kernels": 0,
                        "total_datasets": 0,
                        "total_competitions": 0,
                    },
                    "last_synced": datetime.now().isoformat(),
                    "last_synced_human": "Vừa xong"
                }
                self.save_cache()
                return True, "Không tìm thấy file credential nào."

            accounts_data = []
            for idx, f in enumerate(files, 1):
                self.refresh_progress = f"Đang đồng bộ {idx}/{total_files}: {os.path.basename(f)}..."
                res = self.fetch_account_data(f)
                accounts_data.append(res)

            accounts_data.sort(key=lambda x: (not x.get("valid", False), x.get("username", "")))

            all_kernels = []
            all_datasets = []
            all_competitions = []

            seen_kernel_refs = {}
            seen_dataset_refs = set()

            for acc in accounts_data:
                for k in acc.get("kernels", []):
                    k_ref = (k.get("ref") or "").strip().lower()
                    if not k_ref:
                        all_kernels.append(k)
                        continue
                    if k_ref not in seen_kernel_refs:
                        seen_kernel_refs[k_ref] = k
                        all_kernels.append(k)
                    else:
                        existing = seen_kernel_refs[k_ref]
                        k_run = str(k.get("status") or "").upper() in ACTIVE_KERNEL_STATUSES
                        ex_run = str(existing.get("status") or "").upper() in ACTIVE_KERNEL_STATUSES
                        if k_run and not ex_run:
                            all_kernels[all_kernels.index(existing)] = k
                            seen_kernel_refs[k_ref] = k
                        elif k_run == ex_run and parse_datetime_to_timestamp(k.get("last_run_time")) > parse_datetime_to_timestamp(existing.get("last_run_time")):
                            all_kernels[all_kernels.index(existing)] = k
                            seen_kernel_refs[k_ref] = k

                for d in acc.get("datasets", []):
                    d_ref = d.get("ref")
                    if d_ref and d_ref not in seen_dataset_refs:
                        seen_dataset_refs.add(d_ref)
                        all_datasets.append(d)
                    elif not d_ref:
                        all_datasets.append(d)

                for c in acc.get("competitions", []):
                    all_competitions.append(c)

            all_kernels.sort(
                key=lambda x: parse_datetime_to_timestamp(x.get("last_run_time")),
                reverse=True,
            )
            all_datasets.sort(key=lambda x: x.get("last_updated") or "", reverse=True)

            new_item_count = len(all_kernels) + len(all_datasets) + len(all_competitions)
            if previous_item_count > 0 and new_item_count == 0:
                logger.error(
                    "Refresh returned no items while cache had %d items; keeping previous cache",
                    previous_item_count,
                )
                self.refresh_progress = "Refresh returned no items; keeping previous cache"
                return False, "API trả về data rỗng; đã giữ nguyên cache cũ."

            failed_sections = {
                f"{acc.get('username')}: {', '.join(acc.get('_fetch_failures', []))}"
                for acc in accounts_data
                if acc.get("_fetch_failures")
            }
            if failed_sections:
                logger.warning("Some refresh sections failed: %s", "; ".join(sorted(failed_sections)))
            for acc in accounts_data:
                acc.pop("_fetch_failures", None)

            running_cnt = sum(1 for k in all_kernels if k.get("status") in ["RUNNING", "RUNNING_INTERACTIVE"])
            queued_cnt = sum(1 for k in all_kernels if k.get("status") == "QUEUED")
            error_cnt = sum(1 for k in all_kernels if k.get("status") in ["ERROR", "CANCEL_ACKNOWLEDGED", "FAILED"])
            complete_cnt = sum(1 for k in all_kernels if k.get("status") == "COMPLETE")
            valid_accounts_cnt = sum(1 for a in accounts_data if a.get("valid"))

            now = datetime.now()
            self.data = {
                "accounts": accounts_data,
                "all_kernels": all_kernels,
                "all_datasets": all_datasets,
                "all_competitions": all_competitions,
                "summary": {
                    "total_accounts": len(accounts_data),
                    "valid_accounts": valid_accounts_cnt,
                    "total_kernels": len(all_kernels),
                    "running_kernels": running_cnt,
                    "queued_kernels": queued_cnt,
                    "complete_kernels": complete_cnt,
                    "error_kernels": error_cnt,
                    "total_datasets": len(all_datasets),
                    "total_competitions": len(all_competitions),
                },
                "last_synced": now.isoformat(),
                "last_synced_human": now.strftime("%H:%M:%S - %d/%m/%Y"),
                "sync_duration_seconds": round(time.time() - start_time, 2)
            }

            # The live scanner and quota cards must use the freshly rebuilt
            # account-scoped kernel list, not a running list from before this
            # refresh.
            self.running_cache = self.extract_running_from_data()

            self.save_cache()
            self.refresh_progress = "Đang lấy quota GPU/TPU trực tiếp từ Kaggle..."
            self.scan_active_running_kernels(allow_during_refresh=True)
            self.save_cache()
            quota_result = self.refresh_quotas_live()
            if not quota_result.get("success"):
                logger.warning("Quota refresh during full refresh was not complete: %s", quota_result)
            self.refresh_progress = "Đồng bộ thành công!"
            return True, f"Đã đồng bộ {len(accounts_data)} tài khoản trong {self.data['sync_duration_seconds']}s"

        except Exception as e:
            logger.error("Lỗi khi refresh: %s", e)
            self.refresh_progress = f"Lỗi: {e}"
            return False, str(e)
        finally:
            self.is_refreshing = False

    def list_local_notebooks(self):
        """Scan user's Desktop projects for local .ipynb notebooks."""
        desktop_dir = os.path.abspath(os.path.join(BASE_DIR, ".."))
        notebooks = []
        
        ignored_names = {
            ".git", ".venv", "node_modules", "__pycache__", "AppData", "cache",
            ".codegraph", ".cache", "dist", "build"
        }
        
        for root, dirs, files in os.walk(desktop_dir):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ignored_names]
            for f in files:
                if f.endswith(".ipynb"):
                    full_p = os.path.join(root, f)
                    try:
                        sz = os.path.getsize(full_p)
                        mtime = os.path.getmtime(full_p)
                        rel_p = os.path.relpath(full_p, desktop_dir).replace("\\", "/")
                        parent_folder = os.path.basename(os.path.dirname(full_p))
                        
                        notebooks.append({
                            "rel_path": rel_p,
                            "filename": f,
                            "folder": parent_folder,
                            "size_kb": round(sz / 1024, 1),
                            "full_path": full_p,
                            "mtime": mtime,
                            "mtime_human": datetime.fromtimestamp(mtime).strftime("%d/%m/%Y %H:%M")
                        })
                    except Exception:
                        pass
        
        notebooks.sort(key=lambda x: x["mtime"], reverse=True)
        return notebooks

    def read_local_notebook(self, rel_or_full_path):
        """Read a local notebook file, extract clean title, slug, and detect input datasets referenced in code."""
        desktop_dir = os.path.abspath(os.path.join(BASE_DIR, ".."))
        target_path = rel_or_full_path
        if not os.path.isabs(target_path):
            target_path = os.path.join(desktop_dir, rel_or_full_path)
            
        if not os.path.exists(target_path):
            target_path = os.path.join(BASE_DIR, rel_or_full_path)
            if not os.path.exists(target_path):
                return {"success": False, "message": f"Không tìm thấy file: {rel_or_full_path}"}

        try:
            with open(target_path, "r", encoding="utf-8", errors="ignore") as fp:
                raw_content = fp.read()
                
            cell_count = 0
            if raw_content.strip().startswith("{"):
                try:
                    nb_json = json.loads(raw_content)
                    cell_count = len(nb_json.get("cells", []))
                except Exception:
                    pass

            fname = os.path.basename(target_path)
            clean_title = re.sub(r"\.[^/.]+$", "", fname).replace("_", " ").replace("-", " ").strip()
            clean_slug = re.sub(r"[^a-zA-Z0-9\-]+", "-", fname.lower().replace(".ipynb", "").replace(".py", "")).strip("-")
            
            detected_slugs = list(set(re.findall(r"/kaggle/input/([a-zA-Z0-9_\-]+)", raw_content)))
            
            all_cached_datasets = self.data.get("datasets", [])
            all_cached_kernels = self.data.get("kernels", [])
            matched_dataset_sources = []
            matched_kernel_sources = []
            
            for dslug in detected_slugs:
                if dslug in ("datasets", "notebooks", "input"):
                    continue
                if dslug == "build-wheels":
                    matched_kernel_sources.append("kitnehi1211/build-wheels")
                    continue

                ds_match = next((d for d in all_cached_datasets if d.get("ref", "").lower().endswith(f"/{dslug.lower()}") or d.get("slug", "").lower() == dslug.lower()), None)
                if ds_match:
                    matched_dataset_sources.append(ds_match["ref"])
                else:
                    k_match = next((k for k in all_cached_kernels if k.get("ref", "").lower().endswith(f"/{dslug.lower()}") or k.get("slug", "").lower() == dslug.lower()), None)
                    if k_match:
                        matched_kernel_sources.append(k_match["ref"])
                    elif self.get_credential_by_username("kitnehi1211"):
                        matched_dataset_sources.append(f"kitnehi1211/{dslug}")

            return {
                "success": True,
                "filename": fname,
                "path": rel_or_full_path,
                "title": clean_title,
                "slug": clean_slug,
                "source_code": raw_content,
                "is_notebook": fname.endswith(".ipynb"),
                "cell_count": cell_count,
                "size_kb": round(len(raw_content) / 1024, 1),
                "detected_inputs": detected_slugs,
                "matched_dataset_sources": list(dict.fromkeys(matched_dataset_sources)),
                "matched_kernel_sources": list(dict.fromkeys(matched_kernel_sources))
            }
        except Exception as e:
            return {"success": False, "message": f"Lỗi đọc file notebook: {e}"}

    def get_training_history(self):
        """Retrieve the central training history across all accounts."""
        if not os.path.exists(HISTORY_FILE):
            return []
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                history = json.load(f)
                if not isinstance(history, list):
                    return []
                all_k = {str(k.get("ref", "")).lower(): k for k in self.data.get("all_kernels", [])}
                running_k = {str(k.get("ref", "")).lower(): k for k in self.running_cache}
                for h in history:
                    ref_lower = str(h.get("ref", "")).lower()
                    if ref_lower in running_k:
                        h["status"] = running_k[ref_lower].get("status", h.get("status"))
                        h["run_seconds"] = running_k[ref_lower].get("run_seconds", 0)
                    elif ref_lower in all_k:
                        h["status"] = all_k[ref_lower].get("status", h.get("status"))
                return history
        except Exception:
            return []

    def append_training_history(self, record):
        """Append a run record to the central training history log."""
        history = self.get_training_history()
        history = [r for r in history if r.get("id") != record.get("id")]
        history.insert(0, record)
        history = history[:150]
        try:
            with open(HISTORY_FILE, "w", encoding="utf-8") as f:
                json.dump(history, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.warning("Could not save training history: %s", e)

monitor_service = KaggleMonitorService()
