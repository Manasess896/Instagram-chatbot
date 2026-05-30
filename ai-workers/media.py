# here lays the code responsible for image scanning, reading, and Gemini-based media analysis.
# this file converts the sent image to bytes and forwards it to the Gemini model for analysis uses gemini-2.5-flash model to anaylze the images 


import hashlib
import logging
import os
import uuid
from datetime import datetime, timezone
import requests
from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv(override=True)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
MEDIA_LIMIT = int(os.getenv("MEDIA_LIMIT", "15"))
CLOUDINARY_CLOUD_NAME = os.getenv("CLOUDINARY_CLOUD_NAME", "")
CLOUDINARY_API_KEY = os.getenv("CLOUDINARY_API_KEY", "")
CLOUDINARY_API_SECRET = os.getenv("CLOUDINARY_API_SECRET", "")
CLOUDINARY_FOLDER = os.getenv("CLOUDINARY_FOLDER", "instagram-media")


gemini_client = None
if GEMINI_API_KEY:
    gemini_client = genai.Client(api_key=GEMINI_API_KEY)
else:
    logger.error("GEMINI_API_KEY not set")


def _download_media_source(media_source: str, return_mime: bool = False):
    try:
        if media_source.startswith(("http://", "https://")):
            response = requests.get(media_source, timeout=20)
            response.raise_for_status()
            mime_type = response.headers.get("Content-Type", "application/octet-stream")
            if return_mime:
                return response.content, mime_type
            return response.content

        access_token = os.getenv("ACCESS_TOKEN") or os.getenv("PAGE_ACCESS_TOKEN") or os.getenv("WHATSAPP_TOKEN")
        headers = {"Authorization": f"Bearer {access_token}"} if access_token else {}
        url = f"https://graph.facebook.com/v21.0/{media_source}"
        response = requests.get(url, headers=headers, timeout=20)
        response.raise_for_status()
        media_data = response.json()
        media_url = media_data.get("url")
        mime_type = media_data.get("mime_type", "")
        file_size = media_data.get("file_size", 0)
        #  set max file size to 5mb 
        max_size_bytes = 5 * 1024 * 1024 

        if file_size and file_size > max_size_bytes:
            logger.warning("Media size %s exceeds 5MB limit.", file_size)
            return (None, None) if return_mime else None

        if not media_url:
            return (None, None) if return_mime else None

        media_response = requests.get(media_url, headers=headers, timeout=20)
        media_response.raise_for_status()
        if return_mime:
            return media_response.content, mime_type or media_response.headers.get("Content-Type", "application/octet-stream")
        return media_response.content
    except Exception as exc:
        logger.error("Error downloading Instagram media: %s", exc)
        return (None, None) if return_mime else None


def _upload_media_to_cloudinary(media_content: bytes, mime_type: str) -> str | None:
    if not CLOUDINARY_CLOUD_NAME or not CLOUDINARY_API_KEY or not CLOUDINARY_API_SECRET:
        return None

    timestamp = str(int(datetime.now(timezone.utc).timestamp()))
    public_id = uuid.uuid4().hex
    upload_url = f"https://api.cloudinary.com/v1_1/{CLOUDINARY_CLOUD_NAME}/auto/upload"

    signature_parts = [f"folder={CLOUDINARY_FOLDER}", f"public_id={public_id}", f"timestamp={timestamp}"]
    signature_base = "&".join(signature_parts)
    signature = hashlib.sha1(f"{signature_base}{CLOUDINARY_API_SECRET}".encode("utf-8")).hexdigest()

    try:
        response = requests.post(
            upload_url,
            data={
                "api_key": CLOUDINARY_API_KEY,
                "folder": CLOUDINARY_FOLDER,
                "public_id": public_id,
                "signature": signature,
                "timestamp": timestamp,
            },
            files={"file": (f"{public_id}", media_content, mime_type)},
            timeout=120,
        )
    except Exception as exc:
        logger.error("Cloudinary media upload failed: %s", exc)
        return None

    if response.status_code >= 400:
        logger.error("Cloudinary media upload failed (%s): %s", response.status_code, response.text)
        return None

    try:
        payload = response.json()
    except Exception:
        logger.error("Cloudinary media upload returned a non-JSON response")
        return None

    secure_url = payload.get("secure_url") if isinstance(payload, dict) else None
    if isinstance(secure_url, str) and secure_url:
        return secure_url

    logger.error("Cloudinary media upload response did not include a secure_url")
    return None


def get_media_part(media_source, return_mime=False):
    downloaded = _download_media_source(media_source, return_mime=True)
    if not downloaded:
        return None

    media_content, mime_type = downloaded
    if not isinstance(media_content, bytes) or not media_content:
        return None

    if not isinstance(mime_type, str) or not mime_type:
        mime_type = "application/octet-stream"

    try:
        cloudinary_url = _upload_media_to_cloudinary(media_content, mime_type)
        if cloudinary_url:
            return types.Part.from_uri(file_uri=cloudinary_url, mime_type=mime_type)

        return types.Part.from_bytes(data=media_content, mime_type=mime_type)
    except Exception as exc:
        logger.error("Error creating Part from media: %s", exc)
        return None


def analyze_media_with_context(
    user_id: str,
    system_prompt: str,
    conversation_history: list,
    media_part,
    user_text: str = "",
    make_user_safe_error=None,
) -> str:
    friendly_failure = "Sorry — I'm having trouble replying right now. Please try again in a moment."

    if not gemini_client or media_part is None:
        return friendly_failure

    try:
        contents = []
        for item in conversation_history:
            role = "user" if item.get("sender_type") == "user" else "model"
            contents.append({"role": role, "parts": [{"text": item.get("message", "")} ]})

        user_parts = []
        if user_text:
            user_parts.append({"text": user_text})
        user_parts.append(media_part)

        contents.append({"role": "user", "parts": user_parts})

        completion = gemini_client.models.generate_content(
            model="gemini-2.5-flash",
            contents=contents,
            config=types.GenerateContentConfig(system_instruction=system_prompt),
        )

        ai_text = (completion.text or "").strip()
        return ai_text or friendly_failure

    except Exception as exc:
        error_msg = str(exc)
        if "429" in error_msg or "RESOURCE_EXHAUSTED" in error_msg.upper() or "QUOTA" in error_msg.upper() or "503" in error_msg or "SERVICE UNAVAILABLE" in error_msg.upper():
            logger.warning("Gemini media analysis hit rate/quota limits.")

        if make_user_safe_error:
            return make_user_safe_error("ERR400")
        return friendly_failure


def check_media_rate_limit(user_id, db_collection):
    if db_collection is None:
        return True

    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    count = db_collection.count_documents(
        {
            "user_id": user_id,
            "message_type": {"$in": ["image", "document", "audio", "video", "share", "reel"]},
            "timestamp": {"$gte": today},
        }
    )

    return count < MEDIA_LIMIT