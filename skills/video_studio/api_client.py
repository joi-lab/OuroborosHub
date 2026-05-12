"""OpenRouter API client for Video Studio — image, video, music, LLM, VLM, and Gemini AV analysis."""
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

logger = logging.getLogger("video_studio.api")

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


async def run_with_timeout(coro, timeout_sec: float, description: str = "operation"):
    """Wrap a coroutine with a timeout."""
    try:
        return await asyncio.wait_for(coro, timeout=timeout_sec)
    except asyncio.TimeoutError:
        raise TimeoutError(f"{description} timed out after {timeout_sec}s")


class OpenRouterClient:
    """Async client for all OpenRouter API interactions for Video Studio."""

    def __init__(self, api_key: str, state_dir: Path):
        self.api_key = api_key
        self.state_dir = state_dir
        self.assets_dir = state_dir / "assets"
        self.assets_dir.mkdir(parents=True, exist_ok=True)

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://ouroboros.local",
            "X-Title": "Video Studio",
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
        """Standard LLM chat completion. Returns content text."""
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
            return data["choices"][0]["message"]["content"]

    async def chat_multimodal(
        self,
        content: list[dict],
        model: str = "google/gemini-2.5-pro",
        max_toks: int = 2048,
    ) -> str:
        """Multimodal chat completion with content array (text + images + video)."""
        async with httpx.AsyncClient(timeout=180) as client:
            resp = await client.post(
                f"{OPENROUTER_BASE}/chat/completions",
                headers=self._headers(),
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": content}],
                    "max_tokens": max_toks,
                    "temperature": 0.1,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"]

    # ─── Gemini AV Analysis ──────────────────────────────────────────

    async def analyze_video_gemini(
        self,
        video_path: Path,
        prompt: str,
        model: str = "google/gemini-2.5-pro",
    ) -> str:
        """Analyze video (and audio) using Gemini 2.5 Pro via OpenRouter.

        Sends video as base64 data URL for clips <= 8MB.
        Falls back to frame extraction for larger clips.
        Routes to google-ai-studio provider for video support.
        """
        video_bytes = video_path.read_bytes()
        size_mb = len(video_bytes) / (1024 * 1024)

        if size_mb <= 8.0:
            b64 = base64.b64encode(video_bytes).decode()
            data_url = f"data:video/mp4;base64,{b64}"
            content = [
                {"type": "text", "text": prompt},
                {"type": "video_url", "video_url": {"url": data_url}},
            ]
        else:
            # Fallback: extract frames and send as images
            logger.info(f"Video {video_path.name} is {size_mb:.1f}MB — using frame extraction fallback")
            frames = await self._extract_frames_for_analysis(video_path, num_frames=5)
            content = [{"type": "text", "text": prompt + "\n(Note: analyzing frames only due to file size, not full video/audio)"}]
            for frame_path in frames:
                try:
                    b64 = base64.b64encode(frame_path.read_bytes()).decode()
                    content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
                except Exception as e:
                    logger.warning(f"Failed to load frame {frame_path}: {e}")

        payload = {
            "model": model,
            "messages": [{"role": "user", "content": content}],
            "max_tokens": 1024,
            "provider": {"only": ["google-ai-studio"]},
        }
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                f"{OPENROUTER_BASE}/chat/completions",
                headers=self._headers(),
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"]

    async def _extract_frames_for_analysis(self, video_path: Path, num_frames: int = 5) -> list[Path]:
        """Extract evenly-spaced JPEG frames from a video for API analysis.

        Uses asyncio subprocesses (non-blocking) so the event loop stays
        responsive; each call is bounded at 15s.
        """
        import shutil

        if not shutil.which("ffprobe") or not shutil.which("ffmpeg"):
            return []

        frames = []
        _open_procs: list = []

        def _safe_kill(p) -> None:
            try:
                p.kill()
            except Exception:
                pass

        async def _safe_wait(p, timeout: float = 5) -> None:
            try:
                await asyncio.wait_for(p.wait(), timeout=timeout)
            except (asyncio.TimeoutError, Exception):
                pass

        try:
            probe = await asyncio.create_subprocess_exec(
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "csv=p=0", str(video_path),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
            _open_procs.append(probe)
            try:
                stdout, _ = await asyncio.wait_for(probe.communicate(), timeout=15)
            except asyncio.TimeoutError:
                _safe_kill(probe)
                await _safe_wait(probe)
                return []
            finally:
                if probe in _open_procs:
                    _open_procs.remove(probe)
            duration = float(stdout.decode().strip()) if stdout.strip() else 5.0
            interval = duration / (num_frames + 1)
            for i in range(num_frames):
                ts = interval * (i + 1)
                out = self.assets_dir / f"_gemini_frame_{video_path.stem}_{i}.jpg"
                proc = await asyncio.create_subprocess_exec(
                    "ffmpeg", "-y", "-ss", f"{ts:.2f}", "-i", str(video_path),
                    "-vframes", "1", "-q:v", "3", str(out),
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                )
                _open_procs.append(proc)
                try:
                    rc = await asyncio.wait_for(proc.wait(), timeout=15)
                    if rc == 0 and out.exists():
                        frames.append(out)
                except asyncio.TimeoutError:
                    _safe_kill(proc)
                    await _safe_wait(proc)
                finally:
                    if proc in _open_procs:
                        _open_procs.remove(proc)
        except asyncio.CancelledError:
            for p in list(_open_procs):
                _safe_kill(p)
            raise
        except Exception as e:
            logger.warning(f"Frame extraction for Gemini analysis failed: {e}")
        return frames

    # ─── Image Generation (GPT-Image-2 via chat completions) ────────

    async def generate_image(
        self,
        prompt: str,
        filename: str,
        aspect_ratio: str = "1:1",
        size: str = "auto",
        model: str = "openai/gpt-5.4-image-2",
    ) -> str:
        """Generate image via OpenRouter chat completions (gpt-image-2 / gpt-5.4-image-2)."""
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
            msg = data["choices"][0]["message"]
            raise RuntimeError(f"No image data in response for {filename}. keys: {list(msg.keys())}")

        filepath = self.assets_dir / filename
        filepath.write_bytes(base64.b64decode(image_b64))
        logger.info(f"Generated image (gpt-image-2): {filepath}")
        return str(filepath)

    # ─── Image Generation (Nanobanana / Gemini Flash) ───────────────

    async def generate_image_nanobanana(
        self,
        prompt: str,
        filename: str,
        aspect_ratio: str = "1:1",
    ) -> str:
        """Generate photorealistic image via Gemini Flash image generation (nanobanana).

        Preferred for photorealistic content — Gemini Flash image model
        produces more natural results than GPT-image-2 for real-world scenes.
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

        # PIL fallback for unusual image formats
        raw_bytes = base64.b64decode(image_b64)
        try:
            _PILImage.open(BytesIO(raw_bytes)).verify()
            filepath = self.assets_dir / filename
            filepath.write_bytes(raw_bytes)
        except Exception:
            try:
                img = _PILImage.open(BytesIO(raw_bytes))
                buf = BytesIO()
                img.convert("RGB").save(buf, format="PNG")
                filepath = self.assets_dir / (filename.rsplit(".", 1)[0] + ".png")
                filepath.write_bytes(buf.getvalue())
            except Exception as e2:
                filepath = self.assets_dir / filename
                filepath.write_bytes(raw_bytes)
                logger.warning(f"PIL could not re-encode image {filename}: {e2}")

        logger.info(f"Generated image (nanobanana): {filepath}")
        return str(filepath)

    # ─── VLM Verification (Image) ──────────────────────────────────

    async def verify_image_vlm(
        self,
        image_path: str,
        original_prompt: str,
        character_ref_description: str = "None provided",
    ) -> dict:
        """Use VLM to verify a generated image matches specifications."""
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
                        "messages": [{"role": "user", "content": [
                            {"type": "text", "text": verify_prompt},
                            {"type": "image_url", "image_url": {"url": image_url}},
                        ]}],
                        "max_tokens": 2048,
                        "temperature": 0.1,
                        "response_format": {"type": "json_object"},
                    },
                )
                resp.raise_for_status()
                data = resp.json()

            if "choices" not in data or not data["choices"]:
                return {"passed": True, "issues": [], "suggestion": "", "vlm_error": True}

            text = data["choices"][0]["message"]["content"].strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[1] if "\n" in text else text[3:]
                if text.endswith("```"):
                    text = text[:-3]
                text = text.strip()
            return json.loads(text)
        except (json.JSONDecodeError, ValueError) as e:
            return {"passed": False, "issues": [f"VLM parse error: {e}"], "suggestion": "", "vlm_error": True}
        except Exception as e:
            return {"passed": False, "issues": [f"VLM call failed: {e}"], "suggestion": "", "vlm_error": True}

    # ─── VLM Compare Two Images ─────────────────────────────────────

    async def compare_images_vlm(self, image_path_1: str, image_path_2: str, prompt: str) -> dict:
        """Compare two images via VLM. Returns parsed JSON."""
        url1 = self.get_image_url(image_path_1)
        url2 = self.get_image_url(image_path_2)
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(
                    f"{OPENROUTER_BASE}/chat/completions",
                    headers=self._headers(),
                    json={
                        "model": "anthropic/claude-sonnet-4.6",
                        "messages": [{"role": "user", "content": [
                            {"type": "text", "text": prompt},
                            {"type": "text", "text": "Image 1:"},
                            {"type": "image_url", "image_url": {"url": url1}},
                            {"type": "text", "text": "Image 2:"},
                            {"type": "image_url", "image_url": {"url": url2}},
                        ]}],
                        "max_tokens": 512,
                        "temperature": 0.1,
                    },
                )
                resp.raise_for_status()
                data = resp.json()

            if "choices" not in data or not data["choices"]:
                return {"winner": 1, "reason": "no choices in response"}

            text = data["choices"][0]["message"]["content"].strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[1] if "\n" in text else text[3:]
                if text.endswith("```"):
                    text = text[:-3]
                text = text.strip()
            return json.loads(text)
        except Exception as e:
            return {"winner": 1, "reason": f"comparison failed: {e}"}

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
            return {"consistent": False, "worst_scene_index": None, "drift_description": "No images loaded for QC", "severity": "qc_skipped"}

        try:
            async with httpx.AsyncClient(timeout=90) as client:
                resp = await client.post(
                    f"{OPENROUTER_BASE}/chat/completions",
                    headers=self._headers(),
                    json={
                        "model": model,
                        "messages": [{"role": "user", "content": content_parts}],
                        "max_tokens": 4096,
                        "temperature": 0.1,
                        "response_format": {"type": "json_object"},
                    },
                )
                resp.raise_for_status()
                data = resp.json()

            text = data["choices"][0]["message"]["content"].strip()
            # Strip markdown fences if model ignores response_format
            if text.startswith("```"):
                text = text.split("\n", 1)[1] if "\n" in text else text[3:]
                if text.endswith("```"):
                    text = text[:-3]
                text = text.strip()
            return json.loads(text)
        except Exception as e:
            logger.warning(f"Multi-image VLM analysis failed: {e}")
            return {"consistent": False, "worst_scene_index": None, "drift_description": f"QC skipped: {e}", "severity": "qc_skipped"}

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

        generate_audio=True asks Seedance to synthesize ambient sound and voice
        for any dialogue in the prompt (natively supported by Seedance 2.0).
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
            if resp.status_code >= 400:
                try:
                    err_body = resp.text[:500]
                except Exception:
                    err_body = "(unreadable)"
                logger.error(f"Video API {resp.status_code} error body: {err_body}")
            resp.raise_for_status()
            job_data = resp.json()
            job_id = job_data.get("id") or job_data.get("jobId")
            if not job_id:
                raise RuntimeError(f"No job ID in video response: {job_data}")

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

    # ─── Music Generation ────────────────────────────────────────────

    async def generate_music(self, prompt: str, filename: str) -> str:
        """Generate music clip via Lyria 3 Pro Preview. Returns local file path."""
        filename = _safe_filename(filename)
        TIMEOUT = 180
        MAX_AUDIO_BYTES = 20 * 1024 * 1024
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
                "POST", f"{OPENROUTER_BASE}/chat/completions",
                headers=self._headers(), json=payload,
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if time.time() > deadline:
                        raise TimeoutError(f"Music timed out after {TIMEOUT}s")
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
                            raise RuntimeError(f"Audio exceeds {MAX_AUDIO_BYTES // (1024 * 1024)} MB cap")
                        audio_buf.extend(decoded)

        if not audio_buf:
            raise RuntimeError(f"No audio bytes returned for {filename}")

        head = bytes(audio_buf[:16])
        ext = "wav"
        if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WAVE":
            ext = "wav"
        elif head[:3] == b"ID3" or (len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0):
            ext = "mp3"
        elif head[:4] == b"OggS":
            ext = "ogg"
        else:
            sample_rate, channels, bits = 48000, 2, 16
            byte_rate = sample_rate * channels * (bits // 8)
            block_align = channels * (bits // 8)
            data_size = len(audio_buf)
            fmt_chunk = struct.pack("<4sIHHIIHH", b"fmt ", 16, 1, channels, sample_rate, byte_rate, block_align, bits)
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

    async def generate_tts(self, text: str, filename: str, voice: str = "nova") -> str:
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

    # ─── Helpers ─────────────────────────────────────────────────────

    def _extract_image_b64(self, data: dict) -> Optional[str]:
        """Extract base64 image data from an OpenRouter response."""
        msg = data["choices"][0]["message"]
        image_b64 = None

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
        """Compress and resize image for API payload size reduction."""
        Image = _PILImage
        path = Path(filepath)
        if not path.exists():
            raise FileNotFoundError(f"Asset not found: {filepath}")

        try:
            img = Image.open(path)
            img.verify()  # Detect truncated/corrupt images early
            img = Image.open(path)  # Re-open after verify() (verify() exhausts the file)
        except Exception as e:
            # PIL cannot parse this image — fall back to raw bytes instead of raising.
            # nanobanana sometimes returns non-standard PNG headers that PIL rejects,
            # but the video API can still accept the raw bytes directly.
            logger.warning(f"PIL cannot re-encode {path.name}, using raw bytes fallback: {e}")
            raw = path.read_bytes()
            ext = path.suffix.lstrip(".")
            mime = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
                    "webp": "image/webp"}.get(ext, "image/png")
            return base64.b64encode(raw).decode(), mime

        w, h = img.size
        if max(w, h) > max_dim:
            scale = max_dim / max(w, h)
            img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)

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
        return base64.b64encode(buf.getvalue()).decode(), "image/jpeg"

    def get_image_url(self, filepath: str, compress: bool = False) -> str:
        """Convert local file to data URL for API input references."""
        if compress:
            b64, mime = self._compress_image_for_api(filepath)
            return f"data:{mime};base64,{b64}"

        path = Path(filepath)
        if not path.exists():
            raise FileNotFoundError(f"Asset not found: {filepath}")
        b64 = base64.b64encode(path.read_bytes()).decode()
        ext = path.suffix.lstrip(".")
        mime = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
                "webp": "image/webp"}.get(ext, "image/png")
        return f"data:{mime};base64,{b64}"

    def make_input_reference(self, filepath: str) -> dict:
        """Create an OpenRouter input_references entry from a local file."""
        return {
            "type": "image_url",
            "image_url": {"url": self.get_image_url(filepath, compress=True)},
        }

    def make_frame_image(self, filepath: str, frame_type: str = "first_frame") -> dict:
        """Create an OpenRouter frame_images entry for hard visual anchoring."""
        return {
            "type": "image_url",
            "image_url": {"url": self.get_image_url(filepath, compress=True)},
            "frame_type": frame_type,
        }
