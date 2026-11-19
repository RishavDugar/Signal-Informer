"""
Flask app exposing the dashboard UI + JSON API.

Run via the project launcher:  python run_ui.py
(which adds the repo root to sys.path so `config`, `data.db`, etc. import.)
"""

from __future__ import annotations

from flask import Flask, jsonify, render_template, request

from webui import jobs, queries

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 60 * 1024 * 1024   # court PDFs can be large


# ── Page ───────────────────────────────────────────────────────────────────────

@app.get("/")
def index():
    return render_template("index.html")


# ── Status / data API ──────────────────────────────────────────────────────────

@app.get("/api/status")
def api_status():
    return jsonify(queries.status())


@app.get("/api/signals")
def api_signals():
    on_date = request.args.get("date") or None
    return jsonify(queries.signals(on_date=on_date))


@app.get("/api/news")
def api_news():
    return jsonify(queries.news())


@app.get("/api/scout")
def api_scout():
    return jsonify(queries.scout())


@app.get("/api/setups")
def api_setups():
    return jsonify(queries.setups())


@app.get("/api/hft")
def api_hft():
    return jsonify(queries.hft_results())


@app.get("/api/litigation")
def api_litigation():
    days = int(request.args.get("days", 7))
    return jsonify(queries.litigation(days=days))


def _upload():
    f = request.files.get("file")
    if not f or not f.filename:
        return None, None
    return f.filename, f.read()


@app.post("/api/litigation/case/<int:case_id>/attach")
def api_litigation_attach(case_id: int):
    """Attach a downloaded court document to one case → Claude reads it."""
    name, data = _upload()
    if not name:
        return jsonify({"error": "no file"}), 400
    try:
        from litigation.attachments import analyze_for_case, save_upload
        path = save_upload(name, data, case_id)
        return jsonify(analyze_for_case(case_id, path))
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/litigation/scan")
def api_litigation_scan():
    """Attach a whole cause list / order list → checked against every tracked case."""
    name, data = _upload()
    if not name:
        return jsonify({"error": "no file"}), 400
    try:
        from litigation.attachments import save_upload, scan_document
        return jsonify(scan_document(save_upload(name, data, None)))
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/litigation/check/done")
def api_litigation_check_done():
    body = request.get_json(silent=True) or {}
    from litigation import db as ldb
    ldb.mark_check(str(body.get("key")), body.get("case_id"), bool(body.get("done", True)),
                   str(body.get("note") or "marked in dashboard"))
    return jsonify({"ok": True})


@app.get("/api/outcomes")
def api_outcomes():
    return jsonify(queries.outcomes_scorecard())


@app.get("/api/stocks")
def api_stocks():
    return jsonify(queries.stocks())


@app.get("/api/ohlcv/<symbol>")
def api_ohlcv(symbol: str):
    days = request.args.get("days", default=120, type=int)
    return jsonify(queries.ohlcv(symbol, days=days))


@app.get("/api/logs")
def api_logs():
    n = request.args.get("lines", default=400, type=int)
    return jsonify(queries.logs(lines=n))


# ── Config ─────────────────────────────────────────────────────────────────────

@app.get("/api/config")
def api_config_get():
    return jsonify(queries.read_env())


@app.post("/api/config")
def api_config_set():
    data = request.get_json(force=True) or {}
    return jsonify(queries.write_env(data))


# ── Jobs ───────────────────────────────────────────────────────────────────────

@app.get("/api/jobs/catalog")
def api_jobs_catalog():
    return jsonify(jobs.job_catalog())


@app.get("/api/jobs")
def api_jobs_list():
    return jsonify(jobs.manager.list())


@app.post("/api/jobs/run")
def api_jobs_run():
    data = request.get_json(force=True) or {}
    spec_id = data.get("id")
    flags = data.get("flags") or []
    extra = data.get("extra") or []
    try:
        job = jobs.manager.launch(spec_id, flags=flags, extra=extra)
    except KeyError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"id": job.id, "label": job.label})


@app.get("/api/jobs/<int:jid>")
def api_jobs_get(jid: int):
    since = request.args.get("since", default=0, type=int)
    job = jobs.manager.get(jid)
    if not job:
        return jsonify({"error": "not found"}), 404
    return jsonify(job.snapshot(since=since))


@app.post("/api/jobs/<int:jid>/stop")
def api_jobs_stop(jid: int):
    return jsonify({"stopped": jobs.manager.stop(jid)})


def create_app() -> Flask:
    return app
