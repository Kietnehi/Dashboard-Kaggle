# Kaggle Multi-Account Monitor Dashboard

Dashboard web chạy local để theo dõi nhiều tài khoản Kaggle trong cùng một giao diện. Ứng dụng đọc các file credential trong thư mục `Credentials/`, đồng bộ notebooks, datasets, competitions, trạng thái notebook đang chạy và quota GPU/TPU theo từng tài khoản.

> Ứng dụng này chỉ dành cho việc quản lý các tài khoản mà bạn sở hữu hoặc được phép quản lý. Không commit API key, token hoặc file credential vào Git.

## Tính năng

- Tổng quan số lượng tài khoản, notebooks, datasets và competitions.
- Lọc dữ liệu theo tài khoản, trạng thái và từ khóa.
- Theo dõi notebook đang ở trạng thái `RUNNING` hoặc `QUEUED`.
- Kiểm tra hardware, thời gian chạy, input, output và log của notebook khi Kaggle cung cấp dữ liệu.
- Quota GPU/TPU theo từng tài khoản, lấy trực tiếp từ endpoint quota của Kaggle.
- Phân biệt quota đã lấy từ Kaggle với quota được nhập thủ công.
- Đồng bộ toàn bộ dữ liệu bằng nút **Làm mới** và quét quota riêng bằng **Quét lại Real-time**.
- Cache local để dashboard mở nhanh và không gọi Kaggle cho mọi lần tải lại giao diện.

## Yêu cầu

- Windows 10/11 hoặc môi trường có Python tương thích.
- Python 3.11 trở lên được khuyến nghị.
- Mỗi tài khoản Kaggle cần một file credential hợp lệ theo định dạng legacy `kaggle.json`:

```json
{
  "username": "your-kaggle-username",
  "key": "your-kaggle-api-key"
}
```

Không đưa giá trị `key` thật vào README, issue, log hoặc repository.

## Cài đặt

Mở PowerShell tại thư mục project:

```powershell
cd "C:\Users\ADMIN\Desktop\Dashboard-Kaggle"
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Nếu PowerShell chặn kích hoạt môi trường ảo, có thể cài dependency bằng Python hiện tại:

```powershell
python -m pip install -r requirements.txt
```

## Thêm credential Kaggle

Tạo thư mục `Credentials/` nếu chưa có, sau đó đặt một hoặc nhiều file JSON vào đó. Tên file có thể tùy ý, ví dụ:

```text
Credentials/
├── kaggle.json
├── kaggle (1).json
└── kaggle (2).json
```

Ứng dụng lấy `username` và `key` từ từng file để tạo client riêng cho từng tài khoản. Nó không nên dùng chung `KAGGLE_API_TOKEN` hoặc `~/.kaggle/access_token` để tránh hiển thị nhầm dữ liệu giữa các account.

## Chạy website

### Windows — một lần bấm

Chạy file `run_dashboard.bat`. Server sẽ mở tại:

```text
http://127.0.0.1:8000
```

### PowerShell

```powershell
cd "C:\Users\ADMIN\Desktop\Dashboard-Kaggle"
python start.py
```

Hoặc chạy trực tiếp bằng Uvicorn:

```powershell
python -m uvicorn main:app --host 127.0.0.1 --port 8000
```

Nhấn `Ctrl+C` trong cửa sổ server để dừng website.

## Cơ chế cập nhật dữ liệu

- **Làm mới**: gọi Kaggle để đồng bộ lại notebooks, datasets, competitions và sau đó cập nhật quota. Với nhiều tài khoản, thao tác này có thể mất vài phút.
- **Quét lại Real-time**: chỉ gọi Kaggle để lấy quota GPU/TPU mới nhất cho từng account.
- **Tự động tải lại giao diện**: chỉ đọc cache hiện tại và tải lại các API local; không tự đồng bộ toàn bộ dữ liệu mới từ Kaggle.
- **Kiểm tra nền**: server kiểm tra trạng thái notebook đang chạy theo chu kỳ an toàn khoảng hai phút để hạn chế rate limit.

Cache được tạo tại `data_cache.json` và `quota_data.json` sau khi chạy. Hai file này bị Git bỏ qua vì chứa trạng thái/account data local và không cần thiết để build source.

## API local chính

| Method | Endpoint | Mục đích |
|---|---|---|
| `GET` | `/api/overview` | KPI và trạng thái đồng bộ |
| `GET` | `/api/accounts` | Danh sách account và số lượng dữ liệu |
| `GET` | `/api/kernels` | Danh sách notebooks |
| `GET` | `/api/datasets` | Danh sách datasets |
| `GET` | `/api/competitions` | Danh sách competitions |
| `GET` | `/api/quotas` | Quota đang lưu trong cache |
| `POST` | `/api/quotas/scan-now` | Quét quota trực tiếp từ Kaggle |
| `POST` | `/api/refresh` | Bắt đầu đồng bộ toàn bộ dữ liệu |
| `GET` | `/api/refresh/status` | Tiến trình đồng bộ |

FastAPI cũng cung cấp tài liệu API tại `http://127.0.0.1:8000/docs` khi server đang chạy.

## Cấu trúc project

```text
Dashboard-Kaggle/
├── Credentials/          # Local only — không commit
├── static/index.html     # Giao diện dashboard
├── kaggle_service.py     # Kaggle clients, đồng bộ dữ liệu, quota và cache
├── main.py               # FastAPI routes và background scanner
├── start.py              # Khởi động server và mở browser
├── run_dashboard.bat     # Lệnh chạy nhanh trên Windows
├── requirements.txt      # Python dependencies
├── data_cache.json       # Local generated cache — bị ignore
└── quota_data.json       # Local generated quota — bị ignore
```

`.kaggle_runtime/` có thể tồn tại như runtime local của máy phát triển nhưng không được commit. Khi clone repository, `requirements.txt` sẽ cài Kaggle SDK cần thiết; nếu đã có thư mục runtime local thì ứng dụng sẽ ưu tiên dùng nó.

## Xử lý lỗi thường gặp

### Không thấy account hoặc dữ liệu

1. Kiểm tra JSON trong `Credentials/` có đúng hai trường `username` và `key`.
2. Chạy lại server rồi bấm **Làm mới**.
3. Xem log trong cửa sổ PowerShell để biết account nào bị lỗi xác thực hoặc rate limit.

### Quota chưa đổi

Nút tự động tải lại giao diện chỉ đọc cache. Hãy bấm **Quét lại Real-time** để lấy quota trực tiếp; không nên bấm liên tục vì Kaggle có rate limit.

### Port 8000 đang được sử dụng

Dừng tiến trình cũ bằng `Ctrl+C`, hoặc chạy port khác:

```powershell
python -m uvicorn main:app --host 127.0.0.1 --port 8001
```

## Bảo mật trước khi push Git

Trước mỗi commit, kiểm tra:

```powershell
git status --short
git check-ignore -v Credentials\kaggle.json data_cache.json quota_data.json
```

Nếu đã từng commit nhầm API key, chỉ xóa file ở commit mới là chưa đủ; cần thu hồi/regenerate key trên Kaggle và làm sạch lịch sử Git trước khi repository được chia sẻ.

## License

Chưa gắn license mặc định. Chỉ thêm license sau khi xác định rõ quyền sử dụng source code và các thành phần phụ thuộc.
