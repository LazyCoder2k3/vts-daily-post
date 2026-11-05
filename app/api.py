"""HTTP API (FastAPI router) — CHỦ SỞ HỮU: agent `strava`.

Endpoint: /health, /auth/strava, /connect/{token}, /auth/callback, /webhook (GET verify, POST event).
`app/main.py` include `router`. Webhook Strava phải được trả 200 trong 2 giây -> xử lý nặng chạy ở BackgroundTasks.
"""
from __future__ import annotations

import html
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app import notify
from app.config import get_settings
from app.db.base import session_scope
from app.db.models import Athlete, WebhookEvent
from app.deps import get_client
from app.services import activities as activities_svc
from app.services import linking as linking_svc
from app.strava.client import StravaError

log = logging.getLogger(__name__)

router = APIRouter()

_HTML = """<!doctype html>
<html lang="vi">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  *{{box-sizing:border-box}}
  body{{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:16px;
       font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
       background:#f3f4f6;color:#1f2933;line-height:1.5}}
  .card{{background:#fff;border-radius:16px;padding:28px 24px;width:100%;max-width:420px;
        box-shadow:0 10px 30px rgba(0,0,0,.08);text-align:center}}
  .icon{{font-size:44px;line-height:1;margin-bottom:12px}}
  h1{{font-size:1.3rem;margin:0 0 10px}}
  p{{margin:0 0 10px}}
  .muted{{color:#6b7280;font-size:.92rem}}
  .brand{{margin-top:20px;font-size:.78rem;color:#fc4c02;font-weight:600;letter-spacing:.02em}}
  code{{background:#f3f4f6;border-radius:6px;padding:2px 6px;font-size:.95em}}
</style>
</head>
<body>
<main class="card">
{body}
<div class="brand">VTS Running Club · Powered by Strava</div>
</main>
</body>
</html>
"""


def _page(title: str, body: str, status: int = 200) -> HTMLResponse:
    """Trang HTML nhỏ, gọn, hiển thị tốt trên điện thoại. `body` là HTML (đã escape phần động)."""
    return HTMLResponse(_HTML.format(title=html.escape(title), body=body), status_code=status)


# --------------------------------------------------------------------------- health
@router.get("/health")
async def health() -> dict:
    return {"status": "ok", "time": datetime.now(timezone.utc).isoformat()}


# --------------------------------------------------------------------------- OAuth
def _redirect_to_strava(state: str | None):
    if not state or not state.strip():
        return _page("Thiếu mã liên kết",
                     '<div class="icon">⚠️</div><h1>Thiếu mã liên kết</h1>'
                     '<p>Hãy gõ <code>/connect</code> trong Telegram để nhận link kết nối mới.</p>', 400)
    return RedirectResponse(get_client().authorize_url(state.strip()), status_code=307)


@router.get("/auth/strava")
async def auth_strava(state: str | None = None):
    return _redirect_to_strava(state)


@router.get("/connect/{token}")
async def connect(token: str):
    return _redirect_to_strava(token)


async def _after_link(athlete: Athlete) -> None:
    """Chạy nền sau khi kết nối: backfill hoạt động trong giải rồi nhắn Telegram cho runner."""
    client = get_client()
    name = athlete.name
    try:
        async with session_scope() as s:
            fresh = await s.get(Athlete, athlete.id) if athlete.id is not None else None
            n = await activities_svc.backfill_athlete(s, client, fresh or athlete, notify=False)
        await notify.dm(athlete.telegram_id,
                        f"✅ Đã kết nối Strava ({name}). Đã nạp {n} hoạt động trong giải. Gõ /me để xem.")
    except Exception as e:  # noqa: BLE001
        log.exception("Backfill sau khi kết nối thất bại (athlete strava_id=%s): %s", athlete.strava_id, e)
        await notify.dm(athlete.telegram_id,
                        f"✅ Đã kết nối Strava ({name}). Chưa nạp được hoạt động cũ, bot sẽ thử lại khi đối soát. "
                        f"Gõ /me để xem.")


