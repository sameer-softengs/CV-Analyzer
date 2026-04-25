import argparse
import html
import hashlib
import json
import math
import os
import re
from collections import Counter
from functools import lru_cache
from urllib.error import URLError, HTTPError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import fitz


STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "has", "have", "in",
    "into", "is", "it", "its", "of", "on", "or", "that", "the", "their", "this", "to", "was",
    "were", "will", "with", "your", "you", "we", "our", "about", "across", "using", "use",
    "required", "preferred", "must", "strong", "excellent", "skills", "skill", "role", "job",
    "responsibilities", "responsibility", "experience", "years", "year", "candidate"
}


CV_POSITIVE_MARKERS = {
    "resume", "curriculum vitae", "experience", "work experience", "employment", "education",
    "skills", "technical skills", "projects", "certifications", "summary", "profile", "achievements"
}


CV_NEGATIVE_MARKERS = {
    "lab manual", "course code", "assignment", "chapter", "theorem", "algorithm",
    "bibliography", "experiment", "objective questions", "question paper", "table of contents"
}


LOW_SIGNAL_JD_TERMS = {
    "ability", "abilities", "background", "business", "collaborate", "collaboration",
    "communication", "company", "culture", "deliver", "detail", "environment",
    "fast", "good", "great", "high", "highly", "including", "learn", "learning",
    "manage", "management", "multiple", "organizational", "passion", "preferred",
    "proven", "quality", "self", "team", "timely", "work", "working", "written",
    "include", "includes", "building", "optimizing", "develop", "developer"
}


PRIORITY_CONTEXT_MARKERS = {
    "required", "requirements", "must", "must have", "qualifications", "responsibilities",
    "skills", "technical", "tech stack", "tools", "experience with", "expertise"
}


TECH_KEYWORD_HINTS = {
    "python", "java", "sql", "excel", "tableau", "power bi", "aws", "azure", "gcp",
    "docker", "kubernetes", "terraform", "spark", "hadoop", "linux", "git", "react",
    "node.js", "node", "pandas", "numpy", "fastapi", "django", "flask", "salesforce", "sap"
}


DOMAIN_KEYWORD_HINTS = {
    "api", "apis", "backend", "frontend", "microservices", "pipeline", "pipelines",
    "etl", "analytics", "analysis", "database", "databases", "devops", "cicd", "ci/cd"
}


OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"
LLM_CACHE = {}
LLM_CACHE_MAX_ITEMS = 128


def _is_high_signal_keyword(token):
    if token in TECH_KEYWORD_HINTS or token in DOMAIN_KEYWORD_HINTS:
        return True
    return bool(re.search(r"[+#./\d]", token))


def extract_text_from_pdf(pdf_path):
    if not os.path.exists(pdf_path):
        raise FileNotFoundError(f"PDF not found: {pdf_path}")

    doc = fitz.open(pdf_path)
    text = " ".join(page.get_text() for page in doc)
    if not text.strip():
        raise ValueError("PDF has no extractable text. It might be scanned or image-only.")
    return text


def _extract_text_from_json_payload(payload):
    candidates = []

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                lk = str(key).lower()
                if lk in {
                    "description", "job_description", "jobdescription", "content",
                    "details", "summary", "body", "requirements", "responsibilities"
                } and isinstance(value, str):
                    candidates.append(value)
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)
    if candidates:
        return "\n\n".join(candidates)
    return json.dumps(payload, ensure_ascii=False)


