"""Copy generation via Claude."""

import json
import os
import re
from pathlib import Path

from core import http

BRAND_VOICE_PATH = Path("brand-voice.md")


def _call_claude(system_prompt, user_prompt, model, max_tokens=1000):
    resp = http.request(
        "POST", "https://api.anthropic.com/v1/messages", "anthropic",
        headers={
            "x-api-key": os.environ["ANTHROPIC_API_KEY"],
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": model,
            "max_tokens": max_tokens,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_prompt}],
        },
    )
    content_blocks = resp.json()["content"]
    # content[0] isn't reliably the text block — claude-sonnet-5 includes a
    # leading "thinking" block by default (confirmed live).
    text_blocks = [b["text"] for b in content_blocks if b.get("type") == "text"]
    if not text_blocks:
        raise RuntimeError(f"No text block in Claude response: {content_blocks}")
    text = text_blocks[0].strip()
    return re.sub(r"^```json\s*|\s*```$", "", text)


def generate_instagram_caption(article, model):
    """Draft a caption + hashtags from the blog article."""
    brand_voice = BRAND_VOICE_PATH.read_text() if BRAND_VOICE_PATH.exists() else ""

    system_prompt = f"""You write Instagram captions for Maison de Cocon, a
quiet-luxury German sleep and bedding brand. Voice: restrained, editorial,
premium — never hype-y, never emoji-stuffed. Tagline: "Sleep, reconsidered."
/ German: "Schlaf, neu gedacht."

{('Brand voice guide:' + brand_voice) if brand_voice else ''}

You will be given a blog post's title and content. Write a companion
Instagram caption that teases the post and drives readers to read more via
the bio link (captions cannot contain clickable links). End with a plain
"Link in bio" call to action (in German, matching the brand's primary
market) — do not include the URL itself in the caption.

Never write health or allergy claims (hypoallergenic, allergikerfreundlich,
antibakteriell …), certification claims (OEKO-TEX, GOTS, Down Pass …), or
statements that we stock or sell a product that is not part of the post's
content. Stay with what the post itself states.

Respond ONLY with valid JSON, no markdown fences, no preamble:
{{"caption": "...", "hashtags": ["...", "..."]}}

Caption must be under 2200 characters. Provide 8-15 relevant hashtags,
without the # symbol (added later), mixing brand/niche/broad tags."""

    user_prompt = (
        f"Title: {article['title']}\n\nContent:\n"
        f"{article.get('summary') or article.get('body', '')[:3000]}"
    )
    return json.loads(_call_claude(system_prompt, user_prompt, model))
