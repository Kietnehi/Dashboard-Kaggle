import os
import sys
import time
import threading
import json
import shutil
import tempfile
import logging
from fastapi import FastAPI, Query, BackgroundTasks, HTTPException, Body, UploadFile, File, Form
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, List
import uvicorn

from kaggle_service import monitor_service, BASE_DIR, parse_datetime_to_timestamp

logger = logging.getLogger(__name__)

CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
DEFAULT_AUTO_REFRESH_SECONDS = 900  # 15 phút (mặc định khuyên dùng)
MIN_SAFE_AUTO_REFRESH_SECONDS = 600  # 10 phút (giới hạn tối thiểu để chống Kaggle Rate Limit)

auto_refresh_state = {
    "interval_seconds": DEFAULT_AUTO_REFRESH_SECONDS,
    "last_run_timestamp": time.time(),
    "is_enabled": True,
}

def load_config():
    global auto_refresh_state
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
                interval = cfg.get("auto_refresh_seconds", DEFAULT_AUTO_REFRESH_SECONDS)
                auto_refresh_state["interval_seconds"] = interval
                auto_refresh_state["is_enabled"] = bool(interval > 0 and cfg.get("auto_refresh_enabled", True))
        except Exception:
            pass

def save_config():
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump({
                "auto_refresh_seconds": auto_refresh_state["interval_seconds"],
                "auto_refresh_enabled": auto_refresh_state["is_enabled"]
            }, f, indent=2)
    except Exception:
        pass

def run_refresh_with_timestamp():
    try:
        monitor_service.refresh_all()
    finally:
        auto_refresh_state["last_run_timestamp"] = time.time()

def periodic_auto_refresher():
    """Chạy ngầm định kỳ đồng bộ toàn bộ dữ liệu từ Kaggle với cơ chế an toàn chống Rate Limit."""
    # Đợi 30 giây sau khi server khởi động để hệ thống ổn định
    time.sleep(30)
    while True:
        try:
            interval = auto_refresh_state["interval_seconds"]
            is_enabled = auto_refresh_state["is_enabled"] and interval > 0
            if is_enabled:
                now = time.time()
                elapsed = now - auto_refresh_state["last_run_timestamp"]
                if elapsed >= interval:
                    if not monitor_service.is_refreshing:
                        run_refresh_with_timestamp()
        except Exception:
            pass
        time.sleep(5)

app = FastAPI(title="Kaggle Multi-Account Monitor Dashboard", version="1.2.0")

CHROME_PROFILE_BY_ACCOUNT = {
    "baovuong3009": "Bảo",
    "justb2464": "12",
    "kitnehi1211": "Kiet Truong",
    "kittruong": "Trinh",
    "lngchip": "Lê Ngọc",
    "ngodongnguyen": "Nguyễn",
    "phatle54": "Phat",
    "thaivu1": "Kiet",
    "truongkietphu": "Truong",
    "xyzabz": "xyz",
}

def get_chrome_profile(username: str) -> str:
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
                profiles = cfg.get("chrome_profiles", {})
                if username in profiles:
                    return str(profiles[username])
        except Exception:
            pass
    return CHROME_PROFILE_BY_ACCOUNT.get(username, "")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:8000",
        "http://localhost:8000",
        "http://127.0.0.1:8001",
        "http://localhost:8001",
        "http://127.0.0.1:3000",
        "http://localhost:3000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

STATIC_DIR = os.path.join(BASE_DIR, "static")
os.makedirs(STATIC_DIR, exist_ok=True)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
IMAGE_DIR = os.path.join(BASE_DIR, "image")
if os.path.isdir(IMAGE_DIR):
    app.mount("/image", StaticFiles(directory=IMAGE_DIR), name="images")

class QuotaUpdateRequest(BaseModel):
    gpu_remaining: Optional[float] = None
    tpu_remaining: Optional[float] = None
    notes: Optional[str] = None

class KernelCancelRequest(BaseModel):
    account: str
    ref: str

class KernelDeleteRequest(BaseModel):
    account: str
    ref: str

class OpenBrowserRequest(BaseModel):
    account: Optional[str] = ""
    url: str

class KernelRunRequest(BaseModel):
    target_account: str
    slug: str
    title: Optional[str] = ""
    source_code: str
    machine_shape: Optional[str] = "NvidiaTeslaT4"
    enable_internet: Optional[bool] = True
    is_private: Optional[bool] = True
    dataset_sources: Optional[List[str]] = []
    kernel_sources: Optional[List[str]] = []
    competition_sources: Optional[List[str]] = []
    model_sources: Optional[List[str]] = []
    is_notebook: Optional[bool] = True
    language: Optional[str] = "python"
    session_timeout_seconds: Optional[int] = None
    backup_to_master: Optional[bool] = False