def _extract_text_from_html(raw_html):
    meta_snippets = re.findall(
        r'(?is)<meta[^>]+(?:name|property)=["\'](?:description|og:description|twitter:description)["\'][^>]+content=["\'](.*?)["\'][^>]*>',
        raw_html,
    )

    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", raw_html)
    text = re.sub(r"(?is)<br\s*/?>", "\n", text)
    text = re.sub(r"(?is)</p>|</div>|</li>|</h\d>", "\n", text)
    text = re.sub(r"(?is)<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()

    if meta_snippets:
        meta_text = " ".join(html.unescape(m).strip() for m in meta_snippets if m and m.strip())
        if meta_text:
            text = f"{meta_text} {text}".strip()

    return text


def fetch_text_from_url(url, timeout=20, max_chars=None, min_words=12):
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Only http/https links are supported for JD fetching.")

    req = Request(url, headers={"User-Agent": "Mozilla/5.0 (ATS-Analyzer/1.0)"})
    try:
        with urlopen(req, timeout=timeout) as response:
            content_type = (response.headers.get("Content-Type") or "").lower()
            raw = response.read() if max_chars is None else response.read(max_chars + 1024)
    except HTTPError as e:
        raise ValueError(f"Failed to fetch URL (HTTP {e.code}).") from e
    except URLError as e:
        raise ValueError(f"Failed to fetch URL: {e.reason}") from e

    if max_chars is not None and len(raw) > max_chars:
        raw = raw[:max_chars]

    decoded = raw.decode("utf-8", errors="ignore")
    if not decoded.strip():
        decoded = raw.decode("latin-1", errors="ignore")

    if "application/json" in content_type:
        try:
            payload = json.loads(decoded)
            text = _extract_text_from_json_payload(payload)
        except json.JSONDecodeError:
            text = decoded
    elif "text/html" in content_type or "<html" in decoded.lower():
        text = _extract_text_from_html(decoded)
    else:
        text = decoded

    text = re.sub(r"\s+", " ", text).strip()
    if len(text.split()) < min_words:
        raise ValueError("Fetched content is too short to be a valid job description.")
    return text


def normalize_text(text):
    text = text.replace("\u00a0", " ")
    text = text.replace("\r", "\n")
    text = re.sub(r"\s+", " ", text)
    return text.strip().lower()


def _to_float_env(name, default):
    raw = os.getenv(name, str(default))
    try:
        return float(raw)
    except (TypeError, ValueError):
        return float(default)


def _to_int_env(name, default):
    raw = os.getenv(name, str(default))
    try:
        return int(raw)
    except (TypeError, ValueError):
        return int(default)


def _clip(value, minimum, maximum):
    return max(min(value, maximum), minimum)


def _safe_float(value, fallback):
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(fallback)


def _truncate_for_prompt(text, max_chars=5500):
    text = (text or "").strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n...[truncated for model context]..."


def _extract_first_json_object(text):
    if not text:
        return None

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None

    candidate = text[start:end + 1]
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        return None


def _llm_cache_get(cache_key):
    return LLM_CACHE.get(cache_key)


def _llm_cache_put(cache_key, value):
    if cache_key in LLM_CACHE:
        LLM_CACHE[cache_key] = value
        return

    if len(LLM_CACHE) >= LLM_CACHE_MAX_ITEMS:
        oldest_key = next(iter(LLM_CACHE))
        del LLM_CACHE[oldest_key]
    LLM_CACHE[cache_key] = value


def analyze_with_openrouter(cv_text, jd_text, base_report):
    api_key = (os.getenv("OPENROUTER_API_KEY") or "").strip()
    if not api_key:
        return None

    model = (os.getenv("OPENROUTER_MODEL") or "openai/gpt-4o-mini").strip()
    timeout_seconds = _clip(_to_int_env("OPENROUTER_TIMEOUT_SECONDS", 18), 5, 90)

    prompt_cv = _truncate_for_prompt(cv_text, max_chars=6000)
    prompt_jd = _truncate_for_prompt(jd_text, max_chars=4500)
    payload_fingerprint = hashlib.sha256(
        (model + "\n" + prompt_cv + "\n" + prompt_jd).encode("utf-8", errors="ignore")
    ).hexdigest()

    cached = _llm_cache_get(payload_fingerprint)
    if cached:
        return cached

    system_prompt = (
        "You are an expert ATS evaluator. "
        "Return ONLY valid JSON with no markdown fences."
    )
    user_prompt = {
        "task": "Evaluate resume-to-JD alignment and suggest concrete fixes.",
        "output_schema": {
            "overall_alignment_score": "number 0-100",
            "confidence": "number 0-1",
            "summary": "string max 220 chars",
            "strengths": ["array of 3 short strings"],
            "gaps": ["array of up to 5 short strings"],
            "missing_keywords": ["array of up to 20 exact or near-exact JD terms missing from CV"],
            "improvement_actions": ["array of up to 5 concise action bullets"],
            "critical_mistakes": ["array of up to 4 specific mistakes/errors found in the CV causing low rating"]
        },
        "resume_text": prompt_cv,
        "job_description_text": prompt_jd,
        "deterministic_context": {
            "ats_score": base_report.get("ats_score"),
            "component_scores": base_report.get("component_scores"),
            "missing_sections": base_report.get("missing_sections"),
            "known_missing_keywords": base_report.get("keyword_coverage", {}).get("missing_top", []),
        },
        "rules": [
            "Penalize keyword stuffing and reward evidence-based achievements.",
            "Prefer measurable and role-specific recommendations.",
            "Use practical ATS language."
        ]
    }

    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(user_prompt, ensure_ascii=False)},
        ],
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": (os.getenv("OPENROUTER_SITE_URL") or "http://localhost").strip(),
        "X-Title": (os.getenv("OPENROUTER_APP_NAME") or "Resume Analyzer").strip(),
    }

    req = Request(
        OPENROUTER_API_URL,
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
        method="POST"
    )

    try:
        with urlopen(req, timeout=timeout_seconds) as response:
            raw = response.read().decode("utf-8", errors="ignore")
    except Exception:
        return None

    try:
        response_json = json.loads(raw)
    except json.JSONDecodeError:
        return None

    choices = response_json.get("choices") or []
    if not choices:
        return None

    message = choices[0].get("message") or {}
    content = message.get("content")
    parsed = None

    if isinstance(content, list):
        merged = "".join(
            item.get("text", "")
            for item in content
            if isinstance(item, dict)
        )
        parsed = _extract_first_json_object(merged)
    elif isinstance(content, str):
        parsed = _extract_first_json_object(content)

    if not isinstance(parsed, dict):
        return None

    score = _clip(_safe_float(parsed.get("overall_alignment_score"), 0), 0.0, 100.0)
    confidence = _clip(_safe_float(parsed.get("confidence"), 0.65), 0.0, 1.0)

    normalized = {
        "model": model,
        "overall_alignment_score": round(score, 2),
        "confidence": round(confidence, 3),
        "summary": str(parsed.get("summary", "")).strip()[:300],
        "strengths": [str(x).strip() for x in (parsed.get("strengths") or []) if str(x).strip()][:5],
        "gaps": [str(x).strip() for x in (parsed.get("gaps") or []) if str(x).strip()][:8],
        "missing_keywords": [str(x).strip().lower() for x in (parsed.get("missing_keywords") or []) if str(x).strip()][:24],
        "improvement_actions": [str(x).strip() for x in (parsed.get("improvement_actions") or []) if str(x).strip()][:6],
        "critical_mistakes": [str(x).strip() for x in (parsed.get("critical_mistakes") or []) if str(x).strip()][:5],
    }

    _llm_cache_put(payload_fingerprint, normalized)
    return normalized


