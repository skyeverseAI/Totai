curl https://openrouter.ai/api/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer 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" \
  -H "HTTP-Referer: https://dancingcow.ai" \
  -d '{
    "model": "openai/gpt-4.1-mini",
    "messages": [{"role": "user", "content": "say hi"}]
  }'