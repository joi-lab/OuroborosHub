"""
Medical Lab Analyzer — Ouroboros extension plugin.

Registers:
  - Tool: analyze_lab_results — full pipeline from OCR text to structured results
  - UI Tab: declarative widget showing results table with status indicators
"""

from __future__ import annotations

import json
import traceback
from typing import Any

from . import analyzer


# Maximum number of tests to process per analysis (cost/timeout guard)
_MAX_TESTS = 40


def register(api: Any) -> None:
    """Called by the Ouroboros extension loader."""

    # ── LLM bridge via direct provider call ──────────────

    def _call_llm(prompt: str) -> str:
        """Call the LLM through the configured provider using httpx."""
        import httpx

        settings = api.get_settings([
            "OPENROUTER_API_KEY",
            "OPENAI_API_KEY",
            "OPENAI_BASE_URL",
            "OPENAI_COMPATIBLE_API_KEY",
            "OPENAI_COMPATIBLE_BASE_URL",
            "OUROBOROS_MODEL",
        ])

        or_key = settings.get("OPENROUTER_API_KEY", "")
        oai_key = settings.get("OPENAI_API_KEY", "")
        oai_compat_key = settings.get("OPENAI_COMPATIBLE_API_KEY", "")
        model = settings.get("OUROBOROS_MODEL", "google/gemini-3.5-flash")

        if or_key:
            base_url = "https://openrouter.ai/api/v1"
            api_key = or_key
        elif oai_key:
            base_url = settings.get("OPENAI_BASE_URL") or "https://api.openai.com/v1"
            api_key = oai_key
            if model.startswith("openai::"):
                model = model[len("openai::"):]
            elif "::" in model or "/" in model:
                model = "gpt-4o-mini"
        elif oai_compat_key:
            base_url = settings.get("OPENAI_COMPATIBLE_BASE_URL", "")
            if not base_url:
                raise RuntimeError(
                    "OPENAI_COMPATIBLE_BASE_URL not set — "
                    "configure it in Settings."
                )
            api_key = oai_compat_key
            # Strip the provider prefix (e.g. "openai-compatible::model-name")
            if model.startswith("openai-compatible::"):
                model = model[len("openai-compatible::"):]
            elif "::" in model:
                model = model.split("::", 1)[1]
        else:
            raise RuntimeError(
                "No LLM API key available. Configure OPENROUTER_API_KEY, "
                "OPENAI_API_KEY, or OPENAI_COMPATIBLE_API_KEY in Settings."
            )

        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1,
            "max_tokens": 4096,
        }

        resp = httpx.post(
            f"{base_url}/chat/completions",
            json=payload,
            headers={
                "Authorization": f"Bearer {api_key}",
            },
            timeout=90.0,
        )
        resp.raise_for_status()
        body = resp.json()

        choices = body.get("choices", [])
        if not choices:
            raise RuntimeError("LLM returned no choices")
        return choices[0].get("message", {}).get("content", "")

    # Wire the LLM caller into the analyzer module
    analyzer.set_llm_caller(_call_llm)
    api.log("info", "LLM caller configured via direct provider")

    # ── Preflight check ──────────────────────────────────

    def _preflight() -> str | None:
        """Quick check that jinja2 and keys are available."""
        try:
            from jinja2 import Environment  # noqa: F401
        except ImportError:
            return "jinja2 не установлен — выполните pip install jinja2"
        settings = api.get_settings([
            "OPENROUTER_API_KEY",
            "OPENAI_API_KEY",
            "OPENAI_COMPATIBLE_API_KEY",
        ])
        if not (
            settings.get("OPENROUTER_API_KEY")
            or settings.get("OPENAI_API_KEY")
            or settings.get("OPENAI_COMPATIBLE_API_KEY")
        ):
            return "Нет API-ключа LLM (OPENROUTER_API_KEY, OPENAI_API_KEY или OPENAI_COMPATIBLE_API_KEY)"
        return None

    # ── Tool: analyze_lab_results ─────────────────────────

    def analyze_lab_results(text: str = "") -> str:
        """
        Анализ лабораторных исследований из текста OCR.

        Принимает текст результатов анализов (из PDF/фото через OCR или
        напрямую) и возвращает структурированную расшифровку: показатели,
        значения, нормы, статус отклонений и интерпретацию.

        Args:
            text: Текст результатов анализов (OCR или ручной ввод)

        Returns:
            JSON со структурированными результатами и интерпретацией
        """
        text = text.strip()
        if not text:
            return json.dumps(
                {"error": "Текст анализов не предоставлен"},
                ensure_ascii=False,
            )
        if len(text) > 100_000:
            return json.dumps(
                {"error": "Текст анализов слишком большой (максимум 100000 символов)"},
                ensure_ascii=False,
            )

        # Preflight
        err = _preflight()
        if err:
            return json.dumps({"error": err}, ensure_ascii=False)

        try:
            result = analyzer.analyze(text, max_tests=_MAX_TESTS)
            return json.dumps(
                {
                    "status": "ok",
                    "text_type": result.text_type,
                    "patient": result.patient.model_dump(),
                    "tests": [t.model_dump() for t in result.tests],
                    "interpretation": result.interpretation,
                    "tests_count": len(result.tests),
                    "abnormal_count": sum(
                        1
                        for t in result.tests
                        if (t.status or "").lower()
                        in ("повышен", "снижен", "отклонение")
                    ),
                },
                ensure_ascii=False,
                indent=2,
            )
        except Exception as exc:
            api.log("error", f"Analysis error: {exc}\n{traceback.format_exc()}")
            return json.dumps(
                {"error": f"Ошибка анализа: {exc}"},
                ensure_ascii=False,
            )

    api.register_tool(
        name="analyze_lab_results",
        description=(
            "Расшифровка лабораторных анализов из текста OCR. "
            "Принимает текст результатов анализов и возвращает "
            "структурированную таблицу показателей с интерпретацией "
            "отклонений. Основано на промптовой базе Maestro SH."
        ),
        handler=analyze_lab_results,
        schema={
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": (
                        "Текст результатов лабораторных анализов "
                        "(OCR из PDF/фото или ручной ввод)"
                    ),
                },
            },
            "required": ["text"],
        },
        timeout_sec=300,
    )

    # ── UI Widget ─────────────────────────────────────────

    api.register_ui_tab(
        "lab_analyzer",
        "🔬 Анализы",
        render={
            "kind": "declarative",
            "schema_version": 1,
            "components": [
                {
                    "type": "markdown",
                    "content": (
                        "### 🔬 Medical Lab Analyzer\n\n"
                        "Используйте инструмент `analyze_lab_results` "
                        "для расшифровки лабораторных анализов.\n\n"
                        "Вставьте текст OCR результатов анализов в чат "
                        "или передайте через параметр `text`.\n\n"
                        "Результаты будут отображены здесь в виде таблицы "
                        "с цветовой индикацией нормы/отклонений."
                    ),
                },
            ],
        },
    )

    api.log("info", "medical-lab-analyzer extension registered successfully")