class DatasetShareRequest(BaseModel):
    owner_account: str
    dataset_slug: str
    collaborators: List[str]
    role: Optional[str] = "WRITER"

class OpenShareDialogRequest(BaseModel):
    account: str
    ref: str

class DatasetCreateJsonRequest(BaseModel):
    title: str
    slug: Optional[str] = ""
    is_private: Optional[bool] = True
    local_folder_path: Optional[str] = ""
    description: Optional[str] = ""
    license_name: Optional[str] = "CC0-1.0"
    subtitle: Optional[str] = ""
    target_account: Optional[str] = "kitnehi1211"
    auto_share_all: Optional[bool] = True

class DatasetPrivacyRequest(BaseModel):
    ref: str
    is_private: bool

def periodic_running_scanner():
    while True:
        # Ten live Kaggle requests every 30 seconds quickly triggers the API
        # rate limit. The explicit scan button remains available for an
        # on-demand check; background polling uses a safer two-minute cadence.
        time.sleep(120)
        try:
            monitor_service.scan_active_running_kernels()
        except Exception as e:
            pass

@app.on_event("startup")
def startup_event():
    load_config()
    summary = monitor_service.data.get("summary", {})
    cache_has_no_content = (
        monitor_service.data.get("accounts")
        and not summary.get("total_kernels")
        and not summary.get("total_datasets")
        and not summary.get("total_competitions")
    )
    if not monitor_service.data.get("accounts") or cache_has_no_content:
        threading.Thread(target=run_refresh_with_timestamp, daemon=True).start()
    threading.Thread(target=periodic_running_scanner, daemon=True).start()
    threading.Thread(target=periodic_auto_refresher, daemon=True).start()

@app.api_route("/", methods=["GET", "HEAD"], response_class=HTMLResponse)
def get_index():
    index_path = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(index_path):
        with open(index_path, "r", encoding="utf-8") as f:
            # Always serve the latest UI so browsers don't keep a stale cached page
            return HTMLResponse(content=f.read(), headers={"Cache-Control": "no-store"})
    return HTMLResponse("<h1>Dashboard is loading...</h1>")

@app.get("/api/overview")
def get_overview():
    return {
        "summary": monitor_service.data.get("summary", {}),
        "last_synced": monitor_service.data.get("last_synced"),
        "last_synced_human": monitor_service.data.get("last_synced_human", "Chưa đồng bộ"),
        "is_refreshing": monitor_service.is_refreshing,
        "refresh_progress": monitor_service.refresh_progress
    }

@app.get("/api/accounts")
def get_accounts():
    accounts = monitor_service.data.get("accounts", [])
    summary_accounts = []
    for a in accounts:
        summary_accounts.append({
            "file": a.get("file"),
            "username": a.get("username"),
            "chrome_profile": get_chrome_profile(a.get("username")),
            "display_name": a.get("display_name"),
            "avatar_url": a.get("avatar_url"),
            "bio": a.get("bio", ""),
            "valid": a.get("valid", False),
            "error_message": a.get("error_message", ""),
            "kernels_count": a.get("kernels_count", 0),
            "datasets_count": a.get("datasets_count", 0),
            "competitions_count": a.get("competitions_count", 0),
        })
    return summary_accounts

@app.get("/api/account/{username}")
def get_account_detail(username: str):
    accounts = monitor_service.data.get("accounts", [])
    for a in accounts:
        if a.get("username") == username:
            return a
    raise HTTPException(status_code=404, detail="Không tìm thấy tài khoản")

