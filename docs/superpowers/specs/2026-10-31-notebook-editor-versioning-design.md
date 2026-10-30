# Thiết Kế: Kaggle Notebook Studio & Versioning Workflow (Chỉnh Sửa Notebook & Chạy Version Mới)

- **Ngày tạo:** 2026-10-31
- **Trạng thái:** Chờ phê duyệt (Pending Approval)
- **Tác giả:** DeepSeek Harness AI Agent & Team Lead
- **Vị trí file:** `docs/superpowers/specs/2026-10-31-notebook-editor-versioning-design.md`

---

## 1. Mục Đích & Bối Cảnh (Intent & The Why)

### 1.1 Vấn đề thực tế
Khi huấn luyện mô hình Deep Learning hoặc thi đấu trên Kaggle, quy trình nghiên cứu mang tính lặp (iterative workflow):
1. Người dùng chạy thử nghiệm **Version 1** (với tập dữ liệu ban đầu, code baseline).
2. Sau khi Version 1 hoàn thành (hoặc crash/cần tinh chỉnh), người dùng cần:
   - Sửa các ô code (hyperparameters, model architecture, training loop, inference script).
   - Thêm ô code mới (ví dụ: bổ sung cell validation, test TTA, bóc tách checkpoint).
   - Thêm hoặc bớt **Input Data Sources** (thêm Kaggle Model weights mới, thêm Dataset phụ trợ, thêm output từ Notebook tiền xử lý trước đó).
   - Tiếp tục đẩy phiên bản mới (**Version 2**) lên đúng notebook đó trên cùng tài khoản (hoặc đổi sang tài khoản có GPU dồi dào hơn) để chạy tiếp nối.

### 1.2 Hiện trạng trên Dashboard-Kaggle
- Hiện tại chưa có trình soạn thảo hoặc chỉnh sửa ô cell trực tiếp trên Dashboard. Nếu muốn sửa code, người dùng phải mở file cục bộ hoặc lên trực tiếp trang web Kaggle.
- Tab Inspector đã có thể xem code snapshot và xem log của từng Version (Version 1, Version 2,...), nhưng thiếu nút hành động để **1-click nạp phiên bản đó vào trình soạn thảo** để tạo Version kế tiếp.
- Kaggle API hỗ trợ `model_data_sources` (đính kèm Model từ Kaggle Models Hub), nhưng Dashboard hiện chỉ mới xử lý `dataset_sources`, `kernel_sources` và `competition_sources`.

---

## 2. Mục Tiêu, Phi Mục Tiêu & Chống Mục Tiêu (Goals, Non-Goals & Anti-Goals)

### 2.1 Mục tiêu (Goals)
1. **Interactive Notebook Studio trong Training Hub:**
   - Cung cấp giao diện chỉnh sửa ô cell trực quan (Jupyter / Kaggle style) ngay trong tab `view-trainer`:
     - Xem danh sách từng ô cell với loại `Code` hoặc `Markdown`.
     - Chỉnh sửa nội dung cell trực tiếp với font đơn cự `JetBrains Mono` / Monospace rõ ràng.
     - Thêm ô code / ô markdown ở bất kỳ vị trí nào (Thêm cell ở đầu, giữa, cuối).
     - Xóa ô cell, di chuyển ô cell lên/xuống (Move Up/Down).
     - Hỗ trợ nút chuyển đổi nhanh (Toggle) sang chế độ **Full Raw Script / JSON** cho ai muốn paste toàn bộ code nhanh chóng.
2. **Quản lý Input Toàn Diện (Hỗ trợ Models Hub):**
   - Hỗ trợ đầy đủ 4 loại Input của Kaggle: **Datasets**, **Models** (mới), **Competitions**, và **Upstream Notebooks**.
   - Hỗ trợ xóa từng input bằng nút `✕`, nhập link Kaggle trực tiếp hoặc chọn qua Autocomplete suggestions.
