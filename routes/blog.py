# -*- coding: utf-8 -*-
"""
Blog publishing endpoint — intermediary between the daily blog cron
(running in trusted_only cloud environment) and the Shopify Admin API.

The cron calls POST /blog/publicar with article content.
This service calls OpenAI for image generation and Shopify for publishing.

Image generation strategy:
- TIPO A (product): fetch real product image from Shopify, use /v1/images/edits
  so GPT sees the actual packaging and generates a faithful cover.
- TIPO B (technique): use /v1/images/generations with editorial prompt.
"""
from __future__ import annotations

import io
import logging
import os
from datetime import datetime
from typing import Optional

import requests
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from auth import verificar_api_key

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/blog", tags=["blog"])

SHOPIFY_SHOP    = os.getenv("SHOPIFY_SHOP", "pknw4n-eg")
SHOPIFY_TOKEN   = os.getenv("SHOPIFY_ACCESS_TOKEN", "")
SHOPIFY_BLOG_ID = 117021376817
SHOPIFY_BASE    = f"https://{SHOPIFY_SHOP}.myshopify.com/admin/api/2024-01"
OPENAI_API_KEY  = os.getenv("OPENAI_API_KEY", "")

# Prompt prefix injected for TIPO A when a reference image is provided.
# Explicitly instructs GPT-image-1 to stay faithful to the real packaging.
_FIDELITY_PREFIX = (
    "Blog cover image for a Brazilian professional hair cosmetics store. "
    "IMPORTANT: the reference image shows the EXACT product — preserve its "
    "packaging colors, branding, logo and design with full fidelity. "
    "Do NOT change colors or invent a different product. "
    "Place the product on a clean white marble surface with soft professional "
    "studio lighting, elegant bokeh background. Product occupies the left half "
    "of the frame. No text overlay. Photorealistic, premium beauty advertising, "
    "widescreen 3:2 format. "
)


class BlogPublicarRequest(BaseModel):
    title: str
    handle: str
    body_html: str
    summary_html: str
    tags: str
    image_prompt: Optional[str] = None
    article_type: Optional[str] = "A"  # "A" = produto, "B" = técnica
    product_search: Optional[str] = None  # TIPO A: search term to find real product image in Shopify


def _shopify_headers() -> dict:
    return {
        "X-Shopify-Access-Token": SHOPIFY_TOKEN,
        "Content-Type": "application/json",
    }


def _handle_exists(handle: str) -> bool:
    """Returns True if an article with this handle already exists."""
    url = f"{SHOPIFY_BASE}/blogs/{SHOPIFY_BLOG_ID}/articles.json"
    params = {"limit": 250, "fields": "handle"}
    r = requests.get(url, headers=_shopify_headers(), params=params, timeout=30)
    if r.status_code != 200:
        return False
    articles = r.json().get("articles", [])
    return any(a.get("handle") == handle for a in articles)


def _fetch_product_image_bytes(search_query: str) -> Optional[bytes]:
    """
    Searches Shopify for a product matching search_query and returns
    the raw bytes of its first image. Returns None if not found or error.
    """
    try:
        url = f"{SHOPIFY_BASE}/products.json"
        params = {"title": search_query, "limit": 3, "fields": "id,title,images", "status": "active"}
        r = requests.get(url, headers=_shopify_headers(), params=params, timeout=15)
        if r.status_code != 200:
            logger.warning(f"Shopify product search failed ({r.status_code}) for: {search_query}")
            return None
        products = r.json().get("products", [])
        if not products:
            logger.warning(f"No Shopify product found for search: {search_query}")
            return None
        images = products[0].get("images", [])
        if not images:
            logger.warning(f"Product found but has no images: {products[0].get('title')}")
            return None
        img_url = images[0]["src"]
        logger.info(f"Using reference image from product '{products[0].get('title')}': {img_url}")
        img_r = requests.get(img_url, timeout=30)
        if img_r.status_code == 200:
            return img_r.content
    except Exception as e:
        logger.error(f"_fetch_product_image_bytes error: {e}")
    return None


def _generate_image_with_reference(prompt: str, image_bytes: bytes) -> Optional[str]:
    """
    Calls gpt-image-1 /edits with a real product image as reference.
    Returns base64 string or None on failure.
    """
    if not OPENAI_API_KEY:
        return None
    try:
        full_prompt = _FIDELITY_PREFIX + prompt
        files = {
            "image[]": ("product_ref.jpg", io.BytesIO(image_bytes), "image/jpeg"),
        }
        data = {
            "model": "gpt-image-1",
            "prompt": full_prompt,
            "size": "1536x1024",
            "quality": "medium",
            "n": "1",
        }
        r = requests.post(
            "https://api.openai.com/v1/images/edits",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
            files=files,
            data=data,
            timeout=180,
        )
        r.raise_for_status()
        return r.json()["data"][0]["b64_json"]
    except Exception as e:
        logger.error(f"Image edit with reference failed: {e}")
        return None