@app.get("/api/kernels")
def get_kernels(
    account: Optional[str] = Query(None, description="Lọc theo username"),
    status: Optional[str] = Query(None, description="Lọc theo status (RUNNING, QUEUED, ERROR, COMPLETE)"),
    search: Optional[str] = Query(None, description="Tìm theo tên kernel")
):
    acc_name = account.strip() if isinstance(account, str) and account.strip() else None
    if acc_name:
        matched = next((a for a in monitor_service.data.get("accounts", []) if a.get("username") == acc_name), None)
        if matched and matched.get("kernels"):
            filtered = list(matched.get("kernels", []))
        else:
            all_kernels = monitor_service.data.get("all_kernels", [])
            filtered = [k for k in all_kernels if k.get("account") == acc_name]
    else:
        filtered = list(monitor_service.data.get("all_kernels", []))

    stat_filter = status.strip().upper() if isinstance(status, str) and status.strip() else None
    if stat_filter and stat_filter != "ALL":
        if stat_filter == "RUNNING":
            filtered = [k for k in filtered if k.get("status") in ["RUNNING", "RUNNING_INTERACTIVE"]]
        elif stat_filter == "QUEUED":
            filtered = [k for k in filtered if k.get("status") == "QUEUED"]
        elif stat_filter == "ERROR":
            filtered = [k for k in filtered if k.get("status") in ["ERROR", "CANCEL_ACKNOWLEDGED", "FAILED"]]
        elif stat_filter == "COMPLETE":
            filtered = [k for k in filtered if k.get("status") == "COMPLETE"]
        else:
            filtered = [k for k in filtered if k.get("status") == stat_filter]

    search_query = search.strip().lower() if isinstance(search, str) and search.strip() else None
    if search_query:
        filtered = [k for k in filtered if search_query in k.get("title", "").lower() or search_query in k.get("ref", "").lower() or search_query in k.get("account", "").lower()]

    filtered.sort(
        key=lambda x: parse_datetime_to_timestamp(x.get("last_run_time")),
        reverse=True
    )
    return filtered

@app.get("/api/kernels/running")
def get_running_kernels(live_scan: bool = Query(False, description="Mặc định trả về cache tức thì <5ms")):
    """Get active running/queued kernels. Fast and instant by default."""
    previous_scan = monitor_service.last_running_scan
    if live_scan:
        running_list = monitor_service.scan_active_running_kernels()
    else:
        running_list = monitor_service.get_running_fast()

    return {
        "count": len(running_list),
        "items": running_list,
        "scan_in_progress": monitor_service.is_scanning_running,
        "scan_completed": bool(live_scan and monitor_service.last_running_scan != previous_scan),
        "last_scan": monitor_service.last_running_scan
    }

@app.post("/api/kernels/scan-now")
def scan_running_async(background_tasks: BackgroundTasks):
    """Trigger background scan without blocking frontend."""
    if monitor_service.is_scanning_running:
        return {
            "success": True,
            "started": False,
            "message": "Đang có một lượt quét trạng thái chạy..."
        }
    background_tasks.add_task(monitor_service.scan_active_running_kernels)
    return {
        "success": True,
        "started": True,
        "message": "Đang quét trạng thái các notebook trong nền..."
    }

@app.get("/api/kernels/scan-status")
def get_running_scan_status():
    return {
        "is_scanning": monitor_service.is_scanning_running,
        "last_scan": monitor_service.last_running_scan,
        "error": monitor_service.running_scan_error,
        "count": len(monitor_service.running_cache)
    }

@app.get("/api/kernel/status")
def check_kernel_status(account: str = Query(...), ref: str = Query(...)):
    res = monitor_service.check_single_kernel_status(account, ref)
    return res

@app.get("/api/kernel/details")
def get_kernel_details(
    account: str = Query(...), 
    ref: str = Query(...),
    version: Optional[int] = Query(None, description="Số version cụ thể muốn xem")
):
    res = monitor_service.get_kernel_details(account, ref, version=version)
    return res

@app.get("/api/kernel/live-output")
def get_kernel_live_output(
    account: str = Query(...),
    ref: str = Query(...),
    check_status: bool = Query(False),
):
    """Fetch only the latest session status and console log for live polling."""
    return monitor_service.get_kernel_live_output(account, ref, check_status=check_status)

@app.post("/api/kernel/cancel")
def cancel_kernel(req: KernelCancelRequest):
    """Request cancellation of a running kernel, with auto-browser launch fallback."""
    return monitor_service.cancel_kernel(req.account, req.ref)

@app.post("/api/kernel/delete")
def delete_kernel(req: KernelDeleteRequest):
    """Permanently delete a kernel on Kaggle via API."""
    return monitor_service.delete_kernel(req.account, req.ref)

@app.post("/api/open-in-browser")
def open_in_browser(req: OpenBrowserRequest):
    """Open Chrome with account profile for 1-click management on Kaggle."""
    opened = monitor_service.open_browser_for_account(req.account, req.url)
    return {"success": opened, "url": req.url}

@app.get("/api/config/browser-profile")
def get_browser_profile():
    """Get active Google Profile information used for Chrome browser opening."""
    return monitor_service.get_google_profile_info()

