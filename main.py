import os
import time
import uuid
from pathlib import Path
from typing import Optional

import requests
import httpx
from fastapi import FastAPI, Request, Header, HTTPException
from fastapi.staticfiles import StaticFiles

from telethon import TelegramClient, events, functions, types
from telethon.sessions import StringSession


# ======================
# ENV
# ======================
API_ID = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]
STRING_SESSION = os.environ["TG_STRING_SESSION"]

PAGER_URL = os.getenv("PAGER_INBOUND_URL", "https://pager.co.ua/api/webhooks/custom")
PAGER_KEY = os.environ["PAGER_CHANNEL_KEY"]

# ВАЖНО: должен быть публичный домен твоего Railway сервиса, например:
# https://mtproto-bridge-production.up.railway.app
PUBLIC_BASE_URL = os.environ["PUBLIC_BASE_URL"].rstrip("/")

# Хранилище (лучше на Railway Volume)
MEDIA_DIR = Path(os.getenv("MEDIA_DIR", "/data/media"))
AVATAR_DIR = Path(os.getenv("AVATAR_DIR", "/data/avatars"))
MEDIA_DIR.mkdir(parents=True, exist_ok=True)
AVATAR_DIR.mkdir(parents=True, exist_ok=True)

# Простой in-memory кэш аватарок: user_id -> url/None
AVATAR_CACHE: dict[int, Optional[str]] = {}

app = FastAPI()
tg = TelegramClient(StringSession(STRING_SESSION), API_ID, API_HASH)

# Раздача файлов наружу (Pager требует http/https URL)
app.mount("/files", StaticFiles(directory=str(MEDIA_DIR)), name="files")
app.mount("/avatars", StaticFiles(directory=str(AVATAR_DIR)), name="avatars")


# ======================
# Helpers
# ======================
def pager_post(payload: dict) -> None:
    headers = {"Content-Type": "application/json", "x-channel-key": PAGER_KEY}
    r = requests.post(PAGER_URL, json=payload, headers=headers, timeout=20)
    if r.status_code >= 400:
        print("Pager inbound error:", r.status_code, r.text[:800])


def client_external_id(peer_id: int) -> str:
    return f"tg_user:{peer_id}"


def message_external_id(msg_id: int) -> str:
    return f"tg_msg:{msg_id}"


def pager_attachment_type_from_telethon_event(event) -> str:
    # Очень простой маппинг
    if getattr(event, "photo", None):
        return "image"
    if getattr(event, "video", None):
        return "video"
    if getattr(event, "audio", None):
        return "audio"
    # документы, файлы
    return "file"


async def save_telegram_media_and_get_attachments(event) -> list:
    """
    Скачивает медиа из Telegram в MEDIA_DIR и возвращает Pager attachments[]:
    [{type, payload:{url}}]
    """
    if not getattr(event, "media", None):
        return []

    att_type = pager_attachment_type_from_telethon_event(event)
    # уникальное имя; telethon сам поставит расширение если сможет
    fname = f"{int(time.time())}_{uuid.uuid4().hex}"
    try:
        local_path = await event.download_media(file=str(MEDIA_DIR / fname))
        if not local_path:
            return []
        local_path = Path(local_path)
        url = f"{PUBLIC_BASE_URL}/files/{local_path.name}"

        return [{
            "type": att_type if att_type in ["image", "video", "audio", "document", "file"] else "file",
            "payload": {"url": url}
        }]
    except Exception as e:
        print("download_media ERROR:", repr(e))
        return []


async def get_userpic_url(sender) -> Optional[str]:
    """
    Скачивает аватар пользователя (если есть) и возвращает публичный URL.
    Кэширует в памяти.
    """
    try:
        if not sender:
            return None
        user_id = getattr(sender, "id", None)
        if not user_id:
            return None

        if user_id in AVATAR_CACHE:
            return AVATAR_CACHE[user_id]

        # нет фото — нечего качать
        if not getattr(sender, "photo", None):
            AVATAR_CACHE[user_id] = None
            return None

        fname = f"avatar_{user_id}.jpg"
        local_path = await tg.download_profile_photo(sender, file=str(AVATAR_DIR / fname))
        if not local_path:
            AVATAR_CACHE[user_id] = None
            return None

        local_path = Path(local_path)
        url = f"{PUBLIC_BASE_URL}/avatars/{local_path.name}"
        AVATAR_CACHE[user_id] = url
        return url

    except Exception as e:
        print("get_userpic_url ERROR:", repr(e))
        return None


# ======================
# Routes
# ======================
@app.get("/")
async def root():
    return {"ok": True}


@app.get("/health")
async def health():
    return {"status": "up"}


