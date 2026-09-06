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
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests

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
    ApiListKernelFilesRequest
)
from kagglesdk.datasets.types.dataset_enums import DatasetSelectionGroup, DatasetSortBy
from kagglesdk.datasets.types.dataset_api_service import ApiListDatasetsRequest

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger("kaggle_service")

CRED_DIR = os.path.join(BASE_DIR, "Credentials")
CACHE_FILE = os.path.join(BASE_DIR, "data_cache.json")
QUOTA_FILE = os.path.join(BASE_DIR, "quota_data.json")

kaggle_api_lock = threading.Lock()

def format_bytes(size):
    if not size or not isinstance(size, (int, float)):
        return "0 B"
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if abs(size) < 1024.0:
            return f"{size:3.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} PB"

def format_datetime(dt):
    if not dt:
        return ""
    if isinstance(dt, str):
        return dt
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
        running = []
        for k in self.data.get("all_kernels", []):
            st = (k.get("status") or "").upper()
            if st in ["RUNNING", "RUNNING_INTERACTIVE", "QUEUED"]:
                running.append(k)
        return running

    def get_running_fast(self):
        return self.running_cache

    def load_cache(self):
        if os.path.exists(self.cache_file):
            try:
                with open(self.cache_file, "r", encoding="utf-8") as fp:
                    data = json.load(fp)
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
                
                kernel_item = {
                    "account": username,
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
                    "status": "COMPLETE" if last_run_time else "NOT_CHECKED",
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
                            except Exception:
                                pass
                        except Exception as e:
                            err_str = str(e)
                            if "404" in err_str:
                                ki["status"] = "COMPLETE"
                            else:
                                ki["status"] = "UNKNOWN"
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

    def scan_active_running_kernels(self):
        """Check every account for live sessions and refresh hardware metadata."""
        if self.is_refreshing:
            return self.running_cache
        if not self._running_scan_lock.acquire(blocking=False):
            return self.running_cache

        self.is_scanning_running = True
        self.running_scan_error = ""

        try:
            files = self.get_credential_files()

            def update_cached_kernel(ref, status, fail_msg, gpu_flag, tpu_flag,
                                     internet_flag, cur_version):
                for ck in self.data.get("all_kernels", []):
                    if ck.get("ref") == ref:
                        ck["status"] = status
                        ck["failure_message"] = fail_msg
                        ck["enable_gpu"] = gpu_flag
                        ck["enable_tpu"] = tpu_flag
                        ck["enable_internet"] = internet_flag
                        ck["version_number"] = cur_version
                for acc in self.data.get("accounts", []):
                    for ck in acc.get("kernels", []):
                        if ck.get("ref") == ref:
                            ck["status"] = status
                            ck["failure_message"] = fail_msg
                            ck["enable_gpu"] = gpu_flag
                            ck["enable_tpu"] = tpu_flag
                            ck["enable_internet"] = internet_flag
                            ck["version_number"] = cur_version

            def check_one(fpath):
                user = ""
                try:
                    with open(fpath, "r", encoding="utf-8") as fp:
                        cred = json.load(fp)
                    user = cred.get("username", "")
                    api = KaggleApi()
                    _configure_kaggle_api(api, cred)
                    found = []

                    # dateRun is enough to put active sessions near the front,
                    # but inspect more than five so an older active notebook is
                    # not left behind in the cached running state.
                    kernels = api.kernels_list(
                        mine=True, sort_by="dateRun", page_size=100
                    ) or []
                    for k in kernels[:10]:
                        ref = getattr(k, "ref", "")
                        if not ref:
                            continue

                        cached_info = {}
                        for cached_kernel in self.data.get("all_kernels", []):
                            if cached_kernel.get("ref") == ref:
                                cached_info = cached_kernel
                                break

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
                            stat_str = "UNKNOWN"
                            fail_msg = ""

                        # Metadata is refreshed for both active and inactive
                        # kernels. This clears stale GPU/TPU flags after a
                        # notebook is changed from GPU to CPU.
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

                        update_cached_kernel(
                            ref, stat_str, fail_msg, gpu_flag, tpu_flag,
                            internet_flag, cur_version
                        )

                        last_run_time = getattr(k, "lastRunTime", None)
                        if last_run_time is None:
                            last_run_time = getattr(k, "last_run_time", None)
                        title = getattr(k, "title", ref.split("/")[-1])
                        if stat_str in {"RUNNING", "RUNNING_INTERACTIVE", "QUEUED"}:
                            found.append({
                                "account": user,
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
                    return {"username": user, "items": found, "success": True}
                except Exception as exc:
                    logger.warning("Live running scan failed for %s: %s", fpath, exc)
                    return {"username": user, "items": [], "success": False}

            with ThreadPoolExecutor(max_workers=min(10, max(2, len(files)))) as ex:
                results = list(ex.map(check_one, files))

            failed_users = {
                result.get("username")
                for result in results
                if not result.get("success") and result.get("username")
            }
            active_list = [
                item
                for result in results
                for item in result.get("items", [])
            ]
            if failed_users:
                # Keep the last known active sessions for accounts that could
                # not be reached. A network/API error is not evidence that a
                # running notebook stopped.
                active_list.extend(
                    item
                    for item in self.running_cache
                    if item.get("account") in failed_users
                )
                self.running_scan_error = (
                    "Không thể quét live cho: " + ", ".join(sorted(failed_users))
                )
            self.running_cache = active_list

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

                for k in self.data.get("all_kernels", []):
                    if k.get("ref") == kernel_ref:
                        k["status"] = status_str
                        k["failure_message"] = failure_msg
                for acc in self.data.get("accounts", []):
                    for k in acc.get("kernels", []):
                        if k.get("ref") == kernel_ref:
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

    def get_kernel_details(self, account_username, kernel_ref, version=None):
        target_cred = self.get_credential_by_username(account_username)
        if not target_cred:
            return {"success": False, "message": "Không tìm thấy credential cho tài khoản này"}

        if "/" in kernel_ref:
            owner, slug = kernel_ref.split("/", 1)
        else:
            owner = account_username
            slug = kernel_ref

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

                # Clean log parser
                log_clean = ""
                if out_log_raw:
                    raw_s = out_log_raw.strip()
                    if raw_s.startswith("[") and '{"stream_name"' in raw_s:
                        try:
                            # Parse JSON event stream
                            fixed = raw_s
                            if not fixed.endswith("]"):
                                fixed = fixed.rstrip(", \r\n") + "]"
                            entries = json.loads(fixed)
                            lines = [e.get("data", "") for e in entries if isinstance(e, dict)]
                            log_clean = "".join(lines)
                        except Exception:
                            # Fallback regex extraction of "data":"..."
                            parts = re.findall(r'"data":\s*"((?:[^"\\]|\\.)*)"', raw_s)
                            if parts:
                                try:
                                    log_clean = "".join(bytes(p, "utf-8").decode("unicode_escape", errors="replace") for p in parts)
                                except Exception:
                                    log_clean = raw_s
                            else:
                                log_clean = raw_s
                    else:
                        log_clean = out_log_raw
                else:
                    if status_str in ["RUNNING", "RUNNING_INTERACTIVE", "QUEUED"]:
                        cur_v = meta.get("currentVersionNumber", 1) or 1
                        log_clean = (
                            f"⚡ PHIÊN CHẠY ĐANG HOẠT ĐỘNG (VERSION {cur_v})\n"
                            f"Trạng thái: {status_str}\n"
                            f"Phần cứng: {'GPU Bật' if meta.get('enableGpu') else 'CPU'} | Internet: {'Bật' if meta.get('enableInternet') else 'Tắt'}\n"
                            f"Lần chạy cuối: {format_datetime(meta.get('lastRunTime'))}\n\n"
                            f"[Ghi chú]: Kaggle đang thực thi phiên chạy trên worker container.\n"
                            f"Nhật ký console sẽ được máy chủ Kaggle flush buffer theo từng giai đoạn hoặc hoàn tất khi phiên chạy kết thúc.\n"
                            f"Bạn có thể bấm 'Làm mới Log' hoặc bấm nút 'Mở trên Kaggle ↗' ở góc trên để theo dõi stream trực tiếp."
                        )
                    else:
                        log_clean = "[Phiên chạy này chưa có output log hoặc log đã được lưu trữ trên giao diện Kaggle]"

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
                        result[resource_name] = {
                            "used_hours": max(0.0, used),
                            "total_hours": max(0.0, total),
                            "remaining_hours": max(0.0, total - used),
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
                        "gpu_quota_known": True,
                    })
                if result.get("tpu") is not None:
                    record.update({
                        "tpu_remaining": round(tpu["remaining_hours"], 4),
                        "tpu_used_hours": round(tpu["used_hours"], 4),
                        "tpu_total_hours": round(tpu["total_hours"], 4),
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

            seen_kernel_refs = set()
            seen_dataset_refs = set()

            for acc in accounts_data:
                for k in acc.get("kernels", []):
                    k_ref = k.get("ref")
                    if k_ref and k_ref not in seen_kernel_refs:
                        seen_kernel_refs.add(k_ref)
                        all_kernels.append(k)
                    elif not k_ref:
                        all_kernels.append(k)

                for d in acc.get("datasets", []):
                    d_ref = d.get("ref")
                    if d_ref and d_ref not in seen_dataset_refs:
                        seen_dataset_refs.add(d_ref)
                        all_datasets.append(d)
                    elif not d_ref:
                        all_datasets.append(d)

                for c in acc.get("competitions", []):
                    all_competitions.append(c)

            all_kernels.sort(key=lambda x: x.get("last_run_time") or "", reverse=True)
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

monitor_service = KaggleMonitorService()