def analyze_cv_only_with_openrouter(cv_text, base_report):
    api_key = (os.getenv("OPENROUTER_API_KEY") or "").strip()
    if not api_key:
        return None

    model = (os.getenv("OPENROUTER_MODEL") or "openai/gpt-4o-mini").strip()
    timeout_seconds = _clip(_to_int_env("OPENROUTER_TIMEOUT_SECONDS", 18), 5, 90)

    prompt_cv = _truncate_for_prompt(cv_text, max_chars=7000)
    payload_fingerprint = hashlib.sha256(
        ("cv-only\n" + model + "\n" + prompt_cv).encode("utf-8", errors="ignore")
    ).hexdigest()
    cached = _llm_cache_get(payload_fingerprint)
    if cached:
        return cached

    system_prompt = (
        "You are an ATS expert evaluator. "
        "Score the CV itself for ATS readiness and hiring clarity. "
        "Return ONLY valid JSON."
    )
    user_prompt = {
        "task": "Analyze this resume without any job description and produce ATS-quality scoring and fixes.",
        "output_schema": {
            "ats_score": "number 0-100",
            "confidence": "number 0-1",
            "summary": "string max 220 chars",
            "strengths": ["array of 3-5 short strings"],
            "gaps": ["array of 3-8 short strings"],
            "missing_keywords": ["array of up to 20 ATS-relevant terms to consider adding"],
            "improvement_actions": ["array of up to 6 concise and practical bullets"],
            "critical_mistakes": ["array of up to 4 specific mistakes/errors found in the CV causing low rating"]
        },
        "resume_text": prompt_cv,
        "deterministic_context": base_report,
        "rules": [
            "Reward evidence, impact, and structure.",
            "Penalize weak formatting, vague bullets, and missing ATS sections.",
            "Avoid fluff and keep recommendations concrete."
        ]
    }

    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(user_prompt, ensure_ascii=False)},
        ],
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": (os.getenv("OPENROUTER_SITE_URL") or "http://localhost").strip(),
        "X-Title": (os.getenv("OPENROUTER_APP_NAME") or "Resume Analyzer").strip(),
    }

    req = Request(
        OPENROUTER_API_URL,
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
        method="POST"
    )

    try:
        with urlopen(req, timeout=timeout_seconds) as response:
            raw = response.read().decode("utf-8", errors="ignore")
    except Exception:
        return None

    try:
        response_json = json.loads(raw)
    except json.JSONDecodeError:
        return None

    choices = response_json.get("choices") or []
    if not choices:
        return None

    message = choices[0].get("message") or {}
    content = message.get("content")
    parsed = None

    if isinstance(content, list):
        merged = "".join(
            item.get("text", "")
            for item in content
            if isinstance(item, dict)
        )
        parsed = _extract_first_json_object(merged)
    elif isinstance(content, str):
        parsed = _extract_first_json_object(content)

    if not isinstance(parsed, dict):
        return None

    normalized = {
        "model": model,
        "ats_score": round(_clip(_safe_float(parsed.get("ats_score"), 0), 0.0, 100.0), 2),
        "confidence": round(_clip(_safe_float(parsed.get("confidence"), 0.65), 0.0, 1.0), 3),
        "summary": str(parsed.get("summary", "")).strip()[:300],
        "strengths": [str(x).strip() for x in (parsed.get("strengths") or []) if str(x).strip()][:5],
        "gaps": [str(x).strip() for x in (parsed.get("gaps") or []) if str(x).strip()][:8],
        "missing_keywords": [str(x).strip().lower() for x in (parsed.get("missing_keywords") or []) if str(x).strip()][:24],
        "improvement_actions": [str(x).strip() for x in (parsed.get("improvement_actions") or []) if str(x).strip()][:6],
        "critical_mistakes": [str(x).strip() for x in (parsed.get("critical_mistakes") or []) if str(x).strip()][:5],
    }

    _llm_cache_put(payload_fingerprint, normalized)
    return normalized


def tokenize(text):
    return re.findall(r"[a-z][a-z0-9+#./-]{1,}", text.lower())


def _text_to_tfidf_vector(texts):
    tokenized = []
    for text in texts:
        tokens = [t for t in tokenize(text) if t not in STOP_WORDS]
        tokenized.append(tokens)

    n_docs = len(tokenized)
    df = Counter()
    for tokens in tokenized:
        for term in set(tokens):
            df[term] += 1

    vectors = []
    for tokens in tokenized:
        tf = Counter(tokens)
        total = max(len(tokens), 1)
        vec = {}
        for term, count in tf.items():
            idf = math.log((1 + n_docs) / (1 + df[term])) + 1
            vec[term] = (count / total) * idf
        vectors.append(vec)

    return vectors


def _cosine_dict(vec_a, vec_b):
    if not vec_a or not vec_b:
        return 0.0

    common = set(vec_a) & set(vec_b)
    dot = sum(vec_a[k] * vec_b[k] for k in common)
    norm_a = math.sqrt(sum(v * v for v in vec_a.values()))
    norm_b = math.sqrt(sum(v * v for v in vec_b.values()))

    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def infer_expected_years(jd_text):
    jd = jd_text.lower()

    range_match = re.search(r"(\d{1,2})\s*(?:-|to)\s*(\d{1,2})\s+years", jd)
    if range_match:
        low = int(range_match.group(1))
        high = int(range_match.group(2))
        return (low + high) / 2

    min_match = re.search(r"(?:minimum|min\.?)\s*(\d{1,2})\+?\s+years", jd)
    if min_match:
        return float(min_match.group(1))

    simple_match = re.search(r"(\d{1,2})\+?\s+years", jd)
    if simple_match:
        return float(simple_match.group(1))

    if any(x in jd for x in ["senior", "lead", "principal", "manager", "head"]):
        return 6.0

    return 2.0


