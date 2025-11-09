import os, math, shutil, subprocess, uuid, threading, time
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

# ───────────────────────────────
UPLOAD_DIR = os.environ.get("UPLOAD_DIR", "uploads")
BASE_URL   = os.environ.get("BASE_URL", "https://audio-preprocess-service.onrender.com")
MAX_MB     = int(os.environ.get("MAX_MB", "25"))
AUTO_DELETE_AFTER_SEC = int(os.environ.get("AUTO_DELETE_AFTER_SEC", str(60*60)))
os.makedirs(UPLOAD_DIR, exist_ok=True)
# ───────────────────────────────

app = FastAPI(title="Audio Preprocess & Compression Service")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ───────────────────────────────
# עזר
# ───────────────────────────────
def _run(cmd, timeout=120):
    """מריץ פקודת ffmpeg עם הגבלת זמן ולוגים ברורים."""
    print(f"[FFMPEG] Running: {' '.join(cmd)}")
    start = time.time()
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, timeout=timeout)
        print(f"[FFMPEG] Completed in {time.time() - start:.1f}s")
        return proc
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"ffmpeg timeout after {timeout}s")
    except subprocess.CalledProcessError as e:
        msg = e.stderr.decode(errors="ignore")[-500:]
        raise RuntimeError(f"ffmpeg error: {msg}")

def ffprobe_duration_seconds(path: str) -> float:
    """בודק משך כולל של קובץ האודיו."""
    try:
        out = subprocess.check_output([
            "ffprobe","-v","error","-show_entries","format=duration",
            "-of","default=noprint_wrappers=1:nokey=1", path
        ]).decode().strip()
        return float(out) if out else 0.0
    except Exception:
        return 0.0

def human_size(bytes_: int) -> str:
    for u in ["B","KB","MB","GB"]:
        if bytes_ < 1024 or u == "GB":
            return f"{bytes_:.1f}{u}"
        bytes_ /= 1024

def delete_later(paths, delay=AUTO_DELETE_AFTER_SEC):
    """מוחק קבצים אחרי זמן קצוב"""
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

# ───────────────────────────────
# טיפול מקדים ודחיסה
# ───────────────────────────────
def preprocess_audio(in_path: str, out_path: str):
    """דחיסה בסיסית ל-OGG Mono 16kHz."""
    cmd = [
        "ffmpeg", "-y", "-i", in_path,
        "-ar", "16000", "-ac", "1",
        "-b:a", "24k", "-c:a", "libopus",
        out_path
    ]
    _run(cmd)

# ───────────────────────────────
# פיצול קובץ גדול מדי
# ───────────────────────────────
def split_ogg(in_path: str, out_dir: str, max_bytes: int):
    os.makedirs(out_dir, exist_ok=True)
    size = os.path.getsize(in_path)
    duration = max(1.0, ffprobe_duration_seconds(in_path))
    parts = max(2, math.ceil(size / max_bytes))
    seg_time = max(1, int(math.ceil(duration / parts)))

    pattern = os.path.join(out_dir, "part_%03d.ogg")
    _run([
        "ffmpeg","-y","-i",in_path,"-vn","-c:a","copy",
        "-f","segment","-segment_time",str(seg_time),
        "-reset_timestamps","1",pattern
    ])
    return sorted([os.path.join(out_dir,p) for p in os.listdir(out_dir) if p.endswith(".ogg")])

# ───────────────────────────────
# נתיבים
# ───────────────────────────────
@app.get("/health")
def health():
    return {"ok": True, "message": "Service running"}

@app.get("/files/{subpath:path}")
def serve_file(subpath: str):
    full = os.path.join(UPLOAD_DIR, subpath)
    if not os.path.exists(full):
        raise HTTPException(status_code=404, detail="File not found")
    if os.path.isdir(full):
        urls = [f"{BASE_URL}/files/{subpath}/{f}" for f in sorted(os.listdir(full))]
        return {"folder": subpath, "files": urls}
    return FileResponse(full, media_type="audio/ogg")

# ───────────────────────────────
# המסלול הראשי
# ───────────────────────────────
@app.post("/process")
async def process_audio(file: UploadFile = File(...), max_mb: int = MAX_MB):
    start = time.time()
    uid = uuid.uuid4().hex
    work_dir = os.path.join(UPLOAD_DIR, uid)
    os.makedirs(work_dir, exist_ok=True)

    in_path = os.path.join(work_dir, file.filename)
    out_path = os.path.join(work_dir, "processed.ogg")
    max_bytes = max_mb * 1024 * 1024

    try:
        with open(in_path, "wb") as f:
            shutil.copyfileobj(file.file, f)

        preprocess_audio(in_path, out_path)
        out_size = os.path.getsize(out_path)

        if out_size <= max_bytes:
            url = public_url_for(out_path)
            delete_later([work_dir])
            return {
                "ok": True,
                "mode": "single",
                "url": url,
                "size_bytes": out_size,
                "size_human": human_size(out_size),
                "processing_time_sec": round(time.time() - start, 2)
            }

        parts_dir = os.path.join(work_dir, "parts")
        parts = split_ogg(out_path, parts_dir, max_bytes)
        folder_url = public_url_for(parts_dir)
        urls = [public_url_for(p) for p in parts]

        delete_later([work_dir])
        return {
            "ok": True,
            "mode": "split",
            "folder_url": folder_url,
            "count": len(urls),
            "parts": urls,
            "processing_time_sec": round(time.time() - start, 2)
        }

    except Exception as e:
        delete_later([work_dir], 5)
        raise HTTPException(status_code=500, detail=str(e))
