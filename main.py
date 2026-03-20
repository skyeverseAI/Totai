import os
import certifi
os.environ['SSL_CERT_FILE'] = certifi.where()

import logging
import json
import asyncio
import time
from datetime import datetime
from dotenv import load_dotenv

from livekit import agents, api
from livekit.agents import AgentSession
from livekit.agents.voice.room_io import RoomOptions
from livekit.plugins.turn_detector.multilingual import MultilingualModel

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"), override=True)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("mia-dancing-cow")

import config

from agent import MiaAssistant
from providers import build_stt, build_tts, build_llm
from postcall import _post_call, _post_call_direct
from utils import _is_ending, _extract_text


def _is_valid_date(date_str: str) -> bool:
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
        return True
    except:
        return False


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
    gender_form = "रही" if config.AGENT_GENDER == "female" else "रहा"
    dynamic_prompt = dynamic_prompt.replace(
        "AGENT_GENDER_PLACEHOLDER", gender_form
    )

    # Create agent instance — stored so silence monitor can check _is_speaking
    agent_instance = MiaAssistant(prompt=dynamic_prompt)

    # Build agent session
    session = AgentSession(
        stt=build_stt(),
        llm=build_llm(),
        tts=build_tts(),
        turn_detection=MultilingualModel(),
        allow_interruptions=True,
        min_interruption_duration=0.5,
        min_interruption_words=3,
        min_endpointing_delay=0.7,
    )

    await session.start(
        room=ctx.room,
        agent=agent_instance,
        room_options=RoomOptions(
            close_on_disconnect=True,
        ),
    )

    async def _shutdown_post_call():
        if not _post_call_fired[0]:
            _post_call_fired[0] = True
            await _post_call("agent_hangup", session, ctx, phone_number, lead_data, agent_instance)
    ctx.add_shutdown_callback(_shutdown_post_call)

    # ── END CALL: detect goodbye phrases ──────────────────────────
    async def _hang_up(delay: int = 3):
        await asyncio.sleep(delay)
        if not _post_call_fired[0]:
            _post_call_fired[0] = True
            await _post_call("agent_hangup", session, ctx, phone_number, lead_data, agent_instance)
        try:
            await ctx.api.room.delete_room(
                api.DeleteRoomRequest(room=ctx.room.name)
            )
            logger.info("Call ended by agent")
        except Exception as e:
            logger.error(f"Error hanging up: {e}")

    async def _complete_and_hangup():
        if agent_instance._hangup_called:
            return
        agent_instance._hangup_called = True
        logger.info("Conversion complete — closing call in 5s")
        await asyncio.sleep(5)
        try:
            await ctx.api.room.delete_room(
                api.DeleteRoomRequest(room=ctx.room.name)
            )
        except Exception as e:
            logger.error(f"Hangup failed: {e}")
        if not _post_call_fired[0]:
            _post_call_fired[0] = True
            await _post_call("call_completed", session, ctx, phone_number, lead_data, agent_instance)

    # Hard exit signals — immediate hang-up, no goodbye
    EXIT_SIGNALS = [
        "not interested", "don't call", "remove my number",
        "bilkul nahi chahiye", "band karo",
        "काट दो", "phone काट दो", "band kar do", "rakh do",
    ]

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

    def on_conversation_item_added(ev):
        try:
            _last_activity[0] = time.monotonic()

            # Only process assistant messages
            if not hasattr(ev, 'item') or ev.item.role != "assistant":
                return

            # Extract text from assistant message
            content = ev.item.content
            if isinstance(content, str):
                text = content.lower()
            elif isinstance(content, list):
                text = " ".join([
                    c if isinstance(c, str) else (c.text if hasattr(c, 'text') else "")
                    for c in content
                ]).lower()
            else:
                text = str(content).lower()

            logger.info(f"Agent message — text: '{text[:80]}'")

            # Agent said goodbye — hang up
            if any(phrase in text for phrase in [
                "thanks for your time", "really appreciate it", "take care", "bye"
            ]):
                if not agent_instance._hangup_called:
                    agent_instance._hangup_called = True
                    if _silence_task:
                        _silence_task.cancel()
                    logger.info(f"Agent goodbye — hanging up: '{text[:50]}'")
                    asyncio.ensure_future(_hang_up(3))
        except Exception as e:
            logger.error(f"Error in conversation_item_added handler: {e}")

    def on_user_speech_committed(ev):
        try:
            text = _extract_text(ev)
            stripped = text.strip()
            _last_activity[0] = time.monotonic()

            # Chain next opening state after user responds
            if agent_instance.state in ["greeting", "intro"]:
                asyncio.create_task(_handle_state_chain())

            # 🔒 Don't process if agent is already speaking
            if agent_instance._is_speaking:
                logger.info("User spoke while agent speaking — ignoring")
                return

            # Hard exit signals → immediate hang-up (before word count filter)
            if any(signal in stripped for signal in EXIT_SIGNALS):
                logger.info(f"Exit signal: '{stripped[:50]}' — hanging up immediately")
                asyncio.ensure_future(_hang_up(1))
                return

            # Stage 1 — Interest detected
            INTEREST_SIGNALS = [
                "we can try", "can try", "try kar", "try karenge",
                "send it", "send the sample", "bhejo", "kar lenge",
                "okay send", "yes send", "sure", "open to it",
                "we are open", "open to trying", "interested",
                "would like to try", "we'll try", "let's try",
                "कर लेंगे", "भेजिए", "ठीक है", "हाँ",
            ]
            # Stage 2 — Address/logistics confirmed
            ADDRESS_SIGNALS = [
                "cafe address", "cafe only", "drop it", "cafe pe",
                "send to cafe", "at the cafe", "our address",
                "whatsapp", "whatsapp you", "whatsapp the details",
                "find it on google", "google pe", "same number",
                "this number", "send it here",
                "काफे पर", "यहाँ भेजो", "address",
            ]
            if agent_instance.conversion_stage == "none":
                if any(s in stripped for s in INTEREST_SIGNALS):
                    agent_instance.conversion_stage = "interested"
                    logger.info("Conversion stage: interested")
            elif agent_instance.conversion_stage == "interested":
                if any(s in stripped for s in ADDRESS_SIGNALS):
                    agent_instance.conversion_stage = "confirmed"
                    logger.info("Conversion stage: confirmed — triggering hangup")
                    asyncio.create_task(_complete_and_hangup())

            # Polite goodbye → hang up with delay (before word count filter)
            if _is_ending(stripped):
                logger.info(f"Customer end phrase: '{stripped[:50]}' — hanging up in 3s")
                if _silence_task:
                    _silence_task.cancel()
                asyncio.ensure_future(_hang_up(3))
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

        except Exception as e:
            logger.error(f"Error in user speech committed handler: {e}")

    session.on("conversation_item_added", on_conversation_item_added)
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
    _post_call_fired = [False]

    def on_participant_disconnected(participant):
        if _post_call_fired[0]:
            return
        if _silence_task:
            _silence_task.cancel()
        logger.info(f"Participant disconnected: {participant.identity}")

        async def _delayed_post_call():
            await asyncio.sleep(2)
            if _post_call_fired[0]:
                return
            _post_call_fired[0] = True
            await _post_call("participant_disconnected", session, ctx, phone_number, lead_data, agent_instance)

        asyncio.create_task(_delayed_post_call())

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
            error_str = str(e).lower()
            if "404" in error_str or "not found" in error_str:
                logger.info("SIP 404 — Invalid number")
                await _post_call_direct("invalid_number", ctx, phone_number, lead_data)
            else:
                logger.info("SIP error — marking as Voice_Mail")
                await _post_call_direct("no_answer", ctx, phone_number, lead_data)
            _post_call_fired[0] = True
    else:
        logger.warning("No phone number in metadata. Skipping dial.")


if __name__ == "__main__":
    agents.cli.run_app(
        agents.WorkerOptions(
            entrypoint_fnc=entrypoint,
            agent_name="outbound-caller",
        )
    )
