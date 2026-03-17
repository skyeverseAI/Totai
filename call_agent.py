import os
import certifi
os.environ['SSL_CERT_FILE'] = certifi.where()

import logging
import json
import asyncio
import aiohttp
import httpx
import time
from datetime import datetime, timedelta
from dotenv import load_dotenv

from livekit import agents, api
from livekit.agents import AgentSession, Agent
from livekit.agents.voice.room_io import RoomOptions
from livekit.plugins import openai, deepgram, elevenlabs
# from livekit.plugins import openai, sarvam
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
        self.state: str = "greeting"             # greeting → intro → discovery → active

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

This information is about the prospect only. It is NOT your location.
Do NOT assume you are in the same city as the prospect.
Do NOT offer to visit or meet in person.
Only use these details if they have real values. Never say "Unknown" aloud.
"""

    dynamic_prompt = config.SYSTEM_PROMPT.replace(
        "PROSPECT_CONTEXT_PLACEHOLDER", context_block
    )

    # Create agent instance — stored so silence monitor can check _is_speaking
    agent_instance = MiaAssistant(prompt=dynamic_prompt)

    # Build agent session
    session = AgentSession(
        stt=deepgram.STT(
            model=config.DEEPGRAM_STT_MODEL,
            language=config.DEEPGRAM_STT_LANGUAGE,
            api_key=os.getenv("DEEPGRAM_API_KEY"),
        ),
        # stt=sarvam.STT(
        #     model=config.STT_MODEL,
        #     language=config.STT_LANGUAGE,
        #     api_key=os.getenv("SARVAM_API_KEY"),
        #     high_vad_sensitivity=False,
        # ),
        llm=openai.LLM(
            base_url="https://openrouter.ai/api/v1",
            api_key=os.getenv("OPENROUTER_API_KEY"),
            model=config.LLM_MODEL,
            temperature=config.LLM_TEMPERATURE,
        ),
        tts=elevenlabs.TTS(
            voice_id=config.ELEVENLABS_VOICE_ID,
            model=config.ELEVENLABS_MODEL,
            api_key=os.getenv("ELEVEN_API_KEY"),
            
        ),
        # tts=sarvam.TTS(
        #     model=config.SARVAM_MODEL,
        #     speaker=config.SARVAM_VOICE,
        #     target_language_code=config.SARVAM_LANGUAGE,
        # ),
        turn_detection=MultilingualModel(),
        allow_interruptions=True,
        min_interruption_duration=0.5,
        min_interruption_words=2,
        min_endpointing_delay=0.4,
    )

    await session.start(
        room=ctx.room,
        agent=agent_instance,
        room_options=RoomOptions(
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

    # Hard exit signals — immediate hang-up, no goodbye
    EXIT_SIGNALS = [
        "not interested", "don't call", "remove my number",
        "bilkul nahi chahiye", "band karo",
    ]

    def _extract_text(ev):
        text = ""
        if hasattr(ev, 'content'):
            if isinstance(ev.content, list):
                text = " ".join([c if isinstance(c, str) else "" for c in ev.content])
            else:
                text = str(ev.content)
        return text.lower()

    # ── STATE-CHAIN HELPER ─────────────────────────────────────────
    async def _handle_state_chain():
        if agent_instance.state == "greeting":
            agent_instance.state = "intro"
            await asyncio.sleep(0.4)          # give user a chance to interrupt
            if agent_instance._is_speaking:
                await asyncio.sleep(0.4)
                if agent_instance._is_speaking:
                    logger.info("Dropping chained reply — still speaking")
                    return
            asyncio.create_task(session.generate_reply(
                instructions="Introduce yourself as Mia from Dancing Cow and mention you'll be brief. One sentence."
            ))
        elif agent_instance.state == "intro":
            agent_instance.state = "discovery"
            await asyncio.sleep(0.4)          # give user a chance to interrupt
            if agent_instance._is_speaking:
                await asyncio.sleep(0.4)
                if agent_instance._is_speaking:
                    logger.info("Dropping chained reply — still speaking")
                    return
            asyncio.create_task(session.generate_reply(
                instructions="Ask a natural question about whether they use plant-based milk. One question only."
            ))

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

            # State chaining — fires at the TOP before any other logic
            asyncio.create_task(_handle_state_chain())

            # Polite goodbye → hang up
            if any(phrase in text for phrase in end_phrases):
                logger.info(f"Agent end phrase: '{text[:50]}' — hanging up in 3s")
                asyncio.create_task(_hang_up(3))

        except Exception as e:
            logger.error(f"Error in agent speech committed handler: {e}")

    def on_user_speech_committed(ev):
        try:
            text = _extract_text(ev)
            stripped = text.strip()
            _last_activity[0] = time.monotonic()

            # 🔒 Don't process if agent is already speaking
            if agent_instance._is_speaking:
                logger.info("User spoke while agent speaking — ignoring")
                return

            # User interrupted opening — jump straight to active mode
            if agent_instance.state in ["greeting", "intro"]:
                agent_instance.state = "active"
                logger.info("User interrupted opening — switching to active mode")

            # Discovery → active once user gives a real reply
            if agent_instance.state == "discovery":
                if len(stripped.split()) >= 2:
                    agent_instance.state = "active"

            word_count = len(stripped.split())
            if word_count < 2:
                logger.info(f"Too short to process ({word_count} words): '{stripped}'")
                return

            # Hard exit signals → immediate hang-up
            if any(signal in stripped for signal in EXIT_SIGNALS):
                logger.info(f"Exit signal: '{stripped[:50]}' — hanging up immediately")
                asyncio.create_task(_hang_up(1))
                return

            # Polite goodbye → hang up with delay
            if any(phrase in stripped for phrase in end_phrases):
                logger.info(f"Customer end phrase: '{stripped[:50]}' — hanging up in 3s")
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

            # ── STATE-CHAINED OPENING ──────────────────────────────
            agent_instance.state = "greeting"
            await session.generate_reply(
                instructions=(
                    f"Ask if you are speaking with {owner_name}. One sentence only."
                    if owner_name else
                    "Introduce yourself as Mia from Dancing Cow and mention you'll be brief. One sentence only."
                )
            )
            # on_agent_speech_committed will chain → intro → discovery

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