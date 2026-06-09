# this is the main file it routes to different files depending on ction and message type 


import importlib.util
import hashlib
import logging
import re
import sys
import threading
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_from_directory
from copy import deepcopy
import os
import requests
from pymongo import MongoClient
from pymongo.errors import ConnectionFailure, DuplicateKeyError, ServerSelectionTimeoutError



load_dotenv(override=True)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)



WORKERS_DIR = Path(__file__).resolve().parent / "ai-workers"
PROMPT_DIR = Path(__file__).resolve().parent / "prompt"






def _load_worker_module(module_name: str):
    module_path = WORKERS_DIR / f"{module_name}.py"
    if not module_path.exists():
        raise FileNotFoundError(f"Missing worker module: {module_path}")
    existing_module = sys.modules.get(module_name)
    if existing_module is not None:
        return existing_module
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Unable to load worker module: {module_name}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module






def _safe_load_worker(module_name: str):
    try:
        return _load_worker_module(module_name)
    except FileNotFoundError:
        logger.warning("Worker module %s not found; disabling related features.", module_name)
        return None
    except Exception as exc:
        dev_log(exc, f"ERR_LOAD_{module_name.upper()}")
        logger.error("Failed to load worker module %s: %s", module_name, exc)
        return None



# routing for different files 
media = _safe_load_worker("media")
_fallback_mod = _safe_load_worker("fallback")
generate_fallback_reply_with_context = getattr(_fallback_mod, "generate_fallback_reply_with_context", None)
gemini = _safe_load_worker("gemini")
_image_mod = _safe_load_worker("image")
generate_image_bytes = getattr(_image_mod, "generate_image_bytes", None)
_profile_mod = _safe_load_worker("profile")
update_user_profile_in_background = getattr(_profile_mod, "update_user_profile_in_background", None)





#load .env
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
PAGE_ACCESS_TOKEN = os.getenv("ACCESS_TOKEN") or os.getenv("PAGE_ACCESS_TOKEN")
GRAPH_API_VERSION = os.getenv("GRAPH_API_VERSION", "v21.0")
BOT_NAME = os.getenv("BOT_NAME", "Instagram Assistant")
INSTAGRAM_ID = os.getenv("INSTAGRAM_ID", "17841413974613580")
CREATOR_NAME = os.getenv("CREATOR_NAME", "the creator")
CREATOR_EMAIL = os.getenv("CREATOR_EMAIL")
PRIVACY_URL = os.getenv("PRIVACY_URL", "")
TERMS_URL = os.getenv("TERMS_URL", "")
CREATOR_CONTACTFORM = os.getenv("CREATOR_CONTACTFORM", "NO INFO")
MEMORY_LIMIT = int(os.getenv("MEMORY_LIMIT", "4"))
MONGO_URI = os.getenv("MONGODB_URI")
DATABASE_NAME = os.getenv("DATABASE_NAME", "instagram_bot")
COLLECTION_NAME = os.getenv("COLLECTION_NAME", "messages")
GENERATED_IMAGES_DIR = Path(__file__).resolve().parent / "generated-images"
GENERATED_IMAGES_DIR.mkdir(parents=True, exist_ok=True)
CLOUDINARY_CLOUD_NAME = os.getenv("CLOUDINARY_CLOUD_NAME", "")
CLOUDINARY_API_KEY = os.getenv("CLOUDINARY_API_KEY", "")
CLOUDINARY_API_SECRET = os.getenv("CLOUDINARY_API_SECRET", "")
CLOUDINARY_FOLDER = os.getenv("CLOUDINARY_FOLDER", "instagram-bot")
IMAGE_LIMIT = int(os.getenv("IMAGE_LIMIT", "1"))






