import os
import sys
import time
import threading
import json
from fastapi import FastAPI, Query, BackgroundTasks, HTTPException, Body
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import uvicorn

from kaggle_service import monitor_service, BASE_DIR

app = FastAPI(title="Kaggle Multi-Account Monitor Dashboard", version="1.2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

STATIC_DIR = os.path.join(BASE_DIR, "static")
os.makedirs(STATIC_DIR, exist_ok=True)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

class QuotaUpdateRequest(BaseModel):
    gpu_remaining: Optional[float] = None
    tpu_remaining: Optional[float] = None
    notes: Optional[str] = None

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
    summary = monitor_service.data.get("summary", {})
    cache_has_no_content = (
        monitor_service.data.get("accounts")
        and not summary.get("total_kernels")
        and not summary.get("total_datasets")
        and not summary.get("total_competitions")
    )
    if not monitor_service.data.get("accounts") or cache_has_no_content:
        threading.Thread(target=monitor_service.refresh_all, daemon=True).start()
    threading.Thread(target=periodic_running_scanner, daemon=True).start()

@app.get("/", response_class=HTMLResponse)
def get_index():
    index_path = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(index_path):
        with open(index_path, "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
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
    account: str = Query(None, description="Lọc theo username"),
    status: str = Query(None, description="Lọc theo status (RUNNING, QUEUED, ERROR, COMPLETE)"),
    search: str = Query(None, description="Tìm theo tên kernel")
):
    all_kernels = monitor_service.data.get("all_kernels", [])
    filtered = all_kernels

    if account and account.strip():
        filtered = [k for k in filtered if k.get("account") == account.strip()]

    if status and status.strip() and status.strip().upper() != "ALL":
        stat = status.strip().upper()
        if stat == "RUNNING":
            filtered = [k for k in filtered if k.get("status") in ["RUNNING", "RUNNING_INTERACTIVE"]]
        elif stat == "QUEUED":
            filtered = [k for k in filtered if k.get("status") == "QUEUED"]
        elif stat == "ERROR":
            filtered = [k for k in filtered if k.get("status") in ["ERROR", "CANCEL_ACKNOWLEDGED", "FAILED"]]
        elif stat == "COMPLETE":
            filtered = [k for k in filtered if k.get("status") == "COMPLETE"]
        else:
            filtered = [k for k in filtered if k.get("status") == stat]

    if search and search.strip():
        s = search.strip().lower()
        filtered = [k for k in filtered if s in k.get("title", "").lower() or s in k.get("ref", "").lower() or s in k.get("account", "").lower()]

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
    
    background_tasks.add_task(monitor_service.refresh_all)
    return {"success": True, "message": "Đã bắt đầu tiến trình đồng bộ lại dữ liệu..."}

@app.get("/api/refresh/status")
def get_refresh_status():
    return {
        "is_refreshing": monitor_service.is_refreshing,
        "progress": monitor_service.refresh_progress,
        "last_synced": monitor_service.data.get("last_synced_human", "Chưa đồng bộ")
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
