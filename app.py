import os
import re
import shutil
import tempfile

import yt_dlp
from flask import Flask, jsonify, request, send_file

app = Flask(__name__, static_folder="static", static_url_path="")


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
        opts = {"quiet": True, "noplaylist": True, "skip_download": True}
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


@app.get("/api/download")
def download():
    try:
        url = check_url(request.args.get("url"))
    except ValueError as e:
        return jsonify(error=str(e)), 400

    mode = request.args.get("mode", "video")
    height = request.args.get("height", "")
    tmp = tempfile.mkdtemp(prefix="ytdl_")
    opts = {
        "quiet": True,
        "noplaylist": True,
        "outtmpl": os.path.join(tmp, "%(title).150B.%(ext)s"),
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
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
        path = os.path.join(tmp, os.listdir(tmp)[0])
    except Exception as e:
        shutil.rmtree(tmp, ignore_errors=True)
        return jsonify(error=short_error(e)), 400

    resp = send_file(path, as_attachment=True)
    resp.call_on_close(lambda: shutil.rmtree(tmp, ignore_errors=True))
    return resp


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
