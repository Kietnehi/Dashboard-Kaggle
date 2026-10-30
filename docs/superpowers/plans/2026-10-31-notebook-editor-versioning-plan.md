# Kaggle Notebook Studio & Versioning Workflow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Xây dựng tính năng Notebook Studio cho phép chỉnh sửa trực tiếp các ô cell (Code & Markdown), hỗ trợ thêm/bớt Input (Datasets, Models, Upstream Notebooks, Competitions), và kích hoạt luồng 1-click "Chỉnh Sửa & Chạy Version Mới" (tạo Version 2 từ Version 1) ngay trên Dashboard-Kaggle.

**Architecture:** Mở rộng backend FastAPI (`main.py` và `kaggle_service.py`) để hỗ trợ lấy metadata/code theo phiên bản cụ thể (`version_label`) và hỗ trợ `model_sources` (`model_data_sources` trong Kaggle API). Frontend (`static/index.html`) được nâng cấp với component Notebook Studio có khả năng biên tập 2 chiều (Cell-based editor ⟷ Full raw script) cùng các nút kích hoạt 1-click từ Inspector Modal và bảng Kernels.

**Tech Stack:** Python 3.10+, FastAPI, Kaggle Official Python SDK (`kagglesdk`), Vanilla JavaScript (ES6+), Tailwind CSS, Lucide Icons.

**Spec:** `docs/superpowers/specs/2026-10-31-notebook-editor-versioning-design.md`

## Global Constraints

- **ZERO-GPU & ZERO-VRAM:** Tuyệt đối không load model hay tác vụ AI nặng vào GPU cục bộ; mọi lệnh chạy notebook được gửi headless lên Kaggle.
- **Bảo toàn dữ liệu:** Giữ nguyên khả năng chạy nhanh từ file local/upload mà không bắt buộc phải sửa cell nếu người dùng không muốn.
- **Tương thích ngược:** Định dạng lưu notebook luôn tuân theo chuẩn Jupyter Notebook `nbformat: 4` để Kaggle phân giải trơn tru.
- **Độc lập nền tảng:** Không dùng thêm npm build step hay bundler phức tạp; duy trì vanilla JS + Tailwind CSS đồng nhất với toàn bộ Dashboard.

## Review Focus

1. **Phiên bản snapshot không đúng:** Khi người dùng chọn xem Version 1 trong Inspector rồi bấm "Sửa & Chạy", hệ thống phải lấy đúng code và input của Version 1 chứ không lấy nhầm phiên bản mới nhất (Version latest).
2. **Kaggle Model URL parsing:** Người dùng có thể dán nhiều dạng URL khác nhau (ví dụ: `https://www.kaggle.com/models/google/gemma-2/transformers/2b-it`, `https://www.kaggle.com/models/google/gemma-2/pyTorch/2b-it/1`, hoặc chuỗi `google/gemma-2/transformers/2b-it`). Parser phải bóc tách đúng định dạng 4 thành phần mà Kaggle API yêu cầu.
3. **Đồng bộ 2 chiều (Two-way Sync):** Khi người dùng sửa ô cell trong giao diện danh sách cell rồi bấm Submit mà không chuyển qua Raw View, chuỗi `currentTrainerSourceCode` vẫn phải được tự động tái tạo đúng chuẩn JSON `.ipynb` và chứa đầy đủ thay đổi mới nhất.
4. **Xóa/Thêm ô cell rỗng:** Khi người dùng xóa hết các cell hoặc thêm cell trắng, hệ thống phải xử lý an toàn, không sinh lỗi parse JSON `null` hoặc làm crash modal/form.
5. **Chuyển đổi Account khi tạo Version:** Khi chạy notebook trên tài khoản phụ khác với tài khoản sở hữu bản gốc, hệ thống phải tự động auto-share các dataset phụ thuộc sang tài khoản đó để tránh lỗi `403 Forbidden` trên Kaggle.

---

### Task 1: Backend Support for Notebook Versioning & Kaggle Models Input

**Files:**
- Create: `test_notebook_editor_backend.py`
- Modify: `kaggle_service.py:2336-2400` (`get_notebook_source_and_meta`)
- Modify: `kaggle_service.py:2400-2650` (`push_and_run_kernel` and `_clean_source`)
- Modify: `main.py:143-158` (`KernelRunRequest`)
- Modify: `main.py:406-452` (`/api/runner/notebook-meta` and `/api/runner/run`)

**Interfaces:**
- Consumes: Kaggle API `ApiGetKernelRequest`, `ApiSaveKernelRequest`, `kagglesdk`.
- Produces:
  - `GET /api/runner/notebook-meta?account={acc}&ref={ref}&version={ver}` -> `{ success, ref, title, slug, version_number, model_sources, dataset_sources, kernel_sources, competition_sources, source_code }`
  - `POST /api/runner/run` with `model_sources: List[str]` accepted and forwarded to `push_and_run_kernel`.