def _extract_year_signals(cv_text):
    text = cv_text.lower()

    explicit_years = [int(n) for n in re.findall(r"(\d{1,2})\+?\s+years", text)]
    best_explicit = max(explicit_years) if explicit_years else 0

    spans = []
    for start, end in re.findall(r"\b(19\d{2}|20\d{2})\s*(?:-|to|–)\s*(present|19\d{2}|20\d{2})\b", text):
        s = int(start)
        e = 2026 if end == "present" else int(end)
        if 1980 <= s <= e <= 2026:
            spans.append(e - s)

    span_estimate = sum(y for y in spans if y > 0)
    if span_estimate > 25:
        span_estimate = 25

    return {
        "explicit_year_mentions": explicit_years,
        "best_explicit_years": best_explicit,
        "detected_date_spans": spans,
        "span_estimate_years": span_estimate
    }


def estimate_experience_years(cv_text, return_details=False):
    details = _extract_year_signals(cv_text)
    estimate = max(details["best_explicit_years"], details["span_estimate_years"])
    if return_details:
        return estimate, details
    return estimate


def assess_cv_document(text):
    raw = text or ""
    normalized = normalize_text(raw)

    words = len(normalized.split())
    if words < 80:
        return {
            "is_cv": False,
            "confidence": 0.0,
            "reasons": ["Document text is too short to be a resume."],
            "signals": {
                "word_count": words,
                "positive_marker_hits": [],
                "negative_marker_hits": [],
                "email_found": False,
                "phone_found": False,
                "date_span_found": False,
            },
        }

    positive_hits = [m for m in CV_POSITIVE_MARKERS if m in normalized]
    negative_hits = [m for m in CV_NEGATIVE_MARKERS if m in normalized]

    has_email = bool(re.search(r"[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}", normalized))
    has_phone = bool(re.search(r"(?:\+?\d[\d\s().-]{8,}\d)", normalized))
    has_date_span = bool(re.search(r"\b(19\d{2}|20\d{2})\s*(?:-|to|–)\s*(present|19\d{2}|20\d{2})\b", normalized))

    score = 0
    score += min(len(positive_hits) * 8, 40)
    score += 18 if has_email else 0
    score += 14 if has_phone else 0
    score += 14 if has_date_span else 0
    score -= min(len(negative_hits) * 10, 40)

    confidence = max(min(score, 100), 0)
    is_cv = confidence >= 45 and len(negative_hits) < 3

    reasons = []
    if not is_cv:
        if len(negative_hits) >= 3:
            reasons.append("Document contains multiple non-resume indicators.")
        if not has_email:
            reasons.append("No email/contact signal detected.")
        if not has_phone:
            reasons.append("No phone/contact number detected.")
        if len(positive_hits) < 3:
            reasons.append("Insufficient resume section markers detected.")
    else:
        reasons.append("Document structure and contact/date signals look like a CV.")

    return {
        "is_cv": is_cv,
        "confidence": round(confidence, 2),
        "reasons": reasons,
        "signals": {
            "word_count": words,
            "positive_marker_hits": positive_hits[:12],
            "negative_marker_hits": negative_hits[:12],
            "email_found": has_email,
            "phone_found": has_phone,
            "date_span_found": has_date_span,
        },
    }


@lru_cache(maxsize=128)
def extract_jd_keywords(jd_text, max_keywords=None):
    jd_normalized = normalize_text(jd_text)
    sentences = [
        s.strip() for s in re.split(r"[\n.;:!?]+", jd_text.lower())
        if s and s.strip()
    ]

    weighted_freq = Counter()
    for sentence in sentences:
        tokens = re.findall(r"\b[a-z][a-z0-9+#./-]{2,}\b", sentence)
        if not tokens:
            continue

        has_priority_context = any(marker in sentence for marker in PRIORITY_CONTEXT_MARKERS)
        base_weight = 2.0 if has_priority_context else 1.0

        for token in tokens:
            if token in STOP_WORDS or token in LOW_SIGNAL_JD_TERMS:
                continue
            weighted_freq[token] += base_weight

        for i in range(len(tokens) - 1):
            left = tokens[i]
            right = tokens[i + 1]
            if left in STOP_WORDS or right in STOP_WORDS:
                continue
            if left in LOW_SIGNAL_JD_TERMS or right in LOW_SIGNAL_JD_TERMS:
                continue
            if not (_is_high_signal_keyword(left) or _is_high_signal_keyword(right)):
                continue
            weighted_freq[f"{left} {right}"] += base_weight + 0.3

    special_terms = re.findall(
        r"\b(?:c\+\+|c#|node\.js|power\s*bi|machine\s*learning|data\s*analysis|project\s*management|salesforce|sap|tableau)\b",
        jd_normalized,
    )
    for term in special_terms:
        weighted_freq[term] += 3.0

    ranked = []
    for term, score in weighted_freq.items():
        if len(term) < 3:
            continue

        token_count = len(term.split())
        is_tech_term = term in TECH_KEYWORD_HINTS or any(h in term for h in TECH_KEYWORD_HINTS)
        has_high_signal_token = any(_is_high_signal_keyword(part) for part in term.split())
        appears_multiple_times = score >= 2.0
        is_priority_phrase = token_count >= 2 and score >= 1.6 and has_high_signal_token

        # Keep only high-signal terms to avoid diluting ATS matching.
        if not (is_tech_term or is_priority_phrase or (appears_multiple_times and has_high_signal_token)):
            continue

        rank_score = score
        if is_tech_term:
            rank_score += 2.2
        if token_count >= 2:
            rank_score += 0.6
        if token_count >= 3:
            rank_score += 0.3

        ranked.append((term, rank_score))

    ranked.sort(key=lambda item: (-item[1], item[0]))
    sorted_terms = [term for term, _ in ranked]

    if not sorted_terms:
        raw_terms = re.findall(r"\b[a-z][a-z0-9+#./-]{2,}\b", jd_normalized)
        fallback = [t for t in raw_terms if t not in STOP_WORDS and t not in LOW_SIGNAL_JD_TERMS]
        sorted_terms = [term for term, _ in Counter(fallback).most_common() if len(term) >= 3]

    if max_keywords is None or max_keywords <= 0:
        return sorted_terms
    return sorted_terms[:max_keywords]


