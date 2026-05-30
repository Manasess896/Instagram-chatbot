# this file uses Groq api to create a small profile for user based on their recent messages and history this profile is then included in the prompt to give the info more info about the user before replying to give the ai more context 

import logging
import time
import os
from datetime import datetime, timezone
from typing import Any, cast
from groq import Groq
from google.genai import types

logger = logging.getLogger(__name__)

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
groq_client = None
if GROQ_API_KEY:
    try:
        groq_client = Groq(api_key=GROQ_API_KEY)
    except Exception as e:
        logger.error(f"Groq setup failed for user profile: {e}")


def _format_profile_facts(user_facts: dict, recent_messages: list, new_message: str, user_name: str | None) -> str:
    lines = []

    if user_name:
        lines.append(f"User name: {user_name}")
    elif user_facts.get("user_name"):
        lines.append(f"User name: {user_facts.get('user_name')}")

    instagram_username = user_facts.get("username")
    if instagram_username:
        lines.append(f"Instagram handle: @{instagram_username}")

    profile_pic = user_facts.get("profile_pic")
    if profile_pic:
        lines.append("Profile picture: available")

    message_count = user_facts.get("message_count")
    if message_count is not None:
        lines.append(f"Observed messages: {message_count}")

    if recent_messages:
        lines.append("Recent user messages:")
        for item in recent_messages:
            message_text = item.get("message") or ""
            message_type = item.get("message_type") or "text"
            lines.append(f"- {message_type}: {message_text}")

    if new_message:
        lines.append(f"Latest user message: {new_message}")

    return "\n".join(lines) if lines else "No structured user facts available."


def _generate_profile_summary_with_gemini(ai_client, system_instruction: str, prompt: str) -> str | None:
    if ai_client is None:
        return None

    try:
        completion = ai_client.models.generate_content(
            model="gemini-2.5-flash",
            contents=[{"role": "user", "parts": [{"text": prompt}]}],
            config=types.GenerateContentConfig(system_instruction=system_instruction),
        )
        content = getattr(completion, "text", "") or ""
        summary = content.strip()
        return summary or None
    except Exception as exc:
        logger.warning("Gemini profile update failed: %s", exc)
        return None


def update_user_profile_in_background(user_id: str, new_message: str, db, ai_client=None, user_name: str | None = None):
    if db is None:
        return

    #add a slight delay so it doesn't fire at the exact same millisecond as the main chat response you know so that they dont run at the same time 
    time.sleep(5)

    try:
        profile_collection = db["profiles"]
        facts_collection = db["user_info"]
        messages_collection = db["messages"]

        user_facts = facts_collection.find_one({"user_id": user_id}) or {}
        recent_messages = list(
            messages_collection.find({"user_id": user_id, "sender_type": "user"}).sort("timestamp", -1).limit(5)
        )
        recent_messages.reverse()

        system_instruction = (
            "You are a user profiler. Read structured user facts and recent messages, then write a concise paragraph "
            "summarizing the user's persona, name, interests, ongoing topics, and cultural context based on the data provided. "
            "Use only the supplied facts and do not invent details. "
            "Do not use bullet points or JSON. Only output the paragraph."
        )

        facts_text = _format_profile_facts(user_facts, recent_messages, new_message, user_name)
        prompt = f"Structured User Facts:\n{facts_text}"

        messages = cast(Any, [
            {"role": "system", "content": system_instruction},
            {"role": "user", "content": prompt}
        ])

        new_profile_summary = _generate_profile_summary_with_gemini(ai_client, system_instruction, prompt)

        if not new_profile_summary and groq_client is not None:
            completion = groq_client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=messages,
                temperature=0.3
            )

            content = completion.choices[0].message.content
            new_profile_summary = content.strip() if content else ""

        if not new_profile_summary:
            logger.warning("Skipping profile update for %s because no AI summary could be generated", user_id[-4:])
            return

        profile_collection.update_one(
            {"user_id": user_id},
            {
                "$set": {
                    "profile_summary": new_profile_summary,
                    "last_updated": datetime.now(timezone.utc),
                    "source": "user_info",
                }
            },
            upsert=True
        )

    except Exception as e:
        logger.error("Error updating user profile in background: %s", e)