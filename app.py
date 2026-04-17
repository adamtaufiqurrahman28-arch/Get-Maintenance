from flask import Flask, render_template, request, redirect, url_for, flash, Response, send_from_directory, jsonify
import os, re, json
from pathlib import Path
from functools import wraps
from datetime import datetime, timezone, timedelta
from dateutil import parser as dtparse

from falcon_wrapper import (
    list_hosts_full,
    uninstall_sensor,
    build_inactive_report,
    is_host_online,
)


app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev")

USER = os.getenv("BASIC_AUTH_USER")
PASS = os.getenv("BASIC_AUTH_PASS")

# ======== BRAND / TENANT (bisa di .env) ========
TENANT_NAME   = os.getenv("TENANT_NAME", "SDT")
# Tema merah yang tetap nyaman di mata
THEME_PRIMARY = os.getenv("THEME_PRIMARY", "#ef4444")  # red-500
THEME_SECOND  = os.getenv("THEME_SECOND",  "#f87171")  # red-400/rose
THEME_DARK    = os.getenv("THEME_DARK",    "#111827")  # gray-900
THEME_TEXT    = os.getenv("THEME_TEXT",    "#ffe4e6")  # rose-100

# live online window (menit)
ONLINE_WINDOW = int(os.getenv("ONLINE_WINDOW", "10"))

UNINSTALL_LOG = Path("uninstall_log.jsonl")