#define erroor messages 
ERROR_MESSAGES = {
    "ERR100": "I encountered a problem when processing your request. Please tell the developer: ERR100.",
    "ERR200": "I encountered a problem when processing your request. Please tell the developer: ERR200.",
    "ERR300": "I encountered a problem when processing your request. Please tell the developer: ERR300.",
    "ERR400": "Sorry — I'm having trouble replying right now. Please try again in a moment.",
}



def dev_log(exc: Exception, code: str) -> None:
    logger.error("Developer error %s: %s", code, exc)
    logger.error(traceback.format_exc())


def make_user_safe_error(code_key: str) -> str:
    return ERROR_MESSAGES.get(code_key, "An error occurred. Please inform the developer.")


#initaianlize database 
mongo_client = None
db = None
collection = None
processed_messages_collection = None
user_info_collection = None


if MONGO_URI:
    try:
        mongo_client = MongoClient(
            MONGO_URI,
            serverSelectionTimeoutMS=15000,
            connectTimeoutMS=15000,
            socketTimeoutMS=15000,
            maxPoolSize=10,
            retryWrites=True,
        )
        mongo_client.admin.command("ping")
        db = mongo_client[DATABASE_NAME]
        collection = db[COLLECTION_NAME]
        processed_messages_collection = db["processed_messages"]
        user_info_collection = db["user_info"]
        logger.info("200 Database connected")
    except (ConnectionFailure, ServerSelectionTimeoutError) as exc:
        dev_log(exc, "ERRDB_CONN")
        logger.error("mongoDB connection failed; memory and profile features disabled.")
        collection = None
        processed_messages_collection = None
        user_info_collection = None
    except Exception as exc:
        dev_log(exc, "ERRDB_CONN")
        logger.error("MongoDB connection error; memory and profile features disabled.")
        collection = None
        processed_messages_collection = None
        user_info_collection = None
else:
    logger.warning("MONGO_URI not set in .env; memory features disabled.")


def fetch_instagram_profile(igsid: str) -> dict | None:
    #   fetch user profile info from instagram and save to database
    if not igsid or not PAGE_ACCESS_TOKEN:
        return None

    url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{igsid}"
    params = {"fields": "username,name,profile_pic", "access_token": PAGE_ACCESS_TOKEN}
    try:
        resp = requests.get(url, params=params, timeout=10)
        if resp.status_code != 200:
            logger.debug("Profile fetch failed (%s): %s", resp.status_code, resp.text)
            return None

        data = resp.json()
        if user_info_collection is not None and data:
            now = datetime.now(timezone.utc)
            upsert_doc = {
                "user_id": igsid,
                "username": data.get("username"),
                "name": data.get("name"),
                "profile_pic": data.get("profile_pic"),
                "fetched_at": now,
            }
            try:
                user_info_collection.update_one({"user_id": igsid}, {"$set": upsert_doc}, upsert=True)
            except Exception as exc:
                dev_log(exc, "ERR_PROFILE_UPSERT")
        return data
    except Exception as exc:
        dev_log(exc, "ERR_PROFILE_FETCH")
        return None


def save_user_info(
    user_id: str,
    message_text: str,
    sender_type: str,
    user_name: str | None = None,
    message_type: str = "text",
) -> bool:
    if user_info_collection is None:
        return False

    try:
        now = datetime.now(timezone.utc)
        existing_facts = user_info_collection.find_one({"user_id": user_id}) or {}
        recent_messages = list(existing_facts.get("recent_messages", []))
        recent_messages.append(
            {
                "sender_type": sender_type,
                "message_type": message_type,
                "message": message_text,
                "timestamp": now,
            }
        )
# this info fetched about the current instagram user and generating fact and gets more info on the user 
        facts_doc = {
            "user_id": user_id,
            "user_name": user_name or existing_facts.get("user_name"),
            "last_sender_type": sender_type,
            "message_count": int(existing_facts.get("message_count", 0)) + 1,
            "first_seen": existing_facts.get("first_seen", now),
            "updated_at": now,
        }

        user_info_collection.update_one({"user_id": user_id}, {"$set": facts_doc}, upsert=True)
        return True
    except Exception as exc:
        dev_log(exc, "ERR_PROFILE_FACTS")
        logger.error("Failed to save profile facts for user %s", user_id[-4:])
        return False


