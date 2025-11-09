import os, shutil, subprocess, uuid, threading, time
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

# ───────────────────────────────────────────────
UPLOAD_DIR = os.environ.get("UPLOAD_DIR", "uploads")
BASE_URL = os.environ.get("BASE_URL", "https://audio-preprocess-service.onrender.com")
MAX_MB = int(os.environ.get("MAX_MB", "25"))
AUTO_DELETE_AFTER_SEC = int(os.environ.get("AUTO_DELETE_AFTER_SEC", "3600"))
os.makedirs(UPLOAD_DIR, exist_ok=True)

app = FastAPI(title="Universal Audio Processor")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ───────────────────────────────────────────────
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


def run_ffmpeg(cmd, timeout=60):
    """הרצה בטוחה של ffmpeg"""
    print("[CMD]", " ".join(cmd))
    subprocess.run(cmd, check=True, timeout=timeout)


def convert_to_wav(in_path: str, out_path: str):
    """המרה לכל קובץ WAV תקני"""
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", in_path,
        "-ar", "16000", "-ac", "1", "-vn", out_path
    ]
    run_ffmpeg(cmd, timeout=60)


def split_audio(in_path: str, out_dir: str, segment_seconds: int = 300):
    """פיצול לפי זמן – תומך בכל פורמט"""
    os.makedirs(out_dir, exist_ok=True)
    pattern = os.path.join(out_dir, "part_%03d.wav")
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", in_path,
        "-f", "segment",
        "-segment_time", str(segment_seconds),
        "-ac", "1", "-ar", "16000",
        "-acodec", "pcm_s16le",
        pattern
    ]
    run_ffmpeg(cmd, timeout=90)
    return [os.path.join(out_dir, f) for f in sorted(os.listdir(out_dir)) if f.endswith(".wav")]


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
    run_ffmpeg(cmd, timeout=60)
    os.remove(list_path)


def compress_to_ogg(in_path: str, out_path: str):
    """דחיסה חכמה – מתאימה איכות וזמן לפי גודל הקובץ"""
    size_mb = os.path.getsize(in_path) / (1024 * 1024)

    # קביעת איכות דינמית
    if size_mb < 10:
        bitrate = "48k"
    elif size_mb < 30:
        bitrate = "32k"
    else:
        bitrate = "24k"  # לקבצים כבדים במיוחד

    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", in_path,
        "-ac", "1", "-ar", "16000",
        "-b:a", bitrate, "-c:a", "libopus",
        out_path
    ]

    try:
        subprocess.run(cmd, check=True, timeout=40)
    except subprocess.TimeoutExpired:
        print(f"[WARN] Compress timed out on {size_mb:.1f} MB, splitting...")
        tmp1 = out_path + ".part1.ogg"
        tmp2 = out_path + ".part2.ogg"

        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", in_path],
            capture_output=True, text=True
        )
        duration = float(probe.stdout.strip() or 0)
        mid = duration / 2

        subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                        "-i", in_path, "-t", str(mid),
                        "-ac", "1", "-ar", "16000", "-b:a", bitrate, "-c:a", "libopus", tmp1],
                       check=True, timeout=40)
        subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                        "-i", in_path, "-ss", str(mid),
                        "-ac", "1", "-ar", "16000", "-b:a", bitrate, "-c:a", "libopus", tmp2],
                       check=True, timeout=40)

        merge_ogg_files([tmp1, tmp2], out_path)
        os.remove(tmp1)
        os.remove(tmp2)

# ───────────────────────────────────────────────
@app.get("/health")
def health():
    return {"ok": True, "message": "Universal audio processor ready"}


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

    with open(in_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    try:
        # שלב 1: המרה ל-WAV תקני
        wav_path = os.path.join(work_dir, "converted.wav")
        convert_to_wav(in_path, wav_path)

        size_mb = os.path.getsize(wav_path) / (1024 * 1024)
        print(f"[INFO] normalized WAV size = {size_mb:.2f} MB")

        # שלב 2: פיצול ודחיסה
        if size_mb > max_mb:
            parts_dir = os.path.join(work_dir, "parts")
            parts = split_audio(wav_path, parts_dir, segment_seconds=300)
            ogg_parts = []
            for p in parts:
                out_p = p.replace(".wav", ".ogg")
                compress_to_ogg(p, out_p)
                ogg_parts.append(out_p)

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

        # שלב 3: דחיסה רגילה
        out_path = os.path.join(work_dir, "compressed.ogg")
        compress_to_ogg(wav_path, out_path)
        delete_later([work_dir])

        return {
            "ok": True,
            "mode": "compressed",
            "url": public_url_for(out_path),
            "size_mb": round(os.path.getsize(out_path) / (1024 * 1024), 2),
            "processing_time_sec": round(time.time() - start, 2)
        }

    except subprocess.CalledProcessError as e:
        raise HTTPException(status_code=500, detail=f"ffmpeg failed: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
