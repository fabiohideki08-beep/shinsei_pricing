# -*- coding: utf-8 -*-
"""
Blog publishing endpoint — intermediary between the daily blog cron
(running in trusted_only cloud environment) and the Shopify Admin API.

The cron calls POST /blog/publicar with article content.
This service calls OpenAI for image generation and Shopify for publishing.
"""
from __future__ import annotations

import base64
import json
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

SHOPIFY_STORE   = os.getenv("SHOPIFY_STORE", "pknw4n-eg.myshopify.com")
SHOPIFY_TOKEN   = os.getenv("SHOPIFY_ACCESS_TOKEN", "")
SHOPIFY_BLOG_ID = 117021376817
SHOPIFY_BASE    = f"https://{SHOPIFY_STORE}/admin/api/2024-01"
OPENAI_API_KEY  = os.getenv("OPENAI_API_KEY", "")


class BlogPublicarRequest(BaseModel):
    title: str
    handle: str
    body_html: str
    summary_html: str
    tags: str
    image_prompt: Optional[str] = None
    article_type: Optional[str] = "A"  # "A" = produto, "B" = técnica


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


def _generate_image_b64(prompt: str) -> Optional[str]:
    """Calls gpt-image-1 and returns base64 string, or None on failure."""
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
    image_b64 = None
    if req.image_prompt:
        image_b64 = _generate_image_b64(req.image_prompt)
        if image_b64:
            logger.info(f"Image generated for {req.handle} ({len(image_b64)} chars b64)")
        else:
            logger.warning(f"Image generation skipped for {req.handle} — publishing without image")

    # 3. Publish
    article = _publish_article(req, image_b64)
    url = f"https://www.shinseimarket.com.br/blogs/novidades/{req.handle}"
    logger.info(f"Published: {url}")

    return {
        "status": "published",
        "handle": req.handle,
        "title": req.title,
        "url": url,
        "article_id": article.get("id"),
        "image_generated": image_b64 is not None,
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