# ======================
# Telegram -> Pager
# ======================
@tg.on(events.NewMessage)
async def on_new_message(event):
    try:
        # только private 1:1
        if not event.is_private:
            return

        sender = await event.get_sender()
        peer_id = sender.id if sender else event.sender_id

        direction = "outgoing" if event.out else "incoming"
        text = event.raw_text or ""

        # 1) вложения
        attachments = await save_telegram_media_and_get_attachments(event)

        # 2) аватар
        image_url = await get_userpic_url(sender)

        # 3) имя
        name = (getattr(sender, "first_name", None) or getattr(sender, "username", None) or None)

        payload = {
            "event": "message.created",
            "client": {
                "externalId": client_external_id(peer_id),
            },
            "message": {
                "externalId": message_external_id(event.id),
                "direction": direction,
                "text": text,
                "attachments": attachments,
            },
        }

        # по документации Pager: name/imageUrl можно передавать при первом контакте или когда обновились
        if name:
            payload["client"]["name"] = name
        if image_url:
            payload["client"]["imageUrl"] = image_url

        pager_post(payload)

    except Exception as e:
        print("TG->Pager ERROR:", repr(e))


# ======================
# Pager -> Telegram
# ======================
@app.post("/pager/outbound")
async def pager_outbound(request: Request, x_channel_key: str = Header(None)):
    if x_channel_key != PAGER_KEY:
        raise HTTPException(status_code=401, detail="bad x-channel-key")

    payload = await request.json()
    if payload.get("event") != "message.created":
        return {"externalMessageId": "ignored"}

    client_obj = payload.get("client") or {}
    msg_obj = payload.get("message") or {}

    c_ext = client_obj.get("externalId")
    text = (msg_obj.get("text") or "").strip()
    attachments = msg_obj.get("attachments") or []

    if not c_ext or not c_ext.startswith("tg_user:"):
        raise HTTPException(status_code=400, detail="missing/invalid client.externalId")

    peer_id = int(c_ext.split(":", 1)[1])

    try:
        last_sent_id = None

        # 1) текст
        if text:
            sent = await tg.send_message(peer_id, text)
            last_sent_id = getattr(sent, "id", None)

        # 2) вложения (Pager отдаёт URL — скачиваем и отправляем как файл)
        for a in attachments:
            url = (((a.get("payload") or {}).get("url")) or "").strip()
            if not url:
                continue

            try:
                async with httpx.AsyncClient(timeout=30) as client:
                    r = await client.get(url)
                    r.raise_for_status()
                    data = r.content

                tmp_path = MEDIA_DIR / f"pager_{uuid.uuid4().hex}"
                tmp_path.write_bytes(data)

                sent_file = await tg.send_file(peer_id, str(tmp_path))
                last_sent_id = getattr(sent_file, "id", None)

                # чистим временный файл
                try:
                    tmp_path.unlink(missing_ok=True)
                except Exception:
                    pass

            except Exception as e:
                print("Pager attachment send error:", repr(e))

        external_id = f"mtproto:{peer_id}:{last_sent_id or 'noid'}"
        return {"externalMessageId": external_id}

    except Exception as e:
        print("Pager->TG ERROR:", repr(e))
        raise HTTPException(status_code=500, detail="send failed")


# ======================
# Start chat by phone (write first)
# ======================
@app.post("/start_chat_by_phone")
async def start_chat_by_phone(request: Request, x_channel_key: str = Header(None)):
    if x_channel_key != PAGER_KEY:
        raise HTTPException(status_code=401, detail="bad x-channel-key")

    data = await request.json()
    phone = (data.get("phone") or "").strip()
    text = (data.get("text") or "Добрый день! Это Stelio. Подскажите, актуально по потолкам?").strip()

    if not phone.startswith("+"):
        raise HTTPException(status_code=400, detail="phone must be in +380... format")

    try:
        res = await tg(functions.contacts.ImportContactsRequest(
            contacts=[
                types.InputPhoneContact(
                    client_id=0,
                    phone=phone,
                    first_name="Client",
                    last_name=""
                )
            ]
        ))

        if not res.users:
            raise HTTPException(status_code=404, detail="user not found by phone (maybe hidden / not on Telegram)")

        user = res.users[0]
        sent = await tg.send_message(user.id, text)

        return {
            "ok": True,
            "phone": phone,
            "telegramUserId": user.id,
            "clientExternalId": client_external_id(user.id),
            "sentMessageId": getattr(sent, "id", None),
        }

    except HTTPException:
        raise
    except Exception as e:
        print("start_chat_by_phone ERROR:", repr(e))
        raise HTTPException(status_code=500, detail="failed")


@app.on_event("startup")
async def startup():
    await tg.start()
    print("MTProto client started")


@app.on_event("shutdown")
async def shutdown():
    await tg.disconnect()
    print("MTProto client stopped")
