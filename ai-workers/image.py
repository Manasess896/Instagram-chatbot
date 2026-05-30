# here we are using clouflare to generate images from user text we are using the flux-1-schnell model from cloudflare and if there is an error we return None and the main.py file will handle the error and send a message to the user that image generation is unavailable we are also using cloudinary for storing the generated images  you can use local storage if you want but what you cant do is  upload the image directly to graph api





from __future__ import annotations

import base64
import logging
import os

import requests

logger = logging.getLogger(__name__)

CLOUDFLARE_ACCOUNT_ID = os.getenv("CLOUDFLARE_ACCOUNT_ID", "")
CLOUDFLARE_API_TOKEN = os.getenv("CLOUDFLARE_API_TOKEN", "")
CLOUDFLARE_IMAGE_MODEL = os.getenv("CLOUDFLARE_IMAGE_MODEL", "@cf/black-forest-labs/flux-1-schnell")


def _generate_ai_image(text: str) -> bytes | None:
    if not CLOUDFLARE_ACCOUNT_ID or not CLOUDFLARE_API_TOKEN:
        logger.error("Cloudflare credentials are missing; image generation is unavailable")
        return None

    url = f"https://api.cloudflare.com/client/v4/accounts/{CLOUDFLARE_ACCOUNT_ID}/ai/run/{CLOUDFLARE_IMAGE_MODEL}"
    try:
        response = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {CLOUDFLARE_API_TOKEN}",
                "Content-Type": "application/json",
            },
            json={"prompt": text},
            timeout=120,
        )
    except Exception:
        logger.exception("Cloudflare image generation request failed")
        return None

    if response.status_code >= 400:
        logger.error("Cloudflare image generation failed (%s): %s", response.status_code, response.text)
        return None

    content_type = (response.headers.get("Content-Type") or "").lower()
    if "image/" in content_type:
        return response.content

    try:
        payload = response.json()
    except Exception:
        logger.error("Cloudflare image generation returned non-image, non-JSON response")
        return None

    result = payload.get("result") if isinstance(payload, dict) else None
    if isinstance(result, dict):
        image_b64 = result.get("image") or result.get("image_base64") or result.get("data")
        if isinstance(image_b64, str) and image_b64:
            try:
                return base64.b64decode(image_b64)
            except Exception:
                logger.exception("Failed to decode Cloudflare image bytes")
                return None

    logger.error("Cloudflare image generation returned no image bytes")
    return None


def generate_image_bytes(text: str) -> bytes | None:
    """Generate image bytes from user text."""
    return _generate_ai_image(text)
