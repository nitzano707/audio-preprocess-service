import os, shutil, subprocess, uuid, threading, time
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

UPLOAD_DIR = os.environ.get("UPLOAD_DIR", "uploads")
BASE_URL = os.environ.get("BASE_URL", "https://audio-preprocess-service.onrender.com")
MAX_MB = int(os.environ.get("MAX_MB", "25"))
AUTO_DELETE_AFTER_SEC = int(os.environ.get("AUTO_DELETE_AFTER_SEC", "3600"))
os.makedirs(UPLOAD_DIR, exist_ok=True)

app = FastAPI(title="Audio Split-Compress-Merge Service")

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
    """דחיסה מהירה ל-OGG"""
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", in_path,
        "-ac", "1", "-ar", "16000",
        "-b:a", "32k", "-c:a", "libopus",
        out_path
    ]
    subprocess.run(cmd, check=True, timeout=20)

def ffmpeg_split_by_time(in_path: str, out_dir: str, segment_seconds: int = 300):
    """פיצול לפי זמן – כל קטע 5 דקות"""
    os.makedirs(out_dir, exist_ok=True)
    pattern = os.path.join(out_dir, "part_%03d.mp3")
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", in_path,
        "-f", "segment",
        "-segment_time", str(segment_seconds),
        "-c", "copy",
        pattern
    ]
    subprocess.run(cmd, check=True, timeout=60)
    return [os.path.join(out_dir, f) for f in sorted(os.listdir(out_dir)) if f.endswith(".mp3")]

def merge_ogg_files(file_list, output_path):
    """מיזוג קבצי OGG לקובץ אחד"""
    list_path = output_path + ".txt"
    with open(list_path, "w", encoding="utf-8") as f:
        for p in file_list:
            f.write(f"file '{p}'\n")
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "concat", "-safe", "0",
        "-i", list_path,
        "-c", "copy",
        output_path
    ]
    subprocess.run(cmd, check=True, timeout=60)
    os.remove(list_path)

# ───────────────────────────────
@app.get("/health")
def health():
    return {"ok": True, "message": "Service running with merge"}

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
                "processing_time_sec": round(time.time() - start, 2)
            }

        # פיצול לפי זמן ודחיסה לכל חלק
        parts_dir = os.path.join(work_dir, "parts")
        parts = ffmpeg_split_by_time(in_path, parts_dir, segment_seconds=300)
        ogg_parts = []

        for p in parts:
            out_p = p.replace(".mp3", ".ogg")
            try:
                fast_compress(p, out_p)
                ogg_parts.append(out_p)
            except Exception:
                pass

        # מיזוג כל קבצי ה-OGG לקובץ סופי אחד
        final_path = os.path.join(work_dir, "merged_final.ogg")
        merge_ogg_files(ogg_parts, final_path)

        delete_later([work_dir])
        return {
            "ok": True,
            "mode": "split_compressed_merged",
            "final_url": public_url_for(final_path),
            "parts_count": len(ogg_parts),
            "processing_time_sec": round(time.time() - start, 2)
        }

    except Exception as e:
        delete_later([work_dir], 5)
        raise HTTPException(status_code=500, detail=str(e))
