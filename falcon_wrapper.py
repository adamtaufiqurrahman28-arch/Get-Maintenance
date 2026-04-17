import os
import shutil
import subprocess
import time
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Optional, Tuple

from dotenv import load_dotenv
from dateutil import parser as dtparse
from falconpy import Hosts, SensorUpdatePolicy

# --- timezone safe (WIB fallback) ---
from datetime import timezone as _tz, timedelta as _td
try:
    from zoneinfo import ZoneInfo
    def _safe_zoneinfo(key: str, fallback_hours: int = 7):
        try:
            return ZoneInfo(key)
        except Exception:
            return _tz(_td(hours=fallback_hours))
except Exception:
    def _safe_zoneinfo(key: str, fallback_hours: int = 7):
        return _tz(_td(hours=fallback_hours))

load_dotenv()

CLIENT_ID  = os.getenv("FALCON_CLIENT_ID")
CLIENT_SEC = os.getenv("FALCON_CLIENT_SECRET")
CLOUD      = os.getenv("FALCON_CLOUD", "us-1")
PS_EXE_ENV = os.getenv("POWERSHELL_EXE")

TZ = _safe_zoneinfo("Asia/Jakarta", 7)


# ------------------- util umum -------------------

def _get_hosts_api() -> Hosts:
    if not CLIENT_ID or not CLIENT_SEC:
        raise RuntimeError("FALCON_CLIENT_ID / FALCON_CLIENT_SECRET belum diset")
    return Hosts(client_id=CLIENT_ID, client_secret=CLIENT_SEC, cloud=CLOUD)

def _scroll_ids(filter_query: str = "") -> List[str]:
    api = _get_hosts_api()
    all_ids: List[str] = []
    resp = api.query_devices_by_filter_scroll(limit=400, filter=filter_query)
    resources = (resp.get("body", {}).get("resources") or [])
    all_ids.extend(resources)
    next_token = resp.get("body", {}).get("meta", {}).get("pagination", {}).get("token")
    while next_token:
        resp = api.query_devices_by_filter_scroll(limit=400, filter=filter_query, cursor=next_token)
        resources = (resp.get("body", {}).get("resources") or [])
        all_ids.extend(resources)
        next_token = resp.get("body", {}).get("meta", {}).get("pagination", {}).get("token")
    return all_ids

def _iso(dt_str: Optional[str]) -> Optional[datetime]:
    if not dt_str:
        return None
    try:
        return datetime.fromisoformat(str(dt_str).replace("Z", "+00:00"))
    except Exception:
        try:
            return dtparse.isoparse(str(dt_str))
        except Exception:
            return None

def _pick_powershell() -> str:
    if PS_EXE_ENV and shutil.which(PS_EXE_ENV):
        return PS_EXE_ENV
    for cand in ("pwsh", "powershell"):
        if shutil.which(cand):
            return cand
    raise RuntimeError("PowerShell tidak ditemukan (pwsh/powershell)")


# ------------------- fitur listing + uninstall -------------------

def list_hosts_full(os_filter: str = "Windows", active_minutes: Optional[int] = None) -> List[Dict]:
    filter_query = f"platform_name:'{os_filter}'" if os_filter and os_filter.lower() != "all" else ""
    aids = _scroll_ids(filter_query)
    if not aids:
        return []

    api = _get_hosts_api()
    details = []
    for i in range(0, len(aids), 400):
        chunk = ",".join(aids[i:i+400])
        resp = api.get_device_details(ids=chunk)
        details.extend(resp.get("body", {}).get("resources", []) or [])

    out: List[Dict] = []
    now = datetime.now(timezone.utc)
    for r in details:
        aid = r.get("device_id") or r.get("aid")
        host = r.get("hostname", "")
        platform = r.get("platform_name", "")
        last_seen = r.get("last_seen") or r.get("last_seen_timestamp")
        iso = _iso(last_seen)
        if active_minutes is not None:
            if not iso or (now - iso) > timedelta(minutes=active_minutes):
                continue
        out.append({"aid": aid, "hostname": host, "platform": platform, "last_seen": last_seen or "-"})
    return out


