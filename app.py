import os
import re
import shutil
import tempfile
import threading
import time
import uuid

import yt_dlp
from flask import Flask, jsonify, request, send_file

app = Flask(__name__, static_folder="static", static_url_path="")

# Put a Netscape-format cookies.txt next to this file to pass YouTube's bot check.
COOKIES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookies.txt")

JOBS = {}
LOCK = threading.Lock()


def with_cookies(opts):
    if os.path.isfile(COOKIES):
        opts["cookiefile"] = COOKIES
    return opts


@app.get("/")
def index():
    return app.send_static_file("index.html")


def check_url(url):
    url = (url or "").strip()
    if not re.match(r"^https?://", url):
        raise ValueError("Enter a valid link starting with http or https.")
    return url


def short_error(e):
    return re.sub(r"\x1b\[[0-9;]*m", "", str(e)).replace("ERROR: ", "").strip()


@app.post("/api/info")
def info():
    try:
        url = check_url((request.get_json(silent=True) or {}).get("url"))
        opts = with_cookies({"quiet": True, "noplaylist": True, "skip_download": True})
        with yt_dlp.YoutubeDL(opts) as ydl:
            d = ydl.extract_info(url, download=False)
    except Exception as e:
        return jsonify(error=short_error(e)), 400

    heights = sorted(
        {
            f["height"]
            for f in d.get("formats", [])
            if f.get("height") and f.get("vcodec") not in (None, "none")
        },
        reverse=True,
    )
    return jsonify(
        title=d.get("title"),
        thumbnail=d.get("thumbnail"),
        uploader=d.get("uploader"),
        duration=d.get("duration"),
        views=d.get("view_count"),
        heights=heights,
    )


def purge_old_jobs():
    now = time.time()
    with LOCK:
        for jid, j in list(JOBS.items()):
            if now - j["ts"] > 3600:
                shutil.rmtree(j["tmp"], ignore_errors=True)
                del JOBS[jid]


def run_job(jid, url, mode, height):
    job = JOBS[jid]

    def on_progress(d):
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes") or 0
            frac = done / total if total else 0
            parts = len(d.get("info_dict", {}).get("requested_formats") or []) or 1
            job["percent"] = max(job["percent"], min(98, (job["finished"] + frac) / parts * 100))
            job["stage"] = "downloading"
        elif d["status"] == "finished":
            job["finished"] += 1

    def on_post(d):
        if d["status"] == "started":
            job["stage"] = "processing"
            job["percent"] = max(job["percent"], 99)

    opts = {
        "quiet": True,
        "noplaylist": True,
        "outtmpl": os.path.join(job["tmp"], "%(title).150B.%(ext)s"),
        "progress_hooks": [on_progress],
        "postprocessor_hooks": [on_post],
    }

    if mode == "audio":
        opts.update(
            format="bestaudio/best",
            postprocessors=[
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }
            ],
        )
    else:
        h = int(height) if height.isdigit() else 4320
        opts.update(
            format=f"bv*[height<={h}]+ba/b[height<={h}]/b",
            format_sort=["res", "ext:mp4:m4a"],
            merge_output_format="mp4",
        )

    try:
        with yt_dlp.YoutubeDL(with_cookies(opts)) as ydl:
            ydl.download([url])
        files = [os.path.join(job["tmp"], f) for f in os.listdir(job["tmp"])]
        job["path"] = max(files, key=os.path.getsize)
        job["percent"] = 100
        job["status"] = "done"
    except Exception as e:
        job["error"] = short_error(e)
        job["status"] = "error"


@app.post("/api/start")
def start():
    purge_old_jobs()
    data = request.get_json(silent=True) or {}
    try:
        url = check_url(data.get("url"))
    except ValueError as e:
        return jsonify(error=str(e)), 400

    jid = uuid.uuid4().hex
    JOBS[jid] = {
        "status": "running",
        "stage": "starting",
        "percent": 0,
        "finished": 0,
        "error": None,
        "path": None,
        "tmp": tempfile.mkdtemp(prefix="ytdl_"),
        "ts": time.time(),
    }
    threading.Thread(
        target=run_job,
        args=(jid, url, data.get("mode", "video"), str(data.get("height") or "")),
        daemon=True,
    ).start()
    return jsonify(id=jid)


@app.get("/api/progress/<jid>")
def progress(jid):
    job = JOBS.get(jid)
    if not job:
        return jsonify(error="This download expired. Start it again."), 404
    return jsonify(
        status=job["status"],
        stage=job["stage"],
        percent=round(job["percent"], 1),
        error=job["error"],
    )


@app.get("/api/file/<jid>")
def file(jid):
    job = JOBS.get(jid)
    if not job or job["status"] != "done":
        return jsonify(error="File is not ready."), 404
    resp = send_file(job["path"], as_attachment=True)

    def cleanup():
        shutil.rmtree(job["tmp"], ignore_errors=True)
        JOBS.pop(jid, None)

    resp.call_on_close(cleanup)
    return resp


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)