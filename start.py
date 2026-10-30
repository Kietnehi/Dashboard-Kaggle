import os
import sys
import time
import webbrowser
import threading
import uvicorn

def open_browser():
    time.sleep(1.2)
    url = "http://localhost:8000"
    try:
        import sys, subprocess
        if sys.platform.startswith("linux"):
            candidates = ["/opt/google/chrome/chrome", "/usr/bin/google-chrome", "/usr/bin/google-chrome-stable"]
            chrome_bin = next((c for c in candidates if os.path.exists(c)), None)
            if chrome_bin:
                subprocess.Popen([chrome_bin, "--profile-directory=Default", url])
                return
        elif os.name == "nt":
            candidates = [
                os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
                os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
                os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
            ]
            chrome_exe = next((c for c in candidates if os.path.exists(c)), None)
            if chrome_exe:
                subprocess.Popen([chrome_exe, "--profile-directory=Default", url])
                return
    except Exception:
        pass
    webbrowser.open(url)

if __name__ == "__main__":
    print("=======================================================")
    print("           KAGGLE MULTI-ACCOUNT MONITOR HUB            ")
    print("=======================================================")
    print("Khoi dong may chu tai: http://localhost:8000")
    print("Dang tu dong mo trinh duyet...")
    print("Nhan Ctrl+C de dung server.\n")

    threading.Thread(target=open_browser, daemon=True).start()
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
