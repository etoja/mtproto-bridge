import os
import requests
from fastapi import FastAPI, Request, Header, HTTPException

from telethon import TelegramClient, events, functions, types
from telethon.sessions import StringSession


# ========= ENV =========
API_ID = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]
STRING_SESSION = os.environ["TG_STRING_SESSION"]

PAGER_URL = os.getenv("PAGER_INBOUND_URL", "https://pager.co.ua/api/webhooks/custom")
PAGER_KEY = os.environ["PAGER_CHANNEL_KEY"]

app = FastAPI()
tg = TelegramClient(StringSession(STRING_SESSION), API_ID, API_HASH)


def pager_post(payload: dict) -> None:
    headers = {"Content-Type": "application/json", "x-channel-key": PAGER_KEY}
    r = requests.post(PAGER_URL, json=payload, headers=headers, timeout=20)
    if r.status_code >= 400:
        print("Pager inbound error:", r.status_code, r.text[:800])


def client_external_id(peer_id: int) -> str:
    return f"tg_user:{peer_id}"


def message_external_id(msg_id: int) -> str:
    return f"tg_msg:{msg_id}"


@app.get("/")
async def root():
    return {"ok": True}


@app.get("/health")
async def health():
    return {"status": "up"}


# ========= Telegram -> Pager =========
@tg.on(events.NewMessage)
async def on_new_message(event):
    try:
        # только private 1:1
        if not event.is_private:
            return

        sender = await event.get_sender()
        peer_id = sender.id if sender else event.sender_id
        text = event.raw_text or ""

        direction = "outgoing" if event.out else "incoming"

        payload = {
            "event": "message.created",
            "client": {
                "externalId": client_external_id(peer_id),
                "name": (getattr(sender, "first_name", None) or getattr(sender, "username", None) or None),
            },
            "message": {
                "externalId": message_external_id(event.id),
                "direction": direction,
                "text": text,
                "attachments": [],
            },
        }
        if not payload["client"]["name"]:
            payload["client"].pop("name", None)

        pager_post(payload)

    except Exception as e:
        print("TG->Pager ERROR:", repr(e))


# ========= Pager -> Telegram =========
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

    if not c_ext or not c_ext.startswith("tg_user:"):
        raise HTTPException(status_code=400, detail="missing/invalid client.externalId")

    peer_id = int(c_ext.split(":", 1)[1])

    try:
        sent = None
        if text:
            sent = await tg.send_message(peer_id, text)

        external_id = f"mtproto:{peer_id}:{getattr(sent, 'id', 'noid')}"
        return {"externalMessageId": external_id}

    except Exception as e:
        print("Pager->TG ERROR:", repr(e))
        raise HTTPException(status_code=500, detail="send failed")


# ========= Start chat by phone (write first) =========
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
