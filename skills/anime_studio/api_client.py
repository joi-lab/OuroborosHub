"""OpenRouter API client for image, video, music, LLM calls, and VLM verification."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import struct
import time
from pathlib import Path, PurePosixPath
from typing import Optional

from io import BytesIO

import httpx
from PIL import Image as _PILImage

logger = logging.getLogger("anime_studio.api")

OPENROUTER_BASE = "https://openrouter.ai/api/v1"
VIDEO_POLL_INTERVAL = 5  # seconds
VIDEO_MAX_WAIT = 1200  # 20 min max wait per clip


def _safe_filename(filename: str) -> str:
    """Sanitize a filename to prevent path traversal."""
    name = PurePosixPath(filename).name
    name = name.split("\\")[-1]
    name = name.replace("..", "")
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name)
    if not name or name.startswith("."):
        name = f"asset_{int(time.time())}.bin"
    return name


# ─── Timeout Helper ─────────────────────────────────────────────────


async def run_with_timeout(coro, timeout_sec: float, description: str = "operation"):
    """Wrap a coroutine with a timeout."""
    try:
        return await asyncio.wait_for(coro, timeout=timeout_sec)
    except asyncio.TimeoutError:
        raise TimeoutError(f"{description} timed out after {timeout_sec}s")


# ─── Client ─────────────────────────────────────────────────────────


class OpenRouterClient:
    """Async client for all OpenRouter API interactions."""

    def __init__(self, api_key: str, state_dir: Path):
        self.api_key = api_key
        self.state_dir = state_dir
        self.assets_dir = state_dir / "assets"
        self.assets_dir.mkdir(parents=True, exist_ok=True)

    def _headers(self) -> dict:
        auth_value = f"Bearer {self.api_key}"
        return {
            "Authorization": auth_value,
            "Content-Type": "application/json",
            "HTTP-Referer": "https://ouroboros.local",
            "X-Title": "Anime Studio",
        }

    # ─── LLM Chat ───────────────────────────────────────────────────

    async def chat(
        self,
        messages: list[dict],
        model: str = "anthropic/claude-sonnet-4.6",
        max_toks: int = 4096,
        temperature: float = 0.7,
        json_mode: bool = False,
    ) -> str:
        """Standard LLM chat completion. Returns content text.

        Args:
            json_mode: If True, sets response_format={"type":"json_object"} so the
                model is guaranteed to return valid JSON. Use for structured storyboard
                and scenario generation calls.
        """
        payload: dict = {
            "model": model,
            "messages": messages,
            "max_tokens": max_toks,
            "temperature": temperature,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        async with httpx.AsyncClient(timeout=300) as client:
            resp = await client.post(
                f"{OPENROUTER_BASE}/chat/completions",
                headers=self._headers(),
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
            if not data.get("choices"):
                raise RuntimeError("No choices in LLM response")
            return data["choices"][0]["message"]["content"]

    # ─── Image Generation (GPT-Image-2) ────────────────────────────

    async def generate_image(
        self,
        prompt: str,
        filename: str,
        aspect_ratio: str = "1:1",
        size: str = "auto",
        model: str = "openai/gpt-image-2",
    ) -> str:
        """Generate image via OpenRouter chat completions (gpt-image-2 or gpt-5.4-image-2).

        Uses /chat/completions with modalities=["image","text"] — the OpenRouter-supported
        path for OpenAI image models. Pass model="openai/gpt-image-2" for the latest model
        or "openai/gpt-5.4-image-2" for the previous generation.
        """
        filename = _safe_filename(filename)
        async with httpx.AsyncClient(timeout=360) as client:
            resp = await client.post(
                f"{OPENROUTER_BASE}/chat/completions",
                headers=self._headers(),
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "modalities": ["image", "text"],
                },
            )
            resp.raise_for_status()
            data = resp.json()

        image_b64 = self._extract_image_b64(data)
        if not image_b64:
            choices = data.get("choices", [])
            detail = list(choices[0]["message"].keys()) if choices else ["(empty choices)"]
            logger.error(f"No image data in response for {filename}. keys: {detail}")
            raise RuntimeError(f"No image data in response for {filename}")

        filepath = self.assets_dir / filename
        filepath.write_bytes(base64.b64decode(image_b64))
        logger.info(f"Generated image (gpt-image-2): {filepath}")
        return str(filepath)

    # ─── Image Generation (GPT-Image-2 native via Images API) ──────

    # Maps common aspect-ratio strings to gpt-image-2 size strings.
    # gpt-image-2 accepts arbitrary WxH divisible by 16 in the [1:3, 3:1] range.
    _GPT_IMAGE_ASPECT_SIZE: dict[str, str] = {
        "16:9": "1344x768",
        "9:16": "768x1344",
        "1:1":  "1024x1024",
        "4:3":  "1024x768",
        "3:4":  "768x1024",
    }

    async def generate_image_gpt(
        self,
        prompt: str,
        filename: str,
        aspect_ratio: str = "16:9",
    ) -> str:
        """Generate image via gpt-image-2 using the native OpenAI Images API.

        Uses /v1/images/generations with model=gpt-image-2.
        Returns local file path. Response is always b64_json for this model.
        """
        filename = _safe_filename(filename)
        size = self._GPT_IMAGE_ASPECT_SIZE.get(aspect_ratio, "1344x768")
        async with httpx.AsyncClient(timeout=360) as client:
            resp = await client.post(
                f"{OPENROUTER_BASE}/images/generations",
                headers=self._headers(),
                json={
                    "model": "openai/gpt-image-2",
                    "prompt": prompt,
                    "n": 1,
                    "size": size,
                    "quality": "medium",
                    "output_format": "png",
                },
            )
            resp.raise_for_status()
            data = resp.json()

        # gpt-image-2 always returns b64_json, never a URL
        image_b64 = None
        for item in data.get("data", []):
            b64 = item.get("b64_json") or item.get("url", "")
            if b64.startswith("data:"):
                b64 = b64.split(",", 1)[1]
            if b64 and not b64.startswith("http"):
                image_b64 = b64
                break

        if not image_b64:
            logger.error(f"No b64_json in gpt-image-2 response for {filename}: {data}")
            raise RuntimeError(f"No image data in gpt-image-2 response for {filename}")

        filepath = self.assets_dir / filename
        filepath.write_bytes(base64.b64decode(image_b64))
        logger.info(f"Generated image (gpt-image-2 native): {filepath}")
        return str(filepath)

    # ─── Image Generation (Nanobanana / Gemini) ─────────────────────

    async def generate_image_nanobanana(
        self,
        prompt: str,
        filename: str,
        aspect_ratio: str = "1:1",
    ) -> str:
        """Generate image via Nanobanana (google/gemini-3.1-flash-image-preview).
        Returns local file path.
        """
        filename = _safe_filename(filename)
        async with httpx.AsyncClient(timeout=180) as client:
            resp = await client.post(
                f"{OPENROUTER_BASE}/chat/completions",
                headers=self._headers(),
                json={
                    "model": "google/gemini-3.1-flash-image-preview",
                    "messages": [{"role": "user", "content": prompt}],
                    "modalities": ["image", "text"],
                },
            )
            resp.raise_for_status()
            data = resp.json()

        image_b64 = self._extract_image_b64(data)
        if not image_b64:
            raise RuntimeError(f"No image data in Nanobanana response for {filename}")

        filepath = self.assets_dir / filename
        filepath.write_bytes(base64.b64decode(image_b64))
        logger.info(f"Generated image (nanobanana): {filepath}")
        return str(filepath)

    # ─── VLM Verification (Image) ──────────────────────────────────

    async def verify_image_vlm(
        self,
        image_path: str,
        original_prompt: str,
        character_ref_description: str = "None provided",
    ) -> dict:
        """Use a VLM to verify a generated image matches specifications.
        Returns {passed: bool, issues: [...], suggestion: str}
        """
        from .prompts import VLM_VERIFY_IMAGE_PROMPT

        image_url = self.get_image_url(image_path)

        verify_prompt = VLM_VERIFY_IMAGE_PROMPT.format(
            original_prompt=original_prompt,
            character_ref_description=character_ref_description,
        )

        try:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(
                    f"{OPENROUTER_BASE}/chat/completions",
                    headers=self._headers(),
                    json={
                        "model": "anthropic/claude-sonnet-4.6",
                        "messages": [
                            {"role": "user", "content": [
                                {"type": "text", "text": verify_prompt},
                                {"type": "image_url", "image_url": {"url": image_url}},
                            ]},
                        ],
                        "max_tokens": 1024,
                        "temperature": 0.1,
                    },
                )
                resp.raise_for_status()
                data = resp.json()

            if "choices" not in data or not data["choices"]:
                err = data.get("error", {})
                logger.warning(f"VLM verification: no choices in response, error={err}")
                return {"passed": True, "issues": [], "suggestion": "", "vlm_error": True}

            text = data["choices"][0]["message"]["content"].strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[1] if "\n" in text else text[3:]
                if text.endswith("```"):
                    text = text[:-3]
                text = text.strip()

            result = json.loads(text)
            return result
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning(f"VLM verification returned non-JSON: {e}")
            return {"passed": False, "issues": [f"VLM parse error: {e}"], "suggestion": "", "vlm_error": True}
        except Exception as e:
            logger.warning(f"VLM verification call failed: {e}")
            return {"passed": False, "issues": [f"VLM call failed: {e}"], "suggestion": "", "vlm_error": True}

    # ─── VLM Compare Two Images ─────────────────────────────────────

    async def compare_images_vlm(
        self,
        image_path_1: str,
        image_path_2: str,
        prompt: str,
    ) -> dict:
        """Compare two images via VLM. Returns parsed JSON from the model."""
        url1 = self.get_image_url(image_path_1)
        url2 = self.get_image_url(image_path_2)

        try:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(
                    f"{OPENROUTER_BASE}/chat/completions",
                    headers=self._headers(),
                    json={
                        "model": "anthropic/claude-sonnet-4.6",
                        "messages": [
                            {"role": "user", "content": [
                                {"type": "text", "text": prompt},
                                {"type": "text", "text": "Image 1:"},
                                {"type": "image_url", "image_url": {"url": url1}},
                                {"type": "text", "text": "Image 2:"},
                                {"type": "image_url", "image_url": {"url": url2}},
                            ]},
                        ],
                        "max_tokens": 512,
                        "temperature": 0.1,
                    },
                )
                resp.raise_for_status()
                data = resp.json()

            if "choices" not in data or not data["choices"]:
                err = data.get("error", {})
                logger.warning(f"VLM compare: no choices in response, error={err}")
                return {"winner": 1, "reason": f"no choices in response: {err}"}

            text = data["choices"][0]["message"]["content"].strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[1] if "\n" in text else text[3:]
                if text.endswith("```"):
                    text = text[:-3]
                text = text.strip()

            return json.loads(text)
        except Exception as e:
            logger.warning(f"VLM compare failed: {e}")
            return {"winner": 1, "reason": "comparison failed, defaulting to first"}

    # ─── VLM Multi-Image Analysis ───────────────────────────────────

    async def analyze_multi_image_vlm(
        self,
        image_paths: list[str],
        prompt: str,
        model: str = "google/gemini-3.1-pro-preview",
    ) -> dict:
        """Send multiple images to a VLM for cross-frame/cross-scene analysis."""
        content_parts = [{"type": "text", "text": prompt}]
        for path in image_paths:
            try:
                url = self.get_image_url(path)
                content_parts.append({"type": "image_url", "image_url": {"url": url}})
            except Exception as e:
                logger.warning(f"Failed to load image {path}: {e}")

        if len(content_parts) < 2:
            return {"consistent": True, "worst_scene_index": None, "drift_description": "", "severity": "none"}

        try:
            async with httpx.AsyncClient(timeout=90) as client:
                resp = await client.post(
                    f"{OPENROUTER_BASE}/chat/completions",
                    headers=self._headers(),
                    json={
                        "model": model,
                        "messages": [{"role": "user", "content": content_parts}],
                        "max_tokens": 1024,
                        "temperature": 0.1,
                    },
                )
                resp.raise_for_status()
                data = resp.json()

            if not data.get("choices"):
                return {"consistent": True, "worst_scene_index": None, "drift_description": "", "severity": "none"}

            text = data["choices"][0]["message"]["content"].strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[1] if "\n" in text else text[3:]
                if text.endswith("```"):
                    text = text[:-3]
                text = text.strip()

            return json.loads(text)
        except Exception as e:
            logger.warning(f"Multi-image VLM analysis failed: {e}")
            return {"consistent": True, "worst_scene_index": None, "drift_description": "", "severity": "none"}

    # ─── VLM Verification (Video via Gemini 3.1 Pro) ────────────────

    async def verify_video_vlm(
        self,
        frame_paths: list[str],
        scene_description: str,
        characters_description: str,
        style: str,
        camera_direction: str,
    ) -> dict:
        """Verify video quality via Gemini 3.1 Pro using pre-extracted frames.

        Frame extraction is the caller's responsibility (should use tracked
        subprocess management). This method only handles the LLM API call.

        Returns {passed: bool, score: int, issues: [...], suggestion: str}
        """
        from .prompts import VLM_VERIFY_VIDEO_PROMPT

        if not frame_paths:
            logger.warning("No frames provided for video verification — skipping")
            return {"passed": True, "score": 7, "issues": [], "suggestion": ""}

        verify_prompt = VLM_VERIFY_VIDEO_PROMPT.format(
            scene_description=scene_description,
            characters_description=characters_description,
            style=style,
            camera_direction=camera_direction,
        )

        # Build multi-image message content
        content_parts = [{"type": "text", "text": verify_prompt}]
        for i, frame_path in enumerate(frame_paths):
            try:
                frame_url = self.get_image_url(frame_path)
                content_parts.append({
                    "type": "image_url",
                    "image_url": {"url": frame_url},
                })
            except Exception as e:
                logger.warning(f"Failed to load frame {i}: {e}")

        if len(content_parts) < 2:
            return {"passed": True, "score": 7, "issues": [], "suggestion": ""}

        try:
            async with httpx.AsyncClient(timeout=90) as client:
                resp = await client.post(
                    f"{OPENROUTER_BASE}/chat/completions",
                    headers=self._headers(),
                    json={
                        "model": "google/gemini-3.1-pro-preview",
                        "messages": [{"role": "user", "content": content_parts}],
                        "max_tokens": 1024,
                        "temperature": 0.1,
                    },
                )
                resp.raise_for_status()
                data = resp.json()

            if not data.get("choices"):
                return {"passed": False, "score": 4, "issues": ["No choices in response"], "suggestion": "", "vlm_error": True}

            text = data["choices"][0]["message"]["content"].strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[1] if "\n" in text else text[3:]
                if text.endswith("```"):
                    text = text[:-3]
                text = text.strip()

            result = json.loads(text)
            # Normalize: score >= 7 means passed
            if "score" in result:
                result["passed"] = result["score"] >= 7
            return result
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning(f"Video VLM returned non-JSON: {e}")
            return {"passed": False, "score": 4, "issues": [f"VLM parse error: {e}"], "suggestion": "", "vlm_error": True}
        except Exception as e:
            logger.warning(f"Video VLM verification failed: {e}")
            return {"passed": False, "score": 4, "issues": [f"VLM call failed: {e}"], "suggestion": "", "vlm_error": True}

    # ─── Video Generation ───────────────────────────────────────────

    async def generate_video(
        self,
        prompt: str,
        filename: str,
        duration: int = 8,
        resolution: str = "720p",
        aspect_ratio: str = "16:9",
        input_references: Optional[list[dict]] = None,
        frame_images: Optional[list[dict]] = None,
        model: str = "bytedance/seedance-2.0",
        generate_audio: bool = True,
    ) -> str:
        """Generate video via OpenRouter /videos API. Returns local file path.

        Args:
            frame_images: Hard anchor frames. Example:
                [{"image_url": {"url": "data:..."}, "frame_type": "first_frame"}]
                When provided, this becomes an image-to-video generation with hard
                visual conditioning on the first/last frame.
            input_references: Soft visual guidance images (character sheets, etc.)
        """
        filename = _safe_filename(filename)
        payload: dict = {
            "model": model,
            "prompt": prompt,
            "duration": duration,
            "resolution": resolution,
            "aspect_ratio": aspect_ratio,
            "generate_audio": generate_audio,
        }
        if frame_images:
            payload["frame_images"] = frame_images
        if input_references:
            payload["input_references"] = input_references

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{OPENROUTER_BASE}/videos",
                headers=self._headers(),
                json=payload,
            )
            resp.raise_for_status()
            job_data = resp.json()
            job_id = job_data.get("id") or job_data.get("jobId")
            if not job_id:
                raise RuntimeError(f"No job ID in video response: {job_data}")

        # Poll until complete
        filepath = self.assets_dir / filename
        start = time.time()
        async with httpx.AsyncClient(timeout=30) as client:
            while time.time() - start < VIDEO_MAX_WAIT:
                await asyncio.sleep(VIDEO_POLL_INTERVAL)
                resp = await client.get(
                    f"{OPENROUTER_BASE}/videos/{job_id}",
                    headers=self._headers(),
                )
                resp.raise_for_status()
                status_data = resp.json()
                status = status_data.get("status", "")

                if status == "completed":
                    dl_resp = await client.get(
                        f"{OPENROUTER_BASE}/videos/{job_id}/content?index=0",
                        headers=self._headers(),
                    )
                    dl_resp.raise_for_status()
                    filepath.write_bytes(dl_resp.content)
                    logger.info(f"Generated video: {filepath}")
                    return str(filepath)
                elif status in ("failed", "error"):
                    error = status_data.get("error", "Unknown error")
                    raise RuntimeError(f"Video generation failed: {error}")

        raise TimeoutError(f"Video generation timed out after {VIDEO_MAX_WAIT}s")

    # ─── Music Generation (SSE Streaming via Lyria 3 Pro) ───────────

    async def generate_music(
        self,
        prompt: str,
        filename: str,
    ) -> str:
        """Generate music clip via Lyria 3 Pro Preview (SSE streaming).
        Returns local file path.
        """
        filename = _safe_filename(filename)
        TIMEOUT = 180
        MAX_AUDIO_BYTES = 20 * 1024 * 1024  # 20 MB
        deadline = time.time() + TIMEOUT
        audio_buf = bytearray()

        payload = {
            "model": "google/lyria-3-pro-preview",
            "messages": [{"role": "user", "content": prompt}],
            "modalities": ["text", "audio"],
            "audio": {"format": "wav"},
            "stream": True,
        }

        async with httpx.AsyncClient(timeout=httpx.Timeout(TIMEOUT, connect=30)) as client:
            async with client.stream(
                "POST",
                f"{OPENROUTER_BASE}/chat/completions",
                headers=self._headers(),
                json=payload,
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if time.time() > deadline:
                        raise TimeoutError(f"Music generation timed out after {TIMEOUT}s")
                    if not line or not line.startswith("data: "):
                        continue
                    data_str = line[6:].strip()
                    if data_str == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data_str)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    choices = chunk.get("choices", [])
                    if not choices:
                        continue
                    delta = choices[0].get("delta", {}) if isinstance(choices[0], dict) else {}
                    audio = delta.get("audio", {})
                    if not isinstance(audio, dict):
                        continue
                    b64_chunk = audio.get("data", "")
                    if b64_chunk:
                        padding = "=" * (-len(b64_chunk) % 4)
                        try:
                            decoded = base64.b64decode(b64_chunk + padding)
                        except Exception:
                            continue
                        if len(audio_buf) + len(decoded) > MAX_AUDIO_BYTES:
                            raise RuntimeError(
                                f"Audio exceeds {MAX_AUDIO_BYTES // (1024 * 1024)} MB cap"
                            )
                        audio_buf.extend(decoded)

        if not audio_buf:
            raise RuntimeError(f"No audio bytes returned for {filename}")

        # Detect format from file signature
        head = bytes(audio_buf[:16])
        ext = "wav"
        if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WAVE":
            ext = "wav"
        elif head[:3] == b"ID3" or (len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0):
            ext = "mp3"
        elif head[:4] == b"OggS":
            ext = "ogg"
        else:
            # Raw PCM — wrap as WAV (48kHz stereo 16-bit)
            sample_rate = 48000
            channels = 2
            bits = 16
            byte_rate = sample_rate * channels * (bits // 8)
            block_align = channels * (bits // 8)
            data_size = len(audio_buf)
            fmt_chunk = struct.pack(
                "<4sIHHIIHH",
                b"fmt ", 16, 1, channels, sample_rate, byte_rate, block_align, bits,
            )
            data_chunk = struct.pack("<4sI", b"data", data_size)
            riff_size = 4 + len(fmt_chunk) + len(data_chunk) + data_size
            riff_header = struct.pack("<4sI4s", b"RIFF", riff_size, b"WAVE")
            audio_buf = bytearray(riff_header + fmt_chunk + data_chunk + bytes(audio_buf))
            ext = "wav"

        final_filename = _safe_filename(filename.rsplit(".", 1)[0] + "." + ext)
        filepath = self.assets_dir / final_filename
        filepath.write_bytes(bytes(audio_buf))
        logger.info(f"Generated music: {filepath} ({len(audio_buf)} bytes, format: {ext})")
        return str(filepath)

    # ─── TTS ────────────────────────────────────────────────────────

    async def generate_tts(
        self,
        text: str,
        filename: str,
        voice: str = "nova",
    ) -> str:
        """Generate speech audio via TTS. Returns local file path."""
        filename = _safe_filename(filename)
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{OPENROUTER_BASE}/audio/speech",
                headers=self._headers(),
                json={
                    "model": "openai/gpt-4o-mini-tts-2025-12-15",
                    "input": text,
                    "voice": voice,
                },
            )
            resp.raise_for_status()

        filepath = self.assets_dir / filename
        filepath.write_bytes(resp.content)
        logger.info(f"Generated TTS: {filepath}")
        return str(filepath)

    # ─── Helpers ────────────────────────────────────────────────────

    def _extract_image_b64(self, data: dict) -> Optional[str]:
        """Extract base64 image data from an OpenRouter response."""
        if not data.get("choices"):
            return None
        msg = data["choices"][0]["message"]
        image_b64 = None

        # Path 1: message.images[] (OpenRouter canonical format)
        images = msg.get("images", [])
        for img in images:
            if isinstance(img, dict):
                url = ""
                if "image_url" in img and isinstance(img["image_url"], dict):
                    url = img["image_url"].get("url", "")
                elif "url" in img:
                    url = img["url"]
                if url.startswith("data:"):
                    image_b64 = url.split(",", 1)[1]
                    break
            elif isinstance(img, str) and img.startswith("data:"):
                image_b64 = img.split(",", 1)[1]
                break

        # Path 2: content as list with image_url parts (Gemini format)
        if not image_b64:
            content = msg.get("content", "")
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "image_url":
                        url = part.get("image_url", {}).get("url", "")
                        if url.startswith("data:"):
                            image_b64 = url.split(",", 1)[1]
                            break

        return image_b64

    def _compress_image_for_api(self, filepath: str, max_dim: int = 720, quality: int = 85) -> tuple[str, str]:
        """Compress and resize image for API payload size reduction.

        Returns (base64_string, mime_type).
        """
        Image = _PILImage

        path = Path(filepath)
        if not path.exists():
            raise FileNotFoundError(f"Asset not found: {filepath}")

        try:
            img = Image.open(path)
        except Exception:
            # Fallback: PIL cannot parse this image (e.g. nanobanana PNG variant);
            # return raw bytes with the original MIME type — no resize/recompress.
            raw = path.read_bytes()
            ext = path.suffix.lstrip(".").lower()
            mime = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
                    "webp": "image/webp"}.get(ext, "image/png")
            return base64.b64encode(raw).decode(), mime

        # Resize if larger than max_dim
        w, h = img.size
        if max(w, h) > max_dim:
            scale = max_dim / max(w, h)
            new_w, new_h = int(w * scale), int(h * scale)
            img = img.resize((new_w, new_h), Image.LANCZOS)

        # Convert RGBA to RGB with white background
        if img.mode in ("RGBA", "LA", "P"):
            background = Image.new("RGB", img.size, (255, 255, 255))
            if img.mode == "P":
                img = img.convert("RGBA")
            background.paste(img, mask=img.split()[-1] if "A" in img.mode else None)
            img = background
        elif img.mode != "RGB":
            img = img.convert("RGB")

        buf = BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        b64 = base64.b64encode(buf.getvalue()).decode()
        return b64, "image/jpeg"

    def get_image_url(self, filepath: str, compress: bool = False) -> str:
        """Convert local file to data URL for API input references.

        Args:
            compress: If True, resize to 720p and convert to JPEG for smaller payloads.
        """
        if compress:
            b64, mime = self._compress_image_for_api(filepath)
            return f"data:{mime};base64,{b64}"

        path = Path(filepath)
        if not path.exists():
            raise FileNotFoundError(f"Asset not found: {filepath}")
        b64 = base64.b64encode(path.read_bytes()).decode()
        ext = path.suffix.lstrip(".")
        mime = {
            "png": "image/png",
            "jpg": "image/jpeg",
            "jpeg": "image/jpeg",
            "webp": "image/webp",
        }.get(ext, "image/png")
        return f"data:{mime};base64,{b64}"

    def make_input_reference(self, filepath: str) -> dict:
        """Create an OpenRouter input_references entry from a local file."""
        return {
            "type": "image_url",
            "image_url": {"url": self.get_image_url(filepath, compress=True)},
        }

    def make_frame_image(self, filepath: str, frame_type: str = "first_frame") -> dict:
        """Create an OpenRouter frame_images entry for hard visual anchoring.

        Args:
            filepath: Local image file path.
            frame_type: "first_frame" or "last_frame" — hard condition for video start/end.
        """
        return {
            "type": "image_url",
            "image_url": {"url": self.get_image_url(filepath, compress=True)},
            "frame_type": frame_type,
        }