def should_update_user_profile(user_id: str, interval: int = 5) -> bool:
    if user_info_collection is None or update_user_profile_in_background is None:
        return False

    try:
        user_info = user_info_collection.find_one({"user_id": user_id}, {"message_count": 1}) or {}
        message_count = int(user_info.get("message_count", 0))
        return message_count > 0 and message_count % interval == 0
    except Exception as exc:
        dev_log(exc, "ERR_PROFILE_CHECK")
        return False

def save_message_to_db(
    user_id: str,
    message: str,
    sender_type: str,
    message_type: str = "text",
    user_name: str | None = None,
) -> bool:
    if collection is None:
        logger.info("DB disabled, skipping save.")
        return False

    try:
        doc = {
            "user_id": user_id,
            "message": message,
            "sender_type": sender_type,
            "message_type": message_type,
            "timestamp": datetime.now(timezone.utc),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "user_name": user_name,
            "platform_user_id": user_id,
            "conversation_id": f"chat_{user_id}",
        }
        collection.insert_one(doc)
        logger.info("200 Saved message to database")
        return True
    except Exception as exc:
        dev_log(exc, "ERR100")
        logger.error("Failed to save message for user %s", user_id)
        return False


def check_image_rate_limit(user_id: str) -> bool:
    if collection is None:
        return True

    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    count = collection.count_documents(
        {
            "user_id": user_id,
            "sender_type": "bot",
            "message_type": "image",
            "timestamp": {"$gte": today},
        }
    )
    return count < IMAGE_LIMIT


def get_conversation_history(user_id: str, limit: int | None = None) -> list:
    if collection is None:
        logger.info("DB disabled: returning empty history.")
        return []

    try:
        actual_limit = limit or MEMORY_LIMIT
        query = {"user_id": user_id, "conversation_id": f"chat_{user_id}"}
        records = list(collection.find(query).sort("timestamp", -1).limit(actual_limit))

        history = []
        for record in reversed(records):
            if record.get("user_id") == user_id:
                history.append(
                    {
                        "sender_type": record.get("sender_type"),
                        "message": record.get("message"),
                        "timestamp": record.get("timestamp"),
                        "user_name": record.get("user_name"),
                        "conversation_id": record.get("conversation_id"),
                    }
                )

        return history
    except Exception as exc:
        dev_log(exc, "ERR200")
        logger.error("Failed to retrieve conversation history for user %s", user_id[-4:])
        return []


def get_user_stats(user_id: str) -> dict:
    if collection is None:
        return {"error": "Database disabled"}

    try:
        user_filter = {"user_id": user_id}
        total_messages = collection.count_documents(user_filter)
        user_messages = collection.count_documents({**user_filter, "sender_type": "user"})
        bot_messages = collection.count_documents({**user_filter, "sender_type": "bot"})
        first_msg = collection.find_one(user_filter, sort=[("timestamp", 1)])
        last_msg = collection.find_one(user_filter, sort=[("timestamp", -1)])
        user_info = collection.find_one(
            {**user_filter, "user_name": {"$exists": True, "$ne": None}},
            sort=[("timestamp", -1)],
        )

        return {
            "user_id": user_id,
            "user_name": user_info.get("user_name") if user_info else "Unknown",
            "platform_user_id": user_info.get("platform_user_id") if user_info else user_id,
            "total_messages": total_messages,
            "user_messages": user_messages,
            "bot_messages": bot_messages,
            "first_message": first_msg.get("timestamp") if first_msg else None,
            "last_message": last_msg.get("timestamp") if last_msg else None,
            "conversation_id": f"chat_{user_id}",
        }
    except Exception as exc:
        logger.error("Error getting user stats for %s: %s", user_id[-4:], exc)
        return {"error": str(exc)}