3. **Luồng 1-Click "Chỉnh Sửa & Chạy Version Mới" (Iterative Versioning):**
   - Bổ sung nút **"✏️ Sửa & Chạy Version Mới"** trong Inspector Modal (kế thừa chính xác snapshot mã nguồn và danh sách input của Version đang xem).
   - Bổ sung nút **"✏️ Chỉnh sửa"** trên từng hàng Notebook trong bảng Kernels.
   - Hiển thị thông báo trạng thái rõ ràng: *"Đang chỉnh sửa từ Version 1 ➔ Sẽ tạo Version 2 trên Kaggle"*.
4. **Backend Kaggle Service mở rộng:**
   - Cập nhật `/api/runner/notebook-meta` để nhận tham số `version` (cho phép tải đúng snapshot của Version 1 thay vì luôn lấy latest).
   - Cập nhật `KernelRunRequest` và `push_and_run_kernel` để nhận và gửi `model_sources` (`model_data_sources`) lên Kaggle API.

### 2.2 Phi mục tiêu (Non-Goals)
- Không biến Dashboard thành một JupyterLab tương tác thời gian thực chạy code qua WebSocket kernel cục bộ. Quy trình thực thi tuân theo kiến trúc headless batch execution chuẩn của Kaggle (Push & Run).
- Không xây dựng tính năng chỉnh sửa đồng thời nhiều người (real-time collaborative editing).

### 2.3 Chống mục tiêu (Anti-Goals)
- **Không phá vỡ luồng chạy hiện tại:** Người dùng chỉ muốn chạy nhanh file `.ipynb` từ máy tính hoặc kéo thả file vẫn hoạt động bình thường, không bị bắt buộc phải mở cell editor.
- **Không thêm thư viện nặng:** Không cài đặt npm packages nặng hay bundler phức tạp. Tiếp tục duy trì chuẩn vanilla JS tối ưu, tốc độ cao, zero-bloat.

---

## 3. Các Phương Án Kiến Trúc & Quyết Định (Approaches & Decision)

### 3.1 Các phương án đã cân nhắc
1. **Phương án 1 (Đã chọn): Tích hợp Notebook Studio trực tiếp vào Trainer Hub.**
   - Biến tab Huấn Luyện thành trung tâm điều khiển và chỉnh sửa toàn năng: vừa sửa cell, vừa xem gợi ý GPU quota, vừa chọn tài khoản, vừa theo dõi live streaming log ở cột bên phải.
   - *Ưu điểm:* Luồng công việc liền mạch, không bị xé nhỏ giao diện.
2. **Phương án 2: Cửa sổ Modal Editor chuyên dụng độc lập.**
   - Mở popup toàn màn hình.
   - *Nhược điểm:* Phải duplicate các logic chọn GPU, chọn account và live console log vào trong modal, gây chật chội và khó quản lý.
3. **Phương án 3: Hybrid - Cho phép edit tại Inspector Modal.**
   - *Nhược điểm:* Inspector Modal vốn dĩ phục vụ mục đích kiểm tra (inspect) log và kết quả; đưa chức năng cấu hình chạy và submit vào làm loãng trải nghiệm người dùng.

### 3.2 Quyết định của người dùng
Người dùng đã lựa chọn **Phương án 1: Tích hợp Notebook Studio trực tiếp vào Trainer Hub**.

---

## 4. Thiết Kế Chi Tiết Hệ Thống (Detailed Architecture & Design)

### 4.1 Cập nhật Backend (`main.py` & `kaggle_service.py`)
1. **Hỗ trợ `version` trong API lấy mã nguồn notebook:**
   - Endpoint: `GET /api/runner/notebook-meta?account={acc}&ref={ref}&version={ver}`
   - `kaggle_service.py` hàm `get_notebook_source_and_meta`:
     - Nhận thêm `version: Optional[int] = None`.
     - Nếu `version` được truyền, thiết lập `req_meta.version_label = f"v{version}"`.
     - Trích xuất thêm `model_sources` từ `meta.get("modelDataSources")` hoặc `meta.get("modelSources")`.
     - Trả về `current_version_number` và `loaded_version`.
2. **Hỗ trợ `model_sources` trong API thực thi notebook:**
   - `KernelRunRequest` thêm trường: `model_sources: Optional[List[str]] = []`.
   - `push_and_run_kernel` trong `kaggle_service.py`:
     - Nhận `model_sources: list = None`.
     - Chuẩn hóa link Kaggle Model (ví dụ: `https://www.kaggle.com/models/google/gemma-2/pyTorch/...` ➔ trích xuất `google/gemma-2/...`).
     - Gán `request.model_data_sources = final_models`.

