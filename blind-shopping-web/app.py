"""Blind Shopping Assistant - camera-first Streamlit front-end.

A shopping helper for blind and low-vision shoppers:

1. The shopper points a phone or laptop camera at a supermarket shelf.
2. One photo is taken.  The photo stays on screen until the next photo
   replaces it, and the analysis result stays under it.
3. The photo is analysed by a vision model through *the shopper's own API
   key*, which is typed into the sidebar and kept only in this browser
   session.  No key is shipped with this app.
4. The answer is shown in large, high-contrast text and read aloud by the
   browser speech engine.

Run locally:
    streamlit run app.py

Deploy:
    Push this folder to GitHub and create a Streamlit Community Cloud app
    with ``app.py`` as the entry point.  See README.md.
"""

from __future__ import annotations

import base64
import hashlib
import html
import io
import json
import re
import time
from dataclasses import dataclass
from typing import Any

import requests
import streamlit as st
import streamlit.components.v1 as components
from PIL import Image, ImageOps

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

APP_NAME = "Blind Shopping Assistant"
MAX_IMAGE_SIDE = 1280
JPEG_QUALITY = 75
REQUEST_TIMEOUT = 45

HKBU_PROVIDER = "HKBU GenAI (api-key)"
OPENAI_PROVIDER = "OpenAI-compatible (Bearer)"

PROVIDERS: dict[str, tuple[str, str, str]] = {
    # provider -> (default base URL, default model, default API version)
    HKBU_PROVIDER: (
        "https://genai.hkbu.edu.hk/api/v0/rest/deployments",
        "gpt-4.1",
        "2024-12-01-preview",
    ),
    OPENAI_PROVIDER: (
        "https://api.openai.com/v1",
        "gpt-4o-mini",
        "",
    ),
}

MODE_SHELF = "Shelf scan"
MODE_GRAB = "Grab check"
MODE_DESCRIBE = "Describe view"
MODES = [MODE_SHELF, MODE_GRAB, MODE_DESCRIBE]

# ---------------------------------------------------------------------------
# Prompts (English, one compact JSON object per answer)
# ---------------------------------------------------------------------------

SYSTEM_SHELF = """You are the shelf-analysis module of a shopping assistant for a blind shopper.
Target item: <<TARGET_ITEM>>
The image is a first-person view of the shelf the shopper is facing.
Return ONLY one compact JSON object with this schema:
{
  "target_visible": false,
  "target_position": "short English position description",
  "grasp_hint": "one or two short English sentences with direction, height, shelf level, and package cues",
  "price": "price text or an empty string",
  "say": "one short English sentence summarising the target and its location"
}
Rules:
- Describe position from the shopper's point of view using left, center, or right, plus upper, middle, or lower shelf.
- The grasp_hint must be safe, concrete, and easy to follow without vision.
- If the target is not visible, say so honestly and give a search hint instead of inventing a location.
- Never invent a price. Use an empty string when no price is readable.
- Do not use markdown fences. Every field must be in English.
"""

SYSTEM_GRAB = """You are the grab-verification module of a shopping assistant for a blind shopper.
Target item: <<TARGET_ITEM>>
The image is captured while the shopper is holding a product toward the camera.
Return ONLY one compact JSON object with this schema:
{
  "grabbed": false,
  "matches": false,
  "feedback": "one short English sentence for the shopper"
}
Rules:
- grabbed is true only when a product is clearly visible in the shopper's hand.
- matches is true only when the product in the hand is the requested target, not a similar package.
- If the image is unclear, set both fields to false and ask the shopper to try again.
- Do not use markdown fences. Every field must be in English.
"""

