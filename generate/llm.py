"""
AI Scheduling & API Router for Insurtech Insights
- Primary: Mistral (via la Plateforme API)
- Fallback: Nvidia NIM (Llama 3.3)
- One judgment per Actions run
"""

import os
import time
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# API Client Setup
# ---------------------------------------------------------------------------

def _build_mistral_client():
    """Lazy-init Mistral client."""
    try:
        from mistralai.client import Mistral
    except ImportError:
        raise ImportError("mistralai package not installed. Run: pip install mistralai")

    api_key = os.environ.get("MISTRAL_API_KEY", "").strip()
    if not api_key:
        raise ValueError("MISTRAL_API_KEY environment variable is not set.")
    return Mistral(api_key=api_key)


def _build_openai_client():
    """Lazy-init OpenAI-compatible client for Nvidia NIM."""
    try:
        from openai import OpenAI
    except ImportError:
        raise ImportError("openai package not installed. Run: pip install openai")

    api_key = os.environ.get("NVIDIA_API_KEY", "").strip()
    if not api_key:
        raise ValueError("NVIDIA_API_KEY environment variable is not set.")
    return OpenAI(
        base_url="https://integrate.api.nvidia.com/v1",
        api_key=api_key,
    )


# ---------------------------------------------------------------------------
# Single-run judgment
# ---------------------------------------------------------------------------

class QuotaExhaustedError(Exception):
    """Raised when the primary (Mistral) quota is depleted."""
    pass


def _attempt_mistral(
    model: str,
    system_prompt: str,
    user_prompt: str,
    temperature: float,
    max_tokens: int,
    retry_attempts: int,
    retry_delays: list,
) -> str:
    """Single call to Mistral. Raises QuotaExhaustedError on quota/retry-exhaustion."""
    client = _build_mistral_client()

    for attempt in range(retry_attempts):
        try:
            resp = client.chat.complete(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=temperature,
                max_tokens=max_tokens,
            )
            if resp and resp.choices and resp.choices[0].message.content:
                return resp.choices[0].message.content.strip()

        except Exception as exc:
            msg = str(exc).lower()
            # Detect quota / rate-limit signals
            quota_hit = any(kw in msg for kw in ["429", "rate limit", "quota exceeded", "too many requests", "insufficient_quota", "billing"])

            if quota_hit and attempt == 0:
                logger.warning("Mistral quota hit on first attempt; switching to fallback.")
                raise QuotaExhaustedError(msg)
            elif quota_hit:
                logger.warning("Mistral quota hit on retry %d; switching to fallback.", attempt + 1)
                raise QuotaExhaustedError(msg)

            # Non-quota error → retry
            delay = retry_delays[min(attempt, len(retry_delays) - 1)]
            logger.warning("Mistral attempt %d/%d failed: %s. Retrying in %ds...", attempt + 1, retry_attempts, exc, delay)
            time.sleep(delay)

    raise RuntimeError(f"Mistral: exhausted {retry_attempts} retries without success.")


def _call_nvidia(
    model: str,
    system_prompt: str,
    user_prompt: str,
    temperature: float,
    max_tokens: int,
    retry_attempts: int,
    retry_delays: list,
) -> str:
    """Single call to Nvidia NIM. Non-recoverable after retries → RuntimeError."""
    client = _build_openai_client()

    for attempt in range(retry_attempts):
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=temperature,
                max_tokens=max_tokens,
            )
            if resp and resp.choices and resp.choices[0].message.content:
                return resp.choices[0].message.content.strip()
        except Exception as exc:
            delay = retry_delays[min(attempt, len(retry_delays) - 1)]
            logger.warning("Nvidia NIM attempt %d/%d failed: %s. Retrying in %ds...", attempt + 1, retry_attempts, exc, delay)
            time.sleep(delay)

    raise RuntimeError(f"Nvidia NIM: exhausted {retry_attempts} retries without success.")


def generate_text(
    system_prompt: str,
    user_prompt: str,
    temperature: float = 0.7,
    max_tokens: int = 4096,
    retry_attempts: int = 3,
    retry_delays: Optional[list] = None,
) -> str:
    """
    One-run judgment: try Mistral first; fall back to Nvidia if quota exhausted.

    Returns generated text (str). Raises RuntimeError if both providers fail.
    """
    if retry_delays is None:
        retry_delays = [5, 15, 30]

    mistral_model = os.environ.get("MISTRAL_MODEL", "mistral-small-latest")
    nvidia_model = os.environ.get("NVIDIA_MODEL", "meta/llama-3.3-70b-instruct")

    # --- Primary: Mistral ---
    try:
        return _attempt_mistral(
            model=mistral_model,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
            retry_attempts=retry_attempts,
            retry_delays=retry_delays,
        )
    except QuotaExhaustedError:
        logger.info("Switching to Nvidia NIM fallback after Mistral quota exhaustion.")

    # --- Fallback: Nvidia NIM ---
    return _call_nvidia(
        model=nvidia_model,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        temperature=temperature,
        max_tokens=max_tokens,
        retry_attempts=retry_attempts,
        retry_delays=retry_delays,
    )