def get_all_users() -> list:
    if collection is None:
        return []

    try:
        users = []
        for user_id in collection.distinct("user_id"):
            stats = get_user_stats(user_id)
            if "error" not in stats:
                users.append(stats)

        return sorted(users, key=lambda item: item.get("last_message", ""), reverse=True)
    except Exception as exc:
        logger.error("Error getting all users: %s", exc)
        return []


def is_first_time_user(user_id: str) -> bool:
    if collection is None:
        return True

    try:
        return collection.count_documents({"user_id": user_id}) == 0
    except Exception:
        return True


def is_message_already_processed(message_id: str) -> bool:
# prevent duplicates 
    if processed_messages_collection is None:
        return False

    try:
        existing = processed_messages_collection.find_one({"_id": message_id})
        return existing is not None
    except Exception:
        return False


def mark_message_as_processed(message_id: str) -> bool:
# mark already replied to messages i noticed the bot was sending the messages it had already replied to it so now it has to check if has already done that before that 
    if processed_messages_collection is None:
        return True

    try:
        processed_messages_collection.insert_one({
            "_id": message_id,
            "processed_at": datetime.now(timezone.utc),
        })
        return True
    except DuplicateKeyError:
        return False
    except Exception as exc:
        logger.warning("Failed to record processed message %s: %s", message_id[:8], exc)
        return True


# prompt our prompts are currently in prompt folder so we have to import them to the code this makes it easier to change the prompt 
def _load_instruction_template(filename: str, fallback: str) -> str:
    try:
        with open(PROMPT_DIR / filename, "r", encoding="utf-8") as file_handle:
            return file_handle.read()
    except Exception:
        return fallback


def build_system_prompt(user_id: str | None = None, prompt_file: str = "dm.md") -> str:
    base_template = _load_instruction_template(
        prompt_file,
        "You are {BOT_NAME}, a helpful Instagram DM assistant created by {CREATOR_NAME}.\n",
    )

    prompt = base_template.format(BOT_NAME=BOT_NAME, CREATOR_NAME=CREATOR_NAME)

    #add env varinbales to prompt from readme files
    prompt += "\n\nLEGAL & CONTACT INFORMATION:\n"
    prompt += f"- Privacy Policy: {PRIVACY_URL}\n"
    prompt += f"- Terms of Service: {TERMS_URL}\n"
    prompt += f"- Creator Email: {CREATOR_EMAIL}\n"
    prompt += f"- Contact Form: {CREATOR_CONTACTFORM}\n"



    if user_id:
        if db is not None:
            try:
                    profile_doc = db["profiles"].find_one({"user_id": user_id})
                    if profile_doc and profile_doc.get("profile_summary"):
                        prompt += "\nUSER PROFILE & HISTORY:\n"
                        prompt += f"- {profile_doc.get('profile_summary')}\n"
                    try:
                        user_info = db["user_info"].find_one({"user_id": user_id})
                        if user_info and user_info.get("username"):
                            prompt += "\nUSER IDENTIFIERS:\n"
                            prompt += f"- Instagram handle: @{user_info.get('username')}\n"
                            prompt += "- When appropriate, address the user using their Instagram handle (for example: @username).\n"
                    except Exception as _:
                        pass
            except Exception as exc:
                logger.error("Failed to inject user profile into prompt: %s", exc)

    return prompt


def build_welcome_message(user_name: str | None = None) -> str:
    name = user_name or "there"
    return f"""👋 Hello {name}! Welcome to {BOT_NAME}!

By messaging me, you have agreed to our Terms of Service {TERMS_URL} and Privacy Policy {PRIVACY_URL}.

If you ever want help creating your own bot or need help with the Meta API, you can contact the creator directly:
- Email: {CREATOR_EMAIL}
- Contact form: {CREATOR_CONTACTFORM}
"""