SYSTEM_DESCRIBE = """You are the scene-description module of a shopping assistant for a blind shopper.
The image is a first-person view of whatever the shopper is facing.
Return ONLY one compact JSON object with this schema:
{
  "say": "one or two short English sentences describing the main things in view and where they are",
  "items": ["short English product or object names"],
  "price": "price text or an empty string"
}
Rules:
- Describe only what is clearly visible. Never guess.
- Use left, center, right, upper, middle, lower to place objects.
- Keep each item name short and concrete.
- Never invent a price. Use an empty string when no price is readable.
- Do not use markdown fences. Every field must be in English.
"""

PROMPTS = {
    MODE_SHELF: "Analyse this shelf photo for the target product and return the required JSON.",
    MODE_GRAB: "Analyse this grab-verification photo and return the required JSON.",
    MODE_DESCRIBE: "Describe this view for the shopper and return the required JSON.",
}

MAX_TOKENS = {
    MODE_SHELF: 650,
    MODE_GRAB: 260,
    MODE_DESCRIBE: 500,
}


class AppError(RuntimeError):
    """An error that is safe and useful to show to the shopper."""


@dataclass(frozen=True)
class ApiSettings:
    provider: str
    key: str
    base_url: str
    model: str
    version: str


# ---------------------------------------------------------------------------
# Image helpers
# ---------------------------------------------------------------------------

try:  # Pillow >= 9.1
    _RESAMPLE = Image.Resampling.LANCZOS
except AttributeError:  # pragma: no cover - very old Pillow
    _RESAMPLE = Image.LANCZOS


def encode_image(image_bytes: bytes) -> tuple[str, bytes]:
    """Return (data URL, compressed JPEG bytes) for a camera photo."""
    try:
        image = Image.open(io.BytesIO(image_bytes))
    except Exception as exc:  # noqa: BLE001 - any decode failure is user-facing
        raise AppError("That photo could not be read. Please take another one.") from exc

    image = ImageOps.exif_transpose(image).convert("RGB")
    image.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE), _RESAMPLE)

    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    jpeg = buffer.getvalue()
    data_url = "data:image/jpeg;base64," + base64.b64encode(jpeg).decode("ascii")
    return data_url, jpeg


# ---------------------------------------------------------------------------
# API layer
# ---------------------------------------------------------------------------


def build_url(settings: ApiSettings) -> str:
    base = (settings.base_url or "").strip().rstrip("/")
    if not base:
        raise AppError("The API base URL is empty. Fill it in under API settings.")

    if base.endswith("/chat/completions"):
        url = base
        if settings.provider == HKBU_PROVIDER and settings.version and "api-version=" not in url:
            url += ("&" if "?" in url else "?") + f"api-version={settings.version}"
        return url

    if settings.provider == HKBU_PROVIDER:
        version = settings.version or "2024-12-01-preview"
        return f"{base}/{settings.model}/chat/completions?api-version={version}"
    return f"{base}/chat/completions"


def _headers(settings: ApiSettings) -> dict[str, str]:
    headers = {"Content-Type": "application/json", "accept": "application/json"}
    if settings.provider == HKBU_PROVIDER:
        headers["api-key"] = settings.key
    else:
        headers["Authorization"] = f"Bearer {settings.key}"
    return headers


def _error_detail(response: requests.Response) -> str:
    try:
        payload = response.json()
        error = payload.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            if message:
                return str(message)[:300]
        if isinstance(error, str):
            return error[:300]
    except Exception:  # noqa: BLE001 - body may not be JSON
        pass
    return (response.text or "no details")[:300]


