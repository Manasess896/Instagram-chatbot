
# this si the fallback file if gemini fails or is ratre limited the code uses groq api and openai/gpt-oss-20B model

import os
import logging
from groq import Groq
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
fallback_client = None
if GROQ_API_KEY:
    try:
        fallback_client = Groq(api_key=GROQ_API_KEY)
        logging.info("200 Fallback AI service ready (Groq)")
    except Exception as e:
        logging.error("Fallback AI service failed: %s", e)
else:
    logging.warning("No Groq API key configured for fallback")

def generate_fallback_reply_with_context(user_id: str, user_text: str, history: list, system_prompt: str) -> str:


    friendly_failure = "Sorry — I'm having trouble replying right now. Please try again in a moment."

    if not fallback_client:
        return friendly_failure






    try:
        messages = [{"role": "system", "content": system_prompt}]
        
        for h in history:
            role = "user" if h["sender_type"] == "user" else "assistant"
            if h.get("conversation_id") == f"chat_{user_id}":
                messages.append({"role": role, "content": h["message"]})
                
        messages.append({"role": "user", "content": user_text})

        completion = fallback_client.chat.completions.create(
            model="openai/gpt-oss-20B", 
            messages=messages,
        )
        ai_text = completion.choices[0].message.content.strip()
        return ai_text or friendly_failure

    except Exception as e:
        logging.error("Fallback Groq Error: %s", e)
        return friendly_failure