def truncate_text_for_instagram(text: str, limit: int = 1000) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text

    trimmed = encoded[:limit]
    while True:
        try:
            return trimmed.decode("utf-8")
        except UnicodeDecodeError:
            trimmed = trimmed[:-1]


def _send_graph_message(to: str, message_payload: dict, reply_to_mid: str | None = None) -> bool:
    if not PAGE_ACCESS_TOKEN:
        logger.error("Instagram page access token missing.")
        return False

    url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/me/messages"
    headers = {"Content-Type": "application/json"}
    payload = {
        "recipient": {"id": to},
        "message": message_payload,
    }
    if reply_to_mid:
        payload["reply_to"] = {"mid": reply_to_mid}

    try:
        response = requests.post(url, headers=headers, params={"access_token": PAGE_ACCESS_TOKEN}, json=payload, timeout=10)
        if response.status_code >= 400:
            logger.error("Instagram API error: %s", response.text)
            return False

        logger.info("200 Sent message to Instagram API")
        return True
    except Exception as exc:
        dev_log(exc, "ERR_WAPP_SEND")
        return False


def send_message(to: str, text: str, reply_to_mid: str | None = None) -> bool:
    return _send_graph_message(to, {"text": truncate_text_for_instagram(text)}, reply_to_mid=reply_to_mid)


def send_image_message(to: str, image_url: str, reply_to_mid: str | None = None) -> bool:
    return _send_graph_message(
        to,
        {
            "attachment": {
                "type": "image",
                "payload": {"url": image_url, "is_reusable": True},
            }
        },
        reply_to_mid=reply_to_mid,
    )


def _save_generated_image(image_bytes: bytes) -> str | None:
    try:
        image_id = f"{uuid.uuid4().hex}.png"
        image_path = GENERATED_IMAGES_DIR / image_id
        image_path.write_bytes(image_bytes)
        return image_id
    except Exception as exc:
        dev_log(exc, "ERR_IMAGE_SAVE")
        return None


def _upload_generated_image_to_cloudinary(image_bytes: bytes) -> str | None:
    if not CLOUDINARY_CLOUD_NAME or not CLOUDINARY_API_KEY or not CLOUDINARY_API_SECRET:
        # like i said saving the image locally is optional but i chose to use cloudinary 
        logger.info("Cloudinary credentials are missing; falling back to local image hosting")
        return None

    timestamp = str(int(datetime.now(timezone.utc).timestamp()))
    public_id = uuid.uuid4().hex
    upload_url = f"https://api.cloudinary.com/v1_1/{CLOUDINARY_CLOUD_NAME}/image/upload"

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
            files={"file": (f"{public_id}.png", image_bytes, "image/png")},
            timeout=120,
        )
    except Exception as exc:
        dev_log(exc, "ERR_CLOUDINARY_UPLOAD")
        return None

    if response.status_code >= 400:
        logger.error("Cloudinary upload failed (%s): %s", response.status_code, response.text)
        return None

    try:
        payload = response.json()
    except Exception:
        logger.error("Cloudinary upload returned a non-JSON response")
        return None

    secure_url = payload.get("secure_url") if isinstance(payload, dict) else None
    if isinstance(secure_url, str) and secure_url:
        return secure_url

    logger.error("Cloudinary upload response did not include a secure_url")
    return None


def _public_base_url() -> str:
    configured = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
    if configured:
        return configured
    try:
        return request.url_root.rstrip("/")
    except Exception:
        return ""


def _extract_media_source(attachment: dict) -> tuple[str | None, str | None]:
    attachment_type = attachment.get("type")
    payload = attachment.get("payload") or {}
    source = payload.get("url") or payload.get("video_url") or payload.get("media_url")
    return source, attachment_type


