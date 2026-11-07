"""
Local, AWS-free stand-in for the API + processor Lambdas (demo/dev only).

Serves frontend/ and emulates:
  POST /upload-url       -> fake presigned upload (points back at this server)
  POST /local-upload      -> receives the file, runs the REAL processor.app
                             logic (same Pillow code used in production)
  GET  /status/{jobId}   -> result, once processed
  GET  /health

No AWS, no network calls, no cost.

    pip install -r requirements-dev.txt
    python scripts/local_server.py
    -> open http://localhost:5000
"""

import json
import os
import re
import sys
import time
import uuid
from pathlib import Path

from flask import Flask, abort, jsonify, request, send_file

ROOT = Path(__file__).resolve().parent.parent
FRONTEND_DIR = ROOT / "frontend"
STORAGE_DIR = ROOT / "local_storage"
PROCESSED_DIR = STORAGE_DIR / "processed"
RESULTS_DIR = STORAGE_DIR / "results"
for d in (PROCESSED_DIR, RESULTS_DIR):
    d.mkdir(parents=True, exist_ok=True)

sys.path.insert(0, str(ROOT))
from processor.app import EXTENSIONS, parse_options, process_image  # noqa: E402

app = Flask(__name__, static_folder=None)

ALLOWED_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp"}
ALLOWED_SIZES = {"256", "512", "1024", "2048"}
ALLOWED_FORMATS = {"webp", "jpeg", "png"}
JOB_ID_RE = re.compile(r"^[a-f0-9]{32}$")


def safe_filename(name: str) -> str:
    name = os.path.basename(name or "image")
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    return name[-100:] or "image"


@app.get("/")
def index():
    return send_file(FRONTEND_DIR / "index.html")


@app.get("/config.js")
def config_js():
    # Override frontend/config.js (left blank for real AWS deploys) with this
    # server's own origin, so app.js doesn't show the "API not configured" banner.
    js = f'window.APP_CONFIG = {{ API_URL: "http://{request.host}" }};'
    return js, 200, {"Content-Type": "application/javascript"}


@app.get("/<path:filename>")
def static_files(filename):
    path = FRONTEND_DIR / filename
    if not path.is_file():
        abort(404)
    return send_file(path)


@app.get("/health")
def health():
    return jsonify({"ok": True, "mode": "local-no-aws"})


@app.post("/upload-url")
def upload_url():
    body = request.get_json(silent=True) or {}
    content_type = body.get("contentType", "")
    if content_type not in ALLOWED_CONTENT_TYPES:
        return jsonify({"error": "Only JPEG, PNG or WebP images are allowed"}), 400

    max_size = str(body.get("maxSize", "1024"))
    out_format = str(body.get("format", "webp")).lower()
    quality = str(body.get("quality", "75"))
    if max_size not in ALLOWED_SIZES or out_format not in ALLOWED_FORMATS or not quality.isdigit():
        return jsonify({"error": "Invalid processing options"}), 400

    job_id = uuid.uuid4().hex
    key = f"uploads/{job_id}/{safe_filename(body.get('filename'))}"

    return jsonify({
        "jobId": job_id,
        "key": key,
        "upload": {
            "url": "/local-upload",
            "fields": {
                "jobId": job_id,
                "max-size": max_size,
                "output-format": out_format,
                "quality": quality,
            },
        },
    })


@app.post("/local-upload")
def local_upload():
    # Mimics: browser -> S3 presigned POST -> S3 ObjectCreated event -> processor Lambda,
    # all collapsed into one request since there's no real S3 to fire an event.
    job_id = request.form.get("jobId", "")
    if not JOB_ID_RE.match(job_id):
        return jsonify({"error": "bad job id"}), 400
    file = request.files.get("file")
    if not file:
        return jsonify({"error": "no file"}), 400

    original = file.read()
    options = parse_options({
        "max-size": request.form.get("max-size", ""),
        "output-format": request.form.get("output-format", ""),
        "quality": request.form.get("quality", ""),
    })

    started = time.time()
    try:
        output, stats = process_image(original, options)
        fmt = options["format"]
        out_name = f"{job_id}.{EXTENSIONS[fmt]}"
        (PROCESSED_DIR / out_name).write_bytes(output)
        result = {
            "status": "done",
            "job_id": job_id,
            "output_key": f"processed/{out_name}",
            "original_bytes": len(original),
            "processed_bytes": len(output),
            "reduction_percent": round(100 * (1 - len(output) / max(len(original), 1)), 1),
            "output_format": fmt,
            "quality": options["quality"],
            "max_size": options["max_size"],
            "processing_ms": int((time.time() - started) * 1000),
            **stats,
        }
    except Exception as exc:  # noqa: BLE001 - report any failure to the user, like the real Lambda
        result = {"status": "error", "job_id": job_id, "error": str(exc)}

    (RESULTS_DIR / f"{job_id}.json").write_text(json.dumps(result))
    return "", 204


@app.get("/status/<job_id>")
def status(job_id):
    if not JOB_ID_RE.match(job_id or ""):
        return jsonify({"error": "Invalid job id"}), 400

    result_path = RESULTS_DIR / f"{job_id}.json"
    if not result_path.is_file():
        return jsonify({"jobId": job_id, "status": "pending"})

    result = json.loads(result_path.read_text())
    if result.get("status") == "done":
        out_name = result["output_key"].split("/", 1)[1]
        result["previewUrl"] = f"/local-files/{out_name}"
        result["downloadUrl"] = f"/local-files/{out_name}?download=1"
    return jsonify(result)


@app.get("/local-files/<name>")
def local_files(name):
    path = PROCESSED_DIR / name
    if not path.is_file():
        abort(404)
    as_attachment = request.args.get("download") == "1"
    return send_file(path, as_attachment=as_attachment, download_name=f"processed-{name}")


if __name__ == "__main__":
    print("LambdaLens local demo server -- no AWS, no cost")
    port = int(os.environ.get("PORT", 5000))
    host = os.environ.get("HOST", "127.0.0.1")
    print(f"Open http://127.0.0.1:{port} in your browser")
    app.run(host=host, port=port, debug=False)