@app.post("/api/config/browser-profile")
def update_browser_profile(req: dict = Body(...)):
    """Update browser profile preferences."""
    config_path = os.path.join(BASE_DIR, "config.json")
    cfg = {}
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            cfg = {}
    cfg.update(req)
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    return monitor_service.get_google_profile_info()

# ==================== TRAINING & RUNNER API ====================

@app.get("/api/runner/recommendations")
def get_runner_recommendations():
    """Get smart account recommendations based on available GPU quota and active load."""
    return monitor_service.get_gpu_recommendations()

@app.get("/api/runner/notebook-meta")
def get_runner_notebook_meta(
    account: str = Query(...),
    ref: str = Query(...),
    version: Optional[int] = Query(None, description="Số phiên bản cụ thể (mặc định lấy mới nhất)")
):
    """Fetch existing notebook source code and metadata to pre-populate training form."""
    return monitor_service.get_notebook_source_and_meta(account, ref, version=version)

@app.get("/api/runner/local-notebooks")
def get_local_notebooks():
    """Scan and list all local .ipynb notebooks found in user's projects."""
    return monitor_service.list_local_notebooks()

@app.get("/api/runner/local-notebook-content")
def get_local_notebook_content(path: str = Query(...)):
    """Read a local notebook file, extract title, slug, and auto-detect referenced inputs."""
    return monitor_service.read_local_notebook(path)

@app.get("/api/runner/suggestions")
def get_runner_suggestions(
    q: Optional[str] = Query("", description="Từ khóa tìm dataset hoặc notebook"),
    limit: int = Query(40, description="Số lượng gợi ý tối đa")
):
    """Get autocomplete suggestions for input datasets and kernel sources."""
    return monitor_service.get_input_suggestions(query=q, limit=limit)

@app.post("/api/runner/run")
def run_notebook(req: KernelRunRequest):
    """Push and run notebook directly on selected Kaggle account with GPU/TPU/CPU."""
    result = monitor_service.push_and_run_kernel(
        target_account=req.target_account,
        slug=req.slug,
        title=req.title,
        source_code=req.source_code,
        machine_shape=req.machine_shape,
        enable_internet=req.enable_internet,
        is_private=req.is_private,
        dataset_sources=req.dataset_sources,
        kernel_sources=req.kernel_sources,
        competition_sources=req.competition_sources,
        model_sources=req.model_sources,
        is_notebook=req.is_notebook,
        language=req.language,
        session_timeout_seconds=req.session_timeout_seconds,
        backup_to_master=req.backup_to_master
    )
    return result

@app.get("/api/runner/history")
def get_runner_history():
    """Get the central training run history across all accounts."""
    return monitor_service.get_training_history()

@app.post("/api/runner/history/delete")
def delete_runner_history(req: dict = Body(...)):
    """Delete a run entry from central training history and cleanup from Kaggle."""
    run_id = req.get("id")
    account = req.get("account")
    ref = req.get("ref")
    return monitor_service.delete_training_history_entry(run_id=run_id, ref=ref, account=account)

@app.get("/api/datasets/collaborators")
def get_dataset_collaborators(ref: str = Query(..., description="Dataset ref (owner/slug)")):
    """Get real-time collaborators for a dataset from Kaggle or cache."""
    owner = "kitnehi1211"
    slug = ref
    if "/" in ref:
        owner, slug = ref.split("/", 1)
    return monitor_service.get_dataset_collaborators(owner, slug)

@app.post("/api/runner/share-dataset")
def share_dataset(req: DatasetShareRequest):
    """Automatically add collaborators to a dataset across multiple accounts."""
    logger.info("share_dataset requested: owner=%s, slug=%s, collaborators=%s, role=%s",
                req.owner_account, req.dataset_slug, req.collaborators, req.role)
    result = monitor_service.share_dataset_collaborators(
        owner_account=req.owner_account,
        dataset_slug=req.dataset_slug,
        collaborator_usernames=req.collaborators,
        role=req.role
    )
    logger.info("share_dataset result: %s", result)
    return result

@app.post("/api/runner/open-share-dialog")
def open_share_dialog(req: OpenShareDialogRequest):
    """Open Chrome with account profile directly at notebook page on Kaggle for sharing."""
    url = f"https://www.kaggle.com/code/{req.ref}"
    opened = monitor_service.open_browser_for_account(req.account, url)
    return {"success": opened, "url": url}

@app.get("/api/quotas")
def get_quotas(live: bool = Query(False, description="Đọc quota GPU/TPU trực tiếp từ Kaggle")):
    if live:
        monitor_service.refresh_quotas_live()
    return monitor_service.get_quotas_overview()