- [ ] **Step 1: Write the failing tests in `test_notebook_editor_backend.py`**
  Viết test kiểm tra:
  - `get_notebook_source_and_meta` nhận tham số `version` và trả về đúng metadata, bao gồm `model_sources`.
  - `_clean_source` nhận diện chính xác link Model Kaggle và chuyển thành `('model', 'owner/name/framework/variation')`.
  - `KernelRunRequest` chấp nhận trường `model_sources: Optional[List[str]] = []`.

- [ ] **Step 2: Run test to verify it fails**
  Run: `./.venv/bin/python3 -m unittest test_notebook_editor_backend.py -v`
  Expected: FAIL with missing fields/arguments or assertion error.

- [ ] **Step 3: Implement backend changes in `kaggle_service.py` and `main.py`**
  - Trong `kaggle_service.py`: Cập nhật `get_notebook_source_and_meta(self, account_username, kernel_ref, version=None)` để gán `req_meta.version_label = f"v{version}"` nếu có `version`. Trích xuất `meta.get("modelDataSources")` hoặc `meta.get("modelSources")`.
  - Trong `kaggle_service.py`: Cập nhật `push_and_run_kernel` nhận thêm `model_sources: list = None`. Trong hàm `_clean_source`, nhận diện regex `r'kaggle\.com/models/([a-zA-Z0-9_\-]+)/([a-zA-Z0-9_\-]+)/([a-zA-Z0-9_\-]+)/([a-zA-Z0-9_\-]+)'` và trả về `('model', ...)`. Gán `request.model_data_sources = final_models`.
  - Trong `main.py`: Thêm `model_sources: Optional[List[str]] = []` vào `KernelRunRequest`. Thêm `version: Optional[int] = Query(None)` vào `@app.get("/api/runner/notebook-meta")` và chuyển tiếp đến `monitor_service.get_notebook_source_and_meta`.

- [ ] **Step 4: Run test to verify it passes**
  Run: `./.venv/bin/python3 -m unittest test_notebook_editor_backend.py -v`
  Expected: PASS

- [ ] **Step 5: Commit changes**
  ```bash
  git add test_notebook_editor_backend.py kaggle_service.py main.py
  git commit -m "feat(backend): support notebook versioning and model sources in runner"
  ```

---

### Task 2: Frontend Notebook Studio & Interactive Cell Editor Component

**Files:**
- Modify: `static/index.html` (Phần layout trong `<section id="view-trainer">` và phần JavaScript functions)
- Test: `test_notebook_editor_frontend.py` (Kiểm thử cấu trúc DOM, các hàm JS xử lý chuyển đổi cell/JSON và render)

**Interfaces:**
- Consumes:
  - `currentTrainerSourceCode`
  - `studioCells = []` (cấu trúc mảng cell)
  - `syncSourceCodeToStudio()`, `syncStudioToSourceCode()`
- Produces:
  - UI khối Notebook Studio nằm ngay trong tab Trainer giữa Tiêu đề/Slug và Target Account.
  - Các hàm thao tác cell: `addStudioCell(type, index)`, `deleteStudioCell(index)`, `moveStudioCell(index, direction)`, `updateStudioCellSource(index, value)`, `updateStudioCellType(index, newType)`, `toggleStudioCellCollapse(index)`.
  - Chuyển đổi giữa chế độ `cells` và `raw`.

- [ ] **Step 1: Write test to verify frontend component specifications in `test_notebook_editor_frontend.py`**
  Viết script kiểm tra nội dung `static/index.html` có chứa:
  - Container `id="trainerNotebookStudioSection"`.
  - Toolbar với các nút `btnStudioAddCodeCell`, `btnStudioAddMdCell`, `btnStudioToggleViewMode`.
  - Các hàm JS `syncStudioToSourceCode`, `syncSourceCodeToStudio`, `renderStudioCells`, `addStudioCell`, `deleteStudioCell`.
  - Kiểm tra hàm parser JSON `.ipynb` hoạt động chính xác với cấu trúc notebook chuẩn.

- [ ] **Step 2: Run test to verify it fails**
  Run: `./.venv/bin/python3 -m unittest test_notebook_editor_frontend.py -v`
  Expected: FAIL (chưa có các ID và hàm trong `index.html`)

