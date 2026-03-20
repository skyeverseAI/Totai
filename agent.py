import time
import logging

from livekit.agents import Agent, function_tool

logger = logging.getLogger("mia-dancing-cow")


class MiaAssistant(Agent):
    def __init__(self, prompt: str) -> None:
        super().__init__(instructions=prompt)
        self._last_user_text: str = ""
        self._last_response_time: float = 0.0
        self._is_speaking: bool = False          # 🔒 speaking lock
        self.state: str = "greeting"             # greeting → intro → discovery → active
        self._hangup_called: bool = False
        self.conversion_stage: str = "none"      # none → interested → confirmed
        self._captured_number = None

    @function_tool
    async def capture_owner_number(self, number: str) -> str:
        """Call this when staff or someone provides the owner's or
        manager's phone number to call instead."""
        self._captured_number = number
        logger.info(f"Captured owner number: {number}")
        return f"Got it, I've noted the number {number}. I'll reach out to them directly."

    async def llm_node(self, chat_ctx, tools, model_settings):

        # 🔒 Block if already speaking
        if self._is_speaking:
            logger.info("Skipped llm_node: agent is already speaking")
            return

        self._is_speaking = True

        try:
            # Deduplicate: skip if same user message processed within 8 seconds
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
                        now - self._last_response_time < 8.0):
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