### 4.2 Thiết Kế Giao Diện Frontend (`static/index.html`)

#### A. Khối "Notebook Studio: Soạn Thảo & Chỉnh Sửa Mã Nguồn"
Đặt ngay bên dưới Step 2 (Tiêu đề & Slug) trong `view-trainer`:
- **Thanh công cụ Studio (Toolbar):**
  - Đếm số lượng cells: `[X] Cells (Code: Y, Markdown: Z)` kèm dung lượng KB.
  - Badge trạng thái phiên bản: Ví dụ `Kế thừa từ: Version 1` ➔ `Mục tiêu: Sẽ tạo Version 2`.
  - Nút thêm cell: `+ Ô Code`, `+ Ô Markdown`.
  - Nút chuyển chế độ: `Xem dạng Cells (Mặc định)` ⟷ `Xem Full Code (Raw Script)`.
- **Giao diện Danh sách Cells (Interactive Cell List):**
  - Mỗi cell có:
    - Số thứ tự: `[Cell 1: Code]` hoặc `[Cell 2: Markdown]`.
    - Thanh điều khiển nhỏ trên từng cell:
      - Nút chuyển loại (Code ⟷ Markdown).
      - Nút di chuyển lên (↑), di chuyển xuống (↓).
      - Nút thu gọn / mở rộng cell (▼ / ▲).
      - Nút xóa cell (🗑️).
    - Vùng gõ code: Textarea font đơn cự `JetBrains Mono`, tự co giãn chiều cao hoặc có thanh cuộn mượt mà, hỗ trợ thụt lề bằng phím Tab.
- **Chế độ Full Raw Script:**
  - Một ô soạn thảo lớn với font `JetBrains Mono` hiển thị toàn bộ mã nguồn script (hoặc JSON notebook), có thể chỉnh sửa tự do. Hai chế độ tự động đồng bộ hóa với nhau khi chuyển đổi.

#### B. Nâng Cấp Khối Input Sources (Hỗ trợ Models Hub)
- Thêm tab hoặc phân loại rõ ràng trong Autocomplete & Chips:
  - **Dataset** (Màu lam nhạt / Cyan)
  - **Model** (Màu xanh lá / Emerald) - Mới!
  - **Notebook** (Màu tím / Purple)
  - **Competition** (Màu cam / Amber)
- Cho phép người dùng dán trực tiếp link Model Kaggle (ví dụ: `https://www.kaggle.com/models/google/gemma-2/transformers/...`) ➔ tự động nhận diện là `model` và thêm vào danh sách Input.

#### C. Hành Động 1-Click Từ Inspector & Kernels List
1. **Trong Inspector Modal (`inspectorModal`):**
   - Thêm nút nổi bật tại header: **"✏️ Chỉnh Sửa & Chạy Version Mới"**.
   - Khi bấm:
     - Đọc version hiện tại đang xem trong dropdown (`inspectorVersionSelect`).
     - Chuyển sang tab Trainer (`switchTab('trainer')`).
     - Gọi `onTrainerNotebookSelectChange(ref, account, version)`.
     - Tự động nạp mã nguồn, danh sách input (datasets, models, competitions, kernels), cấu hình phần cứng của version đó vào Studio.
     - Đặt tiêu đề và slug giữ nguyên (để Kaggle tự động tạo version tiếp theo) kèm thông báo cho người dùng.
2. **Trong Bảng Notebooks (`renderKernelsTab`):**
   - Thêm nút hành động nhanh **"Sửa & Chạy"** bên cạnh nút "Xem Log & Input" và "Huấn luyện GPU".

---

