import os, shutil, subprocess, uuid, threading, time, math
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

UPLOAD_DIR = os.environ.get("UPLOAD_DIR", "uploads")
BASE_URL = os.environ.get("BASE_URL", "https://audio-preprocess-service.onrender.com")
MAX_MB = int(os.environ.get("MAX_MB", "25"))
AUTO_DELETE_AFTER_SEC = int(os.environ.get("AUTO_DELETE_AFTER_SEC", "3600"))
os.makedirs(UPLOAD_DIR, exist_ok=True)

app = FastAPI(title="Ultra-Fast Audio Split & Compress")

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

# ───────────────────────────────
def fast_compress(in_path: str, out_path: str):
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", in_path,
        "-ac", "1", "-ar", "16000",
        "-b:a", "32k", "-c:a", "libopus",
        out_path
    ]
    subprocess.run(cmd, check=True, timeout=20)

def fast_split(in_path: str, out_dir: str, target_size_mb: int = 20):
    """פיצול לפי משקל בלבד – בלי ffprobe, מהיר במיוחד"""
    os.makedirs(out_dir, exist_ok=True)
    chunk_size = target_size_mb * 1024 * 1024
    with open(in_path, "rb") as f:
        i, chunk = 0, f.read(chunk_size)
        while chunk:
            part_path = os.path.join(out_dir, f"part_{i:03d}.mp3")
            with open(part_path, "wb") as out:
                out.write(chunk)
            i += 1
            chunk = f.read(chunk_size)
    return [os.path.join(out_dir, f) for f in sorted(os.listdir(out_dir))]

# ───────────────────────────────
@app.get("/health")
def health():
    return {"ok": True, "message": "Service running ultra-fast"}

@app.get("/files/{subpath:path}")
def serve_file(subpath: str):
    full = os.path.join(UPLOAD_DIR, subpath)
    if not os.path.exists(full):
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(full, media_type="audio/ogg")

# ───────────────────────────────
@app.post("/process")
async def process_audio(file: UploadFile = File(...), max_mb: int = MAX_MB):
    start = time.time()
    uid = uuid.uuid4().hex
    work_dir = os.path.join(UPLOAD_DIR, uid)
    os.makedirs(work_dir, exist_ok=True)
    in_path = os.path.join(work_dir, file.filename)

    with open(in_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    try:
        size_mb = os.path.getsize(in_path) / (1024 * 1024)
        print(f"[INFO] File size: {size_mb:.2f} MB")

        # אם קטן מ-25MB → דחיסה רגילה
        if size_mb <= max_mb:
            out_path = os.path.join(work_dir, "compressed.ogg")
            fast_compress(in_path, out_path)
            delete_later([work_dir])
            return {
                "ok": True,
                "mode": "compressed",
                "url": public_url_for(out_path),
                "size_mb": round(os.path.getsize(out_path) / (1024 * 1024), 2),
                "processing_time_sec": round(time.time() - start, 2)
            }

        # אם גדול מדי → פיצול למקטעים ודחיסה לכל חלק
        parts_dir = os.path.join(work_dir, "parts")
        parts = fast_split(in_path, parts_dir, target_size_mb=20)
        urls = []

        for p in parts:
            out_p = p.replace(".mp3", ".ogg")
            try:
                fast_compress(p, out_p)
                urls.append(public_url_for(out_p))
            except Exception:
                urls.append(public_url_for(p))  # fallback

        delete_later([work_dir])
        return {
            "ok": True,
            "mode": "split_compressed",
            "count": len(urls),
            "urls": urls,
            "processing_time_sec": round(time.time() - start, 2)
        }

    except Exception as e:
        delete_later([work_dir], 5)
        raise HTTPException(status_code=500, detail=str(e))
