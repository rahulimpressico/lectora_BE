"""
Send source documents to the Azure OpenAI Responses API for TO generation.

The Responses API only accepts PDF as a file input. DOCX files are converted to PDF
first using LibreOffice (``libreoffice --headless``), then all files are attached
inline as base64. No text extraction. No truncation. Full content always sent.

Files are resolved from local paths (upload temp dir or blob cache after Azure download).
"""

from __future__ import annotations

import base64
import logging
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from openai import AzureOpenAI

from lectora_backend.config import settings
from lectora_backend.pipeline.shared_llm_config.model_registry import get_to_file_deployment
from lectora_backend.pipeline.shared_llm_config.tracer import LLMTrace, write_trace

logger = logging.getLogger(__name__)

_RESPONSES_API_VERSION = os.environ.get(
    "AZURE_OPENAI_RESPONSES_API_VERSION", "2025-03-01-preview"
).strip()

_PDF_SUFFIXES  = frozenset({".pdf"})
_DOCX_SUFFIXES = frozenset({".docx", ".doc"})


def _get_responses_client() -> AzureOpenAI:
    if not settings.azure_openai_api_key:
        raise RuntimeError("azure_openai_api_key is not configured.")
    if not settings.azure_openai_endpoint:
        raise RuntimeError("azure_openai_endpoint is not configured.")

    return AzureOpenAI(
        api_key=settings.azure_openai_api_key,
        api_version=_RESPONSES_API_VERSION,
        azure_endpoint=settings.azure_openai_endpoint,
    )


def _collect_paths(file_paths: list[str]) -> list[Path]:
    paths: list[Path] = []
    for raw in file_paths:
        path = Path(raw).expanduser().resolve()
        if not path.is_file():
            logger.warning("[TO files] Skipping missing path: %s", raw)
            continue
        suffix = path.suffix.lower()
        if suffix not in _PDF_SUFFIXES | _DOCX_SUFFIXES:
            logger.warning("[TO files] Skipping unsupported extension: %s", path)
            continue
        paths.append(path)
    return paths


def _find_libreoffice() -> str | None:
    """Return the path to the LibreOffice executable, or None if not found."""
    for candidate in ("libreoffice", "soffice"):
        found = shutil.which(candidate)
        if found:
            return found
    # Common install paths on Linux / Docker images
    for path in (
        "/usr/bin/libreoffice",
        "/usr/bin/soffice",
        "/usr/lib/libreoffice/program/soffice",
        "/opt/libreoffice/program/soffice",
        "/opt/libreoffice25.8/program/soffice",
        "/opt/libreoffice24.8/program/soffice",
        "/opt/libreoffice24.2/program/soffice",
    ):
        if Path(path).is_file():
            return path
    return None


def _convert_docx_to_pdf(docx_path: Path) -> bytes:
    """Convert a DOCX file to PDF bytes using LibreOffice headless mode.

    The Responses API only accepts PDF — DOCX must be converted before sending.
    Raises RuntimeError if LibreOffice is not installed.
    """
    lo = _find_libreoffice()
    if not lo:
        raise RuntimeError(
            "LibreOffice is required to convert DOCX to PDF for TO generation "
            "but was not found on this system. Install it with: "
            "apt-get install -y libreoffice  (Debian/Ubuntu/Docker)"
        )

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        cmd = [lo, "--headless", "--convert-to", "pdf", "--outdir", tmp, str(docx_path)]
        logger.info(
            "[TO files] Converting DOCX → PDF via LibreOffice: %s", docx_path.name
        )
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"LibreOffice conversion failed for {docx_path.name} "
                f"(exit {result.returncode}): {result.stderr.strip()}"
            )
        # LibreOffice writes <stem>.pdf in --outdir
        pdf_path = tmp_path / (docx_path.stem + ".pdf")
        if not pdf_path.is_file():
            raise RuntimeError(
                f"LibreOffice ran but output PDF not found for {docx_path.name}. "
                f"stdout: {result.stdout.strip()}"
            )
        data = pdf_path.read_bytes()
        logger.info(
            "[TO files] DOCX → PDF conversion done (%.1f MB → %.1f MB): %s",
            docx_path.stat().st_size / (1024 * 1024),
            len(data) / (1024 * 1024),
            docx_path.name,
        )
        return data


def _inline_pdf_item(name: str, data: bytes) -> dict:
    """Encode PDF bytes as an inline base64 input_file item."""
    encoded = base64.standard_b64encode(data).decode("ascii")
    return {
        "type": "input_file",
        "filename": name,
        "file_data": f"data:application/pdf;base64,{encoded}",
    }


