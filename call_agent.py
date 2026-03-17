import os
import certifi
os.environ['SSL_CERT_FILE'] = certifi.where()

import logging
import json
import asyncio
import aiohttp
import httpx
import time
from dotenv import load_dotenv

from livekit import agents, api
from livekit.agents import AgentSession, Agent, RoomInputOptions
from livekit.plugins import openai, sarvam
from livekit.plugins.turn_detector.multilingual import MultilingualModel

load_dotenv(".env")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("mia-dancing-cow")

import config


class MiaAssistant(Agent):
    def __init__(self, prompt: str) -> None:
        super().__init__(instructions=prompt)
        self._last_user_text: str = ""
        self._last_response_time: float = 0.0
        self._is_speaking: bool = False          # 🔒 speaking lock

    async def llm_node(self, chat_ctx, tools, model_settings):

        # 🔒 Block if already speaking
        if self._is_speaking:
            logger.info("Skipped llm_node: agent is already speaking")
            return

        self._is_speaking = True

        try:
            # Deduplicate: skip if same user message processed within 5 seconds
            try:
                msgs = chat_ctx.messages() if callable(chat_ctx.messages) else list(chat_ctx.messages)
                last_user_msg = ""
                for msg in reversed(msgs):
                    if msg.role == "user":
                        last_user_msg = str(msg.content) if msg.content else ""
                        break

                now = time.monotonic()
                if (last_user_msg and
                        last_user_msg == self._last_user_text and
                        now - self._last_response_time < 5.0):
                    logger.info(f"Duplicate user turn suppressed: '{last_user_msg[:40]}'")
                    return

                self._last_user_text = last_user_msg
                self._last_response_time = now
            except Exception as e:
                logger.warning(f"Dedup check failed (non-critical): {e}")

            # Stream LLM output to terminal in real-time
            print("\n[LLM] ", end="", flush=True)
            async for chunk in super().llm_node(chat_ctx, tools, model_settings):
                try:
                    if chunk.choices and chunk.choices[0].delta.content:
                        print(chunk.choices[0].delta.content, end="", flush=True)
                except Exception:
                    pass
                yield chunk
            print()  # newline after full response

        finally:
            self._is_speaking = False            # 🔒 always release lock


async def _classify_outcome(transcript: str, groq_api_key: str, model: str) -> dict:
    prompt = f"""
You are analyzing a sales call transcript for Dancing Cow (Oatish oat milk).

TRANSCRIPT:
{transcript}

Return ONLY a JSON object with these exact fields:
{{
  "Call_outcome": "Hot_lead" | "Warm_lead" | "Call_back" | "Not_interested" | "Voice_mail" | "Wrong_Number",
  "sample_booked": true | false,
  "ai_summary": "2-3 sentence summary of the call",
  "key_objection": "main objection raised or empty string",
  "Scheduled_date": "ISO date if callback requested or empty string"
}}

Rules:
- Hot_lead: showed clear interest OR agreed to sample
- sample_booked: true only if they explicitly agreed to receive a sample
- Warm_lead: some interest but no commitment
- Call_back: asked to be called at specific time
- Not_interested: clearly declined
- Voice_mail: no human answered or voicemail picked up
- Wrong_Number: wrong person or number

Return ONLY the JSON. No explanation. No markdown.
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
            return json.loads(content.strip())
    except Exception as e:
        logger.error(f"Outcome classification failed: {e}")
        return {
            "Call_outcome": "Not_interested",
            "sample_booked": False,
            "ai_summary": "",
            "key_objection": "",
            "Scheduled_date": ""
        }


async def _post_call(reason, session, ctx, phone_number, lead_data):
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


async def entrypoint(ctx: agents.JobContext):
    logger.info(f"Room: {ctx.room.name}")

    phone_number = None
    lead_data = {}

    # Extract lead data from job metadata
    try:
        if ctx.job.metadata:
            data = json.loads(ctx.job.metadata)
            phone_number = data.get("phone_number")
            lead_data = data
    except Exception:
        pass

    # Also check room metadata
    try:
        if ctx.room.metadata:
            data = json.loads(ctx.room.metadata)
            phone_number = data.get("phone_number", phone_number)
            lead_data.update(data)
    except Exception:
        pass

    # Build dynamic prompt with prospect context
    cafe_name = lead_data.get("cafe_name", "")
    owner_name = lead_data.get("owner_name", "")
    city = lead_data.get("city", "")
    prospect_type = lead_data.get("prospect_type", "")

    context_block = ""
    if any([cafe_name, owner_name, city, prospect_type]):
        context_block = f"""
