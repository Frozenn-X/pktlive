"""
FastAPI web UI for networkInterface.

Endpoints: / (dashboard), /fragment/* (HTMX), /api/health.
Supports query params for filtering (shareable URLs), optional API token auth,
and anonymization mode for shared dashboards.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Callable

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .._paths import PROJECT_ROOT

LIVE_PATH = PROJECT_ROOT / "_live.json"
METRICS_CAPTURE_PATH = PROJECT_ROOT / "_metrics_capture.json"
METRICS_SINK_PATH = PROJECT_ROOT / "_metrics.json"

# Optional API token for protected access (env NI_WEB_API_TOKEN).
API_TOKEN: str | None = os.environ.get("NI_WEB_API_TOKEN") or None

app = FastAPI(title="networkInterface Web UI", docs_url=None, redoc_url=None)

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
app.mount(
    "/static",
    StaticFiles(directory=str(Path(__file__).parent / "static")),
    name="static",
)


def _jinja_max_or_default(seq: list[Any], default: float = 0) -> float:
    """Jinja filter: max of numeric list or default if empty."""
    if not seq:
        return default
    try:
        return float(max((x if x is not None else 0) for x in seq))
    except (TypeError, ValueError):
        return default


templates.env.filters["max_or_default"] = _jinja_max_or_default


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _is_private_ip(ip: str) -> bool:
    """True if IPv4 is private (RFC 1918) or link-local (169.254)."""
    if not ip:
        return False
    parts = str(ip).strip().split(".")
    if len(parts) != 4:
        return False
    try:
        a, b, c, d = (int(x) & 0xFF for x in parts)
    except ValueError:
        return False
    if a == 10:
        return True
    if a == 172 and 16 <= b <= 31:
        return True
    if a == 192 and b == 168:
        return True
    if a == 169 and b == 254:
        return True
    return False


def _apply_anonymize(snap: dict[str, Any]) -> dict[str, Any]:
    """Mask only private IPs (and related ports/subnets) for shareable views; public IPs stay visible."""
    out = json.loads(json.dumps(snap, default=str))
    for pkt in out.get("recent_packets") or []:
        src_private = _is_private_ip(str(pkt.get("src_ip") or ""))
        dst_private = _is_private_ip(str(pkt.get("dst_ip") or ""))
        if src_private:
            pkt["src_ip"] = "x.x.x.xxx"
            if pkt.get("src_port") is not None:
                pkt["src_port"] = "****"
        if dst_private:
            pkt["dst_ip"] = "x.x.x.xxx"
            if pkt.get("dst_port") is not None:
                pkt["dst_port"] = "****"
    for lst in ("top_src_ips", "top_dst_ips", "top_subnets"):
        for item in out.get(lst) or []:
            if "ip" in item and _is_private_ip(str(item.get("ip", ""))):
                item["ip"] = "x.x.x.xxx"
            if "subnet" in item:
                sub = str(item["subnet"])
                if sub.startswith(("10.", "172.", "192.168.", "169.254.")):
                    item["subnet"] = "x.x.x.0/24"
    return out


async def _require_token_if_configured(
    request: Request,
    x_api_key: str | None = Header(None, alias="X-API-Key"),
) -> None:
    """Dependency: 401 if NI_WEB_API_TOKEN is set and request has no valid token."""
    if not API_TOKEN:
        return
    token = x_api_key or request.query_params.get("api_key")
    if token != API_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


@app.get("/api/health")
async def api_health() -> JSONResponse:
    """
    Health payload for monitoring. Status: green | yellow | red from drop rate.
    """
    live = _load_json(LIVE_PATH)
    cap = _load_json(METRICS_CAPTURE_PATH)
    health = live.get("capture_health") or {}
    drop_rate = float(health.get("drop_rate_pct") or 0)
    if drop_rate <= 0.5:
        status = "green"
    elif drop_rate <= 2.0:
        status = "yellow"
    else:
        status = "red"
    return JSONResponse({
        "status": status,
        "drop_rate_pct": round(drop_rate, 4),
        "snapshot_at": live.get("snapshot_at"),
        "os_recv": health.get("os_recv", 0),
        "os_drop": health.get("os_drop", 0),
        "counters": cap.get("counters", {}),
    })


@app.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    _: None = Depends(_require_token_if_configured),
) -> HTMLResponse:
    """Main dashboard page – tabs rendered by htmx fragments."""
    return templates.TemplateResponse(request,  "dashboard.html", {"request": request})


@app.get("/fragment/live", response_class=HTMLResponse)
async def live_fragment(
    request: Request,
    _: None = Depends(_require_token_if_configured),
) -> HTMLResponse:
    data = _load_json(LIVE_PATH)
    if request.query_params.get("anonymize") == "1":
        data = _apply_anonymize(data)
    return templates.TemplateResponse(
        request,
        "fragments/live.html",
        {"request": request, "snap": data},
    )


def _parse_port(s: str | None) -> int | None:
    if not s:
        return None
    try:
        v = int(s)
        return v if 0 <= v <= 65535 else None
    except ValueError:
        return None


def _match_subnet(ip: str, subnet_param: str) -> bool:
    """True if ip belongs to subnet (subnet_param like '192.168.1' or '192.168.1.0')."""
    if not subnet_param or not ip:
        return False
    parts = ip.split(".")
    prefix = subnet_param.strip().rstrip(".0").rstrip(".")
    sub_parts = prefix.split(".")
    if len(sub_parts) > 4 or len(parts) != 4:
        return False
    return ".".join(parts[: len(sub_parts)]) == ".".join(sub_parts)


def _filter_and_sort_recent(data: dict[str, Any], request: Request) -> dict[str, Any]:
    recent = list(data.get("recent_packets") or [])
    if not recent:
        data = dict(data)
        data["recent_packets"] = recent
        return data

    params = request.query_params
    q = (params.get("q") or "").strip().lower()
    proto = (params.get("proto") or "").strip().upper()
    sort = (params.get("sort") or "ts_desc").strip()
    src_subnet = (params.get("src_subnet") or "").strip()
    dst_port_min = _parse_port(params.get("dst_port_min"))
    dst_port_max = _parse_port(params.get("dst_port_max"))
    port_category = (params.get("port_category") or "").strip().lower()

    filtered: list[dict[str, Any]] = []
    for pkt in recent:
        if proto and str(pkt.get("protocol", "")).upper() != proto:
            continue
        if src_subnet and not _match_subnet(str(pkt.get("src_ip", "")), src_subnet):
            continue
        dp = pkt.get("dst_port")
        if dp is not None:
            try:
                dp = int(dp)
            except (TypeError, ValueError):
                dp = None
        if dst_port_min is not None and (dp is None or dp < dst_port_min):
            continue
        if dst_port_max is not None and (dp is None or dp > dst_port_max):
            continue
        if port_category and (pkt.get("port_category") or "").lower() != port_category:
            continue
        if q:
            text = " ".join(
                str(pkt.get(k, "")) for k in ("src_ip", "dst_ip", "src_port", "dst_port", "service_info")
            ).lower()
            if q not in text:
                continue
        filtered.append(pkt)

    def _ts_key(p: dict[str, Any]) -> str:
        return str(p.get("ts", ""))

    def _len_key(p: dict[str, Any]) -> int:
        try:
            return int(p.get("length", 0) or 0)
        except (TypeError, ValueError):
            return 0

    if sort == "len_desc":
        filtered.sort(key=_len_key, reverse=True)
    else:
        filtered.sort(key=_ts_key, reverse=True)

    data = dict(data)
    data["recent_packets"] = filtered
    return data


@app.get("/fragment/live-body", response_class=HTMLResponse)
async def live_body_fragment(
    request: Request,
    _: None = Depends(_require_token_if_configured),
) -> HTMLResponse:
    """Return only <tr> rows so HTMX innerHTML swap into #live-tbody does not break the table."""
    data = _load_json(LIVE_PATH)
    if request.query_params.get("anonymize") == "1":
        data = _apply_anonymize(data)
    data = _filter_and_sort_recent(data, request)
    return templates.TemplateResponse(
        request,
        "fragments/live_trs.html",
        {"request": request, "snap": data},
    )