- [ ] **Step 3: Implement Notebook Studio in `static/index.html`**
  - Thêm thẻ HTML `<div id="trainerNotebookStudioSection">` vào `view-trainer` với thiết kế giao diện hiện đại:
    - Header với badge đếm số cells, badge phiên bản (`Kế thừa từ: Version X ➔ Dự kiến tạo: Version X+1`).
    - Nút `+ Ô Code`, `+ Ô Ghi Chú (Markdown)`, nút chuyển `Chế độ Cells / Raw Script`.
    - Danh sách các cell render động với card có viền, số thứ tự cell, nút di chuyển ↑/↓, nút thu gọn, nút xóa 🗑️, và textarea soạn thảo font đơn cự `JetBrains Mono`.
    - Textarea cho chế độ Raw Script.
  - Cài đặt các hàm JS:
    - `syncSourceCodeToStudio(rawSource, isNotebook)`: Phân tích cú pháp JSON `nbformat: 4` hoặc code raw thành mảng `studioCells`.
    - `syncStudioToSourceCode()`: Tái tạo JSON `.ipynb` từ `studioCells` và lưu vào `currentTrainerSourceCode`.
    - `renderStudioCells()`: Vẽ danh sách cell ra DOM, gắn icon Lucide và event listeners.
    - `addStudioCell(type, atIndex)`: Thêm cell mới.
    - `deleteStudioCell(index)`: Xóa cell (có xác nhận nếu cell có nhiều code).
    - `moveStudioCell(index, dir)`: Đổi vị trí cell.
    - `toggleStudioViewMode(mode)`: Chuyển đổi giữa `'cells'` và `'raw'`.

- [ ] **Step 4: Run test to verify it passes**
  Run: `./.venv/bin/python3 -m unittest test_notebook_editor_frontend.py -v`
  Expected: PASS

- [ ] **Step 5: Commit changes**
  ```bash
  git add static/index.html test_notebook_editor_frontend.py
  git commit -m "feat(ui): add interactive notebook studio and cell editor to trainer hub"
  ```

---

### Task 3: Input Sources Enhancement (Models Hub Integration & Input Badges)

**Files:**
- Modify: `static/index.html` (Cập nhật hàm `addInputSource`, `renderSelectedInputs`, `handleTrainerInputSearch`, `addCustomInputSource`, và `submitRunNotebook`)
- Test: `test_notebook_inputs.py` (Kiểm thử logic thêm input Model, render badge màu ngọc lục bảo và payload submit)

**Interfaces:**
- Consumes:
  - `selectedInputSources` mảng đối tượng `{ type: 'dataset'|'kernel'|'competition'|'model', ref: string, title: string }`.
- Produces:
  - Hiển thị badge màu xanh lá ngọc lục bảo (`bg-emerald-50 text-emerald-800 border-emerald-200`) cho Model.
  - Tự động nhận diện URL `https://www.kaggle.com/models/...` thành `type: 'model'`.
  - Tách mảng `model_sources` gửi trong body `fetch('/api/runner/run')`.

- [ ] **Step 1: Write test in `test_notebook_inputs.py`**
  Viết script kiểm tra:
  - URL Model Kaggle được nhận diện đúng loại `model`.
  - Payload gửi lên `/api/runner/run` bóc tách riêng 4 mảng: `dataset_sources`, `kernel_sources`, `competition_sources`, và `model_sources`.

- [ ] **Step 2: Run test to verify it fails**
  Run: `./.venv/bin/python3 -m unittest test_notebook_inputs.py -v`
  Expected: FAIL

- [ ] **Step 3: Implement Model input support in `static/index.html`**
  - Cập nhật hàm `addInputSource(type, ref, title)`: Hỗ trợ `type === 'model'`.
  - Cập nhật hàm `renderSelectedInputs()`: Vẽ chip màu xanh lá sang trọng với icon `box` hoặc `cpu` cho Models.
  - Cập nhật hàm `addCustomInputSource()` và `handleTrainerInputSearch()`: Bắt link dạng `kaggle.com/models/...`.
  - Cập nhật hàm `submitRunNotebook()`: Lọc các input có `type === 'model'` thành mảng `model_sources` và đưa vào JSON gửi lên `/api/runner/run`.

- [ ] **Step 4: Run test to verify it passes**
  Run: `./.venv/bin/python3 -m unittest test_notebook_inputs.py -v`
  Expected: PASS

- [ ] **Step 5: Commit changes**
  ```bash
  git add static/index.html test_notebook_inputs.py
  git commit -m "feat(ui): add kaggle models hub support to input sources in trainer hub"
  ```

---

### Task 4: 1-Click Versioning Workflow from Inspector Modal & Kernels Table

**Files:**
- Modify: `static/index.html` (Thêm nút trong `inspectorModal` header, thêm nút trong `renderKernelsTab`, triển khai hàm `editNotebookFromInspector` và `editNotebookFromTable`)
- Test: `test_versioning_actions.py`