PROSPECT CONTEXT FOR THIS CALL:
Business Name: {cafe_name}
Contact Name: {owner_name}
City: {city}
Business Type: {prospect_type}

Only use these details if they have real values. Never say "Unknown" aloud.
"""

    dynamic_prompt = config.SYSTEM_PROMPT.replace(
        "PROSPECT_CONTEXT_PLACEHOLDER", context_block
    )

    # Create agent instance — stored so silence monitor can check _is_speaking
    agent_instance = MiaAssistant(prompt=dynamic_prompt)

    # Build agent session
    session = AgentSession(
        stt=sarvam.STT(
            model=config.STT_MODEL,
            language=config.STT_LANGUAGE,
            api_key=os.getenv("SARVAM_API_KEY"),
            high_vad_sensitivity=False,
        ),
        llm=openai.LLM(
            base_url="https://openrouter.ai/api/v1",
            api_key=os.getenv("OPENROUTER_API_KEY"),
            model=config.LLM_MODEL,
            temperature=config.LLM_TEMPERATURE,
        ),
        tts=sarvam.TTS(
            model=config.SARVAM_MODEL,
            speaker=config.SARVAM_VOICE,
            target_language_code=config.SARVAM_LANGUAGE,
        ),
        turn_detection=MultilingualModel(),
        allow_interruptions=True,
        min_interruption_duration=0.5,
        min_interruption_words=2,
        min_endpointing_delay=0.4,
    )

    await session.start(
        room=ctx.room,
        agent=agent_instance,                    # 👈 use stored reference
        room_input_options=RoomInputOptions(
            close_on_disconnect=True,
        ),
    )

    # ── END CALL: detect goodbye phrases ──────────────────────────
    async def _hang_up(delay: int = 3):
        await asyncio.sleep(delay)
        try:
            await ctx.api.room.delete_room(
                api.DeleteRoomRequest(room=ctx.room.name)
            )
            logger.info("Call ended by agent")
        except Exception as e:
            logger.error(f"Error hanging up: {e}")

    end_phrases = [
        "have a wonderful day", "have a great day", "take care",
        "goodbye", "good bye", "thank you for your time",
        "thanks for your time", "talk to you soon", "really appreciate it",
        "bye", "ok bye", "okay bye", "alvida", "theek hai bye",
        "dhanyavaad", "shukriya", "accha theek hai", "bilkul theek hai",
    ]

    def _extract_text(ev):
        text = ""
        if hasattr(ev, 'content'):
            if isinstance(ev.content, list):
                text = " ".join([c if isinstance(c, str) else "" for c in ev.content])
            else:
                text = str(ev.content)
        return text.lower()

    # ── SILENCE MONITOR ───────────────────────────────────────────
    _last_activity = [time.monotonic()]
    _silence_task: asyncio.Task = None
    _silence_generating = [False]

    async def silence_monitor():
        # Grace period — don't fire during/after greeting
        await asyncio.sleep(config.GREETING_GRACE_PERIOD_SECONDS)
        logger.info("Silence monitor active")

        while True:
            await asyncio.sleep(1)
            if time.monotonic() - _last_activity[0] > config.SILENCE_THRESHOLD_SECONDS:

                # 🔒 Don't fire if agent is already speaking or generating
                if _silence_generating[0] or agent_instance._is_speaking:
                    _last_activity[0] = time.monotonic()  # reset to avoid loop
                    continue

                _silence_generating[0] = True
                _last_activity[0] = time.monotonic()
                logger.info("Silence detected — re-engaging prospect")
                try:
                    await session.generate_reply(
                        instructions="The prospect has gone quiet. Say 'Hello, are you still there?' naturally and wait for their response."
                    )
                    _last_activity[0] = time.monotonic()
                except Exception as e:
                    logger.error(f"Re-engagement failed: {e}")
                finally:
                    _silence_generating[0] = False

    def on_agent_speech_committed(ev):
        try:
            _last_activity[0] = time.monotonic()
            text = _extract_text(ev)
            if any(phrase in text for phrase in end_phrases):
                logger.info(f"Agent end phrase detected: '{text[:50]}' — hanging up in 3s")
                asyncio.create_task(_hang_up(3))
        except Exception as e:
            logger.error(f"Error in agent speech committed handler: {e}")

    def on_user_speech_committed(ev):
        try:
            text = _extract_text(ev)
            stripped = text.strip()
            _last_activity[0] = time.monotonic()  # always reset, even for short speech
            word_count = len(stripped.split())
            if word_count < 2:
                logger.info(f"Too short to process ({word_count} words): '{stripped}'")
                return
            if any(phrase in stripped for phrase in end_phrases):
                logger.info(f"Customer end phrase detected: '{stripped[:50]}' — hanging up in 3s")
                asyncio.create_task(_hang_up(3))
        except Exception as e:
            logger.error(f"Error in user speech committed handler: {e}")

    session.on("agent_speech_committed", on_agent_speech_committed)
    session.on("user_speech_committed", on_user_speech_committed)

    # ── END CALL: max duration (4 minutes) ────────────────────────
    async def enforce_max_duration():
        await asyncio.sleep(config.MAX_CALL_DURATION_SECONDS)
        logger.info("Max call duration reached — hanging up")
        try:
            await ctx.api.room.delete_room(
                api.DeleteRoomRequest(room=ctx.room.name)
            )
        except Exception as e:
            logger.error(f"Error ending call at max duration: {e}")

    asyncio.create_task(enforce_max_duration())

    # ── POST CALL: fires when participant disconnects ──────────────
    _post_call_fired = False

    def on_participant_disconnected(participant):
        nonlocal _post_call_fired
        if _post_call_fired:
            return
        _post_call_fired = True
        if _silence_task:
            _silence_task.cancel()
        logger.info(f"Participant disconnected: {participant.identity}")
        asyncio.create_task(
            _post_call("participant_disconnected", session, ctx, phone_number, lead_data)
        )

    ctx.room.on("participant_disconnected", on_participant_disconnected)

    # ── DIAL OUT ──────────────────────────────────────────────────
    if phone_number:
        trunk_id = config.SIP_TRUNK_ID
        if not trunk_id:
            logger.error("SIP_TRUNK_ID is empty! Check VOBIZ_SIP_TRUNK_ID in .env")
            return

        logger.info(f"Dialing {phone_number} via trunk {trunk_id}...")
        try:
            await ctx.api.sip.create_sip_participant(
                api.CreateSIPParticipantRequest(
                    room_name=ctx.room.name,
                    sip_trunk_id=trunk_id,
                    sip_call_to=phone_number,
                    participant_identity=f"sip_{phone_number}",
                    wait_until_answered=True,
                )
            )
            logger.info("Call answered. Mia is speaking.")

            # Start silence monitor AFTER call is answered
            _silence_task = asyncio.create_task(silence_monitor())

            # Send greeting
            greeting = config.INITIAL_GREETING
            await session.generate_reply(instructions=greeting)
            # Don't reset _last_activity here — on_agent_speech_committed handles it

        except Exception as e:
            logger.error(f"Call failed: {e}")
            if _silence_task:
                _silence_task.cancel()
            await _post_call("call_failed", session, ctx, phone_number, lead_data)
    else:
        logger.warning("No phone number in metadata. Skipping dial.")


if __name__ == "__main__":
    agents.cli.run_app(
        agents.WorkerOptions(
            entrypoint_fnc=entrypoint,
            agent_name="outbound-caller",
        )
    )