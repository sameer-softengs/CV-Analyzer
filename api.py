import os
import tempfile
import asyncio
import logging
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

# Load .env before reading any env vars
load_dotenv(dotenv_path=Path(__file__).resolve().parent / ".env", override=False)

# Import existing logic
from ai import (
    analyze_cv_ats,
    assess_cv_document,
    build_debug_info_cv,
    extract_text_from_pdf,
    get_last_pdf_parse_debug,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(
    title="AI Resume Matcher API",
    description="Microservice backend for CV to Job Description matching logic",
    version="1.0.0",
)


def _parse_cors_origins() -> list[str]:
    raw = os.getenv("CORS_ALLOW_ORIGINS", "*")
    origins = [item.strip() for item in raw.split(",") if item.strip()]
    return origins or ["*"]


def _to_int_safe(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (ValueError, TypeError):
        return default


MAX_UPLOAD_MB = _to_int_safe("MAX_UPLOAD_MB", 10)
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024
ANALYZE_CONCURRENCY = max(1, _to_int_safe("ANALYZE_CONCURRENCY", 4))
OPENROUTER_ENABLED_DEFAULT = (
    os.getenv("OPENROUTER_ENABLED", "1").strip().lower() not in {"0", "false", "no"}
)

analyze_semaphore = asyncio.Semaphore(ANALYZE_CONCURRENCY)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_parse_cors_origins(),
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def startup_event():
    api_key = os.getenv("OPENROUTER_API_KEY", "").strip()
    model = os.getenv("OPENROUTER_MODEL", "openai/gpt-4o-mini").strip()
    logger.info(
        "API starting — max_upload=%dMB, concurrency=%d, openrouter=%s, model=%s",
        MAX_UPLOAD_MB,
        ANALYZE_CONCURRENCY,
        "enabled" if OPENROUTER_ENABLED_DEFAULT else "disabled",
        model if api_key else "no-key-configured",
    )


async def _save_upload_to_tempfile(upload: UploadFile, max_bytes: int) -> tuple[str, int]:
    size = 0
    chunk_size = 1024 * 64  # 64 KB chunks for efficiency
    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
        while True:
            chunk = await upload.read(chunk_size)
            if not chunk:
                break
            size += len(chunk)
            if size > max_bytes:
                tmp_path = tmp.name
                # Clean up partial file before raising
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
                raise ValueError(
                    f"Uploaded file exceeds the maximum allowed size of {MAX_UPLOAD_MB} MB."
                )
            tmp.write(chunk)
        return tmp.name, size


@app.post("/analyze")
async def analyze_endpoint(
    cv_file: UploadFile = File(..., description="Resume PDF file"),
    use_llm: str = Form("auto", description="LLM mode: 'on', 'off', or 'auto'"),
):
    # Validate file type
    filename = (cv_file.filename or "").lower()
    if not filename.endswith(".pdf"):
        return JSONResponse(
            status_code=400,
            content={"error": "Only PDF files are accepted. Please upload a .pdf resume."},
        )

    llm_mode = (use_llm or "auto").strip().lower()
    if llm_mode == "on":
        enable_llm = True
    elif llm_mode == "off":
        enable_llm = False
    else:
        enable_llm = OPENROUTER_ENABLED_DEFAULT

    temp_path = None
    try:
        # 1. Stream CV upload to temp file with size guard
        temp_path, file_size = await _save_upload_to_tempfile(cv_file, MAX_UPLOAD_BYTES)
        logger.info("Received file: %s (%.1f KB)", cv_file.filename, file_size / 1024)

        # 2. Concurrency guard for CPU-heavy extraction and scoring
        async with analyze_semaphore:
            cv_text = extract_text_from_pdf(temp_path)

            # 3. Validate document is actually a CV
            val = assess_cv_document(cv_text)
            if not val["is_cv"]:
                return JSONResponse(
                    status_code=422,
                    content={
                        "error": "The uploaded document does not appear to be a standard CV or Resume.",
                        "reasons": val["reasons"],
                        "cv_validation": val,
                    },
                )

            # 4. Analyze CV using ATS + optional LLM
            report = analyze_cv_ats(cv_text, use_llm=enable_llm)
            debug = build_debug_info_cv(cv_text, parsing_debug=get_last_pdf_parse_debug())
            report["cv_validation"] = val

        logger.info(
            "Analysis complete for %s — score=%.1f grade=%s llm_used=%s",
            cv_file.filename,
            report.get("ats_score", 0),
            report.get("grade", "?"),
            report.get("llm", {}).get("used", False),
        )

        # 5. Return JSON package
        return {
            "report": report,
            "debug": debug,
            "meta": {
                "filename": cv_file.filename,
                "file_size_kb": round(file_size / 1024, 1),
                "llm_mode": llm_mode,
                "openrouter_enabled": enable_llm,
            },
        }

    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    except FileNotFoundError as e:
        return JSONResponse(status_code=500, content={"error": f"File processing error: {str(e)}"})
    except Exception as e:
        logger.exception("Unexpected error during analysis for file: %s", cv_file.filename)
        return JSONResponse(
            status_code=500,
            content={"error": f"Internal analysis error. Please try again."},
        )
    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass
        try:
            await cv_file.close()
        except Exception:
            pass


@app.get("/health")
def health_check():
    api_key_set = bool(os.getenv("OPENROUTER_API_KEY", "").strip())
    return {
        "status": "ok",
        "service": "ai-resume-matcher-api",
        "max_upload_mb": MAX_UPLOAD_MB,
        "analyze_concurrency": ANALYZE_CONCURRENCY,
        "openrouter_enabled_default": OPENROUTER_ENABLED_DEFAULT,
        "openrouter_key_configured": api_key_set,
    }
@app.get("/")
async def root():
    return {
        "message": "AI Resume Matcher API is live!",
        "endpoints": {
            "analyze": "/analyze (POST)",
            "health": "/health (GET)",
            "docs": "/docs"
        }
    }