@app.post("/api/quotas/scan-now")
def scan_quotas_now():
    """Fetch the real weekly GPU/TPU balance for every local credential."""
    result = monitor_service.refresh_quotas_live()
    overview = monitor_service.get_quotas_overview()
    return {
        "success": result.get("success", False),
        "scan": result,
        "overview": overview,
    }

@app.post("/api/quota/{username}")
def update_quota(username: str, req: QuotaUpdateRequest):
    ok, res = monitor_service.update_account_quota(
        username=username,
        gpu_remaining=req.gpu_remaining,
        tpu_remaining=req.tpu_remaining,
        notes=req.notes
    )
    return {"success": ok, "data": res}

@app.get("/api/datasets")
def get_datasets(
    account: str = Query(None),
    search: str = Query(None),
    live: bool = Query(False, description="Lấy dữ liệu real-time trực tiếp từ Kaggle API")
):
    if live:
        monitor_service.fetch_datasets_live(target_account=account)

    all_datasets = monitor_service.data.get("all_datasets", [])
    filtered = all_datasets

    if account and account.strip():
        filtered = [d for d in filtered if d.get("account") == account.strip()]

    if search and search.strip():
        s = search.strip().lower()
        filtered = [d for d in filtered if s in d.get("title", "").lower() or s in d.get("ref", "").lower() or s in d.get("account", "").lower()]

    return filtered

@app.post("/api/datasets/sync")
def sync_datasets_live(account: Optional[str] = Query(None)):
    """API đồng bộ real-time các dataset trực tiếp từ Kaggle API."""
    fresh_datasets = monitor_service.fetch_datasets_live(target_account=account)
    return {
        "success": True,
        "count": len(fresh_datasets),
        "total": len(monitor_service.data.get("all_datasets", [])),
        "message": f"Đã quét real-time thành công {len(fresh_datasets)} dataset trực tiếp từ Kaggle API!"
    }

@app.get("/api/datasets/check-slug")
def check_slug(slug: str = Query(...), target_account: str = Query("kitnehi1211")):
    """Kiểm tra xem slug dataset đã tồn tại trên Kaggle hoặc local chưa."""
    return monitor_service.check_dataset_slug_available(slug=slug, target_account=target_account)

@app.post("/api/datasets/create")
async def create_dataset_endpoint(
    title: str = Form(...),
    slug: Optional[str] = Form(""),
    is_private: bool = Form(True),
    local_folder_path: Optional[str] = Form(""),
    description: Optional[str] = Form(""),
    license_name: Optional[str] = Form("CC0-1.0"),
    subtitle: Optional[str] = Form(""),
    target_account: Optional[str] = Form("kitnehi1211"),
    auto_share_all: bool = Form(True),
    files: Optional[List[UploadFile]] = File(None)
):
    """Tạo dataset mới trên Kaggle (mặc định kitnehi1211) hỗ trợ upload file và/hoặc chọn thư mục local."""
    temp_dir = tempfile.mkdtemp(prefix="kaggle_upload_")
    try:
        file_tuples = []
        if files:
            for upload in files:
                if not upload.filename:
                    continue
                safe_name = os.path.basename(upload.filename)
                saved_path = os.path.join(temp_dir, safe_name)
                with open(saved_path, "wb") as buffer:
                    while chunk := await upload.read(1024 * 1024):
                        buffer.write(chunk)
                file_tuples.append((safe_name, saved_path))

        res = monitor_service.create_dataset(
            title=title,
            slug=slug,
            is_private=is_private,
            local_folder_path=local_folder_path,
            file_tuples=file_tuples,
            description=description,
            license_name=license_name,
            subtitle=subtitle,
            target_account=target_account or "kitnehi1211",
            auto_share_all=auto_share_all
        )
        return res
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

@app.post("/api/datasets/create-local")
def create_dataset_local_endpoint(req: DatasetCreateJsonRequest):
    """Tạo dataset từ thư mục local trên máy (không cần upload qua HTTP)."""
    return monitor_service.create_dataset(
        title=req.title,
        slug=req.slug,
        is_private=req.is_private,
        local_folder_path=req.local_folder_path,
        description=req.description,
        license_name=req.license_name,
        subtitle=req.subtitle,
        target_account=req.target_account or "kitnehi1211",
        auto_share_all=getattr(req, "auto_share_all", True)
    )