def uninstall_sensor(aid: str) -> Tuple[bool, str]:
    ps = f"""
    try {{
      Import-Module PSFalcon -ErrorAction Stop
      Request-FalconToken -ClientId '{CLIENT_ID}' -ClientSecret '{CLIENT_SEC}' -Cloud {CLOUD} | Out-Null
      Uninstall-FalconSensor -Id '{aid}' -QueueOffline:$true -Verbose
    }} catch {{
      Write-Error $_
      exit 1
    }}
    """
    exe = _pick_powershell()
    proc = subprocess.run(
        [exe, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
        capture_output=True, text=True, timeout=180
    )
    ok = (proc.returncode == 0)
    return ok, (proc.stdout if ok else (proc.stderr or proc.stdout))


# ------------------- fitur baru: inactive report + tokens -------------------

def _age_days(dt_iso: Optional[str]) -> Optional[int]:
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

def _make_out_dir() -> Path:
    stamp = datetime.now(TZ).strftime("%Y%m%d_%H%M%S")
    out_dir = Path(f"report_{stamp}")
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir

def _export_pdf(html_path: Path, pdf_path: Path) -> Tuple[bool, str]:
    html_str = html_path.read_text(encoding="utf-8")
    try:
        import pdfkit  # type: ignore
        pdfkit.from_string(html_str, str(pdf_path))
        return True, "pdfkit/wkhtmltopdf"
    except Exception:
        pass
    try:
        from weasyprint import HTML  # type: ignore
        HTML(string=html_str, base_url=str(html_path.parent)).write_pdf(str(pdf_path))
        return True, "WeasyPrint"
    except Exception:
        return False, "none"

def build_inactive_report(
    threshold_days: int = 14,
    cloud: Optional[str] = None,
    platform_filter: Optional[str] = None,
    audit_message: str = "Export uninstall token via API",
    sleep_between_calls: float = 0.0,
    make_artifacts: bool = True
) -> Tuple[List[Dict], Optional[Path]]:
    _ = cloud  # (override disediakan, saat ini pakai CLOUD global/env)
    if not CLIENT_ID or not CLIENT_SEC:
        raise RuntimeError("Set ENV FALCON_CLIENT_ID & FALCON_CLIENT_SECRET")

    hosts_api = _get_hosts_api()
    sup_api   = SensorUpdatePolicy(client_id=CLIENT_ID, client_secret=CLIENT_SEC, cloud=CLOUD)

    # 1) scroll AIDs
    query = platform_filter if platform_filter else ""
    scroll = hosts_api.query_devices_by_filter_scroll(limit=400, filter=query)
    resources = scroll.get("body", {}).get("resources", [])
    meta = scroll.get("body", {}).get("meta", {})
    next_token = meta.get("pagination", {}).get("token")

    all_aids: List[str] = []
    while True:
        all_aids.extend(resources)
        if not next_token:
            break
        scroll = hosts_api.query_devices_by_filter_scroll(limit=400, filter=query, cursor=next_token)
        resources = scroll.get("body", {}).get("resources", [])
        meta = scroll.get("body", {}).get("meta", {})
        next_token = meta.get("pagination", {}).get("token")

    if not all_aids:
        if make_artifacts:
            out_dir = _make_out_dir()
            (out_dir / "report.html").write_text("<h3>Tidak ada host ditemukan.</h3>", encoding="utf-8")
        return [], (out_dir if make_artifacts else None)

    # 2) detail host
    details = []
    for i in range(0, len(all_aids), 400):
        chunk = all_aids[i:i+400]
        resp = hosts_api.get_device_details(ids=",".join(chunk))
        details.extend(resp.get("body", {}).get("resources", []))

    idx: Dict[str, Dict] = {}
    for r in details:
        aid = r.get("device_id") or r.get("aid")
        if not aid:
            continue
        host = r.get("hostname", "")
        last_seen = r.get("last_seen") or r.get("last_seen_timestamp")
        idx[aid] = {
            "device_id": aid,
            "hostname": host,
            "last_seen": last_seen,
            "days_since_last_seen": _age_days(last_seen)
        }

    # 3) token + filter stale
    out: List[Dict] = []
    for aid, rec in idx.items():
        days = rec["days_since_last_seen"]
        if days is None or days <= threshold_days:
            continue
        token = ""
        try:
            resp = sup_api.reveal_uninstall_token(audit_message=audit_message, device_id=aid)
            token = resp["body"]["resources"][0]["uninstall_token"]
        except Exception as e:
            token = f"ERROR: {e}"
        out.append({**rec, "uninstall_token": token})
        if sleep_between_calls:
            time.sleep(sleep_between_calls)

    out.sort(key=lambda x: (x["days_since_last_seen"] or -1), reverse=True)

    out_dir: Optional[Path] = None
    if make_artifacts:
        out_dir = _make_out_dir()
        generated_at = datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S %Z")

        def row_html(row: dict) -> str:
            return ("<tr>"
                    f"<td>{row.get('device_id','')}</td>"
                    f"<td>{row.get('hostname','')}</td>"
                    f"<td>{row.get('last_seen','')}</td>"
                    f"<td>{row.get('days_since_last_seen','')}</td>"
                    f"<td class='token-cell'>{row.get('uninstall_token','')}</td>"
                    "</tr>")
        table_rows = "".join(row_html(r) for r in out)
        total_merged = len(idx)
        stale_count  = len(out)
        missing_last = sum(1 for v in idx.values() if v["days_since_last_seen"] is None)

        html = f"""<!doctype html>
<html lang="en" data-bs-theme="light">
<head>
  <meta charset="utf-8">
  <title>Inactive Hosts & Uninstall Tokens (&gt;{threshold_days} days)</title>
  <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
  <style>
    body {{ padding: 24px; }}
    .token-cell {{ white-space: nowrap; max-width: 520px; overflow: hidden; text-overflow: ellipsis; }}
    thead th {{ position: sticky; top: 0; background: var(--bs-body-bg); z-index: 1; }}
    .table-wrap {{ max-height: 70vh; overflow: auto; }}
  </style>
</head>
<body>
  <div class="container-fluid">
    <header class="d-flex flex-wrap align-items-center justify-content-between mb-3">
      <div>
        <h1 class="h4 mb-1">Inactive Hosts &amp; Uninstall Tokens</h1>
        <div class="text-secondary small">Threshold: &gt; {threshold_days} days · Generated: {generated_at}</div>
      </div>
      <div class="d-flex gap-2">
        <span class="badge text-bg-secondary rounded-pill px-3 py-2">Total merged: {total_merged}</span>
        <span class="badge text-bg-dark rounded-pill px-3 py-2">Inactive: {stale_count}</span>
        <span class="badge text-bg-secondary rounded-pill px-3 py-2">Missing last_seen: {missing_last}</span>
      </div>
    </header>

    <div class="row g-3 mb-3">
      <div class="col-12 col-md-6 col-lg-4">
        <div class="input-group">
          <span class="input-group-text">Search</span>
          <input id="q" type="search" class="form-control" placeholder="Cari hostname / device_id / token ..." oninput="filterTable()">
        </div>
      </div>
      <div class="col-12 col-md-6 col-lg-4">
        <button class="btn btn-outline-primary w-100" onclick="copyVisibleTokens()">Copy all visible tokens</button>
      </div>
    </div>

    <div class="table-wrap">
      <table class="table table-hover table-striped align-middle">
        <thead>
          <tr>
            <th>device_id</th>
            <th>hostname</th>
            <th>last_seen</th>
            <th>days_since_last_seen</th>
            <th>uninstall_token</th>
          </tr>
        </thead>
        <tbody>
          {table_rows}
        </tbody>
      </table>
    </div>

    <p class="text-secondary small mt-3">
      Catatan: kolom <b>uninstall_token</b> sensitif. Simpan file ini secara aman.
    </p>
  </div>

  <script>
    (function(){{
      const idxToken = Array.from(document.querySelectorAll('thead th'))
        .findIndex(th => th.textContent.trim().toLowerCase() === 'uninstall_token');
      document.querySelectorAll('tbody tr').forEach(tr => {{
        const cell = tr.querySelectorAll('td')[idxToken];
        if (cell) {{
          const val = cell.textContent.trim();
          const btn = document.createElement('button');
          btn.className = 'btn btn-sm btn-outline-secondary ms-2';
          btn.textContent = 'Copy';
          btn.onclick = () => navigator.clipboard.writeText(val);
          cell.appendChild(btn);
        }}
      }});
    }})();

    function filterTable(){{
      const q = document.getElementById('q').value.toLowerCase();
      document.querySelectorAll('tbody tr').forEach(tr => {{
        tr.style.display = tr.textContent.toLowerCase().includes(q) ? '' : 'none';
      }});
    }}

    function copyVisibleTokens(){{
      const idxToken = Array.from(document.querySelectorAll('thead th'))
        .findIndex(th => th.textContent.trim().toLowerCase() === 'uninstall_token');
      const tokens = [];
      document.querySelectorAll('tbody tr').forEach(tr => {{
        if (tr.style.display === 'none') return;
        const cell = tr.querySelectorAll('td')[idxToken];
        if (cell) tokens.push(cell.textContent.trim());
      }});
      navigator.clipboard.writeText(tokens.join('\\n'));
    }}
  </script>
</body>
</html>"""
        html_path = out_dir / "report.html"
        html_path.write_text(html, encoding="utf-8")

        pdf_path = out_dir / "report.pdf"
        ok, how = _export_pdf(html_path, pdf_path)
        if not ok:
            pass

    return out, out_dir


# ------------------- NEW: checker host online (server-side) -------------------

def is_host_online(aid: str, window_minutes: int = 10) -> bool:
    """
    True jika last_seen host <= window_minutes dari sekarang (UTC).
    """
    api = _get_hosts_api()
    resp = api.get_device_details(ids=aid)
    resources = (resp.get("body", {}).get("resources") or [])
    if not resources:
        return False
    last_seen = resources[0].get("last_seen") or resources[0].get("last_seen_timestamp")
    iso = _iso(last_seen)
    if not iso:
        return False
    now = datetime.now(timezone.utc)
    return (now - iso) <= timedelta(minutes=window_minutes)
