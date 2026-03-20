import os
import json
import logging
import httpx
import aiohttp
from datetime import datetime, timedelta

import config

logger = logging.getLogger("mia-dancing-cow")


def _is_valid_date(date_str: str) -> bool:
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
        return True
    except:
        return False


async def _classify_outcome(transcript: str, groq_api_key: str, model: str) -> dict:
    today = datetime.now().strftime('%Y-%m-%d')
    tomorrow = (datetime.now() + timedelta(days=1)).strftime('%Y-%m-%d')

    prompt = f"""
You are analyzing a sales call transcript for Dancing Cow (Oatish oat milk).

TRANSCRIPT:
{transcript}

Today's date: {today}

CLASSIFICATION RULES:

Hot_lead:
- Prospect showed clear interest OR
- Agreed to try/receive a sample OR
- Asked for next steps

sample_booked:
- true ONLY if they explicitly agreed to receive or try a sample

Warm_lead:
- Some interest but no clear commitment

Call_back:
- Prospect asked to be contacted at a specific time or date

Not_interested:
- Clearly declined

Voice_mail:
- No human conversation

Wrong_Number:
- Wrong person

SCHEDULED DATE RULES:
- If prospect said "tomorrow" → use {tomorrow}
- If prospect gave a specific date → convert to YYYY-MM-DD
- If unclear (e.g., "next week", "later") → return empty string ""
- If no callback requested → return empty string ""
- NEVER return natural language dates

STRICT OUTPUT FORMAT:
Return ONLY this JSON:
{{
  "Call_outcome": "Hot_lead" | "Warm_lead" | "Call_back" | "Not_interested" | "Voice_mail" | "Wrong_Number",
  "sample_booked": true | false,
  "ai_summary": "2-3 sentence summary of the call",
  "key_objection": "main objection raised or empty string",
  "Scheduled_date": "YYYY-MM-DD or empty string"
}}

If unsure between Hot_lead and Warm_lead → choose Warm_lead.
Return ONLY valid JSON. No explanation.
"""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {groq_api_key}",
                    "Content-Type": "application/json"
                },
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0,
                },
                timeout=15
            )
            result = resp.json()
            content = result["choices"][0]["message"]["content"].strip()
            if "```" in content:
                content = content.split("```")[1]
                if content.startswith("json"):
                    content = content[4:]
            result = json.loads(content.strip())
            # Validate Scheduled_date
            if result.get("Scheduled_date") and not _is_valid_date(result["Scheduled_date"]):
                result["Scheduled_date"] = ""
            return result
    except Exception as e:
        logger.error(f"Outcome classification failed: {e}")
        return {
            "Call_outcome": "Not_interested",
            "sample_booked": False,
            "ai_summary": "",
            "key_objection": "",
            "Scheduled_date": ""
        }


async def _post_call(reason, session, ctx, phone_number, lead_data, agent_instance=None):
    logger.info(f"Call ended. Reason: {reason}")

    # Extract transcript
    try:
        history = session.history
        transcript = "\n".join([
            f"{m.role.upper()}: {m.content}"
            for m in history.items
            if hasattr(m, 'content') and m.content
        ])
    except Exception as e:
        logger.error(f"Could not extract transcript: {e}")
        transcript = ""

    # If transcript is empty, likely voicemail or no answer
    if not transcript.strip():
        result = {
            "Call_outcome": "Voice_mail",
            "sample_booked": False,
            "ai_summary": "No conversation recorded. Likely voicemail or no answer.",
            "key_objection": "",
            "Scheduled_date": ""
        }
    else:
        result = await _classify_outcome(
            transcript=transcript,
            groq_api_key=os.getenv("GROQ_API_KEY", ""),
            model=config.GROQ_MODEL,
        )

    logger.info(f"Outcome: {result.get('Call_outcome')} | Sample: {result.get('sample_booked')}")

    webhook_url = os.getenv("WEBHOOK_URL", "")
    if not webhook_url:
        logger.warning("WEBHOOK_URL not set. Skipping post-call webhook.")
        return

    payload = {
        "phone": phone_number,
        "room_name": ctx.room.name,
        "call_id": lead_data.get("call_id", ctx.room.name),
        "cafe_name": lead_data.get("cafe_name", ""),
        "owner_name": lead_data.get("owner_name", ""),
        "city": lead_data.get("city", ""),
        "prospect_type": lead_data.get("prospect_type", ""),
        "airtable_record_id": lead_data.get("airtable_record_id", ""),
        "transcript": transcript,
        "Call_outcome": result.get("Call_outcome", "Not_interested"),
        "sample_booked": result.get("sample_booked", False),
        "ai_summary": result.get("ai_summary", ""),
        "key_objection": result.get("key_objection", ""),
        "Scheduled_date": result.get("Scheduled_date", ""),
        "status": "COMPLETED",
        "alternate_number": (agent_instance._captured_number if agent_instance else "") or "",
    }

    try:
        async with aiohttp.ClientSession() as http:
            async with http.post(
                webhook_url,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=10)
            ) as r:
                logger.info(f"Webhook sent → {r.status}")
    except Exception as e:
        logger.error(f"Webhook failed: {e}")


async def _post_call_direct(reason: str, ctx, phone_number, lead_data):
    webhook_url = os.getenv("WEBHOOK_URL", "")
    if not webhook_url:
        return
    outcome_map = {
        "invalid_number": "Invalid_Number",
        "no_answer": "Voice_mail",
    }
    payload = {
        "phone": phone_number,
        "room_name": ctx.room.name,
        "call_id": lead_data.get("call_id", ctx.room.name),
        "cafe_name": lead_data.get("cafe_name", ""),
        "owner_name": lead_data.get("owner_name", ""),
        "city": lead_data.get("city", ""),
        "prospect_type": lead_data.get("prospect_type", ""),
        "airtable_record_id": lead_data.get("airtable_record_id", ""),
        "transcript": "",
        "Call_outcome": outcome_map.get(reason, "Voice_mail"),
        "sample_booked": False,
        "ai_summary": "Call could not be connected.",
        "key_objection": "",
        "Scheduled_date": "",
        "status": "COMPLETED",
    }
    try:
        async with aiohttp.ClientSession() as http:
            async with http.post(
                webhook_url,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=10)
            ) as r:
                logger.info(f"Direct webhook sent → {r.status}")
    except Exception as e:
        logger.error(f"Direct webhook failed: {e}")