@app.post("/api/datasets/set-privacy")
def set_dataset_privacy_endpoint(req: DatasetPrivacyRequest):
    """Thay đổi quyền Private <-> Public cho dataset trên Kaggle."""
    return monitor_service.update_dataset_privacy(
        dataset_ref=req.ref,
        is_private=req.is_private
    )

@app.get("/api/competitions")
def get_competitions(account: str = Query(None)):
    all_competitions = monitor_service.data.get("all_competitions", [])
    if account and account.strip():
        return [c for c in all_competitions if c.get("account") == account.strip()]
    return all_competitions

@app.post("/api/refresh")
def trigger_refresh(background_tasks: BackgroundTasks):
    if monitor_service.is_refreshing:
        return {"success": False, "message": "Đang có tiến trình đồng bộ đang chạy. Vui lòng chờ."}
    
    background_tasks.add_task(run_refresh_with_timestamp)
    return {"success": True, "message": "Đã bắt đầu tiến trình đồng bộ lại dữ liệu..."}

@app.get("/api/refresh/status")
def get_refresh_status():
    return {
        "is_refreshing": monitor_service.is_refreshing,
        "progress": monitor_service.refresh_progress,
        "last_synced": monitor_service.data.get("last_synced_human", "Chưa đồng bộ")
    }

class AutoRefreshSettingRequest(BaseModel):
    interval_seconds: int

@app.get("/api/settings/auto-refresh")
def get_auto_refresh_settings():
    interval = auto_refresh_state["interval_seconds"]
    is_enabled = auto_refresh_state["is_enabled"] and interval > 0
    now = time.time()
    elapsed = max(0, now - auto_refresh_state["last_run_timestamp"])
    remaining = max(0, interval - elapsed) if is_enabled else None
    
    return {
        "enabled": is_enabled,
        "interval_seconds": interval,
        "interval_minutes": round(interval / 60, 1) if interval > 0 else 0,
        "last_run_timestamp": auto_refresh_state["last_run_timestamp"],
        "seconds_until_next_refresh": int(remaining) if remaining is not None else 0,
        "is_refreshing": monitor_service.is_refreshing,
        "refresh_progress": monitor_service.refresh_progress,
        "last_synced_human": monitor_service.data.get("last_synced_human", "Chưa đồng bộ")
    }

@app.post("/api/settings/auto-refresh")
def update_auto_refresh_settings(req: AutoRefreshSettingRequest):
    interval = req.interval_seconds
    if interval < 0:
        raise HTTPException(status_code=400, detail="Khoảng thời gian không hợp lệ.")
    
    clamped = False
    if 0 < interval < MIN_SAFE_AUTO_REFRESH_SECONDS:
        interval = MIN_SAFE_AUTO_REFRESH_SECONDS
        clamped = True

    auto_refresh_state["interval_seconds"] = interval
    auto_refresh_state["is_enabled"] = bool(interval > 0)
    auto_refresh_state["last_run_timestamp"] = time.time()
    save_config()

    if interval > 0:
        msg = f"Đã thiết lập tự động làm mới Kaggle mỗi {interval // 60} phút."
        if clamped:
            msg += f" (Đã nâng lên mức an toàn tối thiểu {MIN_SAFE_AUTO_REFRESH_SECONDS // 60} phút để chống Rate Limit)."
    else:
        msg = "Đã tắt tự động làm mới Kaggle."

    return {
        "success": True,
        "message": msg,
        "interval_seconds": interval,
        "interval_minutes": interval // 60 if interval > 0 else 0,
        "enabled": interval > 0
    }

@app.get("/api/credentials")
def get_credentials_info():
    files = monitor_service.get_credential_files()
    result = []
    for f in files:
        fname = os.path.basename(f)
        try:
            with open(f, "r", encoding="utf-8") as fp:
                c = json.load(fp)
                result.append({
                    "file": fname,
                    "username": c.get("username", "Trống"),
                    "key_masked": (c.get("key", "")[:4] + "••••••••" + c.get("key", "")[-4:]) if c.get("key") else "None",
                    "valid_format": bool(c.get("username") and c.get("key"))
                })
        except Exception as e:
            result.append({
                "file": fname,
                "username": "Lỗi định dạng",
                "key_masked": "---",
                "valid_format": False,
                "error": str(e)
            })
    return result

@app.get("/api/export/json")
def export_json():
    return JSONResponse(
        content=monitor_service.data,
        headers={"Content-Disposition": "attachment; filename=kaggle_dashboard_export.json"}
    )

if __name__ == "__main__":
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=False)
