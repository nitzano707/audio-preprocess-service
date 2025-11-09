import os, shutil, subprocess, uuid, threading, time
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

UPLOAD_DIR = os.environ.get("UPLOAD_DIR", "uploads")
BASE_URL = os.environ.get("BASE_URL", "https://audio-preprocess-service.onrender.com")
MAX_MB = int(os.environ.get("MAX_MB", "25"))
AUTO_DELETE_AFTER_SEC = int(os.environ.get("AUTO_DELETE_AFTER_SEC", "3600"))
os.makedirs(UPLOAD_DIR, exist_ok=True)

app = FastAPI(title="Fast Audio Preprocessor")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ───────────────────────────────
def delete_later(paths, delay=AUTO_DELETE_AFTER_SEC):
    def _worker():
        time.sleep(delay)
        for p in paths:
            try:
                if os.path.isdir(p):
                    shutil.rmtree(p, ignore_errors=True)
                elif os.path.exists(p):
                    os.remove(p)
            except:
                pass
        print(f"[Auto Delete] cleaned {len(paths)} items")
    threading.Thread(target=_worker, daemon=True).start()

def public_url_for(path: str) -> str:
    rel = os.path.relpath(path, start=UPLOAD_DIR).replace("\\", "/")
    return f"{BASE_URL}/files/{rel}"

def fast_compress(in_path: str, out_path: str):
    """מהדק קובץ במהירות ל-OGG/Opus עם הגבלת זמן קצרה"""
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", in_path,
        "-ac", "1", "-ar", "16000",
        "-b:a", "32k", "-c:a", "libopus",
        out_path
    ]
    print(f"[FFMPEG] Compressing {os.path.basename(in_path)}")
    try:
        subprocess.run(cmd, check=True, timeout=20)
    except subprocess.TimeoutExpired:
        raise TimeoutError("ffmpeg timeout after 20s")
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"ffmpeg failed ({e})")

# ───────────────────────────────
@app.get("/health")
def health():
    return {"ok": True, "message": "Service running fast"}

@app.get("/files/{subpath:path}")
def serve_file(subpath: str):
    full = os.path.join(UPLOAD_DIR, subpath)
    if not os.path.exists(full):
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(full, media_type="audio/ogg")

@app.post("/process")
async def process_audio(file: UploadFile = File(...), max_mb: int = MAX_MB):
    start = time.time()
    uid = uuid.uuid4().hex
    work_dir = os.path.join(UPLOAD_DIR, uid)
    os.makedirs(work_dir, exist_ok=True)
    in_path = os.path.join(work_dir, file.filename)
    out_path = os.path.join(work_dir, "processed.ogg")

    try:
        with open(in_path, "wb") as f:
            shutil.copyfileobj(file.file, f)

        # אם הקובץ כבר קטן מדי – נחזיר אותו ישירות
        size_mb = os.path.getsize(in_path) / (1024 * 1024)
        if size_mb <= max_mb:
            print("[FAST] File small enough, skipping heavy conversion.")
            url = public_url_for(in_path)
            delete_later([work_dir])
            return {
                "ok": True,
                "mode": "original",
                "url": url,
                "size_mb": round(size_mb, 2),
                "processing_time_sec": round(time.time() - start, 2)
            }

        # דחיסה מהירה
        fast_compress(in_path, out_path)
        out_size = os.path.getsize(out_path)
        url = public_url_for(out_path)
        delete_later([work_dir])

        return {
            "ok": True,
            "mode": "compressed",
            "url": url,
            "size_mb": round(out_size / (1024 * 1024), 2),
            "processing_time_sec": round(time.time() - start, 2)
        }

    except TimeoutError as e:
        print("[TIMEOUT] Compression exceeded 20s, returning original file.")
        url = public_url_for(in_path)
        delete_later([work_dir])
        return {
            "ok": False,
            "error": str(e),
            "fallback_url": url,
            "processing_time_sec": round(time.time() - start, 2)
        }

    except Exception as e:
        delete_later([work_dir], 5)
        raise HTTPException(status_code=500, detail=str(e))