def _post_chat(settings: ApiSettings, payload: dict[str, Any]) -> str:
    url = build_url(settings)
    last_error: AppError | None = None

    for attempt in range(2):
        try:
            response = requests.post(
                url, headers=_headers(settings), json=payload, timeout=REQUEST_TIMEOUT
            )
        except requests.Timeout as exc:
            raise AppError(
                "The vision API did not answer in time. Check the network and try again."
            ) from exc
        except requests.RequestException as exc:
            raise AppError(f"Network error: {exc}") from exc

        if response.status_code in (401, 403):
            raise AppError(
                f"The API key was rejected (HTTP {response.status_code}). "
                "Check the key and the selected provider."
            )
        if response.status_code == 404:
            raise AppError(
                "Endpoint not found (HTTP 404). Check the base URL, the model name, "
                "and the API version."
            )
        if response.status_code == 429:
            last_error = AppError("Rate limit reached (HTTP 429). Wait a moment and try again.")
        elif response.status_code >= 500:
            last_error = AppError(
                f"The vision service is unavailable (HTTP {response.status_code}). "
                "Try again shortly."
            )
        elif not response.ok:
            raise AppError(f"API error (HTTP {response.status_code}): {_error_detail(response)}")
        else:
            try:
                data = response.json()
            except ValueError as exc:
                raise AppError("The API returned a response that is not valid JSON.") from exc
            return _extract_content(data)

        if attempt == 0:
            time.sleep(1.2)

    raise last_error or AppError("The vision API call failed.")


def _extract_content(data: dict[str, Any]) -> str:
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise AppError("The API returned an unexpected response shape.") from exc

    if isinstance(content, list):
        # Some OpenAI-compatible gateways return a list of content blocks.
        content = " ".join(
            str(block.get("text", "")) if isinstance(block, dict) else str(block)
            for block in content
        )
    return str(content or "").strip()


def _extract_json(text: str) -> dict[str, Any] | None:
    """Pull the first complete JSON object out of a model answer."""
    if not isinstance(text, str) or not text.strip():
        return None

    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)

    left, right = cleaned.find("{"), cleaned.rfind("}")
    if left == -1 or right == -1 or right <= left:
        return None

    candidate = cleaned[left : right + 1]
    for attempt in (candidate, re.sub(r",\s*([}\]])", r"\1", candidate)):
        try:
            data = json.loads(attempt)
        except Exception:  # noqa: BLE001 - try the repaired candidate
            continue
        if isinstance(data, dict):
            return data
    return None


def analyze_photo(settings: ApiSettings, mode: str, target: str, image_bytes: bytes) -> dict[str, Any]:
    """Send one photo to the vision model and return the parsed JSON object."""
    data_url, _ = encode_image(image_bytes)
    system_prompt = {
        MODE_SHELF: SYSTEM_SHELF,
        MODE_GRAB: SYSTEM_GRAB,
        MODE_DESCRIBE: SYSTEM_DESCRIBE,
    }[mode].replace("<<TARGET_ITEM>>", target.strip() or "(not specified)")

    payload = {
        "messages": [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": PROMPTS[mode]},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            },
        ],
        "max_tokens": MAX_TOKENS[mode],
        "stream": False,
    }

    content = _post_chat(settings, payload)
    data = _extract_json(content)
    if data is None:
        raise AppError(
            "The model answered in an unexpected format. Take the photo again or try another model."
        )
    return data


def test_connection(settings: ApiSettings) -> str:
    """Small text-only call used by the 'Test connection' button."""
    payload = {
        "messages": [{"role": "user", "content": "Reply with the single word: OK"}],
        "max_tokens": 8,
        "stream": False,
    }
    return _post_chat(settings, payload)


# ---------------------------------------------------------------------------
# Result normalisation (model JSON -> what the shopper sees)
# ---------------------------------------------------------------------------


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"true", "yes", "1"}
    return False


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text.strip(" .,!?:;\"'")