def _generate_image_b64(prompt: str) -> Optional[str]:
    """Calls gpt-image-1 /generations (TIPO B — no reference image). Returns base64 or None."""
    if not OPENAI_API_KEY:
        logger.warning("OPENAI_API_KEY not set — skipping image generation")
        return None
    try:
        r = requests.post(
            "https://api.openai.com/v1/images/generations",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
            json={"model": "gpt-image-1", "prompt": prompt, "size": "1536x1024", "quality": "medium", "n": 1},
            timeout=120,
        )
        r.raise_for_status()
        return r.json()["data"][0]["b64_json"]
    except Exception as e:
        logger.error(f"Image generation failed: {e}")
        return None


def _publish_article(req: BlogPublicarRequest, image_b64: Optional[str]) -> dict:
    """POSTs article to Shopify. Returns the created article JSON."""
    article: dict = {
        "title": req.title,
        "author": "Shinsei Market",
        "tags": req.tags,
        "body_html": req.body_html,
        "summary_html": req.summary_html,
        "published": True,
        "handle": req.handle,
    }
    if image_b64:
        article["image"] = {
            "attachment": image_b64,
            "filename": f"{req.handle}-shinsei.jpg",
        }

    url = f"{SHOPIFY_BASE}/blogs/{SHOPIFY_BLOG_ID}/articles.json"
    r = requests.post(url, headers=_shopify_headers(), json={"article": article}, timeout=60)
    if r.status_code not in (200, 201):
        raise HTTPException(status_code=502, detail=f"Shopify error {r.status_code}: {r.text[:500]}")
    return r.json().get("article", {})


@router.post("/publicar")
def publicar_blog(req: BlogPublicarRequest, _=Depends(verificar_api_key)):
    """
    Receives article content from the daily blog cron and publishes to Shopify.
    Also generates the cover image via gpt-image-1.
    """
    logger.info(f"blog/publicar: handle={req.handle} type={req.article_type}")

    # 1. Duplicate check
    if _handle_exists(req.handle):
        return {
            "status": "skipped",
            "reason": "article with this handle already exists",
            "handle": req.handle,
        }

    # 2. Generate image
    # TIPO A (product): fetch real Shopify product image → use /edits for faithful cover
    # TIPO B (technique): use /generations with editorial prompt
    image_b64 = None
    if req.image_prompt:
        article_type = (req.article_type or "B").upper()
        if article_type == "A" and req.product_search:
            ref_bytes = _fetch_product_image_bytes(req.product_search)
            if ref_bytes:
                logger.info(f"TIPO A: generating image with real product reference for {req.handle}")
                image_b64 = _generate_image_with_reference(req.image_prompt, ref_bytes)
                if not image_b64:
                    logger.warning(f"Reference edit failed for {req.handle}, falling back to /generations")
                    image_b64 = _generate_image_b64(req.image_prompt)
            else:
                logger.warning(f"Could not find reference image for '{req.product_search}', using /generations")
                image_b64 = _generate_image_b64(req.image_prompt)
        else:
            image_b64 = _generate_image_b64(req.image_prompt)

        if image_b64:
            logger.info(f"Image generated for {req.handle} ({len(image_b64)} chars b64)")
        else:
            logger.warning(f"Image generation skipped for {req.handle} — publishing without image")

    # 3. Publish
    article = _publish_article(req, image_b64)
    url = f"https://www.shinseimarket.com.br/blogs/novidades/{req.handle}"
    logger.info(f"Published: {url}")

    image_method = "none"
    if image_b64:
        if (req.article_type or "B").upper() == "A" and req.product_search:
            image_method = "edits_with_reference"
        else:
            image_method = "generations"

    return {
        "status": "published",
        "handle": req.handle,
        "title": req.title,
        "url": url,
        "article_id": article.get("id"),
        "image_generated": image_b64 is not None,
        "image_method": image_method,
        "published_at": datetime.utcnow().isoformat(),
    }


@router.get("/status")
def blog_status(_=Depends(verificar_api_key)):
    """Returns basic status: Shopify token present, OpenAI key present."""
    return {
        "shopify_token": bool(SHOPIFY_TOKEN),
        "openai_key": bool(OPENAI_API_KEY),
        "blog_id": SHOPIFY_BLOG_ID,
        "store": SHOPIFY_STORE,
    }