def _build_user_content(
    user_instructions: str,
    paths: list[Path],
) -> list[dict]:
    """Build Responses API content — all files converted to PDF and attached inline.

    PDFs are attached as-is. DOCX files are converted to PDF via LibreOffice first.
    """
    content: list[dict] = [{"type": "input_text", "text": user_instructions}]
    for path in paths:
        if path.suffix.lower() in _DOCX_SUFFIXES:
            pdf_bytes = _convert_docx_to_pdf(path)
            pdf_name = path.stem + ".pdf"
            logger.info(
                "[TO files] Attaching converted PDF inline (%.1f MB): %s → %s",
                len(pdf_bytes) / (1024 * 1024),
                path.name,
                pdf_name,
            )
            content.append(_inline_pdf_item(pdf_name, pdf_bytes))
        else:
            data = path.read_bytes()
            logger.info(
                "[TO files] Attaching PDF inline (%.1f MB): %s",
                len(data) / (1024 * 1024),
                path.name,
            )
            content.append(_inline_pdf_item(path.name, data))
    return content


def _call_responses_api(
    client: AzureOpenAI,
    *,
    deployment_name: str,
    system_prompt: str,
    content: list[dict],
    max_output_tokens: int | None,
) -> tuple[str, int, int, int]:
    create_kwargs: dict = {
        "model": deployment_name,
        "instructions": system_prompt,
        "input": [{"role": "user", "content": content}],
    }
    if max_output_tokens is not None:
        create_kwargs["max_output_tokens"] = max_output_tokens

    response = client.responses.create(**create_kwargs)
    text = response.output_text.strip()
    if not text:
        raise ValueError("Responses API returned an empty response.")

    prompt_tokens = completion_tokens = total_tokens = 0
    if response.usage:
        prompt_tokens = response.usage.input_tokens or 0
        completion_tokens = response.usage.output_tokens or 0
        total_tokens = response.usage.total_tokens or (
            prompt_tokens + completion_tokens
        )
    return text, prompt_tokens, completion_tokens, total_tokens


def chat_with_uploaded_files(
    system_prompt: str,
    user_instructions: str,
    file_paths: list[str],
    *,
    deployment: str | None = None,
    max_output_tokens: int | None = 65536,
    agent: str = "A0",
) -> str:
    """Send all source files to the Responses API for TO generation.

    Every file (PDF and DOCX) is attached inline as base64 — no Files API upload,
    no text extraction, no truncation. All original bytes sent in one request.

    Raises:
        ValueError: No valid files found, or LLM returned empty/invalid response.
    """
    client = _get_responses_client()
    deployment_name = deployment or get_to_file_deployment()
    paths = _collect_paths(file_paths)

    if not paths:
        raise ValueError(
            "No valid PDF or DOCX source files found for TO generation. "
            f"Checked: {file_paths}"
        )

    logger.info(
        "[TO files] Sending %d file(s) to LLM (deployment=%s): %s",
        len(paths),
        deployment_name,
        ", ".join(f"{p.name} ({p.stat().st_size / (1024*1024):.1f} MB)" for p in paths),
    )

    t_start = time.perf_counter()
    error_msg: str | None = None
    response_text = ""
    prompt_tokens = completion_tokens = total_tokens = 0

    trace_user_msg = user_instructions
    trace_user_msg += "\n\n[Source files: " + ", ".join(p.name for p in paths) + "]"

    try:
        content = _build_user_content(user_instructions, paths)

        (
            response_text,
            prompt_tokens,
            completion_tokens,
            total_tokens,
        ) = _call_responses_api(
            client,
            deployment_name=deployment_name,
            system_prompt=system_prompt,
            content=content,
            max_output_tokens=max_output_tokens,
        )

        logger.info(
            "[TO files] Responses API succeeded "
            "(tokens: prompt=%d, completion=%d, total=%d).",
            prompt_tokens, completion_tokens, total_tokens,
        )
        return response_text

    except Exception as exc:
        error_msg = str(exc)
        raise
    finally:
        latency_ms = (time.perf_counter() - t_start) * 1000
        write_trace(
            LLMTrace(
                agent=agent,
                deployment=deployment_name,
                system_prompt=system_prompt,
                user_msg=trace_user_msg,
                response=response_text,
                latency_ms=latency_ms,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                error=error_msg,
            )
        )
