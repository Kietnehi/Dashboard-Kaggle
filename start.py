import os
import sys
import time
import webbrowser
import threading
import uvicorn

def open_browser():
    time.sleep(1.2)
    webbrowser.open("http://localhost:8000")

if __name__ == "__main__":
    print("=======================================================")
    print("           KAGGLE MULTI-ACCOUNT MONITOR HUB            ")
    print("=======================================================")
    print("Khoi dong may chu tai: http://localhost:8000")
    print("Dang tu dong mo trinh duyet...")
    print("Nhan Ctrl+C de dung server.\n")

    threading.Thread(target=open_browser, daemon=True).start()
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=False)