**Interfaces:**
- Consumes:
  - `currentInspectedKernel = { account, ref, version }`
  - `onTrainerNotebookSelectChange(ref, explicitAccount, version)`
- Produces:
  - Nút **"✏️ Sửa & Chạy Version Mới"** trong modal Inspector.
  - Nút **"Sửa & Chạy"** trong bảng Notebooks.
  - Tự động nạp đúng code snapshot và inputs của version đang chọn, cập nhật thông báo badge *"Kế thừa từ Version N ➔ Sẽ tạo Version N+1"*, tự cuộn đến vùng Studio.

- [ ] **Step 1: Write test in `test_versioning_actions.py`**
  Kiểm tra:
  - Nút `btnInspectorEditVersion` xuất hiện trong header của `inspectorModal`.
  - Nút `btnEditNotebookRow` xuất hiện trong `renderKernelsTab`.
  - Hàm `editNotebookFromInspector` đọc đúng `currentInspectedKernel.version` và gọi sang Trainer Hub.

- [ ] **Step 2: Run test to verify it fails**
  Run: `./.venv/bin/python3 -m unittest test_versioning_actions.py -v`
  Expected: FAIL

- [ ] **Step 3: Implement 1-click Versioning actions in `static/index.html`**
  - Trong `inspectorModal` (header): Thêm nút:
    `<button id="btnInspectorEditVersion" onclick="editNotebookFromInspector()" class="px-2.5 py-1.5 text-purple-700 bg-purple-50 border border-purple-200 hover:bg-purple-100 rounded-lg text-xs flex items-center gap-1 font-semibold shadow-sm cursor-pointer" title="Chỉnh sửa mã nguồn và tạo phiên bản tiếp theo (Version 2) từ phiên bản này"><i data-lucide="edit-3" class="w-3.5 h-3.5 text-purple-600"></i><span>Sửa & Chạy Version Mới</span></button>`
  - Trong `renderKernelsTab()`: Thêm nút tương tự trên mỗi hàng notebook:
    `<button onclick="quickEditNotebook('${k.account}', '${k.ref}')" class="px-2 py-1 text-xs font-semibold text-indigo-700 bg-indigo-50 hover:bg-indigo-100 border border-indigo-200 rounded-md shadow-xs inline-flex items-center gap-1 cursor-pointer" title="Chỉnh sửa code và tạo phiên bản mới"><i data-lucide="edit-3" class="w-3 h-3 text-indigo-600"></i> Sửa code</button>`
  - Cài đặt `editNotebookFromInspector()`:
    1. Đóng modal Inspector (`closeInspectorModal()`).
    2. Lấy version hiện tại từ `inspectorVersionSelect.value` (nếu có).
    3. Chuyển tab sang `trainer` (`switchTab('trainer')`).
    4. Gọi `onTrainerNotebookSelectChange(ref, account, version)`.
    5. Đặt banner thông báo: *"Đang chỉnh sửa từ Version [v] của @account/ref. Bấm Run sẽ tạo Version mới trên Kaggle."*
    6. Mở rộng phần Studio và cuộn màn hình đến Studio.
  - Cập nhật `onTrainerNotebookSelectChange(ref, explicitAccount, version)`: Nhận thêm `version` và truyền lên URL `/api/runner/notebook-meta?account=...&ref=...&version=...`.

- [ ] **Step 4: Run test to verify it passes**
  Run: `./.venv/bin/python3 -m unittest test_versioning_actions.py -v`
  Expected: PASS

- [ ] **Step 5: Commit changes**
  ```bash
  git add static/index.html test_versioning_actions.py
  git commit -m "feat(workflow): add 1-click edit and create next version actions from inspector and table"
  ```

---

### Task 5: End-to-End Verification & Health Check

**Files:**
- Test: All test suites (`test_notebook_editor_backend.py`, `test_notebook_editor_frontend.py`, `test_notebook_inputs.py`, `test_versioning_actions.py`, `test_dataset_features.py`, `test_live_console_runner.py`).

- [ ] **Step 1: Run all test suites across the project**
  Run: `./.venv/bin/python3 -m unittest discover -p "test_*.py" -v`
  Expected: All tests PASS with zero regressions.

- [ ] **Step 2: Verify FastAPI backend startup and OpenAPI schema**
  Run test script to verify app boots cleanly and OpenAPI schema has `model_sources` and `version` parameter in docs.

- [ ] **Step 3: Verification commit**
  ```bash
  git status
  git commit --allow-empty -m "chore(test): verify all notebook studio and versioning tests pass cleanly"
  ```