def _extract_image_prompt(text_body: str) -> str | None:
# we have to use /generate for image generation to tell main.py whcih route to assign ecause the agent we are using fortext reply is different from the one that generateds the image 
    if not text_body:
        return None
    cleaned = text_body.strip()
    # allow '/generate ' or '/generate:' prefixes (case-insensitive)
    m = re.match(r"^/generate[:\s]+(.+)$", cleaned, flags=re.IGNORECASE | re.DOTALL)
    if m:
        return m.group(1).strip()
    return None


def _handle_image_generation_message(
    user_id: str,
    prompt_text: str,
    user_name: str | None,
    reply_to_mid: str | None = None,
) -> None:
    if not generate_image_bytes:
        send_message(user_id, "Image generation is currently unavailable.", reply_to_mid=reply_to_mid)
        return

    if not check_image_rate_limit(user_id):
        send_message(
            user_id,
            f"You have reached your daily image generation limit ({IMAGE_LIMIT} per day). Please try again tomorrow.",
            reply_to_mid=reply_to_mid,
        )
        return

    save_message_to_db(
        user_id=user_id,
        message=prompt_text,
        sender_type="user",
        message_type="text",
        user_name=user_name,
    )
    save_user_info(
        user_id=user_id,
        message_text=prompt_text,
        sender_type="user",
        user_name=user_name,
        message_type="text",
    )

    send_message(user_id, "Generating your image, please wait...", reply_to_mid=reply_to_mid)

    image_url = None
    try:
        image_bytes = generate_image_bytes(prompt_text)
        if image_bytes:
            image_url = _upload_generated_image_to_cloudinary(image_bytes)
            if not image_url:
                image_name = _save_generated_image(image_bytes)
                if image_name:
                    base_url = _public_base_url()
                    if base_url:
                        image_url = f"{base_url}/generated-images/{image_name}"

    except Exception as exc:
        dev_log(exc, "ERR_IMAGE_GENERATION")
        image_url = None

    if not image_url:
        send_message(user_id, "I couldn't generate that image right now. Please try again.", reply_to_mid=reply_to_mid)
        return

    if not send_image_message(user_id, image_url, reply_to_mid=reply_to_mid):
        send_message(user_id, "I generated the image, but I couldn't send it right now.", reply_to_mid=reply_to_mid)
        return

    save_message_to_db(
        user_id=user_id,
        message=image_url,
        sender_type="bot",
        message_type="image",
        user_name=user_name,
    )

    if should_update_user_profile(user_id):
        try:
            threading.Thread(
                target=update_user_profile_in_background,
                args=(user_id, prompt_text, db, user_name),
                daemon=True,
            ).start()
        except Exception as exc:
            dev_log(exc, "ERR_PROFILE_THREAD")