def summarize(mode: str, data: dict[str, Any]) -> dict[str, str]:
    """Map the raw model JSON to a small display model."""
    if mode == MODE_SHELF:
        found = _as_bool(data.get("target_visible"))
        return {
            "status": "found" if found else "missing",
            "label": "TARGET FOUND" if found else "TARGET NOT VISIBLE",
            "headline": _clean_text(data.get("say")) or "The shelf could not be read clearly.",
            "hint": _clean_text(data.get("grasp_hint")),
            "position": _clean_text(data.get("target_position")),
            "price": _clean_text(data.get("price")),
        }

    if mode == MODE_GRAB:
        grabbed = _as_bool(data.get("grabbed"))
        matches = _as_bool(data.get("matches"))
        if grabbed and matches:
            label = "ITEM VERIFIED"
        elif grabbed:
            label = "DIFFERENT ITEM"
        else:
            label = "NOT CONFIRMED"
        return {
            "status": "found" if (grabbed and matches) else "missing",
            "label": label,
            "headline": _clean_text(data.get("feedback")) or "The item could not be verified.",
            "hint": "",
            "position": "",
            "price": "",
        }

    items = data.get("items") or []
    if isinstance(items, list):
        items_text = " 路 ".join(_clean_text(item) for item in items if _clean_text(item))
    else:
        items_text = _clean_text(items)
    return {
        "status": "info",
        "label": "WHAT IS IN FRONT OF YOU",
        "headline": _clean_text(data.get("say")) or "Nothing clear was recognised.",
        "hint": items_text,
        "position": "",
        "price": _clean_text(data.get("price")),
    }


def speech_text(summary: dict[str, str]) -> str:
    parts = [summary.get("headline", "")]
    if summary.get("hint"):
        parts.append(summary["hint"])
    if summary.get("price"):
        parts.append(f"Price {summary['price']}.")
    return " ".join(part for part in parts if part)


# ---------------------------------------------------------------------------
# UI helpers
# ---------------------------------------------------------------------------