# -------------- helpers --------------
def require_auth(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not USER:
            return f(*args, **kwargs)
        auth = request.authorization
        if not auth or not (auth.username == USER and auth.password == PASS):
            return Response("Authentication required", 401,
                            {"WWW-Authenticate": 'Basic realm="Login Required"'})
        return f(*args, **kwargs)
    return wrapper

def _safe_int(val, default=None, min_value=None):
    try:
        if val is None:
            return default
        val = int(str(val).strip())
        if min_value is not None and val < min_value:
            return min_value
        return val
    except (ValueError, TypeError):
        return default

def _age_days(dt_iso: str | None) -> int | None:
    if not dt_iso:
        return None
    try:
        dt = dtparse.isoparse(str(dt_iso))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        return int((now - dt).total_seconds() // 86400)
    except Exception:
        return None

def _log_uninstall(aid: str, hostname: str | None, platform: str | None, ok: bool, raw_msg: str):
    rec = {
        "ts": datetime.utcnow().isoformat() + "Z",
        "aid": aid,
        "hostname": hostname or "",
        "platform": platform or "",
        "ok": bool(ok),
        "msg": (raw_msg or "")[:800]
    }
    with UNINSTALL_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")

def _read_uninstall_events(days: int = 30, limit: int = 1000):
    if not UNINSTALL_LOG.exists():
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    out = []
    with UNINSTALL_LOG.open("r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
                ts = dtparse.isoparse(rec.get("ts"))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if ts >= cutoff:
                    out.append(rec)
            except Exception:
                continue
            if len(out) >= limit:
                break
    # terbaru di atas
    out.sort(key=lambda x: x.get("ts",""), reverse=True)
    return out

# ======== inject tenant & theme ke semua template ========
@app.context_processor
def inject_brand():
    return dict(
        tenant_name=TENANT_NAME,
        theme_primary=THEME_PRIMARY,
        theme_second=THEME_SECOND,
        theme_dark=THEME_DARK,
        theme_text=THEME_TEXT,
    )

# -------------- routes --------------
@app.route("/")
@require_auth
def index():
    """
    Landing -> 2 tombol:
      - mode=inactive  -> Inactive + Tokens (+ daftar host hasil filter)
      - mode=live      -> Live Uninstall (online only + log)
    """
    mode = (request.args.get("mode") or "menu").lower()

    # Filter minimal: OS + Search
    os_filter = request.args.get("os", "Windows")          # Windows | Linux | Mac | All
    q = (request.args.get("q") or "").strip().lower()

    ctx = {
        "mode": mode,
        "os_filter": os_filter,
        "q": q,
        "online_window": ONLINE_WINDOW
    }

    if mode == "inactive":
        # Parameter filter untuk preview daftar + tokens
        platform = request.args.get("platform", os_filter)  # default ikut OS filter
        threshold_days = _safe_int(request.args.get("threshold_days"), default=14, min_value=1)
        show_list = request.args.get("show", "") == "1"

        ctx.update({"threshold_days": threshold_days, "platform_sel": platform})

        if show_list:
            # Ambil daftar + tokens tanpa bikin artefak
            platform_filter = None if (platform in ["All", "", None]) else f"platform_name:'{platform}'"
            try:
                rows, _ = build_inactive_report(
                    threshold_days=threshold_days,
                    platform_filter=platform_filter,
                    audit_message=f"{TENANT_NAME} preview inactive tokens (> {threshold_days} days)",
                    sleep_between_calls=0.0,
                    make_artifacts=False
                )
            except Exception as e:
                flash(("danger", f"Gagal memuat daftar inactive: {e}"))
                rows = []
            # optional search q di preview
            if q:
                ql = q.lower()
                rows = [r for r in rows if ql in (r.get("hostname") or "").lower()
                                     or ql in (r.get("device_id") or "").lower()
                                     or ql in (r.get("uninstall_token") or "").lower()]
            ctx["inactive_rows"] = rows

        return render_template("index.html", **ctx)

    if mode == "live":
        # Tampilkan host online (≤ ONLINE_WINDOW menit)
        try:
            live_hosts = list_hosts_full(os_filter=os_filter, active_minutes=ONLINE_WINDOW)
        except Exception as e:
            flash(("danger", f"Failed to load hosts: {e}"))
            live_hosts = []

        if q:
            live_hosts = [h for h in live_hosts
                          if q in (h["hostname"] or "").lower()
                          or q in (h["aid"] or "").lower()]
        # Log uninstall (succeed/failed)
        logs = _read_uninstall_events(days=30, limit=200)

        ctx.update({"live": live_hosts, "uninstall_logs": logs})
        return render_template("index.html", **ctx)

    # default landing
    return render_template("index.html", **ctx)

@app.route("/uninstall/<aid>", methods=["POST"])
@require_auth
def uninstall(aid):
    if not re.fullmatch(r"[A-Za-z0-9\\-]+", aid or ""):
        flash(("danger", "Invalid AID format"))
        return redirect(url_for("index", mode="live"))

    hostname = request.form.get("hostname") or ""
    platform = request.form.get("platform") or ""

    # pastikan masih online
    try:
        if not is_host_online(aid, window_minutes=ONLINE_WINDOW):
            flash(("warning", f"Host {hostname or aid} tidak online (≤ {ONLINE_WINDOW} menit). Uninstall dibatalkan."))
            return redirect(url_for("index", mode="live"))
    except Exception as e:
        flash(("danger", f"Gagal verifikasi status online: {e}"))
        return redirect(url_for("index", mode="live"))

    ok, msg = uninstall_sensor(aid)
    _log_uninstall(aid, hostname, platform, ok, msg)
    flash(("success" if ok else "danger", (msg or "")[:600]))
    return redirect(url_for("index", mode="live"))

# --------- Inactive: Generate Report ---------
@app.route("/report/inactive", methods=["POST"])
@require_auth
def report_inactive():
    platform = request.form.get("platform", "All")
    threshold_days = _safe_int(request.form.get("threshold_days"), default=14, min_value=1)
    make_artifacts = request.form.get("make_artifacts", "on") == "on"
    platform_filter = None if platform == "All" else f"platform_name:'{platform}'"
    try:
        data, out_dir = build_inactive_report(
            threshold_days=threshold_days, platform_filter=platform_filter,
            audit_message=f"{TENANT_NAME} inactive host token export (> {threshold_days} days)",
            sleep_between_calls=0.0, make_artifacts=make_artifacts
        )
    except Exception as e:
        flash(("danger", f"Generate report failed: {e}"))
        return redirect(url_for("index", mode="inactive", platform=platform, threshold_days=threshold_days))
    msg = f"[{TENANT_NAME}] Report OK. {len(data)} host tidak aktif > {threshold_days} hari."
    if make_artifacts and out_dir:
        html_url = url_for("report_file", folder=out_dir.name, filename="report.html")
        pdf_url  = url_for("report_file", folder=out_dir.name, filename="report.pdf")
        msg += f' Artefak: <a href="{html_url}">HTML</a>'
        if (out_dir / "report.pdf").exists(): msg += f' &middot; <a href="{pdf_url}">PDF</a>'
    flash(("success", msg))
    return redirect(url_for("index", mode="inactive", platform=platform, threshold_days=threshold_days, show=1))

@app.route("/report/file/<path:folder>/<path:filename>")
@require_auth
def report_file(folder: str, filename: str):
    base = Path().resolve()
    directory = (base / folder).resolve()
    if base not in directory.parents and base != directory:
        return Response("Invalid path", 400)
    return send_from_directory(directory, filename, as_attachment=False)

# --------- Metrics untuk chart Inactive (tetap ada) ---------
@app.route("/api/metrics")
@require_auth
def api_metrics():
    os_filter = request.args.get("os", "All")
    threshold_days = _safe_int(request.args.get("threshold_days"), default=14, min_value=1)
    buckets = [(0,7), (8,14), (15,30), (31,90), (91, 99999)]
    try:
        hosts = list_hosts_full(os_filter=os_filter if os_filter in ["Windows","Linux","Mac"] else "All", active_minutes=None)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    def bname(lo, hi): return f"{lo}-{hi}d" if hi < 99999 else f">{lo}d"
    labels = [bname(lo,hi) for lo,hi in buckets]; values = [0]*len(buckets)
    ages, platform_counts, stale_total = [], {}, 0
    for h in hosts:
        platform_counts[h["platform"]] = platform_counts.get(h["platform"], 0) + 1
        d = _age_days(h.get("last_seen")); ages.append(d)
        if d is not None and d > threshold_days: stale_total += 1
    for d in ages:
        if d is None: continue
        for i,(lo,hi) in enumerate(buckets):
            if lo <= d <= hi: values[i]+=1; break
    return jsonify({
        "totals":{"hosts": len(hosts), "stale_over_threshold": stale_total, "threshold_days": threshold_days},
        "inactive_buckets":{"labels": labels, "values": values},
        "platform_counts": platform_counts
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)