def _handle_text_message(user_id: str, text_body: str, user_name: str | None, reply_to_mid: str | None = None) -> None:
    image_prompt = _extract_image_prompt(text_body)
    if image_prompt:
        _handle_image_generation_message(user_id, image_prompt, user_name, reply_to_mid=reply_to_mid)
        return

    is_new_user = is_first_time_user(user_id)
    if is_new_user:
        logger.info("New user registered")

    saved = save_message_to_db(
        user_id=user_id,
        message=text_body,
        sender_type="user",
        message_type="text",
        user_name=user_name,
    )
    if not saved:
        send_message(user_id, make_user_safe_error("ERR100"), reply_to_mid=reply_to_mid)
        return

    save_user_info(
        user_id=user_id,
        message_text=text_body,
        sender_type="user",
        user_name=user_name,
        message_type="text",
    )

    if is_new_user:
        welcome_message = build_welcome_message(user_name)
        send_message(user_id, welcome_message, reply_to_mid=reply_to_mid)
        save_message_to_db(
            user_id=user_id,
            message=welcome_message,
            sender_type="bot",
            message_type="text",
            user_name=user_name,
        )

    
    history = get_conversation_history(user_id, limit=MEMORY_LIMIT)
    system_prompt = build_system_prompt(user_id)
    if gemini and hasattr(gemini, "generate_ai_reply_with_context"):
        try:
            reply_text = gemini.generate_ai_reply_with_context(
                user_id=user_id,
                system_prompt=system_prompt,
                conversation_history=history,
                user_text=text_body,
                media_part=None,
                make_user_safe_error=make_user_safe_error,
            )
        except Exception as exc:
            dev_log(exc, "ERR_GEMINI_CALL")
            if generate_fallback_reply_with_context:
                reply_text = generate_fallback_reply_with_context(user_id, text_body, history, system_prompt)
            else:
                reply_text = make_user_safe_error("ERR400")
    else:
        if generate_fallback_reply_with_context:
            reply_text = generate_fallback_reply_with_context(user_id, text_body, history, system_prompt)
        else:
            reply_text = make_user_safe_error("ERR400")

    if not send_message(user_id, reply_text, reply_to_mid=reply_to_mid):
        logger.error("Failed to send Instagram message to %s", user_id)

    save_message_to_db(
        user_id=user_id,
        message=reply_text,
        sender_type="bot",
        message_type="text",
        user_name=user_name,
    )

    if should_update_user_profile(user_id):
        try:
            threading.Thread(
                target=update_user_profile_in_background,
                args=(user_id, text_body, db, user_name),
                daemon=True,
            ).start()
        except Exception as exc:
            dev_log(exc, "ERR_PROFILE_THREAD")


def _handle_media_message(
    user_id: str,
    attachment: dict,
    user_name: str | None,
    reply_label: str,
    caption_text: str = "",
    reply_to_mid: str | None = None,
) -> None:
    if media is None:
        send_message(user_id, "Media processing is currently unavailable.", reply_to_mid=reply_to_mid)
        return

    if not media.check_media_rate_limit(user_id, collection):
        send_message(
            user_id,
            f"You have reached your daily limit for media processing ({media.MEDIA_LIMIT} per day). Please try again tomorrow.",
            reply_to_mid=reply_to_mid,
        )
        return

    source, attachment_type = _extract_media_source(attachment)
    if not source:
        send_message(user_id, f"Sorry, I couldn't download or analyze the {reply_label}.", reply_to_mid=reply_to_mid)
        return

    send_message(user_id, f"Analyzing your {reply_label}, please wait...", reply_to_mid=reply_to_mid)
    media_part = media.get_media_part(source)

    if media_part:
        prompt = f"[User sent a {reply_label}."
        if caption_text:
            prompt += f" Caption: {caption_text}."
        prompt += " Consider the media and respond in context of the conversation.]"

        history = get_conversation_history(user_id, limit=MEMORY_LIMIT)
        system_prompt = build_system_prompt(user_id)
        if media and hasattr(media, "analyze_media_with_context"):
            try:
                processing_result = media.analyze_media_with_context(
                    user_id=user_id,
                    system_prompt=system_prompt,
                    conversation_history=history,
                    media_part=media_part,
                    user_text=prompt,
                    make_user_safe_error=make_user_safe_error,
                )
            except Exception as exc:
                dev_log(exc, "ERR_MEDIA_ANALYSIS")
                processing_result = make_user_safe_error("ERR400")
        else:
            processing_result = make_user_safe_error("ERR400")
    else:
        processing_result = f"Sorry, I couldn't download or analyze the {reply_label}."

    save_message_to_db(user_id, f"[{reply_label.upper()}]", "user", attachment_type or reply_label)
    save_user_info(
        user_id=user_id,
        message_text=f"[{reply_label.upper()}]",
        sender_type="user",
        user_name=user_name,
        message_type=attachment_type or reply_label,
    )
    save_message_to_db(user_id, processing_result, "bot", "text")
    send_message(user_id, processing_result, reply_to_mid=reply_to_mid)

    if should_update_user_profile(user_id):
        try:
            threading.Thread(
                target=update_user_profile_in_background,
                args=(user_id, f"[{reply_label.upper()}]", db, user_name),
                daemon=True,
            ).start()
        except Exception as exc:
            dev_log(exc, "ERR_PROFILE_THREAD")