def keyword_match_score(cv_text, jd_keywords):
    cv = normalize_text(cv_text)

    matched = []
    missing = []

    for kw in jd_keywords:
        pattern = r"\b" + re.escape(kw) + r"\b"
        if re.search(pattern, cv):
            matched.append(kw)
        else:
            missing.append(kw)

    coverage = (len(matched) / max(len(jd_keywords), 1)) * 100
    return coverage, matched, missing


def semantic_relevance_score(cv_text, jd_text):
    vectors = _text_to_tfidf_vector([cv_text, jd_text])
    score = _cosine_dict(vectors[0], vectors[1]) * 100
    return float(score)


def detect_sections(cv_text):
    text = cv_text.lower()

    required_sections = {
        "contact": ["contact", "email", "phone", "mobile"],
        "summary": ["summary", "profile", "objective", "about me"],
        "experience": ["experience", "work history", "employment"],
        "education": ["education", "qualification", "degree"],
        "skills": ["skills", "technical skills", "competencies"]
    }

    optional_sections = {
        "projects": ["projects", "portfolio"],
        "certifications": ["certifications", "certificates", "courses"],
        "achievements": ["achievements", "awards", "accomplishments"]
    }

    found_required = []
    missing_required = []
    for section, markers in required_sections.items():
        if any(m in text for m in markers):
            found_required.append(section)
        else:
            missing_required.append(section)

    found_optional = [
        section for section, markers in optional_sections.items()
        if any(m in text for m in markers)
    ]

    return {
        "required_found": found_required,
        "required_missing": missing_required,
        "optional_found": found_optional
    }


def section_score(cv_text):
    section_status = detect_sections(cv_text)
    required_hits = len(section_status["required_found"])
    missing_required = section_status["required_missing"]
    optional_hits = len(section_status["optional_found"])

    # 85 points from required sections, 15 from optional sections.
    required_part = (required_hits / 5) * 85
    optional_part = (optional_hits / 3) * 15
    score = required_part + optional_part

    return score, missing_required


def achievements_score(cv_text):
    text = cv_text.lower()
    words = max(len(text.split()), 1)

    quantified_patterns = [
        r"\b\d+%\b",
        r"\b\d+(?:\.\d+)?\s*(?:k|m|b|million|billion)\b",
        r"\b(?:usd|eur|pkr|inr|\$)\s*\d+",
        r"\b(?:increased|reduced|improved|boosted|decreased|saved|grew)\b"
    ]

    hits = 0
    for pattern in quantified_patterns:
        hits += len(re.findall(pattern, text))

    density = (hits / words) * 1000
    # Around 12+ quantified mentions per 1000 words gives a full score.
    return min((density / 12) * 100, 100)


def readability_score(cv_text):
    raw = cv_text
    text = cv_text.lower()
    words = len(text.split())

    # Length quality.
    if words < 180:
        length_score = 35
    elif words < 320:
        length_score = 65
    elif words <= 950:
        length_score = 100
    elif words <= 1300:
        length_score = 75
    else:
        length_score = 45

    # ATS parsers like clean symbols and less OCR noise.
    gid_noise = len(re.findall(r"gid\d+", text))
    odd_symbol_density = len(re.findall(r"[^a-z0-9\s\n.,:/+()\-]", raw)) / max(len(raw), 1)

    noise_penalty = min(gid_noise * 1.5 + odd_symbol_density * 100, 60)
    score = max(length_score - noise_penalty, 0)
    return score


def detect_mistakes(cv_text, component_scores, missing_sections):
    mistakes = []
    text = cv_text.lower()
    
    # 1. Missing Contact Info
    if not re.search(r"[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}", text):
        mistakes.append("Missing Contact Email: Recruiters cannot reach you.")
    if not re.search(r"(?:\+?\d[\d\s().-]{8,}\d)", text):
        mistakes.append("Missing Phone Number: Essential for outreach and scheduling.")
        
    # 2. Missing Sections
    if "experience" in missing_sections:
        mistakes.append("Missing Experience Section: The core of your career story is absent.")
    if "skills" in missing_sections:
        mistakes.append("Missing Skills Section: Critical technical competencies are hard to verify.")
        
    # 3. Quantified achievements
    if component_scores.get("achievements", 100) < 40:
        mistakes.append("Weak Impact Evidence: Your bullets lack numbers, percentages, or data to prove impact.")
        
    # 4. Length/Readability
    words = len(text.split())
    if words < 200:
        mistakes.append("CV is significantly too short: Lacks the depth required for professional evaluation.")
    elif words > 1600:
        mistakes.append("CV is excessively long: Information density is low; aim for concise 1-2 page structure.")
        
    return mistakes