@router.get("/auth/callback")
async def auth_callback(background: BackgroundTasks, code: str | None = None, state: str | None = None,
                        scope: str | None = None, error: str | None = None):
    if error:
        log.info("OAuth callback bị từ chối: error=%s state=%s", error, state)
        return _page("Chưa kết nối Strava",
                     '<div class="icon">🙁</div><h1>Bạn đã từ chối cấp quyền</h1>'
                     '<p>Bot chưa thể đọc hoạt động của bạn. Nếu đổi ý, gõ <code>/connect</code> trong Telegram '
                     'để nhận link mới.</p>')
    if not code or not state:
        return _page("Thiếu thông tin",
                     '<div class="icon">⚠️</div><h1>Thiếu mã xác thực</h1>'
                     '<p>Link không hợp lệ. Gõ <code>/connect</code> trong Telegram để nhận link mới.</p>', 400)
    client = get_client()
    try:
        async with session_scope() as s:
            athlete = await linking_svc.complete_link(s, client, state, code, scope or "")
    except ValueError as e:
        log.info("OAuth callback với state không hợp lệ/hết hạn: %s", e)
        return _page("Liên kết hết hạn",
                     '<div class="icon">⌛</div><h1>Liên kết hết hạn</h1>'
                     '<p>Liên kết hết hạn, gõ <code>/connect</code> lại trong Telegram để nhận link mới.</p>', 400)
    except StravaError as e:
        log.warning("Strava lỗi khi đổi code: %s", e)
        return _page("Strava đang gặp lỗi",
                     '<div class="icon">⚠️</div><h1>Strava đang gặp lỗi</h1>'
                     '<p>Không đổi được mã xác thực với Strava. Vui lòng thử lại sau ít phút bằng cách gõ '
                     '<code>/connect</code> trong Telegram.</p>', 502)
    name = html.escape(athlete.name)
    background.add_task(_after_link, athlete)
    return _page("Kết nối Strava thành công",
                 f'<div class="icon">✅</div><h1>Kết nối Strava thành công, {name}!</h1>'
                 '<p>Quay lại Telegram để xem thống kê. Bot đang nạp các hoạt động trong giải của bạn '
                 'và sẽ nhắn khi xong.</p>'
                 '<p class="muted">Bạn có thể đóng trang này. Gõ <code>/me</code> trong Telegram để xem số liệu, '
                 '<code>/unlink</code> để ngắt kết nối bất kỳ lúc nào.</p>')


# --------------------------------------------------------------------------- webhook
@router.get("/webhook")
async def webhook_verify(request: Request):
    q = request.query_params
    verify_token = q.get("hub.verify_token")
    challenge = q.get("hub.challenge", "")
    if verify_token and verify_token == get_settings().strava_webhook_verify_token:
        return JSONResponse({"hub.challenge": challenge})
    log.warning("Webhook verify sai token (mode=%s)", q.get("hub.mode"))
    return JSONResponse({"detail": "verify_token không đúng"}, status_code=403)


def _int_or_none(v) -> int | None:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


async def _process_webhook(event_id: int) -> None:
    """Chạy nền: nạp lại WebhookEvent rồi giao cho services xử lý."""
    client = get_client()
    try:
        async with session_scope() as s:
            ev = await s.get(WebhookEvent, event_id)
            if ev is None:
                log.warning("WebhookEvent id=%s không còn trong DB", event_id)
                return
            await activities_svc.handle_webhook_event(s, client, ev)
    except Exception as e:  # noqa: BLE001
        log.exception("Xử lý webhook event id=%s thất bại: %s", event_id, e)
        try:
            async with session_scope() as s:
                ev = await s.get(WebhookEvent, event_id)
                if ev is not None and not ev.error:
                    ev.error = f"{type(e).__name__}: {e}"[:2000]
        except Exception:  # noqa: BLE001
            log.exception("Không ghi được lỗi vào WebhookEvent id=%s", event_id)


@router.post("/webhook")
async def webhook_event(request: Request, background: BackgroundTasks):
    try:
        body = await request.json()
    except Exception as e:  # noqa: BLE001
        log.warning("Webhook body không parse được JSON: %s", e)
        return {"ok": False}
    if not isinstance(body, dict):
        log.warning("Webhook body không phải object JSON: %r", body)
        return {"ok": False}

    object_type = body.get("object_type")
    aspect_type = body.get("aspect_type")
    object_id = _int_or_none(body.get("object_id"))
    owner_id = _int_or_none(body.get("owner_id"))
    if not isinstance(object_type, str) or not isinstance(aspect_type, str) or object_id is None or owner_id is None:
        log.warning("Webhook thiếu trường bắt buộc: %r", body)
        return {"ok": False}

    try:
        async with session_scope() as s:
            ev = WebhookEvent(
                object_type=object_type[:20],
                object_id=object_id,
                aspect_type=aspect_type[:20],
                owner_id=owner_id,
                event_time=_int_or_none(body.get("event_time")),
                payload=body,
            )
            s.add(ev)
            await s.flush()
            event_id = ev.id
    except Exception as e:  # noqa: BLE001
        log.exception("Không lưu được WebhookEvent: %s", e)
        return {"ok": False}

    log.info("Webhook %s %s object_id=%s owner_id=%s -> event id=%s",
             object_type, aspect_type, object_id, owner_id, event_id)
    background.add_task(_process_webhook, event_id)
    return {"ok": True}