def _inject_css() -> None:
    st.markdown(
        """
        <style>
        .block-container { padding-top: 2.2rem; padding-bottom: 3rem; max-width: 1180px; }
        .app-sub { font-size: 1.05rem; line-height: 1.5; color: #C9C9CF; margin-bottom: 1.2rem; }
        [data-testid="stCameraInput"] img { border-radius: 14px; }

        .result-card {
            border-radius: 16px;
            padding: 22px 24px 18px 24px;
            margin: 6px 0 14px 0;
            border: 1px solid #2A2A30;
            background: #121216;
        }
        .result-card.found   { border-left: 9px solid #3DDC84; background: #0E1A13; }
        .result-card.missing { border-left: 9px solid #FFB020; background: #1A150C; }
        .result-card.info    { border-left: 9px solid #5B9DFF; background: #0E141F; }

        .pill {
            display: inline-block;
            font-size: 0.82rem;
            font-weight: 800;
            letter-spacing: 0.09em;
            padding: 6px 12px;
            border-radius: 999px;
            margin-bottom: 12px;
        }
        .pill.found   { background: #123D25; color: #7BF2AE; }
        .pill.missing { background: #3D2B0C; color: #FFC96B; }
        .pill.info    { background: #122A4D; color: #9CC4FF; }

        .headline {
            font-size: 1.65rem;
            font-weight: 750;
            line-height: 1.3;
            color: #FFFFFF;
            margin-bottom: 10px;
        }
        .hint {
            font-size: 1.22rem;
            line-height: 1.5;
            color: #E8E8EC;
            margin-bottom: 6px;
        }
        .meta { display: flex; flex-wrap: wrap; gap: 18px; margin-top: 16px; }
        .meta-item { display: flex; flex-direction: column; gap: 2px; }
        .meta-item span {
            font-size: 0.72rem;
            letter-spacing: 0.08em;
            text-transform: uppercase;
            color: #9A9AA5;
        }
        .meta-item strong { font-size: 1.05rem; color: #F4F4F6; font-weight: 650; }

        .placeholder {
            border: 2px dashed #33333B;
            border-radius: 16px;
            padding: 30px 24px;
            color: #A9A9B3;
            font-size: 1.05rem;
            line-height: 1.6;
        }
        @media (max-width: 640px) {
            .headline { font-size: 1.35rem; }
            .hint { font-size: 1.08rem; }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def _init_state() -> None:
    if "provider" not in st.session_state:
        first = next(iter(PROVIDERS))
        st.session_state["provider"] = first
        st.session_state["base_url"], st.session_state["model"], st.session_state["api_version"] = PROVIDERS[first]

    for key, value in {
        "api_key": "",
        "read_aloud": True,
        "mode": MODE_SHELF,
        "target": "",
        "result": None,
        "raw": None,
        "error": None,
        "result_sig": "",
        "photo_digest": "",
        "photo_time": "",
        "history": [],
        "speak_seq": 0,
        "spoken_seq": 0,
        "speak_text": "",
    }.items():
        st.session_state.setdefault(key, value)


def _reset_provider_defaults() -> None:
    provider = st.session_state.get("provider", next(iter(PROVIDERS)))
    base, model, version = PROVIDERS[provider]
    st.session_state["base_url"] = base
    st.session_state["model"] = model
    st.session_state["api_version"] = version
    st.session_state["result_sig"] = ""


def _render_sidebar() -> ApiSettings:
    st.sidebar.header("API settings")
    st.sidebar.caption(
        "This app ships without any key. Paste your own key below; it is kept "
        "only in this browser session and never saved to disk."
    )

    st.sidebar.selectbox(
        "Provider",
        list(PROVIDERS),
        key="provider",
        on_change=_reset_provider_defaults,
    )
    st.sidebar.text_input(
        "API key",
        key="api_key",
        type="password",
        placeholder="Paste your key here",
        help="HKBU: paste the api-key value. OpenAI-compatible: paste the secret key.",
    )

    with st.sidebar.expander("Advanced endpoint settings"):
        st.text_input(
            "Base URL",
            key="base_url",
            help="HKBU: .../deployments (the model name is appended). "
            "OpenAI-compatible: e.g. https://api.openai.com/v1",
        )
        st.text_input("Model", key="model")
        st.text_input(
            "API version",
            key="api_version",
            help="Only used by the HKBU endpoint (?api-version=...).",
        )

    settings = ApiSettings(
        provider=st.session_state["provider"],
        key=(st.session_state["api_key"] or "").strip(),
        base_url=st.session_state["base_url"],
        model=st.session_state["model"],
        version=st.session_state["api_version"],
    )

    if st.sidebar.button("Test connection", width="stretch"):
        if not settings.key:
            st.sidebar.warning("Paste an API key first.")
        else:
            with st.sidebar:
                with st.spinner("Calling the model..."):
                    try:
                        test_connection(settings)
                    except AppError as exc:
                        st.error(str(exc))
                    else:
                        st.success("Connection works. The model answered.")

    st.sidebar.divider()
    st.sidebar.toggle("Read results aloud", key="read_aloud")
    st.sidebar.caption("Uses the browser's built-in English voice. No extra audio service.")

    st.sidebar.divider()
    if st.sidebar.button("Start a new session", width="stretch"):
        for key in (
            "camera",
            "upload",
            "result",
            "raw",
            "error",
            "result_sig",
            "photo_digest",
            "photo_time",
            "history",
            "speak_seq",
            "spoken_seq",
            "speak_text",
        ):
            st.session_state.pop(key, None)
        st.rerun()

    st.sidebar.divider()
    st.sidebar.caption(
        "Camera access needs https:// or http://localhost. On a phone, open the "
        "Streamlit Cloud URL (https) rather than a plain LAN address."
    )
    return settings


def _signature(*parts: str) -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(str(part).encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()


def _image_digest(image_bytes: bytes) -> str:
    return hashlib.sha256(image_bytes).hexdigest()[:16]


def _card_html(summary: dict[str, str]) -> str:
    parts = [
        f'<div class="result-card {summary["status"]}">',
        f'<div class="pill {summary["status"]}">{html.escape(summary["label"])}</div>',
        f'<div class="headline">{html.escape(summary["headline"])}</div>',
    ]
    if summary.get("hint"):
        parts.append(f'<div class="hint">{html.escape(summary["hint"])}</div>')
    meta = []
    if summary.get("position"):
        meta.append(
            '<div class="meta-item"><span>Position</span>'
            f'<strong>{html.escape(summary["position"])}</strong></div>'
        )
    if summary.get("price"):
        meta.append(
            '<div class="meta-item"><span>Price</span>'
            f'<strong>{html.escape(summary["price"])}</strong></div>'
        )
    if meta:
        parts.append('<div class="meta">' + "".join(meta) + "</div>")
    parts.append("</div>")
    return "".join(parts)


def _request_speech(text: str) -> None:
    if not text:
        return
    st.session_state["speak_seq"] = int(st.session_state.get("speak_seq", 0)) + 1
    st.session_state["speak_text"] = text


def _render_speech() -> None:
    if not st.session_state.get("read_aloud"):
        return
    if st.session_state.get("speak_seq", 0) == st.session_state.get("spoken_seq", 0):
        return
    text = st.session_state.get("speak_text", "")
    if not text:
        return

    markup = """
    <script>
    (function () {
        const text = %s;
        if (!text) { return; }
        let synth = null;
        let Utterance = null;
        try {
            // Prefer the top document: it has the user gesture and the voices.
            if (window.parent && window.parent !== window && window.parent.speechSynthesis) {
                synth = window.parent.speechSynthesis;
                Utterance = window.parent.SpeechSynthesisUtterance;
            }
        } catch (err) { synth = null; }
        if (!synth) {
            synth = window.speechSynthesis;
            Utterance = window.SpeechSynthesisUtterance;
        }
        if (!synth || !Utterance) { return; }
        synth.cancel();
        const utterance = new Utterance(text);
        utterance.lang = 'en-US';
        utterance.rate = 0.95;
        const voices = synth.getVoices ? synth.getVoices() : [];
        const voice = voices.find(v => v.lang === 'en-US') ||
                      voices.find(v => v.lang.startsWith('en'));
        if (voice) { utterance.voice = voice; }
        synth.speak(utterance);
    })();
    </script>
    """ % json.dumps(text)

    try:  # Streamlit >= 1.67; a 1px iframe only carries the speech script
        st.iframe(markup, height=1)
    except AttributeError:  # older Streamlit
        components.html(markup, height=0)
    except Exception:  # speech is a bonus, never break the page for it
        pass
    st.session_state["spoken_seq"] = st.session_state.get("speak_seq", 0)


# ---------------------------------------------------------------------------
# Main app
# ---------------------------------------------------------------------------


def main() -> None:
    st.set_page_config(
        page_title=APP_NAME,
        page_icon="馃洅",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    _inject_css()
    _init_state()
    settings = _render_sidebar()

    st.title(APP_NAME)
    st.markdown(
        "<p class='app-sub'>Take one photo. It stays on screen until the next photo "
        "replaces it. The assistant describes what it sees in large text and reads "
        "the answer aloud.</p>",
        unsafe_allow_html=True,
    )

    mode = st.radio("What do you need?", MODES, horizontal=True, key="mode")
    target = st.text_input(
        "Product name",
        key="target",
        placeholder="For example: Coca-Cola, milk, toothpaste",
        disabled=(mode == MODE_DESCRIBE),
        help="Used by Shelf scan and Grab check. Describe view ignores it.",
    )

    left, right = st.columns([1, 1], gap="large")

    with left:
        photo = st.camera_input("Take a photo", key="camera")
        with st.expander("Or upload a photo instead"):
            upload = st.file_uploader(
                "Upload an image",
                type=["jpg", "jpeg", "png", "webp"],
                key="upload",
                label_visibility="collapsed",
            )
        st.caption("The last photo stays here until you take the next one.")

    image_bytes: bytes | None = None
    if photo is not None:
        image_bytes = photo.getvalue()
    elif upload is not None:
        image_bytes = upload.getvalue()

    if image_bytes is None:
        st.session_state["photo_digest"] = ""
        st.session_state["result"] = None
        st.session_state["raw"] = None
        st.session_state["error"] = None
        st.session_state["result_sig"] = ""
    else:
        digest = _image_digest(image_bytes)
        if digest != st.session_state.get("photo_digest"):
            st.session_state["photo_digest"] = digest
            st.session_state["photo_time"] = time.strftime("%H:%M:%S")
            st.session_state["result"] = None
            st.session_state["raw"] = None
            st.session_state["error"] = None
            st.session_state["result_sig"] = ""

    with right:
        st.subheader("Result")

        force = False
        if st.session_state.get("result") or st.session_state.get("error"):
            force = st.button("Analyze again", width="stretch")

        target_ready = mode == MODE_DESCRIBE or bool(target.strip())
        can_analyse = image_bytes is not None and bool(settings.key) and target_ready

        if image_bytes is None:
            st.markdown(
                "<div class='placeholder'>No photo yet.<br>"
                "Take a photo on the left, then the answer appears here.</div>",
                unsafe_allow_html=True,
            )
        elif not settings.key:
            st.warning(
                "Add your own API key in the sidebar under **API settings**. "
                "This app never contains a key."
            )
        elif not target_ready:
            st.info("Type the product name first, then the photo is analysed automatically.")
        else:
            signature = _signature(
                st.session_state.get("photo_digest", ""),
                mode,
                target.strip().lower(),
                settings.provider,
                settings.model,
                settings.base_url,
                hashlib.sha256(settings.key.encode("utf-8")).hexdigest()[:12],
            )

            if force or signature != st.session_state.get("result_sig"):
                with st.spinner("Looking at the photo..."):
                    try:
                        raw = analyze_photo(settings, mode, target, image_bytes)
                    except AppError as exc:
                        st.session_state["error"] = str(exc)
                        st.session_state["result"] = None
                        st.session_state["raw"] = None
                        st.session_state["result_sig"] = signature
                    else:
                        summary = summarize(mode, raw)
                        st.session_state["result"] = summary
                        st.session_state["raw"] = raw
                        st.session_state["error"] = None
                        st.session_state["result_sig"] = signature
                        st.session_state["history"] = (
                            st.session_state.get("history", [])
                            + [
                                {
                                    "time": st.session_state.get("photo_time", ""),
                                    "mode": mode,
                                    "target": target.strip(),
                                    "label": summary["label"],
                                    "headline": summary["headline"],
                                }
                            ]
                        )[-10:]
                        if st.session_state.get("read_aloud"):
                            _request_speech(speech_text(summary))

        if st.session_state.get("error"):
            st.error(st.session_state["error"])

        if st.session_state.get("result"):
            summary = st.session_state["result"]
            st.markdown(_card_html(summary), unsafe_allow_html=True)

            actions = st.columns([1, 1])
            if actions[0].button("Speak again", width="stretch"):
                _request_speech(speech_text(summary))
            with actions[1]:
                st.caption(
                    "Photo taken at " + (st.session_state.get("photo_time") or "--:--:--")
                )

            if st.session_state.get("raw") is not None:
                with st.expander("What the model returned"):
                    st.json(st.session_state["raw"])

    if st.session_state.get("history"):
        with st.expander(f"Recent scans ({len(st.session_state['history'])})"):
            for entry in reversed(st.session_state["history"][-5:]):
                st.markdown(
                    f"**{html.escape(entry['time'])} 路 {html.escape(entry['mode'])}"
                    f"{' 路 ' + html.escape(entry['target']) if entry['target'] else ''}**  \n"
                    f"{html.escape(entry['label'])} — {html.escape(entry['headline'])}"
                )

    _render_speech()
    st.caption(
        "Photos are processed only for the current request and are not saved by this app. "
        "The API provider may log requests according to its own policy."
    )


if __name__ == "__main__":
    main()