def build_recommendations(component_scores, missing_keywords, missing_sections):
    recs = []

    low = sorted(component_scores.items(), key=lambda x: x[1])
    weakest = [name for name, _ in low[:3]]

    if "hard_skills" in weakest and missing_keywords:
        recs.append("Add a dedicated Core Skills section containing exact JD terms, especially: " + ", ".join(missing_keywords[:8]))

    if "experience_evidence" in weakest:
        recs.append("Make work experience more role-aligned: add similar job title keywords and timeline clarity with total years.")

    if "structure" in weakest and missing_sections:
        recs.append("Add missing ATS-critical sections: " + ", ".join(missing_sections) + ".")

    if "achievements" in weakest:
        recs.append("Convert responsibilities into impact bullets with numbers (%, revenue, time saved, volume, team size).")

    if "readability" in weakest:
        recs.append("Simplify formatting and remove noisy symbols or OCR artifacts to improve parser readability.")

    if "relevance" in weakest:
        recs.append("Increase role relevance by adding domain-specific tools, methodologies, and repeated JD language in experience bullets.")

    if not recs:
        recs.append("Good overall alignment. Fine-tune by adding more quantified outcomes and role-specific keywords.")

    return recs[:5]


def analyze_cv_against_jd(cv_text, jd_text, keyword_limit=None, use_llm=True):
    relevance = semantic_relevance_score(cv_text, jd_text)

    jd_keywords = extract_jd_keywords(jd_text, max_keywords=keyword_limit)
    skill_score, matched_keywords, missing_keywords = keyword_match_score(cv_text, jd_keywords)

    expected_years = infer_expected_years(jd_text)
    estimated_years = estimate_experience_years(cv_text)
    experience_score = min((estimated_years / max(expected_years, 1)) * 100, 100)

    structure, missing_sections = section_score(cv_text)
    achievement = achievements_score(cv_text)
    readability = readability_score(cv_text)

    component_scores = {
        "relevance": relevance,
        "hard_skills": skill_score,
        "experience_evidence": experience_score,
        "structure": structure,
        "achievements": achievement,
        "readability": readability
    }

    weights = {
        "relevance": 0.35,
        "hard_skills": 0.25,
        "experience_evidence": 0.15,
        "structure": 0.10,
        "achievements": 0.10,
        "readability": 0.05
    }

    deterministic_score = sum(component_scores[k] * weights[k] for k in component_scores)
    deterministic_score = round(min(max(deterministic_score, 0), 100), 2)

    ats_score = deterministic_score
    llm_analysis = None
    llm_blend_weight = _clip(_to_float_env("OPENROUTER_BLEND_WEIGHT", 0.25), 0.0, 0.5)
    llm_used = False

    if use_llm:
        base_report = {
            "ats_score": deterministic_score,
            "component_scores": {k: round(v, 2) for k, v in component_scores.items()},
            "missing_sections": missing_sections,
            "keyword_coverage": {
                "missing_top": missing_keywords[:20]
            },
        }
        llm_analysis = analyze_with_openrouter(cv_text, jd_text, base_report)
        if llm_analysis:
            llm_used = True
            llm_confidence = _clip(_safe_float(llm_analysis.get("confidence"), 0.65), 0.0, 1.0)
            effective_weight = llm_blend_weight * (0.5 + 0.5 * llm_confidence)
            llm_score = _clip(_safe_float(llm_analysis.get("overall_alignment_score"), deterministic_score), 0.0, 100.0)
            ats_score = (deterministic_score * (1.0 - effective_weight)) + (llm_score * effective_weight)
            ats_score = round(min(max(ats_score, 0), 100), 2)

    recommendations = build_recommendations(
        component_scores,
        missing_keywords,
        missing_sections
    )
    if llm_analysis and llm_analysis.get("improvement_actions"):
        llm_actions = [x for x in llm_analysis["improvement_actions"] if x]
        recommendations = (llm_actions + recommendations)[:6]

    merged_missing_keywords = list(missing_keywords)
    if llm_analysis and llm_analysis.get("missing_keywords"):
        seen = set(merged_missing_keywords)
        for kw in llm_analysis["missing_keywords"]:
            if kw not in seen:
                merged_missing_keywords.append(kw)
                seen.add(kw)

    grade = (
        "A" if ats_score >= 85 else
        "B" if ats_score >= 70 else
        "C" if ats_score >= 55 else
        "D" if ats_score >= 40 else
        "F"
    )

    # Mistakes detection
    mistakes = detect_mistakes(cv_text, component_scores, missing_sections)
    if llm_analysis and llm_analysis.get("critical_mistakes"):
        llm_mistakes = [x for x in llm_analysis["critical_mistakes"] if x]
        # Merge and unique
        seen = set(mistakes)
        for m in llm_mistakes:
            if m not in seen:
                mistakes.append(m)
                seen.add(m)

    return {
        "ats_score": ats_score,
        "grade": grade,
        "deterministic_score": deterministic_score,
        "component_scores": {k: round(v, 2) for k, v in component_scores.items()},
        "experience": {
            "expected_years_from_jd": round(expected_years, 2),
            "estimated_years_from_cv": round(estimated_years, 2)
        },
        "keyword_coverage": {
            "required_keywords_evaluated": len(jd_keywords),
            "matched": len(matched_keywords),
            "missing": len(merged_missing_keywords),
            "evaluated_keywords": jd_keywords,
            "matched_keywords": matched_keywords,
            "missing_keywords": merged_missing_keywords,
            "matched_top": matched_keywords[:20],
            "missing_top": merged_missing_keywords[:20]
        },
        "missing_sections": missing_sections,
        "recommendations": recommendations,
        "mistakes": mistakes[:8],
        "llm": {
            "used": llm_used,
            "model": (llm_analysis or {}).get("model"),
            "blend_weight": llm_blend_weight,
            "summary": (llm_analysis or {}).get("summary"),
            "strengths": (llm_analysis or {}).get("strengths", []),
            "gaps": (llm_analysis or {}).get("gaps", []),
            "raw_alignment_score": (llm_analysis or {}).get("overall_alignment_score"),
            "confidence": (llm_analysis or {}).get("confidence"),
        }
    }