@app.get("/fragment/stats", response_class=HTMLResponse)
async def stats_fragment(
    request: Request,
    _: None = Depends(_require_token_if_configured),
) -> HTMLResponse:
    data = _load_json(LIVE_PATH)
    if request.query_params.get("anonymize") == "1":
        data = _apply_anonymize(data)
    return templates.TemplateResponse(
        request,
        "fragments/stats.html",
        {"request": request, "snap": data},
    )


@app.get("/fragment/ports", response_class=HTMLResponse)
async def ports_fragment(
    request: Request,
    _: None = Depends(_require_token_if_configured),
) -> HTMLResponse:
    data = _load_json(LIVE_PATH)
    if request.query_params.get("anonymize") == "1":
        data = _apply_anonymize(data)
    return templates.TemplateResponse(
        request,
        "fragments/ports.html",
        {"request": request, "snap": data},
    )


@app.get("/fragment/pipeline", response_class=HTMLResponse)
async def pipeline_fragment(
    request: Request,
    _: None = Depends(_require_token_if_configured),
) -> HTMLResponse:
    live = _load_json(LIVE_PATH)
    cap_metrics = _load_json(METRICS_CAPTURE_PATH)
    sink_metrics = _load_json(METRICS_SINK_PATH)
    return templates.TemplateResponse(
        request,
        "fragments/pipeline.html",
        {
            "request": request,
            "live": live,
            "cap": cap_metrics,
            "sink": sink_metrics,
        },
    )