## 5. Ràng Buộc & An Toàn (Constraints & Safeguards)
- **Zero Local GPU/VRAM:** Toàn bộ công việc thực hiện trên giao diện web và Kaggle API headless, tuyệt đối không dùng GPU máy tính.
- **Bảo toàn dữ liệu cũ:** Khi người dùng mở một notebook sẵn có để tạo version 2, mã nguồn gốc trên máy hoặc bản lưu trên Kaggle không bị xóa hay ghi đè mất dấu mà được Kaggle lưu thành một version mới trong lịch sử (Version history).
- **Auto-Sync:** Khi người dùng sửa nội dung trong các ô cell, biến `currentTrainerSourceCode` tự động cập nhật cấu trúc JSON `.ipynb` chuẩn `nbformat: 4` để khi bấm "1-Click: Tự Động Share & Huấn Luyện GPU" thì Kaggle nhận diện đúng định dạng notebook.

---

## 6. Các Quyết Định Kỹ Thuật Đã Chốt (Builder Decisions & Edge Cases)

1. **Cơ chế đồng bộ 2 chiều giữa Cell Editor và Raw Script (chuẩn `# %%`):**
   - Cấu trúc dữ liệu nội tại lưu mảng các ô `studioCells = [{ id, type: 'code'|'markdown', source: '...' }]`.
   - Trong chế độ Cell Editor: Mỗi ô cell có textarea riêng biệt, hỗ trợ kéo thả, thêm/xóa/sửa.
   - Trong chế độ Raw Script: Hiển thị mã nguồn Python trực quan với các điểm phân tách cell chuẩn `# %%` (cho code) và `# %% [markdown]` (cho ghi chú) - tương thích hoàn hảo với VS Code & Jupyter.
   - Khi chuyển từ Raw Script về dạng Cells: Bộ parser tự động tách theo delimiter `# %%` và `# %% [markdown]`, giữ nguyên vẹn cấu trúc cell của người dùng mà không bắt họ phải gõ JSON.
   - Hàm `syncStudioToSourceCode()` tự động sinh chuỗi JSON Jupyter `.ipynb` chuẩn `nbformat: 4` để nạp vào `currentTrainerSourceCode` khi gửi lệnh lên Kaggle.

2. **Xử lý tài khoản thực thi (Target Account) & Đổi Slug:**
   - Khi người dùng giữ nguyên tài khoản (ví dụ `@kitnehi1211`): Kaggle tạo Version tiếp theo trên namespace `kitnehi1211/slug`.
   - Khi người dùng chọn tài khoản phụ khác (ví dụ `@baovuong3009`):
     - Slug được áp dụng cho namespace mới `baovuong3009/slug`.
     - Nếu slug đã tồn tại trên `@baovuong3009`, Kaggle tạo version tiếp theo trên tài khoản đó. Nếu chưa, tạo notebook mới Version 1.
     - Hệ thống tự động kích hoạt cơ chế `Auto-Share` tất cả các Dataset / Model riêng tư sang `@baovuong3009` để không bị lỗi thiếu quyền truy cập dữ liệu.
   - Người dùng có thể tự do sửa trường `Slug` bất cứ lúc nào nếu muốn rẽ nhánh (branching) thành một notebook độc lập.

3. **Quy chuẩn định dạng Kaggle Model Sources & Autocomplete:**
   - Kaggle API yêu cầu chuỗi dạng `owner/model-slug/framework/variation` (ví dụ `google/gemma-2/transformers/2b-it`).
   - Hàm `_clean_source` trên backend bóc tách đầy đủ 4 phân đoạn từ URL `https://www.kaggle.com/models/owner/slug/framework/variation/...` hoặc chuỗi do người dùng nhập.
   - Frontend bổ sung danh sách Pinned Models thịnh hành (Gemma 2, Llama 3, Qwen 2.5, DeepSeek) và cho phép dán bất kỳ link Model Kaggle nào vào ô input search để thêm nhanh.

4. **Tính toán Version mục tiêu (Head Version + 1):**
   - Lịch sử version trên Kaggle luôn tăng đơn điệu. Khi người dùng nạp snapshot từ một phiên bản cũ (ví dụ Version 1 trong khi notebook đã có 5 phiên bản), backend trả về cả `loaded_version = 1` và `current_head_version = 5`.
   - Badge thông báo hiển thị chính xác: *"Kế thừa code từ Version 1 ➔ Sẽ tạo Version 6 trên Kaggle"* (hoặc Version 2 nếu hiện tại mới chỉ có Version 1).


