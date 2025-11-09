import os, shutil, subprocess, uuid, threading, time, asyncio, json
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

# ───────────────────────────────────────────────
UPLOAD_DIR = os.environ.get("UPLOAD_DIR", "uploads")
BASE_URL = os.environ.get("BASE_URL", "https://audio-preprocess-service.onrender.com")
MAX_MB = int(os.environ.get("MAX_MB", "25"))
AUTO_DELETE_AFTER_SEC = int(os.environ.get("AUTO_DELETE_AFTER_SEC", "3600"))
os.makedirs(UPLOAD_DIR, exist_ok=True)

app = FastAPI(title="Live Audio Split Streamer")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ───────────────────────────────────────────────
def delete_later(paths, delay=AUTO_DELETE_AFTER_SEC):
    """ניקוי קבצים אחרי זמן"""
    def _worker():
        time.sleep(delay)
        for p in paths:
            if os.path.isdir(p):
                shutil.rmtree(p, ignore_errors=True)
            elif os.path.exists(p):
                os.remove(p)
        print(f"[Auto Delete] cleaned {len(paths)} items")
    threading.Thread(target=_worker, daemon=True).start()

def public_url_for(path: str) -> str:
    rel = os.path.relpath(path, start=UPLOAD_DIR).replace("\\", "/")
    return f"{BASE_URL}/files/{rel}"

def run_ffmpeg(cmd, timeout=90):
    """הרצת ffmpeg"""
    subprocess.run(cmd, check=True, timeout=timeout)

def get_duration(in_path: str) -> float:
    """שליפת משך האודיו"""
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", in_path],
        capture_output=True, text=True
    )
    try:
        return float(probe.stdout.strip())
    except:
        return 0.0

# ───────────────────────────────────────────────
def split_audio(in_path: str, out_dir: str, max_mb: int = MAX_MB):
    """מפצל קובץ גדול לחלקים לפי גודל"""
    os.makedirs(out_dir, exist_ok=True)
    total_size = os.path.getsize(in_path) / (1024 * 1024)
    if total_size <= max_mb:
        return [in_path]

    duration = get_duration(in_path)
    if duration == 0:
        raise HTTPException(status_code=400, detail="Cannot read duration")

    parts_count = int(total_size // max_mb) + 1
    part_dur = duration / parts_count
    part_files = []

    for i in range(parts_count):
        start = i * part_dur
        out_p = os.path.join(out_dir, f"part_{i:03d}.ogg")
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", in_path,
            "-ss", str(start),
            "-t", str(part_dur),
            "-c", "copy", out_p
        ]
        run_ffmpeg(cmd)
        part_files.append(out_p)
    return part_files

# ───────────────────────────────────────────────
@app.get("/health")
def health():
    return {"ok": True, "message": "Streaming audio splitter ready"}

@app.get("/files/{subpath:path}")
def serve_file(subpath: str):
    full = os.path.join(UPLOAD_DIR, subpath)
    if not os.path.exists(full):
        raise HTTPException(status_code=404, detail="File not found")
    from fastapi.responses import FileResponse
    return FileResponse(full, media_type="audio/ogg")

@app.post("/process_stream")
async def process_stream(file: UploadFile = File(...)):
    """
    פיצול קובץ גדול ושליחה מיידית של כתובת כל חלק
    בצורה זורמת (StreamingResponse) – נצפה בזמן אמת בפוסטמן.
    """
    uid = uuid.uuid4().hex
    work_dir = os.path.join(UPLOAD_DIR, uid)
    os.makedirs(work_dir, exist_ok=True)
    in_path = os.path.join(work_dir, file.filename)
    with open(in_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    async def event_generator():
        start = time.time()
        size_mb = os.path.getsize(in_path) / (1024 * 1024)
        yield f"data: {json.dumps({'status': 'uploaded', 'size_mb': round(size_mb,2)})}\n\n"

        # אם קטן מהמגבלה
        if size_mb <= MAX_MB:
            url = public_url_for(in_path)
            yield f"data: {json.dumps({'part': 0, 'url': url, 'done': True})}\n\n"
            delete_later([work_dir])
            return

        # פיצול בזמן אמת
        parts_dir = os.path.join(work_dir, "parts")
        total_parts = 0
        for i, part in enumerate(split_audio(in_path, parts_dir, MAX_MB)):
            url = public_url_for(part)
            total_parts += 1
            yield f"data: {json.dumps({'part': i, 'url': url, 'done': False})}\n\n"
            await asyncio.sleep(0.2)

        yield f"data: {json.dumps({'done': True, 'parts_count': total_parts, 'processing_time_sec': round(time.time()-start,2)})}\n\n"
        delete_later([work_dir])

    return StreamingResponse(event_generator(), media_type="text/event-stream")