def analyze_cv_ats(cv_text, use_llm=True):
    structure, missing_sections = section_score(cv_text)
    achievement = achievements_score(cv_text)
    readability = readability_score(cv_text)
    estimated_years = estimate_experience_years(cv_text)

    experience_evidence = _clip((estimated_years / 5.0) * 100.0, 0.0, 100.0)
    component_scores = {
        "structure": structure,
        "achievements": achievement,
        "readability": readability,
        "experience_evidence": experience_evidence,
    }
    weights = {
        "structure": 0.30,
        "achievements": 0.30,
        "readability": 0.20,
        "experience_evidence": 0.20,
    }
    deterministic_score = round(
        min(max(sum(component_scores[k] * weights[k] for k in component_scores), 0), 100), 2
    )

    llm_analysis = None
    llm_used = False
    ats_score = deterministic_score
    if use_llm:
        llm_analysis = analyze_cv_only_with_openrouter(
            cv_text,
            base_report={
                "deterministic_score": deterministic_score,
                "component_scores": {k: round(v, 2) for k, v in component_scores.items()},
                "missing_sections": missing_sections,
                "estimated_years_from_cv": round(estimated_years, 2),
            }
        )
        if llm_analysis:
            llm_used = True
            ats_score = round(_clip(_safe_float(llm_analysis.get("ats_score"), deterministic_score), 0.0, 100.0), 2)

    recommendations = build_recommendations(component_scores, [], missing_sections)
    if llm_analysis and llm_analysis.get("improvement_actions"):
        recommendations = (llm_analysis["improvement_actions"] + recommendations)[:6]

    missing_keywords = (llm_analysis or {}).get("missing_keywords", [])
    grade = (
        "A" if ats_score >= 85 else
        "B" if ats_score >= 70 else
        "C" if ats_score >= 55 else
        "D" if ats_score >= 40 else
        "F"
    )

    # Mistakes detection
    mistakes = detect_mistakes(cv_text, component_scores, missing_sections)
    if llm_analysis and llm_analysis.get("critical_mistakes"):
        llm_mistakes = [x for x in llm_analysis["critical_mistakes"] if x]
        seen = set(mistakes)
        for m in llm_mistakes:
            if m not in seen:
                mistakes.append(m)
                seen.add(m)

    return {
        "ats_score": ats_score,
        "grade": grade,
        "deterministic_score": deterministic_score,
        "component_scores": {k: round(v, 2) for k, v in component_scores.items()},
        "experience": {
            "estimated_years_from_cv": round(estimated_years, 2),
        },
        "keyword_coverage": {
            "required_keywords_evaluated": len(missing_keywords),
            "matched": 0,
            "missing": len(missing_keywords),
            "evaluated_keywords": missing_keywords,
            "matched_keywords": [],
            "missing_keywords": missing_keywords,
            "matched_top": [],
            "missing_top": missing_keywords[:20]
        },
        "missing_sections": missing_sections,
        "recommendations": recommendations,
        "mistakes": mistakes[:8],
        "llm": {
            "used": llm_used,
            "model": (llm_analysis or {}).get("model"),
            "summary": (llm_analysis or {}).get("summary"),
            "strengths": (llm_analysis or {}).get("strengths", []),
            "gaps": (llm_analysis or {}).get("gaps", []),
            "raw_alignment_score": (llm_analysis or {}).get("ats_score"),
            "confidence": (llm_analysis or {}).get("confidence"),
        }
    }


def build_debug_info_cv(cv_text, preview_len=700):
    normalized_cv = normalize_text(cv_text)
    cv_tokens = [t for t in tokenize(normalized_cv) if t not in STOP_WORDS]
    year_estimate, year_details = estimate_experience_years(cv_text, return_details=True)
    section_status = detect_sections(cv_text)
    token_freq = Counter(cv_tokens)
    top_cv_tokens = [tok for tok, _ in token_freq.most_common(20)]

    return {
        "cv_characters": len(cv_text),
        "cv_words": len(cv_text.split()),
        "cv_token_count": len(cv_tokens),
        "cv_preview": cv_text[:preview_len],
        "top_cv_tokens": top_cv_tokens,
        "year_detection": {
            "estimated_experience_years": year_estimate,
            **year_details
        },
        "section_detection": section_status,
    }


def build_debug_info(cv_text, jd_text, keyword_limit=None, preview_len=700):
    normalized_cv = normalize_text(cv_text)
    normalized_jd = normalize_text(jd_text)

    cv_tokens = [t for t in tokenize(normalized_cv) if t not in STOP_WORDS]
    jd_tokens = [t for t in tokenize(normalized_jd) if t not in STOP_WORDS]

    year_estimate, year_details = estimate_experience_years(cv_text, return_details=True)
    section_status = detect_sections(cv_text)

    jd_keywords = extract_jd_keywords(jd_text, max_keywords=keyword_limit)
    _, matched_keywords, missing_keywords = keyword_match_score(cv_text, jd_keywords)

    token_freq = Counter(cv_tokens)
    top_cv_tokens = [tok for tok, _ in token_freq.most_common(20)]

    return {
        "cv_characters": len(cv_text),
        "cv_words": len(cv_text.split()),
        "cv_token_count": len(cv_tokens),
        "jd_characters": len(jd_text),
        "jd_words": len(jd_text.split()),
        "jd_token_count": len(jd_tokens),
        "cv_preview": cv_text[:preview_len],
        "top_cv_tokens": top_cv_tokens,
        "year_detection": {
            "estimated_experience_years": year_estimate,
            **year_details
        },
        "section_detection": section_status,
        "keyword_debug": {
            "keyword_limit": keyword_limit,
            "jd_keywords_sample": jd_keywords[:20],
            "matched_sample": matched_keywords[:20],
            "missing_sample": missing_keywords[:20]
        }
    }


