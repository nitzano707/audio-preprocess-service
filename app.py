import os, math, shutil, subprocess, uuid, threading, time
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

# ───────────────────────────────
UPLOAD_DIR = os.environ.get("UPLOAD_DIR", "uploads")
BASE_URL   = os.environ.get("BASE_URL", "https://your-service.onrender.com")
MAX_MB     = int(os.environ.get("MAX_MB", "25"))
AUTO_DELETE_AFTER_SEC = int(os.environ.get("AUTO_DELETE_AFTER_SEC", str(60*60)))
os.makedirs(UPLOAD_DIR, exist_ok=True)
# ───────────────────────────────

app = FastAPI(title="Audio Preprocessor & OGG Splitter")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ───────────────────────────────
# פונקציות עזר
# ───────────────────────────────
def _run(cmd):
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except subprocess.CalledProcessError as e:
        raise RuntimeError(e.stderr.decode(errors="ignore")[:2000])

def ffprobe_duration_seconds(path: str) -> float:
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
    def _worker():
        time.sleep(delay)
        for p in paths:
            try:
                if os.path.isdir(p): shutil.rmtree(p, ignore_errors=True)
                elif os.path.exists(p): os.remove(p)
            except: pass
    threading.Thread(target=_worker, daemon=True).start()

def public_url_for(path: str) -> str:
    rel = os.path.relpath(path, start=UPLOAD_DIR).replace("\\", "/")
    return f"{BASE_URL}/files/{rel}"

# ───────────────────────────────
# שלב 1: טיפול מקדים (דחיסה + הסרת שקטים + נירמול)
# ───────────────────────────────
def preprocess_audio(in_path: str, out_path: str):
    cmd = [
        "ffmpeg", "-y", "-i", in_path,
        "-ar", "16000", "-ac", "1",
        "-b:a", "24k", "-c:a", "libopus",
        "-af", "silenceremove=stop_periods=-1:stop_threshold=-30dB,loudnorm",
        out_path
    ]
    _run(cmd)

# ───────────────────────────────
# שלב 2: פילוח לחלקים במקרה של קובץ גדול מדי
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
        files = sorted(os.listdir(full))
        urls = [f"{BASE_URL}/files/{subpath}/{f}" for f in files]
        return {"folder": subpath, "files": urls}
    return FileResponse(full, media_type="audio/ogg")

# ───────────────────────────────
# הנתיב הראשי
# ───────────────────────────────
@app.post("/process")
async def process_audio(file: UploadFile = File(...), max_mb: int = MAX_MB):
    uid = uuid.uuid4().hex
    work_dir = os.path.join(UPLOAD_DIR, uid)
    os.makedirs(work_dir, exist_ok=True)
    in_path = os.path.join(work_dir, file.filename)
    out_path = os.path.join(work_dir, "processed.ogg")
    max_bytes = max_mb * 1024 * 1024

    try:
        # שמירת הקובץ המקורי
        with open(in_path, "wb") as f:
            shutil.copyfileobj(file.file, f)

        # טיפול מקדים ודחיסה
        preprocess_audio(in_path, out_path)
        out_size = os.path.getsize(out_path)

        # אם נכנס בגודל - קובץ יחיד
        if out_size <= max_bytes:
            url = public_url_for(out_path)
            delete_later([work_dir])
            return {
                "ok": True,
                "mode": "single",
                "url": url,
                "size_bytes": out_size,
                "size_human": human_size(out_size),
                "expires_in_sec": AUTO_DELETE_AFTER_SEC
            }

        # אחרת - פיצול
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
            "expires_in_sec": AUTO_DELETE_AFTER_SEC
        }

    except Exception as e:
        delete_later([work_dir], 5)
        raise HTTPException(status_code=500, detail=str(e))
