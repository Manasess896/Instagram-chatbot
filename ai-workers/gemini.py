
#gemini  for generating AI response we are using gemini-2.5-flash model and if there is an error or rate limit we fallback to groq api and openai/gpt-oss-20B model in the fallback.py file

import logging
import os
from typing import Callable
from dotenv import load_dotenv
from google.genai import types
import media
from fallback import generate_fallback_reply_with_context




load_dotenv(override=True)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
# this is the limit of history conversations to be attached on the prompt being sent the more the limit the better the ai is relatable to the situatin but remmber there is a prompt wording  limit i think 
MEMORY_LIMIT = int(os.getenv("MEMORY_LIMIT", "4"))

gemini_client = media.gemini_client
if gemini_client:
    logger.info("200 AI service ready (Gemini)")
else:
    logger.error("No Gemini API key configured in media")


def generate_ai_reply_with_context(
    user_id: str,
    system_prompt: str,
    conversation_history: list,
    user_text: str = "",
    media_part=None,
    make_user_safe_error: Callable = None,
) -> str:
   
    
    default_reply = "Sorry — I'm having trouble replying right now. Please try again in a moment."
    
    if not gemini_client:
        return default_reply
    
    try:
        # Build message history for Gemini
        contents = []
        for item in conversation_history:
            role = "user" if item["sender_type"] == "user" else "model"
            contents.append({"role": role, "parts": [{"text": item["message"]}]})
        
        #add the  current user message
        user_parts = []
        if user_text:
            user_parts.append({"text": user_text})
        if media_part:
            user_parts.append(media_part)
        
        contents.append({"role": "user", "parts": user_parts})
        
        #i think the model is free with limit 
        completion = gemini_client.models.generate_content(
            model="gemini-2.5-flash",
            contents=contents,
            config=types.GenerateContentConfig(system_instruction=system_prompt),
        )
        
        ai_text = completion.text.strip()
        return ai_text or default_reply
        
    except Exception as exc:
        error_msg = str(exc)
        if "429" in error_msg or "RESOURCE_EXHAUSTED" in error_msg.upper() or "QUOTA" in error_msg.upper() or "503" in error_msg or "SERVICE UNAVAILABLE" in error_msg.upper():
            logger.warning("Gemini 429/503 error detected. Switching to fallback Groq AI.")
            fallback_history = [
                {
                    "sender_type": item.get("sender_type"),
                    "message": item.get("message", ""),
                }
                for item in conversation_history
            ]
            return generate_fallback_reply_with_context(
                user_id,
                user_text or "Media received.",
                fallback_history,
                system_prompt,
            )
        
        error_handler = make_user_safe_error or (lambda code: "An error occurred. Please try again later.")
        return error_handler("ERR400")