def read_jd_text(args):
    if args.jd:
        return args.jd

    if args.jd_url:
        return fetch_text_from_url(args.jd_url)

    if args.jd_file:
        if not os.path.exists(args.jd_file):
            raise FileNotFoundError(f"JD file not found: {args.jd_file}")
        with open(args.jd_file, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()

    raise ValueError("Provide job description using --jd, --jd-url, or --jd-file")


def print_human_report(report):
    print("=" * 72)
    print("REAL-WORLD ATS ANALYSIS")
    print("=" * 72)
    print(f"ATS Score: {report['ats_score']}/100")
    print(f"Grade    : {report['grade']}")
    print("-" * 72)

    print("Component Scores")
    for name, value in report["component_scores"].items():
        print(f"  {name:20s} {value:6.2f}")

    print("-" * 72)
    exp = report.get("experience", {})
    print("Experience Fit")
    expected = exp.get("expected_years_from_jd")
    if expected is not None:
        print(f"  Expected (JD): {expected} years")
    print(f"  Estimated(CV): {exp.get('estimated_years_from_cv', 'N/A')} years")

    kw = report["keyword_coverage"]
    print("-" * 72)
    print("Keyword Coverage")
    print(f"  Evaluated: {kw['required_keywords_evaluated']}")
    print(f"  Matched  : {kw['matched']}")
    print(f"  Missing  : {kw['missing']}")
    if kw["missing_top"]:
        print("  Top Missing:")
        for item in kw["missing_top"][:12]:
            print(f"    - {item}")

    if report["missing_sections"]:
        print("-" * 72)
        print("Missing Sections")
        for section in report["missing_sections"]:
            print(f"  - {section}")

    print("-" * 72)
    print("Priority Fixes")
    for i, rec in enumerate(report["recommendations"], start=1):
        print(f"  {i}. {rec}")
    print("=" * 72)


def print_debug_report(debug_info):
    print("-" * 72)
    print("DEBUG DIAGNOSTICS")
    print("-" * 72)
    print("Text Stats")
    print(f"  CV: chars={debug_info.get('cv_characters','?')}, words={debug_info.get('cv_words','?')}, tokens={debug_info.get('cv_token_count','?')}")
    # JD stats only present in full JD analysis debug
    if debug_info.get("jd_characters") is not None:
        print(f"  JD: chars={debug_info['jd_characters']}, words={debug_info['jd_words']}, tokens={debug_info['jd_token_count']}")
    else:
        print("  JD: N/A (CV-only analysis)")

    yd = debug_info.get("year_detection", {})
    print("Year Detection")
    print(f"  Estimated years: {yd.get('estimated_experience_years', '?')}")
    print(f"  Explicit mentions: {yd.get('explicit_year_mentions', [])[:12]}")
    print(f"  Date spans: {yd.get('detected_date_spans', [])[:12]}")

    sd = debug_info.get("section_detection", {})
    print("Section Detection")
    print(f"  Required found  : {sd.get('required_found', [])}")
    print(f"  Required missing: {sd.get('required_missing', [])}")
    print(f"  Optional found  : {sd.get('optional_found', [])}")

    kd = debug_info.get("keyword_debug")
    if kd:
        print("Keyword Debug")
        print(f"  JD sample     : {kd.get('jd_keywords_sample', [])[:10]}")
        print(f"  Matched sample: {kd.get('matched_sample', [])[:10]}")
        print(f"  Missing sample: {kd.get('missing_sample', [])[:10]}")

    print("CV Preview")
    print(f"  {debug_info.get('cv_preview', '')}")
    print("-" * 72)


def build_parser():
    parser = argparse.ArgumentParser(description="Analyze CV against Job Description and estimate ATS score.")
    parser.add_argument("--cv", required=True, help="Path to CV PDF")
    parser.add_argument("--jd", help="Job description text")
    parser.add_argument("--jd-url", help="Job description URL (web page or JSON endpoint)")
    parser.add_argument("--jd-file", help="Path to text file containing job description")
    parser.add_argument("--keyword-limit", type=int, default=0, help="Number of JD keywords/phrases to evaluate. Use 0 to analyze the full JD keyword set.")
    parser.add_argument("--allow-non-cv", action="store_true", help="Bypass CV-only validation and analyze any PDF text")
    parser.add_argument("--debug", action="store_true", help="Print extraction diagnostics for debugging low scores")
    parser.add_argument("--debug-preview-len", type=int, default=700, help="Character count for CV preview in debug mode")
    parser.add_argument("--json", action="store_true", help="Print output as JSON")
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    jd_text = read_jd_text(args)
    cv_text = extract_text_from_pdf(args.cv)

    cv_validation = assess_cv_document(cv_text)
    if not cv_validation["is_cv"] and not args.allow_non_cv:
        reason_text = "; ".join(cv_validation["reasons"])
        raise ValueError(
            "Input PDF does not look like a CV/Resume. "
            f"Validation confidence={cv_validation['confidence']}. Reasons: {reason_text}. "
            "Use --allow-non-cv to bypass this guard."
        )

    report = analyze_cv_against_jd(cv_text, jd_text, keyword_limit=args.keyword_limit)
    report["cv_validation"] = cv_validation

    if args.debug:
        report["debug"] = build_debug_info(
            cv_text,
            jd_text,
            keyword_limit=args.keyword_limit,
            preview_len=args.debug_preview_len
        )

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print_human_report(report)
        if args.debug:
            print_debug_report(report["debug"])


if __name__ == "__main__":
    main()