# webhooks 
def handle_instagram_webhook(data: dict) -> None:
    try:
        obj = data.get("object")
        if obj not in ("instagram", "page"):
            logger.info("Ignoring non-Instagram/Page webhook payload: %s", obj)
            return

        for entry in data.get("entry", []):
            for messaging_event in entry.get("messaging", []):
                sender = messaging_event.get("sender", {})
                recipient = messaging_event.get("recipient", {})
                sender_id = sender.get("id")
                recipient_id = recipient.get("id")
                message_id = messaging_event.get("message", {}).get("mid")

                if not sender_id or str(sender_id) == str(INSTAGRAM_ID):
                    continue
                if str(sender_id) == str(recipient_id):
                    continue
                if messaging_event.get("message", {}).get("is_echo"):
                    continue

                # claim the message id before processing so retries do not double-reply
                if message_id and not mark_message_as_processed(message_id):
                    logger.info("Skipping duplicate message %s from %s", message_id[:8], sender_id[-4:])
                    continue

                message = messaging_event.get("message") or {}
                # fetch public profile info if possible  this will help me provide ai with as much data as possible to make it more relatable 
                user_name = None
                try:
                    profile = fetch_instagram_profile(sender_id)
                    if profile:
                        user_name = profile.get("name") or profile.get("username")
                except Exception:
                    user_name = None

                attachments = message.get("attachments") or []
                if attachments:
                    attachment = attachments[0]
                    reply_label = attachment.get("type", "media")
                    _handle_media_message(
                        sender_id,
                        attachment,
                        user_name,
                        reply_label,
                        caption_text=message.get("text", "").strip(),
                        reply_to_mid=message_id,
                    )
                    continue

                if message.get("text"):
                    _handle_text_message(sender_id, message.get("text", ""), user_name, reply_to_mid=message_id)
                    continue

                if messaging_event.get("reaction") or messaging_event.get("read") or messaging_event.get("postback"):
                    logger.info("Received Instagram non-text event from %s", sender_id)
                    continue

                save_message_to_db(sender_id, "[UNSUPPORTED EVENT]", "user", "unsupported", user_name=user_name)
                logger.info("Skipping unsupported format event from %s", sender_id)
    except Exception as exc:
        error_id = str(uuid.uuid4())[:8]
        dev_log(exc, f"WEBHOOK_ERR_{error_id}")
        logger.error("Webhook processing failed with error id %s", error_id)



app = Flask(__name__)


@app.route("/", methods=["GET"])
@app.route("/webhook", methods=["GET"])
def verify_webhook():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        return challenge or "", 200

    return "Verification failed", 403

@app.route("/", methods=["POST"])
@app.route("/webhook", methods=["POST"])
def receive_webhook():
    payload = request.get_json(silent=True) or {}
    def _scrub_payload_for_logging(obj: dict) -> dict:
        try:
            copy = deepcopy(obj)
            for entry in copy.get("entry", []) or []:
                messaging = entry.get("messaging") or []
                filtered = [m for m in messaging if not m.get("message", {}).get("is_echo")]
                entry["messaging"] = filtered
            return copy
        except Exception:
            return {"error": "failed to scrub payload"}

    scrubbed = _scrub_payload_for_logging(payload)
    logger.info("Received webhook payload: %s", scrubbed)
    handle_instagram_webhook(payload)
    return jsonify({"status": "received"}), 200


@app.route("/generated-images/<path:image_name>", methods=["GET"])
def serve_generated_image(image_name: str):
    return send_from_directory(GENERATED_IMAGES_DIR, image_name)


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
