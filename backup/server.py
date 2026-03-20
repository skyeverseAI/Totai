import os
import certifi
os.environ['SSL_CERT_FILE'] = certifi.where()

import json
import random
import logging
import asyncio
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional
from livekit import api

load_dotenv(".env")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("mia-server")

app = FastAPI(title="Mia - Dancing Cow Call Server")


class CallRequest(BaseModel):
    phone: str
    cafe_name: Optional[str] = ""
    owner_name: Optional[str] = ""
    city: Optional[str] = ""
    prospect_type: Optional[str] = ""
    airtable_record_id: Optional[str] = ""


@app.get("/health")
def health():
    return {"status": "ok", "agent": "Mia - Dancing Cow"}


@app.post("/make-call")
async def make_call(req: CallRequest):
    """
    Triggered by n8n Workflow 1 to fire an outbound call.
    Replaces the VAPI POST /call endpoint.
    """

    phone = req.phone.strip()
    if not phone.startswith("+"):
        raise HTTPException(status_code=400, detail="Phone must start with + and country code")

    url = os.getenv("LIVEKIT_URL")
    api_key = os.getenv("LIVEKIT_API_KEY")
    api_secret = os.getenv("LIVEKIT_API_SECRET")

    if not (url and api_key and api_secret):
        raise HTTPException(status_code=500, detail="LiveKit credentials missing")

    room_name = f"call-{phone.replace('+', '')}-{random.randint(1000, 9999)}"

    metadata = json.dumps({
        "phone_number": phone,
        "cafe_name": req.cafe_name,
        "owner_name": req.owner_name,
        "city": req.city,
        "prospect_type": req.prospect_type,
        "airtable_record_id": req.airtable_record_id,
        "call_id": room_name,
    })

    lk_api = api.LiveKitAPI(url=url, api_key=api_key, api_secret=api_secret)

    try:
        dispatch = await lk_api.agent_dispatch.create_dispatch(
            api.CreateAgentDispatchRequest(
                agent_name="outbound-caller",
                room=room_name,
                metadata=metadata,
            )
        )

        logger.info(f"Call dispatched → {phone} | Room: {room_name} | Dispatch: {dispatch.id}")

        return JSONResponse({
            "status": "queued",
            "phone": phone,
            "room_name": room_name,
            "dispatch_id": dispatch.id,
            "call_id": room_name,
        })

    except Exception as e:
        logger.error(f"Dispatch failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

    finally:
        await lk_api.aclose()


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("SERVER_PORT", 8090))
    uvicorn.run("server:app", host="0.0.0.0", port=port, reload=False)